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
import hashlib
import json
import math
import os
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Union, cast

from .root import open_root
from .root.errors import UnsupportedFeatureError
from .root.interp import Numeric

# ``_torch`` is the import of PyTorch with the refusal that says what to
# install; there is one of those in this library and this module wants it too.
from .root.ml import _torch, mixed, numeric
from .url import parse

if TYPE_CHECKING:
    from .config import Config
    from .root.file import ROOTFile
    from .root.tree import TTree

__all__ = [
    "load",
    "download",
    "load_image_2d",
    "visualize_2d",
    "Image2D",
    "Normalization",
    "Dataset",
    "Split",
    "Column",
]

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

#: The order in which the visual materials converters flatten their three
#: orthogonal projections into the ``projection`` branch.
IMAGE_PLANES = ("xy", "xz", "yz")

#: Ways an :class:`Image2D` can turn its stored values into model inputs.
Normalization = Literal["max", "minmax", "none"]

_Pixel = Union[int, float]
_Pixels = tuple[tuple[_Pixel, ...], ...]
_NormalizedPixels = tuple[tuple[float, ...], ...]


@dataclasses.dataclass(frozen=True)
class Image2D:
    """One ROOT image in both its stored and model-ready forms.

    ``raw`` is the two-dimensional matrix exactly as stored in its branch.
    ``normalized`` is a floating-point matrix made according to
    :attr:`normalization`. For the JARVIS projections, a raw zero is empty
    space and a nonzero pixel is the largest atomic number occupying that
    fractional-coordinate cell.

    Instances come from :func:`load_image_2d` (or its descriptive alias,
    :func:`visualize_2d`). The matrices are immutable nested tuples of ordinary
    Python numbers, so inspection and serialization require no NumPy,
    Matplotlib, or training framework. Conversion and presentation helpers
    import their optional dependencies only when called.
    """

    dataset: str
    tree: str
    entry: int
    plane: str | None
    jid: str
    formula: str
    target: float | None
    raw: _Pixels = dataclasses.field(repr=False)
    normalized: _NormalizedPixels = dataclasses.field(repr=False)
    branch: str = "projection"
    normalization: Normalization = "max"

    @property
    def shape(self) -> tuple[int, int]:
        """The image height and width."""
        return len(self.raw), len(self.raw[0]) if self.raw else 0

    @property
    def height(self) -> int:
        """The number of pixel rows."""
        return self.shape[0]

    @property
    def width(self) -> int:
        """The number of pixels in each row."""
        return self.shape[1]

    @property
    def raw_min(self) -> _Pixel:
        """The smallest stored pixel value, or zero for an empty image."""
        return min((min(row, default=0) for row in self.raw), default=0)

    @property
    def raw_max(self) -> _Pixel:
        """The largest stored pixel value, or zero for an empty image."""
        return max((max(row, default=0) for row in self.raw), default=0)

    @property
    def metadata(self) -> dict[str, str | int | float | None]:
        """Traceable identifiers and the exact ROOT location of this image."""
        return {
            "dataset": self.dataset,
            "tree": self.tree,
            "entry": self.entry,
            "branch": self.branch,
            "plane": self.plane,
            "jid": self.jid,
            "formula": self.formula,
            "target": self.target,
            "normalization": self.normalization,
        }

    def values(self, *, normalized: bool = True) -> _Pixels | _NormalizedPixels:
        """Return the normalized matrix by default, or the stored values."""
        return self.normalized if normalized else self.raw

    def flat(self, *, normalized: bool = True) -> tuple[_Pixel, ...]:
        """Return a row-major flat tuple, ready for a non-image model input."""
        return tuple(value for row in self.values(normalized=normalized) for value in row)

    def with_normalization(self, normalization: Normalization) -> Image2D:
        """Return the same raw image normalized a different way."""
        normalized = _normalizer(normalization)(self.raw)
        return dataclasses.replace(
            self,
            normalized=normalized,
            normalization=normalization,
        )

    def to_numpy(self, *, normalized: bool = True, dtype: Any = None) -> Any:
        """Return a two-dimensional NumPy array.

        Normalized values default to ``float32``; raw values retain NumPy's
        inferred integer or floating-point type unless ``dtype`` is supplied.
        NumPy remains optional and is imported only for this call.
        """
        numpy = _numpy()
        wanted = dtype if dtype is not None else ("float32" if normalized else None)
        return numpy.asarray(self.values(normalized=normalized), dtype=wanted)

    def to_tensor(
        self,
        *,
        normalized: bool = True,
        channel: bool = True,
        dtype: Any = None,
        device: Any = None,
    ) -> Any:
        """Return a PyTorch tensor, optionally with a leading channel axis.

        The default shape is ``(1, height, width)``, ready for a convolutional
        model. ``channel=False`` returns ``(height, width)``. Normalized values
        default to PyTorch ``float32``; a raw conversion lets PyTorch infer its
        type unless ``dtype`` is supplied.
        """
        torch = _torch()
        wanted = dtype if dtype is not None else (torch.float32 if normalized else None)
        options = {"device": device}
        if wanted is not None:
            options["dtype"] = wanted
        tensor = torch.tensor(self.values(normalized=normalized), **options)
        return tensor.unsqueeze(0) if channel else tensor

    def plot(
        self,
        *,
        axes: Any = None,
        cmap: str = "magma",
        origin: Literal["lower", "upper"] = "lower",
        interpolation: str = "nearest",
        colorbar: bool = True,
        title: str | None = None,
    ) -> tuple[Any, Any]:
        """Draw the raw and normalized images side by side with matplotlib.

        Bring a pair of ``axes`` to embed the comparison in an existing
        figure, or omit it to create one. The returned ``(figure, axes)`` can
        be further styled or displayed. ``title=""`` suppresses the automatic
        dataset title. Install the optional dependency with
        ``pip install pyxrootdclient[plot]``.
        """
        pyplot = _pyplot()
        figure, selected = _image_axes(pyplot, axes)
        _draw_image(
            figure,
            selected[0],
            self.raw,
            "Raw values",
            cmap,
            origin,
            interpolation,
            colorbar,
        )
        _draw_image(
            figure,
            selected[1],
            self.normalized,
            f"Normalized ({self.normalization})",
            cmap,
            origin,
            interpolation,
            colorbar,
        )
        caption = self._title() if title is None else title
        if caption:
            figure.suptitle(caption)
        return figure, selected

    def save(
        self,
        destination: str | os.PathLike[str],
        *,
        dpi: int = 160,
        **plot_options: Any,
    ) -> Path:
        """Plot to ``destination`` and return its path.

        A figure created by this call is closed after writing. If ``axes=``
        embeds the plot in a caller-owned figure, that figure is left open.
        """
        if type(dpi) is not int or dpi <= 0:
            raise ValueError(f"dpi must be a positive integer, not {dpi!r}")
        pyplot = _pyplot()
        private = plot_options.get("axes") is None
        figure, _axes = self.plot(**plot_options)
        try:
            figure.savefig(destination, dpi=dpi)
        finally:
            if private:
                pyplot.close(figure)
        return Path(destination)

    def _title(self) -> str:
        """A concise title retaining both human and source identity."""
        identity = self.formula or self.jid or Path(self.dataset).name
        plane = f" · {self.plane}" if self.plane is not None else ""
        return f"{identity} · {self.tree}[{self.entry}] · {self.branch}{plane}"

    def __repr__(self) -> str:
        """Describe the image without printing thousands of pixels."""
        plane = f", plane={self.plane!r}" if self.plane is not None else ""
        return (
            f"<Image2D {self.width}x{self.height} from {self.tree!r}/{self.branch!r}"
            f" entry={self.entry}{plane}, normalization={self.normalization!r}>"
        )


