"""Training data, from a URL to a training loop, without a download.

    >>> import xrd.ml                                              # doctest: +SKIP
    >>> data = xrd.ml.load("root://eos.example.org//store/mnist.root")
    >>> print(data)                                                # doctest: +SKIP
    mnist.root: 70,000 rows, 10 classes
      inputs   image: 784 x uint8, scaled to 0-1
      answer   label: int32
      splits   train 60,000 rows, test 10,000 rows
    >>> for images, labels in data.train.batches(256):             # doctest: +SKIP
    ...     loss = criterion(model(images), labels)

That is the whole of it. Nothing is downloaded, nothing is opened twice, and
nothing about the file's layout has to be known first: the trees say which
rows are for training and which class each of them is, the columns say which
is the picture and which the answer, and every minibatch is a read of one
basket out of the file wherever it lives.

What a batch holds is decided the way a person would decide it. Every input
column becomes one ``(rows, features)`` block of ``float32``, side by side if
there are several; a column of bytes is a picture, so it is divided by 255
into the nought-to-one that networks expect; and the answer comes back as
whole numbers for a classifier or as floats for a regression, because that is
what the loss functions of each take. Pass ``scale=False`` to be handed the
numbers exactly as the file holds them.

This module is the friendly face of :mod:`xrd.root.ml`, which is where the
tensors are actually made and where to go for control over any of it.
PyTorch is not imported until a batch is asked for, so a file can be opened,
described and read here on a machine that has no framework installed at all.
"""

from __future__ import annotations

import array
import dataclasses
import json
import math
import os
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any, cast

from .root import open_root
from .root.interp import Numeric

# ``_torch`` is the import of PyTorch with the refusal that says what to
# install; there is one of those in this library and this module wants it too.
from .root.ml import _torch, mixed, numeric

if TYPE_CHECKING:
    from .config import Config
    from .root.file import ROOTFile
    from .root.tree import TTree

__all__ = ["load", "Dataset", "Split", "Column"]

#: Tree-name prefixes that mean "this part of the data", in the order they
#: belong in a summary: a dataset is trained on the first and scored on the
#: last, whatever else it may have in between.
SPLITS = ("train", "validation", "valid", "val", "dev", "eval", "test")

#: Column names that hold the answer rather than the question, best first.
#: The one a file has decides what a batch's second half is.
ANSWERS = ("label", "target", "class", "y")

#: Columns that are neither: bookkeeping written beside the data so that a row
#: can be traced back to where it came from.
BOOKKEEPING = ("index", "entry")

#: How much of the file to hold at once, in bytes, when nobody says otherwise.
#: Rows are read in pools and shuffled within them, so this is the memory a
#: training loop costs no matter how large the dataset is; sixty-four
#: megabytes is small enough for a laptop and large enough that the reads are
#: whole baskets rather than scraps.
POOL_BYTES = 64 * 1024 * 1024

#: Darkest last: a byte becomes one of these when a picture is printed.
SHADES = " .:-=+*#%@"


@dataclasses.dataclass(frozen=True)
class Column:
    """One column of the file: what it holds and how much of it a row holds."""

    name: str
    #: What the numbers are, in Python's words - ``uint8``, ``float32``.
    typename: str
    #: The :mod:`array` code behind that name, which is what decides its size.
    typecode: str
    #: How many values one row holds; zero when the rows differ in length.
    width: int

    @property
    def itemsize(self) -> int:
        """How many bytes one value takes."""
        return array.array(self.typecode).itemsize

    @property
    def is_picture(self) -> bool:
        """Whether a row of this is a square greyscale image, worth showing."""
        side = math.isqrt(self.width)
        return self.typecode == "B" and side > 1 and side * side == self.width

    def __str__(self) -> str:
        if not self.width:
            return f"{self.name}: {self.typename}, a different number of values each row"
        if self.width == 1:
            return f"{self.name}: {self.typename}"
        return f"{self.name}: {self.width} x {self.typename}"


@dataclasses.dataclass(frozen=True)
class _Part:
    """Rows of one tree, and the class they all are if the file says so."""

    tree: TTree
    start: int
    stop: int
    label: str

    @property
    def rows(self) -> int:
        return self.stop - self.start


