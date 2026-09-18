"""``sss`` - Simple Shared Secret.

The credential is a 16-byte cleartext header followed by a Blowfish-CFB64
blob covering a 40-byte data header, a NAME type-length-value, and an IEEE
CRC-32 of both. Note the polynomial: SSS uses ``zlib.crc32``, *not* the
CRC-32C of paged I/O.
"""

from __future__ import annotations

import errno as _errno
import os
import struct
import time
import zlib
from dataclasses import dataclass

from .._compat import SLOTS
from .._log import get_logger
from ..config import Config
from ..crypto.blowfish import Blowfish
from ..errors import CredentialError
from .base import Credential, Offer
from .prompt import Ask

__all__ = ["SSSKey", "SSSCredential", "read_keytab", "default_keytab_path", "build_credential"]

_log = get_logger(__name__)

HDR_LEN = 16
DATA_HDR_LEN = 40
#: Epoch SSS timestamps count from (2008-09-23T14:11:20Z).
BASE_TIME = 1222183880
ENC_BF32 = ord("0")
OPT_USEDATA = 0x00
TYPE_NAME = 0x01
NONCE_LEN = 32


@dataclass(frozen=True, **SLOTS)
class SSSKey:
    """One keytab entry."""

    id: int
    secret: bytes
    name: str = ""
    user: str = ""
    group: str = ""
    expires: int = 0

    @property
    def expired(self) -> bool:
        return self.expires != 0 and self.expires <= time.time()

    def __repr__(self) -> str:
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

    A keytab holds shared secrets in the clear, so one that group or others
    can read is refused outright, exactly as the C implementation refuses it.
    ``require_private=False`` is for inspecting a keytab you already know is
    exposed - never for authenticating with it.
    """
    if require_private:
        _require_private(path)
    keys: list[SSSKey] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            key = _keytab_line(raw)
            if key is not None and (include_expired or not key.expired):
                keys.append(key)
    return keys


def _require_private(path: str) -> None:
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        raise PermissionError(
            _errno.EACCES,
            f"SSS keytab is readable by group or others (mode {mode:03o}); chmod 600 it",
            path,
        )


def _attributes(fields: list[str]) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for field in fields:
        if field.startswith("#"):
            break
        if len(field) > 1 and field[1] == ":":
            attrs[field[0]] = field[2:]
    return attrs


def _keytab_line(raw: str) -> SSSKey | None:
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
    struct.pack_into(">I", data_hdr, 32, gen_time & 0xFFFFFFFF)
    data_hdr[39] = OPT_USEDATA

    user = (username or "xrd").encode("utf-8")
    ulen = min(len(user) + 1, 64)  # counts the trailing NUL
    tlv = bytes([TYPE_NAME, 0x00, ulen]) + user[: ulen - 1] + b"\x00"

    body = bytes(data_hdr) + tlv
    plain = body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    cipher = Blowfish(key.secret).encrypt_cfb64(bytes(8), plain)

    header = bytearray(HDR_LEN)
    header[0:4] = b"sss\x00"
    header[4] = 0x01  # version
    header[6] = 0x00  # named-key length: unnamed
    header[7] = ENC_BF32
    struct.pack_into(">q", header, 8, key.id)
    return bytes(header) + cipher


class SSSCredential(Credential):
    """``sss`` - a shared secret from a keytab."""

    __slots__ = ("key", "username")
    name = "sss"

    def __init__(self, key: SSSKey, username: str) -> None:
        self.key = key
        self.username = username

    def initial(self) -> bytes:
        if self.key.expired:
            raise CredentialError(f"SSS key {self.key.id} expired at {self.key.expires}")
        return build_credential(self.key, self.username)

    @classmethod
    def available(
        cls, offer: Offer, config: Config, *, username: str, host: str
    ) -> SSSCredential | None:
        path = default_keytab_path(config)
        if not os.path.isfile(path):
            return None
        try:
            keys = read_keytab(path)
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
                return cls(key, username or config.username)
        return None

    @classmethod
    def missing(cls, offer: Offer, config: Config, *, username: str, host: str) -> Ask | None:
        path = default_keytab_path(config)
        wanted = offer.options().get("n")
        if not os.path.isfile(path):
            reason = f"there is no keytab at {path}"
        else:
            try:
                keys = read_keytab(path)
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
        return config.evolve(keytab=os.path.expanduser(answer))

    def __repr__(self) -> str:
        return f"SSSCredential(key={self.key!r}, username={self.username!r})"
