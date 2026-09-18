"""The two 64-bit CRCs XRootD servers report, both reflected and both seeded
and finished with all ones.

``crc64`` is CRC-64/XZ (ECMA-182, normal polynomial ``0x42F0E1EBA9EA3693``) -
what ``xrdcrc64`` computes and what ``kXR_Qcksum`` and a WebDAV ``Digest``
header call ``crc64``. ``crc64nvme`` is CRC-64/NVME (Rocksoft, normal
polynomial ``0xAD93D23594C93659``), the one S3 spells
``x-amz-checksum-crc64nvme``; a server offers it under its own name, never as
``crc64``, because the two disagree on every input.

Stock XRootD computes neither, so a checksum in one of these names comes from
a gateway - and this is what verifies it without one.
"""

from __future__ import annotations

__all__ = ["crc64", "crc64nvme"]

_MASK = (1 << 64) - 1


def _table(reflected_poly: int) -> list[int]:
    table = []
    for n in range(256):
        crc = n
        for _ in range(8):
            crc = (crc >> 1) ^ reflected_poly if crc & 1 else crc >> 1
        table.append(crc)
    return table


#: Reflections of 0x42F0E1EBA9EA3693 and 0xAD93D23594C93659.
_XZ = _table(0xC96C5795D7870F42)
_NVME = _table(0x9A6C9329AC4BC9B5)


def _fold(table: list[int], data: bytes | bytearray | memoryview, crc: int) -> int:
    c = ~crc & _MASK
    for byte in memoryview(data).cast("B"):
        c = table[(c ^ byte) & 0xFF] ^ (c >> 8)
    return ~c & _MASK


def crc64(data: bytes | bytearray | memoryview, crc: int = 0) -> int:
    """CRC-64/XZ of ``data``, chained onto ``crc``.

    ``crc`` is the running value in the form it is reported in, so chaining
    two chunks gives what one call over both would::

        >>> crc64(b"9", crc64(b"12345678")) == crc64(b"123456789")
        True
    """
    return _fold(_XZ, data, crc)


def crc64nvme(data: bytes | bytearray | memoryview, crc: int = 0) -> int:
    """CRC-64/NVME of ``data``, chained onto ``crc``."""
    return _fold(_NVME, data, crc)
