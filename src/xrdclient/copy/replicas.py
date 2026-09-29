"""One file read from several servers at once: XrdCl's extreme copy.

``xrdcp --sources N`` (``CopyProcess.add_job(sourcelimit=N)``) asks the
redirector where every replica of the file is - a deep locate, managers
followed down to the servers behind them - and then reads the file from up
to ``N`` of them together (``XrdClXCpCtx.cc``, ``XrdClXCpSrc.cc``). The file
is handed out in blocks, so a fast server simply comes back for more while a
slow one is still busy, and a server that fails part way through a block
gives the rest of it back to whichever reader asks next, then moves on to a
replica nobody has tried. The copy fails only when every replica has.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from contextlib import ExitStack
from typing import IO, TYPE_CHECKING

from ..config import Config
from ..errors import XRootDError
from ..types import LocationInfo
from ..url import XRootDURL

if TYPE_CHECKING:
    from ..io.raw import XRootDRawIO

__all__ = ["NoMoreReplicasError", "locate", "fetch", "BLOCK_SIZE"]

#: XrdCl's ``XCpBlockSize``: the most one reader takes at a time.
BLOCK_SIZE = 128 << 20

#: ``kXR_compress | kXR_prefname``: one entry per server, by name, as
#: XrdCl's ``DeepLocate`` for a copy asks.
_LOCATE_OPTIONS = 0x0001 | 0x0100

#: Called with ``(bytes_done, total_or_None)`` after every chunk.
Progress = Callable[[int, "int | None"], None]


class NoMoreReplicasError(XRootDError):
    """Every replica failed before the whole file had been read."""


def locate(url: XRootDURL, config: Config) -> list[XRootDURL]:
    """Every data server holding ``url``, as ``url`` pointed at each of them.

    A manager in the answer is asked in turn, and so on down, as XrdCl's
    ``DeepLocate`` does; a manager that cannot be reached is passed over,
    but the first question failing - no such file - fails the copy.
    """
    found: dict[str, XRootDURL] = {}
    asked: set[str] = set()
    pending = [url]
    while pending:
        where = pending.pop(0)
        if where.netloc in asked:
            continue
        asked.add(where.netloc)
        for entry in _ask(where, config, first=where is url):
            there = url.evolve(host=entry.host, port=entry.port)
            if entry.is_manager:
                pending.append(there)
            else:
                found.setdefault(there.netloc, there)
    return list(found.values())


def _ask(where: XRootDURL, config: Config, *, first: bool) -> list[LocationInfo]:
    """One ``kXR_locate`` at ``where``; a failure is the copy's only for the first."""
    from ..client import FileSystem

    try:
        with FileSystem(where.with_path("/"), config) as fs:
            return fs.locate(where.path, flags=_LOCATE_OPTIONS)
    except OSError:
        if first:
            raise
        return []


class _Blocks:
    """The file's blocks, handed out one at a time and taken back when a read fails.

    A reader with nothing to do waits while any other reader still holds a
    block, since that block may yet come back unfinished.
    """

    def __init__(self, size: int, block: int) -> None:
        spans = ((offset, min(block, size - offset)) for offset in range(0, size, block))
        self._pending = deque(spans)
        self._busy = 0
        self._stopped = False
        self._ready = threading.Condition()

    def take(self) -> tuple[int, int] | None:
        """The next block to read, or ``None`` when there will be no more."""
        with self._ready:
            while not self._pending and self._busy and not self._stopped:
                self._ready.wait()
            if self._stopped or not self._pending:
                return None
            self._busy += 1
            return self._pending.popleft()

    def done(self, offset: int, length: int, moved: int) -> None:
        """A block is finished with; what ``moved`` did not cover goes back."""
        with self._ready:
            self._busy -= 1
            if moved < length:
                self._pending.appendleft((offset + moved, length - moved))
            self._ready.notify_all()

    def stop(self) -> None:
        """No more blocks for anybody: the copy has failed."""
        with self._ready:
            self._stopped = True
            self._ready.notify_all()


