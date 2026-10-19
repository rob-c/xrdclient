"""``sss`` - Simple Shared Secret.

The credential is a 16-byte cleartext header followed by a Blowfish-CFB64
blob covering a 40-byte data header, a NAME type-length-value, and an IEEE
CRC-32 of both. Note the polynomial: SSS uses ``zlib.crc32``, *not* the
CRC-32C of paged I/O.

The key comes from a keytab written by ``xrdsssadmin`` and shared with the
server out of band; it is a symmetric secret in the clear on disk, which is
why :func:`read_keytab` holds the file to the same ownership and mode rules
as ``XrdSecsss`` does, and why a keytab failing them is reported loudly
rather than skipped. The mechanism is one round: :meth:`SSSCredential.step`
is never needed.
"""

from __future__ import annotations

import os
import struct
import time
import zlib
from dataclasses import dataclass

from cryptography.exceptions import UnsupportedAlgorithm
from xrdsec.credentials import read_private_file

from ..config import Config
from ..crypto.blowfish import Blowfish
from ..errors import CredentialError
from ..log import get_logger
from .base import Credential, Offer
from .prompt import Ask

__all__ = ["SSSCredential", "SSSKey", "build_credential", "default_keytab_path", "read_keytab"]

_log = get_logger(__name__)

#: The cleartext header: ``"sss\0"``, version, named-key length, the
#: encryption type and the big-endian key number the server looks up.
HDR_LEN = 16
#: The encrypted data header: a 32-byte nonce, a 4-byte generation time, and
#: padding up to the options byte at offset 39.
DATA_HDR_LEN = 40
#: Epoch SSS timestamps count from (2008-09-23T14:11:20Z).
BASE_TIME = 1222183880
#: The encryption-type byte for Blowfish (``'0'``), the only one sent.
ENC_BF32 = ord("0")
#: Options byte: take the identity from the data that follows, not the key.
OPT_USEDATA = 0x00
#: Type of the TLV that carries the user name.
TYPE_NAME = 0x01
#: Random bytes at the front of the encrypted part. With a zero IV, CFB
#: would otherwise turn the same name at the same second into the same
#: ciphertext; the nonce makes every credential distinct.
NONCE_LEN = 32


@dataclass(frozen=True, slots=True)
class SSSKey:
    """One keytab entry.

    ``id`` is the ``N:`` key number the server finds its copy of the secret
    by (``-1`` when the line has none); ``expires`` is a UNIX time, ``0`` for
    a key that never expires. The secret is kept out of :func:`repr`.
    """

    id: int
    secret: bytes
    name: str = ""
    user: str = ""
    group: str = ""
    expires: int = 0

    @property
    def expired(self) -> bool:
        """Whether the ``e:`` time has passed; a key with ``e:0`` never expires."""
        return self.expires != 0 and self.expires <= time.time()

    def __repr__(self) -> str:
        """Identifies the key without the secret, which would end up in debug logs."""
        return f"SSSKey(id={self.id}, name={self.name!r}, secret=<redacted>)"


def default_keytab_path(config: Config | None = None) -> str:
    """``$XrdSecSSSKT``, then ``$XrdSecsssKT``, then ``~/.xrd/sss.keytab``."""
    if config and config.keytab:
        return config.keytab
    for var in ("XrdSecSSSKT", "XrdSecsssKT"):
        val = os.environ.get(var)
        if val:
            return val
    return os.path.join(os.path.expanduser("~"), ".xrd", "sss.keytab")


def read_keytab(
    path: str, *, include_expired: bool = False, require_private: bool = True
) -> list[SSSKey]:
    """Parse an SSS keytab.

    Lines look like ``0 u:anon g:anon n:mykey N:1 c:... e:0 k:<hex>``; the
    leading field is the format version.

    A keytab holds shared secrets in the clear, so one that is not a regular
    file of this user's, or that group or others can read or write, is
    refused outright with :class:`~xrdclient.errors.InsecureCredentialError`
    (a :class:`PermissionError`), exactly as the C implementation refuses
    it. The checks are made on the open file, so the file checked is the
    file read. ``require_private=False`` is for inspecting a keytab you
    already know is exposed - never for authenticating with it.
    """
    if require_private:
        data = read_private_file(path, kind="SSS keytab")
    else:
        with open(path, "rb") as fh:
            data = fh.read()
    keys: list[SSSKey] = []
    for raw in data.decode("utf-8", "replace").splitlines():
        key = _keytab_line(raw)
        if key is not None and (include_expired or not key.expired):
            keys.append(key)
    return keys


