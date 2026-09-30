"""fsspec bindings: ``root://`` and ``davs://`` for pandas, dask, and pyarrow.

    >>> import fsspec
    >>> with fsspec.open("root://eos.example.org//store/f.root", "rb") as fh:
    ...     header = fh.read(1024)
    >>> import pandas as pd
    >>> pd.read_parquet("root://eos.example.org//store/t.parquet")

Registered through the ``fsspec.specs`` entry points for ``root``, ``roots``
and ``xroot``, so nothing has to be imported by hand. This package does not
depend on ``fsspec``: the module is loaded by ``fsspec`` itself, wherever it
is installed, and the rest of the package never imports it.
"""

from __future__ import annotations

import io
import posixpath
from typing import IO, Any

try:
    from fsspec.spec import AbstractFileSystem
    from fsspec.utils import stringify_path
except ImportError as exc:  # pragma: no cover - exercised by the extra, not by us
    raise ImportError(
        "fsspec is not installed; pip install fsspec"
    ) from exc

from ._compat import zip_strict
from .client import FileSystem
from .config import Config
from .types import StatInfo
from .url import XRootDURL, parse

__all__ = ["XRootDFileSystem", "HTTPXRootDFileSystem", "S3XRootDFileSystem"]


class _ClassOrInstance:
    """One name that means one method on the class and another on an instance.

    fsspec calls ``_strip_protocol`` and ``_parent`` both ways. On the class
    - which is how it builds the path it hands the instance it made for a URL
    - no endpoint is known, and the path alone is the answer. An instance
    knows its own endpoint, and it alone can tell that a URL names some
    *other* server, which a bare path would silently send back to its own.
    """

    def __init__(self, on_class: str, on_instance: str) -> None:
        self.on_class = on_class
        self.on_instance = on_instance

    def __get__(self, instance: object, owner: type) -> Any:
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


class XRootDFileSystem(AbstractFileSystem):
    """An :class:`fsspec.AbstractFileSystem` over one XRootD or HTTP endpoint.

        >>> fs = XRootDFileSystem("root://eos.example.org")
        >>> fs.ls("/store", detail=False)
        ['/store/a.root', '/store/b.root']

    One instance is one endpoint. ``fsspec`` caches instances by their
    constructor arguments, so repeated ``fsspec.open`` calls against the same
    server share this object - and therefore share its connection.
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
        super().__init__(**kwargs)
        self.config = config or Config()
        self.endpoint = endpoint
        self._fs = FileSystem(parse(endpoint).with_path("/"), self.config) if endpoint else None
        #: Endpoints reached through a fully-qualified path, kept so they are
        #: opened once and closed with this object rather than leaked.
        self._elsewhere: dict[str, FileSystem] = {}

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
        return posixpath.dirname(str(cls._bare_path(path)))

    def _local_parent(self, path: Any) -> str:
        url = self._foreign(path)
        if url is None:
            return self._bare_parent(path)
        return str(url.with_path(posixpath.dirname(_trimmed(url.path))))

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
        self.dircache.clear()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # pragma: no cover - interpreter shutdown
            pass

    def close(self) -> None:
        """Release every connection this object opened. Safe to call twice."""
        if self._fs is not None:
            self._fs.close()
            self._fs = None
        for filesystem in self._elsewhere.values():
            filesystem.close()
        self._elsewhere.clear()

    # ------------------------------------------------------------------
    # Namespace
    # ------------------------------------------------------------------

    def _info_of(self, info: StatInfo, path: str) -> dict[str, Any]:
        return {
            "name": path,
            "size": info.st_size,
            "type": "directory" if info.is_dir() else "file",
            "mtime": info.st_mtime,
            "mode": info.st_mode,
        }

    def info(self, path: str, **kwargs: Any) -> dict[str, Any]:
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

    def exists(self, path: str, **kwargs: Any) -> bool:
        filesystem, target = self._target(path)
        return bool(filesystem.exists(target))

    def isdir(self, path: str) -> bool:
        filesystem, target = self._target(path)
        return bool(filesystem.isdir(target))

    def isfile(self, path: str) -> bool:
        filesystem, target = self._target(path)
        return bool(filesystem.isfile(target))

    def size(self, path: str) -> int:
        return int(self.info(path)["size"])

    def created(self, path: str) -> Any:
        return self._timestamp(self._stat(path).st_ctime)

    def modified(self, path: str) -> Any:
        return self._timestamp(self._stat(path).st_mtime)

    def _stat(self, path: str) -> StatInfo:
        filesystem, target = self._target(path)
        return filesystem.stat(target)

    @staticmethod
    def _timestamp(seconds: int) -> Any:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(seconds, tz=timezone.utc)

    def checksum(self, path: str, algorithm: str | None = None) -> str:
        """The *server's* checksum, not fsspec's synthetic one."""
        filesystem, target = self._target(path)
        return filesystem.checksum(target, algorithm).value

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def mkdir(self, path: str, create_parents: bool = True, **kwargs: Any) -> None:
        filesystem, target = self._target(path)
        filesystem.mkdir(target, parents=create_parents, exist_ok=create_parents)

    def makedirs(self, path: str, exist_ok: bool = False) -> None:
        filesystem, target = self._target(path)
        filesystem.makedirs(target, exist_ok=exist_ok)

    def rmdir(self, path: str) -> None:
        filesystem, target = self._target(path)
        filesystem.rmdir(target)

    def _rm(self, path: str) -> None:
        filesystem, target = self._target(path)
        filesystem.remove(target)

    def rm(self, path: Any, recursive: bool = False, maxdepth: int | None = None) -> None:
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
        """One ``kXR_readv`` per file, which is the point of this method.

        The bounds are :meth:`cat_file`'s, and, as in fsspec, a single value
        rather than a list applies to every path.
        """
        if not isinstance(starts, list):
            starts = [starts] * len(paths)
        if not isinstance(ends, list):
            ends = [ends] * len(paths)
        wanted: dict[str, list[tuple[int, int | None, int | None]]] = {}
        for index, (path, start, end) in enumerate(zip_strict(paths, starts, ends)):
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

        file = getattr(handle, "file", None)
        if file is None or not hasattr(file, "readv"):  # pragma: no cover - HTTP
            return [(index, _read_span(handle, start, end)) for index, start, end in items]
        size = handle.seek(0, io.SEEK_END)
        spans = [(index, *_span(start, end, size)) for index, start, end in items]
        # An empty range has nothing to ask the server for.
        ranges = [ReadRange(offset=offset, length=length) for _i, offset, length in spans if length]
        chunks = iter(file.readv(ranges))
        return [(index, bytes(next(chunks)) if length else b"") for index, _o, length in spans]

    def pipe_file(self, path: str, value: bytes, **kwargs: Any) -> None:
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
        """
        filesystem, target = self._target(path)
        buffering = -1 if block_size is None else block_size
        return filesystem.open(target, mode, buffering=buffering)


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
