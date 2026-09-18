"""Byte transports: a real socket, and an in-memory pipe for tests.

The in-memory pipe is imported only when it is asked for: it exists for the
test suite, and a program that opens a socket should not pay to load it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import Transport, tls_context
from .sync import SocketTransport

if TYPE_CHECKING:
    from .memory import MemoryTransport, pipe

__all__ = ["Transport", "SocketTransport", "MemoryTransport", "pipe", "tls_context"]


def __getattr__(name: str) -> object:
    if name in ("MemoryTransport", "pipe"):
        import importlib

        value = getattr(importlib.import_module(f"{__name__}.memory"), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
