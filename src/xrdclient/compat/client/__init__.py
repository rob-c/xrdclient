"""``XRootD.client``, over this library: change the import and nothing else.

    from xrdclient.compat import client        # was: from XRootD import client

    fs = client.FileSystem("root://eos.example.org")
    status, info = fs.stat("/store/f.root")
    if not status.ok:
        raise RuntimeError(status.message)

Every class, function and flag the bindings export is here under the same
name, returning the same ``(XRootDStatus, response)`` pairs with the same
field names and the same numbers in them. Submodules are importable by the
bindings' names too - ``xrdclient.compat.client.flags``,
``.responses``, ``.utils`` - so ``from XRootD.client.flags import OpenFlags``
ports the same way.

The native API is still there underneath: a compat ``FileSystem`` or ``File``
keeps its :class:`xrdclient.FileSystem` or :class:`xrdclient.File` as
``.native``, for code moving over one call at a time.
"""

from __future__ import annotations

from . import flags, responses, utils
from .copyprocess import CopyProcess
from .env import (
    EnvDelInt,
    EnvDelString,
    EnvGetDefault,
    EnvGetInt,
    EnvGetString,
    EnvPutInt,
    EnvPutString,
    SetLogLevel,
    SetLogMask,
)
from .file import File
from .filesystem import FileSystem
from .glob_funcs import glob, iglob
from .url import URL
from .xattr import setXAttrAdler32

__all__ = [
    "CopyProcess",
    "EnvDelInt",
    "EnvDelString",
    "EnvGetDefault",
    "EnvGetInt",
    "EnvGetString",
    "EnvPutInt",
    "EnvPutString",
    "File",
    "FileSystem",
    "SetLogLevel",
    "SetLogMask",
    "URL",
    "flags",
    "glob",
    "iglob",
    "responses",
    "setXAttrAdler32",
    "utils",
]
