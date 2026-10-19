"""Frame-level codecs.

Three fixed layouts: the 20-byte ``ClientInitHandShake``, the 24-byte
``ClientRequestHdr`` (streamid[2] requestid[2] params[16] dlen[4]), and the
8-byte ``ServerResponseHdr`` (streamid[2] status[2] dlen[4]). Big-endian.

:class:`Request` is the base every ``kXR_*`` encoder in
:mod:`xrdclient.proto.requests` derives from, and the place its hooks are
documented; :func:`encode_parts` is the one function that turns a request into
wire bytes. Both size limits are enforced here - ``MAX_FRAME_PAYLOAD`` on the
way out, ``MAX_RESPONSE_BODY`` on the way in - so no caller can skip them.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..errors import ProtocolError
from . import constants as c
from .buffer import Writer

__all__ = [
    "HANDSHAKE",
    "Payload",
    "Request",
    "ResponseHeader",
    "decode_header",
    "encode",
    "encode_parts",
    "header_fields",
]

#: What a request body may be handed over as. A write's payload is the
#: caller's own buffer - usually a :class:`memoryview` slice of it - and stays
#: that way until the kernel copies it out of the socket call.
Payload = bytes | bytearray | memoryview

# Three zero words, then 4, then ROOTD_PQ (2012): the ``ClientInitHandShake``
# that marks an XRootD client. The server answers on stream 0 with its
# protocol version and server type, ahead of the pipelined ``kXR_protocol``
# reply (see ``SessionMachine._handshaken``).
HANDSHAKE = bytes(12) + struct.pack(">II", 4, c.ROOTD_PQ)

_HDR = struct.Struct(">HH16sI")
_RESP_HDR = struct.Struct(">HHI")

#: A body at most this long rides in the same buffer as its header. Joining
#: costs a copy of the body; sending it separately costs a buffer of its own
#: in the socket call, which for a path or a login is the dearer of the two.
_INLINE = 4096

#: The name of the per-request fast path :meth:`Request.__init_subclass__` checks.
_FAST_PATH = "header_params"


class Request:
    """Base of every client request.

    Subclasses set :attr:`opcode` and override :meth:`params` (which must
    write exactly 16 bytes), :meth:`payload`, and :attr:`signed`. The other
    hooks - :meth:`header_params`, :meth:`path_data`, :meth:`reply_cap`,
    :meth:`trailer` - have safe defaults and are overridden only by the
    requests that need them; each is documented below once, so subclasses
    say only what their opcode puts there.

    Requests are plain value objects: built by the client layer, framed by
    the machine, and kept (in the machine's pending table) until answered so
    that a ``kXR_wait`` or a redirect can resend them. They must not change
    after submission. ``__slots__`` everywhere because the data path builds
    one per read.
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

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Drop an inherited ``header_params`` fast path that no longer matches ``params``."""
        super().__init_subclass__(**kwargs)
        # A precomputed layout is only the truth for the ``params`` it was
        # written beside. A class whose ``params`` resolves elsewhere - its
        # own, or a mixin's ahead of the parent in the MRO - must not inherit
        # the parent's layout, so it gets the general path back.
        owner = next(k for k in cls.__mro__ if _FAST_PATH in k.__dict__)
        if owner is not Request and cls.params is not getattr(owner, "params", None):
            # Rebinding a method on the class, which plain assignment would
            # spell as overwriting one by mistake.
            setattr(cls, _FAST_PATH, Request.header_params)

    def params(self, w: Writer) -> None:
        """Write the 16 parameter bytes. Default: all zero."""
        w.zeros(16)

    def header_params(self) -> bytes | bytearray:
        """The 16 parameter bytes exactly as :meth:`params` writes them.

        What :func:`encode` puts on the wire. The requests sent on every
        operation - a read, a stat, an open - override it with a single
        ``struct`` pack of the same layout; everything else goes through
        :meth:`params` and a :class:`~xrdclient.proto.buffer.Writer`.
        """
        w = Writer()
        self.params(w)
        return w.buffer

    def payload(self) -> Payload:
        """Bytes after the header, counted in ``dlen``.

        A request carrying the caller's data - a write - may hand back a view
        of that data rather than a copy; :func:`encode_parts` keeps it a view
        all the way to the socket.
        """
        return b""

    def path_data(self) -> Payload:
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
        """The class name alone; subclasses add the fields worth logging.

        No default repr lists fields, because some hold credentials
        (``Auth.cred``) or a user's data (``Write.data``).
        """
        return f"{type(self).__name__}()"


def encode(req: Request, streamid: int) -> bytes:
    """Serialise ``req`` into a complete wire frame, as one ``bytes``.

    For a caller that needs the frame whole - a signature over it, a
    checkpoint that embeds it. What goes to the socket is
    :func:`encode_parts`, which does not copy a large body to say the same.
    """
    parts = encode_parts(req, streamid)
    if len(parts) == 1 and type(parts[0]) is bytes:
        return parts[0]
    return b"".join(parts)


def encode_parts(req: Request, streamid: int) -> list[Payload]:
    """``req`` as the buffers that make up its frame, in wire order.

    The 24-byte header first, then the body and the trailer each as a buffer
    of its own - the very object the request handed over, so a write of a
    caller's ``memoryview`` puts that view, uncopied, in the list a vectored
    send (``sendmsg``) takes. A small body is joined to its header instead,
    since one buffer of a few hundred bytes is cheaper to send than two.
    """
    params = req.header_params()
    if len(params) != 16:
        raise ProtocolError(f"{type(req).__name__}.params wrote {len(params)} bytes, expected 16")
    body = req.payload()
    trailer = req.trailer()
    dlen = len(body) + len(req.path_data())
    if dlen > c.MAX_FRAME_PAYLOAD:
        raise ProtocolError(f"payload of {dlen} bytes exceeds the protocol maximum")
    head = _HDR.pack(streamid & 0xFFFF, req.opcode, params, dlen)
    if len(body) + len(trailer) <= _INLINE:
        # bytes + any buffer is a bytes; a request with no body is the header.
        return [head + body + trailer if body or trailer else head]
    parts: list[Payload] = [head]
    if body:
        parts.append(body)
    if trailer:
        parts.append(trailer)
    return parts


@dataclass(frozen=True, slots=True)
class ResponseHeader:
    """The 8-byte ``ServerResponseHdr``."""

    streamid: int
    status: int
    dlen: int

    def __repr__(self) -> str:
        """The status by name (``kXR_ok``), not number, for readable traces."""
        return (
            f"ResponseHeader(streamid={self.streamid}, "
            f"status={c.status_name(self.status)}, dlen={self.dlen})"
        )


def header_fields(data: bytes | bytearray | memoryview, offset: int = 0) -> tuple[int, int, int]:
    """``(streamid, status, dlen)`` of the response header at ``offset``.

    The allocation-free form of :func:`decode_header`, for the receive path,
    which reads one of these per reply. The caller has checked that eight
    bytes are there.
    """
    streamid, status, dlen = _RESP_HDR.unpack_from(data, offset)
    if dlen > c.MAX_RESPONSE_BODY:
        raise ProtocolError(
            f"response declares a body of {dlen} bytes, past the "
            f"{c.MAX_RESPONSE_BODY} this client will buffer"
        )
    return streamid, status, dlen


def decode_header(data: bytes | bytearray | memoryview) -> ResponseHeader:
    """Decode the leading 8 bytes of a response frame."""
    if len(data) < c.RESPONSE_HDRLEN:
        raise ProtocolError(f"response header needs 8 bytes, got {len(data)}")
    return ResponseHeader(*header_fields(data))
