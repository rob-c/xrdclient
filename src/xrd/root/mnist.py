"""MNIST as a ROOT file: one tree per digit, images and labels inside.

The handwritten digits are the worked example this library's machine learning
side is documented against: small enough to convert in a few seconds, big
enough that reading it entry by entry would be the wrong idea, and shaped like
real data - a fixed-size array per entry, a label beside it.

The images come out separated by class, one tree per digit, because that is
the shape most training loops want: sample a class, read a range of that
class's entries, and the file gives them back in one read rather than ten
thousand. Every tree carries the label anyway, so concatenating them all and
shuffling works exactly as well.

Nothing here needs the dataset to be on disk first - the IDX files are read
through this library like any other URL, so converting straight from the
mirror into a file on an XRootD server is one call.

MNIST keeps its own module because it is the one everybody starts with.
:mod:`xrd.root.datasets` does the work, and holds the wider catalogue.
"""

from __future__ import annotations

from typing import Any

from .datasets import IDX_FILES as FILES
from .datasets import IDX_TYPES, IMAGE_BASKET, MNIST_MIRROR, fetch, read_idx
from .datasets import MNIST_MIRROR as MIRROR
from .datasets import convert as _convert

__all__ = [
    "COLUMNS", "FILES", "IDX_TYPES", "IMAGE_BASKET", "MIRROR", "PIXELS", "SIDE",
    "convert", "fetch", "read_idx",
]

#: One MNIST image is 28 by 28 greyscale pixels, one byte apiece.
SIDE = 28
PIXELS = SIDE * SIDE

#: What each entry holds: the image flat, the digit it is, and where it came
#: from in the original file, so a row can always be traced back.
COLUMNS: dict[str, Any] = {"image": ("B", PIXELS), "label": "i", "index": "i"}


def convert(
    target: Any,
    *,
    split: str = "train",
    images: Any = None,
    labels: Any = None,
    base: str = MNIST_MIRROR,
    prefix: str | None = None,
    compression: str | None = "zlib",
    basket_size: int = IMAGE_BASKET,
    config: Any = None,
) -> dict[str, int]:
    """Write MNIST into a ROOT file, one tree per digit; say what went where.

        >>> from xrd.root import mnist                       # doctest: +SKIP
        >>> mnist.convert("mnist.root")                      # doctest: +SKIP
        {'train_0': 5923, 'train_1': 6742, ...}

    ``target`` is where the file goes - a path, a URL, an open binary file,
    or a :class:`~.writer.WritableFile` already open, which is how both
    splits end up in one file. ``images`` and ``labels`` default to the
    ``split`` downloaded from ``base``, and take raw IDX bytes, a path or a
    URL when the dataset is already to hand.

    Each tree is named for its split and digit - ``train_7`` - and holds the
    image flat as 784 bytes, the label, and the entry's place in the original
    file. Reading it back needs nothing but this library.
    """
    supplied = {
        role: source
        for role, source in (("images", images), ("labels", labels))
        if source is not None
    }
    return _convert(
        "mnist",
        target,
        split=split,
        parts=supplied,
        base=base,
        prefix=prefix,
        compression=compression,
        basket_size=basket_size,
        config=config,
    )
