"""``gsi`` — X.509 proxy authentication, in pure Python.

The wire format is XrdSut's bucket buffer: the NUL-terminated name ``"gsi"``,
a big-endian step code, then type-length-value buckets terminated by a zero
type. The handshake this implements is the two-round unsigned-Diffie-Hellman
path, which is what a stock client negotiates when it advertises a version
below ``XrdSecgsiVersDHsigned``:

1. **kXGC_certreq** — the client names its crypto module, its version, the CA
   hash the server asked for, and a random tag, with a nested message holding
   the tag the *server* must sign.
2. **kXGS_cert** — the server answers with its Diffie-Hellman public blob and
   a random tag of its own.
3. **kXGC_cert** — the client agrees an AES-128 session key over that group,
   signs the server's tag with the proxy's private key (proof of possession),
   and returns its own public value plus the proxy chain, encrypted under the
   session key.

Everything it needs is here or in :mod:`xrdclient.crypto`: DH is
``pow(g, x, p)`` on Python integers, AES-CBC and RSA are
:mod:`xrdclient.crypto.aes` and :mod:`xrdclient.crypto.rsa`, and the proxy is read by
:mod:`xrdclient.crypto.x509`. There is no ``cryptography`` dependency and no
extra to install.

**Not implemented:** the signed-DH path (the server must offer ``kXRS_puk``,
not ``kXRS_cipher``) and X.509 delegation (``kXGS_pxyreq``). Both are
refused by name rather than silently mis-answered.

Translated from go-hep ``xrootd/xrdproto/auth/gsi`` and ``XrdSecgsi``.
"""

from __future__ import annotations

import binascii
import os
import struct
from dataclasses import dataclass

from .._compat import SLOTS
from .._log import get_logger
from ..config import Config
from ..crypto.aes import cbc_encrypt
from ..crypto.der import DERError, parse, read_integer
from ..crypto.rsa import RSAPrivateKey, pem_blocks
from ..crypto.x509 import ProxyCredential, default_proxy_path, load_proxy
from ..errors import CredentialError
from .base import Credential, Offer
from .prompt import Ask, humanise

__all__ = [
    "GSICredential",
    "Bucket",
    "encode_message",
    "decode_message",
    "find_bucket",
    "build_certreq",
    "build_cert_response",
    "parse_dh_parameters",
    "parse_peer_blob",
    "encode_public_blob",
    "session_key",
    "PeerPublic",
    "STEP_CLIENT_CERTREQ",
    "STEP_CLIENT_CERT",
    "STEP_SERVER_CERT",
    "STEP_SERVER_PXYREQ",
    "BUCKET_CRYPTOMOD",
    "BUCKET_MAIN",
    "BUCKET_PUK",
    "BUCKET_CIPHER",
    "BUCKET_RTAG",
    "BUCKET_SIGNED_RTAG",
    "BUCKET_VERSION",
    "BUCKET_CLNT_OPTS",
    "BUCKET_X509",
    "BUCKET_ISSUER_HASH",
    "BUCKET_CIPHER_ALG",
    "BUCKET_MD_ALG",
]

_log = get_logger(__name__)

# -- steps: server messages are kXGS_*, client messages kXGC_* --------------
STEP_SERVER_INIT = 2000
STEP_SERVER_CERT = 2001
STEP_SERVER_PXYREQ = 2002
STEP_CLIENT_CERTREQ = 1000
STEP_CLIENT_CERT = 1001
STEP_CLIENT_SIGPXY = 1002

# -- XrdSutBucket type codes -----------------------------------------------
BUCKET_NONE = 0
BUCKET_CRYPTOMOD = 3000
BUCKET_MAIN = 3001
BUCKET_PUK = 3004
BUCKET_CIPHER = 3005
BUCKET_RTAG = 3006
BUCKET_SIGNED_RTAG = 3007
BUCKET_USER = 3008
BUCKET_VERSION = 3014
BUCKET_CLNT_OPTS = 3019
BUCKET_X509 = 3022
BUCKET_ISSUER_HASH = 3023
BUCKET_X509_REQ = 3024
BUCKET_CIPHER_ALG = 3025
BUCKET_MD_ALG = 3026

