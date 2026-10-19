"""Redirect following and reconnection.

XRootD's whole federation model is redirection: a manager answers ``kXR_open``
with "go ask this data server", possibly through several tiers. A
:class:`Router` owns whichever :class:`~xrdclient.session.sync.Session` is current,
re-issues requests as it is bounced along, and reconnects a dropped
connection so that an idle handle survives a server restart.

Whether a redirect outlives the request it answered depends on who is asking,
and follows XrdCl. A file's router is *sticky*: after a file is opened on a
data server, its reads must go to that same server, so the router moves there
and :meth:`~Router.pin` hands back one already positioned. A filesystem's is
not: the next path may live on a different data server, so every request
starts again at the manager, and a redirect sends only that one request on,
over a pooled connection of its own. Moving the shared connection instead
would both misroute the next request and close the connection other threads
were in the middle of using.

How a redirect rewrites the request it answered - path fields, tokens and
``tried=`` CGI - is pure string work and lives in
:mod:`xrdclient.session._rewrite`; it is re-exported here, so every name
that was importable from this module still is.

Where to start: :meth:`Router.execute` -> :meth:`Router._run`, whose loop
maps each outcome of one attempt to what XrdCl would do next (follow,
reconnect, go back to the load balancer, or raise). Ownership rules that
must not break: a connection is either the router's alone (``_loan`` is
``None``) or shared through a :class:`_Loan`, and only the last holder pools
or closes it; the collector's finalizer (:func:`_abandoned`) takes no lock
and does no I/O.
"""

from __future__ import annotations

import contextvars
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Unpack, cast

from xrdcore import retry as _retry

from ..config import Config
from ..errors import (
    AbortedError,
    RedirectLimitError,
    ServerError,
    TransientError,
    WaitLimitError,
    kXR_FSError,
    kXR_IOError,
    kXR_NoMemory,
    kXR_NotFound,
    kXR_Overloaded,
    kXR_ServerError,
)
from ..errors import ConnectionError as XrdConnectionError
from ..errors import TimeoutError as XrdTimeoutError
from ..log import get_logger
from ..proto import constants as c
from ..proto import requests as r
from ..proto.frames import Request
from ..proto.responses import RedirectInfo
from ..url import XRootDURL, parse
from . import deadline as dl
from . import local as _local
from ._rewrite import _PATH_FIELDS as _PATH_FIELDS
from ._rewrite import LOST as LOST
from ._rewrite import TRIED_REASONS as TRIED_REASONS
from ._rewrite import PathFields as PathFields
from ._rewrite import _as_url as _as_url
from ._rewrite import _base_path as _base_path
from ._rewrite import _path_fields as _path_fields
from ._rewrite import _redirect_path as _redirect_path
from ._rewrite import _redirect_url as _redirect_url
from ._rewrite import _repath as _repath
from ._rewrite import _retarget as _retarget
from ._rewrite import _with_token as _with_token
from ._rewrite import _without_tried as _without_tried
from ._rewrite import tried as tried
from ._rewrite import with_tried as with_tried
from .pool import SESSIONS, same_server
from .sync import ExecuteOptions, RedirectRequired, Result, Session

__all__ = ["Hop", "Router", "trace"]

_log = get_logger(__name__)

#: Errors from a data server that XrdCl takes back to the load balancer
#: rather than to the caller (``RetriableErrorResponse``): the manager may
#: well know another server that can do better.
_RETRY_AT_BALANCER = frozenset(
    {kXR_FSError, kXR_IOError, kXR_ServerError, kXR_NotFound, kXR_Overloaded, kXR_NoMemory}
)

#: Of those, the ones after which an open asks the manager to refresh its
#: idea of where the file is (``kXR_refresh``), as XrdCl's
#: ``SwitchOnRefreshFlag`` does.
_REFRESH = frozenset({kXR_NotFound, kXR_Overloaded})


@dataclass(slots=True)
class Hop:
    """One server a request passed through, as XrdCl's ``HostInfo`` records it.

    The first hop is where the router sends everything; each redirect adds
    the server it pointed at. ``flags`` and ``version`` are the server's
    ``kXR_protocol`` flags and handshake protocol version, filled in once it
    has answered the request - they stay 0 for a server that never did.
    """

    #: The server, as a URL: the router's own for the first hop, and for a
    #: redirect the router's URL moved to the host and port it named.
    url: XRootDURL
    #: For a hop a redirect led to, the path the request named there (CGI
    #: off) and the CGI token the redirect carried.
    path: str = ""
    token: str = ""
    flags: int = 0
    version: int = 0
    #: Whether this server is the request's load balancer: the manager -
    #: or, superseding it, the meta-manager - that redirected it on.
    load_balancer: bool = False
    #: Whether a redirect led here, rather than this being where it started.
    redirected: bool = False

    @property
    def endpoint(self) -> str:
        """``host:port`` of this hop, as XrdCl's ``HostInfo`` prints it."""
        return f"{self.url.host}:{self.url.port}"


