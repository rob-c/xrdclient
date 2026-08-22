"""Writing a ROOT file: keys, objects, and the description that reads them back.

A file written here is the real thing - a header, a directory, one key per
object, a key list, a free list, and the streamer information saying what the
classes look like - laid out the way ROOT 6.08 laid out the donor files the
layouts in :mod:`.winfo` were harvested from. Anything that reads ROOT files
by their own self-description, this library included, walks the result by the
same map it was written from.

What can be written is what can be written *correctly*: histograms, graphs,
strings and arrays of numbers. Anything else is refused by name rather than
guessed at, because a plausible-looking file that ROOT misreads is worse than
an error message. The same goes for the parts of an object this writer cannot
carry - a histogram with fits attached refuses rather than silently dropping
them.

The file is built in memory and written out in one piece when it is closed,
so a ``with`` block that raises leaves nothing behind rather than half a file.
"""

from __future__ import annotations

import array
import datetime
import struct
import uuid
from collections.abc import Mapping
from typing import IO, TYPE_CHECKING, Any

from ..url import parse
from .buffer import BYTE_COUNT_MASK, IS_REFERENCED, NEW_CLASS_TAG
from .compression import CODES, LEVELS, compress
from .errors import UnsupportedFeatureError
from .graph import GRAPHS, Graph
from .hist import HISTOGRAMS, Histogram
from .interp import ARRAYS, OFFSET_L, OFFSET_P
from .winfo import INFOS, SUBVERSIONS, WRITER_VERSION, Element

if TYPE_CHECKING:  # pragma: no cover - for the type checker, not for running
    from .wtree import WritableTree

__all__ = ["create", "WritableFile"]

#: The bits a freshly made object carries: on the heap, and not deleted.
BITS = 0x03000000
#: Where the first record starts; the hundred bytes before it are the header.
BEGIN = 100
#: Where ROOT says big-file territory starts, which is where the free list ends.
BIG = 2_000_000_000
#: The key version this writer emits: small offsets, which :data:`BIG` protects.
KEY_VERSION = 4
#: How many bytes of one tree column gather before that column writes a
#: basket. ROOT's own default, and a reasonable trade: bigger baskets read
#: faster in bulk, smaller ones let a reader take a few entries cheaply.
BASKET_BYTES = 32_000

#: The struct code for each basic streamer type this writer packs.
FORMS = {
    1: "b",
    2: "h",
    3: "i",
    4: "q",
    5: "f",
    6: "i",
    8: "d",
    11: "B",
    12: "H",
    13: "I",
    14: "Q",
    15: "I",
    16: "q",
    17: "Q",
    18: "B",
}

#: The ``TArray`` class for each :mod:`array` typecode ROOT has a class for.
ARRAY_CLASSES = {
    "b": "TArrayC",
    "h": "TArrayS",
    "i": "TArrayI",
    "q": "TArrayL64",
    "f": "TArrayF",
    "d": "TArrayD",
}

#: The lists this writer will write - empty, because what a list holds could
#: be anything at all, and only nothing is nothing in every layout.
LISTS = ("TList", "THashList")


def packed_now() -> int:
    """The moment, in ROOT's packed date word - the inverse of ``as_datetime``."""
    now = datetime.datetime.now()
    return (
        ((now.year - 1995) << 26)
        | (now.month << 22)
        | (now.day << 17)
        | (now.hour << 12)
        | (now.minute << 6)
        | now.second
    )