def load(
    source: Any,
    *,
    inputs: Sequence[str] | None = None,
    answer: str | None = None,
    scale: bool = True,
    step: int | None = None,
    config: Config | None = None,
) -> Dataset:
    """Open a dataset, wherever it is.

        >>> data = load("root://host//store/mnist.root")           # doctest: +SKIP
        >>> data = load("mnist")                                   # doctest: +SKIP

    ``source`` is a URL of any scheme this library speaks, a local path, or an
    open binary file. A bare name - no scheme, no slash, no file where one
    would be - is looked up in the catalogue named by
    :attr:`~xrd.Config.catalogue` (usually the ``XRD_CATALOGUE`` environment
    variable), which is the ``index.json`` an ``xrd-datasets`` site serves;
    that is how the second line above finds the same file as the first.

    The columns to learn from and the one to learn are worked out from their
    names - see :class:`Dataset` - and ``inputs`` and ``answer`` say so
    outright when a file's names are its own.

    The dataset holds the file open. Use it in a ``with`` block, or let it go
    and it closes itself.
    """
    if isinstance(source, str) and _is_name(source):
        source = _from_catalogue(source, config)
    handle = open_root(source, config=config)
    try:
        return Dataset(
            handle, inputs=inputs, answer=answer, scale=scale, step=step, owned=True
        )
    except BaseException:
        handle.close()  # a file whose rows make no dataset should not stay open
        raise


def _is_name(source: str) -> bool:
    """Whether ``source`` is a catalogue name rather than somewhere to look.

    Anything that could plausibly be a place - a scheme, a slash, a ``.root``
    suffix, or a file that actually exists here - is treated as one, so no
    file anyone can already open is ever shadowed by a catalogue entry.
    """
    return (
        "://" not in source
        and "/" not in source
        and not source.endswith(".root")
        and not os.path.exists(source)
    )


def _from_catalogue(name: str, config: Config | None) -> str:
    """The URL behind ``name``, according to the catalogue's ``index.json``."""
    from .config import Config as _Config
    from .root.datasets import fetch

    where = (config or _Config()).catalogue
    if not where:
        raise ValueError(
            f"{name!r} is not a path or a URL, and no catalogue is set to look "
            "it up in - point XRD_CATALOGUE (or Config.catalogue) at a "
            "datasets site, or give the whole URL"
        )
    base = where.rstrip("/")
    index = json.loads(fetch(f"{base}/index.json", config=config))
    for entry in index["datasets"]:
        if entry["name"] == name:
            return f"{base}/{entry['file']}"
    names = sorted(entry["name"] for entry in index["datasets"])
    held = ", ".join(names[:8]) + (f", and {len(names) - 8} more" if len(names) > 8 else "")
    raise ValueError(
        f"the catalogue at {where} has {held or 'nothing in it'}, and no {name!r}"
    )


