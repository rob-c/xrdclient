"""Namespace operations against one storage endpoint.

:class:`FileSystem` is the explicit, per-operation layer, and the ``root://``
implementation of :class:`~xrdclient.client.BaseFileSystem`; constructed with
an ``http(s)``/``dav(s)`` or ``s3`` URL it hands back the WebDAV or S3
sibling instead (see :meth:`FileSystem.__new__`). It reads like
``os`` and ``shutil`` because those are the names Python programmers already
know: :meth:`stat`, :meth:`listdir`, :meth:`makedirs`, :meth:`remove`,
:meth:`rename`, :meth:`walk`.

    >>> fs = FileSystem("root://eos.example.org")
    >>> fs.makedirs("/store/user/me/out", exist_ok=True)
    >>> for entry in fs.scandir("/store/user/me"):
    ...     print(entry.name, entry.stat.st_size)
"""

from __future__ import annotations

import posixpath
import urllib.parse
from collections.abc import Callable, Sequence
from typing import IO, TYPE_CHECKING, Any, Unpack, cast

from ..config import Config
from ..errors import ExistsError, ProtocolError
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
from ..log import get_logger, redact
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
from . import _fattr
from ._fsbase import BaseFileSystem

# Re-exported: defined here before the shared helpers moved to the base
# class, and still imported from this module.
from ._fsbase import _glob_regex as _glob_regex

if TYPE_CHECKING:
    from ..s3.fs import S3Options
from ._fsbase import _split_cgi as _split_cgi

__all__ = ["BaseFileSystem", "FileSystem"]

_log = get_logger(__name__)


def _cgi(explicit: str, inherited: XRootDURL) -> str:
    """The opaque suffix for a path: what was asked for, plus what was implied.

    The inherited fields go on as the filesystem's URL spelled them, byte for
    byte: decoding and re-encoding would turn ``authz=Bearer%20...`` into
    ``Bearer+...``, which a token-checking server does not accept.
    """
    named = {key for key, _ in urllib.parse.parse_qsl(explicit, keep_blank_values=True)}
    extra = inherited.cgi_except(named)
    if not extra:
        return f"?{explicit}" if explicit else ""
    return f"?{explicit}&{extra}" if explicit else f"?{extra}"


class _Batches:
    """Each part of a listing, parsed and handed on as the server sends it.

    Only the first part of a stat-ed listing opens with the dot entry that
    says the stat lines are there, so the first part decides how every later
    one is read.
    """

    def __init__(
        self, target: str, with_stat: bool, deliver: Callable[[list[DirEntry]], None]
    ) -> None:
        """``deliver`` receives each part's entries; ``with_stat`` is what was asked for."""
        self._target = target
        self._with_stat = with_stat
        self._deliver = deliver
        self._started = False

    def __call__(self, piece: bytes) -> None:
        """Parse one ``kXR_oksofar``/final part of a ``kXR_dirlist`` reply and deliver it.

        Whether the listing really carries stat lines is decided by the first
        part alone (a server that ignores ``kXR_dstat`` sends names only), and
        that decision then governs every later part.
        """
        if not self._started:
            self._started = True
            self._with_stat = self._with_stat and piece.startswith(b".\n")
        elif self._with_stat:
            # A later part is pairs of lines with no dot entry of its own:
            # lend it one, which the parser reads as the directory and drops.
            piece = b".\n.\n" + piece
        self._deliver(rp.parse_dirlist(piece, self._target, with_stat=self._with_stat))


def _refuse_s3_options(options: S3Options, url: XRootDURL) -> None:
    """Refuse S3's own keywords for a URL that is not ``s3://``, as a constructor would."""
    if options:
        names = ", ".join(sorted(options))
        raise TypeError(f"{names}: only an s3:// filesystem takes these, not {url.scheme}://")


