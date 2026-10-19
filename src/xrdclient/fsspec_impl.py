"""fsspec bindings: ``root://`` and ``davs://`` for pandas, dask, and pyarrow.

    >>> import fsspec
    >>> with fsspec.open("root://eos.example.org//store/f.root", "rb") as fh:
    ...     header = fh.read(1024)
    >>> import pandas as pd
    >>> pd.read_parquet("root://eos.example.org//store/t.parquet")

Registered through the ``fsspec.specs`` entry points for ``root``, ``roots``
and ``xroot`` (and ``dav``, ``davs``, ``webdav`` for
:class:`HTTPXRootDFileSystem`), so nothing has to be imported by hand. This
package does not depend on ``fsspec``: the module is loaded by ``fsspec``
itself, wherever it is installed, and the rest of the package never imports
it. ``https://`` is left to fsspec's own HTTP filesystem, which owns it.
Entry points do not settle a protocol another package also claims
(``fsspec-xrootd`` registers ``root``); :func:`register_fsspec` does, for
the rest of the process, and is what a program that must be sure calls.

How it maps onto fsspec: every public method resolves its path with
:meth:`XRootDFileSystem._target` to one of the library's own
:class:`~xrdclient.FileSystem` objects and a path on it, then makes one call.
Paths are named as fsspec-xrootd names them - bare ``/store/...`` on the
instance's endpoint - so code written for that package keeps working; a
fully-qualified URL to another server is honoured rather than silently
re-rooted on this one. ``_open`` returns the library's real :mod:`io` file
objects, not an ``AbstractBufferedFile``; fsspec's named caches are
available on request through :class:`_CachedReader`.

What must not break: fsspec caches instances by constructor arguments
(see :meth:`XRootDFileSystem._get_kwargs_from_urls`), so an instance is
shared by every caller in the process and must stay thread-safe and must
never carry per-call state.
"""

from __future__ import annotations

import io
import posixpath
import threading
from importlib import import_module, metadata
from typing import IO, Any

from fsspec import register_implementation
from fsspec.caching import BaseCache, caches
from fsspec.spec import AbstractFileSystem
from fsspec.utils import stringify_path

from . import _abandoned
from .client import FileSystem
from .config import Config
from .types import StatInfo
from .url import XRootDURL, parse

__all__ = [
    "HTTPXRootDFileSystem",
    "S3XRootDFileSystem",
    "XRootDFileSystem",
    "register_fsspec",
]


class _ClassOrInstance:
    """One name that means one method on the class and another on an instance.

    fsspec calls ``_strip_protocol`` and ``_parent`` both ways. On the class
    - which is how it builds the path it hands the instance it made for a URL
    - no endpoint is known, and the path alone is the answer. An instance
    knows its own endpoint, and it alone can tell that a URL names some
    *other* server, which a bare path would silently send back to its own.
    """

    def __init__(self, on_class: str, on_instance: str) -> None:
        """Name the attribute to use on the class and the one on an instance."""
        self.on_class = on_class
        self.on_instance = on_instance

    def __get__(self, instance: object, owner: type) -> Any:
        """The class's method when looked up on the class, else the instance's."""
        if instance is None:
            return getattr(owner, self.on_class)
        return getattr(instance, self.on_instance)


def _trimmed(path: str) -> str:
    """``path`` without the trailing slash fsspec's comparisons assume gone."""
    return path.rstrip("/") or "/"


def _span(start: int | None, end: int | None, size: int) -> tuple[int, int]:
    """The offset and length of the slice ``[start:end]`` of ``size`` bytes.

    fsspec defines ``cat_file``'s bounds as a Python slice's: ``None`` is the
    matching end of the file, a negative bound counts back from the end, and
    either is clamped to the file, so a range that ends before it starts is
    simply empty.
    """
    offset, stop, _step = slice(start, end).indices(size)
    return offset, max(0, stop - offset)