#: Advertised so the server chooses the unsigned-DH path. Anything at or above
#: ``XrdSecgsiVersDHsigned`` (10400) selects signed DH, which is not written.
VERSION_UNSIGNED_DH = 10300
#: A stock client's default options, with proxy delegation off.
CLIENT_OPTS_DEFAULT = 0x80
#: AES-128: the session key is the leading 16 bytes of the DH shared secret.
SESSION_KEY_LEN = 16
RTAG_LEN = 8

_NAME = b"gsi\x00"
_BPUB = b"---BPUB---"
#: The reference encoder drops the final dash when it writes the closing
#: delimiter, so matching on nine bytes is what actually parses.
_EPUB = b"---EPUB--"


@dataclass(frozen=True, **SLOTS)
class Bucket:
    """One type-length-value element of a GSI message."""

    type: int
    data: bytes

    def __repr__(self) -> str:
        return f"Bucket(type={self.type}, len={len(self.data)})"


def encode_message(step: int, buckets: list[Bucket] | tuple[Bucket, ...]) -> bytes:
    """``"gsi\\0"``, the step, the buckets, the terminator."""
    out = bytearray(_NAME)
    out += struct.pack(">I", step)
    for bucket in buckets:
        out += struct.pack(">II", bucket.type, len(bucket.data))
        out += bucket.data
    out += struct.pack(">I", BUCKET_NONE)
    return bytes(out)


def decode_message(data: bytes) -> tuple[int, list[Bucket]]:
    """Split a GSI message into its step code and buckets."""
    end = data.find(b"\x00")
    if end < 0:
        raise CredentialError("GSI message has no protocol name")
    pos = end + 1
    if pos + 4 > len(data):
        raise CredentialError("GSI message is too short for a step code")
    (step,) = struct.unpack_from(">I", data, pos)
    pos += 4
    buckets: list[Bucket] = []
    while pos + 4 <= len(data):
        (kind,) = struct.unpack_from(">I", data, pos)
        pos += 4
        if kind == BUCKET_NONE:
            break
        if pos + 4 > len(data):
            raise CredentialError(f"GSI bucket {kind} has a truncated length")
        (length,) = struct.unpack_from(">I", data, pos)
        pos += 4
        if length > len(data) - pos:
            raise CredentialError(
                f"GSI bucket {kind} claims {length} bytes, {len(data) - pos} available"
            )
        buckets.append(Bucket(kind, data[pos : pos + length]))
        pos += length
    return step, buckets


def find_bucket(data: bytes, kind: int) -> bytes | None:
    """The first bucket of type ``kind``, or ``None``."""
    try:
        _step, buckets = decode_message(data)
    except CredentialError:
        return None
    for bucket in buckets:
        if bucket.type == kind:
            return bucket.data
    return None


# ---------------------------------------------------------------------------
# Diffie-Hellman over the server's group
# ---------------------------------------------------------------------------


@dataclass(frozen=True, **SLOTS)
class PeerPublic:
    """The server's DH blob: PEM parameters, the group, and its public value."""

    params_pem: bytes
    p: int
    g: int
    public: int


def parse_dh_parameters(pem: bytes) -> tuple[int, int]:
    """The prime and generator from a ``DH PARAMETERS`` PEM block."""
    blocks = [der for label, der in pem_blocks(pem) if label.endswith("PARAMETERS")]
    if not blocks:
        raise CredentialError("no PEM block in the server's DH parameters")
    try:
        element, _ = parse(blocks[0])
        fields = element.children()
        if len(fields) < 2:
            raise DERError("DHParameter needs a prime and a base")
        return read_integer(fields[0]), read_integer(fields[1])
    except DERError as exc:
        raise CredentialError(f"unreadable DH parameters: {exc}") from exc


