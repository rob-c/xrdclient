"""The copy pump and the endpoint adapters that feed it.

The engine is deliberately thin. Every endpoint - a local path, a remote URL,
or an already-open file object - is reduced to a binary stream first, so the
loop in the middle knows nothing about XRootD, and adding a protocol means
teaching :func:`_reader`/`_writer` one more scheme rather than touching the
transfer logic.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, ExitStack, nullcontext, suppress
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import IO, Any, Literal, cast

from .._compat import SLOTS
from .._log import get_logger
from ..config import Config
from ..crypto import new as new_checksum
from ..errors import ChecksumMismatchError, UnsupportedError, XRootDError, kXR_Unsupported
from ..session.bulk import BulkUnsupported
from ..types import ChecksumInfo
from ..url import XRootDURL, parse
from . import replicas
from .limits import Limits, Pace, _Stop

__all__ = ["copy", "copy_tree", "CopyResult", "SyncMode"]

#: Called with ``(bytes_done, total_or_None)`` after every chunk.
Progress = Callable[[int, "int | None"], None]

#: How ``copy_tree(sync=...)`` decides a file is already at the target:
#: ``"size"`` trusts the length, ``"mtime"`` also wants the copy to be no
#: older than the original, and ``"checksum"`` compares the bytes themselves.
SyncMode = Literal["size", "mtime", "checksum"]

_log = get_logger(__name__)

#: What :func:`copy` accepts on either side.
Endpoint = "str | os.PathLike[str] | XRootDURL | IO[bytes]"


@dataclass(frozen=True, **SLOTS)
class CopyResult:
    """What one completed transfer did."""

    source: str
    target: str
    #: Bytes this call moved - which is not the size of the file when the
    #: transfer resumed part way through it. See :attr:`resumed_at`.
    size: int
    seconds: float
    checksum: ChecksumInfo | None = None
    #: The offset a resumed transfer started from, and 0 for a whole copy, so
    #: ``resumed_at + size`` is the length of the finished file either way.
    resumed_at: int = 0

    @property
    def rate(self) -> float:
        """Bytes per second; ``inf`` if the copy took no measurable time."""
        return self.size / self.seconds if self.seconds > 0 else float("inf")

    @property
    def resumed(self) -> bool:
        return self.resumed_at > 0

    @property
    def verified(self) -> bool:
        return self.checksum is not None

    def __str__(self) -> str:
        # A dry run, or a copy too quick for the clock, has no rate to quote.
        rate = f", {self.rate / 1e6:.1f} MB/s" if self.seconds > 0 else ""
        resumed = f", resumed at {self.resumed_at}" if self.resumed_at else ""
        return f"{self.source} -> {self.target} ({self.size} bytes{resumed}{rate})"


# ---------------------------------------------------------------------------
# Endpoint adapters
# ---------------------------------------------------------------------------


def _is_stream(obj: object) -> bool:
    """True for something already open - a file object, a socket wrapper."""
    return hasattr(obj, "read") or hasattr(obj, "write")


def _target_url(obj: object) -> XRootDURL | None:
    """The URL ``obj`` names, or ``None`` if it is an open stream."""
    return None if _is_stream(obj) else parse(obj)  # type: ignore[arg-type]


def _reader(url: XRootDURL, config: Config, stack: ExitStack) -> tuple[IO[bytes], int | None]:
    """A binary reader for ``url``, plus its size when the endpoint knows it."""
    if url.is_local:
        return stack.enter_context(open(url.path, "rb")), os.path.getsize(url.path)
    if url.is_root:
        from ..io import open_url

        raw = stack.enter_context(open_url(url, "rb", buffering=0, config=config))
        # A RawIOBase reads bytes but is not an ``IO[bytes]`` as far as
        # typeshed is concerned; the pump only ever calls ``read``.
        return cast("IO[bytes]", raw), raw.file.size
    if url.is_http:
        from ..http import open_http

        return stack.enter_context(open_http(url, "rb", config=config)), None
    if url.is_s3:
        from ..s3 import open_s3

        return stack.enter_context(open_s3(url, "rb", config=config)), None
    raise UnsupportedError(kXR_Unsupported, f"cannot read from {url.scheme}://")


def _writer(
    url: XRootDURL, config: Config, stack: ExitStack, *, overwrite: bool, coerce: bool = False
) -> IO[bytes]:
    """A binary writer for ``url``. ``overwrite=False`` means exclusive create.

    ``coerce`` opens a ``root://`` target with ``kXR_force``, which tells the
    server to ignore its file usage rules - another client's open of the same
    file - as XrdCl's ``coerce`` does. Nothing else has such rules to ignore.
    """
    mode = "wb" if overwrite else "xb"
    if url.is_local:
        return stack.enter_context(open(url.path, mode))
    if url.is_root:
        return _root_writer(url, mode, config, stack, coerce=coerce)
    if url.is_http:
        from ..http import open_http

        return stack.enter_context(open_http(url, mode, config=config))
    if url.is_s3:
        from ..s3 import open_s3

        return stack.enter_context(open_s3(url, mode, config=config))
    raise UnsupportedError(kXR_Unsupported, f"cannot write to {url.scheme}://")


def _root_writer(
    url: XRootDURL, mode: str, config: Config, stack: ExitStack, *, coerce: bool
) -> IO[bytes]:
    """``url`` opened for writing in ``mode``, with ``kXR_force`` if ``coerce``."""
    if not coerce:
        from ..io import open_url

        raw = stack.enter_context(open_url(url, mode, buffering=0, config=config))
        return cast("IO[bytes]", raw)
    from ..client.file import File
    from ..flags import Access, OpenFlags, flags_for_mode
    from ..io.raw import XRootDRawIO

    handle = File(url, config)
    access = Access.OWNER_READ | Access.OWNER_WRITE | Access.GROUP_READ
    try:
        handle.open(flags_for_mode(mode) | OpenFlags.FORCE, access)
    except BaseException:
        handle.close()
        raise
    return cast("IO[bytes]", stack.enter_context(XRootDRawIO(handle, mode, opened=True)))


def _resumer(
    url: XRootDURL, config: Config, stack: ExitStack, offset: int, *, coerce: bool = False
) -> IO[bytes]:
    """A writer for ``url`` positioned at ``offset``, keeping what is there.

    An HTTP target has no such thing: a ``PUT`` replaces the whole resource,
    so a partial upload can only be repeated, never continued.
    """
    if url.is_local:
        handle = stack.enter_context(open(url.path, "r+b"))
        handle.seek(offset)
        return handle
    if url.is_root:
        raw = _root_writer(url, "r+b", config, stack, coerce=coerce)
        raw.seek(offset)
        return raw
    raise UnsupportedError(kXR_Unsupported, f"cannot resume a copy into {url.scheme}://")


@dataclass(frozen=True, **SLOTS)
class _Ends:
    """The two files a transfer that cannot digest its stream must compare."""

    source: XRootDURL
    target: XRootDURL


@dataclass(frozen=True, **SLOTS)
class _Resume(_Ends):
    """A transfer that picks up where an interrupted one stopped."""

    offset: int


@dataclass(frozen=True, **SLOTS)
class _Spread(_Ends):
    """A transfer moved as several spans at once, one connection each."""

    workers: int
    size: int


def _continue_from(
    source: XRootDURL | None, target: XRootDURL | None, config: Config, *, overwrite: bool
) -> _Resume | None:
    """Where to continue ``target`` from, or ``None`` if there is nothing to."""
    if not overwrite:
        raise ValueError("resume continues a partial target, which overwrite=False forbids")
    if source is None or target is None:
        raise ValueError("resume needs a URL on both sides, not an already-open stream")
    there = _probe(target, config)
    if there is None or there[0] == 0:
        return None  # nothing there yet, so this is an ordinary copy
    here = _probe(source, config)
    if here is not None and there[0] > here[0]:
        raise ValueError(f"{target} is longer than {source}, so it is not a partial copy of it")
    return _Resume(source, target, there[0])


def _spread(
    source: XRootDURL | None, target: XRootDURL | None, config: Config, chunk: int
) -> _Spread | None:
    """How to split this transfer across connections, or ``None`` for one stream.

    A session serialises its own calls, so several requests are only in flight
    at once if there are several sessions: one span each. That needs a pair
    :func:`_divisible` accepts and a source that will say how long it is, and
    it is not worth the connections unless every worker gets a whole chunk to
    move.
    """
    if source is None or target is None or not _divisible(source, target):
        return None
    known = _probe(source, config)
    if known is None:
        return None
    workers = min(config.parallel_chunks, known[0] // chunk)
    return _Spread(source, target, workers, known[0]) if workers > 1 else None


def _divisible(source: XRootDURL, target: XRootDURL) -> bool:
    """Can this pair be moved as spans at all?

    The target is written at an offset, which rules out an HTTP ``PUT`` and
    any scheme the writer would refuse anyway; the source is read at one, and
    is asked how long it is first, which a scheme nothing here speaks would
    turn into a connection attempt to nowhere.
    """
    return (target.is_local or target.is_root) and (
        source.is_local or source.is_root or source.is_http
    )


def _spans(size: int, workers: int) -> list[tuple[int, int]]:
    """``size`` divided into ``workers`` contiguous ``(offset, length)`` pieces."""
    step = -(-size // workers)  # rounded up, so the last piece is the short one
    return [(offset, min(step, size - offset)) for offset in range(0, size, step)]


def _in_parallel(
    plan: _Spread,
    config: Config,
    chunk: int,
    progress: Progress | None,
    *,
    overwrite: bool,
    coerce: bool = False,
) -> int:
    """Move every byte, one span per worker, and report the total as it goes."""
    with ExitStack() as stack:
        # Create it, then fill it in.
        _writer(plan.target, config, stack, overwrite=overwrite, coerce=coerce)
    reported = 0
    lock = threading.Lock()

    def move(span: tuple[int, int]) -> int:
        nonlocal reported
        offset, length = span
        moved = 0
        with ExitStack() as stack:
            reader, _ = _reader(plan.source, config, stack)
            reader.seek(offset)
            writer = _resumer(plan.target, config, stack, offset, coerce=coerce)
            while moved < length:
                data = reader.read(min(chunk, length - moved))
                if not data:
                    break
                writer.write(data)
                moved += len(data)
                if progress is not None:
                    with lock:
                        reported += len(data)
                        progress(reported, plan.size)
        return moved

    with ThreadPoolExecutor(plan.workers, thread_name_prefix="xrd-copy") as pool:
        moved = sum(pool.map(move, _spans(plan.size, plan.workers)))
    # A span whose reads ran dry stops quietly, so the only sign that the
    # source ended early is the sum coming up short of what it promised.
    _require_whole(plan.target, moved, plan.size)
    return moved


def _require_whole(target: object, moved: int, expected: int | None) -> None:
    """Fail a transfer that ended before the source said it would.

    A read that returns nothing looks the same whether the file is finished
    or the server has stopped sending it, so the length the source reported
    up front is the only thing that tells the two apart. Without it, a
    truncated target would be reported as a copy - and a move would then
    delete the only complete version. ``expected`` is ``None`` for a source
    that never said how long it was, where nothing can be checked. More
    bytes than promised is a file that grew while it was read, and every one
    of them is at the target, so only a shortfall is an error. The target is
    left as it is: a resumed copy can carry on from it.
    """
    if expected is not None and moved < expected:
        raise XRootDError(
            f"{target}: transfer ended with {moved} of {expected} bytes; the file is incomplete"
        )


def _shifted(progress: Progress | None, offset: int) -> Progress | None:
    """``progress`` reporting where in the *file* it is, not where in the tail."""
    if progress is None:
        return None
    report = progress
    return lambda done, total: report(done + offset, total)


def _probe(url: XRootDURL, config: Config) -> tuple[int, int] | None:
    """``(size, mtime)`` for ``url``, or ``None`` when there is nothing there."""
    if url.is_local:
        try:
            info = os.stat(url.path)
        except OSError:
            return None
        return info.st_size, int(info.st_mtime)
    from ..client import FileSystem

    with FileSystem(url.with_path("/"), config) as fs:
        try:
            stat = fs.stat(url.path)
        except OSError:
            return None
    return stat.st_size, stat.st_mtime


def _remove(url: XRootDURL, config: Config) -> None:
    """Delete ``url`` - what turns a copy into a move."""
    if url.is_local:
        os.remove(url.path)
        return
    from ..client import FileSystem

    with FileSystem(url.with_path("/"), config) as fs:
        fs.remove(url.path)


def _server_checksum(url: XRootDURL, config: Config, algorithm: str) -> ChecksumInfo:
    """Ask whichever server owns ``url`` what it thinks the checksum is."""
    if url.is_http:
        from ..http import digest

        return digest(url, algorithm, config=config)
    from ..client import FileSystem

    with FileSystem(url.with_path("/"), config) as fs:
        return fs.checksum(url.path, algorithm)


# ---------------------------------------------------------------------------
# The fast path
# ---------------------------------------------------------------------------


def _bulk_usable(source: XRootDURL | None, target: XRootDURL | None, config: Config) -> bool:
    """Whether this pair can go over the bulk data plane.

    It reads ``root://`` and writes either a local file, which every worker
    fills at its own offset, or an open stream, which one worker feeds in
    order. Anything else - an upload, a remote target, a resume - keeps the
    general pump.
    """
    return bool(
        config.bulk
        and source is not None
        and source.is_root
        and (target is None or target.is_local)
    )


def _bulk_to_file(
    source: XRootDURL,
    target: XRootDURL,
    config: Config,
    progress: Progress | None,
    *,
    overwrite: bool,
) -> int:
    """Download to a local path with every connection writing its own span.

    The target is opened before the source is, so a copy that fails before
    a byte arrives - no such source, say - leaves the destination as it was
    found: a file it created is removed again, and one that was there is not
    truncated, since the download sizes the file itself once the source has
    answered. A failure after that keeps what was written, for a resume.
    """
    from ..client import bulk

    fd, created = _create(target.path, overwrite=overwrite)
    arrived = False

    def reported(done: int, total: int | None) -> None:
        nonlocal arrived
        arrived = True
        if progress is not None:
            progress(done, total)

    try:
        return bulk.download(source, fd, config=config, progress=reported).size
    except BaseException as exc:
        # ``BulkUnsupported`` sends the caller the ordinary way next, which
        # must find an exclusive create still exclusive.
        if created and (not arrived or isinstance(exc, BulkUnsupported)):
            with suppress(OSError):
                os.remove(target.path)
        raise
    finally:
        os.close(fd)


def _create(path: str, *, overwrite: bool) -> tuple[int, bool]:
    """A descriptor for writing ``path``, and whether this call created the file."""
    try:
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644), True
    except FileExistsError:
        if not overwrite:
            raise
    return os.open(path, os.O_WRONLY | os.O_CREAT), False


def _descriptor_of(writer: IO[bytes]) -> int | None:
    """``writer``'s file descriptor, with its buffer flushed, or ``None``.

    ``None`` covers everything that is not a real file: an in-memory buffer, a
    compressing wrapper, anything that only implements ``write``.
    """
    try:
        writer.flush()
        fd = writer.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    return fd if isinstance(fd, int) and fd >= 0 else None


def _bulk_to_stream(
    source: XRootDURL,
    writer: IO[bytes],
    config: Config,
    progress: Progress | None,
    digest: Any | None,
) -> int:
    """Stream to an open file object, in file order, digesting on the way."""
    from ..client import bulk

    write = writer.write
    # A buffered writer copies a multi-megabyte chunk through its own buffer
    # and takes its lock to do it. When the object is backed by a descriptor,
    # the bytes can go straight there instead - after flushing whatever the
    # caller had already put in the buffer, so the file stays in order.
    fd = _descriptor_of(writer)

    def emit(view: memoryview) -> None:
        if digest is not None:
            digest.update(view)
        if fd is None:
            write(view)
            return
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])

    # One connection: the writer is the serialisation point, so a second would
    # only wait its turn. Depth, not width, is what hides the round trip here.
    return bulk.stream(source, emit, config=config, progress=progress, workers=1).size


# ---------------------------------------------------------------------------
# The pump
# ---------------------------------------------------------------------------


def _fill(reader: IO[bytes], buffer: bytearray) -> int:
    """One chunk of the source into ``buffer``; how many bytes arrived."""
    readinto = getattr(reader, "readinto", None)
    if readinto is not None:
        return cast("int", readinto(buffer) or 0)
    data = reader.read(len(buffer))  # a stream that only implements read()
    buffer[: len(data)] = data
    return len(data)


def _chunks(reader: IO[bytes], chunk_size: int) -> Iterator[memoryview]:
    """The source, one chunk at a time, into a buffer the loop reuses.

    Each piece is only valid until the next one is asked for, which is all the
    pump needs and is what lets one buffer serve the whole transfer.
    """
    buffer = bytearray(chunk_size)
    view = memoryview(buffer)
    while count := _fill(reader, buffer):
        yield view[:count]


class _Ahead:
    """A thread that reads the next chunks while the last one is written.

    A read and a write are both round trips, and doing them strictly in turn
    means each waits out the other: over a long link that halves the rate for
    no reason but the shape of the loop. A window ``depth`` chunks deep lets
    them overlap, so a transfer goes at the slower of its two ends rather than
    at their sum. It costs ``depth`` buffers of memory and one thread, which is
    why it is worth turning off (``config.in_flight = 1``) for a copy between
    two local files, where there is no latency to hide.

    The chunks come out in the order they were read - one thread reads, and the
    queue is a queue - so the digest the pump computes is still the file's.
    """

    def __init__(self, reader: IO[bytes], chunk_size: int, depth: int) -> None:
        self._reader = reader
        self._chunk_size = chunk_size
        self._ready: queue.Queue[memoryview | BaseException | None] = queue.Queue(depth)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._read, name="xrd-readahead", daemon=True
        )

    def __enter__(self) -> Iterator[memoryview]:
        self._thread.start()
        return self._drain()

    def __exit__(self, *exc: object) -> None:
        """Stop reading. The thread may be blocked on a full queue, so empty it."""
        self._stop.set()
        while self._thread.is_alive():
            with suppress(queue.Empty):
                self._ready.get(timeout=0.05)
        self._thread.join()

    def _read(self) -> None:
        try:
            while True:
                buffer = bytearray(self._chunk_size)  # a fresh one: the last is in flight
                count = _fill(self._reader, buffer)
                # Nothing left to read, or nobody left to read it for.
                if not count or not self._offer(memoryview(buffer)[:count]):
                    break
        except BaseException as exc:
            self._offer(exc)
        finally:
            self._offer(None)

    def _offer(self, item: memoryview | BaseException | None) -> bool:
        """Hand one item over, unless the consumer has stopped wanting them."""
        while not self._stop.is_set():
            with suppress(queue.Full):
                self._ready.put(item, timeout=0.05)
                return True
        return False

    def _drain(self) -> Iterator[memoryview]:
        while True:
            item = self._ready.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item


def _pump(
    reader: IO[bytes],
    writer: IO[bytes],
    total: int | None,
    chunk_size: int,
    progress: Progress | None,
    digest: Any,
    depth: int = 1,
) -> int:
    """Move every byte, digesting and reporting as it goes."""
    source: AbstractContextManager[Iterator[memoryview]] = (
        _Ahead(reader, chunk_size, depth) if depth > 1 else nullcontext(_chunks(reader, chunk_size))
    )
    done = 0
    with source as pieces:
        for piece in pieces:
            writer.write(piece)
            if digest is not None:
                digest.update(piece)
            done += len(piece)
            if progress is not None:
                progress(done, total)
    return done


class _Dynamic:
    """A source read as XrdCl reads a dynamic one: until a read comes back short.

    Its size is not asked for and not trusted - it is a file still being
    written - so the copy takes whatever there is when the reads run out.
    """

    def __init__(self, reader: IO[bytes]) -> None:
        self._reader = reader
        self._done = False

    def readinto(self, buffer: bytearray) -> int:
        if self._done:
            return 0
        count = _fill(self._reader, buffer)
        self._done = count < len(buffer)
        return count


@dataclass(frozen=True, **SLOTS)
class _Job:
    """One transfer, as :func:`copy` was asked for it."""

    source: Any
    target: Any
    src_url: XRootDURL | None
    dst_url: XRootDURL | None
    config: Config
    chunk: int
    overwrite: bool
    coerce: bool = False
    dynamic: bool = False
    #: A rate or time limit applies, which XrdCl measures chunk by chunk.
    paced: bool = False

    def names(self) -> tuple[str, str]:
        """The two ends as a :class:`CopyResult` names them."""
        return (
            str(self.src_url) if self.src_url else repr(self.source),
            str(self.dst_url) if self.dst_url else repr(self.target),
        )


@dataclass(frozen=True, **SLOTS)
class _Plan:
    """Which way the bytes go, and what that leaves to compare afterwards."""

    resuming: _Resume | None = None
    bulking: bool = False
    spread: _Spread | None = None
    #: More than one: read from that many replicas at once.
    sources: int = 0
    #: The two files to compare, for a transfer nobody read in order.
    ends: _Ends | None = None

    @property
    def offset(self) -> int:
        return self.resuming.offset if self.resuming is not None else 0


def _plan(job: _Job, *, resume: bool, sources: int) -> _Plan:
    """Choose the transfer: several replicas, a resume, the bulk plane, spans, or one stream.

    Several replicas win over everything, as XrdCl's extreme copy does over
    a dynamic source, and cannot continue a partial target (XrdCl answers
    that with ``errNotImplemented``). A dynamic source is read in order and
    to its end, so it keeps to the single stream; so does a paced one, whose
    limits XrdCl applies as each chunk of the one stream arrives.
    """
    several = _several(job, sources, resume=resume)
    if several is not None:
        return several
    src, dst, cfg = job.src_url, job.dst_url, job.config
    resuming = _continue_from(src, dst, cfg, overwrite=job.overwrite) if resume else None
    if resuming is not None:
        return _Plan(resuming=resuming, ends=resuming)
    if job.dynamic or job.paced:
        return _Plan()
    if _bulk_usable(src, dst, cfg):
        # Workers fill the file in whatever order the network answers, so
        # there is no stream to digest; the two ends are compared instead.
        return _Plan(bulking=True, ends=_both_ends(src, dst))
    spread = _spread(src, dst, cfg, job.chunk)
    return _Plan(spread=spread, ends=spread)


def _several(job: _Job, sources: int, *, resume: bool) -> _Plan | None:
    """The plan for reading several replicas, or ``None`` if this copy cannot.

    Replicas are located over ``root://``, and the target has to be one that
    is written at offsets.
    """
    src, dst = job.src_url, job.dst_url
    if sources < 2 or src is None or not src.is_root or dst is None:
        return None
    if not (dst.is_local or dst.is_root):
        return None
    if resume:
        raise NotImplementedError("a copy from several sources cannot continue a partial one")
    return _Plan(sources=sources, ends=_Ends(src, dst))


def _both_ends(source: XRootDURL | None, target: XRootDURL | None) -> _Ends | None:
    """The two files to compare, if both are files rather than streams."""
    return _Ends(source, target) if source is not None and target is not None else None


@dataclass(**SLOTS)
class _Verify:
    """What checking a copy will take, settled before the first byte moves."""

    ends: _Ends | None
    wanted: bool
    strict: bool
    algorithm: str
    checkable: XRootDURL | None
    digest: Any | None = None
    expected: ChecksumInfo | None = None


def _verification(job: _Job, ends: _Ends | None, algorithm: str, verify: bool | None) -> _Verify:
    """How this copy will be checked, asking the source first where that helps.

    Only a transfer that reads the file from the beginning, in order, can
    digest it on the way past; the others ask both ends afterwards. And a
    digest is only worth taking at all if some server can be asked to compare
    it with what it holds.
    """
    src, dst = job.src_url, job.dst_url
    checkable = next((u for u in (dst, src) if u is not None and not u.is_local), None)
    check = _Verify(
        ends=_strict_ends(src, dst, ends, checkable, verify=verify),
        wanted=job.config.verify_checksums if verify is None else verify,
        strict=verify is True,
        algorithm=algorithm,
        checkable=checkable,
    )
    if check.wanted and checkable is not None and check.ends is None:
        check.digest = new_checksum(algorithm)
        if checkable is src:
            _source_first(check, checkable, job.config)
    return check


def _source_first(check: _Verify, source: XRootDURL, config: Config) -> None:
    """Ask the source for its checksum before the transfer, where it can answer.

    Reading a remote file, the answer to compare against exists before the
    transfer does: a server that cannot checksum is worth finding out about
    before digesting a gigabyte that nothing will read. Only the two schemes
    that can answer one, and only ones the reader will accept - anything else
    would reach for a server before the scheme itself has been rejected.
    """
    if not (source.is_root or source.is_http):
        return
    check.expected = _ask_first(source, config, check.algorithm, strict=check.strict)
    if check.expected is None:
        check.digest = None


def _checked(check: _Verify, config: Config) -> ChecksumInfo | None:
    """The copy's checksum once it has been compared, or ``None`` if it was not."""
    if check.ends is not None:
        if not check.wanted:
            return None
        return _compare_ends(
            check.ends.source, check.ends.target, config, check.algorithm, strict=check.strict
        )
    if check.digest is None or check.checkable is None:
        return None
    ours = check.digest.hexdigest()
    if check.expected is not None:
        return _matched(check.expected, check.algorithm, ours)
    return _compare(check.checkable, config, check.algorithm, ours, strict=check.strict)