def _read_span(handle: IO[bytes], start: int | None, end: int | None) -> bytes:
    """``[start:end]`` of an open file, in as many reads as the file needs.

    With no ``end`` the read runs to whatever end the file has now, rather
    than to the size the open reported, which a file still growing outruns.
    """
    offset, length = _span(start, end, handle.seek(0, io.SEEK_END))
    handle.seek(offset)
    if end is None:
        return handle.read()
    chunks: list[bytes] = []
    while length > 0:
        # An unbuffered read may return less than was asked for.
        chunk = handle.read(length)
        if not chunk:
            break
        chunks.append(chunk)
        length -= len(chunk)
    return b"".join(chunks)


class _Held:
    """The filesystems an :class:`XRootDFileSystem` holds, apart from it.

    So that its :func:`weakref.finalize` can reach them without keeping the
    fsspec object alive.
    """

    __slots__ = ("elsewhere", "own")

    def __init__(self, own: FileSystem | None) -> None:
        """Hold ``own`` (the instance's endpoint, if it has one) and nothing else yet."""
        self.own = own
        self.elsewhere: dict[str, FileSystem] = {}

    def take(self) -> list[FileSystem]:
        """Every filesystem held, which are no longer held once taken."""
        taken = [] if self.own is None else [self.own]
        taken += self.elsewhere.values()
        self.own = None
        self.elsewhere.clear()
        return taken


def _dropped(held: _Held) -> None:
    """An :class:`XRootDFileSystem` was collected unclosed: queue what it held.

    Runs inside the garbage collector, so it only queues; the closes happen
    on an ordinary thread.
    """
    for filesystem in held.take():
        _abandoned.adopt(filesystem)


