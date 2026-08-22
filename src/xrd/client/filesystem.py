"""Namespace operations against one storage endpoint.

:class:`FileSystem` is the explicit, per-operation layer. It reads like
``os`` and ``shutil`` because those are the names Python programmers already
know: :meth:`stat`, :meth:`listdir`, :meth:`makedirs`, :meth:`remove`,
:meth:`rename`, :meth:`walk`.

    >>> fs = FileSystem("root://eos.example.org")
    >>> fs.makedirs("/store/user/me/out", exist_ok=True)
    >>> for entry in fs.scandir("/store/user/me"):
    ...     print(entry.name, entry.stat.st_size)
"""

from __future__ import annotations

import errno
import os
import posixpath
import re
import urllib.parse
from collections.abc import Iterator, Sequence
from typing import IO, Any

from .._compat import zip_strict
from .._log import get_logger
from ..config import Config
from ..errors import (
    AttrNotFoundError,
    ExistsError,
    InvalidArgumentError,
    IOError_,
    NotFoundError,
    PermissionError_,
    ProtocolError,
    ServerError,
    kXR_AttrNotFound,
    kXR_IOError,
    kXR_ItExists,
    kXR_NotAuthorized,
    kXR_NotFound,
)
from ..flags import (
    Access,
    DirListFlags,
    LocateFlags,
    OpenFlags,
    PrepareFlags,
    QueryCode,
    StatInfoFlags,
    dirlist_flags,
    locate_flags,
    permissions,
    prepare_flags,
)
from ..proto import constants as c
from ..proto import requests as r
from ..proto import responses as rp
from ..session.router import Router
from ..types import (
    ChecksumInfo,
    DirEntry,
    LocationInfo,
    PrepareStatus,
    ProtocolInfo,
    SpaceInfo,
    StatInfo,
    VFSInfo,
)
from ..url import XRootDURL, parse

__all__ = ["FileSystem"]

_log = get_logger(__name__)

