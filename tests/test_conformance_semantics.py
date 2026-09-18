"""Session semantics: the promises that only a failure can break.

The wire tests say what a frame means. These say what an *operation* means
when the connection under it dies halfway through, when the server says "not
yet", and when the client gives up. Two of them matter more than the rest:

* a request that changes something is never sent twice, so a dropped reply
  cannot turn one ``rm`` into two or one ``write`` into a doubled extent;
* a request that changes nothing is retried, because failing an idle ``stat``
  over a server restart is a worse client than a slower one.

Everything here drives the real client over a loopback socket. The failures
are made by hand: a handler that swallows the request and closes the
connection is a server crash between "done it" and "told you", which is the
one moment where at-most-once is the only safe answer.
"""

from __future__ import annotations

import struct
import time

import pytest

from conftest import handshake_reply, login_body, ok, protocol_body
from xrdclient.client.file import File
from xrdclient.config import Config
from xrdclient.errors import ChecksumMismatchError, TransientError, WaitLimitError
from xrdclient.errors import ConnectionError as XrdConnectionError
from xrdclient.errors import TimeoutError as XrdTimeoutError
from xrdclient.flags import OpenFlags
from xrdclient.proto import constants as c
from xrdclient.proto import machine as m
from xrdclient.proto import requests as r
from xrdclient.session.sync import Session
from xrdclient.testing import error, frame

IMPATIENT = Config(
    username="tester",
    auth_order=("host",),
    require_tls=False,
    connect_retries=2,
    retry_backoff=0.0,
    request_timeout=5.0,
    connect_timeout=5.0,
)


@pytest.fixture
def fs(server):
    """A filesystem on the shared server, retrying but never sleeping."""
    from xrdclient import FileSystem

    server.add_file("/data/doomed.root", b"x" * 32)
    server.dirs.add("/data")
    filesystem = FileSystem(server.url, IMPATIENT)
    try:
        yield filesystem
    finally:
        filesystem.close()


def vanish_once(server, opcode):
    """Take the request, answer nothing, and drop the connection.

    A server that has already done the work and dies before the reply leaves
    the building. It disarms itself, so a client that retries gets a real
    answer and the test can tell "retried" from "never got through".
    """

    def handler(conn, sid, params, body):
        server.handlers.pop(opcode, None)
        crash(conn)
        return iter(())

    server.handlers[opcode] = handler


def crash(conn):
    """Cut the connection now.

    ``close`` alone would only mark the socket: the server keeps a file object
    over the same descriptor, so nothing reaches the client until the handler
    thread unwinds - and the client would sit on its timeout instead of seeing
    the reset a crashed server really sends.
    """
    import socket as _socket

    try:
        conn.sock.shutdown(_socket.SHUT_RDWR)
    except OSError:  # pragma: no cover - the peer may already have gone
        pass
    conn.sock.close()


# ---------------------------------------------------------------------------
# At-most-once for mutations, at-least-once for the rest
# ---------------------------------------------------------------------------

#: ``(name, opcode, call)`` for operations that may be repeated safely.
IDEMPOTENT = [
    ("stat", c.kXR_stat, lambda fs: fs.stat("/data/a.root")),
    ("dirlist", c.kXR_dirlist, lambda fs: fs.listdir("/data")),
    ("statx", c.kXR_statx, lambda fs: fs.statx(["/data/a.root"])),
    ("query", c.kXR_query, lambda fs: fs.checksum("/data/a.root")),
]

#: The same, for operations that change something and so must not be.
MUTATIONS = [
    ("rm", c.kXR_rm, lambda fs: fs.remove("/data/doomed.root")),
    ("mv", c.kXR_mv, lambda fs: fs.rename("/data/doomed.root", "/data/moved.root")),
    ("mkdir", c.kXR_mkdir, lambda fs: fs.mkdir("/data/fresh")),
    ("rmdir", c.kXR_rmdir, lambda fs: fs.rmdir("/data/empty")),
    ("truncate", c.kXR_truncate, lambda fs: fs.truncate("/data/doomed.root", 4)),
    ("chmod", c.kXR_chmod, lambda fs: fs.chmod("/data/doomed.root", 0o640)),
]


@pytest.mark.parametrize(("name", "opcode", "call"), IDEMPOTENT, ids=[e[0] for e in IDEMPOTENT])
def test_an_idempotent_request_is_reissued_when_its_reply_is_lost(server, fs, name, opcode, call):
    vanish_once(server, opcode)
    assert call(fs) is not None
    assert server.seen.count(opcode) == 2  # the lost one, and the one that answered


