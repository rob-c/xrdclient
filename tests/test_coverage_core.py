"""The corners of the core client that the scenario suites do not reach.

Each test here drives one path the broader suites pass by - a server that
answers a read with something other than bytes, a destination that cannot
seek, a stall the clock notices, a lazily-imported name - and checks what the
caller sees when it happens. They run against
:class:`~xrdclient.testing.FakeServer` or an in-memory pipe, so none of them
needs a real daemon.
"""

from __future__ import annotations

import dataclasses
import itertools
import os
import pickle
import socket
import time
from types import SimpleNamespace

import pytest

import xrdclient
import xrdclient.crypto
import xrdclient.transport
from xrdclient.client import bulk as client_bulk
from xrdclient.client.bulk import BulkResult, download, stream
from xrdclient.client.file import File
from xrdclient.config import Config
from xrdclient.crypto.sigver import Signer
from xrdclient.errors import ConnectionError as XrdConnectionError
from xrdclient.errors import PageIntegrityError, ProtocolError, WaitLimitError, XRootDError
from xrdclient.errors import TimeoutError as XrdTimeoutError
from xrdclient.proto import constants as c
from xrdclient.proto import requests as r
from xrdclient.proto.machine import SessionMachine, State
from xrdclient.session import bulk as session_bulk
from xrdclient.session.bulk import BulkReader, BulkUnsupported
from xrdclient.session.router import Router
from xrdclient.session.sync import Session
from xrdclient.testing import FakeServer, error
from xrdclient.transport.memory import pipe
from xrdclient.transport.sync import SocketTransport

#: One mebibyte with every byte position distinct: exactly the smallest read
#: that the bulk data plane takes on.
PAYLOAD = bytes(range(256)) * 4096


@pytest.fixture
def cfg() -> Config:
    """Bulk settings small enough to put several reads in flight on 1 MiB."""
    return Config(
        username="tester",
        auth_order=("host",),
        require_tls=False,
        data_streams=0,
        bulk_chunk=64 << 10,
        bulk_depth=4,
        bulk_workers=2,
    )


@pytest.fixture
def big():
    """A server holding one 1 MiB file and one three-byte file."""
    with FakeServer(files={"/big.bin": PAYLOAD, "/tiny.bin": b"abc"}) as srv:
        yield srv


def _opened(server: FakeServer, config: Config) -> File:
    handle = File(server.url.with_path("/big.bin"), config)
    handle.open("r")
    return handle


def _hang_up(conn, sid, params, body):  # type: ignore[no-untyped-def]
    """Cut the connection the way a crashed data server does."""
    conn.sock.shutdown(socket.SHUT_RDWR)
    conn.sock.close()
    return iter(())


# ---------------------------------------------------------------------------
# client.bulk: the transfer
# ---------------------------------------------------------------------------


def test_a_bulk_result_describes_itself():
    """The summary names the rate, the connections and any restarts."""
    result = BulkResult(size=1 << 20, seconds=1.0, workers=2, restarts=3)
    assert result.rate == float(1 << 20)
    text = str(result)
    assert "1 MiB/s" in text and "2 connections" in text and "3 restart(s)" in text
    single = BulkResult(size=5, seconds=0.0, workers=1)
    assert single.rate == 0.0
    assert "1 connection)" in str(single) and "restart" not in str(single)


def test_a_transfer_with_no_recovery_budget_fails_on_the_first_loss(tmp_path, big, cfg):
    """With nothing left to spend on waiting, a lost server ends every worker.

    Two workers are in flight and both lose their connection; neither is
    allowed to resume, and the first failure is what the caller sees.
    """
    big.handlers[c.kXR_read] = _hang_up
    brittle = dataclasses.replace(cfg, bulk_recovery=0.0)
    with pytest.raises(XRootDError):
        download(big.url.with_path("/big.bin"), tmp_path / "lost.bin", config=brittle, workers=2)


def test_a_download_into_a_pipe_arrives_in_order(big, cfg):
    """A destination that cannot seek is written front to back, once."""
    read_end, write_end = os.pipe()
    try:
        result = download(big.url.with_path("/tiny.bin"), write_end, config=cfg)
        assert result.size == 3
        assert os.read(read_end, 16) == b"abc"
    finally:
        os.close(read_end)
        os.close(write_end)


def test_a_destination_that_cannot_be_sized_is_still_written(tmp_path, big, cfg, monkeypatch):
    """Pre-sizing the file is an optimisation; a device that refuses it is fine."""

    def refuse(fd: int, length: int) -> None:
        raise OSError("this device has no length")

    monkeypatch.setattr(client_bulk.os, "ftruncate", refuse)
    target = tmp_path / "unsized.bin"
    result = download(big.url.with_path("/big.bin"), target, config=cfg)
    assert result.size == len(PAYLOAD)
    assert target.read_bytes() == PAYLOAD


