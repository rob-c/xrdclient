"""An open remote file.

:class:`File` is the low-level handle: every method is one protocol
operation, with no buffering and no implicit position. The file-like objects
in :mod:`xrdclient.io` are built on top of it, and that is what most code should
use. Reach for :class:`File` when you want vector reads, paged I/O with
per-page checksums, extended attributes on a handle, or checkpoints.
"""

from __future__ import annotations

import io
import struct
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress

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
    WaitLimitError,
    XRootDError,
    kXR_InvalidRequest,
    kXR_Unsupported,
)
from ..errors import ConnectionError as XrdConnectionError
from ..flags import Access, ChkPointCode, OpenFlags, open_flags, permissions
from ..proto import constants as c
from ..proto import requests as r
from ..proto import responses as rp
from ..proto.frames import Request
from ..session.bulk import BulkUnsupported
from ..session.deadline import OperationExpiredError
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
from . import _fattr

__all__ = ["File", "Checkpoint"]

_log = get_logger(__name__)

#: Server-side ceilings on one ``kXR_readv``: ``XrdProto::maxRvecsz``
#: elements, and the byte total we choose to put in one request so that a
#: reply stays about the size of one of the server's buffers.
READV_MAX_CHUNKS = 1024
READV_MAX_BYTES = 2 << 20

#: ``maxReadv_ior`` - the largest single ``kXR_readv`` element xrootd accepts
#: with its default 2 MiB buffer. The server keeps back room for the 16-byte
#: ``readahead_list`` header it writes before each element's data, and refuses
#: anything bigger with ``kXR_NoMemory``. XrdCl uses the same default.
READV_MAX_ELEMENT = (2 << 20) - 16

#: ``maxTransz`` - the largest single ``kXR_writev`` element xrootd accepts
#: with its default buffer. A bigger one is refused and the connection with it.
WRITEV_MAX_ELEMENT = 2 << 20

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

#: Most a sequential reader fetches beyond what it was asked for. The window
#: starts at ``config.readahead`` and doubles with every read that carries on
#: where the last one stopped, up to this; it is also what the end of a
#: sequential scan can cost in bytes nobody reads.
_MAX_AHEAD = 8 << 20

