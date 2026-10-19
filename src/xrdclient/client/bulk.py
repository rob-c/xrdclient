"""Parallel, restartable bulk transfer over the fast data plane.

:mod:`xrdclient.session.bulk` moves bytes over one connection as fast as the
interpreter can; this module is what makes that a transfer you would trust a
job to. It adds the three things the reader deliberately leaves out:

**Several connections.** One socket is one round-trip at a time however deep
the pipeline, and one Python thread is one ``recv_into`` at a time. Each worker
gets its own connection, its own file handle and its own span of the file, and
because ``recv_into`` and ``pwrite`` both drop the GIL the workers genuinely
overlap.

**Restart.** A worker that loses its server re-opens the file and carries on
from its own high-water mark, so a transfer survives a data server going away
without re-reading what already landed. Nothing is written twice: each worker
owns a disjoint span and writes it at an absolute offset.

**Strict accounting.** The transfer ends with every byte of the expected
length written, or it raises. A short read that the protocol calls end of file
is still a short file here, because the size was established before the
transfer started.

The entry points are :func:`download`, which fans out across a file and writes
at absolute offsets, and :func:`stream`, which keeps the bytes in order for a
pipe or a socket that cannot seek.
"""

from __future__ import annotations

import errno
import functools
import os
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext, suppress
from dataclasses import dataclass

from xrdcore import cancel as _cancel
from xrdcore import fanout
from xrdcore import retry as _retry

from ..config import Config
from ..errors import AbortedError, TransientError, XRootDError
from ..errors import ConnectionError as XrdConnectionError
from ..errors import TimeoutError as XrdTimeoutError
from ..flags import OpenFlags
from ..log import get_logger
from ..session.bulk import BulkUnsupported
from ..url import XRootDURL, parse
from .file import File

__all__ = ["BulkResult", "BulkUnsupported", "download", "stream", "upload"]

_log = get_logger(__name__)

#: Longest a worker waits between attempts to get its span back. It caps the
#: exponential backoff, and so also caps how long a transfer can sit idle
#: after the server is already up again: at five seconds a restart costs the
#: outage plus at most five seconds, rather than the outage plus the next
#: doubling.
_MAX_BACKOFF = 5.0

#: What a worker treats as "the server went away, try again".
_TRANSIENT = (XrdConnectionError, XrdTimeoutError, TransientError)

Progress = Callable[[int, "int | None"], None]


@dataclass(frozen=True, slots=True)
class _Options:
    """The keyword options of one :func:`download` or :func:`stream` call.

    Carried as one value from the public entry point to :func:`_start` and
    :class:`_Transfer`, so the two entry points cannot drift apart in how
    they size and plan a fan-out. Every field is exactly the public
    argument of the same name, unresolved: ``None`` still means "the
    configured default", which :func:`_plan` and :class:`_Transfer` apply.
    """

    progress: Progress | None = None
    workers: int | None = None
    chunk: int | None = None
    depth: int | None = None
    #: The file's length, when the caller already knows it; saves the open.
    size: int | None = None
    cancel: _cancel.Cancel | None = None


@dataclass(frozen=True, slots=True)
class BulkResult:
    """What one bulk transfer moved."""

    size: int
    seconds: float
    workers: int
    #: How many times a worker lost its connection and resumed. Zero on a
    #: healthy transfer; worth logging when it is not.
    restarts: int = 0

    @property
    def rate(self) -> float:
        """Bytes a second, or 0.0 for a transfer too short to time."""
        return self.size / self.seconds if self.seconds > 0 else 0.0

    def __str__(self) -> str:
        """One summary line for a log: size, time, MiB/s, connections and any restarts."""
        speed = self.rate / (1 << 20)
        tail = f", {self.restarts} restart(s)" if self.restarts else ""
        return (
            f"{self.size} bytes in {self.seconds:.2f}s ({speed:.0f} MiB/s, "
            f"{self.workers} connection{'s' if self.workers != 1 else ''}{tail})"
        )


