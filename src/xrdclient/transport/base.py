"""Transport contract, and the TLS context every scheme connects with.

A transport is a byte pipe with an in-place TLS upgrade. The protocol layer
never sees a socket, so the same :class:`~xrdclient.proto.machine.SessionMachine`
runs over a real connection, a TLS connection, or an in-memory pipe in tests.

:func:`tls_context` is the one place a :class:`~xrdclient.Config` becomes an
:class:`ssl.SSLContext`, shared by every scheme (``roots://``, ``https://``,
``davs://``, S3). Since 0.4.0 the builder underneath - grid trust, CRLs, the
private-key pipe, the shared cache - is :func:`xrdsec.tls.client_context`
(also ``xrdclient.tls.client_context``), shared with xgfalclient, xrdarc and
any tool built on them; it is re-exported here with ``clear_tls_cache`` and
the two environment variable names, as before. What must not break: a cached
context is shared between threads and connections, so nothing may mutate one
after it is returned; and the builder presents no certificate unless asked,
so only :func:`tls_context`'s ``present_default`` - the gfal2 and XrdCl
behaviour - offers a proxy found lying in ``/tmp``.
"""

from __future__ import annotations

import os
import ssl
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Sequence

from xrdsec.tls import CRL_ENV, SYSTEM_CA_ENV, KeyPassword, client_context
from xrdsec.tls import clear_cache as clear_tls_cache

from .. import _usercred
from ..config import Config

__all__ = [
    "CRL_ENV",
    "SYSTEM_CA_ENV",
    "Buffer",
    "KeyPassword",
    "Transport",
    "clear_tls_cache",
    "client_context",
    "coalesce",
    "tls_context",
]

#: Anything a send takes: the protocol layer frames a write's payload as a
#: view of the caller's buffer, and it is sent as one.
Buffer = bytes | bytearray | memoryview


def byte_view(buffer: Buffer) -> memoryview:
    """``buffer`` as a flat view of bytes, so lengths and slices count bytes."""
    view = memoryview(buffer)
    return view if view.format == "B" else view.cast("B")


def coalesce(buffers: Iterable[Buffer], size: int) -> Iterator[Buffer]:
    """``buffers`` regrouped into pieces of at most ``size`` bytes, in order.

    For a link that has no vectored send - TLS, whose records are at most
    16 KiB of plaintext each anyway. Small buffers (a request header, a
    short body) are joined, so a header does not go out as a record and a
    packet of its own; a large one is passed through as slices of itself,
    so its bytes are copied only where a piece straddles a buffer boundary.
    """
    stage = bytearray()
    for buffer in buffers:
        view = byte_view(buffer)
        at, end = 0, len(view)
        while at < end:
            if not stage and end - at >= size:
                yield view[at : at + size]
                at += size
                continue
            take = min(size - len(stage), end - at)
            stage += view[at : at + take]
            at += take
            if len(stage) >= size:
                yield stage
                stage = bytearray()
    if stage:
        yield stage


def tls_context(config: Config, *, system_ca: bool = False) -> ssl.SSLContext:
    """:func:`xrdsec.tls.client_context` for ``config``: the context every scheme connects with.

    What ``config`` supplies: its CA settings, ``verify_tls``,
    ``check_crl`` (``$XRD_CHECKCRL``), and who the client is - ``proxy``,
    else the separate ``usercert``/``userkey`` (pass phrase from
    ``key_password`` or a prompt; see :mod:`xrdclient._usercred`), else the
    default proxy if one is there (``present_default``), which is what
    gfal2 and XrdCl present unasked.

    ``system_ca=True`` is what callers pass for an endpoint outside the
    grid's PKI - an S3 service, a public ``https://`` site - to add the
    operating system's store to a grid CA directory;
    ``config.tls_system_ca`` (``$XRD_TLSSYSTEMCA``, see
    :data:`~xrdsec.tls.SYSTEM_CA_ENV`) overrides the caller in either direction when it
    is set.
    """
    user = _usercred.wanted(config)
    key = os.path.expanduser(str(config.userkey)) if user else None
    return client_context(
        cert=config.proxy or (os.path.expanduser(str(config.usercert)) if user else None),
        key=key,
        ca_path=config.ca_path,
        ca_file=config.ca_file,
        verify=config.verify_tls,
        system_ca=_wants_system_ca(system_ca, config),
        check_crl=config.check_crl,
        # ``Config.key_password``, or a person asked - only if the key is
        # encrypted, which only reading it tells.
        key_password=(lambda: _usercred.pass_phrase(config, str(key))) if user else None,
        present_default=not (config.proxy or user),
    )


def _wants_system_ca(requested: bool, config: Config) -> bool:
    """The caller's choice, unless :attr:`Config.tls_system_ca` makes it for them."""
    forced = config.tls_system_ca
    return requested if forced is None else forced


class Transport(ABC):
    """A bidirectional byte stream."""

    __slots__ = ()

    @property
    @abstractmethod
    def closed(self) -> bool:
        """Whether :meth:`close` has run; a peer's hang-up is seen as ``b""`` instead."""

    @abstractmethod
    def send(self, data: bytes) -> None:
        """Write every byte of ``data``."""

    def send_buffers(self, buffers: Sequence[Buffer]) -> None:
        """Write every byte of every buffer, in order, as one stream.

        A request goes out as a header and the caller's own payload, and a
        socket that can send several buffers in one call (``sendmsg``) sends
        it without the two ever being joined. This default joins them -
        correct for any transport, and one copy - for those that cannot.
        """
        self.send(b"".join(buffers))

    def wait_readable(self, timeout: float | None) -> bool:
        """Block until a :meth:`receive` would not block, or ``timeout`` passes.

        Lets a driver wait on the link without holding anything a sender or
        another reader needs, then take what has arrived. ``False`` means the
        time ran out. A transport that cannot tell says ``True`` at once and
        leaves the waiting to :meth:`receive` - as an in-memory pipe, which
        never blocks, does.
        """
        return True

    def wait_writable(self, timeout: float | None) -> bool:
        """Block until a send would make progress, or ``timeout`` passes.

        The counterpart of :meth:`wait_readable`, for a link whose sends and
        receives must not overlap: a sender waits here, holding nothing, for
        room on the wire. Says ``True`` at once by default.
        """
        return True

    def interrupt(self) -> None:
        """Wake whatever is blocked waiting on this link, before it is closed.

        A thread in :meth:`wait_readable` or :meth:`receive` is not woken by
        the descriptor being closed under it on every platform; shutting the
        link down first wakes it to an end of stream. Nothing to do by default.
        """
        return None

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
    def close(self) -> None:
        """Release the link. Idempotent, and must not raise: it runs in cleanup paths."""
