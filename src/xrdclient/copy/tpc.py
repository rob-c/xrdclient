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

import secrets
import time

from ..client import FileSystem
from ..config import Config
from ..flags import Access, OpenFlags
from ..proto import constants as c
from ..proto import requests as r
from ..proto import responses as rp
from ..proto.frames import Request
from ..session import Router
from ..url import XRootDURL, parse
from .engine import CopyResult

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
    """
    cfg = config or Config()
    su, du = parse(source), parse(target)
    if su.is_http and du.is_http:
        return _http_copy(su, du, cfg, overwrite, token_mode, timeout)
    _require_root_pair(su, du)
    cfg = _timeout_config(cfg, timeout)

    key = secrets.token_hex(16)
    started = time.monotonic()

    # 1. Placement: the source session follows any cluster redirect, so its
    #    live endpoint is the data server the destination must pull from.
    with FileSystem(su.with_path("/"), cfg) as src_fs:
        size = src_fs.stat(su.path).st_size
        src_endpoint = src_fs.endpoint

        dst_router = Router(du.with_path("/"), cfg)
        try:
            flags = OpenFlags.UPDATE | (OpenFlags.DELETE if overwrite else OpenFlags.NEW)
            if posc:
                flags |= OpenFlags.POSC
            opaque = _dst_opaque(key, su, src_endpoint, size, token_mode)
            result = dst_router.execute(
                r.Open(f"{du.path}?{opaque}", int(flags) | c.kXR_retstat, _MODE),
                path=du.path,
            )
            # The handle exists only on the server that answered the open, and
            # the pinned router takes the connection over: the name it is
            # bound to here is the only one left holding it.
            dst_router = dst_router.pin(transfer=True)
            handle, _, _ = rp.parse_open(result.data, du.path)

            # 2. Arm the rendezvous, then register the key at the source. The
            #    source open may be deferred until the pull completes.
            dst_router.execute(r.Sync(handle))
            src_router = src_fs._router.pin()
            src_result = src_router.execute(
                r.Open(f"{su.path}?{_src_opaque(key, du.host, token_mode)}", int(OpenFlags.READ)),
                path=su.path,
            )
            src_handle, _, _ = rp.parse_open(src_result.data, su.path)

            # 3. Trigger the pull and wait for the destination to finish.
            try:
                dst_router.execute(r.Sync(handle))
            finally:
                _quietly(src_router, r.Close(src_handle))
            dst_router.execute(r.Close(handle))
        finally:
            dst_router.close()

    return CopyResult(source=str(su), target=str(du), size=size, seconds=time.monotonic() - started)


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
