"""Writing a ``TTree``: columns declared once, then entries, then baskets.

A tree written here is laid out the way ROOT laid out the trees in
``tests/data`` - a ``TTree`` record holding one ``TBranch`` per column, each
with its own ``TLeaf`` saying what the values are, and the entries themselves
in ``TBasket`` records elsewhere in the file. That is what makes a tree worth
writing rather than an array per column: reading a range of entries back
costs one read per basket it touches, not one read of everything.

The columns are fixed-size - a number per entry, or a fixed count of numbers
per entry. Rows of varying length, strings and split objects are refused by
name rather than approximated, on the same principle as the rest of the
writer: a file ROOT misreads is worse than an error message.

Entries are buffered a basket at a time, so a tree far larger than memory
costs one basket per column and nothing else. The tree's own record has to
say where every basket landed, so it is written when the file closes.
"""

from __future__ import annotations

import array
import struct
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from .buffer import MAP_OFFSET
from .objects import LEAF_TYPES
from .writer import BASKET_BYTES, WBuffer, _checked, _keylen

if TYPE_CHECKING:  # pragma: no cover - for the type checker, not for running
    from .writer import WritableFile

__all__ = ["WritableTree", "BASKET_BYTES"]

#: The record versions written here, which are the ones :mod:`.winfo`
#: describes: what the file says about its classes is what its records are.
TREE_VERSION = 19
BRANCH_VERSION = 12
LEAF_VERSION = 2
SUBLEAF_VERSION = 1
OBJARRAY_VERSION = 3
BASKET_VERSION = 3
ATTRIBUTE_VERSION = 2

#: What ROOT puts in the fields a tree written in one pass never uses: the
#: values ``TTree``'s own constructor sets, so nothing reading finds a tree
#: that looks unlike every other tree.
MAX_ENTRIES = 1_000_000_000_000
AUTO_SAVE = -300_000_000
AUTO_FLUSH = -30_000_000
ESTIMATE = 1_000_000
SCAN_FIELD = 25
OFFSET_LEN = 1000
WEIGHT = 1.0
#: How many basket slots a branch declares room for, as ROOT's does.
MIN_BASKETS = 10

#: What ROOT calls a column of each :mod:`array` type code: the leaf class,
#: the letter a branch title spells the type with, how wide one value is, and
#: whether the values are unsigned.
LEAVES: dict[str, tuple[str, str, int, bool]] = {
    "?": ("TLeafO", "O", 1, False),
    "b": ("TLeafB", "B", 1, False),
    "B": ("TLeafB", "b", 1, True),
    "h": ("TLeafS", "S", 2, False),
    "H": ("TLeafS", "s", 2, True),
    "i": ("TLeafI", "I", 4, False),
    "I": ("TLeafI", "i", 4, True),
    "q": ("TLeafL", "L", 8, False),
    "Q": ("TLeafL", "l", 8, True),
    "f": ("TLeafF", "F", 4, False),
    "d": ("TLeafD", "D", 8, False),
}

#: The type a column spelled with a plain Python type asks for. A Python int
#: is as big as it likes, so it gets the widest ROOT has rather than one that
#: would quietly stop fitting somewhere down the file.
PYTHON_TYPES: dict[type, str] = {bool: "?", int: "q", float: "d"}

#: Type codes whose width is the platform's, resolved to codes that are not.
_WIDE = array.array("l").itemsize == 8
PLATFORM = {"l": "q" if _WIDE else "i", "L": "Q" if _WIDE else "I"}


