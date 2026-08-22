"""The ROOT on-disk encoding, read one field at a time.

Everything ROOT writes is big-endian and self-describing: a record starts with
its byte count and version, so a reader that does not know a class can still
step over it. That is what makes a partial reader like this one honest - it
never has to guess a length.

Positions are counted the way ROOT counts them, from the start of the key
rather than the start of the decompressed payload, because the class and
object back-references written into the stream are absolute in those terms.
"""

from __future__ import annotations

import array
import datetime
import struct
import sys
from typing import Any

from .errors import FormatError, UnsupportedFeatureError

__all__ = ["Buffer", "as_datetime", "to_native"]

if sys.byteorder == "little":  # pragma: no cover - one of two, decided by the machine

    def to_native(values: array.array[Any]) -> array.array[Any]:
        """ROOT writes big-endian; make it the order this machine reads."""
        values.byteswap()
        return values

else:  # pragma: no cover - big-endian machines, where ROOT's order is ours

    def to_native(values: array.array[Any]) -> array.array[Any]:
        return values


def as_datetime(packed: int) -> datetime.datetime:
    """ROOT's packed date word, which counts its years from 1995.

    >>> as_datetime(0x2C44F105)
    datetime.datetime(2006, 1, 2, 15, 4, 5)
    """
    return datetime.datetime(
        (packed >> 26) + 1995,
        (packed >> 22) & 0xF,
        (packed >> 17) & 0x1F,
        (packed >> 12) & 0x1F,
        (packed >> 6) & 0x3F,
        packed & 0x3F,
    )


BYTE_COUNT_MASK = 0x40000000
NEW_CLASS_TAG = 0xFFFFFFFF
CLASS_MASK = 0x80000000
MAP_OFFSET = 2
IS_REFERENCED = 1 << 4
#: The bits a ``TClonesArray`` carries that say how it wrote what it holds:
#: the first asks ROOT to write the objects field by field rather than one
#: after another, and the second takes that back for a class that cannot be.
BYPASS_STREAMER = 1 << 12
NO_MEMBER_WISE = 1 << 17

_U8 = struct.Struct(">B")
_I8 = struct.Struct(">b")
_U16 = struct.Struct(">H")
_I16 = struct.Struct(">h")
_U32 = struct.Struct(">I")
_I32 = struct.Struct(">i")
_I64 = struct.Struct(">q")
_F64 = struct.Struct(">d")


