"""``XRootD.client.FileSystem``, returning ``(XRootDStatus, response)``.

Each method is one native call wrapped by :func:`._dispatch.call`, which turns
an exception into a status and supplies the ``timeout=`` and ``callback=``
behaviour every method of the bindings has. The native object underneath is
reachable as :attr:`FileSystem.native` for code partway through a port.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Optional, TypeVar

from ...client.filesystem import FileSystem as NativeFileSystem
from ...errors import ServerError
from ...flags import LocateFlags
from ...proto import constants as c
from ...proto import requests as r
from ...session.router import Hop, trace
from ...types import DirEntry
from . import _args, _convert, env
from ._dispatch import call, no_answer, now, quick, stream
from ._status import (
    OK,
    UnsupportedURLError,
    errErrorResponse,
    errInvalidArgs,
    errNotSupported,
    failure,
    status,
    stOK,
    suPartial,
)
from .copyprocess import CopyProcess
from .flags import AccessMode, DirListFlags, MkDirFlags
from .responses import DirectoryList, HostList, XRootDStatus
from .url import URL

__all__ = ["FileSystem"]

T = TypeVar("T")

#: A callback, as the bindings take one: ``callback(status, response, hostlist)``.
Callback = Optional[Callable[[XRootDStatus, Any, HostList], object]]

#: What :meth:`FileSystem.mkdir` creates with when no mode is given: rwxr-x---.
_DEFAULT_DIR_MODE = AccessMode.UR | AccessMode.UW | AccessMode.UX | AccessMode.GR | AccessMode.GX

#: The client-side properties a filesystem has, and their defaults.
_PROPERTIES = {"FollowRedirects": "true"}

#: What XrdCl's ``DirList`` with ``Locate`` locates with: prefer host names,
#: ``kXR_compress``'s bit, and "this is for a directory listing".
_LOCATE_FOR_LISTING = LocateFlags.PREFER_NAME | LocateFlags.FOR_DIRLIST | LocateFlags.ADD_PEERS


class FileSystem:
    """Filesystem operations on one server, in the bindings' shape.

    ``FileSystem("root://host")`` connects on first use, like the bindings'.
    """

    def __init__(self, url: str) -> None:
        self.__url = str(url)
        parsed = URL(self.__url)
        # XrdCl takes a URL it cannot parse without complaint and refuses
        # every operation on it afterwards; a valid one is handed on in the
        # form XrdCl reads it in, so ``"host"`` means ``root://host:1094``.
        self.__valid = parsed.is_valid()
        target = str(parsed) if self.__valid else "root://invalid/"
        self.native = NativeFileSystem(target, env.config())
        #: How XrdCl spells where this filesystem's requests start.
        self.__origin = target if self.__valid else ""
        self.__properties = dict(_PROPERTIES)

    @property
    def url(self) -> URL:
        """The server URL, as a :class:`~.url.URL`."""
        return URL(self.__url)

    def __run(
        self,
        operation: Callable[[], Any],
        convert: Callable[[Any], Any] = no_answer,
        timeout: float = 0,
        callback: Callback = None,
    ) -> Any:
        if not self.__valid:
            operation = _unsupported
        return call(
            operation, convert, timeout=_args.u16(timeout), callback=callback, hosts=self.__hosts
        )

    def __now(self, timeout: object, callback: Callback) -> bool:
        """Whether a call can skip :meth:`__run`: no timeout, no callback, a valid URL."""
        return self.__valid and quick(timeout, callback)

    def __hosts(self, hops: list[Hop]) -> HostList:
        return _convert.host_list(hops, self.__origin)

    def __answerer(self, hops: list[Hop]) -> str:
        """The ``hostaddr`` of a listing: the server that answered it, as XrdCl names it."""
        if not hops:
            return str(self.native.endpoint)
        return URL(_convert.hop_url(hops[-1], self.__origin)).hostid

    # -- namespace -----------------------------------------------------------

    def stat(self, path: str, timeout: float = 0, callback: Callback = None) -> Any:
        """``(status, StatInfo)`` for ``path``."""
        if self.__now(timeout, callback):
            return now(self.native.stat, _convert.stat_info, path)
        return self.__run(lambda: self.native.stat(path), _convert.stat_info, timeout, callback)

    def statvfs(self, path: str, timeout: float = 0, callback: Callback = None) -> Any:
        """``(status, StatInfoVFS)`` for the space behind ``path``."""
        return self.__run(
            lambda: self.native.statvfs(path), _convert.stat_info_vfs, timeout, callback
        )

    def dirlist(
        self, path: str, flags: int = 0, timeout: float = 0, callback: Callback = None
    ) -> Any:
        """``(status, DirectoryList)``; ``DirListFlags.STAT`` fills in ``statinfo``.

        ``RECURSIVE`` names each entry by its path below ``path``, the way
        XrdCl does. ``MERGE`` sorts the listing by name and drops duplicates.
        ``LOCATE``, without a callback, lists the directory on every data
        server a manager locates it on and puts the answers together - merged
        with ``MERGE`` - each entry naming its server in ``hostaddr``; asked
        of a data server it is a plain listing. ``CHUNKED`` hands a callback
        each part of the listing as the server sends it, with
        ``[SUCCESS] Continue`` until the last; without a callback XrdCl
        refuses it, and so does this.

        ``ZIP`` lists the members of the archive ``path`` names, from its
        central directory, each with the archive's stat and the member's
        size; a directory is listed as if the flag were not there. As in
        XrdCl, a call without a callback takes a path ending in ``.zip`` to
        mean ``ZIP`` whether or not the flag says so.
        """
        wanted = _args.u16(flags, "flags")
        if callback is None:
            if wanted & DirListFlags.CHUNKED:
                # XrdCl's synchronous DirList cannot deliver parts.
                return failure(errNotSupported), None
            if path.endswith(".zip"):
                # XrdCl's synchronous DirList does this; the asynchronous one,
                # which is what a callback gets, does not.
                wanted |= DirListFlags.ZIP
            if wanted & DirListFlags.LOCATE:
                return self.__located(path, wanted, _args.u16(timeout))
        elif wanted & DirListFlags.CHUNKED and self.__valid:
            return stream(
                lambda part: self.__in_parts(path, wanted, part),
                timeout=_args.u16(timeout),
                callback=callback,
                hosts=self.__hosts,
            )
        if self.__now(timeout, callback):
            return now(self.__list, _same, path, wanted)
        return self.__run(lambda: self.__list(path, wanted), _same, timeout, callback)

    def __list(self, path: str, flags: int) -> DirectoryList:
        with trace() as hops:
            listed = self.__listed(path, flags)
        listing = listed(self.__answerer(hops))
        return _merged(listing) if flags & DirListFlags.MERGE else listing

    def __listed(self, path: str, flags: int) -> Callable[[str], DirectoryList]:
        """The listing, still to be told which server answered it."""
        if flags & DirListFlags.ZIP:
            # XrdCl stats first, and lists a directory as if the flag were not
            # there; the listing of an archive is named by its path, CGI off.
            info = self.native.stat(path)
            if not info.is_dir():
                members = self.native._archive(path, info)
                archive = _convert.listing(path.partition("?")[0], members, self.native.endpoint)
                return lambda _: archive
        if flags & DirListFlags.RECURSIVE:
            below = self.__below(path)
            return lambda host: _convert.directory_list(path, below, host)
        entries = self.native.scandir(path, stat=bool(flags & DirListFlags.STAT))
        return lambda host: _convert.listing(path, entries, host)

    def __in_parts(self, path: str, flags: int, part: Callable[[Any], None]) -> DirectoryList:
        """A listing a part at a time: every part but the last to ``part``, the last returned.

        A part is known to be the last only once the answer is complete, so
        each is held back until the next one arrives.
        """
        if flags & DirListFlags.RECURSIVE:
            return self.__list(path, flags)  # XrdCl delivers a recursive listing whole
        merge = _PartMerge() if flags & DirListFlags.MERGE else None
        held: list[DirectoryList] = []
        with trace() as hops:

            def arrived(entries: list[DirEntry]) -> None:
                if held:
                    part(held.pop())
                listing = _convert.listing(path, entries, self.__answerer(hops))
                held.append(merge(listing) if merge is not None else listing)

            self.native._scandir_parts(path, arrived, stat=bool(flags & DirListFlags.STAT))
        return held.pop() if held else _convert.listing(path, [], self.__answerer(hops))

    def __located(self, path: str, flags: int, timeout: int) -> tuple[XRootDStatus, Any]:
        """XrdCl's synchronous ``DirList`` with ``Locate``: every server's listing, together."""
        found, servers = call(lambda: self.__servers(path), _same, timeout=timeout)
        rest = flags & ~DirListFlags.LOCATE
        if not found.ok:
            return found, None
        if servers is None:
            # A data server is the only one that can hold the directory.
            listed: tuple[XRootDStatus, Any] = call(
                lambda: self.__list(path, rest), _same, timeout=timeout
            )
            return listed
        if not servers:
            # XrdCl's deep locate, finding no server, says so as a server would.
            return status(errErrorResponse, errno=3011, message=_NO_LOCATION), None
        return self.__gathered(path, rest, list(servers), timeout)

    def __servers(self, path: str) -> list[str] | None:
        """The data servers a deep locate finds ``path`` on, or ``None`` on a data server."""
        if not self.__valid:
            raise UnsupportedURLError
        router = getattr(self.native, "_router", None)
        if router is None or getattr(router.session.protocol, "flags", 0) & c.kXR_isServer:
            return None
        located = self.native._deep_locate(path, create=True, flags=_LOCATE_FOR_LISTING)
        return [location.address for location in located]

    def __gathered(
        self, path: str, flags: int, servers: list[str], timeout: int
    ) -> tuple[XRootDStatus, DirectoryList]:
        """Each server's listing of ``path``, one after another, as XrdCl asks them."""
        scheme = self.native.url.scheme
        entries: list[Any] = []
        failed: XRootDStatus | None = None
        failures = 0
        for server in servers:
            outcome, listing = FileSystem(f"{scheme}://{server}/").dirlist(path, flags, timeout)
            if not outcome.ok:
                failed, failures = outcome, failures + 1
                continue
            entries.extend(listing.dirlist)
        together = _convert.gathered(path, entries)
        if flags & DirListFlags.MERGE:
            together = _merged(together)
        if failed is None:
            return OK, together
        # Every server failing is the last failure; some of them, a partial answer.
        return (failed if failures == len(servers) else _PARTIAL), together

    def __below(self, top: str) -> list[tuple[str, Any]]:
        """Everything under ``top``, a level at a time, named relative to it.

        This is XrdCl's order - each directory's entries, then the entries of
        each of its subdirectories in turn - and like XrdCl it always has the
        stat information, since it needed that to know what to descend into.
        """
        found: list[tuple[str, Any]] = []
        pending = [""]
        while pending:
            prefix = pending.pop(0)
            for entry in self.native.scandir(f"{top.rstrip('/')}/{prefix}", stat=True):
                found.append((f"{prefix}{entry.name}", entry.stat))
                if entry.is_dir():
                    pending.append(f"{prefix}{entry.name}/")
        return found

    def mkdir(
        self,
        path: str,
        flags: int = 0,
        mode: int = 0,
        timeout: float = 0,
        callback: Callback = None,
    ) -> Any:
        """Create a directory; ``MkDirFlags.MAKEPATH`` creates its parents too."""
        permissions = _args.u16(mode, "mode") or _DEFAULT_DIR_MODE
        parents = bool(_args.u16(flags, "flags") & MkDirFlags.MAKEPATH)
        return self.__run(
            lambda: self.native.mkdir(path, permissions, parents=parents),
            timeout=timeout,
            callback=callback,
        )

    def rmdir(self, path: str, timeout: float = 0, callback: Callback = None) -> Any:
        """Remove an empty directory."""
        return self.__run(lambda: self.native.rmdir(path), timeout=timeout, callback=callback)

    def rm(self, path: str, timeout: float = 0, callback: Callback = None) -> Any:
        """Remove a file."""
        return self.__run(lambda: self.native.remove(path), timeout=timeout, callback=callback)

    def mv(self, source: str, dest: str, timeout: float = 0, callback: Callback = None) -> Any:
        """Rename ``source`` to ``dest`` on this server."""
        return self.__run(
            lambda: self.native.rename(source, dest), timeout=timeout, callback=callback
        )

    def truncate(self, path: str, size: int, timeout: float = 0, callback: Callback = None) -> Any:
        """Cut or extend ``path`` to ``size`` bytes."""
        length = _args.u64(size, "size")
        return self.__run(
            lambda: self.native.truncate(path, length), timeout=timeout, callback=callback
        )

    def chmod(self, path: str, mode: int, timeout: float = 0, callback: Callback = None) -> Any:
        """Set ``path``'s permissions to ``mode``, an ``AccessMode`` combination."""
        bits = _args.u16(mode, "mode")
        return self.__run(lambda: self.native.chmod(path, bits), timeout=timeout, callback=callback)

    def locate(self, path: str, flags: int, timeout: float = 0, callback: Callback = None) -> Any:
        """``(status, LocationInfo)``: the servers that hold ``path``.

        ``flags`` are ``OpenFlags``, as in the bindings; the ones ``kXR_locate``
        understands - ``REFRESH``, ``NOWAIT`` and ``MAKEPATH``'s bit, which the
        protocol reads as "prefer names" - are passed on.
        """
        wanted = _args.u16(flags, "flags") & (128 | 256 | 8192)
        return self.__run(
            lambda: self.native.locate(path, flags=wanted),
            _convert.location_info,
            timeout,
            callback,
        )

    def deeplocate(
        self, path: str, flags: int, timeout: float = 0, callback: Callback = None
    ) -> Any:
        """``(status, LocationInfo)`` with every manager followed to its servers."""
        del flags  # the bindings take them; a deep locate has none of its own
        return self.__run(
            lambda: self.native.deep_locate(path), _convert.location_info, timeout, callback
        )

    # -- server --------------------------------------------------------------

    def ping(self, timeout: float = 0, callback: Callback = None) -> Any:
        """Check the server answers."""
        if self.__now(timeout, callback):
            return now(self.native.ping, no_answer)
        return self.__run(self.native.ping, timeout=timeout, callback=callback)

    def protocol(self, timeout: float = 0, callback: Callback = None) -> Any:
        """``(status, ProtocolInfo)``."""
        return self.__run(self.native.protocol, _convert.protocol_info, timeout, callback)

    def query(self, querycode: int, arg: str, timeout: float = 0, callback: Callback = None) -> Any:
        """``(status, bytes)``: the server's raw answer to a ``QueryCode`` question."""
        return self.__run(lambda: self.native.query(int(querycode), arg), _same, timeout, callback)

    def prepare(
        self,
        files: Sequence[str],
        flags: int,
        priority: int = 0,
        timeout: float = 0,
        callback: Callback = None,
    ) -> Any:
        """``(status, bytes)``: stage ``files``; the answer is the request handle."""
        wanted = _args.u16(flags, "flags")
        priority = _args.u16(priority, "priority")
        entries = [str(entry) for entry in files]
        return self.__run(
            lambda: self.__prepare(entries, wanted, priority), _same, timeout, callback
        )

    def __prepare(self, entries: list[str], flags: int, priority: int) -> bytes:
        """``kXR_prepare`` with the list as given, the way XrdCl sends it.

        XrdCl joins the entries and sends them untouched: a cancel's leading
        request id is not a path, and a full URL is the server's to read, so
        neither is resolved against this filesystem the way a native
        :meth:`~xrdclient.FileSystem.prepare` resolves its paths. Over HTTP,
        where staging is the Tape REST API, the native call is the way in.
        """
        router = getattr(self.native, "_router", None)
        if router is None:
            return str(self.native.prepare(entries, flags=flags, priority=priority)).encode()
        request = r.Prepare(entries, flags & 0xFF, priority, extended=flags >> 8)
        return bytes(router.execute(request).data).split(b"\x00", 1)[0]

    def sendinfo(self, info: str, timeout: float = 0, callback: Callback = None) -> Any:
        """``(status, bytes)``: tell the server's monitoring about this client."""
        if len(info) > 1024:
            return failure(errInvalidArgs, "info is limited to 1024 characters"), None
        return self.__run(lambda: self.__monitor(info), _same, timeout, callback)

    def __monitor(self, info: str) -> bytes:
        router = getattr(self.native, "_router", None)
        if router is None:
            raise NotImplementedError("sendinfo needs a root:// server")
        return bytes(router.execute(r.Set(f"monitor info {info}")).data)

    # -- extended attributes -------------------------------------------------

    def set_xattr(
        self,
        path: str,
        attrs: Iterable[tuple[str, str]],
        timeout: float = 0,
        callback: Callback = None,
    ) -> Any:
        """``(status, [(name, status)])``: set each ``(name, value)``."""
        values = {name: _raw(value) for name, value in attrs}

        def one(name: str) -> None:
            self.native.setxattr(path, name, values[name])

        return self.__run(lambda: _statuses(list(values), one), _same, timeout, callback)

    def get_xattr(
        self, path: str, attrs: Iterable[str], timeout: float = 0, callback: Callback = None
    ) -> Any:
        """``(status, [(name, value, status)])``; a name that is missing has ``""``."""
        names = list(attrs)

        def one(name: str) -> bytes:
            return self.native.getxattr(path, name)

        return self.__run(lambda: _values(names, one), _same, timeout, callback)

    def del_xattr(
        self, path: str, attrs: Iterable[str], timeout: float = 0, callback: Callback = None
    ) -> Any:
        """``(status, [(name, status)])``: remove each named attribute."""
        names = list(attrs)

        def one(name: str) -> None:
            self.native.removexattr(path, name)

        return self.__run(lambda: _statuses(names, one), _same, timeout, callback)

    def list_xattr(self, path: str, timeout: float = 0, callback: Callback = None) -> Any:
        """``(status, [(name, value, status)])`` for every attribute ``path`` has."""
        return self.__run(lambda: _listed(self.native.xattrs(path)), _same, timeout, callback)

    # -- whole files ---------------------------------------------------------

    def copy(self, source: str, target: str, force: bool = False) -> tuple[XRootDStatus, None]:
        """Copy ``source`` to ``target``, both full URLs; ``force`` overwrites.

        One :class:`CopyProcess` job, as the bindings run it, so the two
        report a failure alike.
        """
        process = CopyProcess()
        process.add_job(source, target, force=force)
        return process.run()[0], None

    def cat(self, path: str) -> XRootDStatus:
        """Write ``path``'s contents to standard output; returns the status alone."""
        status, data = call(lambda: self.native.read_bytes(path), _same)
        if status.ok:
            out = getattr(sys.stdout, "buffer", None)
            if out is not None:
                out.write(data)
            else:
                sys.stdout.write(data.decode("utf-8", "replace"))
            sys.stdout.flush()
        return status  # type: ignore[no-any-return]

    # -- properties ----------------------------------------------------------

    def get_property(self, name: str) -> str | None:
        """A client-side property, or ``None`` for one this client does not have."""
        return self.__properties.get(name)

    def set_property(self, name: str, value: str) -> bool:
        """Set a client-side property; ``False`` for one this client does not have.

        ``FollowRedirects`` is ``"true"`` or, for any other value, ``"false"``,
        as XrdCl reads it; off, a redirect is the answer - ``errRedirect``
        (401), its message naming where the server pointed.
        """
        if name not in self.__properties:
            return False
        follow = str(value) == "true"
        self.__properties[name] = "true" if follow else "false"
        router = getattr(self.native, "_router", None)
        if router is not None:
            router.follow_redirects = follow
        return True

    def __del__(self) -> None:
        # The bindings have no ``close``: a filesystem lets its connection go
        # when it is collected, so this one hands its own back to the pool
        # then too, rather than leaving the socket for the interpreter.
        native = getattr(self, "native", None)
        if native is not None:
            with contextlib.suppress(Exception):
                native.close()

    def __repr__(self) -> str:
        return f"<XRootD.client.FileSystem {self.__url!r}>"


