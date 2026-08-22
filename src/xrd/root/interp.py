"""From what a branch says it holds to what Python gets back.

A column is one of three shapes and no more: a flat run of numbers, rows of
numbers of differing lengths, or one Python object per entry. Everything
ROOT can write into a branch - a member split out of a C++ class, a
``vector<float>``, a ``map<string,short>`` - lands in one of the three, and
whatever does not land in one of them is refused by name rather than read
approximately. A plausible misreading of physics data is worse than a
refusal, which is why this module says no as precisely as it says yes.
"""

from __future__ import annotations

import array
import datetime
import math
import struct
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .._compat import zip_strict
from .buffer import Buffer, as_datetime, to_native
from .cxx import SEQUENCES, Mapping, Pair, Prim, Seq, Str, parse, py_name
from .errors import FormatError, UnsupportedFeatureError
from .graph import GRAPHS, Graph
from .hist import HISTOGRAMS, Histogram

if TYPE_CHECKING:
    from .file import Source
    from .objects import BranchRecord, LeafRecord
    from .streamers import Member
    from .tree import Basket

__all__ = ["Column", "Flat", "Rows", "Values", "Members", "Refused", "build", "whole_object"]

#: Set in a record's version when a container was written field by field
#: rather than object by object: all the keys, then all the values.
MEMBER_WISE = 0x4000

#: A record header - four bytes of byte count, two of version.
RECORD = 6

#: The streamer type of a member, offset by the shape it was declared in:
#: ``fType`` is the fundamental type plus one of these.
OFFSET_L = 20  # a fixed-size array, ``x[10]``
OFFSET_P = 40  # a pointer to a counted one, ``x[n]``

#: The streamer types that are a class instance written into the entry: a base
#: class, and a member held by value with or without a dictionary of its own.
OBJECTS = (0, 61, 62)

#: The streamer types that are a pointer: the ones the class promises are
#: always there, which ROOT writes in place, and the ones that may be null and
#: so carry the name of the class they point at - or, for an object written
#: earlier in the same entry, a reference back to where it was written.
OBJECTS_HELD = (63, 68)
OBJECTS_POINTED = (64, 69)

#: The collections that stream themselves rather than by the members they
#: declare, each holding objects that say which class they are: a list, which
#: keeps beside each object the option it was added under, and an array, which
#: keeps its length instead.
LISTS = ("TList", "THashList", "TSortedList")
OBJECT_ARRAYS = ("TObjArray",)

#: The array of one class, which names that class once at the front rather
#: than in front of every object it holds.
CLONES = ("TClonesArray",)

#: The arrays ROOT's own kit keeps numbers in - the contents of a histogram is
#: one - each of which streams itself as a count and then that many values,
#: with no record round it and nothing of the base class it inherits.
ARRAYS = {
    "TArrayC": Prim("int8", "b", 1),
    "TArrayS": Prim("int16", "h", 2),
    "TArrayI": Prim("int32", "i", 4),
    "TArrayL": Prim("int64", "q", 8),
    "TArrayL64": Prim("int64", "q", 8),
    "TArrayF": Prim("float32", "f", 4),
    "TArrayD": Prim("float64", "d", 8),
}

#: Everything above, which writes itself its own way: what the file says its
#: members are would not read one of these, and neither would reading it a
#: member at a time.
SELF_STREAMING = frozenset(LISTS + OBJECT_ARRAYS + CLONES + tuple(ARRAYS))

#: The two bases ROOT gives its own classes, which stream themselves rather
#: than being written out the way the streamer information describes them.
TOBJECT, TNAMED = 66, 67

#: The two fundamental types that are not their own width: a float squeezed
#: into a range the declaration's comment spells out. ``True`` for the one
#: that is a ``double`` when the comment asks for nothing in particular.
PACKED = {9: True, 19: False}

#: The fundamental types, as ROOT numbers them in a streamer.
BASIC: dict[int, Prim] = {
    1: Prim("int8", "b", 1),
    2: Prim("int16", "h", 2),
    3: Prim("int32", "i", 4),
    4: Prim("int64", "q", 8),
    5: Prim("float32", "f", 4),
    6: Prim("int32", "i", 4),  # a counter, which is an int that another branch uses
    8: Prim("float64", "d", 8),
    11: Prim("uint8", "B", 1),
    12: Prim("uint16", "H", 2),
    13: Prim("uint32", "I", 4),
    14: Prim("uint64", "Q", 8),
    15: Prim("uint32", "I", 4),  # a bit field, which is written as the integer it is
    16: Prim("int64", "q", 8),
    17: Prim("uint64", "Q", 8),
    18: Prim("bool", "b", 1),
}

