"""The flag namespaces of ``XRootD.client.flags``, value for value.

These are deliberately *not* the enums in :mod:`xrdclient.flags`. Those carry
the wire protocol's numbering, and in two places the official bindings number
things differently: ``DirListFlags`` are XrdCl's client-side switches
(``STAT`` is 1, where the wire's 1 is ``kXR_online``), and ``AccessMode`` spells
``UR``/``UW``/``UX`` where the native enum spells ``OWNER_READ``. Code being
ported passes these as plain integers as often as by name, so a name that
matched and a number that did not would be the worst of both: here both match.

Each namespace is a plain class of ``int`` attributes with a
``reverse_mapping``, which is exactly what the bindings' own ``enum()`` helper
builds, so ``OpenFlags.reverse_mapping[16]`` is ``"READ"`` in either library.
"""

from __future__ import annotations

from typing import ClassVar

__all__ = [
    "AccessMode",
    "AccessType",
    "DirListFlags",
    "HostTypes",
    "LocationType",
    "MkDirFlags",
    "OpenFlags",
    "PrepareFlags",
    "QueryCode",
    "StatInfoFlags",
]


class _Namespace:
    """Base for a namespace of named integers with a ``reverse_mapping``."""

    reverse_mapping: ClassVar[dict[int, str]] = {}

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        cls.reverse_mapping = {
            value: name
            for name, value in vars(cls).items()
            if not name.startswith("_") and isinstance(value, int)
        }


class OpenFlags(_Namespace):
    """``XrdCl::OpenFlags``: how :meth:`File.open` opens."""

    NONE = 0
    DELETE = 2
    FORCE = 4
    NEW = 8
    READ = 16
    UPDATE = 32
    REFRESH = 128
    MAKEPATH = 256
    REPLICA = 2048
    POSC = 4096
    NOWAIT = 8192
    SEQIO = 16384
    WRITE = 32768
    DUP = 65536
    SAMEFS = 131072


class AccessMode(_Namespace):
    """``XrdCl::Access``: permission bits, in the bindings' short spelling."""

    NONE = 0
    UR = 256
    UW = 128
    UX = 64
    GR = 32
    GW = 16
    GX = 8
    OR = 4
    OW = 2
    OX = 1


class MkDirFlags(_Namespace):
    """``XrdCl::MkDirFlags``."""

    NONE = 0
    MAKEPATH = 1


class DirListFlags(_Namespace):
    """``XrdCl::DirListFlags``: client-side switches, not wire bits."""

    NONE = 0
    STAT = 1
    LOCATE = 2
    RECURSIVE = 4
    MERGE = 8
    CHUNKED = 16
    ZIP = 32


class PrepareFlags(_Namespace):
    """``XrdCl::PrepareFlags``."""

    STAGE = 8
    WRITEMODE = 16
    COLOCATE = 32
    FRESH = 64
    EVICT = 256


class QueryCode(_Namespace):
    """``XrdCl::QueryCode``."""

    STATS = 1
    PREPARE = 2
    CHECKSUM = 3
    XATTR = 4
    SPACE = 5
    CHECKSUMCANCEL = 6
    CONFIG = 7
    VISA = 8
    OPAQUE = 16
    OPAQUEFILE = 32


class StatInfoFlags(_Namespace):
    """``XrdCl::StatInfo::Flags``."""

    X_BIT_SET = 1
    IS_DIR = 2
    OTHER = 4
    OFFLINE = 8
    IS_READABLE = 16
    IS_WRITABLE = 32
    POSC_PENDING = 64
    BACKUP_EXISTS = 128


class LocationType(_Namespace):
    """``XrdCl::LocationInfo::LocationType``."""

    MANAGER_ONLINE = 0
    MANAGER_PENDING = 1
    SERVER_ONLINE = 2
    SERVER_PENDING = 3


class AccessType(_Namespace):
    """``XrdCl::LocationInfo::AccessType``."""

    READ = 0
    READ_WRITE = 1


class HostTypes(_Namespace):
    """``XrdCl::HostTypes``: what :meth:`FileSystem.protocol` says a host is."""

    IS_MANAGER = 2
    IS_SERVER = 1
    ATTR_META = 256
    ATTR_PROXY = 512
    ATTR_SUPER = 1024
