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
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterable, Iterator, Sequence
from functools import partial
from typing import Any, Optional, TypeVar

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
Callback = Optional[Callable[[XRootDStatus, Any, HostList], object]]

#: XrdCl's ``suAlreadyDone``: what closing a file that is not open reports.
_ALREADY_DONE = 4

#: How much ``readline`` fetches at a time when it is not told.
_LINE_CHUNK = 2 * 1024 * 1024

#: One source file and the ``(offset, length, target offset)`` spans taken from it.
_CloneGroup = tuple[NativeFile, list[tuple[int, int, int]]]

#: The ``OpenFlags`` that name a template file, and so need one.
_TEMPLATED = OpenFlags.DUP | OpenFlags.SAMEFS

#: What :meth:`File.__open` passes for an open without a template.
_PLAIN: tuple[NativeFile | None, bool] = (None, False)

#: The client-side properties a file has, and their defaults.
_PROPERTIES = {"ReadRecovery": "true", "WriteRecovery": "true", "FollowRedirects": "true"}


class File:
    """A remote file, driven the way the bindings drive one."""

    def __init__(self) -> None:
        self.native: NativeFile | None = None
        self.__cursor = 0
        self.__properties = dict(_PROPERTIES)
        #: How XrdCl spells where this file's requests start: the URL an open
        #: was given while it is under way, then the data server it reached.
        self.__origin = ""
        #: Why an open failed. An XrdCl file whose open failed is finished
        #: with: every later open and close answers with this same status.
        self.__failed: XRootDStatus | None = None

    # -- lifecycle -----------------------------------------------------------

    def open(
        self,
        url: str,
        flags: int = 0,
        mode: int = 0,
        timeout: float = 0,
        callback: Callback = None,
    ) -> Any:
        """Open ``url``; ``flags`` are ``OpenFlags``, where ``NONE`` means read."""
        wanted = _args.u16(flags, "flags")
        return self.__open(url, wanted, _args.u16(mode, "mode"), timeout, callback, _PLAIN)

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
        if not wanted & _TEMPLATED:
            return self.__open(url, wanted & 0xFFFF, mode, timeout, callback, _PLAIN)
        if self.__failed is None and self.native is None and template is None:
            return failure(errInvalidOp, "Template file not open"), None
        dup = bool(wanted & OpenFlags.DUP)
        return self.__open(url, wanted & 0xFFFF, mode, timeout, callback, (template, dup))

    def __open(
        self,
        url: str,
        wanted: int,
        mode: int,
        timeout: float,
        callback: Callback,
        template: tuple[NativeFile | None, bool],
    ) -> Any:
        if self.__failed is not None:
            return self.__failed, None
        if self.native is not None:
            return failure(errInvalidOp), None
        config = env.config()
        native = NativeFile(url, config, router=_channels.router_for(url, config))
        native.follow_redirects = self.__flag("FollowRedirects")
        native.recover_handles = self.__flag("ReadRecovery")
        origin = self.__origin = str(URL(url))
        wanted = wanted or OpenFlags.READ
        if wanted & (OpenFlags.NEW | OpenFlags.DELETE):
            # XrdCl sets ``kXR_async`` on every open, and xrootd has long read
            # that bit on a create or truncate as "make the parent directories"
            # (``XrdXrootdXeq.cc``, do_Open) - so with the bindings, writing
            # ``/a/b/f`` creates ``/a/b``. ``MAKEPATH`` asks for it outright.
            wanted |= OpenFlags.MAKEPATH
        source, dup = template

        def opened() -> None:
            try:
                with trace() as hops:
                    if mode:
                        native.open(NativeOpenFlags(wanted), int(mode), template=source, dup=dup)
                    else:
                        native.open(NativeOpenFlags(wanted), template=source, dup=dup)
            except BaseException:
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
            try:
                native.close()
            finally:
                self.native = None

        return self.__run(closed, timeout=timeout, callback=callback)

    def is_open(self) -> bool:
        """Whether :meth:`open` succeeded and :meth:`close` has not been called."""
        return self.native is not None

    def __enter__(self) -> File:
        return self

    def __exit__(self, *exc: object) -> None:
        if self.native is not None:
            self.close()

    def __del__(self) -> None:
        # As with the bindings, a file that is dropped while open is closed -
        # quietly, since whatever it was connected to may already be gone.
        native = getattr(self, "native", None)
        if native is not None:
            _close_quietly(native)

    # -- reading -------------------------------------------------------------

    def read(
        self, offset: int = 0, size: int = 0, timeout: float = 0, callback: Callback = None
    ) -> Any:
        """``(status, bytes)``: ``size`` bytes at ``offset``, or to the end for ``0``."""
        native = self.__opened()
        start, count = _args.u64(offset), _args.u32(size) or -1
        if quick(timeout, callback):
            return now(native.read, _same, count, start)
        return self.__run(lambda: native.read(count, start), _same, timeout, callback)

    def readline(self, offset: int = 0, size: int = 0, chunksize: int = 0) -> str:
        """One line as ``str``, ``""`` at the end; see the module for the cursor."""
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
        self.__opened()
        return self

    def __next__(self) -> str:
        line = self.readline()
        if not line:
            raise StopIteration
        return line

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
            native.follow_redirects = self.__flag("FollowRedirects")
            native._router.follow_redirects = native.follow_redirects
            native.recover_handles = self.__flag("ReadRecovery")
        return True

    def __flag(self, name: str) -> bool:
        return self.__properties[name] == "true"

    # -- plumbing ------------------------------------------------------------

    def __opened(self) -> NativeFile:
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
        return call(
            operation, convert, timeout=_args.u16(timeout), callback=callback, hosts=self.__hosts
        )

    def __hosts(self, hops: list[Hop]) -> HostList:
        native = self.native
        if not hops and native is not None:
            # Answered without a request going through the router - off the
            # bulk data plane, or from what the open already said - but by
            # the data server all the same, and XrdCl names it.
            hops = [_answering(native)]
        return _convert.host_list(hops, self.__origin)

    def __repr__(self) -> str:
        where = str(self.native.url) if self.native is not None else "not open"
        return f"<XRootD.client.File {where}>"