#: What the streamer types this reader will not decode actually are, so that
#: a refusal names the thing rather than the number.
KINDS = {
    0: "a base class written into the entry",
    7: "a C string pointer",
    10: "a legacy char",
    61: "an object",
    62: "an object without a dictionary",
    63: "a pointer to an object",
    64: "a pointer to an object",
    66: "a TObject",
    67: "a TNamed",
    365: "an STL string",
    500: "a class with a streamer of its own",
    501: "a loop over a member of another class",
}

#: How a packed float column turns its bytes into the doubles they stand for.
Unpack = Callable[[bytes], "array.array[Any]"]

_COUNT = struct.Struct(">I")
_BITS = struct.Struct(">I")
_FLOAT = struct.Struct(">f")

#: How ROOT lets a packing range be written in units of pi, longest first.
_PI = (
    ("2pi", 2 * math.pi),
    ("2*pi", 2 * math.pi),
    ("twopi", 2 * math.pi),
    ("pi/2", math.pi / 2),
    ("pi/4", math.pi / 4),
    ("pi", math.pi),
)


class Column:
    """How one branch's bytes become values, and what to call them."""

    __slots__ = ("typename",)
    #: ``flat``, ``rows``, ``values`` or ``refused``.
    kind = "refused"

    def __init__(self, typename: str | None) -> None:
        self.typename = typename

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.typename or 'unreadable'}>"


class Numeric(Column):
    """A column of numbers: how wide one is on disk, and how it decodes.

    Nearly every column is the machine's own numbers in the other byte order,
    which is a byte swap and nothing else. A ``Double32_t`` is not: it is a
    double squeezed into three or four bytes, and ``unpack`` is how those
    bytes come back out as doubles.
    """

    __slots__ = ("typecode", "itemsize", "unpack")

    def __init__(self, prim: Prim, unpack: Unpack | None = None) -> None:
        super().__init__(prim.typename)
        self.typecode = prim.typecode
        #: Bytes one value takes in the file, which is not always its width here.
        self.itemsize = prim.itemsize
        self.unpack = unpack

    def decode(self, raw: bytes) -> array.array[Any]:
        """A run of values, from the bytes the file holds them in."""
        if self.unpack is not None:
            return self.unpack(raw)
        return to_native(array.array(self.typecode, raw))


class Flat(Numeric):
    """The same number of values in every entry, one after another."""

    __slots__ = ("length",)
    kind = "flat"

    def __init__(
        self, prim: Prim, length: int, unpack: Unpack | None = None
    ) -> None:
        super().__init__(prim, unpack)
        self.length = length


class Rows(Numeric):
    """A different number of values in each entry, and where each row is."""

    __slots__ = ("header", "counted")
    kind = "rows"

    def __init__(
        self,
        prim: Prim,
        header: int,
        counted: bool,
        unpack: Unpack | None = None,
    ) -> None:
        super().__init__(prim, unpack)
        #: Bytes in front of the values: none, a marker byte, or a record.
        self.header = header
        #: Does the entry say how many values it holds, or only where it ends?
        self.counted = counted

    def span(self, basket: Basket, entry: int, offset: int) -> tuple[int, int]:
        """Where this entry's values start and stop inside the basket."""
        start = basket.start_of(entry, offset) + self.header
        end = basket.end_of(entry)
        if not self.counted:
            return start, end
        count = _COUNT.unpack_from(basket.data, start - 4)[0]
        stop = start + count * self.itemsize
        if stop > end:
            raise FormatError(
                f"an entry says it holds {count} values, which do not fit in the "
                f"{end - start} bytes it was written into"
            )
        return start, stop


class Values(Column):
    """One Python object per entry: a string, a list, a dict."""

    __slots__ = ("_read",)
    kind = "values"

    def __init__(self, typename: str, read: Callable[[Buffer], Any]) -> None:
        super().__init__(typename)
        self._read = read

    def value(self, buf: Buffer, at: int) -> Any:
        buf.pos = at
        return self._read(buf)


class Members(Column):
    """A branch with no bytes of its own, whose members are the branches under it.

    This is what ROOT leaves behind when it splits a C++ object: the object
    itself is a branch holding nothing, and each member is a branch of its own
    beneath it. There is nothing here to decode - the values come from the
    members - so this class carries only the name of the shape they go back
    into, which is a plain :class:`dict` per entry.
    """

    __slots__ = ()
    kind = "members"

    def __init__(self) -> None:
        super().__init__("dict")


class Refused(Column):
    """A column this reader will not guess at, and the reason in words."""

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        super().__init__(None)
        self.reason = reason


# -- reading one value ------------------------------------------------------


def _items(node: Any, buf: Buffer, count: int) -> Any:
    """``count`` values of one type, written one after another."""
    if isinstance(node, Prim):
        return to_native(array.array(node.typecode, buf.take(count * node.itemsize)))
    if isinstance(node, Str):
        return [buf.string() for _ in range(count)]
    return [_items(node.item, buf, buf.u32()) for _ in range(count)]