def load_image_2d(
    source: Any,
    *,
    tree: str = "train",
    entry: int = 0,
    branch: str = "projection",
    plane: str | int | None = "xy",
    shape: tuple[int, int] | None = None,
    planes: Sequence[str] | None = IMAGE_PLANES,
    normalization: Normalization = "max",
    config: Config | None = None,
) -> Image2D:
    """Read one 2D image without downloading the whole ROOT dataset.

        >>> image = load_image_2d("jarvis_dft2d_formation_energy")  # doctest: +SKIP
        >>> image.shape, image.raw_max                              # doctest: +SKIP
        ((32, 32), 83)
        >>> image.to_tensor().shape                                # doctest: +SKIP
        torch.Size([1, 32, 32])

    ``source`` accepts the same local path, remote URL, open binary file, or
    catalogue name as :func:`load`. ``tree``, ``entry`` and ``branch`` select
    the stored values; negative entries count from the end. Only that row and
    its short metadata columns are read, so this remains a small range read
    against a hosted ``.root`` file.

    The defaults understand every ``jarvis_dft2d_*`` file: ``projection`` is
    three square images named ``xy``, ``xz`` and ``yz``. For an ordinary
    single-image branch, pass ``planes=None`` and ``plane=None``; a square is
    inferred, or ``shape=(height, width)`` describes a rectangle. A layered
    custom branch can provide names through ``planes`` and select one by name
    or zero-based integer through ``plane``.

    ``normalization="max"`` divides by the largest absolute value,
    ``"minmax"`` maps the stored minimum and maximum to zero and one, and
    ``"none"`` makes an unchanged floating-point copy. The raw matrix is
    always retained.
    """
    normalize = _normalizer(normalization)
    names = _plane_names(planes)
    if isinstance(source, str) and _is_name(source):
        source = _from_catalogue(source, config)[0]
    with open_root(source, config=config) as handle:
        selected, actual = _selected_entry(handle, tree, entry)
        raw, selected_plane = _image_rows(
            selected,
            actual,
            branch,
            plane,
            shape,
            names,
        )
        return Image2D(
            dataset=handle.name,
            tree=tree,
            entry=actual,
            plane=selected_plane,
            jid=_text_value(selected, "jid", actual),
            formula=_text_value(selected, "formula", actual),
            target=_number_value(selected, "target", actual),
            raw=raw,
            normalized=normalize(raw),
            branch=branch,
            normalization=normalization,
        )