def _attributes(fields: list[str]) -> dict[str, str]:
    """A keytab line's ``x:value`` fields by their one-letter key.

    A ``#`` field starts a trailing comment, so nothing after it counts;
    anything not shaped ``x:...`` is ignored rather than refused, as the
    reference parser does with fields it does not know.
    """
    attrs: dict[str, str] = {}
    for field in fields:
        if field.startswith("#"):
            break
        if len(field) > 1 and field[1] == ":":
            attrs[field[0]] = field[2:]
    return attrs


def _keytab_line(raw: str) -> SSSKey | None:
    """One keytab line as a key, or ``None`` for a comment, a blank, or no key.

    The leading field is the line format; only formats ``0`` and ``1`` are
    understood, and a line in any other is skipped rather than misread. A
    line without a ``k:`` secret cannot authenticate anything.
    """
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    fields = line.split()
    if fields[0] not in ("0", "1"):
        return None
    attrs = _attributes(fields[1:])
    secret = bytes.fromhex(attrs["k"]) if "k" in attrs else b""
    if not secret:
        return None
    return SSSKey(
        id=int(attrs.get("N", -1)),
        secret=secret,
        name=attrs.get("n", ""),
        user=attrs.get("u", ""),
        group=attrs.get("g", ""),
        expires=int(attrs.get("e", 0)),
    )


def build_credential(
    key: SSSKey,
    username: str,
    *,
    nonce: bytes | None = None,
    gen_time: int | None = None,
) -> bytes:
    """Mint a ``kXR_auth`` blob from ``key``.

    ``nonce`` and ``gen_time`` are injectable so the encoding can be pinned
    by a test; leave them unset in production.
    """
    if nonce is None:
        nonce = os.urandom(NONCE_LEN)
    elif len(nonce) != NONCE_LEN:
        raise ValueError(f"SSS nonce must be {NONCE_LEN} bytes, got {len(nonce)}")
    if gen_time is None:
        gen_time = int(time.time()) - BASE_TIME

    data_hdr = bytearray(DATA_HDR_LEN)
    data_hdr[0:NONCE_LEN] = nonce
    # Seconds since BASE_TIME, so the server can bound how long a captured
    # credential stays usable; the field is 32 bits wide, hence the mask.
    struct.pack_into(">I", data_hdr, 32, gen_time & 0xFFFFFFFF)
    data_hdr[39] = OPT_USEDATA

    user = (username or "xrd").encode("utf-8")
    # A name longer than 63 bytes is truncated rather than refused; the TLV
    # length is big-endian 16-bit, whose high byte is therefore always zero.
    ulen = min(len(user) + 1, 64)  # counts the trailing NUL
    tlv = bytes([TYPE_NAME, 0x00, ulen]) + user[: ulen - 1] + b"\x00"

    # The CRC covers the data header and the TLV, and is encrypted with them:
    # it is how the server tells a wrong key from a right one.
    body = bytes(data_hdr) + tlv
    plain = body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    cipher = _encrypt(key.secret, plain)

    header = bytearray(HDR_LEN)
    header[0:4] = b"sss\x00"
    header[4] = 0x01  # version
    header[6] = 0x00  # named-key length: unnamed
    header[7] = ENC_BF32
    struct.pack_into(">q", header, 8, key.id)
    return bytes(header) + cipher


#: Why SSS cannot work on this host, and what can be done about it.
_NO_BLOWFISH = (
    "SSS encrypts its credential with Blowfish, which this host's OpenSSL does not "
    "provide - the host is in FIPS mode, or OpenSSL 3 is running without its "
    "'legacy' provider. SSS cannot be used here as things stand: if site policy "
    "allows it, load the legacy provider (OPENSSL_CONF pointing at an openssl.cnf "
    "that activates both 'default' and 'legacy'); otherwise ask the server's "
    "administrators for another mechanism - ztn (a bearer token), gsi or krb5"
)


