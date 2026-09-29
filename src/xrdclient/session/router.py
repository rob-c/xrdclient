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
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .._compat import SLOTS
from .._log import get_logger
from ..config import Config
from ..errors import ConnectionError as XrdConnectionError
from ..errors import ProtocolError, RedirectLimitError, TransientError, WaitLimitError
from ..errors import TimeoutError as XrdTimeoutError
from ..proto import constants as c
from ..proto import requests as r
from ..proto.frames import Request
from ..proto.responses import RedirectInfo
from ..url import ROOT_SCHEMES, XRootDURL, parse
from .pool import SESSIONS, same_server
from .sync import RedirectRequired, Result, Session

__all__ = ["Router"]

_log = get_logger(__name__)

#: The fields of a request that name something the server resolves as a path,
#: and so may carry opaque CGI - checked in order, subclasses first. A
#: symlink's first field is the text the link will hold, not a path: a token
#: appended there would end up inside the link. Anything not listed names its
#: path, if it has one, in ``path``.
_PATH_FIELDS: tuple[tuple[type[Request], tuple[str, ...]], ...] = (
    (r.Symlink, ("dst",)),
    (r.Mv, ("src", "dst")),
    (r.Statx, ("paths",)),
    (r.Prepare, ("paths",)),
)

#: A request's path fields as they were before any redirect touched them.
PathFields = dict[str, "str | list[str]"]


def _path_fields(request: Request) -> PathFields:
    """Snapshot the fields a redirect token may be folded into.

    A cancelling ``kXR_prepare`` names a request id rather than paths, and a
    request by handle has an empty path: neither has anywhere for a token.
    """
    if isinstance(request, r.Prepare) and request.options & c.kXR_cancel:
        return {}
    names = next((f for kind, f in _PATH_FIELDS if isinstance(request, kind)), ("path",))
    found: PathFields = {}
    for name in names:
        value = getattr(request, name, None)
        if isinstance(value, (str, list)):
            found[name] = list(value) if isinstance(value, list) else value
    return found


def _with_token(path: str, token: str) -> str:
    if not token or not path:
        return path
    return f"{path}{'&' if '?' in path else '?'}{token}"


def _retarget(request: Request, original: PathFields, token: str) -> None:
    """Put this hop's redirect token on the request's paths.

    Built from ``original`` rather than from what the request says now: each
    redirector's token is a capability for the server it points at, so the
    previous hop's is stale by the time the next one arrives, and appending
    would pile them up - two values for one key, and the wrong one first. The
    caller's own CGI is in ``original`` and survives every hop.
    """
    for name, value in original.items():
        if isinstance(value, list):
            setattr(request, name, [_with_token(p, token) for p in value])
        else:
            setattr(request, name, _with_token(value, token))


def _as_url(host: str) -> str:
    """A negative-port host field as a URL, reading a bare ``host[:port]`` as XRootD.

    XrdCl's ``URL::FromString`` takes a string with no scheme to be a
    ``root://`` one, so a server that names only ``host:port`` - or a host
    with no port, which then gets the default - is followed rather than
    refused.
    """
    return host if "://" in host else f"root://{host}"


def _redirect_url(base: XRootDURL, host: str) -> XRootDURL:
    """Where a ``kXR_redirect`` with a negative port points.

    A negative port says the host field is a whole URL - one the server sends
    only to a client that could take it - rather than a host name. A URL for
    another protocol (``https://``, say) is a redirect this client cannot
    follow on the same request, so it is refused by name rather than dialled
    as if it were XRootD.
    """
    target = parse(_as_url(host))
    if target.scheme not in ROOT_SCHEMES:
        raise ProtocolError(f"redirected to a {target.scheme} URL, which is not XRootD: {host}")
    if not target.host:
        raise ProtocolError(f"kXR_redirect with a negative port names no host: {host!r}")
    return base.evolve(scheme=target.scheme, host=target.host, port=target.port)


def _redirect_path(target: RedirectInfo) -> str:
    """The path a redirect tells the request to use instead, or ``""``.

    Only a negative port's URL can carry one - ``root://ds//real/name``, as
    ``XrdOfs`` sends for a file whose storage is elsewhere - and XrdCl puts
    it in place of the request's own (``RewriteCGIAndPath`` with the new
    URL's path). A URL that stops at its host, or at the slash after it,
    names no path, and the request keeps its own.
    """
    if target.port >= 0:
        return ""
    url = _as_url(target.host)
    _, _, path = url.partition("://")[2].partition("/")
    return parse(url).path if path else ""


