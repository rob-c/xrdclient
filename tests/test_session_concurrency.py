"""Many callers on one session: requests in flight together, failures for all.

A :class:`~xrdclient.session.sync.Session` lets any number of threads have a
request on its connection at once (leader/follower: one reader per link,
everyone else asleep until their mailbox fills). These tests pin the
interleavings that design exists for. They synchronise on barriers and
events, never on sleeps: where a test needs to know a thread has reached a
point inside the session, it wraps the method that point is.

Two kinds of peer. :class:`~xrdclient.testing.FakeServer` answers one request
at a time per connection, as a stock server mostly does. :class:`_Peer` is a
socket the test answers itself, for what no polite server does - reading
every request before answering any, and answering out of order in pieces.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

import xrdclient
from tests.conftest import frame, ok
from tests.test_machine import ready
from xrdclient.config import Config
from xrdclient.errors import ConnectionError as XrdConnectionError
from xrdclient.errors import ProtocolError
from xrdclient.proto import constants as c
from xrdclient.proto import requests as r
from xrdclient.session.sync import Session
from xrdclient.testing import FakeServer
from xrdclient.transport.base import Buffer, coalesce
from xrdclient.transport.sync import SocketTransport, _advance

#: Long enough that a pass never comes near it, short enough that a hang is
#: a failure rather than a stuck suite.
PATIENCE = 10.0

_REQ = struct.Struct(">HH16sI")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Breakable(SocketTransport):
    """A socket whose next read fails, once :attr:`fault` names why."""

    __slots__ = ("fault",)

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "peer", 1094)
        self.fault = ""

    def receive(self, size: int = 65536) -> bytes:
        if self.fault:
            raise XrdConnectionError(self.fault)
        return super().receive(size)


class _Peer:
    """The far end of a socketpair, read and answered by the test itself."""

    def __init__(self, config: Config | None = None) -> None:
        near, far = socket.socketpair()
        far.settimeout(PATIENCE)
        near.settimeout(300.0)
        self.sock = far
        # Requests are read through one buffered reader, which loops until a
        # frame is complete (a stream socket may deliver it in pieces).
        self.reader = far.makefile("rb")
        self.config = config or Config(username="tester", auth_order=("host",))
        self.link = _Breakable(near)
        self.session = Session(self.link, ready(), self.config)

    def requests(self, count: int) -> list[tuple[int, int, bytes, bytes]]:
        """The next ``count`` request frames: ``(streamid, opcode, params, body)``."""
        out = []
        for _ in range(count):
            sid, opcode, params, dlen = _REQ.unpack(self._exact(_REQ.size))
            out.append((sid, opcode, params, self._exact(dlen)))
        return out

    def _exact(self, size: int) -> bytes:
        """Exactly ``size`` bytes of what the client sent; ConnectionError if it hung up first."""
        data = self.reader.read(size)
        if len(data) < size:
            raise ConnectionError("client hung up")
        return data

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def close(self) -> None:
        self.session.close()
        self.reader.close()
        self.sock.close()


@contextmanager
def _peer(config: Config | None = None) -> Iterator[_Peer]:
    peer = _Peer(config)
    try:
        yield peer
    finally:
        peer.close()


class _Threads:
    """Run callables on threads, keeping each one's result or exception."""

    def __init__(self) -> None:
        self.results: dict[int, object] = {}
        self.errors: dict[int, BaseException] = {}
        self._threads: list[threading.Thread] = []

    def start(self, index: int, call: Callable[[], object]) -> None:
        def run() -> None:
            try:
                self.results[index] = call()
            except BaseException as exc:
                self.errors[index] = exc

        thread = threading.Thread(target=run, daemon=True)
        self._threads.append(thread)
        thread.start()

    def join(self, timeout: float = PATIENCE) -> float:
        """Wait for all of them; how long that took. Fails on a hang."""
        started = time.monotonic()
        for thread in self._threads:
            thread.join(max(timeout - (time.monotonic() - started), 0.0))
            assert not thread.is_alive(), "a thread is still blocked in the session"
        return time.monotonic() - started