def _answering(native: NativeFile) -> Hop:
    """The data server an open file's requests go to, as a hop that answered."""
    router = native._router
    session = router._session
    if session is None:
        return Hop(router.url)
    flags = getattr(session.protocol, "flags", 0)
    return Hop(router.url, flags=flags, version=session.handshake_version)


def _close_quietly(native: NativeFile) -> None:
    with contextlib.suppress(Exception):
        native.close()


def _marking(mark: Callable[[XRootDStatus], None], callback: Callback) -> Callback:
    """``callback``, first passing a failure to ``mark``: an open that failed is dead."""
    if not callable(callback):
        return callback  # ``None``, or something ``call`` will refuse
    user = callback

    def delivered(status: XRootDStatus, response: Any, hosts: HostList) -> object:
        if not status.ok:
            mark(status)
        return user(status, response, hosts)

    return delivered


def _same(value: T) -> T:
    """The ``convert`` of an operation whose native answer is the response."""
    return value


def _stat(native: NativeFile, force: object) -> Any:
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
        if groups and groups[-1][0] is source:
            groups[-1][1].append(span)
        else:
            groups.append((source, [span]))
    return groups


def _raw(value: str | bytes) -> bytes:
    return value if isinstance(value, bytes) else str(value).encode("utf-8", "surrogateescape")


def _attr(action: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
    """One attribute's outcome, its status a plain dict with XrdCl's empty message."""
    try:
        return action(), dict(vars(OK))
    except ServerError as exc:
        return None, dict(vars(status(errErrorResponse, errno=exc.code)))


def _values(native: NativeFile, names: Sequence[str]) -> list[tuple[str, str, dict[str, Any]]]:
    out = []
    for name in names:
        value, outcome = _attr(partial(native.getxattr, name))
        text = value.decode("utf-8", "surrogateescape") if value is not None else ""
        out.append((name, text, outcome))
    return out
