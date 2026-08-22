"""Graphs, as points rather than as the arrays they are written as.

A graph in a ROOT file is two arrays of the same length and, if whoever wrote
it kept them, two or four more holding the error bars. This turns that into
the thing it is - points, and what the bars round them are - while leaving
every member it was written with in reach under :attr:`Graph.members`.
"""

from __future__ import annotations

import array
from typing import Any

from .._compat import zip_strict
from .draw import axes
from .errors import FormatError, UnsupportedFeatureError
from .hist import FILL, LINE, MARKER

__all__ = ["GRAPHS", "Graph"]

#: The graph classes this reads: points, points with error bars, points whose
#: bars are a different length each side, and points whose y errors are kept
#: in more than one layer.
GRAPHS = ("TGraph", "TGraphErrors", "TGraphAsymmErrors", "TGraphMultiErrors")


def _core(row: dict[str, Any]) -> dict[str, Any] | None:
    """The ``TGraph`` part of a graph, however far down it is inherited."""
    if "fNpoints" in row:
        return row
    for value in row.values():
        if isinstance(value, dict):
            found = _core(value)
            if found is not None:
                return found
    return None


class Graph:
    """A graph: its points, and the error bars round them.

        >>> for x, y in graph:                     # doctest: +SKIP
        ...     print(x, y)

    ``x`` and ``y`` are :class:`array.array` of one value per point, which
    :func:`numpy.asarray` takes without copying. :attr:`xerr` and :attr:`yerr`
    are the bars either side of each point, or ``None`` for a graph written
    without them; a graph keeping its errors in layers has them in
    :attr:`layers`.
    """

    __slots__ = ("classname", "members", "x", "y", "_core")

    def __init__(self, classname: str, members: dict[str, Any]) -> None:
        core = _core(members)
        if core is None or "fX" not in core or "fY" not in core:
            raise FormatError(f"a {classname} was written without its points")
        points = int(core["fNpoints"])
        if len(core["fX"]) < points or len(core["fY"]) < points:
            raise FormatError(
                f"a {classname} of {points} points holds only "
                f"{min(len(core['fX']), len(core['fY']))} of them"
            )
        #: The class the file says this is, such as ``TGraphErrors``.
        self.classname = classname
        #: Every member, as it was written, for whatever is not here by name.
        self.members = members
        self._core = core
        #: Where each point is, one value per point.
        self.x: array.array[float] = array.array("d", core["fX"][:points])
        self.y: array.array[float] = array.array("d", core["fY"][:points])

    @property
    def name(self) -> str:
        """What the graph is called, which is the key it was written under."""
        return str(self._core["TNamed"]["fName"])

    @property
    def title(self) -> str:
        """The title it is drawn with, which is usually a sentence about it."""
        return str(self._core["TNamed"]["fTitle"])

    def __len__(self) -> int:
        return len(self.x)

    def __getitem__(self, index: int) -> tuple[float, float]:
        return (self.x[index], self.y[index])

    def __iter__(self) -> Any:
        return iter(zip_strict(self.x, self.y))

    def points(self) -> list[tuple[float, float]]:
        """Every point as a pair, which is what a graph is a picture of."""
        return list(self)

    def _pair(self, low: Any, high: Any) -> tuple[array.array[float], array.array[float]]:
        """Two arrays of bars, cut to the points the graph says it has."""
        points = len(self)
        return (array.array("d", low[:points]), array.array("d", high[:points]))

    def _bars(
        self, axis: str, *spellings: tuple[str, str]
    ) -> tuple[array.array[float], array.array[float]] | None:
        """The bars either side along one axis, or ``None`` if there are none.

        Each spelling is the pair of names one class gives the low and the
        high bar; a graph whose bars are the same length both sides keeps a
        single array instead, and that array is both sides of the point.
        """
        for below, above in spellings:
            low, high = self.members.get(below), self.members.get(above)
            if low is not None and high is not None:
                return self._pair(low, high)
        same = self.members.get(f"fE{axis}")
        return None if same is None else self._pair(same, same)

    @property
    def xerr(self) -> tuple[array.array[float], array.array[float]] | None:
        """The bars left and right of each point, or ``None`` if none were kept."""
        return self._bars("X", ("fEXlow", "fEXhigh"), ("fExL", "fExH"))

    @property
    def layers(self) -> tuple[tuple[array.array[float], array.array[float]], ...]:
        """The bars below and above each point, one pair per layer of them.

        A graph told to keep its statistical and its systematic errors apart
        keeps a layer of each, in the order they were added. An ordinary graph
        keeps one layer, and a graph written without y errors keeps none.
        """
        low, high = self.members.get("fEyL"), self.members.get("fEyH")
        if low is None or high is None:
            bars = self._bars("Y", ("fEYlow", "fEYhigh"))
            return () if bars is None else (bars,)
        return tuple(self._pair(a, b) for a, b in zip_strict(low, high))

    @property
    def yerr(self) -> tuple[array.array[float], array.array[float]] | None:
        """The bars below and above each point, or ``None`` if none were kept.

        A graph keeping more than one layer of them refuses here rather than
        answer with one of the layers or with a sum of them it made up: which
        of those you want is yours to say, and :attr:`layers` has them all.
        """
        layers = self.layers
        if len(layers) > 1:
            raise UnsupportedFeatureError(
                f"this {self.classname} keeps its y errors in {len(layers)} layers, "
                f"which are one pair of bars only once you have said how to add them "
                f"up; they are each of them in .layers"
            )
        return layers[0] if layers else None

    @classmethod
    def new(
        cls,
        name: str,
        x: Any,
        y: Any,
        *,
        title: str = "",
        xerr: Any = None,
        yerr: Any = None,
    ) -> Graph:
        """A graph built from Python numbers, ready to write.

            >>> g = Graph.new("scan", [1, 2, 3], [2.0, 3.9, 6.1], yerr=[0.1, 0.2, 0.2])
            >>> g.classname
            'TGraphErrors'

        ``xerr`` and ``yerr`` are each either one bar per point, or a
        ``(low, high)`` pair of runs for bars of different lengths each side.
        The class picks itself: plain points make a ``TGraph``, even bars a
        ``TGraphErrors``, and any uneven pair a ``TGraphAsymmErrors`` with
        the even ones carried on both sides.
        """
        xs = array.array("d", (float(value) for value in x))
        ys = array.array("d", (float(value) for value in y))
        if len(xs) != len(ys):
            raise ValueError(f"{len(xs)} x values and {len(ys)} y values are not points")
        count = len(xs)
        across = _sides(xerr, count, "xerr")
        upward = _sides(yerr, count, "yerr")
        core = _graph_core(name, title, xs, ys)
        if across is None and upward is None:
            return cls("TGraph", core)
        zeros = array.array("d", [0.0]) * count
        if _symmetric(across, upward):
            return cls("TGraphErrors", _even_members(core, across, upward, zeros))
        return cls("TGraphAsymmErrors", _uneven_members(core, across, upward, zeros))

    def plot(self, ax: Any = None, **options: Any) -> Any:
        """Draw onto matplotlib axes, made fresh unless ``ax`` brings some.

        Points with their error bars; a graph keeping layers of them draws
        every layer over the same points. The axes come back, so styling and
        saving carry on where this left off - and matplotlib not being
        installed refuses with the two ways out by name.
        """
        if ax is None:
            ax = axes()
        style: dict[str, Any] = {"fmt": "o", "markersize": 4}
        style.update(options)
        for index, bars in enumerate(self.layers or (None,)):
            ax.errorbar(self.x, self.y, yerr=bars, xerr=self.xerr if index == 0 else None, **style)
        if self.title:
            ax.set_title(self.title)
        return ax

    def text(self, width: int = 60, height: int = 16) -> str:
        """The graph as a grid of points, for a terminal or a log file."""
        if not self.x:
            return "(a graph of no points)"
        xlo, xhi = min(self.x), max(self.x)
        ylo, yhi = min(self.y), max(self.y)
        xspan, yspan = (xhi - xlo) or 1.0, (yhi - ylo) or 1.0
        grid = [[" "] * width for _ in range(height)]
        for x, y in self:
            column = round((x - xlo) / xspan * (width - 1))
            line = round((y - ylo) / yspan * (height - 1))
            grid[height - 1 - line][column] = "*"
        lines = []
        for index, cells in enumerate(grid):
            label = _axis_label(index, height, ylo, yhi)
            lines.append(f"{label:>10} |{''.join(cells)}|")
        left, right = f"{xlo:g}", f"{xhi:g}"
        lines.append(f"{'':>10}  {left:<{width - len(right)}}{right}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"<{self.classname} {self.name!r} of {len(self)} points>"


def _sides(
    err: Any, count: int, label: str
) -> tuple[array.array[float], array.array[float], bool] | None:
    """The bars along one axis: low, high, and whether they were given uneven.

    One bar per point is both sides of it; a pair of runs is a side each,
    which is only tellable from two points' worth of bars because runs are
    not numbers.
    """
    if err is None:
        return None
    given = list(err)
    if _is_side_pair(given):
        return _uneven_sides(given, count, label)
    return _even_sides(given, count, label)


def _is_side_pair(given: list[Any]) -> bool:
    return len(given) == 2 and not any(isinstance(side, (int, float)) for side in given)


def _uneven_sides(
    given: list[Any], count: int, label: str
) -> tuple[array.array[float], array.array[float], bool]:
    low = array.array("d", (float(value) for value in given[0]))
    high = array.array("d", (float(value) for value in given[1]))
    if len(low) != count or len(high) != count:
        raise ValueError(f"{label} has {len(low)} low and {len(high)} high bars for {count} points")
    return low, high, True


def _even_sides(
    given: list[Any], count: int, label: str
) -> tuple[array.array[float], array.array[float], bool]:
    bars = array.array("d", (float(value) for value in given))
    if len(bars) != count:
        raise ValueError(
            f"{label} has {len(bars)} bars for {count} points: give one per "
            f"point, or a (low, high) pair of runs"
        )
    return (bars, bars, False)


def _graph_core(
    name: str, title: str, xs: array.array[float], ys: array.array[float]
) -> dict[str, Any]:
    return {
        "TNamed": {"fName": str(name), "fTitle": str(title)},
        "TAttLine": dict(LINE),
        "TAttFill": dict(FILL),
        "TAttMarker": dict(MARKER),
        "fNpoints": len(xs),
        "fX": xs,
        "fY": ys,
        "fFunctions": None,
        "fHistogram": None,
        "fMinimum": -1111.0,
        "fMaximum": -1111.0,
    }


def _symmetric(across: Any, upward: Any) -> bool:
    return (across is None or not across[2]) and (upward is None or not upward[2])


def _even_members(core: dict[str, Any], across: Any, upward: Any, zeros: Any) -> dict[str, Any]:
    return {
        "TGraph": core,
        "fEX": across[0] if across is not None else zeros,
        "fEY": upward[0] if upward is not None else zeros,
    }


def _uneven_members(core: dict[str, Any], across: Any, upward: Any, zeros: Any) -> dict[str, Any]:
    return {
        "TGraph": core,
        "fEXlow": across[0] if across is not None else zeros,
        "fEXhigh": across[1] if across is not None else zeros,
        "fEYlow": upward[0] if upward is not None else zeros,
        "fEYhigh": upward[1] if upward is not None else zeros,
    }


def _axis_label(index: int, height: int, low: float, high: float) -> str:
    if index == 0:
        return f"{high:g}"
    if index == height - 1:
        return f"{low:g}"
    return ""