def _block(node: Any, buf: Buffer, count: int) -> Any:
    """One member-wise block: a run of values of one type, all together.

    A block of a class is introduced by a record of its own; a block of
    numbers, or of ``TString``, is simply the values. An empty container has
    no blocks at all, not even the records that would introduce them.
    """
    if count and not isinstance(node, Prim) and not (isinstance(node, Str) and not node.record):
        buf.header()
    return _items(node, buf, count)


def _string(buf: Buffer) -> str:
    """A string standing alone in the entry, as a top-level branch writes it."""
    return buf.string()


def _member_string(buf: Buffer) -> str:
    """A string that is a member of a class, which carries a record header."""
    buf.header()
    return buf.string()


def _sequence(item: Any) -> Callable[[Buffer], Any]:
    def read(buf: Buffer) -> Any:
        version, end = buf.header()
        if isinstance(item, Pair):
            # A container of pairs is written like a map that is not one: all
            # the firsts in a block, then all the seconds.
            if not version & MEMBER_WISE:
                raise UnsupportedFeatureError(
                    "this container of pairs was written pair by pair, which this "
                    "reader has never seen a file do and will not decode on a guess"
                )
            if buf.i16() <= 0:
                buf.u32()  # a pair has no version of its own, only a checksum
            count = buf.u32()
            firsts = _block(item.first, buf, count)
            seconds = _block(item.second, buf, count)
            if end is not None and buf.pos != end:
                raise FormatError(
                    f"a container of {count} pairs ended {abs(end - buf.pos)} bytes "
                    f"from where it said it would, so what was read out of it "
                    f"cannot be trusted"
                )
            return list(zip_strict(firsts, seconds))
        if version & MEMBER_WISE:
            raise UnsupportedFeatureError(
                "this container was written field by field, which happens for a "
                "container of C++ objects and is not a shape this reader decodes"
            )
        return _items(item, buf, buf.u32())

    return read


def _mapping(node: Mapping) -> Callable[[Buffer], Any]:
    def read(buf: Buffer) -> Any:
        version, _end = buf.header()
        if not version & MEMBER_WISE:
            raise UnsupportedFeatureError(
                "this map was written pair by pair, which this reader has never "
                "seen a file do and will not decode on a guess"
            )
        if buf.i16() <= 0:
            buf.u32()  # a class with no version of its own says so with a checksum
        count = buf.u32()
        keys = _block(node.key, buf, count)
        values = _block(node.value, buf, count)
        return dict(zip_strict(keys, values))

    return read


# -- deciding what a column is ----------------------------------------------


def _nested(node: Any) -> str:
    """Why a type below the top of an entry cannot be read, if it cannot."""
    if isinstance(node, Mapping):
        return "a map inside another container, which no file this reader has met writes"
    if isinstance(node, Pair):
        return "a pair below the top of its container, which no file this reader has met writes"
    if isinstance(node, Seq):
        return _nested(node.item)
    return ""


def _unusable(node: Any) -> str:
    """Why a whole column cannot be read, if it cannot."""
    if isinstance(node, Pair):
        return "a pair standing on its own, which no file this reader has met writes"
    if isinstance(node, Mapping):
        if not isinstance(node.key, (Prim, Str)):
            return "a map keyed by a container, which is not a thing a dict can be keyed by"
        return _nested(node.value)
    if isinstance(node, Seq) and isinstance(node.item, Pair):
        pair = node.item
        if not isinstance(pair.first, (Prim, Str)) or not isinstance(pair.second, (Prim, Str)):
            return "a pair holding a container, which no file this reader has met writes"
        return ""
    return _nested(node)


def _number(text: str) -> float:
    """One end of a packing range, which ROOT lets you write in units of pi."""
    for spelling, value in _PI:
        if spelling in text:
            return -value if "-" in text else value
    return float(text)


def _range(title: str) -> tuple[float, float, float] | None:
    """``xmin``, ``xmax`` and the scale factor a title asks for.

    All zero means the title says nothing, which is itself a recipe: the
    default packing for the type. ``None`` means it says something this
    reader cannot make sense of, which is worth refusing over.
    """
    beg, end = title.rfind("["), title.rfind("]")
    if beg < 0 or end < 0:
        return 0.0, 0.0, 0.0
    if beg > 0:
        slash = title.rfind("/", 0, beg)
        if slash < 0 or slash + 2 != beg:
            return 0.0, 0.0, 0.0  # a title like ``x[10]``: an array, not a range
    parts = [part.strip().lower() for part in title[beg + 1 : end].split(",")]
    if len(parts) == 1:
        return 0.0, 0.0, 0.0
    if len(parts) > 3:
        return None
    nbits = 32
    try:
        if len(parts) == 3:
            nbits = int(parts[2])
        xmin, xmax = _number(parts[0]), _number(parts[1])
    except ValueError:
        return None
    if not 2 <= nbits <= 32:
        return None
    if xmin >= xmax:
        # No range: the bit count is all there is, and ROOT keeps it in xmin.
        return (float(nbits) + 0.1 if nbits < 15 else 0.0), xmax, 0.0
    return xmin, xmax, float((1 << nbits) if nbits < 32 else 0xFFFFFFFF) / (xmax - xmin)


