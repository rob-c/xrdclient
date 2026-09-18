"""The async facade: everything the synchronous API does, awaitable.

    >>> import xrdclient.aio
    >>> async with xrdclient.aio.open("root://eos.example.org//store/f.root") as fh:
    ...     header = await fh.read(1024)

    >>> async with xrdclient.aio.FileSystem("root://eos.example.org") as fs:
    ...     info = await fs.stat("/store/f.root")
    ...     async for entry in fs.iterdir("/store"):
    ...         print(entry.name)

The objects here are thin awaitable mirrors of :class:`xrdclient.FileSystem` and the
file objects :func:`xrdclient.open` returns: same names, same arguments, same
exceptions, same semantics — with ``await`` in front and ``async for`` over
what used to be a generator. Whatever the sync object supports, so does this:
``root://`` and ``davs://`` alike, because the mirror wraps whichever
implementation the scheme selected.

**How it runs.** Each call is handed to a worker thread
(:func:`asyncio.to_thread`), so the event loop is never blocked and one
coroutine's ``await`` does not stall another's. Two consequences worth
knowing:

* Concurrent calls on **one** endpoint are serialised by that session's lock,
  exactly as concurrent threads are in the sync API. Concurrency across
  *different* endpoints is real. To read four files at once, open four
  filesystems (or four files) and :func:`asyncio.gather` them.
* Cancelling an ``await`` stops *your* coroutine, not the request already in
  flight on the worker thread. Anything that must be undone on cancellation
  should be undone in a ``finally:``.

Nothing in the synchronous package imports this module, and this module
imports nothing the synchronous package does not already provide.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import io
from collections.abc import AsyncIterator, Callable, Coroutine, Generator, Iterable, Sequence
from typing import Any, TypeVar

from .client import Checkpoint as _Checkpoint
from .client import File as _File
from .client import FileSystem as _FileSystem
from .config import Config
from .copy import CopyResult
from .copy import copy as _copy
from .copy import copy_tree as _copy_tree
from .copy import third_party as _third_party
from .errors import UnsupportedError, kXR_Unsupported
from .flags import DirListFlags, LocateFlags, PrepareFlags, QueryCode
from .io import open_url as _open_url
from .types import (
    CheckpointInfo,
    ChecksumInfo,
    CloneRange,
    DirEntry,
    LocationInfo,
    PageResult,
    PrepareStatus,
    ProtocolInfo,
    ReadRange,
    SpaceInfo,
    StatInfo,
    VFSInfo,
    WriteChunk,
)
from .url import XRootDURL

__all__ = [
    "AsyncCheckpoint",
    "AsyncFile",
    "AsyncFileSystem",
    "FileSystem",
    "copy",
    "copy_tree",
    "open",
    "third_party",
]

T = TypeVar("T")

_MISSING = object()


async def _run(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run a blocking library call off the event loop."""
    return await asyncio.to_thread(func, *args, **kwargs)


class _Iterating:
    """``async for`` over a synchronous iterator, one step per thread hop.

    The underlying generator stays lazy: nothing is materialised, and a
    directory tree that is never fully walked is never fully listed.
    """

    __slots__ = ("_factory", "_iterator")

    def __init__(self, factory: Callable[[], Iterable[Any]]) -> None:
        self._factory = factory
        self._iterator: Any = None

    def __aiter__(self) -> _Iterating:
        return self

    async def __anext__(self) -> Any:
        if self._iterator is None:
            self._iterator = await _run(lambda: iter(self._factory()))
        item = await _run(next, self._iterator, _MISSING)
        if item is _MISSING:
            raise StopAsyncIteration
        return item


class _Opening:
    """What :func:`open` returns: awaitable, and an async context manager.

        >>> fh = await xrdclient.aio.open(url)            # explicit close
        >>> async with xrdclient.aio.open(url) as fh:     # closed for you
        ...     ...
    """

    __slots__ = ("_factory", "_file")

    def __init__(self, factory: Callable[[], Coroutine[Any, Any, AsyncFile]]) -> None:
        self._factory = factory
        self._file: AsyncFile | None = None

    def __await__(self) -> Generator[Any, None, AsyncFile]:
        return self._factory().__await__()

    async def __aenter__(self) -> AsyncFile:
        self._file = await self._factory()
        return self._file

    async def __aexit__(self, *exc: object) -> None:
        if self._file is not None:
            await self._file.close()


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


