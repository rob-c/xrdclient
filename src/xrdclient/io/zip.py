"""Seekable reads of one member inside a local or remote ZIP archive, and appends.

This is the file-object half of XrdCl's ZIP support (``XrdClZipArchive``):
``root://host//a.zip?xrdcl.unzip=f.root``, or :func:`~xrdclient.io.open_url`'s
``member=``, opens one member without downloading the archive. The table of
contents is read by :mod:`xrdclient.client._zip` (a few ranged reads at the end
of the file); this module turns one :class:`~xrdclient.client._zip.ZipMember`
into a raw :mod:`io` layer and stacks the usual buffered/text layers on it
through :func:`xrdclient.io._xrootd_layers`.

:func:`append_member` is the write side :mod:`xrdclient.copy.zip` uses: add one
*stored* (uncompressed) member to an archive in place, so a copy into a ZIP
never rewrites the gigabytes already in it.

Layout reminder (PKWARE APPNOTE.TXT), since every ``struct`` below is one of
these records, little-endian throughout::

    [local file header 1][data 1] ... [local header N][data N]
    [central directory header 1] ... [central directory header N]
    [ZIP64 end-of-central-directory record]   } only when a count, size or
    [ZIP64 end-of-central-directory locator]  } offset overflows 16/32 bits
    [end-of-central-directory record][archive comment]

What a maintainer must not break: a member is verified against the CRC-32
and size its central record declares before the last byte is handed over (a
corrupt or truncated archive raises :class:`ZipArchiveError`, never returns
short data); and an append that fails leaves the archive as it was - by an
XRootD checkpoint remotely, by restoring the overwritten tail locally.
"""

from __future__ import annotations

import io
import stat
import struct
import tempfile
import zlib
from collections.abc import Callable, Generator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
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

#: Compression methods (APPNOTE 4.4.5): 0 is stored as-is, 8 is raw deflate.
#: Nothing else - bzip2, LZMA, zstd - is read; XrdCl reads only these two too.
_STORED = 0
_DEFLATED = 8
#: General-purpose flag bit 0: the member is encrypted. Refused outright, as
#: there is no key to decrypt with and the bytes would be noise.
_ENCRYPTED = 0x0001
#: How much inflated data the seek cache holds in RAM before spilling to a
#: temporary file. A member is often far larger than this.
_CACHE_MEMORY = 64 << 20
#: The 16- and 32-bit "this field overflowed, see the ZIP64 record" markers.
_U16 = 0xFFFF
_U32 = 0xFFFFFFFF
#: General-purpose flag bit 11 (language encoding flag): the name is UTF-8.
#: Every member this module writes sets it, so non-ASCII names survive.
_UTF8 = 0x0800
#: Local file header, 30 bytes: signature ``PK\x03\x04``, version needed,
#: flags, method, DOS time, DOS date, CRC-32, compressed size, uncompressed
#: size, name length, extra length - then the name and the extra field.
_LFH = struct.Struct("<4s5H3I2H")
#: Central directory header, 46 bytes: signature ``PK\x01\x02``, version made
#: by, version needed, flags, method, DOS time, DOS date, CRC-32, compressed
#: size, uncompressed size, name length, extra length, comment length, start
#: disk, internal attributes, external attributes, local header offset.
_CDFH = struct.Struct("<4s6H3I5H2I")
#: End of central directory, 22 bytes: signature ``PK\x05\x06``, this disk,
#: the central directory's disk, entries here, entries in all, directory
#: size, directory offset, comment length.
_EOCD = struct.Struct("<4s4H2IH")
#: ZIP64 end of central directory, 56 bytes: signature ``PK\x06\x06``, size
#: of the rest of the record (44), version made by, version needed, this
#: disk, the directory's disk, entries here, entries in all, directory size,
#: directory offset - the same fields as the classic record, but 64-bit.
_EOCD64 = struct.Struct("<4sQ2H2I4Q")
#: ZIP64 locator, 20 bytes: signature ``PK\x06\x07``, the ZIP64 record's
#: disk, its offset, the number of disks. It sits right before the classic
#: record so a reader that found that can find the 64-bit one.
_LOCATOR = struct.Struct("<4sIQI")
#: An extra-field header: id (``0x0001`` is ZIP64) and length of what follows.
_FIELD = struct.Struct("<HH")


def _plain_cache() -> BinaryIO:
    """A deflated member's seek cache; the member that asked owns and closes it."""
    return cast("BinaryIO", tempfile.SpooledTemporaryFile(max_size=_CACHE_MEMORY, mode="w+b"))


