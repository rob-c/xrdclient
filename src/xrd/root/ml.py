"""From a tree on a storage element to tensors in a training loop.

    >>> import torch, xrd.root, xrd.root.ml                    # doctest: +SKIP
    >>> tree = xrd.root.open_root("root://eos.example.org//store/events.root").tree()
    >>> loader = torch.utils.data.DataLoader(xrd.root.ml.dataset(tree), batch_size=None)
    >>> for batch in loader:
    ...     model(batch["pt"])

TensorFlow is the same tree through :func:`tf_dataset`:

    >>> for batch in xrd.root.ml.tf_dataset(tree):             # doctest: +SKIP
    ...     model(batch["pt"])

Neither framework is imported until it is called, so :mod:`xrd.root` costs
nothing on a machine that has neither, and the batches come off the wire a
basket at a time - the file never has to fit anywhere.
"""

from __future__ import annotations

import array
import sys
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

from .._compat import zip_strict
from .errors import UnsupportedFeatureError
from .interp import Numeric
from .tree import DEFAULT_STEP, Jagged

if TYPE_CHECKING:
    from .tree import Branch, TTree

__all__ = [
    "to_tensor",
    "to_tensors",
    "iter_tensors",
    "dataset",
    "mixed",
    "to_tf_tensor",
    "to_tf_tensors",
    "tf_dataset",
]

_LITTLE = sys.byteorder == "little"

#: Which tensor type each :mod:`array` code becomes, and what to widen it to
#: first. The unsigned types beyond a byte are too thinly supported by either
#: framework to hand a beginner, so those are widened into a signed type that
#: holds them.
DTYPES = {
    "b": ("int8", None),
    "B": ("uint8", None),
    "h": ("int16", None),
    "H": ("int32", "i"),
    "i": ("int32", None),
    "I": ("int64", "q"),
    "q": ("int64", None),
    "Q": ("int64", "q"),
    "f": ("float32", None),
    "d": ("float64", None),
}


def _torch() -> Any:
    try:
        import torch  # type: ignore[import-not-found]
    except ImportError:
        raise UnsupportedFeatureError(
            "reading the tree worked, but turning it into tensors needs PyTorch: "
            "pip install torch, or use the arrays as they come (tree.arrays())"
        ) from None
    return torch


def _tensorflow() -> Any:
    try:
        import tensorflow
    except ImportError:
        raise UnsupportedFeatureError(
            "reading the tree worked, but turning it into a tf.data.Dataset needs "
            "TensorFlow: pip install tensorflow, or use the arrays as they come "
            "(tree.arrays())"
        ) from None
    return tensorflow


def _numbers(values: Any, width: int | None, fill: float) -> tuple[array.array[Any], str, Any]:
    """One column flattened, named and squared off, whichever framework wants it.

    This is the part that is the same for both: a jagged column is padded to a
    rectangle, a column that is not numbers at all is refused here rather than
    deep inside a framework, and the unsigned types are widened.
    """
    if isinstance(values, Jagged):
        values, width = values.padded(width, fill)
    if not isinstance(values, array.array):
        raise UnsupportedFeatureError(
            f"a column of {type(values).__name__} is not a tensor; strings and objects have to "
            f"be turned into numbers first, which only you know how to do"
        )

    name, widen = DTYPES[values.typecode]
    if widen is not None:
        try:
            values = array.array(widen, values)
        except OverflowError:
            raise UnsupportedFeatureError(
                f"a value in this column is too large for {name}, which is the widest signed "
                f"type there is; read it with .array() and scale it yourself"
            ) from None
    return values, name, width


