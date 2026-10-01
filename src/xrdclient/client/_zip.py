"""The table of contents of a ZIP archive, read out of a remote file.

What XrdCl's ``DirListFlags::Zip`` does (``XrdClZipArchive.cc``): read the
end of the file for the end-of-central-directory record, follow it - through
the ZIP64 locator when there is one - to the central directory, and parse
that. Only those two or three ranged reads cross the network; the members
themselves are never touched.

The checks, and the words for what fails them, are XrdCl's own, so an archive
it calls corrupt is corrupt here too, for the same reason.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass

from ..errors import XRootDError

__all__ = ["ZipArchiveError", "ZipDirectory", "ZipMember", "directory", "member", "members"]


class ZipArchiveError(XRootDError):
    """A file read as a ZIP archive is not one, or is a damaged one."""


@dataclass(frozen=True)
class ZipMember:
    """One central directory record: the name as stored, and the sizes."""

    name: str
    #: What the member holds once inflated - the size a listing reports.
    size: int
    compressed_size: int = 0
    offset: int = 0
    method: int = 0
    crc32: int = 0
    flags: int = 0
    data_offset: int = 0


@dataclass(frozen=True)
class ZipDirectory:
    """The intact central directory and the members parsed from it."""

    offset: int
    size: int
    count: int
    records: bytes
    comment: bytes
    members: tuple[ZipMember, ...]


#: ``read(offset, length)``: the bytes of the archive there.
Reader = Callable[[int, int], bytes]

_EOCD = struct.Struct("<4s4H2IH")  # the end-of-central-directory record
_LOCATOR = struct.Struct("<4sIQI")  # the ZIP64 end-of-central-directory locator
_EOCD64 = struct.Struct("<4sQ2H2I4Q")  # the ZIP64 end-of-central-directory record
_CDFH = struct.Struct("<4s6H3I5H2I")  # a central directory file header
_LFH = struct.Struct("<4s5H3I2H")  # the member's local file header
_FIELD = struct.Struct("<HH")  # an extra field's id and length

_EOCD_SIG = b"PK\x05\x06"
_LOCATOR_SIG = b"PK\x06\x07"
_EOCD64_SIG = b"PK\x06\x06"
_CDFH_SIG = b"PK\x01\x02"
_LFH_SIG = b"PK\x03\x04"
_ZIP64_FIELD = 0x0001
_UTF8 = 0x0800

#: How much of the end of the archive the first read takes: the record, the
#: longest comment it can carry, and the ZIP64 locator in front of it.
_TAIL = _EOCD.size + 0xFFFF + _LOCATOR.size

_NO_EOCD = "End-of-central-directory signature not found."
_BAD_EOCD = "End-of-central-directory signature corrupted."
_NO_EOCD64 = "ZIP64 End-of-central-directory signature not found."
_BAD_CD = "ZIP Central Directory corrupted."

_U16, _U32 = 0xFFFF, 0xFFFFFFFF

#: A header's narrow values that can overflow into the ZIP64 field - the
#: sizes, the offset and the disk - the value that says one did, and how
#: wide it is there.
_LIMITS = (_U32, _U32, _U32, _U16)
_WIDTHS = (8, 8, 8, 4)


@dataclass(frozen=True)
class _Directory:
    """Where the central directory is, and how many records it holds."""

    count: int
    size: int
    offset: int


def members(read: Reader, size: int) -> list[ZipMember]:
    """The archive's members, in central directory order.

    ``size`` is the archive's length. An empty file is an archive with
    nothing in it, as XrdCl has it; anything else without an intact central
    directory raises :class:`ZipArchiveError`.
    """
    return list(directory(read, size).members)


def directory(read: Reader, size: int) -> ZipDirectory:
    """Read the archive's central directory, preserving its exact records."""
    if not size:
        return ZipDirectory(0, 0, 0, b"", b"", ())
    start = size - min(size, _TAIL)
    tail = read(start, size - start)
    at = _find_eocd(tail)
    located = _eocd(tail, at)
    comment_at = at + _EOCD.size
    comment = tail[comment_at : comment_at + _EOCD.unpack_from(tail, at)[-1]]
    # A ZIP writer may use ZIP64 before it strictly has to.  Follow the
    # locator even when the whole (small) archive fitted in the tail read.
    located = _zip64(read, tail, start, at) or located
    if located.offset + located.size > size:
        raise ZipArchiveError(_BAD_EOCD)
    if len(tail) == size:
        # The whole archive came back: the directory is already here.
        records = tail[located.offset : located.offset + located.size]
    else:
        records = read(located.offset, located.size)
    found = tuple(_records(records, located.count, size))
    return ZipDirectory(located.offset, located.size, located.count, records, comment, found)


def member(read: Reader, size: int, name: str) -> ZipMember:
    """The member called ``name``, including where its compressed bytes begin."""
    found = next((item for item in members(read, size) if item.name == name), None)
    if found is None:
        raise KeyError(name)
    header = read(found.offset, _LFH.size)
    if len(header) != _LFH.size:
        raise ZipArchiveError(_BAD_CD)
    fields = _LFH.unpack(header)
    if fields[0] != _LFH_SIG or fields[2] != found.flags or fields[3] != found.method:
        raise ZipArchiveError(_BAD_CD)
    name_len, extra_len = fields[9:11]
    data_offset = found.offset + _LFH.size + name_len + extra_len
    if data_offset + found.compressed_size > size:
        raise ZipArchiveError(_BAD_CD)
    return ZipMember(
        found.name,
        found.size,
        found.compressed_size,
        found.offset,
        found.method,
        found.crc32,
        found.flags,
        data_offset,
    )


