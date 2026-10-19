"""The async facade: everything the synchronous API does, awaitable.

    >>> import xrdclient.aio
    >>> async with xrdclient.aio.open("root://eos.example.org//store/f.root") as fh:
    ...     header = await fh.read(1024)

    >>> async with xrdclient.aio.FileSystem("root://eos.example.org") as fs:
    ...     info = await fs.stat("/store/f.root")
    ...     async for entry in fs.iterdir("/store"):
    ...         print(entry.name)

The objects here are thin awaitable mirrors of :class:`xrdclient.FileSystem` and the
file objects :func:`xrdclient.open` returns: same names, same arguments, same
exceptions, same semantics — with ``await`` in front and ``async for`` over
what used to be a generator. Whatever the sync object supports, so does this:
``root://`` and ``davs://`` alike, because the mirror wraps whichever
implementation the scheme selected.

**How it runs.** Each call is handed to a worker thread of this module's own
pool - :attr:`Config.aio_workers <xrdclient.Config.aio_workers>` threads,
32 unless ``$XRD_AIOWORKERS`` says otherwise, not the event loop's default
executor - so the event loop is never blocked and one coroutine's ``await``
does not stall another's. The caller's :mod:`contextvars` go with the call, as
they do with :func:`asyncio.to_thread`. Consequences worth knowing:

* Concurrent calls on **one** session are multiplexed on its connection,
  exactly as concurrent threads are in the sync API: each is in flight at
  once on a stream of its own, and one the server parks (``kXR_wait``,
  ``kXR_waitresp``) holds up no other. A bulk transfer has the connection to
  itself while it runs. To move four large files at once, open four
  filesystems (or four files) and :func:`asyncio.gather` them.
* Concurrent calls on **one file** are safe - ``gather(fh.pread(...),
  fh.pread(...))`` - because :class:`xrdclient.File` guards its own state
  (readahead, data-stream cursor, re-open after a lost server) with a lock.
  Calls that move the cursor (``read``, ``seek``, ``write``) still race each
  other for *position*, exactly as two threads sharing a file object would;
  use ``pread``/``pwrite`` for concurrent access to one handle.

**Cancellation reaches the request.** Each call runs inside a
:class:`~xrdcore.cancel.CancelScope` of its own, and cancelling the
coroutine - :meth:`asyncio.Task.cancel`, :func:`asyncio.timeout`, a
:class:`asyncio.TaskGroup` whose sibling failed - cancels the scope. The
coroutine raises :class:`asyncio.CancelledError` at once; the worker notices
within :data:`~xrdcore.cancel.POLL` seconds wherever it waits on a
``root://`` connection, and the request it was waiting on is *abandoned*: its
stream id stays out of circulation until the server's late answer has arrived
and been dropped, so the connection stays in step and goes on serving the
calls that come after. Nothing more is sent for a cancelled call - no next
chunk of a copy, no further ``readv`` batch, no retry - and the worker thread
is free for the next call. A bulk transfer stops between frames and leaves
what is still in flight to be dropped the same way; caught mid-frame and
unable to finish it promptly, it closes the connection rather than leave it
out of step, and the next call opens a fresh one. Over ``http(s)``/``s3``, a
request already sent runs to its answer or its timeout, and nothing after it
is sent.

Two things are deliberately *not* cancelled. An :func:`open` already under way
completes on its thread, because abandoning it would strand the server-side
handle it creates; the handle is closed for you rather than leaked. And a
cancelled call's own clean-up round trips (a checkpoint rollback inside a
cancelled ``copy``, removing a partial remote target) are skipped, since they
would wait on the server that was too slow - clean-up you write yourself, in
an ``except``/``finally`` around the ``await``, is a call of its own and runs
normally.

Nothing in the synchronous package imports this module, and this module
imports nothing the synchronous package does not already provide.

**Where to start reading.** :func:`_run` and :func:`_run_owned` are the whole
mechanism; every public coroutine below is one of them around the synchronous
call of the same name, and :class:`_Iterating` does the same for generators.
What a change here must not break:

* Nothing runs a round trip on the event loop's thread. The members left
  synchronous (``closed``, ``mode``, ``abort``, ...) answer from local state.
* A call that creates something on the server for the caller to release - an
  open - goes through :func:`_run_owned`, never :func:`_run`, or a cancelled
  caller leaks a server-side handle.
* Arguments are forwarded by keyword under the synchronous parameter names;
  :func:`_run`'s ``ParamSpec`` makes mypy check them, so a parameter renamed
  in the synchronous API is a type error here rather than a silent default.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import contextvars
import functools
import io
from collections.abc import AsyncIterator, Callable, Generator, Iterable, Iterator, Sequence
from typing import IO, Any, AnyStr, Generic, ParamSpec, TypeVar, cast, overload

from xrdcore import cancel as _cancel
from xrdcore.fanout import Pool

from . import easy as _easy
from .client import Checkpoint as _Checkpoint
from .client import File as _File
from .client import FileSystem as _FileSystem
from .config import Config
from .copy import CopyResult, SyncMode
from .copy import copy as _copy
from .copy import copy_tree as _copy_tree
from .copy import third_party as _third_party
from .easy import Location
from .errors import UnsupportedError, kXR_Unsupported
from .flags import DirListFlags, LocateFlags, PrepareFlags, QueryCode
from .io import open_url as _open_url
from .io.raw import OpenBinaryMode, OpenTextMode
from .log import get_logger
from .path import XRootDPath
from .types import (
    CheckpointInfo,
    ChecksumInfo,
    CloneRange,
    DirEntry,
    LocationInfo,
    PageResult,
    PrepareStatus,
    ProtocolInfo,
    ReadRange,
    SpaceInfo,
    StatInfo,
    VFSInfo,
    WriteChunk,
)
from .url import XRootDURL

_log = get_logger(__name__)

__all__ = [
    "AsyncCheckpoint",
    "AsyncFile",
    "AsyncFileSystem",
    "FileSystem",
    "checksum",
    "copy",
    "copy_tree",
    "exists",
    "glob",
    "is_online",
    "ls",
    "mkdir",
    "move",
    "open",
    "read_bytes",
    "read_text",
    "remove",
    "size",
    "stage",
    "stat",
    "third_party",
    "write_bytes",
    "write_text",
]

T = TypeVar("T")
P = ParamSpec("P")

#: What :func:`copy` and :func:`copy_tree` call with ``(done, total)``.
Progress = Callable[[int, "int | None"], None]


def _workers() -> int:
    """How many threads :data:`_pool` gets: :attr:`Config.aio_workers` when it is made."""
    return Config().aio_workers


#: The worker pool every call here runs on, made on first use (and again in a
#: forked child, whose copy of the parent's pool has no threads).
#:
#: Not the event loop's default executor: that one is sized for the machine's
#: cores (as few as five threads), shared with everything else the
#: application runs in it, and silently caps how many requests this module
#: can have in flight - a ``gather`` of fifty reads would run five at a time
#: and nobody would be told.
_pool = Pool("xrd-aio", _workers)


def _submit(
    call: Callable[[], T], scope: _cancel.CancelScope | None
) -> concurrent.futures.Future[T]:
    """Start ``call`` on the pool, in a copy of the caller's context.

    The copy is what :func:`asyncio.to_thread` would have made: a
    :func:`~xrdclient.deadline` or a :func:`~xrdclient.config.override`
    around the ``await`` reaches the worker thread that does the work. So
    does ``scope``, when there is one, for :func:`_run` to cancel.
    """
    context = contextvars.copy_context()
    if scope is None:
        return _pool.submit(context.run, call)
    return _pool.submit(context.run, _cancel.within, scope, call)


async def _run(func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """Run a blocking library call off the event loop, cancellably.

    The call gets a cancel scope of its own, nested in whatever scope the
    caller is in. Cancelling the awaiting coroutine cancels the scope, which
    abandons the request the worker is waiting on and frees the worker (see
    the module docstring); the coroutine itself raises at once, without
    waiting for the worker to notice - a worker stuck somewhere the scope
    does not reach, such as a TCP connect, must not hold up the event loop's
    view of the cancellation.
    """
    scope = _cancel.CancelScope(_cancel.current())
    future = _submit(functools.partial(func, *args, **kwargs), scope)
    try:
        return await asyncio.wrap_future(future)
    except asyncio.CancelledError:
        scope.cancel()
        raise


def _release_late(future: concurrent.futures.Future[T], release: Callable[[T], None]) -> None:
    """Hand what a cancelled call produced anyway to ``release``, on the pool.

    Runs as the future's done-callback, on whichever thread finished it, and
    only queues the release: closing a file is a round trip, and this may be
    the event loop's own thread.
    """
    if future.cancelled() or future.exception() is not None:
        return
    _pool.submit(_quietly, release, future.result())


def _quietly(release: Callable[[T], None], value: T) -> None:
    """Release what a cancelled call produced, logging rather than raising.

    Nobody is awaiting this - it runs on the pool after the caller has gone -
    so an exception would only reach the executor's unread future. Logged at
    debug: the caller already chose to give up on ``value``.
    """
    try:
        release(value)
    except Exception:
        _log.debug("releasing %r after its caller gave up on it failed", value, exc_info=True)


async def _run_owned(func: Callable[[], T], release: Callable[[T], None]) -> T:
    """:func:`_run` for a call whose result must be released if nobody takes it.

    Not cancellable once it has started: an open abandoned mid-flight would
    still create its handle on the server, and the reply naming it would be
    dropped unread - a server-side handle nobody can close. So the call runs
    to completion, and a cancelled caller has ``release`` applied to
    whatever it eventually returns. One cancelled before it started is
    simply never run.
    """
    future = _submit(func, None)
    try:
        return await asyncio.wrap_future(future)
    except asyncio.CancelledError:
        if not future.cancel():
            future.add_done_callback(lambda done: _release_late(done, release))
        raise


def _close(handle: IO[Any] | io.IOBase) -> None:
    """Close a file an abandoned open produced."""
    handle.close()


def _advance(iterator: Iterator[T]) -> list[T]:
    """The iterator's next item, as a one-item list; empty at its end."""
    for item in iterator:
        return [item]
    return []