@pytest.mark.parametrize(("name", "opcode", "call"), MUTATIONS, ids=[e[0] for e in MUTATIONS])
def test_a_mutation_is_never_reissued_when_its_reply_is_lost(server, fs, name, opcode, call):
    """One request reached the server. Whether it ran is unknowable here."""
    vanish_once(server, opcode)
    with pytest.raises(TransientError) as caught:
        call(fs)
    assert server.seen.count(opcode) == 1
    assert "failed" in str(caught.value)
    assert caught.value.attempts == 1


def test_the_error_from_an_unrepeatable_request_says_which_one_it_was(server, fs):
    vanish_once(server, c.kXR_rm)
    with pytest.raises(TransientError, match=r"Rm on 127\.0\.0\.1"):
        fs.remove("/data/doomed.root")


def test_a_write_is_not_replayed_over_a_reconnect(server):
    """The one that would corrupt data rather than merely repeat an action."""
    server.add_file("/data/w.root", b"")
    handle = File(server.url.with_path("/data/w.root"), IMPATIENT)
    handle.open(OpenFlags.UPDATE)
    vanish_once(server, c.kXR_write)
    with pytest.raises((TransientError, XrdConnectionError)):
        handle.write(b"payload", 0)
    # The handle was opened for writing and its server has gone, so the close
    # cannot commit the file either - and a writer's close says so rather
    # than letting the caller believe the write landed.
    with pytest.raises((TransientError, XrdConnectionError)):
        handle.close()
    assert server.seen.count(c.kXR_write) == 1


def test_the_filesystem_still_works_after_a_request_it_would_not_replay(server, fs):
    """Refusing to retry ends the request, not the client."""
    vanish_once(server, c.kXR_rm)
    with pytest.raises(TransientError):
        fs.remove("/data/doomed.root")
    assert fs.stat("/data/a.root").size == len(b"hello world")


def test_the_session_a_reconnection_replaces_gives_its_socket_back(server, fs):
    """A dropped session is closed as far as the protocol goes; the file
    descriptor under it has to go too, or a flapping server leaks one per
    reconnection until the process runs out."""
    vanish_once(server, c.kXR_stat)
    dead = fs._router.session
    fs.stat("/data/a.root")
    assert dead is not fs._router.session
    assert dead._t.closed


def test_giving_up_counts_the_attempts_it_made(server, fs):
    """``attempts`` is what a caller logs; it has to be the real number."""

    def handler(conn, sid, params, body):
        crash(conn)
        return iter(())

    server.handlers[c.kXR_stat] = handler
    with pytest.raises(TransientError) as caught:
        fs.stat("/data/a.root")
    assert caught.value.attempts == IMPATIENT.connect_retries + 1
    assert server.seen.count(c.kXR_stat) == IMPATIENT.connect_retries + 1


# ---------------------------------------------------------------------------
# A session that has lost its transport
# ---------------------------------------------------------------------------


def test_a_closed_session_refuses_work_rather_than_reconnecting(server):
    session = Session.connect(server.url, config=IMPATIENT)
    session.close()
    assert session.closed
    with pytest.raises(XrdConnectionError, match=r"session to 127\.0\.0\.1"):
        session.execute(r.Stat("/data/a.root"), path="/data/a.root")


def test_closing_a_session_twice_is_allowed(server):
    session = Session.connect(server.url, config=IMPATIENT)
    session.close()
    session.close()
    assert session.closed


def test_a_pinned_router_refuses_to_reconnect_underneath_a_file_handle(server):
    """The handle only exists on the connection that opened it."""
    from xrdclient.session.router import Router

    router = Router(server.url, IMPATIENT, reconnect=False)
    try:
        router.execute(r.Stat("/data/a.root"), path="/data/a.root")
        router.session.close()
        with pytest.raises(XrdConnectionError, match="was lost"):
            router.execute(r.Stat("/data/a.root"), path="/data/a.root")
    finally:
        router.close()


def test_closing_the_machine_ends_the_requests_in_flight():
    """Whatever was outstanding fails; nothing is left waiting for a reply."""
    machine = m.SessionMachine(host="srv", config=Config(username="t", auth_order=("host",)))
    machine.start()
    machine.data_to_send()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body()))
    machine.receive_data(ok(2, login_body()))
    machine.data_to_send()
    list(machine.events())
    sid = machine.submit(r.Stat("/data/a.root"), path="/data/a.root")
    machine.data_to_send()
    machine.receive_data(b"")  # the peer went away
    events = list(machine.events())
    failed = [e for e in events if isinstance(e, m.Failed) and e.streamid == sid]
    assert failed and isinstance(failed[0].error, XrdConnectionError)
    assert any(isinstance(e, m.Disconnected) for e in events)


