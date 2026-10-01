"""Seekable reads of one member inside a local or remote ZIP archive."""

from __future__ import annotations

import io
import stat
import struct
import tempfile
import zlib
from collections.abc import Callable, Generator
from contextlib import contextmanager, nullcontext
from datetime import datetime
from typing import IO, TYPE_CHECKING, Any, BinaryIO, cast

from ..client._zip import ZipArchiveError, ZipDirectory, ZipMember, directory
from ..client._zip import member as find_member
from ..config import Config
from ..errors import NotFoundError
from ..session.router import Router
from ..url import XRootDURL

if TYPE_CHECKING:
    from _typeshed import WriteableBuffer

__all__ = ["ZipMemberIO", "append_member", "open_member"]

_STORED = 0
_DEFLATED = 8
_ENCRYPTED = 0x0001
_CACHE_MEMORY = 64 << 20
_U16 = 0xFFFF
_U32 = 0xFFFFFFFF
_UTF8 = 0x0800
_LFH = struct.Struct("<4s5H3I2H")
_CDFH = struct.Struct("<4s6H3I5H2I")
_EOCD = struct.Struct("<4s4H2IH")
_EOCD64 = struct.Struct("<4sQ2H2I4Q")
_LOCATOR = struct.Struct("<4sIQI")
_FIELD = struct.Struct("<HH")


