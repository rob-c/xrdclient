"""Redirect following and reconnection.

XRootD's whole federation model is redirection: a manager answers ``kXR_open``
with "go ask this data server", possibly through several tiers. A
:class:`Router` owns whichever :class:`~xrdclient.session.sync.Session` is current,
re-issues requests as it is bounced along, and reconnects a dropped
connection so that an idle handle survives a server restart.

The redirect target is sticky: after a file is opened on a data server, its
reads must go to that same server, so :meth:`pin` hands back a Router already
positioned there.
"""

from __future__ import annotations

import threading
import time

from .._log import get_logger
from ..config import Config
from ..errors import ConnectionError as XrdConnectionError
from ..errors import RedirectLimitError, TransientError, WaitLimitError
from ..errors import TimeoutError as XrdTimeoutError
from ..proto.frames import Request
from ..url import XRootDURL, parse
from .pool import SESSIONS
from .sync import RedirectRequired, Result, Session

__all__ = ["Router"]

_log = get_logger(__name__)


def _retarget(request: Request, token: str) -> None:
    """Fold a redirect's opaque token into the request's path CGI."""
    path = getattr(request, "path", None)
    if not token or not isinstance(path, str):
        return
    # Not every request carries a path, which is why this goes through
    # getattr; the ones that do are ordinary mutable dataclasses.
    request.path = f"{path}{'&' if '?' in path else '?'}{token}"  # type: ignore[attr-defined]


class Router:
    """A connection that knows how to move."""

    def __init__(
        self,
        url: str | XRootDURL,
        config: Config | None = None,
        *,
        reconnect: bool = True,
    ) -> None:
        self.url = parse(url) if isinstance(url, str) else url
        self.config = config or Config()
        #: Whether a dropped connection may be replaced under a live request.
        #: False on a pinned router, because the file handle its caller holds
        #: exists only on the connection that issued it: reconnecting would
        #: turn "the server went away" into "invalid file handle", five frames
        #: further on and much harder to act on.
        self.reconnect = reconnect
        self._session: Session | None = None
        #: Whether this router may hand its connection back to the pool. False
        #: on a pinned router that borrowed the connection it shares: the
        #: router it came from is still using it.
        self._owns = True
        self._lock = threading.RLock()

    # ------------------------------------------------------------------

    @property
    def session(self) -> Session:
        """The live session, connecting on first use."""
        with self._lock:
            if self._session is not None and self._session.closed and not self.reconnect:
                # Silently opening a replacement would hand the caller a live
                # connection on which its file handle does not exist, and the
                # server would answer "file is not open" - true, useless, and
                # three layers away from the cause.
                raise XrdConnectionError(f"the connection to {self.endpoint} was lost")
            if self._session is None or self._session.closed:
                if self._session is not None:
                    # A session whose peer went away is closed as far as the
                    # protocol goes, but its socket is still a descriptor.
                    self._session.close()
                self._owns = True
                self._session = SESSIONS.acquire(self.url, self.config) or Session.connect(
                    self.url, config=self.config
                )
            return self._session

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
        hops = 0
        attempts = 0
        while True:
            try:
                return self.session.execute(request, path=path, **kwargs)  # type: ignore[arg-type]
            except RedirectRequired as redirect:
                hops += 1
                self._redirect(request, path, redirect, hops)
            except WaitLimitError:
                # A busy server is not a broken connection: the budget for
                # "come back later" has already been spent once here, and
                # reconnecting would only spend it again on a new socket.
                raise
            except XrdConnectionError as exc:
                attempts += 1
                self._recover(request, exc, attempts)

    def _redirect(self, request: Request, path: str, redirect: RedirectRequired, hops: int) -> None:
        if hops > self.config.redirect_limit:
            raise RedirectLimitError(
                f"more than {self.config.redirect_limit} redirects for "
                f"{type(request).__name__} {path}"
            ) from redirect
        self._follow(redirect)
        _retarget(request, redirect.target.token)

    def _recover(self, request: Request, error: XrdConnectionError, attempts: int) -> None:
        if not self._retryable(request, attempts):
            # A timeout stays a timeout: "it was slow" and "it bounced" call
            # for different things from the caller.
            kind = XrdTimeoutError if isinstance(error, XrdTimeoutError) else TransientError
            raise kind(
                f"{type(request).__name__} on {self.endpoint} failed: {error}",
                attempts=attempts,
            ) from error
        _log.debug("reconnecting to %s after %s", self.endpoint, error)
        self._drop()
        self._pause(attempts)

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

    def _follow(self, redirect: RedirectRequired) -> None:
        target = redirect.target
        scheme = "roots" if target.port < 0 else self.url.scheme
        # The connection being left behind is not broken - it is simply not
        # the server holding the file - so it goes back to the pool, keyed by
        # the endpoint it is still connected to. The next open asks the same
        # manager the same question, and finds it already answered once.
        self.close()
        self.url = self.url.evolve(
            scheme=scheme, host=target.host, port=abs(target.port) or self.url.port
        )
        _log.debug("redirected to %s", self.endpoint)

    def _drop(self) -> None:
        with self._lock:
            if self._session is not None:
                self._session.close()
                self._session = None
            self._owns = True

    def discard(self) -> None:
        """Close this connection for good, keeping it out of the pool.

        For one that has just misbehaved: pooling a connection whose server
        has gone away only moves the failure to whoever picks it up next.
        """
        self._drop()

    def pin(self, *, transfer: bool = False) -> Router:
        """A router bound to the current endpoint, sharing this connection.

        Used after an open: further operations on the handle must not be
        re-routed, because the handle only exists on this server — nor
        silently reconnected, for the same reason. A pinned router therefore
        reports a lost connection as a :class:`~xrdclient.errors.TransientError` and
        leaves the recovery to :class:`~xrdclient.client.file.File`, which is the
        only layer that knows how to get the handle back.

        ``transfer`` hands the connection over rather than sharing it: the
        caller is done with this router and the pinned one becomes the single
        owner, free to return the connection to the pool when it closes.
        Without it the pinned router is a borrower, and closing one of those
        lets go of the connection without touching it - the router it came
        from still has work for it.
        """
        pinned = Router(self.url, self.config, reconnect=False)
        with self._lock:
            pinned._session = self._session
            if transfer:
                self._session = None
            pinned._owns = transfer or pinned._session is None
        return pinned

    def close(self) -> None:
        """Finish with this connection, offering it to the pool if it is ours."""
        with self._lock:
            session, self._session = self._session, None
            owns, self._owns = self._owns, True
        if session is None or not owns:
            return
        if not SESSIONS.release(session, self.url, self.config):
            session.close()

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
