"""Big-endian read/write cursors.

Every codec goes through these so that a truncated frame raises
:class:`~xrdclient.errors.ProtocolError` with context rather than ``struct.error``.

XRootD is big-endian ("network order") throughout - every ``kXR_unt16``,
``kXR_int32`` and ``kXR_int64`` in ``XProtocol.hh`` - so there is no byte-order
switch here. The typed methods are named after the width and signedness the
spec declares a field with, which is what a reader cross-checking a layout
against ``XProtocol.hh`` looks for: ``u8`` is ``kXR_char``, ``u16`` is
``kXR_unt16``, ``i32`` is ``kXR_int32`` and so on. Signedness matters on the
wire: an offset is ``kXR_int64`` and a length ``kXR_int32``, so a value past
the signed range raises from ``struct`` instead of silently wrapping.

Neither class is thread-safe; each is owned by the one codec call that made it.
"""

from __future__ import annotations

import builtins
import struct

from ..errors import ProtocolError

__all__ = ["Reader", "Writer"]

_U8 = struct.Struct(">B")
_U16 = struct.Struct(">H")
_I16 = struct.Struct(">h")
_U32 = struct.Struct(">I")
_I32 = struct.Struct(">i")
_U64 = struct.Struct(">Q")
_I64 = struct.Struct(">q")


class Reader:
    """A bounds-checked big-endian cursor over a read-only buffer.

    Every read either returns exactly the bytes asked for and advances, or
    raises :class:`~xrdclient.errors.ProtocolError` naming ``what`` and the
    offset - so a parser can be written as a straight sequence of field reads
    with no length checks of its own, and a short or lying server reply turns
    into one clear error instead of an ``IndexError`` deep in a codec.
    """

    __slots__ = ("_buf", "_pos", "_what")

    def __init__(self, data: bytes | bytearray | memoryview, what: str = "frame") -> None:
        """Wrap ``data`` without copying; ``what`` labels it in error messages.

        The view is made read-only so that a parser cannot scribble on a
        receive buffer the state machine still owns.
        """
        self._buf = memoryview(data).toreadonly()
        self._pos = 0
        self._what = what

    def __len__(self) -> int:
        """Bytes not yet consumed - so ``if reader:`` means "more to parse"."""
        return len(self._buf) - self._pos

    @property
    def remaining(self) -> int:
        """Bytes not yet consumed."""
        return len(self._buf) - self._pos

    @property
    def position(self) -> int:
        """Offset of the next byte to be read, from the start of the buffer."""
        return self._pos

    def _take(self, n: int) -> memoryview:
        """Consume ``n`` bytes as a view, or raise without moving the cursor.

        The single bounds check every other read goes through. A negative
        ``n`` - a length field a server declared as a huge unsigned value and
        a caller subtracted from - is refused rather than read as "from the
        end", which slicing would otherwise do.
        """
        if n < 0:
            raise ProtocolError(f"{self._what}: negative read of {n} bytes")
        end = self._pos + n
        if end > len(self._buf):
            raise ProtocolError(
                f"{self._what}: truncated - wanted {n} bytes at offset {self._pos}, "
                f"only {self.remaining} remain"
            )
        chunk = self._buf[self._pos : end]
        self._pos = end
        return chunk

    def _unpack(self, s: struct.Struct) -> int:
        return int(s.unpack(self._take(s.size))[0])

    def u8(self) -> int:
        """A ``kXR_char``."""
        return self._unpack(_U8)

    def u16(self) -> int:
        """A ``kXR_unt16``."""
        return self._unpack(_U16)

    def i16(self) -> int:
        """A ``kXR_int16``."""
        return self._unpack(_I16)

    def u32(self) -> int:
        """A ``kXR_unt32``."""
        return self._unpack(_U32)

    def i32(self) -> int:
        """A ``kXR_int32`` - lengths, error numbers, wait seconds."""
        return self._unpack(_I32)

    def u64(self) -> int:
        """A ``kXR_unt64``."""
        return self._unpack(_U64)

    def i64(self) -> int:
        """A ``kXR_int64`` - file offsets and sizes."""
        return self._unpack(_I64)

    def bytes(self, n: int) -> builtins.bytes:
        """``n`` bytes as an independent copy, safe to keep after the frame is gone."""
        return self._take(n).tobytes()

    def view(self, n: int) -> memoryview:
        """``n`` bytes without copying."""
        return self._take(n)

    def rest(self) -> builtins.bytes:
        """Everything left, copied; the reader is exhausted afterwards."""
        return self._take(self.remaining).tobytes()

    def skip(self, n: int) -> None:
        """Step over ``n`` reserved or ignored bytes, still bounds-checked."""
        self._take(n)

    def cstring(self, n: int | None = None) -> str:
        """A string of ``n`` bytes, or up to the next NUL (or the end).

        With a width, the whole field is consumed and the NUL padding
        stripped; without one, the terminator is consumed but not returned,
        so a fixed-length string can be followed by more fields.
        """
        if n is not None:
            raw = self._take(n).tobytes()
            return raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
        tail = self._buf[self._pos :].tobytes()
        end = tail.find(b"\x00")
        self._pos += len(tail) if end < 0 else end + 1
        return (tail if end < 0 else tail[:end]).decode("utf-8", "replace")


