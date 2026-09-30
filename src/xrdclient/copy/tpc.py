"""Server-side third-party copy: the bytes never touch this process.

The client only brokers a rendezvous - it mints a key, tells the destination
to pull from the source, and waits. The dialect below is the stock XRootD one
(``XrdOucTPC``'s ``cgiC2Dst``/``cgiC2Src`` plus ``XrdCl``'s control order),
translated from ``libxrdc/lib/xfer/copy_remote.c:copy_tpc``. The legacy
full-URL ``tpc.src=root://host:port/path`` form is deliberately *not* emitted:
a stock destination cannot parse it, and a stock source cannot match its
``tpc.dst`` against the puller's hostname.
"""

from __future__ import annotations

import dataclasses
import secrets
import time
from collections.abc import Callable

from ..config import Config
from ..errors import ChecksumMismatchError
from ..flags import Access, OpenFlags
from ..proto import constants as c
from ..proto import requests as r
from ..proto import responses as rp
from ..proto.frames import Request
from ..session import Router
from ..url import XRootDURL, parse
from .engine import CopyResult, _compare_ends, _probe, _remove
from .limits import CopyTimeoutError

__all__ = ["third_party"]

#: Creation mode for the destination file: owner read/write, as ``xrdcp`` uses.
_MODE = int(Access.OWNER_READ | Access.OWNER_WRITE)


def _dst_opaque(key: str, src: XRootDURL, src_endpoint: str, size: int, token_mode: str) -> str:
    """``cgiC2Dst`` - what tells the destination who to pull from.

    ``tpc.dlg`` names the originally requested source endpoint and is inert
    while ``tpc.dlgon=0``; it is emitted for wire parity with ``XrdCl``.
    """
    parts = [
        f"tpc.key={key}",
        f"tpc.src={src_endpoint}",
        f"tpc.lfn={src.path}",
        f"tpc.dlg={src.host}:{src.port}",
        "tpc.spr=root",
        "tpc.tpr=root",
        "tpc.dlgon=0",
    ]
    if size >= 0:
        parts.append(f"oss.asize={size}")
    parts.append("tpc.stage=copy")
    if token_mode:
        parts.append(f"tpc.token_mode={token_mode}")
    return "&".join(parts)


def _src_opaque(key: str, dst_host: str, token_mode: str) -> str:
    """``cgiC2Src`` - what authorises the pull the destination is about to make."""
    parts = [f"tpc.key={key}", f"tpc.dst={dst_host}", "tpc.stage=copy"]
    if token_mode:
        parts.append(f"tpc.token_mode={token_mode}")
    return "&".join(parts)


def third_party(
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
) -> CopyResult:
    """Ask the destination server to pull ``source`` directly.

        >>> third_party("root://src.example//store/f", "root://dst.example//store/f")
        >>> third_party("davs://src.example/store/f", "davs://dst.example/store/f")

    Both endpoints must speak the same protocol, because each dialect is one
    server asking another for the file in a language it understands: two
    ``root://`` URLs use the ``XrdOucTPC`` rendezvous below, and two
    ``http(s)``/``dav(s)`` URLs use the WLCG ``COPY`` dialect in
    :func:`xrdclient.http.third_party`. For a mixed pair - or for a copy where the
    data must pass through you anyway - use :func:`~xrdclient.copy`, which streams
    it through this process.

    The transfer is complete when this returns; over ``root://`` the final
    ``kXR_sync`` blocks until the destination has finished, which the server
    may report through a ``kXR_waitresp`` deferral.

    No byte passes through this process, so there is no stream to digest.
    Verification instead asks both servers for their checksum of the file
    once the transfer is done - ``algorithm``, or
    ``config.preferred_checksum`` - and raises
    :class:`~xrdclient.errors.ChecksumMismatchError` if they differ, having
    removed the destination this copy wrote. It is on by default (``None``
    follows ``config.verify_checksums``), because a destination can call a
    pull finished that never happened: RAL's Echo answered one from EOS with
    success and a full-size file of no data. Left to the default, an end that
    cannot say its checksum leaves the copy unverified; ``verify=True``
    raises the server's error instead, and ``verify=False`` skips it.

    Over ``root://``, ``timeout`` bounds the wait for the transfer itself and
    ``init_timeout`` everything before it - opening both ends and arming the
    pull - raising :class:`~xrdclient.copy.CopyTimeoutError` when it runs out,
    as XrdCl's ``initTimeout`` does; ``coerce`` opens the destination with
    ``kXR_force``, so the server ignores its file usage rules.
    """
    cfg = config or Config()
    su, du = parse(source), parse(target)
    if su.is_http and du.is_http:
        result = _http_copy(su, du, cfg, overwrite, token_mode, timeout)
    else:
        _require_root_pair(su, du)
        cfg = _timeout_config(cfg, timeout)
        options = _Options(overwrite, posc, coerce, token_mode)
        result = _root_transfer(su, du, cfg, options, init_timeout)
    return _verified(result, su, du, cfg, verify, algorithm)


