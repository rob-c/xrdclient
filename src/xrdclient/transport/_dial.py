"""Turning a host name into a connected socket, quickly and within a budget.

:func:`socket.create_connection` is the obvious tool and the wrong one for a
client that talks to grid storage. It tries each address the resolver returns
in turn, each with the *whole* timeout: a host whose IPv6 address is
advertised but black-holed - common enough on campus networks that RFC 8305
exists because of it - costs the full 30 s connect window before IPv4 is even
tried. And it resolves with :func:`socket.getaddrinfo`, which takes as long
as the resolver does.

So this module does what RFC 8305 ("Happy Eyeballs v2") describes, with
:mod:`selectors` and non-blocking sockets rather than an event loop:

* resolution runs on a helper thread the caller waits on for no longer than
  the connect budget (a numeric address skips the thread - there is nothing
  to look up);
* the addresses are interleaved by family, starting with whichever the
  resolver put first, so a broken family costs one attempt and not a list;
* attempts start :data:`STAGGER` apart, or at once when the previous one
  fails outright, and race: the first to connect wins, the rest are closed;
* the whole thing - resolution included - shares one budget, the
  configuration's ``connect_timeout``;
* when every address fails, the error names every address and why, as
  ``create_connection(all_errors=True)`` would.

Which families take part is :attr:`~xrdclient.Config.network_stack`, in
XrdCl's vocabulary (``IPAuto``, ``IPv4``, ``IPv6``, ``IPv4Mapped6``).
"""

from __future__ import annotations

import errno
import ipaddress
import itertools
import os
import selectors
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field

__all__ = ["STAGGER", "DialError", "dial", "interleave", "resolve"]

#: RFC 8305's recommended "Connection Attempt Delay": how long one attempt
#: has to itself before the next address is tried alongside it.
STAGGER = 0.25

#: A socket address as ``getaddrinfo`` gives one: IPv4, IPv6, or (never
#: asked for here, but in the type) a raw family's.
SockAddr = tuple[str, int] | tuple[str, int, int, int] | tuple[int, bytes]

#: ``getaddrinfo`` row: family, type, proto, canonname, sockaddr.
AddrInfo = tuple[socket.AddressFamily, socket.SocketKind, int, str, SockAddr]

#: The families each :attr:`~xrdclient.Config.network_stack` value admits.
_FAMILIES = {
    "ipauto": socket.AF_UNSPEC,
    "ipv4": socket.AF_INET,
    "ipv6": socket.AF_INET6,
    "ipv4mapped6": socket.AF_UNSPEC,
}


class DialError(OSError):
    """Every address failed, or the budget ran out first.

    ``failures`` holds one :class:`OSError` per address that was refused or
    unreachable, each naming the address; ``timed_out`` says whether the
    budget ended the attempt rather than the last address failing.
    """

    def __init__(self, message: str, failures: list[OSError], *, timed_out: bool) -> None:
        """Carry ``ETIMEDOUT`` or ``ECONNREFUSED`` as ``errno``, as one socket would."""
        super().__init__(errno.ETIMEDOUT if timed_out else errno.ECONNREFUSED, message)
        self.failures = failures
        self.timed_out = timed_out


def _family(stack: str) -> int:
    """The ``getaddrinfo`` family for a ``network_stack`` name, case-insensitively.

    A misspelt setting is a :class:`ValueError` naming the valid values:
    silently falling back to ``IPAuto`` would hide the very misconfiguration
    someone set the option to work around.
    """
    try:
        return _FAMILIES[stack.lower()]
    except KeyError:
        raise ValueError(
            f"network_stack {stack!r} is not one of IPAuto, IPv4, IPv6, IPv4Mapped6"
        ) from None


