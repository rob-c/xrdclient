"""Connection setup: resolution in budget, Happy Eyeballs, keepalive, families.

Real loopback sockets throughout, as in ``test_transport``: what is being
tested is how the standard library's non-blocking connect behaves, and a mock
would only test the mock. The one thing a test cannot get from loopback - an
address that neither answers nor refuses - is a socket that is never ready to
write, which is what a black-holed SYN looks like to the selector.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import socket
import threading
import time
from collections.abc import Iterator
from typing import cast

import pytest

from xrdclient.config import Config
from xrdclient.errors import ConnectionError as XrdConnectionError
from xrdclient.errors import TimeoutError as XrdTimeoutError
from xrdclient.transport import _dial
from xrdclient.transport import sync as transport_sync
from xrdclient.transport.sync import SocketTransport

V4 = socket.AF_INET
V6 = socket.AF_INET6


def row(family: socket.AddressFamily, host: str, port: int = 1094) -> _dial.AddrInfo:
    sockaddr: _dial.SockAddr = (host, port) if family == V4 else (host, port, 0, 0)
    return (family, socket.SOCK_STREAM, 6, "", sockaddr)


@contextlib.contextmanager
def listening() -> Iterator[int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    try:
        port: int = sock.getsockname()[1]
        yield port
    finally:
        sock.close()


def refused_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port: int = sock.getsockname()[1]
    sock.close()
    return port


# -- resolution -----------------------------------------------------------------


def test_interleave_alternates_families_starting_with_the_first():
    infos = [
        row(V6, "::1"),
        row(V6, "::2"),
        row(V4, "10.0.0.1"),
        row(V4, "10.0.0.2"),
        row(V6, "::3"),
    ]
    hosts = [info[4][0] for info in _dial.interleave(infos)]
    assert hosts == ["::1", "10.0.0.1", "::2", "10.0.0.2", "::3"]
    assert _dial.interleave([]) == []


def test_a_numeric_host_is_resolved_without_a_thread(monkeypatch):
    monkeypatch.setattr(threading, "Thread", lambda *a, **k: pytest.fail("threaded"))
    found = _dial.resolve("127.0.0.1", 1094, "IPAuto", 1.0)
    assert [info[4] for info in found] == [("127.0.0.1", 1094)]


def test_a_resolver_that_hangs_is_abandoned_at_the_budget(monkeypatch):
    release = threading.Event()

    def stuck(*args):
        release.wait(5)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", stuck)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match=r"resolving storage\.example took longer"):
            _dial.resolve("storage.example", 1094, "IPAuto", 0.1)
    finally:
        release.set()
    assert time.monotonic() - started < 2


def test_a_resolver_failure_comes_back_as_it_was(monkeypatch):
    def unknown(*args):
        raise socket.gaierror(socket.EAI_NONAME, "no such host")

    monkeypatch.setattr(socket, "getaddrinfo", unknown)
    with pytest.raises(socket.gaierror):
        _dial.resolve("nowhere.example", 1094, "IPAuto", 1.0)


@pytest.mark.parametrize(
    ("stack", "family"), [("IPAuto", 0), ("ipv4", V4), ("IPv6", V6), ("IPv4Mapped6", 0)]
)
def test_the_network_stack_picks_the_families_asked_for(monkeypatch, stack, family):
    asked = []

    def lookup(host, port, fam, kind):
        asked.append(fam)
        return [row(V4, "192.0.2.1"), row(V4, "192.0.2.1")]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    found = _dial.resolve("storage.example", 1094, stack, 1.0)
    assert asked == [family]
    assert len(found) == 1  # duplicates folded
    if stack == "IPv4Mapped6":
        assert found[0][0] == V6 and found[0][4] == ("::ffff:192.0.2.1", 1094, 0, 0)


def test_an_unknown_network_stack_is_refused():
    with pytest.raises(ValueError, match="IPAuto, IPv4, IPv6, IPv4Mapped6"):
        _dial.resolve("127.0.0.1", 1094, "IPv5", 1.0)


def test_a_mapped_address_leaves_an_ipv6_row_alone():
    original = row(V6, "::1")
    assert _dial._mapped(original) is original


# -- racing ---------------------------------------------------------------------


def test_dial_connects_and_hands_back_a_blocking_socket():
    with listening() as port:
        sock = _dial.dial("127.0.0.1", port, timeout=2.0)
        with sock:
            assert sock.getblocking()
            assert sock.getpeername()[1] == port


def test_a_refused_address_moves_straight_on_to_the_next(monkeypatch):
    with listening() as port:
        dead = refused_port()
        monkeypatch.setattr(
            _dial, "resolve", lambda *a: [row(V4, "127.0.0.1", dead), row(V4, "127.0.0.1", port)]
        )
        started = time.monotonic()
        with _dial.dial("storage", port, timeout=5.0, stagger=3.0) as sock:
            assert sock.getpeername()[1] == port
        assert time.monotonic() - started < 2.5  # did not wait out the stagger


def _black_hole(info: _dial.AddrInfo) -> tuple[socket.socket, int]:
    """A connect that never completes: a socket whose send buffer is full."""
    near, far = socket.socketpair()
    near.setblocking(False)
    with contextlib.suppress(BlockingIOError):
        while True:
            near.send(b"x" * 65536)
    _held.append(far)
    return near, errno.EINPROGRESS


_held: list[socket.socket] = []


@pytest.fixture
def black_hole(monkeypatch):
    real = _dial._start

    def start(info):
        return _black_hole(info) if info[4][0] == "192.0.2.1" else real(info)

    monkeypatch.setattr(_dial, "_start", start)
    yield
    while _held:
        _held.pop().close()


def test_a_silent_address_is_raced_by_the_next_after_the_stagger(monkeypatch, black_hole):
    with listening() as port:
        monkeypatch.setattr(
            _dial, "resolve", lambda *a: [row(V4, "192.0.2.1", port), row(V4, "127.0.0.1", port)]
        )
        started = time.monotonic()
        with _dial.dial("storage", port, timeout=5.0, stagger=0.05) as sock:
            assert sock.getpeername()[1] == port
        assert time.monotonic() - started < 2


def test_nothing_answering_in_time_is_a_timeout_naming_every_address(monkeypatch, black_hole):
    dead = refused_port()
    monkeypatch.setattr(
        _dial,
        "resolve",
        lambda *a: [
            row(V4, "127.0.0.1", dead),
            row(V4, "192.0.2.1", 1094),
            row(V4, "192.0.2.1", 1095),
        ],
    )
    with pytest.raises(_dial.DialError) as caught:
        _dial.dial("storage", 1094, timeout=0.2, stagger=5.0)
    error = caught.value
    assert error.timed_out
    told = str(error.strerror)
    assert f"127.0.0.1:{dead}" in told
    assert "192.0.2.1:1094: no answer within 0.2s" in told
    assert "192.0.2.1:1095: not tried in time" in told
    assert len(error.failures) == 1


def test_every_address_refusing_is_a_failure_naming_each(monkeypatch):
    dead = refused_port()
    monkeypatch.setattr(
        _dial, "resolve", lambda *a: [row(V4, "127.0.0.1", dead), row(V6, "::1", dead)]
    )
    with pytest.raises(_dial.DialError) as caught:
        _dial.dial("storage", dead, timeout=2.0)
    assert not caught.value.timed_out
    assert len(caught.value.failures) == 2
    assert f"[::1]:{dead}" in str(caught.value.strerror)


def test_an_attempt_that_fails_on_the_spot_is_recorded(monkeypatch):
    def broken(info):
        raise OSError(errno.EAFNOSUPPORT, "no such family here")

    monkeypatch.setattr(_dial, "_start", broken)
    monkeypatch.setattr(_dial, "resolve", lambda *a: [row(V6, "::1")])
    with pytest.raises(_dial.DialError) as caught:
        _dial.dial("storage", 1094, timeout=1.0)
    assert caught.value.failures[0].errno == errno.EAFNOSUPPORT


def test_a_connect_that_finishes_at_once_wins_at_once(monkeypatch):
    with listening() as port:
        sock = socket.create_connection(("127.0.0.1", port))
        monkeypatch.setattr(_dial, "_start", lambda info: (sock, 0))
        monkeypatch.setattr(_dial, "resolve", lambda *a: [row(V4, "127.0.0.1", port)])
        with _dial.dial("storage", port, timeout=1.0) as got:
            assert got is sock


def test_an_immediate_refusal_closes_the_socket(monkeypatch):
    made = []
    real = _dial._start

    def refused_now(info):
        sock, _ = real(info)
        made.append(sock)
        return sock, errno.ECONNREFUSED

    monkeypatch.setattr(_dial, "_start", refused_now)
    monkeypatch.setattr(_dial, "resolve", lambda *a: [row(V4, "127.0.0.1", refused_port())])
    with pytest.raises(_dial.DialError):
        _dial.dial("storage", 1, timeout=1.0)
    assert made[0].fileno() == -1


def test_a_host_with_no_address_in_the_stack_says_so(monkeypatch):
    monkeypatch.setattr(_dial, "resolve", lambda *a: [])
    with pytest.raises(_dial.DialError, match="no address in network stack IPv6"):
        _dial.dial("storage", 1094, stack="IPv6", timeout=1.0)


def test_a_mapped_ipv4_address_connects_over_ipv6():
    if not socket.has_ipv6:
        pytest.skip("environmental: this Python was built without IPv6")
    with listening() as port:
        try:
            sock = _dial.dial("127.0.0.1", port, stack="IPv4Mapped6", timeout=2.0)
        except _dial.DialError as exc:
            pytest.skip(f"environmental: IPv6 is disabled on this host: {exc}")
        with sock:
            assert sock.family == V6


def test_an_exception_while_starting_closes_the_socket(monkeypatch):
    made = []

    class Doomed(socket.socket):
        def setblocking(self, flag: bool) -> None:
            made.append(self)
            raise KeyboardInterrupt

    monkeypatch.setattr(socket, "socket", Doomed)
    with pytest.raises(KeyboardInterrupt):
        _dial._start(row(V4, "127.0.0.1", 1))
    assert made[0].fileno() == -1


# -- the transport on top ---------------------------------------------------------


def test_the_transport_reports_every_address_and_chains_them(monkeypatch):
    dead = refused_port()
    monkeypatch.setattr(
        _dial, "resolve", lambda *a: [row(V4, "127.0.0.1", dead), row(V6, "::1", dead)]
    )
    with pytest.raises(XrdConnectionError, match=f"cannot connect to storage:{dead}") as caught:
        SocketTransport.connect("storage", dead, Config(connect_timeout=2.0))
    assert "127.0.0.1" in str(caught.value) and "[::1]" in str(caught.value)
    assert isinstance(caught.value.__cause__, ConnectionRefusedError)


def test_the_transport_reports_a_timeout_as_one(monkeypatch, black_hole):
    monkeypatch.setattr(_dial, "resolve", lambda *a: [row(V4, "192.0.2.1", 1094)])
    with pytest.raises(XrdTimeoutError, match="connecting to storage:1094 timed out") as caught:
        SocketTransport.connect("storage", 1094, Config(connect_timeout=0.1))
    assert caught.value.__cause__ is None  # nothing failed outright


def test_the_transport_reports_a_name_that_does_not_resolve(monkeypatch):
    def unknown(*args):
        raise socket.gaierror(socket.EAI_NONAME, "no such host")

    monkeypatch.setattr(socket, "getaddrinfo", unknown)
    with pytest.raises(XrdConnectionError, match=r"cannot connect to nowhere\.example:1094"):
        SocketTransport.connect("nowhere.example", 1094, Config(connect_timeout=1.0))


def test_the_transport_honours_the_configured_stack(monkeypatch):
    seen = []

    def dial(host, port, *, stack, timeout):
        seen.append((stack, timeout))
        raise TimeoutError("stop here")

    monkeypatch.setattr(_dial, "dial", dial)
    with pytest.raises(XrdTimeoutError):
        SocketTransport.connect("h", 1, Config(network_stack="IPv6", connect_timeout=7.0))
    assert seen == [("IPv6", 7.0)]


# -- keepalive and locality -------------------------------------------------------


def _option(sock: socket.socket, name: str) -> int | None:
    option = getattr(socket, name, None)
    return None if option is None else sock.getsockopt(socket.IPPROTO_TCP, option)


def test_a_connection_probes_after_the_keepalive_interval():
    with listening() as port:
        transport = SocketTransport.connect("127.0.0.1", port, Config(keepalive_interval=90))
        try:
            sock = transport._sock
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
            idle = _option(sock, "TCP_KEEPIDLE") or _option(sock, "TCP_KEEPALIVE")
            assert idle == 90
            assert _option(sock, "TCP_KEEPINTVL") in (None, 30)
            assert _option(sock, "TCP_KEEPCNT") in (None, transport_sync.KEEPALIVE_PROBES)
        finally:
            transport.close()


def test_a_zero_keepalive_interval_turns_keepalive_off():
    with listening() as port:
        transport = SocketTransport.connect("127.0.0.1", port, Config(keepalive_interval=0))
        try:
            assert not transport._sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
        finally:
            transport.close()


def test_a_kernel_that_refuses_a_keepalive_timing_keeps_its_own(monkeypatch, caplog):
    class Picky:
        def __init__(self) -> None:
            self.set: list[tuple[int, int]] = []

        def setsockopt(self, level: int, option: int, value: int) -> None:
            if level == socket.IPPROTO_TCP:
                raise OSError(errno.ENOPROTOOPT, "not here")
            self.set.append((option, value))

    sock = Picky()
    with caplog.at_level(logging.DEBUG, logger="xrdclient.transport.sync"):
        # keepalive only calls setsockopt, which is all the fake has.
        transport_sync.keepalive(cast(socket.socket, sock), 10)
    assert sock.set == [(socket.SO_KEEPALIVE, 1)]
    assert "keeping the system's" in caplog.text


def test_a_platform_without_a_timing_option_skips_it(monkeypatch):
    for name in ("TCP_KEEPIDLE", "TCP_KEEPALIVE", "TCP_KEEPINTVL", "TCP_KEEPCNT"):
        monkeypatch.delattr(socket, name, raising=False)
    set_options = []

    class Recording:
        def setsockopt(self, level, option, value):
            set_options.append((level, option))

    # keepalive only calls setsockopt, which is all the fake has.
    transport_sync.keepalive(cast(socket.socket, Recording()), 10)
    assert set_options == [(socket.SOL_SOCKET, socket.SO_KEEPALIVE)]


def test_a_loopback_peer_is_local():
    with listening() as port:
        transport = SocketTransport.connect("127.0.0.1", port, Config())
        try:
            assert transport.peer_is_local
        finally:
            transport.close()


def test_a_closed_connection_is_not_known_to_be_local():
    with listening() as port:
        transport = SocketTransport.connect("127.0.0.1", port, Config())
        transport.close()
        assert not transport.peer_is_local


@pytest.mark.parametrize(
    ("address", "local"),
    [
        ("127.0.0.2", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("fe80::1%en0", False),
        ("192.0.2.1", False),
        ("not-an-address", False),
    ],
)
def test_loopback_is_recognised_in_every_spelling(address, local):
    assert transport_sync._is_loopback(address) is local


def test_a_peer_at_this_hosts_own_address_is_local():
    class Mirror:
        def getpeername(self):
            return ("192.0.2.7", 1094)

        def getsockname(self):
            return ("192.0.2.7", 50000)

    transport = SocketTransport.__new__(SocketTransport)
    # peer_is_local only asks for the two names, which is all the fake has.
    transport._sock = cast(socket.socket, Mirror())
    assert transport.peer_is_local
