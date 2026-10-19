"""A Python 3 XRootD client.

Speaks the native ``root://`` binary protocol and HTTP/WebDAV, with no
compiled extension of its own and no XRootD installation required. The
cryptographic, ASN.1 and AWS-signing primitives come from a small set of
maintained libraries (``cryptography``, ``asn1crypto`` and ``botocore``)
installed from wheels; HTTP is the standard library's.

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

Tools built on this package (xgfalclient, xrdarc) need the same grid
plumbing it uses itself, and get it from public modules rather than private
ones: :mod:`xrdclient.credentials` (proxy, certificate, token and CA
discovery), :mod:`xrdclient.tls` (client TLS contexts), :mod:`xrdclient.report`
(the shared ``--json``/``--xml`` CLI report), :mod:`xrdclient.safexml`,
:mod:`xrdclient.retry` and :mod:`xrdclient.log`.

Three packages build on this one and are installed separately:
`xrdroot <https://github.com/rob-c/xrdroot>`_ reads and writes the ROOT file
format over any URL here, `xrdml <https://github.com/rob-c/xrdml>`_ hands a
file of rows to PyTorch a minibatch at a time, and
`xrddatasets <https://github.com/rob-c/xrddatasets>`_ converts open data into
ROOT files and publishes the catalogue they are served from.
"""

from __future__ import annotations

# Underscored: ``types`` is a submodule here, and a bare ``import types``
# would shadow it as ``xrdclient.types``.
import importlib as _importlib
import sys as _sys
from types import ModuleType as _ModuleType
from typing import TYPE_CHECKING

from ._version import __version__

if TYPE_CHECKING:  # bound at runtime by ``__getattr__`` below, named here so
    # that a type checker and the documentation can both see what they are
    from .client import BaseFileSystem, Checkpoint, File, FileSystem
    from .config import Config, configure, current, find_config_file, override
    from .copy import (
        CopyResult,
        SyncMode,
        append_zip,
        copy,
        copy_tree,
        failed_end,
        third_party,
    )
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
        AbortedError,
        AttrNotFoundError,
        AuthenticationError,
        BusyError,
        ChecksumMismatchError,
        ConnectionError,
        CredentialError,
        InvalidArgumentError,
        MetalinkError,
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
    from .metalink import MetalinkFile, parse_metalink
    from .path import XRootDPath
    from .path import XRootDPath as Path  # ``xrdclient.Path`` reads the way pathlib does
    from .session.deadline import OperationExpiredError, deadline
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
del TYPE_CHECKING  # imported for the block above, not part of the package

#: Every public name, by the submodule that defines it. Nothing here is
#: imported until it is first asked for: ``import xrdclient`` costs the
#: version string, so a program that only parses URLs, or a tool that imports
#: one leaf module, never pays for TLS, the session layer or the X.509 stack.
_EXPORTS: dict[str, tuple[str, ...]] = {
    "client": ("BaseFileSystem", "Checkpoint", "File", "FileSystem"),
    "config": ("Config", "configure", "current", "find_config_file", "override"),
    "copy": (
        "CopyResult",
        "SyncMode",
        "append_zip",
        "copy",
        "copy_tree",
        "failed_end",
        "third_party",
    ),
    "doctor": ("Check", "Report", "diagnose"),
    "easy": (
        "checksum",
        "exists",
        "glob",
        "is_online",
        "ls",
        "mkdir",
        "move",
        "read_bytes",
        "read_text",
        "remove",
        "size",
        "stage",
        "stat",
        "write_bytes",
        "write_text",
    ),
    "errors": (
        "AbortedError",
        "AttrNotFoundError",
        "AuthenticationError",
        "BusyError",
        "ChecksumMismatchError",
        "ConnectionError",
        "CredentialError",
        "InvalidArgumentError",
        "MetalinkError",
        "NoMechanismError",
        "NoSpaceError",
        "NotFoundError",
        "PageIntegrityError",
        "ProtocolError",
        "QuotaError",
        "ReadOnlyError",
        "RedirectLimitError",
        "ServerError",
        "ServerTimeoutError",
        "TimeoutError",
        "TLSRequiredError",
        "TokenExpiredError",
        "TooLargeError",
        "TransientError",
        "UnsupportedError",
        "WaitLimitError",
        "XRootDError",
    ),
    "flags": (
        "Access",
        "DirListFlags",
        "LocateFlags",
        "MkDirFlags",
        "OpenFlags",
        "PrepareFlags",
        "QueryCode",
        "StatInfoFlags",
    ),
    "metalink": ("MetalinkFile", "parse_metalink"),
    "path": ("XRootDPath",),
    "session.deadline": ("OperationExpiredError", "deadline"),
    "types": (
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
    ),
    "url": ("XRootDURL", "parse"),
}

#: Public name -> (submodule, attribute there). Two names are renamed on the
#: way out: ``open`` is :func:`~xrdclient.io.open_url`, and ``Path`` is
#: :class:`~xrdclient.path.XRootDPath` under the name :mod:`pathlib` uses.
_LAZY: dict[str, tuple[str, str]] = {
    name: (module, name) for module, names in _EXPORTS.items() for name in names
}
_LAZY.update(open=("io", "open_url"), Path=("path", "XRootDPath"))