def test_a_stream_that_ends_short_of_its_promised_size_is_an_error(big, cfg):
    """A caller that named the size is told when the file did not have it."""
    joined = bytearray()
    with pytest.raises(XRootDError, match="stream ended"):
        stream(big.url.with_path("/big.bin"), joined.extend, config=cfg, size=len(PAYLOAD) + 10)
    assert bytes(joined) == PAYLOAD


# ---------------------------------------------------------------------------
# File.readinto and the bulk reader
# ---------------------------------------------------------------------------


def test_readinto_falls_back_when_the_server_asks_to_wait(big, cfg):
    """A ``kXR_wait`` is a conversation the bulk reader does not hold.

    The read is re-run on the ordinary path, and the caller gets the same
    bytes it would have had either way.
    """
    big.waits[c.kXR_read] = 1
    with _opened(big, cfg) as handle:
        buffer = bytearray(len(PAYLOAD))
        assert handle.readinto(buffer) == len(PAYLOAD)
    assert bytes(buffer) == PAYLOAD


def test_readinto_assembles_replies_sent_in_instalments(big, cfg):
    """``kXR_oksofar`` pieces of one read land back to back in the buffer."""
    big.chunk_reads = 16 << 10
    with _opened(big, cfg) as handle:
        buffer = bytearray(len(PAYLOAD))
        assert handle.readinto(buffer) == len(PAYLOAD)
    assert bytes(buffer) == PAYLOAD


def test_a_bulk_read_error_without_a_code_is_a_protocol_error(big, cfg):
    """An error reply whose code maps to nothing still stops the read."""
    big.handlers[c.kXR_read] = lambda conn, sid, params, body: iter([error(sid, 0, "odd")])
    with _opened(big, cfg) as handle, pytest.raises(ProtocolError, match="error code 0: odd"):
        handle.readinto(bytearray(len(PAYLOAD)))


def test_a_bulk_reader_asked_for_nothing_sends_nothing(big, cfg):
    """Empty requests, and a read wholly past the end, hand back no pieces."""
    with _opened(big, cfg) as handle:
        with handle.session.bulk(handle.handle, chunk=1024, depth=2) as reader:
            assert list(reader.stream(0, 0)) == []
            assert reader.into(memoryview(bytearray(0)), 0) == 0
            assert list(reader.stream(len(PAYLOAD), 10)) == []
            assert reader.delivered == 0
        seen = big.seen.count(c.kXR_read)
    assert seen == 1  # only the read past the end went out


class _Lent:
    """What a :class:`BulkReader` borrows from a session, over a memory pipe."""

    def __init__(self, config: Config) -> None:
        self.transport, self.peer = pipe()
        self.machine = SessionMachine(host="h")
        self.machine.state = State.READY
        self.config = config
        self.broken = False

    def mark_broken(self) -> None:
        self.broken = True


