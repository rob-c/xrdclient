"""``KEYRING:`` caches: MIT's credential caches in Linux kernel keyrings.

MIT's ``cc_keyring.c`` keeps a cache collection as a keyring, and each cache
in it as a keyring of ``user`` (or ``big_key``) keys: the default principal
under ``__krb5_princ__``, the KDC clock offset under
``__krb5_time_offsets__``, and one key per credential, named after its
server principal. Each payload is marshalled as in a version 4 FILE cache
(:mod:`.ccache`). The collection's current cache is named by its
``krb_ccache:primary`` key: a 32-bit version (1), then a counted name.

The residual is ``anchor:collection[:cache]``, and the anchor says where
the collection hangs:

* ``persistent:<uid>`` - the user's persistent keyring (``KEYCTL_GET_PERSISTENT``),
  collection ``_krb``; RHEL 7 and 8 default to this;
* ``session:<name>``, ``user:<name>``, ``process:<name>``, ``thread:<name>`` -
  that special keyring, collection ``_krb_<name>``;
* a bare ``<name>`` - MIT's legacy form, in the session keyring, where a
  cache made by a pre-collection MIT is found by its bare name as well.

This module only reads. MIT creates the collection keyrings on first use,
and stores fetched service tickets back as keys; here, a keyring that is not
there is a cache that is not there, and a fetched ticket stays in this
process's memory, as it does for a FILE cache. The kernel is reached through
the ``keyctl`` system call, by ``ctypes``: :class:`Keyctl` wraps the five
operations a reader needs around a syscall function, which tests replace.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import struct
import sys
from dataclasses import dataclass, field
from typing import Callable, Union

from ..._log import get_logger
from ...errors import CredentialError
from .ccache import read_credentials, read_principal
from .model import Principal, Ticket

__all__ = ["Keyctl", "KeyringCache", "parse_residual", "system_keyctl"]

_log = get_logger(__name__)

# keyctl(2) operations, from <linux/keyctl.h>.
KEYCTL_GET_KEYRING_ID = 0
KEYCTL_DESCRIBE = 6
KEYCTL_SEARCH = 10
KEYCTL_READ = 11
KEYCTL_GET_PERSISTENT = 22

# Special keyring IDs.
KEY_SPEC_THREAD_KEYRING = -1
KEY_SPEC_PROCESS_KEYRING = -2
KEY_SPEC_SESSION_KEYRING = -3
KEY_SPEC_USER_KEYRING = -4
KEY_SPEC_USER_SESSION_KEYRING = -5

#: ``__NR_keyctl`` per machine. The asm-generic number (219) serves arm64 and riscv64.
SYS_KEYCTL = {
    "x86_64": 250,
    "amd64": 250,
    "i386": 288,
    "i686": 288,
    "aarch64": 219,
    "arm64": 219,
    "riscv64": 219,
    "armv7l": 311,
    "armv6l": 311,
    "ppc64le": 271,
    "ppc64": 271,
    "s390x": 280,
}

# The errnos that mean "no such key" (their generic Linux values where this
# Python's errno module, off Linux, does not know them).
ENOKEY = getattr(errno, "ENOKEY", 126)
EKEYEXPIRED = getattr(errno, "EKEYEXPIRED", 127)
EKEYREVOKED = getattr(errno, "EKEYREVOKED", 128)
_MISSING = (ENOKEY, EKEYEXPIRED, EKEYREVOKED, errno.ENOENT)

# MIT's names, from cc_keyring.c.
ANCHORS = ("persistent", "session", "user", "process", "thread", "legacy")
PERSISTENT_COLLECTION = "_krb"
COLLECTION_PREFIX = "_krb_"
PRIMARY_KEY = "krb_ccache:primary"
PRINCIPAL_KEY = "__krb5_princ__"
OFFSETS_KEY = "__krb5_time_offsets__"
COLLECTION_VERSION = 1
_CRED_TYPES = ("user", "big_key")

#: What the syscall is given: key serials and lengths, names, and buffers to fill.
Arg = Union[int, bytes, None, "ctypes.Array[ctypes.c_char]"]
#: ``syscall(op, *args)``: the result, or :class:`OSError` with the errno.
Syscall = Callable[..., int]


def _missing(exc: OSError) -> bool:
    return exc.errno in _MISSING


class Keyctl:
    """The keyctl operations a reader needs, over ``syscall(op, *args)``."""

    def __init__(self, syscall: Syscall) -> None:
        self._call = syscall

    def keyring_id(self, special: int) -> int:
        """The serial of a special keyring, without creating it."""
        return self._call(KEYCTL_GET_KEYRING_ID, special, 0)

    def search(self, ring: int, kind: str, description: str) -> int:
        """A key of ``kind`` named ``description`` in ``ring`` or a keyring it holds."""
        return self._call(KEYCTL_SEARCH, ring, kind.encode(), description.encode("utf-8"), 0)

    def persistent(self, uid: int) -> int:
        """``uid``'s persistent keyring, linked into the process keyring as MIT links it."""
        return self._call(KEYCTL_GET_PERSISTENT, uid, KEY_SPEC_PROCESS_KEYRING)

    def _fetch(self, op: int, key: int) -> bytes:
        """The whole of what ``op`` returns for ``key``: sized first, then read."""
        size = self._call(op, key, None, 0)
        while True:
            buffer = ctypes.create_string_buffer(max(size, 1))
            got = self._call(op, key, buffer, size)
            if got <= size:
                return buffer.raw[:got]
            size = got  # it grew between the calls

    def read(self, key: int) -> bytes:
        """A key's payload; for a keyring, its members' serials."""
        return self._fetch(KEYCTL_READ, key)

    def describe(self, key: int) -> tuple[str, str]:
        """A key's type and description."""
        text = self._fetch(KEYCTL_DESCRIBE, key).rstrip(b"\0").decode("utf-8", "replace")
        parts = text.split(";", 4)  # type;uid;gid;perm;description
        return parts[0], parts[-1]

    def members(self, ring: int) -> list[int]:
        payload = self.read(ring)
        return list(struct.unpack(f"={len(payload) // 4}i", payload[: len(payload) // 4 * 4]))


def _libc_syscall(number: int) -> Syscall:
    """``syscall(2)`` for keyctl, through the C library."""
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.syscall
    function.restype = ctypes.c_long

    def call(op: int, *args: Arg) -> int:  # pragma: no cover - runs only on Linux
        # Integers go as longs, which is what syscall(2) reads its arguments as.
        wide = [ctypes.c_long(arg) if isinstance(arg, int) else arg for arg in args]
        result = int(function(ctypes.c_long(number), ctypes.c_long(op), *wide))
        if result == -1:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        return result

    return call


def system_keyctl() -> Keyctl:
    """This kernel's keyctl; a clear error where there are no kernel keyrings."""
    if not sys.platform.startswith("linux"):
        raise CredentialError(
            f"KEYRING: credential caches are Linux kernel keyrings, and this is {sys.platform}; "
            "use a file instead: KRB5CCNAME=FILE:/tmp/krb5cc_$(id -u) kinit"
        )
    number = SYS_KEYCTL.get(platform.machine())
    if number is None:
        raise CredentialError(
            f"reading KEYRING: credential caches is not supported on {platform.machine()}; "
            "use a file instead: KRB5CCNAME=FILE:/tmp/krb5cc_$(id -u) kinit"
        )
    return Keyctl(_libc_syscall(number))


def parse_residual(residual: str) -> tuple[str, str, str | None]:
    """``anchor:collection[:cache]`` as MIT's ``parse_residual`` splits it.

    With no colon at all the anchor is ``legacy``. An unknown anchor, or a
    persistent collection that is not a uid, is a :class:`CredentialError`.
    """
    anchor, sep, rest = residual.partition(":")
    if not sep:
        anchor, rest = "legacy", residual
    collection, sep, cache = rest.partition(":")
    if anchor not in ANCHORS:
        raise CredentialError(f"KEYRING:{residual} names an unknown keyring anchor {anchor!r}")
    if anchor == "persistent" and collection and not collection.isdigit():
        raise CredentialError(f"KEYRING:{residual}: {collection!r} is not a uid")
    return anchor, collection, (cache if sep else None)


def parse_primary(payload: bytes) -> str:
    """The name in a ``krb_ccache:primary`` key: version 1, a 32-bit length, the name."""
    if len(payload) < 8:
        raise ValueError("a keyring collection's primary key is truncated")
    version, length = struct.unpack(">II", payload[:8])
    if version != COLLECTION_VERSION:
        raise ValueError(f"unknown keyring collection version {version}")
    if length > len(payload) - 8:
        raise ValueError("a keyring collection's primary key is truncated")
    return payload[8 : 8 + length].decode("utf-8", "replace")


def _offset(payload: bytes) -> float:
    """``__krb5_time_offsets__``: signed seconds and microseconds, big-endian."""
    if len(payload) < 8:
        return 0.0
    seconds, micros = struct.unpack(">ii", payload[:8])
    return float(seconds + micros / 1_000_000)


@dataclass
class _Contents:
    """What a cache keyring holds, sorted by kind."""

    principal: bytes | None = None
    offset: float = 0.0
    creds: list[bytes] = field(default_factory=list)


@dataclass
class KeyringCache:
    """A cache in a kernel keyring, named ``KEYRING:<residual>``."""

    residual: str
    keyctl: Keyctl

    def __post_init__(self) -> None:
        self.anchor, self.collection, self.cache = parse_residual(self.residual)

    @property
    def name(self) -> str:
        return f"KEYRING:{self.residual}"

    def read(self) -> tuple[Principal, list[Ticket]]:
        """The cache's default principal and credentials, configuration entries left out.

        A keyring or key that is not there raises :class:`FileNotFoundError`,
        what a missing FILE cache raises.
        """
        try:
            contents = self._contents(self._cache_id())
        except OSError as exc:
            if _missing(exc) and not isinstance(exc, FileNotFoundError):
                raise FileNotFoundError(f"credential cache {self.name} not found") from exc
            raise
        if contents.principal is None:  # a keyring MIT made but never initialized
            raise FileNotFoundError(f"credential cache {self.name} holds no principal")
        return read_principal(contents.principal), read_credentials(
            contents.creds, contents.offset, self.name
        )

    def store(self, ticket: Ticket) -> bool:
        """Not done: a fetched ticket stays in this process's memory."""
        return False

    # -- finding the cache keyring -----------------------------------------

    def _anchor_id(self) -> int:
        """The special keyring the collection hangs from (all anchors but persistent)."""
        fixed = {
            "user": KEY_SPEC_USER_KEYRING,
            "process": KEY_SPEC_PROCESS_KEYRING,
            "thread": KEY_SPEC_THREAD_KEYRING,
        }
        if self.anchor in fixed:
            return fixed[self.anchor]
        # MIT's session_write_anchor: the user-session keyring when the session is it.
        session = self.keyctl.keyring_id(KEY_SPEC_SESSION_KEYRING)
        user_session = self.keyctl.keyring_id(KEY_SPEC_USER_SESSION_KEYRING)
        return (
            KEY_SPEC_USER_SESSION_KEYRING if session == user_session else KEY_SPEC_SESSION_KEYRING
        )

    def _collection_id(self) -> int:
        if self.anchor == "persistent":
            uid = int(self.collection) if self.collection else os.geteuid()
            ring = self.keyctl.persistent(uid)
            return self.keyctl.search(ring, "keyring", PERSISTENT_COLLECTION)
        return self.keyctl.search(self._anchor_id(), "keyring", COLLECTION_PREFIX + self.collection)

    def _primary(self, collection: int) -> str:
        """The collection's current cache: its primary key, else MIT's first name for one."""
        try:
            key = self.keyctl.search(collection, "user", PRIMARY_KEY)
        except OSError as exc:
            if not _missing(exc):
                raise
            return self.collection or "tkt"
        return parse_primary(self.keyctl.read(key))

    def _cache_id(self) -> int:
        try:
            collection = self._collection_id()
            return self.keyctl.search(
                collection, "keyring", self.cache or self._primary(collection)
            )
        except OSError as exc:
            if self.anchor != "legacy" or not _missing(exc):
                raise
        # A legacy cache from an MIT before collections: named alone, in the session keyring.
        return self.keyctl.search(KEY_SPEC_SESSION_KEYRING, "keyring", self.collection)

    # -- reading it -----------------------------------------------------------

    def _contents(self, cache: int) -> _Contents:
        out = _Contents()
        for key in self.keyctl.members(cache):
            try:
                kind, description = self.keyctl.describe(key)
                if kind in _CRED_TYPES:
                    self._sort(out, description, self.keyctl.read(key))
            except OSError as exc:
                if not _missing(exc):
                    raise
                _log.debug("key %d left %s while it was read", key, self.name)
        return out

    @staticmethod
    def _sort(out: _Contents, description: str, payload: bytes) -> None:
        if description == PRINCIPAL_KEY:
            out.principal = payload
        elif description == OFFSETS_KEY:
            out.offset = _offset(payload)
        else:
            out.creds.append(payload)
