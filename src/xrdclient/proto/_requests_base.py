"""What every request encoder shares: fixed parameter layouts and argument coercion.

The ``kXR_*`` encoders are split by area across ``_requests_session``,
``_requests_ns``, ``_requests_query`` and ``_requests_io``; the public face
of all of them is :mod:`xrdclient.proto.requests`, which re-exports every
class, so nothing outside ``proto`` should import from these private modules.
This one holds the pieces more than one of them needs - the precomputed
``struct`` layouts behind the hot-path ``header_params`` methods and the
helpers that turn a file handle, a path or a payload buffer into the bytes
the wire wants - so the area modules depend on it and never on each other.
"""

from __future__ import annotations

import struct

#: Precomputed parameter layouts for the requests every operation sends; each
#: packs to the same 16 bytes its ``params`` writes (``4s`` NUL-pads and
#: truncates exactly as :meth:`Writer.padded` does).
_ZERO_PARAMS = bytes(16)
_STAT_PARAMS = struct.Struct(">B11x4s")
_OPEN_PARAMS = struct.Struct(">HHH6x4s")
_HANDLE_PARAMS = struct.Struct(">4s12x")
_READ_PARAMS = struct.Struct(">4sqi")
_WRITE_PARAMS = struct.Struct(">4sqB3x")


def _handle(fhandle: bytes | bytearray | memoryview | str) -> bytes:
    """A file handle as ``struct``'s ``4s`` takes it.

    Whatever :meth:`Writer.padded` accepts - a ``bytearray``, a
    ``memoryview``, text - and not only ``bytes``, so that the fast path
    refuses nothing the general one would have sent.
    """
    if type(fhandle) is bytes:
        return fhandle
    return fhandle.encode("utf-8") if isinstance(fhandle, str) else bytes(fhandle)


def _byte_view(data: bytearray | memoryview) -> memoryview:
    """``data`` as a flat view of bytes, without copying it.

    A ``memoryview`` of something other than bytes - an ``array('d')``, say -
    counts items, not bytes, and the frame's ``dlen`` must count bytes.
    """
    view = memoryview(data)
    return view if view.format == "B" else view.cast("B")


def _encode_path(path: str) -> bytes:
    return path.encode("utf-8")