def _numeric(host: str) -> bool:
    """Whether ``host`` is a literal IP address, so resolving it cannot block."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _getaddrinfo(host: str, port: int, family: int) -> list[AddrInfo]:
    """``getaddrinfo`` for a stream socket, as a list of :data:`AddrInfo`."""
    found: list[AddrInfo] = []
    found.extend(socket.getaddrinfo(host, port, family, socket.SOCK_STREAM))
    return found


def _lookup(host: str, port: int, family: int, timeout: float) -> list[AddrInfo]:
    """``getaddrinfo``, abandoned (not cancelled - it cannot be) after ``timeout``."""
    if _numeric(host):
        return _getaddrinfo(host, port, family)
    outcome: list[list[AddrInfo] | BaseException] = []
    finished = threading.Event()

    def ask() -> None:
        """Run on the helper thread; hand the result or the error back."""
        try:
            outcome.append(_getaddrinfo(host, port, family))
        except BaseException as exc:  # handed to the waiting thread
            outcome.append(exc)
        finally:
            finished.set()

    threading.Thread(target=ask, name=f"xrd-resolve-{host}", daemon=True).start()
    if not finished.wait(max(timeout, 0.0)):
        raise TimeoutError(f"resolving {host} took longer than {timeout:g}s")
    found = outcome[0]
    if isinstance(found, BaseException):
        raise found
    return found


def _mapped(info: AddrInfo) -> AddrInfo:
    """An IPv4 row as the IPv6 socket address ``::ffff:a.b.c.d`` reaches it by."""
    family, kind, proto, name, sockaddr = info
    if family != socket.AF_INET:
        return info
    host, port = str(sockaddr[0]), int(sockaddr[1])
    return (socket.AF_INET6, kind, proto, name, (f"::ffff:{host}", port, 0, 0))


def resolve(host: str, port: int, stack: str, timeout: float) -> list[AddrInfo]:
    """The addresses ``host`` has in the families ``stack`` admits, deduplicated.

    ``IPv4Mapped6`` resolves both families and then reaches the IPv4 ones
    over IPv6 sockets, as XrdCl's mode of that name does on a host whose
    stack is IPv6-only towards the application.
    """
    found = _lookup(host, port, _family(stack), timeout)
    if stack.lower() == "ipv4mapped6":
        found = [_mapped(info) for info in found]
    unique: dict[tuple[int, SockAddr], AddrInfo] = {}
    for info in found:
        unique.setdefault((info[0], info[4]), info)
    return list(unique.values())


def interleave(infos: list[AddrInfo]) -> list[AddrInfo]:
    """RFC 8305 section 4: alternate families, the resolver's first family first."""
    first = infos[0][0] if infos else None
    preferred = [info for info in infos if info[0] == first]
    others = [info for info in infos if info[0] != first]
    ordered: list[AddrInfo] = []
    for pair in itertools.zip_longest(preferred, others):
        ordered.extend(info for info in pair if info is not None)
    return ordered


def _where(info: AddrInfo) -> str:
    """``host:port``, bracketed for IPv6, for naming an address in an error."""
    host, port = str(info[4][0]), str(info[4][1])
    return f"[{host}]:{port}" if info[0] == socket.AF_INET6 else f"{host}:{port}"


def _start(info: AddrInfo) -> tuple[socket.socket, int]:
    """A non-blocking connect to ``info`` under way, and its immediate status.

    The status is ``0`` for a connection already made (loopback can manage
    that), ``EINPROGRESS``-like for one in flight, anything else for one that
    failed on the spot.
    """
    family, kind, proto, _, sockaddr = info
    sock = socket.socket(family, kind, proto)
    try:
        if family == socket.AF_INET6 and str(sockaddr[0]).startswith("::ffff:"):
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.setblocking(False)
        return sock, sock.connect_ex(sockaddr)
    except BaseException:
        sock.close()
        raise


_IN_FLIGHT = frozenset({errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR})


