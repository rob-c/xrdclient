"""``XRootD.client.CopyProcess``: queue jobs, ``prepare()``, ``run(handler)``.

Each job is one :func:`xrdclient.copy` (or :func:`xrdclient.third_party`),
configured from :meth:`CopyProcess.add_job`'s keywords, which are the
bindings' keywords with the bindings' defaults. ``run`` returns
``(status, results)`` with one dict per job - ``status`` always, ``size``
when it succeeded, and ``sourceCheckSum``/``targetCheckSum`` when a checksum
was compared - and reports progress to a :class:`~.utils.CopyProgressHandler`.

What the keywords do here:

- ``force``, ``posc``, ``mkdir``, ``cont``, ``retry``, ``rtrplc``,
  ``thirdparty`` (``"none"``, ``"first"``, ``"only"``), ``checksummode``,
  ``checksumtype``, ``checksumpreset``, ``rmBadCksum``, ``chunksize``,
  ``parallelchunks`` and ``tpctimeout`` do what they do in XrdCl
  (``XrdClCopyProcess.cc``, ``XrdClClassicCopyJob.cc``). In particular a
  retry under the ``"force"`` policy overwrites what the failed attempt left,
  and one under ``"continue"`` carries on from it; ``checksummode`` takes the
  source's checksum for ``"end2end"`` and ``"source"`` (or the preset, for any
  mode), the target's for ``"end2end"`` and ``"target"``, and compares them
  only when it has both - so any other mode, ``"end"`` included, checks
  nothing; and ``target`` names the file itself, a trailing ``/`` or all.
- ``sourcelimit`` above 1 reads a ``root://`` file from that many of its
  located replicas at once (XrdCl's extreme copy), ``dynamicsource`` reads
  until a read comes back short instead of trusting the size, ``xrate`` caps
  the rate, ``xrateThreshold`` fails a copy slower than it every
  ``parallelchunks + 1`` chunks (``errThresholdExceeded``), ``cptimeout``
  fails one that has run longer (``errOperationExpired``, whole seconds, as
  XrdCl counts them), and ``coerce`` opens the target with ``kXR_force``.
  ``inittimeout`` bounds a third-party copy's set-up, and - as in XrdCl -
  nothing about a classic one.
"""

from __future__ import annotations

import dataclasses
import errno
import os
import posixpath
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ... import errors
from ...client.filesystem import FileSystem
from ...copy.engine import _digest_of
from ...copy.engine import copy as _copy_file
from ...copy.limits import RateThresholdError
from ...copy.replicas import NoMoreReplicasError
from ...copy.tpc import third_party as _third_party
from ...url import parse
from . import env
from ._status import (
    OK,
    errCheckSumError,
    errErrorResponse,
    errInvalidArgs,
    errLocalError,
    errOperationInterrupted,
    failure,
    from_exception,
    status,
)
from .responses import XRootDStatus
from .url import URL

__all__ = ["CopyProcess", "ProgressHandlerWrapper"]


class _Cancelled(Exception):
    """Raised inside a copy's progress callback when the handler asks to stop."""


@dataclasses.dataclass
class _Job:
    """One :meth:`CopyProcess.add_job`, with the keywords that affect it."""

    source: str
    target: str
    force: bool = False
    posc: bool = False
    mkdir: bool = False
    thirdparty: str = "none"
    checksummode: str = "none"
    checksumtype: str = ""
    checksumpreset: str = ""
    chunksize: int = 0
    parallelchunks: int = 0
    tpctimeout: int = 0
    rmBadCksum: bool = False
    retry: int = 0
    cont: bool = False
    rtrplc: str = "force"
    sourcelimit: int = 1
    coerce: bool = False
    dynamicsource: bool = False
    inittimeout: int = 0
    cptimeout: int = 0
    xrateThreshold: int = 0
    xrate: int = 0


