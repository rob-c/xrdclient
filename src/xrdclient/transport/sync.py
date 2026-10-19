"""Blocking socket transport: TCP from :mod:`._dial`, optionally upgraded to TLS.

Every XRootD and HTTP session in the library ends up on a
:class:`SocketTransport`. It owns its socket outright - a
:func:`weakref.finalize` closes it if the transport is dropped without
:meth:`~SocketTransport.close` - and turns every :class:`OSError` into the
library's :class:`~xrdclient.errors.ConnectionError` or
:class:`~xrdclient.errors.TimeoutError`, naming the peer, so callers never
handle raw socket errors.

Concurrency: the transport itself takes no locks. The session layer above
guarantees one sender and one receiver at a time (they may be different
threads - a socket allows that), and uses :meth:`~SocketTransport.interrupt`
to wake a blocked reader before closing.
"""

from __future__ import annotations

import contextlib
import ipaddress
import math
import os
import select
import socket
import ssl
import weakref
from collections.abc import Sequence

from ..config import Config
from ..errors import ConnectionError as XrdConnectionError
from ..errors import TimeoutError as XrdTimeoutError
from ..log import get_logger
from . import _dial
from .base import Buffer, Transport, byte_view, coalesce, tls_context

__all__ = ["SocketTransport"]

_log = get_logger(__name__)


def _iov_max() -> int:
    """How many buffers one ``sendmsg`` may carry on this system."""
    try:
        limit = os.sysconf("SC_IOV_MAX")
    except (AttributeError, OSError, ValueError):  # not a POSIX system
        return 16
    return max(16, min(limit, 1024))


_IOV_MAX = _iov_max()

#: What a TLS send hands ``sendall`` at once: whole records' worth of
#: plaintext, with a request header joined to the start of its body.
_TLS_PIECE = 64 << 10


def _advance(views: list[memoryview], sent: int) -> list[memoryview]:
    """What is left of ``views`` once their first ``sent`` bytes are gone."""
    for index, view in enumerate(views):
        if sent < len(view):
            return [view[sent:], *views[index + 1 :]]
        sent -= len(view)
    return []


def _ready(sock: socket.socket, events: int, timeout: float | None) -> bool:
    """Whether ``sock`` reports ``events`` within ``timeout`` seconds.

    ``poll`` rather than ``select``, which cannot watch a descriptor numbered
    past ``FD_SETSIZE``. A socket closed under the caller counts as ready: the
    receive or send that follows is what reports it.
    """
    try:
        poller = select.poll()
        poller.register(sock, events)
        wait = None if timeout is None else max(0, math.ceil(timeout * 1000))
        return bool(poller.poll(wait))
    except (OSError, ValueError):
        return True


#: Unanswered keepalive probes before the kernel gives up on a connection.
KEEPALIVE_PROBES = 3


