"""A pure-Python XRootD client.

Speaks the native ``root://`` binary protocol and HTTP/WebDAV, with no
compiled extension and no XRootD installation required.

    >>> import xrdclient
    >>> with xrdclient.open("root://eos.example.org//store/data.root") as f:
    ...     header = f.read(1024)

The five levels of the API, from most to least convenient:

:mod:`xrdclient.easy`
    One-line verbs on a URL - ``xrdclient.ls``, ``xrdclient.size``, ``xrdclient.read_text`` -
    for when there is one question to ask and no reason to build anything.
``xrdclient.open`` / :class:`~xrdclient.path.XRootDPath`
    File objects and ``pathlib`` semantics.
:class:`~xrdclient.client.FileSystem` / :class:`~xrdclient.client.File`
    Explicit per-operation control, sync or async.
:class:`~xrdclient.session.Session`
    A single authenticated connection.
:mod:`xrdclient.proto`
    The sans-io protocol machinery.

Three packages build on this one and are installed separately:
`xrdroot <https://github.com/rob-c/xrdroot>`_ reads and writes the ROOT file
format over any URL here, `xrdml <https://github.com/rob-c/xrdml>`_ hands a
file of rows to PyTorch a minibatch at a time, and
`xrddatasets <https://github.com/rob-c/xrddatasets>`_ converts open data into
ROOT files and publishes the catalogue they are served from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING as _TYPE_CHECKING

from .client import Checkpoint, File, FileSystem
from .config import Config, configure, current, find_config_file, override
from .copy import CopyResult, SyncMode, copy, copy_tree, third_party
from .easy import (
    checksum,
    exists,
    glob,
    is_online,
    ls,
    mkdir,
    move,
    read_bytes,
    read_text,
    remove,
    size,
    stage,
    stat,
    write_bytes,
    write_text,
)
from .errors import (
    AttrNotFoundError,
    AuthenticationError,
    BusyError,
    ChecksumMismatchError,
    ConnectionError,
    CredentialError,
    InvalidArgumentError,
    NoMechanismError,
    NoSpaceError,
    NotFoundError,
    PageIntegrityError,
    ProtocolError,
    QuotaError,
    ReadOnlyError,
    RedirectLimitError,
    ServerError,
    ServerTimeoutError,
    TimeoutError,
    TLSRequiredError,
    TokenExpiredError,
    TooLargeError,
    TransientError,
    UnsupportedError,
    WaitLimitError,
    XRootDError,
)
from .flags import (
    Access,
    DirListFlags,
    LocateFlags,
    MkDirFlags,
    OpenFlags,
    PrepareFlags,
    QueryCode,
    StatInfoFlags,
)
from .io import open_url as open
from .path import XRootDPath
from .path import XRootDPath as Path  # ``xrdclient.Path`` reads the way pathlib does
from .types import (
    CheckpointInfo,
    ChecksumInfo,
    CloneRange,
    DirEntry,
    LocationInfo,
    PageResult,
    PrepareStatus,
    ProtocolInfo,
    ReadRange,
    SpaceInfo,
    StatInfo,
    VFSInfo,
    WriteChunk,
    human_bytes,
)
from .url import XRootDURL, parse

if _TYPE_CHECKING:  # bound at runtime by ``__getattr__`` below, named here so
    # that a type checker and the documentation can both see what they are
    from .doctor import Check, Report, diagnose

__version__ = "0.1.0"


#: Names that live in a submodule nobody should pay to import. Each is bound
#: on first use by :func:`__getattr__`; the value is the module it comes from.
_LAZY = {"Check": "doctor", "Report": "doctor", "diagnose": "doctor"}


def __getattr__(name: str) -> object:
    """Expose ``xrdclient.aio`` and the diagnostics lazily.

    Each is one attribute access away and neither costs anything until it is
    asked for: nothing under ``xrd`` imports :mod:`asyncio` until someone
    reaches for ``xrdclient.aio``, or the environment checks until someone runs
    :func:`~xrdclient.doctor.diagnose`. A transfer is the common case and pays for
    neither.
    """
    import importlib

    if name in ("aio", "doctor"):
        # Not ``from . import aio``: the import system resolves that by asking
        # this very function for the attribute, and the recursion is infinite.
        return importlib.import_module(f"{__name__}.{name}")
    if name in _LAZY:
        value = getattr(importlib.import_module(f"{__name__}.{_LAZY[name]}"), name)
        globals()[name] = value  # bound once; later lookups skip this function
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "__version__",
    # the facades that are imported on first use
    "aio",
    # configuration
    "Config",
    "configure",
    "current",
    "override",
    "find_config_file",
    # copying
    "copy",
    "copy_tree",
    "third_party",
    "CopyResult",
    "SyncMode",
    # diagnosing
    "diagnose",
    "Check",
    "Report",
    # files and paths
    "open",
    "XRootDPath",
    "Path",
    "FileSystem",
    "File",
    "Checkpoint",
    # one-line verbs, for when a URL is all you have
    "ls",
    "glob",
    "stat",
    "exists",
    "size",
    "checksum",
    "read_bytes",
    "read_text",
    "write_bytes",
    "write_text",
    "mkdir",
    "remove",
    "move",
    "stage",
    "is_online",
    "human_bytes",
    # urls
    "XRootDURL",
    "parse",
    # values
    "CheckpointInfo",
    "ChecksumInfo",
    "CloneRange",
    "DirEntry",
    "LocationInfo",
    "PageResult",
    "ProtocolInfo",
    "ReadRange",
    "StatInfo",
    "SpaceInfo",
    "PrepareStatus",
    "VFSInfo",
    "WriteChunk",
    # flags
    "Access",
    "DirListFlags",
    "LocateFlags",
    "MkDirFlags",
    "OpenFlags",
    "PrepareFlags",
    "QueryCode",
    "StatInfoFlags",
    # errors
    "XRootDError",
    "ProtocolError",
    "ConnectionError",
    "TimeoutError",
    "TransientError",
    "AuthenticationError",
    "NoMechanismError",
    "CredentialError",
    "TokenExpiredError",
    "RedirectLimitError",
    "WaitLimitError",
    "ChecksumMismatchError",
    "PageIntegrityError",
    "ServerError",
    "NotFoundError",
    "NoSpaceError",
    "UnsupportedError",
    "ReadOnlyError",
    "QuotaError",
    "AttrNotFoundError",
    "BusyError",
    "InvalidArgumentError",
    "ServerTimeoutError",
    "TLSRequiredError",
    "TooLargeError",
]