class WBuffer:
    """Bytes being written, with the record bookkeeping ROOT wraps them in.

    A record is a byte count that can only be known once the record is over,
    so :meth:`start` leaves a hole and :meth:`end` comes back to fill it -
    and :meth:`tag` does the same for an object that names its class first.
    """

    __slots__ = ("data",)

    def __init__(self) -> None:
        self.data = bytearray()

    def raw(self, chunk: bytes) -> None:
        self.data += chunk

    def u8(self, value: int) -> None:
        self.data += struct.pack(">B", value)

    def u16(self, value: int) -> None:
        self.data += struct.pack(">H", value)

    def i16(self, value: int) -> None:
        self.data += struct.pack(">h", value)

    def u32(self, value: int) -> None:
        self.data += struct.pack(">I", value)

    def i32(self, value: int) -> None:
        self.data += struct.pack(">i", value)

    def i64(self, value: int) -> None:
        self.data += struct.pack(">q", value)

    def string(self, text: str) -> None:
        """A ``TString``: one length byte, or ``255`` and then four."""
        raw = text.encode("utf-8", "surrogateescape")
        if len(raw) >= 255:
            self.u8(255)
            self.i32(len(raw))
        else:
            self.u8(len(raw))
        self.data += raw

    def cstring(self, text: str) -> None:
        """A NUL-terminated name, which is how class names are written."""
        self.data += text.encode("utf-8", "surrogateescape") + b"\x00"

    def start(self, version: int) -> int:
        """Open a record of ``version``; :meth:`end` closes what this returns."""
        index = len(self.data)
        self.data += b"\x00\x00\x00\x00"
        self.u16(version)
        return index

    def tag(self, classname: str) -> int:
        """Open a counted object that names its class, the way ``any`` reads one.

        The class is spelled out every time rather than referred back to,
        which is longer and legal, and means no bookkeeping can be wrong.
        """
        index = len(self.data)
        self.data += b"\x00\x00\x00\x00"
        self.u32(NEW_CLASS_TAG)
        self.cstring(classname)
        return index

    def end(self, index: int) -> None:
        """Close a record or a tag by filling in how long it turned out to be."""
        count = (len(self.data) - index - 4) | BYTE_COUNT_MASK
        struct.pack_into(">I", self.data, index, count)

    def tobject(self, bits: int = BITS, unique: int = 0) -> None:
        """A ``TObject`` base: version, identifier and bits, no record round it.

        The referenced bit is cleared whatever came in: it promises two more
        bytes this writer does not write, and an object in a new file is not
        on anyone's reference list anyway.
        """
        self.u16(1)
        self.u32(unique)
        self.u32(bits & ~IS_REFERENCED)

    def named(self, name: str, title: str) -> None:
        """A ``TNamed`` base: a record holding the object bits and two strings."""
        index = self.start(1)
        self.tobject()
        self.string(name)
        self.string(title)
        self.end(index)


def _find(row: dict[str, Any], name: str) -> Any:
    """A counter's value, wherever the class or one of its bases keeps it."""
    if name in row:
        return row[name]
    for value in row.values():
        if isinstance(value, dict):
            try:
                return _find(value, name)
            except KeyError:
                continue
    raise KeyError(name)


def _numbers(buf: WBuffer, typecode: str, values: Any) -> None:
    """A run of values of one type, big-endian, as every ROOT number is."""
    values = list(values)
    buf.raw(struct.pack(f">{len(values)}{typecode}", *values))


def _array(buf: WBuffer, classname: str, values: Any) -> None:
    """A ``TArray``: its length and its values, with no record round them."""
    values = list(values if values is not None else ())
    buf.i32(len(values))
    _numbers(buf, ARRAYS[classname].typecode, values)


def _list(buf: WBuffer, classname: str, entries: Any) -> None:
    """An empty ``TList``, which is the only list this writer will write."""
    if entries:
        raise UnsupportedFeatureError(
            f"a {classname} holding {len(entries)} entries is not written: what a "
            f"list carries could be anything at all, and writing it wrongly would "
            f"be worse than refusing; empty it first"
        )
    index = buf.start(5)
    buf.tobject()
    buf.string("")
    buf.i32(0)
    buf.end(index)


def _record(buf: WBuffer, classname: str, row: Any, used: dict[str, None]) -> None:
    """One object written in place: its record, then its members in order.

    Every class actually written lands in ``used``, so an object hung behind
    a pointer - a graph's fitted histogram, say - gets described by the
    file's streamer information exactly as a key of the file would be.
    """
    if classname in LISTS:
        _list(buf, classname, row)
        return
    info = INFOS.get(classname)
    if info is None:
        raise UnsupportedFeatureError(
            f"a {classname} inside what is being written is not a class this "
            f"writer carries a layout for"
        )
    if not isinstance(row, dict):
        raise UnsupportedFeatureError(
            f"a {classname} here is a {type(row).__name__} rather than the dict "
            f"of members writing one starts from"
        )
    used[classname] = None
    index = buf.start(info[1])
    for element in info[2]:
        _element(buf, element, row, used)
    buf.end(index)