class AsyncFile:
    """An awaitable file object over whatever :func:`xrdclient.open` returned.

    The read/write surface is :mod:`io`'s, so ``read``, ``write``, ``seek``,
    ``tell``, ``readline``, ``writelines``, ``truncate``, ``flush`` and
    ``close`` are all there and all awaitable. The predicates that answer
    without touching the network — ``readable``, ``writable``, ``seekable``,
    ``closed``, ``mode``, ``name`` — stay synchronous, because making them
    coroutines would buy nothing and cost every caller an ``await``.
    """

    __slots__ = ("_sync",)

    def __init__(self, handle: io.IOBase) -> None:
        self._sync = handle

    # -- what needs no round trip --------------------------------------

    @property
    def raw(self) -> io.IOBase:
        """The synchronous object underneath, for anything not mirrored here."""
        return self._sync

    @property
    def file(self) -> _File | None:
        """The protocol-level :class:`xrdclient.File`, on ``root://`` endpoints.

        Buffering and text decoding are layers over the raw object that owns
        the handle, so this digs down through them rather than giving up.
        """
        layer: object = self._sync
        while layer is not None:
            found = getattr(layer, "file", None)
            if isinstance(found, _File):
                return found
            layer = getattr(layer, "buffer", None) or getattr(layer, "raw", None)
        return None

    @property
    def mode(self) -> str:
        return str(getattr(self._sync, "mode", ""))

    @property
    def name(self) -> str:
        return str(getattr(self._sync, "name", ""))

    @property
    def closed(self) -> bool:
        return bool(self._sync.closed)

    def readable(self) -> bool:
        return bool(self._sync.readable())

    def writable(self) -> bool:
        return bool(self._sync.writable())

    def seekable(self) -> bool:
        return bool(self._sync.seekable())

    def fileno(self) -> int:
        return self._sync.fileno()

    # -- reading -------------------------------------------------------

    async def read(self, size: int = -1) -> Any:
        """Up to ``size`` bytes (or characters in text mode); all of it if ``-1``."""
        return await _run(self._sync.read, size)

    async def readinto(self, buffer: bytearray | memoryview) -> int:
        """Fill ``buffer`` in place; returns how many bytes arrived."""
        return await _run(self._sync.readinto, buffer)  # type: ignore[attr-defined]

    async def readline(self, size: int = -1) -> Any:
        return await _run(self._sync.readline, size)

    async def readlines(self, hint: int = -1) -> list[Any]:
        return await _run(self._sync.readlines, hint)

    async def readv(self, ranges: Iterable[ReadRange | tuple[int, int]]) -> list[bytes]:
        """One ``kXR_readv``: many ranges, one round trip. ``root://`` only."""
        return await _run(self._native("readv").readv, ranges)

    async def pgread(self, size: int, offset: int, *, verify: bool = True) -> PageResult:
        """Paged read with per-page CRC32c. ``root://`` only."""
        return await _run(
            functools.partial(self._native("pgread").pgread, size, offset, verify=verify)
        )

    # -- writing -------------------------------------------------------

    async def write(self, data: Any) -> int:
        return await _run(self._sync.write, data)

    async def writelines(self, lines: Iterable[Any]) -> None:
        await _run(self._sync.writelines, lines)

    async def writev(
        self, chunks: Iterable[WriteChunk | tuple[int, bytes]], *, sync: bool = False
    ) -> int:
        """Scattered writes in one round trip. ``root://`` only."""
        return await _run(functools.partial(self._native("writev").writev, chunks, sync=sync))

    async def clone(
        self,
        source: AsyncFile | _File,
        ranges: Iterable[CloneRange | tuple[int, int] | tuple[int, int, int]] | None = None,
    ) -> int:
        """Copy ranges of ``source`` into this file, server-side. ``root://`` only."""
        native = source._native("clone") if isinstance(source, AsyncFile) else source
        return await _run(self._native("clone").clone, native, ranges)

    async def pgwrite(self, data: bytes, offset: int = 0) -> int:
        """Paged write with per-page CRC32c. ``root://`` only."""
        return await _run(self._native("pgwrite").pgwrite, data, offset)

    async def truncate(self, size: int | None = None) -> int:
        return await _run(self._sync.truncate, size)

    async def flush(self) -> None:
        await _run(self._sync.flush)

    async def sync(self) -> None:
        """Commit to storage (``kXR_sync``), after flushing what is buffered."""
        await _run(self._sync.flush)
        await _run(self._native("sync").sync)

    # -- position ------------------------------------------------------

    async def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return await _run(self._sync.seek, offset, whence)

    async def tell(self) -> int:
        return await _run(self._sync.tell)

    async def bind_data_path(self) -> int:
        """Move bulk I/O onto a second connection. ``root://`` only.

        See :meth:`xrdclient.File.bind_data_path`. It costs a connection and a
        handshake, so it is worth it for a file being streamed and not for
        one being peeked at.
        """
        return await _run(self._native("bind_data_path").bind_data_path)

    @property
    def data_path(self) -> int:
        """The bound data path this file's bulk I/O uses, or 0 for none."""
        found = self.file
        return found.data_path if found is not None else 0

    # -- metadata ------------------------------------------------------

    async def stat(self, *, refresh: bool = False) -> StatInfo:
        """``kXR_stat`` on the open handle. ``root://`` only."""
        return await _run(functools.partial(self._native("stat").stat, refresh=refresh))

    async def checksum(self, algorithm: str | None = None) -> ChecksumInfo:
        """The server's checksum for this file. ``root://`` only."""
        return await _run(self._native("checksum").checksum, algorithm)

    @contextlib.asynccontextmanager
    async def checkpoint(self) -> AsyncIterator[AsyncCheckpoint]:
        """Transactional writes: committed on a clean exit, rolled back on one.

            >>> async with fh.checkpoint() as cp:      # doctest: +SKIP
            ...     await fh.write(payload)
            ...     await fh.flush()
            ...     (await cp.query()).free

        The journal only sees what has reached the server, so flush inside the
        block - a buffered write still sitting in the buffer is written after
        the commit, not inside it. ``root://`` only, and only where the server
        implements ``kXR_chkpoint``.
        """
        block = self._native("checkpoint").checkpoint()
        started = await _run(block.__enter__)
        try:
            yield AsyncCheckpoint(started)
        except BaseException as exc:
            await _run(block.__exit__, type(exc), exc, exc.__traceback__)
            raise
        else:
            await _run(block.__exit__, None, None, None)

    # -- lifetime ------------------------------------------------------

    async def close(self) -> None:
        """Flush and release. Safe to call more than once."""
        await _run(self._sync.close)

    async def __aenter__(self) -> AsyncFile:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._lines()

    async def _lines(self) -> AsyncIterator[Any]:
        while line := await self.readline():
            yield line

    def _native(self, operation: str) -> _File:
        """The protocol-level file, or a clear refusal if there is not one."""
        found = self.file
        if found is None:
            raise UnsupportedError(
                kXR_Unsupported, f"{operation} needs a root:// endpoint, not {self.name}"
            )
        return found

    def __repr__(self) -> str:
        return f"AsyncFile({self._sync!r})"


