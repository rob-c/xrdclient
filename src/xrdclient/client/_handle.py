"""The life of one file handle: open, re-open after a lost server, close, abort.

:class:`_Handle` is the part of :class:`~xrdclient.client.file.File` that
every operation shares - which connection the handle lives on, how a request
naming it is sent and retried, how its data paths are spread, and how it is
given back. The operations themselves are in :mod:`._reading` and
:mod:`._writing`; :mod:`xrdclient.client.file` puts them together.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Callable
from contextlib import suppress
from typing import Self

from ..config import Config
from ..errors import (
    AbortedError,
    TransientError,
    UnsupportedError,
    WaitLimitError,
    XRootDError,
    kXR_Unsupported,
)
from ..errors import ConnectionError as XrdConnectionError
from ..flags import Access, OpenFlags, open_flags, permissions
from ..log import get_logger, redact
from ..proto import constants as c
from ..proto import requests as r
from ..proto import responses as rp
from ..proto.frames import Request
from ..session.deadline import OperationExpiredError
from ..session.router import LOST, Router, tried, with_tried
from ..session.sync import Result, Session
from ..types import (
    StatInfo,
)
from ..url import XRootDURL, parse

_log = get_logger("xrdclient.client.file")

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


class _Handle:
    """One open file handle on one data server: its state and its lifecycle."""

    def __init__(
        self,
        url: str | XRootDURL,
        config: Config | None = None,
        *,
        router: Router | None = None,
    ) -> None:
        """Prepare a handle for ``url``; nothing is sent until :meth:`open`.

        With no ``router`` the handle makes and owns one (and closes it with the
        file); a router passed in - a :class:`~xrdclient.FileSystem`'s, so files
        share its connection - stays the caller's.
        """
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
        #: Guards the state above that a read updates as a side effect - the
        #: readahead block and window, the data-path cursor - and serialises
        #: re-opening, so that threads sharing one handle (or an
        #: ``asyncio.gather`` over its ``pread``) neither tear that state nor
        #: re-open the file twice. Never held across a read itself.
        self._state = threading.RLock()
        #: Data servers this handle has lost, for the ``tried=`` CGI a
        #: re-open sends the manager so that it routes around them.
        self._tried: list[str] = []
        #: Set by :meth:`abort`, never cleared.
        self._aborted = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        """Whether the server has given a ``kXR_open`` file handle that is not yet closed."""
        return self._handle is not None

    @property
    def handle(self) -> bytes:
        """The 4-byte ``fhandle`` the server assigned at open, sent with every file request.

        Raises :class:`ValueError` once closed, the same message :mod:`io` uses,
        so a read after close fails like a local one does.
        """
        if self._handle is None:
            raise ValueError("I/O operation on closed file")
        return self._handle

    @property
    def endpoint(self) -> str:
        """``host:port`` of the data server the file is open on (after any redirect)."""
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
        with self._state:
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
            with self._state, suppress(ValueError):  # another thread got there first
                self._data_paths.remove(pathid)
            with self._state:
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
        template: _Handle | None = None,
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

    def _join(self, template: _Handle, dup: bool) -> None:
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
            with_tried(self.url.path_with_cgi, self._tried, LOST),
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

    def _execute(self, build: Callable[[bytes], Request]) -> Result:
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
        attempts = 0
        reopen = False
        while True:
            # Which incarnation of the handle this attempt used: a thread
            # beside this one may have re-opened it already, and then this
            # one's failure is already dealt with.
            seen = self.recoveries
            try:
                if reopen:
                    self._reopen(seen)
                    reopen = False
                    seen = self.recoveries
                return self._router.execute(build(self.handle), path=self.url.path)
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

    def _reopen(self, seen: int | None = None) -> bytes:
        """Get the handle back on a fresh connection, from the original URL.

        Not from the pinned endpoint: the data server that just went away is
        the least likely one to answer, and going back to where the open
        started is what lets a manager route around it - told which server
        failed (``tried=``, accumulated over every re-open, with
        ``triedrc=srverr``), as XrdCl tells it.

        ``seen`` is the :attr:`recoveries` count the failed operation ran
        under. If it has moved on, another thread sharing this handle has
        re-opened it meanwhile, and the handle it got is the answer here too:
        re-opening again would orphan that thread's connection and handle.
        The lock makes the check and the re-open one step; the old handle
        stays in place until the new one replaces it, so a thread reading
        beside this one fails over to the new handle instead of finding none.
        """
        with self._state:
            self._unless_aborted()
            if seen is not None and seen != self.recoveries and self._handle is not None:
                return self._handle
            stale, self._stat = self._router, None
            self._pathid = 0
            # The data paths belonged to the connection that just died; drop
            # them and let the fresh open bind its own.
            self._data_paths = []
            self._rr = 0
            self._multistream = True
            self._ahead, self._sequel = _NOTHING_AHEAD, -1
            if (stale.url.host, stale.url.port) != (self.url.host, self.url.port):
                # Only a server the open was redirected to is worth naming:
                # the manager cannot route around itself.
                self._tried = tried(self._tried, stale.url.host)
            if self._owns_router:
                # Discarded, not pooled: this connection has just failed
                # under a live handle, and the next caller deserves better.
                stale.discard()
            self._router = Router(self.url, self.config)
            self._owns_router = True
            if self._aborted:
                # An abort that came while the old router was being let go
                # reached that one, not this: it is aborted here instead.
                self._router.abort()
            self.recoveries += 1
            try:
                handle = self._do_open()
            except BaseException:
                self._handle = None
                raise
            self._bind_data_streams()
            return handle

    def _paths_idle(self) -> bool:
        """Whether the automatic data paths have nothing to offer this handle.

        True with none bound or after falling back from them, and on a server
        that has said it will not serve a request that arrives on one - the
        stock daemon, where the plane on the control link is the fast path.
        """
        if not self._data_paths or not self._multistream:
            return True
        return self._router.session.ask_arrival_routing() is False

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
        if self._aborted:
            # Nothing can be sent: the abort took the connection down. The
            # server drops the handle with it - discarding a ``posc`` file -
            # and what is left to do is let go of the router.
            self._router.close()
            return
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

    def abort(self) -> None:
        """Call off whatever this handle is doing, from any thread, at once.

        Thread-safe, idempotent and non-blocking. The connection the handle
        is open on - its control link and every data path bound to it - is
        shut down, which wakes a thread blocked in a read, a write or a
        ``kXR_wait`` on it straight away rather than at its timeout; that
        call, and every call on this handle after it, raises
        :class:`~xrdclient.errors.AbortedError` (``errno`` ``ECANCELED``).
        The handle is never re-opened afterwards, whatever
        :attr:`recover_handles` says.

        Aborting is giving up on the file, so nothing is sent to the server:
        it drops the handle with the connection, which for a ``posc`` file
        means the partial file is removed, as it is for any writer that
        never closed. :meth:`close` is still worth calling - it releases
        what is left - and after an abort it never raises.

        The connection is what an abort can reach, so another handle sharing
        it (a file opened through the same
        :class:`~xrdclient.FileSystem`, say) finds it dropped as well.
        """
        self._aborted = True
        self.recover_handles = False
        self._router.abort()

    def _unless_aborted(self, cause: BaseException | None = None) -> None:
        """Raise :class:`~xrdclient.errors.AbortedError` if :meth:`abort` was called."""
        if self._aborted:
            raise AbortedError(f"{self.url} was aborted") from cause

    def __enter__(self) -> Self:
        """Open the file if it is not open yet, so ``with File(url, mode) as f:`` works."""
        if self._handle is None:
            self.open()
        return self

    def __exit__(self, exc_type: object, *_: object) -> None:
        """Close the file; a close failure never masks the block's own exception.

        A failing close is worth raising, but not over the top of the
        exception that is already on its way out of the ``with`` body: that one
        is the reason the block is unwinding, so the close error is only logged.
        """
        try:
            self.close()
        except Exception:
            if exc_type is None:
                raise
            _log.debug("close of %s failed while unwinding", self.url, exc_info=True)

    def __repr__(self) -> str:
        """``File('<url>', open|closed)``, with credentials in the URL redacted.

        A ``repr`` ends up in tracebacks, logs and notebook output, so an
        ``authz=`` token or signed-URL parameter in the CGI is replaced by
        ``<redacted>`` (:func:`xrdclient.log.redact`), as ``repr(url)`` does.
        """
        state = "open" if self.is_open else "closed"
        return f"File({redact(str(self.url))!r}, {state})"

    def __fspath__(self) -> str:
        """The file's URL as text, for APIs that take ``os.PathLike``."""
        return str(self.url)


class _Landing(io.BytesIO):
    """The buffer a plane read lands in, and then the ``bytes`` it returns.

    A read that fails part-way leaves slices of this buffer's view in the
    traceback, and the collector may finalize the buffer before them; closing
    a :class:`io.BytesIO` with its storage still exported raises, which here
    would only be reported as an ignored exception. There is nothing to
    release that the collector will not, so the close is let go.
    """

    def close(self) -> None:
        """Close the buffer, tolerating views still exported by a failed read's traceback."""
        with suppress(BufferError):
            super().close()


def _transient(error: XrdConnectionError) -> TransientError:
    """``error`` as the :class:`TransientError` a lost connection is reported as."""
    if isinstance(error, TransientError):
        return error
    failure = TransientError(str(error))
    failure.__cause__ = error
    return failure