def _pointed(buf: WBuffer, typename: str, value: Any, used: dict[str, None]) -> None:
    """A pointer that may be null: nothing, or an object naming its class."""
    if value is None or (isinstance(value, (list, tuple)) and not value):
        buf.u32(0)
        return
    classname = typename.rstrip("*")
    if isinstance(value, (Histogram, Graph)):
        classname, value = value.classname, value.members
    elif isinstance(value, (list, tuple)):
        _list(buf, classname, value)  # non-empty here, so this refuses by name
    index = buf.tag(classname)
    _record(buf, classname, value, used)
    buf.end(index)


def _element(buf: WBuffer, element: Element, row: dict[str, Any], used: dict[str, None]) -> None:
    """One member of one class, laid out exactly as its element describes."""
    _kind, name, _title, stype, _size, alen, _adim, _maxidx, typename, extras = element
    value = row.get(name)
    if _base_element(buf, name, stype, value, used):
        return
    if _object_element(buf, stype, typename, value, used):
        return
    if _number_element(buf, name, stype, alen, extras, value, row):
        return
    raise UnsupportedFeatureError(
        f"{name} is of streamer type {stype}, which this writer does not lay out"
    )


def _base_element(buf: WBuffer, name: str, stype: int, value: Any, used: dict[str, None]) -> bool:
    if stype == 66:  # the TObject base, which streams itself
        _write_tobject(buf, value)
    elif stype == 67:  # the TNamed base, likewise
        _write_tnamed(buf, value)
    elif stype == 0:  # any other base, written under its own name
        _write_base(buf, name, value, used)
    elif stype == 65:  # a TString member: its bytes, no record
        buf.string(str(value if value is not None else ""))
    else:
        return False
    return True


def _write_tobject(buf: WBuffer, value: Any) -> None:
    bits = value if isinstance(value, dict) else {}
    buf.tobject(int(bits.get("fBits", BITS)), int(bits.get("fUniqueID", 0)))


def _write_tnamed(buf: WBuffer, value: Any) -> None:
    named = value if isinstance(value, dict) else {}
    buf.named(str(named.get("fName", "")), str(named.get("fTitle", "")))


def _write_base(buf: WBuffer, name: str, value: Any, used: dict[str, None]) -> None:
    if name in ARRAYS:
        _array(buf, name, value)
    else:
        _record(buf, name, value if value is not None else {}, used)


def _object_element(
    buf: WBuffer, stype: int, typename: str, value: Any, used: dict[str, None]
) -> bool:
    if stype in (61, 62):  # an object held by value
        if typename in ARRAYS:
            _array(buf, typename, value)
        else:
            _record(buf, typename, value if value is not None else {}, used)
    elif stype in (63, 68):  # a pointer promised never null, written in place
        _record(buf, typename.rstrip("*"), value if value is not None else [], used)
    elif stype in (64, 69):  # a pointer that may be null
        _pointed(buf, typename, value, used)
    else:
        return False
    return True


def _number_element(
    buf: WBuffer,
    name: str,
    stype: int,
    alen: int,
    extras: tuple[Any, ...],
    value: Any,
    row: dict[str, Any],
) -> bool:
    if stype in FORMS:  # one number
        buf.raw(struct.pack(">" + FORMS[stype], value))
    elif stype - OFFSET_L in FORMS:  # a fixed-size array, x[10]
        values = list(value if value is not None else ())
        if len(values) != alen:
            raise ValueError(f"{name} holds {len(values)} values where {alen} were declared")
        _numbers(buf, FORMS[stype - OFFSET_L], values)
    elif stype - OFFSET_P in FORMS:  # a counted array, x[n], behind its marker
        counter = str(extras[1])
        try:
            count = int(_find(row, counter))
        except KeyError:
            raise ValueError(f"{name} is counted by {counter}, which is not here") from None
        values = list(value if value is not None else ())
        if len(values) < count:
            raise ValueError(f"{name} holds {len(values)} values where {counter} says {count}")
        buf.u8(1)
        _numbers(buf, FORMS[stype - OFFSET_P], values[:count])
    else:
        return False
    return True


