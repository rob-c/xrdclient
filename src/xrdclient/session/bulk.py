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
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import NoReturn

from .._compat import SLOTS
from .._log import get_logger
from ..errors import ProtocolError, XRootDError, raise_for_status
from ..errors import TimeoutError as XrdTimeoutError
from ..proto import constants as c
from ..proto import requests as r
from ..proto.frames import Request, ResponseHeader, decode_header

__all__ = ["BulkReader", "BulkUnsupported"]

_log = get_logger(__name__)

#: Header of every server reply: streamid, status, dlen.
_HEADER = 8

#: Most a drain holds of a reply it is throwing away, at a time.
_SINK = 64 << 10

#: Where ``dlen`` starts in a request header: streamid, requestid, 16 bytes of
#: parameters.
_DLEN = 20


class BulkUnsupported(XRootDError):
    """This exchange needs the full event path; the caller should fall back.

    Raised before any byte of the transfer has been handed over, so a caller
    can simply re-run the read the ordinary way. A server that answers a read
    with ``kXR_wait``, a redirect, or anything else that is a conversation
    rather than a block of bytes lands here.
    """


@dataclass(**SLOTS)
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


class BulkReader:
    """Pipelined ``kXR_read`` over one open file on one connection.

    Construct it through :meth:`xrdclient.session.sync.Session.bulk`, which holds the
    session lock, checks that nothing else is outstanding, and settles the
    reader when the connection is handed back.

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
        "_session",
        "_transport",
        "_machine",
        "_handle",
        "_chunk",
        "_depth",
        "_bufs",
        "_views",
        "_leased",
        "_owed",
        "_torn",
        "_expires",
        "delivered",
    )

    def __init__(self, session: object, handle: bytes, *, chunk: int, depth: int) -> None:
        if chunk < 1 or chunk > c.MAX_RESPONSE_BODY:
            raise ValueError(f"chunk must be 1..{c.MAX_RESPONSE_BODY} bytes, not {chunk}")
        if depth < 1:
            raise ValueError("depth must be at least 1")
        self._session = session
        self._transport = session.transport  # type: ignore[attr-defined]
        self._machine = session.machine  # type: ignore[attr-defined]
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
        #: Bytes handed to the caller so far, over every call to :meth:`stream`.
        self.delivered = 0

    def __enter__(self) -> BulkReader:
        return self

    def __exit__(self, *exc: object) -> None:
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
        total = len(view)
        if total <= 0:
            return 0
        free_sids = self._begin()
        pending: dict[int, int] = {}
        header = memoryview(bytearray(_HEADER))
        cursor = acked = 0
        try:
            while True:
                while free_sids and cursor < total:
                    size = min(self._chunk, total - cursor)
                    sid = free_sids.pop()
                    self._issue_write(sid, start + cursor, view[cursor : cursor + size])
                    pending[sid] = size
                    cursor += size
                if not pending:
                    return acked
                answered = self._acknowledged(header)
                if answered is not None:
                    acked += pending.pop(answered)
                    free_sids.append(answered)
        finally:
            self.settle()

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
            if self._owed and not self._torn:
                self._drain()
        finally:
            if self._torn or self._owed:
                self._owed.clear()
                self._session.mark_broken()  # type: ignore[attr-defined]
            else:
                self._machine.release_sids(sids)

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
        deadline = self._session.config.stall_deadline  # type: ignore[attr-defined]
        self._expires = time.monotonic() + deadline if deadline else None
        self._torn = False
        self._leased = self._machine.lease_sids(self._depth)
        return list(self._leased)

    def _issue(self, sid: int, request: Request) -> None:
        """Put ``request`` on the wire on ``sid`` and count its reply as owed."""
        self._send(sid, self._machine.frame_for(request, sid))

    def _issue_write(self, sid: int, offset: int, piece: memoryview) -> None:
        """Put one ``kXR_write`` of ``piece`` on the wire, without copying it.

        The request header is framed with no payload and its length patched
        in, and the payload follows from the caller's buffer. A session that
        signs its requests signs the whole frame, payload included, so there
        the frame is built whole.
        """
        machine = self._machine
        if machine.signer is not None:
            frame = machine.frame_for(r.Write(self._handle, offset, piece.tobytes()), sid)
            self._send(sid, frame)
            return
        head = machine.frame_for(r.Write(self._handle, offset, b""), sid)
        self._send(sid, head[:_DLEN] + len(piece).to_bytes(4, "big"), piece)

    def _send(self, sid: int, *frames: bytes | memoryview) -> None:
        """Send one request's bytes on ``sid`` and count its reply as owed."""
        # A send that fails part-way leaves half a request on the wire, which
        # the server may answer or not; either way nothing after it is sound.
        self._torn = True
        for frame in frames:
            self._transport.send(frame)
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

    def _acknowledged(self, header: memoryview) -> int | None:
        """Take one reply to a write off the wire; the stream id it settles.

        ``None`` for a ``kXR_oksofar`` instalment, which a write is not meant
        to get but which is harmless if it does. A refusal raises once its
        frame is off the wire, like a failed read.
        """
        reply = self._next_reply(header)
        body = self._body(reply.dlen)
        if reply.status == c.kXR_oksofar:
            return None
        self._owed.discard(reply.streamid)
        if reply.status != c.kXR_ok:
            _refuse(reply.status, body)
        return reply.streamid

    def _next_reply(self, header: memoryview) -> ResponseHeader:
        """The next reply header, which must answer a request still owed.

        The wire is torn from here until the frame's body is off it too.
        """
        self._torn = True
        self._exact(header, _HEADER)
        reply = decode_header(bytes(header))
        if reply.streamid not in self._owed:
            raise ProtocolError(
                f"bulk transfer: reply on stream {reply.streamid}, "
                f"which is not one of the {len(self._owed)} in flight"
            )
        return reply

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
        connection to itself; anything else means the stream is out of step.
        """
        header = memoryview(bytearray(_HEADER))
        sink = memoryview(bytearray(min(self._chunk, _SINK)))
        while self._owed:
            self._torn = True
            self._exact(header, _HEADER)
            reply = decode_header(bytes(header))
            if reply.streamid not in self._owed:
                raise ProtocolError(
                    f"bulk read: reply on stream {reply.streamid} while draining, "
                    "which is not one still owed"
                )
            left = reply.dlen
            while left:
                step = min(left, len(sink))
                self._exact(sink, step)
                left -= step
            self._torn = False
            if reply.status != c.kXR_oksofar:
                self._owed.discard(reply.streamid)

    def _exact(self, view: memoryview, size: int) -> None:
        """Fill exactly ``size`` bytes of ``view``, or raise."""
        got = 0
        while got < size:
            if self._expires is not None and time.monotonic() > self._expires:
                raise XrdTimeoutError(
                    f"bulk read stalled with {size - got} bytes of a frame outstanding"
                )
            read = self._transport.receive_into(view[got:size])
            if not read:
                raise XrdTimeoutError(
                    f"connection closed with {size - got} bytes of a reply outstanding"
                )
            got += read


def _refuse(status: int, body: bytearray) -> NoReturn:
    """Raise for a final reply that is not the data or the ``kXR_ok`` asked for."""
    if status == c.kXR_error:
        code = int.from_bytes(body[:4], "big") if len(body) >= 4 else 0
        text = bytes(body[4:]).split(b"\x00", 1)[0].decode("utf-8", "replace")
        raise_for_status(code, text)
        raise ProtocolError(f"bulk request failed with error code {code}: {text}")
    # kXR_wait, kXR_waitresp, kXR_redirect: a conversation, not bytes.
    raise BulkUnsupported(
        f"server answered a bulk request with {c.status_name(status)}; "
        "the transfer falls back to the standard path"
    )