class ProgressHandlerWrapper:
    """The bindings' adapter between a copy engine and a progress handler.

    It hands the handler :class:`~.url.URL` objects rather than strings and a
    ``results["status"]`` that is an :class:`XRootDStatus` rather than a
    ``dict``, and tolerates no handler at all. :class:`CopyProcess` does
    both itself; this is here for code that builds one by name.
    """

    def __init__(self, handler: Any) -> None:
        self.handler = handler

    def begin(self, jobId: int, total: int, source: Any, target: Any) -> None:
        if self.handler:
            self.handler.begin(jobId, total, URL(source), URL(target))

    def end(self, jobId: int, results: dict[str, Any]) -> None:
        if isinstance(results.get("status"), dict):
            results["status"] = XRootDStatus(results["status"])
        if self.handler:
            self.handler.end(jobId, results)

    def update(self, jobId: int, processed: int, total: int) -> None:
        if self.handler:
            self.handler.update(jobId, processed, total)

    def should_cancel(self, jobId: int) -> bool:
        if self.handler:
            return bool(self.handler.should_cancel(jobId))
        return False


class CopyProcess:
    """Copy jobs run together, in the bindings' shape."""

    def __init__(self) -> None:
        self.__jobs: list[_Job] = []
        self.__parallel = 1

    def parallel(self, parallel: int) -> None:
        """Run up to ``parallel`` jobs at once."""
        self.__parallel = max(1, int(parallel))

    def add_job(
        self,
        source: str,
        target: str,
        sourcelimit: int = 1,
        force: bool = False,
        posc: bool = False,
        coerce: bool = False,
        mkdir: bool = False,
        thirdparty: str = "none",
        checksummode: str = "none",
        checksumtype: str = "",
        checksumpreset: str = "",
        dynamicsource: bool = False,
        chunksize: int | None = None,
        parallelchunks: int | None = None,
        inittimeout: int | None = None,
        tpctimeout: int | None = None,
        rmBadCksum: bool = False,
        cptimeout: int | None = None,
        xrateThreshold: int | None = None,
        xrate: int = 0,
        retry: int | None = None,
        cont: bool = False,
        rtrplc: str | None = None,
    ) -> None:
        """Queue a copy of ``source`` to the file ``target``."""
        self.__jobs.append(
            _Job(
                source=str(source),
                target=str(target),
                force=force,
                posc=posc,
                mkdir=mkdir,
                thirdparty=thirdparty,
                checksummode=checksummode,
                checksumtype=checksumtype,
                checksumpreset=checksumpreset,
                chunksize=chunksize or 0,
                parallelchunks=parallelchunks or 0,
                tpctimeout=_given(tpctimeout, "CPTPCTimeout"),
                rmBadCksum=rmBadCksum,
                retry=_given(retry, "CpRetry"),
                cont=cont,
                rtrplc=rtrplc or env.EnvGetString("CpRetryPolicy") or "force",
                sourcelimit=sourcelimit,
                coerce=coerce,
                dynamicsource=dynamicsource,
                inittimeout=_given(inittimeout, "CPInitTimeout"),
                cptimeout=_given(cptimeout, "CPTimeout"),
                # XrdCl takes a threshold of 0 as "not given" and uses the default.
                xrateThreshold=xrateThreshold or _int("XRateThreshold"),
                xrate=xrate,
            )
        )

    def prepare(self) -> XRootDStatus:
        """Check every job names two usable URLs; must come before :meth:`run`."""
        for job in self.__jobs:
            for url in (job.source, job.target):
                if not URL(url).is_valid():
                    return failure(errInvalidArgs, f"invalid URL: {url}")
            if job.thirdparty not in ("none", "first", "only"):
                return failure(errInvalidArgs, f"thirdparty={job.thirdparty!r}")
        return OK

    def run(self, handler: Any = None) -> tuple[XRootDStatus, list[dict[str, Any]]]:
        """Run the jobs; ``(status, results)``, the status the first failure's."""
        total = len(self.__jobs)
        numbered = list(enumerate(self.__jobs, 1))
        if self.__parallel > 1 and total > 1:
            with ThreadPoolExecutor(min(self.__parallel, total)) as pool:
                results = list(pool.map(lambda item: _run(item, total, handler), numbered))
        else:
            results = [_run(item, total, handler) for item in numbered]
        failed = next((r["status"] for r in results if not r["status"].ok), OK)
        return failed, results