#: No readahead: nothing held, and no read that the next one could follow.
_NOTHING_AHEAD: tuple[int, bytes] = (0, b"")


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
        #: ``(optiont, fhtemplt)`` of an open made against a template file:
        #: ``kXR_dup`` or ``kXR_samefs``, and that file's handle.
        self._template: tuple[int, bytes] = (0, c.NULL_FHANDLE)
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
        #: Whether this handle may be re-opened after losing its server, in
        #: place of :attr:`Config.recover_handles <xrdclient.Config.recover_handles>`
        #: for this one file; ``None`` follows the configuration. Read at the
        #: moment a connection is lost, so it can be changed while open.
        self.recover_handles: bool | None = None
        #: Whether the open follows redirects. ``False`` makes a redirect the
        #: open's answer - :class:`~xrdclient.session.sync.RedirectRequired`,
        #: naming where it points - instead of a hop towards the data server.
        self.follow_redirects = True
        #: Sequential readahead on a read-only handle: the block fetched
        #: beyond the last read, where the next read is expected to start,
        #: and how far the next fetch looks ahead. See :meth:`_sequential`.
        self._ahead = _NOTHING_AHEAD
        self._sequel = -1
        self._window = self.config.readahead

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
        session, and routes every subsequent read's answer over it - the
        requests still go out on the control link, so a stat or a close is
        never stuck behind a megabyte of file. Returns the path id, and is
        idempotent: a handle already bound keeps the path it has. Writes stay
        on the control link, because a stock server answers a split write on
        the path where nothing waits for it (see :meth:`_split_path`).

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
        want = self.config.data_streams - len(self._data_paths)
        if want <= 0:
            return
        # A pooled connection keeps the paths an earlier file bound on it, and
        # the server keeps serving them; binding afresh at every open would
        # leave each connection with one more socket per file ever opened.
        spare = [p for p in self._router.session.data_paths if p not in self._data_paths]
        self._data_paths.extend(spare[:want])
        for _ in range(want - len(spare[:want])):
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

    def _bulk(self, build: Callable[[bytes, int], Request], *, write: bool = False) -> Result:
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

        A ``write`` never takes the split: see :meth:`_split_path`.
        """
        pathid = self._next_path()
        if not pathid or self._checkpoint:
            return self._execute(lambda handle: build(handle, self._split_path(write)))
        # One attempt, straight at the session so a server that will not serve
        # the bound op is not chased through the router's reconnect loop.
        # The question is settled before the request is built: a server that
        # disclaims arrival would otherwise be handed that request split,
        # which is fine for a read and a hang for a write.
        if self._router.session.ask_arrival_routing() is not False:
            result = self._on_arrival_path(build, pathid)
            if result is not None:
                return result
            pathid = self._next_path()
        if write or not pathid:
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

    def _on_arrival_path(
        self, build: Callable[[bytes, int], Request], pathid: int
    ) -> Result | None:
        """Send the whole op down ``pathid`` and take the answer there.

        ``None`` when the server would not serve it: the session has let that
        socket go with the request on it, so a fresh one is bound in its place
        for the standard split to use.
        """
        try:
            return self._router.session.execute(
                build(self.handle, pathid), path=self.url.path, arrive_on_path=True
            )
        except (XRootDError, ValueError) as exc:
            _log.debug("data path %d does not serve what arrives on it: %s", pathid, exc)
            self._data_paths.remove(pathid)
            self._bind_data_streams()
            return None

    def _split_path(self, write: bool) -> int:
        """The path an op that is not arriving on a data path names.

        The manual :meth:`bind_data_path` for a read, and always the control
        link for a write. A stock xrootd takes a split write - header on the
        control link, payload on the bound socket - and then answers it *on
        the bound socket*, where nothing is listening, so the write would sit
        out the whole ``request_timeout`` with its bytes already on disk. Only
        a server that serves what arrives on the path takes a write there,
        header and payload together (:meth:`_on_arrival_path`).
        """
        return 0 if write else self._pathid

    def open(
        self,
        flags: OpenFlags | int | str = OpenFlags.READ,
        mode: Access | int | str = Access.OWNER_READ | Access.OWNER_WRITE,
        *,
        template: File | None = None,
        dup: bool = False,
    ) -> StatInfo | None:
        """``kXR_open``. Returns the stat the server volunteered, if any.

        Say what you want in whichever way reads best::

            fh.open("r")                              # as for the builtin
            fh.open("w", "rw-r--r--")                 # and its permissions
            fh.open("new makepath")                   # the protocol's names
            fh.open(OpenFlags.NEW | OpenFlags.MAKEPATH)   # or the bits

        A string of mode letters is a mode; any other string is read as
        option names. ``mode`` is the permission set for a file this creates.

        ``template`` is another open :class:`File`, and makes this a new file
        placed on the same filesystem as that one (``kXR_samefs``); with
        ``dup=True`` the server also gives it that file's contents
        (``kXR_dup``, a server-side clone). The open goes out on the
        template's connection, since its handle means nothing anywhere else,
        and the server wants ``NEW`` - and, for ``dup``, a writable open::

            copy = File(url_of_copy)
            copy.open("new update", template=original, dup=True)

        A server whose storage cannot place or clone files that way refuses
        with :class:`~xrdclient.errors.UnsupportedError`, and so does this
        method, without asking, for one whose protocol predates the options.
        """
        if self._handle is not None:
            raise ValueError(f"{self.url} is already open")
        self._flags, self._mode = open_flags(flags), permissions(mode)
        self._template = (0, c.NULL_FHANDLE)
        try:
            if template is not None:
                self._join(template, dup)
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

    def _join(self, template: File, dup: bool) -> None:
        """Move onto ``template``'s connection, where its handle is valid.

        The file takes the template's data server as its own address, as
        XrdCl does: that is where it is going to be, whatever server the URL
        it was made with named.
        """
        handle = template.handle  # open, or this raises
        router = template._router
        version = getattr(router.session.protocol, "version", 0)
        if version < c.kXR_PROTCLONEVERSION:
            raise UnsupportedError(
                kXR_Unsupported,
                f"{router.endpoint} speaks protocol {version:#x}, older than the "
                f"{c.kXR_PROTCLONEVERSION:#x} that brought opens against a template file",
            )
        shared = router.pin()
        if self._owns_router:
            self._router.close()
        self._router, self._owns_router = shared, True
        where = router.url
        self.url = self.url.evolve(host=where.host, port=where.port, username=where.username)
        self._template = (c.kXR_dup if dup else c.kXR_samefs, handle)

    def _do_open(self) -> bytes:
        """Issue the ``kXR_open`` and adopt the handle it returns."""
        optiont, fhtemplt = self._template
        request = r.Open(
            self.url.path_with_cgi,
            int(self._flags) | c.kXR_retstat,
            self._mode,
            optiont=optiont,
            fhtemplt=fhtemplt,
        )
        self._router.follow_redirects = self.follow_redirects
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
        wanted = self.recover_handles
        if wanted is None:
            wanted = self.config.recover_handles
        return bool(wanted) and not (self._flags & _WRITING)

    def _execute(self, build: Callable[[bytes], Request], **kwargs: object) -> Result:
        """Run a handle-bearing request, re-opening if the server is lost.

        ``build`` takes the handle rather than closing over it, because a
        recovered file has a different one: the retry has to be issued against
        the new handle, not the dead one. A link that keeps flaking is met with
        up to :attr:`~xrdclient.config.Config.connect_retries` re-opens, not one,
        so a handle-bound request (a ``stat`` of the open file, a checkpoint)
        survives what the bulk read beside it already survives; the re-open is
        inside the retry too, so a drop *during* recovery is just another try.
        A write handle is never re-opened (:attr:`recoverable`), and a caller's
        expired deadline ends it at once.
        """
        kwargs.setdefault("path", self.url.path)
        attempts = 0
        reopen = False
        while True:
            try:
                if reopen:
                    self._reopen()
                    reopen = False
                return self._router.execute(build(self.handle), **kwargs)  # type: ignore[arg-type]
            except TransientError as exc:
                # A busy server (kXR_wait past the budget) and an expired
                # deadline are not a dropped link: re-opening the handle would
                # not help, so they stand, as they do at the router.
                if not self.recoverable or isinstance(exc, (OperationExpiredError, WaitLimitError)):
                    raise
                attempts += 1
                if attempts > self.config.connect_retries:
                    raise
                _log.debug("recovering %s after %s (attempt %d)", self.url, exc, attempts)
                reopen = True

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
        self._ahead, self._sequel = _NOTHING_AHEAD, -1
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

        A connection already known to be broken - one a bulk read had to give
        up on, say - is not asked at all: the handle is released here, and the
        same rule decides whether that is worth raising.
        """
        handle, self._handle = self._handle, None
        try:
            if handle is not None:
                self._close_handle(handle)
        except TransientError as exc:
            if self._flags & _WRITING or isinstance(exc, OperationExpiredError):
                raise
            _log.debug("close of %s found the connection gone: %s", self.url, exc)
        finally:
            # Always: a router that only borrows its connection leaves it be,
            # and one that moved to a data server on a connection of its own
            # gives that back to the pool now rather than when collected.
            self._router.close()

    def _close_handle(self, handle: bytes) -> None:
        """Send the ``kXR_close``, unless the connection can no longer carry it.

        A broken session is still "open" as far as its socket goes, so the
        router would happily send on it - and what came back could be a reply
        still in transit from the operation that broke it, or nothing until
        the request timeout. There is no need to ask: the server drops every
        handle of a connection it has lost, so the handle is gone either way.
        The :class:`~xrdclient.errors.TransientError` this raises instead is
        what :meth:`close` forgives on a reader and reports on a writer.
        """
        try:
            broken = self._router.session.broken
        except XrdConnectionError:
            # The router will not reconnect a handle's connection behind its
            # back, and says so this way: that connection is lost too.
            broken = True
        if broken:
            raise TransientError(
                f"the connection {self.url} was open on broke before its close, "
                f"so the server never heard it"
            )
        self._router.execute(r.Close(handle))

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
        start, block = self._ahead
        if start <= offset and end <= start + len(block):
            self._sequel = end
            return block[offset - start : end - start]
        streak, self._sequel = offset == self._sequel, end
        if not streak:
            self._window = self.config.readahead
            return self._plane_read(size, offset)
        window, self._window = self._window, min(self._window * 2, _MAX_AHEAD)
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
        # A reply longer than the read is refused by the session machine,
        # which caps what it will accumulate from the request itself; there
        # is nothing left to check by the time the bytes get here.
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

    def _paths_idle(self) -> bool:
        """Whether the automatic data paths have nothing to offer this handle.

        True with none bound or after falling back from them, and on a server
        that has said it will not serve a request that arrives on one - the
        stock daemon, where the plane on the control link is the fast path.
        """
        if not self._data_paths or not self._multistream:
            return True
        return self._router.session.ask_arrival_routing() is False

    def _recover_from_plane(self, error: XrdConnectionError) -> None:
        """Re-open after the bulk plane lost the connection, or raise.

        The plane has already marked the connection broken - its wire is out
        of step, with replies still owed on it - so nothing may be sent on it
        again, the event path included. A handle that can be re-opened is,
        on a fresh one, and the caller's read is then run on the event path
        there; one that cannot gets the failure, as the
        :class:`~xrdclient.errors.TransientError` the event path would have
        raised.
        """
        if not self.recoverable or isinstance(error, OperationExpiredError):
            raise _transient(error)
        _log.debug("recovering %s after the bulk plane lost its connection: %s", self.url, error)
        self._reopen()

    def _recover_from_step(self, error: ProtocolError) -> None:
        """Re-open after the bulk plane found its wire out of step, or re-raise.

        A plane that could not tell whose
        reply it was reading has marked the connection broken, which a
        handle that can be re-opened survives, as it does a lost connection:
        the read is run again, on the event path, over a fresh one. Anything
        else - a connection still sound, a handle that cannot be re-opened -
        is the caller's to see.
        """
        if not (self.session.broken and self.recoverable):
            raise error
        _log.debug("recovering %s after the bulk plane lost step with the server", self.url)
        self._reopen()

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
        try:
            with self.session.bulk(self.handle, chunk=chunk, depth=depth) as reader:
                return reader.into(view, offset)
        except BulkUnsupported as exc:
            _log.debug("bulk read declined for %s (%s); using the event path", self.url, exc)
        except XrdConnectionError as exc:
            self._recover_from_plane(exc)
        except ProtocolError as exc:
            self._recover_from_step(exc)
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
        try:
            with self.session.bulk(handle, chunk=1, depth=depth) as channel:
                return channel.gather(requests)
        except BulkUnsupported as exc:
            _log.debug("bulk readv declined for %s (%s); using the event path", self.url, exc)
        except XrdConnectionError as exc:
            self._recover_from_plane(exc)
        except ProtocolError as exc:
            self._recover_from_step(exc)
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
            raise _transient(exc) from exc
        return None

    def _event_write(self, view: memoryview, offset: int) -> int:
        """``view`` at ``offset`` through the event path, a chunk at a time."""
        limit = self.config.chunk_size
        written = 0
        while written < len(view):
            piece = view[written : written + limit]
            at = offset + written
            body = piece.tobytes()
            if self._checkpoint:
                self._submit(r.Write(self.handle, at, body, self._split_path(write=True)))
            else:
                self._bulk(_write_for(at, body), write=True)
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

    # ------------------------------------------------------------------
    # Extended attributes on the handle
    # ------------------------------------------------------------------

    def getxattr(self, name: str) -> bytes:
        """One attribute's value; :class:`~xrdclient.errors.AttrNotFoundError` if absent.

        The same error :meth:`FileSystem.getxattr` raises, so a caller need
        not know whether it asked a path or an open handle.
        """
        result = self._execute(lambda handle: r.Fattr.get("", name, fhandle=handle))
        for item in rp.parse_fattr(result.data).items:
            if item.code == 0 and item.value is not None:
                return item.value
        raise _fattr.missing(name, self.url.path)

    def setxattr(self, name: str, value: bytes) -> None:
        """Set one attribute, raising if the server refused it."""
        res = self._router.execute(
            r.Fattr.set("", name, value, fhandle=self.handle), path=self.url.path
        )
        _fattr.check(rp.parse_fattr(res.data, values=False), self.url.path)

    def listxattr(self) -> list[str]:
        result = self._execute(lambda handle: r.Fattr.list("", fhandle=handle))
        return [i.name for i in rp.parse_fattr_list(result.data).items]

    def removexattr(self, name: str) -> None:
        """Remove one attribute, raising if the server refused - one absent, say."""
        res = self._router.execute(
            r.Fattr.delete("", name, fhandle=self.handle), path=self.url.path
        )
        _fattr.check(rp.parse_fattr(res.data, values=False), self.url.path)

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


