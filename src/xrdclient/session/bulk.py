"""The bulk data plane: pipelined, zero-copy reads over one connection.

A ``kXR_read`` answered through the ordinary event path costs four copies of
every byte - the kernel's into a fresh ``bytes``, that one into the framer's
buffer, the framer's into the completed body, and the body into whatever the
caller meant to fill. At a gigabyte that is most of the time spent, and none
of it is protocol work.

This module is the exception the rest of the library is built to allow. The
codecs stay where they are (:mod:`xrdclient.proto` frames the requests and names the
statuses); what changes is who holds the wire. A :class:`BulkReader` borrows an
idle session, keeps several reads in flight so the socket never waits for the
next request, and lands each reply *directly* in the buffer it will be used
from, via :meth:`~xrdclient.transport.base.Transport.receive_into`. Nothing is
accumulated: a chunk is handed to the caller as soon as its last byte arrives.

The reader is deliberately small and does one thing. It does not redirect, it
does not re-open, and it does not retry: those are recovery policies, and they
belong to the caller that knows what the transfer is for. See
:mod:`xrdclient.client.bulk` for the parallel, restartable transfer built on top.

What it does own is the wire it borrowed. A read that ends early - a
``kXR_wait``, an error, a consumer that stops iterating - still has replies in
flight, and the session must not reuse their stream ids until they have
arrived, or its next request takes a stray chunk as its answer. The reader
drains them before giving the connection back, and marks the session broken
when it cannot.

A cancelled reader (see :mod:`xrdcore.cancel`) does not drain: it stops at
the next frame boundary and leaves the replies still owed to the machine to
drop as they arrive, exactly as when the caller's deadline runs out, so the
connection stays in step. Caught part-way through a frame it first gives the
rest of that frame :data:`CANCEL_GRACE` to arrive, since it is nearly always
already on its way; only a peer that stalls mid-frame costs the connection.
"""

from __future__ import annotations

import sys
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, Protocol

from xrdcore import cancel

from ..errors import ConnectionError as XrdConnectionError
from ..errors import ProtocolError, XRootDError
from ..errors import TimeoutError as XrdTimeoutError
from ..log import get_logger
from ..proto import constants as c
from ..proto import requests as r
from ..proto import responses as rp
from ..proto.frames import Payload, Request, ResponseHeader, decode_header
from ..proto.machine import server_error
from . import deadline as dl

if TYPE_CHECKING:
    from ..config import Config
    from ..proto.machine import SessionMachine
    from ..transport.base import Transport

__all__ = ["BulkReader", "BulkUnsupported"]


class Lender(Protocol):
    """What a :class:`BulkReader` borrows from the session that lends it a link.

    :class:`~xrdclient.session.sync.Session` is one; naming only these
    members keeps this module free of an import of it (which imports this).
    """

    @property
    def transport(self) -> Transport:
        """The control link the reader reads and writes directly."""
        ...

    @property
    def machine(self) -> SessionMachine:
        """The protocol state, for framing requests on leased stream ids."""
        ...

    @property
    def config(self) -> Config:
        """The session's settings: timeouts, stall deadline, wait budget."""
        ...

    def mark_broken(self) -> None:
        """Never trust the link again: a frame was left half read."""
        ...

    def note(self, info: rp.AttnInfo) -> None:
        """Pass on an unsolicited ``kXR_attn`` that arrived mid-read."""
        ...


_log = get_logger(__name__)

#: Header of every server reply: streamid, status, dlen.
_HEADER = 8

#: Most a drain holds of a reply it is throwing away, at a time.
_SINK = 64 << 10

#: How long past the delay a ``kXR_waitresp`` names its answer may still take.
WAITRESP_GRACE = 30.0

#: What wraps a ``kXR_asynresp`` answer: action, reserved, then its own header.
_ASYNC = 16

#: How long a cancelled reader caught part-way through a frame waits for the
#: rest of it, so that it can stop between frames with the wire in step.
CANCEL_GRACE = 0.25


