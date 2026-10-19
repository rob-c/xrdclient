"""Public value types.

All frozen and slotted. ``StatInfo`` deliberately mirrors ``os.stat_result``
field names so code that inspects a local stat works unchanged on a remote one.

These are what the protocol parsers in :mod:`xrdclient.proto.responses`
build and what every scheme (``root://``, WebDAV, S3) returns, so a field
here is a promise all three keep. The ``kXR_*`` names in the docstrings are
the XRootD protocol's own, for looking a flag up in ``XProtocol.hh``. The
pyxrootd-compatible shapes live in :mod:`xrdclient.compat` and are converted
from these; adding a field here never changes them. :class:`ChecksumInfo` is
:class:`xrdcore.checksum.ChecksumInfo` itself, the one checksum type xrdhttp
(Metalink, WebDAV) and this package share, so checksums from any protocol
compare equal.
"""

from __future__ import annotations

import datetime
import stat as _stat
from dataclasses import dataclass, field

from xrdcore.checksum import ChecksumInfo

from .flags import StatInfoFlags

__all__ = [
    "CheckpointInfo",
    "ChecksumInfo",
    "CloneRange",
    "DirEntry",
    "LocationInfo",
    "PageResult",
    "PrepareStatus",
    "ProtocolInfo",
    "ReadRange",
    "SpaceInfo",
    "StatInfo",
    "VFSInfo",
    "WriteChunk",
    "human_bytes",
]


def human_bytes(size: float) -> str:
    """A byte count the way a person reads it: ``human_bytes(1536)`` is 1.5 KiB.

    Powers of 1024, because that is what storage elements report and what
    ``ls -lh`` prints; a petabyte-scale number keeps its units rather than
    running off the end.
    """
    value = float(size)
    if abs(value) < 1024:
        return f"{value:.0f} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024
        if abs(value) < 1024:
            return f"{value:.1f} {unit}"
    return f"{value / 1024:.1f} PiB"


@dataclass(frozen=True, slots=True)
class StatInfo:
    """Result of a ``stat``. ``st_*`` names match ``os.stat_result``.

    Printing one gives the line ``ls -l`` would have printed::

        >>> print(StatInfo(st_size=1536, flags=StatInfoFlags.IS_READABLE))
        -r--r--r--    1.5 KiB  -
    """

    id: str = ""
    st_size: int = 0
    flags: StatInfoFlags = StatInfoFlags.NONE
    st_mtime: int = 0
    st_ctime: int = 0
    st_atime: int = 0
    path: str = ""
    mode_str: str = ""
    owner: str = ""
    group: str = ""

    @property
    def size(self) -> int:
        """Alias of :attr:`st_size`, the name pyxrootd's ``StatInfo`` used."""
        return self.st_size

    @property
    def modtime(self) -> int:
        """Alias of :attr:`st_mtime` (seconds since the epoch), pyxrootd's name for it."""
        return self.st_mtime

    @property
    def modified(self) -> datetime.datetime:
        """When it last changed, as a :class:`~datetime.datetime` in UTC.

        Storage elements keep times in UTC and people read them in their own,
        so this is aware rather than naive: ``.astimezone()`` puts it in
        yours, and printing it says which one it is.
        """
        return datetime.datetime.fromtimestamp(self.st_mtime, datetime.UTC)

    def __str__(self) -> str:
        """The line ``ls -l`` would print for it."""
        when = self.modified.strftime("%Y-%m-%d %H:%M") if self.st_mtime else "-"
        size = human_bytes(self.st_size)
        return f"{_stat.filemode(self.st_mode)} {size:>10}  {when}  {self.path}".rstrip()

    @property
    def st_mode(self) -> int:
        """POSIX mode bits: the server's own, or synthesised from the flag set.

        A protocol-5 server sends the permission bits (:attr:`mode_str`), and
        those are what ``ls`` shows on the server. An older one sends only
        "readable" and "writable", which are this client's access rather than
        the file's mode, and are the best there is then.
        """
        mode = _stat.S_IFDIR if self.is_dir() else _stat.S_IFREG
        if self.mode_str:
            return mode | (int(self.mode_str, 8) & 0o7777)
        if self.flags & StatInfoFlags.IS_READABLE:
            mode |= 0o444
        if self.flags & StatInfoFlags.IS_WRITABLE:
            mode |= 0o222
        if self.is_dir():
            mode |= 0o111
        return mode

    def is_dir(self) -> bool:
        """``True`` when the server set ``kXR_isDir`` for this path."""
        return bool(self.flags & StatInfoFlags.IS_DIR)

    def is_file(self) -> bool:
        """A regular file: neither a directory nor ``kXR_other``.

        ``kXR_other`` covers whatever the server could not classify (a FIFO, a
        device, a dangling link on some back ends); those are reported as neither
        file nor directory, as ``os.path.isfile`` would.
        """
        return not (self.flags & (StatInfoFlags.IS_DIR | StatInfoFlags.OTHER))

    def is_offline(self) -> bool:
        """``kXR_offline``: the bytes are on tape or another slow tier.

        Opening it will either stage it (slowly) or fail, depending on the site;
        see :meth:`~xrdclient.FileSystem.prepare` to stage ahead of time.
        """
        return bool(self.flags & StatInfoFlags.OFFLINE)

    def is_readable(self) -> bool:
        """``kXR_readable``: this client's identity may read it (not the POSIX mode)."""
        return bool(self.flags & StatInfoFlags.IS_READABLE)

    def is_writable(self) -> bool:
        """``kXR_writable``: this client's identity may write it (not the POSIX mode)."""
        return bool(self.flags & StatInfoFlags.IS_WRITABLE)


