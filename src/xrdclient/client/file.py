"""An open remote file.

:class:`File` is the low-level handle: every method is one protocol
operation, with no buffering and no implicit position. The file-like objects
in :mod:`xrdclient.io` are built on top of it, and that is what most code should
use. Reach for :class:`File` when you want vector reads, paged I/O with
per-page checksums, extended attributes on a handle, or checkpoints.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager

from .._log import get_logger
from ..config import Config
from ..crypto.crc32c import pack_pages, page_span, unpack_pages
from ..errors import (
    ChecksumMismatchError,
    PageIntegrityError,
    ProtocolError,
    ServerError,
    TransientError,
    UnsupportedError,
    XRootDError,
    kXR_InvalidRequest,
    kXR_Unsupported,
)
from ..flags import Access, ChkPointCode, OpenFlags, open_flags, permissions
from ..proto import constants as c
from ..proto import requests as r
from ..proto import responses as rp
from ..proto.frames import Request
from ..session.bulk import BulkUnsupported
from ..session.router import Router
from ..session.sync import Result, Session
from ..types import (
    CheckpointInfo,
    ChecksumInfo,
    CloneRange,
    PageResult,
    ReadRange,
    StatInfo,
    WriteChunk,
)
from ..url import XRootDURL, parse

__all__ = ["File", "Checkpoint"]

_log = get_logger(__name__)

#: Server-side ceilings on one ``kXR_readv``.
READV_MAX_CHUNKS = 1024
READV_MAX_BYTES = 2 << 20

#: ``maxClonesz`` - how many ranges one ``kXR_clone`` may carry.
CLONE_MAX_RANGES = 1024

#: Opening with any of these means the handle has side effects on the server,
#: so it is not safe to silently re-open: ``NEW`` and ``DELETE`` would recreate
#: or re-truncate the file, and a re-opened writer would have lost whatever the
#: dead connection had not yet flushed.
_WRITING = OpenFlags.WRITE | OpenFlags.UPDATE | OpenFlags.NEW | OpenFlags.DELETE | OpenFlags.APPEND

#: Smallest read worth taking off the event path. Below this the pipeline
#: cannot pay for the connection it borrows, and one request is one request
#: either way.
_MIN_BULK_READ = 1 << 20

#: Floor on a bulk request, so dividing a buffer across the pipeline never
#: turns one read into a burst of tiny ones.
_MIN_BULK_CHUNK = 1 << 18


class File:
    """One open file handle on one data server."""

    def __init__(
        self,
        url: str | XRootDURL,
        config: Config | None = None,
        *,
        router: Router | None = None,
    ) -> None:
        self.url = parse(url) if isinstance(url, str) else url
        self.config = config or Config()
        self._router = router or Router(self.url, self.config)
        self._owns_router = router is None
        self._handle: bytes | None = None
        self._stat: StatInfo | None = None
        self._compression: tuple[int, str] = (0, "")
        self._size_hint = 0
        self._flags = OpenFlags.NONE
        self._mode = 0
        self._checkpoint = False
        self._pathid = 0
        #: Data sub-streams bound automatically at open for bulk transfer, and
        #: a cursor that spreads chunks over them. ``_multistream`` latches off
        #: for the file the first time a server declines a bound bulk op, so the
        #: rest of the transfer runs on the control link without retrying it.
        self._data_paths: list[int] = []
        self._rr = 0
        self._multistream = True
        #: How many times this handle has been re-opened after losing its
        #: server. Zero on a healthy connection; useful in a log line when a
        #: long read survived a restart nobody noticed.
        self.recoveries = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    @property
    def handle(self) -> bytes:
        if self._handle is None:
            raise ValueError("I/O operation on closed file")
        return self._handle

    @property
    def endpoint(self) -> str:
        return self._router.endpoint

    @property
    def session(self) -> Session:
        """The live connection this handle is open on.

        Bulk transfer borrows it outright - see
        :meth:`xrdclient.session.sync.Session.bulk` - which is the one thing in the
        library that needs the connection rather than the request API.
        """
        return self._router.session

    @property
    def data_path(self) -> int:
        """The bound data path this handle's bulk I/O uses, or 0 for none."""
        return self._pathid

    def bind_data_path(self) -> int:
        """Move this handle's bulk I/O onto a second connection.

        Opens one connection more to the same server, binds it to the same
        session, and routes every subsequent read and write over it - the
        requests still go out on the control link, so a stat or a close is
        never stuck behind a megabyte of file. Returns the path id, and is
        idempotent: a handle already bound keeps the path it has.

        The path belongs to the connection. If the data server vanishes and
        the handle is re-opened elsewhere, the binding is not carried over -
        :attr:`data_path` goes back to 0 and this can be called again.
        """
        if not self._pathid:
            self._pathid = self._router.bind_data_path()
        return self._pathid

    def _bind_data_streams(self) -> None:
        """Top the automatic bulk-transfer data streams up to the configured
        number, which at open is all of them.

        Best-effort: a server that refuses ``kXR_bind`` (or speaks HTTP, which
        has none) simply leaves the transfer on the control link. The manual
        :meth:`bind_data_path` is untouched - these are a separate, automatic
        set that :meth:`read` and :meth:`write` spread their chunks over.
        """
        for _ in range(self.config.data_streams - len(self._data_paths)):
            try:
                pathid = self._router.bind_data_path()
            except Exception:  # binding is strictly optional
                break
            if not pathid:
                break
            self._data_paths.append(pathid)

    def _next_path(self) -> int:
        """The next automatic data path to carry a bulk chunk, round-robin, or
        0 when there are none or the file has fallen back to the control link.
        """
        if not self._multistream or not self._data_paths:
            return 0
        pathid = self._data_paths[self._rr % len(self._data_paths)]
        self._rr += 1
        return pathid

    def _bulk(self, build: Callable[[bytes, int], Request]) -> Result:
        """Run one bulk read/write, over an automatic data path when there is
        one and the server serves it there, otherwise on the control link.

        ``build(handle, pathid)`` makes the request for a given handle and path
        id. There are three ways to run it, and each falls back to the next.
        A server that answers what arrives on the data socket is asked there;
        one that does not gets the standard split instead - the request on the
        control link, the bytes on the bound socket - which is what XProtocol
        describes and what the first attempt cost a
        :attr:`~xrdclient.Config.data_stream_timeout` to rule out, once per
        connection rather than once per file. A server that declines that too
        latches multi-stream off for this file and the identical op is re-run
        on the control link (pathid 0), an idempotent read or write at the same
        offset, so the transfer stays byte-exact on any server. A checkpoint
        journals only what it was handed, so a checkpointed write stays on the
        control link where :meth:`_submit` can route it.
        """
        pathid = self._next_path()
        if not pathid or self._checkpoint:
            return self._execute(lambda handle: build(handle, self._pathid))
        # One attempt, straight at the session so a server that will not serve
        # the bound op is not chased through the router's reconnect loop.
        if self._router.session.arrives_on_path is not False:
            try:
                return self._router.session.execute(
                    build(self.handle, pathid), path=self.url.path, arrive_on_path=True
                )
            except (XRootDError, ValueError) as exc:
                _log.debug("data path %d does not serve what arrives on it: %s", pathid, exc)
                # The session let that socket go with the request on it. Take
                # a fresh one and use it the way the specification says.
                self._data_paths.remove(pathid)
                self._bind_data_streams()
                pathid = self._next_path()
                if not pathid:
                    return self._execute(lambda handle: build(handle, 0))
        try:
            return self._execute(lambda handle: build(handle, pathid))
        except (XRootDError, ValueError) as exc:
            if self._handle is None:
                # Recovery tried and failed, and closed the file on its way
                # out. There is no handle left to re-run anything against, so
                # the caller gets what actually went wrong.
                raise
            self._multistream = False
            _log.debug("data path %d fell back to the control link: %s", pathid, exc)
            return self._execute(lambda handle: build(handle, 0))

    def open(
        self,
        flags: OpenFlags | int | str = OpenFlags.READ,
        mode: Access | int | str = Access.OWNER_READ | Access.OWNER_WRITE,
    ) -> StatInfo | None:
        """``kXR_open``. Returns the stat the server volunteered, if any.

        Say what you want in whichever way reads best::

            fh.open("r")                              # as for the builtin
            fh.open("w", "rw-r--r--")                 # and its permissions
            fh.open("new makepath")                   # the protocol's names
            fh.open(OpenFlags.NEW | OpenFlags.MAKEPATH)   # or the bits

        A string of mode letters is a mode; any other string is read as
        option names. ``mode`` is the permission set for a file this creates.
        """
        if self._handle is not None:
            raise ValueError(f"{self.url} is already open")
        self._flags, self._mode = open_flags(flags), permissions(mode)
        try:
            self._do_open()
            self._bind_data_streams()
        except BaseException:
            # An open that fails leaves nothing to close, so nobody closes it,
            # so a caller that catches FileExistsError in a loop would hold a
            # connection per attempt. Only a connection this handle made
            # itself is dropped: a shared router belongs to its owner.
            if self._owns_router:
                self._router.close()
            raise
        return self._stat

    def _do_open(self) -> bytes:
        """Issue the ``kXR_open`` and adopt the handle it returns."""
        request = r.Open(self.url.path_with_cgi, int(self._flags) | c.kXR_retstat, self._mode)
        result = self._router.execute(request, path=self.url.path)
        # The open may have been redirected; every later operation on this
        # handle must stay on the server that issued it. A connection this
        # handle made itself is handed over rather than shared, so that there
        # is exactly one router responsible for putting it back.
        self._router = self._router.pin(transfer=self._owns_router)
        self._handle, self._stat, self._compression = rp.parse_open(result.data, self.url.path)
        if self._stat is not None:
            self._size_hint = self._stat.st_size
        return self.handle

    @property
    def compression(self) -> tuple[int, str]:
        """The compression page size and algorithm the open reported.

        ``(0, "")`` for a file stored whole, which is every file a modern
        server has: the fields survive in the ``kXR_open`` reply, and reading
        them is how you can tell rather than assume.
        """
        return self._compression

    @property
    def recoverable(self) -> bool:
        """Whether losing the connection is survivable rather than fatal.

        A read-only handle can be got back: re-opening the same path yields a
        file with the same contents, and every read carries its own offset, so
        nothing was lost with the connection. A handle opened for writing
        cannot — see :data:`_WRITING`.
        """
        return bool(self.config.recover_handles) and not (self._flags & _WRITING)

    def _execute(self, build: Callable[[bytes], Request], **kwargs: object) -> Result:
        """Run a handle-bearing request, re-opening once if the server is lost.

        ``build`` takes the handle rather than closing over it, because a
        recovered file has a different one: the retry has to be issued against
        the new handle, not the dead one.
        """
        kwargs.setdefault("path", self.url.path)
        try:
            return self._router.execute(build(self.handle), **kwargs)  # type: ignore[arg-type]
        except TransientError as exc:
            if not self.recoverable:
                raise
            _log.debug("recovering %s after %s", self.url, exc)
            handle = self._reopen()
            return self._router.execute(build(handle), **kwargs)  # type: ignore[arg-type]

    def _reopen(self) -> bytes:
        """Get the handle back on a fresh connection, from the original URL.

        Not from the pinned endpoint: the data server that just went away is
        the least likely one to answer, and going back to where the open
        started is what lets a manager route around it.
        """
        stale, self._handle, self._stat = self._router, None, None
        self._pathid = 0
        # The data paths belonged to the connection that just died; drop them
        # and let the fresh open bind its own.
        self._data_paths = []
        self._rr = 0
        self._multistream = True
        if self._owns_router:
            # Discarded, not pooled: this connection has just failed under a
            # live handle, and the next caller deserves better than that.
            stale.discard()
        self._router = Router(self.url, self.config)
        self._owns_router = True
        self.recoveries += 1
        handle = self._do_open()
        self._bind_data_streams()
        return handle

    def close(self) -> None:
        """``kXR_close``, then release the connection. Idempotent.

        A handle that was never opened still owns a connection, so the
        release happens either way.

        On a reader, a connection that has already gone is not an error -
        closing is what releases it, and there is nothing left to lose. On a
        writer it is: the close is the point at which the server commits the
        file, and a caller told the write succeeded when the server never
        heard the close would go on to trust a file that may be short. That
        failure is raised, not logged.
        """
        handle, self._handle = self._handle, None
        try:
            if handle is not None:
                self._router.execute(r.Close(handle))
        except TransientError as exc:
            if self._flags & _WRITING:
                raise
            _log.debug("close of %s found the connection gone: %s", self.url, exc)
        finally:
            if self._owns_router:
                self._router.close()

    def __enter__(self) -> File:
        if self._handle is None:
            self.open()
        return self

    def __exit__(self, exc_type: object, *_: object) -> None:
        # A failing close is worth raising, but not over the top of the
        # exception that is already on its way out of the ``with`` body:
        # that one is the reason the block is unwinding.
        try:
            self.close()
        except Exception:
            if exc_type is None:
                raise
            _log.debug("close of %s failed while unwinding", self.url, exc_info=True)

    def __repr__(self) -> str:
        state = "open" if self.is_open else "closed"
        return f"File({str(self.url)!r}, {state})"

    def __fspath__(self) -> str:
        return str(self.url)

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def stat(self, *, refresh: bool = False) -> StatInfo:
        """Stat the open handle."""
        if self._stat is not None and not refresh:
            return self._stat
        result = self._execute(lambda handle: r.Stat(fhandle=handle))
        self._stat = rp.parse_stat(result.data, self.url.path)
        self._size_hint = self._stat.st_size
        return self._stat

    @property
    def size(self) -> int:
        """File length; cached from the open, refreshed on demand."""
        return self._size_hint if self._stat is not None else self.stat().st_size

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

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def read(self, size: int = -1, offset: int = 0) -> bytes:
        """Read ``size`` bytes at ``offset``; ``-1`` means to end of file.

        Large reads are split into ``config.chunk_size`` requests so that one
        stalled request cannot hold an unbounded buffer, and a read to the end
        of a file bigger than ``config.max_read_size`` is refused with a
        :class:`~xrdclient.errors.TooLargeError` rather than allocated.
        """
        if size < 0:
            size = max(self.size - offset, 0)
            self.config.check_whole_read(size, self.url.path)
        if size == 0:
            return b""
        if self.config.bulk and size >= _MIN_BULK_READ:
            # One allocation, filled by the pipeline. The ordinary path below
            # builds the same bytes out of a list of chunks and a join, which
            # is two more copies of every byte than this needs.
            buffer = bytearray(size)
            try:
                got = self._bulk_readinto(memoryview(buffer), offset)
            except BulkUnsupported as exc:
                _log.debug("bulk read declined for %s (%s); using the event path", self.url, exc)
            else:
                return bytes(buffer) if got == size else bytes(buffer[:got])
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
        # A reply longer than the read is refused by the session machine,
        # which caps what it will accumulate from the request itself; there
        # is nothing left to check by the time the bytes get here.
        return self._bulk(lambda handle, pid: r.Read(handle, offset, length, pid)).data

    def pread(self, size: int, offset: int) -> bytes:
        """:func:`os.pread` order of arguments."""
        return self.read(size, offset)

    def readinto(self, buffer: bytearray | memoryview, offset: int = 0) -> int:
        """Read into a pre-allocated buffer; returns the byte count.

        A request big enough to be worth it is served by the bulk data plane:
        several reads in flight at once, each landing straight in its own
        slice of ``buffer``, so the bytes are never copied between the socket
        and the caller. Anything smaller, or a server that answers a read with
        something other than bytes, takes the ordinary path.
        """
        view = memoryview(buffer).cast("B")
        if not view:
            return 0
        if self.config.bulk and len(view) >= _MIN_BULK_READ:
            try:
                return self._bulk_readinto(view, offset)
            except BulkUnsupported as exc:
                _log.debug("bulk read declined for %s (%s); using the event path", self.url, exc)
        data = self.read(len(view), offset)
        view[: len(data)] = data
        return len(data)

    def _bulk_readinto(self, view: memoryview, offset: int) -> int:
        """One pipelined, zero-copy fill of ``view``, on this file's connection.

        The request size is whatever divides this buffer into a full pipeline:
        a caller asking for exactly one chunk would otherwise get one read at a
        time, which is the round trip the pipeline exists to hide.
        """
        depth = self.config.bulk_depth
        chunk = max(_MIN_BULK_CHUNK, min(self.config.bulk_chunk, -(-len(view) // depth)))
        with self.session.bulk(self.handle, chunk=chunk, depth=depth) as reader:
            return reader.into(view, offset)

    def readv(self, ranges: Iterable[ReadRange | tuple[int, int]]) -> list[bytes]:
        """``kXR_readv`` - many scattered ranges in as few round trips as possible.

        Returns one ``bytes`` per requested range, in the order asked for,
        regardless of how the server batches or reorders them.
        """
        wanted = [
            rng if isinstance(rng, ReadRange) else ReadRange(rng[0], rng[1]) for rng in ranges
        ]
        if not wanted:
            return []
        out: dict[int, list[bytes]] = {}
        for batch in _batches(wanted):
            result = self._execute(_readv_for(batch, self))
            for segment in rp.parse_readv(result.data):
                out.setdefault(segment.offset, []).append(segment.data)
        return [_segment_for(rng, out.get(rng.offset)) for rng in wanted]

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
            chunk = self._read_one(offset, self.config.chunk_size)
            if not chunk:
                return
            yield chunk
            offset += len(chunk)

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

    def write(self, data: bytes, offset: int = 0) -> int:
        """``kXR_write``. Returns the number of bytes accepted."""
        view = memoryview(data).cast("B")
        limit = self.config.chunk_size
        written = 0
        while written < len(view):
            piece = view[written : written + limit]
            at = offset + written
            body = piece.tobytes()
            if self._checkpoint:
                self._submit(r.Write(self.handle, at, body, self._pathid))
            else:
                self._bulk(_write_for(at, body))
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
        source: File,
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

    def _validate_clone(self, source: File) -> None:
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
        source: File,
        origin: bytes,
        ranges: Iterable[CloneRange | tuple[int, int] | tuple[int, int, int]] | None,
    ) -> list[tuple[bytes, int, int, int]]:
        wanted = [CloneRange(0, source.size)] if ranges is None else [_range(x) for x in ranges]
        return [
            (origin, item.offset, item.length, item.destination) for item in wanted if item.length
        ]

    def _send_clone(self, target: bytes, batch: list[tuple[bytes, int, int, int]]) -> None:
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
            r.PgWrite(self.handle, offset, pack_pages(data, offset), retry, self._pathid)
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

    # ------------------------------------------------------------------
    # Extended attributes on the handle
    # ------------------------------------------------------------------

    def getxattr(self, name: str) -> bytes:
        result = self._execute(lambda handle: r.Fattr.get("", name, fhandle=handle))
        for item in rp.parse_fattr(result.data).items:
            if item.code == 0 and item.value is not None:
                return item.value
        raise KeyError(name)

    def setxattr(self, name: str, value: bytes) -> None:
        self._router.execute(r.Fattr.set("", name, value, fhandle=self.handle), path=self.url.path)

    def listxattr(self) -> list[str]:
        result = self._execute(lambda handle: r.Fattr.list("", fhandle=handle))
        return [i.name for i in rp.parse_fattr(result.data, values=False).items]

    def removexattr(self, name: str) -> None:
        self._router.execute(r.Fattr.delete("", name, fhandle=self.handle), path=self.url.path)

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------

    @contextmanager
    def checkpoint(self) -> Iterator[Checkpoint]:
        """Transactional writes: commit on clean exit, roll back on error.

            >>> with fh.checkpoint() as cp:
            ...     fh.write(payload, offset)
            ...     cp.query().free
            65536

        Every :meth:`write`, :meth:`pgwrite` and :meth:`truncate` inside the
        block goes to the server as ``kXR_ckpXeq`` and so is undone by the
        rollback; :meth:`writev` is not one of the three the server can undo
        and refuses. Requires server-side ``kXR_chkpoint`` support; a server
        without it raises :class:`~xrdclient.errors.UnsupportedError` on entry,
        before any write has happened.

        Checkpoints do not nest - the server keeps one per handle.
        """
        if self._checkpoint:
            raise UnsupportedError(kXR_Unsupported, f"{self.url} already has a checkpoint open")
        self._router.execute(r.ChkPoint(self.handle, int(ChkPointCode.BEGIN)))
        self._checkpoint = True
        try:
            yield Checkpoint(self)
        except BaseException:
            self._checkpoint = False
            self._router.execute(r.ChkPoint(self.handle, int(ChkPointCode.ROLLBACK)))
            self._stat = None  # the file is back to a size this handle never saw
            raise
        else:
            self._checkpoint = False
            self._router.execute(r.ChkPoint(self.handle, int(ChkPointCode.COMMIT)))

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify(self, expected: str, algorithm: str = "adler32") -> None:
        """Compare the server's checksum against ``expected``."""
        actual = self.checksum(algorithm)
        if actual.value.lower() != expected.lower():
            raise ChecksumMismatchError(algorithm, expected.lower(), actual.value.lower())


class Checkpoint:
    """The checkpoint a :meth:`File.checkpoint` block is writing into.

    There is nothing to do with it in the common case - the writes go through
    the file as usual - but the server will only journal so much, and this is
    where you ask how much of that is left.
    """

    __slots__ = ("file",)

    def __init__(self, file: File) -> None:
        self.file = file

    def query(self) -> CheckpointInfo:
        """``kXR_ckpQuery`` - how much room the journal has left."""
        result = self.file._router.execute(
            r.ChkPoint(self.file.handle, int(ChkPointCode.QUERY)),
            path=self.file.url.path,
        )
        return rp.parse_checkpoint(result.data)

    def __repr__(self) -> str:
        return f"Checkpoint({str(self.file.url)!r})"


def _readv_for(batch: Sequence[ReadRange], file: File) -> Callable[[bytes], Request]:
    """Bind ``batch`` to a builder that still takes the handle as an argument.

    :meth:`File._execute` re-opens a lost file and retries, and the retry must
    be issued against the new handle - so the ranges are captured here and the
    handle is not.
    """
    return lambda handle: r.ReadV(
        [(handle, rng.offset, rng.length) for rng in batch], file.data_path
    )


def _write_for(at: int, body: bytes) -> Callable[[bytes, int], Request]:
    """Bind one chunk to a builder that still takes the handle and the path.

    Same reason as :func:`_readv_for`, and one more: :meth:`File._bulk` may run
    the builder again on another path while the loop that made it has moved on
    to the next chunk, so the chunk it was made for is captured here.
    """
    return lambda handle, pid: r.Write(handle, at, body, pid)


def _segment_for(wanted: ReadRange, answers: list[bytes] | None) -> bytes:
    """One vector-read segment, or a refusal - never a silent empty string."""
    if not answers:
        raise ProtocolError(
            f"the server left the {wanted.length} bytes at offset {wanted.offset} "
            "out of its vector read reply"
        )
    data = answers.pop(0) if len(answers) > 1 else answers[0]
    if len(data) > wanted.length:
        raise ProtocolError(
            f"the server answered a {wanted.length} byte vector-read segment at "
            f"offset {wanted.offset} with {len(data)} bytes"
        )
    return data


def _batches(ranges: Sequence[ReadRange]) -> Iterator[list[ReadRange]]:
    """Split vector reads to respect the server's per-request ceilings."""
    batch: list[ReadRange] = []
    total = 0
    for rng in ranges:
        if rng.length > READV_MAX_BYTES:
            raise ProtocolError(
                f"readv element of {rng.length} bytes exceeds the {READV_MAX_BYTES} limit; "
                "use read() for ranges this large"
            )
        if batch and (len(batch) >= READV_MAX_CHUNKS or total + rng.length > READV_MAX_BYTES):
            yield batch
            batch, total = [], 0
        batch.append(rng)
        total += rng.length
    if batch:
        yield batch


def _range(item: CloneRange | tuple[int, int] | tuple[int, int, int]) -> CloneRange:
    """One clone range, however it was spelled."""
    span = item if isinstance(item, CloneRange) else CloneRange(*item)
    if span.offset < 0 or span.length < 0 or span.destination < 0:
        raise ValueError(f"a clone range is two offsets and a length, none negative: {span}")
    return span


def _clone_extent(batch: list[tuple[bytes, int, int, int]]) -> tuple[int, int]:
    copied = sum(length for _, _, length, _ in batch)
    extent = max((destination + length for _, _, length, destination in batch), default=0)
    return copied, extent


def _write_batches(chunks: Sequence[WriteChunk]) -> Iterator[list[WriteChunk]]:
    batch: list[WriteChunk] = []
    total = 0
    for chunk in chunks:
        if batch and (len(batch) >= READV_MAX_CHUNKS or total + len(chunk.data) > READV_MAX_BYTES):
            yield batch
            batch, total = [], 0
        batch.append(chunk)
        total += len(chunk.data)
    if batch:
        yield batch
