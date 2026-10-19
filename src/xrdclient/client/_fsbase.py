"""What every :class:`~xrdclient.FileSystem` is, whatever its scheme.

:class:`BaseFileSystem` is the contract the ``root://``, WebDAV and S3
implementations share: the protocol operations each answers in its own way
are abstract here, and everything written in terms of them - ``walk``,
``glob``, ``rmtree``, listing a ZIP archive, the whole-file helpers - lives
here once, so the three cannot drift apart and none of them inherits another
one's wire code by accident.

The concrete classes are siblings: :class:`~xrdclient.client.FileSystem`
(``root://``), :class:`~xrdclient.http.HTTPFileSystem` and, beneath it,
:class:`~xrdclient.s3.S3FileSystem`. ``FileSystem`` is also the name callers
construct and check against - ``FileSystem(url)`` picks the implementation by
scheme, and the other two are registered as its virtual subclasses, so
``isinstance(fs, FileSystem)`` holds for all of them as it always has. Code
that wants to say "any filesystem" in a type annotation, or to implement one
of its own, uses this class.
"""

from __future__ import annotations

import abc
import dataclasses
import errno
import posixpath
import re
from collections.abc import Callable, Iterator, Sequence
from typing import IO, Any, Self

from ..config import Config
from ..errors import InvalidArgumentError, NotFoundError
from ..flags import DirListFlags, LocateFlags, PrepareFlags, QueryCode, StatInfoFlags
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
from ..url import XRootDURL
from . import _zip

__all__ = ["BaseFileSystem"]

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


def _not_empty(exc: OSError) -> bool:
    """Whether a refused ``rmdir`` said "that directory has something in it".

    Stock xrootd reports ``ENOTEMPTY`` as ``kXR_ItExists``, which arrives as
    an ``EEXIST``; the POSIX spelling is accepted as well. dCache sends
    neither: ``kXR_rmdir`` on a full directory comes back as a generic server
    error carrying ``EIO``, with the reason in its text alone ("Failed to
    delete directory (Directory is not empty: ...)"). Read that text where the
    errno does not carry the meaning, so a tree on dCache is cleared instead of
    taken for a server fault and abandoned half-removed.
    """
    if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
        return True
    # ServerError keeps the server's own words in ``message``; a plain OSError
    # has only ``strerror``. Either may be absent, hence the empty fallback.
    text = getattr(exc, "message", None) or exc.strerror or ""
    return "not empty" in text.lower()


class _Links:
    """Whether a path is a symbolic link, from the one source that can say.

    :meth:`BaseFileSystem.is_symlink` is a ``kXR_readlink``, a vendor opcode, and a
    stock server answers it with an error that reads as "not a link" - for a
    link too. So it is believed only from a server whose :meth:`extensions`
    list it, or from a subclass that answers it without asking (WebDAV and S3
    have no links at all). Anywhere else the answer is ``None``: unknown. The
    decision costs one ``kXR_query``, made the first time it is needed.
    """

    def __init__(self, fs: BaseFileSystem) -> None:
        """Bound to ``fs``; the trust decision is deferred until the first question."""
        self._fs = fs
        self._trusted: bool | None = None

    def __call__(self, path: str) -> bool | None:
        """``True``/``False`` for a link or not, or ``None`` when the server cannot say."""
        if self._trusted is None:
            self._trusted = self._decide()
        return self._fs.is_symlink(path) if self._trusted else None

    def _decide(self) -> bool:
        """Whether :meth:`BaseFileSystem.is_symlink` answers can be believed here.

        A subclass that overrides ``is_symlink`` knows its own answer; otherwise
        only a server advertising ``readlink`` in its extensions does. A failed
        query is "no", so an unreachable extension list never makes a walk
        mistake a link for a directory it may delete through.
        """
        if type(self._fs).is_symlink is not BaseFileSystem.is_symlink:
            return True
        try:
            return "readlink" in self._fs.extensions()
        except OSError:
            return False


