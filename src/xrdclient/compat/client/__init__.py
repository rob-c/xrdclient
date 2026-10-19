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
``.responses``, ``.utils``, ``.tape``, ``.finalize`` - so
``from XRootD.client.flags import OpenFlags`` ports the same way.

The native API is still there underneath: a compat ``FileSystem`` or ``File``
keeps its :class:`xrdclient.FileSystem` or :class:`xrdclient.File` as
``.native``, for code moving over one call at a time.

What must not break here: the exported names and the submodule names. Both
are the bindings' public surface - :func:`xrdclient.compat.install` maps
``XRootD.client.<name>`` onto these modules one for one - so a rename here
is a break for every ported program, whatever the native API does.
"""

from __future__ import annotations

# Importing ``finalize`` registers the exit handler, as importing the
# bindings' does; it is imported here so that ``import client`` alone does it.
from . import finalize, flags, responses, tape, utils
from ._version import __version__
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
from .responses import (
    XRootDAuthorizationError,
    XRootDChecksumError,
    XRootDError,
    XRootDNotFoundError,
    XRootDOperationError,
    XRootDTimeoutError,
    raise_on_error,
)
from .tape import TapeClient
from .url import URL
from .xattr import setXAttrAdler32

__all__ = [
    "URL",
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
    "TapeClient",
    "XRootDAuthorizationError",
    "XRootDChecksumError",
    "XRootDError",
    "XRootDNotFoundError",
    "XRootDOperationError",
    "XRootDTimeoutError",
    "__version__",
    "finalize",
    "flags",
    "glob",
    "iglob",
    "raise_on_error",
    "responses",
    "setXAttrAdler32",
    "tape",
    "utils",
]