def test_a_reply_arriving_after_the_caller_gave_up_is_dropped():
    """The streamid is reusable; a late answer must not land on its next use."""
    machine = m.SessionMachine(host="srv", config=Config(username="t", auth_order=("host",)))
    machine.start()
    machine.data_to_send()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body()))
    machine.receive_data(ok(2, login_body()))
    machine.data_to_send()
    list(machine.events())
    sid = machine.submit(r.Stat("/data/a.root"), path="/data/a.root")
    machine.release(sid)
    machine.receive_data(error(sid, 3011, "too late"))
    assert list(machine.events()) == []


# ---------------------------------------------------------------------------
# Being told to wait
# ---------------------------------------------------------------------------


def test_a_wait_is_slept_through_and_the_request_reissued(server, fs):
    """``kXR_wait`` means "ask me again", so the same request goes twice."""
    server.waits[c.kXR_stat] = 2
    assert fs.stat("/data/a.root").size == len(b"hello world")
    assert server.seen.count(c.kXR_stat) == 3


def test_a_server_that_only_ever_says_wait_is_given_up_on(server, fs):
    server.waits[c.kXR_stat] = IMPATIENT.redirect_limit + 5
    with pytest.raises(WaitLimitError, match="kept asking to wait"):
        fs.stat("/data/a.root")
    assert server.seen.count(c.kXR_stat) == IMPATIENT.redirect_limit + 1


def test_a_busy_server_is_not_mistaken_for_a_broken_connection(server, fs):
    """``kXR_wait`` past the budget must not start a reconnection loop.

    :class:`~xrdclient.WaitLimitError` is a :class:`~xrdclient.TransientError`, and the
    router retries those - so without an exception for it the budget would be
    spent once per reconnection attempt and the server asked four times over.
    """
    server.waits[c.kXR_stat] = 500
    with pytest.raises(WaitLimitError):
        fs.stat("/data/a.root")
    assert server.seen.count(c.kXR_stat) == IMPATIENT.redirect_limit + 1


def test_giving_up_on_a_wait_says_how_many_times_it_asked(server, fs):
    server.waits[c.kXR_stat] = 500
    with pytest.raises(WaitLimitError) as caught:
        fs.stat("/data/a.root")
    assert caught.value.attempts == IMPATIENT.redirect_limit + 1
    assert isinstance(caught.value, TransientError)


def test_a_waitresp_parks_the_request_instead_of_repeating_it(server, fs):
    """The answer arrives unsolicited on the same stream; asking again would
    have the server do the work twice."""

    def handler(conn, sid, params, body):
        yield frame(sid, c.kXR_waitresp, struct.pack(">i", 0))
        yield frame(sid, c.kXR_ok, conn._stat_line("/data/a.root"))

    server.handlers[c.kXR_stat] = handler
    assert fs.stat("/data/a.root").size == len(b"hello world")
    assert server.seen.count(c.kXR_stat) == 1


def test_an_absurd_wait_is_capped_rather_than_obeyed():
    """A server asking for an hour must not hang the caller for an hour."""
    config = Config(username="t", auth_order=("host",), wait_cap=0.25)
    machine = m.SessionMachine(host="srv", config=config)
    machine.start()
    machine.data_to_send()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body()))
    machine.receive_data(ok(2, login_body()))
    machine.data_to_send()
    list(machine.events())
    sid = machine.submit(r.Stat("/d/f"), path="/d/f")
    machine.data_to_send()
    machine.receive_data(frame(sid, c.kXR_wait, struct.pack(">i", 3600) + b"busy\x00"))
    waiting = [e for e in machine.events() if isinstance(e, m.Waiting)]
    assert waiting[0].seconds == 0.25
    assert waiting[0].resend


# ---------------------------------------------------------------------------
# The stall deadline: a peer that is neither answering nor gone
# ---------------------------------------------------------------------------

#: Long enough that nothing healthy trips it, short enough to test with.
IMPATIENT_STALL = IMPATIENT.evolve(stall_deadline=0.5, request_timeout=30.0)


def test_a_server_that_goes_quiet_is_given_up_on(server):
    """No reply at all: the socket is open, the peer is running, nothing is
    coming. Only an absolute deadline ends this."""

    def silence(conn, sid, params, body):
        return iter(())

    server.handlers[c.kXR_stat] = silence
    with Session.connect(server.url, config=IMPATIENT_STALL) as session:
        started = time.monotonic()
        with pytest.raises(XrdTimeoutError, match="stall deadline"):
            session.execute(r.Stat("/data/a.root"), path="/data/a.root")
        assert time.monotonic() - started < IMPATIENT_STALL.request_timeout


