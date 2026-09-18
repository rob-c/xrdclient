"""Blocking socket transport."""

from __future__ import annotations

import socket
import ssl

from .._compat import TIMEOUTS
from .._log import get_logger
from ..config import Config
from ..errors import ConnectionError as XrdConnectionError
from ..errors import TimeoutError as XrdTimeoutError
from .base import Transport, tls_context

__all__ = ["SocketTransport"]

_log = get_logger(__name__)


class SocketTransport(Transport):
    """A TCP connection, optionally upgraded to TLS."""

    __slots__ = ("_sock", "host", "port")

    def __init__(self, sock: socket.socket, host: str, port: int) -> None:
        self._sock = sock
        self.host = host
        self.port = port

    @classmethod
    def connect(cls, host: str, port: int, config: Config | None = None) -> SocketTransport:
        """Open a connection, honouring ``connect_timeout``."""
        config = config or Config()
        try:
            sock = socket.create_connection((host, port), timeout=config.connect_timeout)
        except TIMEOUTS as exc:
            raise XrdTimeoutError(f"connecting to {host}:{port} timed out") from exc
        except OSError as exc:
            raise XrdConnectionError(f"cannot connect to {host}:{port}: {exc}") from exc
        sock.settimeout(config.request_timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        return cls(sock, host, port)

    @property
    def closed(self) -> bool:
        return self._sock.fileno() < 0

    def send(self, data: bytes) -> None:
        if not data:
            return
        try:
            self._sock.sendall(data)
        except TIMEOUTS as exc:
            raise XrdTimeoutError(f"send to {self.host}:{self.port} timed out") from exc
        except OSError as exc:
            raise XrdConnectionError(f"send to {self.host}:{self.port} failed: {exc}") from exc

    def receive(self, size: int = 65536) -> bytes:
        try:
            return self._sock.recv(size)
        except TIMEOUTS as exc:
            raise XrdTimeoutError(f"read from {self.host}:{self.port} timed out") from exc
        except OSError as exc:
            raise XrdConnectionError(f"read from {self.host}:{self.port} failed: {exc}") from exc

    def receive_into(self, view: memoryview) -> int:
        """``recv`` straight into ``view``, with no intermediate ``bytes``."""
        try:
            return self._sock.recv_into(view, len(view))
        except TIMEOUTS as exc:
            raise XrdTimeoutError(f"read from {self.host}:{self.port} timed out") from exc
        except OSError as exc:
            raise XrdConnectionError(f"read from {self.host}:{self.port} failed: {exc}") from exc

    def start_tls(self, hostname: str, config: Config) -> None:
        ctx = tls_context(config)
        try:
            self._sock = ctx.wrap_socket(self._sock, server_hostname=hostname)
        except ssl.SSLError as exc:
            raise XrdConnectionError(f"TLS handshake with {hostname} failed: {exc}") from exc
        _log.debug("TLS established with %s (%s)", hostname, self._sock.version())

    def settimeout(self, timeout: float | None) -> None:
        self._sock.settimeout(timeout)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def __repr__(self) -> str:
        kind = "tls" if isinstance(self._sock, ssl.SSLSocket) else "tcp"
        return f"SocketTransport({self.host}:{self.port}, {kind})"