def visualize_2d(
    source: Any,
    *,
    tree: str = "train",
    entry: int = 0,
    branch: str = "projection",
    plane: str | int | None = "xy",
    shape: tuple[int, int] | None = None,
    planes: Sequence[str] | None = IMAGE_PLANES,
    normalization: Normalization = "max",
    config: Config | None = None,
) -> Image2D:
    """Alias for :func:`load_image_2d`, retained for visualization-first code."""
    return load_image_2d(
        source,
        tree=tree,
        entry=entry,
        branch=branch,
        plane=plane,
        shape=shape,
        planes=planes,
        normalization=normalization,
        config=config,
    )


def _selected_entry(file: ROOTFile, name: str, entry: int) -> tuple[TTree, int]:
    """Find one tree entry and turn a negative index into its real position."""
    try:
        tree = cast("TTree", file[name])
    except KeyError:
        raise KeyError(
            f"{file.name} has no {name!r} tree; it has {', '.join(file.keys())}"
        ) from None
    actual = len(tree) + entry if entry < 0 else entry
    if not 0 <= actual < len(tree):
        raise IndexError(f"entry {entry} is outside {name!r}, which has {len(tree)} entries")
    return tree, actual


def _image_rows(
    tree: TTree,
    entry: int,
    name: str,
    plane: str | int | None,
    shape: tuple[int, int] | None,
    planes: tuple[str, ...],
) -> tuple[_Pixels, str | None]:
    """Read and reshape one layer of one fixed-size image branch."""
    if name not in tree:
        raise KeyError(f"{tree.name!r} has no {name!r} branch; it has {', '.join(tree.keys())}")
    branch = tree[name]
    if not isinstance(branch.column, Numeric) or branch.is_jagged:
        raise ValueError(
            f"{tree.name!r}/{name!r} is not a fixed-size numeric branch and cannot be a 2D image"
        )
    height, width, layers = _image_layout(tree.name, name, branch.length, shape, planes)
    layer, label = _plane_index(plane, planes, layers)
    size = height * width
    start = layer * size
    values = branch.array(entry, entry + 1)[start : start + size]
    return _pixel_rows(values, height, width), label