def _when_parked(
    monkeypatch: pytest.MonkeyPatch, session: Session, longer_than: float
) -> threading.Event:
    """An event set once some thread sleeps on the session for ``longer_than`` seconds.

    How a test knows a ``kXR_wait`` is being sat out: that is the only sleep
    that long.
    """
    parked = threading.Event()
    real = session._park

    def park(timeout: float | None) -> None:
        if timeout is not None and timeout > longer_than:
            parked.set()
        real(timeout)

    monkeypatch.setattr(session, "_park", park)
    return parked


def _when_started(
    monkeypatch: pytest.MonkeyPatch, session: Session, count: int
) -> threading.Barrier:
    """A barrier the test joins to know ``count`` requests are on the wire."""
    barrier = threading.Barrier(count + 1, timeout=PATIENCE)
    real = session._start

    def start(*args: Any, **kwargs: Any) -> Any:
        started = real(*args, **kwargs)
        barrier.wait()
        return started

    monkeypatch.setattr(session, "_start", start)
    return barrier


def _query_arg(body: bytes) -> str:
    return body.rstrip(b"\x00").decode()


# ---------------------------------------------------------------------------
# Requests really are in flight together
# ---------------------------------------------------------------------------


def test_every_request_is_on_the_wire_before_any_is_answered():
    """Eight threads, one connection: all eight go out, then come back reversed.

    The peer reads every request before it answers one, so a session that
    let only one request out at a time would never get past the first. The
    answers then come back in reverse order, each in two instalments, the
    first instalments of all eight before any final one - so every reader
    files pieces for other streams before its own.
    """
    count = 8
    with _peer() as peer:
        barrier = threading.Barrier(count, timeout=PATIENCE)
        threads = _Threads()
        for index in range(count):

            def ask(index: int = index) -> bytes:
                barrier.wait()
                return peer.session.execute(r.Query(c.kXR_Qconfig, f"key{index}")).data

            threads.start(index, ask)
        asked = sorted(peer.requests(count), key=lambda req: _query_arg(req[3]))
        sids = [sid for sid, *_ in asked]
        assert len(set(sids)) == count  # every one on a stream of its own
        for index, sid in reversed(list(enumerate(sids))):
            peer.send(frame(sid, c.kXR_oksofar, f"answer-{index}:".encode()))
        for index, sid in reversed(list(enumerate(sids))):
            peer.send(ok(sid, f"{index}".encode()))
        threads.join()
        assert not threads.errors
        assert threads.results == {i: f"answer-{i}:{i}".encode() for i in range(count)}
        assert peer.session._inbox == {}  # every mailbox taken down
        assert peer.session.machine.in_flight == 0


def test_many_threads_stat_and_read_on_one_session(server, config):
    """A real server, many callers, every answer the right one."""
    files = {f"/data/f{i}": bytes([65 + i]) * (1000 + i) for i in range(6)}
    for path, data in files.items():
        server.add_file(path, data)
    count, rounds = 6, 20
    with Session.connect(server.url, config=config) as session:
        handles = {path: session.execute(r.Open(path, c.kXR_open_read)).data[:4] for path in files}
        barrier = threading.Barrier(count, timeout=PATIENCE)
        threads = _Threads()
        for index, (path, data) in enumerate(files.items()):

            def work(path: str = path, data: bytes = data) -> int:
                barrier.wait()
                for turn in range(rounds):
                    if turn % 2:
                        info = session.execute(r.Stat(path)).data
                        assert info.split()[1] == str(len(data)).encode()
                    else:
                        got = session.execute(r.Read(handles[path], turn, 64)).data
                        assert got == data[turn : turn + 64]
                return rounds

            threads.start(index, work)
        threads.join()
        assert not threads.errors
        assert session.machine.in_flight == 0
        assert not session.broken


def test_concurrent_writes_of_large_payloads_land_whole(server, config):
    """Payloads sent as views of the caller's buffers, from four threads at once."""
    payloads = {f"/data/w{i}": bytes(range(256)) * (1024 + 256 * i) for i in range(4)}
    with Session.connect(server.url, config=config) as session:
        barrier = threading.Barrier(len(payloads), timeout=PATIENCE)
        threads = _Threads()
        for index, (path, data) in enumerate(payloads.items()):

            def write(path: str = path, data: bytes = data) -> None:
                handle = session.execute(r.Open(path, c.kXR_new | c.kXR_open_updt, 0o644)).data
                barrier.wait()
                view = memoryview(bytearray(data))
                for at in range(0, len(data), 100_000):
                    session.execute(r.Write(handle[:4], at, view[at : at + 100_000]))
                session.execute(r.Close(handle[:4]))

            threads.start(index, write)
        threads.join()
        assert not threads.errors
    for path, data in payloads.items():
        assert server.contents(path) == data