#: The hop list :func:`trace` is filling in this context, if any.
_trace: contextvars.ContextVar[list[Hop] | None] = contextvars.ContextVar(
    "xrdclient_route_trace", default=None
)


@contextmanager
def trace() -> Iterator[list[Hop]]:
    """Record the servers each request made inside the block passes through.

    The list yielded holds the hops of the most recent request, replaced as
    each new one starts, so after a single call it describes that call - the
    manager it asked, the data server it was sent to - the way XrdCl's
    ``HostList`` does for a callback. A block inside another shares its list,
    so the outer one still sees the last request made anywhere inside it.
    """
    hops = _trace.get()
    if hops is not None:
        yield hops
        return
    hops = []
    token = _trace.set(hops)
    try:
        yield hops
    finally:
        _trace.reset(token)


def _start_trace(url: XRootDURL) -> list[Hop] | None:
    """Reset an active :func:`trace` to one hop at ``url``; ``None`` if none is active.

    In place, so the list a ``with trace() as hops`` holds is the one updated.
    """
    hops = _trace.get()
    if hops is not None:
        hops[:] = [Hop(url)]
    return hops


def _mark_balancer(hops: list[Hop]) -> None:
    """Make the hop that just redirected the load balancer, if XrdCl would.

    A manager becomes the request's load balancer when it is the first one
    met; a meta-manager supersedes any earlier one.
    """
    hop = hops[-1]
    if not hop.flags & c.kXR_isManager:
        return
    if hop.flags & c.kXR_attrMeta or not any(h.load_balancer for h in hops):
        for each in hops:
            each.load_balancer = False
        hop.load_balancer = True


def _hop(hops: list[Hop] | None, destination: XRootDURL, path: str, token: str) -> None:
    """Record the server a redirect sent a traced request to, naming ``path`` there."""
    if hops is None:
        return
    _mark_balancer(hops)
    last = hops[-1].url
    if (destination.host, destination.port) == (last.host, last.port):
        return  # XrdCl adds a host only when the redirect changes where it is
    hops.append(Hop(destination, path, token, redirected=True))


@dataclass(slots=True)
class _Route:
    """Where one request has been sent, and how often."""

    #: The request's path fields before any redirect touched them, taken at
    #: the first redirect - nothing changes them before one, and most
    #: requests never meet one, so most never pay for the snapshot.
    original: PathFields | None
    #: The server a redirect sent this request to, or ``None`` for the
    #: router's own. Only ever set on the request, never on the router, when
    #: the router is not sticky.
    target: XRootDURL | None = None
    #: The connection the last attempt went out on.
    session: Session | None = None
    hops: int = 0
    attempts: int = 0
    #: The servers this request has been through, when a :func:`trace` asked.
    trace: list[Hop] | None = None
    #: Where the request started: the router's URL before any redirect moved
    #: it, and so where it goes back to when a data server fails it.
    home: XRootDURL | None = None
    #: Whether a redirect moved this (sticky) router during this request -
    #: away from :attr:`home`, which it returns to if the new server fails.
    moved: bool = False
    #: Whether a manager - a server flagging ``kXR_isManager`` - redirected
    #: this request: XrdCl's "there is a load balancer", the condition for
    #: taking a data server's error back to it rather than to the caller.
    balancer: bool = False
    #: Data servers that failed this request, for the ``tried=`` CGI.
    tried: list[str] = field(default_factory=list)


class _Loan:
    """A connection more than one router is using at once.

    What a :class:`~xrdclient.client.FileSystem` and the files it opened
    share when an open was not redirected. Whichever of them lets go last
    puts it back in the pool - or closes it, if any of them saw it fail - so
    the filesystem closing first neither pools a connection a file is still
    reading from, nor closes it under the file.
    """

    __slots__ = ("_lock", "config", "holders", "session", "spoiled", "url")

    def __init__(self, session: Session, url: XRootDURL, config: Config) -> None:
        """Share ``session`` with its current user as the first holder."""
        self.session = session
        #: Where, and as whom, the connection goes back to the pool.
        self.url = url
        self.config = config
        self.holders = 1
        #: Whether a holder let go of it because it failed or misbehaved.
        self.spoiled = False
        self._lock = threading.Lock()

    def join(self) -> _Loan:
        """Add a holder; returns ``self`` so a share reads as one expression."""
        with self._lock:
            self.holders += 1
        return self

    def leave(self, *, spoiled: bool = False) -> None:
        """Drop a holder; the last one out pools the connection, or closes it.

        ``spoiled`` is sticky: one holder that saw the connection fail is
        enough for the last to close it rather than pool it. The pool call and
        the close happen outside the loan's lock, since either may block.
        """
        with self._lock:
            self.holders -= 1
            self.spoiled = self.spoiled or spoiled
            last = self.holders == 0
        if not last:
            return
        if self.spoiled or not SESSIONS.release(self.session, self.url, self.config):
            self.session.close()