def _plan(size: int, config: Config, workers: int | None, chunk: int | None) -> tuple[int, int]:
    """How many connections and how big a request, for a file of ``size``.

    A worker that would not get a whole chunk is not worth its connection, so
    a small file ends up on one link however high the setting is.
    """
    chunk = chunk or config.bulk_chunk
    want = config.bulk_workers if workers is None else workers
    want = max(1, want)
    return max(1, min(want, -(-size // chunk) if size else 1)), chunk


def _spans(size: int, workers: int) -> list[tuple[int, int]]:
    """``size`` split into ``workers`` contiguous ``(start, length)`` pieces."""
    if size <= 0:
        # An empty file is one empty piece: there is nothing to divide, and a
        # step of nothing would never get through it.
        return [(0, 0)]
    step = -(-size // workers) if workers else size
    return [(start, min(step, size - start)) for start in range(0, size, step)]


@contextmanager
def _opened(url: XRootDURL, config: Config) -> Iterator[File]:
    """One file, open for reading, on its own connection."""
    handle = File(url, config)
    handle.open("r")
    try:
        yield handle
    finally:
        # A dead connection has nothing left to close.
        with suppress(OSError):
            handle.close()


class _Transfer:
    """The shared state of one fan-out: progress, restarts, and first error."""

    __slots__ = (
        "_files",
        "_lock",
        "_progress",
        "aborted",
        "cancel",
        "chunk",
        "config",
        "depth",
        "halt",
        "moved",
        "restarts",
        "total",
        "url",
    )

    def __init__(
        self,
        url: XRootDURL,
        config: Config,
        options: _Options,
        *,
        total: int,
        chunk: int,
    ) -> None:
        """State for moving ``total`` bytes of ``url`` in ``chunk``-byte reads.

        ``chunk`` is the planned request size (see :func:`_plan`), not the
        caller's raw option, which may have been ``None``; the pipeline depth
        and the cancel hook are resolved here from ``options``.
        """
        self.url = url
        self.config = config
        self.chunk = chunk
        self.depth = options.depth or config.bulk_depth
        self.total = total
        self.moved = 0
        self.restarts = 0
        #: Set when any worker of this transfer has failed: the others stop
        #: at their next piece rather than finish spans nobody will use.
        self.halt = threading.Event()
        self._lock = threading.Lock()
        self._progress = options.progress
        #: The caller's cancel hook, asked between pieces (see :mod:`xrdcore.cancel`).
        self.cancel = _cancel.as_check(options.cancel)
        #: Set once the transfer has been cancelled: every worker stops, and
        #: none of them treats the connection it lost as a reason to resume.
        self.aborted = False
        #: The handles open right now, for a cancel to wake.
        self._files: set[File] = set()

    def track(self, handle: File) -> File:
        """Note ``handle`` as open, so that a cancel reaches it; aborted at once if too late."""
        with self._lock:
            self._files.add(handle)
            late = self.aborted
        if late:
            handle.abort()
        return handle

    def untrack(self, handle: File) -> None:
        """Forget ``handle`` once its worker has closed it; a later cancel leaves it alone."""
        with self._lock:
            self._files.discard(handle)

    def stop_now(self) -> None:
        """Cancel: stop every worker and wake any blocked on the network.

        Called from the watcher thread, or from a worker that saw the hook
        itself. Aborting a handle shuts its connection down, which wakes a
        worker blocked in a receive at once rather than at its timeout.
        """
        with self._lock:
            self.aborted = True
            handles = list(self._files)
        self.halt.set()
        for handle in handles:
            handle.abort()

    @contextmanager
    def opened(self) -> Iterator[File]:
        """:func:`_opened`, tracked for the length of the block."""
        with _opened(self.url, self.config) as handle:
            self.track(handle)
            try:
                yield handle
            finally:
                self.untrack(handle)

    def advance(self, count: int) -> None:
        """Count ``count`` more bytes landed and report progress.

        Called from every worker thread; the lock makes the running total and
        the callback order agree, so a progress hook sees totals that only grow.
        """
        with self._lock:
            self.moved += count
            if self._progress is not None:
                self._progress(self.moved, self.total)

    def restarted(self) -> int:
        """Count one resume after a lost connection; returns the new total for the log line."""
        with self._lock:
            self.restarts += 1
            return self.restarts

    def _unless_halted(
        self, pieces: Iterator[tuple[int, memoryview]], start: int
    ) -> Iterator[tuple[int, memoryview]]:
        """``pieces``, until another worker of this transfer has failed.

        Checked between pieces, which is as often as a worker can look up:
        the transfer is lost, and finishing this span would only spend the
        bandwidth and the time before the caller hears about it.
        """
        for piece in pieces:
            if self.cancel is not None and not self.aborted and self.cancel():
                self.stop_now()
            if self.aborted:
                raise AbortedError(f"the transfer of {self.url} was cancelled")
            if self.halt.is_set():
                raise fanout.Stopped(f"span at {start} stopped: another worker failed")
            yield piece

    def span(
        self,
        start: int,
        length: int,
        consume: Callable[[int, memoryview], None],
        opened: File | None = None,
    ) -> int:
        """Read ``length`` bytes from ``start``, restarting as often as allowed.

        ``consume(offset, view)`` is called with each piece as it lands; the
        view is only valid until the next one. Returns how many bytes were
        delivered, which is short of ``length`` only at end of file.

        ``opened`` is a handle the caller has already opened - the one the
        length was read from - so the common single-connection transfer opens
        the file once rather than twice. It is used for the first attempt only;
        a retry needs a connection of its own anyway, and the caller still owns
        closing it.
        """
        done = 0
        recovery = _Recovery(self.config, self.chunk)
        while True:
            attempt_start = done
            try:
                borrowed, opened = opened, None
                with (
                    nullcontext(borrowed) if borrowed is not None else self.opened() as handle,
                    handle.session.bulk(
                        handle.handle, chunk=recovery.chunk, depth=self.depth
                    ) as reader,
                ):
                    for offset, view in self._unless_halted(
                        reader.stream(start + done, length - done), start
                    ):
                        consume(offset, view)
                        done += len(view)
                        self.advance(len(view))
                        recovery.progressed()
                # The reader ran to the end of what the server would give: the
                # span is either complete or the file stopped short of it.
                return done
            except _TRANSIENT as exc:
                if self.aborted:
                    # The cancel shut the connection down; that is not a
                    # server to wait for.
                    raise AbortedError(f"the transfer of {self.url} was cancelled") from exc
                waiting = recovery.pause(made_progress=done > attempt_start)
                if waiting is None:
                    raise
                pause, left = waiting
                self.restarted()
                _log.warning(
                    "bulk worker on %s lost the server %d bytes into its span at "
                    "offset %d (%s); resuming with %d-byte reads in %.1fs, "
                    "%.0fs of recovery left",
                    self.url.host,
                    done,
                    start,
                    exc,
                    recovery.chunk,
                    pause,
                    left,
                )
                time.sleep(pause)


class _Recovery:
    """How long a transfer keeps trying, and how long it waits between tries.

    The budget is a span of time rather than a count of attempts: what decides
    whether a job survives a data server going away is whether the server
    comes back before the client gives up, and a restart is commonly tens of
    seconds. Progress refills it, so a long transfer is never killed by the
    sum of outages it already survived.
    """

    __slots__ = ("_backoff", "_budget", "_expires", "_floor", "_tries", "chunk")

    def __init__(self, config: Config, chunk: int) -> None:
        """Start the clock: ``config`` supplies the budget, ``chunk`` the first request size.

        ``_floor`` caps how small :meth:`pause` may shrink the chunk, and never
        exceeds the starting chunk, so a small transfer does not grow on retry.
        """
        self._budget = config.bulk_recovery
        self._backoff = config.retry_backoff
        self._expires = time.monotonic() + self._budget
        self._floor = min(max(config.bulk_recovery_chunk, 1), chunk)
        self._tries = 0
        self.chunk = chunk

    def progressed(self) -> None:
        """Bytes arrived: the allowance starts again."""
        self._tries = 0
        self._expires = time.monotonic() + self._budget

    def pause(self, *, made_progress: bool = False) -> tuple[float, float] | None:
        """How long to wait and how much budget is left, or ``None`` to stop."""
        if not made_progress:
            self.chunk = max(self._floor, self.chunk // 2)
        self._tries += 1
        left = self._expires - time.monotonic()
        if left <= 0:
            return None
        return _retry.delay(self._tries, self._backoff, _MAX_BACKOFF, remaining=left), left


def _seekable(fd: int) -> bool:
    """Whether ``fd`` can be written at an absolute offset."""
    try:
        os.lseek(fd, 0, os.SEEK_CUR)
    except OSError:
        return False
    return True


def _write_all(fd: int, view: memoryview) -> None:
    """``os.write`` all of ``view`` to ``fd``, looping over short writes.

    A pipe or socket may accept fewer bytes than offered; a zero-byte write
    is reported as ``EIO`` rather than looped on forever.
    """
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise OSError(errno.EIO, "local write made no progress")
        written += count


def _preallocate(fd: int, size: int) -> None:
    """Size a destination, preserving capacity and integrity failures."""
    try:
        os.ftruncate(fd, size)
    except OSError as exc:
        # Pipes and devices may reject sizing but still accept writes. A
        # regular file (including FUSE) is different when it reports a real
        # storage failure. Some filesystems merely do not support pre-sizing;
        # retain the old grow-as-we-write fallback for those.
        unsupported = {None, errno.EINVAL, errno.ENOSYS, errno.ESPIPE}
        if hasattr(errno, "ENOTSUP"):
            unsupported.add(errno.ENOTSUP)
        if stat.S_ISREG(os.fstat(fd).st_mode) and exc.errno not in unsupported:
            raise


def _pwrite_all(fd: int, view: memoryview, offset: int) -> None:
    """Land all of ``view`` or fail if the filesystem stops progressing."""
    written = 0
    while written < len(view):
        count = os.pwrite(fd, view[written:], offset + written)
        if count <= 0:
            raise OSError(errno.EIO, "local pwrite made no progress")
        written += count


def _begin(
    url: XRootDURL, config: Config, size: int | None, stack: ExitStack
) -> tuple[int, File | None]:
    """The file's length, and the open handle it was learned from.

    Opening is what a transfer has to do anyway, and the open reply carries
    the length, so this is one round trip rather than a stat and then an open.
    The handle is passed to the first worker so that nothing opens the file
    twice - which on a token-authorised endpoint would mean authorising twice.

    First contact, so a failure here is final: the recovery budget
    deliberately does not cover it, because until one request has been
    answered there is nothing to tell a server that is restarting from a host
    that does not exist.
    """
    if size is not None:
        return size, None
    handle = stack.enter_context(_opened(url, config))
    return int(handle.stat().size), handle


def _start(
    target: XRootDURL, config: Config, options: _Options, stack: ExitStack
) -> tuple[_Transfer, int, File | None]:
    """Size and plan a fan-out: its shared state, worker count, and first handle.

    The common opening of :func:`download` and :func:`stream`. The handle the
    length was read from (if one had to be opened) is tracked at once, so a
    cancel that arrives before the workers start still reaches it; ``stack``
    owns closing it.
    """
    total, first = _begin(target, config, options.size, stack)
    count, span_chunk = _plan(total, config, options.workers, options.chunk)
    transfer = _Transfer(target, config, options, total=total, chunk=span_chunk)
    if first is not None:
        transfer.track(first)
    return transfer, count, first


def _jobs(
    transfer: _Transfer,
    total: int,
    count: int,
    consume: Callable[[int, memoryview], None],
    first: File | None,
) -> list[Callable[[], object]]:
    """One job per span, the first of them reusing the already-open handle."""
    spans = [(start, length) for start, length in _spans(total, count) if length]
    return [
        functools.partial(transfer.span, start, length, consume, first if i == 0 else None)
        for i, (start, length) in enumerate(spans)
    ]


def _run(
    jobs: list[Callable[[], object]],
    transfer: _Transfer,
    on_failure: Callable[[], None] | None = None,
) -> None:
    """Run every job, one thread each, and re-raise the first failure.

    ``on_failure`` is called as soon as any job raises, before the others are
    joined. An ordered stream needs it: its workers wait their turn to write,
    and a worker that died is a turn that will never come, so the rest would
    wait on it forever. Failures after the first are listed in a note on it
    (see :func:`xrdcore.fanout.run_all`).

    While they run, the transfer's cancel hook is watched (see
    :mod:`xrdcore.cancel`), and whatever a cancelled transfer ends with
    is reported as the :class:`~xrdclient.errors.AbortedError` it is.
    """
    try:
        with _cancel.watching(transfer.cancel, transfer.stop_now, name="xrd-bulk-cancel"):
            fanout.run_all(jobs, name="xrd-bulk", stop=transfer.halt, on_failure=on_failure)
    except AbortedError:
        raise
    except Exception as exc:
        if transfer.aborted:
            raise AbortedError(f"the transfer of {transfer.url} was cancelled") from exc
        raise


def download(
    url: str | XRootDURL,
    dest: str | os.PathLike[str] | int,
    *,
    config: Config | None = None,
    progress: Progress | None = None,
    workers: int | None = None,
    chunk: int | None = None,
    depth: int | None = None,
    size: int | None = None,
    cancel: _cancel.Cancel | None = None,
) -> BulkResult:
    """Copy a ``root://`` file to a local file, several connections at once.

    ``dest`` is a path or an already-open file descriptor. Each worker writes
    its own span with ``pwrite`` at an absolute offset, so no lock and no
    ordering is needed between them and the destination is filled in whatever
    order the network delivers.

    Raises if the transfer ends short of ``size`` bytes, so a caller never has
    to check the length itself.

    ``cancel`` is a :class:`threading.Event` or a callable answering whether
    to stop. It is asked between pieces by every worker, and watched while
    they wait on the network: once it says stop, the handles in flight are
    aborted (:meth:`~xrdclient.File.abort`), every worker stops within a
    piece, and the call raises :class:`~xrdclient.errors.AbortedError`. What
    already landed in ``dest`` stays there.
    """
    cfg = config or Config()
    target = parse(url)
    options = _Options(progress, workers, chunk, depth, size, cancel)
    with ExitStack() as stack:
        transfer, count, first = _start(target, cfg, options, stack)
        total = transfer.total
        if isinstance(dest, int):
            fd = dest
        else:
            fd = os.open(os.fspath(dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            stack.callback(os.close, fd)
        if not _seekable(fd):
            # A pipe, a terminal, /dev/stdout: there are no offsets to write
            # at, so the bytes have to arrive in order instead.
            return stream(
                target,
                lambda view: _write_all(fd, view),
                config=cfg,
                progress=progress,
                workers=1,
                chunk=chunk,
                depth=depth,
                size=total,
                cancel=cancel,
            )
        _preallocate(fd, total)  # one allocation, not a growing file per worker

        started = time.monotonic()
        _run(
            _jobs(transfer, total, count, lambda o, v: _pwrite_all(fd, v, o), first)
            or [lambda: None],
            transfer,
        )
        elapsed = time.monotonic() - started

    if transfer.moved != total:
        raise XRootDError(
            f"{target}: transfer ended with {transfer.moved} of {total} bytes; "
            "the file is incomplete"
        )
    return BulkResult(transfer.moved, elapsed, count, transfer.restarts)


def stream(
    url: str | XRootDURL,
    writer: Callable[[memoryview], None],
    *,
    config: Config | None = None,
    progress: Progress | None = None,
    workers: int | None = None,
    chunk: int | None = None,
    depth: int | None = None,
    size: int | None = None,
    cancel: _cancel.Cancel | None = None,
) -> BulkResult:
    """Read a ``root://`` file in order, handing each piece to ``writer``.

    For a destination that cannot seek - a pipe, standard output, a socket -
    the bytes have to arrive in the order they are in the file. Workers still
    read in parallel; a cursor lets exactly one of them write at a time, and
    only when its piece is the next one. ``writer`` is therefore called from
    one thread at a time, in file order, with a view valid until it returns.
    ``cancel`` is as for :func:`download`.
    """
    cfg = config or Config()
    target = parse(url)
    options = _Options(progress, workers, chunk, depth, size, cancel)
    with ExitStack() as stack:
        transfer, count, first = _start(target, cfg, options, stack)
        return _ordered_run(transfer, writer, count, first)


def _ordered_run(
    transfer: _Transfer,
    writer: Callable[[memoryview], None],
    count: int,
    first: File | None,
) -> BulkResult:
    """Run the workers with exactly one of them writing at a time, in order.

    ``count`` workers share ``transfer``; ``first`` is the handle the length
    was read from, reused by the first span. The file and its length are
    the transfer's own (``transfer.url``, ``transfer.total``).
    """
    target, total = transfer.url, transfer.total
    cursor = 0
    abandoned = False
    turn = threading.Condition()

    def ordered(offset: int, view: memoryview) -> None:
        """Write ``view`` once every byte before ``offset`` has been written.

        Workers fetch in parallel but block here for their turn, which is what
        lets a non-seekable destination (a pipe) receive the file in order. A
        worker whose predecessor gave up raises instead of waiting forever.
        """
        nonlocal cursor
        with turn:
            turn.wait_for(lambda: cursor == offset or abandoned)
            if abandoned:
                raise XRootDError(
                    f"{target}: another worker stopped, so this one does too "
                    "rather than write its piece out of order"
                )
            writer(view)
            cursor = offset + len(view)
            turn.notify_all()

    def abandon() -> None:
        """Release every worker waiting for its turn; they raise rather than write out of order."""
        nonlocal abandoned
        with turn:
            abandoned = True
            turn.notify_all()

    started = time.monotonic()
    _run(
        _jobs(transfer, total, count, ordered, first) or [lambda: None],
        transfer,
        abandon,
    )
    elapsed = time.monotonic() - started
    if transfer.moved != total:
        raise XRootDError(
            f"{target}: stream ended with {transfer.moved} of {total} bytes; the file is incomplete"
        )
    return BulkResult(transfer.moved, elapsed, count, transfer.restarts)


def upload(
    source: str | os.PathLike[str] | int,
    url: str | XRootDURL,
    *,
    config: Config | None = None,
    progress: Progress | None = None,
    chunk: int | None = None,
    depth: int | None = None,
    overwrite: bool = True,
    posc: bool = False,
    mode: int = 0o644,
    cancel: _cancel.Cancel | None = None,
) -> BulkResult:
    """Copy a local file to ``root://``, pipelined over one connection.

    ``source`` is a path or an open descriptor, read from where it stands to
    its end, ``chunk`` (``config.chunk_size``) bytes a request with ``depth``
    (``config.bulk_depth``) in flight - see :meth:`File.write_pieces`, which
    this drives. The destination is created, or replaced only with
    ``overwrite``, and with ``posc`` it exists under its name only once the
    close has committed it. ``progress(done, total)`` counts acknowledged
    bytes; ``total`` is the source's size when it has one.

    A piece the server answers ``kXR_wait`` is sent again once the stream
    has settled, read back from the source - which needs a source that can
    seek; from a pipe, such an answer fails the upload instead.

    ``cancel`` is as for :func:`download`: asked after every piece, and
    watched while the upload waits on the network. Once it says stop the
    destination's handle is aborted, which wakes the upload at once and
    leaves the server to drop the half-written file (``posc``) or keep what
    it already has, and the call raises :class:`~xrdclient.errors.AbortedError`.
    """
    cfg = config or Config()
    target = parse(url)
    check = _cancel.as_check(cancel)
    with ExitStack() as stack:
        fd = source if isinstance(source, int) else _read_fd(source, stack)
        total = _source_size(fd)
        start = os.lseek(fd, 0, os.SEEK_CUR) if _seekable(fd) else 0
        local = _Source(fd, start, chunk or cfg.chunk_size)
        handle = File(target, cfg)
        flags = OpenFlags.UPDATE | (OpenFlags.DELETE if overwrite else OpenFlags.NEW)
        handle.open(flags | OpenFlags.POSC if posc else flags, mode)
        stack.callback(handle.close)
        moved = _Moved(progress, total)
        started = time.monotonic()
        with _cancel.watching(check, handle.abort, name="xrd-upload-cancel"):
            again = _send(handle, local, depth, moved, check)
            for at, length in again:
                handle.write(local.reread(at, length), at)
                moved(at, length)
        elapsed = time.monotonic() - started
    return BulkResult(moved.done, elapsed, 1, 0)


class _Moved:
    """An upload's running total of acknowledged bytes, reported as it grows."""

    __slots__ = ("_progress", "_total", "done")

    def __init__(self, progress: Progress | None, total: int | None) -> None:
        """Start at zero; ``total`` is ``None`` for a source of unknown length (a pipe)."""
        self.done = 0
        self._progress = progress
        self._total = total

    def __call__(self, at: int, count: int) -> None:
        """Add ``count`` acknowledged bytes (``at`` is unused: this is the write hook signature)."""
        self.done += count
        if self._progress is not None:
            self._progress(self.done, self._total)


@dataclass(frozen=True, slots=True)
class _Source:
    """Where an upload's bytes come from: a local descriptor, read in order.

    ``start`` is where the descriptor stood when the upload began; offsets
    on the destination count from there, so a resend of destination range
    ``(at, length)`` is a ``pread`` at ``start + at`` of the same source.
    """

    fd: int
    start: int
    #: Bytes per ``os.read``, which is also bytes per ``kXR_write``.
    chunk: int

    def pieces(self) -> Iterator[bytes]:
        """The rest of the source, ``chunk`` bytes at a time, until end of file."""
        while piece := os.read(self.fd, self.chunk):
            yield piece

    def reread(self, at: int, length: int) -> bytes:
        """Destination range ``(at, length)`` read again, for a resend."""
        return os.pread(self.fd, length, self.start + at)


def _read_fd(path: str | os.PathLike[str], stack: ExitStack) -> int:
    fd = os.open(os.fspath(path), os.O_RDONLY)
    stack.callback(os.close, fd)
    return fd


def _source_size(fd: int) -> int | None:
    """The size of a regular file behind ``fd``; ``None`` for a pipe or a device."""
    info = os.fstat(fd)
    return info.st_size if stat.S_ISREG(info.st_mode) else None


def _send(
    handle: File,
    source: _Source,
    depth: int | None,
    moved: _Moved,
    check: Callable[[], bool] | None,
) -> list[tuple[int, int]]:
    """Stream ``source`` into ``handle``; the ranges a ``kXR_wait`` sent back, for resending.

    Waited ranges are collected only from a source that can seek: from a
    pipe there is no way to read them again, so :meth:`File.write_pieces`
    is left to fail on the first ``kXR_wait`` instead.
    """
    again: list[tuple[int, int]] = []
    seekable = _seekable(source.fd)

    def stop() -> None:
        """The between-writes cancel check: raise :class:`AbortedError` if the hook says stop."""
        if check is not None and check():
            raise AbortedError(f"the upload to {handle.url} was cancelled")

    try:
        handle.write_pieces(
            source.pieces(),
            0,
            depth=depth,
            acknowledged=moved,
            waited=(lambda at, count: again.append((at, count))) if seekable else None,
            check=stop,
        )
    except XRootDError as exc:
        if not isinstance(exc, AbortedError) and check is not None and check():
            raise AbortedError(f"the upload to {handle.url} was cancelled") from exc
        raise
    return again