# ---------------------------------------------------------------------------
# Waiting without holding anyone up
# ---------------------------------------------------------------------------


def _wait_once(seconds: int) -> tuple[Callable[..., Iterator[bytes]], threading.Event]:
    """A query handler that answers ``kXR_wait`` the first time, ``ok`` after."""
    asked = threading.Event()

    def handler(conn, sid, params, body):
        if not asked.is_set():
            asked.set()
            yield frame(sid, c.kXR_wait, struct.pack(">i", seconds) + b"busy\x00")
            return
        yield frame(sid, c.kXR_ok, b"done")

    return handler, asked


def test_a_kxr_wait_on_one_request_does_not_block_another(server, config, monkeypatch):
    handler, _ = _wait_once(30)
    server.handlers[c.kXR_query] = handler
    with Session.connect(server.url, config=config) as session:
        parked = _when_parked(monkeypatch, session, longer_than=10)
        threads = _Threads()
        threads.start(0, lambda: session.execute(r.Query(c.kXR_Qconfig, "x")))
        assert parked.wait(PATIENCE)  # sitting out its 30 seconds
        with xrdclient.deadline(PATIENCE):
            assert session.execute(r.Ping()).data == b""
            assert session.execute(r.Stat("/data/a.root")).data
        started = time.monotonic()
        session.close()
        assert time.monotonic() - started < 2.0
        assert threads.join() < 2.0  # the 30 s wait was cut short
    assert isinstance(threads.errors[0], XrdConnectionError)
    assert "waited to be resent" in str(threads.errors[0])


def test_a_kxr_wait_is_resent_and_answered_while_others_come_and_go(server, config, monkeypatch):
    handler, _ = _wait_once(1)
    server.handlers[c.kXR_query] = handler
    with Session.connect(server.url, config=config) as session:
        parked = _when_parked(monkeypatch, session, longer_than=0.5)
        threads = _Threads()
        threads.start(0, lambda: session.execute(r.Query(c.kXR_Qconfig, "x")).data)
        assert parked.wait(PATIENCE)
        for _ in range(5):
            session.execute(r.Ping())
        threads.join()
    assert threads.results == {0: b"done"}
    assert server.seen.count(c.kXR_query) == 2


def test_a_deferred_answer_does_not_block_another_request(server, config):
    """``kXR_waitresp``: the request sleeps on the link; others use it meanwhile."""
    asked = threading.Event()

    def later(conn, sid, params, body):
        asked.set()
        yield frame(sid, c.kXR_waitresp, struct.pack(">i", 60))

    server.handlers[c.kXR_query] = later
    with Session.connect(server.url, config=config) as session:
        threads = _Threads()
        threads.start(0, lambda: session.execute(r.Query(c.kXR_Qconfig, "x")))
        assert asked.wait(PATIENCE)
        with xrdclient.deadline(PATIENCE):
            for _ in range(3):
                session.execute(r.Ping())
        session.close()  # the deferred one is woken, wherever it was waiting
        assert threads.join() < 2.0
    assert isinstance(threads.errors[0], XrdConnectionError)


def test_a_blocking_chunk_callback_does_not_block_other_requests(server, config):
    """``on_chunk`` runs holding nothing: the session reads on for others meanwhile."""
    server.chunk_reads = 4
    inside, carry_on = threading.Event(), threading.Event()
    pieces: list[bytes] = []

    def consume(piece: bytes) -> None:
        pieces.append(piece)
        if len(pieces) == 1:
            inside.set()
            assert carry_on.wait(PATIENCE)

    with Session.connect(server.url, config=config) as session:
        handle = session.execute(r.Open("/data/a.root", c.kXR_open_read)).data[:4]
        threads = _Threads()
        threads.start(0, lambda: session.execute(r.Read(handle, 0, 64), on_chunk=consume).data)
        assert inside.wait(PATIENCE)
        with xrdclient.deadline(PATIENCE):
            session.execute(r.Ping())  # reads the rest of the stalled read on the way
        carry_on.set()
        threads.join()
    assert threads.results == {0: b"hello world"}
    assert b"".join(pieces) == b"hello world"