class ZipMemberIO(io.RawIOBase):
    """A read-only, seekable view of one ZIP member.

    Stored members are ranged straight from the archive. Deflated members
    are inflated once, progressively, into a spooled seek cache; backward
    seeks therefore cost no network traffic and large members spill to disk
    instead of consuming unbounded RAM.

    The object owns ``archive`` (the raw layer of the archive file) and closes
    it with itself. Integrity is checked as the data goes by: the CRC-32 and
    size from the central directory are compared once the end of the member
    has been produced, so a reader that reads to the end has read verified
    bytes. A stored member read out of order cannot be checksummed that way
    (the CRC must run over the bytes in order), so verification then simply
    stops rather than reading the whole member behind the caller's back.
    """

    def __init__(self, archive: BinaryIO, member: ZipMember, *, name: str = "") -> None:
        """Wrap ``member`` of the already-open ``archive``; refuse what cannot be read.

        Encrypted members, compression methods other than stored and deflate,
        and a stored member whose two sizes disagree (which can only be a
        damaged record) are refused here, before any data is read. ``name`` is
        what :attr:`name` reports; the default joins the archive's own name
        and the member's.
        """
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
        # Stored members: how far a strictly sequential read has checksummed,
        # and whether the reads have stayed sequential.
        self._verified_until = 0
        self._stored_crc = 0
        self._stored_sequential = True
        # Deflated members: the inflater, how far into the compressed stream
        # it has read, how much plain data the cache holds and its CRC, and
        # input zlib has been handed but not yet consumed.
        self._inflater: Any | None = None
        self._compressed_at = 0
        self._inflated = 0
        self._inflated_crc = 0
        self._compressed_pending = b""
        self._finished = False
        if member.method == _DEFLATED:
            self._cache = _plain_cache()
            # Negative window bits: ZIP holds raw deflate, with no zlib header.
            self._inflater = zlib.decompressobj(-zlib.MAX_WBITS)

    @property
    def name(self) -> str:
        """``<archive URL>#<member>``, credentials stripped, as :func:`open_member` names it."""
        return self._name

    @property
    def mode(self) -> str:
        """Always ``"rb"``: a member can only be read."""
        return "rb"

    def readable(self) -> bool:
        """Always true."""
        return True

    def seekable(self) -> bool:
        """Always true: stored members seek in the archive, deflated ones in the cache."""
        return True

    def writable(self) -> bool:
        """Always false; :func:`append_member` is the only way to write."""
        return False

    def tell(self) -> int:
        """The position in the member's *plain* (inflated) bytes."""
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Move within the plain bytes, as :meth:`io.IOBase.seek` does.

        Only the position moves; nothing is read or inflated until the next
        :meth:`readinto`. Seeking past the end is allowed (reads there return
        nothing), and a negative result is ``EINVAL`` like a local file's.
        """
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
        """Fill ``buffer`` from the position, never past the member's declared size."""
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
        """Range ``count`` bytes straight out of the archive, checksumming in order.

        A short read means the archive ends inside the member - it was
        truncated after its directory was written - and is an error rather
        than a short result, which a buffered reader would take for EOF.
        """
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
            # A gap or a re-read: the running CRC can no longer cover the
            # member in order, so verification is abandoned for this handle.
            self._stored_sequential = False
        return data

    def _read_deflated(self, count: int) -> bytes:
        """Inflate far enough to cover the request, then serve it from the cache."""
        self._inflate_to(self._pos + count)
        cache = cast("BinaryIO", self._cache)
        cache.seek(self._pos)
        return cache.read(count)

    def _inflate_to(self, wanted: int) -> None:
        """Inflate until the cache holds ``wanted`` plain bytes (or the whole member).

        Reaching the member's end is special: the loop then runs on until
        zlib reports the end of the stream, so the trailing checks in
        :meth:`_finish_inflate` - no extra compressed bytes, no extra plain
        bytes, matching CRC - run before the caller gets the last byte.
        """
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
        """Return pending input or range the next bounded block from the archive.

        Blocks are capped at a megabyte so that a small read early in a large
        member costs one modest ranged read, not the whole compressed size.
        """
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
        """Inflate no farther than the caller needs, retaining unused input.

        ``max_length`` bounds the output, which is what keeps a "zip bomb"
        from expanding into memory in one call. At the end of the member the
        bound is one byte *more* than declared, so an over-long stream is
        caught by :meth:`_cache_plain` rather than silently clipped.
        """
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
        """Check the stream ended exactly where the directory said, then verify.

        Any compressed bytes left over - pending, unused after the end marker,
        or not yet read - mean the declared compressed size is wrong, which
        XrdCl also treats as corruption.
        """
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
        """Compare what was produced with the central directory's size and CRC-32."""
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
        """Drop the seek cache and close the archive; safe to call twice."""
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
    """Open ``member_name`` from ``archive_url`` through ranged reads.

    The archive is opened unbuffered (local paths with the builtin, anything
    else through :func:`~xrdclient.io.open_url`) because :class:`ZipMemberIO`
    does its own positioned reads and a buffer underneath would only read
    ahead into the wrong place. ``binary``, ``buffering``, ``encoding``,
    ``errors`` and ``newline`` say what to stack on top, exactly as for any
    other :func:`~xrdclient.io.open_url` result. They stay separate keywords
    because this name is in ``__all__``; inside they travel as one
    :class:`~xrdclient.io._Layers`. This is reached
    through ``open_url(..., member=...)`` or an ``xrdcl.unzip`` query, which
    is where callers should start.

    A member that is not there raises :class:`ZipArchiveError` (XrdCl's own
    wording), and the archive handle is closed on every failure path so a
    bad name does not leak a connection.
    """
    from . import DEFAULT_BUFFER_SIZE, _Layers, _xrootd_layers

    layers = _Layers(binary, buffering, encoding, errors, newline)
    cfg = config or Config()
    archive = _open_archive_for_read(archive_url, cfg, router)
    try:
        size = archive.seek(0, io.SEEK_END)

        def read_at(offset: int, length: int) -> bytes:
            """The :data:`~xrdclient.client._zip.Reader` the directory parser calls."""
            archive.seek(offset)
            return archive.read(length)

        found = find_member(read_at, size, member_name)
        # The name ends up in tracebacks and logs: no query, no password.
        public_url = archive_url.without_query().evolve(username="", password="")
        raw = ZipMemberIO(archive, found, name=f"{public_url}#{member_name}")
    except BaseException:
        archive.close()
        raise
    if layers.buffering == -1:
        layers = replace(layers, buffering=DEFAULT_BUFFER_SIZE)
    return _xrootd_layers(raw, False, layers)


