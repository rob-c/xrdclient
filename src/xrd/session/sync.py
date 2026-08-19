"""The blocking session driver.

One :class:`Session` is one connection: it owns a transport and a
:class:`~xrd.proto.machine.SessionMachine`, and turns the machine's events
into ordinary Python returns and exceptions. Redirects are *not* followed
here - a redirect means a different server, which is a policy decision the
caller makes; :class:`Session` reports it as :class:`RedirectRequired`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .._compat import SLOTS
from .._log import get_logger
from ..config import Config
from ..errors import ConnectionError as XrdConnectionError
from ..errors import ProtocolError, WaitLimitError, XRootDError
from ..errors import TimeoutError as XrdTimeoutError
from ..proto import constants as c
from ..proto import machine as m
from ..proto import requests as r
from ..proto import responses as rp
from ..proto.frames import Request
from ..transport.base import Transport
from ..transport.sync import SocketTransport
from ..url import XRootDURL, parse

__all__ = ["Session", "Result", "RedirectRequired"]

_log = get_logger(__name__)

_RECV = 1 << 18

#: Grace added to a kXR_waitresp delay before the deferred reply is overdue.
_WAITRESP_GRACE = 30.0


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
        transport = SocketTransport.connect(target.host, target.port, config)
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
        while self._m.state not in (m.State.READY, m.State.FAILED, m.State.CLOSED):
            for event in self._pump():
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
    def mechanism(self) -> str:
        return self._m.mechanism

    @property
    def is_tls(self) -> bool:
        return self._m.tls_active

    @property
    def closed(self) -> bool:
        return self._m.state in (m.State.CLOSED, m.State.FAILED)

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
        :attr:`~xrd.Config.data_stream_timeout`.
        """
        return self._arrives_on_path

    def notices(self) -> list[rp.AttnInfo]:
        """Drain any unsolicited ``kXR_attn`` messages the server sent."""
        out, self._notices = self._notices, []
        return out

    # ------------------------------------------------------------------
    # Data paths
    # ------------------------------------------------------------------

    def bind_data_path(self) -> int:
        """Open a second connection to this server and bind it to the session.

        Returns the path id the server assigned. Pass it to a request that
        takes one - :class:`~xrd.proto.requests.Read`, ``ReadV``, ``PgRead``,
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
                    raise ProtocolError(
                        f"{self.endpoint} handed out path id {pathid} twice"
                    )
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
        with self._lock:
            if self.closed:
                raise XrdConnectionError(f"session to {self.endpoint} is closed")
            if request.pathid and request.pathid not in self._paths:
                raise ValueError(
                    f"data path {request.pathid} is not bound to {self.endpoint}"
                )
            if arrive_on_path and self._arrives_on_path is None and request.pathid:
                self._ask_arrival_routing()
            on_path = (
                arrive_on_path
                and self._arrives_on_path is not False
                and bool(request.pathid)
                and request.pathid in self._paths
            )
            sid = self._m.submit(request, path=path, arrive_on_path=on_path)
            if on_path:
                answers_on = request.pathid
            else:
                answers_on = request.pathid if request.reply_on_path else 0
            if not on_path:
                return self._await(sid, on_chunk, answers_on)
            try:
                result = self._await(sid, on_chunk, answers_on)
            except Exception:
                # This server does not answer where it was asked. Remember it
                # for the whole connection - the next file would otherwise pay
                # the same timeout to learn the same thing - and let the socket
                # go, because the answer to the request just abandoned may
                # still arrive on it and would be read as the answer to
                # whatever is asked next.
                self._arrives_on_path = False
                self._close_path(request.pathid)
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
        :attr:`~xrd.Config.data_stream_timeout`. A query that itself fails
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

    def _await(
        self, sid: int, on_chunk: Callable[[bytes], None] | None, pathid: int = 0
    ) -> Result:
        waits = 0
        parked = 0.0
        streamed = 0
        # Absolute, over the whole logical operation: see Config.stall_deadline.
        deadline = self._armed()
        while True:
            for event in self._events_for(sid, pathid, deadline):
                if isinstance(event, m.Completed):
                    # The machine's Completed carries the whole body; hand the
                    # streaming caller only the tail it has not seen yet.
                    if on_chunk is not None and len(event.data) > streamed:
                        on_chunk(event.data[streamed:])
                    return Result(event.data, event.status)
                if isinstance(event, m.Failed):
                    raise event.error
                if isinstance(event, m.Chunk):
                    if on_chunk is not None:
                        on_chunk(event.data)
                    streamed += len(event.data)
                elif isinstance(event, m.Redirected):
                    self._m.release(sid)
                    raise RedirectRequired(event.target)
                elif isinstance(event, m.Waiting):
                    # Parking is not stalling, so the deadline restarts - but
                    # the delays are summed, because a server that answers
                    # every resend with another wait is stalling after all.
                    parked += event.seconds
                    if parked > self.config.wait_budget:
                        raise WaitLimitError(
                            f"{type(event.request).__name__} was parked for "
                            f"{parked:.0f}s, over the {self.config.wait_budget:.0f}s "
                            f"budget for one operation",
                            attempts=waits,
                        )
                    if not event.resend:
                        # "The answer is coming later": nothing is re-sent, a
                        # deferral is not a retry, but the deadline has to
                        # cover the delay the server named.
                        deadline = self._armed(event.seconds)
                        continue
                    waits += 1
                    if waits > self.config.redirect_limit:
                        raise WaitLimitError(
                            f"server kept asking to wait for "
                            f"{type(event.request).__name__}",
                            attempts=waits,
                        )
                    _log.debug("server asked to wait %.1fs: %s", event.seconds, event.message)
                    time.sleep(event.seconds)
                    self._m.resume(sid)
                    self._flush()
                    streamed = 0  # the body starts over on the resend
                    deadline = self._armed()

    # ------------------------------------------------------------------
    # I/O pump
    # ------------------------------------------------------------------

    def _flush(self) -> None:
        data = self._m.data_to_send()
        if data:
            self._t.send(data)
        for pathid, transport in self._paths.items():
            queued = self._m.path_data_to_send(pathid)
            if queued:
                transport.send(queued)

    def _receive(self, pathid: int, deadline: float | None) -> bytes:
        """Read once from ``pathid``'s link, never past ``deadline``."""
        transport = self._paths[pathid] if pathid else self._t
        resting = self.config.data_stream_timeout if pathid else self.config.request_timeout
        if deadline is None:
            return transport.receive(_RECV)
        left = deadline - time.monotonic()
        if left <= 0:
            raise XrdTimeoutError(self._stalled())
        if left >= resting:
            # The socket's own timeout already comes in first; leave it be,
            # so a data path keeps the short one its fallback depends on.
            return transport.receive(_RECV)
        transport.settimeout(left)
        try:
            return transport.receive(_RECV)
        except XrdTimeoutError as exc:
            raise XrdTimeoutError(self._stalled()) from exc
        finally:
            transport.settimeout(resting)

    def _stalled(self) -> str:
        return (
            f"{self.endpoint} did not finish answering within the "
            f"{self.config.stall_deadline:.0f}s stall deadline"
        )

    def _pump(self, pathid: int = 0, deadline: float | None = None) -> list[m.Event]:
        """One send/receive turn; returns whatever events it produced."""
        self._flush()
        self._m.receive_data(self._receive(pathid, deadline), pathid=pathid)
        return list(self._m.events())

    def _events_for(
        self, sid: int, pathid: int = 0, deadline: float | None = None
    ) -> list[m.Event]:
        """Block until at least one event for ``sid`` is available.

        ``pathid`` says which link the answer is expected on: a read that
        named a data path is answered there, and waiting on the control link
        for it would wait forever. ``deadline`` is the whole operation's, so
        a peer that dribbles cannot renew it one byte at a time.
        """
        queued = self._inbox.pop(sid, None)
        if queued:
            return queued
        while True:
            mine: list[m.Event] = []
            for event in self._pump(pathid, deadline):
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
            if mine:
                return mine

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

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