@dataclass
class _Race:
    """The attempts of one :func:`dial`, and what became of each."""

    order: deque[AddrInfo]
    deadline: float
    stagger: float
    selector: selectors.BaseSelector
    failures: list[OSError] = field(default_factory=list)
    #: When the next address may be started without waiting for a result.
    next_start: float = 0.0
    #: Whether the budget ran out before every address had answered.
    expired: bool = False

    def run(self) -> socket.socket | None:
        """The first socket to connect, or ``None`` when none did in time."""
        while self.order or self.selector.get_map():
            now = time.monotonic()
            if now >= self.deadline:
                self.expired = True
                return None
            if self.order and (now >= self.next_start or not self.selector.get_map()):
                winner = self._launch(now)
            else:
                winner = self._settle(now)
            if winner is not None:
                return winner
        return None

    def _launch(self, now: float) -> socket.socket | None:
        """Start the next address; the socket if it connected on the spot.

        A connect that fails at once is recorded and frees the next address
        to start immediately; one in flight is watched for writability and
        holds the next attempt back for :attr:`stagger` seconds.
        """
        info = self.order.popleft()
        try:
            sock, status = _start(info)
        except OSError as exc:
            self._failed(info, exc.errno or errno.EIO)
            return None
        if status == 0:
            return sock
        if status not in _IN_FLIGHT:
            sock.close()
            self._failed(info, status)
            return None
        # The socket rides along as data, so the selector hands it back typed.
        self.selector.register(sock, selectors.EVENT_WRITE, (sock, info))
        self.next_start = now + self.stagger
        return None

    def _settle(self, now: float) -> socket.socket | None:
        """Wait for an attempt in flight to finish, until the next may start.

        A non-blocking connect reports completion as writability; whether it
        *succeeded* is only in ``SO_ERROR``. The first success wins; failures
        are closed and recorded. The wait never outlasts the dial's deadline.
        """
        until = self.next_start if self.order else self.deadline
        for key, _ in self.selector.select(max(min(until, self.deadline) - now, 0.0)):
            attempt: tuple[socket.socket, AddrInfo] = key.data
            sock, info = attempt
            self.selector.unregister(sock)
            status = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if status == 0:
                return sock
            sock.close()
            self._failed(info, status)
        return None

    def _failed(self, info: AddrInfo, code: int) -> None:
        self.failures.append(OSError(code, f"{_where(info)}: {os.strerror(code)}"))
        # A refusal is an answer: the next address need not wait its turn.
        self.next_start = 0.0

    def abandon(self) -> list[str]:
        """Close every attempt still in flight; the addresses they were for."""
        pending = []
        for key in list(self.selector.get_map().values()):
            attempt: tuple[socket.socket, AddrInfo] = key.data
            sock, info = attempt
            self.selector.unregister(sock)
            sock.close()
            pending.append(_where(info))
        return pending


def dial(
    host: str, port: int, *, stack: str = "IPAuto", timeout: float, stagger: float = STAGGER
) -> socket.socket:
    """A connected, *blocking* TCP socket to ``host:port`` within ``timeout`` seconds.

    Raises :class:`TimeoutError` if the name cannot be resolved in time,
    :class:`socket.gaierror` if it cannot be resolved at all, and
    :class:`DialError` - naming every address tried and how each failed -
    when no address could be connected to.
    """
    deadline = time.monotonic() + timeout
    infos = interleave(resolve(host, port, stack, timeout))
    if not infos:
        raise DialError(f"{host} has no address in network stack {stack}", [], timed_out=False)
    with selectors.DefaultSelector() as selector:
        race = _Race(deque(infos), deadline, stagger, selector)
        try:
            winner = race.run()
        finally:
            pending = race.abandon()
    if winner is not None:
        winner.setblocking(True)
        return winner
    raise _verdict(timeout, race, pending)


def _verdict(timeout: float, race: _Race, pending: list[str]) -> DialError:
    """The :class:`DialError` that says what happened to every address."""
    told = [str(failure.strerror) for failure in race.failures]
    if pending:
        told.append(f"{', '.join(pending)}: no answer within {timeout:g}s")
    if race.order:
        told.append(f"{', '.join(_where(info) for info in race.order)}: not tried in time")
    return DialError("; ".join(told), race.failures, timed_out=race.expired)