def _image_layout(
    tree: str,
    branch: str,
    width: int,
    shape: tuple[int, int] | None,
    planes: tuple[str, ...],
) -> tuple[int, int, int]:
    """Validate and describe the rectangular layers in an image column."""
    if shape is None:
        return _square_layout(tree, branch, width, len(planes) or 1)
    return _shaped_layout(tree, branch, width, shape, planes)


def _square_layout(tree: str, branch: str, width: int, layers: int) -> tuple[int, int, int]:
    """Infer equal square layers from a fixed column width."""
    size, remainder = divmod(width, layers)
    side = math.isqrt(size)
    if remainder or not side or side * side != size:
        raise _image_layout_error(tree, branch, width)
    return side, side, layers


def _shaped_layout(
    tree: str,
    branch: str,
    width: int,
    shape: tuple[int, int],
    planes: tuple[str, ...],
) -> tuple[int, int, int]:
    """Check an explicit height and width against the column and layer names."""
    height, image_width = _valid_shape(shape)
    size = height * image_width
    layers, remainder = divmod(width, size)
    if remainder or not layers:
        raise _image_layout_error(tree, branch, width)
    if planes and len(planes) != layers:
        raise ValueError(
            f"{tree!r}/{branch!r} contains {layers} images of shape {shape}, "
            f"but {len(planes)} plane names were supplied"
        )
    return height, image_width, layers


def _valid_shape(shape: tuple[int, int]) -> tuple[int, int]:
    """Return a positive integer image shape with a user-facing refusal."""
    if not isinstance(shape, tuple) or len(shape) != 2:
        raise ValueError(f"shape must be two positive integers, not {shape!r}")
    if any(type(value) is not int or value <= 0 for value in shape):
        raise ValueError(f"shape must be two positive integers, not {shape!r}")
    return shape


def _image_layout_error(tree: str, branch: str, width: int) -> ValueError:
    """Explain how to resolve a branch whose image geometry is ambiguous."""
    return ValueError(
        f"{tree!r}/{branch!r} has {width} values per entry, which does not match "
        "the requested image layout; provide shape=(height, width), planes=, "
        "and plane= explicitly"
    )


def _plane_names(planes: Sequence[str] | None) -> tuple[str, ...]:
    """Freeze and validate optional layer names."""
    names = _frozen_plane_names(planes)
    _require_named_planes(names)
    _require_unique_planes(names)
    return names


def _frozen_plane_names(planes: Sequence[str] | None) -> tuple[str, ...]:
    """Distinguish a sequence of names from one accidentally bare name."""
    if planes is None:
        return ()
    if isinstance(planes, str):
        raise ValueError("planes must be a sequence of names, not one string")
    return tuple(planes)


def _require_named_planes(names: tuple[str, ...]) -> None:
    """Reject empty and non-text plane labels."""
    if any(not isinstance(name, str) or not name for name in names):
        raise ValueError("every plane name must be a non-empty string")


def _require_unique_planes(names: tuple[str, ...]) -> None:
    """Reject a layer map whose labels cannot identify one layer."""
    if len(set(names)) != len(names):
        raise ValueError(f"plane names must be unique, not {names!r}")


def _plane_index(
    plane: str | int | None,
    names: tuple[str, ...],
    layers: int,
) -> tuple[int, str | None]:
    """Resolve a named, numbered, or sole image layer."""
    if plane is None:
        return _sole_plane(layers)
    if type(plane) is int:
        return _numbered_plane(plane, names, layers)
    if isinstance(plane, str):
        return _named_plane(plane, names)
    raise TypeError(f"plane must be a name, integer, or None, not {type(plane).__name__}")


def _sole_plane(layers: int) -> tuple[int, None]:
    """Select an unnamed branch only when it holds exactly one image."""
    if layers != 1:
        raise ValueError(f"plane=None is ambiguous for a branch containing {layers} images")
    return 0, None


def _named_plane(plane: str, names: tuple[str, ...]) -> tuple[int, str]:
    """Resolve a layer label against the declared names."""
    if plane not in names:
        choices = ", ".join(repr(name) for name in names) or "no named planes"
        raise ValueError(f"plane must be one of {choices}, not {plane!r}")
    return names.index(plane), plane