_MAGIC = "*?["


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile a glob pattern, slash-aware.

    :func:`fnmatch.fnmatch` is no use here because its ``*`` swallows path
    separators, which makes ``/store/*/file`` match three levels down and
    ``**`` mean nothing in particular. This is the pathlib reading: ``**/``
    is zero or more directories, everything else stays in its component.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[":
            end = pattern.find("]", i + 1)
            if end < 0:  # an unclosed bracket is a literal one, as in fnmatch
                out.append(re.escape("["))
                i += 1
                continue
            body = pattern[i + 1 : end].replace("\\", "\\\\")
            out.append("[" + ("^" + body[1:] if body.startswith("!") else body) + "]")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def _literal_prefix(pattern: str) -> str:
    """The deepest directory of ``pattern`` that contains no wildcard."""
    parts = pattern.split("/")[:-1]
    keep: list[str] = []
    for part in parts:
        if any(ch in part for ch in _MAGIC):
            break
        keep.append(part)
    return "/".join(keep) or "/"


def _split_cgi(path: str) -> tuple[str, str]:
    """A resolved path split into the path itself and its opaque suffix."""
    base, sep, cgi = path.partition("?")
    return base, (sep + cgi if sep else "")


def _cgi(explicit: str, inherited: dict[str, str]) -> str:
    """The opaque suffix for a path: what was asked for, plus what was implied."""
    if not inherited:
        return f"?{explicit}" if explicit else ""
    named = {key for key, _ in urllib.parse.parse_qsl(explicit, keep_blank_values=True)}
    extra = urllib.parse.urlencode({k: v for k, v in inherited.items() if k not in named})
    if not extra:
        return f"?{explicit}" if explicit else ""
    return f"?{explicit}&{extra}" if explicit else f"?{extra}"


def _partition(entries: Sequence[DirEntry]) -> tuple[list[str], list[str]]:
    dirs = [entry.name for entry in entries if entry.is_dir()]
    files = [entry.name for entry in entries if not entry.is_dir()]
    return dirs, files


class FileSystem:
    """Namespace and administrative operations on one endpoint."""

    def __new__(
        cls, url: str | XRootDURL, config: Config | None = None, **_extra: object
    ) -> FileSystem:
        """Pick the implementation the scheme calls for.

        ``_extra`` is whatever a subclass's own constructor takes - S3's
        credentials, say - which ``__new__`` has no use for but must accept,
        because Python hands it the same arguments ``__init__`` gets.

        ``root://`` is this class; ``https://`` and the WebDAV spellings are
        :class:`~xrd.http.HTTPFileSystem`, and ``s3://`` is
        :class:`~xrd.s3.S3FileSystem`. All three offer the same methods. The
        dispatch lives in ``__new__`` for the same reason
        :class:`pathlib.Path`'s does: callers should name what they want, not
        which implementation provides it.
        """
        if cls is FileSystem:
            target = parse(url)
            if target.is_http:
                from ..http.dav import HTTPFileSystem

                return object.__new__(HTTPFileSystem)
            if target.is_s3:
                from ..s3.fs import S3FileSystem

                return object.__new__(S3FileSystem)
        return object.__new__(cls)

    def __init__(self, url: str | XRootDURL, config: Config | None = None) -> None:
        self.url = parse(url) if isinstance(url, str) else url
        self.config = config or Config()
        self._router = Router(self.url, self.config)

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _abs(self, path: str) -> str:
        """Resolve ``path`` against the URL's path, carrying the opaque data.

        Opaque data is how ``root://`` passes an authorisation token, so a
        token on the filesystem's own URL belongs on every path derived from
        it - and a caller who spells the same key out for one call means that
        one, so their value wins.
        """
        base, sep, cgi = path.partition("?")
        if not base.startswith("/"):
            base = posixpath.join(self.url.path or "/", base)
        return posixpath.normpath(base) + _cgi(cgi if sep else "", self.url.query)

    def _url_for(self, path: str) -> XRootDURL:
        """The URL of ``path`` under this filesystem, with the CGI on it once.

        :meth:`_abs` has already folded in whatever the filesystem's own URL
        carried, so the query is cleared rather than applied a second time.
        """
        return self.url.evolve(path=self._abs(path), query={})

    @property
    def endpoint(self) -> str:
        return self._router.endpoint

    def close(self) -> None:
        self._router.close()

    def __enter__(self) -> FileSystem:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"FileSystem({str(self.url)!r})"

    def __fspath__(self) -> str:
        return str(self.url)

    # ------------------------------------------------------------------
    # Interrogation
    # ------------------------------------------------------------------

    def ping(self) -> None:
        """Round-trip the server. Raises if it is unhealthy."""
        self._router.execute(r.Ping())

    def protocol(self) -> ProtocolInfo:
        """Capabilities of the endpoint, from the connection's negotiation."""
        info = self._router.session.protocol
        assert isinstance(info, ProtocolInfo)
        return info

    def stat(self, path: str, *, follow_symlinks: bool = True) -> StatInfo:
        """``kXR_stat``. Raises :class:`FileNotFoundError` if absent.

        ``follow_symlinks=False`` asks for the link itself, as ``os.lstat``
        does. That is a vendor option: a server that does not know it follows
        the link as usual, and the reply does not say which happened, so
        :meth:`is_symlink` is the reliable question to ask.
        """
        target = self._abs(path)
        options = 0 if follow_symlinks else c.kXR_statNoFollow
        res = self._router.execute(r.Stat(target, options), path=target)
        return rp.parse_stat(res.data, target)

    def lstat(self, path: str) -> StatInfo:
        """``os.lstat``: stat a symbolic link rather than what it points at."""
        return self.stat(path, follow_symlinks=False)

    def is_symlink(self, path: str) -> bool:
        """Whether ``path`` is a symbolic link.

        Asked as a :meth:`readlink`, because that is the question the server
        can answer without ambiguity: a stat that followed the link looks
        exactly like a stat of a file that never was one.
        """
        try:
            self.readlink(path)
        except (NotFoundError, InvalidArgumentError):
            return False
        return True

    def statvfs(self, path: str = "/") -> VFSInfo:
        """Space and staging utilisation, in ``os.statvfs`` spirit."""
        target = self._abs(path)
        res = self._router.execute(r.StatVFS(target), path=target)
        return rp.parse_statvfs(res.data)

    def statx(self, paths: Sequence[str]) -> list[StatInfo]:
        """Flags-only stat of many paths in one round trip."""
        targets = [self._abs(p) for p in paths]
        res = self._router.execute(r.Statx(targets))
        flags = rp.parse_statx(res.data)
        if len(flags) != len(targets):
            raise ProtocolError(
                f"statx returned {len(flags)} flags for {len(targets)} paths"
            )
        return [StatInfo(flags=f, path=p) for f, p in zip_strict(flags, targets)]

    def exists(self, path: str) -> bool:
        """``True`` if ``path`` resolves. Never raises for a missing file."""
        try:
            self.stat(path)
        except NotFoundError:
            return False
        return True

    def isdir(self, path: str) -> bool:
        try:
            return self.stat(path).is_dir()
        except NotFoundError:
            return False

    def isfile(self, path: str) -> bool:
        try:
            return self.stat(path).is_file()
        except NotFoundError:
            return False

    def getsize(self, path: str) -> int:
        return self.stat(path).st_size

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def scandir(
        self,
        path: str = "",
        *,
        stat: bool = True,
        online: bool = False,
        algorithm: str = "",
        flags: DirListFlags | int | str | None = None,
    ) -> list[DirEntry]:
        """Directory entries with stat information attached.

        ``stat=False`` asks for names alone, which is one short answer
        instead of a long one when that is all you need, and ``online=True``
        asks the server to leave out anything it would have to fetch from
        tape to describe.

        ``algorithm`` asks the server to digest every entry as it lists it
        (``kXR_dcksm``), which is one round trip where a checksum per entry is
        one each::

            for entry in fs.scandir("/store/run7", algorithm="adler32"):
                print(entry.name, entry.checksum)

        The digest lands on :attr:`DirEntry.checksum`, and is ``None`` for an
        entry the server had none for - a directory, or a file it could not
        read. A server that ignores the option answers an ordinary listing, so
        every entry comes back with ``None``; an algorithm it does not have is
        an error on the listing rather than a listing without digests.
        """
        target = self._abs(path or "/")
        options = dirlist_flags(stat=stat, online=online, algorithm=algorithm, flags=flags)
        if algorithm:
            target += ("&" if "?" in target else "?") + f"cks.type={algorithm}"
        with_stat = bool(options & DirListFlags.STAT)
        res = self._router.execute(
            r.Dirlist(target, int(options) & ~int(DirListFlags.RECURSIVE)), path=target
        )
        return rp.parse_dirlist(res.data, target, with_stat=with_stat)

    def listdir(self, path: str = "") -> list[str]:
        """Entry names only, like :func:`os.listdir`."""
        return [e.name for e in self.scandir(path, stat=False)]

    def iterdir(self, path: str = "") -> Iterator[DirEntry]:
        """Iterate entries. The protocol has no cursor, so this reads it all."""
        yield from self.scandir(path)

    def walk(
        self, top: str = "", *, topdown: bool = True, onerror: object = None
    ) -> Iterator[tuple[str, list[str], list[str]]]:
        """``os.walk`` over the remote namespace."""
        root = self._abs(top or "/")
        entries = self._walk_entries(root, onerror)
        if entries is None:
            return
        # Opaque data belongs on the request, not in the name of a directory:
        # what is yielded is a path, and what descends carries the token.
        base, cgi = _split_cgi(root)
        dirs, files = _partition(entries)
        if topdown:
            yield base, dirs, files
        for name in list(dirs):
            child = posixpath.join(base, name) + cgi
            yield from self.walk(child, topdown=topdown, onerror=onerror)
        if not topdown:
            yield base, dirs, files

    def _walk_entries(self, root: str, onerror: object) -> list[DirEntry] | None:
        try:
            return self.scandir(root)
        except OSError as exc:
            if callable(onerror):
                onerror(exc)
            return None

    def glob(self, pattern: str, *, root: str = "") -> Iterator[str]:
        """Match ``pattern`` against the namespace, absolute paths out.

        The semantics are :meth:`pathlib.Path.glob`'s, because that is what a
        caller writing ``**/*.root`` means: ``*`` and ``?`` stay inside one
        path component, ``**`` crosses them, and directories match as well as
        files. A relative pattern is taken from ``root``, an absolute one as
        it stands.

        Only the directories a pattern can actually reach are listed: the
        literal prefix is walked, not the whole namespace, so
        ``glob("/store/mc/**/*.root")`` never asks about ``/store/data``.
        """
        base, cgi = _split_cgi(self._abs(root or "/"))
        target = pattern if pattern.startswith("/") else posixpath.join(base, pattern)
        match = _glob_regex(target).fullmatch
        start = _literal_prefix(target)
        deep = "**" in posixpath.basename(target)  # ``/d/**.root`` still descends
        if not deep and start == posixpath.dirname(target):  # magic in the last component only
            yield from self._flat_glob(start, cgi, match)
            return
        yield from self._deep_glob(start, cgi, match)

    def _flat_glob(self, start: str, cgi: str, match: Any) -> Iterator[str]:
        for entry in self.scandir(start + cgi, stat=False):
            full = posixpath.join(start, entry.name)
            if match(full):
                yield full

    def _deep_glob(self, start: str, cgi: str, match: Any) -> Iterator[str]:
        for dirpath, dirs, files in self.walk(start + cgi):
            for name in sorted(dirs + files):
                full = posixpath.join(dirpath, name)
                if match(full):
                    yield full

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def mkdir(
        self,
        path: str,
        mode: int | str = 0o755,
        *,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        """Create a directory, with ``pathlib.Path.mkdir`` semantics."""
        target = self._abs(path)
        try:
            self._router.execute(
                r.Mkdir(target, permissions(mode), mkpath=parents), path=target
            )
        except FileExistsError:
            # ``exist_ok`` forgives an existing *directory*, as pathlib does;
            # a file squatting on the name is still an error.
            if not exist_ok or not self.isdir(path):
                raise

    def makedirs(self, path: str, mode: int | str = 0o755, exist_ok: bool = False) -> None:
        """:func:`os.makedirs`."""
        self.mkdir(path, mode, parents=True, exist_ok=exist_ok)

    def rmdir(self, path: str) -> None:
        """Remove an empty directory."""
        target = self._abs(path)
        self._router.execute(r.Rmdir(target), path=target)

    def remove(self, path: str) -> None:
        """Remove a file."""
        target = self._abs(path)
        self._router.execute(r.Rm(target), path=target)

    #: :func:`os.unlink` is the same call.
    unlink = remove

    def rmtree(self, path: str, *, ignore_errors: bool = False) -> None:
        """Recursively remove a directory. There is no server-side primitive."""
        target = self._abs(path)
        _, cgi = _split_cgi(target)
        for dirpath, dirs, files in self.walk(target, topdown=False):
            self._remove_names(self.remove, dirpath, files, cgi, ignore_errors)
            self._remove_names(self.rmdir, dirpath, dirs, cgi, ignore_errors)
        self._remove_tree_root(target, ignore_errors)

    def _remove_names(
        self,
        operation: Any,
        directory: str,
        names: Sequence[str],
        cgi: str,
        ignore_errors: bool,
    ) -> None:
        for name in names:
            try:
                operation(posixpath.join(directory, name) + cgi)
            except OSError:
                if not ignore_errors:
                    raise

    def _remove_tree_root(self, target: str, ignore_errors: bool) -> None:
        try:
            self.rmdir(target)
        except OSError:
            if not ignore_errors:
                raise

    def rename(self, src: str, dst: str) -> None:
        """Rename within the same storage element."""
        source, destination = self._abs(src), self._abs(dst)
        self._router.execute(r.Mv(source, destination), path=source)

    #: ``shutil``'s spelling.
    move = rename

    def symlink(self, target: str, link: str) -> None:
        """``os.symlink`` order: make ``link`` point at ``target``.

        A vendor extension (``kXR_symlink``), not part of XProtocol - the same
        opcode XRootD.jl and XrdRust use. A server that has not been taught it
        answers :class:`~xrd.errors.UnsupportedError`, which is the honest
        answer for a namespace that has no links.
        """
        source, destination = self._abs(target), self._abs(link)
        self._router.execute(r.Symlink(source, destination), path=destination)

    def link(self, src: str, dst: str) -> None:
        """``os.link`` order: hard-link ``dst`` to ``src``. Vendor extension."""
        source, destination = self._abs(src), self._abs(dst)
        self._router.execute(r.Link(source, destination), path=destination)

    #: ``os``'s spelling of the same call.
    hardlink = link

    def readlink(self, path: str) -> str:
        """What a symbolic link points at. Vendor extension."""
        target = self._abs(path)
        result = self._router.execute(r.Readlink(target), path=target)
        return rp.parse_readlink(result.data)

    def chmod(self, path: str, mode: int | str) -> None:
        target = self._abs(path)
        self._router.execute(r.Chmod(target, permissions(mode)), path=target)

    def utime(
        self,
        path: str,
        times: tuple[float, float] | None = None,
        *,
        ns: tuple[int, int] | None = None,
    ) -> None:
        """``os.utime``: set the access and modification times.

        ``times`` is ``(atime, mtime)`` in seconds, ``ns`` the same pair in
        integer nanoseconds, and neither of them means "now" - the two are
        mutually exclusive, as they are in ``os``. Nanoseconds survive the
        round trip; the protocol carries seconds and nanoseconds separately.

        A vendor extension (``kXR_setattr``), like the link family, and one
        that never follows a final symbolic link - the server applies it the
        way ``os.utime(..., follow_symlinks=False)`` would.
        """
        if times is not None and ns is not None:
            raise ValueError("utime: specify either times or ns, not both")
        if times is None and ns is None:
            now = (0, c.UTIME_NOW)
            atime, mtime = now, now
        else:
            pair = ns if ns is not None else times
            assert pair is not None
            if len(pair) != 2:
                raise TypeError("utime: times/ns must be a pair (atime, mtime)")
            scale = 1 if ns is not None else 10**9
            atime, mtime = (divmod(round(v * scale), 10**9) for v in pair)
        target = self._abs(path)
        request = r.Setattr(target, c.kXR_sa_times, atime, mtime)
        self._router.execute(request, path=target)

    def chown(self, path: str, uid: int = -1, gid: int = -1) -> None:
        """``os.chown``: change ownership. ``-1`` leaves an id alone.

        The same vendor extension :meth:`utime` uses, with the same rule about
        symbolic links: it changes the link, not what the link points at.
        """
        target = self._abs(path)
        request = r.Setattr(target, c.kXR_sa_owner, uid=int(uid), gid=int(gid))
        self._router.execute(request, path=target)

    def truncate(self, path: str, size: int) -> None:
        """Resize a file by path, without opening it."""
        target = self._abs(path)
        self._router.execute(r.Truncate(target, size), path=target)

    def touch(self, path: str, *, exist_ok: bool = True) -> None:
        """Create an empty file if it is not there.

        ``kXR_new`` is the only flag that creates without truncating, and a
        server refuses it when the file exists - which is precisely the case
        ``exist_ok`` is about, so it is caught rather than pre-checked. The
        mtime of an existing file is left alone, because XProtocol has no
        request that moves it and rewriting the file to fake one would be
        worse than not doing it; where the server speaks the vendor extension,
        :meth:`utime` is the call that means ``touch`` on something that is
        already there.
        """
        from .file import File

        flags = OpenFlags.NEW | OpenFlags.UPDATE | OpenFlags.MAKEPATH
        fh = File(self._url_for(path), self.config, router=self._router)
        try:
            fh.open(flags=flags, mode=Access.OWNER_READ | Access.OWNER_WRITE)
        except ExistsError:
            if not exist_ok:
                raise
            return
        fh.close()

    # ------------------------------------------------------------------
    # Query, checksums, staging, location
    # ------------------------------------------------------------------

    def query(self, code: QueryCode | int | str, args: str = "") -> bytes:
        """Raw ``kXR_query``."""
        res = self._router.execute(r.Query(int(QueryCode(code)), args), path=args)
        return res.data

    def checksum(self, path: str, algorithm: str | None = None) -> ChecksumInfo:
        """Server-computed checksum. ``algorithm`` selects via CGI when given."""
        target = self._abs(path)
        if algorithm:
            target += ("&" if "?" in target else "?") + f"cks.type={algorithm}"
        res = self._router.execute(r.Query(c.kXR_Qcksum, target), path=target)
        return rp.parse_checksum(res.data)

    def query_config(self, *names: str) -> dict[str, str]:
        """``kXR_query`` config lookup; one value per requested name.

        Named ``query_config`` and not ``config`` because :attr:`config` is
        this filesystem's own :class:`~xrd.config.Config`.

        A name the server has no value for is absent from the result, the
        way a missing key is absent from a :class:`dict`. Splitting on
        ``\\n`` rather than by lines keeps the remaining names lined up with
        their values when an earlier one comes back empty.
        """
        wanted = list(names) or ["version"]
        res = self._router.execute(r.Query(c.kXR_Qconfig, "\n".join(wanted)))
        body = res.data.split(b"\x00", 1)[0].decode("utf-8", "replace")
        values = body.split("\n")
        return {name: value for name, value in zip(wanted, values) if value}

    def extensions(self) -> frozenset[str]:
        """Which vendor opcodes the server admits to implementing.

        ``setattr``, ``symlink``, ``readlink`` and ``link`` are extensions no
        stock XRootD has, so a program that must not fail can ask before it
        asks::

            if "setattr" in fs.extensions():
                fs.utime(path)

        The answer is the server's ``xrdfs.ext`` configuration value. A server
        that has never heard of the key answers by echoing it back or by
        saying nothing at all, and both arrive here as an empty set - which is
        the right answer for a stock daemon, and the reason this is worth one
        round trip rather than an exception per call.
        """
        listed = self.query_config("xrdfs.ext").get("xrdfs.ext", "")
        # The one server that answers this repeats the key in the value.
        names = {name.strip() for name in listed.rpartition("=")[2].split(",")}
        return frozenset(names - {"", "xrdfs.ext"})

    def checksum_cancel(self, path: str) -> None:
        """Abandon a checksum the server is still computing (``kXR_Qckscan``).

        Checksumming a multi-terabyte file costs the server a full read of it.
        A caller that has given up - a timeout, a cancelled job - says so,
        rather than leaving the server to finish work nobody is waiting for.
        """
        target = self._abs(path)
        self._router.execute(r.Query(c.kXR_Qckscan, target), path=target)

    def query_stats(self, selectors: str = "a") -> str:
        """Server statistics as the XML summary ``kXR_QStats`` answers with.

        ``selectors`` is the letter set the protocol defines - ``"a"`` for all
        of them, or a subset such as ``"io"``. The XML is returned verbatim:
        the schema is the server's monitoring format, it varies by version and
        by which plugins are loaded, and parsing it here would be inventing a
        structure the protocol does not promise.
        """
        res = self._router.execute(r.Query(c.kXR_QStats, selectors))
        return res.data.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()

    def query_space(self, path: str = "/") -> SpaceInfo:
        """Space available where ``path`` lives (``kXR_Qspace``).

        This is the space *token* - the named pool ``oss.cgroup`` selects -
        reported in bytes, where :meth:`statvfs` describes the whole storage
        element in megabytes. A site that has separate pools for different
        experiments answers differently for two paths on the same server.
        """
        target = self._abs(path)
        res = self._router.execute(r.Query(c.kXR_Qspace, target), path=target)
        return rp.parse_space(res.data)

    def set_property(self, directive: str) -> None:
        """Set a per-connection property on the server (``kXR_set``).

        The directive is the protocol's own text, ``"<what> <value>"``. Only
        ``appid`` is defined for clients, and :meth:`appid` is the way to send
        it; this is here for a server that understands more.
        """
        self._router.execute(r.Set(directive))

    def appid(self, name: str) -> None:
        """Label this connection in the server's monitoring stream.

        An operator looking at the server sees which application the traffic
        belongs to instead of another anonymous connection. The name is
        advisory and a server that does not monitor ignores it.
        """
        self.set_property(f"appid {name}")

    def locate(
        self,
        path: str,
        *,
        create: bool = False,
        refresh: bool = False,
        no_wait: bool = False,
        add_peers: bool = False,
        prefer_name: bool = False,
        flags: LocateFlags | int | str | None = None,
    ) -> list[LocationInfo]:
        """Which servers hold ``path``.

        ``refresh=True`` makes the redirector ask its servers again rather
        than answer from what it remembers, and ``no_wait=True`` takes
        whatever it can say now instead of waiting for a file to come back
        from tape. The rarer options are still there as words or bits:
        ``flags="add_peers refresh"``.

        ``create=True`` asks where the file *would* go instead of where it is:
        the redirector picks a server with room for it, and a path that does
        not exist yet is the expected case rather than an error. It is the
        question a writer has to ask before it can place anything, and the
        only way to get an answer for a file that is not there.
        """
        target = self._abs(path)
        options = locate_flags(
            refresh=refresh,
            no_wait=no_wait,
            add_peers=add_peers,
            prefer_name=prefer_name,
            flags=flags,
        )
        # The ``*`` goes on the wire and nowhere else: it is a mode, not part
        # of the name, and an error about it should still name the file.
        wire = f"*{target}" if create else target
        res = self._router.execute(r.Locate(wire, int(options)), path=target)
        return rp.parse_locate(res.data)

    def deep_locate(self, path: str, *, create: bool = False) -> list[LocationInfo]:
        """Locate, resolving managers down to the servers behind them."""
        seen: dict[str, LocationInfo] = {}
        pending = list(self.locate(path, create=create))
        while pending:
            loc = pending.pop()
            known = seen.get(loc.address)
            if known is not None:
                # A supervisor answers as a manager to the tier above it and as
                # a server to the tier below; keeping only the first answer
                # would drop a node that does hold the file.
                if known.is_manager and not loc.is_manager:
                    seen[loc.address] = loc
                continue
            seen[loc.address] = loc
            if loc.is_manager:
                child = FileSystem(self.url.evolve(host=loc.host, port=loc.port), self.config)
                try:
                    pending.extend(child.locate(path, create=create))
                except OSError:
                    pass
                finally:
                    child.close()
        return [v for v in seen.values() if not v.is_manager]

    def prepare(
        self,
        paths: Sequence[str],
        *,
        stage: bool | None = None,
        evict: bool = False,
        notify: bool = False,
        fresh: bool = False,
        priority: int = 0,
        flags: PrepareFlags | int | str | None = None,
    ) -> str:
        """Ask the site to bring files onto disk. Returns the request handle.

        A bare ``fs.prepare(paths)`` stages, which is what a tape site is
        being asked for nine times in ten::

            handle = fs.prepare(["/store/raw/run7.root"])
            while not all(fs.query_prepare(handle, paths)):
                time.sleep(60)

        ``evict=True`` releases the disk copy instead, ``notify=True`` asks
        to be told when it is done, and ``fresh=True`` re-stages a file the
        site thinks it already has. The remaining ``kXR_prepare`` options are
        reachable as words: ``flags="stage colocate"``.
        """
        targets = [self._abs(p) for p in paths]
        options = prepare_flags(stage=stage, evict=evict, notify=notify, fresh=fresh, flags=flags)
        # The options byte, then the extended half-word above it: see
        # PrepareFlags, where EVICT is the one that lives up there.
        request = r.Prepare(targets, int(options) & 0xFF, priority, extended=int(options) >> 8)
        res = self._router.execute(request)
        return res.data.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()

    def evict(self, paths: Sequence[str]) -> str:
        """Ask the server to drop its cached copies."""
        return self.prepare(paths, evict=True)

    def archive_info(self, paths: Sequence[str]) -> list[PrepareStatus]:
        """Where each of these files lives, without asking for any of it to move.

        One round trip for the lot, because it is a :meth:`statx`: the flags a
        server answers with say whether a file is offline, which is the same
        question the HTTP tape API's ``archiveinfo`` asks. A server that keeps
        a copy on disk *and* on tape reports the file as online, since the
        protocol has one flag and it means "not readable now".
        """
        reports = []
        for info in self.statx(paths):
            here = not info.flags & StatInfoFlags.OTHER
            offline = info.is_offline()
            reports.append(
                PrepareStatus(
                    path=info.path,
                    exists=here,
                    on_tape=here and offline,
                    online=here and not offline,
                    error="" if here else "no such file",
                    # The tape API's vocabulary, so that the two schemes answer
                    # this question in the same words as well as the same shape.
                    state=("NEARLINE" if offline else "ONLINE") if here else "",
                )
            )
        return reports

    def query_prepare(self, handle: str, paths: Sequence[str]) -> list[PrepareStatus]:
        """How the staging :meth:`prepare` asked for is going (``kXR_QPrep``).

        ``prepare`` returns as soon as the server has taken the request down,
        which for a tape-backed site is a long time before the files are
        readable. This is the question that has an answer worth waiting on:
        one :class:`~xrd.types.PrepareStatus` per path, in the order asked,
        each of which is true once its file is online.

        The handle is the one :meth:`prepare` returned; a server that has
        never heard of it says so rather than reporting the files as absent.
        """
        targets = [self._abs(p) for p in paths]
        args = "\n".join([handle, *targets])
        res = self._router.execute(r.Query(c.kXR_QPrep, args), path=targets[0] if targets else "")
        return rp.parse_prepare_status(res.data)

    def cancel_prepare(self, handle: str) -> None:
        """Withdraw a staging request :meth:`prepare` made.

        The handle takes the place of the path list, which is what makes this
        a separate method rather than a flag on :meth:`prepare`: cancelling
        names the request, not the files, and passing paths here would ask the
        server to cancel requests whose handles happen to look like filenames.
        """
        self._router.execute(r.Prepare([handle], int(PrepareFlags.CANCEL), 0))

    # ------------------------------------------------------------------
    # Extended attributes
    # ------------------------------------------------------------------

    def getxattr(self, path: str, name: str) -> bytes:
        """One attribute value, in ``os.getxattr`` spirit."""
        target = self._abs(path)
        res = self._router.execute(r.Fattr.get(target, name), path=target)
        result = rp.parse_fattr(res.data)
        for item in result.items:
            if item.code == 0 and item.value is not None:
                return item.value
        raise _attr_error(name, target)

    def setxattr(self, path: str, name: str, value: bytes, *, create_only: bool = False) -> None:
        target = self._abs(path)
        res = self._router.execute(
            r.Fattr.set(target, name, value, create_only=create_only), path=target
        )
        _check_fattr(rp.parse_fattr(res.data, values=False), target)

    def removexattr(self, path: str, name: str) -> None:
        target = self._abs(path)
        res = self._router.execute(r.Fattr.delete(target, name), path=target)
        _check_fattr(rp.parse_fattr(res.data, values=False), target)

    def listxattr(self, path: str) -> list[str]:
        target = self._abs(path)
        res = self._router.execute(r.Fattr.list(target), path=target)
        return [item.name for item in rp.parse_fattr(res.data, values=False).items]

    def listxattr_tree(self, path: str) -> dict[str, list[str]]:
        """Attribute names for a whole subtree: ``relative path -> names``.

        One round trip for a directory that :meth:`listxattr` would have to
        walk. It is a vendor extension - ``kXR_fattrRecurse``, nginx-xrootd's
        ``kXR_fa_recurse`` - and a server without it lists the directory's own
        attributes instead, which parses as an empty tree rather than as an
        error, so treat the empty answer as "nothing, or nobody listening".

        Only regular files are reported, the paths are relative to *path*, and
        a server that hits its reply ceiling drops the rest of the tree
        silently; a subtree of any size is safer walked with :meth:`walk`.
        """
        target = self._abs(path)
        res = self._router.execute(r.Fattr.list(target, recurse=True), path=target)
        return rp.parse_fattr_tree(res.data)

    def xattrs(self, path: str) -> dict[str, bytes]:
        """Every attribute and its value, in one round trip."""
        target = self._abs(path)
        res = self._router.execute(r.Fattr.list(target, values=True), path=target)
        return rp.parse_fattr(res.data).as_dict()

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    def open(
        self,
        path: str,
        mode: str = "rb",
        *,
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
        posc: bool = False,
    ) -> IO[Any]:
        """Open a remote file with :func:`open`'s signature.

        The element type follows the mode, so a caller that wants ``bytes``
        or ``str`` statically should reach for :func:`xrd.open`, whose
        overloads know the difference.
        """
        from ..io import open_url

        return open_url(
            self._url_for(path),
            mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
            config=self.config,
            router=self._router,
            posc=posc,
        )

    def read_bytes(self, path: str) -> bytes:
        """Whole-file read, like :meth:`pathlib.Path.read_bytes`."""
        with self.open(path, "rb") as fh:
            data: bytes = fh.read()
        return data

    def write_bytes(self, path: str, data: bytes) -> int:
        """Whole-file write, like :meth:`pathlib.Path.write_bytes`."""
        with self.open(path, "wb") as fh:
            written: int = fh.write(data)
        return written

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        with self.open(path, "r", encoding=encoding) as fh:
            text: str = fh.read()
        return text

    def write_text(self, path: str, text: str, encoding: str = "utf-8") -> int:
        with self.open(path, "w", encoding=encoding) as fh:
            written: int = fh.write(text)
        return written


def _attr_error(name: str, path: str) -> ServerError:
    return AttrNotFoundError(kXR_AttrNotFound, f"no attribute {name!r}", path=path)


#: ``kXR_fattr`` reports one ``errno`` per attribute, not a ``kXR_`` code, and
#: a whole-request ``kXR_ok`` can still carry a failed attribute inside it.
_ATTR_ERRNO: dict[int, tuple[type[ServerError], int]] = {
    errno.EEXIST: (ExistsError, kXR_ItExists),
    errno.ENODATA: (AttrNotFoundError, kXR_AttrNotFound),
    errno.ENOENT: (NotFoundError, kXR_NotFound),
    errno.EACCES: (PermissionError_, kXR_NotAuthorized),
}


def _check_fattr(result: rp.FattrResult, path: str) -> None:
    """Raise for the first attribute the server refused."""
    for item in result.items:
        if item.code:
            kind, code = _ATTR_ERRNO.get(item.code, (IOError_, kXR_IOError))
            raise kind(code, f"{os.strerror(item.code)}: attribute {item.name!r}", path=path)
