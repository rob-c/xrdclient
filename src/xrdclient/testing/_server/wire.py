"""Frame builders and shared helpers for the fake XRootD server's handlers.

This is the leaf every other piece of the fake builds on: the response-frame
encoders (:func:`frame`, :func:`error`, :func:`pgwrite_cse` - public, and
re-exported by :mod:`xrdclient.testing.server` and :mod:`xrdclient.testing`),
the request/response header structs, path normalisation, and the
``_NotFound`` exception a handler raises to have the dispatcher answer with a
``kXR_NotFound``. It imports nothing else from the fake, so the handler
modules and the facade can all depend on it without an import cycle.
"""

from __future__ import annotations

import posixpath
import struct
from collections.abc import Sequence

from ...proto import constants as c

#: A request header: ``streamid[2] requestid[2] parms[16] dlen[4]`` - 24 bytes.
_REQ = struct.Struct(">HH16sI")
#: A response header: ``streamid[2] status[2] dlen[4]`` - 8 bytes.
_RESP = struct.Struct(">HHI")
#: The all-zero file handle a path-addressed request leaves in its params.
_NULL_HANDLE = b"\x00\x00\x00\x00"


def frame(streamid: int, status: int, body: bytes = b"") -> bytes:
    """One complete response frame: the 8-byte header and its body.

    Public because :attr:`FakeServer.handlers` is: answering badly means
    building the frame yourself.
    """
    return _RESP.pack(streamid, status, len(body)) + body


def error(streamid: int, code: int, message: str) -> bytes:
    """A ``kXR_error`` frame carrying a server error code and its text."""
    return frame(streamid, c.kXR_error, struct.pack(">i", code) + message.encode() + b"\x00")


def pgwrite_cse(streamid: int, corrupt: Sequence[int]) -> bytes:
    """A ``kXR_pgwrite`` reply naming the pages that arrived corrupt.

    A ``kXR_status`` frame whose trailer is ``cseCRC[4] dlFirst[2] dlLast[2]``
    and then one big-endian ``int64`` file offset per bad page.
    ``dlFirst``/``dlLast`` say how much of the first and last page the request
    covered - a whole page each here, which is what a write of whole pages
    means. Public for the same reason :func:`frame` is.
    """
    trailer = struct.pack(">IHH", 0, c.kXR_pgPageSZ, c.kXR_pgPageSZ) + b"".join(
        struct.pack(">q", off) for off in corrupt
    )
    status = (
        struct.pack(">I", 0)
        + struct.pack(">H", streamid)
        + bytes([c.kXR_pgwrite - c.kXR_1stRequest, c.kXR_FinalResult])
        + bytes(4)
        + struct.pack(">i", len(trailer))
        + struct.pack(">q", 0)
    )
    return frame(streamid, c.kXR_status, status) + trailer


# The underscored spellings are what the handler modules and older tests
# use; the public ones exist for custom :attr:`FakeServer.handlers`.
_frame = frame
_error = error


def _clean(path: str) -> str:
    """Strip any CGI and normalise, the way a server resolves a path."""
    base = path.partition("?")[0]
    return posixpath.normpath("/" + base.lstrip("/")) if base else "/"


class _NotFound(Exception):
    """Raised inside a handler to produce a ``kXR_NotFound``.

    The dispatcher turns it into an error frame for the request's stream,
    so a handler deep in a helper need not thread the stream id back up.
    ``code`` lets the same shortcut carry another error (``kXR_ArgMissing``
    for a request that names neither path nor handle, for instance).
    """

    def __init__(self, path: str, code: int = 3011, message: str = "no such file or directory"):
        """Remember what was missing, and the error code and text to answer with."""
        self.path = path
        self.code = code
        self.message = message
