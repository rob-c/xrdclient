"""What the oldest Python this library runs on has not got.

That is 3.9, because it is what RHEL 9 and AlmaLinux 9 ship, and a physicist
on a login node at a site that runs one of those cannot choose otherwise. The
handful of things 3.10 added that this library would use anyway are written
out here rather than scattered through it, and where the interpreter has the
real thing, the real thing is what runs.

Nothing else in this package may name a version. If a module needs something
newer than the floor, it belongs here, with the fallback beside it.
"""

from __future__ import annotations

import itertools
import socket
import sys
from collections.abc import Iterable, Iterator
from typing import Any, TypeVar, overload

__all__ = ["SLOTS", "TIMEOUTS", "flag_members", "zip_strict"]

T = TypeVar("T")
U = TypeVar("U")

#: ``slots=True`` for a dataclass, where the interpreter takes it. Slots make
#: a record smaller and its attributes quicker to reach; 3.9 has no such
#: argument, and the only thing missing there is that saving.
SLOTS: dict[str, bool] = {"slots": True} if sys.version_info >= (3, 10) else {}

#: What a socket raises when it runs out of patience. 3.10 made
#: :exc:`socket.timeout` another name for :exc:`TimeoutError`; before that they
#: were two exceptions and only the first was ever raised, so every ``except``
#: that means "the far end went quiet" names both.
TIMEOUTS: tuple[type[BaseException], ...] = (socket.timeout, TimeoutError)

#: Whether :func:`zip` can do the length check itself, in C.
_NATIVE_STRICT_ZIP = sys.version_info >= (3, 10)


@overload
def zip_strict(a: Iterable[T], b: Iterable[U], /) -> Iterator[tuple[T, U]]: ...


@overload
def zip_strict(*iterables: Iterable[Any]) -> Iterator[tuple[Any, ...]]: ...


def zip_strict(*iterables: Iterable[Any]) -> Iterator[tuple[Any, ...]]:
    """:func:`zip`, refusing iterables that turn out to be different lengths.

    ``zip(..., strict=True)`` where there is one, and the same check written
    out where there is not. The check is the whole point: everything paired
    this way in this library is two halves of one thing - a flag per path, a
    byte per byte of the block before it - and a bug that makes them different
    lengths is invisible if the shorter one simply ends the loop.
    """
    if _NATIVE_STRICT_ZIP:
        return zip(*iterables, strict=True)
    return _paired(*iterables)


def flag_members(flag: Any) -> list[Any]:
    """The single bits a :class:`~enum.Flag` is made of, in declaration order.

    3.11 taught a flag member to iterate into its own parts. Before that a
    flag was not iterable at all, so the parts are picked out of the class
    here: the same members, in the same order, whatever the interpreter. A bit
    nobody named is dropped, as 3.11 drops it, and a member standing for more
    than one bit is not a part of anything.
    """
    bits = int(flag)
    return [
        member
        for member in type(flag)
        if member.value and not member.value & (member.value - 1) and member.value & bits
    ]


def _paired(*iterables: Iterable[Any]) -> Iterator[tuple[Any, ...]]:
    """3.9's :func:`zip`, with the length check 3.10 does for itself."""
    ended = object()
    for values in itertools.zip_longest(*iterables, fillvalue=ended):
        if any(value is ended for value in values):
            raise ValueError("zip_strict() was given iterables of different lengths")
        yield values