class _Iterating(Generic[T]):
    """``async for`` over a synchronous iterator, one step per thread hop.

    The underlying generator stays lazy: nothing is materialised, and a
    directory tree that is never fully walked is never fully listed. Each
    step is a call of its own, so cancelling the ``async for`` abandons the
    listing request in flight and ends the walk.
    """

    __slots__ = ("_factory", "_iterator")

    def __init__(self, factory: Callable[[], Iterable[T]]) -> None:
        """Defer ``factory`` - the call that starts the listing - to the first step.

        Calling it here would run its first round trip on the event loop's thread.
        """
        self._factory = factory
        self._iterator: Iterator[T] | None = None

    def __aiter__(self) -> _Iterating[T]:
        """Itself: single-pass, like the synchronous generator it wraps."""
        return self

    async def __anext__(self) -> T:
        """The next item, fetched on a worker thread; each step is cancellable.

        The iterator is started on a worker too, because a generator's first
        ``next`` (or, for a non-generator iterable, building it) may already be
        a round trip. Items are wrapped in a one-item list by :func:`_advance` so
        that the end is a value rather than a :class:`StopIteration` raised
        across the thread boundary, which :mod:`asyncio` futures cannot carry.
        """
        if self._iterator is None:
            self._iterator = await _run(lambda: iter(self._factory()))
        step = await _run(_advance, self._iterator)
        if not step:
            raise StopAsyncIteration
        return step[0]


class _Opening(Generic[AnyStr]):
    """What :func:`open` returns: awaitable, and an async context manager.

    >>> fh = await xrdclient.aio.open(url)            # explicit close
    >>> async with xrdclient.aio.open(url) as fh:     # closed for you
    ...     ...
    """

    __slots__ = ("_file", "_open")

    def __init__(self, open_: Callable[[], IO[Any] | io.IOBase]) -> None:
        """Hold the open call; nothing is sent until this is awaited or entered."""
        self._open = open_
        self._file: AsyncFile[AnyStr] | None = None

    async def _opened(self) -> AsyncFile[AnyStr]:
        return AsyncFile(await _run_owned(self._open, _close))

    def __await__(self) -> Generator[Any, None, AsyncFile[AnyStr]]:
        """``await open(...)``: the caller owns the file and must ``close`` it."""
        return self._opened().__await__()

    async def __aenter__(self) -> AsyncFile[AnyStr]:
        """``async with open(...) as fh``: the file is closed when the block ends."""
        self._file = await self._opened()
        return self._file

    async def __aexit__(self, *exc: object) -> None:
        """Close the file the block opened; the block's exception, if any, propagates.

        ``async with`` never gets here after a failed open; the ``None``
        check only guards a hand-made call without a successful ``__aenter__``.
        """
        if self._file is not None:
            await self._file.close()


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