class Buffer:
    """A cursor over one decompressed ROOT record.

        >>> buf = Buffer(b"\\x00\\x2a")
        >>> buf.u16()
        42

    ``offset`` is the length of the key this payload came from, and ``refs``
    is the class and object map shared by everything read out of it.
    """

    __slots__ = ("data", "pos", "offset", "refs")

    def __init__(self, data: bytes, offset: int = 0) -> None:
        self.data = data
        self.offset = offset
        self.pos = offset
        self.refs: dict[int, object] = {}

    def __repr__(self) -> str:
        return f"<Buffer at {self.pos} of {self.offset + len(self.data)}>"

    @property
    def remaining(self) -> int:
        """Bytes left, in case a caller wants to stop before running out."""
        return len(self.data) - (self.pos - self.offset)

    def _take(self, form: struct.Struct) -> Any:
        index = self.pos - self.offset
        if index + form.size > len(self.data):
            raise FormatError(f"the record ends {form.size} bytes before it said it would")
        self.pos += form.size
        return form.unpack_from(self.data, index)[0]

    def u8(self) -> int:
        return int(self._take(_U8))

    def i8(self) -> int:
        return int(self._take(_I8))

    def u16(self) -> int:
        return int(self._take(_U16))

    def i16(self) -> int:
        return int(self._take(_I16))

    def u32(self) -> int:
        return int(self._take(_U32))

    def i32(self) -> int:
        return int(self._take(_I32))

    def i64(self) -> int:
        return int(self._take(_I64))

    def f64(self) -> float:
        return float(self._take(_F64))

    def bool(self) -> bool:
        return self.u8() != 0

    def take(self, count: int) -> bytes:
        """``count`` raw bytes."""
        index = self.pos - self.offset
        if index + count > len(self.data):
            raise FormatError(f"asked for {count} bytes with {self.remaining} left")
        self.pos += count
        return self.data[index : index + count]

    def string(self) -> str:
        """A ``TString``: one length byte, or ``255`` and then four."""
        length = self.u8()
        if length == 255:
            length = self.i32()
        return self.take(length).decode("utf-8", "surrogateescape")

    def cstring(self) -> str:
        """A NUL-terminated name, which is how class names are written."""
        index = self.pos - self.offset
        end = self.data.find(b"\x00", index)
        if end < 0:
            raise FormatError("a class name ran off the end of the record")
        self.pos += end - index + 1
        return self.data[index:end].decode("utf-8", "surrogateescape")

    # -- records ----------------------------------------------------------

    def header(self) -> tuple[int, int | None]:
        """The version of the record starting here, and where it ends.

        The end is ``None`` for the rare record written without a byte count,
        which is legal and means only that it cannot be skipped over.
        """
        start = self.pos
        count = self.u32()
        if count & BYTE_COUNT_MASK:
            end = self.pos + (count & ~BYTE_COUNT_MASK)
        else:
            self.pos = start
            end = None
        return self.u16(), end

    def resume(self, end: int | None) -> None:
        """Carry on after a record, wherever the caller stopped inside it.

        A record whose byte count was written says where it ends and the
        cursor goes there; the rare one written without a byte count leaves
        the cursor where the fields ran out, which is all that can be done.
        """
        if end is not None:
            self.pos = end

    def skip_record(self) -> int:
        """Step over a whole record, and say which version it was.

        This is how a reader that only cares about a tree walks past the
        drawing attributes nobody streaming data will ever look at.
        """
        version, end = self.header()
        if end is None:
            raise FormatError("a record without a byte count cannot be skipped")
        self.pos = end
        return version

    def tobject(self) -> tuple[int, int]:
        """A ``TObject``: its identifier and bits, once the version is past."""
        self.u16()
        unique, bits = self.u32(), self.u32()
        if bits & IS_REFERENCED:
            self.u16()
        return unique, bits

    def named(self) -> tuple[str, str]:
        """A ``TNamed``: the name and title every ROOT object carries."""
        _version, end = self.header()
        self.tobject()
        name, title = self.string(), self.string()
        self.resume(end)
        return name, title

    # -- arrays -----------------------------------------------------------

    def i32s(self, count: int) -> list[int]:
        return list(struct.unpack_from(f">{count}i", self.data, self._span(count * 4)))

    def i64s(self, count: int) -> list[int]:
        return list(struct.unpack_from(f">{count}q", self.data, self._span(count * 8)))

    def _span(self, size: int) -> int:
        index = self.pos - self.offset
        if index + size > len(self.data):
            raise FormatError(f"an array of {size} bytes does not fit in the record")
        self.pos += size
        return index

    # -- references -------------------------------------------------------

    def any(self, classes: dict[str, Any]) -> Any:
        """Read whatever object comes next, by the class name in the stream.

        ``classes`` maps a ROOT class name to a callable taking this buffer.
        A name that is not in it is stepped over and comes back as its own
        string, so an unknown branch in a tree costs a skip rather than a
        failure - the caller decides whether it needed that one.
        """
        start = self.pos
        count = self.u32()
        if not count & BYTE_COUNT_MASK or count == NEW_CLASS_TAG:
            # No byte count: what was read is the tag itself, and there is no
            # length to step over if the class turns out to be unknown.
            tag, after, end = count, self.pos, None
        else:
            after = self.pos
            end = start + 4 + (count & ~BYTE_COUNT_MASK)
            tag = self.u32()

        if not tag & CLASS_MASK:
            if tag == 0:
                return None
            return self.refs.get(tag)  # an object written once and pointed at since

        if tag == NEW_CLASS_TAG:
            name = self.cstring()
            self.refs[after + MAP_OFFSET] = name
        else:
            name = str(self.refs.get(tag & ~CLASS_MASK, ""))
            if not name:
                raise FormatError(f"a class reference at {start} points nowhere")

        reader = classes.get(name)
        if reader is None:
            if end is None:
                raise FormatError(f"a {name} at {start} was written with no length to skip")
            self.pos = end
            return name
        obj = reader(self)
        self.refs[start + MAP_OFFSET] = obj
        return obj

    def tlist(self, classes: dict[str, Any]) -> list[Any]:
        """A ``TList``: like a ``TObjArray``, but each entry carries an option.

        The option is a string nobody reading data wants, written after the
        object it belongs to, so it has to be stepped over one at a time.
        """
        version, end = self.header()
        if version <= 3:
            raise FormatError(f"a TList of version {version} is older than any that names itself")
        self.tobject()
        self.string()
        items = []
        for _ in range(self.i32()):
            items.append(self.any(classes))
            self.string()  # the option it was added under, which is drawing, not data
        self.resume(end)
        return items

    def objarray(self, classes: dict[str, Any]) -> list[Any]:
        """A ``TObjArray``: the container ROOT keeps branches and leaves in."""
        _version, end = self.header()
        self.tobject()
        self.string()  # the array's own name, always empty in a tree
        size, _low = self.i32(), self.i32()
        items = [self.any(classes) for _ in range(size)]
        self.resume(end)
        return items

    def clones(self, classes: dict[str, Any], fields: Any = None) -> list[Any]:
        """A ``TClonesArray``: many objects of one class, the class written once.

        ROOT keeps a pool of objects of a single class and reuses it entry
        after entry, which is the whole point of the thing, so the class is
        named at the front of the array rather than in front of every object.
        A byte before each object says whether that slot was ever filled; an
        empty one comes back as ``None`` rather than shifting the rest along.

        An array told to bypass its streamer is written field by field instead
        - every object's first member, then every object's second - with no
        byte per slot, since an array written that way cannot have holes.
        ``fields`` is asked for the members of the held class, in order, when
        the array turns out to be one of those.
        """
        version, end = self.header()
        bits = self.tobject()[1] if version > 2 else 0
        if version > 1:
            self.string()  # the name the array was given
        held = self.string().partition(";")[0]  # the class, and the version of it
        count, _low = abs(self.i32()), self.i32()
        if bits & BYPASS_STREAMER and not bits & NO_MEMBER_WISE:
            return self._memberwise_clones(held, count, end, fields)
        return self._streamed_clones(held, count, end, classes)

    def _memberwise_clones(self, held: str, count: int, end: int | None, fields: Any) -> list[Any]:
        steps = fields(held) if fields is not None else None
        if steps is None:
            raise UnsupportedFeatureError(
                f"a TClonesArray of {held} was written field by field, and this "
                f"file's streamer information does not describe {held} well "
                f"enough to read it that way"
            )
        rows: list[Any] = [{} for _ in range(count)]
        for label, step in steps:
            for row in rows:
                row[label] = step(self, row)
        self._require_clone_end(held, count, end)
        self.resume(end)
        return rows

    def _require_clone_end(self, held: str, count: int, end: int | None) -> None:
        if end is not None and self.pos != end:
            raise FormatError(
                f"a TClonesArray of {count} {held} written field by field ended "
                f"{abs(end - self.pos)} bytes from where it said it would, so "
                f"what was read out of it cannot be trusted"
            )

    def _streamed_clones(
        self, held: str, count: int, end: int | None, classes: dict[str, Any]
    ) -> list[Any]:
        one = classes.get(held)
        if one is None:
            raise UnsupportedFeatureError(
                f"a TClonesArray holds {held}, which this file's streamer information "
                f"does not describe well enough to read one"
            )
        items = [one(self) if self.u8() else None for _ in range(count)]
        self.resume(end)
        return items