def _move(job: _Job, plan: _Plan, progress: Progress | None, digest: Any) -> int:
    """Move the bytes the way ``plan`` says; how many this call moved."""
    if plan.bulking:
        moved = _bulk(job, progress, digest)
        if moved is not None:
            return moved
    if plan.sources:
        return _from_replicas(job, plan.sources, progress)
    if plan.spread is not None:
        return _in_parallel(
            plan.spread, job.config, job.chunk, progress, overwrite=job.overwrite,
            coerce=job.coerce,
        )  # fmt: skip
    return _one_stream(job, plan.resuming, progress, digest)


def _bulk(job: _Job, progress: Progress | None, digest: Any) -> int | None:
    """The download over the bulk data plane, or ``None`` if the server declined it."""
    source = cast("XRootDURL", job.src_url)
    try:
        if job.dst_url is not None:
            return _bulk_to_file(source, job.dst_url, job.config, progress, overwrite=job.overwrite)
        return _bulk_to_stream(source, cast("IO[bytes]", job.target), job.config, progress, digest)
    except BulkUnsupported as exc:
        # A server that answers a read with a conversation rather than
        # bytes; the general pump knows how to hold that conversation.
        _log.debug("%s declined the bulk path (%s); using the pump", source, exc)
        return None


def _from_replicas(job: _Job, sources: int, progress: Progress | None) -> int:
    """Read the file from up to ``sources`` of its replicas at once."""
    source, target = cast("XRootDURL", job.src_url), cast("XRootDURL", job.dst_url)
    # Where the replicas are is the source's question, asked before the
    # target is touched: no such file leaves the destination as it was.
    found = replicas.locate(source, job.config)
    with ExitStack() as stack:
        writer = _writer(target, job.config, stack, overwrite=job.overwrite, coerce=job.coerce)
        return replicas.fetch(
            found, writer, job.config, chunk=job.chunk, sources=sources, progress=progress
        )