def _find_eocd(tail: bytes) -> int:
    """Where the last end-of-central-directory signature starts."""
    at = tail.rfind(_EOCD_SIG, 0, max(len(tail) - _EOCD.size + len(_EOCD_SIG), 0))
    if at < 0:
        raise ZipArchiveError(_NO_EOCD)
    return at


def _eocd(tail: bytes, at: int) -> _Directory:
    fields = _EOCD.unpack_from(tail, at)
    count, cd_size, cd_offset, comment = fields[4:]
    if _EOCD.size + comment > len(tail) - at:
        raise ZipArchiveError(_BAD_EOCD)
    return _Directory(count, cd_size, cd_offset)


def _zip64(read: Reader, tail: bytes, start: int, at: int) -> _Directory | None:
    """The ZIP64 record's directory, when a locator stands before the record."""
    locator = at - _LOCATOR.size
    if locator <= 0 or tail[locator : locator + 4] != _LOCATOR_SIG:
        return None
    offset = _LOCATOR.unpack_from(tail, locator)[2]
    if start > offset:
        # The record is further back than the first read went.
        record = read(offset, _EOCD64.size)
    else:
        record = tail[offset - start :]
    if len(record) < _EOCD64.size or record[:4] != _EOCD64_SIG:
        raise ZipArchiveError(_NO_EOCD64)
    fields = _EOCD64.unpack_from(record)
    return _Directory(fields[7], fields[8], fields[9])


def _records(data: bytes, count: int, size: int) -> list[ZipMember]:
    """Parse ``count`` headers, and check them against the archive's ``size``."""
    found: list[ZipMember] = []
    packed = 0
    pos = 0
    for _ in range(count):
        if len(data) - pos < _CDFH.size:
            break  # XrdCl stops at the end of the directory, whatever it claimed
        member, compressed, pos = _record(data, pos, size)
        found.append(member)
        packed += compressed
    if packed > size:
        raise ZipArchiveError(_BAD_CD)
    return found


def _record(data: bytes, pos: int, size: int) -> tuple[ZipMember, int, int]:
    """One header at ``pos``: the member, its compressed size, and where the next starts."""
    fields = _CDFH.unpack_from(data, pos)
    if fields[0] != _CDFH_SIG:
        raise ZipArchiveError(_BAD_CD)
    flags, method, crc32 = fields[3], fields[4], fields[7]
    compressed, uncompressed, name_len, extra_len, comment_len = fields[8:13]
    disk, offset = fields[13], fields[16]
    end = pos + _CDFH.size + name_len + extra_len + comment_len
    if end > len(data):
        raise ZipArchiveError(_BAD_CD)
    name = data[pos + _CDFH.size : pos + _CDFH.size + name_len]
    extra = data[pos + _CDFH.size + name_len : pos + _CDFH.size + name_len + extra_len]
    wide = _wide_values(extra, (uncompressed, compressed, offset, disk))
    if wide is not None:
        uncompressed, compressed, offset, disk = wide
    if offset + compressed > size:
        raise ZipArchiveError(_BAD_CD)
    return (
        ZipMember(
            name.decode("utf-8" if flags & _UTF8 else "cp437", "replace"),
            uncompressed,
            compressed,
            offset,
            method,
            crc32,
            flags,
        ),
        compressed,
        end,
    )


def _wide_values(
    extra: bytes, narrow: tuple[int, int, int, int]
) -> tuple[int, int, int, int] | None:
    """Resolve every narrow value that overflowed into the ZIP64 field.

    Only the values the header overflowed are in it, in the order the format
    lays them out: uncompressed size, compressed size, offset, then disk.
    """
    present = [value == limit for value, limit in zip(narrow, _LIMITS)]
    expected = sum(width for width, there in zip(_WIDTHS, present) if there)
    field = _find_field(extra) if expected else None
    if field is None:
        return None
    if len(field) != expected:
        raise ZipArchiveError(_BAD_CD)
    return _replace_wide(narrow, present, field)


def _replace_wide(
    narrow: tuple[int, int, int, int], present: list[bool], field: bytes
) -> tuple[int, int, int, int]:
    """Replace the overflow sentinels in one central-directory record."""
    resolved = list(narrow)
    pos = 0
    for index, (there, width) in enumerate(zip(present, _WIDTHS)):
        if there:
            resolved[index] = int.from_bytes(field[pos : pos + width], "little")
            pos += width
    return resolved[0], resolved[1], resolved[2], resolved[3]


def _find_field(extra: bytes) -> bytes | None:
    """The body of the ZIP64 extended information field, if the header has one."""
    pos = 0
    while pos + _FIELD.size <= len(extra):
        kind, length = _FIELD.unpack_from(extra, pos)
        if kind == _ZIP64_FIELD:
            return extra[pos + _FIELD.size : pos + _FIELD.size + length]
        pos += _FIELD.size + length
    return None
