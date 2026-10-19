"""The session's and transport's rarer branches, driven one at a time.

Most of these are what a race or a failing link does, which the concurrency
tests reach only when the scheduler happens to cooperate. Here each is put
in place directly - a closed pipe, a mailbox already holding an answer, a
lock another thread holds - so the branch runs every time.
"""

from __future__ import annotations

import contextlib
import socket
import ssl
import threading
import time
from collections import deque
from collections.abc import Iterator
from pathlib import Path

import pytest
from xrdsec.testing.pki import PKI

import xrdclient
from tests.conftest import ok
from tests.test_machine import ready
from xrdclient.config import Config
from xrdclient.errors import ConnectionError as XrdConnectionError
from xrdclient.errors import ProtocolError, ServerError, WaitLimitError, kXR_NotAuthorized
from xrdclient.errors import TimeoutError as XrdTimeoutError
from xrdclient.proto import machine as m
from xrdclient.proto import requests as r
from xrdclient.proto.frames import Payload
from xrdclient.session import deadline as dl
from xrdclient.session.local import LocalFile
from xrdclient.session.router import Router, _Route
from xrdclient.session.sync import Session, _AwaitState, _Stalled
from xrdclient.transport.base import Transport
from xrdclient.transport.memory import MemoryTransport, pipe
from xrdclient.transport.sync import SocketTransport, _ready
from xrdclient.url import parse

URL = parse("root://srv.example.org:1094//data/f")


def _session(config: Config | None = None) -> tuple[Session, MemoryTransport]:
    """A ready session on a memory pipe, and the server's end of it."""
    transport, peer = pipe()
    return Session(transport, ready(), config or Config()), peer


def _closed_pipe() -> MemoryTransport:
    """A transport whose every send fails, as a dead link's does."""
    transport, _ = pipe()
    transport.close()
    return transport


class _Boom(BaseException):
    """Not an error a send is expected to raise: it must not be filed, only passed on."""


class _Exploding(MemoryTransport):
    """A link whose send raises something no request handles."""

    __slots__ = ()

    def send(self, data: bytes) -> None:
        raise _Boom


# ---------------------------------------------------------------------------
# Bring-up and data paths
# ---------------------------------------------------------------------------


def test_a_session_already_on_tls_locks_its_control_link():
    transport, _ = pipe()
    machine = ready()
    machine.tls_active = True
    assert 0 in Session(transport, machine, Config())._io_locks


def test_a_failed_flush_stops_the_tls_upgrade():
    session, _ = _session()
    session._t.close()
    session._m.submit(r.Ping())
    with pytest.raises(XrdConnectionError):
        session._upgrade()


def test_a_path_bound_after_the_session_closed_is_refused():
    session, _ = _session()
    session.close()
    machine = ready()
    machine.pathid = 2
    with pytest.raises(XrdConnectionError, match="closed while binding a path"):
        session._adopt_path(pipe()[0], machine)


def test_a_tls_data_path_gets_a_lock_of_its_own():
    session, _ = _session()
    machine = ready()
    machine.pathid, machine.tls_active = 2, True
    assert session._adopt_path(pipe()[0], machine) == 2
    assert 2 in session._io_locks


def test_a_refusal_after_a_proving_login_needs_no_explanation():
    session, _ = _session()
    session._m.mechanism = "gsi"
    refusal = ServerError(kXR_NotAuthorized, "no")
    session._explain_refusal(refusal)
    assert refusal.hint == ""


# ---------------------------------------------------------------------------
# Locks, starts and mailboxes
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _lock_held_elsewhere(session: Session) -> Iterator[None]:
    """The session lock, held by another thread for the length of the block."""
    taken, done = threading.Event(), threading.Event()

    def hold() -> None:
        with session._lock:
            taken.set()
            done.wait(10)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    taken.wait(10)
    try:
        yield
    finally:
        done.set()
        thread.join(10)


def test_waiting_for_the_lock_past_the_deadline_expires():
    session, _ = _session()
    with (
        _lock_held_elsewhere(session),
        xrdclient.deadline(0.05),
        pytest.raises(dl.OperationExpiredError, match="waiting its turn"),
        session._held(),
    ):
        pass


def test_something_other_than_a_send_failure_takes_the_mailbox_down():
    session = Session(_Exploding(deque(), deque()), ready(), Config())
    with pytest.raises(_Boom):
        session._start(r.Ping())
    assert session._inbox == {}


def test_a_finished_waiter_releases_its_stream():
    session, _ = _session()
    sid, box = session._start(r.Ping())
    session._forget(sid, box)
    assert sid not in session._inbox


def test_a_stream_that_is_no_longer_the_callers_is_left_alone():
    session, _ = _session()
    session._release(99, [])
    assert session._inbox == {}


def test_events_are_read_into_the_mailbox_the_request_registered():
    session, peer = _session()
    sid, _ = session._start(r.Ping())
    peer.send(ok(sid, b""))
    (event,) = session._events_for(sid)
    assert isinstance(event, m.Completed)