class BulkUnsupported(XRootDError):
    """This exchange needs the full event path; the caller should fall back.

    Raised before any byte of the transfer has been handed over, so a caller
    can simply re-run the read the ordinary way. A server that answers a read
    with ``kXR_wait``, a redirect, or anything else that is a conversation
    rather than a block of bytes lands here.
    """


@dataclass(slots=True)
class _InFlight:
    """One outstanding read: where it belongs, and where its bytes land.

    ``dest`` is the buffer this reply is received into - a rotating scratch
    buffer when the caller wants pieces handed to it, or a slice of the
    caller's own buffer when it asked to be filled directly. Either way the
    socket writes into it once and nothing copies it again.
    """

    offset: int
    length: int
    dest: memoryview
    slot: int = -1
    got: int = 0


@dataclass(frozen=True, slots=True)
class _WriteReplies:
    """How one :meth:`BulkReader.write_chunks` call treats each write's answer.

    The caller's two callbacks and its strictness, fixed for the whole
    stream and consulted by :meth:`BulkReader._write_answer` per reply.
    """

    #: ``(offset, length)`` of each write the server stored (``kXR_ok``).
    acknowledged: Callable[[int, int], None] | None = None
    #: ``(offset, length)`` of each write answered ``kXR_wait``; without
    #: it a wait is an ordinary refusal.
    waited: Callable[[int, int], None] | None = None
    #: A reply that is neither acknowledgement, wait nor error tears the
    #: connection down as a protocol error rather than being reported.
    strict: bool = False