def _repath(original: PathFields, path: str) -> None:
    """Put ``path`` in place of the request's path, keeping the caller's CGI.

    The field that names where the request acts - a rename's destination,
    as XrdCl rewrites it, and a request's only path otherwise. A request by
    handle has no path to replace, and one naming a list of paths cannot
    have them all become one, so both are left as they are.
    """
    for name in reversed(original):
        value = original[name]
        if isinstance(value, str) and value:
            _, sep, query = value.partition("?")
            original[name] = f"{path}{sep}{query}"
            return


@dataclass(**SLOTS)
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


class _Loan:
    """A connection more than one router is using at once.

    What a :class:`~xrdclient.client.FileSystem` and the files it opened
    share when an open was not redirected. Whichever of them lets go last
    puts it back in the pool - or closes it, if any of them saw it fail - so
    the filesystem closing first neither pools a connection a file is still
    reading from, nor closes it under the file.
    """

    __slots__ = ("session", "url", "config", "holders", "spoiled", "_lock")

    def __init__(self, session: Session, url: XRootDURL, config: Config) -> None:
        self.session = session
        #: Where, and as whom, the connection goes back to the pool.
        self.url = url
        self.config = config
        self.holders = 1
        #: Whether a holder let go of it because it failed or misbehaved.
        self.spoiled = False
        self._lock = threading.Lock()

    def join(self) -> _Loan:
        with self._lock:
            self.holders += 1
        return self

    def leave(self, *, spoiled: bool = False) -> None:
        with self._lock:
            self.holders -= 1
            self.spoiled = self.spoiled or spoiled
            last = self.holders == 0
        if not last:
            return
        if self.spoiled or not SESSIONS.release(self.session, self.url, self.config):
            self.session.close()


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
        self.url = parse(url) if isinstance(url, str) else url
        self.config = config or Config()
        #: Whether a dropped connection may be replaced under a live request.
        #: False on a pinned router, because the file handle its caller holds
        #: exists only on the connection that issued it: reconnecting would
        #: turn "the server went away" into "invalid file handle", five frames
        #: further on and much harder to act on.
        self.reconnect = reconnect
        #: Whether a redirect moves the router (a file) or only the request
        #: that got it (a filesystem, which asks its manager every time).
        self.sticky = sticky
        self._session: Session | None = None
        #: Set while the connection is shared with other routers - a lender
        #: and the files it lent to - and ``None`` while this router is its
        #: only user, free to pool or close it alone.
        self._loan: _Loan | None = None
        #: The router a :meth:`lend` borrows its first connection from.
        self._lender: Router | None = None
        #: Whether this router was lent for one open, so that pinning it hands
        #: its hold on the connection over rather than sharing it; see
        #: :meth:`pin`.
        self._lent = False
        self._lock = threading.RLock()

    # ------------------------------------------------------------------

    @property
    def session(self) -> Session:
        """The live session, connecting on first use."""
        session = self._session
        if session is not None and not session.closed:
            # The steady state, answered without the lock: reading one
            # reference is atomic, and a thread replacing it meanwhile is a
            # race the locked read would have lost the same way.
            return session
        with self._lock:
            if self._session is not None and self._session.closed and not self.reconnect:
                # Silently opening a replacement would hand the caller a live
                # connection on which its file handle does not exist, and the
                # server would answer "file is not open" - true, useless, and
                # three layers away from the cause.
                raise XrdConnectionError(f"the connection to {self.endpoint} was lost")
            if self._session is None or self._session.closed:
                self._connect()
            assert self._session is not None
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
        return SESSIONS.acquire(url, self.config) or SESSIONS.connect(url, self.config)

    def _offer(self, session: Session, url: XRootDURL) -> None:
        """Pool a connection this router is finished with, or close it."""
        if not SESSIONS.release(session, url, self.config):
            session.close()

    @property
    def endpoint(self) -> str:
        return f"{self.url.host}:{self.url.port}"

    @property
    def connected(self) -> bool:
        return self._session is not None and not self._session.closed

    def bind_data_path(self) -> int:
        """Bind a second connection to the current session for bulk data.

        Not retried and not followed across a redirect: a path id belongs to
        one session on one server, so a caller that loses the connection must
        ask the new one for a new path rather than be handed a stale number.
        """
        return self.session.bind_data_path()

    def execute(self, request: Request, *, path: str = "", **kwargs: object) -> Result:
        """Run ``request``, following redirects and retrying dropped connections."""
        route = _Route(None)
        while True:
            try:
                return self._attempt(request, route, path=path, **kwargs)
            except RedirectRequired as redirect:
                self._redirect(request, path, route, redirect)
            except WaitLimitError:
                # A busy server is not a broken connection: the budget for
                # "come back later" has already been spent once here, and
                # reconnecting would only spend it again on a new socket.
                raise
            except XrdConnectionError as exc:
                self._recover(request, route, exc)

    def _attempt(self, request: Request, route: _Route, **kwargs: object) -> Result:
        """Send ``request`` once, to wherever ``route`` says it is going."""
        target = route.target
        session = route.session = self.session if target is None else self._dial(target)
        if target is None:
            return session.execute(request, **kwargs)  # type: ignore[arg-type]
        # A redirected request's own connection: borrowed from the pool for
        # this one attempt and given back straight after, so that the next
        # request redirected to the same data server finds it waiting.
        try:
            return session.execute(request, **kwargs)  # type: ignore[arg-type]
        except WaitLimitError:
            raise
        except XrdConnectionError:
            # Failed under a live request: whoever picks it up next deserves
            # better than a connection with an unknown reply still owed on it.
            session.close()
            raise
        finally:
            self._offer(session, target)

    def _redirect(
        self, request: Request, path: str, route: _Route, redirect: RedirectRequired
    ) -> None:
        route.hops += 1
        if route.hops > self.config.redirect_limit:
            raise RedirectLimitError(
                f"more than {self.config.redirect_limit} redirects for "
                f"{type(request).__name__} {path}"
            ) from redirect
        destination = self._destination(route.target or self.url, redirect.target)
        moved = self.sticky and route.target is None and self._move(route.session, destination)
        if not moved:
            home = same_server(destination, self.url, self.config)
            route.target = None if home else destination
        if route.original is None:
            route.original = _path_fields(request)
        path = _redirect_path(redirect.target)
        if path:
            # Into the snapshot, not only the request: every later hop, and a
            # retry at the manager, builds on the path this one moved it to.
            _repath(route.original, path)
        _retarget(request, route.original, redirect.target.token)
        _log.debug("redirected to %s:%s", destination.host, destination.port)

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
        if route.target is not None:
            # A data server that has gone away is the least likely one to
            # answer the retry; its manager can route around it, as XrdCl's
            # does, and the manager hands out a fresh token with the redirect.
            route.target = None
            # A target is only ever set by a redirect, which took the snapshot.
            assert route.original is not None
            _retarget(request, route.original, "")
        else:
            self._drop(route.session)
        self._pause(route.attempts)

    def _retryable(self, request: Request, attempts: int) -> bool:
        return self.reconnect and attempts <= self.config.connect_retries and request.idempotent

    def _pause(self, attempts: int) -> None:
        """Wait before retrying, doubling each time.

        Reconnecting three times in as many microseconds only ever finds the
        server still down; a restarting daemon needs a second or two, and the
        wait costs nothing when the connection comes back on the first try.
        """
        backoff = self.config.retry_backoff
        if backoff > 0:
            time.sleep(min(backoff * 2 ** (attempts - 1), self.config.wait_cap))

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

    def __del__(self) -> None:
        """Give the connection back even when nobody said ``close``.

        A one-liner - ``xrdclient.read_text(url)``, or a path used and dropped -
        should not cost a socket for the rest of the process. Closing here is
        belt to the ``with`` block's braces: the pool takes the connection
        back and the next call reuses it.
        """
        try:
            self.close()
        except Exception:  # pragma: no cover - only reachable at interpreter shutdown
            pass

    def __enter__(self) -> Router:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"Router({self.endpoint}, {'connected' if self.connected else 'idle'})"
