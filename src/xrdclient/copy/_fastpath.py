"""The fast path: downloads over the bulk data plane, and the descriptor helpers
they write through.

:func:`~xrdclient.copy.engine._plan` picks this path when
:func:`_bulk_usable` says the pair qualifies (``root://`` to a local file or an
open stream); :func:`~xrdclient.copy.engine._bulk` then calls
:func:`_bulk_to_file` or :func:`_bulk_to_stream`, and falls back to the general
pump when the server raises :class:`~xrdclient.session.bulk.BulkUnsupported`.
Everything here is a thin adapter over :mod:`xrdclient.client.bulk`; it is
separate from the engine because it owns raw descriptors (exclusive create,
``os.write`` loops) that nothing else in the copy path touches.
"""

from __future__ import annotations

import errno
import os
from contextlib import suppress
from typing import IO, TYPE_CHECKING, Any

from ..config import Config
from ..session.bulk import BulkUnsupported
from ..url import XRootDURL
from ._endpoints import _zip_member

if TYPE_CHECKING:
    from .engine import Progress


def _bulk_usable(source: XRootDURL | None, target: XRootDURL | None, config: Config) -> bool:
    """Whether this pair can go over the bulk data plane.

    It reads ``root://`` and writes either a local file, which every worker
    fills at its own offset, or an open stream, which one worker feeds in
    order. Anything else - an upload, a remote target, a resume - keeps the
    general pump.
    """
    return bool(
        config.bulk
        and source is not None
        and source.is_root
        and _zip_member(source) is None
        and (target is None or target.is_local)
    )


def _bulk_to_file(
    source: XRootDURL,
    target: XRootDURL,
    config: Config,
    progress: Progress | None,
    *,
    overwrite: bool,
) -> int:
    """Download to a local path with every connection writing its own span.

    The target is opened before the source is, so a copy that fails before
    a byte arrives - no such source, say - leaves the destination as it was
    found: a file it created is removed again, and one that was there is not
    truncated, since the download sizes the file itself once the source has
    answered. A failure after that keeps what was written, for a resume.
    """
    from ..client import bulk

    fd, created = _create(target.path, overwrite=overwrite)
    arrived = False

    def reported(done: int, total: int | None) -> None:
        """Note that bytes have landed - from then on a failure keeps the file."""
        nonlocal arrived
        arrived = True
        if progress is not None:
            progress(done, total)

    try:
        return bulk.download(source, fd, config=config, progress=reported).size
    except BaseException as exc:
        # ``BulkUnsupported`` sends the caller the ordinary way next, which
        # must find an exclusive create still exclusive.
        if created and (not arrived or isinstance(exc, BulkUnsupported)):
            with suppress(OSError):
                os.remove(target.path)
        raise
    finally:
        os.close(fd)


def _create(path: str, *, overwrite: bool) -> tuple[int, bool]:
    """A descriptor for writing ``path``, and whether this call created the file."""
    try:
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644), True
    except FileExistsError:
        if not overwrite:
            raise
    return os.open(path, os.O_WRONLY | os.O_CREAT), False


def _descriptor_of(writer: IO[bytes]) -> int | None:
    """``writer``'s file descriptor, with its buffer flushed, or ``None``.

    ``None`` covers everything that is not a real file: an in-memory buffer, a
    compressing wrapper, anything that only implements ``write``.
    """
    try:
        writer.flush()
        fd = writer.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    return fd if isinstance(fd, int) and fd >= 0 else None


def _write_descriptor(fd: int, view: memoryview) -> None:
    """Write all bytes, rejecting a filesystem that reports no progress."""
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise OSError(errno.EIO, "local write made no progress")
        written += count


def _bulk_to_stream(
    source: XRootDURL,
    writer: IO[bytes],
    config: Config,
    progress: Progress | None,
    digest: Any | None,
) -> int:
    """Stream to an open file object, in file order, digesting on the way."""
    from ..client import bulk

    write = writer.write
    # A buffered writer copies a multi-megabyte chunk through its own buffer
    # and takes its lock to do it. When the object is backed by a descriptor,
    # the bytes can go straight there instead - after flushing whatever the
    # caller had already put in the buffer, so the file stays in order.
    fd = _descriptor_of(writer)

    def emit(view: memoryview) -> None:
        """Digest, then write one in-order chunk; called on the single worker."""
        if digest is not None:
            digest.update(view)
        if fd is None:
            write(view)
            return
        _write_descriptor(fd, view)

    # One connection: the writer is the serialisation point, so a second would
    # only wait its turn. Depth, not width, is what hides the round trip here.
    return bulk.stream(source, emit, config=config, progress=progress, workers=1).size
