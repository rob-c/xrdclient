"""fsspec bindings: ``root://`` and ``davs://`` for pandas, dask, and pyarrow.

    >>> import fsspec
    >>> with fsspec.open("root://eos.example.org//store/f.root", "rb") as fh:
    ...     header = fh.read(1024)
    >>> import pandas as pd
    >>> pd.read_parquet("root://eos.example.org//store/t.parquet")

Registered through the ``fsspec.specs`` entry points for ``root``, ``roots``
and ``xroot``, so nothing has to be imported by hand. ``fsspec`` is an optional
extra (``pip install pyxrootdclient[fsspec]``); the rest of the package never
imports this module.
"""

from __future__ import annotations

import posixpath
from typing import Any

try:
    from fsspec.spec import AbstractFileSystem
except ImportError as exc:  # pragma: no cover - exercised by the extra, not by us
    raise ImportError(
        "fsspec is not installed; pip install 'pyxrootdclient[fsspec]'"
    ) from exc

from ._compat import zip_strict
from .client import FileSystem
from .config import Config
from .types import StatInfo
from .url import parse

__all__ = ["XRootDFileSystem", "HTTPXRootDFileSystem", "S3XRootDFileSystem"]


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
    def _strip_protocol(cls, path: str) -> str:
        """fsspec hands paths around bare; keep the path, drop the endpoint."""
        if "://" in str(path):
            return parse(path).path or "/"
        return "/" + str(path).lstrip("/")

    @staticmethod
    def _get_kwargs_from_urls(path: str) -> dict[str, str]:
        """What the instance cache keys on: the endpoint the URL names."""
        url = parse(path)
        return {"endpoint": str(url.with_path("/"))} if url.host else {}

    def _target(self, path: str) -> tuple[FileSystem, str]:
        """The filesystem to use for ``path``, and the path within it."""
        url = parse(path)
        if url.host and (self._fs is None or url.netloc != parse(self.endpoint).netloc):
            # A fully-qualified path to somewhere else: honour it rather than
            # silently reading the wrong server.
            key = str(url.with_path("/"))
            found = self._elsewhere.get(key)
            if found is None:
                found = self._elsewhere[key] = FileSystem(url.with_path("/"), self.config)
            return found, url.path or "/"
        if self._fs is None:
            raise ValueError("no endpoint: give a full URL or construct with endpoint=")
        return self._fs, self._strip_protocol(path)

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
        return self._info_of(filesystem.stat(target), target)

    def ls(self, path: str, detail: bool = True, **kwargs: Any) -> list[Any]:
        filesystem, target = self._target(path)
        listing: list[Any] = [
            self._info_of(entry.stat or StatInfo(), posixpath.join(target, entry.name))
            for entry in filesystem.scandir(target)
        ]
        listing.sort(key=lambda item: str(item["name"]))
        return listing if detail else [str(item["name"]) for item in listing]

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

    def mv(self, path1: str, path2: str, **kwargs: Any) -> None:
        filesystem, source = self._target(path1)
        _other, destination = self._target(path2)
        filesystem.rename(source, destination)
        self.invalidate_cache()

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
        filesystem, target = self._target(path)
        with filesystem.open(target, "rb", buffering=0) as handle:
            if start:
                handle.seek(start)
            data: bytes = handle.read() if end is None else handle.read(end - (start or 0))
        return data

    def cat_ranges(
        self,
        paths: list[str],
        starts: list[int],
        ends: list[int],
        max_gap: int | None = None,
        **kwargs: Any,
    ) -> list[bytes]:
        """One ``kXR_readv`` per file, which is the point of this method."""
        from .types import ReadRange

        wanted: dict[str, list[tuple[int, int, int]]] = {}
        for index, (path, start, end) in enumerate(zip_strict(paths, starts, ends)):
            wanted.setdefault(path, []).append((index, start, end))
        out: list[bytes] = [b""] * len(paths)
        for path, items in wanted.items():
            filesystem, target = self._target(path)
            with filesystem.open(target, "rb", buffering=0) as handle:
                file = getattr(handle, "file", None)
                if file is not None and hasattr(file, "readv"):
                    ranges = [ReadRange(offset=s, length=e - s) for _i, s, e in items]
                    for (index, _s, _e), chunk in zip_strict(items, file.readv(ranges)):
                        out[index] = bytes(chunk)
                else:  # pragma: no cover - HTTP has no vector read
                    for index, start, end in items:
                        handle.seek(start)
                        out[index] = handle.read(end - start)
        return out

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