class ZipMemberIO(io.RawIOBase):
    """A read-only, seekable view of one ZIP member.

    Stored members are ranged straight from the archive. Deflated members
    are inflated once, progressively, into a spooled seek cache; backward
    seeks therefore cost no network traffic and large members spill to disk
    instead of consuming unbounded RAM.
    """

    def __init__(self, archive: BinaryIO, member: ZipMember, *, name: str = "") -> None:
        super().__init__()
        # ``RawIOBase`` may call ``close`` while finalising an object whose
        # constructor raised, so the resources it needs exist before any
        # metadata validation can fail.
        self._archive = archive
        self._cache: BinaryIO | None = None
        if member.flags & _ENCRYPTED:
            raise ZipArchiveError(f"ZIP member {member.name!r} is encrypted")
        if member.method not in (_STORED, _DEFLATED):
            raise ZipArchiveError(
                f"ZIP member {member.name!r} uses unsupported compression method {member.method}"
            )
        if member.method == _STORED and member.compressed_size != member.size:
            raise ZipArchiveError(
                f"stored ZIP member {member.name!r} has different compressed and plain sizes"
            )
        self._member = member
        self._name = name or f"{getattr(archive, 'name', '<archive>')}#{member.name}"
        self._pos = 0
        self._verified_until = 0
        self._stored_crc = 0
        self._stored_sequential = True
        self._inflater: Any | None = None
        self._compressed_at = 0
        self._inflated = 0
        self._inflated_crc = 0
        self._compressed_pending = b""
        self._finished = False
        if member.method == _DEFLATED:
            self._cache = cast(
                "BinaryIO", tempfile.SpooledTemporaryFile(max_size=_CACHE_MEMORY, mode="w+b")
            )
            self._inflater = zlib.decompressobj(-zlib.MAX_WBITS)

    @property
    def name(self) -> str:
        return self._name

    @property
    def mode(self) -> str:
        return "rb"

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._pos + offset
        elif whence == io.SEEK_END:
            target = self._member.size + offset
        else:
            raise ValueError(f"invalid whence {whence!r}")
        if target < 0:
            raise OSError(22, "negative seek position", self.name)
        self._pos = target
        return target

    def readinto(self, buffer: WriteableBuffer) -> int:
        self._checkClosed()
        view = memoryview(buffer).cast("B")
        count = min(len(view), max(self._member.size - self._pos, 0))
        if count <= 0:
            return 0
        data = (
            self._read_stored(count)
            if self._member.method == _STORED
            else self._read_deflated(count)
        )
        view[: len(data)] = data
        self._pos += len(data)
        return len(data)

    def _read_stored(self, count: int) -> bytes:
        member = self._member
        self._archive.seek(member.data_offset + self._pos)
        data = self._archive.read(count)
        if len(data) != count:
            raise ZipArchiveError(f"ZIP member {member.name!r} ended before its stored size")
        if self._stored_sequential and self._pos == self._verified_until:
            self._stored_crc = zlib.crc32(data, self._stored_crc)
            self._verified_until += len(data)
            if self._verified_until == member.size:
                self._verify(self._stored_crc, self._verified_until)
        else:
            self._stored_sequential = False
        return data

    def _read_deflated(self, count: int) -> bytes:
        self._inflate_to(self._pos + count)
        cache = cast("BinaryIO", self._cache)
        cache.seek(self._pos)
        return cache.read(count)

    def _inflate_to(self, wanted: int) -> None:
        member = self._member
        inflater = self._inflater
        assert inflater is not None
        target = min(wanted, member.size)
        while not self._finished and (self._inflated < target or target == member.size):
            if inflater.eof:
                self._finish_inflate()
                break
            compressed = self._next_compressed()
            plain = self._decompress(compressed, target)
            self._cache_plain(plain)

    def _next_compressed(self) -> bytes:
        """Return pending input or range the next bounded block from the archive."""
        if self._compressed_pending:
            return self._compressed_pending
        member = self._member
        remaining = member.compressed_size - self._compressed_at
        if remaining <= 0:
            raise ZipArchiveError(f"ZIP member {member.name!r} has an incomplete deflate stream")
        self._archive.seek(member.data_offset + self._compressed_at)
        compressed = self._archive.read(min(1 << 20, remaining))
        if not compressed:
            raise ZipArchiveError(f"ZIP member {member.name!r} ended before its compressed size")
        self._compressed_at += len(compressed)
        return compressed

    def _decompress(self, compressed: bytes, target: int) -> bytes:
        """Inflate no farther than the caller needs, retaining unused input."""
        inflater = self._inflater
        assert inflater is not None
        limit = max(target - self._inflated, 1)
        if target == self._member.size:
            limit = max(self._member.size - self._inflated + 1, 1)
        try:
            plain = inflater.decompress(compressed, limit)
        except zlib.error as exc:
            raise ZipArchiveError(f"ZIP member {self._member.name!r} is corrupt: {exc}") from None
        self._compressed_pending = inflater.unconsumed_tail
        return cast("bytes", plain)

    def _cache_plain(self, plain: bytes) -> None:
        """Append inflated bytes to the seek cache and its running CRC."""
        cache = cast("BinaryIO", self._cache)
        cache.seek(self._inflated)
        cache.write(plain)
        self._inflated += len(plain)
        self._inflated_crc = zlib.crc32(plain, self._inflated_crc)
        if self._inflated > self._member.size:
            raise ZipArchiveError(
                f"ZIP member {self._member.name!r} expands beyond its declared size"
            )

    def _finish_inflate(self) -> None:
        if self._finished:
            return
        inflater = self._inflater
        assert inflater is not None
        cache = cast("BinaryIO", self._cache)
        member = self._member
        if (
            not inflater.eof
            or self._compressed_pending
            or inflater.unused_data
            or self._compressed_at != member.compressed_size
        ):
            raise ZipArchiveError(
                f"ZIP member {member.name!r} has an invalid compressed size or deflate stream"
            )
        # Reaching ``eof`` above means zlib accepted the complete raw stream;
        # flushing that valid terminal state cannot consume further input.
        tail = inflater.flush()
        cache.seek(self._inflated)
        cache.write(tail)
        self._inflated += len(tail)
        self._inflated_crc = zlib.crc32(tail, self._inflated_crc)
        self._finished = True
        self._verify(self._inflated_crc, self._inflated)

    def _verify(self, crc: int, length: int) -> None:
        if length != self._member.size:
            raise ZipArchiveError(
                f"ZIP member {self._member.name!r} size mismatch: expected "
                f"{self._member.size}, got {length}"
            )
        if crc & 0xFFFFFFFF != self._member.crc32:
            raise ZipArchiveError(
                f"ZIP member {self._member.name!r} CRC32 mismatch: expected "
                f"{self._member.crc32:08x}, got {crc & 0xFFFFFFFF:08x}"
            )

    def close(self) -> None:
        if self.closed:
            return
        try:
            if self._cache is not None:
                self._cache.close()
            self._archive.close()
        finally:
            super().close()