class BulkReader:
    """Pipelined ``kXR_read`` over one open file on one connection.

    Construct it through :meth:`xrdclient.session.sync.Session.bulk`, which
    gives it the wire to itself - the control link's reader role and its send
    lock, so every other request on the session waits - checks that nothing
    else is outstanding, and settles the reader when the connection is handed
    back.

    Why a second framer, beside the machine's: the machine is fed bytes that
    have already been received, which is one copy too many for a gigabyte. A
    bulk read must know where the next body bytes go *before* the ``recv``,
    so that the kernel writes them straight into the caller's buffer. The
    wire codecs are still shared - :func:`~xrdclient.proto.frames.decode_header`
    for every header and :mod:`~xrdclient.proto.responses` for every body
    that is not data - so the two can only disagree about where bytes land,
    not about what they mean.

    ``chunk`` is how much each request asks for and ``depth`` how many are in
    flight at once, so the reader's whole memory cost is ``chunk * depth``.

    Whenever a call ends - finished, failed, or abandoned by its consumer -
    every reply it asked for must be off the wire before its stream ids are
    reused, or the next request on this connection would take a stray chunk
    as its answer. :meth:`settle` does that: it drains what is still owed, and
    if it cannot, it marks the session broken and keeps the ids out of
    circulation so the connection is never trusted again.
    """

    __slots__ = (
        "_bufs",
        "_cancel_by",
        "_chunk",
        "_deadline",
        "_depth",
        "_expires",
        "_handle",
        "_leased",
        "_machine",
        "_owed",
        "_resting",
        "_session",
        "_torn",
        "_transport",
        "_views",
        "delivered",
    )

    def __init__(self, session: Lender, handle: bytes, *, chunk: int, depth: int) -> None:
        """Borrow ``session``'s control link for the open file ``handle``.

        ``session`` is any :class:`Lender`; a
        :class:`~xrdclient.session.sync.Session` is the one that does.
        ``chunk`` may not exceed what one reply body may carry
        (:data:`~xrdclient.proto.constants.MAX_RESPONSE_BODY`), since a read
        answered in several ``kXR_oksofar`` pieces would no longer land in one
        slot. Nothing is sent until the first call.
        """
        if chunk < 1 or chunk > c.MAX_RESPONSE_BODY:
            raise ValueError(f"chunk must be 1..{c.MAX_RESPONSE_BODY} bytes, not {chunk}")
        if depth < 1:
            raise ValueError("depth must be at least 1")
        self._session = session
        self._transport = session.transport
        self._machine = session.machine
        self._handle = handle
        self._chunk = chunk
        self._depth = depth
        #: The rotating scratch buffers :meth:`stream` receives into, made the
        #: first time it runs: the other calls land bytes in buffers of their
        #: own, and a small read should not pay for ``chunk * depth`` of them.
        self._bufs: list[bytearray] = []
        self._views: list[memoryview] = []
        #: Stream ids the call in progress holds; empty between calls.
        self._leased: list[int] = []
        #: Stream ids whose final reply has not come off the wire yet.
        self._owed: set[int] = set()
        #: Whether the wire is part-way through a frame (or a request), so the
        #: next byte on it is not a header and nothing more can be read.
        self._torn = False
        self._expires: float | None = None
        #: When the caller's :func:`~xrdclient.deadline` runs out, if it set one.
        self._deadline: float | None = None
        #: The link's own read timeout, put back after a deadline shortens it.
        self._resting: float = session.config.request_timeout
        #: When a cancelled reader caught mid-frame stops waiting for the rest.
        self._cancel_by: float | None = None
        #: Bytes handed to the caller so far, over every call to :meth:`stream`.
        self.delivered = 0

    def __enter__(self) -> BulkReader:
        """The reader itself; the ``with`` exists so leaving it settles the wire."""
        return self

    def __exit__(self, *exc: object) -> None:
        """:meth:`settle`, whatever the block did - the wire must be in step after."""
        self.settle()

    def stream(self, start: int, length: int) -> Iterator[tuple[int, memoryview]]:
        """Yield ``(offset, view)`` for ``length`` bytes from ``start``.

        Pieces come back in the file's own order, whatever order the server
        answers in, because a consumer writing to a pipe has no other option
        and one waiting its turn costs only the slot it already holds. Each
        view is valid only until the next piece is asked for - the buffers
        rotate, which is what keeps the memory bounded - so a consumer writes
        or copies it before coming back for more.

        The iterator stops early, without error, if the server reports end of
        file: a short read is the file's length, not a failure. A consumer that
        needs all ``length`` bytes checks :attr:`delivered`.
        """
        if length <= 0:
            return
        free_sids = self._begin()
        views = self._scratch()
        free_slots = list(range(self._depth))
        inflight: dict[int, _InFlight] = {}
        # The order the reads went out in, which is the order they are handed
        # back in, however the server interleaves its answers.
        issued: deque[int] = deque()
        done: set[int] = set()
        header = memoryview(bytearray(_HEADER))
        offset, end = start, start + length
        try:
            while True:
                # Keep the wire busy: every free slot gets a request before we
                # block on an answer, so the server is never waiting on us.
                while free_slots and offset < end:
                    want = min(self._chunk, end - offset)
                    slot = free_slots.pop()
                    sid = free_sids.pop()
                    inflight[sid] = _InFlight(offset, want, views[slot][:want], slot)
                    self._issue(sid, r.Read(self._handle, offset, want))
                    issued.append(sid)
                    offset += want
                if not issued:
                    return
                sid = issued.popleft()
                entry = self._await_in_turn(sid, header, inflight, done)
                free_sids.append(sid)
                free_slots.append(entry.slot)
                if entry.got:
                    self.delivered += entry.got
                    yield entry.offset, entry.dest[: entry.got]
                if entry.got < entry.length:
                    # Short reply: end of file. Nothing beyond it can arrive,
                    # so stop asking and hand back what is already in flight.
                    end = min(end, entry.offset + entry.got)
                    offset = min(offset, end)
        finally:
            self.settle()

    def into(self, view: memoryview, start: int) -> int:
        """Fill ``view`` from ``start``, with nothing copied on the way.

        Each request in flight receives into its own disjoint slice of the
        caller's buffer, so the bytes cross from the kernel to their final
        place once and never again. Returns how many bytes landed, which is
        short of ``len(view)`` only at end of file.

        The scratch buffers :meth:`stream` rotates are not used here and not
        allocated by this path; ``depth`` only decides how many requests are
        outstanding.
        """
        want = len(view)
        if want <= 0:
            return 0
        free_sids = self._begin()
        inflight: dict[int, _InFlight] = {}
        header = memoryview(bytearray(_HEADER))
        cursor, end = 0, want
        landed = 0
        try:
            while True:
                while free_sids and cursor < end:
                    size = min(self._chunk, end - cursor)
                    sid = free_sids.pop()
                    inflight[sid] = _InFlight(cursor, size, view[cursor : cursor + size])
                    self._issue(sid, r.Read(self._handle, start + cursor, size))
                    cursor += size
                if not inflight:
                    self.delivered += landed
                    return landed
                finished = self._collect(header, inflight)
                if finished is None:
                    continue
                entry = inflight.pop(finished)
                free_sids.append(finished)
                landed += entry.got
                if entry.got < entry.length:
                    # End of file: stop asking, and keep only what is known to
                    # be contiguous once everything still in flight has landed.
                    end = min(end, entry.offset + entry.got)
                    cursor = min(cursor, end)
        finally:
            self.settle()

    def gather(self, requests: Sequence[tuple[Request, int]]) -> list[memoryview]:
        """Run ``requests`` pipelined, and return each one's whole reply body.

        Each entry is a request and the most its reply may carry; the body is
        received straight into a buffer of that size, however many
        ``kXR_oksofar`` instalments the server sends it in, and a reply that
        would overflow it is a protocol error rather than a reallocation. Up
        to ``depth`` requests are in flight at once, so a vector read that had
        to be split into several still costs about one round trip. Bodies come
        back in the order the requests were given.
        """
        bodies: list[memoryview] = []
        if not requests:
            return bodies
        free_sids = self._begin()
        inflight: dict[int, _InFlight] = {}
        header = memoryview(bytearray(_HEADER))
        results: dict[int, memoryview] = {}
        issued = 0
        try:
            while True:
                while free_sids and issued < len(requests):
                    request, bound = requests[issued]
                    sid = free_sids.pop()
                    inflight[sid] = _InFlight(issued, bound, memoryview(bytearray(bound)))
                    self._issue(sid, request)
                    issued += 1
                if not inflight:
                    return [results[index] for index in range(len(requests))]
                finished = self._collect(header, inflight)
                if finished is not None:
                    entry = inflight.pop(finished)
                    free_sids.append(finished)
                    results[entry.offset] = entry.dest[: entry.got]
        finally:
            self.settle()

    def write_from(self, view: memoryview, start: int) -> int:
        """Write ``view`` at ``start``, with ``depth`` writes in flight.

        Each ``kXR_write`` carries one ``chunk`` of ``view`` and is sent
        straight from it - the header, then the caller's own bytes - so a
        payload is never copied on its way to the socket, and the next write
        is on the wire while the server is still storing the last. Returns
        the bytes the server acknowledged, which is all of them: a write the
        server refuses raises, once every other write still in flight has
        been answered, so a failure anywhere in the window is never lost
        behind a success that happened to come back later.
        """
        if not view:
            return 0
        return self.write_chunks(
            (view[at : at + self._chunk] for at in range(0, len(view), self._chunk)), start
        )

    def write_chunks(
        self,
        pieces: Iterable[memoryview],
        start: int = 0,
        *,
        acknowledged: Callable[[int, int], None] | None = None,
        waited: Callable[[int, int], None] | None = None,
        check: Callable[[], None] | None = None,
        strict_replies: bool = False,
    ) -> int:
        """Stream bounded pieces, reporting only acknowledged byte ranges.

        A ``waited`` callback records ranges declined with ``kXR_wait``. It
        must not use this borrowed session: replay belongs to the caller,
        after this exchange settles. Without it, WAIT retains the ordinary
        :meth:`write_from` fallback. No write is silently retried here.
        """
        replies = _WriteReplies(acknowledged, waited, strict_replies)
        pending: dict[int, tuple[int, int]] = {}
        header = memoryview(bytearray(_HEADER))
        source = iter(pieces)
        offset, acked = start, 0
        exhausted = False
        failure: XRootDError | None = None
        free_sids = self._begin()
        try:
            while True:
                if not exhausted and failure is None:
                    offset, exhausted = self._write_batch(source, free_sids, pending, offset, check)
                if not pending:
                    if failure is not None:
                        raise failure
                    return acked
                reply = self._next_reply(header)
                body = self._body(reply.dlen)
                if reply.status == c.kXR_oksofar:
                    continue
                self._owed.discard(reply.streamid)
                span = pending.pop(reply.streamid)
                free_sids.append(reply.streamid)
                count, error = self._write_answer(reply, body, span, replies)
                acked += count
                failure = failure or error
        finally:
            with suppress(Exception) if sys.exc_info()[0] is not None else nullcontext():
                self.settle()

    def _write_batch(
        self,
        source: Iterator[memoryview],
        free: list[int],
        pending: dict[int, tuple[int, int]],
        offset: int,
        check: Callable[[], None] | None,
    ) -> tuple[int, bool]:
        """Issue writes from ``source`` until no stream id is free.

        Returns the next file offset and whether ``source`` ran dry. Each id
        sent is recorded in ``pending`` with its ``(offset, length)``, which
        is how an answer - which names only its stream id - is matched to the
        byte range it acknowledges. ``check`` runs after every send, so a
        caller's cancellation or rate limit is honoured between frames, never
        part-way through one.
        """
        while free:
            try:
                piece = next(source)
            except StopIteration:
                return offset, True
            if not piece:
                continue
            if len(piece) > self._chunk:
                raise ValueError("write chunk exceeds the configured chunk size")
            sid = free.pop()
            self._issue_write(sid, offset, piece)
            pending[sid] = (offset, len(piece))
            offset += len(piece)
            if check is not None:
                check()
        return offset, False

    def _write_answer(
        self,
        reply: ResponseHeader,
        body: bytearray,
        span: tuple[int, int],
        replies: _WriteReplies,
    ) -> tuple[int, XRootDError | None]:
        """One write's answer: the bytes it acknowledged, and its refusal if any.

        ``span`` is the ``(offset, length)`` the write carried. A refusal is
        returned rather than raised so the caller can keep draining the
        window - every reply owed must be read before the connection is
        usable again - and raise the first refusal once it has.
        """
        if reply.status == c.kXR_ok:
            if replies.acknowledged is not None:
                replies.acknowledged(*span)
            return span[1], None
        if reply.status == c.kXR_wait and replies.waited is not None:
            replies.waited(*span)
            return 0, None
        if replies.strict and reply.status != c.kXR_error:
            self._torn = True
            raise ProtocolError(
                f"unexpected reply to a write: stream {reply.streamid}, status {reply.status}"
            )
        try:
            _refuse(reply.status, body)
        except XRootDError as exc:
            return 0, exc

    def settle(self) -> None:
        """Make the connection safe for its next request, or mark it broken.

        Every reply still owed is taken off the wire and thrown away, so that
        the stream ids can go back to the pool with nothing left to answer on
        them. When that is impossible - the wire stopped part-way through a
        frame, a reply arrived that nobody is owed, or the owed replies never
        came - the session is marked broken, which keeps it out of the pool,
        and the ids stay leased, so no later request on it can be matched to
        a reply that is still in transit. Calling it again is harmless.
        """
        sids, self._leased = self._leased, []
        if not sids:
            return
        try:
            if self._owed and not self._torn and not self._stopped():
                self._drain()
        finally:
            self._hand_back(sids)

    def _hand_back(self, sids: list[int]) -> None:
        """Return this call's stream ids, or mark the session broken.

        Replies still owed after the caller's deadline ran out or it was
        cancelled, with the wire stopped between frames, are the machine's to
        drop: the ids go back as abandoned, and the event path discards each
        reply as it arrives - the connection stays in step and in use.
        Anything else still owed, or a wire stopped part-way through a frame,
        cannot be trusted again.
        """
        owed, self._owed = self._owed, set()
        if not owed and not self._torn:
            self._machine.release_sids(sids)
        elif not self._torn and self._stopped():
            self._machine.retire_sids(sids, owed)
        else:
            self._session.mark_broken()

    def _expired(self) -> bool:
        """Whether the caller's deadline has passed."""
        return self._deadline is not None and time.monotonic() >= self._deadline

    def _stopped(self) -> bool:
        """Whether the caller has stopped waiting: out of time, or cancelled."""
        return cancel.cancelled() or self._expired()

    def _scratch(self) -> list[memoryview]:
        """The rotating buffers :meth:`stream` lands pieces in, made on first use."""
        if not self._bufs:
            self._bufs = [bytearray(self._chunk) for _ in range(self._depth)]
            self._views = [memoryview(b) for b in self._bufs]
        return self._views

    def _begin(self) -> list[int]:
        """Lease this call's stream ids and start its stall clock."""
        if self._leased:
            # A second call while a stream is suspended would put two sets of
            # reads on the wire with nothing to tell their replies apart.
            raise ProtocolError("this bulk reader already has a read in progress")
        deadline = self._session.config.stall_deadline
        self._expires = time.monotonic() + deadline if deadline else None
        left = dl.remaining()
        self._deadline = None if left is None else time.monotonic() + left
        self._torn = False
        self._cancel_by = None
        self._leased = self._machine.lease_sids(self._depth)
        return list(self._leased)

    def _issue(self, sid: int, request: Request) -> None:
        """Put ``request`` on the wire on ``sid`` and count its reply as owed."""
        self._send(sid, *self._machine.frame_parts_for(request, sid))

    def _issue_write(self, sid: int, offset: int, piece: memoryview) -> None:
        """Put one ``kXR_write`` of ``piece`` on the wire, without copying it.

        The frame is the request header and the caller's own buffer, sent
        together in one vectored call. A session that signs its requests
        signs the whole frame, payload included, so there the frame is built
        whole - the machine does that, as it does for every signed request.
        """
        self._send(sid, *self._machine.frame_parts_for(r.Write(self._handle, offset, piece), sid))

    def _send(self, sid: int, *frames: Payload) -> None:
        """Send one request's bytes on ``sid`` and count its reply as owed."""
        # Nothing more goes out for a caller that has gone: what is already
        # in flight is left for the machine to drop.
        cancel.check("the bulk transfer")
        # A send that fails part-way leaves half a request on the wire, which
        # the server may answer or not; either way nothing after it is sound.
        self._torn = True
        self._transport.send_buffers(frames)
        self._torn = False
        self._owed.add(sid)

    def _await_in_turn(
        self, sid: int, header: memoryview, inflight: dict[int, _InFlight], done: set[int]
    ) -> _InFlight:
        """Collect replies until the read on ``sid`` is complete, and return it.

        Replies to reads issued later may complete first; they are marked in
        ``done`` and handed back when their own turn comes.
        """
        while sid not in done:
            finished = self._collect(header, inflight)
            if finished is not None:
                done.add(finished)
        done.discard(sid)
        return inflight.pop(sid)

    def _collect(self, header: memoryview, inflight: dict[int, _InFlight]) -> int | None:
        """Take one reply frame off the wire into its chunk's buffer.

        Returns the stream id whose chunk is now complete, or ``None`` when the
        frame was a ``kXR_oksofar`` instalment and more of it is still to come.
        A final reply that is not data raises, but only once its whole frame is
        off the wire, so the replies still owed can be drained after it.
        """
        reply = self._next_reply(header)
        if reply.status in (c.kXR_ok, c.kXR_oksofar):
            self._land(inflight[reply.streamid], reply.dlen)
            self._torn = False
            if reply.status == c.kXR_oksofar:
                return None
            self._owed.discard(reply.streamid)
            return reply.streamid
        body = self._body(reply.dlen)
        self._owed.discard(reply.streamid)
        _refuse(reply.status, body)

    def _next_reply(self, header: memoryview) -> ResponseHeader:
        """The next reply header, which must answer a request still owed.

        What is not an answer is dealt with on the way, as the event path
        would: a server notice is kept for :meth:`Session.notices`, and a
        ``kXR_waitresp`` leaves its stream owed - the answer it promises comes
        later, as a ``kXR_asynresp``, and is unwrapped here into the reply it
        carries. Until that answer is in, the stream id cannot be reused, or
        it would be taken for the reply to whatever was sent on it next.

        The wire is torn from here until the frame's body is off it too.
        """
        while True:
            reply = self._frame_header(header)
            if reply is None:
                continue
            if reply.streamid not in self._owed:
                raise ProtocolError(
                    f"bulk transfer: reply on stream {reply.streamid}, "
                    f"which is not one of the {len(self._owed)} in flight"
                )
            if reply.status != c.kXR_waitresp:
                return reply
            self._deferred(self._body(reply.dlen))

    def _frame_header(self, header: memoryview) -> ResponseHeader | None:
        """The next frame's header, unwrapped if it is a ``kXR_asynresp``.

        ``None`` when the frame was a notice, which is off the wire already.
        """
        self._exact(header, _HEADER, fresh=True)
        reply = decode_header(bytes(header))
        if reply.status != c.kXR_attn:
            return reply
        size = reply.dlen
        head = b""
        if size >= _ASYNC:
            self._exact(header, _HEADER)
            head = bytes(header)
            if rp.parse_attn(head).action == c.kXR_asynresp:
                self._exact(header, _HEADER)
                inner = decode_header(bytes(header))
                # What is on the wire is what is left of this frame, whatever
                # the inner header claims - which is how the event path reads it.
                return ResponseHeader(inner.streamid, inner.status, size - _ASYNC)
        notice = rp.parse_attn(head + self._body(size - len(head)))
        _log.debug("server notice during a bulk transfer: %r", notice)
        self._session.note(notice)
        return None

    def _deferred(self, body: bytearray) -> None:
        """Give a ``kXR_waitresp``'s answer the time the server asked for."""
        budget = self._session.config.wait_budget
        seconds = min(rp.parse_waitresp(bytes(body)).seconds, budget)
        if self._expires is not None:
            self._expires = max(self._expires, time.monotonic() + seconds + WAITRESP_GRACE)

    def _body(self, size: int) -> bytearray:
        """The rest of a frame whose header is already off the wire."""
        body = bytearray(size)
        self._exact(memoryview(body), size)
        self._torn = False
        return body

    def _land(self, entry: _InFlight, size: int) -> None:
        """Receive ``size`` bytes of data straight into ``entry``'s buffer."""
        if entry.got + size > entry.length:
            raise ProtocolError(
                f"bulk read at offset {entry.offset}: the server sent {entry.got + size} "
                f"bytes, more than the {entry.length} bytes it asked for"
            )
        if size:
            self._exact(entry.dest[entry.got : entry.got + size], size)
            entry.got += size

    def _drain(self) -> None:
        """Receive and discard frames until no reply is owed any more.

        Bounded by the same stall clock as the read itself. Only replies to
        this reader's own reads can be on the wire, since it holds the
        connection to itself, and notices; anything else means the stream is
        out of step. A deferred answer is waited for like any other.
        """
        header = memoryview(bytearray(_HEADER))
        sink = memoryview(bytearray(min(self._chunk, _SINK)))
        while self._owed:
            reply = self._next_reply(header)
            left = reply.dlen
            while left:
                step = min(left, len(sink))
                self._exact(sink, step)
                left -= step
            self._torn = False
            if reply.status != c.kXR_oksofar:
                self._owed.discard(reply.streamid)

    def _exact(self, view: memoryview, size: int, *, fresh: bool = False) -> None:
        """Fill exactly ``size`` bytes of ``view``, or raise.

        ``fresh`` says nothing of this frame is off the wire yet: the wire is
        torn from its first byte, and not before, so running out of time while
        waiting for a frame to start leaves the connection in step.
        """
        got = 0
        while got < size:
            self._check_clocks(size - got)
            read = self._receive_into(view[got:size])
            if not read:
                # The peer hung up: the event path's ConnectionError, so that
                # a caller retries a dropped link as it would any other and
                # does not mistake it for a stall (a TimeoutError is not
                # retried - see copy.engine._retry_transfer).
                raise XrdConnectionError(
                    f"connection closed with {size - got} bytes of a reply outstanding"
                )
            if fresh:
                self._torn = True
            got += read

    def _check_clocks(self, outstanding: int) -> None:
        """Raise if the caller is cancelled, or its deadline or the stall clock has run out."""
        if cancel.cancelled() and self._abandon_now():
            raise cancel.Cancelled("the bulk transfer was cancelled")
        if self._expired():
            raise dl.OperationExpiredError("the bulk transfer expired before it completed")
        if self._expires is not None and time.monotonic() > self._expires:
            raise XrdTimeoutError(
                f"bulk read stalled with {outstanding} bytes of a frame outstanding"
            )

    def _abandon_now(self) -> bool:
        """Whether a cancelled reader stops here, or first finishes its frame.

        Between frames it stops at once. Part-way through one it waits up to
        :data:`CANCEL_GRACE` for the rest, which is usually already in
        flight: stopping torn would cost the whole connection.
        """
        if not self._torn:
            return True
        now = time.monotonic()
        if self._cancel_by is None:
            self._cancel_by = now + CANCEL_GRACE
        return now >= self._cancel_by

    def _await_readable(self, outstanding: int) -> None:
        """Wait for bytes to read in short slices, looking at the clocks between.

        Only inside a cancel scope: a ``recv`` blocked for the link's whole
        timeout would not notice a cancellation until it returned.
        """
        give_up = time.monotonic() + self._resting
        while not self._transport.wait_readable(cancel.POLL):
            self._check_clocks(outstanding)
            if time.monotonic() >= give_up:
                raise XrdTimeoutError(
                    f"bulk read timed out with {outstanding} bytes of a frame outstanding"
                )

    def _receive_into(self, view: memoryview) -> int:
        """One ``recv`` into ``view``, cut short by the caller's deadline."""
        transport = self._transport
        if cancel.current() is not None:
            self._await_readable(len(view))
        if self._deadline is None:
            return int(transport.receive_into(view))
        left = self._deadline - time.monotonic()
        if left >= self._resting:
            return int(transport.receive_into(view))
        transport.settimeout(max(left, 0.001))
        try:
            return int(transport.receive_into(view))
        except XrdTimeoutError as exc:
            raise dl.OperationExpiredError("the bulk transfer expired before it completed") from exc
        finally:
            transport.settimeout(self._resting)


def _refuse(status: int, body: bytearray) -> NoReturn:
    """Raise for a final reply that is not the data or the ``kXR_ok`` asked for.

    A ``kXR_error`` is parsed and turned into an exception exactly as the
    event path does it (:func:`~xrdclient.proto.machine.server_error`), so a
    refusal reads the same whichever path took it off the wire.
    """
    if status == c.kXR_error:
        raise server_error(rp.parse_error(bytes(body)))
    # kXR_wait, kXR_redirect: a conversation, not bytes.
    raise BulkUnsupported(
        f"server answered a bulk request with {c.status_name(status)}; "
        "the transfer falls back to the standard path"
    )