def _scaled(factor: float, xmin: float) -> Unpack:
    """Values written as a whole number of steps across a known range."""

    def unpack(raw: bytes) -> array.array[Any]:
        count = len(raw) // 4
        return array.array("d", [v / factor + xmin for v in struct.unpack(f">{count}I", raw)])

    return unpack


def _truncated(nbits: int) -> Unpack:
    """Values written as a float with its mantissa cut short."""
    mask = (1 << (nbits + 1)) - 1
    sign = 1 << (nbits + 1)

    def unpack(raw: bytes) -> array.array[Any]:
        out = array.array("d")
        for exp, man in struct.iter_unpack(">BH", raw):
            bits = ((exp << 23) | ((man & mask) << (23 - nbits))) & 0xFFFFFFFF
            value = _FLOAT.unpack(_BITS.pack(bits))[0]
            out.append(-value if man & sign else value)
        return out

    return unpack


def _widened(raw: bytes) -> array.array[Any]:
    """A ``Double32_t`` with no recipe at all, which is a plain ``float``."""
    return array.array("d", to_native(array.array("f", raw)))


def _packing(title: str, double32: bool) -> tuple[Prim, Unpack] | None:
    """How wide a packed float is on disk and how it comes back out.

    ``None`` means the title says something about the packing that this reader
    cannot turn into numbers, which is worth refusing over: the alternative is
    a column of plausible wrong values.
    """
    spec = _range(title)
    if spec is None:
        return None
    xmin, _xmax, factor = spec
    if factor:
        return Prim("float64", "d", 4), _scaled(factor, xmin)
    if int(xmin) == 0 and double32:
        return Prim("float64", "d", 4), _widened
    return Prim("float64", "d", 3), _truncated(int(xmin) or 12)


def _unreadable_range(title: str) -> Refused:
    return Refused(
        f"a packed float whose range is written {title!r}, which is not a "
        f"spelling this reader can turn into numbers"
    )


def _packed(leaf: LeafRecord) -> Column:
    """A ``Double32_t`` or ``Float16_t``, whose title says how it was squeezed."""
    found = _packing(leaf.title, leaf.classname == "TLeafD32")
    if found is None:
        return _unreadable_range(leaf.title)
    prim, unpack = found
    if leaf.count is not None:
        return Rows(prim, 0, False, unpack)
    return Flat(prim, leaf.length, unpack)


def _packed_member(
    branch: BranchRecord, leaf: LeafRecord, source: Source, kind: int, counted: bool
) -> Column:
    """A packed float split out of a class, whose range the streamer holds.

    A branch of its own carries the recipe in the leaf's title; a member does
    not, because ROOT put it in the declaration's trailing comment instead.
    Without that comment there is no honest way to read the bytes, so a class
    the file does not describe is refused rather than read at a guess.
    """
    member = source.streamers().get(branch.classname, {}).get(leaf.name)
    if member is None:
        return Refused(
            f"a packed float member of {branch.classname or 'a class'} that this file's "
            f"streamer information does not describe, so the range it was squeezed into "
            f"is not knowable"
        )
    found = _packing(member.title, PACKED[kind])
    if found is None:
        return _unreadable_range(member.title)
    prim, unpack = found
    if counted:
        return Rows(prim, 1, False, unpack)  # one marker byte, then the packed values
    return Flat(prim, leaf.length, unpack)


# -- a whole object, written into the entry ---------------------------------