def _int(key: str) -> int:
    return env.EnvGetInt(key) or 0


def _given(value: int | None, key: str) -> int:
    """``value``, or XrdCl's setting ``key`` where the caller left it out."""
    return value if value is not None else _int(key)


def _run(item: tuple[int, _Job], total: int, handler: Any) -> dict[str, Any]:
    """One job, start to end, with the handler told at each step."""
    job_id, job = item
    _tell(handler, "begin", job_id, total, URL(job.source), URL(job.target))
    try:
        results = _attempts(job, job_id, handler)
    except _Cancelled:
        results = {"status": failure(errOperationInterrupted)}
    except (TypeError, ValueError) as exc:
        # A job the engine cannot run as described - a third-party copy to a
        # local path, say - which XrdCl reports as a status like any other.
        results = {"status": failure(errInvalidArgs, str(exc))}
    except Exception as exc:
        results = {"status": _job_failure(exc, job)}
    _tell(handler, "end", job_id, results)
    return results


def _job_failure(exc: Exception, job: _Job) -> XRootDStatus:
    """``exc`` as XrdCl reports it: a local file's error is ``errLocalError``.

    XrdCl opens a local end through its own file handler, which answers with
    code 402 and the errno translated to the protocol's number
    (``XProtocol::mapError``): an existing target is ``kXR_ItExists``. Its
    message names the end that failed - ``file exists:  (destination)``,
    two spaces and all.
    """
    named = _ENGINE_CODES.get(type(exc))
    if named is not None:
        return _named(*named)
    if not isinstance(exc, OSError) or isinstance(exc, errors.XRootDError):
        return (
            _server_failure(exc, job)
            if isinstance(exc, errors.ServerError)
            else (from_exception(exc))
        )
    eno = exc.errno or 0
    detail = (exc.strerror or str(exc)).lower()
    source = parse(job.source)
    end = "source" if source.is_local and exc.filename == source.path else "destination"
    return status(errLocalError, errno=_LOCAL_ERRNOS.get(eno, eno), message=f"{detail}:  ({end})")


def _server_failure(exc: errors.ServerError, job: _Job) -> XRootDStatus:
    """A server's refusal, naming the end it came from as XrdCl does.

    ``[3011] Unable to open file ...; No such file or directory (source)``:
    the end is what tells a user whether the file was missing or the
    destination was refused, and XrdCl always says it.
    """
    reported = from_exception(exc)
    where = exc.path
    ends = (("source", job.source), ("destination", job.target))
    end = next((name for name, url in ends if where and _names_path(url, where)), None)
    if end is None:
        return reported
    return status(errErrorResponse, errno=exc.code, message=f"{exc.message} ({end})")


def _names_path(url: str, path: str) -> bool:
    """Whether ``url`` is the file ``path`` a server named in its error."""
    try:
        mine = parse(url).path
    except ValueError:
        return False
    return mine.rstrip("/").lstrip("/") == path.rstrip("/").lstrip("/")


#: What XrdCl reports for the engine's own failures that have no status of
#: their own elsewhere: code, XrdCl's words for it, and the job's detail.
_ENGINE_CODES: dict[type, tuple[int, str, str]] = {
    RateThresholdError: (
        208,
        "Threshold exceeded",
        "The transfer rate dropped below requested threshold!",
    ),
    NoMoreReplicasError: (16, "No more replicas to try", " (source)"),
}


