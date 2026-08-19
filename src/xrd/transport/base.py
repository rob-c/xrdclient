"""Transport contract and the shared TLS context builder.

A transport is a byte pipe with an in-place TLS upgrade. The protocol layer
never sees a socket, so the same :class:`~xrd.proto.machine.SessionMachine`
runs over a real connection, a TLS connection, or an in-memory pipe in tests.
"""

from __future__ import annotations

import ssl
from abc import ABC, abstractmethod

from ..config import Config

__all__ = ["Transport", "tls_context"]


def tls_context(config: Config) -> ssl.SSLContext:
    """A client TLS context honouring the X.509 environment.

    Verification is on unless ``config.verify_tls`` is explicitly ``False``;
    nothing in the library turns it off implicitly.
    """
    ctx = ssl.create_default_context(cafile=config.ca_file, capath=config.ca_path)
    if not config.verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if config.proxy:
        # An RFC 3820 proxy is a cert chain plus its key in one PEM file.
        try:
            ctx.load_cert_chain(config.proxy)
        except OSError as exc:
            # ssl's own message names neither the file nor the setting that
            # chose it, and a stale X509_USER_PROXY is how most people get here.
            raise type(exc)(f"cannot use the X.509 proxy {config.proxy}: {exc}") from exc
    return ctx


class Transport(ABC):
    """A bidirectional byte stream."""

    __slots__ = ()

    @property
    @abstractmethod
    def closed(self) -> bool: ...

    @abstractmethod
    def send(self, data: bytes) -> None:
        """Write every byte of ``data``."""

    @abstractmethod
    def receive(self, size: int = 65536) -> bytes:
        """Read up to ``size`` bytes; ``b""`` at end of stream."""

    @abstractmethod
    def start_tls(self, hostname: str, config: Config) -> None:
        """Upgrade the live connection in place."""

    @abstractmethod
    def settimeout(self, timeout: float | None) -> None:
        """Bound how long one :meth:`receive` may block, in seconds.

        The session layer sets this on whichever link it is about to read
        from, to hold a deadline that is shorter than the transport's own.
        """

    @abstractmethod
    def close(self) -> None: ...
