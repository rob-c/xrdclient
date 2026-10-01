"""Streaming copies into ZIP archives, compatible with ``xrdcp --zip-append``."""

from __future__ import annotations

import io
import os
import posixpath
import time
from contextlib import ExitStack
from typing import IO, Any, cast

from ..config import Config
from ..io.zip import append_member
from ..types import ChecksumInfo
from ..url import XRootDURL
from .engine import CopyResult, Progress, _reader, _remove, _target_url

__all__ = ["append_zip"]


def append_zip(
    source: Any,
    archive: str | os.PathLike[str] | XRootDURL,
    *,
    member: str | None = None,
    chunk_size: int | None = None,
    progress: Progress | None = None,
    config: Config | None = None,
    dry_run: bool = False,
    remove_source: bool = False,
) -> CopyResult:
    """Append ``source`` as one stored member of local or remote ``archive``.

    Existing members and the archive comment are preserved byte-for-byte.
    A duplicate member is refused, and an interrupted update is rolled back
    locally or through an XRootD checkpoint before the error is re-raised.
    """
    cfg = config or Config()
    source_url = _target_url(source)
    archive_url = _target_url(archive)
    if archive_url is None:
        raise ValueError("a ZIP archive target must be a path or URL")
    name = member or _source_name(source, source_url)
    with ExitStack() as stack:
        reader, size, source_name = _append_source(source, source_url, cfg, stack)
        if dry_run:
            return CopyResult(source_name, str(archive_url), size, 0.0)
        started = time.monotonic()
        added = append_member(
            archive_url,
            name,
            reader,
            size,
            config=cfg,
            chunk_size=chunk_size or cfg.chunk_size,
            progress=progress,
        )
    if remove_source and source_url is not None:
        _remove(source_url, cfg)
    checksum = ChecksumInfo("zcrc32", f"{added.crc32:08x}")
    return CopyResult(
        source_name,
        str(archive_url),
        size,
        time.monotonic() - started,
        checksum=checksum,
    )


def _append_source(
    source: Any,
    source_url: XRootDURL | None,
    config: Config,
    stack: ExitStack,
) -> tuple[IO[bytes], int, str]:
    """Open an append source and establish its remaining byte count."""
    if source_url is None:
        reader = cast("IO[bytes]", source)
        return reader, _remaining_size(reader), repr(source)
    reader, known = _reader(source_url, config, stack)
    size = known if known is not None else _remaining_size(reader)
    return reader, size, str(source_url)


def _source_name(source: Any, url: XRootDURL | None) -> str:
    if url is not None:
        name = posixpath.basename(url.path.rstrip("/"))
    else:
        raw_name = getattr(source, "name", "")
        name = (
            os.path.basename(os.fsdecode(raw_name))
            if isinstance(raw_name, (str, bytes, os.PathLike))
            else ""
        )
    if not name:
        raise ValueError("member= is required when the source has no file name")
    return name


def _remaining_size(reader: IO[bytes]) -> int:
    try:
        start = reader.tell()
        end = reader.seek(0, io.SEEK_END)
        reader.seek(start)
    except (AttributeError, OSError, io.UnsupportedOperation) as exc:
        raise ValueError("a ZIP append source must have a known size or be seekable") from exc
    return end - start
