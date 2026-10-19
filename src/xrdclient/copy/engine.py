"""The copy pump and the endpoint adapters that feed it.

The engine is deliberately thin. Every endpoint - a local path, a remote URL,
or an already-open file object - is reduced to a binary stream first, so the
loop in the middle knows nothing about XRootD, and adding a protocol means
teaching :func:`_reader`/`_writer` one more scheme rather than touching the
transfer logic.

This module is the core: the public :func:`copy`, :func:`copy_tree` and
:class:`CopyResult`, and the job -> plan -> move -> verify pipeline between
them. The pieces around it live in private siblings, each re-exported here so
every name that was importable from ``xrdclient.copy.engine`` still is:
``_endpoints`` (the stream adapters and per-endpoint questions), ``_local``
(local-filesystem recovery and durability), ``_fastpath`` (the bulk data
plane), ``_metalink`` (Metalink failover) and ``_tree`` (walking, selecting and
running a tree). Code here calls those names through this module's globals, so
``monkeypatch.setattr(engine, "_probe", ...)`` still changes what a transfer
does. See ``docs/architecture.md`` for the map.
"""

from __future__ import annotations

import functools
import os
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from typing import IO, Any, Literal, cast

from xrdcore import fanout
from xrdcore import retry as _retry
from xrdcore.checksum import new as new_checksum
from xrdcore.pipeline import fill as _fill
from xrdcore.pipeline import pump as _pump

from ..config import Config
from ..errors import (
    ChecksumMismatchError,
    UnsupportedError,
    WaitLimitError,
    XRootDError,
)
from ..errors import ConnectionError as XRDConnectionError
from ..errors import TimeoutError as XRDTimeoutError
from ..log import get_logger
from ..session import deadline as dl
from ..session.bulk import BulkUnsupported
from ..types import ChecksumInfo
from ..url import XRootDURL, parse
from . import replicas
from ._endpoints import _is_stream as _is_stream
from ._endpoints import _make_parent_collection as _make_parent_collection
from ._endpoints import _parents_already_made as _parents_already_made
from ._endpoints import _probe as _probe
from ._endpoints import _reader as _reader
from ._endpoints import _remove as _remove
from ._endpoints import _resumer as _resumer
from ._endpoints import _root_writer as _root_writer
from ._endpoints import _server_checksum as _server_checksum
from ._endpoints import _target_url as _target_url
from ._endpoints import _writer as _writer
from ._endpoints import _zip_member as _zip_member
from ._fastpath import _bulk_to_file as _bulk_to_file
from ._fastpath import _bulk_to_stream as _bulk_to_stream
from ._fastpath import _bulk_usable as _bulk_usable
from ._fastpath import _create as _create
from ._fastpath import _descriptor_of as _descriptor_of
from ._fastpath import _write_descriptor as _write_descriptor
from ._local import _TRANSIENT_LOCAL_READ_ERRORS as _TRANSIENT_LOCAL_READ_ERRORS
from ._local import _file_identity as _file_identity
from ._local import _RecoveringLocalReader as _RecoveringLocalReader
from ._local import _sync_local_target as _sync_local_target
from ._metalink import _copy_metalink as _copy_metalink
from ._metalink import _copy_metalink_replica as _copy_metalink_replica
from ._metalink import _copy_metalink_replicas as _copy_metalink_replicas
from ._metalink import _load_metalink as _load_metalink
from ._metalink import _metalink_checksum as _metalink_checksum
from ._metalink import _metalink_dry_run as _metalink_dry_run
from ._metalink import _metalink_endpoint as _metalink_endpoint
from ._metalink import _metalink_replica_config as _metalink_replica_config
from ._metalink import _metalink_urls as _metalink_urls
from ._metalink import _secure_metalink_url as _secure_metalink_url
from ._tree import _each as _each
from ._tree import _in_order as _in_order
from ._tree import _make_tree_dirs as _make_tree_dirs
from ._tree import _prune as _prune
from ._tree import _selected as _selected
from ._tree import _sync_options as _sync_options
from ._tree import _Total as _Total
from ._tree import _tree_total as _tree_total
from ._tree import _walk as _walk
from .limits import Limits, Pace, _Stop

