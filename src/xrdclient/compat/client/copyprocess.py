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
  ``parallelchunks`` and ``tpctimeout`` do what they do in XrdCl.
- ``sourcelimit``, ``coerce``, ``dynamicsource``, ``inittimeout``,
  ``cptimeout``, ``xrate`` and ``xrateThreshold`` are accepted and have no
  effect: this client reads from one source, applies no rate limit, and bounds
  a copy by :class:`~xrdclient.config.Config`'s own timeouts.
"""

from __future__ import annotations

import dataclasses
import os
import posixpath
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ... import errors
from ...client.filesystem import FileSystem
from ...copy.engine import copy as _copy_file
from ...copy.tpc import third_party as _third_party
from ...url import parse
from . import env
from ._status import (
    OK,
    errCheckSumError,
    errInvalidArgs,
    errOperationInterrupted,
    failure,
    from_exception,
)
from .responses import XRootDStatus
from .url import URL

__all__ = ["CopyProcess"]


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
        """Queue a copy of ``source`` to ``target`` (a file, or a directory ending ``/``)."""
        del sourcelimit, coerce, dynamicsource, inittimeout, cptimeout, xrateThreshold, xrate
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
                tpctimeout=tpctimeout if tpctimeout is not None else _int("CPTPCTimeout"),
                rmBadCksum=rmBadCksum,
                retry=retry if retry is not None else _int("CpRetry"),
                cont=cont,
                rtrplc=rtrplc or env.EnvGetString("CpRetryPolicy") or "force",
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
        results = {"status": from_exception(exc)}
    _tell(handler, "end", job_id, results)
    return results


def _attempts(job: _Job, job_id: int, handler: Any) -> dict[str, Any]:
    """Copy, retrying a transient failure ``job.retry`` times."""
    resume = job.cont
    for attempt in range(job.retry + 1):
        try:
            return _once(job, job_id, handler, resume=resume)
        except errors.TransientError:
            if attempt == job.retry:
                raise
            resume = job.rtrplc == "continue"
    raise AssertionError("unreachable")  # pragma: no cover


def _once(job: _Job, job_id: int, handler: Any, *, resume: bool) -> dict[str, Any]:
    target = _destination(job)
    if job.mkdir:
        _make_parent(target)
    try:
        result = _transfer(job, job_id, target, handler, resume=resume)
    except errors.ChecksumMismatchError as exc:
        if job.rmBadCksum:
            _remove(target)
        return {"status": from_exception(exc)}
    outcome: dict[str, Any] = {"size": result.size, "status": OK}
    if result.checksum is not None:
        found = f"{result.checksum.algorithm}:{result.checksum.value}"
        outcome.update(sourceCheckSum=found, targetCheckSum=found)
        if job.checksumpreset and job.checksumpreset.lower() != result.checksum.value.lower():
            outcome["status"] = failure(errCheckSumError, f"expected {job.checksumpreset}")
    return outcome


def _transfer(job: _Job, job_id: int, target: str, handler: Any, *, resume: bool) -> Any:
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
            )
        except (errors.UnsupportedError, ValueError):
            if job.thirdparty == "only":
                raise
    verify = job.checksummode != "none"

    def progress(done: int, size: int | None) -> None:
        if _tell(handler, "should_cancel", job_id):
            raise _Cancelled
        _tell(handler, "update", job_id, done, size or 0)

    return _copy_file(
        job.source,
        target,
        chunk_size=job.chunksize or None,
        verify=verify,
        algorithm=job.checksumtype or None,
        # Continuing a partial target means writing over what is there.
        overwrite=job.force or resume,
        progress=progress,
        config=config,
        resume=resume,
    )


def _destination(job: _Job) -> str:
    """Where the file goes: into ``target`` when that names a directory."""
    if not job.target.endswith("/") and not _is_local_dir(job.target):
        return job.target
    name = posixpath.basename(parse(job.source).path.rstrip("/"))
    return job.target.rstrip("/") + "/" + name


def _is_local_dir(target: str) -> bool:
    url = parse(target)
    return url.is_local and os.path.isdir(url.path)


def _make_parent(target: str) -> None:
    url = parse(target)
    parent = posixpath.dirname(url.path.rstrip("/")) or "/"
    if url.is_local:
        os.makedirs(parent, exist_ok=True)
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
