"""Conformance tests for the session driver.

Everything a server can send that is *not* the answer to the question asked:
unsolicited notices, an answer smuggled inside one, a TLS upgrade in the
middle of the bring-up, and a connection that is already gone by the time
someone closes it. The paper-server tests drive :class:`~xrdclient.session.sync.Session`
over an in-memory pipe, which is the only way to reach the TLS branch without
a certificate and a real handshake.
"""

from __future__ import annotations

import struct
import threading
from collections import deque

import pytest

from conftest import handshake_reply, login_body, ok, protocol_body
from xrdclient.config import Config
from xrdclient.errors import ConnectionError as XrdConnectionError
from xrdclient.proto import constants as c
from xrdclient.proto import machine as m
from xrdclient.proto import requests as r
from xrdclient.session.sync import Session
from xrdclient.testing import FakeServer, frame
from xrdclient.transport.memory import MemoryTransport

# ---------------------------------------------------------------------------
# Unsolicited server chatter
# ---------------------------------------------------------------------------


def attn(action: int, params: bytes = b"") -> bytes:
    """A ``kXR_attn``, which carries no stream id of its own."""
    return frame(0, c.kXR_attn, struct.pack(">i", action) + params)


def test_an_unsolicited_notice_is_recorded_and_does_not_answer_the_request(config):
    """``kXR_attn`` interleaves with a reply; only the reply completes it."""

    def chatty(conn, sid, params, body):
        yield attn(c.kXR_asyncms, b"going down at 17:00\x00")
        yield frame(sid, c.kXR_ok, b"")

    with FakeServer() as srv:
        srv.handlers[c.kXR_ping] = chatty
        with Session.connect(srv.url, config=config) as session:
            assert session.execute(r.Ping()).data == b""
            notices = session.notices()
            assert [n.action for n in notices] == [c.kXR_asyncms]
            assert notices[0].message == "going down at 17:00"
            assert session.notices() == []  # drained


def test_several_notices_arrive_in_the_order_they_were_sent(config):
    def chatty(conn, sid, params, body):
        yield attn(c.kXR_asyncwt, b"busy\x00")
        yield attn(c.kXR_asyncgo, b"go\x00")
        yield frame(sid, c.kXR_ok, b"")

    with FakeServer() as srv:
        srv.handlers[c.kXR_ping] = chatty
        with Session.connect(srv.url, config=config) as session:
            session.execute(r.Ping())
            assert [n.action for n in session.notices()] == [c.kXR_asyncwt, c.kXR_asyncgo]


def test_a_reply_delivered_inside_an_attn_completes_the_request(config):
    """``kXR_asynresp`` wraps a whole response; unwrapping it is the point."""

    def deferred(conn, sid, params, body):
        payload = b"late but correct"
        inner = struct.pack(">HHI", sid, c.kXR_ok, len(payload)) + payload
        yield attn(c.kXR_asynresp, struct.pack(">i", 0) + inner)

    with FakeServer() as srv:
        srv.handlers[c.kXR_ping] = deferred
        with Session.connect(srv.url, config=config) as session:
            assert session.execute(r.Ping()).data == b"late but correct"
            assert session.notices() == []  # it was an answer, not an announcement


def test_an_attn_too_short_to_hold_a_response_is_treated_as_a_notice(config):
    """A truncated ``kXR_asynresp`` must not be decoded as a header anyway."""

    def stunted(conn, sid, params, body):
        yield attn(c.kXR_asynresp, b"\x00\x00")
        yield frame(sid, c.kXR_ok, b"")

    with FakeServer() as srv:
        srv.handlers[c.kXR_ping] = stunted
        with Session.connect(srv.url, config=config) as session:
            session.execute(r.Ping())
            assert [n.action for n in session.notices()] == [c.kXR_asynresp]


# ---------------------------------------------------------------------------
# A session over a pipe, for the parts a socket cannot reach
# ---------------------------------------------------------------------------


def paper_session(config, *, want_tls: bool = False, flags: int = 0, sec: str = ""):
    """A brought-up session over an in-memory pipe, and the pipe.

    The replies are queued before the bring-up runs, which is legitimate: the
    transport hands them over one at a time and the machine buffers, exactly
    as a socket would. Reaching for the private ``_bringup`` is the price of
    having no socket to connect through.
    """
    transport = MemoryTransport(deque(), deque(), host="paper", port=1094)
    transport.feed(handshake_reply())
    transport.feed(ok(1, protocol_body(flags=flags)))
    transport.feed(ok(2, login_body(sec=sec)))
    machine = m.SessionMachine(
        host="paper", port=1094, config=config, username="tester", want_tls=want_tls
    )
    session = Session(transport, machine, config)
    machine.start()
    session._bringup()
    return session, transport


def test_a_session_that_asked_for_tls_upgrades_before_it_logs_in(config):
    session, transport = paper_session(config, want_tls=True, flags=c.kXR_haveTLS)
    assert transport.tls_started
    assert session.is_tls
    assert not session.closed
    assert session.host == "paper" and session.port == 1094


def test_a_server_that_demands_tls_gets_it_even_when_the_client_did_not_ask(config):
    session, transport = paper_session(config, flags=c.kXR_haveTLS | c.kXR_gotoTLS)
    assert transport.tls_started and session.is_tls


def test_tls_asked_for_but_not_offered_stops_the_bring_up(config):
    with pytest.raises(Exception, match="does not offer it"):
        paper_session(config, want_tls=True, flags=0)


def test_a_machine_that_is_already_finished_is_not_taken_for_a_live_session(config):
    """The guard behind the bring-up loop: no state means no session."""
    transport = MemoryTransport(deque(), deque())
    machine = m.SessionMachine(host="paper", port=1094, config=config, username="tester")
    machine.state = m.State.CLOSED
    with pytest.raises(XrdConnectionError, match="bring-up ended in state CLOSED"):
        Session(transport, machine, config)._bringup()


def test_closing_a_session_whose_connection_already_died_is_not_an_error(config):
    """``kXR_endsess`` is a courtesy; a dead socket cannot refuse it."""
    session, transport = paper_session(config)
    transport.close()
    session.close()
    assert session.closed


# ---------------------------------------------------------------------------
# One connection, many callers
# ---------------------------------------------------------------------------


def test_one_session_serialises_the_threads_that_share_it(config):
    """Every answer reaches the caller that asked for it, under contention."""
    with FakeServer(files={f"/f{i}": bytes([i]) * (i + 1) for i in range(8)}) as srv:
        with Session.connect(srv.url, config=config) as session:
            wrong: list[str] = []

            def ask(index: int) -> None:
                for _ in range(10):
                    got = session.execute(r.Stat(f"/f{index}"), path=f"/f{index}")
                    size = int(got.data.split()[1])
                    if size != index + 1:
                        wrong.append(f"/f{index} -> {size}")

            threads = [threading.Thread(target=ask, args=(i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            assert not wrong
            assert srv.seen.count(c.kXR_stat) == 80


def test_a_config_with_no_wait_budget_still_answers_the_first_time(config):
    """``redirect_limit=0`` bounds waits, not ordinary requests."""
    strict = Config(username="tester", auth_order=("host",), require_tls=False, redirect_limit=0)
    with FakeServer(files={"/f": b"x"}) as srv:
        with Session.connect(srv.url, config=strict) as session:
            assert session.execute(r.Stat("/f"), path="/f").data
