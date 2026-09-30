"""The blocking session driver.

One :class:`Session` is one connection: it owns a transport and a
:class:`~xrdclient.proto.machine.SessionMachine`, and turns the machine's events
into ordinary Python returns and exceptions. Redirects are *not* followed
here - a redirect means a different server, which is a policy decision the
caller makes; :class:`Session` reports it as :class:`RedirectRequired`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import NoReturn

from .._compat import SLOTS
from .._log import get_logger
from ..config import Config
from ..errors import ConnectionError as XrdConnectionError
from ..errors import ProtocolError, ServerError, WaitLimitError, XRootDError, kXR_NotAuthorized
from ..errors import TimeoutError as XrdTimeoutError
from ..proto import constants as c
from ..proto import machine as m
from ..proto import requests as r
from ..proto import responses as rp
from ..proto.frames import Request
from ..transport.base import Transport
from ..transport.sync import SocketTransport
from ..url import XRootDURL, parse
from . import deadline as dl
from .bulk import WAITRESP_GRACE, BulkReader, BulkUnsupported

__all__ = ["Session", "Result", "RedirectRequired"]

_log = get_logger(__name__)

_RECV = 1 << 18

#: Machine states in which a session can carry nothing more.
_DEAD = frozenset({m.State.CLOSED, m.State.FAILED})

#: Grace added to a kXR_waitresp delay before the deferred reply is overdue.
_WAITRESP_GRACE = WAITRESP_GRACE


class RedirectRequired(XRootDError):
    """The server redirected; the caller must re-issue elsewhere."""

    def __init__(self, target: rp.RedirectInfo) -> None:
        self.target = target
        super().__init__(f"redirected to {target.url}")


@dataclass(frozen=True, **SLOTS)
class Result:
    """A completed request."""

    data: bytes
    status: rp.StatusInfo | None = None

    def __len__(self) -> int:
        return len(self.data)

    def __bytes__(self) -> bytes:
        return self.data


@dataclass(**SLOTS)
class _AwaitState:
    waits: int
    parked: float
    streamed: int
    #: When the stall clock (``Config.stall_deadline``) runs out.
    deadline: float | None
    #: When the caller's own :func:`~xrdclient.deadline` runs out, if it set one.
    expires: float | None = None
    #: When a ``kXR_waitresp`` has waited out :attr:`Config.wait_budget`.
    parked_until: float | None = None


def _expiry() -> float | None:
    """The caller's deadline as an absolute time on the monotonic clock."""
    left = dl.remaining()
    return None if left is None else time.monotonic() + left


def _sooner(stall: float | None, expires: float | None) -> tuple[float | None, bool]:
    """Whichever of the two clocks runs out first, and whether it is the caller's."""
    if expires is None or (stall is not None and stall < expires):
        return stall, False
    return expires, True


def _bounded(config: Config) -> Config:
    """``config`` with its connect timeout cut to what the caller's deadline leaves."""
    left = dl.remaining()
    if left is None or left >= config.connect_timeout:
        return config
    return config.evolve(connect_timeout=max(left, 0.001))


