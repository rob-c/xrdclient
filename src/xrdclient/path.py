"""``pathlib`` semantics for remote paths.

:class:`XRootDPath` is the layer most code should reach for. It composes with
``/``, compares and hashes by URL, and carries the same method names
:class:`pathlib.Path` does, so code written against local paths ports by
changing one constructor:

    >>> from xrdclient import XRootDPath
    >>> base = XRootDPath("root://eos.example.org//store/user/me")
    >>> for run in (base / "runs").iterdir():
    ...     if run.suffix == ".root":
    ...         print(run.name, run.stat().st_size)

It deliberately does not subclass :class:`pathlib.Path`: that class assumes
a local filesystem in ways that change between Python releases, and
inheriting from it would trade a stable surface for a fragile one.

**Where it sits.** The pure-path half (``name``, ``parent``, ``/``, ...) is
string work on the :class:`~xrdclient.url.XRootDURL` and never touches the
network. The concrete half forwards each call to a
:class:`~xrdclient.client.FileSystem` for the endpoint, so the scheme decides
the protocol (``root://``, ``davs://``, ...) and the errors are that
filesystem's - :class:`~xrdclient.errors.NotFoundError` is also a
:class:`FileNotFoundError`, so ``pathlib``-style ``except`` clauses work.

**What not to break.** Every path derived from another (``/``, ``parent``,
``iterdir``, ``glob``, ...) shares its :class:`FileSystem`, so a traversal is
one connection rather than one per entry; :meth:`XRootDPath._derive` is the
one place that wiring happens. Equality and hashing are by endpoint and path,
so paths work as dict keys and set members across separately built objects.
"""

from __future__ import annotations

import dataclasses
import posixpath
from collections.abc import Iterator, Sequence
from typing import IO, TYPE_CHECKING, Any

from .config import Config
from .types import StatInfo
from .url import XRootDURL, parse

if TYPE_CHECKING:
    from .client.filesystem import FileSystem

__all__ = ["XRootDPath"]


