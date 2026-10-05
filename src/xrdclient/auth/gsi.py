"""``gsi`` — X.509 proxy authentication and delegation, in pure Python.

The wire format is XrdSut's bucket buffer: the NUL-terminated name ``"gsi"``,
a big-endian step code, then type-length-value buckets terminated by a zero
type. The handshake is two rounds, three with delegation:

1. **kXGC_certreq** — the client names its crypto module, its version, the
   hashes of the CA that issued *its own* certificate (``new.0|old.0``, the
   names that CA has in a certificate directory - the server loads it by them
   to verify the chain it will be sent), its options (whether it will
   delegate) and a random tag, with a nested message holding the tag the
   *server* must sign.
2. **kXGS_cert** — the server answers with its certificate, its
   Diffie-Hellman public blob, a random tag of its own, and the message
   digests it supports.
3. **kXGC_cert** — the client agrees an AES-128 session key over that group,
   picks a digest from the server's list (``sha256`` where offered, else
   ``sha1`` - dCache's doors offer ``sha1:md5`` alone), signs the server's
   tag with the proxy's private key (proof of possession), and returns its
   own public value plus the proxy chain, encrypted under the session key.
4. **kXGS_pxyreq** / **kXGC_sigpxy** — only when the client offered to
   delegate: the server sends a certificate request for a key pair it made,
   and the client signs it into a proxy one link below its own
   (:mod:`xrdclient.crypto.delegation`). The client's private key never leaves
   the client.

Which Diffie-Hellman the exchange uses is the server's version's to decide,
as it is for the stock client. From ``XrdSecgsiVersDHsigned`` (10400) each
side *signs* its DH blob with its private key - the server's is checked
against the key in its certificate - the shared secret keeps its leading
zeros, and every encrypted buffer starts with a fresh IV. Older servers get
the original unsigned exchange. Delegation needs the signed one: a server
refuses to take a proxy over an exchange it cannot attribute, and so does
this client, which in addition checks the server's certificate against the
CA directory and the host name (:mod:`xrdclient.crypto.trust`) before it signs
anything for it.

Protocol adapters are here or in :mod:`xrdclient.crypto`: DH is
``pow(g, x, p)`` on Python integers, AES-CBC uses ``cryptography`` through
:mod:`xrdclient.crypto.aes`, and legacy raw-RSA operations remain in
:mod:`xrdclient.crypto.rsa`. Proxy parsing uses :mod:`xrdclient.crypto.x509`.
These are included in the default installation; no GSI extra is needed.

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
from ..crypto.aes import cbc_decrypt, cbc_encrypt
from ..crypto.delegation import DelegationError, load_proxy_request, sign_proxy_request
from ..crypto.der import DERError, parse, read_integer
from ..crypto.rsa import RSAPrivateKey, RSAPublicKey, pem_blocks
from ..crypto.trust import TrustError, verify_server
from ..crypto.x509 import (
    ProxyCredential,
    default_proxy_path,
    issuer_hashes,
    load_certificates,
    load_proxy,
)
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
    "answer_certificate",
    "answer_proxy_request",
    "Session",
    "seal",
    "unseal",
    "parse_dh_parameters",
    "parse_peer_blob",
    "encode_public_blob",
    "session_key",
    "PeerPublic",
    "Delegation",
    "STEP_CLIENT_CERTREQ",
    "STEP_CLIENT_CERT",
    "STEP_SERVER_CERT",
    "STEP_SERVER_PXYREQ",
    "STEP_CLIENT_SIGPXY",
    "BUCKET_CRYPTOMOD",
    "BUCKET_MAIN",
    "BUCKET_PUK",
    "BUCKET_CIPHER",
    "BUCKET_RTAG",
    "BUCKET_SIGNED_RTAG",
    "BUCKET_VERSION",
    "BUCKET_CLNT_OPTS",
    "BUCKET_X509",
    "BUCKET_X509_REQ",
    "BUCKET_MESSAGE",
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
BUCKET_MESSAGE = 3011
BUCKET_VERSION = 3014
BUCKET_CLNT_OPTS = 3019
BUCKET_X509 = 3022
BUCKET_ISSUER_HASH = 3023
BUCKET_X509_REQ = 3024
BUCKET_CIPHER_ALG = 3025
BUCKET_MD_ALG = 3026

#: Advertised to a server older than signed DH: the original exchange.
VERSION_UNSIGNED_DH = 10300
#: ``XrdSecgsiVersDHsigned``: advertised to - and by - a server that signs its
#: DH blob, which selects the signed exchange on both ends. The client claims
#: no later version, so a newer server expects the random tag signed raw, as
#: this client signs it.
VERSION_SIGNED_DH = 10400
#: A stock client's default options (``kOptsCreatePxy``), with delegation off.
CLIENT_OPTS_DEFAULT = 0x80
#: ``kOptsDlgPxy | kOptsSigReq``: "ask me for a delegated proxy, I will sign
#: the request" - what ``XrdSecGSIDELEGPROXY=1`` adds.
CLIENT_OPTS_DELEGATE = 0x01 | 0x04
#: ``gNoPadTag``: a crypto module name ending in it cannot pad a DH secret.
NOPAD = "nopad"
#: ``EVP_MAX_IV_LENGTH``: the IV the signed exchange puts before each buffer.
IV_LEN = 16
#: What the stock client tells a server whose proxy request it will not sign.
REFUSAL = "Not allowed to sign proxy requests"
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


def session_key(
    peer: PeerPublic, private: int, length: int = SESSION_KEY_LEN, *, padded: bool = False
) -> bytes:
    """The leading ``length`` bytes of the DH shared secret.

    XrdSecgsi's unsigned path takes the secret's *minimal* big-endian form —
    leading zeros stripped, as OpenSSL's ``DH_compute_key`` returns it — and
    uses its first bytes directly, with no KDF. The signed path, when both
    ends can, keeps the secret at the full width of the prime
    (``EVP_PKEY_CTX_set_dh_pad``), leading zeros and all.
    """
    secret = pow(peer.public, private, peer.p)
    width = peer.p.bit_length() if padded else secret.bit_length()
    raw = secret.to_bytes((width + 7) // 8, "big")
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


@dataclass(frozen=True, **SLOTS)
class Session:
    """What the certificate round leaves for a delegation round after it."""

    key: bytes
    use_iv: bool
    server_pem: bytes = b""


def seal(key: bytes, plain: bytes, *, use_iv: bool, iv: bytes | None = None) -> bytes:
    """AES-CBC under the session key; the signed exchange prefixes a fresh IV."""
    if not use_iv:
        return cbc_encrypt(key, plain)
    vector = iv if iv is not None else os.urandom(IV_LEN)
    return vector + cbc_encrypt(key, plain, vector)


def unseal(key: bytes, data: bytes, *, use_iv: bool) -> bytes:
    """The inverse of :func:`seal`."""
    try:
        if not use_iv:
            return cbc_decrypt(key, data)
        return cbc_decrypt(key, data[IV_LEN:], data[:IV_LEN])
    except ValueError as exc:
        raise CredentialError(f"cannot decrypt the server's GSI message: {exc}") from exc


def _server_key(server_pem: bytes | None) -> RSAPublicKey:
    certificates = load_certificates(server_pem or b"")
    key = certificates[0].public_key if certificates else None
    if key is None:
        raise CredentialError("the server signed its DH parameters but sent no RSA certificate")
    return key


def _unsign(blob: bytes, server_pem: bytes | None) -> bytes:
    """The server's DH blob, recovered with the key in its certificate."""
    try:
        return _server_key(server_pem).decrypt_public(blob)
    except ValueError as exc:
        raise CredentialError(
            "the server's DH parameters are not signed by its certificate's key"
        ) from exc


