"""What a tree looks like once it has been read: columns you can ask for.

The unit of I/O is the basket - a compressed block of consecutive entries for
one branch - and everything here is arranged around that. Asking for entries
100 to 200 reads the baskets that hold them and nothing else, which is what
makes it reasonable to iterate a hundred-gigabyte tree over the network from a
laptop: the bytes that cross the wire are the ones asked for.
"""

from __future__ import annotations

import array
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

from .buffer import Buffer
from .compression import decompress
from .errors import UnsupportedFeatureError
from .interp import Column, Flat, Members, Refused, Rows, Values, build

if TYPE_CHECKING:
    from .file import Source
    from .objects import BranchRecord, LeafRecord

__all__ = ["Jagged", "Branch", "Group", "TTree"]

#: Entries per step when iterating, if nobody says otherwise.
DEFAULT_STEP = 10_000


class Jagged(Sequence[Any]):
    """Rows of different lengths: one flat array, and where each row starts.

        >>> jets.tolist()                          # doctest: +SKIP
        [[12.5, 3.5], [], [88.0, 1.25, 0.5]]

    This is the shape a physics file is usually in - a variable number of
    particles per collision - and it is kept flat because that is how it
    arrives and how a tensor wants it back.
    """

    __slots__ = ("content", "offsets")

    def __init__(self, content: array.array[Any], offsets: array.array[Any]) -> None:
        self.content = content
        self.offsets = offsets

    def __repr__(self) -> str:
        return f"<Jagged {len(self)} rows of {len(self.content)} {self.content.typecode} values>"

    def __len__(self) -> int:
        return len(self.offsets) - 1

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError("row out of range")
        return self.content[self.offsets[index] : self.offsets[index + 1]]

    def lengths(self) -> list[int]:
        """How long each row is."""
        return [self.offsets[i + 1] - self.offsets[i] for i in range(len(self))]

    def tolist(self) -> list[list[Any]]:
        return [list(row) for row in self]

    def padded(self, width: int | None = None, fill: float = 0.0) -> tuple[array.array[Any], int]:
        """A rectangle: every row cut or filled to the same width.

        Gives back the flat values and the width, which is what a tensor of
        shape ``(rows, width)`` is made of. ``width`` defaults to the longest
        row, so nothing is lost unless a number is asked for.
        """
        if width is None:
            width = max(self.lengths(), default=0)
        code = self.content.typecode
        out = array.array(code, [fill if code in "fd" else int(fill)]) * (len(self) * width)
        for row in range(len(self)):
            start, end = self.offsets[row], self.offsets[row + 1]
            end = min(end, start + width)
            out[row * width : row * width + (end - start)] = self.content[start:end]
        return out, width