class _Held:
    """What a :class:`Router` must give back when it is finished: its connection.

    Kept apart from the router so that a :func:`weakref.finalize` can reach it
    without keeping the router alive. The router reads and writes these
    through properties of its own; this object is all the finalizer holds.
    """

    __slots__ = ("config", "loan", "session", "url")

    def __init__(self, url: XRootDURL, config: Config) -> None:
        """Nothing held yet: the router dials on first use."""
        self.session: Session | None = None
        self.loan: _Loan | None = None
        self.url = url
        self.config = config


def _abandoned(held: _Held) -> None:
    """A router was collected without ``close``: queue its connection for release.

    This runs inside the garbage collector, on whichever thread allocated at
    the wrong moment - one that may hold the pool's lock, or this router's,
    or be half-way through a request on the very connection being let go.
    So it takes no lock and does no I/O: the connection is queued with the
    pool (:meth:`~xrdclient.session.pool.SessionPool.adopt`), which pools or
    closes it on an ordinary thread soon after.
    """
    session, loan = held.session, held.loan
    held.session, held.loan = None, None
    if session is not None:
        SESSIONS.adopt(session, loan, held.url, held.config)


class Router:
    """A connection that knows how to move."""

    def __init__(
        self,
        url: str | XRootDURL,
        config: Config | None = None,
        *,
        reconnect: bool = True,
        sticky: bool = True,
    ) -> None:
        """A router for ``url``; nothing is dialled until the first request.

        ``reconnect`` allows a dropped connection to be replaced and an
        idempotent request retried (``connect_retries`` times); ``sticky``
        lets a redirect move the router itself. See the attributes below.
        """
        #: The connection, its loan, the URL and the configuration, in the one
        #: object the collector's finalizer may hold - see :func:`_abandoned`.
        self._held = _Held(parse(url) if isinstance(url, str) else url, config or Config())
        # Give the connection back even when nobody said ``close``: a
        # one-liner - ``xrdclient.read_text(url)``, or a path used and
        # dropped - should not cost a socket for the rest of the process.
        weakref.finalize(self, _abandoned, self._held)
        #: Whether a dropped connection may be replaced under a live request.
        #: False on a pinned router, because the file handle its caller holds
        #: exists only on the connection that issued it: reconnecting would
        #: turn "the server went away" into "invalid file handle", five frames
        #: further on and much harder to act on.
        self.reconnect = reconnect
        #: Whether a redirect moves the router (a file) or only the request
        #: that got it (a filesystem, which asks its manager every time).
        self.sticky = sticky
        #: Whether redirects are followed at all. When they are not, a
        #: redirect ends the request: :meth:`execute` raises the
        #: :class:`~xrdclient.session.sync.RedirectRequired` it got, naming
        #: where the server said to go - XrdCl's ``FollowRedirects=false``.
        self.follow_redirects = True
        #: The router a :meth:`lend` borrows its first connection from.
        self._lender: Router | None = None
        #: Whether this router was lent for one open, so that pinning it hands
        #: its hold on the connection over rather than sharing it; see
        #: :meth:`pin`.
        self._lent = False
        self._lock = threading.RLock()
        #: Set by :meth:`abort`, never cleared: an aborted router carries
        #: nothing more, and everything asked of it raises
        #: :class:`~xrdclient.errors.AbortedError`.
        self._aborted = threading.Event()
        #: The pooled connections redirected requests are out on right now,
        #: which are not :attr:`_session` and so need naming for :meth:`abort`
        #: to reach them.
        self._borrowed: set[Session] = set()

    # ------------------------------------------------------------------

    @property
    def url(self) -> XRootDURL:
        """Where this router sends requests: its first server, or where it moved."""
        return self._held.url

    @url.setter
    def url(self, value: XRootDURL) -> None:
        """Repoint the router; the finalizer sees it too, so an orphan pools correctly."""
        self._held.url = value

    @property
    def config(self) -> Config:
        """The settings this router connects and retries with."""
        return self._held.config

    @config.setter
    def config(self, value: Config) -> None:
        """Replace the settings; the next dial and pool release use them."""
        self._held.config = value

    @property
    def _session(self) -> Session | None:
        """The current connection, kept on :class:`_Held` for the finalizer's sake."""
        return self._held.session

    @_session.setter
    def _session(self, value: Session | None) -> None:
        self._held.session = value

    @property
    def _loan(self) -> _Loan | None:
        """Set while the connection is shared with other routers - a lender
        and the files it lent to - and ``None`` while this router is its only
        user, free to pool or close it alone."""
        return self._held.loan

    @_loan.setter
    def _loan(self, value: _Loan | None) -> None:
        self._held.loan = value

    @property
    def session(self) -> Session:
        """The live session, connecting on first use.

        Raises :class:`~xrdclient.errors.AbortedError` once :meth:`abort`
        has been called.
        """
        if self._aborted.is_set():
            raise AbortedError(f"the connection to {self.endpoint} was aborted")
        session = self._session
        if session is not None and not session.closed:
            # The steady state, answered without the lock: reading one
            # reference is atomic, and a thread replacing it meanwhile is a
            # race the locked read would have lost the same way.
            return session
        with self._lock:
            return self._live_session()

    def _live_session(self) -> Session:
        """:attr:`session`'s slow path: reconnect if allowed. Called with the lock held."""
        if self._session is not None and self._session.closed and not self.reconnect:
            # Silently opening a replacement would hand the caller a live
            # connection on which its file handle does not exist, and the
            # server would answer "file is not open" - true, useless, and
            # three layers away from the cause.
            raise XrdConnectionError(f"the connection to {self.endpoint} was lost")
        if self._session is None or self._session.closed:
            self._connect()
        assert self._session is not None
        if self._aborted.is_set():
            # Aborted while the dial was under way, which nothing could
            # interrupt: the fresh connection goes the way of the old.
            self._drop()
            raise AbortedError(f"the connection to {self.endpoint} was aborted")
        return self._session

    def _connect(self) -> None:
        """Replace a missing or dead session. Called with the lock held."""
        if self._session is not None:
            # A session whose peer went away is closed as far as the
            # protocol goes, but its socket is still a descriptor.
            self._let_go(self._session, self._loan, self.url, spoiled=True)
            self._session, self._loan = None, None
        lender, self._lender = self._lender, None
        if lender is not None:
            # Borrowed, not dialled: the open this router was lent for starts
            # on the connection its lender already has.
            self._loan = lender._share()
            self._session = self._loan.session
            return
        self._session = self._dial(self.url)

    def _hold(self, session: Session) -> _Loan:
        """This router's share of ``session``, its current connection.

        Called with the lock held. A connection this router had to itself
        becomes a shared one, with this router as its first holder.
        """
        if self._loan is None:
            self._loan = _Loan(session, self.url, self.config)
        return self._loan

    def _share(self) -> _Loan:
        """A new holder's share of this router's connection, connecting first."""
        with self._lock:
            return self._hold(self.session).join()

    def _let_go(
        self, session: Session, loan: _Loan | None, url: XRootDURL, *, spoiled: bool = False
    ) -> None:
        """Finish with ``session``: leave its loan, else close or pool it."""
        if loan is not None:
            loan.leave(spoiled=spoiled)
        elif spoiled:
            session.close()
        else:
            self._offer(session, url)

    def _dial(self, url: XRootDURL) -> Session:
        """A pooled connection to ``url`` as this config's identity, else a new one."""
        return SESSIONS.acquire(url, self.config) or SESSIONS.connect(url, self.config)

    def _offer(self, session: Session, url: XRootDURL) -> None:
        """Pool a connection this router is finished with, or close it."""
        if not SESSIONS.release(session, url, self.config):
            session.close()

    @property
    def endpoint(self) -> str:
        """``host:port`` the router currently points at (after a sticky move)."""
        return f"{self.url.host}:{self.url.port}"

    @property
    def connected(self) -> bool:
        """Whether a live connection is held now; ``False`` before the first request."""
        return self._session is not None and not self._session.closed

    def bind_data_path(self) -> int:
        """Bind a second connection to the current session for bulk data.

        Not retried and not followed across a redirect: a path id belongs to
        one session on one server, so a caller that loses the connection must
        ask the new one for a new path rather than be handed a stale number.
        """
        return self.session.bind_data_path()

    def execute(
        self, request: Request, *, path: str = "", **kwargs: Unpack[ExecuteOptions]
    ) -> Result:
        """Run ``request``, following redirects and retrying dropped connections.

        Within a :func:`~xrdclient.deadline`, the request expires where it
        stands when the time is up: nothing further is sent for it - no
        retry, no redirect followed - and
        :class:`~xrdclient.session.deadline.OperationExpiredError` is raised.
        """
        route = _Route(None, trace=_start_trace(self.url), home=self.url)
        try:
            return self._run(request, route, path, **kwargs)
        except AbortedError:
            raise
        except Exception as exc:
            # Whatever the abort woke this request with - a closed link, a
            # retry that found no connection - the caller asked for it.
            if self._aborted.is_set():
                raise AbortedError(
                    f"{type(request).__name__} on {self.endpoint} was aborted"
                ) from exc
            raise

    def _run(
        self, request: Request, route: _Route, path: str, **kwargs: Unpack[ExecuteOptions]
    ) -> Result:
        """:meth:`execute`'s loop: one attempt, and what each outcome calls for."""
        while True:
            self._may_send(request)
            try:
                result = self._attempt(request, route, path=path, **kwargs)
            except RedirectRequired as redirect:
                self._answered(route)
                served = self._follow(request, path, route, redirect)
                if served is not None:
                    return served
            except (WaitLimitError, dl.OperationExpiredError):
                # A busy server is not a broken connection: the budget for
                # "come back later" has already been spent once here, and
                # reconnecting would only spend it again on a new socket. An
                # expired request is over, by definition.
                raise
            except XrdConnectionError as exc:
                # Before the server's refusals: one saying it timed out is
                # both, and is retried as it always has been.
                self._recover(request, route, exc)
            except ServerError as exc:
                self._answered(route)
                if not self._retry_at_balancer(request, route, exc):
                    raise
            else:
                self._answered(route)
                return result

    def _may_send(self, request: Request) -> None:
        """Raise if ``request`` must not go out again: out of time, or aborted."""
        dl.check(type(request).__name__)
        if self._aborted.is_set():
            raise AbortedError(f"{type(request).__name__} on {self.endpoint} was aborted")

    @staticmethod
    def _answered(route: _Route) -> None:
        """Note, on a traced request's current hop, who answered it there."""
        hops, session = route.trace, route.session
        if not hops or session is None:
            return
        hop = hops[-1]
        hop.flags = getattr(session.protocol, "flags", 0)
        hop.version = session.handshake_version

    def _attempt(
        self, request: Request, route: _Route, *, path: str, **kwargs: Unpack[ExecuteOptions]
    ) -> Result:
        """Send ``request`` once, to wherever ``route`` says it is going."""
        target = route.target
        session = route.session = self.session if target is None else self._dial(target)
        if target is None:
            return session.execute(request, path=path, **kwargs)
        # A redirected request's own connection: borrowed from the pool for
        # this one attempt and given back straight after, so that the next
        # request redirected to the same data server finds it waiting.
        with self._lock:
            self._borrowed.add(session)
        try:
            return session.execute(request, path=path, **kwargs)
        except (WaitLimitError, dl.OperationExpiredError):
            raise
        except XrdConnectionError:
            # Failed under a live request: whoever picks it up next deserves
            # better than a connection with an unknown reply still owed on it.
            session.close()
            raise
        finally:
            with self._lock:
                self._borrowed.discard(session)
            self._offer(session, target)

    def _follow(
        self, request: Request, path: str, route: _Route, redirect: RedirectRequired
    ) -> Result | None:
        """Act on a redirect: raise it, follow it, or serve it from local disk.

        Returns the request's answer only when a ``file://`` redirect was
        answered on the spot (see :meth:`_go_local`); otherwise the request
        has been pointed somewhere new and the caller sends it again.
        """
        if not self.follow_redirects:
            raise redirect
        target = redirect.target
        local = _local.target_path(target.host) if target.port < 0 else None
        if local is None:
            self._redirect(request, path, route, redirect)
            return None
        return self._go_local(request, route, target.host, local)

    def _go_local(self, request: Request, route: _Route, url: str, path: str) -> Result | None:
        """Follow a redirect to ``file://`` by reading ``path`` from this machine's disk.

        Allowed or refused by :attr:`Config.local_redirect
        <xrdclient.Config.local_redirect>`, judged against the server that
        sent it (:func:`xrdclient.session.local.check_follow`). A sticky
        router - a file's - then has a :class:`~xrdclient.session.local.LocalFile`
        for its connection, which answers the open and every read after it;
        the connection to the server is let go as a redirect elsewhere would
        let it go. Any other router answers just this one request locally.
        """
        transport = getattr(route.session, "transport", None)
        _local.check_follow(url, self.config, bool(getattr(transport, "peer_is_local", False)))
        local = _local.LocalFile(path, self.config)
        _log.debug("following the redirect to %s from local disk", url)
        if self.sticky:
            with self._lock:
                leaving, loan, origin = self._session, self._loan, self.url
                if leaving is route.session:
                    where = XRootDURL(scheme="file", host="localhost", port=0, path=path)
                    # Not a Session, but it answers every request a file
                    # sends the way one would; see xrdclient.session.local.
                    self._session, self._loan, self.url = cast("Session", local), None, where
            if leaving is route.session:
                if leaving is not None:
                    self._let_go(leaving, loan, origin)
                return None
        try:
            return local.execute(request)
        finally:
            local.close()

    def _redirect(
        self, request: Request, path: str, route: _Route, redirect: RedirectRequired
    ) -> None:
        """Point ``request`` at the server a ``kXR_redirect`` named.

        Counts the hop against ``redirect_limit`` (XrdCl's
        ``RedirectLimit``), moves the route or the router, and rewrites the
        request's path fields: a redirect may name a new path, and its opaque
        CGI (often an authorisation token for the data server) goes on every
        path field the request sends. The pre-redirect fields are snapshotted
        once, so each later hop rewrites from the original rather than
        stacking stale tokens from earlier hops (see :func:`_retarget`).
        """
        route.hops += 1
        if route.hops > self.config.redirect_limit:
            raise RedirectLimitError(
                f"more than {self.config.redirect_limit} redirects for "
                f"{type(request).__name__} {path}"
            ) from redirect
        destination = self._destination(route.target or self.url, redirect.target)
        self._point(route, destination)
        if route.original is None:
            route.original = _path_fields(request)
        path = _redirect_path(redirect.target)
        if path:
            # Into the snapshot, not only the request: every later hop, and a
            # retry at the manager, builds on the path this one moved it to.
            _repath(route.original, path)
        _retarget(request, route.original, redirect.target.token)
        _hop(route.trace, destination, _base_path(route.original), redirect.target.token)
        _log.debug("redirected to %s:%s", destination.host, destination.port)

    def _point(self, route: _Route, destination: XRootDURL) -> None:
        """Aim ``route`` - or, for a sticky router, the router - at ``destination``.

        Also notes whether the server that redirected is a manager, which
        is what makes it the load balancer a failure is taken back to.
        """
        if getattr(getattr(route.session, "protocol", None), "flags", 0) & c.kXR_isManager:
            route.balancer = True
        moved = self.sticky and route.target is None and self._move(route.session, destination)
        if moved:
            assert route.home is not None  # set by execute, the only caller
            route.moved = route.moved or not same_server(destination, route.home, self.config)
            return
        home = same_server(destination, self.url, self.config)
        route.target = None if home else destination

    @staticmethod
    def _destination(base: XRootDURL, target: RedirectInfo) -> XRootDURL:
        """The URL a redirect points at, keeping what it does not change.

        A port of zero means "the port you already had"; a negative one means
        the host field is a URL (see :func:`_redirect_url`). It does not mean
        "use TLS": a TLS upgrade is negotiated in ``kXR_protocol``, and XRootD
        spends the rest of a negative port on redirect flags.
        """
        if target.port < 0:
            return _redirect_url(base, target.host)
        return base.evolve(host=target.host, port=target.port or base.port)

    def _move(self, came_from: Session | None, destination: XRootDURL) -> bool:
        """Move a sticky router to ``destination``, if it is still where it was.

        ``False`` when another thread has moved it since this request went
        out: the router is now wherever that thread's redirect sent it, and
        this request follows its own redirect on a connection of its own
        rather than dragging the router - and the other thread's live
        connection - somewhere else.
        """
        with self._lock:
            if self._session is not came_from:
                return False
            if same_server(destination, self.url, self.config):
                return True
            leaving, loan, origin = self._session, self._loan, self.url
            self._session, self._loan, self.url = None, None, destination
        # The connection being left behind is not broken - it is simply not
        # the server holding the file - so it goes back to the pool, keyed by
        # the endpoint it is still connected to. The next open asks the same
        # manager the same question, and finds it already answered once.
        # Never ``None``: the request that brought the redirect went out on it.
        assert leaving is not None
        self._let_go(leaving, loan, origin)
        return True

    def _recover(self, request: Request, route: _Route, error: XrdConnectionError) -> None:
        """Get ready to resend ``request`` after its connection failed, or raise.

        Only an idempotent request is resent, at most ``connect_retries``
        times, and only while the deadline allows: a lost ``kXR_write`` may or
        may not have been applied, and repeating it is not this layer's call.
        A redirected request goes back to its manager with ``tried=`` naming
        the lost server; one at home just drops the dead connection so the
        next attempt dials afresh. Either way there is a backoff first.
        """
        left = dl.remaining()
        if left is not None and left <= 0:
            raise dl.OperationExpiredError(
                f"{type(request).__name__} expired before it could be retried: {error}"
            ) from error
        route.attempts += 1
        where = route.target or self.url
        if not self._retryable(request, route.attempts):
            # A timeout stays a timeout: "it was slow" and "it bounced" call
            # for different things from the caller.
            kind = XrdTimeoutError if isinstance(error, XrdTimeoutError) else TransientError
            raise kind(
                f"{type(request).__name__} on {where.host}:{where.port} failed: {error}",
                attempts=route.attempts,
            ) from error
        _log.debug("reconnecting to %s after %s", self.endpoint, error)
        if route.target is not None or route.moved:
            # A data server that has gone away is the least likely one to
            # answer the retry; its manager can route around it, as XrdCl's
            # does - told which server failed, so that it does - and hands
            # out a fresh token with the next redirect.
            self._go_home(request, route, LOST, spoiled=True)
        else:
            self._drop(route.session)
        self._pause(route.attempts)

    def _go_home(self, request: Request, route: _Route, reason: str, *, spoiled: bool) -> None:
        """Send ``request`` back to where it started, naming the server that failed it.

        The ``tried=`` list accumulates over the request's whole life, so a
        manager asked a third time hears about both servers that failed, not
        only the last. A sticky router the failed redirect had moved is moved
        back too, letting go of its connection there - closed if it failed
        (``spoiled``), pooled if it merely answered with an error.
        """
        failed = route.target or self.url
        route.tried = tried(route.tried, failed.host)
        if route.target is not None:
            route.target = None
        else:
            self._return_home(route, failed, spoiled=spoiled)
        # Only a redirect sends a request away from home, and the first one
        # took the snapshot.
        assert route.original is not None
        for name, value in route.original.items():
            if isinstance(value, str):
                setattr(request, name, with_tried(value, route.tried, reason))
            else:
                setattr(request, name, [with_tried(p, route.tried, reason) for p in value])
        if route.trace is not None:
            route.trace.append(Hop(self.url))

    def _return_home(self, route: _Route, failed: XRootDURL, *, spoiled: bool) -> None:
        """Move a sticky router back to :attr:`_Route.home` from ``failed``.

        Its connection there may be live (the server answered with an error),
        dead, or never made (the connect itself failed).
        """
        assert route.home is not None  # set by execute, the only caller
        with self._lock:
            leaving, loan, origin = self._session, self._loan, self.url
            if origin is not failed:
                # Another thread has moved it since; leave it where it is.
                return
            self._session, self._loan, self.url = None, None, route.home
        route.moved = False
        if leaving is not None:
            self._let_go(leaving, loan, origin, spoiled=spoiled)

    def _balancer_may_retry(self, request: Request, route: _Route, error: ServerError) -> bool:
        """Every condition :meth:`_retry_at_balancer` needs, checked together."""
        conditions = (
            route.target is not None or route.moved,
            route.balancer,
            error.code in _RETRY_AT_BALANCER,
            request.idempotent,
            route.hops < self.config.redirect_limit,
        )
        return all(conditions)

    def _retry_at_balancer(self, request: Request, route: _Route, error: ServerError) -> bool:
        """Take a data server's refusal back to the manager, as XrdCl does.

        Only for a request a manager sent on (:attr:`_Route.balancer`), with
        an error another server might not have (:data:`_RETRY_AT_BALANCER`),
        that is safe to repeat, and while the redirect limit allows another
        hop - the manager's next redirect is one more, so a file that is
        nowhere still ends, with the server's own error rather than the
        limit's. ``tried=`` and ``triedrc=`` tell the manager whom to avoid
        and why; a missing file also asks it to refresh its location cache.
        """
        if not self._balancer_may_retry(request, route, error):
            return False
        _log.debug("taking %s from %s back to its manager", error, (route.target or self.url).host)
        self._go_home(request, route, TRIED_REASONS.get(error.code, ""), spoiled=False)
        if error.code in _REFRESH and isinstance(request, r.Open):
            request.options |= c.kXR_refresh
        return True

    def _retryable(self, request: Request, attempts: int) -> bool:
        """Whether attempt number ``attempts`` may still be made for ``request``."""
        return self.reconnect and attempts <= self.config.connect_retries and request.idempotent

    def _pause(self, attempts: int) -> None:
        """Wait before retrying: exponential, jittered, cut to the deadline.

        Reconnecting three times in as many microseconds only ever finds the
        server still down; a restarting daemon needs a second or two, and the
        wait costs nothing when the connection comes back on the first try.
        The jitter (see :mod:`xrdcore.retry`) keeps every client that lost
        the same server from coming back to it in lockstep.
        """
        config = self.config
        _retry.pause(attempts, config.retry_backoff, config.wait_cap, remaining=dl.remaining())

    def _drop(self, failed: Session | None = None) -> None:
        """Let go of the current connection after it failed.

        Only if it is still ``failed``: another thread may have replaced it
        already, and closing its fresh connection would turn one failure into
        two. A shared connection is not closed under the routers still using
        it: it is marked, and the last of them closes it instead of pooling it.
        """
        with self._lock:
            session, loan = self._session, self._loan
            if session is None or (failed is not None and session is not failed):
                return
            self._session, self._loan = None, None
        self._let_go(session, loan, self.url, spoiled=True)

    def discard(self) -> None:
        """Close this connection for good, keeping it out of the pool.

        For one that has just misbehaved: pooling a connection whose server
        has gone away only moves the failure to whoever picks it up next.
        """
        self._drop()

    def lend(self) -> Router:
        """A sticky router for one open, starting on this router's connection.

        What a :class:`~xrdclient.client.FileSystem` gives the files it opens:
        the open goes out on the filesystem's connection when it is not
        redirected, and when it is, the lent router moves to the data server
        on a connection of its own - leaving the filesystem where it was.
        """
        lent = Router(self.url, self.config)
        lent._lender, lent._lent = self, True
        lent.follow_redirects = self.follow_redirects
        return lent

    def pin(self, *, transfer: bool = False) -> Router:
        """A router bound to the current endpoint, sharing this connection.

        Used after an open: further operations on the handle must not be
        re-routed, because the handle only exists on this server — nor
        silently reconnected, for the same reason. A pinned router therefore
        reports a lost connection as a :class:`~xrdclient.errors.TransientError` and
        leaves the recovery to :class:`~xrdclient.client.file.File`, which is the
        only layer that knows how to get the handle back.

        ``transfer`` hands this router's hold on the connection over rather
        than sharing it: the caller is done with this router, and the pinned
        one takes its place - the single owner, if this router was. Without
        it the two share the connection, and whichever lets go last returns
        it to the pool. A :meth:`lend`-ed router always hands over: it was
        made for the one open, and its caller keeps only the pinned router.
        """
        pinned = Router(self.url, self.config, reconnect=False)
        pinned.follow_redirects = self.follow_redirects
        with self._lock:
            session = self._session
            if session is None:
                return pinned
            if transfer or self._lent:
                pinned._loan = self._loan
                self._session, self._loan = None, None
            else:
                pinned._loan = self._hold(session).join()
            pinned._session = session
        return pinned

    @property
    def aborted(self) -> bool:
        """Whether :meth:`abort` has been called on this router."""
        return self._aborted.is_set()

    def abort(self) -> None:
        """Stop everything on this router now, and refuse anything after.

        Thread-safe and idempotent, and never blocks on the network: every
        connection a request of this router is out on - its own, and the
        pooled ones redirected requests borrowed - is marked broken and shut
        down, which wakes a thread blocked on it at once (see
        :meth:`Session.close <xrdclient.session.sync.Session.close>`), and
        the request it was waiting on fails with
        :class:`~xrdclient.errors.AbortedError`. So does every later call.
        Nothing is reconnected, and nothing goes back to the pool.

        The connection is the unit an abort can reach: a connection this
        router shares with others - a filesystem and the files it opened -
        is shut down under them too, and they meet it as a dropped
        connection. A dial in progress cannot be interrupted; its connection
        is dropped as soon as it completes.
        """
        with self._lock:
            self._aborted.set()
            self.reconnect = False
            session, loan = self._session, self._loan
            self._session, self._loan = None, None
            borrowed = list(self._borrowed)
        for link in (*borrowed, *([session] if session is not None else [])):
            link.mark_broken()
            link.close()
        if session is not None:
            self._let_go(session, loan, self.url, spoiled=True)

    def close(self) -> None:
        """Finish with this connection, offering it to the pool if it is ours.

        One shared with other routers stays open until the last of them has
        finished with it: a filesystem closed before a file it opened must not
        pool, or close, the connection that file is still using.
        """
        with self._lock:
            session, loan = self._session, self._loan
            self._session, self._loan = None, None
        if session is not None:
            self._let_go(session, loan, self.url)

    def __enter__(self) -> Router:
        """Use the router as a context manager; :meth:`close` on exit."""
        return self

    def __exit__(self, *exc: object) -> None:
        """:meth:`close` - pooling the connection, unless it is shared or broken."""
        self.close()

    def __repr__(self) -> str:
        """Endpoint and whether a connection is held."""
        return f"Router({self.endpoint}, {'connected' if self.connected else 'idle'})"