class AsyncCheckpoint:
    """The checkpoint an :meth:`AsyncFile.checkpoint` block is writing into."""

    __slots__ = ("_sync",)

    def __init__(self, checkpoint: _Checkpoint) -> None:
        self._sync = checkpoint

    async def query(self) -> CheckpointInfo:
        """``kXR_ckpQuery`` - how much room the journal has left."""
        return await _run(self._sync.query)

    def __repr__(self) -> str:
        return f"AsyncCheckpoint({str(self._sync.file.url)!r})"


# ---------------------------------------------------------------------------
# Filesystems
# ---------------------------------------------------------------------------


class AsyncFileSystem:
    """Namespace operations on one endpoint, awaitable.

        >>> async with AsyncFileSystem("root://eos.example.org") as fs:
        ...     await fs.makedirs("/store/user/me/out", exist_ok=True)
        ...     await fs.write_bytes("/store/user/me/out/f.bin", b"data")

    Constructing one touches no network — the connection is made on the first
    call that needs it, as in the sync API. The scheme picks the
    implementation, so ``davs://`` works here too and raises
    :class:`~xrdclient.UnsupportedError` for the handful of operations HTTP has no
    verb for.
    """

    __slots__ = ("_sync",)

    def __init__(self, url: str | XRootDURL, config: Config | None = None) -> None:
        self._sync = _FileSystem(url, config)

    @classmethod
    def wrap(cls, filesystem: _FileSystem) -> AsyncFileSystem:
        """Mirror a filesystem that already exists, sharing its connection."""
        mirrored = cls.__new__(cls)
        mirrored._sync = filesystem
        return mirrored

    # -- what needs no round trip --------------------------------------

    @property
    def sync(self) -> _FileSystem:
        """The synchronous filesystem underneath."""
        return self._sync

    @property
    def url(self) -> XRootDURL:
        return self._sync.url

    @property
    def config(self) -> Config:
        return self._sync.config

    @property
    def endpoint(self) -> str:
        return self._sync.endpoint

    # -- interrogation -------------------------------------------------

    async def ping(self) -> None:
        """Round-trip the server. Raises if it is unhealthy."""
        await _run(self._sync.ping)

    async def protocol(self) -> ProtocolInfo:
        """Capabilities of the endpoint, from the connection's negotiation."""
        return await _run(self._sync.protocol)

    async def stat(self, path: str, *, follow_symlinks: bool = True) -> StatInfo:
        return await _run(self._sync.stat, path, follow_symlinks=follow_symlinks)

    async def lstat(self, path: str) -> StatInfo:
        """``os.lstat``: stat a symbolic link rather than what it points at."""
        return await _run(self._sync.lstat, path)

    async def is_symlink(self, path: str) -> bool:
        return bool(await _run(self._sync.is_symlink, path))

    async def statvfs(self, path: str = "/") -> VFSInfo:
        return await _run(self._sync.statvfs, path)

    async def statx(self, paths: Sequence[str]) -> list[StatInfo]:
        return await _run(self._sync.statx, paths)

    async def exists(self, path: str) -> bool:
        return await _run(self._sync.exists, path)

    async def isdir(self, path: str) -> bool:
        return await _run(self._sync.isdir, path)

    async def isfile(self, path: str) -> bool:
        return await _run(self._sync.isfile, path)

    async def getsize(self, path: str) -> int:
        return await _run(self._sync.getsize, path)

    async def checksum(self, path: str, algorithm: str | None = None) -> ChecksumInfo:
        return await _run(self._sync.checksum, path, algorithm)

    async def query(self, code: QueryCode | int | str, args: str = "") -> bytes:
        return await _run(self._sync.query, code, args)

    async def query_config(self, *names: str) -> dict[str, str]:
        return await _run(self._sync.query_config, *names)

    async def extensions(self) -> frozenset[str]:
        return await _run(self._sync.extensions)

    async def query_stats(self, selectors: str = "a") -> str:
        return await _run(self._sync.query_stats, selectors)

    async def query_space(self, path: str = "/") -> SpaceInfo:
        return await _run(self._sync.query_space, path)

    async def checksum_cancel(self, path: str) -> None:
        await _run(self._sync.checksum_cancel, path)

    async def set_property(self, directive: str) -> None:
        await _run(self._sync.set_property, directive)

    async def appid(self, name: str) -> None:
        await _run(self._sync.appid, name)

    async def locate(
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
        return await _run(
            functools.partial(
                self._sync.locate,
                path,
                create=create,
                refresh=refresh,
                no_wait=no_wait,
                add_peers=add_peers,
                prefer_name=prefer_name,
                flags=flags,
            )
        )

    async def deep_locate(self, path: str, *, create: bool = False) -> list[LocationInfo]:
        return await _run(functools.partial(self._sync.deep_locate, path, create=create))

    async def prepare(
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
        return await _run(
            functools.partial(
                self._sync.prepare,
                paths,
                stage=stage,
                evict=evict,
                notify=notify,
                fresh=fresh,
                priority=priority,
                flags=flags,
            )
        )

    async def evict(self, paths: Sequence[str]) -> str:
        return await _run(self._sync.evict, paths)

    async def query_prepare(self, handle: str, paths: Sequence[str]) -> list[PrepareStatus]:
        return await _run(self._sync.query_prepare, handle, paths)

    async def cancel_prepare(self, handle: str) -> None:
        await _run(self._sync.cancel_prepare, handle)

    async def archive_info(self, paths: Sequence[str]) -> list[PrepareStatus]:
        return await _run(self._sync.archive_info, paths)

    # -- listing -------------------------------------------------------

    async def scandir(
        self,
        path: str = "",
        *,
        stat: bool = True,
        online: bool = False,
        algorithm: str = "",
        flags: DirListFlags | int | str | None = None,
    ) -> list[DirEntry]:
        return await _run(
            functools.partial(
                self._sync.scandir,
                path,
                stat=stat,
                online=online,
                algorithm=algorithm,
                flags=flags,
            )
        )

    async def listdir(self, path: str = "") -> list[str]:
        return await _run(self._sync.listdir, path)

    def iterdir(self, path: str = "") -> _Iterating:
        """``async for entry in fs.iterdir("/store")``."""
        return _Iterating(functools.partial(self._sync.iterdir, path))

    def walk(self, path: str = "", *, topdown: bool = True) -> _Iterating:
        """``async for root, dirs, files in fs.walk("/store")``."""
        return _Iterating(functools.partial(self._sync.walk, path, topdown=topdown))

    def glob(self, pattern: str, *, root: str = "") -> _Iterating:
        """``async for path in fs.glob("/store/*.root")``."""
        return _Iterating(functools.partial(self._sync.glob, pattern, root=root))

    # -- mutation ------------------------------------------------------

    async def mkdir(
        self, path: str, mode: int | str = 0o755, *, parents: bool = False, exist_ok: bool = False
    ) -> None:
        await _run(
            functools.partial(self._sync.mkdir, path, mode, parents=parents, exist_ok=exist_ok)
        )

    async def makedirs(self, path: str, mode: int | str = 0o755, exist_ok: bool = False) -> None:
        await _run(self._sync.makedirs, path, mode, exist_ok)

    async def rmdir(self, path: str) -> None:
        await _run(self._sync.rmdir, path)

    async def remove(self, path: str) -> None:
        await _run(self._sync.remove, path)

    async def rmtree(self, path: str, *, ignore_errors: bool = False) -> None:
        await _run(functools.partial(self._sync.rmtree, path, ignore_errors=ignore_errors))

    async def rename(self, src: str, dst: str) -> None:
        await _run(self._sync.rename, src, dst)

    async def chmod(self, path: str, mode: int | str) -> None:
        await _run(self._sync.chmod, path, mode)

    async def utime(
        self,
        path: str,
        times: tuple[float, float] | None = None,
        *,
        ns: tuple[int, int] | None = None,
    ) -> None:
        await _run(self._sync.utime, path, times, ns=ns)

    async def chown(self, path: str, uid: int = -1, gid: int = -1) -> None:
        await _run(self._sync.chown, path, uid, gid)

    async def truncate(self, path: str, size: int) -> None:
        await _run(self._sync.truncate, path, size)

    async def symlink(self, target: str, link: str) -> None:
        """``os.symlink`` order. A vendor extension - see the sync method."""
        await _run(self._sync.symlink, target, link)

    async def link(self, src: str, dst: str) -> None:
        """``os.link`` order. A vendor extension - see the sync method."""
        await _run(self._sync.link, src, dst)

    #: ``os``'s spelling of the same call.
    hardlink = link

    async def readlink(self, path: str) -> str:
        """What a symbolic link points at. A vendor extension."""
        return str(await _run(self._sync.readlink, path))

    async def touch(self, path: str, *, exist_ok: bool = True) -> None:
        await _run(functools.partial(self._sync.touch, path, exist_ok=exist_ok))

    # -- extended attributes -------------------------------------------

    async def getxattr(self, path: str, name: str) -> bytes:
        return await _run(self._sync.getxattr, path, name)

    async def setxattr(
        self, path: str, name: str, value: bytes, *, create_only: bool = False
    ) -> None:
        await _run(
            functools.partial(self._sync.setxattr, path, name, value, create_only=create_only)
        )

    async def removexattr(self, path: str, name: str) -> None:
        await _run(self._sync.removexattr, path, name)

    async def listxattr(self, path: str) -> list[str]:
        return await _run(self._sync.listxattr, path)

    async def listxattr_tree(self, path: str) -> dict[str, list[str]]:
        return await _run(self._sync.listxattr_tree, path)

    async def xattrs(self, path: str) -> dict[str, bytes]:
        return await _run(self._sync.xattrs, path)

    # -- whole files ---------------------------------------------------

    async def read_bytes(self, path: str) -> bytes:
        return await _run(self._sync.read_bytes, path)

    async def write_bytes(self, path: str, data: bytes) -> int:
        return await _run(self._sync.write_bytes, path, data)

    async def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return await _run(self._sync.read_text, path, encoding)

    async def write_text(self, path: str, text: str, encoding: str = "utf-8") -> int:
        return await _run(self._sync.write_text, path, text, encoding)

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
    ) -> _Opening:
        """Open a file on this endpoint; ``await`` it or ``async with`` it."""

        async def opened() -> AsyncFile:
            handle = await _run(
                functools.partial(
                    self._sync.open,
                    path,
                    mode,
                    buffering=buffering,
                    encoding=encoding,
                    errors=errors,
                    newline=newline,
                    posc=posc,
                )
            )
            return AsyncFile(handle)  # type: ignore[arg-type]

        return _Opening(opened)

    # -- lifetime ------------------------------------------------------

    async def close(self) -> None:
        await _run(self._sync.close)

    async def __aenter__(self) -> AsyncFileSystem:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def __repr__(self) -> str:
        return f"AsyncFileSystem({str(self._sync.url)!r})"


#: ``xrdclient.aio.FileSystem`` reads better at a call site than ``AsyncFileSystem``,
#: and the module name already says which one you have.
FileSystem = AsyncFileSystem
File = AsyncFile


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


def open(
    url: str | XRootDURL,
    mode: str = "rb",
    *,
    buffering: int = -1,
    encoding: str | None = None,
    errors: str | None = None,
    newline: str | None = None,
    config: Config | None = None,
    posc: bool = False,
) -> _Opening:
    """:func:`xrdclient.open`, awaitable.

        >>> async with xrdclient.aio.open("root://host//store/f.root") as fh:
        ...     header = await fh.read(1024)

    The returned object is both awaitable and an async context manager, so
    either spelling works and only one of them leaves you to call ``close``.
    """

    async def opened() -> AsyncFile:
        handle = await _run(
            functools.partial(
                _open_url,
                url,
                mode,
                buffering=buffering,
                encoding=encoding,
                errors=errors,
                newline=newline,
                config=config,
                posc=posc,
            )
        )
        return AsyncFile(handle)

    return _Opening(opened)


async def copy(source: Any, target: Any, **kwargs: Any) -> CopyResult:
    """:func:`xrdclient.copy`, awaitable. ``progress=`` is called from the worker thread."""
    return await _run(functools.partial(_copy, source, target, **kwargs))


async def copy_tree(source: Any, target: Any, **kwargs: Any) -> list[CopyResult]:
    """:func:`xrdclient.copy_tree`, awaitable."""
    return await _run(functools.partial(_copy_tree, source, target, **kwargs))


async def third_party(source: Any, target: Any, **kwargs: Any) -> CopyResult:
    """:func:`xrdclient.third_party`, awaitable."""
    return await _run(functools.partial(_third_party, source, target, **kwargs))