def _encrypt(secret: bytes, plain: bytes) -> bytes:
    """Blowfish-CFB64 under ``secret`` with a zero IV, as ``XrdSecsss`` sends it.

    Raises :class:`~xrdclient.errors.CredentialError` - saying why and what
    to do - where OpenSSL will not do Blowfish at all, rather than leaking
    ``cryptography``'s :class:`UnsupportedAlgorithm` out of a login.
    """
    try:
        return Blowfish(secret).encrypt_cfb64(bytes(8), plain)
    except UnsupportedAlgorithm as exc:
        raise CredentialError(_NO_BLOWFISH) from exc


class SSSCredential(Credential):
    """``sss`` - a shared secret from a keytab.

    The user name travels inside the encrypted blob, so the server trusts it
    only as far as it trusts whoever holds the key.
    """

    __slots__ = ("key", "username")
    name = "sss"

    def __init__(self, key: SSSKey, username: str) -> None:
        """Log in as ``username`` with ``key``; no I/O, the keytab was read already."""
        self.key = key
        self.username = username

    def initial(self) -> bytes:
        """A freshly minted credential; refused if the key expired since it was read."""
        if self.key.expired:
            raise CredentialError(f"SSS key {self.key.id} expired at {self.key.expires}")
        return build_credential(self.key, self.username)

    @classmethod
    def available(
        cls, offer: Offer, config: Config, *, username: str, host: str
    ) -> SSSCredential | None:
        """The first live key - the one the server's ``n:`` names, when it names one.

        ``None`` for no keytab, an unreadable or exposed one, or no matching
        key; an exposed keytab is also warned about. Blowfish is tried here,
        so a FIPS host fails over to the next mechanism instead of failing
        the login.
        """
        path = default_keytab_path(config)
        try:
            keys = read_keytab(path)
        except FileNotFoundError:
            return None
        except PermissionError as exc:
            # Loud rather than silent: falling through to a weaker mechanism
            # because the keytab was world-readable is precisely the kind of
            # downgrade nobody notices in a debug log.
            _log.warning("ignoring the SSS keytab - %s", exc)
            return None
        except (OSError, ValueError) as exc:
            _log.debug("unusable SSS keytab %s: %s", path, exc)
            return None
        if not keys:
            return None
        wanted = offer.options().get("n")
        for key in keys:
            if wanted is None or key.name == wanted:
                # Found out now, while the ladder can still move on to another
                # mechanism, not after the server has been told to expect SSS.
                _encrypt(key.secret, b"")
                return cls(key, username or config.username)
        return None

    @classmethod
    def missing(cls, offer: Offer, config: Config, *, username: str, host: str) -> Ask | None:
        """Why no key was usable - the same checks as :meth:`available`, in words.

        ``None`` when the keytab is fine, which leaves a Blowfish-less host:
        no path a person could type would fix that.
        """
        path = default_keytab_path(config)
        wanted = offer.options().get("n")
        try:
            keys = read_keytab(path)
        except FileNotFoundError:
            reason = f"there is no keytab at {path}"
        except PermissionError as exc:
            reason = str(exc)
        except (OSError, ValueError) as exc:
            reason = f"{path} could not be read as a keytab: {exc}"
        else:
            if not keys:
                reason = f"{path} holds no unexpired key"
            elif wanted is not None and not any(key.name == wanted for key in keys):
                reason = f"{path} holds no key named {wanted!r}, which is the one asked for"
            else:
                return None
        return Ask(
            mechanism=cls.name,
            what="a shared-secret keytab",
            reason=reason,
            hint="xrdsssadmin -k <name> add <keytab>, kept mode 0600 and off shared storage",
            prompt="path to a keytab file",
            host=host,
        )

    @classmethod
    def using(cls, answer: str, config: Config) -> Config:
        """The typed answer is a keytab path; ``~`` is expanded as a shell would."""
        return config.evolve(keytab=os.path.expanduser(answer))

    def __repr__(self) -> str:
        """The key's identity and the user, with the secret redacted by :class:`SSSKey`."""
        return f"SSSCredential(key={self.key!r}, username={self.username!r})"
