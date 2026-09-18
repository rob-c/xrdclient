"""Checksum algorithms, by the names XRootD servers use.

Every algorithm exposes the incremental ``hashlib``-ish shape so a copy can
digest as it streams:

    >>> h = new("adler32")
    >>> h.update(b"hello")
    >>> h.hexdigest()
    '062c0215'
"""

from __future__ import annotations

import hashlib
import zlib
from collections.abc import Callable, Iterable

from .crc32c import crc32c
from .crc64 import crc64, crc64nvme

__all__ = ["Checksum", "new", "algorithms", "checksum_file", "checksum_bytes"]

#: One chunk folded into a running value - the shape every algorithm here has.
Step = Callable[["bytes | bytearray | memoryview", int], int]


class Checksum:
    """Incremental digest with a stable lowercase hex representation."""

    __slots__ = ("name", "_value", "_step", "_width")

    def __init__(self, name: str, step: Step, seed: int, width: int) -> None:
        self.name = name
        self._step = step
        self._value = seed
        self._width = width

    def update(self, data: bytes | bytearray | memoryview) -> None:
        self._value = self._step(data, self._value)

    @property
    def value(self) -> int:
        return self._value

    def hexdigest(self) -> str:
        return f"{self._value:0{self._width}x}"

    def digest(self) -> bytes:
        return self._value.to_bytes(self._width // 2, "big")

    def __repr__(self) -> str:
        return f"Checksum({self.name!r}, {self.hexdigest()})"


class _HashlibChecksum:
    """Adapter giving a ``hashlib`` object the same surface as `Checksum`."""

    __slots__ = ("name", "_h")

    def __init__(self, name: str) -> None:
        self.name = name
        self._h = hashlib.new(name)

    def update(self, data: bytes | bytearray | memoryview) -> None:
        self._h.update(data)

    @property
    def value(self) -> int:
        return int.from_bytes(self._h.digest(), "big")

    def hexdigest(self) -> str:
        return self._h.hexdigest()

    def digest(self) -> bytes:
        return self._h.digest()

    def __repr__(self) -> str:
        return f"Checksum({self.name!r}, {self.hexdigest()})"


_NATIVE: dict[str, tuple[Callable[..., int], int, int]] = {
    "adler32": (zlib.adler32, 1, 8),
    "crc32": (zlib.crc32, 0, 8),
    "crc32c": (crc32c, 0, 8),
    "crc64": (crc64, 0, 16),
    "crc64nvme": (crc64nvme, 0, 16),
}
#: The one alias a server registers: CRC-64/XZ answers to both names.
_ALIASES = {"crc64xz": "crc64"}
_HASHLIB = ("md5", "sha1", "sha224", "sha256", "sha384", "sha512")


def algorithms() -> tuple[str, ...]:
    """Every algorithm this build can compute."""
    return tuple(sorted(_NATIVE)) + _HASHLIB


def new(name: str) -> Checksum | _HashlibChecksum:
    """A fresh incremental digest for ``name`` (case-insensitive)."""
    key = name.lower().replace("-", "")
    key = _ALIASES.get(key, key)
    if key in _NATIVE:
        step, seed, width = _NATIVE[key]
        return Checksum(key, step, seed, width)
    if key in _HASHLIB:
        return _HashlibChecksum(key)
    raise ValueError(f"unknown checksum algorithm {name!r}; have {algorithms()}")


def checksum_bytes(name: str, data: bytes) -> str:
    """Hex digest of ``data``."""
    h = new(name)
    h.update(data)
    return h.hexdigest()


def checksum_file(name: str, chunks: Iterable[bytes]) -> str:
    """Hex digest of a stream of chunks."""
    h = new(name)
    for chunk in chunks:
        h.update(chunk)
    return h.hexdigest()
