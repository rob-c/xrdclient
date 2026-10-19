"""An in-memory transport, for driving the state machine in tests.

Both halves of a :func:`pipe` share two queues, so a fake server can be
written as a plain function without a socket, a thread, or a port.
"""

from __future__ import annotations

from collections import deque

from ..config import Config
from ..errors import ConnectionError as XrdConnectionError
from .base import Transport

__all__ = ["MemoryTransport", "pipe"]


class MemoryTransport(Transport):
    """One end of an in-memory byte pipe."""

    __slots__ = ("_closed", "_rx", "_tx", "host", "port", "tls_started")

    def __init__(self, rx: deque[bytes], tx: deque[bytes], host: str = "memory", port: int = 0):
        """Wrap the two queues; ``rx`` is the peer's ``tx`` (see :func:`pipe`).

        ``host``/``port`` exist only so code that names its peer in messages
        and in redirect checks finds something there.
        """
        self._rx = rx
        self._tx = tx
        self._closed = False
        self.tls_started = False
        self.host = host
        self.port = port

    @property
    def closed(self) -> bool:
        """Whether *this* end was closed; the peer's end is independent."""
        return self._closed

    def send(self, data: bytes) -> None:
        """Queue ``data`` for the peer as one chunk; empty sends are dropped.

        Raises :class:`~xrdclient.errors.ConnectionError` after :meth:`close`,
        as a socket would, so tests see the same failure a closed link gives.
        """
        if self._closed:
            raise XrdConnectionError("transport is closed")
        if data:
            self._tx.append(data)

    def receive(self, size: int = 65536) -> bytes:
        """The next queued chunk, at most ``size`` bytes of it.

        Never blocks: an empty queue reads as ``b""``, end of stream, which is
        how a scripted fake server "hangs up". A chunk longer than ``size`` is
        split and its remainder put back at the front, so short reads -
        which the frame parser must tolerate on a real socket - happen here too.
        """
        if not self._rx:
            return b""
        chunk = self._rx.popleft()
        if len(chunk) > size:
            self._rx.appendleft(chunk[size:])
            return chunk[:size]
        return chunk

    def start_tls(self, hostname: str, config: Config) -> None:
        """Record that the upgrade was asked for; the bytes stay plaintext."""
        self.tls_started = True

    def settimeout(self, timeout: float | None) -> None:
        """Nothing to bound: a receive here returns whatever is queued."""

    def close(self) -> None:
        """Refuse further sends; already queued bytes can still be read."""
        self._closed = True

    # -- test helpers ---------------------------------------------------

    def feed(self, data: bytes) -> None:
        """Queue bytes as if the peer had sent them."""
        self._rx.append(data)

    def sent(self) -> bytes:
        """Everything written to this end so far, drained."""
        out = b"".join(self._tx)
        self._tx.clear()
        return out

    def __repr__(self) -> str:
        """Queue depths in chunks, which is what a failing test wants to see."""
        return f"MemoryTransport(rx={len(self._rx)}, tx={len(self._tx)}, closed={self._closed})"


def pipe() -> tuple[MemoryTransport, MemoryTransport]:
    """Two transports wired back to back."""
    a: deque[bytes] = deque()
    b: deque[bytes] = deque()
    return MemoryTransport(a, b), MemoryTransport(b, a)