@dataclass(frozen=True, slots=True)
class DirEntry:
    """One entry of a directory listing."""

    name: str
    parent: str = ""
    stat: StatInfo | None = None
    #: What ``kXR_dcksm`` said, for a listing that asked for digests.
    checksum: ChecksumInfo | None = None

    @property
    def path(self) -> str:
        """Full path, ``parent`` joined with ``name``."""
        return f"{self.parent.rstrip('/')}/{self.name}" if self.parent else self.name

    def is_dir(self) -> bool:
        """``True`` for a directory; ``False`` when the listing carried no stat at all."""
        return self.stat is not None and self.stat.is_dir()

    def is_file(self) -> bool:
        """``True`` for a regular file; ``False`` when the listing carried no stat at all."""
        return self.stat is not None and self.stat.is_file()

    def __str__(self) -> str:
        """Where it is, so that printing a listing prints paths."""
        return self.path

    def __fspath__(self) -> str:
        """Lets ``os.fspath`` and ``open``-style APIs take an entry as its path."""
        return self.path


@dataclass(frozen=True, slots=True)
class VFSInfo:
    """Result of a ``statvfs`` (``kXR_stat`` with ``kXR_vfs``)."""

    nodes_rw: int = 0
    free_rw: int = 0
    utilization_rw: int = 0
    nodes_staging: int = 0
    free_staging: int = 0
    utilization_staging: int = 0

    @property
    def f_bavail(self) -> int:
        """``os.statvfs_result``-style alias of :attr:`free_rw`.

        Note the unit: ``kXR_vfs`` reports free space in **megabytes**, not in
        blocks, so this is not interchangeable with a local ``f_bavail * f_frsize``.
        """
        return self.free_rw


@dataclass(frozen=True, slots=True)
class SpaceInfo:
    """Result of ``kXR_query`` with ``kXR_Qspace``.

    Where :class:`VFSInfo` describes a whole storage element in megabytes and
    percentages, this describes one space token - the named pool a write with
    ``oss.cgroup`` lands in - and does so in bytes.

    ``quota`` is ``-1`` when the pool has none, which is how the server says
    "unlimited" and not a number to compare against ``used``.
    """

    name: str = ""
    total: int = 0
    free: int = 0
    largest_free: int = 0
    used: int = 0
    quota: int = -1

    @property
    def unlimited(self) -> bool:
        """``True`` when no quota applies to this pool."""
        return self.quota < 0

    def __str__(self) -> str:
        """A one-line summary for logs and the CLI's ``spaceinfo``."""
        return f"{self.name or 'default'}: {self.free} of {self.total} bytes free"