def test_a_request_runs_out_of_its_own_time_while_another_waits_on_the_link():
    """A deadline holds for each request alone, whichever of them is reading.

    The expired request's answer, when it does come, is dropped; the other
    request still gets its own.
    """
    with _peer() as peer:
        threads = _Threads()
        threads.start(0, lambda: peer.session.execute(r.Query(c.kXR_Qconfig, "slow")).data)
        ((sid, *_),) = peer.requests(1)
        with xrdclient.deadline(0.3), pytest.raises(xrdclient.OperationExpiredError):
            peer.session.execute(r.Ping())
        ((ping, *_),) = peer.requests(1)
        peer.send(ok(ping) + ok(sid, b"late but fine"))
        threads.join()
        assert threads.results == {0: b"late but fine"}


# ---------------------------------------------------------------------------
# Failures reach every waiter
# ---------------------------------------------------------------------------


def test_a_dropped_connection_fails_every_request_in_flight(config, monkeypatch):
    count = 6
    release = threading.Event()
    with FakeServer() as srv:

        def stuck(conn, sid, params, body):
            release.wait(PATIENCE)
            yield frame(sid, c.kXR_ok, b"too late")

        srv.handlers[c.kXR_stat] = stuck
        with Session.connect(srv.url, config=config) as session:
            started = _when_started(monkeypatch, session, count)
            threads = _Threads()
            for index in range(count):
                threads.start(index, lambda: session.execute(r.Stat("/")))
            started.wait()
            srv.disconnect()
            took = threads.join()
            release.set()
        assert took < 5.0
        assert sorted(threads.errors) == list(range(count))
        assert all(isinstance(exc, XrdConnectionError) for exc in threads.errors.values())
        assert session.broken


def test_a_control_link_that_fails_under_its_reader_fails_every_waiter(config):
    """Not only the reader: each follower gets a copy of the error to raise."""
    count = 4
    with _peer() as peer:
        session = peer.session
        threads = _Threads()
        for index in range(count):
            threads.start(index, lambda: session.execute(r.Ping()))
        peer.requests(count)
        peer.link.fault = "connection reset by the test"
        peer.send(b"\x00")  # something to read, so whoever is reading reads
        threads.join()
        assert len(threads.errors) == count
        assert all("reset by the test" in str(exc) for exc in threads.errors.values())
        assert all(isinstance(exc, XrdConnectionError) for exc in threads.errors.values())
        assert session.broken
        assert session._inbox == {}


def test_a_data_path_that_fails_fails_only_the_requests_routed_over_it(config):
    with _peer(config) as peer:
        session = peer.session
        near, far = socket.socketpair()
        path = _Breakable(near)
        session._paths[1] = path
        threads = _Threads()
        for index in range(2):
            threads.start(index, lambda: session.execute(r.Read(b"\0\0\0\1", 0, 8, 1)))
        peer.requests(2)
        path.fault = "data path reset by the test"
        far.sendall(b"\x00")
        threads.join()
        far.close()
        assert sorted(threads.errors) == [0, 1]
        assert all("data path reset" in str(exc) for exc in threads.errors.values())
        assert session.data_paths == []
        assert not session.broken  # the control link carries on
        threads = _Threads()
        threads.start(0, lambda: session.execute(r.Ping()).data)
        ((sid, *_),) = peer.requests(1)
        peer.send(ok(sid, b"still here"))
        threads.join()
        assert threads.results == {0: b"still here"}


def test_close_wakes_a_thread_blocked_reading_the_link():
    with _peer() as peer:
        threads = _Threads()
        threads.start(0, lambda: peer.session.execute(r.Ping()))
        peer.requests(1)  # sent, and its sender now waits on the socket
        started = time.monotonic()
        peer.session.close()
        assert threads.join() < 2.0
        assert time.monotonic() - started < 2.0
        assert isinstance(threads.errors[0], XrdConnectionError)


# ---------------------------------------------------------------------------
# The bulk reader has the wire to itself
# ---------------------------------------------------------------------------


def _open_for_bulk(session: Session) -> bytes:
    return session.execute(r.Open("/data/a.root", c.kXR_open_read)).data[:4]


