"""Talking to a KDC: one request, one reply, over UDP or TCP.

RFC 4120 section 7.2: over UDP a message is one datagram; over TCP it is
preceded by its length as four big-endian bytes. The client tries UDP first,
as MIT does, and moves to TCP when the KDC answers ``KRB_ERR_RESPONSE_TOO_BIG``
or when the request is larger than ``udp_preference_limit``; a ``kdc``
entry written ``tcp/host`` is only ever asked over TCP. KDCs are tried in
the order ``krb5.conf`` lists them, and the first to answer wins.

A reply is returned as bytes. Whether it is a KRB-ERROR is for the caller
to decide, except for "too big", which is this module's business.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

from ..._compat import SLOTS
from ..._log import get_logger
from ...crypto.der import DERError
from ...errors import CredentialError
from .asn1 import APP_KRB_ERROR, decode_krb_error
from .profile import Profile

__all__ = ["KdcAddress", "exchange", "parse_kdc"]

_log = get_logger(__name__)

KDC_PORT = 88

#: MIT's default: requests longer than this go over TCP to begin with.
UDP_PREFERENCE_LIMIT = 1465

KRB_ERR_RESPONSE_TOO_BIG = 52

#: Seconds to wait for one KDC before trying the next.
TIMEOUT = 5.0

#: No KDC reply this client wants is anywhere near this long.
MAX_REPLY = 1 << 20


@dataclass(frozen=True, **SLOTS)
class KdcAddress:
    """One ``kdc =`` entry: where, and over what."""

    host: str
    port: int
    transport: str  # "udp", "tcp", or "" for either

    def __str__(self) -> str:
        prefix = f"{self.transport}/" if self.transport else ""
        return f"{prefix}{self.host}:{self.port}"


def parse_kdc(entry: str) -> KdcAddress | None:
    """``host``, ``host:port``, ``[v6]:port``, with an optional ``tcp/`` or ``udp/``.

    ``kkdcp`` HTTPS proxies are not supported; they come back as ``None``.
    """
    transport = entry[:3].lower() if entry[3:4] == "/" else ""
    if transport not in ("", "tcp", "udp") or "://" in entry:
        return None
    host, port = _host_port(entry[4:] if transport else entry)
    if not host or (port and not port.isdigit()):
        return None
    return KdcAddress(host, int(port) if port else KDC_PORT, transport)


def _host_port(text: str) -> tuple[str, str]:
    """Split ``host:port``; a bracketed or bare IPv6 address keeps its colons."""
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        return host, rest[1:] if rest.startswith(":") else ""
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        return host, port
    return text, ""


def _udp(address: KdcAddress, request: bytes, timeout: float) -> bytes:
    for family, kind, proto, _name, sockaddr in socket.getaddrinfo(
        address.host, address.port, type=socket.SOCK_DGRAM
    ):
        with socket.socket(family, kind, proto) as sock:
            sock.settimeout(timeout)
            # Connecting a datagram socket means only the KDC's own replies arrive.
            sock.connect(sockaddr)
            sock.send(request)
            return sock.recv(65535)
    raise OSError(f"no address for {address.host}")  # pragma: no cover - getaddrinfo raises instead


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    out = bytearray()
    while len(out) < count:
        chunk = sock.recv(count - len(out))
        if not chunk:
            raise OSError(f"the KDC closed the connection after {len(out)} of {count} bytes")
        out += chunk
    return bytes(out)


def _tcp(address: KdcAddress, request: bytes, timeout: float) -> bytes:
    with socket.create_connection((address.host, address.port), timeout=timeout) as sock:
        sock.sendall(len(request).to_bytes(4, "big") + request)
        size = int.from_bytes(_recv_exact(sock, 4), "big")
        if size > MAX_REPLY:
            raise OSError(f"the KDC announced a {size}-byte reply")
        return _recv_exact(sock, size)


def _too_big(reply: bytes) -> bool:
    if not reply.startswith(bytes([APP_KRB_ERROR])):
        return False
    try:
        return decode_krb_error(reply).code == KRB_ERR_RESPONSE_TOO_BIG
    except DERError:
        return False  # malformed; let the caller say so


def _ask(address: KdcAddress, request: bytes, udp_limit: int, timeout: float) -> bytes:
    """One KDC, by the transport its entry and the request size call for."""
    if address.transport == "tcp" or (address.transport == "" and len(request) > udp_limit):
        return _tcp(address, request, timeout)
    reply = _udp(address, request, timeout)
    if _too_big(reply) and address.transport != "udp":
        _log.debug("KDC %s: reply too big for UDP, retrying over TCP", address)
        return _tcp(address, request, timeout)
    return reply


def exchange(profile: Profile, realm: str, request: bytes, *, timeout: float = TIMEOUT) -> bytes:
    """Send ``request`` to a KDC for ``realm`` and return its reply."""
    entries = profile.kdcs(realm)
    addresses = [found for found in map(parse_kdc, entries) if found is not None]
    if not addresses:
        raise CredentialError(
            f"krb5.conf lists no usable KDC for realm {realm!r}; this client does not look "
            f"KDCs up in DNS, so add [realms] {realm} = {{ kdc = <host> }} "
            "(the file is $KRB5_CONFIG, else /etc/krb5.conf)"
        )
    limit_text = profile.libdefault("udp_preference_limit")
    limit = int(limit_text) if limit_text.isdigit() else UDP_PREFERENCE_LIMIT
    failures: list[str] = []
    for address in addresses:
        try:
            return _ask(address, request, limit, timeout)
        except OSError as exc:  # timeouts included: both are OSError subclasses
            failures.append(f"{address}: {exc or type(exc).__name__}")
    raise CredentialError(f"no KDC for realm {realm} answered ({'; '.join(failures)})")
