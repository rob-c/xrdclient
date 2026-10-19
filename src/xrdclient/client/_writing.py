"""Writing an open handle: plain, vector, paged, cloned, and over the bulk data plane.

The write half of :class:`~xrdclient.client.file.File`. A write handle is
never silently re-opened (see :data:`~._handle._WRITING`), so unlike a read
nothing here retries on a fresh connection: a lost connection is the
caller's to see.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from ..crypto.crc32c import pack_pages, page_span
from ..errors import ConnectionError as XrdConnectionError
from ..errors import (
    PageIntegrityError,
    ProtocolError,
    ServerError,
    UnsupportedError,
    kXR_InvalidRequest,
    kXR_Unsupported,
)
from ..flags import OpenFlags
from ..log import get_logger
from ..proto import constants as c
from ..proto import requests as r
from ..proto import responses as rp
from ..proto.frames import Request
from ..session.bulk import BulkUnsupported
from ..session.sync import Result
from ..types import (
    CloneRange,
    WriteChunk,
)
from ._handle import _Handle, _transient
from ._vector import CLONE_MAX_RANGES, _clone_extent, _range, _write_batches, _write_for

_log = get_logger("xrdclient.client.file")


@dataclass(frozen=True, slots=True)
class _PieceHooks:
    """The caller's callbacks for one :meth:`_Writing.write_pieces` stream.

    Carried as one value from the public method down to whichever path -
    the bulk plane or the serial fallback - ends up sending the pieces, so
    both see exactly the hooks the caller gave. ``waited`` only means
    something on the plane: the serial path sits out a ``kXR_wait`` inside
    :meth:`_Writing.write` instead of reporting it.
    """

    #: ``(offset, length)`` of each piece the server confirmed.
    acknowledged: Callable[[int, int], None] | None = None
    #: ``(offset, length)`` of each piece answered ``kXR_wait`` (not stored).
    waited: Callable[[int, int], None] | None = None
    #: Called after each piece is sent; whatever it raises stops the stream.
    check: Callable[[], None] | None = None


class _Writing(_Handle):
    """The write operations of an open file."""

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def _submit(self, request: Request) -> Result:
        """Send a write-side request, through the open checkpoint if there is one.

        A plain ``kXR_write`` inside a checkpoint is not part of it: the
        server only journals what it was handed as ``kXR_ckpXeq``. Routing
        every write through here is what makes :meth:`checkpoint` mean
        something.
        """
        if self._checkpoint:
            request = r.ChkPoint.execute(self.handle, request)
        return self._router.execute(request, path=self.url.path)

    def write(self, data: bytes | bytearray | memoryview, offset: int = 0) -> int:
        """``kXR_write``. Returns the number of bytes accepted.

        Over the bulk data plane where it can be: ``config.chunk_size``
        writes, ``config.bulk_depth`` of them in flight, each sent straight
        from ``data`` without a copy. Any write the server refuses raises
        here, after the rest of the window has been answered.
        """
        view = memoryview(data).cast("B")
        if view and self._writes_on_plane():
            written = self._plane_write(view, offset)
            if written is not None:
                self._invalidate(offset + written)
                return written
        return self._event_write(view, offset)

    def _writes_on_plane(self) -> bool:
        """Whether a write goes over the bulk data plane.

        Not inside a checkpoint, which journals only what arrives as
        ``kXR_ckpXeq``; not on a handle opened to append, whose writes the
        server places itself; and not where the automatic data paths are
        served (see :meth:`_paths_idle`).
        """
        if not self.config.bulk or self._checkpoint or self._flags & OpenFlags.APPEND:
            return False
        return self._paths_idle()

    def _plane_write(self, view: memoryview, offset: int) -> int | None:
        """Pipelined, zero-copy write of ``view``, or ``None`` if declined.

        Declined means a server that answered with a conversation - a
        ``kXR_wait``, a redirect - rather than an acknowledgement. Some of the
        window may have landed by then; the event path writes all of it again,
        which puts the same bytes at the same offsets.
        """
        chunk = self.config.chunk_size
        depth = min(self.config.bulk_depth, -(-len(view) // chunk))
        try:
            with self.session.bulk(self.handle, chunk=chunk, depth=depth) as channel:
                return channel.write_from(view, offset)
        except BulkUnsupported as exc:
            _log.debug("bulk write declined for %s (%s); using the event path", self.url, exc)
        except XrdConnectionError as exc:
            # Never re-opened: see :data:`_WRITING`.
            self._unless_aborted(exc)
            raise _transient(exc) from exc
        return None

    def write_pieces(
        self,
        pieces: Iterable[bytes | bytearray | memoryview],
        offset: int = 0,
        *,
        depth: int | None = None,
        acknowledged: Callable[[int, int], None] | None = None,
        waited: Callable[[int, int], None] | None = None,
        check: Callable[[], None] | None = None,
    ) -> int:
        """Write a stream of pieces back to back from ``offset``, pipelined.

        The upload half of the bulk data plane, for a caller whose bytes
        arrive a piece at a time - read off a local file, a socket, a
        pipeline - rather than in one buffer for :meth:`write`. Each piece is
        one ``kXR_write`` at the offset where the last one ended, sent
        straight from the caller's memory, with up to ``depth``
        (``config.bulk_depth``) of them in flight. A piece is sent before the
        next one is asked for, so a producer may reuse its buffer as soon as
        the iterator is advanced again. Keep pieces to about
        ``config.chunk_size``: each is one request.

        ``acknowledged(offset, length)`` is called as the server confirms each
        piece - the place to report progress, since only acknowledged bytes
        are on the server. ``check()`` is called after each piece is sent;
        whatever it raises stops the upload once the pieces in flight have
        been answered (a cancel hook: raise from it). Returns the bytes
        acknowledged.

        A server that answers a piece ``kXR_wait`` ("not now") has not stored
        it. With ``waited(offset, length)`` given, such a piece is reported
        there and the stream carries on; sending those ranges again - with
        :meth:`write`, once this has returned - is the caller's, who still has
        the bytes, and a reply that is none of acknowledgement, refusal or
        ``kXR_wait`` is a :class:`~xrdclient.errors.ProtocolError` after which
        the connection is not used again. Without ``waited``, the first such
        answer fails the call with
        :class:`~xrdclient.session.bulk.BulkUnsupported` once the window has
        settled, ``acknowledged`` having said what did land. A refusal raises
        the server's error the same way, after the window has settled.

        Where the plane cannot be used - inside a :meth:`checkpoint`, on a
        handle opened to append, with ``config.bulk`` off, or while the
        connection has other requests outstanding - the pieces go out one at
        a time through :meth:`write` instead, with the same callbacks (a
        ``kXR_wait`` is then sat out rather than reported). :meth:`abort`
        from another thread wakes either at once.
        """
        source = (memoryview(piece).cast("B") for piece in pieces)
        hooks = _PieceHooks(acknowledged, waited, check)
        if self._writes_on_plane():
            started: list[bool] = []
            try:
                return self._stream_on_plane(_noting(source, started), offset, depth, hooks)
            except BulkUnsupported as exc:
                if started:
                    raise
                _log.debug("bulk upload declined for %s (%s); writing serially", self.url, exc)
        return self._stream_serially(source, offset, hooks)

    def _stream_on_plane(
        self,
        source: Iterator[memoryview],
        offset: int,
        depth: int | None,
        hooks: _PieceHooks,
    ) -> int:
        """:meth:`write_pieces` over the bulk data plane.

        ``high`` tracks the furthest acknowledged byte so the cached size is
        invalidated up to there even when the stream fails part-way: what
        the server confirmed is on disk whatever happens next.
        """
        high = [offset]
        acknowledged = hooks.acknowledged

        def landed(at: int, count: int) -> None:
            """Per-acknowledgement hook: advance the high-water mark, then the caller's hook."""
            high[0] = max(high[0], at + count)
            if acknowledged is not None:
                acknowledged(at, count)

        try:
            with self.session.bulk(
                self.handle, chunk=self.config.chunk_size, depth=depth or self.config.bulk_depth
            ) as channel:
                done = channel.write_chunks(
                    source,
                    offset,
                    acknowledged=landed,
                    waited=hooks.waited,
                    check=hooks.check,
                    strict_replies=hooks.waited is not None,
                )
        except XrdConnectionError as exc:
            self._unless_aborted(exc)
            raise _transient(exc) from exc
        finally:
            self._invalidate(high[0])
        return done

    def _stream_serially(
        self,
        source: Iterator[memoryview],
        offset: int,
        hooks: _PieceHooks,
    ) -> int:
        """:meth:`write_pieces` one acknowledged :meth:`write` at a time."""
        done = 0
        for view in source:
            count = self.write(view, offset + done)
            if hooks.acknowledged is not None:
                hooks.acknowledged(offset + done, count)
            done += count
            if hooks.check is not None:
                hooks.check()
        return done

    def _event_write(self, view: memoryview, offset: int) -> int:
        """``view`` at ``offset`` through the event path, a chunk at a time.

        Each chunk is a slice of the caller's own buffer, framed and sent as
        it is - the request header and that slice, in one vectored send - so
        the payload is copied once, by the kernel. ``write`` returns only
        once every chunk is answered, which is what makes lending the
        caller's memory safe.
        """
        limit = self.config.chunk_size
        written = 0
        while written < len(view):
            piece = view[written : written + limit]
            at = offset + written
            if self._checkpoint:
                self._submit(r.Write(self.handle, at, piece, self._split_path(write=True)))
            else:
                self._bulk(_write_for(at, piece), write=True)
            written += len(piece)
        self._invalidate(offset + written)
        return written

    def pwrite(self, data: bytes, offset: int) -> int:
        """:func:`os.pwrite` order of arguments."""
        return self.write(data, offset)

    def writev(
        self, chunks: Iterable[WriteChunk | tuple[int, bytes]], *, sync: bool = False
    ) -> int:
        """``kXR_writev`` - many scattered writes in one round trip."""
        items = [ch if isinstance(ch, WriteChunk) else WriteChunk(ch[0], ch[1]) for ch in chunks]
        if not items:
            return 0
        if self._checkpoint:
            raise UnsupportedError(
                kXR_Unsupported,
                "kXR_writev cannot be checkpointed; write, pgwrite and truncate can",
            )
        total = 0
        high = 0
        for batch in _write_batches(items):
            request = r.WriteV([(self.handle, ch.offset, ch.data) for ch in batch], sync=sync)
            self._router.execute(request, path=self.url.path)
            for ch in batch:
                total += len(ch.data)
                high = max(high, ch.offset + len(ch.data))
        self._invalidate(high)
        return total

    def clone(
        self,
        source: _Handle,
        ranges: Iterable[CloneRange | tuple[int, int] | tuple[int, int, int]] | None = None,
    ) -> int:
        """``kXR_clone`` - have the server copy ranges of ``source`` into this file.

            >>> dst.clone(src)                       # all of it, server-side
            >>> dst.clone(src, [(4096, 1024, 0)])    # one range, moved to the front

        The bytes never cross the network: the server reads them out of one
        open handle and writes them into another, which is the cheap way to
        assemble a file out of pieces of another one. Each range is
        ``(offset, length)`` or ``(offset, length, target_offset)``, or a
        :class:`~xrdclient.types.CloneRange`; leaving ``ranges`` out copies the
        whole of ``source`` to the same offsets. Returns the bytes copied.

        Both handles must belong to the same session - a handle means nothing
        to a server that did not hand it out - so open them from one
        :class:`~xrdclient.FileSystem`.

        Opcode 3032 is not in XProtocol.hh, so this is a fast path to try
        rather than one to depend on: a server that does not implement it
        rejects the request outright, and that comes back as
        :class:`~xrdclient.errors.UnsupportedError` rather than as the bare "invalid
        request code" a stock xrootd sends.
        """
        target, origin = self.handle, source.handle  # both open, or this raises
        self._validate_clone(source)
        items = self._clone_items(source, origin, ranges)
        total = 0
        high = 0
        for start in range(0, len(items), CLONE_MAX_RANGES):
            batch = items[start : start + CLONE_MAX_RANGES]
            self._send_clone(target, batch)
            copied, extent = _clone_extent(batch)
            total += copied
            high = max(high, extent)
        if total:
            self._invalidate(high)
        return total

    def _validate_clone(self, source: _Handle) -> None:
        """Refuse a clone the server could not perform as asked.

        ``kXR_clone`` names the source by its file handle, which only means
        something on the connection that opened it, so both files must share
        one session. A checkpointed file cannot clone because the server's
        checkpoint journals only write, pgwrite and truncate.
        """
        if source._router.session is not self._router.session:
            raise ValueError(
                f"{source.url} and {self.url} are open on different connections; "
                f"a clone copies between two handles of one session"
            )
        if self._checkpoint:
            raise UnsupportedError(
                kXR_Unsupported,
                "kXR_clone cannot be checkpointed; write, pgwrite and truncate can",
            )

    @staticmethod
    def _clone_items(
        source: _Handle,
        origin: bytes,
        ranges: Iterable[CloneRange | tuple[int, int] | tuple[int, int, int]] | None,
    ) -> list[tuple[bytes, int, int, int]]:
        """``(source fhandle, offset, length, destination offset)`` per non-empty range.

        No ``ranges`` means the whole source at the same offsets.
        """
        wanted = [CloneRange(0, source.size)] if ranges is None else [_range(x) for x in ranges]
        return [
            (origin, item.offset, item.length, item.destination) for item in wanted if item.length
        ]

    def _send_clone(self, target: bytes, batch: list[tuple[bytes, int, int, int]]) -> None:
        """Send one batch of at most ``CLONE_MAX_RANGES`` ranges.

        ``kXR_clone`` is a vendor opcode outside ``XProtocol.hh``; a stock server
        answers ``kXR_InvalidRequest``, which is translated to
        :class:`UnsupportedError` so a caller can fall back to copying the bytes.
        """
        try:
            self._router.execute(r.Clone(target, batch), path=self.url.path)
        except ServerError as exc:
            if exc.code != kXR_InvalidRequest:
                raise
            raise UnsupportedError(
                kXR_Unsupported,
                f"{self.url.host} does not implement kXR_clone (opcode 3032, "
                f"outside XProtocol.hh); copy the ranges through the client",
            ) from exc

    def pgwrite(self, data: bytes, offset: int = 0) -> int:
        """``kXR_pgwrite`` - write with a CRC-32C per 4 KiB page.

        The server verifies each page as it arrives and stores the write
        either way, answering with a checksum-error trailer that names the
        pages whose CRC did not survive the wire. Each of those is
        retransmitted here with ``kXR_pgRetry`` until the server takes it or
        the retry budget runs out, which fails the write rather than leaving
        a page of corruption behind.
        """
        if not data:
            return 0
        corrupt = self._pgwrite_once(data, offset)
        for page_offset in corrupt:
            self._pgwrite_retry(data, offset, page_offset)
        self._invalidate(offset + len(data))
        return len(data)

    def _pgwrite_once(self, data: bytes, offset: int, *, retry: bool = False) -> tuple[int, ...]:
        """One ``kXR_pgwrite``, returning the offsets the server found corrupt."""
        result = self._submit(
            r.PgWrite(
                self.handle, offset, pack_pages(data, offset), retry, self._split_path(write=True)
            )
        )
        return rp.parse_pgwrite_cse(result.data) if result.data else ()

    def _pgwrite_retry(self, data: bytes, base: int, page_offset: int) -> None:
        """Resend one page until the server has it intact, or give up loudly."""
        start = page_offset - base
        if not 0 <= start < len(data):
            raise ProtocolError(
                f"kXR_pgwrite reported a corrupt page at offset {page_offset}, which is "
                f"outside the {len(data)} bytes written at offset {base}"
            )
        page = data[start : start + page_span(page_offset, len(data) - start)]
        for _ in range(c.PGW_MAX_RETRY):
            if not self._pgwrite_once(page, page_offset, retry=True):
                return
        raise PageIntegrityError(page_offset, c.PGW_MAX_RETRY, path=self.url.path)

    def truncate(self, size: int = 0) -> int:
        """``kXR_truncate`` on the open handle."""
        self._submit(r.Truncate(size=size, fhandle=self.handle))
        self._invalidate(size, exact=True)
        return size

    def sync(self) -> None:
        """``kXR_sync`` - flush the server's buffers to storage."""
        self._router.execute(r.Sync(self.handle), path=self.url.path)

    #: :func:`os.fsync` spelling.
    flush = sync

    def _invalidate(self, high_water: int, *, exact: bool = False) -> None:
        self._size_hint = high_water if exact else max(self._size_hint, high_water)
        self._stat = None


def _noting(pieces: Iterator[memoryview], started: list[bool]) -> Iterator[memoryview]:
    """``pieces``, noting in ``started`` once the first has been taken.

    What tells :meth:`_Writing.write_pieces` whether a declined plane can
    still fall back: before the first piece is taken nothing was consumed,
    and the serial path can have the whole stream.
    """
    for piece in pieces:
        if not started:
            started.append(True)
        yield piece
