"""Frame-level codecs.

Three fixed layouts: the 20-byte ``ClientInitHandShake``, the 24-byte
``ClientRequestHdr`` (streamid[2] requestid[2] params[16] dlen[4]), and the
8-byte ``ServerResponseHdr`` (streamid[2] status[2] dlen[4]). Big-endian.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .._compat import SLOTS
from ..errors import ProtocolError
from . import constants as c
from .buffer import Writer

__all__ = ["HANDSHAKE", "Request", "ResponseHeader", "encode", "decode_header"]

# Three zero words, then 4, then ROOTD_PQ (2012).
HANDSHAKE = bytes(12) + struct.pack(">II", 4, c.ROOTD_PQ)

_HDR = struct.Struct(">HH16sI")
_RESP_HDR = struct.Struct(">HHI")


class Request:
    """Base of every client request.

    Subclasses set :attr:`opcode` and override :meth:`params` (which must
    write exactly 16 bytes), :meth:`payload`, and :attr:`signed`.
    """

    __slots__ = ()

    opcode: int = 0
    #: Whether a security level >= standard requires a ``kXR_sigver`` prefix.
    signed: bool = False
    #: Whether replaying this request after a reconnect is safe.
    idempotent: bool = True
    #: The ``kXR_bind`` data path this request uses, or 0 for the control
    #: link. Requests that can name one shadow this with a real attribute.
    pathid: int = 0
    #: Whether the *response* comes back over :attr:`pathid` rather than over
    #: the link the request was sent on. True for the reading opcodes, false
    #: for the writing ones - a write puts its data on the path and is
    #: answered on the control link.
    reply_on_path: bool = False

    def params(self, w: Writer) -> None:
        """Write the 16 parameter bytes. Default: all zero."""
        w.zeros(16)

    def payload(self) -> bytes:
        """Bytes after the header, counted in ``dlen``."""
        return b""

    def path_data(self) -> bytes:
        """Bytes that travel on :attr:`pathid` instead of on this link.

        ``dlen`` still counts them - the server sizes the read from the
        header it got on the control link and then takes the bytes off the
        bound socket - so they are declared here and sent elsewhere.
        """
        return b""

    def reply_cap(self) -> int:
        """Most bytes the answer to this request may add up to; 0 for no cap.

        A request that names how much it wants back - a read, a readv - knows
        the size of its own answer, so a server that keeps sending past it is
        either broken or hostile. Either way the client stops buying memory
        for it. Requests whose reply has no size known in advance (a dirlist,
        a query) are uncapped, and an error body is never capped at all: it is
        small, and truncating it would cost the reason for the failure.
        """
        return 0

    def trailer(self) -> bytes:
        """Bytes streamed after the frame that ``dlen`` does not count.

        Only ``kXR_writev`` has one: the server sizes its ``write_list`` from
        ``dlen`` and then reads the data that follows, so counting the data
        would make the descriptor block unparseable and the request invalid.
        Trailing bytes are outside the signed region for the same reason.
        """
        return b""

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


def encode(req: Request, streamid: int) -> bytes:
    """Serialise ``req`` into a complete wire frame."""
    w = Writer()
    req.params(w)
    params = w.bytes()
    if len(params) != 16:
        raise ProtocolError(
            f"{type(req).__name__}.params wrote {len(params)} bytes, expected 16"
        )
    body = req.payload()
    dlen = len(body) + len(req.path_data())
    if dlen > c.MAX_FRAME_PAYLOAD:
        raise ProtocolError(f"payload of {dlen} bytes exceeds the protocol maximum")
    return _HDR.pack(streamid & 0xFFFF, req.opcode, params, dlen) + body + req.trailer()


@dataclass(frozen=True, **SLOTS)
class ResponseHeader:
    """The 8-byte ``ServerResponseHdr``."""

    streamid: int
    status: int
    dlen: int

    def __repr__(self) -> str:
        return (
            f"ResponseHeader(streamid={self.streamid}, "
            f"status={c.status_name(self.status)}, dlen={self.dlen})"
        )


def decode_header(data: bytes | bytearray | memoryview) -> ResponseHeader:
    """Decode the leading 8 bytes of a response frame."""
    if len(data) < c.RESPONSE_HDRLEN:
        raise ProtocolError(f"response header needs 8 bytes, got {len(data)}")
    streamid, status, dlen = _RESP_HDR.unpack(bytes(data[:8]))
    if dlen > c.MAX_RESPONSE_BODY:
        raise ProtocolError(
            f"response declares a body of {dlen} bytes, past the "
            f"{c.MAX_RESPONSE_BODY} this client will buffer"
        )
    return ResponseHeader(streamid, status, dlen)
