"""Transport contract and the shared TLS context builder.

A transport is a byte pipe with an in-place TLS upgrade. The protocol layer
never sees a socket, so the same :class:`~xrdclient.proto.machine.SessionMachine`
runs over a real connection, a TLS connection, or an in-memory pipe in tests.
"""

from __future__ import annotations

import os
import ssl
from abc import ABC, abstractmethod

from .._log import get_logger
from ..config import Config
from ..crypto.x509 import default_proxy_path

_log = get_logger("transport")

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
            raise _unusable_proxy(exc, config.proxy) from exc
        return ctx
    _present_default_proxy(ctx)
    return ctx


def _unusable_proxy(exc: OSError, path: str) -> OSError:
    """``exc`` again, saying which file - as one message, not an ``args`` tuple."""
    # ``(errno, message)``, because an ``SSLError`` given one argument prints
    # it as a tuple: ``('cannot use the X.509 proxy ...',)``.
    reason = exc.strerror or str(exc)
    return type(exc)(exc.errno or 0, f"cannot use the X.509 proxy {path}: {reason}")


def _present_default_proxy(ctx: ssl.SSLContext) -> None:
    """Offer ``/tmp/x509up_u<uid>`` when nothing named a proxy, as gfal2 and XrdCl do.

    ``roots://`` GSI has always found it there; without this, ``https://`` to a
    grid storage element went out with no certificate and every request that
    needed one came back ``403``. A discovered file is a guess, so one that
    will not load is passed over with a warning rather than failing a request
    that may not need a certificate at all. A proxy somebody *named* - in
    ``Config(proxy=)`` or ``$X509_USER_PROXY`` - is never replaced by this one.
    """
    path = default_proxy_path()
    if not os.path.isfile(path):
        return
    try:
        ctx.load_cert_chain(path)
    except OSError as exc:
        _log.warning("not presenting the X.509 proxy %s: %s", path, exc)


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

    def receive_into(self, view: memoryview) -> int:
        """Read up to ``len(view)`` bytes *into* ``view``; 0 at end of stream.

        The bulk reader uses this to land a file's bytes straight in their
        final buffer, so a gigabyte crosses the interpreter without being
        copied into an intermediate ``bytes`` first. The default implementation
        goes through :meth:`receive` for transports that cannot do better; a
        socket overrides it with ``recv_into``.
        """
        chunk = self.receive(len(view))
        if not chunk:
            return 0
        view[: len(chunk)] = chunk
        return len(chunk)

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