class _Descent:
    """Which directories one :meth:`BaseFileSystem.walk` goes into.

    ``os.walk`` does not enter a link to a directory unless told to, and a
    walk that does - by request, or because the server cannot say which
    entries are links - still enters each directory once: the stat id is the
    server's device and inode, so a link cycle is seen coming round again.
    """

    def __init__(self, links: _Links, followlinks: bool) -> None:
        """One per walk: ``_seen`` holds stat ids of directories already entered."""
        self._links = links
        self._follow = followlinks
        self._seen: set[str] = set()

    def enters(self, path: str, ident: str) -> bool:
        """Whether the walk should descend into ``path`` (stat id ``ident``).

        Records ``ident`` as entered when it says yes, so call it exactly once per
        candidate directory.
        """
        if not self._follow and self._links(path):
            return False
        if self._identifies(ident):
            if ident in self._seen:
                return False
            self._seen.add(ident)
        return True

    @staticmethod
    def _identifies(ident: str) -> bool:
        """Whether a stat id truly names one directory, for the cycle guard.

        The id is the server's device and inode, so a link that comes round to
        an ancestor is seen the second time its id turns up. That only holds
        when the id identifies: a server that carries no inodes must not have
        its placeholder taken as one identity shared by every directory.
        dCache answers every directory with id ``0`` and S3 with none at all -
        trust either as an id and the walk, having entered the first child,
        takes every later sibling for that same child and stops. Such an id
        guards nothing, and without ``followlinks`` the tree it walks has no
        cycle to guard against; an all-zero (or empty) id is treated as absent.
        """
        return bool(ident.strip("0:"))


class _TreeRemoval:
    """One :meth:`BaseFileSystem.rmtree`: what to remove, and what to do on failure.

    A link is removed with ``kXR_rm``, as ``unlink(2)`` removes a link, and
    never descended into: a listing describes what a link points at, so a
    link to a directory is indistinguishable from one, and following it would
    delete whatever it named, inside the tree or not. Where :class:`_Links`
    cannot tell, the directory is first sent a ``kXR_rmdir``, which never
    follows a link: an empty directory goes, a full one answers "not empty"
    and is cleared, and any other answer means it is not a directory we may
    enter, so it is unlinked instead.
    """

    def __init__(self, fs: BaseFileSystem, cgi: str, ignore_errors: bool) -> None:
        """``cgi`` is re-attached to every child path so a token reaches each request."""
        self._fs = fs
        self._cgi = cgi
        self._ignore = ignore_errors
        self._links = _Links(fs)

    def run(self, target: str) -> None:
        """Remove the tree at ``target``; a link as the root is refused like ``shutil.rmtree``."""
        link = self._links(target)
        if link:
            # ``shutil.rmtree`` refuses a link as its root rather than choose
            # between deleting the link and deleting through it.
            self._fail(NotADirectoryError(errno.ENOTDIR, "rmtree on a symbolic link", target))
        elif link is False or self._full(target, self._fail):
            self._clear(target)

    def _entry(self, path: str, entry: DirEntry) -> None:
        """Remove one listed entry: unlink a file or link, clear then rmdir a directory.

        An entry with no stat in the listing is stat-ed; one that is gone or
        dangling (``None``) is treated as a link and unlinked.
        """
        info = entry.stat if entry.stat is not None else self._fs._stat_or_none(path)
        link = self._links(path) if info is not None and info.is_dir() else True
        if link:
            self._attempt(self._fs.remove, path)
        elif link is False or self._full(path, lambda _: self._attempt(self._fs.remove, path)):
            self._clear(path)

    def _full(self, path: str, refused: Callable[[OSError], None]) -> bool:
        """``kXR_rmdir`` as a question: is this a directory with things in it?"""
        try:
            self._fs.rmdir(path)
        except OSError as exc:
            if _not_empty(exc):
                return True
            refused(exc)
        return False

    def _clear(self, path: str) -> None:
        """Remove what is in the directory ``path``, then the directory."""
        try:
            entries = self._fs.scandir(path)
        except OSError as exc:
            self._fail(exc)
            return
        base, _ = _split_cgi(path)
        for entry in entries:
            self._entry(posixpath.join(base, entry.name) + self._cgi, entry)
        self._attempt(self._fs.rmdir, path)

    def _attempt(self, operation: Callable[[str], None], path: str) -> None:
        """Run ``operation(path)``, routing an ``OSError`` through :meth:`_fail`."""
        try:
            operation(path)
        except OSError as exc:
            self._fail(exc)

    def _fail(self, exc: OSError) -> None:
        if not self._ignore:
            raise exc


