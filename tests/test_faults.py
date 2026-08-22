"""Resilience: what happens when the network stops cooperating.

Every test here puts a :class:`~xrd.testing.FaultProxy` between the client and
a real loopback server and then breaks it — drops, stalls, corruption,
byte-at-a-time delivery, refused connections. The assertions are about what
the *client* does, which is the only part of this that ships.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from xrd import FileSystem
from xrd.client.file import File
from xrd.config import Config
from xrd.errors import ConnectionError as XrdConnectionError
from xrd.errors import ProtocolError, TransientError
from xrd.errors import TimeoutError as XrdTimeoutError
from xrd.flags import OpenFlags
from xrd.testing import FakeServer, FaultProxy
from xrd.testing.faults import _address, _pieces
from xrd.url import parse

PAYLOAD = b"".join(bytes([i % 251]) for i in range(8192))


@pytest.fixture
def broken(server):
    """A proxy in front of the shared :class:`FakeServer`, healthy to start."""
    server.add_file("/data/big.root", PAYLOAD)
    with FaultProxy(server) as proxy:
        yield proxy


@pytest.fixture
def patient() -> Config:
    """Retries on, but a short enough window that a stall ends the test."""
    return Config(
        username="tester",
        auth_order=("host",),
        request_timeout=1.5,
        connect_timeout=1.5,
        connect_retries=2,
        retry_backoff=0.1,
    )


# ---------------------------------------------------------------------------
# The proxy itself
# ---------------------------------------------------------------------------


def test_a_healthy_proxy_is_invisible(broken, patient):
    # Counting connections means counting one: a data sub-stream is a second
    # connection through the proxy, and this test is about the proxy, not
    # about how many links a file spreads itself over.
    with FileSystem(broken.url, patient.evolve(data_streams=0)) as fs:
        assert fs.read_bytes("/data/a.root") == b"hello world"
        assert fs.read_bytes("/data/big.root") == PAYLOAD
    assert broken.connections == 1
    assert broken.bytes_from_server > len(PAYLOAD)
    assert broken.bytes_from_client > 0


def test_the_proxy_says_where_it_points_and_what_is_armed(broken):
    assert repr(broken).endswith("healthy)")
    assert f"{broken.target[0]}:{broken.target[1]}" in repr(broken)
    broken.drop_after(10).delay(0.1).corrupt(3).chop(8).stall_after(4).refuse()
    broken.rewrite(lambda data: data)
    assert broken.armed == ["drop", "stall", "delay", "corrupt", "chop", "filter", "refuse"]
    assert broken.heal().armed == []


@pytest.mark.parametrize(
    "target",
    [
        parse("root://example.org:1094/"),
        "example.org:1094",
        "root://example.org:1094/store",
        ("example.org", 1094),
    ],
)
def test_a_proxy_takes_an_address_in_any_of_the_usual_spellings(target):
    assert _address(target) == ("example.org", 1094)


def test_an_address_it_cannot_understand_is_refused():
    with pytest.raises(TypeError, match="cannot take an address"):
        _address(object())


def test_a_fake_server_can_be_passed_straight_in(server):
    assert _address(server) == server.address


@pytest.mark.parametrize(
    "size, expected", [(0, [b"abcdef"]), (99, [b"abcdef"]), (2, [b"ab", b"cd", b"ef"])]
)
def test_chunks_are_split_the_way_a_slow_link_would(size, expected):
    assert _pieces(b"abcdef", size) == expected


def test_a_response_delivered_one_byte_at_a_time_still_parses(broken, patient):
    """Nothing in the protocol layer may assume one recv is one response."""
    broken.chop(1)
    with FileSystem(broken.url, patient) as fs:
        assert fs.stat("/data/a.root").st_size == 11
        assert fs.read_bytes("/data/a.root") == b"hello world"


def test_a_refused_connection_is_a_connection_error(broken, patient):
    broken.refuse()
    with FileSystem(broken.url, patient) as fs, pytest.raises(XrdConnectionError):
        fs.stat("/data/a.root")
    assert broken.connections >= 1


def test_the_retry_finds_the_server_once_it_comes_back(broken, patient):
    """A storage element restarting under a client is the everyday failure."""
    broken.refuse()
    threading.Timer(0.15, broken.accept).start()
    with FileSystem(broken.url, patient) as fs:
        assert fs.stat("/data/a.root").st_size == 11


def test_cutting_the_connection_reports_how_many_went(broken, patient):
    with FileSystem(broken.url, patient) as fs:
        fs.stat("/data/a.root")
        assert broken.cut() == 2  # both halves of the one connection
        assert broken.cut() == 0


# ---------------------------------------------------------------------------
# Metadata operations across a break
# ---------------------------------------------------------------------------


def test_an_idempotent_request_survives_a_dropped_connection(broken, patient):
    """The router reconnects and re-issues; the caller never finds out."""
    with FileSystem(broken.url, patient) as fs:
        assert fs.stat("/data/a.root").st_size == 11
        broken.cut()
        assert fs.stat("/data/a.root").st_size == 11
    assert broken.connections == 2


def test_a_connection_that_keeps_dropping_gives_up_and_says_so(broken, patient):
    broken.drop_after(0)
    with FileSystem(broken.url, patient) as fs:
        with pytest.raises(TransientError) as caught:
            fs.stat("/data/a.root")
    assert caught.value.attempts == patient.connect_retries + 1
    assert broken.connections > patient.connect_retries


def test_a_stalled_server_times_out_rather_than_hanging(broken, patient):
    """No answer and no error is the failure that costs a whole grid job."""
    broken.stall_after(0)
    started = time.monotonic()
    with FileSystem(broken.url, patient) as fs:
        with pytest.raises(XrdTimeoutError) as caught:
            fs.stat("/data/a.root")
    assert time.monotonic() - started < 30
    # It is still transient - retrying is reasonable - but it says *why*.
    assert isinstance(caught.value, TransientError)
    assert isinstance(caught.value, TimeoutError)
    assert caught.value.attempts == patient.connect_retries + 1


def test_a_delayed_server_is_merely_slow(broken, patient):
    broken.delay(0.05)
    with FileSystem(broken.url, patient) as fs:
        assert fs.read_bytes("/data/a.root") == b"hello world"


def test_a_status_the_client_does_not_know_is_refused_not_guessed_at(broken, patient):
    """Every response frame starts ``streamid, status``; mangle the status.

    Only the stat reply - identifiable by the fixed mtime the fake server
    stamps on everything - so the bring-up completes and the failure lands
    where it can be attributed.
    """

    def mangle(data: bytes) -> bytes:
        if b"1700000000" in data and data[2:4] == b"\x00\x00":
            return data[:2] + b"\x00\x63" + data[4:]
        return data

    broken.rewrite(mangle)
    with FileSystem(broken.url, patient) as fs, pytest.raises(ProtocolError):
        fs.stat("/data/a.root")


# ---------------------------------------------------------------------------
# Handle recovery
# ---------------------------------------------------------------------------


def test_a_read_handle_re_opens_itself_when_its_server_goes_away(broken, patient):
    """The point of the whole exercise: a long read outlives a restart."""
    handle = File(broken.url.with_path("/data/big.root"), patient)
    with handle:
        assert handle.read(64, 0) == PAYLOAD[:64]
        broken.cut()
        assert handle.read(64, 4096) == PAYLOAD[4096:4160]
        assert handle.recoveries == 1
        assert handle.is_open


def test_recovery_survives_a_vector_read_and_a_paged_read(broken, patient):
    handle = File(broken.url.with_path("/data/big.root"), patient)
    with handle:
        handle.stat()
        broken.cut()
        assert handle.readv([(0, 16), (256, 16)]) == [PAYLOAD[:16], PAYLOAD[256:272]]
        broken.cut()
        assert handle.pgread(32, 64).data == PAYLOAD[64:96]
        broken.cut()
        assert handle.stat(refresh=True).st_size == len(PAYLOAD)
        assert handle.recoveries == 3


def test_recovery_can_be_turned_off(broken, patient):
    """Some callers would rather see the failure than a silent re-open."""
    config = patient.evolve(recover_handles=False)
    handle = File(broken.url.with_path("/data/big.root"), config)
    with handle:
        handle.read(16, 0)
        assert not handle.recoverable
        broken.cut()
        with pytest.raises(TransientError):
            handle.read(16, 0)


def test_a_write_handle_is_never_silently_re_opened(broken, patient):
    """Re-opening a writer would lose data, or worse, re-truncate the file."""
    handle = File(broken.url.with_path("/data/new.root"), patient)
    handle.open(OpenFlags.NEW | OpenFlags.WRITE)
    assert not handle.recoverable
    handle.write(b"first", 0)
    broken.cut()
    with pytest.raises(TransientError):
        handle.write(b"second", 5)
    # The server that would have committed the file has gone, so the close
    # cannot succeed either - and a writer's close says so rather than
    # letting the caller believe the bytes landed.
    with pytest.raises(TransientError):
        handle.close()


@pytest.mark.parametrize(
    "flags, recoverable",
    [
        (OpenFlags.READ, True),
        (OpenFlags.READ | OpenFlags.REFRESH, True),
        (OpenFlags.UPDATE, False),
        (OpenFlags.NEW, False),
        (OpenFlags.DELETE, False),
        (OpenFlags.READ | OpenFlags.APPEND, False),
    ],
)
def test_what_counts_as_recoverable(flags, recoverable, broken, patient):
    handle = File(broken.url.with_path("/data/big.root"), patient)
    handle._flags = flags
    assert handle.recoverable is recoverable


def test_closing_a_file_whose_server_vanished_does_not_raise(broken, patient):
    """A ``with`` block must not turn a lost server into a second exception."""
    handle = File(broken.url.with_path("/data/big.root"), patient)
    with handle:
        handle.read(16, 0)
        broken.cut()
        broken.refuse()
    assert not handle.is_open


def test_recovery_gives_up_when_the_server_is_really_gone(broken, patient):
    handle = File(broken.url.with_path("/data/big.root"), patient)
    with handle:
        handle.read(16, 0)
        broken.cut()
        broken.refuse()
        with pytest.raises(XrdConnectionError):
            handle.read(16, 0)


def test_the_high_level_file_object_recovers_too(broken, patient):
    """``xrd.open`` is what most callers use; it must inherit the property."""
    import xrd

    with xrd.open(broken.url.with_path("/data/big.root"), "rb", config=patient) as fh:
        assert fh.read(32) == PAYLOAD[:32]
        broken.cut()
        assert fh.read(32) == PAYLOAD[32:64]


# ---------------------------------------------------------------------------
# Corruption the transport cannot see
# ---------------------------------------------------------------------------


def test_a_flipped_bit_in_a_page_is_caught_by_its_checksum(patient):
    """``kXR_pgread`` exists for exactly this; the proxy is the bad memory."""
    page = bytes(range(256)) * 16  # 4 KiB
    with FakeServer(files={"/p.root": page + page}) as origin:
        with FaultProxy(origin) as proxy:
            proxy.rewrite(lambda data: data.replace(b"\x00\x01\x02\x03", b"\x00\x01\x02\xff"))
            handle = File(proxy.url.with_path("/p.root"), patient)
            with handle:
                result = handle.pgread(8192, 0)
    assert result.corrupt_pages
    assert result.data != page + page


def test_an_unverified_paged_read_hands_back_what_arrived(patient):
    page = bytes(range(256)) * 16
    with FakeServer(files={"/p.root": page}) as origin:
        with FaultProxy(origin) as proxy:
            proxy.rewrite(lambda data: data.replace(b"\x00\x01\x02\x03", b"\x00\x01\x02\xff"))
            handle = File(proxy.url.with_path("/p.root"), patient)
            with handle:
                result = handle.pgread(4096, 0, verify=False)
    assert result.corrupt_pages == ()
    assert result.data != page


def test_a_flipped_byte_at_a_chosen_offset_reaches_the_caller(patient):
    """``corrupt`` counts bytes from the server, so arm it once setup is done.

    Single-stream, because the offset it is armed at is an offset into what
    this proxy carries: a data sub-stream would carry the read's bytes on a
    connection of its own and the count would land somewhere else.
    """
    with FakeServer(files={"/f.root": b"A" * 64}) as origin, FaultProxy(origin) as proxy:
        with File(proxy.url.with_path("/f.root"), patient.evolve(data_streams=0)) as handle:
            proxy.corrupt(proxy.bytes_from_server + 8, 0x20)  # the first byte of the data
            data = handle.read(64, 0)
    assert data == b"a" + b"A" * 63


def test_a_proxy_in_front_of_nothing_is_a_connection_error(patient, closed_port):
    """The upstream is gone: the proxy accepts, finds nobody, and hangs up."""
    with FaultProxy(closed_port) as proxy:
        with FileSystem(proxy.url, patient) as fs, pytest.raises(XrdConnectionError):
            fs.stat("/f.root")
    assert proxy.connections >= 1


def test_a_borrowed_connection_is_not_closed_by_the_handle_that_lost_it(broken, patient):
    """``fs.open`` lends its router; recovery replaces it without shutting it."""
    with FileSystem(broken.url, patient) as fs:
        with fs.open("/data/big.root", "rb", buffering=0) as fh:
            assert fh.read(32) == PAYLOAD[:32]
            broken.cut()
            assert fh.read(32) == PAYLOAD[32:64]
            assert fh.file.recoveries == 1
        assert fs.stat("/data/big.root").st_size == len(PAYLOAD)


def test_a_proxy_asked_to_stop_finishes_the_turn_it_is_in(broken):
    """``close`` cuts its connections, so the loop normally leaves through the
    ``break``. Setting the flag on an idle connection retires it the other way:
    the pump comes back empty and the ``while`` condition ends the thread."""
    import socket

    sock = socket.create_connection(broken.address)
    try:
        deadline = time.monotonic() + 2.0
        while not broken._threads and time.monotonic() < deadline:
            time.sleep(0.01)
        assert broken._threads, "the proxy never picked up the connection"
        thread = broken._threads[-1]
        broken._stop.set()  # nothing in flight: the next turn is the last
        thread.join(timeout=2.0)
        assert not thread.is_alive()
    finally:
        sock.close()


def test_a_connection_is_recorded_only_once_its_thread_is_running(broken, monkeypatch):
    """Anything that reaches into ``_threads`` - ``close``, or the test above -
    joins what it finds there, and joining a thread that has not started yet is
    a ``RuntimeError``. So the record has to come second."""
    recorded_early = []
    real_start = threading.Thread.start

    def start(self: threading.Thread) -> None:
        recorded_early.append(self in broken._threads)
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    with FileSystem(broken.url) as fs:
        fs.ping()
    assert recorded_early and not any(recorded_early)


def test_a_peer_that_stops_reading_is_dropped_instead_of_jamming_the_proxy():
    """The proxy writes with a timeout, so one wedged connection cannot stop it
    serving the next: the wedged one is torn down and the port stays usable."""
    blast = b"Z" * (2 << 20)
    listener = socket.create_server(("127.0.0.1", 0))
    thread = threading.Thread(target=_flood, args=(listener, blast), daemon=True)
    thread.start()
    try:
        with FaultProxy(listener.getsockname()) as proxy:
            got = _wedged_transfer(proxy)
            # How much arrived depends on the kernel's buffers; that the proxy
            # gave up part-way rather than delivering the whole 2 MiB does not.
            assert got < len(blast)
            assert proxy.connections == 1 and proxy.bytes_from_server < len(blast)

            with socket.create_connection(proxy.address) as after:
                assert after.recv(1)  # and the proxy is still serving
    finally:
        listener.close()


def _send_blast(peer: socket.socket, blast: bytes) -> None:
    with peer:
        try:
            peer.sendall(blast)
        except OSError:
            pass  # the proxy hung up on us, which is the point


def _flood(listener: socket.socket, blast: bytes) -> None:
    while True:
        peer, _ = listener.accept()
        threading.Thread(target=_send_blast, args=(peer, blast), daemon=True).start()


def _trickle(client: socket.socket, done: threading.Event) -> None:
    """Keep asking, so the proxy is never idle - only wedged."""
    while not done.is_set():
        try:
            client.send(b".")
        except OSError:
            return
        time.sleep(0.001)


def _wait_for_quiet(proxy: FaultProxy) -> None:
    quiet = 0
    while quiet < 4:  # nothing moving means the proxy gave up
        seen = proxy.bytes_from_server
        time.sleep(0.1)
        quiet = quiet + 1 if proxy.bytes_from_server == seen else 0


def _drain(client: socket.socket) -> int:
    got = 0
    try:
        while chunk := client.recv(65536):
            got += len(chunk)
    except ConnectionError:
        pass  # dropped with a reset rather than a shutdown; dropped
    return got


def _wedged_transfer(proxy: FaultProxy) -> int:
    client = socket.socket()
    client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)  # no autotuning
    with client:
        client.connect(proxy.address)
        client.settimeout(10.0)
        done = threading.Event()
        threading.Thread(target=_trickle, args=(client, done), daemon=True).start()
        _wait_for_quiet(proxy)
        done.set()
        return _drain(client)
