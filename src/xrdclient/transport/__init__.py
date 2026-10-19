"""Byte transports: a real socket, and an in-memory pipe for tests.

Start with :mod:`.base`, which holds the :class:`Transport` contract every
session drives and the cached TLS context builder; :mod:`.sync` is the TCP/TLS
implementation and :mod:`._dial` the RFC 8305 connector behind it. Nothing
above this package touches a socket, which is what lets the protocol state
machine run unchanged over an in-memory pipe.

The in-memory pipe is imported only when it is asked for: it exists for the
test suite, and a program that opens a socket should not pay to load it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import Transport, tls_context
from .sync import SocketTransport

if TYPE_CHECKING:
    from .memory import MemoryTransport, pipe

__all__ = ["MemoryTransport", "SocketTransport", "Transport", "pipe", "tls_context"]


def __getattr__(name: str) -> object:
    """Load :mod:`.memory` on first use of one of its names (PEP 562).

    The value is cached in the module's globals, so the import happens once
    and later lookups never reach this hook.
    """
    if name in ("MemoryTransport", "pipe"):
        import importlib

        value = getattr(importlib.import_module(f"{__name__}.memory"), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