def keepalive(sock: socket.socket, interval: float) -> None:
    """Have the kernel probe an idle ``sock`` after ``interval`` seconds.

    The probes then go every third of ``interval`` and :data:`KEEPALIVE_PROBES`
    unanswered ones drop the connection, so a peer that vanished silently is
    found out in about twice ``interval``. ``0`` or less turns keepalive off.
    The per-socket timing options differ by platform - ``TCP_KEEPIDLE`` on
    Linux, ``TCP_KEEPALIVE`` on macOS - and are set where they exist; a
    kernel that refuses one keeps its system-wide default for it.
    """
    if interval <= 0:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 0)
        return
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    idle = max(1, int(interval))
    timings = (
        ("TCP_KEEPIDLE", idle),
        ("TCP_KEEPALIVE", idle),
        ("TCP_KEEPINTVL", max(1, idle // 3)),
        ("TCP_KEEPCNT", KEEPALIVE_PROBES),
    )
    for name, value in timings:
        option = getattr(socket, name, None)
        if option is None:
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, option, value)
        except OSError as exc:
            _log.debug("keeping the system's %s: %s", name, exc)


def _is_loopback(address: str) -> bool:
    """Whether ``address`` is a loopback IP, ``::ffff:127.x`` and scoped forms included.

    The ``%zone`` suffix of a link-local IPv6 address is not something
    :mod:`ipaddress` parses, and an IPv4-mapped loopback is not ``is_loopback``
    on its own - both seen on dual-stack hosts where a local server answers
    over an IPv6 socket.
    """
    try:
        parsed = ipaddress.ip_address(address.partition("%")[0])
    except ValueError:
        return False
    mapped = getattr(parsed, "ipv4_mapped", None)
    return bool((mapped or parsed).is_loopback)


class SocketTransport(Transport):
    """A TCP connection, optionally upgraded to TLS."""

    __slots__ = ("__weakref__", "_finalizer", "_sock", "host", "port")

    def __init__(self, sock: socket.socket, host: str, port: int) -> None:
        """Take ownership of a connected ``sock``; use :meth:`connect` to make one.

        ``host`` and ``port`` are the names the caller dialled (not the
        resolved address), used in error messages and as the TLS server name.
        """
        self._sock = sock
        close = getattr(sock, "close", lambda: None)
        self._finalizer = weakref.finalize(self, close)
        self.host = host
        self.port = port

    @classmethod
    def connect(cls, host: str, port: int, config: Config | None = None) -> SocketTransport:
        """Open a connection within ``connect_timeout``, racing the host's addresses.

        Resolution and every attempt share the one budget, the families tried
        are :attr:`~xrdclient.Config.network_stack`'s, and they are raced as
        RFC 8305 describes - see :mod:`xrdclient.transport._dial`. A failure
        names every address and what became of it.
        """
        config = config or Config()
        try:
            sock = _dial.dial(
                host, port, stack=config.network_stack, timeout=config.connect_timeout
            )
        except _dial.DialError as exc:
            # The message names every address and its error; the cause is the
            # last of them, as ``create_connection`` raises it, so that code
            # walking the chain for an errno (``ECONNREFUSED`` and the like)
            # still finds one.
            cause = exc.failures[-1] if exc.failures else None
            if exc.timed_out:
                raise XrdTimeoutError(
                    f"connecting to {host}:{port} timed out: {exc.strerror}"
                ) from cause
            raise XrdConnectionError(f"cannot connect to {host}:{port}: {exc.strerror}") from cause
        except TimeoutError as exc:
            raise XrdTimeoutError(f"connecting to {host}:{port} timed out: {exc}") from exc
        except OSError as exc:
            raise XrdConnectionError(f"cannot connect to {host}:{port}: {exc}") from exc
        sock.settimeout(config.request_timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        keepalive(sock, config.keepalive_interval)
        return cls(sock, host, port)

    @property
    def peer_is_local(self) -> bool:
        """Whether the server at the other end runs on this machine.

        True for a loopback peer, and for one whose address is the address
        this end of the connection has - the host talking to itself through
        one of its own interfaces. What a ``file://`` redirect is checked
        against: only a server on this host can know what a local path means.
        """
        try:
            peer = self._sock.getpeername()[0]
            mine = self._sock.getsockname()[0]
        except OSError:
            return False
        return _is_loopback(peer) or peer == mine

    @property
    def closed(self) -> bool:
        """Whether the descriptor is gone; a peer hang-up shows as ``b""`` instead."""
        return self._sock.fileno() < 0

    def send(self, data: bytes) -> None:
        """``sendall`` ``data``, mapping socket errors to the library's own."""
        if not data:
            return
        try:
            self._sock.sendall(data)
        except TimeoutError as exc:
            raise XrdTimeoutError(f"send to {self.host}:{self.port} timed out") from exc
        except OSError as exc:
            raise XrdConnectionError(f"send to {self.host}:{self.port} failed: {exc}") from exc

    def send_buffers(self, buffers: Sequence[Buffer]) -> None:
        """Send ``buffers`` in order with one ``sendmsg`` per kernel call.

        A request header and the caller's payload go out together without
        being joined. ``sendmsg`` may take only part of what it is offered,
        so whatever is left - down to part of one buffer - goes round again.
        TLS has no vectored send: there the buffers are regrouped into
        record-sized pieces, small ones joined (see :func:`coalesce`).
        """
        sock = self._sock
        try:
            if len(buffers) == 1:
                # A request with no large body: one buffer, nothing to gather.
                sock.sendall(buffers[0])
                return
            if isinstance(sock, ssl.SSLSocket):
                for piece in coalesce(buffers, _TLS_PIECE):
                    sock.sendall(piece)
                return
            views = [view for view in map(byte_view, buffers) if view]
            while views:
                views = _advance(views, sock.sendmsg(views[:_IOV_MAX]))
        except TimeoutError as exc:
            raise XrdTimeoutError(f"send to {self.host}:{self.port} timed out") from exc
        except OSError as exc:
            raise XrdConnectionError(f"send to {self.host}:{self.port} failed: {exc}") from exc

    def wait_readable(self, timeout: float | None) -> bool:
        """Wait for something to read, including what TLS has already decrypted."""
        sock = self._sock
        if isinstance(sock, ssl.SSLSocket) and sock.pending():
            # Bytes already off the socket and decrypted are invisible to
            # poll: waiting on it would wait for the next record instead.
            return True
        return _ready(sock, select.POLLIN, timeout)

    def wait_writable(self, timeout: float | None) -> bool:
        """Poll for ``POLLOUT``; a closed socket counts as writable (see :func:`_ready`)."""
        return _ready(self._sock, select.POLLOUT, timeout)

    def interrupt(self) -> None:
        """Shut the socket down, waking any thread blocked on it."""
        with contextlib.suppress(OSError):
            self._sock.shutdown(socket.SHUT_RDWR)

    def receive(self, size: int = 65536) -> bytes:
        """One ``recv`` of up to ``size`` bytes, bounded by :meth:`settimeout`."""
        try:
            return self._sock.recv(size)
        except TimeoutError as exc:
            raise XrdTimeoutError(f"read from {self.host}:{self.port} timed out") from exc
        except OSError as exc:
            raise XrdConnectionError(f"read from {self.host}:{self.port} failed: {exc}") from exc

    def receive_into(self, view: memoryview) -> int:
        """``recv`` straight into ``view``, with no intermediate ``bytes``."""
        try:
            return self._sock.recv_into(view, len(view))
        except TimeoutError as exc:
            raise XrdTimeoutError(f"read from {self.host}:{self.port} timed out") from exc
        except OSError as exc:
            raise XrdConnectionError(f"read from {self.host}:{self.port} failed: {exc}") from exc

    def start_tls(self, hostname: str, config: Config) -> None:
        """Wrap the socket in TLS in place, verifying ``hostname``.

        XRootD switches to TLS mid-connection: ``kXR_protocol`` negotiates it
        after the plaintext handshake, and the very next bytes are a TLS
        ClientHello on the same TCP stream. The wrapped socket replaces the
        plain one, and the finalizer is moved onto it so the TLS object - which
        now owns the descriptor - is what gets closed.
        """
        ctx = tls_context(config)
        try:
            self._sock = ctx.wrap_socket(self._sock, server_hostname=hostname)
        except ssl.SSLError as exc:
            raise XrdConnectionError(f"TLS handshake with {hostname} failed: {exc}") from exc
        self._finalizer.detach()
        self._finalizer = weakref.finalize(self, self._sock.close)
        _log.debug("TLS established with %s (%s)", hostname, self._sock.version())

    def settimeout(self, timeout: float | None) -> None:
        """Bound each blocking socket call; ``None`` waits for ever."""
        self._sock.settimeout(timeout)

    def close(self) -> None:
        """Close the socket; idempotent, and it never raises.

        No TLS ``close_notify`` is waited for - XRootD ends a session with
        ``kXR_endsess`` or by dropping the connection, so there is nothing a
        clean TLS shutdown would add except a possible stall.
        """
        try:
            self._sock.close()
        except OSError:
            pass
        finally:
            self._finalizer.detach()

    def __repr__(self) -> str:
        """Peer and whether the link has been upgraded to TLS yet."""
        kind = "tls" if isinstance(self._sock, ssl.SSLSocket) else "tcp"
        return f"SocketTransport({self.host}:{self.port}, {kind})"