class Session:
    """An authenticated connection to one XRootD server."""

    def __init__(self, transport: Transport, machine: m.SessionMachine, config: Config) -> None:
        self._t = transport
        self._m = machine
        self.config = config
        self._lock = threading.RLock()
        self._inbox: dict[int, list[m.Event]] = {}
        self._notices: list[rp.AttnInfo] = []
        self._paths: dict[int, Transport] = {}
        #: Whether a :meth:`bulk` reader currently owns this connection.
        self._bulk_active = False
        #: Set the moment this connection fails on the wire. A socket whose
        #: peer went away still has a file descriptor, so ``closed`` alone
        #: cannot tell a live connection from a dead one - and a dead one
        #: handed back out of the pool fails the *next* caller instead.
        self._broken = False
        #: Whether this server answers a request that *arrived* on a data
        #: path. ``None`` until one has been tried - see :attr:`arrives_on_path`.
        self._arrives_on_path: bool | None = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def connect(
        cls,
        url: str | XRootDURL,
        *,
        config: Config | None = None,
        username: str = "",
    ) -> Session:
        """Connect, negotiate, upgrade to TLS if required, log in, authenticate."""
        target = parse(url) if isinstance(url, str) else url
        config = config or Config()
        dl.check(f"connecting to {target.host}:{target.port}")
        transport = SocketTransport.connect(target.host, target.port, _bounded(config))
        machine = m.SessionMachine(
            host=target.host,
            port=target.port,
            config=config,
            username=username or target.username or config.username,
            want_tls=target.use_tls or config.require_tls,
        )
        session = cls(transport, machine, config)
        try:
            machine.start()
            session._bringup()
        except BaseException:
            transport.close()
            raise
        return session

    def _bringup(self) -> None:
        expires = _expiry()
        while self._m.state not in (m.State.READY, m.State.FAILED, m.State.CLOSED):
            for event in self._pump(0, None, expires):
                if isinstance(event, m.NeedTLS):
                    self._flush()
                    self._t.start_tls(self._m.host, self.config)
                    self._m.tls_established()
                elif isinstance(event, m.Failed):
                    raise event.error
                elif isinstance(event, m.Disconnected):
                    raise XrdConnectionError(f"connection lost during bring-up: {event.reason}")
        if self._m.state is not m.State.READY:
            raise XrdConnectionError(f"bring-up ended in state {self._m.state.name}")
        _log.debug(
            "session ready with %s:%d via %s%s",
            self._m.host,
            self._m.port,
            self._m.mechanism or "no auth",
            " over TLS" if self._m.tls_active else "",
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def host(self) -> str:
        return self._m.host

    @property
    def port(self) -> int:
        return self._m.port

    @property
    def endpoint(self) -> str:
        return f"{self._m.host}:{self._m.port}"

    @property
    def protocol(self) -> object:
        return self._m.protocol_info

    @property
    def handshake_version(self) -> int:
        """The protocol version the server's handshake reply gave.

        What XrdCl reports as a host's protocol in a ``HostInfo``; the
        ``kXR_protocol`` answer's own version is :attr:`protocol`.
        """
        return self._m.handshake_version

    @property
    def mechanism(self) -> str:
        return self._m.mechanism

    @property
    def is_tls(self) -> bool:
        return self._m.tls_active

    @property
    def broken(self) -> bool:
        """Whether this connection has failed and must not be reused.

        True once a send or a receive raised, or the protocol gave up. The
        pool asks before keeping a connection, so a server that restarted
        costs the transfer that found out and nothing after it.
        """
        return self._broken or self._m.state in (m.State.FAILED, m.State.CLOSED)

    def mark_broken(self) -> None:
        """Record that this connection can no longer be trusted.

        For a caller that drove the wire itself - the bulk reader - and left
        it in a state the session cannot recover from, such as a reply still
        in transit on a stream id nothing is waiting for. The pool then drops
        the connection rather than handing it to someone else.
        """
        self._broken = True

    @property
    def closed(self) -> bool:
        """Whether this session can carry nothing more.

        Its socket counts as well as its state: a session whose socket has
        been finalized under it - the cycle collector does that to a
        connection it frees at the same time as the object that was about to
        pool it - reads as closed, so the pool discards it instead of handing
        a dead descriptor to the next caller (``EBADF``).
        """
        return self._m.state in (m.State.CLOSED, m.State.FAILED) or self._t.closed

    @property
    def data_paths(self) -> list[int]:
        """The path ids bound to this session, in the order they were bound."""
        return list(self._paths)

    @property
    def has_data_path(self) -> bool:
        return bool(self._paths)

    @property
    def arrives_on_path(self) -> bool | None:
        """Whether this server serves a request that arrived on a data path.

        ``None`` until the question has been settled, then the answer. It is
        settled once per connection: first by asking outright - a gateway
        that routes by arrival says so in ``kXR_Qconfig`` - and only when the
        server has no opinion by trying one and seeing, which costs a whole
        :attr:`~xrdclient.Config.data_stream_timeout`.
        """
        return self._arrives_on_path

    def ask_arrival_routing(self) -> bool | None:
        """Settle :attr:`arrives_on_path` by asking, if it is not yet settled.

        For a caller that must know *before* it builds a request which way it
        is going: an :meth:`execute` with ``arrive_on_path`` asks too, but a
        server that disclaims arrival then gets the standard split of the very
        request that was meant for the path, and a write split that way is
        answered where nobody waits for it. Returns the answer, still ``None``
        when the server would not say and only a trial can tell.
        """
        with self._lock:
            if self._arrives_on_path is None:
                self._ask_arrival_routing()
            return self._arrives_on_path

    def notices(self) -> list[rp.AttnInfo]:
        """Drain any unsolicited ``kXR_attn`` messages the server sent."""
        out, self._notices = self._notices, []
        return out

    def note(self, info: rp.AttnInfo) -> None:
        """Keep a notice the bulk reader took off the wire, for :meth:`notices`."""
        self._notices.append(info)

    # ------------------------------------------------------------------
    # Data paths
    # ------------------------------------------------------------------

    def bind_data_path(self) -> int:
        """Open a second connection to this server and bind it to the session.

        Returns the path id the server assigned. Pass it to a request that
        takes one - :class:`~xrdclient.proto.requests.Read`, ``ReadV``, ``PgRead``,
        ``Write``, ``PgWrite`` - and that request's bulk bytes travel on the
        new socket instead of competing with control traffic on this one.

        The new connection does not log in and is not authenticated: it says
        which session it belongs to and inherits that session's identity,
        which is exactly why the server will only accept it from the same
        client. It therefore costs a handshake, not a login.
        """
        with self._lock:
            if self.closed:
                raise XrdConnectionError(f"session to {self.endpoint} is closed")
            if not self._m.session_id:
                raise XRootDError(
                    f"{self.endpoint} gave this session no id, so it cannot "
                    f"be bound to a second connection"
                )
            transport = SocketTransport.connect(self._m.host, self._m.port, self.config)
            machine = m.SessionMachine(
                host=self._m.host,
                port=self._m.port,
                config=self.config,
                username=self._m.username,
                want_tls=self.is_tls,
                bind_to=self._m.session_id,
            )
            try:
                # The bring-up of a data path is a session's worth of work
                # minus the login, so it is driven by a session - which is
                # then thrown away, leaving only the socket.
                bringup = Session(transport, machine, self.config)
                machine.start()
                bringup._bringup()
                pathid = machine.pathid
                if pathid in self._paths:
                    raise ProtocolError(f"{self.endpoint} handed out path id {pathid} twice")
            except BaseException:
                transport.close()
                raise
            # A data path carries only bulk transfer, and a caller routing a
            # bound op over it wants to know quickly if the server will not
            # serve it there (so it can fall back to the control link) rather
            # than block for the whole request_timeout. Bound its reads to the
            # short data_stream_timeout; the control link keeps the long one.
            transport.settimeout(self.config.data_stream_timeout)
            self._paths[pathid] = transport
            _log.debug("bound data path %d to %s", pathid, self.endpoint)
            return pathid

    def _close_path(self, pathid: int) -> None:
        transport = self._paths.pop(pathid, None)
        if transport is not None:
            transport.close()
        self._m.forget_path(pathid)

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def execute(
        self,
        request: Request,
        *,
        path: str = "",
        on_chunk: Callable[[bytes], None] | None = None,
        arrive_on_path: bool = False,
    ) -> Result:
        """Send ``request`` and block until it completes.

        ``on_chunk`` receives the body in pieces as they arrive, including the
        final piece, so concatenating them gives exactly
        :attr:`Result.data` - a caller can stream straight to a file. The
        full body is accumulated for the return value either way.

        ``arrive_on_path`` moves the whole request onto its bound data socket
        and waits for the answer there, for a server that routes a sub-stream
        by arrival connection rather than by the request's path id. It is
        honoured only while that path is actually bound and while this server
        has not already declined one; a redirect or a reconnect that dropped
        the path silently falls back to the control link, so the caller never
        has to unwind its own routing on recovery. The first such request
        asks the server outright - see :meth:`_ask_arrival_routing` - so a
        server with an answer never costs the trial-and-timeout.
        """
        with self._held():
            pathid = request.pathid
            if self._m.state in _DEAD:
                raise XrdConnectionError(f"session to {self.endpoint} is closed")
            if pathid and pathid not in self._paths:
                raise ValueError(f"data path {pathid} is not bound to {self.endpoint}")
            try:
                if arrive_on_path and self._use_arrival_path(request):
                    sid = self._m.submit(request, path=path, arrive_on_path=True)
                    return self._await_arrival(sid, on_chunk, pathid)
                sid = self._m.submit(request, path=path)
                return self._await(sid, on_chunk, pathid if request.reply_on_path else 0)
            except ServerError as exc:
                self._explain_refusal(exc)
                raise

    #: Mechanisms that prove who someone is; the rest only assert it.
    _IDENTIFYING = ("gsi", "ztn", "krb5")

    def _explain_refusal(self, exc: ServerError) -> None:
        """Say why a refused request was sent as nobody in particular.

        A login falls through to ``unix`` when the proxy is missing or
        expired, and the server's answer to what follows is then only
        "unauthorized identity used" - true, and no help. The reason the
        proxy was passed over was known at login; this puts it where the
        person reading the error will see it.
        """
        if exc.code != kXR_NotAuthorized or exc.hint:
            return
        used = self._m.mechanism
        if used in self._IDENTIFYING:
            return
        skipped = self._m.skipped
        reasons = [f"{name} was not used - {skipped[name]}" for name in self._IDENTIFYING
                   if name in skipped]
        if reasons:
            exc.explain(f"logged in as {used or 'nobody'}: " + "; ".join(reasons))

    @contextmanager
    def _held(self) -> Iterator[None]:
        """The session lock, waited for no longer than the caller's deadline allows."""
        left = dl.remaining()
        if left is None:
            with self._lock:
                yield
            return
        if not self._lock.acquire(timeout=max(left, 0.0)):
            raise dl.OperationExpiredError(
                f"the request expired waiting its turn on {self.endpoint}"
            )
        try:
            yield
        finally:
            self._lock.release()

    def _use_arrival_path(self, request: Request) -> bool:
        """Whether a request that asked to arrive on its data path may."""
        if not request.pathid:
            return False
        if self._arrives_on_path is None:
            self._ask_arrival_routing()
        return self._arrives_on_path is not False and request.pathid in self._paths

    def _await_arrival(
        self, sid: int, on_chunk: Callable[[bytes], None] | None, pathid: int
    ) -> Result:
        try:
            result = self._await(sid, on_chunk, pathid)
        except Exception:
            # The abandoned answer may still arrive on this link, so the path
            # cannot safely carry a later request.
            self._arrives_on_path = False
            self._close_path(pathid)
            raise
        self._arrives_on_path = True
        return result

    def _ask_arrival_routing(self) -> None:
        """Settle :attr:`arrives_on_path` by asking, when the server will say.

        A gateway that serves requests arriving on a data path advertises it
        as a ``kXR_Qconfig`` value (``brix.substreams``); a stock daemon
        echoes the key back or answers nothing, which is the convention for
        "never heard of it". Either answer costs one round trip on the
        control link where trying and failing would cost a whole
        :attr:`~xrdclient.Config.data_stream_timeout`. A query that itself fails
        settles nothing - the behavioural probe is still there.
        """
        try:
            sid = self._m.submit(r.Query(c.kXR_Qconfig, "brix.substreams"))
            answer = self._await(sid, None).data
        except XRootDError:
            return
        value = answer.rstrip(b"\x00").strip().decode("utf-8", "replace")
        self._arrives_on_path = bool(value) and value != "brix.substreams"
        _log.debug(
            "%s %s arrival routing (brix.substreams=%r)",
            self.endpoint,
            "advertises" if self._arrives_on_path else "disclaims",
            value,
        )

    def _armed(self, seconds: float = 0.0) -> float | None:
        """A fresh stall deadline, or ``None`` when there is not to be one.

        ``seconds`` is a delay the server has asked for: the deadline never
        comes in sooner than that plus a grace, so a server that says "in ten
        minutes" is not cut off at a shorter deadline for saying so.
        """
        limit = self.config.stall_deadline
        if limit <= 0:
            return None
        return time.monotonic() + max(limit, seconds + _WAITRESP_GRACE if seconds else 0.0)

    def _await(self, sid: int, on_chunk: Callable[[bytes], None] | None, pathid: int = 0) -> Result:
        try:
            return self._answer(sid, on_chunk, pathid)
        finally:
            # Given up on with its answer maybe still to come - a stall, a
            # deferred reply over the wait budget, a consumer that raised: the
            # id stays out of circulation until that answer is off the wire,
            # so it can never be taken for a later request's. A completed,
            # failed or redirected stream is released already, and this is
            # a no-op for it.
            self._m.abandon(sid)

    def _answer(self, sid: int, on_chunk: Callable[[bytes], None] | None, pathid: int) -> Result:
        # Absolute, over the whole logical operation: see Config.stall_deadline.
        state = _AwaitState(0, 0.0, 0, self._armed(), _expiry())
        while True:
            for event in self._events_until(sid, pathid, state):
                if type(event) is m.Completed:
                    # Completed carries the whole body; stream only its unseen tail.
                    if on_chunk is not None and len(event.data) > state.streamed:
                        on_chunk(event.data[state.streamed :])
                    return Result(event.data, event.status)
                self._consume_event(event, sid, on_chunk, state)

    def _events_until(self, sid: int, pathid: int, state: _AwaitState) -> list[m.Event]:
        """The next events for ``sid``, within every clock that applies to it.

        A deferred answer's wait budget runs as a caller's deadline would -
        the connection is kept, and the id stays held for the late answer -
        and reads as the :class:`WaitLimitError` it is.
        """
        parked = state.parked_until
        expires = state.expires
        if parked is not None and (expires is None or parked < expires):
            expires = parked
        try:
            return self._events_for(sid, pathid, state.deadline, expires)
        except dl.OperationExpiredError:
            if expires is not parked or parked is None:
                raise
            raise WaitLimitError(
                f"the deferred answer did not come within the "
                f"{self.config.wait_budget:.0f}s budget for one operation",
                attempts=state.waits,
            ) from None

    def _consume_event(
        self,
        event: m.Event,
        sid: int,
        on_chunk: Callable[[bytes], None] | None,
        state: _AwaitState,
    ) -> None:
        """Act on one event for ``sid`` that is not its completion."""
        if isinstance(event, m.Failed):
            raise event.error
        if isinstance(event, m.Chunk):
            if on_chunk is not None:
                on_chunk(event.data)
            state.streamed += len(event.data)
        elif isinstance(event, m.Redirected):
            self._m.release(sid)
            raise RedirectRequired(event.target)
        elif isinstance(event, m.Waiting):
            self._wait_event(event, sid, state)

    def _wait_event(self, event: m.Waiting, sid: int, state: _AwaitState) -> None:
        if not event.resend:
            self._wait_for_answer(event, sid, state)
            return
        # Parking is not stalling, but repeated delays share one budget.
        state.parked += event.seconds
        if state.parked > self.config.wait_budget:
            self._give_up(
                sid,
                event,
                f"{type(event.request).__name__} was parked for {state.parked:.0f}s, "
                f"over the {self.config.wait_budget:.0f}s budget for one operation",
                state.waits,
            )
        if state.expires is not None and time.monotonic() + event.seconds >= state.expires:
            # XrdCl's rule: a resend that would go out after the request has
            # expired is not waited for - it expires now.
            self._m.release(sid)
            raise dl.OperationExpiredError(
                f"{type(event.request).__name__} expired: the server asked for "
                f"{event.seconds:.0f}s more than the deadline left"
            )
        state.waits += 1
        if state.waits > self.config.redirect_limit:
            self._give_up(
                sid,
                event,
                f"server kept asking to wait for {type(event.request).__name__}",
                state.waits,
            )
        _log.debug("server asked to wait %.1fs: %s", event.seconds, event.message)
        time.sleep(event.seconds)
        self._m.resume(sid)
        self._flush()
        state.streamed = 0  # the body starts over on the resend
        state.deadline = self._armed()

    def _wait_for_answer(self, event: m.Waiting, sid: int, state: _AwaitState) -> None:
        """``kXR_waitresp``: the answer comes later, on its own - wait for it.

        Its seconds are the most the server may take, not what it will: EOS
        parks the ``kXR_sync`` of a third-party copy for up to an hour and
        answers when the copy is done, usually far sooner. So the wait is
        armed for as much of it as :attr:`Config.wait_budget` has left, and
        only running out of that is giving up - never the promise alone, which
        XrdCl does not hold against a request either.
        """
        budget = self.config.wait_budget
        left = float("inf") if budget <= 0 else budget - state.parked
        # Zero names no bound at all: as long as the budget allows.
        allowed = min(event.seconds, left) if event.seconds > 0 else left
        if allowed <= 0:
            self._give_up(
                sid,
                event,
                f"{type(event.request).__name__} has already waited out the "
                f"{budget:.0f}s budget for one operation",
                state.waits,
            )
        state.deadline = self._armed(0 if allowed == float("inf") else allowed)
        if allowed != float("inf"):
            state.parked += allowed
            state.parked_until = time.monotonic() + allowed

    def _give_up(self, sid: int, event: m.Waiting, message: str, attempts: int) -> NoReturn:
        """Stop waiting on ``sid``, freeing it when nothing more will come.

        ``kXR_wait`` is the server's last word on a request until it is sent
        again, so its id can be reused at once. A ``kXR_waitresp`` promises an
        answer later; :meth:`_await` keeps that id out of circulation until
        the answer is off the wire.
        """
        if event.resend:
            self._m.release(sid)
        raise WaitLimitError(message, attempts=attempts)

    # ------------------------------------------------------------------
    # I/O pump
    # ------------------------------------------------------------------

    def _flush(self) -> None:
        data = self._m.data_to_send()
        if data:
            self._t.send(data)
        if not self._m.has_path_data:
            return
        for pathid, transport in self._paths.items():
            queued = self._m.path_data_to_send(pathid)
            if queued:
                transport.send(queued)

    def _receive(
        self, pathid: int, deadline: float | None, expires: float | None = None
    ) -> bytes:
        """Read once from ``pathid``'s link, never past ``deadline`` or ``expires``.

        ``deadline`` is the stall clock, and running into it means the
        connection has gone quiet: a :class:`~xrdclient.errors.TimeoutError`
        that costs the connection. ``expires`` is the caller's own deadline,
        and running into that costs only the request -
        :class:`~xrdclient.session.deadline.OperationExpiredError`. A timed-out
        ``recv`` has taken nothing off the wire, so the link is still in step.
        """
        transport = self._paths[pathid] if pathid else self._t
        resting = self.config.data_stream_timeout if pathid else self.config.request_timeout
        limit, expiring = _sooner(deadline, expires)
        if limit is None:
            return transport.receive(_RECV)
        left = limit - time.monotonic()
        if left <= 0:
            raise self._out_of_time(expiring)
        if left >= resting:
            # The socket's own timeout already comes in first; leave it be,
            # so a data path keeps the short one its fallback depends on.
            return transport.receive(_RECV)
        transport.settimeout(left)
        try:
            return transport.receive(_RECV)
        except XrdTimeoutError as exc:
            raise self._out_of_time(expiring) from exc
        finally:
            transport.settimeout(resting)

    def _out_of_time(self, expired: bool) -> XrdTimeoutError:
        """The error for running out of time: the caller's deadline, or a stall."""
        if expired:
            return dl.OperationExpiredError(f"no answer from {self.endpoint} before the deadline")
        return XrdTimeoutError(self._stalled())

    def _stalled(self) -> str:
        return (
            f"{self.endpoint} did not finish answering within the "
            f"{self.config.stall_deadline:.0f}s stall deadline"
        )

    def _pump(
        self, pathid: int = 0, deadline: float | None = None, expires: float | None = None
    ) -> list[m.Event]:
        """One send/receive turn; returns whatever events it produced."""
        try:
            self._flush()
        except (XrdConnectionError, XrdTimeoutError):
            self._broken = True
            raise
        try:
            self._m.receive_data(self._receive(pathid, deadline, expires), pathid=pathid)
        except dl.OperationExpiredError:
            # The caller's time ran out, not the connection's: nothing was
            # taken off the wire, and the request's stream id is abandoned
            # by whoever was waiting on it.
            raise
        except (XrdConnectionError, XrdTimeoutError):
            if pathid:
                # A data path that failed or went quiet takes only itself
                # down: the control link, and every handle opened on it, is
                # as good as it was, and calling the session broken would
                # have its files skip their closes and the pool drop it.
                self._close_path(pathid)
            else:
                self._broken = True
            raise
        return self._m.drain()

    def _events_for(
        self,
        sid: int,
        pathid: int = 0,
        deadline: float | None = None,
        expires: float | None = None,
    ) -> list[m.Event]:
        """Block until at least one event for ``sid`` is available.

        ``pathid`` says which link the answer is expected on: a read that
        named a data path is answered there, and waiting on the control link
        for it would wait forever. ``deadline`` is the whole operation's, so
        a peer that dribbles cannot renew it one byte at a time; ``expires``
        is the caller's :func:`~xrdclient.deadline`.
        """
        if self._inbox:
            queued = self._inbox.pop(sid, None)
            if queued:
                return queued
        while True:
            events = self._pump(pathid, deadline, expires)
            if len(events) == 1 and getattr(events[0], "streamid", None) == sid:
                # One reply, and it is the one being waited for: nearly every
                # turn of a request-at-a-time caller.
                return events
            mine: list[m.Event] = []
            for event in events:
                self._route_event(event, sid, mine)
            if mine:
                return mine

    def _route_event(self, event: m.Event, sid: int, mine: list[m.Event]) -> None:
        target = getattr(event, "streamid", None)
        if target == sid:
            mine.append(event)
        elif isinstance(event, m.Attention):
            self._notices.append(event.info)
        elif isinstance(event, m.PathLost):
            self._close_path(event.pathid)
        elif isinstance(event, m.Disconnected):
            raise XrdConnectionError(f"connection lost: {event.reason}")
        elif isinstance(event, m.Failed) and target is None:
            raise event.error
        elif target is not None:
            self._inbox.setdefault(target, []).append(event)

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Bulk data plane
    # ------------------------------------------------------------------

    @property
    def transport(self) -> Transport:
        """The control link itself, for the bulk reader that borrows it."""
        return self._t

    @property
    def machine(self) -> m.SessionMachine:
        """The protocol state, for framing a bulk request on a leased stream."""
        return self._m

    @contextmanager
    def bulk(self, handle: bytes, *, chunk: int, depth: int) -> Iterator[BulkReader]:
        """Lend this connection to a :class:`~xrdclient.session.bulk.BulkReader`.

        The session lock is held throughout, and the machine must be idle: a
        reader that framed its own requests while an ordinary one was in
        flight would take the other's reply off the wire. Nothing else may use
        this session until the block ends, which is why the reader is given
        out by a context manager rather than returned.

        A reader whose caller's :func:`~xrdclient.deadline` runs out leaves the
        connection in step - it stops between frames and hands the replies
        still owed back to the machine to drop - so, as on the event path,
        running out of time costs the request and not the connection.
        """
        with self._held():
            # The lock is reentrant, so it alone would let one thread open a
            # second reader inside the first; two readers would then take each
            # other's replies off the wire. This flag is the real invariant.
            if self._bulk_active:
                raise ProtocolError(
                    "this connection is already lent to a bulk read; "
                    "one reader owns the wire at a time"
                )
            if not self._m.idle():
                # Something is still owed an answer on this wire - a request
                # given up on whose reply has not arrived yet - so the event
                # path, which knows to drop it, carries this one.
                raise BulkUnsupported(
                    "a bulk read needs the connection to itself, and this one "
                    "still has requests outstanding"
                )
            self._flush()
            self._bulk_active = True
            try:
                # Leaving the block settles the reader - every reply it asked
                # for is taken off the wire, or the connection is marked
                # broken - even when its consumer abandoned a stream part-way
                # and the suspended iterator has not been closed yet.
                with BulkReader(self, handle, chunk=chunk, depth=depth) as reader:
                    yield reader
            except dl.OperationExpiredError:
                # Settled already: in step and left to the machine, or marked
                # broken by the reader itself if it stopped part-way through.
                raise
            except (XrdConnectionError, XrdTimeoutError):
                # The reader drives the socket itself, so this is where its
                # failures reach the session that owns it.
                self._broken = True
                raise
            finally:
                self._bulk_active = False

    def close(self) -> None:
        """End the session and drop the connection."""
        with self._lock:
            if self._m.state is m.State.READY:
                try:
                    self._m.close()
                    self._flush()
                except (XRootDError, OSError):
                    pass
            self._t.close()
            for transport in self._paths.values():
                transport.close()
            self._paths.clear()
            self._m.state = m.State.CLOSED

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        paths = f", {len(self._paths)} data path" if self._paths else ""
        if len(self._paths) > 1:
            paths += "s"
        return (
            f"Session({self.endpoint}, {self._m.state.name.lower()}"
            f"{', tls' if self.is_tls else ''}"
            f"{', ' + self._m.mechanism if self._m.mechanism else ''}{paths})"
        )