def to_tensor(
    values: Any,
    *,
    width: int | None = None,
    fill: float = 0.0,
    device: Any = None,
) -> Any:
    """One column as a torch tensor.

    A fixed column of single values gives a 1-D tensor; ``width`` reshapes a
    fixed-size array column into rows. A :class:`~xrd.root.Jagged` column is
    padded to a rectangle - to its longest row unless ``width`` says how wide.
    """
    torch = _torch()
    values, name, width = _numbers(values, width, fill)
    tensor = torch.frombuffer(values, dtype=getattr(torch, name))
    if width is not None:
        tensor = tensor.reshape(len(tensor) // width if width else 0, width)
    return tensor.to(device) if device is not None else tensor


def to_tf_tensor(values: Any, *, width: int | None = None, fill: float = 0.0) -> Any:
    """One column as a TensorFlow tensor, shaped the same way as for torch.

    The bytes go across whole rather than one Python number at a time, which
    is what ``tf.io.decode_raw`` is for and why this needs no NumPy of its own.
    """
    tf = _tensorflow()
    values, name, width = _numbers(values, width, fill)
    tensor = tf.io.decode_raw(
        tf.constant(values.tobytes()), getattr(tf, name), little_endian=_LITTLE
    )
    if width is not None:
        tensor = tf.reshape(tensor, (len(values) // width if width else 0, width))
    return tensor


def to_tensors(
    batch: dict[str, Any],
    *,
    widths: dict[str, int] | None = None,
    device: Any = None,
) -> dict[str, Any]:
    """A whole batch of columns, each one a torch tensor."""
    widths = widths or {}
    return {
        name: to_tensor(values, width=widths.get(name), device=device)
        for name, values in batch.items()
    }


def to_tf_tensors(
    batch: dict[str, Any],
    *,
    widths: dict[str, int] | None = None,
    fill: float = 0.0,
) -> dict[str, Any]:
    """A whole batch of columns, each one a TensorFlow tensor."""
    widths = widths or {}
    return {
        name: to_tf_tensor(values, width=widths.get(name), fill=fill)
        for name, values in batch.items()
    }


def numeric(tree: TTree) -> list[str]:
    """The columns of a tree that can become tensors: the ones that are numbers.

    A column of strings, lists or maps is left out rather than left in to fail
    later, which is what makes calling either dataset with no names at all a
    reasonable thing for a beginner to do.
    """
    return [name for name in tree.readable() if isinstance(tree[name].column, Numeric)]


def iter_tensors(
    tree: TTree,
    names: Sequence[str] | None = None,
    *,
    step: int = DEFAULT_STEP,
    entry_start: int = 0,
    entry_stop: int | None = None,
    device: Any = None,
) -> Iterator[dict[str, Any]]:
    """Walk a tree in batches, each one a dictionary of tensors.

    With no names it takes every numeric column, since a string is not a
    tensor and guessing an encoding for one would be worse than leaving it.
    Fixed-size array columns come out as ``(entries, width)``, and variable
    ones are padded to the widest row in that batch.
    """
    wanted = numeric(tree) if names is None else list(names)
    widths = {name: tree[name].length for name in wanted if tree[name].length > 1}
    for batch in tree.iterate(wanted, step=step, entry_start=entry_start, entry_stop=entry_stop):
        yield to_tensors(batch, widths=widths, device=device)


def _shape(branch: Branch) -> tuple[int | None, ...]:
    """What a batch of one column looks like, before any of it is read."""
    if branch.is_jagged:
        return (None, None)  # padded to the longest row in each batch
    return (None, branch.length) if branch.length > 1 else (None,)


def _dtype(tf: Any, branch: Branch) -> Any:
    """What a column's values will be, said in TensorFlow's words."""
    column = branch.column
    if not isinstance(column, Numeric):
        raise UnsupportedFeatureError(
            f"{branch.name!r} holds {branch.typename}, which is not numbers and so has no "
            f"tensor shape; leave it out of the names, or make numbers of it yourself"
        )
    return getattr(tf, DTYPES[column.typecode][0])


def tf_dataset(
    tree: TTree,
    names: Sequence[str] | None = None,
    *,
    step: int = DEFAULT_STEP,
    fill: float = 0.0,
) -> Any:
    """A ``tf.data.Dataset`` over a tree, batches already made.

        >>> for batch in tf_dataset(tree, step=4096):          # doctest: +SKIP
        ...     model(batch["pt"])

    Each item is a dictionary of tensors holding ``step`` entries, so one
    basket read serves thousands of rows and nothing rebatches behind your
    back. The shapes and types are declared up front, from what the file says
    the columns are, so ``.prefetch`` and the rest of ``tf.data`` work on it
    without a first pass over the data.
    """
    tf = _tensorflow()
    wanted = numeric(tree) if names is None else list(names)
    widths = {name: tree[name].length for name in wanted if tree[name].length > 1}
    signature = {name: tf.TensorSpec(shape=_shape(tree[name]), dtype=_dtype(tf, tree[name]))
                 for name in wanted}

    def batches() -> Iterator[dict[str, Any]]:
        for batch in tree.iterate(wanted, step=step):
            yield to_tf_tensors(batch, widths=widths, fill=fill)

    return tf.data.Dataset.from_generator(batches, output_signature=signature)


def dataset(
    tree: TTree,
    names: Sequence[str] | None = None,
    *,
    step: int = DEFAULT_STEP,
    device: Any = None,
) -> Any:
    """A PyTorch ``IterableDataset`` over a tree, batches already made.

        >>> loader = DataLoader(dataset(tree, step=4096), batch_size=None)  # doctest: +SKIP

    Use it with ``batch_size=None``: each item is already a batch of ``step``
    entries, which is the point - one basket read serves thousands of rows.
    Several worker processes split the entries between them, so each reads its
    own share of the file rather than all of them reading all of it.
    """
    torch = _torch()

    class TreeDataset(torch.utils.data.IterableDataset):  # type: ignore[misc, name-defined]
        """Entries of one tree, in batches, over whatever it was opened on."""

        def __iter__(self) -> Iterator[dict[str, Any]]:
            start, stop = _share(torch, 0, len(tree))
            return iter_tensors(
                tree, names, step=step, entry_start=start, entry_stop=stop, device=device
            )

        def __len__(self) -> int:
            return -(-len(tree) // step)

    return TreeDataset()


def _share(torch: Any, start: int, stop: int) -> tuple[int, int]:
    """Which slice of the entries from ``start`` to ``stop`` this process reads.

    All of them outside a worker, and an equal share of them inside one, so
    that several workers read a file between them rather than each reading all
    of it.
    """
    worker = torch.utils.data.get_worker_info()
    if worker is None:
        return start, stop
    share = -(-(stop - start) // worker.num_workers)
    first = min(start + worker.id * share, stop)
    return first, min(first + share, stop)


def _cut(
    torch: Any, pool: list[dict[str, Any]], batch: int | None, shuffle: bool, device: Any
) -> Iterator[dict[str, Any]]:
    """One read from each tree, put together and handed out a batch at a time.

    A pool of one is left as it is rather than copied through ``cat``, which is
    what the last rounds are once the shorter trees have run out.
    """
    if len(pool) == 1:
        columns = pool[0]
    else:
        columns = {name: torch.cat([chunk[name] for chunk in pool]) for name in pool[0]}
    rows = len(next(iter(columns.values())))
    if shuffle:
        order = torch.randperm(rows, device=device)
        columns = {name: values[order] for name, values in columns.items()}
    size = rows if batch is None else batch
    for at in range(0, rows, size):
        yield {name: values[at : at + size] for name, values in columns.items()}


def mixed(
    trees: Sequence[TTree],
    names: Sequence[str] | None = None,
    *,
    step: int = DEFAULT_STEP,
    batch: int | None = None,
    shuffle: bool = True,
    device: Any = None,
    spans: Sequence[tuple[int, int]] | None = None,
) -> Any:
    """A PyTorch ``IterableDataset`` over several trees at once, rows mixed.

        >>> loader = DataLoader(mixed(trees, batch=256), batch_size=None)  # doctest: +SKIP

    The sets in :mod:`xrd.root.datasets` keep one tree per class, which is the
    wrong order to learn in: a loop over the trees in turn shows a model five
    thousand cats and then five thousand dogs. This reads ``step`` entries from
    each tree, shuffles that pool together and cuts ``batch`` rows off it at a
    time, so every minibatch holds every class while the file is still read a
    basket at a time and only a pool of it is ever in memory.

    ``spans`` reads only part of each tree - one ``(first, last)`` pair per
    tree, as :func:`range` bounds them - which is how a training set and a
    validation set are cut out of the same file without reading either twice.

    ``batch`` left out hands the whole pool over at once. The shuffling is
    PyTorch's own, so :func:`torch.manual_seed` settles what it does; pass
    ``shuffle=False`` for the pass that scores a model, where the order of the
    rows makes no difference and their arrival can be watched.

    Columns of a fixed width only: a jagged one is padded to the widest row of
    each tree's own read, and the pool would be putting different widths side
    by side.
    """
    torch = _torch()
    trees = list(trees)
    if not trees:
        raise ValueError("mixed() needs a tree to read: it was given none")
    ranges = [(0, len(tree)) for tree in trees] if spans is None else [(s[0], s[1]) for s in spans]
    if len(ranges) != len(trees):
        raise ValueError(
            f"mixed() was given {len(trees)} trees and {len(ranges)} spans; "
            f"a span says which entries of one tree to read, so there is one apiece"
        )

    class MixedDataset(torch.utils.data.IterableDataset):  # type: ignore[misc, name-defined]
        """Entries of several trees, pooled and shuffled, in batches."""

        def __iter__(self) -> Iterator[dict[str, Any]]:
            left = [
                iter_tensors(
                    tree, names, step=step, entry_start=start, entry_stop=stop, device=device
                )
                for tree, (start, stop) in (
                    (tree, _share(torch, *span)) for tree, span in zip_strict(trees, ranges)
                )
            ]
            while left:
                pool = []
                for stream in list(left):
                    chunk = next(stream, None)
                    if chunk is None:
                        left.remove(stream)
                    else:
                        pool.append(chunk)
                if pool:
                    yield from _cut(torch, pool, batch, shuffle, device)

        def __len__(self) -> int:
            left, batches = [stop - start for start, stop in ranges], 0
            while any(left):
                rows = sum(min(step, entries) for entries in left)
                batches += 1 if batch is None else -(-rows // batch)
                left = [max(0, entries - step) for entries in left]
            return batches

    return MixedDataset()
