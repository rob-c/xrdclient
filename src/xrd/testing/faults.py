"""A TCP proxy that misbehaves on purpose.

Resilience code is the code least likely to be exercised and most likely to be
wrong, because provoking it needs a network that fails on cue. :class:`FaultProxy`
is that network: it sits between a client and any server — :class:`FakeServer`,
a real ``xrootd``, an HTTP endpoint — and drops, stalls, truncates or corrupts
the bytes going past::

    with FakeServer(files={"/f": b"payload"}) as server:
        with FaultProxy(server.url) as proxy:
            proxy.drop_after(64)                    # kill the connection mid-reply
            fs = xrd.FileSystem(proxy.url)
            assert fs.read_bytes("/f") == b"payload"   # ... and it recovers

Every fault is armed by a call and disarmed by :meth:`heal`, so the usual
shape of a test is "break it, do the thing, assert it survived". Faults apply
to connections *accepted after* they are armed, plus the live ones for the
byte-counting faults, which is what makes "fail the first attempt, succeed on
the retry" expressible.

It is pure stdlib sockets and threads: no privileges, no ``iptables``, no
external proxy. The counters (:attr:`connections`, :attr:`bytes_from_server`,
:attr:`bytes_from_client`) are the other half of the point — a test can assert
that a retry really did open a second connection.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable

from .._compat import TIMEOUTS
from ..url import XRootDURL, parse

__all__ = ["FaultProxy"]


class FaultProxy:
    """A loopback TCP proxy in front of ``target`` that can be made to fail.

    ``target`` is anything with an address: an :class:`~xrd.url.XRootDURL`, a
    ``"host:port"`` string, a ``(host, port)`` pair, or an object exposing
    ``.url`` (so a :class:`~xrd.testing.FakeServer` can be passed directly).
    """

    def __init__(
        self,
        target: object,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        backlog: int = 16,
    ) -> None:
        self.target = _address(target)
        self._listener = socket.create_server((host, port), backlog=backlog, reuse_port=False)
        self._listener.settimeout(0.2)
        self._threads: list[threading.Thread] = []
        self._live: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._accepting = threading.Event()
        self._accepting.set()

        #: Connections accepted since the proxy started.
        self.connections = 0
        #: Bytes forwarded in each direction, over the proxy's whole lifetime.
        self.bytes_from_server = 0
        self.bytes_from_client = 0

        # -- armed faults, all disarmed by heal() --------------------------
        self._drop_after: int | None = None
        self._stall_after: int | None = None
        self._delay = 0.0
        self._delay_after = 0
        self._corrupt: dict[int, int] = {}
        self._chop = 0
        self._filter: Callable[[bytes], bytes] | None = None

        self._server = threading.Thread(target=self._accept_loop, daemon=True)
        self._server.start()

    # ------------------------------------------------------------------
    # Where to point the client
    # ------------------------------------------------------------------

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._listener.getsockname()[:2]
        return (host, int(port))

    @property
    def url(self) -> XRootDURL:
        """A ``root://`` URL for the proxy's own address."""
        host, port = self.address
        return parse(f"root://{host}:{port}/")

    def __repr__(self) -> str:
        host, port = self.address
        armed = ",".join(self.armed) or "healthy"
        return f"FaultProxy({host}:{port} -> {self.target[0]}:{self.target[1]}, {armed})"

    # ------------------------------------------------------------------
    # Arming faults
    # ------------------------------------------------------------------

    @property
    def armed(self) -> list[str]:
        """The names of the faults currently in force."""
        names = []
        if self._drop_after is not None:
            names.append("drop")
        if self._stall_after is not None:
            names.append("stall")
        if self._delay:
            names.append("delay")
        if self._corrupt:
            names.append("corrupt")
        if self._chop:
            names.append("chop")
        if self._filter is not None:
            names.append("filter")
        if not self._accepting.is_set():
            names.append("refuse")
        return names

    def drop_after(self, offset: int = 0) -> FaultProxy:
        """Close the connection once ``offset`` bytes have come back.

        The client sees a reset in the middle of a reply — the most common way
        a storage element fails, and the one that exercises reconnection.
        """
        self._drop_after = max(offset, 0)
        return self

    def stall_after(self, offset: int = 0) -> FaultProxy:
        """Stop forwarding after ``offset`` bytes but hold the socket open.

        Worse than a drop, because nothing tells the client anything: only a
        timeout ends it.
        """
        self._stall_after = max(offset, 0)
        return self

    def delay(self, seconds: float, *, after: int = 0) -> FaultProxy:
        """Sleep ``seconds`` before forwarding each server chunk past ``after``."""
        self._delay, self._delay_after = seconds, max(after, 0)
        return self

    def corrupt(self, offset: int, mask: int = 0xFF) -> FaultProxy:
        """XOR the byte at ``offset`` of the server stream with ``mask``.

        Data corruption that the transport cannot see is exactly what page
        checksums exist for, so this is how that path gets tested.
        """
        self._corrupt[offset] = mask & 0xFF
        return self

    def chop(self, size: int) -> FaultProxy:
        """Forward the server's bytes in pieces of at most ``size``.

        Not a failure — a reassembly test. A client that assumes one ``recv``
        is one response works fine until the day it does not.
        """
        self._chop = max(size, 0)
        return self

    def rewrite(self, filter: Callable[[bytes], bytes] | None) -> FaultProxy:
        """Pass every server chunk through ``filter`` before forwarding it."""
        self._filter = filter
        return self

    def refuse(self) -> FaultProxy:
        """Stop accepting connections; new ones are closed immediately."""
        self._accepting.clear()
        return self

    def accept(self) -> FaultProxy:
        """Undo :meth:`refuse`."""
        self._accepting.set()
        return self

    def heal(self) -> FaultProxy:
        """Disarm every fault. Connections already made are unaffected."""
        self._drop_after = self._stall_after = None
        self._delay = 0.0
        self._delay_after = 0
        self._corrupt.clear()
        self._chop = 0
        self._filter = None
        self._accepting.set()
        return self

    def cut(self) -> int:
        """Close every live connection now. Returns how many were cut."""
        with self._lock:
            live, self._live = list(self._live), set()
        for sock in live:
            _shutdown(sock)
        return len(live)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> FaultProxy:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Stop listening and cut every connection. Idempotent."""
        self._stop.set()
        self.cut()
        self._listener.close()
        self._server.join(timeout=2.0)
        for thread in list(self._threads):
            thread.join(timeout=2.0)

    # ------------------------------------------------------------------
    # The plumbing
    # ------------------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._listener.accept()
            except (TimeoutError, OSError):
                continue
            self.connections += 1
            if not self._accepting.is_set():
                _shutdown(client)
                continue
            thread = threading.Thread(target=self._serve, args=(client,), daemon=True)
            # Started before it is recorded: `close` joins everything in the
            # list, and joining a thread that has not started yet raises.
            thread.start()
            self._threads.append(thread)

    def _serve(self, client: socket.socket) -> None:
        try:
            upstream = socket.create_connection(self.target, timeout=10.0)
        except OSError:
            _shutdown(client)
            return
        with self._lock:
            self._live.update({client, upstream})
        client.settimeout(0.2)
        upstream.settimeout(0.2)
        seen = 0  # bytes from the server on *this* connection
        try:
            while not self._stop.is_set():
                if not self._pump(client, upstream):
                    break
                keep_going, seen = self._forward(client, upstream, seen)
                if not keep_going:
                    break
        except OSError:
            pass
        finally:
            with self._lock:
                self._live.discard(client)
                self._live.discard(upstream)
            _shutdown(client)
            _shutdown(upstream)

    def _forward(
        self, client: socket.socket, upstream: socket.socket, seen: int
    ) -> tuple[bool, int]:
        """Forward one server chunk; return whether and where to continue."""
        chunk = _read(upstream)
        if chunk is None:
            return True, seen
        if not chunk:
            return False, seen
        return self._forward_chunk(client, chunk, seen)

    def _forward_chunk(
        self, client: socket.socket, chunk: bytes, seen: int
    ) -> tuple[bool, int]:
        """Apply an armed stop or deliver one non-empty server chunk."""
        if self._stall_after is not None and seen >= self._stall_after:
            time.sleep(0.05)
            return True, seen
        if self._drop_after is not None and seen >= self._drop_after:
            return False, seen
        return True, self._deliver(client, chunk, seen)

    def _deliver(self, client: socket.socket, chunk: bytes, seen: int) -> int:
        """Doctor, delay, account for, and send one server chunk."""
        chunk = self._doctor(chunk, seen)
        if self._delay and seen >= self._delay_after:
            time.sleep(self._delay)
        seen += len(chunk)
        # Count before forwarding, so that a caller who has the bytes can
        # never read a total that leaves them out.
        self.bytes_from_server += len(chunk)
        for piece in _pieces(chunk, self._chop):
            client.sendall(piece)
        return seen

    def _pump(self, client: socket.socket, upstream: socket.socket) -> bool:
        """Move one client chunk upstream. False when the client has gone."""
        chunk = _read(client)
        if chunk is None:
            return True
        if not chunk:
            return False
        upstream.sendall(chunk)
        self.bytes_from_client += len(chunk)
        return True

    def _doctor(self, chunk: bytes, seen: int) -> bytes:
        """Apply the byte-level faults to one outbound chunk."""
        if self._filter is not None:
            chunk = self._filter(chunk)
        if not self._corrupt:
            return chunk
        out = bytearray(chunk)
        for offset, mask in self._corrupt.items():
            index = offset - seen
            if 0 <= index < len(out):
                out[index] ^= mask
        return bytes(out)


def _address(target: object) -> tuple[str, int]:
    """Whatever was passed in, as a ``(host, port)`` pair."""
    target = getattr(target, "url", target)
    if isinstance(target, XRootDURL):
        return (target.host, target.port)
    if isinstance(target, str):
        url = parse(target if "://" in target else f"root://{target}/")
        return (url.host, url.port)
    if isinstance(target, tuple) and len(target) == 2:
        return (str(target[0]), int(target[1]))
    raise TypeError(f"cannot take an address from {target!r}")


def _read(sock: socket.socket, size: int = 65536) -> bytes | None:
    """A chunk, ``b""`` at end of stream, or ``None`` if nothing was ready."""
    try:
        return sock.recv(size)
    except TIMEOUTS:
        return None
    except OSError:
        return b""


def _pieces(chunk: bytes, size: int) -> list[bytes]:
    if not size or len(chunk) <= size:
        return [chunk]
    return [chunk[at : at + size] for at in range(0, len(chunk), size)]


def _shutdown(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()