# ---------------------------------------------------------------------------
# kXR_wait and kXR_waitresp
# ---------------------------------------------------------------------------


def _state(parked: float = 0.0) -> _AwaitState:
    return _AwaitState(waits=0, parked=parked, streamed=0, deadline=None)


def test_a_wait_cut_short_by_an_answer_does_not_resend():
    session, _ = _session()
    box: list[m.Event] = [m.Completed(4, r.Ping(), b"")]
    state = _state()
    state.streamed = 5
    session._wait_event(m.Waiting(4, r.Ping(), 0.01), 4, state, box)
    assert state.streamed == 5  # not reset: nothing was sent again


def test_a_deferred_answer_past_the_wait_budget_is_given_up():
    session, _ = _session(Config(wait_budget=10))
    event = m.Waiting(4, r.Ping(), 5, resend=False)
    with pytest.raises(WaitLimitError, match="already waited out"):
        session._wait_for_answer(event, 4, _state(parked=10), [])


def test_a_deferred_answer_with_no_budget_waits_unbounded():
    session, _ = _session(Config(wait_budget=0))
    state = _state()
    session._wait_for_answer(m.Waiting(4, r.Ping(), 0, resend=False), 4, state, [])
    assert state.parked == 0 and state.parked_until is None


# ---------------------------------------------------------------------------
# Readers and followers
# ---------------------------------------------------------------------------


def test_waiting_on_a_data_path_that_is_gone_fails():
    session, _ = _session()
    with pytest.raises(XrdConnectionError, match="data path 2"):
        session._check_link(2)


def test_a_follower_with_no_clock_sleeps_until_woken():
    session, _ = _session()

    def wake() -> None:
        while not session._parked:
            time.sleep(0.001)
        with session._lock:
            session._cond.notify_all()

    thread = threading.Thread(target=wake, daemon=True)
    thread.start()
    with session._lock:
        session._follow(0, None, None)
    thread.join(10)


def test_a_follower_out_of_stall_time_breaks_the_control_link():
    session, _ = _session()
    with pytest.raises(_Stalled):
        session._follow(0, time.monotonic() - 1, None)
    assert session._broken


def test_a_link_shut_down_under_another_reader_waits_for_it():
    session, _ = _session()
    link = pipe()[0]
    session._doomed = [(2, link)]
    session._reading = {2}
    session._let_go(0)
    assert session._doomed == [(2, link)]
    assert not link.closed


def test_a_stalled_data_path_costs_only_itself():
    session, _ = _session()
    session._paths[2] = pipe()[0]
    session._stalled_out(2, XrdTimeoutError("quiet"), [])
    assert 2 not in session._paths
    assert not session._broken


def test_a_data_path_that_already_went_is_not_failed_twice():
    session, _ = _session()
    session._link_failed(2, XrdConnectionError("gone"), None)
    assert not session._broken


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def test_a_failed_control_link_skips_the_rest_of_its_queue():
    session, _ = _session()
    bound, stale = _closed_pipe(), _closed_pipe()
    session._paths[2] = bound
    later = pipe()[0]
    batch: list[tuple[int, Transport, list[Payload]]] = [
        (0, _closed_pipe(), [b"x"]),
        (0, later, [b"y"]),
        (2, bound, [b"z"]),
        (3, stale, [b"w"]),
    ]
    failure = session._send_batch(batch)
    assert isinstance(failure, XrdConnectionError)
    assert later.sent() == b""  # skipped: the control link was already gone
    assert 2 not in session._paths  # its data path was closed for the failed send


# ---------------------------------------------------------------------------
# Receiving
# ---------------------------------------------------------------------------


class _Hook(MemoryTransport):
    """A link whose receive runs ``action`` first."""

    __slots__ = ("action", "readable")

    def __init__(self) -> None:
        super().__init__(deque(), deque())
        self.action: object = None
        self.readable = True

    def receive(self, size: int = 65536) -> bytes:
        if callable(self.action):
            self.action()
        return super().receive(size)

    def wait_readable(self, timeout: float | None) -> bool:
        return self.readable


def test_reading_a_data_path_that_is_gone_fails():
    session, _ = _session()
    with session._lock, pytest.raises(XrdConnectionError, match="data path 2"):
        session._receive(2, None)


def test_a_read_that_fails_after_the_session_closed_under_it_is_nothing():
    link = _Hook()
    session = Session(link, ready(), Config())

    def close_and_fail() -> None:
        session._m.state = m.State.CLOSED
        raise XrdConnectionError("shut down")

    link.action = close_and_fail
    with session._lock:
        assert session._receive(0, None) is None


def test_a_read_that_lands_after_the_session_closed_under_it_is_nothing():
    """The read succeeds, but the session closed while the lock was let go.

    Otherwise reached only when a close races a read, which the suites do
    not always hit (Linux, Python 3.11: one run in several).
    """
    link = _Hook()
    session = Session(link, ready(), Config())

    def close() -> None:
        session._m.state = m.State.CLOSED

    link.action = close
    with session._lock:
        assert session._receive(0, None) is None