def _same(value: T) -> T:
    """The ``convert`` of an operation whose native answer is the response."""
    return value


#: How XrdCl's deep locate words finding nothing.
_NO_LOCATION = "No valid location found"

#: What XrdCl returns when some of the servers a ``LOCATE`` listing asked failed.
_PARTIAL = status(suPartial, level=stOK)


def _key(entry: Any) -> tuple[str, int, int, int]:
    """How XrdCl's merge orders entries - and so which count as the same.

    By name; then an entry without stat information before one with; then
    by size and flags. Two with the same name, size and flags are one entry.
    """
    info = entry.statinfo
    if info is None:
        return entry.name, 0, 0, 0
    return entry.name, 1, info.size, info.flags


def _unique(entries: Iterable[Any]) -> dict[tuple[str, int, int, int], Any]:
    """The first of each set of entries XrdCl's merge counts as the same."""
    found: dict[tuple[str, int, int, int], Any] = {}
    for entry in entries:
        found.setdefault(_key(entry), entry)
    return found


def _merged(listing: DirectoryList) -> DirectoryList:
    """XrdCl's ``Merge``: the listing sorted, duplicates dropped."""
    unique = _unique(listing.dirlist)
    return _convert.gathered(listing.parent, [unique[key] for key in sorted(unique)])