def _one_stream(
    job: _Job, resuming: _Resume | None, progress: Progress | None, digest: Any
) -> int:
    """Move the file through one reader and one writer, in order.

    Returns the bytes this call moved, which for a resumed transfer is only
    the tail - but the tail is checked against the whole file, since what has
    to be complete at the end is the target, not this call's share of it.
    """
    offset = resuming.offset if resuming is not None else 0
    with ExitStack() as stack:
        reader, total = _source_stream(job, stack)
        if resuming is not None:
            reader.seek(offset)
            writer = _resumer(resuming.target, job.config, stack, offset, coerce=job.coerce)
        elif job.dst_url is None:
            writer = job.target
        else:
            writer = _writer(
                job.dst_url, job.config, stack, overwrite=job.overwrite, coerce=job.coerce
            )
        report = progress if resuming is None else _shifted(progress, offset)
        depth = job.config.in_flight
        if job.dynamic:
            # One read at a time, each when the last has been written, as
            # XrdCl's dynamic source reads: reading ahead would find the end
            # of a growing file before its writer had got there.
            reader, depth = cast("IO[bytes]", _Dynamic(reader)), 1
        moved = _pump(reader, writer, total, job.chunk, report, digest, depth)
    _require_whole(job.dst_url or repr(job.target), offset + moved, total)
    return moved