class _Landing(io.BytesIO):
    """The buffer a plane read lands in, and then the ``bytes`` it returns.

    A read that fails part-way leaves slices of this buffer's view in the
    traceback, and the collector may finalize the buffer before them; closing
    a :class:`io.BytesIO` with its storage still exported raises, which here
    would only be reported as an ignored exception. There is nothing to
    release that the collector will not, so the close is let go.
    """

    def close(self) -> None:
        with suppress(BufferError):
            super().close()


def _transient(error: XrdConnectionError) -> TransientError:
    """``error`` as the :class:`TransientError` a lost connection is reported as."""
    if isinstance(error, TransientError):
        return error
    failure = TransientError(str(error))
    failure.__cause__ = error
    return failure


#: A ``kXR_readv`` reply's ``readahead_list``: fhandle, length, offset.
_READ_LIST = struct.Struct(">4xiq")


class _PackedReadV(r.ReadV):
    """A ``kXR_readv`` whose element list is packed in one call.

    The same frame as :class:`~xrdclient.proto.requests.ReadV` builds a field
    at a time, which for a thousand elements is most of the request's cost.
    """

    __slots__ = ()

    def payload(self) -> bytes:
        fields: list[bytes | int] = []
        for fhandle, offset, length in self.chunks:
            fields += (fhandle, length, offset)  # "4s" pads or truncates, as the field does
        return struct.pack(">" + "4siq" * len(self.chunks), *fields)