def test_a_tls_read_that_waits_out_the_link_timeout_times_out():
    link = _Hook()
    link.readable = False
    session = Session(link, ready(), Config(request_timeout=0.01))
    session._io_locks[0] = threading.Lock()
    with session._lock, pytest.raises(XrdTimeoutError, match="timed out"):
        session._receive(0, None)


def test_a_reader_that_cannot_send_what_is_queued_fails():
    session, _ = _session()
    session._t.close()
    session._outq.append((0, session._t, [b"x"]))
    with session._lock, pytest.raises(XrdConnectionError):
        session._read_unlocked(0, session._t, None)


def test_a_session_whose_goodbye_cannot_be_framed_still_closes(monkeypatch):
    def refuse(self: m.SessionMachine, *, graceful: bool = True) -> None:
        raise ProtocolError("no stream id left")

    session, _ = _session()
    monkeypatch.setattr(m.SessionMachine, "close", refuse)
    session.close()
    assert session._m.state is m.State.CLOSED


# ---------------------------------------------------------------------------
# A local file and the router
# ---------------------------------------------------------------------------


def test_a_local_file_has_no_data_paths(tmp_path):
    assert LocalFile(str(tmp_path / "f"), Config()).data_paths == []


def test_a_router_takes_new_settings():
    router = Router(URL, Config())
    fresh = Config(connect_retries=0)
    router.config = fresh
    assert router.config is fresh


def _local_file(tmp_path: Path) -> str:
    path = tmp_path / "local.bin"
    path.write_bytes(b"data")
    return str(path)


def test_a_local_redirect_for_a_request_the_router_has_moved_on_from(tmp_path):
    """Another thread moved the router meanwhile: only this request goes local."""
    path = _local_file(tmp_path)
    router = Router(URL, Config(local_redirect="always"))
    other, _ = _session()
    route = _Route(None, session=other)
    result = router._go_local(r.Stat(path), route, f"file://localhost{path}", path)
    assert result is not None
    assert router.url == URL


def test_a_local_redirect_before_any_connection_moves_the_router(tmp_path):
    path = _local_file(tmp_path)
    router = Router(URL, Config(local_redirect="always"))
    assert router._go_local(r.Stat(path), _Route(None), "file://localhost", path) is None
    assert router.url.scheme == "file"


def test_going_home_names_the_failed_server_in_every_path():
    router = Router(URL, Config(), sticky=False)
    request = r.Prepare(["/a", "/b"])
    route = _Route({"paths": ["/a", "/b"]}, target=parse("root://data.example.org:1094//a"))
    router._go_home(request, route, "lost", spoiled=False)
    assert all("tried=data.example.org" in p for p in request.paths)


def test_a_router_another_thread_moved_is_not_moved_home():
    router = Router(URL, Config())
    route = _Route(None, home=parse("root://home.example.org:1094//"))
    router._return_home(route, parse("root://elsewhere.example.org:1094//"), spoiled=False)
    assert router.url == URL


# ---------------------------------------------------------------------------
# The socket transport
# ---------------------------------------------------------------------------


def test_a_closed_socket_counts_as_ready():
    sock = socket.socket()
    sock.close()
    assert _ready(sock, 1, 0)


def test_a_send_that_cannot_drain_times_out():
    left, right = socket.socketpair()
    with left, right:
        left.settimeout(0.05)
        transport = SocketTransport(left, "peer", 1094)
        with pytest.raises(XrdTimeoutError, match="timed out"):
            transport.send_buffers([b"x" * (64 << 20)])


@pytest.fixture
def tls_pair(pki: PKI) -> Iterator[tuple[ssl.SSLSocket, ssl.SSLSocket]]:
    """Two ends of a socket pair with a finished TLS handshake: client, server."""
    server_context = pki.server_context(require_client=False)
    client_context = pki.client_context(anonymous=True)
    left, right = socket.socketpair()
    served: list[ssl.SSLSocket] = []
    thread = threading.Thread(
        target=lambda: served.append(server_context.wrap_socket(right, server_side=True)),
        daemon=True,
    )
    thread.start()
    client = client_context.wrap_socket(left, server_hostname="localhost")
    thread.join(10)
    try:
        yield client, served[0]
    finally:
        client.close()
        served[0].close()


def test_tls_sends_several_buffers_as_records(tls_pair):
    client, server = tls_pair
    SocketTransport(client, "localhost", 0).send_buffers([b"head", b"body"])
    received = b""
    while len(received) < 8:
        received += server.recv(8)
    assert received == b"headbody"


def test_decrypted_bytes_already_read_count_as_readable(tls_pair):
    client, server = tls_pair
    server.sendall(b"0123456789")
    transport = SocketTransport(client, "localhost", 0)
    assert transport.receive(1) == b"0"
    assert client.pending()
    assert transport.wait_readable(0)