def _peer_blob(challenge: bytes) -> tuple[bytes, bool]:
    """The server's DH blob, and whether it came signed."""
    signed = find_bucket(challenge, BUCKET_CIPHER)
    if signed is not None:
        return _unsign(signed, find_bucket(challenge, BUCKET_X509)), True
    blob = find_bucket(challenge, BUCKET_PUK)
    if blob is None:
        raise CredentialError("the server's GSI challenge carries no DH public key")
    return blob, False


def _signed_tag(message: bytes | None, key: RSAPrivateKey) -> list[Bucket]:
    """Proof of possession: raw PKCS#1 v1.5 over the tag the server sent, if it sent one."""
    tag = find_bucket(message, BUCKET_RTAG) if message is not None else None
    return [Bucket(BUCKET_SIGNED_RTAG, key.sign(tag))] if tag else []


#: The message digests this client can sign with, most preferred first.
CLIENT_DIGESTS = ("sha256", "sha1")


def choose_digest(offered: bytes | None) -> bytes:
    """The digest to answer ``kXGS_cert`` with: ours, the first the server lists.

    The server says what it accepts in the challenge's ``kXRS_md_alg`` bucket
    (``sha256`` from a current xrootd, ``sha1:md5`` from dCache and older
    xrootd) and refuses a reply naming anything else. A server that sends no
    list predates the choice and gets ``sha256``, which is what this client
    always said before it read the list.
    """
    if not offered:
        return CLIENT_DIGESTS[0].encode()
    names = [name.strip().lower() for name in offered.decode("ascii", "replace").split(":")]
    for digest in CLIENT_DIGESTS:
        if digest in names:
            return digest.encode()
    raise CredentialError(
        f"the server's GSI accepts only the digests {':'.join(names)}, "
        f"and this client signs with {':'.join(CLIENT_DIGESTS)}"
    )