#: The subpackages and modules reachable as ``xrdclient.<name>`` without an
#: ``import`` of their own, as they all once were by side effect. ``copy`` is
#: not among them: that name is the function, see :class:`_Package`.
_SUBMODULES = frozenset(
    {
        "aio",
        "auth",
        "cli",
        "client",
        "compat",
        "config",
        "credentials",
        "crypto",
        "doctor",
        "easy",
        "errors",
        "flags",
        "fsspec_impl",
        "http",
        "io",
        "log",
        "metalink",
        "path",
        "proto",
        "report",
        "retry",
        "s3",
        "safexml",
        "session",
        "testing",
        "tls",
        "transport",
        "types",
        "url",
        "voms_client",
    }
)


def __getattr__(name: str) -> object:
    """Bind a public name, or a submodule, the first time it is asked for.

    Each value is bound into the package once and later lookups never reach
    this function. Submodules are returned rather than bound, since the
    import system binds them itself. ``import xrdclient`` therefore imports
    nothing but the version, and nothing under ``xrd`` imports :mod:`asyncio`
    until someone reaches for ``xrdclient.aio``.
    """
    if name in _SUBMODULES:
        # Not ``from . import aio``: the import system resolves that by asking
        # this very function for the attribute, and the recursion is infinite.
        return _importlib.import_module(f"{__name__}.{name}")
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, attribute = target
    value = getattr(_importlib.import_module(f"{__name__}.{module}"), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """What is bound already, plus every public name still waiting to be."""
    return sorted(set(globals()) | set(__all__))


class _Package(_ModuleType):
    """This package's module type: one that keeps ``xrdclient.copy`` the function.

    ``xrdclient.copy`` is both a subpackage and the function the package
    exports under that name. Importing a submodule sets it as an attribute of
    its parent, so ``import xrdclient.copy.engine`` - or anything that
    imports one of the copy modules - would otherwise rebind
    ``xrdclient.copy`` to the subpackage behind the caller's back. Eager
    imports used to settle the order once; with them gone, the binding is
    refused here instead and ``copy`` resolves through ``__getattr__`` like
    every other name. ``sys.modules["xrdclient.copy"]`` is the subpackage
    either way.
    """

    def __setattr__(self, name: str, value: object) -> None:
        """Ignore a submodule being bound over a lazy public name; set anything else.

        Only module objects are refused, and only for names in ``_LAZY``, so
        ``monkeypatch.setattr(xrdclient, ...)`` and ordinary assignment work.
        """
        if isinstance(value, _ModuleType) and name in _LAZY:
            return  # the import system binding a submodule over a public name
        super().__setattr__(name, value)


_sys.modules[__name__].__class__ = _Package


# docs/api.md groups these by meaning; here they are in ruff's RUF022 order.
__all__ = [
    "AbortedError",
    "Access",
    "AttrNotFoundError",
    "AuthenticationError",
    "BaseFileSystem",
    "BusyError",
    "Check",
    "Checkpoint",
    "CheckpointInfo",
    "ChecksumInfo",
    "ChecksumMismatchError",
    "CloneRange",
    "Config",
    "ConnectionError",
    "CopyResult",
    "CredentialError",
    "DirEntry",
    "DirListFlags",
    "File",
    "FileSystem",
    "InvalidArgumentError",
    "LocateFlags",
    "LocationInfo",
    "MetalinkError",
    "MetalinkFile",
    "MkDirFlags",
    "NoMechanismError",
    "NoSpaceError",
    "NotFoundError",
    "OpenFlags",
    "OperationExpiredError",
    "PageIntegrityError",
    "PageResult",
    "Path",
    "PrepareFlags",
    "PrepareStatus",
    "ProtocolError",
    "ProtocolInfo",
    "QueryCode",
    "QuotaError",
    "ReadOnlyError",
    "ReadRange",
    "RedirectLimitError",
    "Report",
    "ServerError",
    "ServerTimeoutError",
    "SpaceInfo",
    "StatInfo",
    "StatInfoFlags",
    "SyncMode",
    "TLSRequiredError",
    "TimeoutError",
    "TokenExpiredError",
    "TooLargeError",
    "TransientError",
    "UnsupportedError",
    "VFSInfo",
    "WaitLimitError",
    "WriteChunk",
    "XRootDError",
    "XRootDPath",
    "XRootDURL",
    "__version__",
    "aio",
    "append_zip",
    "checksum",
    "configure",
    "copy",
    "copy_tree",
    "current",
    "deadline",
    "diagnose",
    "exists",
    "failed_end",
    "find_config_file",
    "glob",
    "human_bytes",
    "is_online",
    "ls",
    "mkdir",
    "move",
    "open",
    "override",
    "parse",
    "parse_metalink",
    "read_bytes",
    "read_text",
    "remove",
    "size",
    "stage",
    "stat",
    "third_party",
    "write_bytes",
    "write_text",
]
