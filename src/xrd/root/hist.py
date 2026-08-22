"""Histograms, as objects rather than as the dictionaries they are written as.

A histogram in a ROOT file is a class like any other: a name, a set of drawing
attributes nobody analysing data wants, three axes whether it uses them or not,
and one flat run of numbers holding every bin including the two the axis keeps
for what fell off each end. This turns that into the thing it is - bins, edges
and what is in them - while leaving every member it was written with in reach
under :attr:`Histogram.members`, so nothing is hidden by being tidied away.
"""

from __future__ import annotations

import array
import math
from typing import Any

from .._compat import zip_strict
from .draw import axes, bar, missing_picture, shade
from .errors import FormatError

__all__ = ["HISTOGRAMS", "Axis", "Histogram"]

#: The histogram classes this reads: one, two and three dimensions, in each of
#: the types ROOT keeps bin contents in.
HISTOGRAMS = tuple(f"TH{dimension}{kind}" for dimension in (1, 2, 3) for kind in "CSILFD")

#: What a freshly made object draws like: ROOT's own defaults, spelled out.
LINE = {"fLineColor": 1, "fLineStyle": 1, "fLineWidth": 1}
FILL = {"fFillColor": 0, "fFillStyle": 1001}
MARKER = {"fMarkerColor": 1, "fMarkerStyle": 1, "fMarkerSize": 1.0}
AXIS_STYLE = {
    "fNdivisions": 510,
    "fAxisColor": 1,
    "fLabelColor": 1,
    "fLabelFont": 42,
    "fLabelOffset": 0.005,
    "fLabelSize": 0.035,
    "fTickLength": 0.03,
    "fTitleOffset": 1.0,
    "fTitleSize": 0.035,
    "fTitleColor": 1,
    "fTitleFont": 42,
}


def _axis(name: str, nbins: int, low: float, high: float, edges: list[float]) -> dict[str, Any]:
    """The members of one freshly made ``TAxis``."""
    return {
        "TNamed": {"fName": name, "fTitle": ""},
        "TAttAxis": dict(AXIS_STYLE),
        "fNbins": nbins,
        "fXmin": low,
        "fXmax": high,
        "fXbins": array.array("d", edges),
        "fFirst": 0,
        "fLast": 0,
        "fBits2": 0,
        "fTimeDisplay": False,
        "fTimeFormat": "",
        "fLabels": None,
        "fModLabs": None,
    }


class Axis:
    """One axis of a histogram: how it is binned, and what it is called."""

    __slots__ = ("name", "title", "nbins", "low", "high", "_edges")

    def __init__(self, row: dict[str, Any]) -> None:
        named = row["TNamed"]
        #: What ROOT calls the axis, which is ``xaxis`` unless it was renamed.
        self.name: str = named["fName"]
        #: The label the axis is drawn with, which is where units usually are.
        self.title: str = named["fTitle"]
        #: How many bins there are, not counting the two for what fell off.
        self.nbins: int = row["fNbins"]
        #: The low end of the first bin, and the high end of the last.
        self.low: float = row["fXmin"]
        self.high: float = row["fXmax"]
        self._edges = row["fXbins"]

    def __len__(self) -> int:
        return self.nbins

    def edges(self) -> array.array[float]:
        """The ``nbins + 1`` edges, whether they were written or are worked out.

        An axis binned unevenly keeps every edge; an evenly binned one keeps
        none of them, because the two ends and the count say where they are.
        """
        if len(self._edges) == self.nbins + 1:
            return array.array("d", self._edges)
        width = (self.high - self.low) / self.nbins
        edges = [self.low + width * step for step in range(self.nbins)]
        return array.array("d", [*edges, self.high])

    def centers(self) -> array.array[float]:
        """The middle of each bin, which is what a point is usually drawn at."""
        edges = self.edges()
        return array.array("d", [(edges[i] + edges[i + 1]) / 2 for i in range(self.nbins)])

    def __repr__(self) -> str:
        return f"<Axis {self.name!r} of {self.nbins} bins from {self.low:g} to {self.high:g}>"