class BaseFileSystem(abc.ABC):
    """Namespace and administrative operations on one endpoint, of any scheme.

    Abstract: construct :class:`~xrdclient.FileSystem`, which picks the
    implementation for the URL's scheme. Subclass this to add a scheme of
    your own; the helpers below then work on it unchanged.
    """

    # ------------------------------------------------------------------
    # Plumbing every implementation provides
    # ------------------------------------------------------------------

    #: Where this filesystem's paths are resolved: its endpoint, and the
    #: path and opaque data relative paths start from.
    url: XRootDURL
    #: The settings it connects, authenticates and retries with.
    config: Config

    @abc.abstractmethod
    def _abs(self, path: str) -> str:
        """``path`` made absolute under :attr:`url`, with whatever CGI it carries."""

    @property
    @abc.abstractmethod
    def endpoint(self) -> str:
        """``host:port`` of the server requests go to."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release the connections this filesystem holds. Idempotent."""

    @abc.abstractmethod
    def abort(self) -> None:
        """Stop every call on this filesystem now, and refuse every call after.

        Thread-safe, idempotent, and never blocks on the network: a thread
        blocked in a request on this filesystem's connections is woken, and
        it - and every later call - raises
        :class:`~xrdclient.errors.AbortedError` (``errno`` ``ECANCELED``).
        An aborted filesystem is spent: :meth:`close` it, and make a new one
        to carry on.
        """

    def __enter__(self) -> Self:
        """Use as a context manager; :meth:`close` runs on exit."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the filesystem's connections, whatever happened in the block."""
        self.close()

    def __fspath__(self) -> str:
        """The filesystem's root URL as a string, for APIs that accept ``os.PathLike``."""
        return str(self.url)

    # ------------------------------------------------------------------
    # The protocol operations: what each scheme answers in its own way
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def ping(self) -> None:
        """Round-trip the server. Raises if it is unhealthy."""

    @abc.abstractmethod
    def protocol(self) -> ProtocolInfo:
        """Capabilities of the endpoint, from the connection's negotiation."""

    @abc.abstractmethod
    def stat(self, path: str, *, follow_symlinks: bool = True) -> StatInfo:
        """Metadata for ``path``; :class:`FileNotFoundError` if it is absent."""

    @abc.abstractmethod
    def statvfs(self, path: str = "/") -> VFSInfo:
        """Space and staging utilisation, in ``os.statvfs`` spirit."""

    @abc.abstractmethod
    def statx(self, paths: Sequence[str]) -> list[StatInfo]:
        """Flags-only stat of many paths in one round trip."""

    @abc.abstractmethod
    def scandir(
        self,
        path: str = "",
        *,
        stat: bool = True,
        online: bool = False,
        algorithm: str = "",
        flags: DirListFlags | int | str | None = None,
    ) -> list[DirEntry]:
        """Directory entries, with stat information attached where asked."""

    @abc.abstractmethod
    def dirlist(
        self,
        path: str = "",
        *,
        stat: bool = True,
        online: bool = False,
        flags: DirListFlags | int | str | None = None,
    ) -> bytes:
        """The server's directory listing as it sent it, for a caller that parses it."""

    @abc.abstractmethod
    def mkdir(
        self,
        path: str,
        mode: int | str = 0o755,
        *,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        """Create a directory, with ``pathlib.Path.mkdir`` semantics."""

    @abc.abstractmethod
    def rmdir(self, path: str) -> None:
        """Remove an empty directory."""

    @abc.abstractmethod
    def remove(self, path: str) -> None:
        """Remove a file."""

    @abc.abstractmethod
    def rename(self, src: str, dst: str) -> None:
        """Rename within the same storage element."""

    @abc.abstractmethod
    def symlink(self, target: str, link: str) -> None:
        """``os.symlink`` order: make ``link`` point at ``target``."""

    @abc.abstractmethod
    def link(self, src: str, dst: str) -> None:
        """``os.link`` order: hard-link ``dst`` to ``src``."""

    @abc.abstractmethod
    def readlink(self, path: str) -> str:
        """What a symbolic link points at."""

    @abc.abstractmethod
    def chmod(self, path: str, mode: int | str) -> None:
        """Change a path's permission bits."""

    @abc.abstractmethod
    def utime(
        self,
        path: str,
        times: tuple[float, float] | None = None,
        *,
        ns: tuple[int, int] | None = None,
    ) -> None:
        """``os.utime``: set the access and modification times."""

    @abc.abstractmethod
    def chown(self, path: str, uid: int = -1, gid: int = -1) -> None:
        """``os.chown``: change ownership. ``-1`` leaves an id alone."""

    @abc.abstractmethod
    def truncate(self, path: str, size: int) -> None:
        """Resize a file by path, without opening it."""

    @abc.abstractmethod
    def touch(self, path: str, *, exist_ok: bool = True) -> None:
        """Create an empty file if it is not there."""

    @abc.abstractmethod
    def query(self, code: QueryCode | int | str, args: str = "") -> bytes:
        """Raw ``kXR_query``."""

    @abc.abstractmethod
    def checksum(self, path: str, algorithm: str | None = None) -> ChecksumInfo:
        """Server-computed checksum of ``path``."""

    @abc.abstractmethod
    def query_config(self, *names: str) -> dict[str, str]:
        """Server configuration values, one per name it has a value for."""

    @abc.abstractmethod
    def extensions(self) -> frozenset[str]:
        """Which vendor operations the server admits to implementing."""

    @abc.abstractmethod
    def checksum_cancel(self, path: str) -> None:
        """Abandon a checksum the server is still computing."""

    @abc.abstractmethod
    def query_stats(self, selectors: str = "a") -> str:
        """Server statistics, verbatim."""

    @abc.abstractmethod
    def query_space(self, path: str = "/") -> SpaceInfo:
        """Space available where ``path`` lives."""

    @abc.abstractmethod
    def set_property(self, directive: str) -> None:
        """Set a per-connection property on the server."""

    @abc.abstractmethod
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
        """Which servers hold ``path``."""

    @abc.abstractmethod
    def deep_locate(self, path: str, *, create: bool = False) -> list[LocationInfo]:
        """Locate, resolving managers down to the servers behind them."""

    @abc.abstractmethod
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
        cancel: bool = False,
        reqid: str | None = None,
    ) -> str:
        """Ask the site to bring files onto disk - or, with ``cancel``, to stop."""

    @abc.abstractmethod
    def query_prepare(self, handle: str, paths: Sequence[str]) -> list[PrepareStatus]:
        """How the staging request ``handle`` is going, one entry per path."""

    @abc.abstractmethod
    def cancel_prepare(self, handle: str, paths: Sequence[str] = ()) -> None:
        """Withdraw a staging request, or only ``paths`` of it."""

    @abc.abstractmethod
    def archive_info(self, paths: Sequence[str]) -> list[PrepareStatus]:
        """Where each of these files lives, without asking for any of it to move."""

    @abc.abstractmethod
    def getxattr(self, path: str, name: str) -> bytes:
        """One extended attribute's value."""

    @abc.abstractmethod
    def setxattr(self, path: str, name: str, value: bytes, *, create_only: bool = False) -> None:
        """Set one extended attribute."""

    @abc.abstractmethod
    def removexattr(self, path: str, name: str) -> None:
        """Remove one extended attribute."""

    @abc.abstractmethod
    def listxattr(self, path: str) -> list[str]:
        """The names of ``path``'s extended attributes."""

    @abc.abstractmethod
    def listxattr_tree(self, path: str) -> dict[str, list[str]]:
        """Attribute names for a whole subtree: ``relative path -> names``."""

    @abc.abstractmethod
    def xattrs(self, path: str) -> dict[str, bytes]:
        """Every extended attribute and its value."""

    @abc.abstractmethod
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
        """Open a remote file with :func:`open`'s signature."""

    # ------------------------------------------------------------------
    # Built on the operations above, and so shared by every scheme
    # ------------------------------------------------------------------

    def unlink(self, path: str) -> None:
        """:func:`os.unlink` is :meth:`remove`."""
        self.remove(path)

    def move(self, src: str, dst: str) -> None:
        """``shutil``'s spelling of :meth:`rename`."""
        self.rename(src, dst)

    def hardlink(self, src: str, dst: str) -> None:
        """``os``'s spelling of :meth:`link`."""
        self.link(src, dst)

    # -- interrogation -----------------------------------------------

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

    def exists(self, path: str) -> bool:
        """``True`` if ``path`` resolves. Never raises for a missing file."""
        try:
            self.stat(path)
        except NotFoundError:
            return False
        return True

    def isdir(self, path: str) -> bool:
        """``True`` for a directory; ``False`` when missing, as :func:`os.path.isdir`."""
        try:
            return self.stat(path).is_dir()
        except NotFoundError:
            return False

    def isfile(self, path: str) -> bool:
        """``True`` for a regular file; ``False`` when missing, as :func:`os.path.isfile`."""
        try:
            return self.stat(path).is_file()
        except NotFoundError:
            return False

    def getsize(self, path: str) -> int:
        """Size in bytes, as :func:`os.path.getsize`; raises ``FileNotFoundError`` when missing."""
        return self.stat(path).st_size

    # -- listing -----------------------------------------------------

    def list_archive(self, path: str) -> list[DirEntry]:
        """The members of the ZIP archive at ``path``, read from its central directory.

            for entry in fs.list_archive("/store/run7/logs.zip"):
                print(entry.name, entry.stat.st_size)

        This is XrdCl's ``DirListFlags::Zip`` listing. Only the end of the
        archive is read - the directory records, found through the
        end-of-central-directory record in two or three ranged reads - so a
        large archive costs what a small one does. Names are as stored,
        directories included (``"sub/"``), in the archive's own order.

        Each entry's stat is the one XrdCl gives it: the archive's own, never
        writable, with the member's uncompressed size in place of the
        archive's. A file that is not an archive, or is a damaged one, raises
        :class:`~xrdclient.client._zip.ZipArchiveError` in XrdCl's words for
        what is wrong with it; an empty file is an empty archive.
        """
        return self._archive(path, self.stat(path))

    def _archive(self, path: str, archive: StatInfo) -> list[DirEntry]:
        """:meth:`list_archive`, for an archive already stat-ed as ``archive``."""
        with self.open(path, "rb") as fh:

            def read(at: int, length: int) -> bytes:
                """One ranged read of the archive: the callback :func:`_zip.members` uses."""
                fh.seek(at)
                data: bytes = fh.read(length)
                return data

            found = _zip.members(read, archive.st_size)
        parent = self._abs(path).partition("?")[0]
        flags = archive.flags & ~StatInfoFlags.IS_WRITABLE
        return [
            DirEntry(
                name=member.name,
                parent=parent,
                stat=dataclasses.replace(
                    archive,
                    st_size=member.size,
                    flags=flags,
                    path=f"{parent.rstrip('/')}/{member.name}",
                ),
            )
            for member in found
        ]

    def listdir(self, path: str = "") -> list[str]:
        """Entry names only, like :func:`os.listdir`."""
        return [e.name for e in self.scandir(path, stat=False)]

    def iterdir(self, path: str = "") -> Iterator[DirEntry]:
        """Iterate entries. The protocol has no cursor, so this reads it all."""
        yield from self.scandir(path)

    def walk(
        self,
        top: str = "",
        *,
        topdown: bool = True,
        onerror: object = None,
        followlinks: bool = False,
    ) -> Iterator[tuple[str, list[str], list[str]]]:
        """``os.walk`` over the remote namespace.

        As in ``os.walk``, a link to a directory is listed among the
        directories but not entered unless ``followlinks`` says so. A stock
        server cannot say which entries are links (``kXR_readlink`` is a
        vendor extension), and there the walk follows them - reading, not
        deleting - but enters each directory once, so a link cycle ends.
        """
        root = self._abs(top or "/")
        yield from self._walk(root, topdown, onerror, _Descent(_Links(self), followlinks))

    def _walk(
        self, root: str, topdown: bool, onerror: object, descent: _Descent
    ) -> Iterator[tuple[str, list[str], list[str]]]:
        """Recursive body of :meth:`walk` for one directory (``root`` carries its CGI).

        Children are joined to the CGI-less ``base`` and the CGI re-appended, so
        the yielded ``dirpath`` is a plain path while every request still sends
        the token.
        """
        entries = self._walk_entries(root, onerror)
        if entries is None:
            return
        # Opaque data belongs on the request, not in the name of a directory:
        # what is yielded is a path, and what descends carries the token.
        base, cgi = _split_cgi(root)
        dirs, files, ids = self._partition(base, cgi, entries)
        if topdown:
            yield base, dirs, files
        for name in list(dirs):
            child = posixpath.join(base, name) + cgi
            if descent.enters(child, ids.get(name, "")):
                yield from self._walk(child, topdown, onerror, descent)
        if not topdown:
            yield base, dirs, files

    def _walk_entries(self, root: str, onerror: object) -> list[DirEntry] | None:
        """The listing of ``root``, or ``None`` after reporting the error to ``onerror``."""
        try:
            return self.scandir(root)
        except OSError as exc:
            if callable(onerror):
                onerror(exc)
            return None

    def _partition(
        self, base: str, cgi: str, entries: Sequence[DirEntry]
    ) -> tuple[list[str], list[str], dict[str, str]]:
        """Directories, everything else, and each directory's stat id.

        A server that ignores ``kXR_dstat`` lists names alone, and an entry
        without a stat is not known to be a file: it is asked about, as
        ``os.walk`` falls back to a ``stat`` when ``d_type`` says nothing.
        """
        dirs: list[str] = []
        files: list[str] = []
        ids: dict[str, str] = {}
        for entry in entries:
            info = entry.stat
            if info is None:
                info = self._stat_or_none(posixpath.join(base, entry.name) + cgi)
            if info is not None and info.is_dir():
                dirs.append(entry.name)
                ids[entry.name] = info.id
            else:
                files.append(entry.name)
        return dirs, files, ids

    def _stat_or_none(self, path: str) -> StatInfo | None:
        """A stat, or ``None`` for an entry that is gone or a dangling link."""
        try:
            return self.stat(path)
        except NotFoundError:
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
        """Match a pattern whose wildcards are all in its last component: one listing."""
        for entry in self.scandir(start + cgi, stat=False):
            full = posixpath.join(start, entry.name)
            if match(full):
                yield full

    def _deep_glob(self, start: str, cgi: str, match: Any) -> Iterator[str]:
        """Match a pattern that spans directories by walking from its literal prefix."""
        for dirpath, dirs, files in self.walk(start + cgi):
            for name in sorted(dirs + files):
                full = posixpath.join(dirpath, name)
                if match(full):
                    yield full

    # -- mutation ----------------------------------------------------

    def makedirs(self, path: str, mode: int | str = 0o755, exist_ok: bool = False) -> None:
        """:func:`os.makedirs`."""
        self.mkdir(path, mode, parents=True, exist_ok=exist_ok)

    def rmtree(self, path: str, *, ignore_errors: bool = False) -> None:
        """``shutil.rmtree``. There is no server-side primitive.

        Symbolic links are removed, never followed: nothing outside the tree
        is touched, and a link as ``path`` itself is refused. Stock xrootd
        will not unlink a link, so a tree holding one fails to go rather
        than be emptied through it; see :class:`_TreeRemoval` for how a link
        is told from a directory on a server that cannot say.
        """
        target = self._abs(path)
        _, cgi = _split_cgi(target)
        _TreeRemoval(self, cgi, ignore_errors).run(target)

    # -- staging and properties --------------------------------------

    def evict(self, paths: Sequence[str]) -> str:
        """Ask the server to drop its cached copies."""
        return self.prepare(paths, evict=True)

    def appid(self, name: str) -> None:
        """Label this connection in the server's monitoring stream.

        An operator looking at the server sees which application the traffic
        belongs to instead of another anonymous connection. The name is
        advisory and a server that does not monitor ignores it.
        """
        self.set_property(f"appid {name}")

    # -- whole files -------------------------------------------------

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
        """Whole-file read decoded as ``encoding``, like :meth:`pathlib.Path.read_text`.

        Subject to :attr:`~xrdclient.Config.max_read_size`, as :meth:`read_bytes` is.
        """
        with self.open(path, "r", encoding=encoding) as fh:
            text: str = fh.read()
        return text

    def write_text(self, path: str, text: str, encoding: str = "utf-8") -> int:
        """Whole-file write, like :meth:`pathlib.Path.write_text`; returns characters written."""
        with self.open(path, "w", encoding=encoding) as fh:
            written: int = fh.write(text)
        return written