def _open_archive_for_read(url: XRootDURL, config: Config, router: Router | None) -> BinaryIO:
    """The archive, unbuffered: a local path with the builtin, else :func:`open_url`.

    The caller owns the handle and closes it.
    """
    from . import open_url

    if url.is_local:
        return cast("BinaryIO", open(url.path, "rb", buffering=0))
    return cast("BinaryIO", open_url(url, "rb", buffering=0, config=config, router=router))


@dataclass(frozen=True, slots=True)
class _AppendPlan:
    """Everything one :func:`append_member` call writes, validated once.

    ``name`` is ``member_name`` already encoded as it goes in the headers;
    ``source`` is read ``chunk_size`` bytes at a time until ``size`` bytes
    have been copied, and ``progress`` (if any) hears ``(done, size)`` after
    each chunk.
    """

    url: XRootDURL
    member_name: str
    name: bytes
    source: IO[bytes]
    size: int
    chunk_size: int
    progress: Callable[[int, int], None] | None


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

    A missing archive is created (an empty one is a valid start). The member
    is *stored*, not deflated: the payload is usually already compressed
    (ROOT files are), and deflating it would cost CPU for nothing while
    making the member unseekable without a cache. Its name must not already
    be in the archive (:class:`FileExistsError`), and ``source`` must supply
    ``size`` bytes. Only local paths and ``root://`` URLs can be appended to;
    the operation needs in-place writes and truncation that HTTP lacks.

    This signature is used by :mod:`xrdclient.copy.zip` and the tests, so it
    stays as it is; the work below it takes one :class:`_AppendPlan`.
    """
    name = _append_name(member_name, size, chunk_size)
    cfg = config or Config()
    plan = _AppendPlan(archive_url, member_name, name, source, size, chunk_size, progress)
    with _open_archive_for_update(archive_url, cfg, router) as (archive, created):
        return _append_open(plan, archive, created)


def _append_name(member_name: str, size: int, chunk_size: int) -> bytes:
    """Validate append metadata and return its UTF-8 wire name.

    A name is held in a 16-bit length field and NUL would end it early for
    C readers, so both are refused before anything touches the archive.
    """
    if size < 0:
        raise ValueError("a ZIP member size cannot be negative")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    name = member_name.encode("utf-8")
    if not name or b"\x00" in name or len(name) > _U16:
        raise ValueError("a ZIP member name must be 1 to 65535 non-NUL UTF-8 bytes")
    return name


def _append_open(plan: _AppendPlan, archive: BinaryIO, created: bool) -> ZipMember:
    """Append with an already-open archive, rolling back any failed mutation.

    The new member's local header and data are written *over* the old
    central directory (that is what keeps the archive dense), so the old
    directory bytes are read into memory first: they are both the records
    to re-emit and, for a local file, the undo log. A remote archive that
    existed is protected by an XRootD checkpoint (``kXR_chkpoint``) instead,
    which the server rolls back if the block raises; one this call created
    is simply truncated back to nothing.
    """
    archive.seek(0, io.SEEK_END)
    archive_size = archive.tell()

    def read_at(offset: int, length: int) -> bytes:
        """The :data:`~xrdclient.client._zip.Reader` the directory parser calls."""
        archive.seek(offset)
        return archive.read(length)

    central = directory(read_at, archive_size)
    if plan.member_name in {item.name for item in central.members}:
        raise FileExistsError(plan.member_name)
    old_tail = read_at(central.offset, archive_size - central.offset)
    checkpoint = _checkpoint(archive, not created)
    try:
        with checkpoint:
            return _write_member(archive, central, plan)
    except BaseException:
        if plan.url.is_local:
            _restore(archive, central.offset, old_tail, archive_size)
        elif created:
            archive.truncate(0)
        raise


@contextmanager
def _open_archive_for_update(
    url: XRootDURL, config: Config, router: Router | None
) -> Generator[tuple[BinaryIO, bool], None, None]:
    """Yield ``(raw archive, whether it was created)`` for local or XRootD URLs.

    ``r+b`` first and ``w+b`` only on "not found": the reverse would truncate
    an existing archive. The window between the two is a race with another
    creator, which is accepted - two writers appending to one ZIP at once
    would corrupt it regardless.
    """
    if url.is_local:
        archive, created = _open_local_for_update(url.path)
    elif url.is_root:
        archive, created = _open_root_for_update(url, config, router)
    else:
        raise ValueError("ZIP append targets must be local paths or root:// URLs")
    try:
        yield archive, created
    finally:
        archive.close()


def _open_local_for_update(path: str) -> tuple[BinaryIO, bool]:
    """A local archive opened ``r+b``, or created ``w+b``; and whether it was created."""
    try:
        return cast("BinaryIO", open(path, "r+b", buffering=0)), False
    except FileNotFoundError:
        return cast("BinaryIO", open(path, "w+b", buffering=0)), True


def _open_root_for_update(
    url: XRootDURL, config: Config, router: Router | None
) -> tuple[BinaryIO, bool]:
    """A remote archive opened ``r+b``, or created ``w+b``; and whether it was created."""
    from . import open_url

    try:
        return cast(
            "BinaryIO", open_url(url, "r+b", buffering=0, config=config, router=router)
        ), False
    except NotFoundError:
        return cast(
            "BinaryIO", open_url(url, "w+b", buffering=0, config=config, router=router)
        ), True


def _checkpoint(archive: BinaryIO, existing: bool) -> Any:
    """The remote rollback journal when existing archive bytes are overwritten.

    Only an XRootD handle has one, and only an archive that already existed
    has bytes worth journalling; everything else gets a no-op context.
    """
    from .raw import XRootDRawIO

    if existing and isinstance(archive, XRootDRawIO):
        return archive.file.checkpoint()
    return nullcontext()


def _write_member(archive: BinaryIO, central: ZipDirectory, plan: _AppendPlan) -> ZipMember:
    """Write the new member, the directory and the end records; return the member.

    Order matters. The local header goes first with a CRC of zero, because
    the CRC is not known until the data has streamed past; the directory
    after the data carries the real one; the file is truncated to its new
    end (an old, longer tail must not survive past the new end records);
    and only then is the local header rewritten with the real CRC. The data
    descriptor (flag bit 3) would avoid the rewrite, but some readers ignore
    it, and a seekable target makes it unnecessary.
    """
    offset = central.offset
    size = plan.size
    stamp = _dos_time(datetime.now())
    local = _local_header(plan.name, size, 0, stamp)
    archive.seek(offset)
    _write_all(archive, local)
    crc = _copy_payload(archive, plan)
    data_offset = offset + len(local)
    central_offset = data_offset + size
    record = _central_header(plan.name, size, crc, offset, stamp)
    records = central.records + record
    ending = _end_records(central.count + 1, central_offset, len(records), central.comment)
    _write_all(archive, records)
    _write_all(archive, ending)
    final_size = central_offset + len(records) + len(ending)
    archive.truncate(final_size)
    archive.seek(offset)
    _write_all(archive, _local_header(plan.name, size, crc, stamp))
    archive.flush()
    return ZipMember(plan.name.decode(), size, size, offset, _STORED, crc, _UTF8, data_offset)


def _copy_payload(archive: BinaryIO, plan: _AppendPlan) -> int:
    """Stream exactly ``plan.size`` bytes from the source; return their CRC-32.

    A source that ends early is an error (the headers already promise the
    size); one that hands back more than asked for is clipped, so a reader
    that ignores the requested length cannot overrun the member.
    """
    crc = 0
    remaining = plan.size
    done = 0
    while remaining:
        data = plan.source.read(min(plan.chunk_size, remaining))
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
        if plan.progress is not None:
            plan.progress(done, plan.size)
    return crc & _U32


def _write_all(target: BinaryIO, data: bytes) -> None:
    """Write all of ``data`` to a raw (possibly short-writing) file.

    Raw layers may write less than asked; a zero or ``None`` count means the
    target cannot take more, and looping on it would spin forever.
    """
    view = memoryview(data)
    written = 0
    while written < len(view):
        count = target.write(view[written:])
        if count is None or count <= 0:
            raise OSError("ZIP archive write made no progress")
        written += count


def _restore(archive: BinaryIO, offset: int, tail: bytes, size: int) -> None:
    """Best-effort local rollback, preserving the exception that prompted it.

    Puts the old central directory and end records back where they were and
    cuts the file to its old length. A failure here is swallowed on purpose:
    the caller is already propagating the error that matters.
    """
    try:
        archive.seek(offset)
        _write_all(archive, tail)
        archive.truncate(size)
        archive.flush()
    except Exception:
        pass


def _dos_time(now: datetime) -> tuple[int, int]:
    """``now`` as MS-DOS ``(time, date)`` words, the only timestamp ZIP requires.

    Time is ``hour << 11 | minute << 5 | second // 2`` (two-second
    resolution); date is ``(year - 1980) << 9 | month << 5 | day``. The year
    field is seven bits from 1980, so the year is clamped to 1980-2107.
    """
    year = min(max(now.year, 1980), 2107)
    date = ((year - 1980) << 9) | (now.month << 5) | now.day
    time = (now.hour << 11) | (now.minute << 5) | (now.second // 2)
    return time, date


def _zip64_extra(values: list[tuple[bool, int, int]]) -> bytes:
    """The ZIP64 extra field (id ``0x0001``) holding each value marked present.

    APPNOTE 4.5.3 fixes the order - uncompressed size, compressed size,
    local header offset - and includes a value only when its 32-bit field
    holds ``0xFFFFFFFF``; no field at all is written when nothing overflowed.
    """
    body = b"".join(value.to_bytes(width, "little") for present, value, width in values if present)
    return _FIELD.pack(1, len(body)) + body if body else b""


def _local_header(name: bytes, size: int, crc: int, stamp: tuple[int, int]) -> bytes:
    """A stored member's local file header, ZIP64-extended when ``size`` needs it.

    In a *local* header both sizes must be in the ZIP64 field once either is
    (APPNOTE 4.5.3), and stored means they are equal. Version 4.5 is what
    ZIP64 requires a reader to understand; 2.0 is the baseline otherwise.
    """
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
    """The new member's central directory record.

    "Version made by" has the host system in its high byte; 3 is Unix, which
    is what makes the high 16 bits of the external attributes a ``st_mode``
    (here a regular file, ``0644``) that ``unzip`` restores. The size and the
    header offset overflow independently, so each gets its own ZIP64 entry.
    """
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
            0,  # file comment length
            0,  # disk number start: single-disk archives only
            0,  # internal attributes: not flagged as text
            (stat.S_IFREG | 0o644) << 16,
            narrow_offset,
        )
        + name
        + extra
    )


def _end_records(count: int, offset: int, size: int, comment: bytes) -> bytes:
    """The end-of-central-directory record, preceded by ZIP64 ones when needed.

    ``count`` entries, a directory of ``size`` bytes at ``offset``, and the
    archive comment carried over unchanged. When any of the three overflows
    its classic field, the ZIP64 record and locator are written first and
    the classic record's fields are set to their all-ones markers, which is
    how a reader knows to look for them.
    """
    wide = count >= _U16 or offset >= _U32 or size >= _U32
    if not wide:
        return _EOCD.pack(b"PK\x05\x06", 0, 0, count, count, size, offset, len(comment)) + comment
    record_offset = offset + size
    record = _EOCD64.pack(b"PK\x06\x06", 44, 45, 45, 0, 0, count, count, size, offset)
    locator = _LOCATOR.pack(b"PK\x06\x07", 0, record_offset, 1)
    end = _EOCD.pack(b"PK\x05\x06", 0, 0, _U16, _U16, _U32, _U32, len(comment))
    return record + locator + end + comment
