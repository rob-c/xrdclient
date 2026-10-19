"""XRootD's paged-I/O framing over CRC-32C; the CRC itself is :mod:`xrdcore.crc32c`.

Every ``kXR_pgread``/``kXR_pgwrite`` page carries a CRC-32C (Castagnoli, not
``zlib.crc32``'s IEEE polynomial, which SSS uses). The checksum moved to
xrdcore in 0.4.0, with the ``crc32c`` checksum type it also serves; what stays
here is XRootD's own framing of it - :func:`pack_pages`, :func:`unpack_pages`
and :func:`page_span` - which no other protocol on the stack uses.

With the ``fast`` extra (``pip install 'xrdclient[fast]'``, which pulls in
``xrdcore[fast]`` and so ``google-crc32c``) the hardware-assisted C
implementation is used - gigabytes per second. Without it a pure-Python
slice-by-eight fallback runs, a few MB/s: correct, but it is what bounds a
paged read. ``xrd-doctor`` (:func:`xrdclient.diagnose`) says which one is in
use, and :data:`IS_ACCELERATED` says so to a program.

:func:`crc32c`, :func:`crc32c_py` and :data:`IS_ACCELERATED` are re-exported,
the same objects as xrdcore's.
"""

from __future__ import annotations

from xrdcore.crc32c import IS_ACCELERATED, crc32c, crc32c_py

__all__ = ["IS_ACCELERATED", "crc32c", "crc32c_py", "pack_pages", "page_span", "unpack_pages"]


# --------------------------------------------------------------------------
# Paged-I/O helpers
# --------------------------------------------------------------------------

#: XRootD's page for paged I/O (``XrdProto::kXR_pgPageSZ``): 4 KiB, each
#: preceded on the wire by its 4-byte big-endian CRC-32C.
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
    # A truncated final page (fewer than 4 + head bytes) is still checked,
    # against the bytes that are there: a short read at end of file is
    # legitimate, and its CRC covers exactly what was sent.
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
