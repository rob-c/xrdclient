"""The sans-io session state machine.

:class:`SessionMachine` owns every bit of protocol state for one connection -
bring-up, stream multiplexing, partial responses, waits, redirects and
signing - and performs no I/O whatsoever. Drivers feed it bytes and drain its
outbox:

    machine = SessionMachine(host="example.org", config=cfg)
    machine.start()
    while True:
        sock.sendmsg(machine.buffers_to_send())   # or sendall(data_to_send())
        machine.receive_data(sock.recv(65536))
        while (event := machine.next_event()) is not None:
            ...

That contract is what lets the blocking and the asyncio front ends share one
implementation, and what makes the protocol testable without a socket.

Where to start reading: :meth:`SessionMachine.start` and :meth:`_bringup` for
the connection lifecycle (handshake -> ``kXR_protocol`` -> optional TLS ->
``kXR_login`` or ``kXR_bind`` -> ``kXR_auth`` rounds -> READY), then
:meth:`submit` / :meth:`_send` for the outbound half and :meth:`receive_data`
-> :meth:`_parse` -> :meth:`_dispatch` -> the ``_response_*`` handlers for the
inbound half. The drivers live in :mod:`xrdclient.session` (blocking) and
:mod:`xrdclient.aio`; neither adds protocol logic.

Invariants a maintainer must not break:

* A stream id is never reused while an answer to its previous request may
  still arrive (``_pending``, ``_retired`` and ``_leased`` are all checked
  before an id is handed out). XRootD matches replies to requests by stream
  id alone, so a reused id silently delivers the wrong data.
* Events are appended in wire order and drained oldest first; drivers rely
  on a ``Chunk`` never overtaking the ``Completed`` of the same stream.
* Every reply body is bounded: by ``MAX_RESPONSE_BODY`` per frame (checked
  in :func:`~xrdclient.proto.frames.header_fields`) and by the request's
  :meth:`~xrdclient.proto.frames.Request.reply_cap` across ``kXR_oksofar``
  chunks, so a hostile server cannot make the client buffer without limit.
* Nothing here blocks, sleeps or reads a clock: ``kXR_wait`` becomes a
  :class:`Waiting` event and the driver does the sleeping.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING

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
from ..log import get_logger
from ..types import ProtocolInfo
from . import constants as c
from . import requests as r
from . import responses as rp
from .frames import HANDSHAKE, Payload, Request, encode, encode_parts, header_fields

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Iterator

    from ..auth.base import Credential

__all__ = [
    "Attention",
    "Chunk",
    "Completed",
    "Disconnected",
    "Event",
    "Failed",
    "NeedTLS",
    "Negotiated",
    "PathLost",
    "Ready",
    "Redirected",
    "SessionMachine",
    "State",
    "Waiting",
    "server_error",
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


@dataclass(frozen=True, slots=True)
class Negotiated(Event):
    """``kXR_protocol`` answered; capabilities are known."""

    info: ProtocolInfo


@dataclass(frozen=True, slots=True)
class NeedTLS(Event):
    """The driver must upgrade the socket, then call :meth:`tls_established`."""

    reason: str = ""


@dataclass(frozen=True, slots=True)
class Ready(Event):
    """Login and authentication are complete; requests may be submitted."""

    session_id: bytes
    mechanism: str = ""


@dataclass(frozen=True, slots=True)
class Completed(Event):
    """A request finished successfully."""

    streamid: int
    request: Request
    data: bytes
    status: rp.StatusInfo | None = None


@dataclass(frozen=True, slots=True)
class Chunk(Event):
    """A partial response body; more will follow on the same stream."""

    streamid: int
    request: Request
    data: bytes


@dataclass(frozen=True, slots=True)
class Redirected(Event):
    """The server handed this request off elsewhere."""

    streamid: int
    request: Request
    target: rp.RedirectInfo


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class Attention(Event):
    """An unsolicited ``kXR_attn`` that was not an embedded response."""

    info: rp.AttnInfo


@dataclass(frozen=True, slots=True)
class Failed(Event):
    """A request, or the session bring-up, failed."""

    streamid: int | None
    request: Request | None
    error: XRootDError


@dataclass(frozen=True, slots=True)
class PathLost(Event):
    """A bound data connection went away.

    The session survives: only the requests routed over that path failed,
    and each of those gets its own :class:`Failed`.
    """

    pathid: int
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Disconnected(Event):
    """The peer closed, or the machine was closed locally."""

    reason: str = ""


# --------------------------------------------------------------------------
# Per-stream state
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Pending:
    """Everything the machine remembers about one stream owed an answer.

    Created by :meth:`SessionMachine._send` and dropped by
    :meth:`SessionMachine.release` when a final reply (or a failure) ends the
    stream. ``buffer`` accumulates ``kXR_oksofar`` / partial ``kXR_status``
    bodies until the final one; ``status`` holds the ``kXR_status`` header
    whose raw data is still arriving as a trailer.
    """

    request: Request
    #: The frame as it went out, buffer by buffer, for a ``kXR_wait`` resend.
    parts: list[Payload]
    buffer: bytearray = field(default_factory=bytearray)
    status: rp.StatusInfo | None = None
    path: str = ""
    pathid: int = 0
    path_bytes: Payload = b""
    #: Most bytes this reply may accumulate; 0 for no limit.
    cap: int = 0


@dataclass(slots=True)
class _Framer:
    """One link's inbound cursor.

    There is one per connection, not one per session: frames from a bound
    data path interleave with the control link's on nobody's schedule, so a
    single buffer would splice two half-frames together.
    """

    buffer: bytearray = field(default_factory=bytearray)
    #: The header of the frame whose body is still arriving: its body
    #: length, or -1 while no header has been read.
    dlen: int = -1
    sid: int = 0
    status: int = 0
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
        username: str = "",
        want_tls: bool = False,
    ) -> None:
        """A machine for one connection to ``host:port``, not yet started.

        ``username`` and ``want_tls`` default to the config's
        (``config.username``, ``config.require_tls``); ``want_tls`` can only
        add TLS, never take away a requirement the config makes. A data
        connection that joins an existing session instead of logging in is
        made with :meth:`data_path`.
        """
        self.host = host
        self.port = port
        self.config = config or Config()
        self.username = username or self.config.username
        self.want_tls = want_tls or self.config.require_tls
        #: Session id to attach to with ``kXR_bind`` instead of logging in;
        #: empty for a control connection (see :meth:`data_path`).
        self.bind_to = b""
        #: The path id the server gave this connection, once bound.
        self.pathid = 0

        self.state = State.NEW
        self.protocol_info = ProtocolInfo()
        #: The protocol version the server gave in its handshake reply: what
        #: XrdCl reports as a host's protocol (``XRootDQuery::ProtocolVersion``).
        self.handshake_version = 0
        self.session_id = b""
        self.mechanism = ""
        self.signer: Signer | None = None
        self.tls_active = False

        #: What is queued for the control link, as the buffers it was framed
        #: in: a write's payload stays the caller's view until the driver's
        #: vectored send hands the lot to the kernel in one call.
        self._out: list[Payload] = []
        self._out_path: dict[int, list[Payload]] = {}
        self._events: list[Event] = []
        self._pending: dict[int, _Pending] = {}
        #: Ids free for reuse, oldest first: an id goes back to the end of the
        #: line, so the one just given up is the last to be handed out again.
        self._free: deque[int] = deque()
        #: Ids whose request was given up on while an answer may still come;
        #: held out of the pool until it does, and whatever arrives is dropped.
        self._retired: set[int] = set()
        #: Ids handed to the bulk reader, which frames its own requests; held
        #: out of the pool until it gives them back.
        self._leased: set[int] = set()
        self._next_sid = _FIRST_SID

        # Inbound framing cursor, one per link.
        self._framers: dict[int, _Framer] = {0: _Framer()}

        # Authentication ladder. The credentials are chosen lazily, from the
        # mechanisms the server offers (see :meth:`_next_credential`).
        self._credentials: Iterator[Credential] | None = None
        self._credential: Credential | None = None
        self._auth_rejected: dict[str, str] = {}
        self._offered: list[str] = []
        #: The last refusal a server sent mid-exchange, kept to be the
        #: session's error should every mechanism after it fail too.
        self._auth_refusal: ServerError | None = None

    @classmethod
    def data_path(
        cls, primary: SessionMachine, session_id: bytes, *, config: Config, want_tls: bool
    ) -> SessionMachine:
        """A machine for a second connection to ``primary``'s session.

        It reaches the same endpoint as the same user, and on :meth:`start`
        sends ``kXR_bind`` with ``session_id`` (the id the server gave
        ``primary`` at login) instead of logging in - which is how XrdCl's
        data streams attach. ``config`` is the owning session's (normally
        ``primary.config`` too). ``want_tls`` is the primary's actual TLS
        state, so a data path is never in the clear when its session is not.
        """
        machine = cls(
            host=primary.host,
            port=primary.port,
            config=config,
            username=primary.username,
            want_tls=want_tls,
        )
        machine.bind_to = session_id
        return machine

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Queue the handshake, pipelined with ``kXR_protocol``."""
        if self.state is not State.NEW:
            raise ProtocolError(f"start() called in state {self.state.name}")
        flags = c.kXR_secreqs | c.kXR_ableTLS | (c.kXR_wantTLS if self.want_tls else 0)
        self._out.append(HANDSHAKE)
        self._out.append(encode(r.Protocol(flags), _SID_PROTOCOL))
        self.state = State.HANDSHAKE

    def data_to_send(self) -> bytes:
        """Drain and return everything queued for the control link, joined."""
        return b"".join(self.buffers_to_send())

    def buffers_to_send(self) -> list[Payload]:
        """Drain everything queued for the control link, as separate buffers.

        In wire order. A write's payload is still the view the caller handed
        in, so a driver that sends the list with one vectored call
        (``socket.sendmsg``) never copies it; one that cannot joins them.
        """
        out, self._out = self._out, []
        return out

    def path_data_to_send(self, pathid: int) -> bytes:
        """Drain and return everything queued for a bound data path, joined."""
        return b"".join(self.path_buffers_to_send(pathid))

    def path_buffers_to_send(self, pathid: int) -> list[Payload]:
        """Drain everything queued for a bound data path, as separate buffers."""
        return self._out_path.pop(pathid, None) or []

    @property
    def has_data_to_send(self) -> bool:
        """Whether anything is queued for the control link.

        A driver checks this after every :meth:`receive_data`: replies such
        as ``kXR_authmore`` make the machine queue a request of its own.
        """
        return bool(self._out)

    @property
    def has_path_data(self) -> bool:
        """Whether anything is queued for a bound data path.

        The driver asks before visiting each path, because on the common
        request nothing is, and asking once is cheaper than asking per path.
        """
        return bool(self._out_path)

    def submit(self, request: Request, *, path: str = "", arrive_on_path: bool = False) -> int:
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
        self._out.extend(pending.parts)
        if pending.path_bytes:
            self._out_path.setdefault(pending.pathid, []).append(pending.path_bytes)

    def release(self, streamid: int) -> None:
        """Abandon a stream - after a redirect, or when the caller gives up."""
        if self._pending.pop(streamid, None) is not None and streamid >= _FIRST_SID:
            self._free.append(streamid)

    def abandon(self, streamid: int) -> None:
        """Give up on a stream whose answer may still be on its way.

        Unlike :meth:`release`, the id is not reused until that answer has
        arrived: a late reply matched to a newer request on the same id is
        the wrong data returned as if it were right. Whatever does arrive is
        dropped, and a final reply puts the id back in the pool.
        """
        if self._pending.pop(streamid, None) is not None:
            self._retired.add(streamid)

    def close(self, *, graceful: bool = True) -> None:
        """Queue ``kXR_endsess`` and mark the machine closed."""
        if self.state is State.READY and graceful and self.session_id:
            self._out.append(encode(r.EndSession(self.session_id), self._acquire_sid()))
        self.state = State.CLOSED
        self._events.append(Disconnected("closed by client"))

    def tls_established(self) -> None:
        """Tell the machine the socket is now encrypted; continue bring-up."""
        if self.state is not State.TLS:
            raise ProtocolError(f"tls_established() called in state {self.state.name}")
        self.tls_active = True
        self._begin_login()

    # -- internals ------------------------------------------------------

    # ------------------------------------------------------------------
    # Bulk data plane
    # ------------------------------------------------------------------

    def idle(self) -> bool:
        """Whether nothing is outstanding, so a caller may take the wire.

        The bulk reader frames and parses its own requests to keep a gigabyte
        out of the event path, which is only safe while this machine is not
        waiting for an answer of its own.
        """
        return self.state is State.READY and not self._pending and not self._retired

    def lease_sids(self, count: int) -> list[int]:
        """Reserve ``count`` stream ids for a caller that frames its own requests.

        The ids are taken out of circulation exactly as a submitted request's
        would be, so nothing this machine sends later can collide with a bulk
        read still in flight. Give them back with :meth:`release_sids`.
        """
        if count < 1:
            raise ValueError("lease_sids needs a positive count")
        leased = []
        for _ in range(count):
            sid = self._acquire_sid()
            self._leased.add(sid)
            leased.append(sid)
        return leased

    def release_sids(self, sids: Iterable[int]) -> None:
        """Return ids from :meth:`lease_sids` to the pool."""
        for sid in sids:
            self._leased.discard(sid)
            self._free.append(sid)

    def retire_sids(self, sids: Iterable[int], owed: Collection[int]) -> None:
        """Return leased ids whose caller gave up, some still owed a reply.

        The ones in ``owed`` are abandoned as :meth:`abandon` abandons a
        submitted request's: kept out of circulation until their reply has
        arrived and been dropped. The rest go straight back to the pool.
        """
        for sid in sids:
            self._leased.discard(sid)
            if sid in owed:
                self._retired.add(sid)
            else:
                self._free.append(sid)

    def frame_for(self, request: Request, sid: int) -> bytes:
        """The exact bytes ``request`` would go out as on ``sid``.

        Signing is applied here when the server asked for it, so a bulk read
        that bypasses :meth:`submit` is still signed the way the session
        negotiated. Nothing is recorded as pending: the caller is reading the
        answer itself.
        """
        return b"".join(self.frame_parts_for(request, sid))

    def frame_parts_for(self, request: Request, sid: int) -> list[Payload]:
        """:meth:`frame_for` as separate buffers, a write's payload uncopied."""
        if sid not in self._leased:
            raise ProtocolError(f"stream id {sid} was not leased for bulk use")
        return self._frame(request, sid)

    def _frame(self, request: Request, sid: int) -> list[Payload]:
        """``request`` on ``sid``, behind its ``kXR_sigver`` when it needs one.

        As buffers in wire order (see :func:`~xrdclient.proto.frames.encode_parts`).
        A signature covers the frame whole, so a signed request is joined to
        be signed - the one case where a write's payload is copied.
        """
        parts = encode_parts(request, sid)
        if self.signer is None:
            return parts
        frame = b"".join(parts)
        signed = self.signer.sign(frame)
        if signed is None:
            return [frame]
        seqno, signature, nodata = signed
        sigver = r.Sigver(request.opcode, seqno, signature, nodata=nodata)
        return [encode(sigver, sid) + frame]

    def _acquire_sid(self) -> int:
        """A stream id no request, lease or late reply can still claim.

        Freed ids are reused oldest first; otherwise the counter advances,
        wrapping past ``0xFFFF`` (the field is 16 bits) back to the first id
        above the bring-up range. If the id it lands on is still in use, all
        65 532 are taken and the session cannot multiplex another request.
        """
        if self._free:
            return self._free.popleft()
        sid = self._next_sid
        self._next_sid += 1
        if self._next_sid > 0xFFFF:
            self._next_sid = _FIRST_SID
        if sid in self._pending or sid in self._leased or sid in self._retired:
            raise ProtocolError("stream id space exhausted")
        return sid

    def _send(
        self, request: Request, sid: int, *, path: str = "", arrive_on_path: bool = False
    ) -> None:
        """Frame ``request`` on ``sid``, record it as pending, and queue it.

        The pending record keeps the framed buffers so that a ``kXR_wait``
        can resend exactly what went out, and the request's ``path_data``
        so that it goes down the bound socket again too.
        """
        parts = self._frame(request, sid)
        data = request.path_data()
        pathid = request.pathid
        self._pending[sid] = _Pending(
            request, parts, path=path, pathid=pathid, path_bytes=data, cap=request.reply_cap()
        )
        # Arrival routing (BriX and any server that keys a sub-stream on which
        # connection a request *arrived* on): the whole frame goes down the
        # bound data socket rather than the control link, and the answer comes
        # back on the same socket. The standard split - header on the control
        # link, payload on the data socket - is what runs otherwise.
        if arrive_on_path and pathid:
            queue = self._out_path.setdefault(pathid, [])
            queue.extend(parts)
            if data:
                queue.append(data)
            return
        self._out.extend(parts)
        if data:
            self._out_path.setdefault(pathid, []).append(data)

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    def receive_data(self, data: bytes | None, *, pathid: int = 0) -> None:
        """Feed bytes from one link. ``None`` or ``b""`` signals its EOF.

        ``pathid`` names the bound data path the bytes came off; 0 is the
        control link. Losing a data path costs only the requests routed over
        it, so its EOF is not the session's.
        """
        framer = self._framers.get(pathid)
        if framer is None:
            framer = self._framers[pathid] = _Framer()
        if not data:
            self._on_eof() if pathid == 0 else self._on_path_eof(pathid)
            return
        if not self._whole_frame(framer, data):
            framer.buffer += data
            self._parse(framer)

    def _whole_frame(self, framer: _Framer, data: bytes) -> bool:
        """Dispatch ``data`` straight off the wire when it is one whole frame.

        Which is what nearly every read of a request-at-a-time caller gets:
        one reply, all of it. Buffering it first would copy the body twice
        for nothing. Anything else - a frame split across reads, several in
        one, a trailer owed - goes through the buffer and :meth:`_parse`.
        """
        if framer.buffer or framer.dlen >= 0 or framer.need_trailer or len(data) < 8:
            return False
        sid, status, dlen = header_fields(data)
        if len(data) != c.RESPONSE_HDRLEN + dlen:
            return False
        self._dispatch(sid, status, data[c.RESPONSE_HDRLEN :], framer)
        return True

    def next_event(self) -> Event | None:
        """The oldest undelivered event, or ``None``."""
        return self._events.pop(0) if self._events else None

    def events(self) -> Iterator[Event]:
        """Drain every pending event."""
        while self._events:
            yield self._events.pop(0)

    def drain(self) -> list[Event]:
        """Every pending event at once, oldest first, leaving none."""
        out, self._events = self._events, []
        return out

    @property
    def in_flight(self) -> int:
        """How many submitted requests are still owed a final answer.

        Leased and retired ids are not counted: they belong to the bulk
        reader or to replies nobody is waiting for.
        """
        return len(self._pending)

    def streams_on(self, pathid: int) -> list[int]:
        """The streams still owed an answer that were routed over ``pathid``.

        What a driver closing that path must fail: :meth:`forget_path` frees
        their ids silently, and whoever is waiting on one would wait forever.
        """
        return [sid for sid, pending in self._pending.items() if pending.pathid == pathid]

    def _on_eof(self) -> None:
        """The control link closed: fail every pending stream, then the session.

        Each waiter gets its own :class:`Failed` before the final
        :class:`Disconnected`, so a driver can resolve futures one by one and
        then decide whether to reconnect. A second EOF is ignored.
        """
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
        """A bound data path closed: fail its streams only, then report :class:`PathLost`."""
        reason = f"data path {pathid} closed by peer"
        for sid, pending in list(self._pending.items()):
            if pending.pathid == pathid:
                self._events.append(Failed(sid, pending.request, XrdConnectionError(reason)))
        self.forget_path(pathid)
        self._events.append(PathLost(pathid, reason))

    def _parse(self, framer: _Framer) -> None:
        """Dispatch every complete frame in ``framer``'s buffer; keep the remainder.

        Three things can be owed, in this order: the raw trailer of a
        ``kXR_status`` reply (its ``dlen`` counted only the status block, the
        data follows uncounted), a frame header, or the body that header
        announced. Returning early leaves the cursor exactly where the next
        :meth:`receive_data` must resume.
        """
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

            if framer.dlen < 0:
                if len(buf) < c.RESPONSE_HDRLEN:
                    return
                framer.sid, framer.status, framer.dlen = header_fields(buf)
                del buf[: c.RESPONSE_HDRLEN]

            dlen = framer.dlen
            if len(buf) < dlen:
                return
            body = bytes(memoryview(buf)[:dlen])
            del buf[:dlen]
            framer.dlen = -1
            self._dispatch(framer.sid, framer.status, body, framer)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, sid: int, status: int, body: bytes, framer: _Framer) -> None:
        """Route one complete frame to bring-up or to its stream's handler.

        A ``kXR_attn`` carrying ``kXR_asynresp`` is unwrapped first: it is the
        late answer to a ``kXR_waitresp``, embedded with its own header, and
        is then handled as if it had arrived directly on that stream. A reply
        to an unknown stream is logged and dropped rather than failing the
        session: nothing is waiting for it, and the other streams on the
        connection are still good.
        """
        if status == c.kXR_attn:
            inner = self._unwrap_attn(body)
            if inner is None:
                return
            sid, status, body = inner

        if self.state in _BRINGUP:
            self._bringup(status, body)
            return

        pending = self._pending.get(sid)
        if pending is None:
            if sid in self._retired:
                self._retired_reply(sid, status, body, framer)
                return
            _log.debug("response on unknown stream %d (%s)", sid, c.status_name(status))
            return
        if status == c.kXR_ok:
            # The answer to nearly everything, so it skips the table.
            self._response_ok(sid, body, pending, framer)
            return
        handler = _HANDLERS.get(status)
        if handler is None:
            self._response_unexpected(sid, status, pending)
            return
        handler(self, sid, body, pending, framer)

    def _retired_reply(self, sid: int, status: int, body: bytes, framer: _Framer) -> None:
        """Drop a late reply to an abandoned stream; free the id once it is final."""
        _log.debug("late %s on abandoned stream %d dropped", c.status_name(status), sid)
        if status == c.kXR_status:
            state = rp.parse_status(body)
            # Its raw data follows the frame, and must be skipped with it.
            framer.need_trailer = state.dlen
            framer.trailer_for = sid if state.dlen else None
            final = state.is_final
        else:
            final = status not in (c.kXR_oksofar, c.kXR_waitresp)
        if final:
            self._retired.discard(sid)
            self._free.append(sid)

    def _unwrap_attn(self, body: bytes) -> tuple[int, int, bytes] | None:
        """Unpack a ``kXR_asynresp``, or record the notice and return None."""
        info = rp.parse_attn(body)
        if info.action == c.kXR_asynresp and len(body) >= 16:
            sid, status, _ = header_fields(body, 8)
            return sid, status, body[16:]
        self._events.append(Attention(info))
        return None

    # -- bring-up -------------------------------------------------------

    def _bringup(self, status: int, body: bytes) -> None:
        """Advance the connection one bring-up step on the reply ``body``.

        Bring-up replies are matched by state, not by stream id: only one
        bring-up request is ever outstanding, and the handshake reply arrives
        on stream 0 with no request behind it. ``kXR_authmore`` is the only
        non-``ok`` status that continues; anything else unexpected fails the
        session.
        """
        if status == c.kXR_error:
            self._bringup_error(body)
            return
        if status not in (c.kXR_ok, c.kXR_authmore):
            self._fail(
                ProtocolError(
                    f"unexpected {c.status_name(status)} during {self.state.name.lower()}"
                )
            )
            return

        if self.state is State.HANDSHAKE:
            self._handshaken(body)
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
        if status == c.kXR_authmore:
            self._auth_step(body)
        else:
            self._become_ready()

    def _bringup_error(self, body: bytes) -> None:
        """A ``kXR_error`` before the session is up: fatal, except mid-login.

        A server refusing the mechanism in play is the cue to try the next
        one, as XrdCl does; anything else ends the bring-up.
        """
        info = rp.parse_error(body)
        if self.state is State.AUTH and self._credential is not None:
            self._auth_refused(info.code, info.message)
            return
        self._fail(ServerError(info.code, info.message))

    def _handshaken(self, body: bytes) -> None:
        """Take the handshake reply - its protocol version - and move on."""
        self.handshake_version = int.from_bytes(body[:4], "big")
        self.state = State.PROTOCOL

    def _after_protocol(self) -> None:
        """Decide on TLS from the ``kXR_protocol`` reply, then log in.

        TLS happens when the client wants it (``roots://``, ``require_tls``)
        or the server's flags demand it; a server that demands or is asked
        for TLS without ``kXR_haveTLS`` fails the session rather than letting
        it continue in the clear.
        """
        flags = self.protocol_info.flags
        # ``kXR_tlsData`` belongs here with the session-wide bits: this client
        # reads and writes on the connection it logged in on, so a server that
        # wants file data encrypted wants this socket encrypted.
        demanded = bool(flags & (c.kXR_gotoTLS | c.kXR_tlsLogin | c.kXR_tlsSess | c.kXR_tlsData))
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
        """Queue ``kXR_login`` - or ``kXR_bind`` for a data path - on its reserved id."""
        if self.bind_to:
            # A data connection never logs in: it says which session it
            # belongs to and inherits that session's identity wholesale.
            self.state = State.BIND
            self._out.append(encode(r.Bind(self.bind_to), _SID_BIND))
            return
        self.state = State.LOGIN
        self._out.append(encode(r.Login(self.username), _SID_LOGIN))

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
            self._out.append(encode(r.Auth(cred.name, blob), _SID_AUTH))
            return
        refusal = self._auth_refusal
        if refusal is not None:
            # A server said no: keep its error (and its code, which the
            # compat layer reports as the errno) but say what else was tried.
            detail = "; ".join(f"{k}: {v}" for k, v in self._auth_rejected.items())
            self._fail(ServerError(refusal.code, f"{refusal.message.strip()} [{detail}]".strip()))
            return
        self._fail(NoMechanismError(offered=self._offered, tried=self._auth_rejected))

    @property
    def skipped(self) -> dict[str, str]:
        """Each mechanism the login did not use, and why."""
        return dict(self._auth_rejected)

    def _auth_refused(self, code: int, message: str) -> None:
        """The server turned the mechanism in play down: try the next one.

        This is what XrdCl does, and it matters: EOS's ``eosuser`` redirector
        refuses a grid proxy's ``gsi`` but takes ``unix``, then redirects to
        the instance that holds the user's home, where ``gsi`` is accepted.
        Stopping at the first refusal would leave that home unreachable.
        """
        cred = self._credential
        assert cred is not None  # guarded by the caller
        why = f"refused by the server: [{code}] {message.strip() or 'no reason given'}"
        self._auth_rejected[cred.name] = why
        self._auth_refusal = ServerError(code, message)
        self._credential = None
        self._next_credential("")

    def _auth_step(self, challenge: bytes) -> None:
        """Answer a ``kXR_authmore`` challenge with the mechanism in play.

        A mechanism that raises or has nothing more to say is recorded as
        rejected and the ladder moves on to the next one, so one broken
        plug-in (an expired proxy, a missing keytab) does not stop a login
        another mechanism could complete.
        """
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
        self._out.append(encode(r.Auth(cred.name, blob), _SID_AUTH))

    def _become_ready(self) -> None:
        """Enter READY, arming request signing if the mechanism produced a key.

        Signing needs both a session key (only a mechanism that negotiates
        one, GSI here, sets ``session_key``) and a server security level that asks for it; the
        :class:`~xrdclient.crypto.sigver.Signer` decides per request.
        """
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
        """Fail the session (not a stream): ``Failed`` with no stream id."""
        self.state = State.FAILED
        self._events.append(Failed(None, None, error))

    # -- request responses ----------------------------------------------

    def _response_ok(self, sid: int, body: bytes, pending: _Pending, _framer: _Framer) -> None:
        """Final answer: join it to any ``kXR_oksofar`` chunks and complete the stream."""
        if pending.cap and not self._within_cap(sid, pending, len(body)):
            return
        if pending.buffer:
            # Extending before freezing avoids copying the accumulated body twice.
            pending.buffer += body
            data = bytes(pending.buffer)
        else:
            data = body
        self.release(sid)
        self._events.append(Completed(sid, pending.request, data, pending.status))

    def _response_chunk(self, sid: int, body: bytes, pending: _Pending, _framer: _Framer) -> None:
        """``kXR_oksofar``: buffer a partial body and report it; the stream stays open."""
        if self._within_cap(sid, pending, len(body)):
            pending.buffer += body
            self._events.append(Chunk(sid, pending.request, body))

    def _response_error(self, sid: int, body: bytes, pending: _Pending, _framer: _Framer) -> None:
        """``kXR_error``: end the stream with the exception the error code maps to."""
        info = rp.parse_error(body)
        self.release(sid)
        self._events.append(Failed(sid, pending.request, server_error(info, pending.path)))

    def _response_redirect(
        self, sid: int, body: bytes, pending: _Pending, _framer: _Framer
    ) -> None:
        """``kXR_redirect``: report the new target; the stream stays pending.

        Following it means a new connection, which only the driver can make;
        the driver calls :meth:`release` once it has re-sent elsewhere.
        """
        self._events.append(Redirected(sid, pending.request, rp.parse_redirect(body)))

    def _response_wait(self, sid: int, body: bytes, pending: _Pending, _framer: _Framer) -> None:
        """``kXR_wait``: ask the driver to sleep (capped by ``wait_cap``) and :meth:`resume`."""
        wait = rp.parse_wait(body)
        self._events.append(
            Waiting(sid, pending.request, min(wait.seconds, self.config.wait_cap), wait.message)
        )

    def _response_waitresp(
        self, sid: int, body: bytes, pending: _Pending, _framer: _Framer
    ) -> None:
        """``kXR_waitresp``: the answer will come later as a ``kXR_attn``; nothing to resend."""
        later = rp.parse_waitresp(body)
        self._events.append(Waiting(sid, pending.request, later.seconds, resend=False))

    def _response_status(self, sid: int, body: bytes, pending: _Pending, framer: _Framer) -> None:
        """``kXR_status`` (pgread/pgwrite): note the header, then expect its raw data.

        The frame's ``dlen`` covers only the status block; the data it
        describes follows as ``state.dlen`` uncounted bytes, so the framer is
        told to collect that trailer before reading the next header.
        """
        state = rp.parse_status(body)
        pending.status = state
        if state.dlen:
            framer.need_trailer = state.dlen
            framer.trailer_for = sid
        else:
            self._on_status_data(sid, b"")

    def _response_unexpected(self, sid: int, status: int, pending: _Pending) -> None:
        """A status no request can be answered with: fail that stream only."""
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
        """The raw data behind a ``kXR_status`` arrived: a chunk, or the final answer.

        ``kXR_PartialResult`` in the status header means more frames follow
        on this stream (as ``kXR_oksofar`` does for plain replies).
        """
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
        """Endpoint, state, load and TLS - what a hung-connection report needs."""
        return (
            f"SessionMachine({self.host}:{self.port}, state={self.state.name}, "
            f"in_flight={len(self._pending)}, tls={self.tls_active})"
        )