def test_a_bulk_read_past_its_stall_deadline_gives_up(monkeypatch):
    """The stall clock is checked before every receive, not only at the end.

    A clock that has already run past the deadline stops the read before
    a byte is taken, and the connection is marked broken because its
    replies are still owed.
    """
    ticks = itertools.count(0.0, 10.0)
    monkeypatch.setattr(session_bulk, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    lent = _Lent(Config(stall_deadline=5.0))
    reader = BulkReader(lent, b"HDL0", chunk=16, depth=1)
    with pytest.raises(XrdTimeoutError, match="stalled"):
        list(reader.stream(0, 16))
    assert lent.broken


def test_a_bulk_read_whose_connection_closes_mid_frame_says_so():
    """A receive that returns nothing is the peer gone, not a short frame.

    The server hangs up after sending only part of a reply header, so the
    reader must stop with the bytes it was still owed rather than wait.
    """
    lent = _Lent(Config(stall_deadline=0))
    reader = BulkReader(lent, b"HDL0", chunk=16, depth=1)
    lent.peer.close()
    with pytest.raises(XrdTimeoutError, match="connection closed"):
        list(reader.stream(0, 16))


# ---------------------------------------------------------------------------
# The session machine's bulk framing
# ---------------------------------------------------------------------------


def test_leasing_no_stream_ids_is_refused():
    machine = SessionMachine(host="h")
    machine.state = State.READY
    with pytest.raises(ValueError, match="positive"):
        machine.lease_sids(0)


def test_a_bulk_read_is_signed_when_the_session_signs_reads():
    """A read framed outside :meth:`submit` still carries its signature."""
    machine = SessionMachine(host="h")
    machine.state = State.READY
    machine.signer = Signer(b"k" * 32, c.kXR_secNone, {c.kXR_read: c.kXR_secStandard})
    (sid,) = machine.lease_sids(1)
    frame = machine.frame_for(r.Read(b"HDL0", 0, 1024), sid)
    assert int.from_bytes(frame[2:4], "big") == c.kXR_sigver
    assert machine.signer.seqno == 1


def test_a_bulk_read_goes_unsigned_when_the_session_does_not_sign_reads():
    machine = SessionMachine(host="h")
    machine.state = State.READY
    machine.signer = Signer(b"k" * 32, c.kXR_secNone)
    (sid,) = machine.lease_sids(1)
    frame = machine.frame_for(r.Read(b"HDL0", 0, 1024), sid)
    assert int.from_bytes(frame[2:4], "big") == c.kXR_read
    assert machine.signer.seqno == 0


# ---------------------------------------------------------------------------
# Session and router
# ---------------------------------------------------------------------------


def test_a_receive_whose_deadline_has_passed_is_a_timeout(server, config):
    """No read is attempted once the operation's deadline is behind it."""
    with Session.connect(server.url, config=config) as session:
        with pytest.raises(XrdTimeoutError):
            session._receive(0, time.monotonic() - 1.0)


def test_a_bulk_reader_is_refused_while_a_request_is_outstanding(server, config):
    """A reader would take the outstanding request's reply off the wire.

    Refused as unsupported, so the caller carries on down the event path.
    """
    with Session.connect(server.url, config=config) as session:
        session.machine.submit(r.Ping())
        with pytest.raises(BulkUnsupported, match="still has requests outstanding"):
            with session.bulk(b"HDL0", chunk=1024, depth=1):
                pass  # pragma: no cover - the entry is what raises


def test_a_busy_data_server_is_not_retried_from_the_manager(config):
    """``kXR_wait`` past the budget on a redirect hop is final there too."""
    with (
        FakeServer() as manager,
        FakeServer(files={"/a": b"A"}) as ds,
        xrdclient.FileSystem(manager.url, config) as fs,
    ):
        manager.redirects[c.kXR_stat] = (*ds.address, "")
        ds.waits[c.kXR_stat] = config.redirect_limit + 5
        with pytest.raises(WaitLimitError):
            fs.stat("/a")
        assert manager.seen.count(c.kXR_stat) == 1


def test_a_sticky_router_already_moved_by_another_thread_stays_put(server, config):
    """A redirect answered on a session the router has since left moves nothing."""
    router = Router(server.url, config)
    try:
        assert not router.session.closed  # connected, and so movable
        elsewhere = server.url.evolve(host="elsewhere.example", port=1)
        assert router._move(None, elsewhere) is False
        assert router.url == server.url
    finally:
        router.close()


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


def test_the_default_receive_into_copies_what_receive_returns():
    """A transport with no zero-copy read still fills the caller's buffer."""
    near, far = pipe()
    view = memoryview(bytearray(8))
    assert near.receive_into(view) == 0  # nothing queued is end of stream
    far.send(b"abc")
    assert near.receive_into(view) == 3
    assert bytes(view[:3]) == b"abc"


def test_a_socket_receive_into_on_a_closed_socket_is_a_connection_error():
    left, right = socket.socketpair()
    right.close()
    transport = SocketTransport(left, "peer", 1)
    transport.close()
    with pytest.raises(XrdConnectionError, match="read from peer:1 failed"):
        transport.receive_into(memoryview(bytearray(4)))


def test_the_memory_transport_is_imported_on_first_use(monkeypatch):
    """``xrdclient.transport`` names the test pipe without loading it eagerly."""
    for name in ("MemoryTransport", "pipe"):
        monkeypatch.delitem(xrdclient.transport.__dict__, name, raising=False)
    assert xrdclient.transport.pipe is pipe
    assert xrdclient.transport.MemoryTransport.__name__ == "MemoryTransport"
    with pytest.raises(AttributeError, match="no attribute 'nothing'"):
        xrdclient.transport.nothing  # noqa: B018


# ---------------------------------------------------------------------------
# Lazily-bound names and pickled errors
# ---------------------------------------------------------------------------


def test_the_crypto_package_lists_and_refuses_names():
    """``dir`` shows every lazily-bound name; an unknown one is an AttributeError."""
    names = dir(xrdclient.crypto)
    assert "Signer" in names and names == sorted(names)
    with pytest.raises(AttributeError, match="no attribute 'nothing'"):
        xrdclient.crypto.nothing  # noqa: B018


def test_a_page_integrity_error_survives_pickling():
    """Crossing a process boundary keeps the offset, the count and the path."""
    back = pickle.loads(pickle.dumps(PageIntegrityError(4096, 3, path="/p")))
    assert (back.offset, back.retries, back.path) == (4096, 3, "/p")
    assert str(back) == str(PageIntegrityError(4096, 3, path="/p"))
