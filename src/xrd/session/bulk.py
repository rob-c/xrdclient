"""The bulk data plane: pipelined, zero-copy reads over one connection.

A ``kXR_read`` answered through the ordinary event path costs four copies of
every byte - the kernel's into a fresh ``bytes``, that one into the framer's
buffer, the framer's into the completed body, and the body into whatever the
caller meant to fill. At a gigabyte that is most of the time spent, and none
of it is protocol work.

This module is the exception the rest of the library is built to allow. The
codecs stay where they are (:mod:`xrd.proto` frames the requests and names the
statuses); what changes is who holds the wire. A :class:`BulkReader` borrows an
idle session, keeps several reads in flight so the socket never waits for the
next request, and lands each reply *directly* in the buffer it will be used
from, via :meth:`~xrd.transport.base.Transport.receive_into`. Nothing is
accumulated: a chunk is handed to the caller as soon as its last byte arrives.

The reader is deliberately small and does one thing. It does not redirect, it
does not re-open, and it does not retry: those are recovery policies, and they
belong to the caller that knows what the transfer is for. See
:mod:`xrd.client.bulk` for the parallel, restartable transfer built on top.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass

from .._compat import SLOTS
from .._log import get_logger
from ..errors import ProtocolError, XRootDError, raise_for_status
from ..errors import TimeoutError as XrdTimeoutError
from ..proto import constants as c
from ..proto import requests as r
from ..proto.frames import decode_header

__all__ = ["BulkReader", "BulkUnsupported"]

_log = get_logger(__name__)

#: Header of every server reply: streamid, status, dlen.
_HEADER = 8


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

    Construct it through :meth:`xrd.session.sync.Session.bulk`, which holds the
    session lock and checks that nothing else is outstanding.

    ``chunk`` is how much each request asks for and ``depth`` how many are in
    flight at once, so the reader's whole memory cost is ``chunk * depth``.
    """

    __slots__ = ("_session", "_handle", "_chunk", "_depth", "_bufs", "_views", "delivered")

    def __init__(self, session: object, handle: bytes, *, chunk: int, depth: int) -> None:
        if chunk < 1 or chunk > c.MAX_RESPONSE_BODY:
            raise ValueError(f"chunk must be 1..{c.MAX_RESPONSE_BODY} bytes, not {chunk}")
        if depth < 1:
            raise ValueError("depth must be at least 1")
        self._session = session
        self._handle = handle
        self._chunk = chunk
        self._depth = depth
        self._bufs = [bytearray(chunk) for _ in range(depth)]
        self._views = [memoryview(b) for b in self._bufs]
        #: Bytes handed to the caller so far, over every call to :meth:`stream`.
        self.delivered = 0

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
        session = self._session
        transport = session.transport  # type: ignore[attr-defined]
        machine = session.machine  # type: ignore[attr-defined]
        deadline = session.config.stall_deadline  # type: ignore[attr-defined]
        sids = machine.lease_sids(self._depth)
        inflight: dict[int, _InFlight] = {}
        # The order the reads went out in, which is the order they come back.
        issued: list[int] = []
        done: set[int] = set()
        free_slots = list(range(self._depth))
        free_sids = list(sids)
        header = bytearray(_HEADER)
        header_view = memoryview(header)
        offset, end = start, start + length
        expires = time.monotonic() + deadline if deadline else None
        try:
            while True:
                # Keep the wire busy: every free slot gets a request before we
                # block on an answer, so the server is never waiting on us.
                while free_slots and offset < end:
                    want = min(self._chunk, end - offset)
                    sid = free_sids.pop()
                    slot = free_slots.pop()
                    request = r.Read(self._handle, offset, want)
                    transport.send(machine.frame_for(request, sid))
                    inflight[sid] = _InFlight(offset, want, self._views[slot][:want], slot)
                    issued.append(sid)
                    offset += want
                if not issued:
                    return
                if issued[0] not in done:
                    finished = self._collect(transport, header_view, inflight, expires)
                    if finished is None:
                        continue
                    done.add(finished)
                    if issued[0] not in done:
                        continue
                sid = issued.pop(0)
                done.discard(sid)
                entry = inflight.pop(sid)
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
            machine.release_sids(sids)

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
        session = self._session
        transport = session.transport  # type: ignore[attr-defined]
        machine = session.machine  # type: ignore[attr-defined]
        deadline = session.config.stall_deadline  # type: ignore[attr-defined]
        sids = machine.lease_sids(self._depth)
        inflight: dict[int, _InFlight] = {}
        free_sids = list(sids)
        header = memoryview(bytearray(_HEADER))
        cursor, end = 0, want
        landed = 0
        expires = time.monotonic() + deadline if deadline else None
        try:
            while True:
                while free_sids and cursor < end:
                    size = min(self._chunk, end - cursor)
                    sid = free_sids.pop()
                    request = r.Read(self._handle, start + cursor, size)
                    transport.send(machine.frame_for(request, sid))
                    inflight[sid] = _InFlight(cursor, size, view[cursor : cursor + size])
                    cursor += size
                if not inflight:
                    self.delivered += landed
                    return landed
                finished = self._collect(transport, header, inflight, expires)
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
            machine.release_sids(sids)

    def _collect(
        self,
        transport: object,
        header: memoryview,
        inflight: dict[int, _InFlight],
        expires: float | None,
    ) -> int | None:
        """Take one reply frame off the wire into its chunk's buffer.

        Returns the stream id whose chunk is now complete, or ``None`` when the
        frame was a ``kXR_oksofar`` instalment and more of it is still to come.
        """
        self._exact(transport, header, _HEADER, expires)
        reply = decode_header(bytes(header))
        entry = inflight.get(reply.streamid)
        if entry is None:
            raise ProtocolError(
                f"bulk read: reply on stream {reply.streamid}, "
                f"which is not one of the {len(inflight)} in flight"
            )
        if reply.status == c.kXR_error:
            body = bytearray(reply.dlen)
            self._exact(transport, memoryview(body), reply.dlen, expires)
            code = int.from_bytes(body[:4], "big") if reply.dlen >= 4 else 0
            text = bytes(body[4:]).split(b"\x00", 1)[0].decode("utf-8", "replace")
            raise_for_status(code, text)
            raise ProtocolError(f"bulk read failed with error code {code}: {text}")
        if reply.status not in (c.kXR_ok, c.kXR_oksofar):
            # kXR_wait, kXR_waitresp, kXR_redirect: a conversation, not bytes.
            body = bytearray(reply.dlen)
            self._exact(transport, memoryview(body), reply.dlen, expires)
            raise BulkUnsupported(
                f"server answered a bulk read with {c.status_name(reply.status)}; "
                "the transfer falls back to the standard path"
            )
        if entry.got + reply.dlen > entry.length:
            raise ProtocolError(
                f"bulk read: server sent {entry.got + reply.dlen} bytes "
                f"for a {entry.length}-byte read at offset {entry.offset}"
            )
        if reply.dlen:
            self._exact(
                transport, entry.dest[entry.got : entry.got + reply.dlen], reply.dlen, expires
            )
            entry.got += reply.dlen
        return None if reply.status == c.kXR_oksofar else reply.streamid

    @staticmethod
    def _exact(transport: object, view: memoryview, size: int, expires: float | None) -> None:
        """Fill exactly ``size`` bytes of ``view``, or raise."""
        got = 0
        while got < size:
            if expires is not None and time.monotonic() > expires:
                raise XrdTimeoutError(
                    f"bulk read stalled with {size - got} bytes of a frame outstanding"
                )
            read = transport.receive_into(view[got:size])  # type: ignore[attr-defined]
            if not read:
                raise XrdTimeoutError(
                    f"connection closed with {size - got} bytes of a reply outstanding"
                )
            got += read