def _closure(seeds: dict[str, None]) -> list[str]:
    """Every class the seeds reach through bases and members, in met order."""
    ordered = dict.fromkeys(seeds)
    queue = list(ordered)
    while queue:
        info = INFOS.get(queue.pop(0))
        if info is None:
            continue
        for kind, name, _t, stype, _s, _a, _d, _m, typename, _x in info[2]:
            if kind == "TStreamerBase":
                held = name
            elif stype in (61, 62, 63, 64, 68, 69):
                held = typename.rstrip("*")
            elif stype == 65:
                held = "TString"
            else:
                continue
            if held in INFOS and held not in ordered:
                ordered[held] = None
                queue.append(held)
    return list(ordered)


def _streamers(used: dict[str, None]) -> bytes:
    """The ``StreamerInfo`` record: what this file says its classes look like.

    Emitted from :data:`~.winfo.INFOS` verbatim, checksums and all, so the
    file describes its classes exactly as the ROOT that the descriptions were
    harvested from would have.
    """
    buf = WBuffer()
    index = buf.start(5)
    buf.tobject()
    buf.string("")
    names = _closure(used)
    buf.i32(len(names))
    for name in names:
        checksum, version, elements = INFOS[name]
        tag = buf.tag("TStreamerInfo")
        info = buf.start(9)
        buf.named(name, "")
        buf.u32(checksum)
        buf.i32(version)
        held = buf.tag("TObjArray")
        arr = buf.start(3)
        buf.tobject()
        buf.string("")
        buf.i32(len(elements))
        buf.i32(0)
        for element in elements:
            _info_element(buf, element)
        buf.end(arr)
        buf.end(held)
        buf.end(info)
        buf.end(tag)
        buf.u8(0)  # the option string every list entry carries, empty
    buf.end(index)
    return bytes(buf.data)


def _info_element(buf: WBuffer, element: Element) -> None:
    """One ``TStreamerElement``, written the way the donor files write them."""
    kind, name, title, stype, size, alen, adim, maxidx, typename, extras = element
    tag = buf.tag(kind)
    sub = buf.start(SUBVERSIONS[kind])
    base = buf.start(4)  # the TStreamerElement the subclass builds on
    buf.named(name, title)
    buf.i32(stype)
    buf.i32(size)
    buf.i32(alen)
    buf.i32(adim)
    for index in maxidx:
        buf.i32(index)
    buf.string(typename)
    buf.end(base)
    if kind == "TStreamerBase":
        buf.i32(int(str(extras[0])))
    elif kind == "TStreamerBasicPointer":
        buf.i32(int(str(extras[0])))
        buf.string(str(extras[1]))
        buf.string(str(extras[2]))
    buf.end(sub)
    buf.end(tag)


def _payload(obj: Any) -> tuple[str, bytes, tuple[str, ...]]:
    """What one object writes as: its class, its bytes, the layouts it needs."""
    if isinstance(obj, str):
        return _string_payload(obj)
    if isinstance(obj, array.array):
        return _array_payload(obj)
    if isinstance(obj, (Histogram, Graph)):
        return _object_payload(obj)
    raise UnsupportedFeatureError(
        f"a {type(obj).__name__} is not something this writer puts in a ROOT "
        f"file: it takes a Histogram, a Graph, a str, or an array.array"
    )


def _string_payload(value: str) -> tuple[str, bytes, tuple[str, ...]]:
    buf = WBuffer()
    buf.string(value)
    return "string", bytes(buf.data), ()


