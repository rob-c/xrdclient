"""Castagnoli CRC-32C (poly 0x1EDC6F41, reflected 0x82F63B78).

Used by XRootD paged I/O and by the ``crc32c`` checksum type. Not the same
polynomial as ``zlib.crc32`` (IEEE), which SSS uses - never share a table.

A C implementation is used when ``google-crc32c`` is installed (the ``fast``
extra); otherwise a slice-by-eight pure-Python fallback runs, which is about
five times faster than the naive per-byte loop.
"""

from __future__ import annotations

__all__ = ["crc32c", "pack_pages", "unpack_pages", "page_span", "IS_ACCELERATED"]

_POLY = 0x82F63B78
_MASK = 0xFFFFFFFF


def _build_tables() -> list[list[int]]:
    first = []
    for n in range(256):
        crc = n
        for _ in range(8):
            crc = (crc >> 1) ^ _POLY if crc & 1 else crc >> 1
        first.append(crc)
    tables = [first]
    for k in range(1, 8):
        prev = tables[k - 1]
        tables.append([(v >> 8) ^ first[v & 0xFF] for v in prev])
    return tables


_T = _build_tables()
_T0, _T1, _T2, _T3, _T4, _T5, _T6, _T7 = _T


def _crc32c_py(data: bytes | bytearray | memoryview, crc: int = 0) -> int:
    """Slice-by-eight software CRC-32C."""
    view = memoryview(data).cast("B")
    n = len(view)
    c = (crc ^ _MASK) & _MASK
    i = 0
    limit = n - (n % 8)
    while i < limit:
        c ^= int.from_bytes(view[i : i + 4], "little")
        b4, b5, b6, b7 = view[i + 4], view[i + 5], view[i + 6], view[i + 7]
        c = (
            _T7[c & 0xFF]
            ^ _T6[(c >> 8) & 0xFF]
            ^ _T5[(c >> 16) & 0xFF]
            ^ _T4[c >> 24]
            ^ _T3[b4]
            ^ _T2[b5]
            ^ _T1[b6]
            ^ _T0[b7]
        )
        i += 8
    while i < n:
        c = _T0[(c ^ view[i]) & 0xFF] ^ (c >> 8)
        i += 1
    return c ^ _MASK


try:  # pragma: no cover - depends on an optional extra
    from google_crc32c import extend as _extend

    def crc32c(data: bytes | bytearray | memoryview, crc: int = 0) -> int:
        """CRC-32C of ``data``, chained onto ``crc``."""
        return int(_extend(crc, bytes(data)))

    IS_ACCELERATED = True
except ImportError:  # pragma: no cover - the common path
    crc32c = _crc32c_py
    IS_ACCELERATED = False


# --------------------------------------------------------------------------
# Paged-I/O helpers
# --------------------------------------------------------------------------

PAGE_SIZE = 4096


def page_span(offset: int, remaining: int) -> int:
    """How much of the page at file ``offset`` is still in ``remaining`` bytes.

    Pages are aligned to the file, not to the buffer, so the page a
    retransmission has to resend runs from ``offset`` to the next 4 KiB
    boundary - or to the end of the data, whichever comes first.
    """
    return min(remaining, PAGE_SIZE - offset % PAGE_SIZE)


def pack_pages(data: bytes, offset: int = 0) -> bytes:
    """Interleave a big-endian CRC-32C before each 4 KiB page.

    ``offset`` is the file offset ``data`` starts at; a non-page-aligned
    start makes the first page short, exactly as ``kXR_pgwrite`` requires.
    """
    head = PAGE_SIZE - offset % PAGE_SIZE if offset % PAGE_SIZE else PAGE_SIZE
    out = bytearray()
    view = memoryview(data)
    pos = 0
    while pos < len(view):
        page = view[pos : pos + head]
        out += crc32c(page).to_bytes(4, "big") + page
        pos += head
        head = PAGE_SIZE
    return bytes(out)


def unpack_pages(data: bytes, offset: int = 0) -> tuple[bytes, tuple[int, ...]]:
    """Strip and verify the per-page CRCs of a ``kXR_pgread`` payload.

    Returns the payload and the file offsets of any pages that failed.
    """
    view = memoryview(data)
    head = PAGE_SIZE - offset % PAGE_SIZE if offset % PAGE_SIZE else PAGE_SIZE
    out = bytearray()
    corrupt: list[int] = []
    pos = 0
    at = offset
    while pos < len(view):
        want = int.from_bytes(view[pos : pos + 4], "big")
        page = view[pos + 4 : pos + 4 + head]
        if crc32c(page) != want:
            corrupt.append(at)
        out += page
        pos += 4 + len(page)
        at += len(page)
        head = PAGE_SIZE
    return bytes(out), tuple(corrupt)