def _source_stream(job: _Job, stack: ExitStack) -> tuple[IO[bytes], int | None]:
    """The source as a stream, and the size it is trusted to have, if any."""
    if job.src_url is None:
        return job.source, None
    reader, total = _reader(job.src_url, job.config, stack)
    return reader, None if job.dynamic else total


def copy(
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
    """Copy ``source`` to ``target`` and report what happened.

    Both sides may be a URL, a local path, an :class:`~xrdclient.XRootDPath`, or an
    already-open binary file object:

        >>> copy("root://host//store/f.root", "/scratch/f.root")     # download
        >>> copy("/scratch/f.root", "root://host//store/f.root")     # upload
        >>> copy("root://a//f", "root://b//f")                       # via here
        >>> with open("/scratch/f", "wb") as fh:
        ...     copy("root://host//store/f.root", fh)                # into a stream

    ``verify`` compares a digest taken while streaming against the server's
    own checksum - of the target if it is remote, otherwise of the source.
    Left at ``None`` it follows ``config.verify_checksums`` and degrades
    quietly when the server cannot checksum; set it to ``True`` to make an
    unverifiable copy an error. With no server at either end, ``True``
    compares two local files by reading both, and refuses a copy to or from
    an open stream outright, since there is nothing to compare it with.

    A source that said how long it was and then stopped sending early raises
    :class:`~xrdclient.errors.XRootDError` rather than leaving a truncated
    target that looks finished, and ``remove_source`` does not run.
    ``dynamic_source=True`` is for a file still being written: its size is
    not trusted, and it is read in order until a read comes back short.

    ``overwrite=False`` creates the target exclusively, raising
    :class:`FileExistsError` if it is already there. ``coerce`` opens a
    ``root://`` target with ``kXR_force``, so the server ignores its file
    usage rules - another client having the file open.

    ``dry_run`` reports the transfer it would have made without moving a byte;
    ``remove_source`` deletes the source once the copy is on disk and has
    passed whatever verification was asked for, which together make a move.

    ``resume`` continues an interrupted transfer: whatever is already at the
    target is kept and the copy starts at the end of it, which
    :attr:`CopyResult.resumed_at` reports. A target that is not there yet is
    copied whole, so the flag is safe to set unconditionally on a retry. Since
    a continued transfer never reads the bytes it did not move, verification
    switches from digesting the stream to comparing the two files afterwards -
    which costs a read of whichever end is local. An HTTP target cannot be
    resumed at all, because a ``PUT`` replaces the whole resource.

    A file long enough to give every worker a whole chunk is moved by
    ``config.parallel_chunks`` connections at once, each carrying one span of
    it. That too is a transfer nobody read in order, so it verifies the same
    way; ``parallel_chunks=1`` keeps the single stream and its cheaper
    in-flight digest.

    ``sources=N`` reads a ``root://`` file from up to ``N`` of its replicas
    at once, as ``xrdcp --sources`` does: the redirector is asked where they
    all are, and each server is given blocks of the file to send until none
    are left - a failing one hands its block to the others. It needs a
    target that can be written at offsets (a local path or ``root://``), is
    verified by comparing the two ends, and cannot be combined with
    ``resume``; for any other pair the copy reads one source as usual.

    ``max_rate`` caps the transfer at that many bytes a second, ``min_rate``
    fails it with :class:`~xrdclient.copy.RateThresholdError` if it runs
    slower, measured every ``config.in_flight + 1`` chunks, and ``timeout``
    fails it with :class:`~xrdclient.copy.CopyTimeoutError` once it has run
    for longer than that many seconds - checked as each chunk arrives and
    around the transfer, as XrdCl checks ``cpTimeout``, so a single stalled
    read is bounded by ``config.request_timeout`` rather than by this. XrdCl
    measures all three chunk by chunk, so a copy with any of them is read as
    one stream of ``chunk_size`` pieces rather than spread over connections.
    """
    cfg = config or Config()
    pace = Pace(Limits(max_rate, min_rate, timeout, interval=cfg.in_flight))
    job = _Job(
        source,
        target,
        _target_url(source),
        _target_url(target),
        cfg,
        chunk_size or cfg.chunk_size,
        overwrite,
        coerce=coerce,
        dynamic=dynamic_source,
        paced=pace.limited,
    )
    if dry_run:
        return _dry_run(job)
    try:
        plan = _plan(job, resume=resume, sources=sources)
        check = _verification(job, plan.ends, algorithm or cfg.preferred_checksum, verify)
        pace.check()
        report = pace.watch(progress, start=plan.offset) if pace.limited else progress
        started = time.monotonic()
        pace.begin()
        size = _move(job, plan, report, check.digest)
        elapsed = time.monotonic() - started
        pace.check()
    except _Stop as stop:
        raise stop.error from None
    checksum = _checked(check, cfg)
    if remove_source and job.src_url is not None:
        _remove(job.src_url, cfg)  # only now: a failed verification kept the original
    return CopyResult(
        *job.names(), size=size, seconds=elapsed, checksum=checksum, resumed_at=plan.offset
    )


def _dry_run(job: _Job) -> CopyResult:
    """The transfer :func:`copy` would make, with nothing moved."""
    known = _probe(job.src_url, job.config) if job.src_url is not None else None
    return CopyResult(*job.names(), size=known[0] if known else 0, seconds=0.0)


def _strict_ends(
    source: XRootDURL | None,
    target: XRootDURL | None,
    ends: _Ends | None,
    checkable: XRootDURL | None,
    *,
    verify: bool | None,
) -> _Ends | None:
    """How ``verify=True`` checks a copy that has no server to ask.

    Left to the default, such a copy goes unverified quietly. Asked for
    explicitly, it must not: two local files are compared by reading both,
    as a spread transfer between them already is, and a copy with an open
    stream at one end and a local file at the other has nothing that could
    be compared at all, so it is refused before a byte moves rather than
    reported afterwards as a success nobody checked.
    """
    if verify is not True or checkable is not None or ends is not None:
        return ends
    if source is None or target is None:
        raise ValueError(
            "verify=True, but there is nothing to verify against: no server holds "
            "either end and an open stream cannot be read back"
        )
    return _Ends(source, target)


def _ask_first(
    url: XRootDURL, config: Config, algorithm: str, *, strict: bool
) -> ChecksumInfo | None:
    """The source's checksum, before the transfer, or ``None`` if it has none.

    Asking first is what lets an unverifiable copy skip the digest entirely
    rather than compute one over the whole file and then discover there is
    nothing to compare it with. It costs one query on a server that answers
    and one refusal on a server that does not.
    """
    try:
        return _server_checksum(url, config, algorithm)
    except OSError:
        if strict:
            raise
        _log.debug("%s will not checksum with %s; the copy goes unverified", url, algorithm)
        return None


def _matched(theirs: ChecksumInfo, algorithm: str, ours: str) -> ChecksumInfo:
    """Check the digest taken on the way past against the one asked for first."""
    if theirs.value.lower() != ours.lower():
        raise ChecksumMismatchError(algorithm, theirs.value, ours)
    return theirs


def _compare(
    url: XRootDURL, config: Config, algorithm: str, ours: str, *, strict: bool
) -> ChecksumInfo | None:
    """Compare our streaming digest with the server's, or explain why not."""
    try:
        theirs = _server_checksum(url, config, algorithm)
    except OSError:
        if strict:
            raise
        return None  # the server cannot checksum; the copy still happened
    if theirs.value.lower() != ours.lower():
        raise ChecksumMismatchError(algorithm, theirs.value, ours)
    return theirs


def _compare_ends(
    source: XRootDURL, target: XRootDURL, config: Config, algorithm: str, *, strict: bool
) -> ChecksumInfo | None:
    """Verify a resumed copy by comparing the two files rather than the stream.

    A transfer that started part way through never saw the beginning of the
    file, so the digest taken in flight covers a tail and proves nothing. The
    only honest check left is to ask both ends what they hold.
    """
    try:
        ours = _digest_of(source, config, algorithm)
        theirs = _digest_of(target, config, algorithm)
    except OSError:
        if strict:
            raise
        return None  # an end that cannot checksum; the copy still happened
    if ours != theirs:
        raise ChecksumMismatchError(algorithm, theirs, ours)
    return ChecksumInfo(algorithm, theirs)


# ---------------------------------------------------------------------------
# Recursive copies
# ---------------------------------------------------------------------------


def _digest_of(url: XRootDURL, config: Config, algorithm: str) -> str:
    """``algorithm`` over ``url``: the server's answer, or ours if it is local."""
    if not url.is_local:
        return _server_checksum(url, config, algorithm).value.lower()
    digest = new_checksum(algorithm)
    with open(url.path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _up_to_date(
    source: XRootDURL, target: XRootDURL, config: Config, mode: SyncMode, algorithm: str
) -> bool:
    """Is ``target`` already the copy of ``source`` that ``mode`` asks for?

    A different length always means a different file, whatever the mode, so
    that comparison comes first and saves the expensive one behind it.
    """
    theirs, ours = _probe(target, config), _probe(source, config)
    if theirs is None or ours is None or theirs[0] != ours[0]:
        return False
    if mode == "size":
        return True
    if mode == "mtime":
        return theirs[1] >= ours[1]
    return _digest_of(source, config, algorithm) == _digest_of(target, config, algorithm)


def _selected(rel: str, include: Sequence[str], exclude: Sequence[str]) -> bool:
    """``rsync``'s rule: an explicit include list is a whitelist, exclude wins."""
    if include and not any(fnmatch(rel, pattern) for pattern in include):
        return False
    return not any(fnmatch(rel, pattern) for pattern in exclude)


def _prune(target: XRootDURL, config: Config, keep: set[str], *, dry_run: bool) -> list[str]:
    """Remove everything under ``target`` that ``keep`` does not name.

    Each removal is logged, because a deletion nobody asked for by name is
    the one thing in a copy worth being able to read back afterwards.
    """
    removed = []
    for rel in _walk(target, config):
        if rel in keep:
            continue
        removed.append(rel)
        _log.info("%s %s", "would delete" if dry_run else "deleting", target / rel)
        if not dry_run:
            _remove(target / rel, config)
    return removed


def _walk(url: XRootDURL, config: Config) -> Iterator[str]:
    """Every file under ``url``, as paths relative to it."""
    if url.is_local:
        for root, _, names in os.walk(url.path):
            rel = os.path.relpath(root, url.path)
            for name in names:
                yield name if rel == "." else os.path.join(rel, name)
        return
    from ..client import FileSystem

    with FileSystem(url.with_path("/"), config) as fs:
        base = url.path.rstrip("/")
        for root, _, names in fs.walk(url.path):
            rel = root[len(base) :].strip("/")
            for name in names:
                yield f"{rel}/{name}" if rel else name


class _Total:
    """Every worker's progress added up, because their files interleave.

    A per-file ``(done, total)`` pair means nothing once several files are in
    flight at once, so each worker reports its own deltas here and the caller
    hears about the tree instead: bytes moved so far, against a total nobody
    can know without walking ahead and asking every file its length.
    """

    def __init__(self, report: Progress) -> None:
        self._report = report
        self._lock = threading.Lock()
        self._done = 0

    def worker(self) -> Progress:
        """A ``progress`` callback for one file, counted into the whole."""
        seen = 0

        def report(done: int, _total: int | None) -> None:
            nonlocal seen
            with self._lock:
                self._done += done - seen
                seen = done
                self._report(self._done, None)

        return report


def _in_order(
    items: Sequence[str], workers: int, run: Callable[[str], CopyResult | None]
) -> list[CopyResult]:
    """``run`` over ``items``, several at a time, answering in the order asked.

    A file that raises stops the tree, as it does one at a time: the first
    failure is re-raised and whatever has not started is cancelled rather than
    left to copy on behind the exception.
    """
    with ThreadPoolExecutor(workers, thread_name_prefix="xrd-tree") as pool:
        pending = [pool.submit(run, item) for item in items]
        try:
            return [done for future in pending if (done := future.result()) is not None]
        finally:
            for future in pending:
                future.cancel()


def _sync_options(options: dict[str, Any], sync: SyncMode | None) -> dict[str, Any]:
    """The options each file of a tree is copied with.

    A sync only copies what it has already judged out of date, and replacing
    that is the whole point: an exclusive create would turn every changed file
    into a :class:`FileExistsError`, so that a sync could only ever add files
    and never bring one up to date. ``overwrite=False`` still holds for a tree
    copied without ``sync``.
    """
    return options if sync is None else {**options, "overwrite": True}


def copy_tree(
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
    """Copy a directory recursively, returning one result per file copied.

    Local target directories are created as needed; remote ones are, too,
    because a remote write asks the server for ``kXR_mkpath``. Options other
    than the ones named here - ``dry_run`` and ``verify`` among them - are
    passed through to :func:`copy`.

    ``include`` and ``exclude`` are :mod:`fnmatch` patterns matched against
    each path relative to ``source``; an include list is a whitelist and an
    exclusion always wins. ``sync`` skips files already at the target and
    replaces the ones it finds out of date, whatever ``overwrite`` says, and
    ``delete`` removes files under the target that the source does not have -
    excluded ones among them, since after this call the target is meant to
    hold what the selection describes and nothing else.

    ``workers`` files are copied at once, defaulting to ``config.parallel_files``
    and, at ``1``, to one after another. It is worth raising for a tree of small
    files, where each transfer is a round trip and no transfer is long enough
    for :func:`copy` to spread over connections of its own; a tree of large
    files is already busy. Results come back in the order the walk found them
    however many workers there were, and the first failure cancels the rest.
    While more than one file is in flight, ``progress`` is called with the bytes
    moved across the whole tree and a total of ``None``, since interleaved
    per-file positions would not add up to anything.
    """
    cfg = config or Config()
    src_url, dst_url = parse(source), parse(target)
    algo = options.get("algorithm") or cfg.preferred_checksum
    wanted = [rel for rel in _walk(src_url, cfg) if _selected(rel, include, exclude)]
    count = cfg.parallel_files if workers is None else workers
    total = _tree_total(progress, count)
    options = _sync_options(options, sync)

    def move(rel: str) -> CopyResult | None:
        destination = dst_url / rel
        if sync is not None and _up_to_date(src_url / rel, destination, cfg, sync, algo):
            return None
        if destination.is_local and not options.get("dry_run"):
            os.makedirs(os.path.dirname(destination.path), exist_ok=True)
        report = progress if total is None else total.worker()
        return copy(src_url / rel, destination, config=cfg, progress=report, **options)

    results = _each(wanted, count, move)
    if delete:
        _prune(dst_url, cfg, set(wanted), dry_run=bool(options.get("dry_run")))
    return results


def _tree_total(progress: Progress | None, count: int) -> _Total | None:
    """Where a tree's workers add up their progress, when there are several."""
    return _Total(progress) if progress is not None and count > 1 else None


def _each(
    items: Sequence[str], workers: int, run: Callable[[str], CopyResult | None]
) -> list[CopyResult]:
    """``run`` over ``items``, ``workers`` at a time, keeping what it copied."""
    if workers > 1:
        return _in_order(items, workers, run)
    return [done for item in items if (done := run(item)) is not None]