def client_ca_hashes(proxy: ProxyCredential) -> str:
    """``new.0|old.0``: the CA-directory names of the authority behind ``proxy``.

    That authority signed the end-entity certificate - the first link in the
    chain that is neither a proxy nor an anchor. Empty when the chain has
    no such link, or it cannot be read far enough to hash.
    """
    for certificate in proxy.chain:
        if certificate.is_proxy or certificate.is_anchor:
            continue
        try:
            new, old = issuer_hashes(certificate)
        except (DERError, ValueError, IndexError):
            return ""
        return f"{new}.0|{old}.0"
    return ""


def _outer_cert(
    peer: PeerPublic,
    public: int,
    key: RSAPrivateKey,
    signed: bool,
    main: bytes,
    digest: bytes = b"sha256",
) -> list[Bucket]:
    blob = encode_public_blob(peer.params_pem, public)
    if signed:
        agreement = [
            Bucket(BUCKET_CIPHER, key.encrypt_private(blob)),
            Bucket(BUCKET_PUK, key.public.pem()),
            Bucket(BUCKET_CIPHER_ALG, b"aes-128-cbc#%d" % IV_LEN),
        ]
    else:
        agreement = [Bucket(BUCKET_PUK, blob), Bucket(BUCKET_CIPHER_ALG, b"aes-128-cbc")]
    return [
        Bucket(BUCKET_CRYPTOMOD, b"ssl"),
        *agreement,
        Bucket(BUCKET_MD_ALG, digest),
        Bucket(BUCKET_MAIN, main),
    ]


