"""Native results in, the bindings' response objects out.

One function per response type, each taking what the native API returned and
building the :class:`~.responses.Struct` the bindings would have built for the
same answer - the same attribute names, the same types, and the same quirks
where ported code will have come to depend on them.
"""

from __future__ import annotations

import stat as _stat
import time
from collections.abc import Iterable, Sequence
from typing import Any, TypeVar

from ... import types as t
from .responses import (
    ChunkInfo,
    DirectoryList,
    HostInfo,
    HostList,
    ListEntry,
    Location,
    LocationInfo,
    ProtocolInfo,
    StatInfo,
    StatInfoVFS,
    Struct,
    VectorReadInfo,
)
from .url import URL

__all__ = [
    "directory_list",
    "host_list",
    "listing",
    "location_info",
    "protocol_info",
    "stat_info",
    "stat_info_vfs",
    "vector_read_info",
]

S = TypeVar("S", bound=Struct)

#: ``LocationInfo::LocationType`` for each letter ``kXR_locate`` answers with.
_LOCATION_TYPES = {"M": 0, "m": 1, "S": 2, "s": 3}


def stat_info(info: t.StatInfo) -> StatInfo:
    """``kXR_stat``'s answer as ``XRootD.client.responses.StatInfo``."""
    mode = info.mode_str
    mtime = info.st_mtime
    text = _UTC.get(mtime)
    if text is None:
        text = _utc(mtime)
    made = _object_new(StatInfo)
    made.__dict__ = {
        "id": info.id,
        "size": info.st_size,
        "flags": int(info.flags),
        "mtime": mtime,
        "modtime": mtime,
        "modtimestr": text,
        "ctime": info.st_ctime,
        "atime": info.st_atime,
        "mode": mode,
        "modeoctstr": _PERMISSIONS.get(mode) or _permissions(mode),
        "owner": info.owner,
        "group": info.group,
        "extended": mode != "",
        "haschecksum": False,
        "checksum": "",
    }
    return made


def _new(cls: type[S], fields: dict[str, Any]) -> S:
    """``cls(fields)`` without the copy: the dict becomes the attributes as it is.

    A listing builds two of these per entry, so skipping ``__init__`` and its
    ``dict.update`` is most of what converting one costs.
    """
    made = _object_new(cls)
    made.__dict__ = fields
    return made


_object_new = object.__new__

#: ``modtimestr`` for each second seen lately. A listing's entries share a
#: handful of times, and formatting one is the dearest thing done per entry.
_UTC: dict[int, str] = {}

#: ``modeoctstr`` for each ``mode`` string seen: a server has few of them.
_PERMISSIONS: dict[str, str] = {}

#: How many of each the caches above keep before starting again.
_CACHE_LIMIT = 4096


def _utc(seconds: int) -> str:
    """XrdCl's ``GetModTimeAsString``: the time in UTC, to the second."""
    year, month, day, hour, minute, second = time.gmtime(seconds)[:6]
    text = f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}"
    if len(_UTC) >= _CACHE_LIMIT:
        _UTC.clear()
    _UTC[seconds] = text
    return text


def _permissions(mode: str) -> str:
    """``modeoctstr`` for the octal ``mode``, empty without one.

    ``filemode`` spells the type in its first letter; XrdCl leaves the type
    out and prints the nine permission letters alone.
    """
    text = _stat.filemode(int(mode, 8))[1:] if mode else ""
    if len(_PERMISSIONS) >= _CACHE_LIMIT:
        _PERMISSIONS.clear()
    _PERMISSIONS[mode] = text
    return text


def stat_info_vfs(info: t.VFSInfo) -> StatInfoVFS:
    """``kXR_stat`` with ``kXR_vfs`` as ``StatInfoVFS``."""
    return StatInfoVFS(
        {
            "nodes_rw": info.nodes_rw,
            "free_rw": info.free_rw,
            "utilization_rw": info.utilization_rw,
            "nodes_staging": info.nodes_staging,
            "free_staging": info.free_staging,
            "utilization_staging": info.utilization_staging,
        }
    )


def directory_list(
    parent: str, entries: Iterable[tuple[str, t.StatInfo | None]], hostaddr: str
) -> DirectoryList:
    """A listing, with ``parent`` spelled the way XrdCl spells it: with a slash."""
    listed = [
        _new(
            ListEntry,
            {
                "hostaddr": hostaddr,
                "name": name,
                "statinfo": stat_info(info) if info is not None else None,
            },
        )
        for name, info in entries
    ]
    return _listing(parent, listed)


def listing(parent: str, entries: Iterable[t.DirEntry], hostaddr: str) -> DirectoryList:
    """:func:`directory_list` straight from native ``scandir`` entries."""
    listed: list[ListEntry] = []
    append = listed.append
    for entry in entries:
        info = entry.stat
        made = _object_new(ListEntry)
        made.__dict__ = {
            "hostaddr": hostaddr,
            "name": entry.name,
            "statinfo": stat_info(info) if info is not None else None,
        }
        append(made)
    return _listing(parent, listed)


def _listing(parent: str, listed: list[ListEntry]) -> DirectoryList:
    folder = parent if parent.endswith("/") else f"{parent}/"
    return _new(DirectoryList, {"size": len(listed), "parent": folder, "dirlist": listed})


def location_info(locations: Sequence[t.LocationInfo]) -> LocationInfo:
    """``kXR_locate``'s answer, with XrdCl's numbers for the type and access."""
    return LocationInfo({"locations": [_location(each) for each in locations]})


def _location(location: t.LocationInfo) -> Location:
    return Location(
        {
            "address": location.address,
            "type": _LOCATION_TYPES.get(location.type, 2),
            "accesstype": 1 if location.is_writable else 0,
            "is_server": location.is_server,
            "is_manager": location.is_manager,
        }
    )


def protocol_info(info: t.ProtocolInfo) -> ProtocolInfo:
    """``kXR_protocol``'s answer, as the bindings report it.

    The bindings hand ``version`` and ``hostinfo`` back with their four bytes
    in reverse order - ``0x520``, protocol 5.2.0, comes out as ``0x20050000``
    - and code written against them compares those numbers. They are
    reproduced exactly; :meth:`xrdclient.FileSystem.protocol` has the values
    the right way round.
    """
    return ProtocolInfo({"version": _swapped(info.version), "hostinfo": _swapped(info.flags)})


def _swapped(value: int) -> int:
    return int.from_bytes((value & 0xFFFFFFFF).to_bytes(4, "big"), "little")


def host_list(url: str, info: t.ProtocolInfo | None) -> HostList:
    """The servers a request visited; the one that answered, here."""
    version = info.version if info is not None else 0
    flags = info.flags if info is not None else 0
    host = HostInfo({"url": URL(url), "protocol": version, "flags": flags, "load_balancer": False})
    return HostList({"hosts": [host]})


def vector_read_info(ranges: Sequence[tuple[int, int]], data: Sequence[bytes]) -> VectorReadInfo:
    """``kXR_readv``'s answer: each chunk with the offset it was asked for."""
    chunks = [
        ChunkInfo({"offset": offset, "length": len(buffer), "buffer": buffer})
        for (offset, _), buffer in zip(ranges, data)
    ]
    return VectorReadInfo({"size": sum(len(buffer) for buffer in data), "chunks": chunks})
