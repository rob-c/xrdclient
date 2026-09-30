"""Argument checks with the bindings' exceptions.

The bindings parse every numeric argument into a C integer of a fixed width -
an offset into 64 bits, a size or chunk size into 32, a timeout, flags or a
mode into 16 - so a value that is not an integer is a :class:`TypeError` and
one that does not fit is an :class:`OverflowError`, raised in the caller's
thread before anything is sent. Code written against them relies on both
(``tests/test_file.py`` in the bindings' own suite checks every one), so the
same checks are made here, with the same outcome.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

__all__ = ["chunks", "u16", "u32", "u64"]


def _unsigned(value: Any, bits: int, name: str) -> int:
    # ``bool`` is an ``int`` to Python and not to the C parser; neither is a
    # float, however whole.
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer, not {type(value).__name__}")
    if not 0 <= value < 1 << bits:
        raise OverflowError(f"{name}={value} does not fit in an unsigned {bits}-bit integer")
    return value


# Each check below tries the case nearly every call is - a plain ``int`` in
# range - before the general one, which is the same test made slowly.


def u64(value: Any, name: str = "offset") -> int:
    """An offset, or a size a file can have."""
    if type(value) is int and 0 <= value < _U64:
        return value
    return _unsigned(value, 64, name)


def u32(value: Any, name: str = "size") -> int:
    """The size of one request, or of one chunk of one."""
    if type(value) is int and 0 <= value < _U32:
        return value
    return _unsigned(value, 32, name)


def u16(value: Any, name: str = "timeout") -> int:
    """A timeout in seconds, a flags word, or a mode."""
    if type(value) is int and 0 <= value < _U16:
        return value
    return _unsigned(value, 16, name)


_U64, _U32, _U16 = 1 << 64, 1 << 32, 1 << 16


def chunks(value: Any) -> list[tuple[int, int]]:
    """``vector_read``'s ``[(offset, length), ...]``, each pair checked."""
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        raise TypeError(f"chunks must be a list of (offset, length) pairs, not {value!r}")
    pairs = []
    for pair in value:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise TypeError(f"each chunk must be an (offset, length) pair, not {pair!r}")
        pairs.append((u64(pair[0], "chunk offset"), u32(pair[1], "chunk length")))
    return pairs