def answer_certificate(
    challenge: bytes,
    chain_pem: bytes,
    key: RSAPrivateKey,
    *,
    private: int | None = None,
    rtag: bytes | None = None,
    iv: bytes | None = None,
    padded: bool = True,
) -> tuple[bytes, Session]:
    """Answer ``kXGS_cert`` with ``kXGC_cert``, in whichever DH the server chose.

    Returns the message and the :class:`Session` a delegation round needs.
    ``private``, ``rtag`` and ``iv`` are injectable so the encoding can be
    pinned by a test; leave them unset in production and they are drawn from
    :func:`os.urandom`. ``padded=False`` is for a server whose crypto module
    says ``nopad``.
    """
    blob, signed = _peer_blob(challenge)
    peer = parse_peer_blob(blob)
    digest = choose_digest(find_bucket(challenge, BUCKET_MD_ALG))
    if private is None:
        # A private exponent in [2, p-2]; the group is the server's choice.
        private = 2 + int.from_bytes(os.urandom((peer.p.bit_length() + 7) // 8), "big") % (
            peer.p - 3
        )
    secret = session_key(peer, private, padded=signed and padded)

    inner = [
        Bucket(BUCKET_X509, chain_pem),
        *_signed_tag(find_bucket(challenge, BUCKET_MAIN), key),
        Bucket(BUCKET_RTAG, rtag if rtag is not None else os.urandom(RTAG_LEN)),
    ]
    main = seal(secret, encode_message(STEP_CLIENT_CERT, inner), use_iv=signed, iv=iv)
    outer = _outer_cert(peer, pow(peer.g, private, peer.p), key, signed, main, digest)
    server_pem = find_bucket(challenge, BUCKET_X509) or b""
    return encode_message(STEP_CLIENT_CERT, outer), Session(secret, signed, server_pem)


def build_cert_response(
    challenge: bytes,
    chain_pem: bytes,
    key: RSAPrivateKey,
    *,
    private: int | None = None,
    rtag: bytes | None = None,
) -> bytes:
    """Answer ``kXGS_cert`` with ``kXGC_cert``: :func:`answer_certificate`'s message alone."""
    return answer_certificate(challenge, chain_pem, key, private=private, rtag=rtag)[0]


def _delegated(plain: bytes, proxy: ProxyCredential, key: RSAPrivateKey) -> Bucket:
    """The signed proxy for the server's request, or the reason there is none."""
    request = find_bucket(plain, BUCKET_X509_REQ)
    if request is None:
        return Bucket(BUCKET_MESSAGE, b"bucket with proxy request missing")
    try:
        issued = sign_proxy_request(load_proxy_request(request), proxy.certificate, key)
    except DelegationError as exc:
        _log.warning("not delegating the X.509 proxy: %s", exc)
        return Bucket(BUCKET_MESSAGE, f"problems signing the request: {exc}".encode())
    _log.debug("delegated a proxy for %s until %s", issued.subject, issued.not_after)
    return Bucket(BUCKET_X509, issued.pem())


def answer_proxy_request(
    challenge: bytes,
    session: Session,
    proxy: ProxyCredential,
    key: RSAPrivateKey,
    *,
    allowed: bool,
    iv: bytes | None = None,
) -> bytes:
    """Answer ``kXGS_pxyreq`` with ``kXGC_sigpxy``.

    With ``allowed`` the request is signed into a proxy below ``proxy``;
    without, the server is told so in words - the stock client's answer, which
    lets the login finish with nothing delegated.
    """
    main = find_bucket(challenge, BUCKET_MAIN)
    if main is None:
        raise CredentialError("the server's proxy request has no main buffer")
    plain = unseal(session.key, main, use_iv=session.use_iv)
    answer = _delegated(plain, proxy, key) if allowed else Bucket(BUCKET_MESSAGE, REFUSAL.encode())
    inner = [answer, *_signed_tag(plain, key)]
    sealed = seal(
        session.key, encode_message(STEP_CLIENT_SIGPXY, inner), use_iv=session.use_iv, iv=iv
    )
    return encode_message(
        STEP_CLIENT_SIGPXY, [Bucket(BUCKET_CRYPTOMOD, b"ssl"), Bucket(BUCKET_MAIN, sealed)]
    )


# ---------------------------------------------------------------------------
# The mechanism
# ---------------------------------------------------------------------------


@dataclass(frozen=True, **SLOTS)
class Delegation:
    """Whether, and to whom, the proxy may be delegated."""

    wanted: bool = False
    host: str = ""
    ca_path: str | None = None


def _server_version(options: dict[str, str]) -> int:
    """The ``v:`` of the offer; ``0`` - the oldest behaviour - when absent or garbled."""
    try:
        return int(options.get("v", "0"))
    except ValueError:
        return 0


class GSICredential(Credential):
    """``gsi`` — an X.509 proxy from ``$X509_USER_PROXY``."""

    __slots__ = (
        "proxy",
        "cryptomod",
        "issuer_hash",
        "server_version",
        "padded",
        "delegation",
        "_rtag",
        "_session",
        "_may_delegate",
    )
    name = "gsi"

    def __init__(
        self,
        proxy: ProxyCredential,
        *,
        cryptomod: str = "ssl",
        issuer_hash: str = "",
        server_version: int = 0,
        delegation: Delegation | None = None,
    ):
        self.proxy = proxy
        # The first module the server lists; "nopad" on it says the server
        # cannot pad a DH secret, which the signed exchange must then match.
        module = (cryptomod or "ssl").split("|")[0] or "ssl"
        self.padded = not module.endswith(NOPAD)
        self.cryptomod = module[: -len(NOPAD)] if not self.padded else module
        self.issuer_hash = issuer_hash
        self.server_version = server_version
        self.delegation = delegation or Delegation()
        self._rtag = b""
        self._session: Session | None = None
        self._may_delegate = False

    @property
    def identity(self) -> str:
        """Who this proxy says you are, with the proxy CNs stripped."""
        return self.proxy.identity

    @property
    def signed(self) -> bool:
        """Whether the server's version makes this the signed-DH exchange."""
        return self.server_version >= VERSION_SIGNED_DH

    def initial(self) -> bytes:
        if self.proxy.expired:
            raise CredentialError(
                f"the X.509 proxy {self.proxy.path or '<memory>'} expired "
                f"{-self.proxy.remaining() / 3600:.1f} hours ago; renew it"
            )
        self._rtag = os.urandom(RTAG_LEN)
        self._session, self._may_delegate = None, False
        return build_certreq(
            cryptomod=self.cryptomod + ("" if self.padded or not self.signed else NOPAD),
            version=VERSION_SIGNED_DH if self.signed else VERSION_UNSIGNED_DH,
            issuer_hash=self.issuer_hash,
            options=CLIENT_OPTS_DEFAULT | (CLIENT_OPTS_DELEGATE if self._offers() else 0),
            rtag=self._rtag,
        )

    def _offers(self) -> bool:
        """Whether to offer the server a delegated proxy."""
        if self.delegation.wanted and not self.signed:
            _log.warning(
                "not delegating the X.509 proxy: the server runs GSI %d, "
                "which predates the signed exchange delegation needs",
                self.server_version,
            )
        return self.delegation.wanted and self.signed

    def _key(self) -> RSAPrivateKey:
        key = self.proxy.key
        if not isinstance(key, RSAPrivateKey):
            raise CredentialError("the proxy's private key is not RSA")
        return key

    def step(self, challenge: bytes) -> bytes | None:
        step, _buckets = decode_message(challenge)
        if step == STEP_SERVER_CERT:
            message, self._session = answer_certificate(
                challenge, self.proxy.pem(), self._key(), padded=self.padded
            )
            self._may_delegate = self._offers() and self._trusted(self._session)
            return message
        if step == STEP_SERVER_PXYREQ:
            if self._session is None:
                raise CredentialError("the server asked for a proxy before the key exchange")
            return answer_proxy_request(
                challenge, self._session, self.proxy, self._key(), allowed=self._may_delegate
            )
        raise CredentialError(f"unexpected GSI step {step} from the server")

    def _trusted(self, session: Session) -> bool:
        """Whether the server proved to be the host it was dialled as."""
        try:
            verify_server(session.server_pem, self.delegation.host, self.delegation.ca_path)
        except TrustError as exc:
            _log.warning("not delegating the X.509 proxy to %s: %s", self.delegation.host, exc)
            return False
        return True

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
        return cls(
            proxy,
            cryptomod=options.get("c", "ssl"),
            # Our CA, not the server's: the server's own ``ca:`` names the
            # authority behind *its* certificate, and echoing it back makes a
            # server whose CA differs from ours build our chain on the wrong
            # anchor ("chain is inconsistent"). Its list is the fallback only
            # for a chain too odd to hash, which is what was sent before.
            issuer_hash=client_ca_hashes(proxy) or options.get("ca", ""),
            server_version=_server_version(options),
            delegation=Delegation(config.gsi_delegate, host, config.ca_path),
        )

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