def _named(code: int, words: str, detail: str) -> XRootDStatus:
    """An error status with XrdCl's words for ``code`` and the job's ``detail``."""
    return XRootDStatus(
        {
            "status": 1,
            "code": code,
            "errno": 0,
            "message": f"[ERROR] {words}: {detail}",
            "shellcode": code // 100 + 50,
            "error": True,
            "fatal": False,
            "ok": False,
        }
    )


#: ``XProtocol::mapError`` for the errors a local copy end meets.
_LOCAL_ERRNOS = {
    errno.ENOENT: errors.kXR_NotFound,
    errno.EPERM: errors.kXR_NotAuthorized,
    errno.EACCES: errors.kXR_NotAuthorized,
    errno.EIO: errors.kXR_IOError,
    errno.ENOSPC: errors.kXR_NoSpace,
    errno.ENAMETOOLONG: errors.kXR_ArgTooLong,
    errno.EISDIR: errors.kXR_isDirectory,
    errno.EEXIST: errors.kXR_ItExists,
    errno.EROFS: errors.kXR_fsReadOnly,
    errno.EDQUOT: errors.kXR_overQuota,
}


def _attempts(job: _Job, job_id: int, handler: Any) -> dict[str, Any]:
    """Copy, retrying a transient failure ``job.retry`` times.

    As in XrdCl, a retry under the ``"continue"`` policy resumes what the
    failed attempt left at the target, and under any other - ``"force"`` is
    the default - overwrites it.
    """
    for attempt in range(job.retry + 1):
        try:
            return _once(job, job_id, handler)
        except errors.TransientError:
            if attempt == job.retry:
                raise
            resume = job.rtrplc == "continue"
            job = dataclasses.replace(job, force=not resume, cont=resume)
    raise AssertionError("unreachable")  # pragma: no cover


def _once(job: _Job, job_id: int, handler: Any) -> dict[str, Any]:
    target = _destination(job)
    _make_parent(target, remote=job.mkdir)
    try:
        result = _transfer(job, job_id, target, handler)
    except errors.ChecksumMismatchError as exc:
        if job.rmBadCksum:
            _remove(target)
        return {"status": from_exception(exc)}
    outcome: dict[str, Any] = {"size": result.size, "status": OK}
    try:
        _verify(job, target, outcome)
    except (errors.XRootDError, OSError) as exc:
        outcome["status"] = _job_failure(exc, job)
    return outcome


def _verify(job: _Job, target: str, outcome: dict[str, Any]) -> None:
    """The checksums ``checksummode`` asks for, compared if there are two.

    ``XrdClClassicCopyJob.cc``, ``Run``: the source's for ``"end2end"`` and
    ``"source"`` - or the preset, whatever the mode - and the target's for
    ``"end2end"`` and ``"target"``; a side that cannot say fails the job.
    """
    algorithm = job.checksumtype or env.config().preferred_checksum
    if job.checksumpreset:
        outcome["sourceCheckSum"] = _labelled(algorithm, job.checksumpreset)
    elif job.checksummode in ("end2end", "source"):
        outcome["sourceCheckSum"] = _checksum(job.source, algorithm)
    if job.checksummode in ("end2end", "target"):
        outcome["targetCheckSum"] = _checksum(target, algorithm)
    theirs, ours = outcome.get("sourceCheckSum"), outcome.get("targetCheckSum")
    if theirs is None or ours is None or theirs.lower() == ours.lower():
        return
    if job.rmBadCksum:
        _remove(target)
    outcome["status"] = failure(errCheckSumError)


def _checksum(url: str, algorithm: str) -> str:
    """``algorithm:value`` for ``url``: its server's answer, or ours if it is local."""
    return _labelled(algorithm, _digest_of(parse(url), env.config(), algorithm))


def _labelled(algorithm: str, value: str) -> str:
    """XrdCl's form: ``Utils::NormalizeChecksum`` drops an adler32 or crc32's leading zeros."""
    if algorithm in ("adler32", "crc32"):
        value = value.lstrip("0")
    return f"{algorithm}:{value}"