class _PartMerge:
    """XrdCl's ``MergeChunked``: each part merged, less what earlier parts had."""

    def __init__(self) -> None:
        self._seen: set[tuple[str, int, int, int]] = set()

    def __call__(self, listing: DirectoryList) -> DirectoryList:
        unique = _unique(listing.dirlist)
        fresh = sorted(key for key in unique if key not in self._seen)
        self._seen.update(fresh)
        return _convert.gathered(listing.parent, [unique[key] for key in fresh])


def _unsupported() -> None:
    """What every operation on a filesystem with an invalid URL does."""
    # No detail: XrdCl's message for this is the bare description.
    raise UnsupportedURLError


def _raw(value: str | bytes) -> bytes:
    return value if isinstance(value, bytes) else str(value).encode("utf-8", "surrogateescape")


def _text(value: bytes) -> str:
    return value.decode("utf-8", "surrogateescape")


def _item(action: Callable[[str], T], name: str) -> tuple[T | None, dict[str, Any]]:
    """Run one attribute's action; its status is a plain dict, as in the bindings.

    Only the server refusing *this* name is that name's status. A dropped
    connection fails every name at once, so it propagates and fails the call.
    """
    try:
        return action(name), dict(vars(OK))
    except ServerError as exc:
        # The protocol carries only a code per attribute, so XrdCl's status
        # for one has an empty message; the native text is not the server's.
        return None, dict(vars(status(errErrorResponse, errno=exc.code)))


def _statuses(
    names: Sequence[str], action: Callable[[str], None]
) -> list[tuple[str, dict[str, Any]]]:
    return [(name, _item(action, name)[1]) for name in names]


def _values(
    names: Sequence[str], fetch: Callable[[str], bytes]
) -> list[tuple[str, str, dict[str, Any]]]:
    out = []
    for name in names:
        value, status = _item(fetch, name)
        out.append((name, _text(value) if value is not None else "", status))
    return out


def _listed(attrs: dict[str, bytes]) -> list[tuple[str, str, dict[str, Any]]]:
    return [(name, _text(value), dict(vars(OK))) for name, value in attrs.items()]
