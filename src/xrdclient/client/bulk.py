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

import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass

from .._compat import SLOTS
from .._log import get_logger
from ..config import Config
from ..errors import ConnectionError as XrdConnectionError
from ..errors import TimeoutError as XrdTimeoutError
from ..errors import TransientError, XRootDError
from ..session.bulk import BulkUnsupported
from ..url import XRootDURL, parse
from .file import File

__all__ = ["download", "stream", "BulkResult", "BulkUnsupported"]

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


@dataclass(frozen=True, **SLOTS)
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
    step = -(-size // workers) if workers else size
    return [(start, min(step, size - start)) for start in range(0, size, step)] or [(0, 0)]


@contextmanager
def _opened(url: XRootDURL, config: Config) -> Iterator[File]:
    """One file, open for reading, on its own connection."""
    handle = File(url, config)
    handle.open("r")
    try:
        yield handle
    finally:
        try:
            handle.close()
        except OSError:  # pragma: no cover - a dead connection has nothing to close
            pass


class _Transfer:
    """The shared state of one fan-out: progress, restarts, and first error."""

    __slots__ = ("config", "url", "chunk", "depth", "moved", "restarts", "_lock", "_progress",
                 "total")

    def __init__(
        self,
        url: XRootDURL,
        config: Config,
        *,
        chunk: int,
        depth: int,
        total: int,
        progress: Progress | None,
    ) -> None:
        self.url = url
        self.config = config
        self.chunk = chunk
        self.depth = depth
        self.total = total
        self.moved = 0
        self.restarts = 0
        self._lock = threading.Lock()
        self._progress = progress

    def advance(self, count: int) -> None:
        with self._lock:
            self.moved += count
            if self._progress is not None:
                self._progress(self.moved, self.total)

    def restarted(self) -> int:
        with self._lock:
            self.restarts += 1
            return self.restarts

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
        recovery = _Recovery(self.config)
        while True:
            try:
                borrowed, opened = opened, None
                with (
                    nullcontext(borrowed) if borrowed is not None
                    else _opened(self.url, self.config)
                ) as handle, handle.session.bulk(
                    handle.handle, chunk=self.chunk, depth=self.depth
                ) as reader:
                    for offset, view in reader.stream(start + done, length - done):
                        consume(offset, view)
                        done += len(view)
                        self.advance(len(view))
                        recovery.progressed()
                # The reader ran to the end of what the server would give: the
                # span is either complete or the file stopped short of it.
                return done
            except _TRANSIENT as exc:
                waiting = recovery.pause()
                if waiting is None:
                    raise
                pause, left = waiting
                self.restarted()
                _log.warning(
                    "bulk worker on %s lost the server %d bytes into its span at "
                    "offset %d (%s); resuming in %.1fs, %.0fs of recovery left",
                    self.url.host,
                    done,
                    start,
                    exc,
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

    __slots__ = ("_backoff", "_budget", "_expires", "_tries")

    def __init__(self, config: Config) -> None:
        self._budget = config.bulk_recovery
        self._backoff = config.retry_backoff
        self._expires = time.monotonic() + self._budget
        self._tries = 0

    def progressed(self) -> None:
        """Bytes arrived: the allowance starts again."""
        self._tries = 0
        self._expires = time.monotonic() + self._budget

    def pause(self) -> tuple[float, float] | None:
        """How long to wait and how much budget is left, or ``None`` to stop."""
        self._tries += 1
        left = self._expires - time.monotonic()
        if left <= 0:
            return None
        return min(self._backoff * (2 ** (self._tries - 1)), _MAX_BACKOFF, left), left


def _seekable(fd: int) -> bool:
    """Whether ``fd`` can be written at an absolute offset."""
    try:
        os.lseek(fd, 0, os.SEEK_CUR)
    except OSError:
        return False
    return True


def _write_all(fd: int, view: memoryview) -> None:
    written = 0
    while written < len(view):
        written += os.write(fd, view[written:])


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


def _jobs(
    transfer: _Transfer,
    total: int,
    count: int,
    consume: Callable[[int, memoryview], None],
    first: File | None,
) -> list[Callable[[], None]]:
    """One job per span, the first of them reusing the already-open handle."""
    spans = [(start, length) for start, length in _spans(total, count) if length]
    return [
        (
            lambda s=start, n=length, h=(first if i == 0 else None): (  # type: ignore[misc]
                transfer.span(s, n, consume, h)
            )
        )
        for i, (start, length) in enumerate(spans)
    ]


def _run(jobs: list[Callable[[], None]], on_failure: Callable[[], None] | None = None) -> None:
    """Run every job, one thread each, and re-raise the first failure.

    ``on_failure`` is called as soon as any job raises, before the others are
    joined. An ordered stream needs it: its workers wait their turn to write,
    and a worker that died is a turn that will never come, so the rest would
    wait on it forever.
    """
    if len(jobs) == 1:
        jobs[0]()
        return
    errors: list[BaseException] = []
    lock = threading.Lock()

    def guarded(job: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                job()
            except BaseException as exc:  # re-raised on the main thread
                with lock:
                    errors.append(exc)
                if on_failure is not None:
                    on_failure()

        return run

    threads = [
        threading.Thread(target=guarded(job), name=f"xrd-bulk-{i}", daemon=True)
        for i, job in enumerate(jobs)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]


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
) -> BulkResult:
    """Copy a ``root://`` file to a local file, several connections at once.

    ``dest`` is a path or an already-open file descriptor. Each worker writes
    its own span with ``pwrite`` at an absolute offset, so no lock and no
    ordering is needed between them and the destination is filled in whatever
    order the network delivers.

    Raises if the transfer ends short of ``size`` bytes, so a caller never has
    to check the length itself.
    """
    cfg = config or Config()
    target = parse(url)
    with ExitStack() as stack:
        total, first = _begin(target, cfg, size, stack)
        count, span_chunk = _plan(total, cfg, workers, chunk)
        transfer = _Transfer(
            target,
            cfg,
            chunk=span_chunk,
            depth=depth or cfg.bulk_depth,
            total=total,
            progress=progress,
        )
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
            )
        try:
            os.ftruncate(fd, total)  # one allocation, not a growing file per worker
        except OSError:
            pass  # a device that cannot be sized; pwrite still lands where it should

        def writer(offset: int, view: memoryview) -> None:
            written = 0
            while written < len(view):
                written += os.pwrite(fd, view[written:], offset + written)

        started = time.monotonic()
        _run(_jobs(transfer, total, count, writer, first) or [lambda: None])
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
) -> BulkResult:
    """Read a ``root://`` file in order, handing each piece to ``writer``.

    For a destination that cannot seek - a pipe, standard output, a socket -
    the bytes have to arrive in the order they are in the file. Workers still
    read in parallel; a cursor lets exactly one of them write at a time, and
    only when its piece is the next one. ``writer`` is therefore called from
    one thread at a time, in file order, with a view valid until it returns.
    """
    cfg = config or Config()
    target = parse(url)
    stack = ExitStack()
    with stack:
        total, first = _begin(target, cfg, size, stack)
        count, span_chunk = _plan(total, cfg, workers, chunk)
        transfer = _Transfer(
            target,
            cfg,
            chunk=span_chunk,
            depth=depth or cfg.bulk_depth,
            total=total,
            progress=progress,
        )
        return _ordered_run(transfer, writer, total, count, first, target)


def _ordered_run(
    transfer: _Transfer,
    writer: Callable[[memoryview], None],
    total: int,
    count: int,
    first: File | None,
    target: XRootDURL,
) -> BulkResult:
    """Run the workers with exactly one of them writing at a time, in order."""
    cursor = 0
    abandoned = False
    turn = threading.Condition()

    def ordered(offset: int, view: memoryview) -> None:
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
        nonlocal abandoned
        with turn:
            abandoned = True
            turn.notify_all()

    started = time.monotonic()
    _run(_jobs(transfer, total, count, ordered, first) or [lambda: None], abandon)
    elapsed = time.monotonic() - started
    if transfer.moved != total:
        raise XRootDError(
            f"{target}: stream ended with {transfer.moved} of {total} bytes; "
            "the file is incomplete"
        )
    return BulkResult(transfer.moved, elapsed, count, transfer.restarts)
