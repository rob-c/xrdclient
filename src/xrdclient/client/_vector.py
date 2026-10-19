"""Packing and unpacking for the vector requests: ``kXR_readv``, ``kXR_writev``, ``kXR_clone``.

Split out of :mod:`xrdclient.client.file` because none of it needs a handle:
these are the pure functions that cut a caller's ranges to the ceilings a
server accepts, frame them in one ``struct`` call, and file the reply's
segments back under the ranges they answer. :class:`~xrdclient.client.file.File`
is what sends them.
"""

from __future__ import annotations

import struct
from collections.abc import Callable, Iterator, Sequence
from typing import TYPE_CHECKING

from ..errors import ProtocolError
from ..proto import requests as r
from ..proto.frames import Request
from ..types import CloneRange, ReadRange, WriteChunk

if TYPE_CHECKING:
    from ._handle import _Handle

#: Server-side ceilings on one ``kXR_readv``: ``XrdProto::maxRvecsz``
#: elements, and the byte total we choose to put in one request so that a
#: reply stays about the size of one of the server's buffers.
READV_MAX_CHUNKS = 1024
READV_MAX_BYTES = 2 << 20

#: ``maxReadv_ior`` - the largest single ``kXR_readv`` element xrootd accepts
#: with its default 2 MiB buffer. The server keeps back room for the 16-byte
#: ``readahead_list`` header it writes before each element's data, and refuses
#: anything bigger with ``kXR_NoMemory``. XrdCl uses the same default.
READV_MAX_ELEMENT = (2 << 20) - 16

#: ``maxTransz`` - the largest single ``kXR_writev`` element xrootd accepts
#: with its default buffer. A bigger one is refused and the connection with it.
WRITEV_MAX_ELEMENT = 2 << 20

#: ``maxClonesz`` - how many ranges one ``kXR_clone`` may carry.
CLONE_MAX_RANGES = 1024


def _readv_for(batch: Sequence[ReadRange], file: _Handle) -> Callable[[bytes], Request]:
    """Bind ``batch`` to a builder that still takes the handle as an argument.

    :meth:`File._execute` re-opens a lost file and retries, and the retry must
    be issued against the new handle - so the ranges are captured here and the
    handle is not.
    """
    return lambda handle: r.ReadV(
        [(handle, rng.offset, rng.length) for rng in batch], file.data_path
    )


def _write_for(at: int, body: bytes | memoryview) -> Callable[[bytes, int], Request]:
    """Bind one chunk to a builder that still takes the handle and the path.

    Same reason as :func:`_readv_for`, and one more: :meth:`File._bulk` may run
    the builder again on another path while the loop that made it has moved on
    to the next chunk, so the chunk it was made for is captured here.
    """
    return lambda handle, pid: r.Write(handle, at, body, pid)


#: A ``kXR_readv`` reply's ``readahead_list``: fhandle, length, offset.
_READ_LIST = struct.Struct(">4xiq")


class _PackedReadV(r.ReadV):
    """A ``kXR_readv`` whose element list is packed in one call.

    The same frame as :class:`~xrdclient.proto.requests.ReadV` builds a field
    at a time, which for a thousand elements is most of the request's cost.
    """

    __slots__ = ()

    def payload(self) -> bytes:
        """The ``readahead_list`` array: 16 bytes per element, one ``struct.pack`` call.

        Each element is ``fhandle[4]``, ``rlen`` (int32) and ``offset`` (int64),
        big-endian, in that order - the field order of the protocol, which is not
        the ``(handle, offset, length)`` order the chunks are kept in.
        """
        fields: list[bytes | int] = []
        for fhandle, offset, length in self.chunks:
            fields += (fhandle, length, offset)  # "4s" pads or truncates, as the field does
        return struct.pack(">" + "4siq" * len(self.chunks), *fields)


def _as_range(rng: ReadRange | tuple[int, int]) -> ReadRange:
    return rng if isinstance(rng, ReadRange) else ReadRange(rng[0], rng[1])


def _as_asked(bodies: list[memoryview], wanted: list[ReadRange]) -> list[bytes] | None:
    """Every range's bytes, if the replies hold exactly ``wanted``, in order.

    ``None`` as soon as anything differs - a segment out of order, a short
    one at the end of the file, one too many or too few - and the caller
    matches the segments up by offset instead.
    """
    out: list[bytes] = []
    expected = iter(wanted)
    for body in bodies:
        for offset, data in _walk(body):
            rng = next(expected, None)
            if rng is None or rng.offset != offset or rng.length != len(data):
                return None
            out.append(bytes(data))
    return out if len(out) == len(wanted) else None


def _by_offset(bodies: list[memoryview]) -> dict[int, list[bytes]]:
    """Every segment of the replies, filed by offset, in the order it came."""
    out: dict[int, list[bytes]] = {}
    for body in bodies:
        for offset, data in _walk(body):
            out.setdefault(offset, []).append(bytes(data))
    return out