def _root_transfer(
    su: XRootDURL, du: XRootDURL, cfg: Config, options: _Options, init_timeout: float | None
) -> CopyResult:
    """The ``root://`` rendezvous; an empty destination it leaves on failure is removed."""
    try:
        return _root_copy(su, du, cfg, options, _starting(init_timeout))
    except OSError:
        discard_if_empty(du, cfg)
        raise


def _verified(
    result: CopyResult,
    su: XRootDURL,
    du: XRootDURL,
    cfg: Config,
    verify: bool | None,
    algorithm: str | None,
) -> CopyResult:
    """``result``, once both servers agree on the file - see :func:`third_party`."""
    if verify is False or (verify is None and not cfg.verify_checksums):
        return result
    try:
        checksum = _compare_ends(
            su, du, cfg, algorithm or cfg.preferred_checksum, strict=bool(verify)
        )
    except ChecksumMismatchError:
        _remove_quietly(du, cfg)  # this copy wrote it, and it is wrong
        raise
    return dataclasses.replace(result, checksum=checksum)


def discard_if_empty(url: XRootDURL, config: Config) -> None:
    """After a failed third-party copy: remove the destination if it is empty.

    A destination names the file before it pulls - EOS does, then refuses
    the copy - and an empty file left there looks like a finished copy of an
    empty one. A destination with bytes in it is left alone. Best effort.
    """
    try:
        there = _probe(url, config)
        if there is not None and there[0] == 0:
            _remove(url, config)
    except OSError:
        pass


def _remove_quietly(url: XRootDURL, config: Config) -> None:
    """Best effort: a failed removal must not hide the mismatch."""
    try:
        _remove(url, config)
    except OSError:
        pass


@dataclasses.dataclass(frozen=True)
class _Options:
    """How the destination is opened, and what the rendezvous carries."""

    overwrite: bool
    posc: bool
    coerce: bool
    token_mode: str


def _starting(init_timeout: float | None) -> Callable[[], None]:
    """A check that the copy is still within ``init_timeout`` of starting.

    XrdCl's ``InitTimeoutCalc``: asked after each step of the set-up, and
    ``errOperationExpired`` once the time is gone.
    """
    started = time.monotonic()

    def check() -> None:
        if init_timeout and time.monotonic() - started > init_timeout:
            raise CopyTimeoutError("the third-party copy took longer than init_timeout to start")

    return check


def _root_copy(
    su: XRootDURL, du: XRootDURL, cfg: Config, options: _Options, check: Callable[[], None]
) -> CopyResult:
    """The ``XrdOucTPC`` rendezvous, in the order ``XrdCl`` runs it.

    Both routers here are sticky - a redirect moves the router, not just the
    one request - because what the rendezvous needs to know is where each
    end *landed*: the source is told which host will pull (``tpc.dst``) and
    the destination which host to pull from (``tpc.src``), and behind a
    redirector neither is the host named in the URL.
    """
    key = secrets.token_hex(16)
    started = time.monotonic()

    src_router = Router(su.with_path("/"), cfg)
    dst_router = Router(du.with_path("/"), cfg)
    try:
        # 1. Placement: open the source as XrdCl does (``tpc.stage=placement``)
        #    and keep the data server it was redirected to - ``tpcSource``.
        size, capability = _place_source(src_router, su)
        check()
        opaque = _dst_opaque(key, su, src_router.endpoint, size, options.token_mode)
        flags = _dst_flags(options.overwrite, options.posc, coerce=options.coerce)
        result = dst_router.execute(r.Open(f"{du.path}?{opaque}", flags, _MODE), path=du.path)
        # The handle exists only on the server that answered the open, and
        # the pinned router takes the connection over: the name it is bound
        # to here is the only one left holding it. That server is also the
        # host that will connect to the source to pull, which is what a stock
        # source checks ``tpc.dst`` against - XrdCl's ``realTarget``, taken
        # from the open's last URL.
        dst_router = dst_router.pin(transfer=True)
        handle, _, _ = rp.parse_open(result.data, du.path)
        puller = dst_router.url.host
        check()
        opaque = _src_opaque(key, puller, options.token_mode)
        if capability:
            opaque = f"{opaque}&{capability}"
        _rendezvous(src_router, dst_router, handle, su, opaque, check)
        dst_router.execute(r.Close(handle))
    finally:
        dst_router.close()
        src_router.close()

    return CopyResult(
        source=str(su), target=str(du), size=max(size, 0), seconds=time.monotonic() - started
    )