def _array_payload(value: array.array[Any]) -> tuple[str, bytes, tuple[str, ...]]:
    code = value.typecode
    if code == "l":  # its width is the platform's, so pick the class by it
        code = "q" if value.itemsize == 8 else "i"
    classname = ARRAY_CLASSES.get(code)
    if classname is None:
        raise UnsupportedFeatureError(
            f"an array of typecode {value.typecode!r} has no ROOT class: the "
            f"TArrays are signed integers b, h, i, l and q, and floats f and d"
        )
    buf = WBuffer()
    buf.i32(len(value))
    _numbers(buf, ARRAYS[classname].typecode, value)
    return classname, bytes(buf.data), ()


def _object_payload(value: Histogram | Graph) -> tuple[str, bytes, tuple[str, ...]]:
    classname = value.classname
    if classname not in INFOS:
        writable = ", ".join(name for name in INFOS if name in HISTOGRAMS or name in GRAPHS)
        raise UnsupportedFeatureError(
            f"a {classname} is not a class this writer carries a layout for; "
            f"the ones it does are {writable}"
        )
    buf = WBuffer()
    used: dict[str, None] = {}
    _record(buf, classname, value.members, used)
    return classname, bytes(buf.data), tuple(used)


def _keylen(classname: str, name: str, title: str, extra: int = 0) -> int:
    """How long the key in front of a record is: 26 fixed bytes, then three
    strings - and whatever else the class writes into its own key, which only
    a ``TBasket`` does."""
    return (
        26
        + extra
        + sum(1 + len(text.encode("utf-8", "surrogateescape")) for text in (classname, name, title))
    )


def _checked(text: str, what: str) -> str:
    """A name or title a key can carry: short enough for its one length byte."""
    if not isinstance(text, str):
        raise ValueError(f"the {what} must be a str, not {type(text).__name__}")
    if len(text.encode("utf-8", "surrogateescape")) > 254:
        raise ValueError(f"the {what} {text[:40]!r}... is too long for a key to carry")
    return text