class Dataset:
    """A file full of rows, in the shape a training loop wants them.

    Usually made by :func:`load`, which opens the file first; hand the
    constructor an open :class:`~xrd.root.ROOTFile` to read a dataset out of
    a file you are already holding.

    Three things are worked out on the way in, and all three can be said
    outright instead:

    *Splits* come from the tree names. A file whose trees are ``train_0`` …
    ``train_9`` and ``test_0`` … ``test_9`` - which is what
    :mod:`xrd.root.datasets` writes - has a ``train`` split and a ``test``
    split, each of ten classes. A file of one tree has one split, ``all``.

    *The answer* is the column called ``label``, ``target``, ``class`` or
    ``y``, whichever the file has.

    *The inputs* are every other column of numbers, less the bookkeeping ones
    (``index``, ``entry``) that say where a row came from.
    """

    def __init__(
        self,
        file: ROOTFile,
        *,
        inputs: Sequence[str] | None = None,
        answer: str | None = None,
        scale: bool = True,
        step: int | None = None,
        owned: bool = False,
    ) -> None:
        self._file = file
        self._owned = owned
        #: What the file is called - a URL, or a path.
        self.name = file.name
        #: Whether byte columns are divided by 255 on the way into a batch.
        self.scale = scale
        grouped = _grouped(file)
        first = file[next(iter(grouped.values()))[0][0]]
        #: Every column that could be read as numbers, by name.
        self.columns = _columns(first)
        #: The column holding the answer, or ``None`` when there is none.
        self.answer = _answer(self.columns, answer, self.name)
        #: The columns holding the question.
        self.inputs = _inputs(self.columns, inputs, self.answer, self.name)
        self._wanted = [*self.inputs, *([self.answer] if self.answer else [])]
        #: Each part of the data, by name: ``train``, ``test``, or ``all``.
        self.splits = {
            name: Split(self, name, [_whole(file[tree], label) for tree, label in trees])
            for name, trees in grouped.items()
        }
        #: How many rows are pooled from each tree at a time. Bigger pools
        #: read less often, shuffle more widely and hold more memory.
        self.step = step or _pool(self.columns, self._wanted, max(len(g) for g in grouped.values()))

    # -- what is in it ------------------------------------------------

    def __len__(self) -> int:
        """How many rows the file holds, over all of its splits."""
        return sum(len(split) for split in self.splits.values())

    def __contains__(self, name: object) -> bool:
        return name in self.splits

    def __getitem__(self, name: str) -> Split:
        try:
            return self.splits[name]
        except KeyError:
            raise KeyError(
                f"{self.name} has no {name!r} rows; it has " + ", ".join(self.splits)
            ) from None

    @property
    def train(self) -> Split:
        """The rows to learn from."""
        return self["train"]

    @property
    def test(self) -> Split:
        """The rows to be scored on."""
        return self["test"]

    @property
    def classes(self) -> list[str]:
        """The classes in the file, or an empty list if it does not say."""
        seen = {label: None for split in self.splits.values() for label in split.classes}
        return sorted(seen, key=_sortable)

    @property
    def default(self) -> Split:
        """The split the shortcuts below use: the training rows, if any."""
        return self.splits.get("train") or next(iter(self.splits.values()))

    # -- looking at it ------------------------------------------------

    def head(self, count: int = 5) -> list[dict[str, Any]]:
        """The first few rows as plain Python, for looking before training."""
        return self.default.head(count)

    def preview(self, count: int = 1) -> str:
        """A row or two drawn in characters. ``print`` it."""
        return self.default.preview(count)

    def batches(self, size: int = 128, **options: Any) -> Iterator[Any]:
        """Minibatches of the training rows; see :meth:`Split.batches`."""
        return self.default.batches(size, **options)

    def loader(self, batch_size: int = 128, **options: Any) -> Any:
        """A PyTorch loader over the training rows; see :meth:`Split.loader`."""
        return self.default.loader(batch_size, **options)

    def __str__(self) -> str:
        classes = f", {len(self.classes)} classes" if self.classes else ""
        lines = [f"{self.name}: {len(self):,} rows{classes}"]
        lines.append(f"  inputs   {self._inputs_line()}")
        if self.answer:
            lines.append(f"  answer   {self._described(self.answer)}")
        splits = ", ".join(f"{name} {len(split):,} rows" for name, split in self.splits.items())
        lines.append(f"  splits   {splits}")
        return "\n".join(lines)

    def _inputs_line(self) -> str:
        """The input columns, named outright or counted when there are many.

        A table of fifty measurements is one line of a summary, not fifty.
        """
        described = [self._described(name) for name in self.inputs]
        if len(described) <= 4:
            return ", ".join(described)
        shown = ", ".join(described[:3])
        return f"{len(described)} columns - {shown}, and {len(described) - 3} more"

    def _described(self, name: str) -> str:
        column = self.columns[name]
        scaled = self.scale and column.typecode == "B" and name != self.answer
        return f"{column}{', scaled to 0-1' if scaled else ''}"

    def __repr__(self) -> str:
        return f"<Dataset {self.name!r} of {len(self):,} rows in {len(self.splits)} splits>"

    # -- putting it down ----------------------------------------------

    def close(self) -> None:
        """Close the file, if this dataset was the one that opened it."""
        if self._owned:
            self._file.close()

    def __enter__(self) -> Dataset:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        """Close a dataset nobody closed, so a script need not remember to."""
        try:
            self.close()
        except Exception:  # pragma: no cover - only reachable at interpreter shutdown
            pass

    # -- making batches -----------------------------------------------

    def _pair(self, torch: Any, batch: dict[str, Any]) -> Any:
        """One batch of tensors as ``(inputs, answers)``, or inputs alone."""
        blocks = []
        for name in self.inputs:
            column = self.columns[name]
            values = batch[name]
            if column.width == 1:
                # A column of single numbers is one feature, so it joins the
                # others as a column of the block rather than as a row of it.
                values = values.reshape(len(values), 1)
            values = values.float()
            if self.scale and column.typecode == "B":
                values = values / 255
            blocks.append(values)
        inputs = blocks[0] if len(blocks) == 1 else torch.cat(blocks, dim=1)
        if self.answer is None:
            return inputs
        # Whole numbers name a class and floats measure something: the loss
        # functions for the two take different types, and this is which.
        answers = batch[self.answer]
        measured = self.columns[self.answer].typecode in "fd"
        return inputs, answers.float() if measured else answers.long()