def _typecode(name: str, spec: Any) -> tuple[str, int]:
    """What a column was declared as: a type code, and values per entry."""
    length = 1
    if isinstance(spec, (tuple, list)):
        if len(spec) != 2:
            raise ValueError(
                f"the column {name!r} is declared as {len(spec)} things; a pair says "
                f"the type and how many values every entry holds, as in ('f', 3)"
            )
        spec, length = spec
        if not isinstance(length, int) or isinstance(length, bool) or length < 1:
            raise ValueError(
                f"the column {name!r} says {length!r} values per entry, which is not a "
                f"count of them; a column holds a fixed number, one or more"
            )
    if isinstance(spec, type):
        found = PYTHON_TYPES.get(spec)
        if found is None:
            raise ValueError(
                f"the column {name!r} is declared as {spec.__name__}, and the Python "
                f"types a column can be spelled with are bool, int and float"
            )
        spec = found
    if not isinstance(spec, str):
        raise ValueError(
            f"the column {name!r} is declared as {spec!r}, which is neither a type "
            f"code such as 'f' nor a Python type such as float"
        )
    spec = PLATFORM.get(spec, spec)
    if spec not in LEAVES:
        raise ValueError(
            f"the column {name!r} is of type {spec!r}, and the types a tree here "
            f"holds are {', '.join(LEAVES)} - fixed-size numbers, nothing else"
        )
    return spec, length


class _Column:
    """One branch being filled: how its values pack, and its bytes so far."""

    __slots__ = (
        "name",
        "typecode",
        "length",
        "classname",
        "letter",
        "itemsize",
        "unsigned",
        "form",
        "size",
        "basket_size",
        "buffer",
        "pending",
        "seeks",
        "sizes",
        "starts",
        "tot_bytes",
        "zip_bytes",
    )

    def __init__(self, name: str, typecode: str, length: int, basket_size: int) -> None:
        self.name = name
        self.typecode = typecode
        self.length = length
        self.classname, self.letter, self.itemsize, self.unsigned = LEAVES[typecode]
        self.form = f">{length}{typecode}" if length > 1 else f">{typecode}"
        #: How many bytes one entry of this column takes, which is what makes
        #: a basket sliceable without a table of where each entry begins.
        self.size = length * self.itemsize
        self.basket_size = basket_size
        self.buffer = bytearray()
        self.pending = 0
        self.seeks: list[int] = []
        self.sizes: list[int] = []
        #: Where each basket starts, in entries; the last is the total, which
        #: is why this starts out holding a zero rather than nothing.
        self.starts = [0]
        self.tot_bytes = 0
        self.zip_bytes = 0

    @property
    def typename(self) -> str:
        """``'float32'``, ``'uint8'`` - what a reader will call this column."""
        name = LEAF_TYPES[self.classname][0]
        return f"u{name}" if self.unsigned else name

    @property
    def title(self) -> str:
        """The branch title, which is how ROOT spells a column's type."""
        return f"{self.leaf_title}/{self.letter}"

    @property
    def leaf_title(self) -> str:
        """The leaf title: the name, and the size of an entry if it is an array."""
        return self.name if self.length == 1 else f"{self.name}[{self.length}]"

    def pack(self, value: Any) -> bytes:
        """One entry's bytes, or a ``ValueError`` saying what was wrong with it."""
        if self.typecode == "B" and isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)  # bytes are exactly what an unsigned byte column holds
            if len(raw) != self.length:
                raise ValueError(
                    f"{self.name!r} takes {self.length} bytes per entry, and this one has "
                    f"{len(raw)}"
                )
            return raw
        if self.length == 1:
            values: Any = (value,)
        else:
            try:
                values = list(value)
            except TypeError:
                raise ValueError(
                    f"{self.name!r} takes {self.length} values per entry, and {value!r} is "
                    f"not a sequence of them"
                ) from None
            if len(values) != self.length:
                raise ValueError(
                    f"{self.name!r} takes {self.length} values per entry, and this one has "
                    f"{len(values)}"
                )
        try:
            return struct.pack(self.form, *values)
        except (struct.error, TypeError) as exc:
            raise ValueError(
                f"{self.name!r} holds {self.typename} values, and this entry is not one "
                f"of those: {exc}"
            ) from None


def _objarray(buf: WBuffer, count: int) -> int:
    """Open a ``TObjArray`` of ``count`` things, for the caller to write and end."""
    index = buf.start(OBJARRAY_VERSION)
    buf.tobject()
    buf.string("")
    buf.i32(count)
    buf.i32(0)  # the lower bound: every array here counts from zero
    return index


