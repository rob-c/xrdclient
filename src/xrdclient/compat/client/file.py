"""``XRootD.client.File``: offset-addressed I/O returning ``(XRootDStatus, response)``.

The bindings' file is not an ``io`` object: ``read(offset, size)`` names its
offset, ``size=0`` means "to the end", and ``readline`` keeps a cursor of its
own that ``read`` does not move. All of that is reproduced here, checked
against the bindings on a live server, including the rule for that cursor:
``readline()`` reads from it and moves it along, while ``readline(offset)``
puts it at ``offset`` and leaves it there.

As in the bindings, using a file that is not open raises ``ValueError``
rather than returning a status; everything the *server* can refuse comes
back as one.

Where it fits: each :class:`File` wraps one native :class:`xrdclient.File`
(``native``) and runs every operation through :func:`._dispatch.call`, which
turns the native outcome - a value or an exception - into the bindings'
``(XRootDStatus, response)`` pair, or hands it to a
callback on a worker thread. :mod:`._convert` builds the response objects
and the ``HostList``; :mod:`._args` range-checks arguments the way the C++
binding's ``PyArg_ParseTuple`` formats would. Start reading at
:meth:`File.open` and :meth:`File.__run`.

What a maintainer must not break: the failed-open latch (an XrdCl file whose
open failed answers every later open and close with that status), the
``readline`` cursor rules above, and the finalizer path - a file dropped
while open is closed on the reaper thread, never inside the collector.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, TypeVar

from ... import _abandoned
from ...client.file import File as NativeFile
from ...errors import ServerError
from ...flags import OpenFlags as NativeOpenFlags
from ...session.router import Hop, trace
from . import _args, _channels, _convert, env
from ._dispatch import call, no_answer, now, quick
from ._status import (
    OK,
    errErrorResponse,
    errInvalidOp,
    failure,
    status,
    stOK,
)
from .flags import OpenFlags
from .responses import HostList, XRootDStatus
from .url import URL

__all__ = ["File"]

T = TypeVar("T")

#: A callback, as the bindings take one: ``callback(status, response, hostlist)``.
Callback = Callable[[XRootDStatus, Any, HostList], object] | None

#: XrdCl's ``suAlreadyDone``: what closing a file that is not open reports.
_ALREADY_DONE = 4

#: How much ``readline`` fetches at a time when it is not told.
_LINE_CHUNK = 2 * 1024 * 1024

#: One source file and the ``(offset, length, target offset)`` spans taken from it.
_CloneGroup = tuple[NativeFile, list[tuple[int, int, int]]]

#: The ``OpenFlags`` that name a template file, and so need one.
_TEMPLATED = OpenFlags.DUP | OpenFlags.SAMEFS

#: The client-side properties a file has, and their defaults. XrdCl keeps
#: them as strings and so do the bindings, so ``get_property`` hands back
#: ``"true"``/``"false"`` rather than a bool.
_PROPERTIES = {"ReadRecovery": "true", "WriteRecovery": "true", "FollowRedirects": "true"}


@dataclass(frozen=True, slots=True)
class _OpenRequest:
    """What one :meth:`File.open` or :meth:`File.openusingtemplate` asks for.

    The two public methods range-check their arguments differently (``open``
    takes 16-bit flags, ``openusingtemplate`` 32-bit ones so that ``DUP`` and
    ``SAMEFS`` fit) and then meet in :meth:`File.__open` with this. The
    ``template`` is the native file of the ``src_file`` a templated open
    names, or ``None`` for a plain open; ``dup`` asks the server to clone the
    template's contents as well as its placement.
    """

    url: str
    flags: int
    mode: int
    template: NativeFile | None = None
    dup: bool = False


class _Held:
    """The native file a :class:`File` has open, in the one object its finalizer holds.

    Kept apart from the :class:`File` so that a :func:`weakref.finalize` can
    reach it without keeping the file alive; :attr:`File.native` reads and
    writes it.
    """

    __slots__ = ("native",)

    def __init__(self) -> None:
        """Start empty: a :class:`File` holds nothing until an open succeeds."""
        self.native: NativeFile | None = None


def _dropped(held: _Held) -> None:
    """A :class:`File` was collected while open: have its native file closed soon.

    As with the bindings, a file dropped while open is closed - but not here,
    inside the garbage collector, where the close's ``kXR_close`` could wait
    on a lock the collecting thread already holds. It is queued instead, and
    closed quietly on an ordinary thread (see :mod:`xrdclient._abandoned`).
    """
    native, held.native = held.native, None
    if native is not None:
        _abandoned.adopt(native)


class File:
    """A remote file, driven the way the bindings drive one.

    Every operation takes the bindings' trailing ``timeout`` and ``callback``:
    with no callback it blocks and returns ``(XRootDStatus, response)``; with
    one it returns ``(XRootDStatus, None)`` at once and the callback later
    receives ``(status, response, hostlist)`` on a worker thread. A
    ``timeout`` of 0 means none of the call's own: ``RequestTimeout``
    applies once it has been put (:func:`.env.request_timeout`).

    A file can be opened again after :meth:`close`, but never after a failed
    open, because XrdCl's cannot either.
    """

    def __init__(self) -> None:
        """A closed file; :meth:`open` connects it, nothing happens before."""
        self.__held = _Held()
        _abandoned.on_collect(self, _dropped, self.__held)
        self.__cursor = 0
        self.__properties = dict(_PROPERTIES)
        #: How XrdCl spells where this file's requests start: the URL an open
        #: was given while it is under way, then the data server it reached.
        self.__origin = ""
        #: Why an open failed. An XrdCl file whose open failed is finished
        #: with: every later open and close answers with this same status.
        self.__failed: XRootDStatus | None = None

    @property
    def native(self) -> NativeFile | None:
        """The :class:`xrdclient.File` underneath, while open; ``None`` otherwise."""
        return self.__held.native

    @native.setter
    def native(self, value: NativeFile | None) -> None:
        """Install or drop the native file, in the slot the finalizer watches."""
        self.__held.native = value

    # -- lifecycle -----------------------------------------------------------

    def open(
        self,
        url: str,
        flags: int = 0,
        mode: int = 0,
        timeout: float = 0,
        callback: Callback = None,
    ) -> Any:
        """Open ``url``; ``flags`` are ``OpenFlags``, where ``NONE`` means read.

        ``mode`` is the POSIX permission set ``Access`` flags make up, used
        only when the open creates the file. The answer's response is
        ``None``; the file is usable once its status is ``ok``.
        """
        request = _OpenRequest(url, _args.u16(flags, "flags"), _args.u16(mode, "mode"))
        return self.__open(request, timeout, callback)

    def openusingtemplate(
        self,
        src_file: File,
        url: str,
        flags: int = 0,
        mode: int = 0,
        timeout: float = 0,
        callback: Callback = None,
    ) -> Any:
        """Open ``url`` next to ``src_file``, an open file: ``OpenFlags.SAMEFS`` / ``DUP``.

        ``SAMEFS`` creates the new file on the filesystem ``src_file`` is on,
        and ``DUP`` also clones its contents into it, both on the server; the
        open goes to ``src_file``'s data server, on its connection. Without
        either flag this is :meth:`open`, and ``src_file`` is not consulted.
        """
        template = src_file.native
        wanted = _args.u32(flags, "flags")
        mode = _args.u16(mode, "mode")
        # ``DUP`` and ``SAMEFS`` live above bit 15: they choose the request,
        # and never go into ``kXR_open``'s 16-bit options field themselves.
        plain = _OpenRequest(url, wanted & 0xFFFF, mode)
        if not wanted & _TEMPLATED:
            return self.__open(plain, timeout, callback)
        # A file that is already open or already failed answers as it would
        # to any second open, ahead of complaining about the template.
        if self.__failed is None and self.native is None and template is None:
            return failure(errInvalidOp, "Template file not open"), None
        request = _OpenRequest(
            plain.url, plain.flags, mode, template, dup=bool(wanted & OpenFlags.DUP)
        )
        return self.__open(request, timeout, callback)

    def __open(self, request: _OpenRequest, timeout: float, callback: Callback) -> Any:
        """The open both public methods share, with the bindings' failure rules.

        Order matters: a file whose open already failed answers with that
        failure (XrdCl's rule); an open file answers ``errInvalidOp``; only
        then is anything sent. The native file is built
        here with this file's ``FollowRedirects``/``ReadRecovery`` properties
        and is installed only once its open succeeds, so a failed or
        timed-out open leaves :attr:`native` ``None``.
        """
        if self.__failed is not None:
            return self.__failed, None
        if self.native is not None:
            return failure(errInvalidOp), None
        # Whatever was dropped open since the last call is closed now, on this
        # ordinary thread, and the reaper is there for the files dropped after.
        _abandoned.reap()
        _abandoned.ensure_reaper()
        url, mode = request.url, request.mode
        config = env.config()
        native = NativeFile(url, config, router=_channels.router_for(url, config))
        native.follow_redirects = self.__flag("FollowRedirects")
        native.recover_handles = self.__flag("ReadRecovery")
        origin = self.__origin = str(URL(url))
        # ``OpenFlags.NONE`` (0) is a read in XrdCl; the native client wants
        # it said.
        wanted = request.flags or OpenFlags.READ
        if wanted & (OpenFlags.NEW | OpenFlags.DELETE):
            # XrdCl sets ``kXR_async`` on every open, and xrootd has long read
            # that bit on a create or truncate as "make the parent directories"
            # (``XrdXrootdXeq.cc``, do_Open) - so with the bindings, writing
            # ``/a/b/f`` creates ``/a/b``. ``MAKEPATH`` asks for it outright.
            wanted |= OpenFlags.MAKEPATH
        source, dup = request.template, request.dup

        def opened() -> None:
            """The blocking open, run by :func:`call` now or on a worker thread."""
            try:
                with trace() as hops:
                    # The bindings' ``mode=0`` means "not given", so the native
                    # default (owner read/write) applies to a file this creates.
                    if mode:
                        native.open(NativeOpenFlags(wanted), int(mode), template=source, dup=dup)
                    else:
                        native.open(NativeOpenFlags(wanted), template=source, dup=dup)
            except BaseException:
                # Nothing was installed yet, so nobody else will close it: a
                # half-open connection or handle must not outlive the failure.
                native.close()
                raise
            # A timeout expires the open where it stands, so an open that got
            # this far is the caller's: nothing can land after it gave up.
            self.__install(native, _convert.hop_url(hops[-1], origin) if hops else origin)

        outcome = call(
            opened,
            timeout=_args.u16(timeout),
            callback=_marking(self.__fail, callback),
            hosts=lambda hops: _convert.host_list(hops, origin),
        )
        if callback is None and not outcome[0].ok:
            # An open that failed stays failed.
            self.__fail(outcome[0])
        return outcome

    def __install(self, native: NativeFile, reached: str) -> None:
        """Adopt a freshly opened native file; ``reached`` becomes ``LastURL``."""
        self.native, self.__cursor, self.__origin = native, 0, reached

    def __fail(self, why: XRootDStatus) -> None:
        """Finish with this file, as XrdCl does after a failed open."""
        self.__failed = why

    def close(self, timeout: float = 0, callback: Callback = None) -> Any:
        """Close the file. Closing one that is not open succeeds, as in XrdCl."""
        if self.__failed is not None:
            return self.__failed, None
        native = self.native
        if native is None:
            return status(_ALREADY_DONE, level=stOK), None

        def closed() -> None:
            """Close natively; the handle is gone afterwards even if that failed."""
            try:
                native.close()
            finally:
                self.native = None

        return self.__run(closed, timeout=timeout, callback=callback)

    def is_open(self) -> bool:
        """Whether :meth:`open` succeeded and :meth:`close` has not been called."""
        return self.native is not None

    def __enter__(self) -> File:
        """The file itself; the bindings support ``with File() as f: f.open(...)``."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the file if it is open, discarding the status, as the bindings do."""
        if self.native is not None:
            self.close()

    # -- reading -------------------------------------------------------------

    def read(
        self, offset: int = 0, size: int = 0, timeout: float = 0, callback: Callback = None
    ) -> Any:
        """``(status, bytes)``: ``size`` bytes at ``offset``, or to the end for ``0``."""
        native = self.__opened()
        # ``size=0`` is the bindings' "to the end"; the native read spells it -1.
        start, count = _args.u64(offset), _args.u32(size) or -1
        if quick(timeout, callback):
            return now(native.read, _same, count, start)
        return self.__run(lambda: native.read(count, start), _same, timeout, callback)

    def readline(self, offset: int = 0, size: int = 0, chunksize: int = 0) -> str:
        """One line as ``str``, ``""`` at the end; see the module for the cursor.

        ``size`` caps the line's length in bytes (0: no cap) and ``chunksize``
        is how much is read per request. Unlike the other reads this one has
        no timeout or callback and returns no status - the bindings' doesn't.
        """
        native = self.__opened()
        offset, size = _args.u64(offset), _args.u32(size)
        chunksize = _args.u32(chunksize, "chunksize")
        if offset:
            self.__cursor = offset
        line = _line(native, self.__cursor, size, chunksize or _LINE_CHUNK)
        if not offset:
            self.__cursor += len(line)
        return line.decode("utf-8")

    def readlines(self, offset: int = 0, size: int = 0, chunksize: int = 0) -> list[str]:
        """Every line from ``offset`` (or the cursor) on; ``size`` caps each line."""
        self.__opened()
        # Position once, then read on from the cursor: passing ``offset`` to
        # every ``readline`` would pin it there and loop on the first line.
        if _args.u64(offset):
            self.__cursor = offset
        lines = []
        line = self.readline(0, size, chunksize)
        while line:
            lines.append(line)
            line = self.readline(0, size, chunksize)
        return lines

    def readchunks(self, offset: int = 0, chunksize: int = _LINE_CHUNK) -> Iterator[bytes]:
        """``chunksize`` bytes at a time from ``offset`` to the end; the cursor stays put."""
        native = self.__opened()
        return _chunks(native, _args.u64(offset), _args.u32(chunksize, "chunksize") or _LINE_CHUNK)

    def __iter__(self) -> File:
        """Iterate lines from the ``readline`` cursor; a closed file raises here."""
        self.__opened()
        return self

    def __next__(self) -> str:
        """The next line, moving the cursor; ``StopIteration`` at the end."""
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    #: The Python 2 spelling the bindings still expose.
    next = __next__

    def vector_read(
        self,
        chunks: Sequence[tuple[int, int]],
        timeout: float = 0,
        callback: Callback = None,
    ) -> Any:
        """``(status, VectorReadInfo)`` for ``(offset, length)`` pairs, in one request."""
        native = self.__opened()
        ranges = _args.chunks(chunks)
        return self.__run(
            lambda: native.readv(ranges),
            lambda data: _convert.vector_read_info(ranges, data),
            timeout,
            callback,
        )

    # -- writing -------------------------------------------------------------

    def write(
        self,
        buffer: bytes | str,
        offset: int = 0,
        size: int = 0,
        timeout: float = 0,
        callback: Callback = None,
    ) -> Any:
        """Write ``buffer`` (its first ``size`` bytes, if given) at ``offset``."""
        native = self.__opened()
        # As in the bindings, text is written as its UTF-8 bytes, and
        # anything that is neither text nor bytes is a ``TypeError``.
        if not isinstance(buffer, (str, bytes, bytearray, memoryview)):
            raise TypeError(f"buffer must be str or bytes, not {type(buffer).__name__}")
        data = buffer.encode("utf-8") if isinstance(buffer, str) else bytes(buffer)
        start, size = _args.u64(offset), _args.u32(size)
        if size:
            data = data[:size]
        if quick(timeout, callback):
            return now(native.write, no_answer, data, start)
        return self.__run(lambda: native.write(data, start), timeout=timeout, callback=callback)

    def sync(self, timeout: float = 0, callback: Callback = None) -> Any:
        """Commit what has been written."""
        native = self.__opened()
        return self.__run(native.sync, timeout=timeout, callback=callback)

    def truncate(self, size: int, timeout: float = 0, callback: Callback = None) -> Any:
        """Cut or extend the file to ``size`` bytes."""
        native = self.__opened()
        length = _args.u64(size, "size")
        return self.__run(lambda: native.truncate(length), timeout=timeout, callback=callback)

    # -- about the file ------------------------------------------------------

    def stat(self, force: bool = False, timeout: float = 0, callback: Callback = None) -> Any:
        """``(status, StatInfo)``; ``force`` asks the server rather than the cache."""
        native = self.__opened()
        if quick(timeout, callback):
            return now(_stat, _convert.stat_info, native, force)
        return self.__run(lambda: _stat(native, force), _convert.stat_info, timeout, callback)

    def visa(self, timeout: float = 0, callback: Callback = None) -> Any:
        """``(status, bytes)``: the server's access token for this handle."""
        native = self.__opened()
        return self.__run(native.visa, _same, timeout, callback)

    def fcntl(self, arg: bytes, timeout: float = 0, callback: Callback = None) -> Any:
        """``(status, bytes)``: ``arg`` for the server's storage plug-in, and its answer.

        As XrdCl's ``File::Fcntl``: a ``kXR_query`` of type ``kXR_Qopaqug``
        naming this handle. A stock xrootd has no plug-in to take it, and says
        so with ``errno`` 3013.
        """
        native = self.__opened()
        data = _raw(arg)
        return self.__run(lambda: native.fcntl(data), _same, timeout, callback)

    def clone(
        self, locs: Iterable[dict[str, Any]], timeout: float = 0, callback: Callback = None
    ) -> Any:
        """Copy ranges of other files into this one, inside the server (``kXR_clone``).

        Each entry is a dict of ``src_file`` (an open :class:`File`),
        ``src_offset``, ``src_length`` and ``dest_offset``, as in the bindings.
        """
        native = self.__opened()
        groups = _clone_groups(locs)

        def cloned() -> None:
            """One ``kXR_clone`` per source, in the order the caller listed them."""
            for source, ranges in groups:
                native.clone(source, ranges)

        return self.__run(cloned, no_answer, timeout, callback)

    # -- extended attributes -------------------------------------------------

    def set_xattr(
        self, attrs: Iterable[tuple[str, str]], timeout: float = 0, callback: Callback = None
    ) -> Any:
        """``(status, [(name, status)])``: set each ``(name, value)``."""
        native = self.__opened()
        pairs = [(name, _raw(value)) for name, value in attrs]
        return self.__run(
            lambda: [(name, _attr(partial(native.setxattr, name, v))[1]) for name, v in pairs],
            _same,
            timeout,
            callback,
        )

    def get_xattr(self, attrs: Iterable[str], timeout: float = 0, callback: Callback = None) -> Any:
        """``(status, [(name, value, status)])``; a name that is missing has ``""``."""
        native = self.__opened()
        names = list(attrs)
        return self.__run(lambda: _values(native, names), _same, timeout, callback)

    def del_xattr(self, attrs: Iterable[str], timeout: float = 0, callback: Callback = None) -> Any:
        """``(status, [(name, status)])``: remove each named attribute."""
        native = self.__opened()
        names = list(attrs)
        return self.__run(
            lambda: [(name, _attr(partial(native.removexattr, name))[1]) for name in names],
            _same,
            timeout,
            callback,
        )

    def list_xattr(self, timeout: float = 0, callback: Callback = None) -> Any:
        """``(status, [(name, value, status)])`` for every attribute the file has."""
        native = self.__opened()
        return self.__run(lambda: _values(native, native.listxattr()), _same, timeout, callback)

    # -- properties ----------------------------------------------------------

    def get_property(self, name: str) -> str | None:
        """A client-side property; ``DataServer`` and ``LastURL`` once open."""
        # The two read-only properties describe the open connection, so they
        # are ``None`` - as in XrdCl - until there is one.
        if name == "DataServer":
            return self.native.endpoint if self.native is not None else None
        if name == "LastURL":
            return self.__origin if self.native is not None else None
        return self.__properties.get(name)

    def set_property(self, name: str, value: str) -> bool:
        """Set a client-side property; ``False`` for one this client does not have.

        Each is ``"true"`` or, for any other value, ``"false"``, as XrdCl
        reads them, and takes effect at once. ``FollowRedirects`` off makes a
        redirect the open's answer - ``errRedirect`` (401). ``ReadRecovery``
        off stops a read-only file being re-opened when its server is lost.
        ``WriteRecovery`` is kept but changes nothing: a file open for writing
        is never re-opened here, since what the server had not yet committed
        could not be put back.
        """
        if name not in self.__properties:
            return False
        self.__properties[name] = "true" if str(value) == "true" else "false"
        native = self.native
        if native is not None:
            # The router too: it is the router that follows - or, with the
            # property off, refuses - a redirect on the file's requests.
            native.follow_redirects = self.__flag("FollowRedirects")
            native._router.follow_redirects = native.follow_redirects
            native.recover_handles = self.__flag("ReadRecovery")
        return True

    def __flag(self, name: str) -> bool:
        """A boolean property as the native file wants it."""
        return self.__properties[name] == "true"

    # -- plumbing ------------------------------------------------------------

    def __opened(self) -> NativeFile:
        """The native file, or the ``ValueError`` the bindings raise when closed."""
        if self.native is None:
            raise ValueError("I/O operation on closed file")
        return self.native

    def __run(
        self,
        operation: Callable[[], Any],
        convert: Callable[[Any], Any] = no_answer,
        timeout: float = 0,
        callback: Callback = None,
    ) -> Any:
        """Run ``operation`` the bindings' way and ``convert`` its result.

        The single funnel every operation after the open goes through, so
        timeout range-checking, callback dispatch and the ``HostList`` are
        decided in one place (:func:`._dispatch.call`).
        """
        return call(
            operation, convert, timeout=_args.u16(timeout), callback=callback, hosts=self.__hosts
        )

    def __hosts(self, hops: list[Hop]) -> HostList:
        """The ``HostList`` for an answer, from the hops the request took."""
        native = self.native
        if not hops and native is not None:
            # Answered without a request going through the router - off the
            # bulk data plane, or from what the open already said - but by
            # the data server all the same, and XrdCl names it.
            hops = [_answering(native)]
        return _convert.host_list(hops, self.__origin)

    def __repr__(self) -> str:
        """Name the open URL, so a file in a traceback says where it points."""
        where = str(self.native.url) if self.native is not None else "not open"
        return f"<XRootD.client.File {where}>"


def _answering(native: NativeFile) -> Hop:
    """The data server an open file's requests go to, as a hop that answered."""
    router = native._router
    session = router._session
    # Between a lost connection and its recovery there is no session to ask
    # for the server's flags; the URL is still the right one to name.
    if session is None:
        return Hop(router.url)
    flags = getattr(session.protocol, "flags", 0)
    return Hop(router.url, flags=flags, version=session.handshake_version)


def _marking(mark: Callable[[XRootDStatus], None], callback: Callback) -> Callback:
    """``callback``, first passing a failure to ``mark``: an open that failed is dead."""
    if not callable(callback):
        return callback  # ``None``, or something ``call`` will refuse
    user = callback

    def delivered(status: XRootDStatus, response: Any, hosts: HostList) -> object:
        """Latch a failure before the user's callback can try the file again."""
        if not status.ok:
            mark(status)
        return user(status, response, hosts)

    return delivered


def _same(value: T) -> T:
    """The ``convert`` of an operation whose native answer is the response."""
    return value


def _stat(native: NativeFile, force: object) -> Any:
    """Stat an open file; ``force`` (any truthy value, as the bindings take it) refreshes."""
    return native.stat(refresh=bool(force))


def _line(native: NativeFile, start: int, limit: int, chunk: int) -> bytes:
    """The bytes from ``start`` up to and including the next newline, or ``limit``."""
    line = bytearray()
    while not limit or len(line) < limit:
        want = chunk if not limit else min(chunk, limit - len(line))
        data = native.read(want, start + len(line))
        if not data:
            break
        cut = data.find(b"\n")
        if cut >= 0:
            data = data[: cut + 1]
        line += data
        if cut >= 0:
            break
    return bytes(line[:limit] if limit else line)


def _chunks(native: NativeFile, offset: int, size: int) -> Iterator[bytes]:
    """Read ``size`` bytes at a time from ``offset`` until the server returns none.

    A generator, so ``readchunks`` costs nothing until iterated and a caller
    that stops early sends no further reads. A short read is not taken as
    the end: only an empty one is, because a server may split a read.
    """
    while True:
        data = native.read(size, offset)
        if not data:
            return
        yield data
        offset += len(data)


def _clone_groups(locs: Iterable[dict[str, Any]]) -> list[_CloneGroup]:
    """Consecutive ranges from the same source, as ``(source, ranges)`` pairs."""
    groups: list[_CloneGroup] = []
    for loc in locs:
        source = loc["src_file"].native
        if source is None:
            raise ValueError("clone source is not open")
        span = (int(loc["src_offset"]), int(loc["src_length"]), int(loc["dest_offset"]))
        # Identity, not equality: one ``kXR_clone`` names one source handle,
        # and only adjacent entries are merged so the server applies the
        # ranges in the order the caller listed them.
        if groups and groups[-1][0] is source:
            groups[-1][1].append(span)
        else:
            groups.append((source, [span]))
    return groups


def _raw(value: str | bytes) -> bytes:
    """Bytes for the wire; text as UTF-8, with lone surrogates round-tripped."""
    return value if isinstance(value, bytes) else str(value).encode("utf-8", "surrogateescape")


def _attr(action: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
    """One attribute's outcome, its status a plain dict with XrdCl's empty message."""
    try:
        return action(), dict(vars(OK))
    except ServerError as exc:
        return None, dict(vars(status(errErrorResponse, errno=exc.code)))


def _values(native: NativeFile, names: Sequence[str]) -> list[tuple[str, str, dict[str, Any]]]:
    """``(name, value, status)`` per attribute, as ``get_xattr``/``list_xattr`` answer.

    Each name is fetched on its own so one missing attribute fails only its
    own entry - the overall status stays ``ok``, matching XrdCl, which reports
    per-attribute errors inside the response rather than failing the call.
    """
    out = []
    for name in names:
        value, outcome = _attr(partial(native.getxattr, name))
        text = value.decode("utf-8", "surrogateescape") if value is not None else ""
        out.append((name, text, outcome))
    return out