class Split:
    """One part of a dataset - the training rows, say - as batches.

    Made by :class:`Dataset`; ``data.train``, ``data["test"]`` and
    :meth:`split` are the ways to one.
    """

    def __init__(self, data: Dataset, name: str, parts: Sequence[_Part]) -> None:
        self.data = data
        #: What this part is called: ``train``, ``test``, ``all``.
        self.name = name
        self._parts = list(parts)

    def __len__(self) -> int:
        """How many rows are in it."""
        return sum(part.rows for part in self._parts)

    @property
    def classes(self) -> list[str]:
        """The classes it holds, from the tree names; empty if it does not say."""
        return sorted({part.label for part in self._parts if part.label}, key=_sortable)

    def counts(self) -> dict[Any, int]:
        """How many rows of each class, so an imbalance is seen before training.

        Free when the file keeps a tree per class, which is how
        :mod:`xrd.root.datasets` writes one. Otherwise it is a read of the
        answer column and nothing else - the pictures stay on the server.
        """
        if self.classes:
            counts: dict[Any, int] = {}
            for part in self._parts:
                counts[part.label] = counts.get(part.label, 0) + part.rows
            return dict(sorted(counts.items(), key=lambda item: _sortable(item[0])))
        if self.data.answer is None:
            raise ValueError(
                f"{self.data.name} says nothing about classes: its trees are not named for "
                f"one and it has no {' or '.join(ANSWERS)} column to count"
            )
        tally: dict[Any, int] = {}
        for part in self._parts:
            for batch in part.tree.iterate(
                [self.data.answer],
                step=self.data.step,
                entry_start=part.start,
                entry_stop=part.stop,
            ):
                for value in batch[self.data.answer]:
                    tally[value] = tally.get(value, 0) + 1
        return dict(sorted(tally.items(), key=lambda item: _sortable(item[0])))

    def split(self, fraction: float = 0.8) -> tuple[Split, Split]:
        """Cut this in two - a training part and a held-back part.

            >>> train, valid = data.train.split(0.9)               # doctest: +SKIP

        The cut is made in every tree, so both halves hold every class in the
        same proportion, and neither reads the other's rows.
        """
        if not 0 < fraction < 1:
            raise ValueError(
                f"a fraction cuts the rows in two, so it lies between 0 and 1: "
                f"{fraction} would leave one side empty"
            )
        first: list[_Part] = []
        second: list[_Part] = []
        for part in self._parts:
            cut = part.start + round(part.rows * fraction)
            first.append(dataclasses.replace(part, stop=cut))
            second.append(dataclasses.replace(part, start=cut))
        return (
            Split(self.data, f"{self.name} (first {fraction:.0%})", first),
            Split(self.data, f"{self.name} (last {1 - fraction:.0%})", second),
        )

    # -- looking at it ------------------------------------------------

    def head(self, count: int = 5) -> list[dict[str, Any]]:
        """The first ``count`` rows as plain Python: lists, ints and floats.

        No framework anywhere near it, which is what makes this the thing to
        print when a file is new and the question is what is actually in it.
        """
        rows: list[dict[str, Any]] = []
        for part in self._parts:
            take = min(count - len(rows), part.rows)
            if take <= 0:
                break
            columns = part.tree.arrays(self.data._wanted, part.start, part.start + take)
            for at in range(take):
                rows.append(
                    {
                        name: _row(values, at, self.data.columns[name].width)
                        for name, values in columns.items()
                    }
                )
        return rows

    def preview(self, count: int = 1) -> str:
        """The first rows drawn in characters, pictures and all.

            >>> print(data.train.preview())                        # doctest: +SKIP
            label 5
                    .:=*#*.
                 :*########+
        """
        picture = next(
            (name for name in self.data.inputs if self.data.columns[name].is_picture), None
        )
        drawn = []
        for row in self.head(count):
            caption = f"{self.data.answer} {row[self.data.answer]}" if self.data.answer else ""
            if picture is None:
                values = ", ".join(f"{name} {row[name]}" for name in self.data.inputs)
                drawn.append(f"{caption}: {values}" if caption else values)
                continue
            drawn.append("\n".join([caption, *_drawn(row[picture])]).lstrip("\n"))
        return "\n\n".join(drawn)

    # -- training on it -----------------------------------------------

    def dataset(self, batch_size: int = 128, *, shuffle: bool = True, device: Any = None) -> Any:
        """A PyTorch ``IterableDataset`` of this split, batches already made.

        Wanted only to hand to a ``DataLoader`` yourself, with workers or a
        sampler of your own; :meth:`loader` and :meth:`batches` are the short
        ways to the same rows. Use ``batch_size=None`` on the loader: the
        batches are made here, out of a pool read from every class at once.
        """
        torch = _torch()
        inner = mixed(
            [part.tree for part in self._parts],
            self.data._wanted,
            step=self.data.step,
            batch=batch_size,
            shuffle=shuffle,
            device=device,
            spans=[(part.start, part.stop) for part in self._parts],
        )
        data = self.data

        class Batches(torch.utils.data.IterableDataset):  # type: ignore[misc, name-defined]
            """Minibatches of one split, each ``(inputs, answers)``."""

            def __iter__(self) -> Iterator[Any]:
                return (data._pair(torch, batch) for batch in inner)

            def __len__(self) -> int:
                return len(inner)

        return Batches()

    def batches(
        self, size: int = 128, *, shuffle: bool = True, device: Any = None
    ) -> Iterator[Any]:
        """Minibatches, straight to a ``for`` loop.

            >>> for images, labels in data.train.batches(256):     # doctest: +SKIP
            ...     ...

        Each is a pair of tensors - the inputs and the answers - or just the
        inputs when the file has no answer column. ``shuffle=False`` for the
        pass that scores a model, where the order changes nothing and being
        able to line the rows up against the file helps.
        """
        return iter(self.dataset(size, shuffle=shuffle, device=device))

    def loader(
        self,
        batch_size: int = 128,
        *,
        shuffle: bool = True,
        workers: int = 0,
        device: Any = None,
    ) -> Any:
        """A ``torch.utils.data.DataLoader`` over this split.

        The same batches as :meth:`batches`, wrapped in what the rest of the
        PyTorch world expects to be handed. ``workers`` reads with that many
        processes, each taking its own share of every tree.
        """
        torch = _torch()
        return torch.utils.data.DataLoader(
            self.dataset(batch_size, shuffle=shuffle, device=device),
            batch_size=None,
            num_workers=workers,
        )

    def __str__(self) -> str:
        classes = f", {len(self.classes)} classes" if self.classes else ""
        return f"{self.name}: {len(self):,} rows in {len(self._parts)} trees{classes}"

    def __repr__(self) -> str:
        return f"<Split {self.name!r} of {len(self):,} rows>"