class _Fetch:
    """The state every reader of one copy shares."""

    def __init__(
        self,
        replicas: list[XRootDURL],
        writer: IO[bytes],
        config: Config,
        chunk: int,
        progress: Progress | None,
    ) -> None:
        self._replicas = deque(replicas)
        self._writer = writer
        self._config = config
        self._chunk = chunk
        self._progress = progress
        self._lock = threading.Lock()
        self.size = 0
        self.moved = 0
        self.failures: list[BaseException] = []

    def open_next(self, stack: ExitStack) -> XRootDRawIO | None:
        """A reader on the next replica nobody has tried, or ``None``."""
        from ..io import open_url

        while True:
            with self._lock:
                if not self._replicas:
                    return None
                url = self._replicas.popleft()
            try:
                return stack.enter_context(open_url(url, "rb", buffering=0, config=self._config))
            except OSError as exc:
                self.failures.append(exc)

    def run(self, blocks: _Blocks, first: XRootDRawIO | None = None) -> None:
        """One reader: blocks from a replica, then from the next when it fails."""
        with ExitStack() as stack:
            reader = first if first is not None else self.open_next(stack)
            while reader is not None and (block := blocks.take()) is not None:
                offset, length = block
                moved = 0
                try:
                    moved = self._span(reader, offset, length)
                except BaseException:
                    blocks.stop()
                    raise
                finally:
                    blocks.done(offset, length, moved)
                if moved < length:
                    stack.close()
                    reader = self.open_next(stack)

    def _span(self, reader: XRootDRawIO, offset: int, length: int) -> int:
        """Read one block into the target; the bytes moved before a failure."""
        moved = 0
        while moved < length:
            data = self._read(reader, offset + moved, min(self._chunk, length - moved))
            if not data:
                break  # this replica failed, or is shorter than the file
            self._put(offset + moved, data)
            moved += len(data)
        return moved

    def _read(self, reader: XRootDRawIO, offset: int, length: int) -> bytes:
        """``length`` bytes at ``offset``, or nothing if this server failed to send them."""
        try:
            reader.seek(offset)
            return reader.read(length) or b""
        except OSError as exc:
            self.failures.append(exc)
            return b""

    def _put(self, offset: int, data: bytes) -> None:
        with self._lock:
            self._writer.seek(offset)
            self._writer.write(data)
            self.moved += len(data)
            if self._progress is not None:
                self._progress(self.moved, self.size)


def fetch(
    replicas: list[XRootDURL],
    writer: IO[bytes],
    config: Config,
    *,
    chunk: int,
    sources: int,
    progress: Progress | None = None,
    block: int = BLOCK_SIZE,
) -> int:
    """Read the file from up to ``sources`` of ``replicas`` into ``writer``.

    ``writer`` is written at offsets, in whatever order the servers answer.
    The size is the first replica's that opens; blocks are XrdCl's - no more
    than ``block``, no fewer than one per reader, and never less than a chunk.
    Raises :class:`NoMoreReplicasError` if every replica failed first.
    """
    shared = _Fetch(replicas, writer, config, chunk, progress)
    with ExitStack() as stack:
        first = shared.open_next(stack)
        if first is None:
            raise _exhausted(shared)
        shared.size = first.file.size
        count = min(sources, len(replicas))
        blocks = _Blocks(shared.size, max(chunk, min(block, shared.size // count)))
        _together(shared, blocks, first, count)
    if shared.moved < shared.size or any(isinstance(f, _Fatal) for f in shared.failures):
        raise _exhausted(shared)
    return shared.moved


def _together(shared: _Fetch, blocks: _Blocks, first: XRootDRawIO, count: int) -> None:
    """``count`` readers at once: this thread on ``first``, the rest on their own."""
    readers = [threading.Thread(target=_guarded, args=(shared, blocks)) for _ in range(1, count)]
    for thread in readers:
        thread.start()
    try:
        shared.run(blocks, first)
    finally:
        for thread in readers:
            thread.join()


def _guarded(shared: _Fetch, blocks: _Blocks) -> None:
    """A reader thread: its failure is recorded for the copy to raise."""
    try:
        shared.run(blocks)
    except BaseException as exc:
        shared.failures.insert(0, _Fatal(exc))


class _Fatal(Exception):
    """A reader's failure that is the copy's, not the replica's: raise it."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(str(error))
        self.error = error


def _exhausted(shared: _Fetch) -> BaseException:
    """What to raise when the copy stops: a reader's own failure, else the replicas'."""
    fatal = next((f.error for f in shared.failures if isinstance(f, _Fatal)), None)
    if fatal is not None:
        return fatal
    last = shared.failures[-1] if shared.failures else None
    detail = f": {last}" if last is not None else ""
    return NoMoreReplicasError(f"No more replicas to try{detail}")
