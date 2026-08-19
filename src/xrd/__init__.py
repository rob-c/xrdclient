"""A pure-Python XRootD client.

Speaks the native ``root://`` binary protocol and HTTP/WebDAV, with no
compiled extension and no XRootD installation required.

    >>> import xrd
    >>> with xrd.open("root://eos.example.org//store/data.root") as f:
    ...     header = f.read(1024)

The five levels of the API, from most to least convenient:

:mod:`xrd.easy`
    One-line verbs on a URL - ``xrd.ls``, ``xrd.size``, ``xrd.read_text`` -
    for when there is one question to ask and no reason to build anything.
``xrd.open`` / :class:`~xrd.path.XRootDPath`
    File objects and ``pathlib`` semantics.
:class:`~xrd.client.FileSystem` / :class:`~xrd.client.File`
    Explicit per-operation control, sync or async.
:class:`~xrd.session.Session`
    A single authenticated connection.
:mod:`xrd.proto`
    The sans-io protocol machinery.

:mod:`xrd.ml` sits beside all of them: a ROOT file of rows on a server,
handed to PyTorch a minibatch at a time.
"""

from __future__ import annotations

from .client import Checkpoint, File, FileSystem
from .config import Config, configure, current, find_config_file, override
from .copy import CopyResult, SyncMode, copy, copy_tree, third_party
from .doctor import Check, Report, diagnose
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
from .path import XRootDPath as Path  # ``xrd.Path`` reads the way pathlib does
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

__version__ = "0.1.0"


def __getattr__(name: str) -> object:
    """Expose ``xrd.aio`` and ``xrd.ml`` without making everyone pay for them.

    Both are one attribute access away, and neither costs anything until it is
    asked for: nothing under ``xrd`` imports :mod:`asyncio` until someone
    reaches for ``xrd.aio``, or the ROOT reader until someone reaches for
    ``xrd.ml``.
    """
    if name in ("aio", "ml"):
        import importlib

        # Not ``from . import aio``: the import system resolves that by asking
        # this very function for the attribute, and the recursion is infinite.
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "__version__",
    # the facades that are imported on first use
    "aio",
    "ml",
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