def _dst_flags(overwrite: bool, posc: bool, *, coerce: bool = False) -> int:
    """The destination open's options: create or replace, persist on close, force."""
    flags = OpenFlags.UPDATE | (OpenFlags.DELETE if overwrite else OpenFlags.NEW)
    if posc:
        flags |= OpenFlags.POSC
    if coerce:
        flags |= OpenFlags.FORCE
    return int(flags) | c.kXR_retstat


def _place_source(router: Router, source: XRootDURL) -> tuple[int, str]:
    """Open ``source`` for placement, leaving ``router`` on its data server.

    Returns the size the open's ``kXR_retstat`` reported (``-1`` for a
    server that sent none), and the CGI the redirect to the data server
    added. The handle is closed straight away; the source is opened again,
    with the key, once the destination has armed the pull - on that same
    data server, and so with that same CGI: EOS's disk servers only open a
    file for a request carrying the capability its head node signed
    (``cap.sym``/``cap.msg``), and refuse one without it as "capability
    illegal". XrdCl re-opens the placement open's last URL, which has it.
    """
    request = r.Open(f"{source.path}?tpc.stage=placement", int(OpenFlags.READ) | c.kXR_retstat)
    result = router.execute(request, path=source.path)
    handle, info, _ = rp.parse_open(result.data, source.path)
    _quietly(router, r.Close(handle))
    return (info.st_size if info is not None else -1), _redirect_cgi(request.path)


def _redirect_cgi(path: str) -> str:
    """The CGI a redirect put on ``path``, less the placement's own."""
    _, _, query = path.partition("?")
    kept = [f for f in query.split("&") if f and not f.startswith("tpc.stage=")]
    return "&".join(kept)


def _rendezvous(
    src_router: Router,
    dst_router: Router,
    handle: bytes,
    source: XRootDURL,
    opaque: str,
    check: Callable[[], None],
) -> None:
    """Arm the pull, register the key at the source, then trigger and wait.

    The source open may be deferred until the pull completes; the final
    ``kXR_sync`` blocks until the destination has finished. Everything before
    that sync is set-up, and ``check`` is asked after each step of it.
    """
    dst_router.execute(r.Sync(handle))
    check()
    src_result = src_router.execute(
        r.Open(f"{source.path}?{opaque}", int(OpenFlags.READ)), path=source.path
    )
    src_handle, _, _ = rp.parse_open(src_result.data, source.path)
    try:
        check()
        dst_router.execute(r.Sync(handle))
    finally:
        _quietly(src_router, r.Close(src_handle))


def _http_copy(
    source: XRootDURL,
    target: XRootDURL,
    config: Config,
    overwrite: bool,
    token_mode: str,
    timeout: float | None,
) -> CopyResult:
    if token_mode:
        raise ValueError(
            "token_mode is a root:// option; HTTP third-party copy delegates "
            "through the Credential header - see xrdclient.http.third_party"
        )
    from ..http import third_party as http_third_party

    return http_third_party(source, target, config=config, overwrite=overwrite, timeout=timeout)


def _require_root_pair(source: XRootDURL, target: XRootDURL) -> None:
    if not source.is_root or not target.is_root:
        raise ValueError(
            "third-party copy needs two endpoints of the same kind, not "
            f"{source.scheme}:// and {target.scheme}://"
        )


def _timeout_config(config: Config, timeout: float | None) -> Config:
    return config.evolve(request_timeout=timeout) if timeout is not None else config


def _quietly(router: Router, request: Request) -> None:
    """Best-effort cleanup: a failed close must not mask the real error."""
    try:
        router.execute(request)
    except OSError:
        pass
