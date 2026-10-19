"""The blocking session driver.

One :class:`Session` is one connection: it owns a transport and a
:class:`~xrdclient.proto.machine.SessionMachine`, and turns the machine's events
into ordinary Python returns and exceptions. Redirects are *not* followed
here - a redirect means a different server, which is a policy decision the
caller makes; :class:`Session` reports it as :class:`RedirectRequired`.

**Many callers, one connection.** XRootD multiplexes requests on stream ids,
and so does this driver: any number of threads may have a request in flight
on one session at once. Nothing is held for a request's lifetime. The session
lock guards only the protocol state, and is held just long enough to submit a
request, to feed the machine what one read brought in, or to file the events
that came out of it. Everything that blocks - waiting for the socket, sitting
out a ``kXR_wait``, waiting for a ``kXR_waitresp`` answer - happens with it
let go.

Where to start reading: :meth:`Session.connect` (bring-up), then
:meth:`Session.execute` -> :meth:`Session._start` (submit and send) ->
:meth:`Session._await` -> :meth:`Session._events_for` (lead or follow). The
lock rules that must not break: :attr:`Session._send_lock` is never *waited
for* while :attr:`Session._lock` is held (only tried), a caller's callback
never runs under either, and a TLS link's per-link I/O lock is only ever
taken for one record-sized send or one ``recv`` that is known not to block.

Reading is a role, not a thread (leader/follower). Each link has at most one
*reader* at a time: whichever waiter found it free. The reader waits on the
socket, feeds the machine, and files every event it produced into the
mailbox of the request it belongs to - its own or another's - waking whoever
it was for. The others *follow*: they sleep on a condition until their
mailbox has something, the reader role comes free, or their own clock runs
out. A reader whose own answer arrives hands the role on as it leaves, and
deals with that answer - streaming callbacks included - holding nothing, so a
slow consumer never stalls the requests behind it.

A link that fails fails every request waiting on it, not only the one that
happened to be reading: each gets its own copy of the error in its mailbox.

**Cancellation.** A request made inside a cancelled
:class:`~xrdcore.cancel.CancelScope` (which is how :mod:`xrdclient.aio` hands
an ``asyncio`` cancellation to its worker thread) is not sent, and one already
waiting is abandoned where it stands: its waiter raises
:class:`~xrdcore.cancel.Cancelled`, passing the reader role on if it held it,
and the stream id stays out of circulation until the late reply has arrived
and been dropped - the same bookkeeping as an expired
:func:`~xrdclient.deadline`, so the connection stays in step and in use. Inside
a scope, waits are taken in short slices (:data:`~xrdcore.cancel.POLL`) so
the waiter notices on its own: nobody has to wake it, which matters because
the canceller is an event loop that must not block on this session's lock.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from typing import NoReturn, TypedDict

from xrdcore import cancel

from ..config import Config
from ..errors import ConnectionError as XrdConnectionError
from ..errors import ProtocolError, ServerError, WaitLimitError, XRootDError, kXR_NotAuthorized
from ..errors import TimeoutError as XrdTimeoutError
from ..log import get_logger
from ..proto import constants as c
from ..proto import machine as m
from ..proto import requests as r
from ..proto import responses as rp
from ..proto.frames import Payload, Request
from ..transport.base import Transport, coalesce
from ..transport.sync import SocketTransport
from ..url import XRootDURL, parse
from . import deadline as dl
from .bulk import WAITRESP_GRACE, BulkReader, BulkUnsupported

__all__ = ["RedirectRequired", "Result", "Session"]

_log = get_logger(__name__)

_RECV = 1 << 18

#: Machine states in which a session can carry nothing more.
_DEAD = frozenset({m.State.CLOSED, m.State.FAILED})

#: Grace added to a kXR_waitresp delay before the deferred reply is overdue.
_WAITRESP_GRACE = WAITRESP_GRACE

#: How much a TLS link sends between chances for its reader to get in. TLS
#: sends and receives cannot overlap (see :meth:`Session._send_on`).
_TLS_PIECE = 64 << 10

#: The lock of a link whose reads and writes may overlap: none at all.
_NO_LOCK: AbstractContextManager[object] = nullcontext()

#: One request's mailbox: the events filed for it, oldest first.
_Box = list[m.Event]


class RedirectRequired(XRootDError):
    """The server redirected; the caller must re-issue elsewhere."""

    def __init__(self, target: rp.RedirectInfo) -> None:
        """Keep the parsed ``kXR_redirect`` (host, port, opaque) for the router."""
        self.target = target
        super().__init__(f"redirected to {target.url}")


class ExecuteOptions(TypedDict, total=False):
    """The keywords :meth:`Session.execute` takes besides ``path``.

    Spelled once so the router can pass them through typed.
    """

    on_chunk: Callable[[bytes], None] | None
    arrive_on_path: bool


@dataclass(frozen=True, slots=True)
class Result:
    """A completed request."""

    data: bytes
    status: rp.StatusInfo | None = None

    def __len__(self) -> int:
        """Body length, so a result can stand in for the ``bytes`` it carries."""
        return len(self.data)

    def __bytes__(self) -> bytes:
        """The response body, every ``kXR_oksofar`` piece and the final one joined."""
        return self.data


@dataclass(slots=True)
class _AwaitState:
    """The clocks and counters of one request's wait, across resends.

    ``waits`` counts ``kXR_wait`` resends (bounded by ``redirect_limit``),
    ``parked`` the seconds spent parked by the server
    against :attr:`Config.wait_budget`, and ``streamed`` how much of the body
    ``on_chunk`` has already been given, so the final ``Completed`` - which
    carries the whole body - only streams its unseen tail.
    """

    waits: int
    parked: float
    streamed: int
    #: When the stall clock (``Config.stall_deadline``) runs out.
    deadline: float | None
    #: When the caller's own :func:`~xrdclient.deadline` runs out, if it set one.
    expires: float | None = None
    #: When a ``kXR_waitresp`` has waited out :attr:`Config.wait_budget`.
    parked_until: float | None = None


class _Stalled(XrdTimeoutError):
    """A request's stall clock ran out (see :attr:`Config.stall_deadline`).

    Its own clock, not the link's: the reader that hits it gives up its own
    request and costs the connection, but does not fail the other requests
    on it - they may well be getting their answers.
    """


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


def _echo(error: XRootDError) -> XRootDError:
    """A fresh copy of ``error`` for one more of the waiters a failure reaches.

    One exception object raised in several threads at once would have each
    thread's traceback spliced onto it. A timeout stays a timeout, because
    callers fall back differently on one; anything else is the connection
    being gone, which is what it is to every waiter but the one that saw it.
    """
    kind = XrdTimeoutError if isinstance(error, XrdTimeoutError) else XrdConnectionError
    echo = kind(str(error))
    echo.__cause__ = error
    return echo


class Session:
    """An authenticated connection to one XRootD server.

    Safe to share between threads, and between the worker threads
    :mod:`xrdclient.aio` runs calls on: requests from all of them are in
    flight on the connection together (see the module docstring for how).
    """

    def __init__(self, transport: Transport, machine: m.SessionMachine, config: Config) -> None:
        """Adopt a connected ``transport`` and a machine about to start on it.

        Takes ownership of both: :meth:`close` closes the transport. Use
        :meth:`connect`, which also runs the bring-up; this is the seam tests
        use to drive a session over an in-memory pipe.
        """
        self._t = transport
        self._m = machine
        self.config = config
        #: Guards the machine and every attribute below it. Held only for
        #: bookkeeping - never across a wait on the socket, a send, a
        #: ``kXR_wait`` or a caller's callback.
        self._lock = threading.RLock()
        #: Where a waiter sleeps until its mailbox, a link, or the session
        #: changes. Shares :attr:`_lock`.
        self._cond = threading.Condition(self._lock)
        #: Each request's mailbox, by stream id: the very list its waiter
        #: holds, so a stream id reused by a newer request gets a new list
        #: rather than the old one's leftovers.
        self._inbox: dict[int, _Box] = {}
        self._notices: list[rp.AttnInfo] = []
        self._paths: dict[int, Transport] = {}
        #: The links someone is reading right now (0 is the control link).
        self._reading: set[int] = set()
        #: Links shut down while someone was reading them, left for that
        #: reader to close as it lets go (see :meth:`_discard`).
        self._doomed: list[tuple[int, Transport]] = []
        #: Threads asleep on :attr:`_cond`; nobody is woken when it is zero.
        self._parked = 0
        #: Buffers the machine framed, waiting for the wire, in the order they
        #: were framed - which a signing session's sequence numbers depend on.
        self._outq: deque[tuple[int, Transport, list[Payload]]] = deque()
        #: Held by whoever is putting :attr:`_outq` on the wire. Never taken
        #: while holding :attr:`_lock`, so a send stuck behind a full socket
        #: never keeps the link's reader from emptying the other direction.
        self._send_lock = threading.Lock()
        #: A lock per TLS link, around every read and write of its socket: an
        #: OpenSSL connection may not be read and written at the same time.
        self._io_locks: dict[int, threading.Lock] = {}
        if machine.tls_active:
            self._io_locks[0] = threading.Lock()
        #: The thread a :meth:`bulk` reader has lent this connection to.
        self._bulk_owner: int | None = None
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
        cancel.check(f"connecting to {target.host}:{target.port}")
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
        """Drive handshake, ``kXR_protocol``, TLS, login and auth to ``READY``.

        Single-threaded - the session is not yet shared - so this thread is
        the only reader and sender; :meth:`_pump` queues each next request as
        the machine produces it. ``NeedTLS`` is the machine saying the server
        accepted ``kXR_ableTLS``: the socket is upgraded before anything else
        is read. Bounded by the caller's :func:`~xrdclient.deadline` only; the
        socket's ``request_timeout`` bounds each read.
        """
        expires = _expiry()
        while self._m.state not in (m.State.READY, m.State.FAILED, m.State.CLOSED):
            with self._lock:
                events = self._pump(0, None, expires)
            for event in events:
                if isinstance(event, m.NeedTLS):
                    self._upgrade()
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

    def _upgrade(self) -> None:
        """Turn the control link into TLS, in place, mid bring-up."""
        failure = self._flush()
        if failure is not None:
            raise failure
        self._t.start_tls(self._m.host, self.config)
        with self._lock:
            self._m.tls_established()
            self._io_locks[0] = threading.Lock()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def host(self) -> str:
        """The host this session dialled (after any redirect that led here)."""
        return self._m.host

    @property
    def port(self) -> int:
        """The port this session dialled."""
        return self._m.port

    @property
    def endpoint(self) -> str:
        """``host:port``, the form used in every message and as the pool key's core."""
        return f"{self._m.host}:{self._m.port}"

    @property
    def protocol(self) -> object:
        """The parsed ``kXR_protocol`` answer: version, server flags, security needs."""
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
        """The security protocol that authenticated (``gsi``, ``ztn``...), or ``""``."""
        return self._m.mechanism

    @property
    def is_tls(self) -> bool:
        """Whether the control link was upgraded to TLS during bring-up."""
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
        """Whether at least one ``kXR_bind`` data path is still bound."""
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
        when the server would not say and only a trial can tell. Two threads
        asking at once may both ask; the question is idempotent.
        """
        if self._arrives_on_path is None:
            self._ask_arrival_routing()
        return self._arrives_on_path

    def notices(self) -> list[rp.AttnInfo]:
        """Drain any unsolicited ``kXR_attn`` messages the server sent."""
        with self._lock:
            out, self._notices = self._notices, []
        return out

    def note(self, info: rp.AttnInfo) -> None:
        """Keep a notice the bulk reader took off the wire, for :meth:`notices`."""
        with self._lock:
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
        client. It therefore costs a handshake, not a login - and it is paid
        with the session lock let go, so requests already on this session
        carry on meanwhile.
        """
        with self._lock:
            if self.closed:
                raise XrdConnectionError(f"session to {self.endpoint} is closed")
            if not self._m.session_id:
                raise XRootDError(
                    f"{self.endpoint} gave this session no id, so it cannot "
                    f"be bound to a second connection"
                )
            session_id = self._m.session_id
        transport = SocketTransport.connect(self._m.host, self._m.port, self.config)
        machine = m.SessionMachine.data_path(
            self._m, session_id, config=self.config, want_tls=self.is_tls
        )
        try:
            # The bring-up of a data path is a session's worth of work
            # minus the login, so it is driven by a session - which is
            # then thrown away, leaving only the socket.
            bringup = Session(transport, machine, self.config)
            machine.start()
            bringup._bringup()
            with self._lock:
                return self._adopt_path(transport, machine)
        except BaseException:
            transport.close()
            raise

    def _adopt_path(self, transport: Transport, machine: m.SessionMachine) -> int:
        """Take a freshly bound data connection on as one of this session's paths."""
        pathid = machine.pathid
        if self.closed:
            raise XrdConnectionError(f"session to {self.endpoint} closed while binding a path")
        if pathid in self._paths:
            raise ProtocolError(f"{self.endpoint} handed out path id {pathid} twice")
        # A data path carries only bulk transfer, and a caller routing a
        # bound op over it wants to know quickly if the server will not
        # serve it there (so it can fall back to the control link) rather
        # than block for the whole request_timeout. Bound its reads to the
        # short data_stream_timeout; the control link keeps the long one.
        transport.settimeout(self.config.data_stream_timeout)
        self._paths[pathid] = transport
        if machine.tls_active:
            self._io_locks[pathid] = threading.Lock()
        _log.debug("bound data path %d to %s", pathid, self.endpoint)
        return pathid

    def _close_path(
        self, pathid: int, error: XRootDError | None = None, own: _Box | None = None
    ) -> None:
        """Drop data path ``pathid``, failing every request still routed over it.

        The machine forgets those requests without a word, so each waiter is
        sent a failure of its own here; ``own`` - the caller's mailbox, when
        the caller is one of them - is left for the caller to raise in.
        Called with the lock held.
        """
        transport = self._paths.pop(pathid, None)
        if transport is not None:
            lost = error or XrdConnectionError(f"data path {pathid} to {self.endpoint} was closed")
            for sid in self._m.streams_on(pathid):
                box = self._inbox.get(sid)
                if box is not None and box is not own:
                    box.append(m.Failed(sid, None, _echo(lost)))
            self._discard(pathid, transport)
            self._wake()
        self._io_locks.pop(pathid, None)
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

        Other threads' requests on this session go out and come back while
        this one waits; see the module docstring.

        ``on_chunk`` receives the body in pieces as they arrive, including the
        final piece, so concatenating them gives exactly
        :attr:`Result.data` - a caller can stream straight to a file. The
        full body is accumulated for the return value either way. It is
        called on the calling thread with nothing held, so it may block
        without holding up anyone else's request.

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
        pathid = request.pathid
        try:
            if arrive_on_path and pathid in self._paths and self._use_arrival_path(request):
                sid, box = self._start(request, path, arrive=True)
                return self._await_arrival(sid, on_chunk, pathid, box)
            sid, box = self._start(request, path)
            return self._await(sid, on_chunk, pathid if request.reply_on_path else 0, box)
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
        reasons = [
            f"{name} was not used - {skipped[name]}"
            for name in self._IDENTIFYING
            if name in skipped
        ]
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

    def _start(self, request: Request, path: str = "", *, arrive: bool = False) -> tuple[int, _Box]:
        """Submit ``request`` and put it on the wire; its stream id and mailbox.

        The mailbox is registered in the same breath as the stream id is
        taken, so no event for the request can arrive before there is
        somewhere to file it. A request whose caller has been cancelled is
        not sent at all.
        """
        cancel.check(type(request).__name__)
        self._acquire()
        try:
            if self._bulk_owner is not None:
                self._take_turn()
            if self._m.state in _DEAD:
                raise XrdConnectionError(f"session to {self.endpoint} is closed")
            if request.pathid and request.pathid not in self._paths:
                raise ValueError(f"data path {request.pathid} is not bound to {self.endpoint}")
            sid = self._m.submit(request, path=path, arrive_on_path=arrive)
            box: _Box = []
            self._inbox[sid] = box
            self._queue_out()
            # The common case: nobody else is sending. Take the send lock
            # while still holding this one - never blocking for it, so the
            # two locks cannot deadlock - and with it everything queued, in
            # order, so the frame goes out without a second trip through the
            # session lock to fetch it.
            batch = self._take_outq() if self._send_lock.acquire(blocking=False) else None
        finally:
            self._lock.release()
        try:
            # A send that fails is filed in every mailbox it affects -
            # this one's included - so it is raised by the wait that follows.
            if batch is None:
                self._transmit()
            else:
                self._send_owned(batch)
        except BaseException:
            self._forget(sid, box)
            raise
        return sid, box

    def _acquire(self) -> None:
        """Take the session lock, waiting no longer than the caller's deadline allows."""
        left = dl.remaining()
        if left is None:
            self._lock.acquire()
        elif not self._lock.acquire(timeout=max(left, 0.0)):
            raise dl.OperationExpiredError(
                f"the request expired waiting its turn on {self.endpoint}"
            )

    def _take_turn(self) -> None:
        """Wait out a :meth:`bulk` reader that has the wire. Called with the lock held."""
        if self._bulk_owner is None:
            return
        if self._bulk_owner == threading.get_ident():
            raise ProtocolError(
                f"the connection to {self.endpoint} is lent to a bulk transfer on this "
                f"thread; nothing else can use it until that transfer ends"
            )
        expires = _expiry()
        while self._bulk_owner is not None:
            left = None if expires is None else expires - time.monotonic()
            if left is not None and left <= 0:
                raise dl.OperationExpiredError(
                    f"the request expired waiting its turn on {self.endpoint}"
                )
            cancel.check("the request waiting its turn")
            self._park(left)

    def _use_arrival_path(self, request: Request) -> bool:
        """Whether a request that asked to arrive on its data path may."""
        if not request.pathid:
            return False
        if self._arrives_on_path is None:
            self._ask_arrival_routing()
        return self._arrives_on_path is not False and request.pathid in self._paths

    def _await_arrival(
        self, sid: int, on_chunk: Callable[[bytes], None] | None, pathid: int, box: _Box
    ) -> Result:
        """Wait for a request sent *on* data path ``pathid``, settling the trial.

        This is the behavioural probe behind :attr:`arrives_on_path`: an
        answer proves the server routes by arrival; any failure - typically
        the short ``data_stream_timeout`` on a server that answers on the
        control link instead - disproves it for this connection.
        """
        try:
            result = self._await(sid, on_chunk, pathid, box)
        except Exception:
            # The abandoned answer may still arrive on this link, so the path
            # cannot safely carry a later request.
            self._arrives_on_path = False
            with self._lock:
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
            sid, box = self._start(r.Query(c.kXR_Qconfig, "brix.substreams"))
            answer = self._await(sid, None, 0, box).data
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

    def _await(
        self,
        sid: int,
        on_chunk: Callable[[bytes], None] | None,
        pathid: int = 0,
        box: _Box | None = None,
    ) -> Result:
        """Wait for stream ``sid`` to complete and return its result.

        Every exit path takes the mailbox down; one that leaves the answer
        still owed abandons the stream id rather than freeing it.
        """
        if box is None:
            with self._lock:
                box = self._inbox.setdefault(sid, [])
        try:
            return self._answer(sid, on_chunk, pathid, box)
        finally:
            # Given up on with its answer maybe still to come - a stall, a
            # deferred reply over the wait budget, a consumer that raised: the
            # id stays out of circulation until that answer is off the wire,
            # so it can never be taken for a later request's. A completed,
            # failed or redirected stream is released already, and this is
            # a no-op for it.
            self._forget(sid, box, abandon=True)

    def _forget(self, sid: int, box: _Box, *, abandon: bool = False) -> None:
        """Take a finished waiter's mailbox down, and abandon its stream if asked.

        Only while the stream is still this waiter's: once its final answer
        freed the id, a newer request may have it - with a mailbox of its own
        in the inbox - and abandoning it then would strand that request.
        """
        with self._lock:
            if self._inbox.get(sid) is not box:
                return
            del self._inbox[sid]
            if abandon:
                self._m.abandon(sid)
            else:
                self._m.release(sid)

    def _release(self, sid: int, box: _Box) -> None:
        """Free a stream the caller is finished with, if it is still the caller's."""
        with self._lock:
            if self._inbox.get(sid) is box:
                self._m.release(sid)

    def _answer(
        self, sid: int, on_chunk: Callable[[bytes], None] | None, pathid: int, box: _Box
    ) -> Result:
        """Consume ``sid``'s events until ``Completed``, acting on the rest.

        Partial bodies are streamed, ``kXR_wait``/``kXR_waitresp`` handled in
        place, and a redirect or failure raised; this loop never returns
        without a completion.
        """
        # Absolute, over the whole logical operation: see Config.stall_deadline.
        state = _AwaitState(0, 0.0, 0, self._armed(), _expiry())
        while True:
            for event in self._events_until(sid, pathid, state, box):
                if type(event) is m.Completed:
                    # Completed carries the whole body; stream only its unseen tail.
                    if on_chunk is not None and len(event.data) > state.streamed:
                        on_chunk(event.data[state.streamed :])
                    return Result(event.data, event.status)
                self._consume_event(event, sid, on_chunk, state, box)

    def _events_until(self, sid: int, pathid: int, state: _AwaitState, box: _Box) -> list[m.Event]:
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
            return self._events_for(sid, pathid, state.deadline, expires, box)
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
        box: _Box,
    ) -> None:
        """Act on one event for ``sid`` that is not its completion."""
        if isinstance(event, m.Failed):
            raise event.error
        if isinstance(event, m.Chunk):
            if on_chunk is not None:
                on_chunk(event.data)
            state.streamed += len(event.data)
        elif isinstance(event, m.Redirected):
            self._release(sid, box)
            raise RedirectRequired(event.target)
        elif isinstance(event, m.Waiting):
            self._wait_event(event, sid, state, box)

    def _wait_event(self, event: m.Waiting, sid: int, state: _AwaitState, box: _Box) -> None:
        """Handle a ``kXR_wait`` (resend later) or ``kXR_waitresp`` (answer later).

        ``kXR_wait`` is held to two limits: the total time parked
        (:attr:`Config.wait_budget`) and the number of waits
        (``redirect_limit``, reused so that a server bouncing a request for
        ever is cut off by the same knob as a redirect loop).
        A resend restarts the stall clock and the streamed-body count, since
        the server answers the resent request from the beginning.
        """
        if not event.resend:
            self._wait_for_answer(event, sid, state, box)
            return
        # Parking is not stalling, but repeated delays share one budget.
        state.parked += event.seconds
        if state.parked > self.config.wait_budget:
            self._give_up(
                sid,
                box,
                event,
                f"{type(event.request).__name__} was parked for {state.parked:.0f}s, "
                f"over the {self.config.wait_budget:.0f}s budget for one operation",
                state.waits,
            )
        if state.expires is not None and time.monotonic() + event.seconds >= state.expires:
            # XrdCl's rule: a resend that would go out after the request has
            # expired is not waited for - it expires now.
            self._release(sid, box)
            raise dl.OperationExpiredError(
                f"{type(event.request).__name__} expired: the server asked for "
                f"{event.seconds:.0f}s more than the deadline left"
            )
        state.waits += 1
        if state.waits > self.config.redirect_limit:
            self._give_up(
                sid,
                box,
                event,
                f"server kept asking to wait for {type(event.request).__name__}",
                state.waits,
            )
        _log.debug("server asked to wait %.1fs: %s", event.seconds, event.message)
        if self._resend_after(event.seconds, sid, box):
            state.streamed = 0  # the body starts over on the resend
            state.deadline = self._armed()

    def _resend_after(self, seconds: float, sid: int, box: _Box) -> bool:
        """Sit out a ``kXR_wait`` holding nothing, then send the request again.

        ``False`` when something was filed for the request meanwhile - the
        connection failing under it - and nothing was resent: the caller's
        next look in its mailbox raises it. :meth:`close` cuts the wait short,
        and so does a cancellation - which releases the stream id rather than
        abandoning it: ``kXR_wait`` was the server's last word on it, so no
        late reply is coming to free it.
        """
        end = time.monotonic() + seconds
        with self._lock:
            while not box and self._m.state not in _DEAD:
                left = end - time.monotonic()
                if left <= 0:
                    break
                if cancel.cancelled():
                    self._release(sid, box)
                    cancel.check("a request waiting to be resent")
                self._park(left)
            if box:
                return False
            if self._m.state in _DEAD:
                raise XrdConnectionError(
                    f"session to {self.endpoint} closed while a request waited to be resent"
                )
            self._m.resume(sid)
            self._queue_out()
        self._transmit()
        return True

    def _wait_for_answer(self, event: m.Waiting, sid: int, state: _AwaitState, box: _Box) -> None:
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
                box,
                event,
                f"{type(event.request).__name__} has already waited out the "
                f"{budget:.0f}s budget for one operation",
                state.waits,
            )
        state.deadline = self._armed(0 if allowed == float("inf") else allowed)
        if allowed != float("inf"):
            state.parked += allowed
            state.parked_until = time.monotonic() + allowed

    def _give_up(
        self, sid: int, box: _Box, event: m.Waiting, message: str, attempts: int
    ) -> NoReturn:
        """Stop waiting on ``sid``, freeing it when nothing more will come.

        ``kXR_wait`` is the server's last word on a request until it is sent
        again, so its id can be reused at once. A ``kXR_waitresp`` promises an
        answer later; :meth:`_await` keeps that id out of circulation until
        the answer is off the wire.
        """
        if event.resend:
            self._release(sid, box)
        raise WaitLimitError(message, attempts=attempts)

    # ------------------------------------------------------------------
    # Waiting: one reader per link, everyone else follows
    # ------------------------------------------------------------------

    def _events_for(
        self,
        sid: int,
        pathid: int = 0,
        deadline: float | None = None,
        expires: float | None = None,
        box: _Box | None = None,
    ) -> list[m.Event]:
        """Block until at least one event for ``sid`` is available.

        ``pathid`` says which link the answer is expected on: a read that
        named a data path is answered there, and waiting on the control link
        for it would wait forever. ``deadline`` is the whole operation's, so
        a peer that dribbles cannot renew it one byte at a time; ``expires``
        is the caller's :func:`~xrdclient.deadline`.

        Whoever finds the link without a reader reads it (:meth:`_lead`) for
        as long as its own mailbox stays empty; anyone else sleeps until it
        is woken (:meth:`_follow`).
        """
        with self._lock:
            if box is None:
                box = self._inbox.setdefault(sid, [])
            led = False
            try:
                while not box:
                    self._check_link(pathid)
                    if pathid in self._reading:
                        self._follow(pathid, deadline, expires)
                    else:
                        led = True
                        self._lead(pathid, deadline, expires, box)
            finally:
                if led:
                    # The role is free again: whoever was waiting for it
                    # takes it over.
                    self._wake()
            events = box[:]
            box.clear()
            return events

    def _check_link(self, pathid: int) -> None:
        """Raise if the answer is no longer wanted, or its link can no longer bring it."""
        cancel.check("the request")
        if self._m.state in _DEAD:
            raise XrdConnectionError(f"session to {self.endpoint} is closed")
        if pathid and pathid not in self._paths:
            raise XrdConnectionError(f"data path {pathid} to {self.endpoint} is closed")

    def _follow(self, pathid: int, deadline: float | None, expires: float | None) -> None:
        """Sleep while another thread reads the link, no longer than our own clocks."""
        limit, expiring = _sooner(deadline, expires)
        left = None
        if limit is not None:
            left = limit - time.monotonic()
            if left <= 0:
                if not expiring and not pathid:
                    self._broken = True
                raise self._out_of_time(expiring)
        self._park(left)

    def _park(self, timeout: float | None) -> None:
        """Sleep on the condition until woken or ``timeout`` passes.

        Inside a cancel scope, no longer than :data:`~xrdcore.cancel.POLL`
        at a time: every caller loops back to look at its scope, so a
        cancelled waiter needs no wake-up from the thread that cancelled it.
        """
        self._parked += 1
        try:
            self._cond.wait(cancel.bounded(timeout))
        finally:
            self._parked -= 1

    def _wake(self) -> None:
        """Wake every sleeper to look again - only if there is one."""
        if self._parked:
            self._cond.notify_all()

    def _lead(self, pathid: int, deadline: float | None, expires: float | None, box: _Box) -> None:
        """Take one turn as ``pathid``'s reader and file what it brought in."""
        self._reading.add(pathid)
        try:
            events = self._pump(pathid, deadline, expires)
        except dl.OperationExpiredError:
            # The caller's time ran out, not the connection's: nothing was
            # taken off the wire, and the request's stream id is abandoned
            # by whoever was waiting on it.
            raise
        except _Stalled as exc:
            self._stalled_out(pathid, exc, box)
            raise
        except (XrdConnectionError, XrdTimeoutError) as exc:
            self._link_failed(pathid, exc, box)
            raise
        finally:
            self._let_go(pathid)
        woken = False
        for event in events:
            woken = self._route_event(event, box) or woken
        if woken:
            self._wake()

    def _discard(self, pathid: int, transport: Transport) -> None:
        """Shut a link down now, and close it as soon as nobody is reading it.

        Shutting it down wakes a reader blocked on it to an end of stream.
        Closing the descriptor as well, at once, can lose that wakeup - on
        macOS a ``poll`` whose descriptor is closed while it is being woken
        goes back to sleep - so a link with a reader is closed by the reader,
        as it lets go of the link (:meth:`_let_go`). Lock held.
        """
        transport.interrupt()
        if pathid in self._reading:
            self._doomed.append((pathid, transport))
        else:
            transport.close()

    def _let_go(self, pathid: int) -> None:
        """Give up the reader role on ``pathid``, closing what was shut down under it."""
        self._reading.discard(pathid)
        if not self._doomed:
            return
        doomed, self._doomed = self._doomed, []
        for link, transport in doomed:
            if link in self._reading:
                self._doomed.append((link, transport))
            else:
                transport.close()

    def _stalled_out(self, pathid: int, error: XRootDError, own: _Box) -> None:
        """The reader's own stall clock ran out: that costs the link, not its users.

        A data path that went quiet takes only itself down: the control
        link, and every handle opened on it, is as good as it was, and
        calling the session broken would have its files skip their closes
        and the pool drop it.
        """
        if pathid:
            self._close_path(pathid, error, own)
        else:
            self._broken = True

    def _link_failed(self, pathid: int, error: XRootDError, own: _Box | None) -> None:
        """A link failed under its reader: fail everyone waiting on it.

        The reader raises ``error`` itself; every other waiter is sent a copy.
        A failed control link takes the whole session with it.
        """
        if pathid:
            if pathid in self._paths:
                self._close_path(pathid, error, own)
            return
        self._broken = True
        self._fail_all(error, own)

    def _fail_all(self, error: XRootDError, own: _Box | None) -> None:
        """Put a copy of ``error`` in every mailbox but ``own``, and wake them all."""
        for sid, box in self._inbox.items():
            if box is not own:
                box.append(m.Failed(sid, None, _echo(error)))
        self._wake()

    def _route_event(self, event: m.Event, own: _Box) -> bool:
        """File one event where it belongs; whether anyone else needs waking.

        ``own`` is the reader's mailbox. An event for a stream nobody is
        waiting on is kept for whoever asks for that stream later.
        """
        target = getattr(event, "streamid", None)
        if target is not None:
            box = self._inbox.get(target)
            if box is None:
                box = self._inbox[target] = []
            box.append(event)
            return box is not own
        if isinstance(event, m.Attention):
            self._notices.append(event.info)
            return False
        if isinstance(event, m.PathLost):
            self._close_path(event.pathid, XrdConnectionError(event.reason), own)
            return True
        if isinstance(event, m.Disconnected):
            self._session_lost(XrdConnectionError(f"connection lost: {event.reason}"), own)
            return True
        if isinstance(event, m.Failed):
            # Belongs to no stream: the session as a whole has failed.
            self._session_lost(event.error, own)
            return True
        return False

    def _session_lost(self, error: XRootDError, own: _Box) -> None:
        """The session is gone: the reader gets ``error``, everyone else a copy."""
        own.append(m.Failed(None, None, error))
        self._fail_all(error, own)

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def _flush(self) -> XRootDError | None:
        """Put everything the machine has framed on the wire now.

        Must not be called holding the lock. A failure is filed with every
        request it affects; the control link's is returned as well, for a
        caller - the bring-up - that has no request to file it with.
        """
        with self._lock:
            self._queue_out()
        return self._transmit()

    def _queue_out(self) -> None:
        """Move what the machine has framed onto the send queue. Lock held."""
        machine = self._m
        if machine.has_data_to_send:
            self._outq.append((0, self._t, machine.buffers_to_send()))
        if not machine.has_path_data:
            return
        for pathid, transport in self._paths.items():
            queued = machine.path_buffers_to_send(pathid)
            if queued:
                self._outq.append((pathid, transport, queued))

    def _transmit(self) -> XRootDError | None:
        """Send the queue, in order; never called holding the lock.

        Whoever holds the send lock sends everything queued, other threads'
        frames included, so a frame is never overtaken by one framed after
        it. A thread that finds the lock taken waits for it and then finds
        its own frame already gone.
        """
        with self._send_lock:
            return self._drain_outq()

    def _drain_outq(self) -> XRootDError | None:
        """Send queued buffers until none are left. The send lock is held."""
        failure: XRootDError | None = None
        while True:
            with self._lock:
                batch = self._take_outq()
            if not batch:
                return failure
            failure = self._send_batch(batch) or failure

    def _take_outq(self) -> list[tuple[int, Transport, list[Payload]]]:
        """Everything queued to send, in order, leaving the queue empty. Lock held."""
        batch = list(self._outq)
        self._outq.clear()
        return batch

    def _send_owned(self, batch: list[tuple[int, Transport, list[Payload]]]) -> None:
        """Send ``batch``, then whatever was queued meanwhile; releases the send lock."""
        try:
            self._send_batch(batch)
            if self._outq:
                self._drain_outq()
        finally:
            self._send_lock.release()

    def _send_batch(self, batch: list[tuple[int, Transport, list[Payload]]]) -> XRootDError | None:
        """Send queued items in order; the control link's failure, if it had one."""
        failure: XRootDError | None = None
        for pathid, transport, buffers in batch:
            if failure is not None and not pathid:
                continue  # the control link is gone; its failure is filed already
            try:
                self._send_on(pathid, transport, buffers)
            except (XrdConnectionError, XrdTimeoutError) as exc:
                with self._lock:
                    if self._send_failed(pathid, transport, exc):
                        failure = failure or exc
        return failure

    def _send_failed(self, pathid: int, transport: Transport, error: XRootDError) -> bool:
        """File a failed send with the requests it costs; whether it was the control link's."""
        if pathid:
            if self._paths.get(pathid) is transport:
                self._close_path(pathid, error)
            return False
        self._broken = True
        self._outq = deque(item for item in self._outq if item[0])
        self._fail_all(error, None)
        return True

    def _send_on(self, pathid: int, transport: Transport, buffers: Sequence[Payload]) -> None:
        """Send ``buffers`` on one link.

        A plain socket is written while its reader reads, which is what keeps
        a large write from deadlocking against a large reply streaming in the
        other way. TLS cannot overlap the two, so its sends take the link's
        lock in record-sized pieces, waiting for room on the wire between
        them holding nothing - giving the reader its turn meanwhile.
        """
        lock = self._io_locks.get(pathid)
        if lock is None:
            transport.send_buffers(buffers)
            return
        resting = self._resting(pathid)
        for piece in coalesce(buffers, _TLS_PIECE):
            transport.wait_writable(resting)
            with lock:
                transport.send_buffers([piece])

    def _resting(self, pathid: int) -> float:
        """How long one read of the link may wait for anything at all."""
        return self.config.data_stream_timeout if pathid else self.config.request_timeout

    def _receive(
        self, pathid: int, deadline: float | None, expires: float | None = None
    ) -> bytes | None:
        """Read once from ``pathid``'s link, never past ``deadline`` or ``expires``.

        Called with the lock held, which is let go while the link is waited
        on and read (:meth:`_read_unlocked`). ``deadline`` is the stall
        clock, and running into it means the request has gone on too long:
        a :class:`~xrdclient.errors.TimeoutError` that costs the connection.
        ``expires`` is the caller's own deadline, and running into that costs
        only the request - :class:`~xrdclient.session.deadline.OperationExpiredError`.
        A wait that timed out has taken nothing off the wire, so the link is
        still in step. ``b""`` is the end of the stream; ``None`` is the link
        having been closed under the reader, by this side, while it waited.
        """
        transport = self._paths.get(pathid) if pathid else self._t
        if transport is None:
            raise XrdConnectionError(f"data path {pathid} to {self.endpoint} is closed")
        wait, cut, expiring = self._read_window(pathid, deadline, expires)
        try:
            data = self._read_unlocked(pathid, transport, wait if cut else None)
        except (XrdConnectionError, XrdTimeoutError):
            if not self._current(pathid, transport):
                return None
            raise
        if not self._current(pathid, transport):
            return None
        if data is not None:
            return data
        if cut:
            raise self._out_of_time(expiring)
        raise XrdTimeoutError(f"read from {self.endpoint} timed out")

    def _read_window(
        self, pathid: int, deadline: float | None, expires: float | None
    ) -> tuple[float, bool, bool]:
        """How long one read may wait: ``(seconds, cut short, by the caller's clock)``.

        The link's own resting timeout, unless one of the request's clocks
        runs out first - in which case the wait is cut to that, and running
        out of it is the request's error rather than the link's. Raises at
        once if a clock has run out already.
        """
        wait = self._resting(pathid)
        limit, expiring = _sooner(deadline, expires)
        if limit is None:
            return wait, False, expiring
        left = limit - time.monotonic()
        if left <= 0:
            raise self._out_of_time(expiring)
        if left < wait:
            return left, True, expiring
        return wait, False, expiring

    def _read_unlocked(self, pathid: int, transport: Transport, wait: float | None) -> bytes | None:
        """Read what the link has, the lock let go; ``None`` if ``wait`` ran out.

        ``wait`` is set when a request's clock is shorter than the socket's
        own timeout, and the link is then waited on first for no longer than
        that. Otherwise a plain socket is simply read, blocking within its
        own timeout as it always has - one system call per turn rather than
        two - and :meth:`close` wakes it by shutting the socket down. A TLS
        link is always waited on first: its read holds the link's lock, which
        its senders need, so it must not block in it. So is any link read
        inside a cancel scope, in slices (:meth:`_readable`), because a read
        blocked in ``recv`` could only be woken by shutting the link down.

        Anything still queued to send goes first: during the bring-up the
        reader is the only thread, and the next request is its to send.
        """
        outgoing = bool(self._outq)
        lock = self._io_locks.get(pathid)
        self._lock.release()
        try:
            if outgoing:
                failure = self._transmit()
                if failure is not None:
                    raise failure
            if wait is None and (lock is not None or cancel.current() is not None):
                wait = self._resting(pathid)
            if wait is not None and not self._readable(transport, wait):
                return None
            with lock or _NO_LOCK:
                return transport.receive(_RECV)
        finally:
            self._lock.acquire()

    @staticmethod
    def _readable(transport: Transport, wait: float) -> bool:
        """Wait up to ``wait`` for ``transport`` to have something to read.

        Inside a cancel scope the wait is taken in slices, looking at the
        scope between them; a cancellation raises having taken nothing off
        the wire, so the link is still in step for whoever reads it next.
        """
        end = time.monotonic() + wait
        while not transport.wait_readable(cancel.bounded(wait)):
            wait = end - time.monotonic()
            if wait <= 0:
                return False
            cancel.check("the request")
        return True

    def _current(self, pathid: int, transport: Transport) -> bool:
        """Whether a link read while the lock was let go is still this session's."""
        if self._m.state in _DEAD:
            return False
        return not pathid or self._paths.get(pathid) is transport

    def _out_of_time(self, expired: bool) -> XrdTimeoutError:
        """The error for running out of time: the caller's deadline, or a stall."""
        if expired:
            return dl.OperationExpiredError(f"no answer from {self.endpoint} before the deadline")
        return _Stalled(self._stalled())

    def _stalled(self) -> str:
        """The message of a stall-deadline timeout, naming the deadline."""
        return (
            f"{self.endpoint} did not finish answering within the "
            f"{self.config.stall_deadline:.0f}s stall deadline"
        )

    def _pump(
        self, pathid: int = 0, deadline: float | None = None, expires: float | None = None
    ) -> list[m.Event]:
        """One send/receive turn; returns whatever events it produced.

        Called with the lock held, by the link's reader.
        """
        if self._m.state is not m.State.READY:
            # The bring-up: each reply the machine takes may queue the next
            # request. A ready session's requests are queued by their senders.
            self._queue_out()
        data = self._receive(pathid, deadline, expires)
        if data is None:
            # Closed under the reader, not by the peer: nothing to feed.
            return []
        self._m.receive_data(data, pathid=pathid)
        return self._m.drain()

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

        The reader frames and parses its own requests, so it must have the
        wire to itself, explicitly: it takes the control link's reader role
        and the send lock for the whole block, and while it has them every
        other request on this session waits its turn (within its own
        :func:`~xrdclient.deadline`). The machine must be idle when it starts:
        a reader that framed its own requests while an ordinary one was in
        flight would take the other's reply off the wire. That is why the
        reader is given out by a context manager rather than returned.

        A reader whose caller's :func:`~xrdclient.deadline` runs out leaves the
        connection in step - it stops between frames and hands the replies
        still owed back to the machine to drop - so, as on the event path,
        running out of time costs the request and not the connection.
        """
        with self._held():
            # One thread opening a second reader inside its first would put
            # two sets of reads on the wire with nothing to tell their
            # replies apart.
            if self._bulk_owner == threading.get_ident():
                raise ProtocolError(
                    "this connection is already lent to a bulk read; "
                    "one reader owns the wire at a time"
                )
            self._take_turn()
            if not self._m.idle() or self._reading:
                # Something is still owed an answer on this wire - a request
                # in flight, or one given up on whose reply has not arrived
                # yet - so the event path, which knows to drop it, carries
                # this one.
                raise BulkUnsupported(
                    "a bulk read needs the connection to itself, and this one "
                    "still has requests outstanding"
                )
            self._bulk_owner = threading.get_ident()
            self._reading.add(0)
            self._queue_out()
        try:
            with self._send_lock:
                self._drain_outq()
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
        except cancel.Cancelled:
            # Cancelled between frames, the reader left the wire in step and
            # its owed replies to the machine. Part-way through a frame it
            # marked the connection broken instead: nothing can find the next
            # header on this wire, so it is closed now - every request waiting
            # its turn fails over to a fresh connection - rather than left
            # for the next request to misread.
            if self._broken:
                self.close()
            raise
        finally:
            with self._lock:
                self._bulk_owner = None
                self._let_go(0)
                self._wake()

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def close(self) -> None:
        """End the session and drop the connection.

        Never waits on another thread's request: one blocked on the wire is
        woken by the link being shut down under it, one sitting out a
        ``kXR_wait`` by the condition it sleeps on, and both fail with a
        :class:`~xrdclient.errors.ConnectionError`. The ``kXR_endsess`` is
        sent only if the wire is free to take it at once.
        """
        with self._lock:
            graceful = self._m.state is m.State.READY and self._bulk_owner is None
            if graceful:
                try:
                    self._m.close()
                    self._queue_out()
                except XRootDError:
                    graceful = False
            self._m.state = m.State.CLOSED
        if graceful and self._send_lock.acquire(blocking=False):
            try:
                self._drain_outq()
            finally:
                self._send_lock.release()
        with self._lock:
            self._discard(0, self._t)
            for pathid, transport in self._paths.items():
                self._discard(pathid, transport)
            self._paths.clear()
            self._outq.clear()
            self._cond.notify_all()

    def __enter__(self) -> Session:
        """Use the session as a context manager; :meth:`close` on exit."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the session, also when the block raised."""
        self.close()

    def __repr__(self) -> str:
        """Endpoint, machine state, TLS, auth mechanism and bound data paths."""
        paths = f", {len(self._paths)} data path" if self._paths else ""
        if len(self._paths) > 1:
            paths += "s"
        return (
            f"Session({self.endpoint}, {self._m.state.name.lower()}"
            f"{', tls' if self.is_tls else ''}"
            f"{', ' + self._m.mechanism if self._m.mechanism else ''}{paths})"
        )