@dataclass(frozen=True, slots=True)
class PrepareStatus:
    """What a staging query (``kXR_query`` with ``kXR_QPrep``) says of one file.

    A tape-backed site answers a :meth:`~xrdclient.FileSystem.prepare` call at once
    and stages for minutes or hours afterwards, so the interesting question is
    this one: is the file :attr:`online` yet, is it still only :attr:`on_tape`,
    and did the request this query names ever ask for it (:attr:`requested`).

    ``error`` is the server's own text for a file it could not report on, and
    is empty for the ones it could. ``state`` is the word the server used -
    ``"COMPLETED"``, ``"NEARLINE"`` and so on - kept verbatim because the
    vocabulary differs between the tape API and the storage behind it, and
    because the booleans above are the part worth branching on.
    """

    path: str = ""
    exists: bool = False
    on_tape: bool = False
    online: bool = False
    requested: bool = False
    has_request_id: bool = False
    requested_at: str = ""
    error: str = ""
    state: str = ""

    def __bool__(self) -> bool:
        """``True`` once the file is on disk, which is what staging is for."""
        return self.online

    def __str__(self) -> str:
        """``path: online``, ``path: on tape`` or ``path: nowhere``, with any server error."""
        where = "online" if self.online else ("on tape" if self.on_tape else "nowhere")
        return f"{self.path}: {where}{f' ({self.error})' if self.error else ''}"


@dataclass(frozen=True, slots=True)
class LocationInfo:
    """One entry of a ``locate`` result."""

    address: str
    type: str = "S"
    access: str = "r"

    @property
    def host(self) -> str:
        """The bare host, with any IPv6 brackets removed."""
        host = self.address.rsplit(":", 1)[0] if self._has_port else self.address
        return host[1:-1] if host.startswith("[") and host.endswith("]") else host

    @property
    def port(self) -> int:
        """The port in :attr:`address`, or 1094 (the XRootD default) when it has none."""
        _, sep, port = self.address.rpartition(":")
        return int(port) if sep and port.isdigit() else 1094

    @property
    def _has_port(self) -> bool:
        _, sep, port = self.address.rpartition(":")
        return bool(sep and port.isdigit())

    @property
    def is_manager(self) -> bool:
        """A redirector (``M``, or ``m`` while pending): following it yields more locations."""
        return self.type in "Mm"

    @property
    def is_server(self) -> bool:
        """A data server (``S``, or ``s`` while pending): it holds the bytes."""
        return self.type in "Ss"

    @property
    def is_pending(self) -> bool:
        """The server has the file staged-out or is still deciding."""
        return self.type.islower()

    @property
    def is_writable(self) -> bool:
        """The server granted ``w`` access, so the file can be opened for update there."""
        return self.access == "w"

    def __str__(self) -> str:
        """The ``host:port`` address, as ``xrdfs locate`` prints it."""
        return self.address