def open_member(
    archive_url: XRootDURL,
    member_name: str,
    *,
    binary: bool,
    buffering: int,
    encoding: str | None,
    errors: str | None,
    newline: str | None,
    config: Config | None,
    router: Router | None,
) -> IO[Any] | io.RawIOBase:
    """Open ``member_name`` from ``archive_url`` through ranged reads."""
    from . import DEFAULT_BUFFER_SIZE, _xrootd_layers, open_url

    cfg = config or Config()
    if archive_url.is_local:
        archive = cast("BinaryIO", open(archive_url.path, "rb", buffering=0))
    else:
        archive = cast(
            "BinaryIO",
            open_url(archive_url, "rb", buffering=0, config=cfg, router=router),
        )
    try:
        size = archive.seek(0, io.SEEK_END)

        def read_at(offset: int, length: int) -> bytes:
            archive.seek(offset)
            return archive.read(length)

        found = find_member(read_at, size, member_name)
        public_url = archive_url.without_query().evolve(username="", password="")
        raw = ZipMemberIO(archive, found, name=f"{public_url}#{member_name}")
    except BaseException:
        archive.close()
        raise
    return _xrootd_layers(
        raw,
        binary,
        False,
        buffering if buffering != -1 else DEFAULT_BUFFER_SIZE,
        encoding,
        errors,
        newline,
    )


def append_member(
    archive_url: XRootDURL,
    member_name: str,
    source: IO[bytes],
    size: int,
    *,
    config: Config | None = None,
    router: Router | None = None,
    chunk_size: int = 8 << 20,
    progress: Callable[[int, int], None] | None = None,
) -> ZipMember:
    """Append one stored member without downloading or rewriting archive data.

    Only the old central directory is held in memory. The payload streams
    straight into its place, then the preserved records and one new record
    are written after it. Existing remote archives use an XRootD checkpoint,
    while local failures restore the original tail before they escape.
    """
    name = _append_name(member_name, size, chunk_size)
    cfg = config or Config()
    with _open_archive_for_update(archive_url, cfg, router) as (archive, created):
        return _append_open(
            archive_url,
            archive,
            created,
            member_name,
            name,
            source,
            size,
            chunk_size,
            progress,
        )


def _append_name(member_name: str, size: int, chunk_size: int) -> bytes:
    """Validate append metadata and return its UTF-8 wire name."""
    if size < 0:
        raise ValueError("a ZIP member size cannot be negative")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    name = member_name.encode("utf-8")
    if not name or b"\x00" in name or len(name) > _U16:
        raise ValueError("a ZIP member name must be 1 to 65535 non-NUL UTF-8 bytes")
    return name


def _append_open(
    archive_url: XRootDURL,
    archive: BinaryIO,
    created: bool,
    member_name: str,
    name: bytes,
    source: IO[bytes],
    size: int,
    chunk_size: int,
    progress: Callable[[int, int], None] | None,
) -> ZipMember:
    """Append with an already-open archive, rolling back any failed mutation."""
    archive.seek(0, io.SEEK_END)
    archive_size = archive.tell()

    def read_at(offset: int, length: int) -> bytes:
        archive.seek(offset)
        return archive.read(length)

    central = directory(read_at, archive_size)
    if member_name in {item.name for item in central.members}:
        raise FileExistsError(member_name)
    old_tail = read_at(central.offset, archive_size - central.offset)
    checkpoint = _checkpoint(archive, not created)
    try:
        with checkpoint:
            return _write_member(
                archive,
                central,
                source,
                size,
                name,
                chunk_size=chunk_size,
                progress=progress,
            )
    except BaseException:
        if archive_url.is_local:
            _restore(archive, central.offset, old_tail, archive_size)
        elif created:
            archive.truncate(0)
        raise


@contextmanager
def _open_archive_for_update(
    url: XRootDURL, config: Config, router: Router | None
) -> Generator[tuple[BinaryIO, bool], None, None]:
    """Yield ``(raw archive, whether it was created)`` for local or XRootD URLs."""
    from . import open_url

    created = False
    if url.is_local:
        try:
            archive = cast("BinaryIO", open(url.path, "r+b", buffering=0))
        except FileNotFoundError:
            archive = cast("BinaryIO", open(url.path, "w+b", buffering=0))
            created = True
    elif url.is_root:
        try:
            archive = cast(
                "BinaryIO", open_url(url, "r+b", buffering=0, config=config, router=router)
            )
        except NotFoundError:
            archive = cast(
                "BinaryIO", open_url(url, "w+b", buffering=0, config=config, router=router)
            )
            created = True
    else:
        raise ValueError("ZIP append targets must be local paths or root:// URLs")
    try:
        yield archive, created
    finally:
        archive.close()


def _checkpoint(archive: BinaryIO, existing: bool) -> Any:
    """The remote rollback journal when existing archive bytes are overwritten."""
    from .raw import XRootDRawIO

    if existing and isinstance(archive, XRootDRawIO):
        return archive.file.checkpoint()
    return nullcontext()