__all__ = ["CopyResult", "SyncMode", "copy", "copy_tree"]

#: Called with ``(bytes_done, total_or_None)`` after every chunk.
Progress = Callable[[int, "int | None"], None]

#: How ``copy_tree(sync=...)`` decides a file is already at the target:
#: ``"size"`` trusts the length, ``"mtime"`` also wants the copy to be no
#: older than the original, and ``"checksum"`` compares the bytes themselves.
SyncMode = Literal["size", "mtime", "checksum"]

_log = get_logger(__name__)

#: What :func:`copy` accepts on either side.
Endpoint = "str | os.PathLike[str] | XRootDURL | IO[bytes]"


@dataclass(frozen=True, slots=True)
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
    #: The replica selected by a Metalink source. Ordinary copies leave it unset.
    replica: str | None = None

    @property
    def rate(self) -> float:
        """Bytes per second; ``inf`` if the copy took no measurable time."""
        return self.size / self.seconds if self.seconds > 0 else float("inf")

    @property
    def resumed(self) -> bool:
        """Whether this call continued a partial target rather than writing it whole."""
        return self.resumed_at > 0

    @property
    def verified(self) -> bool:
        """Whether end-to-end checksum verification ran and passed.

        A mismatch never produces a result - it raises
        :class:`~xrdclient.errors.ChecksumMismatchError` - so a checksum here
        is always one both ends agreed on.
        """
        return self.checksum is not None

    def __str__(self) -> str:
        """``src -> dst (N bytes, resumed at M, R MB/s)``, as the CLI prints it."""
        # A dry run, or a copy too quick for the clock, has no rate to quote.
        rate = f", {self.rate / 1e6:.1f} MB/s" if self.seconds > 0 else ""
        resumed = f", resumed at {self.resumed_at}" if self.resumed_at else ""
        return f"{self.source} -> {self.target} ({self.size} bytes{resumed}{rate})"


@dataclass(frozen=True, slots=True)
class _CopyOptions:
    """Every control of one :func:`copy` call, as a single value.

    :func:`copy` takes these as keyword arguments (its public signature);
    inside the engine they travel together, from :func:`copy` to
    :func:`_copy_one` and :func:`_transfer`, and through the Metalink
    failover in :mod:`._metalink`, which rewrites a few per replica with
    :func:`dataclasses.replace` instead of re-spelling all of them. Each
    field means exactly what the :func:`copy` argument of the same name
    does, except that ``config`` is already resolved (never ``None``) and
    ``declared`` has no public spelling at all.
    """

    config: Config
    chunk_size: int | None = None
    verify: bool | None = None
    algorithm: str | None = None
    overwrite: bool = True
    progress: Progress | None = None
    dry_run: bool = False
    remove_source: bool = False
    resume: bool = False
    sources: int = 1
    dynamic_source: bool = False
    max_rate: float | None = None
    min_rate: float | None = None
    timeout: float | None = None
    coerce: bool = False
    #: A checksum the catalogue promised (a Metalink ``<hash>``): the copy
    #: must match it, rather than whatever the server reports.
    declared: ChecksumInfo | None = None


@dataclass(frozen=True, slots=True)
class _Ends:
    """The two files a transfer that cannot digest its stream must compare."""

    source: XRootDURL
    target: XRootDURL


@dataclass(frozen=True, slots=True)
class _Resume(_Ends):
    """A transfer that picks up where an interrupted one stopped."""

    offset: int


