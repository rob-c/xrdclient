"""``xrdclient.deadline``: a request that runs out of time expires, and only it.

Each test puts a request in front of a server that is slow to answer - or
never does - and checks the three things XrdCl's per-request timeout
promises: the call ends when the time is up, with
:class:`~xrdclient.OperationExpiredError`; nothing more is done for the
request once it has expired (no retry, no redirect followed, no wait sat
out); and the connection is left as it was, the late reply dropped when it
comes, so the next request goes out on it as usual.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

import xrdclient
from xrdclient import File, FileSystem
from xrdclient.errors import TimeoutError as XrdTimeoutError
from xrdclient.proto import constants as c
from xrdclient.session import SESSIONS
from xrdclient.session import deadline as dl
from xrdclient.session.router import Router
from xrdclient.session.sync import Session
from xrdclient.testing import FakeServer
from xrdclient.testing.server import frame


def _slow(seconds: float, *answer: bytes):
    """A handler that sits on a request for ``seconds``, then answers it."""

    def handler(conn, sid, params, body):
        time.sleep(seconds)
        yield frame(sid, c.kXR_ok, *answer)

    return handler


def _elapsed(start: float) -> float:
    return time.monotonic() - start


# -- the helper itself ---------------------------------------------------------


def test_outside_every_block_there_is_no_deadline():
    assert dl.remaining() is None
    dl.check()  # nothing to run out


def test_a_deadline_counts_down_and_then_has_passed():
    with xrdclient.deadline(0.05) as when:
        assert 0 < dl.remaining() <= 0.05
        assert when == pytest.approx(time.monotonic() + dl.remaining(), abs=0.01)
        time.sleep(0.06)
        with pytest.raises(xrdclient.OperationExpiredError, match="Stat expired"):
            dl.check("Stat")
    assert dl.remaining() is None


def test_an_inner_deadline_shortens_but_never_extends():
    with xrdclient.deadline(5):
        with xrdclient.deadline(60):
            assert dl.remaining() <= 5
        with xrdclient.deadline(0.5):
            assert dl.remaining() <= 0.5
        assert dl.remaining() > 1


def test_an_expired_request_is_a_timeout_and_this_packages_error():
    assert issubclass(xrdclient.OperationExpiredError, xrdclient.TimeoutError)
    assert issubclass(xrdclient.OperationExpiredError, xrdclient.XRootDError)
    assert str(xrdclient.OperationExpiredError()) == "operation expired"


# -- the event path ------------------------------------------------------------


def test_a_request_expires_and_its_connection_carries_on(server, config):
    with FileSystem(server.url, config) as fs:
        fs.ping()
        session = fs._router.session
        server.handlers[c.kXR_ping] = _slow(1.5)
        started = time.monotonic()
        with pytest.raises(xrdclient.OperationExpiredError), xrdclient.deadline(0.3):
            fs.ping()
        assert _elapsed(started) < 1.2
        del server.handlers[c.kXR_ping]
        # Same connection, not broken, and the reply that turns up late is
        # taken for nobody else's.
        assert fs._router.session is session and not session.broken
        assert fs.stat("/data/a.root").st_size == 11
        assert fs.read_bytes("/data/a.root") == b"hello world"
        assert session._m._retired == set()


def test_a_deadline_already_passed_sends_nothing(server, config):
    with FileSystem(server.url, config) as fs:
        fs.ping()
        seen = len(server.seen)
        with xrdclient.deadline(0), pytest.raises(xrdclient.OperationExpiredError):
            fs.stat("/data/a.root")
        assert len(server.seen) == seen


def test_a_wait_longer_than_the_deadline_expires_at_once(server, config):
    def wait(conn, sid, params, body):
        yield frame(sid, c.kXR_wait, struct.pack(">i", 30) + b"busy\x00")

    server.handlers[c.kXR_stat] = wait
    with FileSystem(server.url, config) as fs:
        started = time.monotonic()
        with xrdclient.deadline(2), pytest.raises(xrdclient.OperationExpiredError, match="30s"):
            fs.stat("/data/a.root")
        assert _elapsed(started) < 1.5
        assert server.seen.count(c.kXR_stat) == 1  # not re-sent
        del server.handlers[c.kXR_stat]
        assert fs.stat("/data/a.root").st_size == 11


def test_a_wait_that_fits_is_sat_out(server, config):
    server.waits[c.kXR_stat] = 1  # a wait of 0 seconds
    with FileSystem(server.url, config) as fs, xrdclient.deadline(5):
        assert fs.stat("/data/a.root").st_size == 11


def test_a_redirect_is_not_followed_once_the_request_has_expired(config):
    with FakeServer(files={"/f": b"x"}) as data, FakeServer() as manager:
        host, port = data.address

        def late_redirect(conn, sid, params, body):
            time.sleep(0.6)
            yield frame(sid, c.kXR_redirect, struct.pack(">i", port) + host.encode())

        manager.handlers[c.kXR_stat] = late_redirect
        with FileSystem(manager.url, config) as fs:
            with xrdclient.deadline(0.2), pytest.raises(xrdclient.OperationExpiredError):
                fs.stat("/f")
            time.sleep(0.6)
            assert c.kXR_stat not in data.seen


def test_a_request_waiting_for_the_connection_expires_in_the_queue(server, config):
    with FileSystem(server.url, config) as fs:
        session = fs._router.session
        with session._lock:  # another thread's request holds the connection
            outcome: list[BaseException] = []

            def ask() -> None:
                try:
                    with xrdclient.deadline(0.2):
                        fs.ping()
                except BaseException as exc:
                    outcome.append(exc)

            asker = threading.Thread(target=ask)
            asker.start()
            asker.join(5)
        assert isinstance(outcome[0], xrdclient.OperationExpiredError)
        assert "waiting its turn" in str(outcome[0])
        fs.ping()


def test_a_stall_is_still_the_connections_problem(server, config):
    """The stall clock running out first costs the connection, as before."""
    server.handlers[c.kXR_ping] = _slow(1.0)
    with FileSystem(server.url, config.evolve(stall_deadline=0.2)) as fs:
        with xrdclient.deadline(5), pytest.raises(XrdTimeoutError) as caught:
            fs.ping()
        assert not isinstance(caught.value, xrdclient.OperationExpiredError)


def test_a_socket_idle_timeout_under_a_long_deadline_is_a_timeout(server, config):
    server.handlers[c.kXR_ping] = _slow(1.0)
    with FileSystem(server.url, config.evolve(request_timeout=0.2, connect_retries=0)) as fs:
        assert fs._router.session  # connected before the handler matters
        with xrdclient.deadline(30), pytest.raises(XrdTimeoutError) as caught:
            fs.ping()
        assert not isinstance(caught.value, xrdclient.OperationExpiredError)


def test_a_file_is_not_reopened_when_its_request_expires(server, config):
    server.handlers[c.kXR_stat] = _slow(1.0, b"0 11 0 0\x00")
    with File(server.url.with_path("/data/a.root"), config) as fh:
        with xrdclient.deadline(0.2), pytest.raises(xrdclient.OperationExpiredError):
            fh.stat(refresh=True)
        assert fh.recoveries == 0
        del server.handlers[c.kXR_stat]
        assert fh.read() == b"hello world"


def test_closing_a_reader_past_its_deadline_says_so(server, config):
    fh = File(server.url.with_path("/data/a.root"), config)
    fh.open("r")
    with xrdclient.deadline(0), pytest.raises(xrdclient.OperationExpiredError):
        fh.close()
    assert not fh.is_open


# -- connecting and reconnecting ---------------------------------------------------


def test_connecting_is_bounded_by_the_deadline(config):
    """A server that accepts and never answers the handshake."""
    with socket.create_server(("127.0.0.1", 0)) as listener:
        port = listener.getsockname()[1]
        started = time.monotonic()
        with xrdclient.deadline(0.3), pytest.raises(xrdclient.OperationExpiredError):
            Session.connect(f"root://127.0.0.1:{port}", config=config)
        assert _elapsed(started) < 2


def test_a_connect_timeout_is_cut_to_the_deadline(config, monkeypatch):
    seen = []
    from xrdclient.session import sync

    def connect(host, port, cfg):
        seen.append(cfg.connect_timeout)
        raise XrdTimeoutError("connecting timed out")

    monkeypatch.setattr(sync.SocketTransport, "connect", connect)
    with xrdclient.deadline(0.5), pytest.raises(XrdTimeoutError):
        Session.connect("root://127.0.0.1:1", config=config)
    assert seen and seen[0] <= 0.5
    with pytest.raises(XrdTimeoutError):
        Session.connect("root://127.0.0.1:1", config=config)
    assert seen[1] == config.connect_timeout


def test_no_retry_is_made_once_the_request_has_expired(closed_port, config):
    from xrdclient.proto import requests as r

    host, port = closed_port
    settings = config.evolve(connect_retries=5, retry_backoff=0.2)
    router = Router(f"root://{host}:{port}", settings)
    started = time.monotonic()
    with xrdclient.deadline(0.3), pytest.raises(xrdclient.OperationExpiredError):
        router.execute(r.Ping())
    assert _elapsed(started) < 1.5


def test_a_failure_after_the_deadline_is_the_expiry(config, monkeypatch):
    from xrdclient.proto import requests as r

    router = Router("root://127.0.0.1:1", config.evolve(connect_retries=5))
    attempts = []

    def attempt(request, route, **kwargs):
        attempts.append(request)
        time.sleep(0.2)
        raise xrdclient.ConnectionError("gone")

    monkeypatch.setattr(router, "_attempt", attempt)
    with xrdclient.deadline(0.1), pytest.raises(xrdclient.OperationExpiredError) as caught:
        router.execute(r.Ping())
    assert len(attempts) == 1
    assert isinstance(caught.value.__cause__, xrdclient.ConnectionError)


def test_a_retry_pause_is_cut_to_the_deadline(monkeypatch, config):
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    router = Router("root://127.0.0.1:1", config.evolve(retry_backoff=10.0))
    with xrdclient.deadline(1):
        router._pause(1)
    router._pause(1)
    assert slept[0] <= 1 and slept[1] == 10.0


# -- the bulk data plane --------------------------------------------------------------


@pytest.fixture
def big():
    payload = bytes(range(256)) * 4096  # 1 MiB
    with FakeServer(files={"/big": payload}) as srv:
        yield srv, payload


def _plane_config(config):
    return config.evolve(bulk=True, bulk_chunk=64 << 10, bulk_depth=4)


def test_a_bulk_read_that_expires_between_frames_keeps_its_connection(big, config):
    srv, payload = big
    with File(srv.url.with_path("/big"), _plane_config(config)) as fh:
        session = fh.session
        srv.handlers[c.kXR_read] = _slow(1.0)
        with xrdclient.deadline(0.3), pytest.raises(xrdclient.OperationExpiredError):
            fh.read(len(payload), 0)
        assert not session.broken
        assert session._m._retired  # the reads still owed, left to the machine
        del srv.handlers[c.kXR_read]
        time.sleep(1.2)
        # The first read goes the event path, which drops the late replies.
        assert fh.read(10, 0) == payload[:10]
        assert fh.read(len(payload), 0) == payload
        assert not session._m._retired and fh.recoveries == 0


def test_a_bulk_read_that_expires_mid_frame_breaks_its_connection(big, config):
    srv, payload = big

    def half(conn, sid, params, body):
        yield struct.pack(">HHI", sid, c.kXR_ok, 1024) + b"x" * 100
        time.sleep(1.0)
        yield b"x" * 924

    with File(srv.url.with_path("/big"), _plane_config(config).evolve(recover_handles=False)) as fh:
        session = fh.session
        srv.handlers[c.kXR_read] = half
        with xrdclient.deadline(0.4), pytest.raises(xrdclient.OperationExpiredError):
            fh.read(len(payload), 0)
        assert session.broken


def test_a_deadline_longer_than_the_link_timeout_leaves_it_alone(big, config):
    srv, payload = big
    with File(srv.url.with_path("/big"), _plane_config(config)) as fh:
        with xrdclient.deadline(3600):
            assert fh.read(len(payload), 0) == payload


def test_a_bulk_read_whose_deadline_has_passed_takes_nothing_off_the_wire():
    """Checked before every receive: the replies owed are left to the machine."""
    from xrdclient.proto.machine import SessionMachine, State
    from xrdclient.session.bulk import BulkReader
    from xrdclient.transport.memory import pipe

    class Lent:
        def __init__(self) -> None:
            self.transport, self.peer = pipe()
            self.machine = SessionMachine(host="h")
            self.machine.state = State.READY
            self.config = xrdclient.Config(stall_deadline=0)
            self.broken = False

        def mark_broken(self) -> None:
            self.broken = True

    lent = Lent()
    with xrdclient.deadline(0):
        reader = BulkReader(lent, b"HDL0", chunk=16, depth=1)
        with pytest.raises(xrdclient.OperationExpiredError):
            list(reader.stream(0, 16))
    assert not lent.broken and len(lent.machine._retired) == 1


def test_retired_bulk_ids_come_back_when_their_replies_do():
    from xrdclient.proto.machine import SessionMachine

    machine = SessionMachine(host="h", port=1)
    sids = machine.lease_sids(3)
    machine.retire_sids(sids, {sids[0]})
    assert machine._retired == {sids[0]} and not machine._leased
    assert sids[1] in machine._free and sids[2] in machine._free


def teardown_module() -> None:
    SESSIONS.clear()