def _write_member(
    archive: BinaryIO,
    central: ZipDirectory,
    source: IO[bytes],
    size: int,
    name: bytes,
    *,
    chunk_size: int,
    progress: Callable[[int, int], None] | None,
) -> ZipMember:
    offset = central.offset
    stamp = _dos_time(datetime.now())
    local = _local_header(name, size, 0, stamp)
    archive.seek(offset)
    _write_all(archive, local)
    crc = 0
    remaining = size
    done = 0
    while remaining:
        data = source.read(min(chunk_size, remaining))
        if not data:
            raise ZipArchiveError(
                f"source ended with {remaining} bytes still expected for ZIP member"
            )
        if len(data) > remaining:
            data = data[:remaining]
        _write_all(archive, data)
        crc = zlib.crc32(data, crc)
        remaining -= len(data)
        done += len(data)
        if progress is not None:
            progress(done, size)
    crc &= _U32
    data_offset = offset + len(local)
    central_offset = data_offset + size
    record = _central_header(name, size, crc, offset, stamp)
    records = central.records + record
    ending = _end_records(central.count + 1, central_offset, len(records), central.comment)
    _write_all(archive, records)
    _write_all(archive, ending)
    final_size = central_offset + len(records) + len(ending)
    archive.truncate(final_size)
    archive.seek(offset)
    _write_all(archive, _local_header(name, size, crc, stamp))
    archive.flush()
    return ZipMember(name.decode(), size, size, offset, _STORED, crc, _UTF8, data_offset)


def _write_all(target: BinaryIO, data: bytes) -> None:
    view = memoryview(data)
    written = 0
    while written < len(view):
        count = target.write(view[written:])
        if count is None or count <= 0:
            raise OSError("ZIP archive write made no progress")
        written += count


def _restore(archive: BinaryIO, offset: int, tail: bytes, size: int) -> None:
    """Best-effort local rollback, preserving the exception that prompted it."""
    try:
        archive.seek(offset)
        _write_all(archive, tail)
        archive.truncate(size)
        archive.flush()
    except Exception:
        pass


def _dos_time(now: datetime) -> tuple[int, int]:
    year = min(max(now.year, 1980), 2107)
    date = ((year - 1980) << 9) | (now.month << 5) | now.day
    time = (now.hour << 11) | (now.minute << 5) | (now.second // 2)
    return time, date


def _zip64_extra(values: list[tuple[bool, int, int]]) -> bytes:
    body = b"".join(value.to_bytes(width, "little") for present, value, width in values if present)
    return _FIELD.pack(1, len(body)) + body if body else b""


def _local_header(name: bytes, size: int, crc: int, stamp: tuple[int, int]) -> bytes:
    wide = size >= _U32
    extra = _zip64_extra([(wide, size, 8), (wide, size, 8)])
    narrow = _U32 if wide else size
    version = 45 if wide else 20
    return (
        _LFH.pack(
            b"PK\x03\x04",
            version,
            _UTF8,
            _STORED,
            *stamp,
            crc,
            narrow,
            narrow,
            len(name),
            len(extra),
        )
        + name
        + extra
    )


def _central_header(name: bytes, size: int, crc: int, offset: int, stamp: tuple[int, int]) -> bytes:
    wide_size, wide_offset = size >= _U32, offset >= _U32
    extra = _zip64_extra([(wide_size, size, 8), (wide_size, size, 8), (wide_offset, offset, 8)])
    version = 45 if extra else 20
    narrow_size = _U32 if wide_size else size
    narrow_offset = _U32 if wide_offset else offset
    return (
        _CDFH.pack(
            b"PK\x01\x02",
            (3 << 8) | version,
            version,
            _UTF8,
            _STORED,
            *stamp,
            crc,
            narrow_size,
            narrow_size,
            len(name),
            len(extra),
            0,
            0,
            0,
            (stat.S_IFREG | 0o644) << 16,
            narrow_offset,
        )
        + name
        + extra
    )


def _end_records(count: int, offset: int, size: int, comment: bytes) -> bytes:
    wide = count >= _U16 or offset >= _U32 or size >= _U32
    if not wide:
        return _EOCD.pack(b"PK\x05\x06", 0, 0, count, count, size, offset, len(comment)) + comment
    record_offset = offset + size
    record = _EOCD64.pack(b"PK\x06\x06", 44, 45, 45, 0, 0, count, count, size, offset)
    locator = _LOCATOR.pack(b"PK\x06\x07", 0, record_offset, 1)
    end = _EOCD.pack(b"PK\x05\x06", 0, 0, _U16, _U16, _U32, _U32, len(comment))
    return record + locator + end + comment