def _as_range(rng: ReadRange | tuple[int, int]) -> ReadRange:
    return rng if isinstance(rng, ReadRange) else ReadRange(rng[0], rng[1])


def _as_asked(bodies: list[memoryview], wanted: list[ReadRange]) -> list[bytes] | None:
    """Every range's bytes, if the replies hold exactly ``wanted``, in order.

    ``None`` as soon as anything differs - a segment out of order, a short
    one at the end of the file, one too many or too few - and the caller
    matches the segments up by offset instead.
    """
    out: list[bytes] = []
    expected = iter(wanted)
    for body in bodies:
        for offset, data in _walk(body):
            rng = next(expected, None)
            if rng is None or rng.offset != offset or rng.length != len(data):
                return None
            out.append(bytes(data))
    return out if len(out) == len(wanted) else None


def _by_offset(bodies: list[memoryview]) -> dict[int, list[bytes]]:
    """Every segment of the replies, filed by offset, in the order it came."""
    out: dict[int, list[bytes]] = {}
    for body in bodies:
        for offset, data in _walk(body):
            out.setdefault(offset, []).append(bytes(data))
    return out


def _walk(body: memoryview) -> Iterator[tuple[int, memoryview]]:
    """``(offset, bytes)`` for each segment of one ``kXR_readv`` reply.

    Walks the reply in place: the views are slices of the buffer the reply
    was received into, so a caller copies each segment once, and only the
    segments it keeps.
    """
    at, end = 0, len(body)
    while at < end:
        if end - at < _READ_LIST.size:
            raise ProtocolError(
                f"kXR_readv reply ends with {end - at} bytes of a {_READ_LIST.size}-byte "
                "segment header"
            )
        length, offset = _READ_LIST.unpack_from(body, at)
        at += _READ_LIST.size
        if length < 0:
            raise ProtocolError(f"kXR_readv segment declares a negative length of {length}")
        if at + length > end:
            raise ProtocolError(
                f"kXR_readv segment at offset {offset} declares {length} bytes, "
                f"and the reply has {end - at} left"
            )
        yield offset, body[at : at + length]
        at += length


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


