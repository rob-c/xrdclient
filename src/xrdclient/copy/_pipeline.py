"""Shared, bounded byte-transfer pipeline; endpoint policy lives in adapters.

Views are valid until the consumer asks for the next chunk. ``recycle=False``
also preserves the lifetime of views retained by existing Xrd stream writers.
No retry happens here: replay safety belongs to the endpoint and job policy.
"""

from __future__ import annotations

import errno
import queue
import threading
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from typing import Any, Protocol, cast


class Reader(Protocol):
    def read(self, size: int) -> bytes: ...


class Writer(Protocol):
    def write(self, data: memoryview) -> int | None: ...


def fill(reader: Reader, buffer: bytearray) -> int:
    readinto = getattr(reader, "readinto", None)
    if readinto is not None:
        return cast("int", readinto(buffer) or 0)
    data = reader.read(len(buffer))
    buffer[: len(data)] = data
    return len(data)


def chunks(reader: Reader, chunk_size: int) -> Iterator[memoryview]:
    buffer = bytearray(chunk_size)
    view = memoryview(buffer)
    while count := fill(reader, buffer):
        yield view[:count]


class ReadAhead:
    """One reader, a bounded buffer window, and exception-safe worker shutdown."""

    def __init__(
        self,
        reader: Reader,
        chunk_size: int,
        depth: int,
        *,
        recycle: bool = False,
        thread_name: str = "xrd-readahead",
    ) -> None:
        self._reader = reader
        self._chunk_size = chunk_size
        self._recycle = recycle
        self._buffers: queue.Queue[bytearray] = queue.Queue()
        for _ in range(max(depth, 2)):
            self._buffers.put(bytearray(chunk_size))
        self._ready: queue.Queue[tuple[bytearray, int] | BaseException | None] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read, name=thread_name, daemon=True)

    def __enter__(self) -> Iterator[memoryview]:
        self._thread.start()
        return self._drain()

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._buffers.put(bytearray())  # wake a worker waiting for a free buffer
        self._thread.join()

    def _read(self) -> None:
        try:
            while not self._stop.is_set():
                buffer = self._buffers.get()
                if self._stop.is_set():
                    break
                count = fill(self._reader, buffer)
                if count <= 0:
                    break
                self._ready.put((buffer, count))
        except BaseException as exc:
            self._ready.put(exc)
        finally:
            self._ready.put(None)

    def _drain(self) -> Iterator[memoryview]:
        while True:
            item = self._ready.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            buffer, count = item
            yield memoryview(buffer)[:count]
            self._buffers.put(buffer if self._recycle else bytearray(self._chunk_size))


def _short_write() -> Exception:
    return OSError(errno.EIO, "The destination stopped accepting data before the chunk was written")


def write_all(
    writer: Writer, piece: memoryview, short_write: Callable[[], Exception] = _short_write
) -> None:
    """Commit one chunk, including writers that accept only part of a buffer."""
    at = 0
    while at < len(piece):
        count = writer.write(piece[at:])
        if count is None:  # legacy plugin/stream writers return no byte count
            return
        if count <= 0 or count > len(piece) - at:
            raise short_write()
        at += count


def pump(
    reader: Reader,
    writer: Writer,
    total: int | None,
    chunk_size: int,
    progress: Callable[[int, int | None], None] | None,
    digest: Any,
    depth: int = 1,
    *,
    recycle: bool = False,
    thread_name: str = "xrd-readahead",
    short_write: Callable[[], Exception] = _short_write,
) -> int:
    source = (
        ReadAhead(reader, chunk_size, depth, recycle=recycle, thread_name=thread_name)
        if depth > 1
        else nullcontext(chunks(reader, chunk_size))
    )
    done = 0
    with source as pieces:
        for piece in pieces:
            write_all(writer, piece, short_write)
            if digest is not None:
                digest.update(piece)
            done += len(piece)
            if progress is not None:
                progress(done, total)
    return done