def parse_peer_blob(blob: bytes) -> PeerPublic:
    """Split ``<PEM params>---BPUB---<hex>---EPUB---`` into its parts."""
    start = blob.find(_BPUB)
    end = blob.find(_EPUB, start + len(_BPUB) if start >= 0 else 0)
    if start < 0 or end <= start + len(_BPUB):
        raise CredentialError("malformed GSI DH public blob")
    params = blob[:start]
    try:
        public = int(blob[start + len(_BPUB) : end].strip(), 16)
    except ValueError as exc:
        raise CredentialError("the DH public value is not hexadecimal") from exc
    prime, generator = parse_dh_parameters(params)
    return PeerPublic(params_pem=bytes(params), p=prime, g=generator, public=public)


def encode_public_blob(params_pem: bytes, public: int) -> bytes:
    """The client's blob: the server's parameters echoed, then our public value."""
    hexed = binascii.hexlify(public.to_bytes((public.bit_length() + 7) // 8, "big")).upper()
    return params_pem + _BPUB + hexed + b"---EPUB---"


def session_key(peer: PeerPublic, private: int, length: int = SESSION_KEY_LEN) -> bytes:
    """The leading ``length`` bytes of the DH shared secret.

    XrdSecgsi's unsigned path takes the secret's *minimal* big-endian form —
    leading zeros stripped, as OpenSSL's ``DH_compute_key`` returns it — and
    uses its first bytes directly, with no KDF.
    """
    secret = pow(peer.public, private, peer.p)
    raw = secret.to_bytes((secret.bit_length() + 7) // 8, "big")
    if len(raw) < length:
        raise CredentialError(f"DH shared secret is {len(raw)} bytes, need {length}")
    return raw[:length]


# ---------------------------------------------------------------------------
# The two client rounds
# ---------------------------------------------------------------------------


def build_certreq(
    *,
    cryptomod: str = "ssl",
    version: int = VERSION_UNSIGNED_DH,
    issuer_hash: str = "",
    options: int = CLIENT_OPTS_DEFAULT,
    rtag: bytes,
) -> bytes:
    """The first client message, ``kXGC_certreq``. No cryptography involved."""
    inner = encode_message(STEP_CLIENT_CERTREQ, [Bucket(BUCKET_RTAG, rtag)])
    return encode_message(
        STEP_CLIENT_CERTREQ,
        [
            Bucket(BUCKET_CRYPTOMOD, (cryptomod or "ssl").encode("ascii")),
            Bucket(BUCKET_VERSION, struct.pack(">I", version)),
            Bucket(BUCKET_ISSUER_HASH, issuer_hash.encode("ascii")),
            Bucket(BUCKET_CLNT_OPTS, struct.pack(">I", options)),
            Bucket(BUCKET_MAIN, inner),
        ],
    )


def build_cert_response(
    challenge: bytes,
    chain_pem: bytes,
    key: RSAPrivateKey,
    *,
    private: int | None = None,
    rtag: bytes | None = None,
) -> bytes:
    """Answer ``kXGS_cert`` with ``kXGC_cert``.

    ``private`` and ``rtag`` are injectable so the encoding can be pinned by
    a test; leave them unset in production and they are drawn from
    :func:`os.urandom`.
    """
    blob = find_bucket(challenge, BUCKET_PUK)
    if blob is None:
        if find_bucket(challenge, BUCKET_CIPHER) is not None:
            raise CredentialError(
                "the server chose GSI signed-DH; this client implements unsigned-DH only"
            )
        raise CredentialError("the server's GSI challenge carries no DH public key")
    peer = parse_peer_blob(blob)

    if private is None:
        # A private exponent in [2, p-2]; the group is the server's choice.
        private = 2 + int.from_bytes(os.urandom((peer.p.bit_length() + 7) // 8), "big") % (
            peer.p - 3
        )
    secret = session_key(peer, private)

    inner = [Bucket(BUCKET_X509, chain_pem)]
    main = find_bucket(challenge, BUCKET_MAIN)
    server_tag = find_bucket(main, BUCKET_RTAG) if main is not None else None
    if server_tag:
        # Proof of possession: raw PKCS#1 v1.5 over the server's tag.
        inner.append(Bucket(BUCKET_SIGNED_RTAG, key.sign(server_tag)))
    inner.append(Bucket(BUCKET_RTAG, rtag if rtag is not None else os.urandom(RTAG_LEN)))

    encrypted = cbc_encrypt(secret, encode_message(STEP_CLIENT_CERT, inner))
    return encode_message(
        STEP_CLIENT_CERT,
        [
            Bucket(BUCKET_CRYPTOMOD, b"ssl"),
            Bucket(BUCKET_PUK, encode_public_blob(peer.params_pem, pow(peer.g, private, peer.p))),
            Bucket(BUCKET_CIPHER_ALG, b"aes-128-cbc"),
            Bucket(BUCKET_MD_ALG, b"sha256"),
            Bucket(BUCKET_MAIN, encrypted),
        ],
    )


# ---------------------------------------------------------------------------
# The mechanism
# ---------------------------------------------------------------------------


class GSICredential(Credential):
    """``gsi`` — an X.509 proxy from ``$X509_USER_PROXY``."""

    __slots__ = ("proxy", "cryptomod", "issuer_hash", "_rtag")
    name = "gsi"

    def __init__(self, proxy: ProxyCredential, *, cryptomod: str = "ssl", issuer_hash: str = ""):
        self.proxy = proxy
        self.cryptomod = cryptomod or "ssl"
        self.issuer_hash = issuer_hash
        self._rtag = b""

    @property
    def identity(self) -> str:
        """Who this proxy says you are, with the proxy CNs stripped."""
        return self.proxy.identity

    def initial(self) -> bytes:
        if self.proxy.expired:
            raise CredentialError(
                f"the X.509 proxy {self.proxy.path or '<memory>'} expired "
                f"{-self.proxy.remaining() / 3600:.1f} hours ago; renew it"
            )
        self._rtag = os.urandom(RTAG_LEN)
        return build_certreq(
            cryptomod=self.cryptomod,
            issuer_hash=self.issuer_hash,
            rtag=self._rtag,
        )

    def step(self, challenge: bytes) -> bytes | None:
        step, _buckets = decode_message(challenge)
        if step == STEP_SERVER_CERT:
            key = self.proxy.key
            if not isinstance(key, RSAPrivateKey):
                raise CredentialError("the proxy's private key is not RSA")
            return build_cert_response(challenge, self.proxy.pem(), key)
        if step == STEP_SERVER_PXYREQ:
            raise CredentialError(
                "the server asked for X.509 delegation, which this client does not do"
            )
        raise CredentialError(f"unexpected GSI step {step} from the server")

    @classmethod
    def available(
        cls, offer: Offer, config: Config, *, username: str, host: str
    ) -> GSICredential | None:
        path = default_proxy_path(config)
        if not os.path.isfile(path):
            return None
        try:
            proxy = load_proxy(path)
        except (OSError, ValueError) as exc:
            _log.debug("unusable X.509 proxy %s: %s", path, exc)
            return None
        if not isinstance(proxy.key, RSAPrivateKey):
            return None
        if proxy.expired:
            _log.debug("X.509 proxy %s expired", path)
            return None
        options = offer.options()
        return cls(proxy, cryptomod=options.get("c", "ssl"), issuer_hash=options.get("ca", ""))

    @classmethod
    def missing(cls, offer: Offer, config: Config, *, username: str, host: str) -> Ask | None:
        path = default_proxy_path(config)
        if not os.path.isfile(path):
            reason = f"there is no file at {path}"
        else:
            try:
                proxy = load_proxy(path)
            except (OSError, ValueError) as exc:
                reason = f"{path} could not be read as a proxy: {exc}"
            else:
                if proxy.expired:
                    reason = f"the proxy in {path} expired {humanise(proxy.remaining())} ago"
                elif not isinstance(proxy.key, RSAPrivateKey):
                    reason = f"the key in {path} is not RSA, which is all GSI does"
                else:
                    return None  # perfectly good: available() said no for another reason
        return Ask(
            mechanism=cls.name,
            what="an X.509 proxy",
            reason=reason,
            hint="voms-proxy-init -voms <your VO>, or point $X509_USER_PROXY at one",
            prompt="path to a proxy file",
            host=host,
        )

    @classmethod
    def using(cls, answer: str, config: Config) -> Config:
        return config.evolve(proxy=os.path.expanduser(answer))

    def __repr__(self) -> str:
        return f"GSICredential(identity={self.identity!r})"
