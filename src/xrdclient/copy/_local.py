"""Local-filesystem behaviour of the copy engine: a reader that survives
``ESTALE`` and the durability barrier at the end of a local write.

Split out of :mod:`xrdclient.copy.engine` because both are about what a POSIX
filesystem - often a network or FUSE one under a grid job - does to a plain
file, not about how a transfer is planned. :class:`_RecoveringLocalReader` is
what :func:`~xrdclient.copy._endpoints._reader` hands the pump for a local
source; :func:`_sync_local_target` is the ``fsync`` that
:func:`~xrdclient.copy.engine._copy_one` runs before it reports success. This
module imports nothing else from the package, so any sibling may use it.
"""

from __future__ import annotations

import errno
import os
import stat as stat_module
from collections.abc import Callable
from contextlib import suppress
from typing import IO, Any, cast

from xrdcore import retry as _retry

from ..config import Config
from ..url import XRootDURL

_TRANSIENT_LOCAL_READ_ERRORS = frozenset(
    getattr(errno, name)
    for name in ("EAGAIN", "EBUSY", "EINTR", "ESTALE", "ETIMEDOUT")
    if hasattr(errno, name)
)


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    """Fields that distinguish a file generation even on inode-virtualising FUSE."""
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_unbuffered(path: str) -> IO[bytes]:
    """``path`` opened ``rb`` with no buffer; the caller owns the handle."""
    return open(path, "rb", buffering=0)


class _RecoveringLocalReader:
    """A read-only local handle that survives transient filesystem invalidation.

    Network filesystems can return ``ESTALE`` after an otherwise healthy read.
    Reopening by name is safe only when it is still the same inode; otherwise
    continuing would silently splice two file generations into one copy.
    """

    def __init__(self, path: str, config: Config) -> None:
        """Open ``path`` unbuffered, retrying transient errors, and record its identity.

        The identity is taken from the descriptor (``fstat``), not the name,
        so it describes the generation actually being read.
        """
        self._path = path
        self._config = config
        self._handle: IO[bytes]
        attempts = 0
        while True:
            try:
                self._handle = _open_unbuffered(path)
                break
            except OSError as exc:
                attempts += 1
                if (
                    exc.errno not in _TRANSIENT_LOCAL_READ_ERRORS
                    or attempts > config.connect_retries
                ):
                    raise
                self._pause(attempts)
        try:
            info = os.fstat(self._handle.fileno())
        except BaseException:
            self._handle.close()
            raise
        self._identity = _file_identity(info)
        self._position = 0
        self._recovery_chunk: int | None = None

    def _pause(self, attempts: int) -> None:
        """Jittered exponential backoff, on the same knobs as network retries."""
        _retry.pause(attempts, self._config.retry_backoff, self._config.wait_cap)

    def _retry(self, operation: Callable[[IO[bytes]], Any]) -> Any:
        """Run ``operation`` on the handle, reopening it after a transient error.

        After a reopen the position is restored from :attr:`_position`, and
        the replacement must have the same identity as the file first opened;
        if not, ``ESTALE`` is raised rather than continue in another file.
        From the first recovery on, reads are capped at 4 KiB: a filesystem
        that has gone stale once tends to again, and smaller reads lose less
        work to each retry.
        """
        attempts = 0
        while True:
            try:
                return operation(self._handle)
            except OSError as exc:
                attempts += 1
                if (
                    exc.errno not in _TRANSIENT_LOCAL_READ_ERRORS
                    or attempts > self._config.connect_retries
                ):
                    raise
                self._recovery_chunk = 4096
                with suppress(OSError):
                    self._handle.close()
                self._pause(attempts)
                replacement = _open_unbuffered(self._path)
                try:
                    info = os.fstat(replacement.fileno())
                    same_file = _file_identity(info) == self._identity
                    replacement.seek(self._position)
                except BaseException:
                    replacement.close()
                    raise
                if not same_file:
                    replacement.close()
                    raise OSError(errno.ESTALE, "local source changed while recovering") from exc
                self._handle = replacement

    def read(self, size: int = -1) -> bytes:
        """Up to ``size`` bytes (all, if negative), surviving a transient ``ESTALE``."""

        def read(handle: IO[bytes]) -> bytes:
            """One read attempt, capped while recovering."""
            wanted = size
            if self._recovery_chunk is not None and (wanted < 0 or wanted > self._recovery_chunk):
                wanted = self._recovery_chunk
            return handle.read(wanted)

        data = cast("bytes", self._retry(read))
        self._position += len(data)
        return data

    def readinto(self, buffer: bytearray) -> int:
        """Fill ``buffer`` (or its first 4 KiB, while recovering); the count."""

        def readinto(handle: IO[bytes]) -> int:
            """One ``readinto`` attempt on the current handle."""
            view = memoryview(buffer)
            if self._recovery_chunk is not None:
                view = view[: self._recovery_chunk]
            return cast("int", cast("Any", handle).readinto(view))

        count = cast("int", self._retry(readinto))
        self._position += count
        return count

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        """Seek, tracking the position a reopen must restore."""
        position = cast("int", self._retry(lambda handle: handle.seek(offset, whence)))
        self._position = position
        return position

    def close(self) -> None:
        """Close the current handle (a replacement, if there was a recovery)."""
        self._handle.close()


def _sync_local_target(target: XRootDURL | None, expected_size: int | None) -> None:
    """Commit a completed regular local target before reporting success.

    Closing a descriptor only hands dirty pages to the filesystem. A FUSE
    cache, network filesystem or failing disk can acknowledge every write and
    still lose them before they reach its backing store. One durability barrier
    at the end keeps the hot copy loop untouched and makes a late ENOSPC/EIO
    visible before verification or ``remove_source`` declares success. Devices,
    sockets and FIFOs are streams, not durable files, and are left alone.
    """
    if target is None or not target.is_local:
        return
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(target.path, flags)
    try:
        info = os.fstat(fd)
        if stat_module.S_ISREG(info.st_mode):
            os.fsync(fd)
            info = os.fstat(fd)
            if expected_size is not None and info.st_size != expected_size:
                raise OSError(
                    errno.EIO,
                    f"local target has {info.st_size} bytes after sync; expected {expected_size}",
                )
    finally:
        os.close(fd)