@dataclass(frozen=True, slots=True)
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
    if (
        source is None
        or target is None
        or _zip_member(source) is not None
        or not _divisible(source, target)
    ):
        return None
    known = _probe(source, config)
    if known is None:
        return None
    workers = min(config.parallel_chunks, known[0] // chunk)
    return _Spread(source, target, workers, known[0]) if workers > 1 else None


def _divisible(source: XRootDURL, target: XRootDURL) -> bool:
    """Can this pair be moved as spans at all?

    The target is a local file: each worker opens it again to write its own
    span, which a remote one does not allow. dCache and XRootD's Ceph
    backend (RAL's Echo) treat a file as written once it is closed, and
    refuse every later open for update - "File already exists" - so a
    ``root://`` target is written by one stream, as ``xrdcp`` writes it (EOS
    would take the spans, but did not go any faster for them). The source is
    read at an offset and asked how long it is first, which a scheme nothing
    here speaks would turn into a connection attempt to nowhere.
    """
    return target.is_local and (source.is_local or source.is_root or source.is_http)


def _spans(size: int, workers: int) -> list[tuple[int, int]]:
    """``size`` divided into ``workers`` contiguous ``(offset, length)`` pieces."""
    step = -(-size // workers)  # rounded up, so the last piece is the short one
    return [(offset, min(step, size - offset)) for offset in range(0, size, step)]


def _in_parallel(job: _Job, plan: _Spread, progress: Progress | None) -> int:
    """Move every byte, one span per worker, and report the total as it goes.

    ``plan`` names the two ends and how many spans; the job supplies the
    config, the chunk size, and how the target may be opened. ``progress``
    sees the sum over all spans, serialised by a lock, since the workers
    report from their own threads.
    """
    config, chunk, coerce = job.config, job.chunk, job.coerce
    with ExitStack() as stack:
        # Create it, then fill it in.
        _writer(plan.target, config, stack, overwrite=job.overwrite, coerce=coerce)
    reported = 0
    lock = threading.Lock()
    # Set by the fan-out the moment one span fails: the copy has failed, and
    # the other spans stop at their next chunk rather than finish moving
    # bytes into a target nobody will use.
    halt = threading.Event()

    def move(span: tuple[int, int]) -> int:
        """One worker: its own reader and writer, positioned at its span.

        Each span opens both ends afresh, so no handle is shared between
        threads; the target was created above, and is opened here for
        writing at an offset without truncation.
        """
        nonlocal reported
        offset, length = span
        moved = 0
        with ExitStack() as stack:
            reader, _ = _reader(plan.source, config, stack)
            reader.seek(offset)
            writer = _resumer(plan.target, config, stack, offset, coerce=coerce)
            while moved < length:
                if halt.is_set():
                    raise fanout.Stopped(f"span at {offset} stopped: another span failed")
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

    spans = _spans(plan.size, plan.workers)
    moved = sum(
        fanout.run_all(
            [functools.partial(move, span) for span in spans],
            workers=plan.workers,
            name="xrd-copy",
            stop=halt,
        )
    )
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


#: Failures that say an end could not be reached: there is nothing to clean up
#: on it, and asking would only wait out the same timeout again.
_UNREACHABLE = (TimeoutError, XRDConnectionError, XRDTimeoutError, UnsupportedError)


@contextmanager
def _cleaned_on_failure(job: _Job, *, resume: bool) -> Iterator[None]:
    """Run a transfer; if it fails, remove an empty target it left behind.

    Not for a resume, whose whole point is the partial target.
    """
    existed = job.dst_url is not None and job.dst_url.is_local and os.path.exists(job.dst_url.path)
    try:
        yield
    except BaseException as exc:
        if not resume and _may_clean(job.dst_url, existed, exc):
            _discard_empty_target(job)
        raise


def _may_clean(target: XRootDURL | None, existed: bool, error: BaseException) -> bool:
    """Whether a failed copy's target is one this copy may tidy up.

    Not an open stream, not an end that could not be reached (asking would
    only wait out the same timeout again), not a scheme this client does not
    speak, and not a local file that was there before the copy began.
    """
    if target is None or isinstance(error, _UNREACHABLE):
        return False
    if target.is_local:
        return not existed
    return target.is_root or target.is_http or target.is_s3


def _discard_empty_target(job: _Job) -> None:
    """Remove the target of a failed copy if it is empty and was never data.

    An empty file at the destination of a failed copy - a local one opened
    before the source answered 404, or the name EOS creates before
    redirecting an upload - looks like a finished copy of an empty file to
    whatever runs next. It is removed when the source was not empty (or not
    there at all). A target with bytes in it is left alone: that is either
    somebody's data or a partial transfer that ``resume=True`` can continue.
    Best effort, so the error the caller sees stays the copy's own.
    """
    target = job.dst_url
    assert target is not None  # checked by _may_clean
    with suppress(Exception):
        there = _probe(target, job.config)
        if there is None or there[0] != 0:
            return
        source = _probe(job.src_url, job.config) if job.src_url is not None else None
        if source is not None and source[0] == 0:
            return  # an empty source: an empty target may be the right answer
        _remove(target, job.config)
        _log.debug("removed the empty %s a failed copy left behind", target)


# ---------------------------------------------------------------------------
# The pump
# ---------------------------------------------------------------------------


class _Dynamic:
    """A source read as XrdCl reads a dynamic one: until a read comes back short.

    Its size is not asked for and not trusted - it is a file still being
    written - so the copy takes whatever there is when the reads run out.
    """

    def __init__(self, reader: IO[bytes]) -> None:
        """Wrap ``reader``; it is not closed here, its opener's stack owns it."""
        self._reader = reader
        self._done = False

    def readinto(self, buffer: bytearray) -> int:
        """Fill ``buffer``; after the first short read, report end of data for good.

        A growing file may have more bytes a moment later, but XrdCl's
        dynamic-source copy stops at the first short read, and so does this.
        """
        if self._done:
            return 0
        count = _fill(self._reader, buffer)
        self._done = count < len(buffer)
        return count


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
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
        """Where the bytes start: the resume point, or 0 for a whole copy."""
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
    if sources < 2 or src is None or not src.is_root or _zip_member(src) is not None or dst is None:
        return None
    if not (dst.is_local or dst.is_root):
        return None
    if resume:
        raise NotImplementedError("a copy from several sources cannot continue a partial one")
    return _Plan(sources=sources, ends=_Ends(src, dst))


def _both_ends(source: XRootDURL | None, target: XRootDURL | None) -> _Ends | None:
    """The two files to compare, if both are files rather than streams."""
    return _Ends(source, target) if source is not None and target is not None else None


@dataclass(slots=True)
class _Verify:
    """What checking a copy will take, settled before the first byte moves."""

    ends: _Ends | None
    wanted: bool
    strict: bool
    algorithm: str
    checkable: XRootDURL | None
    digest: Any | None = None
    expected: ChecksumInfo | None = None


def _verification(
    job: _Job,
    ends: _Ends | None,
    algorithm: str,
    verify: bool | None,
    declared: ChecksumInfo | None = None,
) -> _Verify:
    """How this copy will be checked, asking the source first where that helps.

    Only a transfer that reads the file from the beginning, in order, can
    digest it on the way past; the others ask both ends afterwards. And a
    digest is only worth taking at all if some server can be asked to compare
    it with what it holds.
    """
    src, dst = job.src_url, job.dst_url
    checkable = next(
        (u for u in (dst, src) if u is not None and not u.is_local and _zip_member(u) is None),
        None,
    )
    check = _Verify(
        ends=_strict_ends(src, dst, ends, checkable, verify=verify),
        wanted=job.config.verify_checksums if verify is None else verify,
        strict=verify is True,
        algorithm=algorithm,
        checkable=checkable,
    )
    if declared is not None:
        return _declared_verification(check, declared)
    return _available_verification(check, src, job.config)


def _declared_verification(check: _Verify, declared: ChecksumInfo) -> _Verify:
    """Require the checksum a Metalink declared before any bytes move."""
    check.wanted = check.strict = True
    check.algorithm = declared.algorithm
    check.expected = declared
    if check.ends is None:
        check.digest = new_checksum(declared.algorithm)
    return check


def _available_verification(check: _Verify, source: XRootDURL | None, config: Config) -> _Verify:
    """Arrange ordinary in-flight verification when an endpoint can answer."""
    if check.wanted and check.checkable is not None and check.ends is None:
        check.digest = new_checksum(check.algorithm)
        if check.checkable is source:
            _source_first(check, check.checkable, config)
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
    if check.expected is not None and check.ends is not None:
        actual = _digest_of(check.ends.target, config, check.algorithm)
        return _matched(check.expected, check.algorithm, actual)
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


def _transfer(
    job: _Job, options: _CopyOptions, algorithm: str, pace: Pace
) -> tuple[int, _Plan, _Verify]:
    """Move the file, reconnecting and continuing when the link drops under it.

    The bulk download plane recovers its own workers; this is the same promise
    for the paths that do not - a single stream, and every upload - so a
    transfer survives a connection that flakes in and out rather than failing
    at the first reset. Each retry re-plans: a target that can be written at an
    offset (a local file, or ``root://``) continues from what already landed,
    and one that cannot (an HTTP ``PUT``, which replaces the whole resource) is
    started again from the beginning, which is safe because a half-done upload
    persists nothing. A timeout or a busy-server wait is not a dropped link and
    is left to stand; so is a mid-transfer failure the copy cannot safely redo
    (an exclusive create, whose retry could clobber another writer's file).

    ``algorithm`` is the resolved digest name (``options.algorithm`` or the
    configured preference); ``pace`` is the copy's rate and time limits,
    shared across retries so a timeout counts the whole copy, not one try.
    """
    attempts = 0
    resume, progress = options.resume, options.progress
    resume_now = resume
    while True:
        plan = _plan(job, resume=resume_now, sources=options.sources)
        check = _verification(job, plan.ends, algorithm, options.verify, options.declared)
        pace.check()
        report = pace.watch(progress, start=plan.offset) if pace.limited else progress
        pace.begin()
        try:
            return _move(job, plan, report, check.digest), plan, check
        except _Stop as stop:
            raise stop.error from None
        except XRDConnectionError as exc:
            attempts += 1
            resume_now = _retry_transfer(job, exc, attempts, resume=resume)
            _log.debug("retrying the transfer to %s after %s", job.names()[1], exc)


def _retry_transfer(job: _Job, exc: XRDConnectionError, attempts: int, *, resume: bool) -> bool:
    """Whether to try the move again, and with resume; or re-raise ``exc``.

    Returns ``True`` to continue from what landed, ``False`` to restart from
    the beginning. Raises when the failure is not a dropped link, the budget
    is spent, the deadline has passed, or the copy cannot be safely redone.
    """
    cfg = job.config
    if isinstance(exc, (XRDTimeoutError, WaitLimitError)):
        raise exc  # a stall or a busy server, not a link that dropped
    if attempts > cfg.connect_retries or not _can_retry(job, resume=resume):
        raise exc
    remaining = dl.remaining()
    if remaining is not None and remaining <= 0:
        raise exc
    _retry.pause(attempts, cfg.retry_backoff, cfg.wait_cap, remaining=remaining)
    return _writes_at_offset(job.dst_url)


def _can_retry(job: _Job, *, resume: bool) -> bool:
    """Whether a dropped transfer to this target may be redone at all.

    It needs a target named by URL, not an open stream this process cannot
    rewind. An overwrite may be restarted or continued freely; an exclusive
    create may not, since a restart could not tell its own half-written file
    from somebody else's and would clobber it - unless the caller already
    asked to resume, having taken that on themselves.
    """
    if job.dst_url is None:
        return False
    return job.overwrite or resume


def _writes_at_offset(target: XRootDURL | None) -> bool:
    """Whether ``target`` can be continued from an offset rather than restarted."""
    return target is not None and (target.is_local or target.is_root)


def _move(job: _Job, plan: _Plan, progress: Progress | None, digest: Any) -> int:
    """Move the bytes the way ``plan`` says; how many this call moved."""
    if plan.bulking:
        moved = _bulk(job, progress, digest)
        if moved is not None:
            return moved
    if plan.sources:
        return _from_replicas(job, plan.sources, progress)
    if plan.spread is not None:
        return _in_parallel(job, plan.spread, progress)
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


def _one_stream(job: _Job, resuming: _Resume | None, progress: Progress | None, digest: Any) -> int:
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
        # The read-ahead's buffers are reused once each chunk is written,
        # which every writer this module opens allows: each copies what it is
        # handed before returning. A stream the caller passed in may keep the
        # view instead, so it gets a fresh buffer per chunk.
        theirs = writer is job.target
        moved = _pump(reader, writer, total, job.chunk, report, digest, depth, recycle=not theirs)
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

    A download long enough to give every worker a whole chunk is moved by
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
    from ..metalink import is_metalink

    options = _CopyOptions(
        config=cfg,
        chunk_size=chunk_size,
        verify=verify,
        algorithm=algorithm,
        overwrite=overwrite,
        progress=progress,
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
    if cfg.metalink_processing and is_metalink(source):
        return _copy_metalink(source, target, options)
    return _copy_one(source, target, options)


def _copy_one(source: Any, target: Any, options: _CopyOptions) -> CopyResult:
    """Copy one concrete source; Metalink dispatch has already happened.

    The Metalink failover calls this once per replica (looked up on this
    module at call time, so a test can replace it), with ``options`` already
    carrying the catalogue's checksum as ``declared``.
    """
    cfg = options.config
    pace = Pace(Limits(options.max_rate, options.min_rate, options.timeout, interval=cfg.in_flight))
    job = _Job(
        source,
        target,
        _target_url(source),
        _target_url(target),
        cfg,
        options.chunk_size or cfg.chunk_size,
        options.overwrite,
        coerce=options.coerce,
        dynamic=options.dynamic_source,
        paced=pace.limited,
    )
    if options.dry_run:
        return _dry_run(job)
    _make_parent_collection(job.dst_url, cfg)
    algo = options.algorithm or cfg.preferred_checksum
    started = time.monotonic()
    with _cleaned_on_failure(job, resume=options.resume):
        size, plan, check = _transfer(job, options, algo, pace)
        _sync_local_target(job.dst_url, plan.offset + size)
    elapsed = time.monotonic() - started
    checksum = _checked(check, cfg)
    if options.remove_source and job.src_url is not None:
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


def _open_for_digest(url: XRootDURL, config: Config) -> IO[bytes]:
    """A local file, or a ZIP member through :func:`~xrdclient.io.open_url`, to hash.

    The caller owns the handle and closes it.
    """
    if _zip_member(url) is not None:
        from ..io import open_url

        return cast("IO[bytes]", open_url(url, "rb", config=config))
    return open(url.path, "rb")


def _digest_of(url: XRootDURL, config: Config, algorithm: str) -> str:
    """``algorithm`` over ``url``: the server's answer, or ours if it is local."""
    if not url.is_local and _zip_member(url) is None:
        return _server_checksum(url, config, algorithm).value.lower()
    digest = new_checksum(algorithm)
    with _open_for_digest(url, config) as handle:
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

    Target directories are created as needed, wherever the target is: a local
    copy calls ``os.makedirs``, a ``root://`` write asks the server for
    ``kXR_mkpath``, and a WebDAV tree has its directories made up front, since
    ``PUT`` makes none. Options other than the ones named here - ``dry_run``
    and ``verify`` among them - are passed through to :func:`copy`.

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
    dry_run = bool(options.get("dry_run"))
    if not dry_run:
        # A WebDAV tree's directories are made once, here, rather than one
        # connection and MKCOL per file inside every copy() below.
        _make_tree_dirs(dst_url, wanted, cfg)

    def move(rel: str) -> CopyResult | None:
        """Copy one file of the tree; ``None`` when a sync found it up to date."""
        destination = dst_url / rel
        if sync is not None and _up_to_date(src_url / rel, destination, cfg, sync, algo):
            return None
        if destination.is_local and not dry_run:
            os.makedirs(os.path.dirname(destination.path), exist_ok=True)
        report = progress if total is None else total.worker()
        # Set in this worker's own thread, so it covers exactly this copy.
        with _parents_already_made():
            return copy(src_url / rel, destination, config=cfg, progress=report, **options)

    results = _each(wanted, count, move)
    if delete:
        _prune(dst_url, cfg, set(wanted), dry_run=dry_run)
    return results
