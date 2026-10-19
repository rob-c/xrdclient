"""One encoder per ``kXR_*`` opcode.

Layouts follow nginx-xrootd ``wire_core_requests.h``; cross-checked against
go-hep ``xrootd/xrdproto/<op>`` and XRootD.jl ``Wire/requests.jl``. Every
``params`` method writes exactly 16 bytes (header offsets 4..19).

This module is the public face of the encoders and the only place callers
import them from (``from xrdclient.proto import requests as r``; ``r.Open``).
The classes themselves live in private modules grouped by what they address,
so a reader looking for one opcode opens one short file:

* ``_requests_session`` - the connection: protocol, login, auth, ping,
  endsess, bind, set, and the ``kXR_sigver`` signature frame;
* ``_requests_ns`` - paths in the namespace: stat, statx, dirlist, locate,
  mkdir, rm, rmdir, mv and the links, readlink, setattr, chmod, truncate;
* ``_requests_query`` - the server and its storage: query and prepare;
* ``_requests_io`` - an open file's handle: open, close, read, write, sync,
  the vector and paged forms, clone, chkpoint and fattr;
* ``_requests_base`` - the ``struct`` layouts and coercion helpers several of
  them share.

Every name is re-exported here, so ``requests.Open is _requests_io.Open`` and
nothing that imported from this module before the split needs to change.
"""

from __future__ import annotations

# The public encoders come in as plain imports and are re-exported through
# ``__all__``. Everything else this module's namespace held before the split -
# the shared layouts and helpers, and the frame primitives the encoders were
# written against - is re-exported by a redundant ``as`` alias, so
# ``r.encode``/``r.Writer`` and any private name a test or a downstream module
# once reached for keep working.
from ..errors import ProtocolError as ProtocolError
from ._requests_base import _HANDLE_PARAMS as _HANDLE_PARAMS
from ._requests_base import _OPEN_PARAMS as _OPEN_PARAMS
from ._requests_base import _READ_PARAMS as _READ_PARAMS
from ._requests_base import _STAT_PARAMS as _STAT_PARAMS
from ._requests_base import _WRITE_PARAMS as _WRITE_PARAMS
from ._requests_base import _ZERO_PARAMS as _ZERO_PARAMS
from ._requests_base import _byte_view as _byte_view
from ._requests_base import _encode_path as _encode_path
from ._requests_base import _handle as _handle
from ._requests_io import _OPEN_WRITES as _OPEN_WRITES
from ._requests_io import (
    ChkPoint,
    Clone,
    Close,
    Fattr,
    Open,
    PgRead,
    PgWrite,
    Read,
    ReadV,
    Sync,
    Write,
    WriteV,
)
from ._requests_ns import (
    Chmod,
    Dirlist,
    Link,
    Locate,
    Mkdir,
    Mv,
    Readlink,
    Rm,
    Rmdir,
    Setattr,
    Stat,
    StatVFS,
    Statx,
    Symlink,
    Truncate,
)
from ._requests_query import (
    Prepare,
    Query,
)
from ._requests_session import (
    Auth,
    Bind,
    EndSession,
    Login,
    Ping,
    Protocol,
    Set,
    Sigver,
)
from .buffer import Writer as Writer
from .frames import Payload as Payload
from .frames import Request as Request
from .frames import encode as encode

__all__ = [
    "Auth",
    "Bind",
    "ChkPoint",
    "Chmod",
    "Clone",
    "Close",
    "Dirlist",
    "EndSession",
    "Fattr",
    "Link",
    "Locate",
    "Login",
    "Mkdir",
    "Mv",
    "Open",
    "PgRead",
    "PgWrite",
    "Ping",
    "Prepare",
    "Protocol",
    "Query",
    "Read",
    "ReadV",
    "Readlink",
    "Rm",
    "Rmdir",
    "Set",
    "Setattr",
    "Sigver",
    "Stat",
    "StatVFS",
    "Statx",
    "Symlink",
    "Sync",
    "Truncate",
    "Write",
    "WriteV",
]