# ---------------------------------------------------------------------------
# Working out what a file holds
# ---------------------------------------------------------------------------


def _grouped(file: ROOTFile) -> dict[str, list[tuple[str, str]]]:
    """Which trees make up which split, and what class each of them is.

    ``train_7`` is the training rows of class 7; ``Events`` is a file that
    knows nothing of splits, and all of it is one.
    """
    trees = file.trees()
    if not trees:
        raise ValueError(
            f"{file.name} holds no trees, so there are no rows to train on; "
            f"it holds " + (", ".join(file.keys()) or "nothing")
        )
    grouped: dict[str, list[tuple[str, str]]] = {}
    for name in trees:
        head, _, label = name.partition("_")
        split = head.lower() if head.lower() in SPLITS else "all"
        grouped.setdefault(split, []).append((name, label if split != "all" else ""))
    order = {name: at for at, name in enumerate(SPLITS)}
    return {name: grouped[name] for name in sorted(grouped, key=lambda s: (order.get(s, 99), s))}


def _columns(tree: TTree) -> dict[str, Column]:
    """Every column of ``tree`` that holds numbers, which is what a tensor is."""
    columns = {}
    for name in numeric(tree):
        branch = tree[name]
        columns[name] = Column(
            name=name,
            typename=branch.typename or "",
            # ``numeric`` kept only the columns whose values are numbers, and
            # numbers are the only columns that state an ``array`` type code.
            typecode=cast(Numeric, branch.column).typecode,
            width=0 if branch.is_jagged else branch.length,
        )
    if not columns:
        raise ValueError(
            f"{tree.name} has no columns of numbers to learn from; it has "
            + ", ".join(f"{name} ({kind})" for name, kind in tree.typenames().items())
        )
    return columns