def _numbered_plane(index: int, names: tuple[str, ...], layers: int) -> tuple[int, str]:
    """Resolve a Python-style integer layer index and give it a display name."""
    actual = layers + index if index < 0 else index
    if not 0 <= actual < layers:
        raise IndexError(f"plane index {index} is outside a branch containing {layers} images")
    return actual, names[actual] if names else str(actual)


def _pixel_rows(values: Any, height: int, width: int) -> _Pixels:
    """Turn one flat numeric array into immutable rows without losing floats."""
    convert: Callable[[Any], _Pixel] = float if getattr(values, "typecode", "") in "fd" else int
    return tuple(
        tuple(convert(value) for value in values[row : row + width])
        for row in range(0, height * width, width)
    )


def _text_value(tree: TTree, name: str, entry: int) -> str:
    """Decode one optional, zero-padded byte metadata column."""
    if name not in tree:
        return ""
    raw = bytes(tree[name].array(entry, entry + 1))
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def _number_value(tree: TTree, name: str, entry: int) -> float | None:
    """Read one optional numeric metadata value."""
    if name not in tree:
        return None
    values = tree[name].array(entry, entry + 1)
    return float(values[0]) if values else None


def _normalizer(normalization: Normalization) -> Callable[[_Pixels], _NormalizedPixels]:
    """Select a normalization implementation or reject a misspelling early."""
    if normalization == "max":
        return _maximum_normalized
    if normalization == "minmax":
        return _minmax_normalized
    if normalization == "none":
        return _float_pixels
    raise ValueError("normalization must be 'max', 'minmax', or 'none'")


def _maximum_normalized(raw: _Pixels) -> _NormalizedPixels:
    """Scale by the largest magnitude, preserving the sign of every value."""
    finite = (
        abs(float(value)) for row in raw for value in row if math.isfinite(float(value))
    )
    magnitude = max(finite, default=0.0)
    return _scaled_pixels(raw, 0.0, 1.0 / magnitude if magnitude else 0.0)


def _minmax_normalized(raw: _Pixels) -> _NormalizedPixels:
    """Map the smallest value to zero and largest to one."""
    lower, upper = _value_limits(raw)
    span = upper - lower
    return _scaled_pixels(raw, lower, 1.0 / span if span else 0.0)


def _float_pixels(raw: _Pixels) -> _NormalizedPixels:
    """Make the unchanged floating-point counterpart of a raw image."""
    return _scaled_pixels(raw, 0.0, 1.0)


def _scaled_pixels(raw: _Pixels, offset: float, scale: float) -> _NormalizedPixels:
    """Apply one affine scaling operation to every pixel."""
    return tuple(tuple((float(value) - offset) * scale for value in row) for row in raw)


def _value_limits(values: Sequence[Sequence[_Pixel]]) -> tuple[float, float]:
    """The finite display range, widened to include zero for a flat image."""
    flattened = _finite_values(values)
    if not flattened:
        return 0.0, 1.0
    lower, upper = min(flattened), max(flattened)
    if lower == upper:
        return min(0.0, lower), max(1.0, upper)
    return lower, upper


def _finite_values(values: Sequence[Sequence[_Pixel]]) -> list[float]:
    """Flatten only the values which can define a normalization or color range."""
    return [float(value) for row in values for value in row if math.isfinite(float(value))]


def _pyplot() -> Any:
    """Import the optional plotting surface with an actionable refusal."""
    try:
        from matplotlib import pyplot
    except ImportError:
        raise UnsupportedFeatureError(
            "visualizing a 2D dataset needs matplotlib, which is not installed: "
            "pip install pyxrootdclient[plot]; the raw and normalized Python "
            "matrices remain available without it"
        ) from None
    return pyplot


def _numpy() -> Any:
    """Import optional NumPy with the same actionable behavior as plotting."""
    try:
        import numpy
    except ImportError:
        raise UnsupportedFeatureError(
            "converting an Image2D to an array needs NumPy, which is not installed: "
            "pip install numpy; raw and normalized Python matrices remain available"
        ) from None
    return numpy