class Basket:
    """One compressed block of entries, decompressed and ready to slice.

    Nearly always a record of its own somewhere else in the file, which is
    what makes reading a range of entries cheap. A tree small enough never to
    have flushed one keeps its baskets inside the branch instead, and those
    arrive with their bytes already in hand.
    """

    __slots__ = ("keylen", "nevsize", "nevbuf", "last", "data", "offsets")

    def __init__(
        self,
        keylen: int,
        nevsize: int,
        nevbuf: int,
        last: int,
        data: bytes,
        offsets: list[int],
    ) -> None:
        self.keylen = keylen
        self.nevsize = nevsize
        self.nevbuf = nevbuf
        self.last = last
        self.data = data
        self.offsets = offsets

    @classmethod
    def keyed(cls, source: Source, seek: int, nbytes: int, has_offsets: bool) -> Basket:
        """The usual kind: a record of its own, read from where the branch says."""
        from .file import Key

        raw = source.read(seek, nbytes)
        head = Buffer(raw)
        key = Key(head)
        head.i16()  # the basket's own version, which adds nothing we use
        head.i32()  # the buffer size it was built with
        nevsize = head.i32()
        if nevsize < 0:  # a negative size means feature bits follow
            nevsize = -nevsize
            head.skip_record()
        nevbuf, last = head.i32(), head.i32()

        payload = raw[key.keylen :]
        data = decompress(payload, key.objlen) if key.compressed else payload
        offsets: list[int] = []
        if has_offsets:
            at = Buffer(data, key.keylen)
            at.pos = last
            offsets = at.i32s(at.i32())
        return cls(key.keylen, nevsize, nevbuf, last, data, offsets)

    @classmethod
    def inline(cls, buf: Buffer) -> Basket | None:
        """A basket written into the branch record, bytes and all.

        ``None`` says this one kept no bytes and the branch's seek point is
        where they are, which is how a tree that was flushed writes the
        placeholders it has already emptied.
        """
        from .file import Key

        key = Key(buf)
        version = buf.i16()
        buf.i32()  # the buffer size it was built with
        nevsize = buf.i32()
        if nevsize < 0:
            nevsize = -nevsize
            buf.skip_record()
        nevbuf, last = buf.i32(), buf.i32()
        flag, offsets = _inline_offsets(buf, buf.u8(), nevbuf)
        if flag != 1 and flag <= 10:
            return None
        size = last if version > 1 else buf.i32()
        return cls(key.keylen, nevsize, nevbuf, last, buf.take(size)[key.keylen :], offsets)

    def start_of(self, entry: int, offset: int) -> int:
        """Where a leaf's bytes for one entry begin, in the payload."""
        if self.offsets:
            return self.offsets[entry] - self.keylen + offset
        return entry * self.nevsize + offset

    def end_of(self, entry: int) -> int:
        """Where the entry after this one begins, for a row of unknown length."""
        boundary = self.offsets[entry + 1] if entry + 1 < self.nevbuf else self.last
        return boundary - self.keylen


def _inline_offsets(buf: Buffer, flag: int, entries: int) -> tuple[int, list[int]]:
    if flag >= 80:
        return flag - 80, []  # offsets dropped; derive them from entry size
    if flag % 10 != 1 or not entries:
        return flag, []
    offsets = buf.i32s(buf.i32())
    if 20 < flag < 40:  # the top byte is a displacement, not a place
        offsets = [at & 0xFFFFFF for at in offsets]
    if flag > 40:
        buf.i32s(buf.i32())  # displacements into a tree of objects
    return flag, offsets