def _answer(columns: dict[str, Column], named: str | None, where: str) -> str | None:
    """The column holding what a model is to predict, if there is one."""
    if named is not None:
        if named not in columns:
            raise ValueError(_no_column(named, columns, where))
        return named
    lowered = {name.lower(): name for name in columns}
    return next((lowered[name] for name in ANSWERS if name in lowered), None)


def _inputs(
    columns: dict[str, Column], named: Sequence[str] | None, answer: str | None, where: str
) -> list[str]:
    """The columns to learn from: what is asked for, or everything else."""
    if named is not None:
        missing = [name for name in named if name not in columns]
        if missing:
            raise ValueError(_no_column(missing[0], columns, where))
        return list(named)
    inputs = [name for name in columns if name != answer and name.lower() not in BOOKKEEPING]
    if not inputs:
        raise ValueError(
            f"every column of {where} is either the answer or bookkeeping, so there is "
            f"nothing to learn from; name the inputs yourself with inputs=[...]"
        )
    return inputs


def _no_column(name: str, columns: dict[str, Column], where: str) -> str:
    return f"{where} has no column of numbers called {name!r}; it has " + ", ".join(columns)


def _whole(tree: TTree, label: str) -> _Part:
    """All of a tree's rows."""
    return _Part(tree=tree, start=0, stop=len(tree), label=label)


def _pool(columns: dict[str, Column], wanted: Sequence[str], trees: int) -> int:
    """How many rows to read from each tree at a time, for a pool that fits.

    The pool is one read from every tree of the split at once, so the memory
    it costs is the row multiplied by both numbers; this is that arithmetic
    turned around, bounded so that a tiny row does not ask for a million and a
    huge one still reads more than a handful.
    """
    row = sum(max(1, columns[name].width) * columns[name].itemsize for name in wanted)
    return max(256, min(POOL_BYTES // (row * trees), 16_384))


def _sortable(label: Any) -> tuple[int, Any]:
    """Class names in the order a person reads them: 2 before 10, a before b."""
    text = str(label)
    return (0, int(text)) if text.isdigit() else (1, text)


def _row(values: Any, at: int, width: int) -> Any:
    """One row's worth of a column, as plain Python."""
    if not width:  # jagged: the column knows where each row starts
        return list(values[at])
    if width == 1:
        return values[at]
    return values[at * width : (at + 1) * width].tolist()


def _drawn(pixels: Sequence[int]) -> list[str]:
    """A square of bytes as lines of characters, darkest last."""
    side = math.isqrt(len(pixels))
    return [
        "".join(SHADES[pixel * (len(SHADES) - 1) // 255] for pixel in pixels[row : row + side])
        for row in range(0, side * side, side)
    ]