def _pieces(rng: ReadRange) -> Iterator[ReadRange]:
    """``rng`` as ``kXR_readv`` elements no longer than the server accepts."""
    if rng.length <= READV_MAX_ELEMENT:
        yield rng
        return
    for start in range(0, rng.length, READV_MAX_ELEMENT):
        yield ReadRange(rng.offset + start, min(READV_MAX_ELEMENT, rng.length - start))


def _assemble(rng: ReadRange, answers: dict[int, list[bytes]]) -> bytes:
    """One requested range, rejoined from the pieces :func:`_pieces` cut it into.

    Every piece is claimed from ``answers``, even after a short one at the end
    of the file, so that a later range starting at the same offset as one of
    them is still handed its own segment and not a leftover.
    """
    parts = [_segment_for(piece, answers.get(piece.offset)) for piece in _pieces(rng)]
    return parts[0] if len(parts) == 1 else b"".join(parts)


def _batches(ranges: Sequence[ReadRange]) -> Iterator[list[ReadRange]]:
    """Split vector reads to respect the server's per-request ceilings."""
    batch: list[ReadRange] = []
    total = 0
    for rng in ranges:
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


def _write_pieces(chunk: WriteChunk) -> Iterator[WriteChunk]:
    """``chunk`` as ``kXR_writev`` elements no longer than the server accepts."""
    if len(chunk.data) <= WRITEV_MAX_ELEMENT:
        yield chunk
        return
    for start in range(0, len(chunk.data), WRITEV_MAX_ELEMENT):
        yield WriteChunk(chunk.offset + start, chunk.data[start : start + WRITEV_MAX_ELEMENT])


def _write_batches(chunks: Sequence[WriteChunk]) -> Iterator[list[WriteChunk]]:
    """Split vector writes to respect the server's per-element and per-request ceilings."""
    batch: list[WriteChunk] = []
    total = 0
    for chunk in (piece for whole in chunks for piece in _write_pieces(whole)):
        if batch and (len(batch) >= READV_MAX_CHUNKS or total + len(chunk.data) > READV_MAX_BYTES):
            yield batch
            batch, total = [], 0
        batch.append(chunk)
        total += len(chunk.data)
    if batch:
        yield batch