class AsyncFile(Generic[AnyStr]):
    """An awaitable file object over whatever :func:`xrdclient.open` returned.

    The read/write surface is :mod:`io`'s, so ``read``, ``write``, ``seek``,
    ``tell``, ``readline``, ``writelines``, ``truncate``, ``flush`` and
    ``close`` are all there and all awaitable. The predicates that answer
    without touching the network — ``readable``, ``writable``, ``seekable``,
    ``closed``, ``mode``, ``name`` — stay synchronous, because making them
    coroutines would buy nothing and cost every caller an ``await``.

    Generic in what it reads and writes: ``AsyncFile[bytes]`` from a binary
    mode, ``AsyncFile[str]`` from a text one, as :func:`open` types them.
    """

    __slots__ = ("_sync",)

    def __init__(self, handle: IO[Any] | io.IOBase) -> None:
        """Wrap an open file object and take ownership: :meth:`close` closes it."""
        self._sync = handle

    # -- what needs no round trip --------------------------------------

    @property
    def raw(self) -> io.IOBase:
        """The synchronous object underneath, for anything not mirrored here."""
        return cast("io.IOBase", self._sync)

    @property
    def file(self) -> _File | None:
        """The protocol-level :class:`xrdclient.File`, on ``root://`` endpoints.

        Buffering and text decoding are layers over the raw object that owns
        the handle, so this digs down through them rather than giving up.
        """
        layer: object = self._sync
        while layer is not None:
            found = getattr(layer, "file", None)
            if isinstance(found, _File):
                return found
            layer = getattr(layer, "buffer", None) or getattr(layer, "raw", None)
        return None

    @property
    def mode(self) -> str:
        """The mode it was opened with, or ``""`` if the layer does not say."""
        return str(getattr(self._sync, "mode", ""))

    @property
    def name(self) -> str:
        """What the handle calls itself - the URL, for a remote file - or ``""``."""
        return str(getattr(self._sync, "name", ""))

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has run. Local state; no round trip."""
        return bool(self._sync.closed)

    def readable(self) -> bool:
        """From the open mode, as :meth:`io.IOBase.readable`; no round trip."""
        return bool(self._sync.readable())

    def writable(self) -> bool:
        """From the open mode, as :meth:`io.IOBase.writable`; no round trip."""
        return bool(self._sync.writable())

    def seekable(self) -> bool:
        """From the open mode, as :meth:`io.IOBase.seekable`; no round trip."""
        return bool(self._sync.seekable())

    def fileno(self) -> int:
        """Raises :class:`io.UnsupportedOperation`: a remote file has no OS descriptor.

        Kept so code probing for a descriptor gets the refusal :mod:`io`
        callers already handle (``TextIOWrapper`` among them), not an
        :class:`AttributeError`.
        """
        return self._sync.fileno()

    @property
    def is_open(self) -> bool:
        """The opposite of :attr:`closed`, spelled as :class:`xrdclient.File` spells it."""
        return not self.closed

    @property
    def endpoint(self) -> str:
        """``host:port`` of the server holding the handle. ``root://`` only."""
        return self._native("endpoint").endpoint

    @property
    def compression(self) -> tuple[int, str]:
        """What the open reported about compression. ``root://`` only."""
        return self._native("compression").compression

    @property
    def recoverable(self) -> bool:
        """Whether a lost connection is re-opened rather than fatal. ``root://`` only."""
        return self._native("recoverable").recoverable

    # -- reading -------------------------------------------------------

    async def read(self, size: int = -1) -> AnyStr:
        """Up to ``size`` bytes (or characters in text mode); all of it if ``-1``."""
        return await _run(self._io.read, size)

    async def readinto(self, buffer: bytearray | memoryview) -> int:
        """Fill ``buffer`` in place; returns how many bytes arrived."""
        return await _run(cast("io.BufferedIOBase", self._sync).readinto, buffer)

    async def readline(self, size: int = -1) -> AnyStr:
        """One line, up to ``size``; ``""``/``b""`` at end of file.

        Served from the buffer where it can be, so most lines cost a thread hop
        and no round trip; ``async for line in fh`` is the same call in a loop.
        """
        return await _run(self._io.readline, size)

    async def readlines(self, hint: int = -1) -> list[AnyStr]:
        """Every remaining line (or about ``hint`` bytes of them), on one worker hop."""
        return await _run(self._io.readlines, hint)

    async def readv(self, ranges: Iterable[ReadRange | tuple[int, int]]) -> list[bytes]:
        """One ``kXR_readv``: many ranges, one round trip. ``root://`` only."""
        return await _run(self._native("readv").readv, ranges)

    async def pread(self, size: int, offset: int) -> bytes:
        """:func:`os.pread`: ``size`` bytes at ``offset``, the cursor left alone. ``root://`` only.

        It goes to the server directly, past this object's buffer, so writes
        still sitting in that buffer are flushed first and the read sees them.
        """
        native = self._native("pread")
        return await _run(self._flushed, functools.partial(native.pread, size, offset))

    async def pgread(self, size: int, offset: int, *, verify: bool = True) -> PageResult:
        """Paged read with per-page CRC32c. ``root://`` only."""
        return await _run(self._native("pgread").pgread, size, offset, verify=verify)

    # -- writing -------------------------------------------------------

    async def write(self, data: AnyStr) -> int:
        """Buffer ``data`` at the cursor; returns how much was accepted.

        Usually no round trip until the buffer fills - call :meth:`flush` (or
        :meth:`close`) to know it has reached the server. Cancelled mid-flush,
        part of the buffer may have been written.
        """
        return await _run(self._io.write, data)

    async def writelines(self, lines: Iterable[AnyStr]) -> None:
        """:meth:`write` each of ``lines`` in turn, on one worker hop; no separators added."""
        await _run(self._io.writelines, lines)

    async def pwrite(self, data: bytes, offset: int) -> int:
        """:func:`os.pwrite`: ``data`` at ``offset``, the cursor left alone. ``root://`` only.

        Buffered writes are flushed first, so an earlier ``write`` cannot land
        on top of this one later. Bytes this object has already read ahead
        into its buffer are not refreshed: open with ``buffering=0`` to mix
        positional writes with cursor reads of the same range.
        """
        native = self._native("pwrite")
        return await _run(self._flushed, functools.partial(native.pwrite, data, offset))

    async def writev(
        self, chunks: Iterable[WriteChunk | tuple[int, bytes]], *, sync: bool = False
    ) -> int:
        """Scattered writes in one round trip. ``root://`` only."""
        return await _run(self._native("writev").writev, chunks, sync=sync)

    async def clone(
        self,
        source: AsyncFile[Any] | _File,
        ranges: Iterable[CloneRange | tuple[int, int] | tuple[int, int, int]] | None = None,
    ) -> int:
        """Copy ranges of ``source`` into this file, server-side. ``root://`` only."""
        native = source._native("clone") if isinstance(source, AsyncFile) else source
        return await _run(self._native("clone").clone, native, ranges)

    async def pgwrite(self, data: bytes, offset: int = 0) -> int:
        """Paged write with per-page CRC32c. ``root://`` only."""
        return await _run(self._native("pgwrite").pgwrite, data, offset)

    async def truncate(self, size: int | None = None) -> int:
        """Resize to ``size`` (the cursor, if ``None``); returns the new size."""
        return await _run(self._sync.truncate, size)

    async def flush(self) -> None:
        """Push buffered writes to the server. Not ``kXR_sync``: see :meth:`sync`."""
        await _run(self._sync.flush)

    async def sync(self) -> None:
        """Commit to storage (``kXR_sync``), after flushing what is buffered."""
        await _run(self._flushed, self._native("sync").sync)

    # -- position ------------------------------------------------------

    async def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Move the cursor; returns the new position.

        A thread hop because seeking a buffered writer flushes it first. Races
        any concurrent ``read``/``write`` on this object for the position.
        """
        return await _run(self._sync.seek, offset, whence)

    async def tell(self) -> int:
        """The cursor position, buffered data accounted for."""
        return await _run(self._sync.tell)

    async def bind_data_path(self) -> int:
        """Move bulk I/O onto a second connection. ``root://`` only.

        See :meth:`xrdclient.File.bind_data_path`. It costs a connection and a
        handshake, so it is worth it for a file being streamed and not for
        one being peeked at.
        """
        return await _run(self._native("bind_data_path").bind_data_path)

    @property
    def data_path(self) -> int:
        """The bound data path this file's bulk I/O uses, or 0 for none."""
        found = self.file
        return found.data_path if found is not None else 0

    # -- metadata ------------------------------------------------------

    async def stat(self, *, refresh: bool = False) -> StatInfo:
        """``kXR_stat`` on the open handle. ``root://`` only."""
        return await _run(self._native("stat").stat, refresh=refresh)

    async def checksum(self, algorithm: str | None = None) -> ChecksumInfo:
        """The server's checksum for this file. ``root://`` only."""
        return await _run(self._native("checksum").checksum, algorithm)

    async def size(self) -> int:
        """File length, cached from the open. ``root://`` only.

        A coroutine here where :attr:`xrdclient.File.size` is a property,
        because it can cost a round trip when nothing was cached.
        """
        native = self._native("size")
        return await _run(lambda: native.size)

    async def verify(self, expected: str, algorithm: str = "adler32") -> None:
        """Raise :class:`~xrdclient.ChecksumMismatchError` on a mismatch. ``root://`` only."""
        await _run(self._native("verify").verify, expected, algorithm)

    async def visa(self) -> bytes:
        """``kXR_query`` visa for the open handle. ``root://`` only."""
        return await _run(self._native("visa").visa)

    async def fcntl(self, data: bytes = b"") -> bytes:
        """``data`` for the storage plug-in, and its answer (``Fcntl``). ``root://`` only."""
        return await _run(self._native("fcntl").fcntl, data)

    async def write_pieces(
        self,
        pieces: Iterable[bytes | bytearray | memoryview],
        offset: int = 0,
        *,
        depth: int | None = None,
        acknowledged: Callable[[int, int], None] | None = None,
        waited: Callable[[int, int], None] | None = None,
        check: Callable[[], None] | None = None,
    ) -> int:
        """:meth:`xrdclient.File.write_pieces` on a worker thread. ``root://`` only.

        The callbacks run on that thread, not the event loop's.
        """
        native = self._native("write_pieces")
        await _run(self._sync.flush)
        return await _run(
            native.write_pieces,
            pieces,
            offset,
            depth=depth,
            acknowledged=acknowledged,
            waited=waited,
            check=check,
        )

    def abort(self) -> None:
        """:meth:`xrdclient.File.abort`, called straight from the event loop. ``root://`` only.

        Not a coroutine, for the reason :meth:`AsyncFileSystem.abort` is not.
        """
        self._native("abort").abort()

    # -- extended attributes -------------------------------------------

    async def getxattr(self, name: str) -> bytes:
        """One attribute of the open file. ``root://`` only."""
        return await _run(self._native("getxattr").getxattr, name)

    async def setxattr(self, name: str, value: bytes) -> None:
        """Set one attribute of the open file. ``root://`` only."""
        await _run(self._native("setxattr").setxattr, name, value)

    async def listxattr(self) -> list[str]:
        """The attribute names of the open file. ``root://`` only."""
        return await _run(self._native("listxattr").listxattr)

    async def removexattr(self, name: str) -> None:
        """Remove one attribute of the open file. ``root://`` only."""
        await _run(self._native("removexattr").removexattr, name)

    @contextlib.asynccontextmanager
    async def checkpoint(self) -> AsyncIterator[AsyncCheckpoint]:
        """Transactional writes: committed on a clean exit, rolled back on one.

            >>> async with fh.checkpoint() as cp:      # doctest: +SKIP
            ...     await fh.write(payload)
            ...     await fh.flush()
            ...     (await cp.query()).free

        The journal only sees what has reached the server, so flush inside the
        block - a buffered write still sitting in the buffer is written after
        the commit, not inside it. ``root://`` only, and only where the server
        implements ``kXR_chkpoint``. A block left by a cancellation is rolled
        back like any other that raised: the rollback is a call of its own.
        """
        block = self._native("checkpoint").checkpoint()
        started = await _run(block.__enter__)
        try:
            yield AsyncCheckpoint(started)
        except BaseException as exc:
            await _run(block.__exit__, type(exc), exc, exc.__traceback__)
            raise
        else:
            await _run(block.__exit__, None, None, None)

    # -- lifetime ------------------------------------------------------

    async def close(self) -> None:
        """Flush and release. Safe to call more than once."""
        await _run(self._sync.close)

    async def __aenter__(self) -> AsyncFile[AnyStr]:
        """Itself; the file is already open."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """:meth:`close`, whether or not the block raised."""
        await self.close()

    def __aiter__(self) -> AsyncIterator[AnyStr]:
        """``async for line in fh``: :meth:`readline` until end of file.

        A fresh generator each time, sharing this file's cursor - two loops
        over one file take turns at its lines rather than each seeing them all.
        """
        return self._lines()

    async def _lines(self) -> AsyncIterator[AnyStr]:
        while line := await self.readline():
            yield line

    @property
    def _io(self) -> IO[AnyStr]:
        """The handle, typed by what this file reads and writes."""
        return cast("IO[AnyStr]", self._sync)

    def _flushed(self, call: Callable[[], T]) -> T:
        """``call`` after the buffer is flushed, both on one worker thread.

        Positional I/O goes straight to the protocol file, so anything still
        buffered above it has to reach the server first or the two would be
        applied out of order.
        """
        self._sync.flush()
        return call()

    def _native(self, operation: str) -> _File:
        """The protocol-level file, or a clear refusal if there is not one."""
        found = self.file
        if found is None:
            raise UnsupportedError(
                kXR_Unsupported, f"{operation} needs a root:// endpoint, not {self.name}"
            )
        return found

    def __repr__(self) -> str:
        """``AsyncFile(<the synchronous object's repr>)``."""
        return f"AsyncFile({self._sync!r})"


class AsyncCheckpoint:
    """The checkpoint an :meth:`AsyncFile.checkpoint` block is writing into."""

    __slots__ = ("_sync",)

    def __init__(self, checkpoint: _Checkpoint) -> None:
        """Mirror a checkpoint already begun; :meth:`AsyncFile.checkpoint` owns its end."""
        self._sync = checkpoint

    async def query(self) -> CheckpointInfo:
        """``kXR_ckpQuery`` - how much room the journal has left."""
        return await _run(self._sync.query)

    def __repr__(self) -> str:
        """``AsyncCheckpoint('<url of the file being written>')``."""
        return f"AsyncCheckpoint({str(self._sync.file.url)!r})"


# ---------------------------------------------------------------------------
# Filesystems
# ---------------------------------------------------------------------------


class AsyncFileSystem:
    """Namespace operations on one endpoint, awaitable.

        >>> async with AsyncFileSystem("root://eos.example.org") as fs:
        ...     await fs.makedirs("/store/user/me/out", exist_ok=True)
        ...     await fs.write_bytes("/store/user/me/out/f.bin", b"data")

    Constructing one touches no network — the connection is made on the first
    call that needs it, as in the sync API. The scheme picks the
    implementation, so ``davs://`` works here too and raises
    :class:`~xrdclient.UnsupportedError` for the handful of operations HTTP has no
    verb for.
    """

    __slots__ = ("_sync",)

    def __init__(self, url: str | XRootDURL, config: Config | None = None) -> None:
        """Build the synchronous filesystem now, and own it.

        No network yet. :meth:`close` (or leaving ``async with``) releases the
        connection it makes on first use.
        """
        self._sync = _FileSystem(url, config)

    @classmethod
    def wrap(cls, filesystem: _FileSystem) -> AsyncFileSystem:
        """Mirror a filesystem that already exists, sharing its connection.

        The mirror does not take ownership away from the caller, but it can
        end it: :meth:`close` on the mirror closes ``filesystem`` too.
        """
        mirrored = cls.__new__(cls)
        mirrored._sync = filesystem
        return mirrored

    # -- what needs no round trip --------------------------------------

    @property
    def sync(self) -> _FileSystem:
        """The synchronous filesystem underneath."""
        return self._sync

    @property
    def url(self) -> XRootDURL:
        """The endpoint URL this filesystem was made for."""
        return self._sync.url

    @property
    def config(self) -> Config:
        """The settings every call on this filesystem uses."""
        return self._sync.config

    @property
    def endpoint(self) -> str:
        """``host:port`` of the server requests go to. No round trip."""
        return self._sync.endpoint

    # -- interrogation -------------------------------------------------

    async def ping(self) -> None:
        """Round-trip the server. Raises if it is unhealthy."""
        await _run(self._sync.ping)

    async def protocol(self) -> ProtocolInfo:
        """Capabilities of the endpoint, from the connection's negotiation."""
        return await _run(self._sync.protocol)

    async def stat(self, path: str, *, follow_symlinks: bool = True) -> StatInfo:
        """``kXR_stat`` (or the scheme's equivalent); :class:`FileNotFoundError` if absent."""
        return await _run(self._sync.stat, path, follow_symlinks=follow_symlinks)

    async def lstat(self, path: str) -> StatInfo:
        """``os.lstat``: stat a symbolic link rather than what it points at."""
        return await _run(self._sync.lstat, path)

    async def is_symlink(self, path: str) -> bool:
        """Whether ``path`` is a symbolic link; see the sync method for servers that cannot say."""
        return await _run(self._sync.is_symlink, path)

    async def statvfs(self, path: str = "/") -> VFSInfo:
        """Space and staging utilisation, in ``os.statvfs`` spirit."""
        return await _run(self._sync.statvfs, path)

    async def statx(self, paths: Sequence[str]) -> list[StatInfo]:
        """Flags-only stat of many paths in one round trip; one result per path, in order."""
        return await _run(self._sync.statx, paths)

    async def exists(self, path: str) -> bool:
        """``False`` for a missing path; any other failure (auth, network) still raises."""
        return await _run(self._sync.exists, path)

    async def isdir(self, path: str) -> bool:
        """``True`` for an existing directory, ``False`` if it is absent or not one."""
        return await _run(self._sync.isdir, path)

    async def isfile(self, path: str) -> bool:
        """``True`` for an existing regular file, ``False`` if it is absent or not one."""
        return await _run(self._sync.isfile, path)

    async def getsize(self, path: str) -> int:
        """Size in bytes, from a stat; :class:`FileNotFoundError` if absent."""
        return await _run(self._sync.getsize, path)

    async def checksum(self, path: str, algorithm: str | None = None) -> ChecksumInfo:
        """Server-computed checksum of ``path``; the server's default algorithm if none.

        Cancelling abandons the wait, not the server's computation of a large
        file - :meth:`checksum_cancel` asks it to stop.
        """
        return await _run(self._sync.checksum, path, algorithm)

    async def query(self, code: QueryCode | int | str, args: str = "") -> bytes:
        """Raw ``kXR_query``: the answer's bytes, uninterpreted."""
        return await _run(self._sync.query, code, args)

    async def query_config(self, *names: str) -> dict[str, str]:
        """Server configuration values (``kXR_Qconfig``), one per name it has a value for."""
        return await _run(self._sync.query_config, *names)

    async def extensions(self) -> frozenset[str]:
        """Which vendor operations the server admits to implementing."""
        return await _run(self._sync.extensions)

    async def query_stats(self, selectors: str = "a") -> str:
        """Server statistics (``kXR_QStats``), the XML verbatim."""
        return await _run(self._sync.query_stats, selectors)

    async def query_space(self, path: str = "/") -> SpaceInfo:
        """Space available where ``path`` lives (``kXR_Qspace``)."""
        return await _run(self._sync.query_space, path)

    async def checksum_cancel(self, path: str) -> None:
        """Ask the server to stop a checksum it is still computing (``kXR_Qckscan``)."""
        await _run(self._sync.checksum_cancel, path)

    async def set_property(self, directive: str) -> None:
        """Set a per-connection property on the server (``kXR_set``).

        ``directive`` is the protocol's own ``"<what> <value>"`` text; for the
        one clients are given, :meth:`appid` is the clearer spelling.
        """
        await _run(self._sync.set_property, directive)

    async def appid(self, name: str) -> None:
        """Label this connection in the server's monitoring stream."""
        await _run(self._sync.appid, name)

    async def locate(
        self,
        path: str,
        *,
        create: bool = False,
        refresh: bool = False,
        no_wait: bool = False,
        add_peers: bool = False,
        prefer_name: bool = False,
        flags: LocateFlags | int | str | None = None,
    ) -> list[LocationInfo]:
        """Which servers hold ``path`` (``kXR_locate``); the keywords are its option bits.

        A manager answers with the data servers (or sub-managers) behind it;
        :meth:`deep_locate` keeps asking until only data servers remain.
        """
        return await _run(
            self._sync.locate,
            path,
            create=create,
            refresh=refresh,
            no_wait=no_wait,
            add_peers=add_peers,
            prefer_name=prefer_name,
            flags=flags,
        )

    async def deep_locate(self, path: str, *, create: bool = False) -> list[LocationInfo]:
        """Locate, resolving managers down to the servers behind them.

        Several round trips, to several hosts; cancelling stops the descent.
        """
        return await _run(self._sync.deep_locate, path, create=create)

    async def prepare(
        self,
        paths: Sequence[str],
        *,
        stage: bool | None = None,
        evict: bool = False,
        notify: bool = False,
        fresh: bool = False,
        priority: int = 0,
        flags: PrepareFlags | int | str | None = None,
        cancel: bool = False,
        reqid: str | None = None,
    ) -> str:
        """Ask the site to bring files onto disk (``kXR_prepare``); returns the request handle.

        The handle names the request for :meth:`query_prepare` and
        :meth:`cancel_prepare`. Cancelling this call only abandons the wait for
        that handle: a stage the server has already queued goes ahead.
        """
        return await _run(
            self._sync.prepare,
            paths,
            stage=stage,
            evict=evict,
            notify=notify,
            fresh=fresh,
            priority=priority,
            flags=flags,
            cancel=cancel,
            reqid=reqid,
        )

    async def evict(self, paths: Sequence[str]) -> str:
        """Ask the server to drop its cached copies (a ``kXR_prepare`` with the evict bit)."""
        return await _run(self._sync.evict, paths)

    async def query_prepare(self, handle: str, paths: Sequence[str]) -> list[PrepareStatus]:
        """How the staging request ``handle`` is going (``kXR_QPrep``), one entry per path."""
        return await _run(self._sync.query_prepare, handle, paths)

    async def cancel_prepare(self, handle: str, paths: Sequence[str] = ()) -> None:
        """Withdraw the staging request ``handle``, or only ``paths`` of it."""
        await _run(self._sync.cancel_prepare, handle, paths)

    async def archive_info(self, paths: Sequence[str]) -> list[PrepareStatus]:
        """Where each file lives (disk, tape), without asking for any of it to move."""
        return await _run(self._sync.archive_info, paths)

    # -- listing -------------------------------------------------------

    async def scandir(
        self,
        path: str = "",
        *,
        stat: bool = True,
        online: bool = False,
        algorithm: str = "",
        flags: DirListFlags | int | str | None = None,
    ) -> list[DirEntry]:
        """Directory entries, with stat information attached where asked (``kXR_dstat``).

        A list, not an iterator: ``kXR_dirlist`` has no cursor, so the whole
        listing arrives before the first entry could be used anyway. Use
        :meth:`iterdir` to consume it with ``async for``.
        """
        return await _run(
            self._sync.scandir, path, stat=stat, online=online, algorithm=algorithm, flags=flags
        )

    async def dirlist(
        self,
        path: str = "",
        *,
        stat: bool = True,
        online: bool = False,
        flags: DirListFlags | int | str | None = None,
    ) -> bytes:
        """The raw ``kXR_dirlist`` answer; see :meth:`xrdclient.FileSystem.dirlist`."""
        return await _run(self._sync.dirlist, path, stat=stat, online=online, flags=flags)

    async def listdir(self, path: str = "") -> list[str]:
        """Entry names only, like :func:`os.listdir`."""
        return await _run(self._sync.listdir, path)

    async def list_archive(self, path: str) -> list[DirEntry]:
        """The members of the ZIP archive at ``path``, from its central directory."""
        return await _run(self._sync.list_archive, path)

    def iterdir(self, path: str = "") -> _Iterating[DirEntry]:
        """``async for entry in fs.iterdir("/store")``."""
        return _Iterating(functools.partial(self._sync.iterdir, path))

    def walk(
        self,
        top: str = "",
        *,
        topdown: bool = True,
        onerror: object = None,
        followlinks: bool = False,
    ) -> _Iterating[tuple[str, list[str], list[str]]]:
        """``async for root, dirs, files in fs.walk("/store")``.

        ``onerror`` is called from the worker thread doing the listing, not
        from the event loop, so it must not touch loop-bound objects.
        """
        return _Iterating(
            functools.partial(
                self._sync.walk, top, topdown=topdown, onerror=onerror, followlinks=followlinks
            )
        )

    def glob(self, pattern: str, *, root: str = "") -> _Iterating[str]:
        """``async for path in fs.glob("/store/*.root")``."""
        return _Iterating(functools.partial(self._sync.glob, pattern, root=root))

    # -- mutation ------------------------------------------------------

    async def mkdir(
        self, path: str, mode: int | str = 0o755, *, parents: bool = False, exist_ok: bool = False
    ) -> None:
        """Create a directory with :meth:`pathlib.Path.mkdir` semantics.

        ``parents`` may be several round trips; a cancelled call can leave the
        upper directories made and the leaf not.
        """
        await _run(self._sync.mkdir, path, mode, parents=parents, exist_ok=exist_ok)

    async def makedirs(self, path: str, mode: int | str = 0o755, exist_ok: bool = False) -> None:
        """:func:`os.makedirs`: every missing directory along ``path``."""
        await _run(self._sync.makedirs, path, mode, exist_ok)

    async def rmdir(self, path: str) -> None:
        """Remove an empty directory."""
        await _run(self._sync.rmdir, path)

    async def remove(self, path: str) -> None:
        """Remove a file (``kXR_rm``); :class:`FileNotFoundError` if absent."""
        await _run(self._sync.remove, path)

    #: ``os``'s spelling of the same call.
    unlink = remove

    async def rmtree(self, path: str, *, ignore_errors: bool = False) -> None:
        """``shutil.rmtree``, one removal per entry - there is no server-side primitive.

        Cancelling stops between removals and leaves the tree partly deleted;
        symbolic links are removed, never followed (see the sync method).
        """
        await _run(self._sync.rmtree, path, ignore_errors=ignore_errors)

    async def rename(self, src: str, dst: str) -> None:
        """Rename within the same storage element (``kXR_mv``); not a cross-server copy."""
        await _run(self._sync.rename, src, dst)

    #: :mod:`shutil`'s spelling of the same call.
    move = rename

    async def chmod(self, path: str, mode: int | str) -> None:
        """Change a path's permission bits; ``mode`` may be octal digits as a string."""
        await _run(self._sync.chmod, path, mode)

    async def utime(
        self,
        path: str,
        times: tuple[float, float] | None = None,
        *,
        ns: tuple[int, int] | None = None,
    ) -> None:
        """``os.utime``: set access and modification times; a vendor extension over ``root://``."""
        await _run(self._sync.utime, path, times, ns=ns)

    async def chown(self, path: str, uid: int = -1, gid: int = -1) -> None:
        """``os.chown``: change ownership; ``-1`` leaves an id alone."""
        await _run(self._sync.chown, path, uid, gid)

    async def truncate(self, path: str, size: int) -> None:
        """Resize a file by path, without opening it (``kXR_truncate``)."""
        await _run(self._sync.truncate, path, size)

    async def symlink(self, target: str, link: str) -> None:
        """``os.symlink`` order. A vendor extension - see the sync method."""
        await _run(self._sync.symlink, target, link)

    async def link(self, src: str, dst: str) -> None:
        """``os.link`` order. A vendor extension - see the sync method."""
        await _run(self._sync.link, src, dst)

    #: ``os``'s spelling of the same call.
    hardlink = link

    async def readlink(self, path: str) -> str:
        """What a symbolic link points at. A vendor extension."""
        return await _run(self._sync.readlink, path)

    async def touch(self, path: str, *, exist_ok: bool = True) -> None:
        """Create an empty file if absent; an existing file's mtime is left alone."""
        await _run(self._sync.touch, path, exist_ok=exist_ok)

    # -- extended attributes -------------------------------------------

    async def getxattr(self, path: str, name: str) -> bytes:
        """One extended attribute's value (``kXR_fattr`` get)."""
        return await _run(self._sync.getxattr, path, name)

    async def setxattr(
        self, path: str, name: str, value: bytes, *, create_only: bool = False
    ) -> None:
        """Set one extended attribute; ``create_only`` refuses to replace an existing one."""
        await _run(self._sync.setxattr, path, name, value, create_only=create_only)

    async def removexattr(self, path: str, name: str) -> None:
        """Remove one extended attribute."""
        await _run(self._sync.removexattr, path, name)

    async def listxattr(self, path: str) -> list[str]:
        """The names of ``path``'s extended attributes."""
        return await _run(self._sync.listxattr, path)

    async def listxattr_tree(self, path: str) -> dict[str, list[str]]:
        """Attribute names for a whole subtree, ``relative path -> names``; many round trips."""
        return await _run(self._sync.listxattr_tree, path)

    async def xattrs(self, path: str) -> dict[str, bytes]:
        """Every extended attribute and its value."""
        return await _run(self._sync.xattrs, path)

    # -- whole files ---------------------------------------------------

    async def read_bytes(self, path: str) -> bytes:
        """The whole file, in one open-read-close on a worker thread."""
        return await _run(self._sync.read_bytes, path)

    async def write_bytes(self, path: str, data: bytes) -> int:
        """Replace the file's contents with ``data``; returns the byte count.

        Cancelled mid-write, the target may be left truncated or partial.
        """
        return await _run(self._sync.write_bytes, path, data)

    async def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """The whole file, decoded; UTF-8 by default, not the locale's encoding."""
        return await _run(self._sync.read_text, path, encoding)

    async def write_text(self, path: str, text: str, encoding: str = "utf-8") -> int:
        """Replace the file's contents with ``text``; returns the character count."""
        return await _run(self._sync.write_text, path, text, encoding)

    def open(
        self,
        path: str,
        mode: str = "rb",
        *,
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
        posc: bool = False,
    ) -> _Opening[Any]:
        """Open a file on this endpoint; ``await`` it or ``async with`` it.

        Typed loosely, as :meth:`xrdclient.FileSystem.open` is; for a file
        typed by its mode, use :func:`xrdclient.aio.open`.
        """
        return _Opening(
            functools.partial(
                self._sync.open,
                path,
                mode,
                buffering=buffering,
                encoding=encoding,
                errors=errors,
                newline=newline,
                posc=posc,
            )
        )

    # -- lifetime ------------------------------------------------------

    async def close(self) -> None:
        """Release this filesystem's connection. Idempotent.

        Over ``root://`` the connection goes back to the pool, or stays open while
        a file opened from this filesystem still uses it.
        """
        await _run(self._sync.close)

    def abort(self) -> None:
        """:meth:`xrdclient.FileSystem.abort`, called straight from the event loop.

        Not a coroutine: it never blocks, and it has to reach a call that is
        blocked in a worker thread right now - which waiting for a worker of
        its own could not promise to do.
        """
        self._sync.abort()

    async def __aenter__(self) -> AsyncFileSystem:
        """Itself; nothing is connected until the first call."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """:meth:`close`, whether or not the block raised."""
        await self.close()

    def __repr__(self) -> str:
        """``AsyncFileSystem('<endpoint url>')``."""
        return f"AsyncFileSystem({str(self._sync.url)!r})"


#: ``xrdclient.aio.FileSystem`` reads better at a call site than ``AsyncFileSystem``,
#: and the module name already says which one you have.
FileSystem = AsyncFileSystem
File = AsyncFile


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


@overload
def open(
    url: str | XRootDURL,
    mode: OpenBinaryMode = "rb",
    *,
    buffering: int = -1,
    encoding: None = None,
    errors: None = None,
    newline: None = None,
    config: Config | None = None,
    posc: bool = False,
    member: str | None = None,
) -> _Opening[bytes]:
    """A binary mode (the default) types the file as ``AsyncFile[bytes]``."""


@overload
def open(
    url: str | XRootDURL,
    mode: OpenTextMode,
    *,
    buffering: int = -1,
    encoding: str | None = None,
    errors: str | None = None,
    newline: str | None = None,
    config: Config | None = None,
    posc: bool = False,
    member: str | None = None,
) -> _Opening[str]:
    """A text mode types the file as ``AsyncFile[str]``."""


@overload
def open(
    url: str | XRootDURL,
    mode: str,
    *,
    buffering: int = -1,
    encoding: str | None = None,
    errors: str | None = None,
    newline: str | None = None,
    config: Config | None = None,
    posc: bool = False,
    member: str | None = None,
) -> _Opening[Any]:
    """A mode only known at run time types the file loosely."""


def open(
    url: str | XRootDURL,
    mode: str = "rb",
    *,
    buffering: int = -1,
    encoding: str | None = None,
    errors: str | None = None,
    newline: str | None = None,
    config: Config | None = None,
    posc: bool = False,
    member: str | None = None,
) -> _Opening[Any]:
    """:func:`xrdclient.open`, awaitable.

        >>> async with xrdclient.aio.open("root://host//store/f.root") as fh:
        ...     header = await fh.read(1024)

    The returned object is both awaitable and an async context manager, so
    either spelling works and only one of them leaves you to call ``close``.
    A binary mode gives an ``AsyncFile[bytes]`` and a text mode an
    ``AsyncFile[str]``, as the synchronous :func:`xrdclient.open` types them.
    """
    return _Opening(
        lambda: _open_url(
            url,
            mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
            config=config,
            posc=posc,
            member=member,
        )
    )


async def copy(
    source: Any,
    target: Any,
    *,
    chunk_size: int | None = None,
    verify: bool | None = None,
    algorithm: str | None = None,
    overwrite: bool = True,
    progress: Progress | None = None,
    config: Config | None = None,
    dry_run: bool = False,
    remove_source: bool = False,
    resume: bool = False,
    sources: int = 1,
    dynamic_source: bool = False,
    max_rate: float | None = None,
    min_rate: float | None = None,
    timeout: float | None = None,
    coerce: bool = False,
) -> CopyResult:
    """:func:`xrdclient.copy`, awaitable. ``progress`` is called from the worker thread.

    Cancelling it stops the transfer: the chunk in flight is abandoned and
    nothing more is read or written. What the cancelled copy left at the
    target is yours to remove; a ``root://`` target opened with ``posc`` is
    removed by the server when its writer goes.
    """
    return await _run(
        _copy,
        source,
        target,
        chunk_size=chunk_size,
        verify=verify,
        algorithm=algorithm,
        overwrite=overwrite,
        progress=progress,
        config=config,
        dry_run=dry_run,
        remove_source=remove_source,
        resume=resume,
        sources=sources,
        dynamic_source=dynamic_source,
        max_rate=max_rate,
        min_rate=min_rate,
        timeout=timeout,
        coerce=coerce,
    )


async def copy_tree(
    source: Any,
    target: Any,
    *,
    config: Config | None = None,
    progress: Progress | None = None,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    sync: SyncMode | None = None,
    delete: bool = False,
    workers: int | None = None,
    **options: Any,
) -> list[CopyResult]:
    """:func:`xrdclient.copy_tree`, awaitable. Cancelling it stops every file in flight."""
    return await _run(
        _copy_tree,
        source,
        target,
        config=config,
        progress=progress,
        include=include,
        exclude=exclude,
        sync=sync,
        delete=delete,
        workers=workers,
        **options,
    )


async def third_party(
    source: str | XRootDURL,
    target: str | XRootDURL,
    *,
    config: Config | None = None,
    overwrite: bool = True,
    posc: bool = True,
    token_mode: str = "",
    timeout: float | None = None,
    verify: bool | None = None,
    algorithm: str | None = None,
    init_timeout: float | None = None,
    coerce: bool = False,
    cancel: _cancel.Cancel | None = None,
    progress: Callable[[int, int | None], None] | None = None,
    delegate: bool = False,
    streams: int = 1,
    target_config: Config | None = None,
    cleanup: bool = True,
) -> CopyResult:
    """:func:`xrdclient.third_party`, awaitable.

    Cancelling it stops this process waiting - over ``root://``, the
    destination's ``kXR_sync`` is abandoned - but not the pull the two
    servers have already agreed on, which is theirs to finish or give up.
    To stop the pull itself, as XrdCl does, pass ``cancel=`` (a
    :class:`threading.Event`) and set it; ``progress`` is called on the
    worker thread.
    """
    return await _run(
        _third_party,
        source,
        target,
        config=config,
        overwrite=overwrite,
        posc=posc,
        token_mode=token_mode,
        timeout=timeout,
        verify=verify,
        algorithm=algorithm,
        init_timeout=init_timeout,
        coerce=coerce,
        cancel=cancel,
        progress=progress,
        delegate=delegate,
        streams=streams,
        target_config=target_config,
        cleanup=cleanup,
    )


# ---------------------------------------------------------------------------
# The one-line verbs of :mod:`xrdclient.easy`
# ---------------------------------------------------------------------------
#
# Each is its synchronous namesake on a worker thread. They are spelled out
# rather than generated so that each keeps its own line in the documentation,
# which is built by reading this source; mypy checks every forwarded argument
# against the synchronous signature, through :func:`_run`'s ``ParamSpec``.


async def ls(url: Location, *, config: Config | None = None) -> list[XRootDPath]:
    """:func:`xrdclient.ls`, awaitable."""
    return await _run(_easy.ls, url, config=config)


async def glob(pattern: Location, *, config: Config | None = None) -> list[XRootDPath]:
    """:func:`xrdclient.glob`, awaitable. The whole match, not an iterator."""
    return await _run(_easy.glob, pattern, config=config)


async def stat(url: Location, *, config: Config | None = None) -> StatInfo:
    """:func:`xrdclient.stat`, awaitable."""
    return await _run(_easy.stat, url, config=config)


async def exists(url: Location, *, config: Config | None = None) -> bool:
    """:func:`xrdclient.exists`, awaitable."""
    return await _run(_easy.exists, url, config=config)


async def size(url: Location, *, config: Config | None = None) -> int:
    """:func:`xrdclient.size`, awaitable."""
    return await _run(_easy.size, url, config=config)


async def checksum(
    url: Location, algorithm: str | None = None, *, config: Config | None = None
) -> ChecksumInfo:
    """:func:`xrdclient.checksum`, awaitable."""
    return await _run(_easy.checksum, url, algorithm, config=config)


async def read_bytes(url: Location, *, config: Config | None = None) -> bytes:
    """:func:`xrdclient.read_bytes`, awaitable."""
    return await _run(_easy.read_bytes, url, config=config)


async def read_text(url: Location, encoding: str = "utf-8", *, config: Config | None = None) -> str:
    """:func:`xrdclient.read_text`, awaitable."""
    return await _run(_easy.read_text, url, encoding, config=config)


async def write_bytes(url: Location, data: bytes, *, config: Config | None = None) -> int:
    """:func:`xrdclient.write_bytes`, awaitable."""
    return await _run(_easy.write_bytes, url, data, config=config)


async def write_text(
    url: Location, text: str, encoding: str = "utf-8", *, config: Config | None = None
) -> int:
    """:func:`xrdclient.write_text`, awaitable."""
    return await _run(_easy.write_text, url, text, encoding, config=config)


async def mkdir(
    url: Location,
    mode: int | str = 0o755,
    *,
    parents: bool = True,
    exist_ok: bool = True,
    config: Config | None = None,
) -> None:
    """:func:`xrdclient.mkdir`, awaitable."""
    await _run(_easy.mkdir, url, mode, parents=parents, exist_ok=exist_ok, config=config)


async def remove(
    url: Location,
    *,
    recursive: bool = False,
    missing_ok: bool = False,
    config: Config | None = None,
) -> None:
    """:func:`xrdclient.remove`, awaitable."""
    await _run(_easy.remove, url, recursive=recursive, missing_ok=missing_ok, config=config)


async def move(source: Location, destination: Location, *, config: Config | None = None) -> None:
    """:func:`xrdclient.move`, awaitable."""
    await _run(_easy.move, source, destination, config=config)


async def stage(
    urls: Location | Sequence[Location], *, priority: int = 0, config: Config | None = None
) -> str:
    """:func:`xrdclient.stage`, awaitable."""
    return await _run(_easy.stage, urls, priority=priority, config=config)


async def is_online(url: Location, *, config: Config | None = None) -> bool:
    """:func:`xrdclient.is_online`, awaitable."""
    return await _run(_easy.is_online, url, config=config)
