"""The response objects of ``XRootD.client.responses``.

Every one is a :class:`Struct` - a bag of attributes built from a ``dict`` -
because that is what the bindings hand back, and code written against them
reaches for ``info.size`` and ``vars(info)`` and ``repr(info)`` alike. The
attribute names, their types and the ``repr`` are the bindings' own, checked
against them on a live server; see ``tests/test_pyxrootd_compat.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .url import URL

__all__ = [
    "ChunkInfo",
    "DirectoryList",
    "HostInfo",
    "HostList",
    "ListEntry",
    "Location",
    "LocationInfo",
    "ProtocolInfo",
    "StatInfo",
    "StatInfoVFS",
    "Struct",
    "VectorReadInfo",
    "XRootDStatus",
]


class Struct:
    """A ``dict`` turned into attributes, printed the way the bindings print it."""

    def __init__(self, entries: dict[str, Any]) -> None:
        self.__dict__.update(entries)

    def __repr__(self) -> str:
        fields = ", ".join(f"{key}: {value!r}" for key, value in self.__dict__.items())
        return f"<{fields}>"

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self.__dict__ == other.__dict__

    __hash__ = None  # type: ignore[assignment]  # mutable, like the bindings'


class XRootDStatus(Struct):
    """The outcome of a request: ``ok``, ``error``, ``fatal``, ``code``, ``errno``.

    ``code`` is XrdCl's error type (400 for "the server said no") and
    ``errno`` the server's own ``kXR_*`` number, so ported code that compares
    either keeps working. Indexing works as well as attribute access,
    because the bindings hand some statuses out as plain dicts - the ones
    inside an xattr result, and :meth:`FileSystem.cat`'s - and code written
    against those says ``status["ok"]``.
    """

    status: int
    code: int
    errno: int
    message: str
    shellcode: int
    error: bool
    fatal: bool
    ok: bool

    def __str__(self) -> str:
        return self.message

    def __getitem__(self, key: str) -> Any:
        return self.__dict__[key]

    def __contains__(self, key: object) -> bool:
        return key in self.__dict__


class ProtocolInfo(Struct):
    """``version`` and ``hostinfo``, both as the server sent them."""

    version: int
    hostinfo: int


class StatInfo(Struct):
    """What ``kXR_stat`` said about one path, in the bindings' vocabulary.

    ``mode`` is the server's octal string (``"0644"``) and ``modeoctstr`` its
    ``rw-r--r--`` rendering - the names are the bindings', not a mix-up here.
    Both are empty, as are ``owner`` and ``group``, against a server too old
    to send them, and ``extended`` says which case this is.
    """

    id: str
    size: int
    flags: int
    mtime: int
    modtime: int
    modtimestr: str
    ctime: int
    atime: int
    mode: str
    modeoctstr: str
    owner: str
    group: str
    extended: bool
    haschecksum: bool
    checksum: str


class StatInfoVFS(Struct):
    """``kXR_stat`` with ``kXR_vfs``: space across the servers behind a path."""

    nodes_rw: int
    free_rw: int
    utilization_rw: int
    nodes_staging: int
    free_staging: int
    utilization_staging: int


class ListEntry(Struct):
    """One name in a :class:`DirectoryList`; ``statinfo`` is ``None`` without ``STAT``."""

    name: str
    hostaddr: str
    statinfo: StatInfo | None


class DirectoryList(Struct):
    """A listing: ``size`` entries under ``parent``, iterable as :class:`ListEntry`."""

    size: int
    parent: str
    dirlist: list[ListEntry]

    def __iter__(self) -> Iterator[ListEntry]:
        return iter(self.dirlist)


class Location(Struct):
    """One server that has a file: ``address``, ``type``, ``accesstype``."""

    address: str
    type: int
    accesstype: int
    is_manager: bool
    is_server: bool


class LocationInfo(Struct):
    """Every :class:`Location` a locate found; iterable."""

    locations: list[Location]

    def __iter__(self) -> Iterator[Location]:
        return iter(self.locations)


class ChunkInfo(Struct):
    """One piece of a vector read: ``offset``, ``length`` and its ``buffer``."""

    offset: int
    length: int
    buffer: bytes


class VectorReadInfo(Struct):
    """The answer to :meth:`File.vector_read`; iterable as :class:`ChunkInfo`."""

    size: int
    chunks: list[ChunkInfo]

    def __iter__(self) -> Iterator[ChunkInfo]:
        return iter(self.chunks)


class HostInfo(Struct):
    """One server a request passed through on its way to an answer."""

    url: URL
    protocol: int
    flags: int
    load_balancer: bool


class HostList(Struct):
    """The servers a request visited, handed to a callback as its third argument."""

    hosts: list[HostInfo]

    def __iter__(self) -> Iterator[HostInfo]:
        return iter(self.hosts)
