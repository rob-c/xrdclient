"""Server-side third-party copy: the bytes never touch this process.

The client only brokers a rendezvous - it mints a key, tells the destination
to pull from the source, and waits. The dialect below is the stock XRootD one
(``XrdOucTPC``'s ``cgiC2Dst``/``cgiC2Src`` plus ``XrdCl``'s control order),
translated from ``libxrdc/lib/xfer/copy_remote.c:copy_tpc``. The legacy
full-URL ``tpc.src=root://host:port/path`` form is deliberately *not* emitted:
a stock destination cannot parse it, and a stock source cannot match its
``tpc.dst`` against the puller's hostname.

Where to start: :func:`third_party`, which picks the HTTP ``COPY`` path or
the ``root://`` rendezvous (:func:`_root_copy`). The rendezvous follows
XrdCl's ``ThirdPartyCopyJob`` (``CanDo``, then ``RunTPC`` or ``RunLite``):

1. *Placement* - open the source with ``tpc.stage=placement`` and close it,
   which finds its data server and size (:func:`_place_source`).
2. Open the destination with the :func:`_dst_opaque` CGI.
3. *Capability checks* (:func:`_settle`) - ``kXR_Qconfig`` ``tpc tpcdlg`` at
   the destination (``CheckTPCLite``) for every copy, then, unless the copy
   is TPC lite, ``tpc`` at the source's data server (``CheckTPC``).
4. ``kXR_sync`` the destination once to arm the pull, open the source with
   ``tpc.key`` and ``tpc.dst`` so it will accept the pull (classic copy
   only), then a second ``kXR_sync`` that blocks until the destination has
   the file (:func:`_pull`), bounded by :attr:`Config.tpc_timeout
   <xrdclient.Config.tpc_timeout>` - XrdCl's ``CPTPCTimeout``.

Every open carries the URL's own CGI - an ``authz=`` token, a space token,
``XRD.*`` options - with the rendezvous's fields added after it, as XrdCl
merges its ``tpc.*`` fields over the URL's (:func:`_opening`); only this
client's ``xrdcl.*`` directives stay behind.

What must not break: the order (a stock source rejects a pull whose key it
has not seen) and the cleanup - every handle opened is closed even when a
later step fails, because the key lives as long as the open; and only a
destination this copy opened is ever removed afterwards (:class:`_Target`).

Deliberate differences from XrdCl, each for a reason given where it is
done: ``CheckTPC`` here does not refuse a one-character answer (XrdCl's
``answer.length() == 1`` test rejects a bare ``1``, which no stock server
sends, and nothing is gained by copying it); a close that fails after the
pull is named for the end that failed it, where XrdCl's ``UpdateErrMsg``
brackets the other one (see :func:`_close_both`); and a destination this
copy made is removed when the copy fails and leaves it empty, which XrdCl
leaves to its caller (``cleanup=False`` leaves it to the caller here too).
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import secrets
import threading
import time
from collections.abc import Callable
from typing import TypeVar

from xrdcore import cancel as _cancel

from ..config import Config
from ..errors import (
    AbortedError,
    ChecksumMismatchError,
    ServerError,
    UnsupportedError,
    XRootDError,
    kXR_FSError,
)
from ..errors import kXR_Unsupported as _UNSUPPORTED
from ..flags import Access, OpenFlags
from ..proto import constants as c
from ..proto import requests as r
from ..proto import responses as rp
from ..proto.frames import Request
from ..session import Result, Router
from ..session import deadline as dl
from ..url import XRootDURL, parse
from .engine import CopyResult, _compare_ends, _Ends, _probe, _remove
from .limits import CopyTimeoutError

__all__ = ["failed_end", "third_party"]

T = TypeVar("T")

#: Creation mode for the destination file: 0644, XrdCl's ``UR|UW|GR|OR`` in
#: both ``ThirdPartyCopyJob`` and ``ClassicCopyJob``. Owner-only (0600) would
#: hide a pulled file from the group and the world that a stock ``xrdcp``
#: copy leaves readable to them.
_MODE = int(Access.OWNER_READ | Access.OWNER_WRITE | Access.GROUP_READ | Access.OTHER_READ)

#: What XrdCl's ``RunTPC``/``RunLite`` send a pull's destination to stop it
#: (``File::Fcntl("ofs.tpc cancel")``): the string with its NUL, which
#: ``XrdOfsFile::fctl`` insists on.
TPC_CANCEL = b"ofs.tpc cancel\x00"

#: How long a cancelled pull waits for the destination to answer its cancel
#: before the connection is shut down under it instead.
CANCEL_WAIT = 60.0

#: How often, at most, a pull asks the destination how far it has got.
PROGRESS_EVERY = 1.0

#: The note :func:`failed_end` reads: which end of the rendezvous refused.
_END_NOTE = "xrdclient third-party copy: failed at the "

#: A progress callback: bytes the destination holds, and the source's size.
Progress = Callable[[int, "int | None"], None]


def failed_end(exc: BaseException) -> str:
    """Which end of a ``root://`` third-party copy ``exc`` came from.

        >>> try:
        ...     xrdclient.third_party(src, dst)
        ... except OSError as exc:
        ...     print(exc, f"({xrdclient.failed_end(exc)})")

    ``"source"`` or ``"destination"`` for a failure of one end's own step -
    the source's keyed open, the destination's open, its first ``kXR_sync``
    or its close - and ``""`` for everything XrdCl names no end for: the
    source's placement open, a capability refusal ("... does not support
    third-party-copy"), the pull itself (the second sync, its
    :attr:`~xrdclient.Config.tpc_timeout` or a cancel), and anything that
    did not come from :func:`third_party` at all. This is the end XrdCl's
    ``ThirdPartyCopyJob`` puts in brackets after a failed job's message
    (``UpdateErrMsg``), which gfal2 and FTS show their users.

    Public as ``xrdclient.failed_end`` (and :mod:`xrdclient.copy`'s
    ``failed_end``): ``xrdclient.copy`` is the copy *function*, so this
    module is not reachable from it by attribute. The end is read from a
    note :func:`third_party` adds to the exception (``__notes__``), so it
    survives being re-raised or chained.
    """
    for note in getattr(exc, "__notes__", ()):
        if isinstance(note, str) and note.startswith(_END_NOTE):
            return note[len(_END_NOTE) :]
    return ""


def _host_id(url: XRootDURL) -> str:
    """``XrdCl::URL::GetHostId``: ``[user@]host:port``, an IPv6 host bracketed."""
    user = f"{url.username}@" if url.username else ""
    host = f"[{url.host}]" if ":" in url.host else url.host
    return f"{user}{host}:{url.port}"


def _dst_opaque(key: str, src: XRootDURL, src_endpoint: str, size: int, options: _Options) -> str:
    """``cgiC2Dst`` - what tells the destination who to pull from.

    ``src_endpoint`` is the ``host:port`` that will serve the pull (the
    placement's data server, see :func:`_root_copy`) and ``size`` the
    source's length, ``-1`` when the placement could not say; ``options``
    supplies the token mode, delegation, stream count and target scheme.

    ``tpc.dlg`` names the originally requested source endpoint; it is what a
    destination given a delegated credential (``tpc.dlgon=1``) contacts,
    and is emitted either way for wire parity with ``XrdCl``. A delegating
    copy also hands the source's own CGI on, tab-separated, as
    ``tpc.scgi`` - an authorisation token included - since the destination,
    not this client, will be the one opening it; ``xrdcl.*`` fields are this
    client's business and stay behind. ``tpc.str`` asks for parallel
    streams, XrdCl's ``SubStreamsPerChannel`` less the control stream.
    """
    streams, delegate, token_mode = options.streams, options.delegate, options.token_mode
    parts = [f"tpc.key={key}", f"tpc.src={src_endpoint}", f"tpc.lfn={src.path}"]
    parts += [f"tpc.str={streams - 1}"] if streams > 1 else []
    parts += [
        f"tpc.dlg={_host_id(src)}",
        f"tpc.spr={src.scheme}",
        f"tpc.tpr={options.target_scheme}",
        f"tpc.dlgon={1 if delegate else 0}",
    ]
    parts += [f"oss.asize={size}"] if size >= 0 else []
    parts.append("tpc.stage=copy")
    parts += _scgi(src) if delegate else []
    parts += [f"tpc.token_mode={token_mode}"] if token_mode else []
    return "&".join(parts)


def _opening(url: XRootDURL, opaque: str, tail: str = "") -> str:
    """The path an open of the rendezvous names: ``url``'s own CGI, then ``opaque``.

    XrdCl builds each of these URLs by merging its fields over the user's
    (``MessageUtils::MergeCGI`` with replacement), so an ``authz=`` token,
    a ``svcClass`` space token or an ``XRD.*`` option on the URL reaches the
    server it names, and a field the rendezvous sets - ``tpc.key`` above
    all - is the rendezvous's, whatever the URL said. The URL's fields come
    first, then ``opaque``, then ``tail``: the CGI a placement redirect
    added (see :func:`_place_source`), last, as the router itself appends a
    redirect's token. ``xrdcl.*`` fields are this client's directives and
    never go on the wire (XrdCl's ``GetPathWithFilteredParams``).
    """
    taken = {field.partition("=")[0] for field in opaque.split("&")}
    own = [
        field
        for field in url.cgi.split("&")
        if field and not field.startswith("xrdcl.") and field.partition("=")[0] not in taken
    ]
    return f"{url.path}?{'&'.join([*own, opaque, *([tail] if tail else [])])}"


def _scgi(src: XRootDURL) -> list[str]:
    """``tpc.scgi``: the source's CGI, tab-separated, less this client's ``xrdcl.*``."""
    scgi = "\t".join(sorted(f for f in src.cgi.split("&") if f and not f.startswith("xrdcl.")))
    return [f"tpc.scgi={scgi}"] if scgi else []


def _src_opaque(key: str, dst_host: str, token_mode: str) -> str:
    """``cgiC2Src`` - what authorises the pull the destination is about to make.

    ``dst_host`` is the host the destination's open landed on (XrdCl's
    ``realTarget.GetHostName()``): a stock source compares it with the
    address the pull arrives from.
    """
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
    cancel: _cancel.Cancel | None = None,
    progress: Progress | None = None,
    delegate: bool = False,
    streams: int = 1,
    target_config: Config | None = None,
    cleanup: bool = True,
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

    Over ``root://``, ``init_timeout`` bounds everything before the pull -
    opening both ends, the capability checks and arming the pull - raising
    :class:`~xrdclient.copy.CopyTimeoutError` when it runs out, as XrdCl's
    ``initTimeout`` does. The pull itself - one long silence on the
    destination's connection - is bounded by ``timeout`` when it is given
    and otherwise by the destination's :attr:`Config.tpc_timeout
    <xrdclient.Config.tpc_timeout>` (XrdCl's ``CPTPCTimeout``, 1800 s): the
    destination's connection is given that long for each read, stall and
    deferred answer, and the pull expires with
    :class:`~xrdclient.session.deadline.OperationExpiredError` once it is
    spent. ``timeout`` is also every other request's timeout, at both ends.
    ``coerce`` opens the destination with ``kXR_force``, so the server
    ignores its file usage rules.

    Each URL's CGI - an ``authz=`` token, a space token, ``XRD.*`` options -
    goes on every open of that end, with the rendezvous's ``tpc.*`` fields
    added to it. Before the pull the destination is asked whether it does
    third-party copy at all (and TPC lite), and, for a classic copy, so is
    the source's data server; a refusal is
    :class:`~xrdclient.errors.UnsupportedError` in XrdCl's words.

    A failed copy leaves the destination as it found it, as far as it can:
    a destination this copy opened, and left empty, is removed - with the
    destination's settings - since an empty file there would pass for a
    finished copy of an empty one. A destination that was there already,
    and refused (``overwrite=False`` meeting an existing file), is not this
    copy's, and is never touched. ``cleanup=False`` - gfal2's
    ``transfer_cleanup`` off - leaves whatever a failed copy wrote, the
    removal after a checksum mismatch included, to the caller.

    After the pull both handles are closed, the source's first, as XrdCl
    closes them, and a close that fails fails the copy: XrdCl does not
    count a transfer whose ends could not be let go of cleanly as done.

    ``cancel`` - a :class:`threading.Event`, or a callable answering whether
    to stop - calls the copy off, as XrdCl's progress handler's
    ``ShouldCancel`` does. Asked between the steps of the set-up and
    watched throughout the pull, it stops the wait for the transfer: over
    ``root://`` the destination is sent what XrdCl sends it
    (``File::Fcntl("ofs.tpc cancel")``, which kills the pull), its answer is
    waited for up to :data:`CANCEL_WAIT` seconds - after which the
    connection is shut down under the wait instead - both handles are
    closed, an empty destination is removed (unless ``cleanup=False``), and
    :class:`~xrdclient.errors.AbortedError` is raised. ``progress(done,
    total)`` is told, about once a second (:data:`PROGRESS_EVERY`), how many
    bytes the destination holds, and ``total`` is the source's size when the
    placement learned it.

    ``delegate`` is XrdCl's delegated copy (``tpc.dlgon=1``): the
    destination's login delegates this client's proxy (see
    :attr:`Config.gsi_delegate <xrdclient.Config.gsi_delegate>`), the
    source's CGI travels to the destination as ``tpc.scgi``, and a
    destination that says it can (``tpc tpcdlg`` in its
    ``kXR_Qconfig``) pulls with that credential on its own - *TPC lite*,
    in which the source is never opened with a key by this client, and need
    not be reachable from it at all. ``streams`` is XrdCl's
    ``SubStreamsPerChannel``: the destination is asked for that many
    parallel streams (``tpc.str``).

    ``target_config`` is the destination's settings when they are not the
    source's - another credential for each end, say; ``config`` is then the
    source's alone. ``timeout`` applies to both, and ``tpc_timeout`` is the
    destination's.

    Which end refused a failed ``root://`` copy is :func:`failed_end`
    (``xrdclient.failed_end``).
    Over HTTP, ``delegate``, ``streams``, ``progress`` and ``cleanup`` go to
    :func:`xrdclient.http.third_party`; ``cancel`` is asked at each
    performance marker the destination sends.
    """
    cfg = config or Config()
    su, du = parse(source), parse(target)
    watch = _Watch(_cancel.as_check(cancel), progress)
    options = _Options(
        overwrite=overwrite,
        posc=posc,
        coerce=coerce,
        token_mode=token_mode,
        delegate=delegate,
        streams=max(streams, 1),
        target_scheme=du.scheme,
        init_timeout=init_timeout,
        timeout=timeout,
        cleanup=cleanup,
    )
    if su.is_http and du.is_http:
        result = _http_copy(su, du, cfg, options, watch)
    else:
        _require_root_pair(su, du)
        cfg = _timeout_config(cfg, timeout)
        theirs = _timeout_config(target_config, timeout) if target_config else None
        result = _root_transfer(su, du, cfg, dataclasses.replace(options, target=theirs), watch)
    try:
        return _verified(result, _Ends(su, du), cfg, verify, algorithm)
    except ChecksumMismatchError:
        # This copy wrote the destination, and it is wrong: removed, as the
        # destination's own settings allow (its credential, not the source's).
        if cleanup:
            _remove_quietly(du, target_config or cfg)
        raise


def _root_transfer(
    su: XRootDURL, du: XRootDURL, cfg: Config, options: _Options, watch: _Watch
) -> CopyResult:
    """The ``root://`` rendezvous; an empty destination it made and left on failure is removed.

    Only a destination whose open succeeded is this copy's (see
    :class:`_Target`): one refused at the open - already there, say, with
    ``overwrite=False`` (``kXR_ItExists``, 3018) - was somebody else's,
    empty or not. The removal goes to the destination with the
    destination's settings, never the source's - and not at all with
    ``options.cleanup`` off.
    """
    target = _Target.of(du, options.target or cfg, options.timeout)
    try:
        return _root_copy(su, target, cfg, options, watch)
    except OSError:
        if target.made and options.cleanup:
            discard_if_empty(du, target.config)
        raise


def _verified(
    result: CopyResult, ends: _Ends, cfg: Config, verify: bool | None, algorithm: str | None
) -> CopyResult:
    """``result``, once both servers agree on the file - see :func:`third_party`.

    ``ends`` are the copy's source and destination. A mismatch raises
    :class:`~xrdclient.errors.ChecksumMismatchError`; :func:`third_party`
    then removes the destination, which this copy wrote and which is wrong.
    """
    if verify is False or (verify is None and not cfg.verify_checksums):
        return result
    checksum = _compare_ends(
        ends.source, ends.target, cfg, algorithm or cfg.preferred_checksum, strict=bool(verify)
    )
    return dataclasses.replace(result, checksum=checksum)


def discard_if_empty(url: XRootDURL, config: Config) -> None:
    """After a failed third-party copy: remove the destination if it is empty.

    A destination names the file before it pulls - EOS does, then refuses
    the copy - and an empty file left there looks like a finished copy of an
    empty one. A destination with bytes in it is left alone. Best effort.
    ``config`` must be the destination's own settings, and the caller must
    know the copy made the file: an empty file that was there before is
    not the copy's to remove (see :func:`_root_transfer`).
    """
    try:
        there = _probe(url, config)
        if there is not None and there[0] == 0:
            _remove(url, config)
    except OSError:
        pass


def _remove_quietly(url: XRootDURL, config: Config) -> None:
    """Best effort: a failed removal must not hide the mismatch."""
    with contextlib.suppress(OSError):
        _remove(url, config)


@dataclasses.dataclass
class _Target:
    """The destination of a ``root://`` copy, and what the copy has done to it.

    ``config`` is the destination's own settings - ``target_config``, or
    the copy's when there is none - before the rendezvous adapts them (see
    :func:`_dst_config`); it is what a clean-up after a failure uses.
    ``patience`` is how long the pull may take (see :func:`_patience`), and
    ``made`` turns true once the destination's open has succeeded: from
    then on the file there is this copy's, created or truncated by it, and
    a failure may remove it; before then it is not, whatever is there.
    """

    url: XRootDURL
    config: Config
    patience: float
    made: bool = False

    @classmethod
    def of(cls, url: XRootDURL, config: Config, timeout: float | None) -> _Target:
        """The destination ``url`` with ``config``; ``timeout`` is the copy's, if any."""
        return cls(url, config, _patience(config, timeout))


def _patience(config: Config, timeout: float | None) -> float:
    """How long the pull may take: the copy's ``timeout``, else ``config.tpc_timeout``.

    XrdCl's ``tpcTimeout`` job property, which ``xrdcp`` and the bindings
    fill from ``CPTPCTimeout`` unless the caller names one. ``0`` is no
    bound of its own: the pull is then held to the ordinary limits.
    """
    return config.tpc_timeout if timeout is None else timeout


def _dst_config(target: _Target, delegate: bool) -> Config:
    """The destination connection's settings: delegating, and with room for the pull.

    XrdCl sets ``XrdSecGSIDELEGPROXY`` to match the copy before this
    login, so a delegating copy logs in with ``gsi_delegate``. And the pull
    is one ``kXR_sync`` that answers only when the whole file has moved,
    often through a ``kXR_waitresp`` that promises an hour: the connection
    is silent all that while, which :attr:`~xrdclient.Config.request_timeout`
    (one read), :attr:`~xrdclient.Config.stall_deadline` (one operation) and
    :attr:`~xrdclient.Config.wait_budget` (one deferred answer) would each
    call a failure long before XrdCl's ``CPTPCTimeout`` does. Each is raised
    to the pull's patience - never lowered, and ``0`` (no limit) is kept -
    and the pull itself expires at it (:func:`_sync_within`). A session's
    settings are fixed when it connects, so this is the whole destination
    connection's: its set-up requests are given as long, as XrdCl's own
    ``RequestTimeout`` (1800 s) would, and ``init_timeout`` is what bounds
    them more tightly.
    """
    config = target.config.evolve(gsi_delegate=True) if delegate else target.config
    patience = target.patience
    if patience <= 0:
        return config
    return config.evolve(
        request_timeout=_lifted(config.request_timeout, patience),
        stall_deadline=_lifted(config.stall_deadline, patience),
        wait_budget=_lifted(config.wait_budget, patience),
    )


def _lifted(limit: float, patience: float) -> float:
    """``limit`` raised to ``patience``; a limit of ``0`` (none at all) stays none."""
    return limit if limit <= 0 else max(limit, patience)


@dataclasses.dataclass(frozen=True)
class _Options:
    """How the destination is opened, what the rendezvous carries, and its time limits.

    Built once by :func:`third_party` from its keyword arguments - every
    default here is that function's - and handed down whole, to the
    ``root://`` rendezvous or to :func:`_http_copy`, so neither path re-reads
    the caller's arguments. ``streams`` is already at least one.
    """

    overwrite: bool = True
    posc: bool = True
    coerce: bool = False
    token_mode: str = ""
    delegate: bool = False
    streams: int = 1
    #: The destination's own settings, when they are not the source's.
    target: Config | None = None
    #: The destination URL's scheme, sent as ``tpc.tpr`` (see :func:`_dst_opaque`).
    target_scheme: str = "root"
    #: XrdCl's ``initTimeout``: the bound on everything before the pull.
    init_timeout: float | None = None
    #: The bound on the transfer itself (for ``root://`` already folded into
    #: the configs as ``request_timeout``; HTTP passes it on as it stands).
    timeout: float | None = None
    #: Whether a failed copy's empty destination is removed (gfal2's
    #: ``transfer_cleanup``); see :func:`third_party`.
    cleanup: bool = True


class _Watch:
    """A copy's cancel hook and progress callback, and how it asks them."""

    __slots__ = ("cancel", "progress", "total")

    def __init__(self, cancel: Callable[[], bool] | None, progress: Progress | None) -> None:
        """Hold the caller's hooks; either may be ``None``."""
        self.cancel = cancel
        self.progress = progress
        #: The source's size, once the placement has learned it.
        self.total: int | None = None

    @property
    def active(self) -> bool:
        """Whether anyone is watching: otherwise the pull is one plain wait."""
        return self.cancel is not None or self.progress is not None

    def cancelled(self) -> bool:
        """Whether the caller has asked to stop; a hook that raises has."""
        if self.cancel is None:
            return False
        try:
            return bool(self.cancel())
        except Exception:
            return True

    def check(self) -> None:
        """Raise :class:`~xrdclient.errors.AbortedError` if the caller has asked to stop."""
        if self.cancelled():
            raise AbortedError("the third-party copy was cancelled")

    def report(self, done: int) -> None:
        """Pass ``done`` and the known source size to the progress callback, if any."""
        if self.progress is not None:
            self.progress(done, self.total)

    def http_progress(self) -> Progress | None:
        """What :func:`xrdclient.http.third_party` is given: progress, and the cancel check."""
        if not self.active:
            return None

        def marker(done: int, total: int | None) -> None:
            """Raise if cancelled - the HTTP poller then aborts the ``COPY`` - else report."""
            self.check()
            if self.progress is not None:
                self.progress(done, total)

        return marker


def _starting(init_timeout: float | None, watch: _Watch) -> Callable[[], None]:
    """A check that the copy is still within ``init_timeout`` of starting.

    XrdCl's ``InitTimeoutCalc``: asked after each step of the set-up, and
    ``errOperationExpired`` once the time is gone. A cancelled copy stops
    at the same points.
    """
    started = time.monotonic()

    def check() -> None:
        """Raise if the caller cancelled or the set-up has outrun ``init_timeout``."""
        watch.check()
        if init_timeout and time.monotonic() - started > init_timeout:
            raise CopyTimeoutError("the third-party copy took longer than init_timeout to start")

    return check


def _root_copy(
    su: XRootDURL, target: _Target, cfg: Config, options: _Options, watch: _Watch
) -> CopyResult:
    """The ``XrdOucTPC`` rendezvous, in the order ``XrdCl`` runs it.

    Both routers here are sticky - a redirect moves the router, not just the
    one request - because what the rendezvous needs to know is where each
    end *landed*: the source is told which host will pull (``tpc.dst``) and
    the destination which host to pull from (``tpc.src``), and behind a
    redirector neither is the host named in the URL. The same goes for the
    capability checks, which ask the data servers, not the redirectors.

    ``check`` is asked after each step of the set-up: the caller's cancel,
    and ``options.init_timeout`` counted from here (see :func:`_starting`).
    ``target.made`` is set the moment the destination's open succeeds (see
    :func:`_root_transfer`).
    """
    check = _starting(options.init_timeout, watch)
    key = secrets.token_hex(16)
    started = time.monotonic()
    du = target.url

    src_router = Router(su.with_path("/"), cfg)
    dst_router = Router(du.with_path("/"), _dst_config(target, options.delegate))
    try:
        # 1. Placement: open the source as XrdCl does (``tpc.stage=placement``)
        #    and keep the data server it was redirected to - ``tpcSource``.
        size, capability, placed = _placement(src_router, su, options.delegate)
        watch.total = size if size >= 0 else None
        check()
        origin = _host_id(src_router.url) if placed else _host_id(su)
        result = _open_target(dst_router, du, _dst_opaque(key, su, origin, size, options), options)
        target.made = True
        # The handle exists only on the server that answered the open, and
        # the pinned router takes the connection over: the name it is bound
        # to here is the only one left holding it. That server is also the
        # host that will connect to the source to pull, which is what a stock
        # source checks ``tpc.dst`` against - XrdCl's ``realTarget``, taken
        # from the open's last URL.
        dst_router = dst_router.pin(transfer=True)
        handle, _, _ = rp.parse_open(result.data, du.path)
        lite = _with_handle(
            dst_router,
            handle,
            lambda: _settle(dst_router, src_router, placed, options.delegate, check),
        )
        source = None
        if not lite:
            source = _Keyed(src_router, su, key, dst_router.url.host, options, capability)
        held = _with_handle(
            dst_router, handle, lambda: _rendezvous(dst_router, handle, source, check)
        )
        pull = _Pull(dst_router, handle, du.path, target.patience)
        try:
            _with_handle(dst_router, handle, lambda: _pull(pull, watch))
        except BaseException:
            if held is not None:
                _quietly(src_router, r.Close(held))
            raise
        _close_both((src_router, held), (dst_router, handle))
    finally:
        dst_router.close()
        src_router.close()

    watch.report(max(size, 0))
    return CopyResult(
        source=str(su), target=str(du), size=max(size, 0), seconds=time.monotonic() - started
    )


def _at(end: str, step: Callable[[], T]) -> T:
    """Run one end's step, noting on a failure which end it was (see :func:`failed_end`)."""
    try:
        return step()
    except Exception as exc:
        exc.add_note(_END_NOTE + end)
        raise


def _close_both(source: tuple[Router, bytes | None], target: tuple[Router, bytes]) -> None:
    """After a pull that worked: close the source's handle, then the destination's.

    XrdCl's ``RunTPC``/``RunLite`` close both, in that order, and fail the
    job if either close failed - the source's failure first, since it is
    the one looked at first. Each is named for the end that failed (see
    :func:`failed_end`). XrdCl's own ``UpdateErrMsg`` call there brackets
    the *other* end (``statusS.IsOK() ? "source" : "destination"``) while
    its log line names the right one; that is a slip, and the log's answer
    is the one kept here. A TPC-lite copy holds no source handle
    (``None``), and only the destination's close can fail it.
    """
    (src_router, held), (dst_router, handle) = source, target
    failures = [_closing(dst_router, handle, "destination")]
    if held is not None:
        failures.insert(0, _closing(src_router, held, "source"))
    failure = next((f for f in failures if f is not None), None)
    if failure is not None:
        raise failure


def _closing(router: Router, handle: bytes, end: str) -> Exception | None:
    """Close one end's handle; its failure, noted with ``end``, or ``None``.

    Returned rather than raised, so that the other end is still closed
    before the first failure is raised (see :func:`_close_both`).
    """
    try:
        router.execute(r.Close(handle))
    except Exception as exc:
        exc.add_note(_END_NOTE + end)
        return exc
    return None


def _with_handle(router: Router, handle: bytes, step: Callable[[], T]) -> T:
    """Run ``step``; if it fails, close the destination's handle first, unheard.

    XrdCl closes both ends of a failed job; the close's own outcome is not
    the news, so it is not reported.
    """
    try:
        return step()
    except BaseException:
        _quietly(router, r.Close(handle))
        raise


def _open_target(router: Router, du: XRootDURL, opaque: str, options: _Options) -> Result:
    """Open the destination with ``opaque`` - ``cgiC2Dst`` - added to its own CGI.

    XrdCl's one special case of this open: a destination refusing it with
    ``kXR_FSError`` and "tpc not supported" is a server without third-party
    copy, and says so as XrdCl does, naming no end. Any other failure is
    the destination's (see :func:`failed_end`).
    """
    flags = _dst_flags(options.overwrite, options.posc, coerce=options.coerce)
    try:
        return router.execute(r.Open(_opening(du, opaque), flags, _MODE), path=du.path)
    except ServerError as exc:
        if exc.code == kXR_FSError and "tpc not supported" in exc.message:
            raise UnsupportedError(_UNSUPPORTED, _NO_TPC_AT_DESTINATION) from exc
        exc.add_note(_END_NOTE + "destination")
        raise
    except Exception as exc:
        exc.add_note(_END_NOTE + "destination")
        raise


def _settle(
    dst: Router, src: Router, placed: bool, delegate: bool, check: Callable[[], None]
) -> bool:
    """Between the destination's open and its first sync: lite, classic, or not at all.

    XrdCl's capability checks, in ``CanDo``'s order. Every copy asks the
    destination (``CheckTPCLite``, :func:`_lite`): one without third-party
    copy is refused here rather than left to fail the pull obscurely. A
    delegating copy to a destination that pulls with the delegated
    credential is TPC lite, and needs nothing of the source. Anything else
    is the classic rendezvous, which a source the placement could not open
    cannot have, and which the source's data server must say it supports
    (``CheckTPC``, :func:`_require_tpc`). Each refusal names no end, as
    XrdCl's do not. ``check`` (cancel, ``init_timeout``) is asked between
    the steps, where XrdCl asks its ``InitTimeoutCalc``.
    """
    check()
    if _lite(dst, delegate):
        return True
    if not placed:
        raise UnsupportedError(_UNSUPPORTED, "Destination does not support delegation.")
    check()
    _require_tpc(src)
    check()
    return False


#: XrdCl's words for a destination without third-party copy - full stop included.
_NO_TPC_AT_DESTINATION = "Destination does not support third-party-copy."


def _lite(router: Router, delegate: bool) -> bool:
    """Whether the destination pulls on its own, with the delegated credential (TPC lite).

    XrdCl's ``CheckTPCLite``: the server's ``kXR_Qconfig`` answer to
    ``tpc tpcdlg`` is one line per name, echoing a name it has no value
    for (``XrdXrootdProtocol::do_Qconf``); ``tpc`` must be a number other
    than zero - or the destination is refused - and ``tpcdlg`` set, to the
    protocol whose credentials it forwards, for TPC lite. Asked whether or
    not the copy delegates; only a delegating copy can make use of a yes.
    """
    lines = _config_lines(router, "tpc tpcdlg")
    if not lines or not _atoi(lines[0]):
        raise UnsupportedError(_UNSUPPORTED, _NO_TPC_AT_DESTINATION)
    return delegate and len(lines) > 1 and lines[1] != "tpcdlg"


def _require_tpc(router: Router) -> None:
    """XrdCl's ``CheckTPC`` at the source's data server: ``tpc`` must be a non-zero number.

    A source that does not answer is refused too, as XrdCl refuses it.
    XrdCl also refuses an answer one character long - a bare ``1`` with no
    newline - which no stock server sends; that quirk is not copied, since
    it would only refuse a source that says yes.
    """
    lines = _config_lines(router, "tpc")
    if not lines or not _atoi(lines[0]):
        # No full stop: XrdCl's message for this end has none.
        raise UnsupportedError(_UNSUPPORTED, "Source does not support third-party-copy")


def _config_lines(router: Router, names: str) -> list[str]:
    """The non-empty lines of a ``kXR_Qconfig`` answer, up to its NUL; none if it failed."""
    try:
        answer = router.execute(r.Query(c.kXR_Qconfig, names)).data
    except XRootDError:
        return []
    text = answer.split(b"\x00", 1)[0].decode("utf-8", "replace")
    return [line for line in text.split("\n") if line]


def _atoi(text: str) -> int:
    """C's ``atoi`` behind XrdCl's ``isdigit`` test: the leading digits, or 0 for none."""
    digits = len(text) - len(text.lstrip("0123456789"))
    return int(text[:digits]) if digits else 0


def _dst_flags(overwrite: bool, posc: bool, *, coerce: bool = False) -> int:
    """The destination open's options: create or replace, persist on close, force."""
    flags = OpenFlags.UPDATE | (OpenFlags.DELETE if overwrite else OpenFlags.NEW)
    if posc:
        flags |= OpenFlags.POSC
    if coerce:
        flags |= OpenFlags.FORCE
    return int(flags) | c.kXR_retstat


def _placement(router: Router, source: XRootDURL, delegate: bool) -> tuple[int, str, bool]:
    """:func:`_place_source`, and whether it worked.

    A delegating copy can go ahead without it: a TPC-lite destination opens
    the source itself, with the credential this client delegated, so a
    source this client cannot open is no reason to stop - ``(-1, "", False)``.
    A failure that stops the copy names no end (see :func:`failed_end`):
    XrdCl returns the placement's error as it stands, where it brackets
    the keyed open's as the source's.
    """
    try:
        size, capability = _place_source(router, source)
    except XRootDError:
        if not delegate:
            raise
        return -1, "", False
    return size, capability, True


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
    The open carries the source URL's own CGI too (see :func:`_opening`).
    """
    sent = _opening(source, "tpc.stage=placement")
    request = r.Open(sent, int(OpenFlags.READ) | c.kXR_retstat)
    result = router.execute(request, path=source.path)
    handle, info, _ = rp.parse_open(result.data, source.path)
    _quietly(router, r.Close(handle))
    return (info.st_size if info is not None else -1), _redirect_cgi(sent, request.path)


def _redirect_cgi(sent: str, landed: str) -> str:
    """The CGI a redirect added to an open: the fields of ``landed`` that ``sent`` lacked.

    ``sent`` is the path the open went out with, the URL's own CGI and
    ``tpc.stage`` included; ``landed`` is what the router rewrote it to on
    the way (it appends each hop's token, see
    :func:`xrdclient.session._rewrite._retarget`). What is left is the
    redirect's alone, so the keyed open does not repeat the URL's CGI.
    """
    ours = set(sent.partition("?")[2].split("&"))
    return "&".join(f for f in landed.partition("?")[2].split("&") if f and f not in ours)


@dataclasses.dataclass
class _Keyed:
    """The source's half of a classic rendezvous: its open with the key."""

    router: Router
    url: XRootDURL
    key: str
    puller: str
    options: _Options
    #: The CGI the placement's redirect added - see :func:`_place_source`.
    capability: str = ""

    def open(self) -> bytes:
        """``cgiC2Src``: register the key, naming who will pull; the handle.

        The URL's own CGI first, then the key's fields, then the capability
        the placement's redirect carried (see :func:`_opening`). A failure
        is the source's (see :func:`failed_end`).
        """
        opaque = _src_opaque(self.key, self.puller, self.options.token_mode)
        wire = _opening(self.url, opaque, self.capability)
        path = self.url.path
        result = _at(
            "source",
            lambda: self.router.execute(r.Open(wire, int(OpenFlags.READ)), path=path),
        )
        handle, _, _ = rp.parse_open(result.data, path)
        return handle


def _rendezvous(
    dst_router: Router, handle: bytes, source: _Keyed | None, check: Callable[[], None]
) -> bytes | None:
    """Arm the pull, then register the key at the source; the source's handle.

    Everything before the pull is set-up, and ``check`` is asked after each
    step of it. The source's handle is held until the pull is over, as
    XrdCl holds it - the key it registered lives as long as the open - and
    a TPC-lite copy (``source`` is ``None``) has no source step at all: the
    destination reaches the source on its own.
    """
    _at("destination", lambda: dst_router.execute(r.Sync(handle)))
    check()
    if source is None:
        return None
    src_handle = source.open()
    try:
        check()
    except BaseException:
        _quietly(source.router, r.Close(src_handle))
        raise
    return src_handle


@dataclasses.dataclass(frozen=True)
class _Pull:
    """The pull under way: the destination's pinned router, the handle, and its patience.

    ``patience`` is the seconds the pull may take (:func:`_patience`); ``0``
    leaves it to the connection's ordinary limits.
    """

    router: Router
    handle: bytes
    path: str
    patience: float

    def sync(self) -> None:
        """The second ``kXR_sync``, expiring after ``patience`` - XrdCl's ``Sync(h, tpcTimeout)``.

        The expiry is a :func:`~xrdclient.deadline`, not a lowered socket
        timeout: running out of it raises
        :class:`~xrdclient.session.deadline.OperationExpiredError` (XrdCl's
        ``errOperationExpired``) and costs only the request, so the
        handles can still be closed on the same connection afterwards.
        """
        within = dl.deadline(self.patience) if self.patience > 0 else contextlib.nullcontext()
        with within:
            self.router.execute(r.Sync(self.handle), path=self.path)


def _pull(pull: _Pull, watch: _Watch) -> None:
    """The second ``kXR_sync``: it blocks until the destination has the file.

    With nobody watching it is one plain wait. Otherwise it waits on a
    worker thread - the session carries other requests while one waits, so
    the cancel and the progress questions go out beside it - while this
    thread keeps an eye on the hook and the clock (see :func:`third_party`).
    Either way it is bounded by ``pull.patience`` (see :meth:`_Pull.sync`).
    """
    router, handle, path = pull.router, pull.handle, pull.path
    if not watch.active:
        pull.sync()
        return
    outcome: list[BaseException | None] = []

    def wait() -> None:
        """The worker: block in the pull's ``kXR_sync``; hand back its outcome."""
        try:
            pull.sync()
        except BaseException as exc:  # handed to the watching thread
            outcome.append(exc)
        else:
            outcome.append(None)

    # In the caller's context, so that a deadline() around the copy bounds
    # the wait it is really waiting on.
    context = contextvars.copy_context()
    worker = threading.Thread(target=context.run, args=(wait,), name="xrd-tpc-pull", daemon=True)
    worker.start()
    asked = time.monotonic()
    while worker.is_alive():
        worker.join(_cancel.POLL)
        if worker.is_alive() and watch.cancelled():
            _call_off(router, handle, worker)
            raise AbortedError(f"the third-party copy to {path} was cancelled") from outcome[0]
        if worker.is_alive() and time.monotonic() - asked >= PROGRESS_EVERY:
            asked = time.monotonic()
            _report(router, handle, path, watch)
    failure = outcome[0]
    if failure is not None:
        raise failure


def _call_off(router: Router, handle: bytes, worker: threading.Thread) -> None:
    """Stop a pull under way, as XrdCl does, and wait for its sync to end.

    The destination is sent ``ofs.tpc cancel``, which kills the pull and
    answers the waiting sync - ``ECANCELED``, "destination file prematurely
    closed" - while the fcntl's own answer is ignored, as XrdCl only logs
    it. One that does not answer within :data:`CANCEL_WAIT` has its
    connection shut down under the wait, which ends it either way; the
    handle then goes with the connection.
    """
    signal = threading.Thread(
        target=_quietly,
        args=(router, r.Query(c.kXR_Qopaqug, TPC_CANCEL, fhandle=handle)),
        name="xrd-tpc-cancel",
        daemon=True,
    )
    signal.start()
    worker.join(CANCEL_WAIT)
    if worker.is_alive():
        router.abort()
        worker.join()
    signal.join(CANCEL_WAIT)


def _report(router: Router, handle: bytes, path: str, watch: _Watch) -> None:
    """Tell the progress callback how much the destination holds; quiet if it cannot say."""
    try:
        info = rp.parse_stat(router.execute(r.Stat(fhandle=handle), path=path).data, path)
    except (XRootDError, ValueError):
        return  # busy, or not created yet: the next poll will know
    watch.report(info.st_size)


def _http_copy(
    source: XRootDURL, target: XRootDURL, config: Config, options: _Options, watch: _Watch
) -> CopyResult:
    """The WLCG ``COPY`` dialect, through :func:`xrdclient.http.third_party`.

    Only the options HTTP has a spelling for travel: ``posc``, ``coerce``
    and ``init_timeout`` have no ``COPY`` equivalent and are ignored, as
    they always were, while ``token_mode`` is refused rather than dropped:
    a caller who set it expects a token handling that HTTP spells
    differently, through the ``Credential`` header.
    """
    if options.token_mode:
        raise ValueError(
            "token_mode is a root:// option; HTTP third-party copy delegates "
            "through the Credential header - see xrdclient.http.third_party"
        )
    from ..http import third_party as http_third_party

    return http_third_party(
        source,
        target,
        config=config,
        overwrite=options.overwrite,
        timeout=options.timeout,
        delegate=options.delegate,
        streams=options.streams if options.streams > 1 else None,
        progress=watch.http_progress(),
        cleanup=options.cleanup,
    )


def _require_root_pair(source: XRootDURL, target: XRootDURL) -> None:
    """Refuse a mixed ``root://``/``https://`` pair: neither dialect can broker it.

    An XRootD destination cannot pull over HTTP through ``XrdOucTPC``, and
    a WebDAV ``COPY`` cannot name a ``root://`` peer, so a mixed pair fails
    here with a clear message rather than on the wire with a confusing one.
    """
    if not source.is_root or not target.is_root:
        raise ValueError(
            "third-party copy needs two endpoints of the same kind, not "
            f"{source.scheme}:// and {target.scheme}://"
        )


def _timeout_config(config: Config, timeout: float | None) -> Config:
    """``config`` with ``request_timeout`` raised or lowered to the copy's ``timeout``.

    The pull is one ``kXR_sync`` that answers only when the transfer ends,
    so the per-request timeout *is* the transfer's bound on this path.
    """
    return config.evolve(request_timeout=timeout) if timeout is not None else config


def _quietly(router: Router, request: Request) -> None:
    """Best-effort cleanup: a failed close must not mask the real error."""
    with contextlib.suppress(OSError, XRootDError):
        router.execute(request)