#: The states in which a reply belongs to bring-up rather than to a request.
_BRINGUP = frozenset({State.HANDSHAKE, State.PROTOCOL, State.LOGIN, State.AUTH, State.BIND})

_Handler = Callable[[SessionMachine, int, bytes, _Pending, _Framer], None]

#: Every reply status a request can be answered with, bar ``kXR_ok`` - which
#: :meth:`SessionMachine._dispatch` takes first - and bar the unexpected ones.
_HANDLERS: dict[int, _Handler] = {
    c.kXR_oksofar: SessionMachine._response_chunk,
    c.kXR_error: SessionMachine._response_error,
    c.kXR_redirect: SessionMachine._response_redirect,
    c.kXR_wait: SessionMachine._response_wait,
    c.kXR_waitresp: SessionMachine._response_waitresp,
    c.kXR_status: SessionMachine._response_status,
}


def server_error(info: rp.ErrorInfo, path: str = "") -> XRootDError:
    """The exception a ``kXR_error`` body stands for, naming ``path`` if known.

    The one place a server's refusal becomes an exception: the event path and
    the bulk reader both come here, so a refusal reads the same whichever of
    them took it off the wire.
    """
    try:
        raise_for_status(info.code, info.message, path=path or None)
    except XRootDError as exc:
        return exc
    return ServerError(info.code, info.message, path=path or None)
