"""The sans-io session state machine.

:class:`SessionMachine` owns every bit of protocol state for one connection -
bring-up, stream multiplexing, partial responses, waits, redirects and
signing - and performs no I/O whatsoever. Drivers feed it bytes and drain its
outbox:

    machine = SessionMachine(host="example.org", config=cfg)
    machine.start()
    while True:
        sock.sendall(machine.data_to_send())
        machine.receive_data(sock.recv(65536))
        while (event := machine.next_event()) is not None:
            ...

That contract is what lets the blocking and the asyncio front ends share one
implementation, and what makes the protocol testable without a socket.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING

from .._compat import SLOTS
from .._log import get_logger
from ..config import Config
from ..crypto.sigver import Signer
from ..errors import (
    AuthenticationError,
    NoMechanismError,
    ProtocolError,
    ServerError,
    XRootDError,
    raise_for_status,
)
from ..errors import ConnectionError as XrdConnectionError
from ..types import ProtocolInfo
from . import constants as c
from . import requests as r
from . import responses as rp
from .frames import HANDSHAKE, Request, ResponseHeader, decode_header, encode

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ..auth.base import Credential

__all__ = [
    "State",
    "SessionMachine",
    "Event",
    "Negotiated",
    "NeedTLS",
    "Ready",
    "Completed",
    "Chunk",
    "Redirected",
    "Waiting",
    "Attention",
    "Failed",
    "PathLost",
    "Disconnected",
]

_log = get_logger(__name__)

#: Streamids reserved for bring-up; regular traffic starts above these.
_SID_HANDSHAKE, _SID_PROTOCOL, _SID_LOGIN, _SID_AUTH = 0, 1, 2, 3
_SID_BIND = 2
_FIRST_SID = 4


class State(IntEnum):
    """Where a connection is in its lifecycle."""

    NEW = 0
    HANDSHAKE = 1
    PROTOCOL = 2
    TLS = 3
    LOGIN = 4
    AUTH = 5
    READY = 6
    FAILED = 7
    CLOSED = 8
    #: Waiting for ``kXR_bind`` to answer. Only a data connection is ever
    #: here; it takes the place of LOGIN and AUTH, which it skips.
    BIND = 9


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


class Event:
    """Base of everything :meth:`SessionMachine.next_event` yields."""

    __slots__ = ()


@dataclass(frozen=True, **SLOTS)
class Negotiated(Event):
    """``kXR_protocol`` answered; capabilities are known."""

    info: ProtocolInfo


@dataclass(frozen=True, **SLOTS)
class NeedTLS(Event):
    """The driver must upgrade the socket, then call :meth:`tls_established`."""

    reason: str = ""


@dataclass(frozen=True, **SLOTS)
class Ready(Event):
    """Login and authentication are complete; requests may be submitted."""

    session_id: bytes
    mechanism: str = ""


@dataclass(frozen=True, **SLOTS)
class Completed(Event):
    """A request finished successfully."""

    streamid: int
    request: Request
    data: bytes
    status: rp.StatusInfo | None = None


@dataclass(frozen=True, **SLOTS)
class Chunk(Event):
    """A partial response body; more will follow on the same stream."""

    streamid: int
    request: Request
    data: bytes


@dataclass(frozen=True, **SLOTS)
class Redirected(Event):
    """The server handed this request off elsewhere."""

    streamid: int
    request: Request
    target: rp.RedirectInfo


@dataclass(frozen=True, **SLOTS)
class Waiting(Event):
    """The server asked for a retry after a delay.

    ``resend`` distinguishes ``kXR_wait`` (the driver must sleep and call
    :meth:`resume`) from ``kXR_waitresp`` (the answer will simply arrive
    later, unsolicited, on the same stream).
    """

    streamid: int
    request: Request
    seconds: float
    message: str = ""
    resend: bool = True


@dataclass(frozen=True, **SLOTS)
class Attention(Event):
    """An unsolicited ``kXR_attn`` that was not an embedded response."""

    info: rp.AttnInfo


@dataclass(frozen=True, **SLOTS)
class Failed(Event):
    """A request, or the session bring-up, failed."""

    streamid: int | None
    request: Request | None
    error: XRootDError


@dataclass(frozen=True, **SLOTS)
class PathLost(Event):
    """A bound data connection went away.

    The session survives: only the requests routed over that path failed,
    and each of those gets its own :class:`Failed`.
    """

    pathid: int
    reason: str = ""


@dataclass(frozen=True, **SLOTS)
class Disconnected(Event):
    """The peer closed, or the machine was closed locally."""

    reason: str = ""


# --------------------------------------------------------------------------
# Per-stream state
# --------------------------------------------------------------------------


@dataclass(**SLOTS)
class _Pending:
    request: Request
    frame: bytes
    buffer: bytearray = field(default_factory=bytearray)
    status: rp.StatusInfo | None = None
    path: str = ""
    pathid: int = 0
    path_bytes: bytes = b""
    #: Most bytes this reply may accumulate; 0 for no limit.
    cap: int = 0


@dataclass(**SLOTS)
class _Framer:
    """One link's inbound cursor.

    There is one per connection, not one per session: frames from a bound
    data path interleave with the control link's on nobody's schedule, so a
    single buffer would splice two half-frames together.
    """

    buffer: bytearray = field(default_factory=bytearray)
    header: ResponseHeader | None = None
    need_trailer: int = 0
    trailer_for: int | None = None


class SessionMachine:
    """Protocol state for one connection. Not thread-safe by itself; the
    session wrapper serialises access."""

    def __init__(
        self,
        *,
        host: str = "",
        port: int = c.DEFAULT_PORT,
        config: Config | None = None,
        credentials: Iterator[Credential] | None = None,
        username: str = "",
        want_tls: bool = False,
        bind_to: bytes = b"",
    ) -> None:
        self.host = host
        self.port = port
        self.config = config or Config()
        self.username = username or self.config.username
        self.want_tls = want_tls or self.config.require_tls
        #: Session id to attach to with ``kXR_bind`` instead of logging in.
        self.bind_to = bind_to
        #: The path id the server gave this connection, once bound.
        self.pathid = 0

        self.state = State.NEW
        self.protocol_info = ProtocolInfo()
        self.session_id = b""
        self.mechanism = ""
        self.signer: Signer | None = None
        self.tls_active = False

        self._out = bytearray()
        self._out_path: dict[int, bytearray] = {}
        self._events: list[Event] = []
        self._pending: dict[int, _Pending] = {}
        self._free: list[int] = []
        self._next_sid = _FIRST_SID

        # Inbound framing cursor, one per link.
        self._framers: dict[int, _Framer] = {0: _Framer()}

        # Authentication ladder.
        self._credentials = credentials
        self._credential: Credential | None = None
        self._auth_rejected: dict[str, str] = {}
        self._offered: list[str] = []

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Queue the handshake, pipelined with ``kXR_protocol``."""
        if self.state is not State.NEW:
            raise ProtocolError(f"start() called in state {self.state.name}")
        flags = c.kXR_secreqs | c.kXR_ableTLS | (c.kXR_wantTLS if self.want_tls else 0)
        self._out += HANDSHAKE
        self._out += encode(r.Protocol(flags), _SID_PROTOCOL)
        self.state = State.HANDSHAKE

    def data_to_send(self) -> bytes:
        """Drain and return everything queued for the control link."""
        data = bytes(self._out)
        del self._out[:]
        return data

    def path_data_to_send(self, pathid: int) -> bytes:
        """Drain and return everything queued for a bound data path."""
        queued = self._out_path.pop(pathid, None)
        return bytes(queued) if queued else b""

    @property
    def has_data_to_send(self) -> bool:
        return bool(self._out)

    def submit(
        self, request: Request, *, path: str = "", arrive_on_path: bool = False
    ) -> int:
        """Queue ``request`` on a fresh stream and return its streamid.

        ``path`` is carried only so that a failure can name the file it was
        about; it never reaches the wire. ``arrive_on_path`` routes the whole
        frame down the request's bound data socket (see :meth:`_send`).
        """
        if self.state is not State.READY:
            raise ProtocolError(f"cannot submit in state {self.state.name}")
        sid = self._acquire_sid()
        self._send(request, sid, path=path, arrive_on_path=arrive_on_path)
        return sid

    def resume(self, streamid: int) -> None:
        """Re-send a request the server answered with ``kXR_wait``."""
        pending = self._pending.get(streamid)
        if pending is None:
            raise ProtocolError(f"stream {streamid} is not waiting")
        self._out += pending.frame
        if pending.path_bytes:
            self._out_path.setdefault(pending.pathid, bytearray()).extend(pending.path_bytes)

    def release(self, streamid: int) -> None:
        """Abandon a stream - after a redirect, or when the caller gives up."""
        if self._pending.pop(streamid, None) is not None and streamid >= _FIRST_SID:
            self._free.append(streamid)

    def close(self, *, graceful: bool = True) -> None:
        """Queue ``kXR_endsess`` and mark the machine closed."""
        if self.state is State.READY and graceful and self.session_id:
            self._out += encode(r.EndSession(self.session_id), self._acquire_sid())
        self.state = State.CLOSED
        self._events.append(Disconnected("closed by client"))

    def tls_established(self) -> None:
        """Tell the machine the socket is now encrypted; continue bring-up."""
        if self.state is not State.TLS:
            raise ProtocolError(f"tls_established() called in state {self.state.name}")
        self.tls_active = True
        self._begin_login()

    # -- internals ------------------------------------------------------

    def _acquire_sid(self) -> int:
        if self._free:
            return self._free.pop()
        sid = self._next_sid
        self._next_sid += 1
        if self._next_sid > 0xFFFF:
            self._next_sid = _FIRST_SID
        if sid in self._pending:
            raise ProtocolError("stream id space exhausted")
        return sid

    def _send(
        self, request: Request, sid: int, *, path: str = "", arrive_on_path: bool = False
    ) -> None:
        frame = encode(request, sid)
        if self.signer is not None:
            signed = self.signer.sign(frame)
            if signed is not None:
                seqno, signature, nodata = signed
                sigver = r.Sigver(request.opcode, seqno, signature, nodata=nodata)
                frame = encode(sigver, sid) + frame
        data = request.path_data()
        # Arrival routing (BriX and any server that keys a sub-stream on which
        # connection a request *arrived* on): the whole frame goes down the
        # bound data socket rather than the control link, and the answer comes
        # back on the same socket. The standard split - header on the control
        # link, payload on the data socket - is what runs otherwise.
        if arrive_on_path and request.pathid:
            self._pending[sid] = _Pending(
                request,
                frame,
                path=path,
                pathid=request.pathid,
                path_bytes=data,
                cap=request.reply_cap(),
            )
            buf = self._out_path.setdefault(request.pathid, bytearray())
            buf.extend(frame)
            if data:
                buf.extend(data)
            return
        self._pending[sid] = _Pending(
            request,
            frame,
            path=path,
            pathid=request.pathid,
            path_bytes=data,
            cap=request.reply_cap(),
        )
        self._out += frame
        if data:
            self._out_path.setdefault(request.pathid, bytearray()).extend(data)

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    def receive_data(self, data: bytes | None, *, pathid: int = 0) -> None:
        """Feed bytes from one link. ``None`` or ``b""`` signals its EOF.

        ``pathid`` names the bound data path the bytes came off; 0 is the
        control link. Losing a data path costs only the requests routed over
        it, so its EOF is not the session's.
        """
        framer = self._framers.setdefault(pathid, _Framer())
        if not data:
            self._on_eof() if pathid == 0 else self._on_path_eof(pathid)
            return
        framer.buffer += data
        self._parse(framer)

    def next_event(self) -> Event | None:
        """The oldest undelivered event, or ``None``."""
        return self._events.pop(0) if self._events else None

    def events(self) -> Iterator[Event]:
        """Drain every pending event."""
        while self._events:
            yield self._events.pop(0)

    @property
    def in_flight(self) -> int:
        return len(self._pending)

    def _on_eof(self) -> None:
        if self.state in (State.CLOSED, State.FAILED):
            return
        self.state = State.CLOSED
        for sid, pending in list(self._pending.items()):
            self._events.append(
                Failed(sid, pending.request, XrdConnectionError("connection closed by peer"))
            )
        self._pending.clear()
        self._events.append(Disconnected("connection closed by peer"))

    def forget_path(self, pathid: int) -> None:
        """Drop a data path and every stream that was waiting on it.

        A socket that has been closed cannot answer what went down it, so the
        requests still expecting an answer there are abandoned rather than
        left in flight forever - and their stream ids go back for re-use, as
        they do after a redirect. Anything queued for the path is dropped with
        it: it was never sent, and the path it was addressed to is gone.
        """
        for sid, pending in list(self._pending.items()):
            if pending.pathid == pathid:
                self.release(sid)
        self._framers.pop(pathid, None)
        self._out_path.pop(pathid, None)

    def _on_path_eof(self, pathid: int) -> None:
        reason = f"data path {pathid} closed by peer"
        for sid, pending in list(self._pending.items()):
            if pending.pathid == pathid:
                self._events.append(Failed(sid, pending.request, XrdConnectionError(reason)))
        self.forget_path(pathid)
        self._events.append(PathLost(pathid, reason))

    def _parse(self, framer: _Framer) -> None:
        buf = framer.buffer
        while True:
            if framer.need_trailer:
                if len(buf) < framer.need_trailer:
                    return
                # Through a memoryview, because bytes(bytearray_slice) copies
                # twice: once to build the slice and once to freeze it. On a
                # multi-megabyte read that second copy is measurable.
                trailer = bytes(memoryview(buf)[: framer.need_trailer])
                del buf[: framer.need_trailer]
                framer.need_trailer = 0
                sid = framer.trailer_for
                framer.trailer_for = None
                assert sid is not None
                self._on_status_data(sid, trailer)
                continue

            if framer.header is None:
                if len(buf) < c.RESPONSE_HDRLEN:
                    return
                framer.header = decode_header(buf[: c.RESPONSE_HDRLEN])
                del buf[: c.RESPONSE_HDRLEN]

            header = framer.header
            if len(buf) < header.dlen:
                return
            body = bytes(memoryview(buf)[: header.dlen])
            del buf[: header.dlen]
            framer.header = None
            self._dispatch(header, body, framer)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, header: ResponseHeader, body: bytes, framer: _Framer) -> None:
        if header.status == c.kXR_attn:
            inner = self._unwrap_attn(body)
            if inner is None:
                return
            header, body = inner

        if self.state in (State.HANDSHAKE, State.PROTOCOL, State.LOGIN, State.AUTH, State.BIND):
            self._bringup(header, body)
            return

        pending = self._pending.get(header.streamid)
        if pending is None:
            _log.debug("response on unknown stream %d (%s)", header.streamid,
                       c.status_name(header.status))
            return
        self._on_response(header, body, pending, framer)

    def _unwrap_attn(self, body: bytes) -> tuple[ResponseHeader, bytes] | None:
        """Unpack a ``kXR_asynresp``, or record the notice and return None."""
        info = rp.parse_attn(body)
        if info.action == c.kXR_asynresp and len(body) >= 16:
            return decode_header(body[8:16]), body[16:]
        self._events.append(Attention(info))
        return None

    # -- bring-up -------------------------------------------------------

    def _bringup(self, header: ResponseHeader, body: bytes) -> None:
        if header.status == c.kXR_error:
            info = rp.parse_error(body)
            self._fail(ServerError(info.code, info.message))
            return
        if header.status not in (c.kXR_ok, c.kXR_authmore):
            self._fail(
                ProtocolError(
                    f"unexpected {c.status_name(header.status)} during "
                    f"{self.state.name.lower()}"
                )
            )
            return

        if self.state is State.HANDSHAKE:
            self.state = State.PROTOCOL
            return

        if self.state is State.PROTOCOL:
            self.protocol_info = rp.parse_protocol(body)
            self._events.append(Negotiated(self.protocol_info))
            self._after_protocol()
            return

        if self.state is State.BIND:
            self.pathid = rp.parse_bind(body)
            self._become_ready()
            return

        if self.state is State.LOGIN:
            login = rp.parse_login(body)
            self.session_id = login.sessid
            self._offered = list(login.mechanisms)
            if not login.sec:
                self._become_ready()
            else:
                self.state = State.AUTH
                self._next_credential(login.sec)
            return

        # State.AUTH
        if header.status == c.kXR_authmore:
            self._auth_step(body)
        else:
            self._become_ready()

    def _after_protocol(self) -> None:
        flags = self.protocol_info.flags
        # ``kXR_tlsData`` belongs here with the session-wide bits: this client
        # reads and writes on the connection it logged in on, so a server that
        # wants file data encrypted wants this socket encrypted.
        demanded = bool(
            flags & (c.kXR_gotoTLS | c.kXR_tlsLogin | c.kXR_tlsSess | c.kXR_tlsData)
        )
        if self.want_tls or demanded:
            if not flags & c.kXR_haveTLS:
                self._fail(
                    ProtocolError(
                        f"TLS required but {self.host}:{self.port} does not offer it "
                        f"(flags 0x{flags:08x})"
                    )
                )
                return
            self.state = State.TLS
            self._events.append(NeedTLS("server requested TLS" if demanded else "client policy"))
            return
        self._begin_login()

    def _begin_login(self) -> None:
        if self.bind_to:
            # A data connection never logs in: it says which session it
            # belongs to and inherits that session's identity wholesale.
            self.state = State.BIND
            self._out += encode(r.Bind(self.bind_to), _SID_BIND)
            return
        self.state = State.LOGIN
        self._out += encode(r.Login(self.username), _SID_LOGIN)

    def _next_credential(self, sec: str) -> None:
        """Advance the ladder and send the next mechanism's first blob."""
        if self._credentials is None:
            from ..auth import select

            self._credentials = select(
                sec,
                self.config,
                username=self.username,
                host=self.host,
                rejected=self._auth_rejected,
                tls=self.tls_active,
            )
        for cred in self._credentials:
            try:
                blob = cred.initial()
            except Exception as exc:
                self._auth_rejected[cred.name] = f"{type(exc).__name__}: {exc}"
                continue
            self._credential = cred
            self.mechanism = cred.name
            self._out += encode(r.Auth(cred.name, blob), _SID_AUTH)
            return
        self._fail(NoMechanismError(offered=self._offered, tried=self._auth_rejected))

    def _auth_step(self, challenge: bytes) -> None:
        cred = self._credential
        if cred is None:
            self._fail(AuthenticationError("server sent kXR_authmore with no exchange open"))
            return
        try:
            blob = cred.step(challenge)
        except Exception as exc:
            self._auth_rejected[cred.name] = f"{type(exc).__name__}: {exc}"
            blob = None
        if blob is None:
            self._auth_rejected.setdefault(
                cred.name, "server asked for another round the mechanism cannot answer"
            )
            self._next_credential("")
            return
        self._out += encode(r.Auth(cred.name, blob), _SID_AUTH)

    def _become_ready(self) -> None:
        self.state = State.READY
        key = self._credential.session_key if self._credential else None
        if key:
            self.signer = Signer(
                key,
                self.protocol_info.security_level,
                self.protocol_info.security_overrides,
                secodata=bool(self.protocol_info.security_options & c.kXR_secOData),
            )
        self._events.append(Ready(self.session_id, self.mechanism))

    def _fail(self, error: XRootDError) -> None:
        self.state = State.FAILED
        self._events.append(Failed(None, None, error))

    # -- request responses ----------------------------------------------

    def _on_response(
        self, header: ResponseHeader, body: bytes, pending: _Pending, framer: _Framer
    ) -> None:
        sid = header.streamid
        status = header.status

        if status == c.kXR_ok:
            if not self._within_cap(sid, pending, len(body)):
                return
            if pending.buffer:
                # Extend and freeze, rather than concatenating and freezing:
                # the latter copies the whole accumulated response twice.
                pending.buffer += body
                data = bytes(pending.buffer)
            else:
                data = body
            self.release(sid)
            self._events.append(Completed(sid, pending.request, data, pending.status))

        elif status == c.kXR_oksofar:
            if not self._within_cap(sid, pending, len(body)):
                return
            pending.buffer += body
            self._events.append(Chunk(sid, pending.request, body))

        elif status == c.kXR_error:
            info = rp.parse_error(body)
            self.release(sid)
            self._events.append(
                Failed(sid, pending.request, _server_error(info, pending.path))
            )

        elif status == c.kXR_redirect:
            self._events.append(Redirected(sid, pending.request, rp.parse_redirect(body)))

        elif status == c.kXR_wait:
            wait = rp.parse_wait(body)
            self._events.append(
                Waiting(sid, pending.request, min(wait.seconds, self.config.wait_cap), wait.message)
            )

        elif status == c.kXR_waitresp:
            later = rp.parse_waitresp(body)
            self._events.append(
                Waiting(sid, pending.request, later.seconds, resend=False)
            )

        elif status == c.kXR_status:
            state = rp.parse_status(body)
            pending.status = state
            if state.dlen:
                framer.need_trailer = state.dlen
                framer.trailer_for = sid
            else:
                self._on_status_data(sid, b"")

        else:
            self.release(sid)
            self._events.append(
                Failed(
                    sid,
                    pending.request,
                    ProtocolError(f"unexpected response status {c.status_name(status)}"),
                )
            )

    def _within_cap(self, sid: int, pending: _Pending, extra: int) -> bool:
        """Whether ``extra`` more bytes still fit the reply's declared size.

        Over it, the stream is finished off with an error: the request said
        how much it wanted, so the surplus can only be a server that has lost
        track of the answer, and buffering it is how a client is talked into
        exhausting its own memory.
        """
        if not pending.cap or len(pending.buffer) + extra <= pending.cap:
            return True
        self.release(sid)
        self._events.append(
            Failed(
                sid,
                pending.request,
                ProtocolError(
                    f"{pending.request!r} was answered with more than the "
                    f"{pending.cap} bytes it asked for"
                ),
            )
        )
        return False

    def _on_status_data(self, sid: int, data: bytes) -> None:
        pending = self._pending.get(sid)
        if pending is None or pending.status is None:
            return
        if not self._within_cap(sid, pending, len(data)):
            return
        info = pending.status
        if info.is_final:
            payload = bytes(pending.buffer + data) if pending.buffer else data
            self.release(sid)
            self._events.append(Completed(sid, pending.request, payload, info))
        else:
            pending.buffer += data
            self._events.append(Chunk(sid, pending.request, data))

    def __repr__(self) -> str:
        return (
            f"SessionMachine({self.host}:{self.port}, state={self.state.name}, "
            f"in_flight={len(self._pending)}, tls={self.tls_active})"
        )


def _server_error(info: rp.ErrorInfo, path: str) -> XRootDError:
    try:
        raise_for_status(info.code, info.message, path=path or None)
    except XRootDError as exc:
        return exc
    return ServerError(info.code, info.message, path=path or None)