class Writer:
    """A big-endian append-only byte builder.

    Every method returns the writer, so a layout reads left to right in the
    order of the spec's struct: ``w.padded(fh, 4).i64(offset).i32(length)``.
    Values out of range for their field raise ``struct.error`` - that is a
    bug in the caller, not a wire condition, so it is not wrapped.
    """

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        """Start an empty frame."""
        self._buf = bytearray()

    def __len__(self) -> int:
        """Bytes written so far - how ``params`` checks it wrote 16."""
        return len(self._buf)

    def u8(self, v: int) -> Writer:
        """Append a ``kXR_char``."""
        self._buf += _U8.pack(v)
        return self

    def u16(self, v: int) -> Writer:
        """Append a ``kXR_unt16``."""
        self._buf += _U16.pack(v)
        return self

    def u32(self, v: int) -> Writer:
        """Append a ``kXR_unt32``."""
        self._buf += _U32.pack(v)
        return self

    def i32(self, v: int) -> Writer:
        """Append a ``kXR_int32``."""
        self._buf += _I32.pack(v)
        return self

    def u64(self, v: int) -> Writer:
        """Append a ``kXR_unt64``."""
        self._buf += _U64.pack(v)
        return self

    def i64(self, v: int) -> Writer:
        """Append a ``kXR_int64``."""
        self._buf += _I64.pack(v)
        return self

    def raw(self, data: bytes | bytearray | memoryview) -> Writer:
        """Append ``data`` as is - an opaque value, a precomputed block."""
        self._buf += data
        return self

    def text(self, s: str, *, nul: bool = False) -> Writer:
        """Append UTF-8 text, NUL-terminated when the field is a C string.

        Paths in a request body are *not* terminated (``dlen`` delimits
        them); names inside a structured body such as ``kXR_fattr`` are.
        """
        self._buf += s.encode("utf-8")
        if nul:
            self._buf += b"\x00"
        return self

    def zeros(self, n: int) -> Writer:
        """Append ``n`` zero bytes - the spec's ``reserved`` fields."""
        self._buf += bytes(n)
        return self

    def padded(self, s: str | bytes, width: int) -> Writer:
        """NUL-pad (or truncate) to exactly ``width`` bytes."""
        b = s.encode("utf-8") if isinstance(s, str) else bytes(s)
        self._buf += b[:width].ljust(width, b"\x00")
        return self

    def bytes(self) -> builtins.bytes:
        """An immutable copy of everything written."""
        return builtins.bytes(self._buf)

    @property
    def buffer(self) -> bytearray:
        """What has been written so far, uncopied - for a caller that only
        reads it before the builder is dropped."""
        return self._buf