def _attributes(buf: WBuffer) -> None:
    """The line, fill and marker bases a tree carries, at ROOT's defaults."""
    index = buf.start(ATTRIBUTE_VERSION)
    buf.i16(602)  # fLineColor
    buf.i16(1)  # fLineStyle
    buf.i16(1)  # fLineWidth
    buf.end(index)
    index = buf.start(ATTRIBUTE_VERSION)
    buf.i16(0)  # fFillColor
    buf.i16(1001)  # fFillStyle
    buf.end(index)
    index = buf.start(ATTRIBUTE_VERSION)
    buf.i16(1)  # fMarkerColor
    buf.i16(1)  # fMarkerStyle
    buf.raw(struct.pack(">f", 1.0))  # fMarkerSize
    buf.end(index)


def _leaf(buf: WBuffer, column: _Column) -> int:
    """One ``TLeaf``; where it landed comes back, for the tree to point at."""
    at = buf.tag(column.classname)
    outer = buf.start(SUBLEAF_VERSION)
    inner = buf.start(LEAF_VERSION)
    buf.named(column.name, column.leaf_title)
    buf.i32(column.length)
    buf.i32(column.itemsize)
    buf.i32(0)  # fOffset: one leaf per branch, so an entry starts where it starts
    buf.u8(0)  # fIsRange: no smallest and largest were recorded
    buf.u8(int(column.unsigned))
    buf.u32(0)  # fLeafCount: nothing counts this column, its size is fixed
    buf.end(inner)
    buf.raw(bytes(2 * column.itemsize))  # fMinimum and fMaximum, both left at zero
    buf.end(outer)
    buf.end(at)
    return at


def _table(buf: WBuffer, values: list[int], width: int, code: str) -> None:
    """One of a branch's basket tables: a marker, then a value per slot."""
    buf.u8(1)  # the marker saying the table is here rather than absent
    buf.raw(struct.pack(f">{width}{code}", *values, *([0] * (width - len(values)))))


def _branch(buf: WBuffer, column: _Column, entries: int, compress: int) -> int:
    """One ``TBranch``, leaf and all; where its leaf landed comes back."""
    at = buf.tag("TBranch")
    index = buf.start(BRANCH_VERSION)
    buf.named(column.name, column.title)
    fill = buf.start(ATTRIBUTE_VERSION)
    buf.i16(0)  # fFillColor
    buf.i16(0)  # fFillStyle
    buf.end(fill)
    written = len(column.seeks)
    width = max(MIN_BASKETS, written + 1)
    buf.i32(compress)
    buf.i32(column.basket_size)
    buf.i32(0)  # fEntryOffsetLen: every entry is the same size, so none is needed
    buf.i32(written)
    buf.i64(entries)  # fEntryNumber
    buf.i32(0)  # fOffset
    buf.i32(width)  # fMaxBaskets
    buf.i32(0)  # fSplitLevel: nothing here is split, a column is a column
    buf.i64(entries)
    buf.i64(0)  # fFirstEntry
    buf.i64(column.tot_bytes)
    buf.i64(column.zip_bytes)
    empty = _objarray(buf, 0)  # fBranches: a column has nothing under it
    buf.end(empty)
    leaves = _objarray(buf, 1)
    place = _leaf(buf, column)
    buf.end(leaves)
    baskets = _objarray(buf, written + 1)  # fBaskets: all on file, none in here
    buf.raw(bytes(4 * (written + 1)))
    buf.end(baskets)
    _table(buf, column.sizes, width, "i")  # fBasketBytes
    _table(buf, column.starts, width, "q")  # fBasketEntry
    _table(buf, column.seeks, width, "q")  # fBasketSeek
    buf.string("")  # fFileName: the baskets are in this file, where they were put
    buf.end(index)
    buf.end(at)
    return place


def _row_trouble(missing: list[str], unknown: list[str]) -> list[str]:
    trouble = []
    if missing:
        trouble.append(f"nothing for {', '.join(missing)}")
    if unknown:
        trouble.append(f"{', '.join(unknown)}, which is not a column")
    return trouble