class XRootDFileSystem(AbstractFileSystem):
    """An :class:`fsspec.AbstractFileSystem` over one XRootD or HTTP endpoint.

        >>> fs = XRootDFileSystem("root://eos.example.org")
        >>> fs.ls("/store", detail=False)
        ['/store/a.root', '/store/b.root']

    One instance is one endpoint. ``fsspec`` caches instances by their
    constructor arguments, so repeated ``fsspec.open`` calls against the same
    server share this object - and therefore share its connection.

    One that is dropped without :meth:`close` lets its connections go as it
    is collected - but not from inside the garbage collector, where a close
    could wait on the session pool's lock while the very thread collecting
    holds it. The filesystems it held are queued instead, and closed on an
    ordinary thread (see :mod:`xrdclient._abandoned`).
    """

    protocol: tuple[str, ...] = ("root", "roots", "xroot")
    root_marker = "/"
    sep = "/"

    def __init__(
        self,
        endpoint: str = "",
        *,
        config: Config | None = None,
        **kwargs: Any,
    ) -> None:
        """A filesystem on ``endpoint`` (``root://host:port``); ``""`` for URLs only.

        With no endpoint every path must be a full URL. ``kwargs`` are
        fsspec's own (``skip_instance_cache``, ``use_listings_cache``, ...).
        Nothing is connected yet: the first call that needs the server does
        that, so constructing one - which fsspec does just to strip a path -
        costs no round trip.
        """
        super().__init__(**kwargs)
        self.config = config or Config()
        self.endpoint = endpoint
        #: What this object must let go of, in the one object its finalizer
        #: holds - so the finalizer does not keep this object alive.
        self._held = _Held(
            FileSystem(parse(endpoint).with_path("/"), self.config) if endpoint else None
        )
        _abandoned.on_collect(self, _dropped, self._held)
        _abandoned.ensure_reaper()

    @property
    def _fs(self) -> FileSystem | None:
        """The filesystem of this object's own endpoint, until :meth:`close`."""
        return self._held.own

    @property
    def _elsewhere(self) -> dict[str, FileSystem]:
        """Endpoints reached through a fully-qualified path, kept so they are
        opened once and closed with this object rather than leaked."""
        return self._held.elsewhere

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    @classmethod
    def _bare_path(cls, path: Any) -> Any:
        """The path alone, as the class - which knows no endpoint - sees it."""
        if isinstance(path, list):
            return [cls._bare_path(one) for one in path]
        text = stringify_path(path)
        if "://" in text:
            return _trimmed(parse(text).path)
        return _trimmed("/" + text.lstrip("/"))

    def _local_name(self, path: Any) -> Any:
        """The name fsspec should carry for ``path`` between calls.

        A bare path on this instance's own endpoint, as fsspec-xrootd names
        them; the whole URL for anywhere else, so that the name finds its way
        back to the server it came from.
        """
        if isinstance(path, list):
            return [self._local_name(one) for one in path]
        url = self._foreign(path)
        if url is None:
            return self._bare_path(path)
        return str(url.with_path(_trimmed(url.path)))

    @classmethod
    def _bare_parent(cls, path: Any) -> str:
        """The parent of ``path`` as the class sees it: a bare path."""
        return posixpath.dirname(str(cls._bare_path(path)))

    def _local_parent(self, path: Any) -> str:
        """The parent of ``path``, still a whole URL when ``path`` was a foreign one."""
        url = self._foreign(path)
        if url is None:
            return self._bare_parent(path)
        return str(url.with_path(posixpath.dirname(_trimmed(url.path))))

    # fsspec's two hooks for turning names into paths; see _ClassOrInstance
    # for why each has a class meaning and an instance meaning.
    _strip_protocol = _ClassOrInstance("_bare_path", "_local_name")
    _parent = _ClassOrInstance("_bare_parent", "_local_parent")

    def unstrip_protocol(self, name: str) -> str:
        """``name`` as a URL any fsspec call could be handed on its own."""
        if "://" in name or not self.endpoint:
            return str(super().unstrip_protocol(name))
        return str(parse(self.endpoint).with_path(name))

    @staticmethod
    def _get_kwargs_from_urls(path: str) -> dict[str, str]:
        """What the instance cache keys on: the endpoint the URL names."""
        url = parse(path)
        return {"endpoint": str(url.with_path("/"))} if url.host else {}

    def _foreign(self, path: Any) -> XRootDURL | None:
        """The URL ``path`` spells, if it names a server other than this one's."""
        text = stringify_path(path)
        if "://" not in text:
            return None
        url = parse(text)
        # Compared by netloc (host and port only), so the same server spelled
        # root:// and roots:// still counts as this one.
        if url.host and (self._fs is None or url.netloc != parse(self.endpoint).netloc):
            return url
        return None

    def _target(self, path: str) -> tuple[FileSystem, str]:
        """The filesystem to use for ``path``, and the path within it."""
        url = self._foreign(path)
        if url is not None:
            # A fully-qualified path to somewhere else: honour it rather than
            # silently reading the wrong server.
            key = str(url.with_path("/"))
            found = self._elsewhere.get(key)
            if found is None:
                found = self._elsewhere[key] = FileSystem(url.with_path("/"), self.config)
            return found, url.path or "/"
        if self._fs is None:
            raise ValueError("no endpoint: give a full URL or construct with endpoint=")
        return self._fs, str(self._bare_path(path))

    def _named(self, path: str, target: str) -> str:
        """``target`` named the way ``path`` was: on its endpoint, if foreign."""
        url = self._foreign(path)
        return target if url is None else str(url.with_path(target))

    def _url_of(self, path: str) -> XRootDURL:
        """``path`` as a whole URL, for the calls that take no filesystem."""
        url = self._foreign(path)
        if url is not None:
            return url
        return parse(self.endpoint).with_path(str(self._bare_path(path)))

    def invalidate_cache(self, path: str | None = None) -> None:
        """Forget every cached listing, whatever ``path`` is.

        fsspec allows clearing only the listings under ``path``; clearing all
        of them is never wrong - a cache may always forget more than asked -
        and costs at most a re-listing.
        """
        self.dircache.clear()

    def close(self) -> None:
        """Release every connection this object opened. Safe to call twice."""
        for filesystem in self._held.take():
            filesystem.close()

    # ------------------------------------------------------------------
    # Namespace
    # ------------------------------------------------------------------

    def _info_of(self, info: StatInfo, path: str) -> dict[str, Any]:
        """An fsspec ``info`` dict: ``name``, ``size`` and ``type`` are the required keys.

        ``type`` is fsspec's ``"directory"``/``"file"`` vocabulary; ``mtime``
        and ``mode`` are extras, which pyarrow's fsspec handler reads for
        ``mtime`` when present.
        """
        return {
            "name": path,
            "size": info.st_size,
            "type": "directory" if info.is_dir() else "file",
            "mtime": info.st_mtime,
            "mode": info.st_mode,
        }

    def info(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """One stat, as fsspec's ``info`` dict; a missing path raises ``FileNotFoundError``.

        Always asks the server rather than consulting the listings cache: a
        stale cached size is exactly what makes a reader stop short.
        """
        filesystem, target = self._target(path)
        return self._info_of(filesystem.stat(target), self._named(path, target))

    def ls(self, path: str, detail: bool = True, **kwargs: Any) -> list[Any]:
        """The entries of a directory, or - fsspec's convention - a file itself."""
        filesystem, target = self._target(path)
        try:
            entries = filesystem.scandir(target)
        except OSError as exc:
            listing = [self._file_listed(path, filesystem, target, exc)]
        else:
            listing = [
                self._info_of(
                    entry.stat or StatInfo(),
                    self._named(path, posixpath.join(target, entry.name)),
                )
                for entry in entries
            ]
            listing.sort(key=lambda item: str(item["name"]))
        if not detail:
            return [str(item["name"]) for item in listing]
        return listing

    def _file_listed(
        self, path: str, filesystem: FileSystem, target: str, failure: OSError
    ) -> dict[str, Any]:
        """``path``'s own entry, once a listing of it has failed.

        A server answers a listing of a file with an error - XRootD's says
        "not found" - so the stat is what tells a file apart from nothing. A
        stat that fails raises its own, truer error; a directory whose listing
        failed re-raises the listing's.
        """
        info = filesystem.stat(target)
        if info.is_dir():
            raise failure
        return self._info_of(info, self._named(path, target))

    # The predicates below are overridden, not inherited, because fsspec's
    # defaults go through info() and an exception; the library answers each
    # in one request of its own.

    def exists(self, path: str, **kwargs: Any) -> bool:
        """Whether ``path`` is there; a refusal other than "not found" still raises.

        fsspec's default swallows every error into ``False``, which would make
        an expired proxy look like an empty directory.
        """
        filesystem, target = self._target(path)
        return bool(filesystem.exists(target))

    def isdir(self, path: str) -> bool:
        """Whether ``path`` is a directory; ``False`` when it does not exist."""
        filesystem, target = self._target(path)
        return bool(filesystem.isdir(target))

    def isfile(self, path: str) -> bool:
        """Whether ``path`` is a regular file; ``False`` when it does not exist."""
        filesystem, target = self._target(path)
        return bool(filesystem.isfile(target))

    def size(self, path: str) -> int:
        """The file's size in bytes, from a fresh stat."""
        return int(self.info(path)["size"])

    def created(self, path: str) -> Any:
        """The server's ``ctime`` as an aware UTC :class:`~datetime.datetime`.

        XRootD reports a change time, not a birth time; fsspec has only the
        one name for it, and this is the closest the protocol has.
        """
        return self._timestamp(self._stat(path).st_ctime)

    def modified(self, path: str) -> Any:
        """The modification time as an aware UTC :class:`~datetime.datetime`."""
        return self._timestamp(self._stat(path).st_mtime)

    def _stat(self, path: str) -> StatInfo:
        """The library's :class:`StatInfo` for ``path``, on whichever endpoint."""
        filesystem, target = self._target(path)
        return filesystem.stat(target)

    @staticmethod
    def _timestamp(seconds: int) -> Any:
        """Epoch seconds as an aware UTC datetime, which is what fsspec returns."""
        # Imported here: nothing else in the module needs datetime.
        from datetime import UTC, datetime

        return datetime.fromtimestamp(seconds, tz=UTC)

    def checksum(self, path: str, algorithm: str | None = None) -> str:
        """The *server's* checksum, not fsspec's synthetic one."""
        filesystem, target = self._target(path)
        return filesystem.checksum(target, algorithm).value

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def mkdir(self, path: str, create_parents: bool = True, **kwargs: Any) -> None:
        """fsspec's ``mkdir``: with ``create_parents`` (the default) also ``exist_ok``.

        fsspec leaves the existing-directory case to the implementation; with
        parents asked for, an existing one is accepted as ``mkdir -p`` does.
        """
        filesystem, target = self._target(path)
        filesystem.mkdir(target, parents=create_parents, exist_ok=create_parents)

    def makedirs(self, path: str, exist_ok: bool = False) -> None:
        """``os.makedirs`` semantics: every missing parent, and ``exist_ok`` as given."""
        filesystem, target = self._target(path)
        filesystem.makedirs(target, exist_ok=exist_ok)

    def rmdir(self, path: str) -> None:
        """Remove an empty directory; a non-empty one is an error, as in ``os.rmdir``."""
        filesystem, target = self._target(path)
        filesystem.rmdir(target)

    def _rm(self, path: str) -> None:
        """fsspec's single-file hook (used by its default ``rm_file``)."""
        filesystem, target = self._target(path)
        filesystem.remove(target)

    def rm(self, path: Any, recursive: bool = False, maxdepth: int | None = None) -> None:
        """Remove one path or a list of them; ``recursive`` deletes whole trees.

        Overridden rather than inherited because fsspec's default expands the
        tree with ``find`` and deletes what it found; the library's
        ``rmtree`` removes symbolic links without following them, so nothing
        outside the tree is touched. ``maxdepth`` is accepted for signature
        compatibility and not honoured.
        """
        for one in [path] if isinstance(path, str) else list(path):
            filesystem, target = self._target(one)
            if recursive and filesystem.isdir(target):
                filesystem.rmtree(target)
            else:
                filesystem.remove(target)
        self.invalidate_cache()

    def mv(
        self,
        path1: str,
        path2: str,
        recursive: bool = False,
        maxdepth: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Rename on one server; copy, verify, and delete between two.

        A rename cannot cross endpoints, and handing the destination's path
        to the source's server would rename the file there instead. The copy
        is checksum-verified before the source goes, as
        :func:`xrdclient.move` does, since a move is the one copy that
        destroys the original.
        """
        filesystem, source = self._target(path1)
        other, destination = self._target(path2)
        if filesystem is other:
            filesystem.rename(source, destination)
        elif filesystem.isdir(source):
            self._move_tree(path1, path2, recursive)
            filesystem.rmtree(source)
        else:
            from .copy import copy

            copy(
                self._url_of(path1),
                self._url_of(path2),
                config=self.config,
                verify=True,
                remove_source=True,
            )
        self.invalidate_cache()

    def _move_tree(self, path1: str, path2: str, recursive: bool) -> None:
        """Copy a directory to another endpoint, as the first half of a move."""
        if not recursive:
            raise IsADirectoryError(f"{path1} is a directory; pass recursive=True to move it")
        from .copy import copy_tree

        copy_tree(self._url_of(path1), self._url_of(path2), config=self.config, verify=True)

    def touch(self, path: str, truncate: bool = True, **kwargs: Any) -> None:
        """Create an empty file; with ``truncate=False`` an existing one is left alone.

        fsspec's ``truncate=False`` means "update the timestamp if you can";
        XRootD has no portable request for that, so an existing file is
        simply left as it is.
        """
        filesystem, target = self._target(path)
        if not truncate and filesystem.exists(target):
            return
        filesystem.touch(target)

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def cat_file(
        self, path: str, start: int | None = None, end: int | None = None, **kw: Any
    ) -> bytes:
        """``[start:end]`` of the file, bounds read as a Python slice's."""
        filesystem, target = self._target(path)
        with filesystem.open(target, "rb", buffering=0) as handle:
            return _read_span(handle, start, end)

    def cat_ranges(
        self,
        paths: list[str],
        starts: list[int | None] | int | None,
        ends: list[int | None] | int | None,
        max_gap: int | None = None,
        **kwargs: Any,
    ) -> list[bytes]:
        """One vector read per file - ``kXR_readv``, or a multi-range ``GET`` -
        which is the point of this method.

        The bounds are :meth:`cat_file`'s, and, as in fsspec, a single value
        rather than a list applies to every path.
        """
        if not isinstance(starts, list):
            starts = [starts] * len(paths)
        if not isinstance(ends, list):
            ends = [ends] * len(paths)
        wanted: dict[str, list[tuple[int, int | None, int | None]]] = {}
        for index, (path, start, end) in enumerate(zip(paths, starts, ends, strict=True)):
            wanted.setdefault(path, []).append((index, start, end))
        out: list[bytes] = [b""] * len(paths)
        for path, items in wanted.items():
            filesystem, target = self._target(path)
            with filesystem.open(target, "rb", buffering=0) as handle:
                for index, chunk in self._ranges_of(handle, items):
                    out[index] = chunk
        return out

    @staticmethod
    def _ranges_of(
        handle: IO[bytes], items: list[tuple[int, int | None, int | None]]
    ) -> list[tuple[int, bytes]]:
        """Each ``(index, start, end)`` of one open file, read in one go if it can."""
        from .types import ReadRange

        # ``root://`` handles carry the file that does ``kXR_readv``; an HTTP
        # handle does its multi-range ``GET`` itself.
        file = getattr(handle, "file", handle)
        if not hasattr(file, "readv"):
            return [(index, _read_span(handle, start, end)) for index, start, end in items]
        size = handle.seek(0, io.SEEK_END)
        spans = [(index, *_span(start, end, size)) for index, start, end in items]
        # An empty range has nothing to ask the server for.
        ranges = [ReadRange(offset=offset, length=length) for _i, offset, length in spans if length]
        chunks = iter(file.readv(ranges))
        return [(index, bytes(next(chunks)) if length else b"") for index, _o, length in spans]

    def pipe_file(self, path: str, value: bytes, **kwargs: Any) -> None:
        """Write ``value`` as the whole content of ``path``, replacing what was there."""
        filesystem, target = self._target(path)
        filesystem.write_bytes(target, value)
        self.invalidate_cache()

    def _open(
        self,
        path: str,
        mode: str = "rb",
        block_size: int | None = None,
        autocommit: bool = True,
        cache_options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """A real file object, not an ``AbstractBufferedFile`` re-implementation.

        The library already returns something from the :mod:`io` stack, so
        fsspec gets the genuine article - seekable, buffered, and iterable -
        instead of a wrapper that re-derives what ``io`` already does.

        That is the default: no ``cache_type`` given. Naming one - any key of
        :data:`fsspec.caching.caches`, ``"readahead"`` and ``"none"``
        included - reads through that fsspec cache instead, built as
        ``AbstractBufferedFile`` builds it, with ``block_size`` (else
        :attr:`blocksize`) and ``cache_options`` as its arguments; see
        :class:`_CachedReader`. A cache is a reading strategy, so a file opened
        for writing ignores it, as fsspec's own files do.
        """
        filesystem, target = self._target(path)
        cache_type = kwargs.pop("cache_type", None)
        if cache_type is None or "r" not in mode or "+" in mode:
            buffering = -1 if block_size is None else block_size
            return filesystem.open(target, mode, buffering=buffering)
        cache = _cache_class(cache_type)
        raw = filesystem.open(target, "rb", buffering=0)
        try:
            return _CachedReader(raw, cache, block_size or self.blocksize, cache_options or {})
        except BaseException:
            raw.close()
            raise


class HTTPXRootDFileSystem(XRootDFileSystem):
    """The same bindings for ``https://``/``davs://`` endpoints."""

    protocol = ("dav", "davs", "webdav")


class S3XRootDFileSystem(XRootDFileSystem):
    """The same bindings for ``s3://`` buckets.

    Deliberately *not* registered as an entry point: ``s3fs`` owns that
    protocol in most environments, and silently taking it over would change
    what an unrelated ``fsspec.open("s3://...")`` does. Ask for it explicitly::

        fsspec.register_implementation("s3", S3XRootDFileSystem, clobber=True)
    """

    protocol: tuple[str, ...] = ("s3",)


def register_fsspec() -> list[str]:
    """Make fsspec use this package's filesystems for every protocol it declares.

    The ``fsspec.specs`` entry points register ``root``, ``davs`` and the
    rest by *name*, and so does ``fsspec-xrootd`` for ``root``: which one
    fsspec then picks depends on the order distributions are listed in (see
    ``docs/fsspec.md``). This registers the classes themselves, with
    ``clobber=True``, so the choice is made here and holds for the rest of
    the process - even over an implementation another package had already
    imported and registered. The protocols are read from this package's own
    entry points, so the list cannot drift from ``pyproject.toml``; ``s3``,
    which is deliberately not one of them, is left alone.

    Call it before the first ``fsspec.open``; a distributed program calls it
    in every process (xrdwms's dask layer does, on the client and on each
    worker). Idempotent. Returns the protocols registered, sorted.
    """
    registered = []
    for entry in metadata.entry_points(group="fsspec.specs"):
        module_name, _, attribute = entry.value.partition(":")
        # Every package's entry points share the group; only ours are ours to force.
        if module_name.split(".", 1)[0] != __name__.split(".", 1)[0]:
            continue
        register_implementation(
            entry.name, getattr(import_module(module_name), attribute), clobber=True
        )
        registered.append(entry.name)
    return sorted(registered)


def _cache_class(name: str) -> type[BaseCache]:
    """The fsspec cache class called ``name``, or a refusal that lists the real ones."""
    found: type[BaseCache] | None = caches.get(name)
    if found is None:
        known = ", ".join(sorted(str(key) for key in caches if key is not None))
        raise ValueError(f"unknown cache_type {name!r}; fsspec offers: {known}")
    return found


class _CachedReader(io.RawIOBase):
    """A read-only file that fetches through one of fsspec's caches.

    For a caller who asked for a particular fsspec cache by name - a
    ``"blockcache"`` for random access into a large file, ``"none"`` to see
    every read reach the server, ``"first"`` for a format whose header is
    re-read. The cache is made exactly as ``AbstractBufferedFile`` makes it,
    ``cache(blocksize, fetcher, size, **cache_options)``, and is reachable
    as :attr:`cache`, as it is on fsspec's own files. Its fetcher reads the
    byte range from the raw ``root://`` (or HTTP) layer underneath, under a
    lock, because a ``"background"`` cache fetches from a thread of its own.
    """

    def __init__(
        self,
        raw: IO[bytes],
        cache: type[BaseCache],
        blocksize: int,
        options: dict[str, Any],
    ) -> None:
        """Wrap ``raw`` (an unbuffered open file, now owned by this reader).

        The size is taken once, here, because fsspec's caches are built for a
        fixed-size object; a file still growing is read to the size it had.
        """
        super().__init__()
        self._raw = raw
        self._lock = threading.Lock()
        # The reader's own position; the raw file's moves with every fetch.
        self._loc = 0
        self.size = raw.seek(0, io.SEEK_END)
        self.blocksize = blocksize
        self.cache = cache(blocksize, self._fetch, self.size, **options)

    def _fetch(self, start: int, end: int) -> bytes:
        """``[start, end)`` from the server, all of it unless the file ends first."""
        parts = []
        with self._lock:
            self._raw.seek(start)
            left = end - start
            while left > 0:
                piece = self._raw.read(left)
                if not piece:
                    break
                parts.append(piece)
                left -= len(piece)
        return b"".join(parts)

    def readable(self) -> bool:
        """Always: this reader exists only for reading."""
        return True

    def seekable(self) -> bool:
        """Always: seeking only moves a number, the cache fetches on read."""
        return True

    def tell(self) -> int:
        """The current position, in bytes from the start."""
        return self._loc

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Move the position; past the end is allowed and reads as end of file."""
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._loc, io.SEEK_END: self.size}.get(whence)
        if base is None:
            raise ValueError(f"invalid whence ({whence})")
        if base + offset < 0:
            raise ValueError("negative seek position")
        self._loc = base + offset
        return self._loc

    def readinto(self, buffer: Any) -> int:
        """Fill ``buffer`` from the cache at the current position; ``0`` at the end.

        This is the one method :class:`io.RawIOBase` needs for ``read``,
        ``readall`` and wrapping in a :class:`io.BufferedReader` to work.
        """
        view = memoryview(buffer).cast("B")
        end = min(self._loc + len(view), self.size)
        if end <= self._loc:
            return 0
        # ``_fetch`` is fsspec's (private but stable) cache entry point; it is
        # what AbstractBufferedFile.read calls too.
        data = self.cache._fetch(self._loc, end)
        view[: len(data)] = data
        self._loc += len(data)
        return len(data)

    def close(self) -> None:
        """Close the raw file underneath; idempotent, as :mod:`io` requires."""
        if not self.closed:
            self._raw.close()
        super().close()