class WritableFile:
    """A ROOT file being written: a mapping from name to object, then a close.

        >>> with xrd.root.create("out.root") as f:            # doctest: +SKIP
        ...     f["hist"] = xrd.root.Histogram.new("hist", edges, counts)

    Everything is kept in memory and written out in one piece at a clean
    close; a ``with`` block that raises writes nothing at all, on the
    principle that no file is better than half a file.
    """

    def __init__(
        self,
        handle: IO[bytes],
        name: str,
        owned: bool,
        algorithm: str | None,
        level: int | None,
    ) -> None:
        self._handle = handle
        self._owned = owned
        self._algorithm = algorithm
        self._level = LEVELS[algorithm] if algorithm is not None and level is None else level
        self._datime = packed_now()
        self._uuid = uuid.uuid4().bytes
        self._closed = False
        #: What the file calls itself: the last segment of where it is going.
        self.name = name
        self._label = _checked(name.rstrip("/").rpartition("/")[2] or name, "file name")
        # The key in front of the file's own record: 26 fixed bytes, then
        # "TFile", the name, and an empty title; the name and title again.
        keylen = 34 + len(self._label.encode())
        self._nname = keylen + 2 + len(self._label.encode())
        #: The header, then the room the file's own record takes; both are
        #: filled in at close, when everything they point at has a place.
        self._data = bytearray(BEGIN + self._nname + 60)
        self._keys: list[bytes] = []
        self._cycles: dict[str, int] = {}
        self._used: dict[str, None] = {}
        self._trees: list[WritableTree] = []

    def __repr__(self) -> str:
        state = "closed" if self._closed else f"{len(self._keys)} keys so far"
        return f"<WritableFile {self.name!r}, {state}>"

    def write(self, name: str, obj: Any, *, title: str | None = None) -> None:
        """Write one object under ``name``, as :meth:`__setitem__` does.

        The title is taken from the object when it has one; a name written
        twice becomes a second cycle of itself, exactly as in ROOT, and
        reading the file back gives the newest.
        """
        self._check_name(name)
        classname, payload, used = _payload(obj)
        if title is None:
            title = obj.title if isinstance(obj, (Histogram, Graph)) else ""
        _checked(title, "title")
        self._used.update(dict.fromkeys(used))
        cycle = self._cycles.get(name, 0) + 1
        self._cycles[name] = cycle
        self._put(classname, name, title, payload, cycle, listed=True)

    def __setitem__(self, name: str, obj: Any) -> None:
        self.write(name, obj)

    def tree(
        self,
        name: str,
        columns: Mapping[str, Any],
        *,
        title: str | None = None,
        basket_size: int = BASKET_BYTES,
    ) -> WritableTree:
        """A tree in this file, to be filled entry by entry.

            >>> with xrd.root.create("out.root") as f:        # doctest: +SKIP
            ...     tree = f.tree("events", {"energy": float, "hits": ("i", 4)})
            ...     tree.fill(energy=12.5, hits=[3, 1, 4, 1])

        ``columns`` maps each column's name to what it holds: a Python
        ``bool``, ``int`` or ``float``, an :mod:`array` type code such as
        ``'f'`` for a narrower number, or a pair of either and how many
        values every entry holds. Entries go out a basket at a time as they
        gather - ``basket_size`` bytes of a column at a time - so the tree
        can be far larger than memory, and the tree's own record is written
        when the file closes.
        """
        from .wtree import WritableTree

        self._check_name(name)
        _checked(title or "", "title")
        cycle = self._cycles.get(name, 0) + 1
        self._cycles[name] = cycle
        tree = WritableTree(self, name, title or "", columns, basket_size, cycle)
        self._trees.append(tree)
        self._used.update(dict.fromkeys(tree.classes))
        return tree

    def _check_name(self, name: str) -> None:
        """Whether a key could be written under this name, and read back by it."""
        if self._closed:
            raise ValueError("this file is closed; whatever it was going to hold is written")
        _checked(name, "name")
        if not name:
            raise ValueError("a key with no name could never be asked for; give it one")
        if "/" in name:
            raise ValueError(
                f"{name!r} has a / in it, and this writer does not make subdirectories: "
                f"write to the top level, or spell the structure in the name another way"
            )
        if ";" in name:
            raise ValueError(
                f"{name!r} has a ; in it, which is how a reader asks for an old cycle; "
                f"a name containing one could never be read back"
            )

    @property
    def _codes(self) -> int:
        """How ROOT spells this file's compression: the algorithm and the level."""
        if self._algorithm is None:
            return 0
        return CODES[self._algorithm] * 100 + (self._level or 0)

    def _put(
        self,
        classname: str,
        name: str,
        title: str,
        payload: bytes,
        cycle: int,
        listed: bool,
        packed: bool = True,
        extra: bytes = b"",
    ) -> tuple[int, int]:
        """One record: its key, then its payload, compressed when that is smaller.

        Where it went and how long it turned out come back, which is what a
        tree writes down about each of its baskets. ``extra`` is whatever the
        class keeps in its own key rather than its payload - only a
        ``TBasket`` does, and what it keeps is how to slice itself.

        The key list and the free list go in raw whatever the file's setting,
        because ROOT - and the reader here - parses both without looking at
        the lengths that would say they were compressed.
        """
        seek = len(self._data)
        if seek > BIG:  # pragma: no cover - a file no test should ever make
            raise UnsupportedFeatureError(
                "this file has grown past 2 GB, which needs the wide layout "
                "this writer does not produce; split the output across files"
            )
        body = payload
        if packed and self._algorithm is not None:
            squeezed = compress(payload, self._algorithm, self._level)
            if len(squeezed) < len(payload):
                body = squeezed
        key = WBuffer()
        keylen = _keylen(classname, name, title, len(extra))
        key.i32(keylen + len(body))
        key.u16(KEY_VERSION)
        key.i32(len(payload))
        key.u32(self._datime)
        key.i16(keylen)
        key.i16(cycle)
        key.i32(seek)
        key.i32(BEGIN)
        key.string(classname)
        key.string(name)
        key.string(title)
        header = bytes(key.data) + extra
        if listed:
            self._keys.append(header)
        self._data += header + body
        return seek, keylen + len(body)

    def close(self) -> None:
        """Lay out the bookkeeping, write the whole file out, and let go."""
        if self._closed:
            return
        self._closed = True
        try:
            self._assemble()
            self._handle.write(self._data)
            if hasattr(self._handle, "flush"):
                self._handle.flush()
        finally:
            self._data = bytearray()
            if self._owned:
                self._handle.close()

    def _assemble(self) -> None:
        """Everything that could only be placed once the objects were in."""
        for tree in self._trees:
            tree._finish()  # its baskets are in; now it can say where they went

        info = _streamers(self._used)
        seek_info, nbytes_info = self._put(
            "TList", "StreamerInfo", "Doubly linked list", info, 1, listed=False
        )

        keylist = WBuffer()
        keylist.i32(len(self._keys))
        for header in self._keys:
            keylist.raw(header)
        seek_keys, nbytes_keys = self._put(
            "TFile", self._label, "", bytes(keylist.data), 1, listed=False, packed=False
        )

        seek_free = len(self._data)
        free_keylen = 34 + len(self._label.encode())
        end = seek_free + free_keylen + 10
        free = WBuffer()
        free.u16(1)
        free.i32(end)
        free.i32(BIG)
        self._put("TFile", self._label, "", bytes(free.data), 1, listed=False, packed=False)

        head = WBuffer()  # the fBEGIN block: the file is a key like any other
        keylen = 34 + len(self._label.encode())
        head.i32(self._nname + 60)
        head.u16(KEY_VERSION)
        head.i32(self._nname - keylen + 60)
        head.u32(self._datime)
        head.i16(keylen)
        head.i16(1)
        head.i32(BEGIN)
        head.i32(0)
        head.string("TFile")
        head.string(self._label)
        head.string("")
        head.string(self._label)
        head.string("")
        head.u16(5)  # the directory record the reader finds at BEGIN + nname
        head.u32(self._datime)
        head.u32(self._datime)
        head.i32(nbytes_keys)
        head.i32(self._nname)
        head.i32(BEGIN)
        head.i32(0)
        head.i32(seek_keys)
        head.u16(1)
        head.raw(self._uuid)
        head.raw(bytes(12))
        self._data[BEGIN : BEGIN + len(head.data)] = head.data

        struct.pack_into(">4sii", self._data, 0, b"root", WRITER_VERSION, BEGIN)
        struct.pack_into(
            ">iiiiiBiii",
            self._data,
            12,
            end,
            seek_free,
            free_keylen + 10,
            1,
            self._nname,
            4,
            self._codes,
            seek_info,
            nbytes_info,
        )
        struct.pack_into(">H16s", self._data, 45, 1, self._uuid)

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> WritableFile:
        return self

    def __exit__(self, kind: object, *rest: object) -> None:
        if kind is not None:
            # Whatever went wrong, do not leave a plausible half-truth behind.
            self._closed = True
            if self._owned:
                self._handle.close()
            return
        self.close()