class XRootDPath:
    """A path on a remote storage element, with :class:`pathlib.Path`'s names.

    Cheap to build and immutable in what it names; the only mutable state is
    the lazily made :attr:`fs`, which derived paths share. Not thread-safe in
    the first-use race on :attr:`fs` (two threads may each build one; the
    loser's is simply unused), but the :class:`FileSystem` itself is, so one
    path may be used from many threads once it has connected.
    """

    __slots__ = ("_config", "_fs", "_url")

    _url: XRootDURL
    _config: Config
    #: Built on first use, or inherited from the path this one was derived
    #: from, so a traversal is one connection rather than one per component.
    _fs: FileSystem | None

    def __init__(self, url: str | XRootDURL | XRootDPath, config: Config | None = None) -> None:
        """Name ``url``; no connection is made until a concrete method needs one.

        A string is parsed with :func:`xrdclient.url.parse`, so a bare local
        path becomes a ``file://`` URL. Copying another :class:`XRootDPath`
        keeps its connection unless a different ``config`` is given - a
        different config may mean different credentials or timeouts, which
        the existing connection was not made with.
        """
        self._fs = None
        if isinstance(url, XRootDPath):
            self._url = url._url
            self._config = config or url._config
            if config is None or config is url._config:
                self._fs = url.fs  # a copy talks over the original's connection
        else:
            self._url = parse(url) if isinstance(url, str) else url
            self._config = config or Config()

    # ------------------------------------------------------------------
    # Pure-path surface
    # ------------------------------------------------------------------

    @property
    def url(self) -> XRootDURL:
        """The parsed URL this path names, query string included."""
        return self._url

    @property
    def name(self) -> str:
        """The last component; a trailing slash is ignored, as :mod:`pathlib` does."""
        return posixpath.basename(self._url.path.rstrip("/"))

    @property
    def stem(self) -> str:
        """:attr:`name` without its final suffix."""
        return posixpath.splitext(self.name)[0]

    @property
    def suffix(self) -> str:
        """The final ``.ext`` of :attr:`name`, or ``""``; a leading-dot name has none."""
        return posixpath.splitext(self.name)[1]

    @property
    def suffixes(self) -> list[str]:
        """Every ``.ext`` of :attr:`name` (``[".tar", ".gz"]``); a leading dot is not one."""
        parts = self.name.lstrip(".").split(".")
        return ["." + p for p in parts[1:]]

    @property
    def parts(self) -> tuple[str, ...]:
        """The server-side path's components, without the root or the endpoint.

        Unlike :attr:`pathlib.PurePath.parts` there is no leading ``"/"``
        element: the anchor is a URL (:attr:`anchor`), not a path component.
        """
        return tuple(p for p in self._url.path.split("/") if p)

    @property
    def parent(self) -> XRootDPath:
        """The containing directory, on the same connection; the root is its own parent."""
        return self._derive(posixpath.dirname(self._url.path.rstrip("/")) or "/")

    @property
    def parents(self) -> tuple[XRootDPath, ...]:
        """Every ancestor, nearest first, ending at the endpoint's root."""
        out = []
        node = self
        while node._url.path not in ("/", ""):
            node = node.parent
            out.append(node)
        return tuple(out)

    @property
    def anchor(self) -> str:
        """The endpoint's root as a URL string - the part no ``..`` can climb above."""
        return str(self._url.with_path("/"))

    def is_absolute(self) -> bool:
        """True for any URL from :func:`~xrdclient.url.parse`, which roots every path at ``/``."""
        return self._url.path.startswith("/")

    def with_name(self, name: str) -> XRootDPath:
        """A sibling called ``name``, on the same connection."""
        return self._derive(posixpath.join(posixpath.dirname(self._url.path.rstrip("/")), name))

    def with_suffix(self, suffix: str) -> XRootDPath:
        """This path with its final suffix replaced (``""`` removes it)."""
        return self.with_name(self.stem + suffix)

    def with_stem(self, stem: str) -> XRootDPath:
        """This path with :attr:`stem` replaced and the suffix kept."""
        return self.with_name(stem + self.suffix)

    def joinpath(self, *parts: str) -> XRootDPath:
        """Append ``parts``; an absolute part restarts from the endpoint's root.

        That is :func:`posixpath.join`'s rule and :mod:`pathlib`'s, and it
        never leaves the endpoint: no part can name another server.
        """
        return self._derive(posixpath.join(self._url.path, *parts))

    def relative_to(self, other: str | XRootDPath, *, walk_up: bool = False) -> str:
        """This path below ``other``, as a relative string.

        :meth:`pathlib.PurePath.relative_to` semantics: ``other`` must be an
        ancestor (or this path itself) on the same endpoint, or this raises
        :class:`ValueError` rather than answering with ``..`` components that
        would silently point somewhere else. ``walk_up=True`` is 3.12's opt-in
        to the ``..`` form; this class is not a ``pathlib`` subclass, so it
        offers it on every interpreter. ``other`` may be another path, a URL,
        or a bare path taken to be on this path's endpoint.
        """
        base = other._url if isinstance(other, XRootDPath) else self._base_url(str(other))
        if base.endpoint != self._url.endpoint:
            raise ValueError(f"{str(self)!r} and {str(base)!r} are on a different endpoint")
        # A relative base is refused outright: relpath would resolve it
        # against this process's working directory, which means nothing here.
        absolute = base.path.startswith("/")
        relative = posixpath.relpath(self._url.path, base.path) if absolute else ""
        climbs = relative == ".." or relative.startswith("../")
        if relative and (walk_up or not climbs):
            return relative
        raise ValueError(f"{self._url.path!r} is not in the subpath of {base.path!r}")

    def _base_url(self, other: str) -> XRootDURL:
        """``other`` as a URL, a bare path being one on this path's endpoint.

        The bare path is not normalised: a relative one must stay relative so
        that it is refused, as :mod:`pathlib` refuses mixing the two.
        """
        if "://" in other:
            return parse(other)
        return dataclasses.replace(self._url, path=other)

    def _derive(self, path: str) -> XRootDPath:
        """Another path on the same endpoint, sharing this one's connection.

        Sharing matters: ``for entry in base.iterdir(): entry.stat()`` would
        otherwise open one connection per entry.
        """
        child = XRootDPath(self._url.with_path(path), self._config)
        child._fs = self.fs
        return child

    def __truediv__(self, other: str) -> XRootDPath:
        """``path / "sub"``, as :meth:`joinpath`."""
        return self.joinpath(other)

    def __rtruediv__(self, other: str) -> XRootDPath:
        """``"/prefix" / path``: ``other`` is a path on this endpoint, put in front.

        The result is built afresh rather than derived, so it opens its own
        connection on first use instead of sharing this path's.
        """
        return XRootDPath(self._url.with_path(other), self._config) / self._url.path.lstrip("/")

    def __str__(self) -> str:
        """The full URL, as :class:`~xrdclient.url.XRootDURL` renders it."""
        return str(self._url)

    def __repr__(self) -> str:
        """``XRootDPath('root://...')``: the full URL, which round-trips through the constructor."""
        return f"XRootDPath({str(self._url)!r})"

    def __fspath__(self) -> str:
        """The URL string, so APIs that accept :class:`os.PathLike` take a remote path.

        Local-only functions such as :func:`open` will then fail on the URL
        rather than silently opening a local file of a similar name.
        """
        return str(self._url)

    def __eq__(self, other: object) -> bool:
        """Equal when the URLs are: scheme, host, port, path, user and query alike.

        A plain string never compares equal, as a :class:`pathlib.Path` never
        equals a ``str``.
        """
        return isinstance(other, XRootDPath) and self._url == other._url

    def __hash__(self) -> int:
        """Hash by endpoint and path only: coarser than :meth:`__eq__`, so still consistent.

        :class:`~xrdclient.url.XRootDURL` cannot be hashed itself - its
        ``query`` is a ``dict``.
        """
        return hash((self._url.endpoint, self._url.path))

    def __lt__(self, other: XRootDPath) -> bool:
        """Order by endpoint, then path, so ``sorted()`` groups paths by server."""
        return (self._url.endpoint, self._url.path) < (other._url.endpoint, other._url.path)

    # ------------------------------------------------------------------
    # Concrete-path surface
    # ------------------------------------------------------------------

    @property
    def fs(self) -> FileSystem:
        """The :class:`~xrdclient.client.FileSystem` backing this path.

        Constructing one costs nothing - the connection is opened on first
        use - and derived paths share it, so a whole traversal runs over a
        single connection.
        """
        if self._fs is None:
            from .client.filesystem import FileSystem

            self._fs = FileSystem(self._url.with_path("/"), self._config)
        return self._fs

    def stat(self, *, follow_symlinks: bool = True) -> StatInfo:
        """Metadata for this path; :class:`FileNotFoundError` if it is absent."""
        return self.fs.stat(self._url.path, follow_symlinks=follow_symlinks)

    def lstat(self) -> StatInfo:
        """``pathlib``'s ``lstat``: the link itself, not what it points at."""
        return self.fs.lstat(self._url.path)

    def is_symlink(self) -> bool:
        """Whether this path is a symbolic link, as :meth:`FileSystem.is_symlink` decides."""
        return self.fs.is_symlink(self._url.path)

    def exists(self) -> bool:
        """``True`` if the path resolves. Absence is ``False``; other errors still raise."""
        return self.fs.exists(self._url.path)

    def is_dir(self) -> bool:
        """``True`` for an existing directory; ``False`` if absent, as :mod:`pathlib` does."""
        return self.fs.isdir(self._url.path)

    def is_file(self) -> bool:
        """``True`` for an existing regular file; ``False`` if absent."""
        return self.fs.isfile(self._url.path)

    def iterdir(self) -> Iterator[XRootDPath]:
        """The directory's entries as paths, in the server's order.

        The whole listing is fetched when iteration starts (``kXR_dirlist``
        has no cursor), so this is lazy only in building the path objects.
        """
        for entry in self.fs.scandir(self._url.path):
            yield self / entry.name

    def glob(self, pattern: str) -> Iterator[XRootDPath]:
        """Paths under this one matching ``pattern``, :meth:`pathlib.Path.glob` style.

        Only the directories the pattern can reach are listed; see
        :meth:`FileSystem.glob` for the matching rules.
        """
        for found in self.fs.glob(pattern, root=self._url.path):
            yield self._derive(found)

    def rglob(self, pattern: str) -> Iterator[XRootDPath]:
        """:meth:`glob` at every depth: ``pattern`` prefixed with ``**/``."""
        yield from self.glob(f"**/{pattern}")

    def walk(self, *, top_down: bool = True) -> Iterator[tuple[XRootDPath, list[str], list[str]]]:
        """:meth:`pathlib.Path.walk` (3.12) on every interpreter: ``(dir, dirs, files)``.

        As with :func:`os.walk`, pruning ``dirs`` in place when ``top_down``
        stops the walk descending there - and so stops those listings being
        requested at all.
        """
        for root, dirs, files in self.fs.walk(self._url.path, topdown=top_down):
            yield self._derive(root), dirs, files

    def mkdir(self, mode: int | str = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
        """:meth:`pathlib.Path.mkdir`; ``mode`` may be octal digits as a string."""
        self.fs.mkdir(self._url.path, mode, parents=parents, exist_ok=exist_ok)

    def rmdir(self) -> None:
        """Remove this directory, which must be empty."""
        self.fs.rmdir(self._url.path)

    def unlink(self, missing_ok: bool = False) -> None:
        """Remove this file; with ``missing_ok``, an absent one is not an error.

        Absence is caught after the fact rather than checked first: a stat
        then a remove would be two round trips and still race another client.
        """
        from .errors import NotFoundError

        try:
            self.fs.remove(self._url.path)
        except NotFoundError:
            if not missing_ok:
                raise

    def rename(self, target: str | XRootDPath) -> XRootDPath:
        """Rename on the server and return the new path, as :mod:`pathlib` does.

        ``target`` is a path on this endpoint; an :class:`XRootDPath` target
        contributes only its path, because a server-side rename (``kXR_mv``,
        WebDAV ``MOVE``) cannot cross endpoints - use :func:`xrdclient.copy`
        for that.
        """
        destination = target._url.path if isinstance(target, XRootDPath) else str(target)
        self.fs.rename(self._url.path, destination)
        return self._derive(destination)

    #: ``pathlib`` distinguishes these two only in overwrite semantics, which
    #: the server decides.
    replace = rename

    def chmod(self, mode: int | str) -> None:
        """Change the permission bits; ``mode`` may be octal digits as a string."""
        self.fs.chmod(self._url.path, mode)

    def touch(self, mode: int | str = 0o644, exist_ok: bool = True) -> None:
        """Create an empty file if absent, as :meth:`FileSystem.touch` does.

        ``mode`` is accepted for :mod:`pathlib` compatibility and ignored: the
        create is an open with ``kXR_new``, whose mode the filesystem fixes.
        An existing file's mtime is left alone - XProtocol has no request that
        moves it.
        """
        self.fs.touch(self._url.path, exist_ok=exist_ok)

    def open(self, mode: str = "rb", **kwargs: Any) -> IO[Any]:
        """:func:`open` on this path; the file borrows this path's connection.

        The caller owns the returned handle and must close it (a ``with``
        block does). Over ``root://`` the file holds its share of the
        connection, so closing this path first does not cut it off.
        """
        return self.fs.open(self._url.path, mode, **kwargs)

    def read_bytes(self) -> bytes:
        """The whole file, in one open-read-close."""
        return self.fs.read_bytes(self._url.path)

    def write_bytes(self, data: bytes) -> int:
        """Replace the file's contents with ``data``; returns the byte count."""
        return self.fs.write_bytes(self._url.path, data)

    def read_text(self, encoding: str = "utf-8") -> str:
        """The whole file, decoded. UTF-8 by default, not the locale's encoding."""
        return self.fs.read_text(self._url.path, encoding)

    def write_text(self, text: str, encoding: str = "utf-8") -> int:
        """Replace the file's contents with ``text``; returns the character count."""
        return self.fs.write_text(self._url.path, text, encoding)

    def checksum(self, algorithm: str | None = None) -> Any:
        """The server-computed checksum; the server's default algorithm if none is named."""
        return self.fs.checksum(self._url.path, algorithm)

    def locate(self) -> Sequence[Any]:
        """Which servers hold this path (``kXR_locate``), as :meth:`FileSystem.locate`."""
        return self.fs.locate(self._url.path)

    def close(self) -> None:
        """Release the connection this path has been using.

        Paths derived from this one share that connection, so this closes
        theirs too; using any of them afterwards simply reconnects.
        """
        if self._fs is not None:
            self._fs.close()
            self._fs = None

    def __enter__(self) -> XRootDPath:
        """Use as ``with XRootDPath(url) as p:`` to :meth:`close` the connection after."""
        return self

    def __exit__(self, *exc: object) -> None:
        """:meth:`close`, whether or not the block raised; exceptions propagate."""
        self.close()