def _core(row: dict[str, Any]) -> dict[str, Any] | None:
    """The ``TH1`` part of a histogram, however far down it is inherited."""
    if "fNcells" in row:
        return row
    for value in row.values():
        if isinstance(value, dict):
            found = _core(value)
            if found is not None:
                return found
    return None


def _contents(row: dict[str, Any]) -> array.array[Any] | None:
    """The bins themselves, which are the array base the class inherits."""
    for name, value in row.items():
        if name.startswith("TArray") and isinstance(value, array.array):
            return value
    return None


class Histogram:
    """A histogram: its bins, what is in them, and the axes they lie on.

    Bins are counted the way Python counts, from zero and without the two
    ROOT keeps at each end for what fell off it - ``values()[0]`` is the first
    bin of the axis, not the underflow. Pass ``flow=True`` to get those two
    back, at the ends where ROOT keeps them.

    A two-dimensional histogram gives a row per bin along x, so
    ``values()[ix][iy]`` is the bin ROOT would call ``GetBinContent(ix+1,
    iy+1)``, and a three-dimensional one nests once more. Everything comes
    back as :class:`array.array`, which :func:`numpy.asarray` takes without
    copying and which needs nothing installed to use as it is.
    """

    __slots__ = ("classname", "members", "axes", "_core", "_bins", "_widths")

    def __init__(self, classname: str, members: dict[str, Any]) -> None:
        core, bins = _core(members), _contents(members)
        if core is None or bins is None:
            raise FormatError(f"a {classname} was written without its bins or its axes")
        #: The class the file says this is, such as ``TH1D``.
        self.classname = classname
        #: Every member, as it was written, for whatever is not here by name.
        self.members = members
        self._core, self._bins = core, bins
        #: One :class:`Axis` per dimension, x first.
        self.axes = tuple(Axis(core[f"f{letter}axis"]) for letter in "XYZ"[: int(classname[2])])
        self._widths = [len(axis) + 2 for axis in self.axes]
        cells = math.prod(self._widths)
        if len(bins) < cells:
            raise FormatError(
                f"a {classname} of {cells} bins counting the ends holds only {len(bins)} values"
            )

    @property
    def name(self) -> str:
        """What the histogram is called, which is the key it was written under."""
        return str(self._core["TNamed"]["fName"])

    @property
    def title(self) -> str:
        """The title it is drawn with, which is usually a sentence about it."""
        return str(self._core["TNamed"]["fTitle"])

    @property
    def entries(self) -> float:
        """How many times it was filled, which weights make a fraction of."""
        return float(self._core["fEntries"])

    @property
    def shape(self) -> tuple[int, ...]:
        """How many bins along each axis, not counting the two at the ends."""
        return tuple(len(axis) for axis in self.axes)

    def __len__(self) -> int:
        return math.prod(self.shape)

    def edges(self, axis: int = 0) -> array.array[float]:
        """The edges of one axis, x by default: one more than it has bins."""
        return self.axes[axis].edges()

    def values(self, flow: bool = False) -> Any:
        """What is in each bin, shaped the way the axes are."""
        return self._shaped(self._bins, flow)

    def errors(self, flow: bool = False) -> Any:
        """The uncertainty on each bin, shaped the way the values are.

        A histogram filled with weights keeps the sum of their squares and
        that is where this comes from; one filled without them keeps no such
        sum, and then the uncertainty on a count of *n* is the root of *n*,
        which is what ROOT itself would give back.
        """
        squared = self._core["fSumw2"]
        source = squared if len(squared) == len(self._bins) else self._bins
        return self._shaped(array.array("d", [math.sqrt(abs(value)) for value in source]), flow)

    def sum(self, flow: bool = False) -> float:
        """Everything in the bins added up, which weights make a float of."""

        def total(values: Any) -> float:
            if isinstance(values, list):
                return math.fsum(total(row) for row in values)
            return math.fsum(values)

        return total(self.values(flow))

    def _shaped(self, flat: array.array[Any], flow: bool) -> Any:
        """One flat run of bins cut into the shape its axes give it.

        ROOT writes the bins with x running fastest, so a row along the last
        axis is a slice with a stride and the ones before it are lists.
        """
        edge = 0 if flow else 1
        counts = [width if flow else width - 2 for width in self._widths]
        strides = [math.prod(self._widths[:level]) for level in range(len(self._widths))]
        last = len(counts) - 1

        def cut(level: int, base: int) -> Any:
            if level == last:
                start = base + edge * strides[level]
                return flat[start : start + counts[level] * strides[level] : strides[level]]
            below, stride = level + 1, strides[level]
            return [cut(below, base + (step + edge) * stride) for step in range(counts[level])]

        return cut(0, 0)

    @classmethod
    def new(
        cls,
        name: str,
        edges: Any,
        values: Any,
        *,
        title: str = "",
        errors: Any = None,
        entries: float | None = None,
    ) -> Histogram:
        """A one-dimensional histogram built from Python numbers, ready to write.

            >>> h = Histogram.new("counts", [0, 1, 2, 4], [5, 3, 1])
            >>> h.values()
            array('d', [5.0, 3.0, 1.0])

        ``edges`` is every bin edge, one more of them than there are bins, in
        increasing order; evenly spaced ones are stored the compact way ROOT
        stores an even axis. ``values`` is what is in each bin - give two
        extra values to fill the flow bins at the ends, which are otherwise
        zero. ``errors`` is the uncertainty per bin, shaped like ``values``;
        without it a bin's error is the square root of its count, as ROOT
        gives for a histogram filled without weights. ``entries`` is how many
        fills the histogram represents, taken to be the sum of the values
        unless said otherwise.
        """
        edges = _validated_edges(edges)
        nbins = len(edges) - 1
        values = _flowed(values, nbins, "values")
        errors = _optional_errors(errors, nbins)
        low, high, stored = _axis_geometry(edges, nbins)
        inner = values[1:-1]
        centers = [(edges[step] + edges[step + 1]) / 2 for step in range(nbins)]
        weights = errors[1:-1] if errors is not None else None
        core = _histogram_core(
            name, title, nbins, low, high, stored, values, inner, centers, errors, weights, entries
        )
        return cls("TH1D", {"TH1": core, "TArrayD": array.array("d", values)})

    def plot(self, ax: Any = None, **options: Any) -> Any:
        """Draw onto matplotlib axes, made fresh unless ``ax`` brings some.

        One dimension draws as steps, two as a shaded mesh; three have no
        flat picture and refuse rather than pretending. The axes come back,
        so styling and saving carry on where this left off - and matplotlib
        not being installed refuses with the two ways out by name.
        """
        if len(self.axes) > 2:
            raise missing_picture("histogram", len(self.axes))
        if ax is None:
            ax = axes()
        if len(self.axes) == 1:
            ax.stairs(self.values(), self.edges(), **options)
        else:
            columns = [list(column) for column in zip_strict(*self.values())]
            ax.pcolormesh(self.edges(0), self.edges(1), columns, **options)
        if self.title:
            ax.set_title(self.title)
        if self.axes[0].title:
            ax.set_xlabel(self.axes[0].title)
        if len(self.axes) > 1 and self.axes[1].title:
            ax.set_ylabel(self.axes[1].title)
        return ax

    def text(self, width: int = 60) -> str:
        """The histogram drawn with characters, for a terminal or a log file.

        One line per bin - its edges, a bar and the value - for one
        dimension; a shaded grid with y upward for two; a refusal for three,
        which have no flat picture.
        """
        if len(self.axes) == 1:
            return _text_1d(self, width)
        if len(self.axes) == 2:
            return _text_2d(self)
        raise missing_picture("histogram", len(self.axes))

    def __repr__(self) -> str:
        shape = " x ".join(str(count) for count in self.shape)
        return f"<{self.classname} {self.name!r} of {shape} bins, {self.entries:g} entries>"


