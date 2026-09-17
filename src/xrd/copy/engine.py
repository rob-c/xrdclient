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
from ..errors import ChecksumMismatchError, UnsupportedError, kXR_Unsupported
from ..session.bulk import BulkUnsupported
from ..types import ChecksumInfo
from ..url import XRootDURL, parse

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


def _writer(url: XRootDURL, config: Config, stack: ExitStack, *, overwrite: bool) -> IO[bytes]:
    """A binary writer for ``url``. ``overwrite=False`` means exclusive create."""
    mode = "wb" if overwrite else "xb"
    if url.is_local:
        return stack.enter_context(open(url.path, mode))
    if url.is_root:
        from ..io import open_url

        raw = stack.enter_context(open_url(url, mode, buffering=0, config=config))
        return cast("IO[bytes]", raw)
    if url.is_http:
        from ..http import open_http

        return stack.enter_context(open_http(url, mode, config=config))
    if url.is_s3:
        from ..s3 import open_s3

        return stack.enter_context(open_s3(url, mode, config=config))
    raise UnsupportedError(kXR_Unsupported, f"cannot write to {url.scheme}://")


def _resumer(url: XRootDURL, config: Config, stack: ExitStack, offset: int) -> IO[bytes]:
    """A writer for ``url`` positioned at ``offset``, keeping what is there.

    An HTTP target has no such thing: a ``PUT`` replaces the whole resource,
    so a partial upload can only be repeated, never continued.
    """
    if url.is_local:
        handle = stack.enter_context(open(url.path, "r+b"))
        handle.seek(offset)
        return handle
    if url.is_root:
        from ..io import open_url

        raw = stack.enter_context(open_url(url, "r+b", buffering=0, config=config))
        raw.seek(offset)
        return cast("IO[bytes]", raw)
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
    plan: _Spread, config: Config, chunk: int, progress: Progress | None, *, overwrite: bool
) -> int:
    """Move every byte, one span per worker, and report the total as it goes."""
    with ExitStack() as stack:
        _writer(plan.target, config, stack, overwrite=overwrite)  # create it, then fill it in
    reported = 0
    lock = threading.Lock()

    def move(span: tuple[int, int]) -> int:
        nonlocal reported
        offset, length = span
        moved = 0
        with ExitStack() as stack:
            reader, _ = _reader(plan.source, config, stack)
            reader.seek(offset)
            writer = _resumer(plan.target, config, stack, offset)
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
        return sum(pool.map(move, _spans(plan.size, plan.workers)))


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
    """Download to a local path with every connection writing its own span."""
    from ..client import bulk

    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    fd = os.open(target.path, flags, 0o644)
    try:
        return bulk.download(source, fd, config=config, progress=progress).size
    except BulkUnsupported:
        # Nothing was transferred, and the caller is about to try again the
        # ordinary way: leave the destination as it was found, so an
        # exclusive create is still exclusive on the second attempt.
        if not overwrite:
            with suppress(OSError):
                os.remove(target.path)
        raise
    finally:
        os.close(fd)


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
) -> CopyResult:
    """Copy ``source`` to ``target`` and report what happened.

    Both sides may be a URL, a local path, an :class:`~xrd.XRootDPath`, or an
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
    unverifiable copy an error.

    ``overwrite=False`` creates the target exclusively, raising
    :class:`FileExistsError` if it is already there.

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
    """
    cfg = config or Config()
    chunk = chunk_size or cfg.chunk_size
    algo = algorithm or cfg.preferred_checksum
    src_url, dst_url = _target_url(source), _target_url(target)

    if dry_run:
        known = _probe(src_url, cfg) if src_url is not None else None
        return CopyResult(
            source=str(src_url) if src_url else repr(source),
            target=str(dst_url) if dst_url else repr(target),
            size=known[0] if known else 0,
            seconds=0.0,
        )

    resuming = _continue_from(src_url, dst_url, cfg, overwrite=overwrite) if resume else None
    # The bulk data plane takes every download it can: pipelined, landed
    # straight in the destination, and several connections wide when the
    # target is a file that can be written at an offset.
    bulking = resuming is None and _bulk_usable(src_url, dst_url, cfg)
    spread = None if resuming is not None or bulking else _spread(src_url, dst_url, cfg, chunk)
    # Only a transfer that reads the file from the beginning, in order, can
    # digest it on the way past; the other two ask both ends afterwards.
    ends: _Ends | None = resuming if resuming is not None else spread
    if bulking and dst_url is not None and src_url is not None:
        # Workers fill the file in whatever order the network answers, so
        # there is no stream to digest; the two ends are compared instead,
        # exactly as a spread transfer's are.
        ends = _Ends(src_url, dst_url)
    wanted = cfg.verify_checksums if verify is None else verify
    # And a digest is only worth taking at all if some server can be asked to
    # compare it with what it holds.
    checkable = next((u for u in (dst_url, src_url) if u is not None and not u.is_local), None)
    digest = new_checksum(algo) if wanted and checkable is not None and ends is None else None
    # Reading a remote file, the answer to compare against exists before the
    # transfer does, so ask for it now: a server that cannot checksum is worth
    # finding out about before digesting a gigabyte that nothing will read.
    expected: ChecksumInfo | None = None
    if (
        digest is not None
        and src_url is not None
        and checkable is src_url
        # Only the two schemes that can answer a checksum, and only ones the
        # reader will accept: asking anything else would reach for a server
        # before the scheme itself has been rejected.
        and (src_url.is_root or src_url.is_http)
    ):
        expected = _ask_first(src_url, cfg, algo, strict=verify is True)
        if expected is None:
            digest = None

    started = time.monotonic()
    moved: int | None = None
    if bulking and src_url is not None:
        try:
            moved = (
                _bulk_to_file(src_url, dst_url, cfg, progress, overwrite=overwrite)
                if dst_url is not None
                else _bulk_to_stream(src_url, cast("IO[bytes]", target), cfg, progress, digest)
            )
        except BulkUnsupported as exc:
            # A server that answers a read with a conversation rather than
            # bytes; the general pump knows how to hold that conversation.
            _log.debug("%s declined the bulk path (%s); using the pump", src_url, exc)
            moved = None
    if moved is not None:
        size = moved
    elif spread is not None:
        size = _in_parallel(spread, cfg, chunk, progress, overwrite=overwrite)
    else:
        with ExitStack() as stack:
            reader, total = (source, None) if src_url is None else _reader(src_url, cfg, stack)
            if resuming is not None:
                reader.seek(resuming.offset)
                writer = _resumer(resuming.target, cfg, stack, resuming.offset)
            elif dst_url is None:
                writer = target
            else:
                writer = _writer(dst_url, cfg, stack, overwrite=overwrite)
            report = progress if resuming is None else _shifted(progress, resuming.offset)
            size = _pump(reader, writer, total, chunk, report, digest, cfg.in_flight)
    elapsed = time.monotonic() - started

    checksum = None
    if ends is not None:
        if wanted:
            checksum = _compare_ends(ends.source, ends.target, cfg, algo, strict=verify is True)
    elif digest is not None and checkable is not None:
        checksum = (
            _matched(expected, algo, digest.hexdigest())
            if expected is not None
            else _compare(checkable, cfg, algo, digest.hexdigest(), strict=verify is True)
        )

    if remove_source and src_url is not None:
        _remove(src_url, cfg)  # only now: a failed verification kept the original

    return CopyResult(
        source=str(src_url) if src_url else repr(source),
        target=str(dst_url) if dst_url else repr(target),
        size=size,
        seconds=elapsed,
        checksum=checksum,
        resumed_at=resuming.offset if resuming is not None else 0,
    )


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
    exclusion always wins. ``sync`` skips files already at the target, and
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
    total = _Total(progress) if progress is not None and count > 1 else None

    def move(rel: str) -> CopyResult | None:
        destination = dst_url / rel
        if sync is not None and _up_to_date(src_url / rel, destination, cfg, sync, algo):
            return None
        if destination.is_local and not options.get("dry_run"):
            os.makedirs(os.path.dirname(destination.path), exist_ok=True)
        report = progress if total is None else total.worker()
        return copy(src_url / rel, destination, config=cfg, progress=report, **options)

    if count > 1:
        results = _in_order(wanted, count, move)
    else:
        results = [done for rel in wanted if (done := move(rel)) is not None]
    if delete:
        _prune(dst_url, cfg, set(wanted), dry_run=bool(options.get("dry_run")))
    return results