def create(
    target: Any,
    *,
    compression: str | None = "zlib",
    level: int | None = None,
    config: Any = None,
) -> WritableFile:
    """A new ROOT file at ``target``, ready to be given objects.

        >>> with xrd.root.create("counts.root") as f:         # doctest: +SKIP
        ...     f["signal"] = xrd.root.Histogram.new("signal", edges, counts)
        ...     f["scan"] = xrd.root.Graph.new("scan", xs, ys, yerr=bars)

    ``target`` is a local path, a URL of any scheme this library writes, or
    an already-open binary file, which is used as it is and left open.
    ``compression`` is ``zlib`` unless said otherwise - ``lzma``, ``lz4``,
    ``zstd``, or ``None`` to store everything as it is - and each object is
    stored raw anyway when compressing it did not make it smaller.
    """
    if compression is not None and compression not in CODES:
        raise ValueError(
            f"compression must be one of {', '.join(CODES)} or None, not {compression!r}"
        )
    if hasattr(target, "write"):
        return WritableFile(target, getattr(target, "name", "<file>"), False, compression, level)
    url = parse(target)
    if url.is_local:
        handle: IO[bytes] = open(url.path, "wb")
    else:
        from ..io import open_url

        handle = open_url(url, "wb", config=config)
    return WritableFile(handle, str(target), True, compression, level)