class Branch:
    """One column of a tree: a name, a type, and the entries under it.

        >>> tree["pt"].array(0, 1000)              # doctest: +SKIP
        array('f', [22.5, 19.0, ...])

    A branch holding several leaves appears once per leaf, named
    ``branch.leaf``, which is how ROOT itself writes such a name.
    """

    __slots__ = ("name", "record", "leaf", "column", "_source", "_cached")

    def __init__(
        self,
        name: str,
        record: BranchRecord,
        leaf: LeafRecord,
        column: Column,
        source: Source,
    ) -> None:
        self.name = name
        self.record = record
        self.leaf = leaf
        #: How this column's bytes turn into values.
        self.column = column
        self._source = source
        self._cached: tuple[int, Basket] | None = None

    def __repr__(self) -> str:
        return f"<Branch {self.name!r} of {self.typename or self.leaf.classname}>"

    def __len__(self) -> int:
        return self.num_entries

    @property
    def title(self) -> str:
        return self.leaf.title

    @property
    def num_entries(self) -> int:
        return self.record.entries

    @property
    def typename(self) -> str | None:
        """``'float32'``, ``'list[str]'``, or ``None`` if it cannot be decoded."""
        return self.column.typename

    @property
    def length(self) -> int:
        """How many values each entry holds, for a column of fixed-size arrays.

        ``1`` for a plain number, ``10`` for ``x[10]``; the flat array a fixed
        column gives back is this wide per entry, which is the shape to give a
        tensor. A variable column says ``1`` here and means the lengths in
        :class:`Jagged` instead.
        """
        return self.column.length if isinstance(self.column, Flat) else 1

    @property
    def is_jagged(self) -> bool:
        """Does the number of values per entry change from entry to entry?"""
        return isinstance(self.column, Rows)

    @property
    def num_baskets(self) -> int:
        return max(len(self.record.basket_seek), len(self.record.baskets))

    def _refuse_if_unreadable(self) -> None:
        if isinstance(self.column, Refused):
            raise UnsupportedFeatureError(
                f"{self.name!r} holds {self.column.reason}; tree.unreadable lists every "
                f"column this file has that cannot be read, each with its reason"
            )
        if self.is_jagged and len(self.record.leaves) > 1:
            raise UnsupportedFeatureError(
                f"{self.name!r} is a variable-length leaf sharing a branch with "
                f"{len(self.record.leaves) - 1} others, where the file does not say alone how "
                f"long each row is; write it as its own branch and it reads"
            )

    def _bounds(self, entry_start: int, entry_stop: int | None) -> tuple[int, int]:
        total = self.num_entries
        start = total + entry_start if entry_start < 0 else entry_start
        stop = total if entry_stop is None else entry_stop
        if stop < 0:
            stop += total
        return max(start, 0), min(stop, total)

    def _spans(self, start: int, stop: int) -> Iterator[tuple[int, int, int]]:
        """Which baskets hold ``[start, stop)``, and the part of each to take."""
        bounds = self.record.basket_entry
        for index in range(self.num_baskets):
            low, high = bounds[index], bounds[index + 1]
            if high <= start or low >= stop:
                continue
            yield index, max(start, low) - low, min(stop, high) - low

    def basket(self, index: int) -> Basket:
        """Read one basket, remembering the last so a small step is not a reread."""
        if index < len(self.record.baskets):
            return self.record.baskets[index]  # already here, and never on its own
        if self._cached is not None and self._cached[0] == index:
            return self._cached[1]
        basket = Basket.keyed(
            self._source,
            self.record.basket_seek[index],
            self.record.basket_bytes[index],
            self.record.entry_offset_len > 0,
        )
        self._cached = (index, basket)
        return basket

    def array(self, entry_start: int = 0, entry_stop: int | None = None) -> Any:
        """The values for a range of entries.

        Fixed-size columns come back as an :class:`array.array`, variable ones
        as :class:`Jagged`, and anything that is neither - strings, lists of
        lists, maps - as a list with one Python value per entry.
        """
        self._refuse_if_unreadable()
        start, stop = self._bounds(entry_start, entry_stop)
        column = self.column
        if isinstance(column, Values):
            return self._objects(column, start, stop)
        if isinstance(column, Rows):
            return self._rows(column, start, stop)
        assert isinstance(column, Flat)
        return self._flat(column, start, stop)

    def _flat(self, column: Flat, start: int, stop: int) -> Any:
        size = column.length * column.itemsize
        offset = self.leaf.offset
        content = bytearray()
        for index, low, high in self._spans(start, stop):
            basket = self.basket(index)
            if not basket.offsets and offset == 0 and basket.nevsize == size:
                content += basket.data[low * size : high * size]  # the whole run at once
            else:
                for entry in range(low, high):
                    at = basket.start_of(entry, offset)
                    content += basket.data[at : at + size]
        return column.decode(bytes(content))

    def _rows(self, column: Rows, start: int, stop: int) -> Jagged:
        content = bytearray()
        counts: list[int] = []
        for index, low, high in self._spans(start, stop):
            basket = self.basket(index)
            for entry in range(low, high):
                at, end = column.span(basket, entry, self.leaf.offset)
                counts.append((end - at) // column.itemsize)
                content += basket.data[at:end]
        values = column.decode(bytes(content))
        offsets = array.array("q", [0]) * (len(counts) + 1)
        total = 0
        for row, count in enumerate(counts):
            total += count
            offsets[row + 1] = total
        return Jagged(values, offsets)

    def _objects(self, column: Values, start: int, stop: int) -> list[Any]:
        out = []
        for index, low, high in self._spans(start, stop):
            basket = self.basket(index)
            reader = Buffer(basket.data, basket.keylen)
            for entry in range(low, high):
                at = basket.start_of(entry, self.leaf.offset) + basket.keylen
                out.append(column.value(reader, at))
        return out


class Group(Branch):
    """A split C++ object, put back together from the branches under it.

        >>> tree["evt"].array(0, 1)                # doctest: +SKIP
        [{'I32': -1, 'Str': 'evt-000', 'P3': {'Px': -1, 'Py': -1.0, 'Pz': -1}}]

    ROOT splits an object into one branch per member and leaves the object
    itself holding nothing at all. Every member is readable on its own - and
    reading them on their own is what makes a large file cheap to walk - so
    this is a convenience rather than the only way in: it reads the same
    baskets and pays the same price, and gives back the object per entry
    instead of the columns across entries.

    A member this reader will not decode is left out of the dictionary and
    named in :attr:`unreadable`, which is the same sentence ``tree.unreadable``
    gives for it under its own name.
    """

    __slots__ = ("members", "_branches")

    def __init__(
        self,
        name: str,
        record: BranchRecord,
        leaf: LeafRecord,
        source: Source,
        branches: dict[str, Branch],
    ) -> None:
        super().__init__(name, record, leaf, Members(), source)
        #: The member's own name, against the branch name it is kept under.
        self.members: dict[str, str] = {}
        self._branches = branches

    def __repr__(self) -> str:
        return f"<Group {self.name!r} of {len(self.members)} members>"

    @property
    def unreadable(self) -> dict[str, str]:
        """The members left out of each dictionary, and why."""
        return {
            member: column.reason
            for member, label in self.members.items()
            if isinstance(column := self._branches[label].column, Refused)
        }

    def array(self, entry_start: int = 0, entry_stop: int | None = None) -> list[dict[str, Any]]:
        """One dictionary per entry, a key for each member that reads."""
        start, stop = self._bounds(entry_start, entry_stop)
        rows: list[dict[str, Any]] = [{} for _ in range(max(stop - start, 0))]
        for member, label in self.members.items():
            branch = self._branches[label]
            if isinstance(branch.column, Refused):
                continue
            values = branch.array(start, stop)
            width = branch.length
            for index, row in enumerate(rows):
                row[member] = (
                    values[index * width : (index + 1) * width] if width > 1 else values[index]
                )
        return rows


class TTree:
    """A tree, and the columns in it.

        >>> tree                                   # doctest: +SKIP
        <TTree 'events' with 4 branches and 1000 entries>
        >>> for batch in tree.iterate(["pt", "eta"], step=1000):   # doctest: +SKIP
        ...     train(batch["pt"], batch["eta"])

    ``len(tree)`` is the number of entries, because that is what a tree is a
    lot of; the number of columns is ``len(tree.branches)``.
    """

    __slots__ = ("name", "title", "num_entries", "branches", "unreadable", "_source")

    def __init__(
        self,
        name: str,
        title: str,
        num_entries: int,
        records: list[BranchRecord],
        source: Source,
    ) -> None:
        self.name = name
        self.title = title
        self.num_entries = num_entries
        self._source = source
        self.branches: dict[str, Branch] = {}
        self.unreadable: dict[str, str] = {}
        for top in records:
            self._add(top, source)

    def _add(self, record: BranchRecord, source: Source) -> list[str]:
        """Take in one branch and everything below it; say what it is called.

        A branch that holds no baskets but has branches under it is ROOT's
        way of writing a split object: nothing of it is in the file except
        its members, so it becomes a :class:`Group` over them rather than a
        column that cannot be read.
        """
        from .objects import LeafRecord

        many = len(record.leaves) > 1
        labels = self._add_leaves(record, source, many)
        split = record.branches and not record.basket_seek and not many
        if not split:
            self._add_children(record, source)
            return labels
        leaf = record.leaves[0] if record.leaves else LeafRecord("TLeafElement")
        group = Group(record.name, record, leaf, source, self.branches)
        self.branches[record.name] = group  # in place, where its own leaf was
        self.unreadable.pop(record.name, None)
        self._add_group_children(record, source, group)
        return [record.name]

    def _add_leaves(self, record: BranchRecord, source: Source, many: bool) -> list[str]:
        labels = []
        for leaf in record.leaves:
            label = f"{record.name}.{leaf.name}" if many else record.name
            column = build(record, leaf, source)
            self.branches[label] = Branch(label, record, leaf, column, source)
            if isinstance(column, Refused):
                self.unreadable[label] = column.reason
            labels.append(label)
        return labels

    def _add_children(self, record: BranchRecord, source: Source) -> None:
        for child in record.branches:
            self._add(child, source)

    def _add_group_children(self, record: BranchRecord, source: Source, group: Group) -> None:
        prefix = f"{record.name}."
        for child in record.branches:
            for label in self._add(child, source):
                group.members[label.removeprefix(prefix)] = label

    def __repr__(self) -> str:
        return (
            f"<TTree {self.name!r} with {len(self.branches)} branches "
            f"and {self.num_entries} entries>"
        )

    def __len__(self) -> int:
        return self.num_entries

    def __iter__(self) -> Iterator[str]:
        return iter(self.branches)

    def __contains__(self, name: object) -> bool:
        return name in self.branches

    def __getitem__(self, name: str) -> Branch:
        try:
            return self.branches[name]
        except KeyError:
            raise KeyError(
                f"{name!r} is not a branch of {self.name!r}; there is " + ", ".join(self.branches)
            ) from None

    def keys(self) -> list[str]:
        """Every column, readable here or not."""
        return list(self.branches)

    def readable(self) -> list[str]:
        """The columns this reader can decode, which is what ``arrays`` defaults to.

        A split object is not one of them: its members are already here under
        their own names, and taking both would read every basket twice.
        """
        return [
            name
            for name, branch in self.branches.items()
            if name not in self.unreadable and not isinstance(branch, Group)
        ]

    def groups(self) -> list[str]:
        """The split objects: branches whose members are the branches under them."""
        return [name for name, branch in self.branches.items() if isinstance(branch, Group)]

    def typenames(self) -> dict[str, str]:
        """What each column holds, in Python's words."""
        return {
            name: branch.typename or f"? ({branch.leaf.classname})"
            for name, branch in self.branches.items()
        }

    def show(self) -> str:
        """A one-line-per-column summary, for looking before leaping.

        >>> print(tree.show())                 # doctest: +SKIP
        pt        float32   variable
        nmuon     int32
        """
        lines = []
        for name, branch in self.branches.items():
            kind = branch.typename or f"? ({branch.leaf.classname})"
            lines.append(f"{name:<24} {kind:<10}{' variable' if branch.is_jagged else ''}".rstrip())
        return "\n".join(lines)

    def arrays(
        self,
        names: Sequence[str] | None = None,
        entry_start: int = 0,
        entry_stop: int | None = None,
    ) -> dict[str, Any]:
        """Several columns at once, over the same range of entries.

        With no names, every column this reader can decode; the ones it cannot
        are in :attr:`unreadable` with the reason, rather than quietly missing.
        """
        wanted = self.readable() if names is None else list(names)
        return {name: self[name].array(entry_start, entry_stop) for name in wanted}

    def iterate(
        self,
        names: Sequence[str] | None = None,
        *,
        step: int = DEFAULT_STEP,
        entry_start: int = 0,
        entry_stop: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Walk the tree in batches, reading only what each batch needs.

            >>> for batch in tree.iterate(step=50_000):        # doctest: +SKIP
            ...     ...

        This is the one to reach for over a network: memory is one step, not
        one file, and a tree far larger than the machine goes through it.
        """
        if step <= 0:
            raise ValueError("step must be at least one entry")
        stop = self.num_entries if entry_stop is None else min(entry_stop, self.num_entries)
        at = entry_start
        while at < stop:
            yield self.arrays(names, at, min(at + step, stop))
            at += step
