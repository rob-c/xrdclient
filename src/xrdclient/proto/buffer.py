"""Big-endian read/write cursors.

Every codec goes through these so that a truncated frame raises
:class:`~xrdclient.errors.ProtocolError` with context rather than ``struct.error``.
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
    """A bounds-checked big-endian cursor over a read-only buffer."""

    __slots__ = ("_buf", "_pos", "_what")

    def __init__(self, data: bytes | bytearray | memoryview, what: str = "frame") -> None:
        self._buf = memoryview(data).toreadonly()
        self._pos = 0
        self._what = what

    def __len__(self) -> int:
        return len(self._buf) - self._pos

    @property
    def remaining(self) -> int:
        return len(self._buf) - self._pos

    @property
    def position(self) -> int:
        return self._pos

    def _take(self, n: int) -> memoryview:
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
        return self._unpack(_U8)

    def u16(self) -> int:
        return self._unpack(_U16)

    def i16(self) -> int:
        return self._unpack(_I16)

    def u32(self) -> int:
        return self._unpack(_U32)

    def i32(self) -> int:
        return self._unpack(_I32)

    def u64(self) -> int:
        return self._unpack(_U64)

    def i64(self) -> int:
        return self._unpack(_I64)

    def bytes(self, n: int) -> builtins.bytes:
        return self._take(n).tobytes()

    def view(self, n: int) -> memoryview:
        """``n`` bytes without copying."""
        return self._take(n)

    def rest(self) -> builtins.bytes:
        return self._take(self.remaining).tobytes()

    def skip(self, n: int) -> None:
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
    """A big-endian append-only byte builder."""

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = bytearray()

    def __len__(self) -> int:
        return len(self._buf)

    def u8(self, v: int) -> Writer:
        self._buf += _U8.pack(v)
        return self

    def u16(self, v: int) -> Writer:
        self._buf += _U16.pack(v)
        return self

    def u32(self, v: int) -> Writer:
        self._buf += _U32.pack(v)
        return self

    def i32(self, v: int) -> Writer:
        self._buf += _I32.pack(v)
        return self

    def u64(self, v: int) -> Writer:
        self._buf += _U64.pack(v)
        return self

    def i64(self, v: int) -> Writer:
        self._buf += _I64.pack(v)
        return self

    def raw(self, data: bytes | bytearray | memoryview) -> Writer:
        self._buf += data
        return self

    def text(self, s: str, *, nul: bool = False) -> Writer:
        self._buf += s.encode("utf-8")
        if nul:
            self._buf += b"\x00"
        return self

    def zeros(self, n: int) -> Writer:
        self._buf += bytes(n)
        return self

    def padded(self, s: str | bytes, width: int) -> Writer:
        """NUL-pad (or truncate) to exactly ``width`` bytes."""
        b = s.encode("utf-8") if isinstance(s, str) else bytes(s)
        self._buf += b[:width].ljust(width, b"\x00")
        return self

    def bytes(self) -> builtins.bytes:
        return builtins.bytes(self._buf)