def _transfer(job: _Job, job_id: int, target: str, handler: Any) -> Any:
    """The bytes themselves: server to server, through here, or one then the other."""
    config = env.config()
    if job.parallelchunks:
        config = dataclasses.replace(config, in_flight=job.parallelchunks)
    if job.thirdparty != "none":
        try:
            return _third_party(
                job.source,
                target,
                config=config,
                overwrite=job.force,
                posc=job.posc,
                timeout=job.tpctimeout or None,
                init_timeout=job.inittimeout or None,
                coerce=job.coerce,
                # XrdCl verifies only what ``checksummode`` asks for, and this
                # job does that itself; the library's own default check
                # would add queries the bindings never make.
                verify=False,
            )
        except (errors.UnsupportedError, ValueError):
            if job.thirdparty == "only":
                raise
    return _classic(job, job_id, target, handler, config)


def _classic(job: _Job, job_id: int, target: str, handler: Any, config: Any) -> Any:
    """The copy through this process, reporting to ``handler`` as it goes."""

    def progress(done: int, size: int | None) -> None:
        if _tell(handler, "should_cancel", job_id):
            raise _Cancelled
        _tell(handler, "update", job_id, done, size or 0)

    if job.xrateThreshold:
        # The threshold is judged every ``parallelChunks + 1`` chunks, and
        # XrdCl's ``parallelChunks`` defaults to four.
        config = dataclasses.replace(config, in_flight=job.parallelchunks or _PARALLEL_CHUNKS)
    return _copy_file(
        job.source,
        target,
        chunk_size=job.chunksize or None,
        # Checksums are XrdCl's to take, after the copy: see ``_verify``.
        verify=False,
        # Continuing a partial target means writing over what is there.
        overwrite=job.force or job.cont,
        progress=progress,
        config=config,
        resume=job.cont,
        **_limits(job),
    )


def _limits(job: _Job) -> dict[str, Any]:
    """``add_job``'s transfer controls, as :func:`xrdclient.copy` takes them."""
    return {
        "sources": job.sourcelimit,
        "dynamic_source": job.dynamicsource,
        "max_rate": job.xrate or None,
        "min_rate": job.xrateThreshold or None,
        # XrdCl counts whole seconds, so ``cptimeout=N`` expires at N + 1.
        "timeout": job.cptimeout + 1 if job.cptimeout else None,
        "coerce": job.coerce,
    }


#: XrdCl's ``DefaultCPParallelChunks``.
_PARALLEL_CHUNKS = 4


def _destination(job: _Job) -> str:
    """Where the file goes: ``target`` itself, as XrdCl takes it.

    A local target loses a trailing ``/`` - ``/tmp/d/`` is the file ``d``, as
    XrdCl's local file handler has it; a remote one goes to the server as given.
    """
    url = parse(job.target)
    return url.path.rstrip("/") or "/" if url.is_local else job.target


def _make_parent(target: str, *, remote: bool) -> None:
    """The target's directory: always for a local one, as XrdCl makes it, else if asked."""
    url = parse(target)
    parent = posixpath.dirname(url.path) or "/"
    if url.is_local:
        os.makedirs(parent, exist_ok=True)
        return
    if not remote:
        return
    with FileSystem(url.with_path("/"), env.config()) as fs:
        fs.makedirs(parent, exist_ok=True)


def _remove(target: str) -> None:
    """Delete a target whose checksum was wrong; a failure to is not the story."""
    url = parse(target)
    try:
        if url.is_local:
            os.remove(url.path)
            return
        with FileSystem(url.with_path("/"), env.config()) as fs:
            fs.remove(url.path)
    except OSError:
        pass


def _tell(handler: Any, method: str, *args: Any) -> Any:
    """Call ``handler.method(*args)`` if the handler has one."""
    hook = getattr(handler, method, None)
    return hook(*args) if callable(hook) else None