class _Unreadable(Exception):
    """Why a class cannot be read member by member, in words."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


#: How one member of a whole object reads: from the buffer, and from the
#: members read before it, which is where a counted one finds its count.
Step = Callable[[Buffer, "dict[str, Any]"], Any]


def _numbers(prim: Prim, unpack: Unpack | None, buf: Buffer, count: int) -> Any:
    """``count`` numbers of one type, packed or plain."""
    raw = buf.take(count * prim.itemsize)
    if unpack is not None:
        return unpack(raw)
    return to_native(array.array(prim.typecode, raw))


def _one(prim: Prim, unpack: Unpack | None) -> Step:
    """A single number, which comes back as a number rather than a run of one."""
    return lambda buf, _row: _numbers(prim, unpack, buf, 1)[0]


def _run(prim: Prim, unpack: Unpack | None, length: int) -> Step:
    """A fixed-size array, ``x[10]``, whose length the declaration fixes."""
    return lambda buf, _row: _numbers(prim, unpack, buf, length)


def _pointer(prim: Prim, unpack: Unpack | None, count: tuple[str, ...]) -> Step:
    """A counted array, ``x[n]``, behind the marker byte that says it is there.

    ``count`` is where in the entry read so far the length is, which is a
    member of the same class or one of a base it inherits - ``TArrayD`` holds
    as many values as the ``fN`` its ``TArray`` base declares.
    """

    def step(buf: Buffer, row: dict[str, Any]) -> Any:
        buf.u8()  # the marker saying the pointer was not null when it was written
        where: Any = row
        for key in count:
            where = where[key]
        return _numbers(prim, unpack, buf, int(where))

    return step


class _Described(dict[str, Any]):
    """The classes this file describes, each made into a reader when first met.

    An object written with its class name in front of it may be of any class at
    all, and a file describes hundreds; building a reader for every one of them
    to read a histogram would cost more than the histogram does. A class this
    file does not describe, or one this reader cannot walk, is left out, and
    then the stream steps over it by the length written in front of it and it
    comes back as the name of its class rather than as a wrong shape.
    """

    def __init__(self, source: Source, seen: tuple[str, ...]) -> None:
        super().__init__()
        self.source, self.seen = source, seen

    def get(self, name: str, default: Any = None) -> Any:
        if name not in self:
            try:
                self[name] = _shown(name, _embedded(name, self.source, self.seen))
            except _Unreadable:
                self[name] = default
        return self[name]


def _shown(name: str, read: Callable[[Buffer], Any]) -> Callable[[Buffer], Any]:
    """The face a class shows when it stands on its own.

    A graph or a histogram met inside another object - in a list a
    ``TMultiGraph`` keeps, say - comes back as the same class it would be as a
    key of the file. Only an object that names its own class is dressed up
    like this: a base written into a derived object stays a plain ``dict``,
    because the members of the derived class count on reaching into it.
    """
    if name in GRAPHS:
        return lambda buf: Graph(name, read(buf))
    if name in HISTOGRAMS:
        return lambda buf: Histogram(name, read(buf))
    return read


def _embedded(name: str, source: Source, seen: tuple[str, ...]) -> Callable[[Buffer], Any]:
    """A whole object written where it stands, rather than pointed at.

    Nearly every class is written out by the file's streamer information, and
    walking the members it declares is the whole of it. A collection is the
    exception: it streams itself, and what it holds is objects that each say
    which class they are.
    """
    if name in LISTS:
        classes = _Described(source, seen)
        return lambda buf: buf.tlist(classes)
    if name in OBJECT_ARRAYS:
        held = _Described(source, seen)
        return lambda buf: buf.objarray(held)
    if name in CLONES:
        pool = _Described(source, seen)
        return lambda buf: buf.clones(pool, lambda held: _fields(held, source, seen))
    if name in ARRAYS:
        prim = ARRAYS[name]
        return lambda buf: _numbers(prim, None, buf, buf.i32())
    return _streamed(_members(name, source, seen))


def _fields(name: str, source: Source, seen: tuple[str, ...]) -> list[tuple[str, Step]] | None:
    """The members of one class in order, or ``None`` for a class that has no
    such list - because it streams itself, or because the file does not
    describe it - which is the sign that field-by-field data cannot be read.
    """
    if name in SELF_STREAMING:
        return None
    try:
        return _steps(name, source, seen)
    except _Unreadable:
        return None


def _class_held(typename: str) -> tuple[str, bool] | None:
    """The class a container holds one of, and whether it holds it by pointer.

    ``None`` for anything else, including a container of a container: what a
    ``vector<vector<TObject>>`` looks like on the wire is not something any
    file this reader has met has ever written.
    """
    head, angle, rest = typename.replace("std::", "").strip().partition("<")
    if not angle or not rest.endswith(">") or head not in SEQUENCES:
        return None
    inside = rest[:-1].strip()
    if "<" in inside or not inside:
        return None
    return inside.rstrip("*").strip(), inside.endswith("*")


def _field_by_field(
    buf: Buffer, end: int | None, fields: list[tuple[str, Step]]
) -> list[dict[str, Any]]:
    """A container written a member at a time rather than an object at a time.

    Every object's first member comes first, then every object's second, and
    so on, with the version of the class written once at the front instead of
    in front of each object. Reading it back is the same steps in the same
    order, only turned inside out.

    The record says how long it is, so a shape read wrongly lands somewhere
    other than the end of it; that is checked here rather than trusted, since
    a plausible misreading of somebody's data is worse than a refusal.
    """
    version = buf.i16()  # of the class held, once for the lot of them
    if version <= 0:
        buf.u32()  # ... or a checksum of it, for a class that carries no version
    rows: list[dict[str, Any]] = [{} for _ in range(buf.i32())]
    for label, step in fields:
        for row in rows:
            row[label] = step(buf, row)
    if end is not None and buf.pos != end:
        raise FormatError(
            f"a container of {len(rows)} written field by field ended "
            f"{abs(end - buf.pos)} bytes from where it said it would, so what was "
            f"read out of it cannot be trusted"
        )
    return rows


def _objects(
    one: Callable[[Buffer], Any], fields: list[tuple[str, Step]] | None = None
) -> Callable[[Buffer], list[Any]]:
    """A container of whole objects, each written the way its own class is."""

    def read(buf: Buffer) -> list[Any]:
        version, end = buf.header()
        if not version & MEMBER_WISE:
            return [one(buf) for _ in range(buf.u32())]
        if fields is None:
            raise UnsupportedFeatureError(
                "this container was written field by field, which happens for a "
                "container of pointers and is not a shape this reader decodes"
            )
        return list(_field_by_field(buf, end, fields))

    return read


def _plainly(read: Callable[[Buffer], Any]) -> Step:
    """A member that reads the same whatever came before it."""
    return lambda buf, _row: read(buf)


def _streamed(read: Callable[[Buffer], Any]) -> Callable[[Buffer], Any]:
    """A class that streamed itself, and so carries a record of its own."""

    def value(buf: Buffer) -> Any:
        version, end = buf.header()
        if version == 0:
            buf.u32()  # a class with no version of its own says so with a checksum
        row = read(buf)
        buf.resume(end)
        return row

    return value


def _named(name: str, read: Callable[[Buffer], Any]) -> Callable[[Buffer], Any]:
    """An entry that says which class it holds before holding it.

    The name is written the way C would have written it: how long it is, and
    then the characters and the NUL that ended them. A branch that says one
    class and holds another is a branch of several classes, which is a thing
    ROOT allows and this reader stops at rather than reading as the wrong one.
    """

    def value(buf: Buffer) -> Any:
        buf.u8()  # how long the name is, which its NUL says as well
        written = buf.cstring()
        if written != name:
            raise FormatError(
                f"an entry of a branch of {name} says it holds {written!r} instead, "
                f"and a branch of more than one class is not one this reader follows"
            )
        return read(buf)

    return value


def _datime(buf: Buffer) -> datetime.datetime:
    """A ``TDatime``, which streams itself as one packed word and no record.

    It is the one class in ROOT's own kit small enough to have skipped the
    version header, so nothing about the layout can be worked out from the
    file: this is what it has always been.
    """
    return as_datetime(buf.u32())


def _bookkeeping(buf: Buffer) -> dict[str, Any]:
    """A ``TObject`` base: the identifier and bits every ROOT object carries."""
    unique, bits = buf.tobject()
    return {"fUniqueID": unique, "fBits": bits}


def _titled(buf: Buffer) -> dict[str, Any]:
    """A ``TNamed`` base: the name and title, which every ROOT object can have."""
    name, title = buf.named()
    return {"fName": name, "fTitle": title}


def _reader(node: Any) -> Callable[[Buffer], Any]:
    """How a string or a container written inside an entry reads back."""
    if isinstance(node, Str):
        return _member_string if node.record else _string
    reason = _unusable(node)
    if reason:
        raise _Unreadable(reason)
    if isinstance(node, Mapping):
        return _mapping(node)
    assert isinstance(node, Seq)
    return _sequence(node.item)


def _step(
    member: Member, source: Source, seen: tuple[str, ...], before: dict[str, tuple[str, ...]]
) -> Step:
    """How to read one member, or a refusal to read the class it belongs to."""
    for base in (0, OFFSET_L, OFFSET_P):
        kind = member.stype - base
        if kind not in BASIC and kind not in PACKED:
            continue
        unpack = None
        prim = BASIC.get(kind)
        if prim is None:
            found = _packing(member.title, PACKED[kind])
            if found is None:
                raise _Unreadable(
                    f"{member.name!r}, a packed float whose range is written "
                    f"{member.title!r}, which is not a spelling this reader can "
                    f"turn into numbers"
                )
            prim, unpack = found
        if base == OFFSET_P:
            where = before.get(member.count)
            if where is None:
                raise _Unreadable(
                    f"{member.name!r}, which says it holds as many values as "
                    f"{member.count or 'a member with no name'} but is written "
                    f"before it"
                )
            return _pointer(prim, unpack, where)
        if base == OFFSET_L:
            return _run(prim, unpack, member.length)
        return _one(prim, unpack)
    if member.typename == "TDatime":
        return _plainly(_datime)  # a class of its own that writes no record
    if member.stype == TOBJECT:
        return _plainly(_bookkeeping)
    if member.stype == TNAMED:
        return _plainly(_titled)
    if member.stype - OFFSET_L in (61, 62):
        # A fixed-size array of a class, written one object after another.
        one = _embedded(member.typename, source, seen)
        return _plainly(lambda buf: [one(buf) for _ in range(member.length)])
    if member.stype in OBJECTS:
        # A base class is declared under its own name, with ``BASE`` for a type.
        inside = member.name if member.stype == 0 else member.typename
        return _plainly(_embedded(inside, source, seen))
    if member.stype in OBJECTS_HELD:
        # A pointer the class promises is never null, so ROOT writes what it
        # points at where it stands, with no class name in front of it.
        return _plainly(_embedded(member.typename.rstrip("*"), source, seen))
    if member.stype in OBJECTS_POINTED:
        classes = _Described(source, seen)
        return _plainly(lambda buf: buf.any(classes))
    node = parse(member.typename)
    if node is not None:
        return _plainly(_reader(node))
    held = _class_held(member.typename)
    if held is not None:
        name, pointed = held
        if pointed:
            classes = _Described(source, seen)
            return _plainly(_objects(lambda buf: buf.any(classes)))
        # A class the file describes can also be written field by field; one
        # that streams itself, such as a TArrayD, only ever comes whole.
        return _plainly(_objects(_embedded(name, source, seen), _fields(name, source, seen)))
    raise _Unreadable(
        f"{member.name!r}, which is {KINDS.get(member.stype, 'of a kind')} that "
        f"this reader does not decode inside an entry"
    )


def _steps(name: str, source: Source, seen: tuple[str, ...] = ()) -> list[tuple[str, Step]]:
    """Every member of one class, in the order the class declares them."""
    if name in seen:
        raise _Unreadable(f"{name}, which is written inside itself")
    described = source.streamers().get(name)
    if described is None:
        raise _Unreadable(
            f"a member of type {name or 'with no name'}, which this file's streamer "
            f"information does not describe"
        )
    steps: list[tuple[str, Step]] = []
    before: dict[str, tuple[str, ...]] = {}
    for member in described.values():
        steps.append((member.name, _step(member, source, (*seen, name), before)))
        if member.stype == 0:  # a base, whose own members are inside its own name
            for inner in source.streamers().get(member.name) or ():
                before[inner] = (member.name, inner)
        before[member.name] = (member.name,)
    return steps


def _members(
    name: str, source: Source, seen: tuple[str, ...] = ()
) -> Callable[[Buffer], dict[str, Any]]:
    """How every member of one class reads, in the order the class declares."""
    steps = _steps(name, source, seen)

    def read(buf: Buffer) -> dict[str, Any]:
        row: dict[str, Any] = {}
        for label, step in steps:
            row[label] = step(buf, row)
        return row

    return read


def _whole(name: str, source: Source, streamed: bool = False, named: bool = False) -> Column:
    """A whole C++ object written into the entry, rather than split into branches.

    Nothing about it is in the file except its bytes and the layout the file's
    streamer information gives for the class, so this walks that layout member
    by member and gives back a :class:`dict` per entry - the same shape a split
    object comes back as, from a file that was written the other way.

    ``streamed`` is for the class that wrote itself rather than being written
    out by the file's streamer information, which puts a record in front of the
    members; ``named`` is for the older branch that writes the class name in
    front of every entry as well.
    """
    if name == "TDatime":  # its word is the whole entry, with no record round it
        return Values("datetime", _named(name, _datime) if named else _datime)
    if source.streamers().get(name) is None:
        return Refused(
            f"{name or 'an unnamed type'}, which is a C++ type this reader does not "
            f"decode: this file's streamer information does not describe its layout, "
            f"and a file written split would have its members as branches of their own"
        )
    try:
        read: Callable[[Buffer], Any] = _members(name, source)
    except _Unreadable as why:
        return Refused(f"{name or 'an unnamed type'}, which holds {why.reason}")
    if streamed:
        read = _streamed(read)
    if named:
        read = _named(name, read)
    return Values("dict", read)


def whole_object(name: str, source: Source) -> Values | Refused:
    """One object standing on its own, the way a key in a file holds one.

    A key's bytes are the object and nothing else: the record its class puts
    in front of itself, and then its members in the order the file's streamer
    information declares them - the same walk an unsplit branch entry takes.
    """
    if isinstance(parse(name), Str):
        return Values("str", _string)  # a string key is its length and its bytes
    if name == "TDatime":
        return Values("datetime", _datime)
    if name in ARRAYS:
        return Values(ARRAYS[name].typename, _embedded(name, source, ()))
    if name in LISTS + OBJECT_ARRAYS + CLONES:
        # A collection streams itself, so the file describing it would not help.
        return Values("list", _embedded(name, source, ()))
    if source.streamers().get(name) is None:
        return Refused("this file's streamer information does not describe its layout")
    try:
        return Values("dict", _streamed(_members(name, source)))
    except _Unreadable as why:
        return Refused(f"it holds {why.reason}")


def _plain(leaf: LeafRecord) -> Column:
    """A ``TLeafI`` and its kind: the type is the class of the leaf itself."""
    if leaf.classname == "TLeafC":
        return Values("str", _string)
    prim = Prim(str(leaf.typename), leaf.typecode, leaf.itemsize)
    if leaf.count is not None:
        return Rows(prim, 0, False)
    return Flat(prim, leaf.length)


def _declared(branch: BranchRecord, leaf: LeafRecord, source: Source) -> Column:
    """A column whose type is a class name the file has to spell out."""
    declared = _declared_name(branch, leaf, source)
    if isinstance(declared, Refused):
        return declared
    name, header = declared
    return _declared_node(parse(name), name, header, branch, source)


def _declared_name(
    branch: BranchRecord, leaf: LeafRecord, source: Source
) -> tuple[str, bool] | Refused:
    """Resolve the class name and whether a member record precedes it."""
    if leaf.ltype < 0 or branch.whole:
        return branch.classname, False  # a whole object: the branch names it
    member = source.streamers().get(branch.classname, {}).get(leaf.name)
    if member is None:
        return Refused(
            f"a member of {branch.classname or 'a class'} that this file's streamer "
            f"information does not describe, so its type is not knowable"
        )
    return member.typename, True


def _declared_node(
    node: object | None, name: str, header: bool, branch: BranchRecord, source: Source
) -> Column:
    """Turn a parsed declared C++ type into its column interpreter."""
    if node is None:
        if not header:
            return _whole(name, source, branch.streamed)  # the whole object
        return Refused(
            f"{name or 'an unnamed type'}, which is a C++ type this reader does not "
            f"decode; a split file has its members as branches of their own"
        )
    if isinstance(node, Str):
        return Values("str", _member_string if header and node.record else _string)
    if isinstance(node, Seq) and isinstance(node.item, Prim):
        return Rows(node.item, RECORD + 4, True)
    reason = _unusable(node)
    if reason:
        return Refused(reason)
    if isinstance(node, Mapping):
        return Values(py_name(node), _mapping(node))
    assert isinstance(node, Seq)
    return Values(py_name(node), _sequence(node.item))


def build(branch: BranchRecord, leaf: LeafRecord, source: Source) -> Column:
    """What this column is, or why it cannot be read.

    Everything else in this module is reached through here: a branch record
    and one of its leaves go in, and a :class:`Column` comes out - a readable
    one, or a :class:`Refused` carrying the sentence that says why not.
    """
    from .objects import LEAF_TYPES, PACKED_LEAVES

    if leaf.classname == "TLeafElement":
        return _element(branch, leaf, source)
    if leaf.classname in LEAF_TYPES:
        return _plain(leaf)
    if leaf.classname in PACKED_LEAVES:
        return _packed(leaf)
    if leaf.classname == "TLeafObject" and branch.classname:
        return _whole(branch.classname, source, streamed=True, named=True)
    return Refused(leaf.reason)


def _element(branch: BranchRecord, leaf: LeafRecord, source: Source) -> Column:
    """Interpret a ``TLeafElement`` from its ROOT type code."""
    if leaf.ltype == 65:
        return Values("str", _string)  # a TString member, written with no header
    primitive = _primitive_element(branch, leaf, source)
    if primitive is not None:
        return primitive
    if leaf.ltype not in KINDS:
        return _declared(branch, leaf, source)
    if branch.whole:
        # ROOT 4 left the code at zero on a branch holding a whole
        # collection, where a later ROOT writes -1; that the branch points
        # at no member of its class is what says the class is the column.
        return _declared(branch, leaf, source)
    return Refused(f"{KINDS[leaf.ltype]}, which this reader does not decode")


def _primitive_element(
    branch: BranchRecord, leaf: LeafRecord, source: Source
) -> Column | None:
    """A primitive, packed primitive, or pointer element when its code says so."""
    for base in (0, OFFSET_L, OFFSET_P):
        kind = leaf.ltype - base
        prim = BASIC.get(kind)
        if prim is None:
            if kind in PACKED:
                return _packed_member(branch, leaf, source, kind, base == OFFSET_P)
            continue
        if base == OFFSET_P:
            return Rows(prim, 1, False)  # one marker byte, then the counted values
        return Flat(prim, leaf.length)
    return None