class WritableTree:
    """A tree being written: named columns, then one entry at a time.

        >>> with xrd.root.create("out.root") as f:            # doctest: +SKIP
        ...     tree = f.tree("events", {"energy": float, "hits": ("i", 4)})
        ...     tree.fill(energy=12.5, hits=[3, 1, 4, 1])

    A column is declared as a type - a Python ``bool``, ``int`` or ``float``,
    or an :mod:`array` type code such as ``'f'`` for a narrower one - or as a
    pair of a type and how many values each entry holds. Every entry needs a
    value for every column, because a tree whose columns disagree about how
    many entries they have is a tree nothing can read.

    Entries are written out a basket at a time as they gather, so the tree
    can be far larger than memory; nothing has to be kept but the file the
    tree is going into, and the tree's own record goes in when that closes.
    """

    def __init__(
        self,
        file: WritableFile,
        name: str,
        title: str,
        columns: Mapping[str, Any],
        basket_size: int,
        cycle: int,
    ) -> None:
        if not isinstance(columns, Mapping):
            raise TypeError(
                f"the columns are a {type(columns).__name__}; a tree is declared with a "
                f"mapping of column name to type, as in {{'energy': float}}"
            )
        if not columns:
            raise ValueError(
                "a tree with no columns holds nothing that could be read back; "
                "declare what each entry is made of"
            )
        if not isinstance(basket_size, int) or basket_size < 1:
            raise ValueError(f"the basket size is {basket_size!r}, which is not a size in bytes")
        self._file = file
        self._cycle = cycle
        #: What the tree is called, and what it says it is.
        self.name = name
        self.title = title
        self._entries = 0
        self._columns: dict[str, _Column] = {}
        for column, spec in columns.items():
            _checked(column, "column name")
            if not column or any(bad in column for bad in "./;[] "):
                raise ValueError(
                    f"{column!r} is not a name a column can have: a name is not empty, "
                    f"and holds none of . / ; [ ] or a space, all of which mean "
                    f"something else to anything reading the tree back"
                )
            typecode, length = _typecode(column, spec)
            self._columns[column] = _Column(column, typecode, length, basket_size)

    def __repr__(self) -> str:
        return (
            f"<WritableTree {self.name!r} with {len(self._columns)} columns "
            f"and {self._entries} entries so far>"
        )

    def __len__(self) -> int:
        """How many entries have been filled, as ``len`` of a tree read back."""
        return self._entries

    @property
    def num_entries(self) -> int:
        """How many entries have been filled, under the reader's name for it."""
        return self._entries

    @property
    def columns(self) -> dict[str, str]:
        """What each column holds: ``{'energy': 'float64', 'hits': 'int32[4]'}``."""
        return {
            name: column.typename if column.length == 1 else f"{column.typename}[{column.length}]"
            for name, column in self._columns.items()
        }

    @property
    def classes(self) -> tuple[str, ...]:
        """The classes this tree will be made of, for the file to describe."""
        return ("TTree", "TBranch", *dict.fromkeys(c.classname for c in self._columns.values()))

    def fill(self, **values: Any) -> None:
        """Add one entry, with a value for every column.

            >>> tree.fill(energy=12.5, hits=[3, 1, 4, 1])     # doctest: +SKIP

        An entry that does not fit its columns is refused whole: nothing is
        written for any column, so the tree is exactly as it was.
        """
        self._row(values)

    def extend(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Add every entry in ``rows``, each a mapping of column to value.

        >>> tree.extend([{"energy": 1.0}, {"energy": 2.0}])   # doctest: +SKIP
        """
        for row in rows:
            self._row(row)

    def _row(self, row: Mapping[str, Any]) -> None:
        """One entry: packed in full before any of it is kept."""
        self._require_open()
        self._require_columns(row)
        packed = [(column, column.pack(row[name])) for name, column in self._columns.items()]
        self._store_row(packed)

    def _require_open(self) -> None:
        if self._file.closed:
            raise ValueError(
                f"the file this tree is in is closed; {self.name!r} holds the "
                f"{self._entries} entries it was given"
            )

    def _require_columns(self, row: Mapping[str, Any]) -> None:
        missing = [name for name in self._columns if name not in row]
        unknown = [str(name) for name in row if name not in self._columns]
        if not missing and not unknown:
            return
        trouble = _row_trouble(missing, unknown)
        raise ValueError(
            f"this entry of {self.name!r} has {' and '.join(trouble)}; its columns "
            f"are {', '.join(self._columns)}, and every entry needs all of them"
        )

    def _store_row(self, packed: list[tuple[_Column, bytes]]) -> None:
        for column, raw in packed:
            column.buffer += raw
            column.pending += 1
        self._entries += 1
        for column, _raw in packed:
            if len(column.buffer) >= column.basket_size:
                self._flush(column)

    def _flush(self, column: _Column) -> None:
        """Write what one column has gathered as a basket of its own."""
        if not column.pending:
            return
        payload = bytes(column.buffer)
        keylen = _keylen("TBasket", column.name, self.name, extra=19)
        extra = struct.pack(
            ">hiiiiB",
            BASKET_VERSION,
            column.basket_size,
            column.size,  # fNevBufSize: one entry's bytes, every entry the same
            column.pending,
            keylen + len(payload),  # fLast: where the entries end
            0,  # no table of entry offsets, because none is needed
        )
        seek, nbytes = self._file._put(
            "TBasket", column.name, self.name, payload, 0, listed=False, extra=extra
        )
        column.seeks.append(seek)
        column.sizes.append(nbytes)
        column.starts.append(column.starts[-1] + column.pending)
        column.tot_bytes += keylen + len(payload)
        column.zip_bytes += nbytes
        column.buffer = bytearray()
        column.pending = 0

    def _finish(self) -> None:
        """Flush what is left, then write the record that ties it all together."""
        for column in self._columns.values():
            self._flush(column)
        keylen = _keylen("TTree", self.name, self.title)
        payload = self._payload(keylen)
        self._file._put("TTree", self.name, self.title, payload, self._cycle, listed=True)

    def _payload(self, origin: int) -> bytes:
        """The tree's own record: its fields, its branches, then its leaves.

        ``origin`` is how long the key in front of this will be, because the
        leaves are written once inside their branches and referred to after
        by where they landed - counted, as every place in a record is, from
        the start of the key rather than the start of the record.
        """
        buf = WBuffer()
        index = buf.start(TREE_VERSION)
        buf.named(self.name, self.title)
        _attributes(buf)
        columns = list(self._columns.values())
        buf.i64(self._entries)
        buf.i64(sum(column.tot_bytes for column in columns))
        buf.i64(sum(column.zip_bytes for column in columns))
        buf.i64(0)  # fSavedBytes: nothing was saved part way, this went in one go
        buf.i64(0)  # fFlushedBytes
        buf.raw(struct.pack(">d", WEIGHT))
        buf.i32(0)  # fTimerInterval
        buf.i32(SCAN_FIELD)
        buf.i32(0)  # fUpdate
        buf.i32(OFFSET_LEN)  # fDefaultEntryOffsetLen
        buf.i32(0)  # fNClusterRange: no cluster boundaries were declared
        buf.i64(MAX_ENTRIES)
        buf.i64(MAX_ENTRIES)  # fMaxEntryLoop
        buf.i64(0)  # fMaxVirtualSize
        buf.i64(AUTO_SAVE)
        buf.i64(AUTO_FLUSH)
        buf.i64(ESTIMATE)
        buf.u8(0)  # fClusterRangeEnd, of which there are none
        buf.u8(0)  # fClusterSize, likewise
        compress = self._file._codes
        branches = _objarray(buf, len(columns))
        places = [_branch(buf, column, self._entries, compress) for column in columns]
        buf.end(branches)
        leaves = _objarray(buf, len(columns))
        for place in places:
            buf.u32(origin + place + MAP_OFFSET)  # the leaf itself is in its branch
        buf.end(leaves)
        buf.u32(0)  # fAliases: none
        buf.i32(0)  # fIndexValues: an empty TArrayD
        buf.i32(0)  # fIndex: an empty TArrayI
        buf.u32(0)  # fTreeIndex: none
        buf.u32(0)  # fFriends: none
        buf.u32(0)  # fUserInfo: none
        buf.u32(0)  # fBranchRef: none
        buf.end(index)
        return bytes(buf.data)