class FileSystem(BaseFileSystem):
    """Namespace and administrative operations on one endpoint.

    The ``root://`` implementation, and the name to construct and to check
    against for every scheme: ``FileSystem("davs://...")`` is an
    :class:`~xrdclient.http.HTTPFileSystem` and ``FileSystem("s3://...")`` an
    :class:`~xrdclient.s3.S3FileSystem`, both registered as virtual
    subclasses, so ``isinstance(fs, FileSystem)`` is true of all three.
    """

    def __new__(
        cls, url: str | XRootDURL, config: Config | None = None, **s3: Unpack[S3Options]
    ) -> FileSystem:
        """Pick the implementation the scheme calls for.

        ``s3`` is what :class:`~xrdclient.s3.S3FileSystem` alone takes
        (credentials, region, endpoint); any other scheme refuses it.

        ``root://`` is this class; ``https://`` and the WebDAV spellings are
        :class:`~xrdclient.http.HTTPFileSystem`, and ``s3://`` is
        :class:`~xrdclient.s3.S3FileSystem`. All three offer the same methods. The
        dispatch lives in ``__new__`` for the same reason
        :class:`pathlib.Path`'s does: callers should name what they want, not
        which implementation provides it.

        The other two are siblings of this class under
        :class:`~xrdclient.client.BaseFileSystem`, not subclasses of it, so
        none of the ``root://`` wire code is inherited where it cannot work.
        Their instance is therefore built here, whole: Python runs
        ``__init__`` after ``__new__`` only for an instance of a real
        subclass, and these are virtual ones (``FileSystem.register``).
        """
        if cls is FileSystem:
            target = parse(url)
            if target.is_http:
                from ..http.dav import HTTPFileSystem

                _refuse_s3_options(s3, target)
                return cast("FileSystem", HTTPFileSystem(url, config))
            if target.is_s3:
                from ..s3.fs import S3FileSystem

                return cast("FileSystem", S3FileSystem(url, config, **s3))
        return super().__new__(cls)

    def __init__(
        self, url: str | XRootDURL, config: Config | None = None, **s3: Unpack[S3Options]
    ) -> None:
        """Bind to ``url``; no connection is made until the first request.

        ``s3`` is refused here: only an ``s3://`` URL takes it (see :meth:`__new__`).

        The router is not sticky: every request starts at this URL - the
        manager, in a federation - and a redirect sends that one request on,
        not the filesystem. The next path may well live on another data server.
        """
        self.url = parse(url) if isinstance(url, str) else url
        _refuse_s3_options(s3, self.url)
        self.config = config or Config()
        self._router = Router(self.url, self.config, sticky=False)

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
        return posixpath.normpath(base) + _cgi(cgi if sep else "", self.url)

    def _url_for(self, path: str) -> XRootDURL:
        """The URL of ``path`` under this filesystem, with the CGI on it once.

        :meth:`_abs` has already folded in whatever the filesystem's own URL
        carried, so the query is cleared rather than applied a second time.
        """
        return self.url.evolve(path=self._abs(path), query={})

    @property
    def endpoint(self) -> str:
        """``host:port`` of the URL this filesystem starts every request from."""
        return self._router.endpoint

    def close(self) -> None:
        """Give the connection back: pooled if nobody else is using it. Idempotent.

        Files opened from this filesystem that share its connection keep it
        alive until they close too; see :meth:`Router.close`.
        """
        self._router.close()

    def abort(self) -> None:
        """Stop every request on this filesystem now, and refuse every one after.

        Thread-safe and idempotent; never blocks on the network. The
        connections this filesystem's requests are out on - its own, and the
        pooled ones a redirected request borrowed - are shut down, which wakes
        a thread blocked on one at once, and its request raises
        :class:`~xrdclient.errors.AbortedError` (``errno`` ``ECANCELED``), as
        does every call made afterwards. Nothing is retried or reconnected,
        and the connection is never pooled again.

        For a caller that must give up on a silent server from another thread
        - a cancelled job, a watchdog - where a timeout would come too late::

            threading.Timer(30, fs.abort).start()
            fs.stat(path)          # AbortedError after 30 s, if still waiting

        The connection is what an abort reaches: a file opened from this
        filesystem that shares its connection finds it dropped, and fails as
        it would if the server had gone away. Abort the
        :class:`~xrdclient.File` itself to stop only that one.
        """
        self._router.abort()

    def __repr__(self) -> str:
        """``FileSystem('<url>')``, with credentials in the URL's CGI redacted.

        See :meth:`File.__repr__`: a ``repr`` is routinely logged, so tokens
        never appear in it.
        """
        return f"FileSystem({redact(str(self.url))!r})"

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
            raise ProtocolError(f"statx returned {len(flags)} flags for {len(targets)} paths")
        return [StatInfo(flags=f, path=p) for f, p in zip(flags, targets, strict=True)]

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

        To list inside a ZIP archive, as XrdCl's ``DirListFlags::Zip`` does,
        see :meth:`list_archive`.
        """
        chosen = dirlist_flags(stat=stat, online=online, algorithm=algorithm, flags=flags)
        return self._scandir_parts(path, None, algorithm=algorithm, flags=chosen)

    def _scandir_parts(
        self,
        path: str,
        on_batch: Callable[[list[DirEntry]], None] | None,
        *,
        stat: bool = True,
        algorithm: str = "",
        flags: DirListFlags | int | str | None = None,
    ) -> list[DirEntry]:
        """:meth:`scandir`, calling ``on_batch`` with each part as it arrives.

        A server answers a big directory in several ``kXR_oksofar``
        instalments; each is parsed and handed to ``on_batch`` as it comes
        in - what XrdCl's ``DirListFlags::Chunked`` delivers - and the whole
        listing is still returned at the end.

        ``flags``, when given, is the listing's options already resolved
        (:meth:`scandir` folds ``online`` into it); without it ``stat`` and
        ``algorithm`` choose them, which is how the pyxrootd compat layer
        asks. ``algorithm`` is still needed beside resolved flags: it names
        the digest in the ``cks.type`` CGI, which no flag bit carries.
        """
        target = self._abs(path or "/")
        options = dirlist_flags(stat=stat, algorithm=algorithm, flags=flags)
        if algorithm:
            target += ("&" if "?" in target else "?") + f"cks.type={algorithm}"
        with_stat = bool(options & DirListFlags.STAT)
        request = r.Dirlist(target, int(options) & ~int(DirListFlags.RECURSIVE))
        if on_batch is None:
            res = self._router.execute(request, path=target)
        else:
            batches = _Batches(target, with_stat, on_batch)
            res = self._router.execute(request, path=target, on_chunk=batches)
        return rp.parse_dirlist(res.data, target, with_stat=with_stat)

    def dirlist(
        self,
        path: str = "",
        *,
        stat: bool = True,
        online: bool = False,
        flags: DirListFlags | int | str | None = None,
    ) -> bytes:
        """The ``kXR_dirlist`` answer as the server sent it, every part joined.

        :meth:`scandir` is this, parsed - and parsing is most of what a long
        listing costs, because every entry becomes a
        :class:`~xrdclient.types.StatInfo`. A caller that wants three fields
        of each, or the names only, parses the bytes itself and pays for
        what it reads. The request goes out exactly as :meth:`scandir`'s
        does: redirects followed, the filesystem's CGI on the path.

        The format is the protocol's: one name per line; with ``stat``
        (``kXR_dstat``) a server that honours it starts with a ``.`` line and
        its stat line, then sends name and stat line in turn
        (``"<id> <size> <flags> <mtime> ..."``). A server that ignores the
        flag sends names alone, so the leading ``.`` - not the flag that was
        sent - says which form arrived. The body may end in a NUL.
        """
        target = self._abs(path or "/")
        options = dirlist_flags(stat=stat, online=online, flags=flags)
        request = r.Dirlist(target, int(options) & ~int(DirListFlags.RECURSIVE))
        return bytes(self._router.execute(request, path=target).data)

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
            self._router.execute(r.Mkdir(target, permissions(mode), mkpath=parents), path=target)
        except FileExistsError:
            # ``exist_ok`` forgives an existing *directory*, as pathlib does;
            # a file squatting on the name is still an error.
            if not exist_ok or not self.isdir(path):
                raise

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
        answers :class:`~xrdclient.errors.UnsupportedError`, which is the honest
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
        """``kXR_chmod``: set the nine permission bits (``0o755``, ``"755"``, ``"rwxr-xr-x"``)."""
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
        request = r.Setattr(target, c.kXR_sa_owner, owner=(int(uid), int(gid)))
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
        lent = self._router.lend()
        fh = File(self._url_for(path), self.config, router=lent)
        try:
            fh.open(flags=flags, mode=Access.OWNER_READ | Access.OWNER_WRITE)
        except BaseException as exc:
            # The lent router shares this filesystem's connection until it
            # lets go, and a failed open leaves it to the caller to let go.
            lent.close()
            if exist_ok and isinstance(exc, ExistsError):
                return
            raise
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
        this filesystem's own :class:`~xrdclient.config.Config`.

        A name the server has no value for is absent from the result, the
        way a missing key is absent from a :class:`dict`. Splitting on
        ``\\n`` rather than by lines keeps the remaining names lined up with
        their values when an earlier one comes back empty.
        """
        wanted = list(names) or ["version"]
        res = self._router.execute(r.Query(c.kXR_Qconfig, "\n".join(wanted)))
        body = res.data.split(b"\x00", 1)[0].decode("utf-8", "replace")
        values = body.split("\n")
        return {name: value for name, value in zip(wanted, values, strict=False) if value}

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
        return self._deep_locate(path, create=create)

    def _deep_locate(
        self, path: str, *, create: bool = False, flags: LocateFlags | int | None = None
    ) -> list[LocationInfo]:
        """:meth:`deep_locate`, with :meth:`locate`'s ``flags`` on every locate it makes."""
        seen: dict[str, LocationInfo] = {}
        pending = list(self.locate(path, create=create, flags=flags))
        while pending:
            loc = pending.pop(0)  # in the order the servers were named, as XrdCl lists them
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
                    pending.extend(child.locate(path, create=create, flags=flags))
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
        cancel: bool = False,
        reqid: str | None = None,
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

        ``cancel=True`` withdraws the request ``reqid`` - the handle an
        earlier stage returned - for ``paths``, or for all of it when
        ``paths`` is empty: ``kXR_cancel``, with the request id leading the
        path list, as XrdCl sends it. The staging words do not apply to a
        cancel and are ignored; ``flags`` is added to it, for a server that
        reads more. A request id is only ever the name of a request to
        cancel, so one without ``cancel=True`` - or a cancel without one -
        is refused with :class:`ValueError` before anything is sent.
        """
        if cancel or reqid is not None:
            return self._cancel(paths, reqid, priority, flags, cancel=cancel)
        targets = [self._abs(p) for p in paths]
        options = prepare_flags(stage=stage, evict=evict, notify=notify, fresh=fresh, flags=flags)
        # The options byte, then the extended half-word above it: see
        # PrepareFlags, where EVICT is the one that lives up there.
        request = r.Prepare(targets, int(options) & 0xFF, priority, extended=int(options) >> 8)
        res = self._router.execute(request)
        return res.data.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()

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
        one :class:`~xrdclient.types.PrepareStatus` per path, in the order asked,
        each of which is true once its file is online.

        The handle is the one :meth:`prepare` returned; a server that has
        never heard of it says so rather than reporting the files as absent.
        """
        targets = [self._abs(p) for p in paths]
        args = "\n".join([handle, *targets])
        res = self._router.execute(r.Query(c.kXR_QPrep, args), path=targets[0] if targets else "")
        return rp.parse_prepare_status(res.data)

    def _cancel(
        self,
        paths: Sequence[str],
        reqid: str | None,
        priority: int,
        flags: PrepareFlags | int | str | None,
        *,
        cancel: bool,
    ) -> str:
        """``prepare(cancel=True, reqid=...)``: see :meth:`prepare`."""
        if not cancel:
            raise ValueError("reqid names a request to cancel; pass cancel=True with it")
        if not reqid:
            raise ValueError("prepare(cancel=True) needs the reqid of the request to cancel")
        options = PrepareFlags.CANCEL | (PrepareFlags(flags) if flags is not None else 0)
        # The id goes on the wire as it is, never resolved as a path: it
        # names a request, and the server reads the first entry as one.
        entries = [reqid, *(self._abs(p) for p in paths)]
        request = r.Prepare(entries, int(options) & 0xFF, priority, extended=int(options) >> 8)
        res = self._router.execute(request)
        return res.data.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()

    def cancel_prepare(self, handle: str, paths: Sequence[str] = ()) -> None:
        """Withdraw a staging request :meth:`prepare` made, or ``paths`` of it.

        The handle leads the path list - cancelling names the request first
        and the files second - which is why it is a separate argument rather
        than one more path: passed as a path it would be resolved against the
        filesystem's directory and lose its meaning. With no ``paths`` the
        whole request goes. ``prepare(paths, cancel=True, reqid=handle)`` is
        the same request.
        """
        self.prepare(paths, cancel=True, reqid=handle)

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
        raise _fattr.missing(name, target)

    def setxattr(self, path: str, name: str, value: bytes, *, create_only: bool = False) -> None:
        """Set one attribute (``kXR_fattr`` set), raising if the server refused it.

        ``create_only`` sends ``kXR_fattrIsNew``, so an existing attribute is an
        error instead of being overwritten - the ``XATTR_CREATE`` of ``setxattr(2)``.
        """
        target = self._abs(path)
        res = self._router.execute(
            r.Fattr.set(target, name, value, create_only=create_only), path=target
        )
        _fattr.check(rp.parse_fattr(res.data, values=False), target)

    def removexattr(self, path: str, name: str) -> None:
        """Remove one attribute (``kXR_fattr`` del), raising if refused - an absent one, say."""
        target = self._abs(path)
        res = self._router.execute(r.Fattr.delete(target, name), path=target)
        _fattr.check(rp.parse_fattr(res.data, values=False), target)

    def listxattr(self, path: str) -> list[str]:
        """Every attribute name on ``path`` (``kXR_fattr`` list), in the server's order."""
        target = self._abs(path)
        res = self._router.execute(r.Fattr.list(target), path=target)
        return [item.name for item in rp.parse_fattr_list(res.data).items]

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
        return rp.parse_fattr_list(res.data, values=True).as_dict()

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
        or ``str`` statically should reach for :func:`xrdclient.open`, whose
        overloads know the difference.
        """
        from ..io import open_url

        lent = self._router.lend()
        try:
            return open_url(
                self._url_for(path),
                mode,
                buffering=buffering,
                encoding=encoding,
                errors=errors,
                newline=newline,
                config=self.config,
                router=lent,
                posc=posc,
            )
        except BaseException:
            # A failed open leaves the lent router, and its share of this
            # filesystem's connection, to be let go of here.
            lent.close()
            raise