@dataclass(frozen=True, slots=True)
class ProtocolInfo:
    """Result of ``kXR_protocol``."""

    version: int = 0
    flags: int = 0
    security_level: int = 0
    security_version: int = 0
    security_options: int = 0
    security_overrides: dict[int, int] = field(default_factory=dict)

    @property
    def version_str(self) -> str:
        """The binary-coded version as dotted text: ``0x520`` becomes ``"5.2.0"``.

        ``kXR_protocol`` packs one version digit per nibble rather than sending
        an integer, so ``hex()`` of :attr:`version` reads naturally but ``str()``
        does not.
        """
        return f"{(self.version >> 8) & 0xF}.{(self.version >> 4) & 0xF}.{self.version & 0xF}"

    @property
    def has_tls(self) -> bool:
        """``kXR_haveTLS``: the server can switch this connection to TLS."""
        return bool(self.flags & 0x80000000)

    @property
    def is_manager(self) -> bool:
        """``kXR_isManager``: a redirector (cmsd manager), not a data server."""
        return bool(self.flags & 0x00000002)

    @property
    def is_server(self) -> bool:
        """``kXR_isServer``: a data server that holds files itself."""
        return bool(self.flags & 0x00000001)

    @property
    def is_meta(self) -> bool:
        """A meta-manager: it redirects to managers, not to data servers."""
        return bool(self.flags & 0x00000100)  # kXR_attrMeta

    @property
    def is_proxy(self) -> bool:
        """``kXR_attrProxy``: an XCache or forwarding proxy in front of another site."""
        return bool(self.flags & 0x00000200)  # kXR_attrProxy

    @property
    def is_supervisor(self) -> bool:
        """``kXR_attrSuper``: a sub-manager in a large cluster's redirector tree."""
        return bool(self.flags & 0x00000400)  # kXR_attrSuper

    @property
    def is_cache(self) -> bool:
        """``kXR_attrCache``: a caching server; reads may be served from a copy."""
        return bool(self.flags & 0x00000080)  # kXR_attrCache

    @property
    def supports_posc(self) -> bool:
        """Persist-on-successful-close: a write that dies leaves no file."""
        return bool(self.flags & 0x00100000)  # kXR_supposc

    @property
    def supports_pgio(self) -> bool:
        """``pgread`` and ``pgwrite`` - reads and writes with a CRC per page."""
        return bool(self.flags & 0x00200000)  # kXR_suppgrw

    @property
    def supports_gpfile(self) -> bool:
        """``kXR_supgpf``: the server implements ``kXR_gpfile`` (server-driven get/put)."""
        return bool(self.flags & 0x00400000)  # kXR_supgpf

    @property
    def allows_anonymous_gpfile(self) -> bool:
        """``kXR_anongpf``: ``kXR_gpfile`` is permitted without authentication."""
        return bool(self.flags & 0x00800000)  # kXR_anongpf

    @property
    def requires_tls_for_data(self) -> bool:
        """File data must move encrypted, whatever the login did."""
        return bool(self.flags & 0x01000000)  # kXR_tlsData


@dataclass(frozen=True, slots=True)
class CheckpointInfo:
    """How much room the open checkpoint has left, in bytes."""

    capacity: int
    used: int

    @property
    def free(self) -> int:
        """What is left before the server refuses further checkpointed writes."""
        return max(0, self.capacity - self.used)

    def __str__(self) -> str:
        """``used/capacity bytes used``, for logs."""
        return f"{self.used}/{self.capacity} bytes used"


@dataclass(frozen=True, slots=True)
class ReadRange:
    """One element of a vector read."""

    offset: int
    length: int


@dataclass(frozen=True, slots=True)
class WriteChunk:
    """One element of a vector write."""

    offset: int
    data: bytes


@dataclass(frozen=True, slots=True)
class CloneRange:
    """One range of a server-side copy.

    ``target_offset`` defaults to ``offset``, which is the common case: the
    same bytes at the same place in another file.
    """

    offset: int
    length: int
    target_offset: int | None = None

    @property
    def destination(self) -> int:
        """Where the bytes land, resolving the ``None`` default."""
        return self.offset if self.target_offset is None else self.target_offset


@dataclass(frozen=True, slots=True)
class PageResult:
    """Result of a ``pgread``: the data plus any pages that failed CRC."""

    data: bytes
    offset: int = 0
    corrupt_pages: tuple[int, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """``True`` when every page passed its CRC32C check."""
        return not self.corrupt_pages

    def __len__(self) -> int:
        """The number of bytes read, so a result can stand in for the ``bytes`` it holds."""
        return len(self.data)