def test_a_request_waits_its_turn_while_a_bulk_reader_has_the_wire(server, config):
    inside, leave = threading.Event(), threading.Event()
    with Session.connect(server.url, config=config) as session:
        handle = _open_for_bulk(session)

        def bulk() -> bytes:
            with session.bulk(handle, chunk=4, depth=2) as reader:
                data = b"".join(bytes(view) for _, view in reader.stream(0, 11))
                inside.set()
                assert leave.wait(PATIENCE)
            return data

        threads = _Threads()
        threads.start(0, bulk)
        assert inside.wait(PATIENCE)
        with xrdclient.deadline(0.2), pytest.raises(xrdclient.OperationExpiredError) as caught:
            session.execute(r.Ping())
        assert "waiting its turn" in str(caught.value)
        threads.start(1, lambda: session.execute(r.Ping()).data)  # queues behind the reader
        leave.set()
        threads.join()
        assert threads.results == {0: b"hello world", 1: b""}
        assert not session.broken


def test_the_bulk_readers_own_thread_cannot_use_the_session_behind_its_back(server, config):
    with Session.connect(server.url, config=config) as session:
        handle = _open_for_bulk(session)
        with (
            session.bulk(handle, chunk=4, depth=1),
            pytest.raises(ProtocolError, match="lent to a bulk transfer"),
        ):
            session.execute(r.Ping())
        assert session.execute(r.Ping()).data == b""


# ---------------------------------------------------------------------------
# Sending: one vectored call, and TLS's serialised one
# ---------------------------------------------------------------------------


def test_a_tls_link_sends_large_frames_in_pieces_under_its_lock():
    """A link with an I/O lock (as a TLS one has) sends the same bytes, in order."""
    with _peer() as peer:
        session = peer.session
        session._io_locks[0] = threading.Lock()
        payload = bytes(range(256)) * 1000  # several record-sized pieces
        threads = _Threads()
        threads.start(0, lambda: session.execute(r.Write(b"\0\0\0\1", 7, memoryview(payload))))
        ((sid, opcode, _, body),) = peer.requests(1)
        assert opcode == c.kXR_write
        assert body == payload
        peer.send(ok(sid))
        threads.join()
        assert not threads.errors


def test_send_buffers_survives_partial_sends():
    """``sendmsg`` takes what fits; the rest - down to part of a buffer - goes again."""
    near, far = socket.socketpair()
    near.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    transport = SocketTransport(near, "peer", 1)
    pieces: list[Buffer] = [
        b"head",
        memoryview(bytes(range(256)) * 512),
        b"",
        bytearray(b"tail") * 5000,
    ]
    expected = b"".join(bytes(p) for p in pieces)
    received = bytearray()

    def drain() -> None:
        while len(received) < len(expected):
            received.extend(far.recv(1000))

    reader = threading.Thread(target=drain)
    reader.start()
    try:
        transport.send_buffers(pieces)
        reader.join(PATIENCE)
    finally:
        transport.close()
        far.close()
    assert bytes(received) == expected


def test_advance_drops_what_was_sent_and_slices_what_was_half_sent():
    views = [memoryview(b"abc"), memoryview(b"defg"), memoryview(b"h")]
    assert [bytes(v) for v in _advance(views, 0)] == [b"abc", b"defg", b"h"]
    assert [bytes(v) for v in _advance(views, 5)] == [b"fg", b"h"]
    assert [bytes(v) for v in _advance(views, 7)] == [b"h"]
    assert _advance(views, 8) == []


@pytest.mark.parametrize("size", [1, 3, 8, 64])
def test_coalesce_keeps_every_byte_in_order_and_bounds_each_piece(size):
    buffers: list[Buffer] = [
        b"ab",
        memoryview(b"cdefghijkl"),
        b"",
        bytearray(b"m"),
        memoryview(b"n" * 40),
    ]
    pieces = list(coalesce(buffers, size))
    assert b"".join(bytes(p) for p in pieces) == b"".join(bytes(b) for b in buffers)
    assert all(0 < len(p) <= size for p in pieces)


def test_coalesce_passes_a_large_buffer_through_as_views_of_itself():
    big = bytearray(b"x" * 100)
    pieces = list(coalesce([big], 32))
    assert [len(p) for p in pieces] == [32, 32, 32, 4]
    assert all(isinstance(p, memoryview) for p in pieces[:3])  # no copy