def _image_axes(pyplot: Any, axes: Any) -> tuple[Any, tuple[Any, Any]]:
    """Create two plotting axes, or validate the pair supplied by the caller."""
    if axes is None:
        figure, made = pyplot.subplots(1, 2, figsize=(10, 4), constrained_layout=True)
        return figure, (made[0], made[1])
    try:
        selected = tuple(axes)
    except TypeError:
        raise ValueError("axes must contain exactly two matplotlib axes") from None
    if len(selected) != 2:
        raise ValueError("axes must contain exactly two matplotlib axes")
    first, second = selected
    figure = getattr(first, "figure", None)
    if figure is None or getattr(second, "figure", None) is not figure:
        raise ValueError("both axes must belong to the same matplotlib figure")
    return figure, (first, second)


def _draw_image(
    figure: Any,
    axes: Any,
    values: Sequence[Sequence[_Pixel]],
    title: str,
    cmap: str,
    origin: str,
    interpolation: str,
    colorbar: bool,
) -> None:
    """Draw one half of an :class:`Image2D` comparison."""
    lower, upper = _value_limits(values)
    image = axes.imshow(
        values,
        origin=origin,
        interpolation=interpolation,
        cmap=cmap,
        vmin=lower,
        vmax=upper,
    )
    axes.set_title(title)
    axes.set_xlabel("column")
    axes.set_ylabel("row")
    if colorbar:
        figure.colorbar(image, ax=axes, shrink=0.82)


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
    split: str | None = None,
    inputs: Sequence[str] | None = None,
    answer: str | None = None,
    scale: bool = True,
    step: int | None = None,
    cache: bool | str | os.PathLike[str] = False,
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
    A catalogue dataset split into physical ROOT shards requires
    ``split="train"`` (or another split named in its index entry); this keeps
    a multi-gigabyte logical dataset streamable without an ambiguous default.

    The columns to learn from and the one to learn are worked out from their
    names - see :class:`Dataset` - and ``inputs`` and ``answer`` say so
    outright when a file's names are its own.

    ``cache=True`` pulls the file to :attr:`~xrd.Config.cache_dir` first and
    opens it from there, so the network is paid once and every run afterwards
    is a local read - see :func:`download`, of which this is the shorthand.
    Pass a directory instead of ``True`` to say where. It is off by default
    because streaming is the point: a training loop reads the baskets it wants
    and never the whole file, so a cache only earns its keep when the same
    dataset is read over and over, or read where the network is worse than the
    disk.

    The dataset holds the file open. Use it in a ``with`` block, or let it go
    and it closes itself.
    """
    if cache is not False:
        if not isinstance(source, str):
            raise ValueError(
                "cache= pulls a file to a local copy, so it needs somewhere to "
                "pull from: a name or a URL, not an already-open file"
            )
        source = str(
            download(
                source,
                split=split,
                into=None if cache is True else cache,
                config=config,
            )
        )
    elif isinstance(source, str) and _is_name(source):
        source = _from_catalogue(source, config, split=split)[0]
    elif split is not None:
        raise ValueError("split= selects a file shard only when source is a catalogue name")
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


def _from_catalogue(
    name: str, config: Config | None, *, split: str | None = None
) -> tuple[str, dict[str, Any]]:
    """The URL behind ``name`` and its index entry, from the catalogue.

    The entry comes back with the URL because it carries the size and the
    checksum of what should arrive, which is what makes a pulled copy
    checkable rather than merely present.
    """
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
            file = _catalogue_file(name, entry, split)
            return f"{base}/{file['file']}", file
    names = sorted(entry["name"] for entry in index["datasets"])
    held = ", ".join(names[:8]) + (f", and {len(names) - 8} more" if len(names) > 8 else "")
    raise ValueError(
        f"the catalogue at {where} has {held or 'nothing in it'}, and no {name!r}"
    )


def _catalogue_file(
    name: str, entry: dict[str, Any], split: str | None
) -> dict[str, Any]:
    """Select one physical file from a logical catalogue entry."""
    files: list[dict[str, Any]] | None = entry.get("files")
    if not files:
        if split is not None:
            raise ValueError(f"{name!r} is one ROOT file and has no file shard {split!r}")
        return entry
    if split is None:
        names = ", ".join(str(file["split"]) for file in files)
        raise ValueError(
            f"{name!r} is stored in ROOT file shards {names}; pass split= to choose one"
        )
    for file in files:
        if file["split"] == split:
            return file
    names = ", ".join(str(file["split"]) for file in files)
    raise ValueError(f"{name!r} has ROOT file shards {names}, not {split!r}")


def download(
    source: str,
    *,
    split: str | None = None,
    into: str | os.PathLike[str] | None = None,
    refresh: bool = False,
    config: Config | None = None,
) -> Path:
    """Pull a dataset once and hand back the path it now lives at.

        >>> path = download("mnist")                               # doctest: +SKIP
        >>> data = load(path)                                      # doctest: +SKIP

    ``source`` is what :func:`load` takes: a catalogue name, or a URL of any
    scheme this library speaks. The file lands under
    :attr:`~xrd.Config.cache_dir` - ``$XRD_CACHE``, or
    ``~/.cache/xrd/datasets`` - unless ``into`` names somewhere else, and a
    second call with the same source transfers nothing and returns the same
    path. Anything already on this machine is its own cache: a local path
    comes straight back, uncopied.
    ``split=`` selects one physical ROOT shard of a sharded catalogue dataset.

    The pull goes to a ``.part`` beside the target and is renamed onto it only
    once the bytes are all there and the catalogue's checksum agrees, so an
    interrupted or corrupted transfer leaves nothing that a later run would
    mistake for the dataset. That check is why a cache hit is cheap: the file
    is re-checked for length, but nothing that was renamed into place was ever
    unverified, so its digest is not recomputed on every open. ``refresh=True``
    pulls again over whatever is there.

    This is the download that :func:`load` exists to avoid, and it is worth
    reaching for when the same data is read many times over, or from somewhere
    the network is slower than the disk. For a single pass, streaming is
    faster to start and reads less.
    """
    from .config import Config as _Config

    settings = config or _Config()
    entry: dict[str, Any] = {}
    if _is_name(source):
        source, entry = _from_catalogue(source, settings, split=split)
    elif split is not None:
        raise ValueError("split= selects a file shard only when source is a catalogue name")
    if parse(source).is_local:
        return Path(source)
    where = Path(into) if into is not None else Path(settings.cache_dir) / "datasets"
    target = where / _cache_name(source)
    if not refresh and _cached(target, entry):
        return target
    where.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    from .copy import copy

    copy(source, str(part), config=settings)
    try:
        _agrees(part, entry, source)
    except BaseException:
        part.unlink(missing_ok=True)  # a bad copy is not left where a run could find it
        raise
    os.replace(part, target)
    return target


def _cache_name(url: str) -> str:
    """A file name that belongs to this URL and to no other.

    The name of the file is kept so that a cache directory can be read by a
    person, and a digest of the whole URL goes in front of it so that two
    catalogues offering their own ``mnist.root`` cannot land on one another.
    """
    stem = url.rstrip("/").rsplit("/", 1)[-1] or "dataset.root"
    return f"{hashlib.sha256(url.encode()).hexdigest()[:12]}-{stem}"


def _cached(target: Path, entry: dict[str, Any]) -> bool:
    """Whether ``target`` is already the file the entry describes."""
    if not target.exists():
        return False
    return "bytes" not in entry or target.stat().st_size == entry["bytes"]


def _agrees(part: Path, entry: dict[str, Any], source: str) -> None:
    """Raise unless the pulled bytes are what the catalogue said they would be.

    A URL nobody catalogued arrives with nothing to check it against, and the
    copy's own verification is all there is; a name out of a catalogue can be
    held to the size and digest that catalogue published.
    """
    size = part.stat().st_size
    if "bytes" in entry and size != entry["bytes"]:
        raise ValueError(
            f"{source} arrived {size} bytes long, and its catalogue says "
            f"{entry['bytes']}"
        )
    if "adler32" in entry:
        from .crypto import checksum_file

        with part.open("rb") as handle:
            got = checksum_file("adler32", iter(lambda: handle.read(1 << 20), b""))
        if got != entry["adler32"]:
            raise ValueError(
                f"{source} arrived with adler32 {got}, and its catalogue says "
                f"{entry['adler32']}"
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