def _flowed(values: Any, nbins: int, what: str) -> list[float]:
    """One value per bin as floats, the flow bins zeroed unless given."""
    given = [float(value) for value in values]
    if len(given) == nbins:
        return [0.0, *given, 0.0]
    if len(given) == nbins + 2:
        return given
    raise ValueError(
        f"{len(given)} {what} for {nbins} bins: give one per bin, or two "
        f"more counting the flow at each end"
    )


def _validated_edges(edges: Any) -> list[float]:
    made = [float(edge) for edge in edges]
    if len(made) < 2:
        raise ValueError("a histogram needs at least two edges to have a bin")
    if any(second <= first for first, second in zip(made, made[1:])):
        raise ValueError("edges must increase: each bin has to be wider than nothing")
    return made


def _optional_errors(errors: Any, nbins: int) -> list[float] | None:
    return _flowed(errors, nbins, "errors") if errors is not None else None


def _axis_geometry(edges: list[float], nbins: int) -> tuple[float, float, list[float]]:
    low, high = edges[0], edges[-1]
    width = (high - low) / nbins
    even = [low + width * step for step in range(nbins)] + [high]
    return low, high, [] if edges == even else edges


def _histogram_core(
    name: str,
    title: str,
    nbins: int,
    low: float,
    high: float,
    stored: list[float],
    values: list[float],
    inner: list[float],
    centers: list[float],
    errors: list[float] | None,
    weights: list[float] | None,
    entries: float | None,
) -> dict[str, Any]:
    sumw2 = (
        math.fsum(error * error for error in weights) if weights is not None else math.fsum(inner)
    )
    error_squares = [error * error for error in errors] if errors is not None else []
    return {
        "TNamed": {"fName": str(name), "fTitle": str(title)},
        "TAttLine": dict(LINE),
        "TAttFill": dict(FILL),
        "TAttMarker": dict(MARKER),
        "fNcells": nbins + 2,
        "fXaxis": _axis("xaxis", nbins, low, high, stored),
        "fYaxis": _axis("yaxis", 1, 0.0, 1.0, []),
        "fZaxis": _axis("zaxis", 1, 0.0, 1.0, []),
        "fBarOffset": 0,
        "fBarWidth": 1000,
        "fEntries": float(entries) if entries is not None else math.fsum(values),
        "fTsumw": math.fsum(inner),
        "fTsumw2": sumw2,
        "fTsumwx": math.fsum(value * center for value, center in zip_strict(inner, centers)),
        "fTsumwx2": math.fsum(
            value * center * center for value, center in zip_strict(inner, centers)
        ),
        "fMaximum": -1111.0,
        "fMinimum": -1111.0,
        "fNormFactor": 0.0,
        "fContour": array.array("d"),
        "fSumw2": array.array("d", error_squares),
        "fOption": "",
        "fFunctions": [],
        "fBufferSize": 0,
        "fBuffer": array.array("d"),
        "fBinStatErrOpt": 0,
    }


def _text_1d(histogram: Histogram, width: int) -> str:
    values = histogram.values()
    edges = histogram.edges()
    top = max((value for value in values if value > 0), default=0.0)
    return "\n".join(
        f"[{edges[step]:g}, {edges[step + 1]:g})".rjust(24)
        + f" {bar(value / top if top else 0.0, width):<{width}} {value:g}"
        for step, value in enumerate(values)
    )


def _text_2d(histogram: Histogram) -> str:
    rows = histogram.values()
    top = max((value for row in rows for value in row if value > 0), default=0.0)
    return "\n".join(
        "".join(shade(row[step] / top if top else 0.0) for row in rows)
        for step in reversed(range(len(histogram.axes[1])))
    )