def _walk(body: memoryview) -> Iterator[tuple[int, memoryview]]:
    """``(offset, bytes)`` for each segment of one ``kXR_readv`` reply.

    Walks the reply in place: the views are slices of the buffer the reply
    was received into, so a caller copies each segment once, and only the
    segments it keeps.
    """
    at, end = 0, len(body)
    while at < end:
        if end - at < _READ_LIST.size:
            raise ProtocolError(
                f"kXR_readv reply ends with {end - at} bytes of a {_READ_LIST.size}-byte "
                "segment header"
            )
        length, offset = _READ_LIST.unpack_from(body, at)
        at += _READ_LIST.size
        if length < 0:
            raise ProtocolError(f"kXR_readv segment declares a negative length of {length}")
        if at + length > end:
            raise ProtocolError(
                f"kXR_readv segment at offset {offset} declares {length} bytes, "
                f"and the reply has {end - at} left"
            )
        yield offset, body[at : at + length]
        at += length


def _segment_for(wanted: ReadRange, answers: list[bytes] | None) -> bytes:
    """One vector-read segment, or a refusal - never a silent empty string."""
    if not answers:
        raise ProtocolError(
            f"the server left the {wanted.length} bytes at offset {wanted.offset} "
            "out of its vector read reply"
        )
    data = answers.pop(0) if len(answers) > 1 else answers[0]
    if len(data) > wanted.length:
        raise ProtocolError(
            f"the server answered a {wanted.length} byte vector-read segment at "
            f"offset {wanted.offset} with {len(data)} bytes"
        )
    return data


def _pieces(rng: ReadRange) -> Iterator[ReadRange]:
    """``rng`` as ``kXR_readv`` elements no longer than the server accepts."""
    if rng.length <= READV_MAX_ELEMENT:
        yield rng
        return
    for start in range(0, rng.length, READV_MAX_ELEMENT):
        yield ReadRange(rng.offset + start, min(READV_MAX_ELEMENT, rng.length - start))


def _assemble(rng: ReadRange, answers: dict[int, list[bytes]]) -> bytes:
    """One requested range, rejoined from the pieces :func:`_pieces` cut it into.

    Every piece is claimed from ``answers``, even after a short one at the end
    of the file, so that a later range starting at the same offset as one of
    them is still handed its own segment and not a leftover.
    """
    parts = [_segment_for(piece, answers.get(piece.offset)) for piece in _pieces(rng)]
    return parts[0] if len(parts) == 1 else b"".join(parts)


def _batches(ranges: Sequence[ReadRange]) -> Iterator[list[ReadRange]]:
    """Split vector reads to respect the server's per-request ceilings."""
    batch: list[ReadRange] = []
    total = 0
    for rng in ranges:
        if batch and (len(batch) >= READV_MAX_CHUNKS or total + rng.length > READV_MAX_BYTES):
            yield batch
            batch, total = [], 0
        batch.append(rng)
        total += rng.length
    if batch:
        yield batch


def _range(item: CloneRange | tuple[int, int] | tuple[int, int, int]) -> CloneRange:
    """One clone range, however it was spelled."""
    span = item if isinstance(item, CloneRange) else CloneRange(*item)
    if span.offset < 0 or span.length < 0 or span.destination < 0:
        raise ValueError(f"a clone range is two offsets and a length, none negative: {span}")
    return span


def _clone_extent(batch: list[tuple[bytes, int, int, int]]) -> tuple[int, int]:
    copied = sum(length for _, _, length, _ in batch)
    extent = max((destination + length for _, _, length, destination in batch), default=0)
    return copied, extent


def _write_pieces(chunk: WriteChunk) -> Iterator[WriteChunk]:
    """``chunk`` as ``kXR_writev`` elements no longer than the server accepts."""
    if len(chunk.data) <= WRITEV_MAX_ELEMENT:
        yield chunk
        return
    for start in range(0, len(chunk.data), WRITEV_MAX_ELEMENT):
        yield WriteChunk(chunk.offset + start, chunk.data[start : start + WRITEV_MAX_ELEMENT])


def _write_batches(chunks: Sequence[WriteChunk]) -> Iterator[list[WriteChunk]]:
    """Split vector writes to respect the server's per-element and per-request ceilings."""
    batch: list[WriteChunk] = []
    total = 0
    for chunk in (piece for whole in chunks for piece in _write_pieces(whole)):
        if batch and (len(batch) >= READV_MAX_CHUNKS or total + len(chunk.data) > READV_MAX_BYTES):
            yield batch
            batch, total = [], 0
        batch.append(chunk)
        total += len(chunk.data)
    if batch:
        yield batch