def test_a_server_that_dribbles_is_given_up_on_too(server):
    """The case a per-read timeout cannot catch: every read succeeds, and the
    operation still never finishes."""

    def dribble(conn, sid, params, body):
        # A header promising far more than will ever arrive, then one byte at
        # a time: every read returns something, so no read ever times out.
        yield frame(sid, c.kXR_ok, b"\x00" * 4096)[:8]
        for _ in range(4096):
            time.sleep(0.05)
            yield b"\x00"

    server.handlers[c.kXR_stat] = dribble
    with Session.connect(server.url, config=IMPATIENT_STALL) as session:
        with pytest.raises(XrdTimeoutError, match="stall deadline"):
            session.execute(r.Stat("/data/a.root"), path="/data/a.root")


def test_a_wait_restarts_the_deadline_rather_than_spending_it(server):
    """A server that says "not yet" is not stalling, so the delay it asked
    for must not be charged against the cutoff it would otherwise trip."""

    def slow(conn, sid, params, body):
        yield frame(sid, c.kXR_wait, struct.pack(">i", 1) + b"staging\x00")
        yield frame(sid, c.kXR_ok, conn._stat_line("/data/a.root"))

    server.handlers[c.kXR_stat] = slow
    with Session.connect(server.url, config=IMPATIENT_STALL) as session:
        # The whole operation outlasts the deadline; the wait is why.
        assert session.execute(r.Stat("/data/a.root"), path="/data/a.root").data


def test_a_deferred_reply_extends_the_deadline_without_a_resend(server):
    """``kXR_waitresp`` is "the answer is coming later" - the deadline has to
    cover the delay the server named, and nothing is sent again."""

    def deferred(conn, sid, params, body):
        yield frame(sid, c.kXR_waitresp, struct.pack(">i", 1))
        time.sleep(0.8)  # past the bare deadline, inside the extended one
        yield frame(sid, c.kXR_ok, conn._stat_line("/data/a.root"))

    server.handlers[c.kXR_stat] = deferred
    with Session.connect(server.url, config=IMPATIENT_STALL) as session:
        assert session.execute(r.Stat("/data/a.root"), path="/data/a.root").data
    assert server.seen.count(c.kXR_stat) == 1


def test_the_parking_a_server_asks_for_is_bounded_in_total(server):
    """Each wait restarts the deadline, so without this a server could park a
    caller forever one honest delay at a time."""

    def parked(conn, sid, params, body):
        yield frame(sid, c.kXR_wait, struct.pack(">i", 10) + b"come back\x00")

    server.handlers[c.kXR_stat] = parked
    with Session.connect(server.url, config=IMPATIENT_STALL.evolve(wait_budget=0.5)) as session:
        with pytest.raises(WaitLimitError, match="budget for one operation"):
            session.execute(r.Stat("/data/a.root"), path="/data/a.root")


def test_the_deadline_can_be_turned_off(server):
    """A caller who would rather wait forever than lose a transfer."""
    with Session.connect(server.url, config=IMPATIENT.evolve(stall_deadline=0)) as session:
        assert session._armed() is None
        assert session.execute(r.Stat("/data/a.root"), path="/data/a.root").data


# ---------------------------------------------------------------------------
# Paged writes
# ---------------------------------------------------------------------------


def test_a_page_the_server_rejects_is_an_error_here_not_a_short_write(server):
    """``kXR_pgwrite`` exists so corruption is loud; it must stay loud."""
    server.add_file("/data/p.root", b"")

    def handler(conn, sid, params, body):
        yield error(sid, 3019, "checksum error on pages [0]")

    handle = File(server.url.with_path("/data/p.root"), IMPATIENT)
    handle.open(OpenFlags.UPDATE)
    with handle:
        server.handlers[c.kXR_pgwrite] = handler
        with pytest.raises(ChecksumMismatchError, match="checksum error"):
            handle.pgwrite(b"a" * 4096, 0)
    assert bytes(server.files["/data/p.root"]) == b""


def test_a_paged_write_of_nothing_is_not_sent_at_all(server):
    server.add_file("/data/p2.root", b"")
    handle = File(server.url.with_path("/data/p2.root"), IMPATIENT)
    handle.open(OpenFlags.UPDATE)
    with handle:
        assert handle.pgwrite(b"", 0) == 0
    assert c.kXR_pgwrite not in server.seen


def test_a_retried_page_is_marked_as_one():
    """The flag tells the server this page is a second attempt, not new data."""
    from xrdclient.proto.frames import encode

    body = encode(r.PgWrite(b"HDL0", 0, b"pageful", retry=True), 4)
    assert body[c.REQUEST_HDRLEN - 4 : c.REQUEST_HDRLEN] == struct.pack(">i", len(b"pageful"))
    assert r.PgWrite(b"HDL0", 0, b"x", retry=True).reqflags == c.kXR_pgRetry
    assert r.PgWrite(b"HDL0", 0, b"x").reqflags == 0
