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
"""

from __future__ import annotations

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
    """A path on a remote storage element."""

    __slots__ = ("_url", "_config", "_fs")

    _url: XRootDURL
    _config: Config
    #: Built on first use, or inherited from the path this one was derived
    #: from, so a traversal is one connection rather than one per component.
    _fs: FileSystem | None

    def __init__(self, url: str | XRootDURL | XRootDPath, config: Config | None = None) -> None:
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
        return self._url

    @property
    def name(self) -> str:
        return posixpath.basename(self._url.path.rstrip("/"))

    @property
    def stem(self) -> str:
        return posixpath.splitext(self.name)[0]

    @property
    def suffix(self) -> str:
        return posixpath.splitext(self.name)[1]

    @property
    def suffixes(self) -> list[str]:
        parts = self.name.lstrip(".").split(".")
        return ["." + p for p in parts[1:]]

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(p for p in self._url.path.split("/") if p)

    @property
    def parent(self) -> XRootDPath:
        return self._derive(posixpath.dirname(self._url.path.rstrip("/")) or "/")

    @property
    def parents(self) -> tuple[XRootDPath, ...]:
        out = []
        node = self
        while node._url.path not in ("/", ""):
            node = node.parent
            out.append(node)
        return tuple(out)

    @property
    def anchor(self) -> str:
        return str(self._url.with_path("/"))

    def is_absolute(self) -> bool:
        return self._url.path.startswith("/")

    def with_name(self, name: str) -> XRootDPath:
        return self._derive(posixpath.join(posixpath.dirname(self._url.path.rstrip("/")), name))

    def with_suffix(self, suffix: str) -> XRootDPath:
        return self.with_name(self.stem + suffix)

    def with_stem(self, stem: str) -> XRootDPath:
        return self.with_name(stem + self.suffix)

    def joinpath(self, *parts: str) -> XRootDPath:
        return self._derive(posixpath.join(self._url.path, *parts))

    def relative_to(self, other: str | XRootDPath) -> str:
        base = other._url.path if isinstance(other, XRootDPath) else str(other)
        return posixpath.relpath(self._url.path, base)

    def _derive(self, path: str) -> XRootDPath:
        """Another path on the same endpoint, sharing this one's connection.

        Sharing matters: ``for entry in base.iterdir(): entry.stat()`` would
        otherwise open one connection per entry.
        """
        child = XRootDPath(self._url.with_path(path), self._config)
        child._fs = self.fs
        return child

    def __truediv__(self, other: str) -> XRootDPath:
        return self.joinpath(other)

    def __rtruediv__(self, other: str) -> XRootDPath:
        return XRootDPath(self._url.with_path(other), self._config) / self._url.path.lstrip("/")

    def __str__(self) -> str:
        return str(self._url)

    def __repr__(self) -> str:
        return f"XRootDPath({str(self._url)!r})"

    def __fspath__(self) -> str:
        return str(self._url)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, XRootDPath) and self._url == other._url

    def __hash__(self) -> int:
        return hash((self._url.endpoint, self._url.path))

    def __lt__(self, other: XRootDPath) -> bool:
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
        return self.fs.stat(self._url.path, follow_symlinks=follow_symlinks)

    def lstat(self) -> StatInfo:
        """``pathlib``'s ``lstat``: the link itself, not what it points at."""
        return self.fs.lstat(self._url.path)

    def is_symlink(self) -> bool:
        return self.fs.is_symlink(self._url.path)

    def exists(self) -> bool:
        return self.fs.exists(self._url.path)

    def is_dir(self) -> bool:
        return self.fs.isdir(self._url.path)

    def is_file(self) -> bool:
        return self.fs.isfile(self._url.path)

    def iterdir(self) -> Iterator[XRootDPath]:
        for entry in self.fs.scandir(self._url.path):
            yield self / entry.name

    def glob(self, pattern: str) -> Iterator[XRootDPath]:
        for found in self.fs.glob(pattern, root=self._url.path):
            yield self._derive(found)

    def rglob(self, pattern: str) -> Iterator[XRootDPath]:
        yield from self.glob(f"**/{pattern}")

    def walk(self, *, top_down: bool = True) -> Iterator[tuple[XRootDPath, list[str], list[str]]]:
        for root, dirs, files in self.fs.walk(self._url.path, topdown=top_down):
            yield self._derive(root), dirs, files

    def mkdir(self, mode: int | str = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
        self.fs.mkdir(self._url.path, mode, parents=parents, exist_ok=exist_ok)

    def rmdir(self) -> None:
        self.fs.rmdir(self._url.path)

    def unlink(self, missing_ok: bool = False) -> None:
        from .errors import NotFoundError

        try:
            self.fs.remove(self._url.path)
        except NotFoundError:
            if not missing_ok:
                raise

    def rename(self, target: str | XRootDPath) -> XRootDPath:
        destination = target._url.path if isinstance(target, XRootDPath) else str(target)
        self.fs.rename(self._url.path, destination)
        return self._derive(destination)

    #: ``pathlib`` distinguishes these two only in overwrite semantics, which
    #: the server decides.
    replace = rename

    def chmod(self, mode: int | str) -> None:
        self.fs.chmod(self._url.path, mode)

    def touch(self, mode: int | str = 0o644, exist_ok: bool = True) -> None:
        self.fs.touch(self._url.path, exist_ok=exist_ok)

    def open(self, mode: str = "rb", **kwargs: Any) -> IO[Any]:
        return self.fs.open(self._url.path, mode, **kwargs)

    def read_bytes(self) -> bytes:
        return self.fs.read_bytes(self._url.path)

    def write_bytes(self, data: bytes) -> int:
        return self.fs.write_bytes(self._url.path, data)

    def read_text(self, encoding: str = "utf-8") -> str:
        return self.fs.read_text(self._url.path, encoding)

    def write_text(self, text: str, encoding: str = "utf-8") -> int:
        return self.fs.write_text(self._url.path, text, encoding)

    def checksum(self, algorithm: str | None = None) -> Any:
        return self.fs.checksum(self._url.path, algorithm)

    def locate(self) -> Sequence[Any]:
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
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
