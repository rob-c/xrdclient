"""Reading an open handle: plain, vector, paged, and over the bulk data plane.

The read half of :class:`~xrdclient.client.file.File`. Every method here
names the handle it reads and lets :class:`~._handle._Handle` decide which
connection, which data path and which incarnation of the handle carry it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence

from ..crypto.crc32c import unpack_pages
from ..errors import ConnectionError as XrdConnectionError
from ..errors import (
    ProtocolError,
)
from ..log import get_logger
from ..proto import constants as c
from ..proto import requests as r
from ..proto import responses as rp
from ..proto.frames import Request
from ..session.bulk import BulkUnsupported
from ..session.deadline import OperationExpiredError
from ..types import (
    ChecksumInfo,
    PageResult,
    ReadRange,
)
from ._handle import (
    _MAX_AHEAD,
    _MIN_BULK_CHUNK,
    _MIN_BULK_READ,
    _WRITING,
    _Handle,
    _Landing,
    _transient,
)
from ._vector import (
    READV_MAX_ELEMENT,
    _as_asked,
    _as_range,
    _assemble,
    _batches,
    _by_offset,
    _PackedReadV,
    _pieces,
    _readv_for,
    _segment_for,
)

_log = get_logger("xrdclient.client.file")


class _Reading(_Handle):
    """The read operations of an open file."""

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def checksum(self, algorithm: str | None = None) -> ChecksumInfo:
        """Ask the server to checksum this file."""
        path = self.url.path_with_cgi
        if algorithm:
            path += ("&" if "?" in path else "?") + f"cks.type={algorithm}"
        result = self._router.execute(r.Query(c.kXR_Qcksum, path), path=self.url.path)
        return rp.parse_checksum(result.data)

    def visa(self) -> bytes:
        """``kXR_query`` visa - opaque server metadata about this handle."""
        return self._execute(lambda handle: r.Query(c.kXR_Qvisa, fhandle=handle)).data

    def fcntl(self, data: bytes = b"") -> bytes:
        """A storage-specific operation on this handle, as XrdCl's ``File::Fcntl``.

        ``data`` goes to the server as it is, in a ``kXR_query`` of type
        ``kXR_Qopaqug`` that names this handle; the answer comes back as it
        is. What either means is up to the storage plug-in behind the server:
        a stock xrootd has none, and refuses with
        :class:`~xrdclient.errors.UnsupportedError` ("fctl operation not
        supported").
        """
        payload = bytes(data)
        return self._execute(lambda handle: r.Query(c.kXR_Qopaqug, payload, fhandle=handle)).data

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def read(self, size: int = -1, offset: int = 0) -> bytes:
        """Read ``size`` bytes at ``offset``; ``-1`` means to end of file.

        Large reads are split into ``config.chunk_size`` requests so that one
        stalled request cannot hold an unbounded buffer, and a read to the end
        of a file bigger than ``config.max_read_size`` is refused with a
        :class:`~xrdclient.errors.TooLargeError` rather than allocated.

        On a handle opened only for reading, a read that starts where the
        previous one ended also fetches ``config.readahead`` bytes beyond it
        (doubling, while the reads stay sequential) and later reads are served
        from them - see :meth:`_sequential`. ``readahead=0`` turns that off.
        """
        if size < 0:
            size = max(self.size - offset, 0)
            self.config.check_whole_read(size, self.url.path)
        if size == 0:
            return b""
        if self._reads_on_plane(size):
            ahead = self._reads_ahead(size)
            data = self._sequential(size, offset) if ahead else self._plane_read(size, offset)
            if data is not None:
                return data
        return self._event_read(size, offset)

    def _reads_ahead(self, size: int) -> bool:
        """Whether a read of ``size`` may be served by, or fill, the readahead.

        Only on a handle that cannot write: its own writes would otherwise
        have to be tracked against the block, and the readahead is as fresh
        as a buffered reader's is, which a reader of a file others may be
        writing already accepts.
        """
        return bool(self.config.readahead) and size < _MAX_AHEAD and not self._flags & _WRITING

    def _sequential(self, size: int, offset: int) -> bytes | None:
        """A read that may be answered from, or read ahead for, the next ones.

        Answered from the block already fetched when it lies inside it.
        Otherwise it goes to the server - and when it starts where the last
        read stopped, it asks for the window beyond it too and keeps that for
        the reads that follow. Random access never reads ahead, and resets
        the window, so it costs nothing but the check.
        """
        end = offset + size
        with self._state:
            start, block = self._ahead
            if start <= offset and end <= start + len(block):
                self._sequel = end
                return block[offset - start : end - start]
            streak, self._sequel = offset == self._sequel, end
            window = 0
            if not streak:
                self._window = self.config.readahead
            else:
                window, self._window = self._window, min(self._window * 2, _MAX_AHEAD)
        # The fetch itself is outside the lock: a read on another thread must
        # not wait for this one's network round trip just to check the block.
        if not streak:
            return self._plane_read(size, offset)
        data = self._plane_read(size + window, offset)
        if data is None:
            return None
        self._ahead = (offset, data)
        return data[:size]

    def _event_read(self, size: int, offset: int) -> bytes:
        """``size`` bytes at ``offset`` through the event path, a chunk at a time."""
        limit = self.config.chunk_size
        if size <= limit:
            return self._read_one(offset, size)
        parts = []
        remaining = size
        at = offset
        while remaining:
            n = min(remaining, limit)
            chunk = self._read_one(at, n)
            parts.append(chunk)
            if len(chunk) < n:
                break  # short read: end of file
            at += n
            remaining -= n
        return b"".join(parts)

    def _read_one(self, offset: int, length: int) -> bytes:
        """One ``kXR_read``, on a bound data path when there is one (see ``_bulk``).

        A reply longer than the read is refused by the session machine,
        which caps what it will accumulate from the request itself; there
        is nothing left to check by the time the bytes get here.
        """
        return self._bulk(lambda handle, pid: r.Read(handle, offset, length, pid)).data

    def pread(self, size: int, offset: int) -> bytes:
        """:func:`os.pread` order of arguments."""
        return self.read(size, offset)

    def readinto(self, buffer: bytearray | memoryview, offset: int = 0) -> int:
        """Read into a pre-allocated buffer; returns the byte count.

        Served by the bulk data plane where it can be: several reads in flight
        at once, each landing straight in its own slice of ``buffer``, so the
        bytes are never copied between the socket and the caller. A server
        that answers a read with something other than bytes takes the
        ordinary path.
        """
        view = memoryview(buffer).cast("B")
        if not view:
            return 0
        if self._reads_on_plane(len(view)):
            got = self._plane_readinto(view, offset)
            if got is not None:
                return got
        data = self._event_read(len(view), offset)
        view[: len(data)] = data
        return len(data)

    def _reads_on_plane(self, size: int) -> bool:
        """Whether a read of ``size`` bytes goes over the bulk data plane.

        Every read does, whatever its size - one request framed and received
        directly is a fraction of the interpreter time the event path spends
        on it - except where a data path has been asked for: a small read on
        a handle the caller bound one for (:meth:`bind_data_path`) keeps its
        answers there, and so does one whose automatic paths the server
        serves requests on.
        """
        if not self.config.bulk:
            return False
        if size >= _MIN_BULK_READ:
            return True
        return not self._pathid and self._paths_idle()

    def _recover_from_plane(self, error: XrdConnectionError, seen: int) -> None:
        """Re-open after the bulk plane lost the connection, or raise.

        The plane has already marked the connection broken - its wire is out
        of step, with replies still owed on it - so nothing may be sent on it
        again, the event path included. A handle that can be re-opened is,
        on a fresh one, and the caller's read is then run on the event path
        there; one that cannot gets the failure, as the
        :class:`~xrdclient.errors.TransientError` the event path would have
        raised.
        """
        self._unless_aborted(error)
        if not self.recoverable or isinstance(error, OperationExpiredError):
            raise _transient(error)
        _log.debug("recovering %s after the bulk plane lost its connection: %s", self.url, error)
        self._reopen(seen)

    def _recover_from_step(self, error: ProtocolError, seen: int) -> None:
        """Re-open after the bulk plane found its wire out of step, or re-raise.

        A plane that could not tell whose
        reply it was reading has marked the connection broken, which a
        handle that can be re-opened survives, as it does a lost connection:
        the read is run again, on the event path, over a fresh one. Anything
        else - a connection still sound, a handle that cannot be re-opened -
        is the caller's to see.
        """
        self._unless_aborted(error)
        if not (self.session.broken and self.recoverable):
            raise error
        _log.debug("recovering %s after the bulk plane lost step with the server", self.url)
        self._reopen(seen)

    def _plane_read(self, size: int, offset: int) -> bytes | None:
        """``size`` bytes at ``offset`` off the bulk data plane, or ``None``.

        The bytes are received straight into the buffer of the ``bytes``
        object that is returned - :class:`io.BytesIO` hands out a writable
        view of its own storage and then the storage itself - so a gigabyte
        read costs one allocation and no copy. ``None`` means the plane was
        declined and the caller should use the event path.
        """
        buffer = _Landing(bytes(size))
        view = buffer.getbuffer()
        try:
            got = self._plane_readinto(view, offset)
        finally:
            view.release()
        if got is None:
            return None
        data = buffer.getvalue()
        return data if got == size else data[:got]

    def _plane_readinto(self, view: memoryview, offset: int) -> int | None:
        """One pipelined, zero-copy fill of ``view``, or ``None`` if declined.

        The request size is whatever divides this buffer into a full pipeline:
        a caller asking for exactly one chunk would otherwise get one read at a
        time, which is the round trip the pipeline exists to hide. A handle
        that can be re-opened is, on the event path, if its connection went.
        """
        depth = self.config.bulk_depth
        chunk = max(_MIN_BULK_CHUNK, min(self.config.bulk_chunk, -(-len(view) // depth)))
        depth = min(depth, -(-len(view) // chunk))
        seen = self.recoveries
        try:
            with self.session.bulk(self.handle, chunk=chunk, depth=depth) as reader:
                return reader.into(view, offset)
        except BulkUnsupported as exc:
            _log.debug("bulk read declined for %s (%s); using the event path", self.url, exc)
        except XrdConnectionError as exc:
            self._recover_from_plane(exc, seen)
        except ProtocolError as exc:
            self._recover_from_step(exc, seen)
        return None

    def readv(self, ranges: Iterable[ReadRange | tuple[int, int]]) -> list[bytes]:
        """``kXR_readv`` - many scattered ranges in as few round trips as possible.

        Returns one ``bytes`` per requested range, in the order asked for,
        regardless of how the server batches or reorders them.
        """
        wanted = [_as_range(rng) for rng in ranges]
        if not wanted:
            return []
        if all(rng.length <= READV_MAX_ELEMENT for rng in wanted):
            return self._readv_elements(wanted)
        # A range longer than the server takes in one element goes out as
        # several, and is stitched back together below; the caller never sees
        # the ceiling.
        out = _by_offset(self._readv_bodies([p for rng in wanted for p in _pieces(rng)]))
        return [_assemble(rng, out) for rng in wanted]

    def _readv_elements(self, wanted: list[ReadRange]) -> list[bytes]:
        """``wanted``, every range of which the server takes as one element."""
        bodies = self._readv_bodies(wanted)
        # The server answers in the order it was asked, whole ranges, all but
        # always; checking that is cheaper than filing each segment by its
        # offset, which is what anything else falls back to.
        quick = _as_asked(bodies, wanted)
        if quick is not None:
            return quick
        out = _by_offset(bodies)
        return [_segment_for(rng, out.get(rng.offset)) for rng in wanted]

    def _readv_bodies(self, pieces: Sequence[ReadRange]) -> list[memoryview]:
        """The reply body of every ``kXR_readv`` it takes to fetch ``pieces``."""
        batches = list(_batches(pieces))
        bodies = self._plane_readv(batches) if self._reads_on_plane(0) else None
        if bodies is None:
            bodies = [memoryview(self._execute(_readv_for(b, self)).data) for b in batches]
        return bodies

    def _plane_readv(self, batches: list[list[ReadRange]]) -> list[memoryview] | None:
        """Every batch's reply body off the bulk data plane, or ``None`` if declined.

        The batches go out together, ``config.bulk_depth`` at a time, so a
        vector read too big for one request still costs about one round trip.
        """
        handle = self.handle
        requests: list[tuple[Request, int]] = []
        for batch in batches:
            request = _PackedReadV([(handle, rng.offset, rng.length) for rng in batch])
            requests.append((request, request.reply_cap()))
        depth = min(self.config.bulk_depth, len(requests))
        seen = self.recoveries
        try:
            with self.session.bulk(handle, chunk=1, depth=depth) as channel:
                return channel.gather(requests)
        except BulkUnsupported as exc:
            _log.debug("bulk readv declined for %s (%s); using the event path", self.url, exc)
        except XrdConnectionError as exc:
            self._recover_from_plane(exc, seen)
        except ProtocolError as exc:
            self._recover_from_step(exc, seen)
        return None

    def pgread(self, size: int, offset: int, *, verify: bool = True) -> PageResult:
        """``kXR_pgread`` - read with a CRC-32C per 4 KiB page.

        With ``verify`` the checksums are checked here and any failing page
        offsets come back in :attr:`~xrdclient.types.PageResult.corrupt_pages`.
        """
        result = self._execute(lambda handle: r.PgRead(handle, offset, size, pathid=self._pathid))
        if not verify:
            data, _ = unpack_pages(result.data, offset)
            return PageResult(data, offset)
        data, corrupt = unpack_pages(result.data, offset)
        return PageResult(data, offset, corrupt)

    def __iter__(self) -> Iterator[bytes]:
        """Iterate the file in ``config.chunk_size`` blocks."""
        offset = 0
        while True:
            chunk = self.read(self.config.chunk_size, offset)
            if not chunk:
                return
            yield chunk
            offset += len(chunk)
