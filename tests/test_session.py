"""The blocking driver and the router that moves it around.

These tests run against a real loopback server (:class:`xrd.testing.FakeServer`)
rather than a mocked transport, so the socket, the session machine and the
router are all exercised together.
"""

from __future__ import annotations

import pytest

from xrd.config import Config
from xrd.errors import ConnectionError as XrdConnectionError
from xrd.errors import NoMechanismError, ProtocolError, RedirectLimitError, TransientError
from xrd.proto import constants as c
from xrd.proto import machine
from xrd.proto import requests as r
from xrd.session.router import Router, _retarget
from xrd.session.sync import RedirectRequired, Result, Session
from xrd.testing import FakeServer

# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def test_connect_brings_the_session_all_the_way_to_ready(server, config):
    with Session.connect(server.url, config=config) as session:
        assert not session.closed
        assert session.endpoint == f"{server.address[0]}:{server.address[1]}"
        assert session.host == server.address[0]
        assert not session.is_tls


def test_the_bring_up_sends_protocol_and_login_before_anything_else(server, config):
    with Session.connect(server.url, config=config):
        pass
    assert server.seen[:2] == [c.kXR_protocol, c.kXR_login]


def test_a_session_that_needs_auth_authenticates(config):
    with FakeServer(sec="&P=host") as srv:
        with Session.connect(srv.url, config=config) as session:
            assert session.mechanism == "host"
        assert srv.seen.count(c.kXR_auth) == 1


def test_a_server_that_wants_more_rounds_than_the_mechanism_has_fails(config):
    """``host`` is a one-shot credential; a second round has no answer."""
    with FakeServer(sec="&P=host") as srv:
        srv.auth_rounds = 1
        with pytest.raises(NoMechanismError):
            Session.connect(srv.url, config=config)


def test_execute_returns_the_body(server, config):
    with Session.connect(server.url, config=config) as session:
        result = session.execute(r.Query(c.kXR_Qconfig, "version"))
        assert isinstance(result, Result)
        assert b"v5.6.0" in bytes(result)
        assert len(result) == len(result.data)


def test_a_server_error_arrives_as_the_matching_builtin(server, config):
    """3011 is the server's ENOENT, so ``except FileNotFoundError`` works."""
    with Session.connect(server.url, config=config) as session:
        with pytest.raises(FileNotFoundError) as excinfo:
            session.execute(r.Stat("/nope"), path="/nope")
        assert "/nope" in str(excinfo.value)


def test_partial_bodies_reach_on_chunk_and_still_accumulate(config):
    with FakeServer(files={"/f": b"x" * 40}) as srv:
        srv.chunk_reads = 10
        with Session.connect(srv.url, config=config) as session:
            opened = session.execute(r.Open("/f", c.kXR_open_read | c.kXR_retstat, 0o644))
            handle = opened.data[:4]
            chunks: list[bytes] = []
            result = session.execute(r.Read(handle, 0, 40), on_chunk=chunks.append)
            assert result.data == b"x" * 40
            assert len(chunks) > 1
            assert b"".join(chunks) == b"x" * 40


def test_a_redirect_is_reported_not_followed(config):
    with FakeServer() as srv:
        srv.redirects[c.kXR_stat] = ("other.example.org", 1094, "tok=1")
        with Session.connect(srv.url, config=config) as session:
            with pytest.raises(RedirectRequired) as excinfo:
                session.execute(r.Stat("/data"), path="/data")
            assert excinfo.value.target.host == "other.example.org"
            assert excinfo.value.target.token == "tok=1"


def test_a_wait_is_slept_through_and_the_request_resent(config):
    with FakeServer(files={"/f": b"ok"}) as srv:
        srv.waits[c.kXR_stat] = 1
        with Session.connect(srv.url, config=config) as session:
            assert session.execute(r.Stat("/f"), path="/f").data
        assert srv.seen.count(c.kXR_stat) == 2


def test_an_endlessly_waiting_server_eventually_gives_up(config):
    tight = Config(username="tester", auth_order=("host",), redirect_limit=2)
    with FakeServer(files={"/f": b"ok"}) as srv:
        srv.waits[c.kXR_stat] = 99
        with Session.connect(srv.url, config=tight) as session:
            with pytest.raises(TransientError) as excinfo:
                session.execute(r.Stat("/f"), path="/f")
            assert excinfo.value.attempts == 3


def test_executing_on_a_closed_session_is_a_connection_error(server, config):
    session = Session.connect(server.url, config=config)
    session.close()
    assert session.closed
    with pytest.raises(XrdConnectionError):
        session.execute(r.Ping())


def test_close_is_idempotent(server, config):
    session = Session.connect(server.url, config=config)
    session.close()
    session.close()
    assert session.closed


def test_notices_starts_empty_and_drains(server, config):
    with Session.connect(server.url, config=config) as session:
        session.execute(r.Ping())
        assert session.notices() == []


def test_repr_names_the_endpoint_and_the_state(server, config):
    with Session.connect(server.url, config=config) as session:
        text = repr(session)
        assert session.endpoint in text
        assert "ready" in text


def test_a_server_that_drops_the_connection_raises_a_connection_error(config):
    with FakeServer(files={"/f": b"ok"}) as srv:
        session = Session.connect(srv.url, config=config)
        srv.disconnect()
        with pytest.raises(XrdConnectionError):
            session.execute(r.Stat("/f"), path="/f")
        session.close()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def test_the_router_connects_lazily(server, config):
    with Router(server.url, config) as router:
        assert not router.connected
        router.execute(r.Ping())
        assert router.connected


def test_the_router_follows_a_redirect_to_the_new_endpoint(config):
    with FakeServer(files={"/f": b"hi"}) as target, FakeServer() as front:
        front.redirects[c.kXR_stat] = (*target.address, "tok=1")
        with Router(front.url, config) as router:
            router.execute(r.Stat("/f"), path="/f")
            assert router.endpoint == f"{target.address[0]}:{target.address[1]}"
        assert c.kXR_stat in target.seen


def test_an_eos_capability_reaches_the_target_as_one_parameter(config):
    """``?&cap.sym=...`` must not arrive as a path with an empty parameter."""
    with FakeServer(files={"/f": b"hi"}) as target, FakeServer() as front:
        front.redirects[c.kXR_stat] = (*target.address, "&cap.sym=abc")
        with Router(front.url, config) as router:
            router.execute(r.Stat("/f"), path="/f")
    assert (c.kXR_stat, "/f?cap.sym=abc") in target.arguments


def test_the_redirect_token_is_folded_into_the_path():
    request = r.Stat("/data/a.root")
    _retarget(request, "xrd.k=1")
    assert request.path == "/data/a.root?xrd.k=1"
    _retarget(request, "xrd.j=2")
    assert request.path == "/data/a.root?xrd.k=1&xrd.j=2"


def test_a_redirect_without_a_token_leaves_the_path_alone():
    request = r.Stat("/data/a.root")
    _retarget(request, "")
    assert request.path == "/data/a.root"


class _Sticky(dict):
    """A redirect table that re-arms itself, so the loop never ends."""

    def pop(self, key, default=None):
        return self.get(key, default)


def test_a_redirect_loop_is_cut_off(config):
    tight = Config(username="tester", auth_order=("host",), redirect_limit=2)
    with FakeServer() as srv:
        srv.redirects = _Sticky({c.kXR_stat: (*srv.address, "tok=1")})
        with Router(srv.url, tight) as router:
            with pytest.raises(RedirectLimitError):
                router.execute(r.Stat("/f"), path="/f")


def test_a_pinned_router_shares_the_connection_and_stays_put(server, config):
    with Router(server.url, config) as router:
        router.execute(r.Ping())
        pinned = router.pin()
        assert pinned.endpoint == router.endpoint
        assert pinned.session is router.session


def test_the_router_reconnects_an_idle_session_that_was_dropped(server, config):
    with Router(server.url, config) as router:
        router.execute(r.Ping())
        first = router.session
        first.close()
        assert not router.connected
        router.execute(r.Ping())  # idempotent, so it just reconnects
        assert router.connected
        assert router.session is not first


def test_the_router_reconnects_after_the_server_drops_the_connection(config):
    with FakeServer(files={"/f": b"ok"}) as srv, Router(srv.url, config) as router:
        router.execute(r.Stat("/f"), path="/f")
        srv.disconnect()
        assert r.Stat("/f").idempotent
        assert router.execute(r.Stat("/f"), path="/f").data


def test_a_non_idempotent_request_is_not_retried(config):
    with FakeServer() as srv, Router(srv.url, config) as router:
        router.execute(r.Ping())
        srv.disconnect()
        assert not r.Mkdir("/x", 0o755).idempotent
        with pytest.raises(TransientError):
            router.execute(r.Mkdir("/x", 0o755), path="/x")


def test_repr_says_whether_it_is_connected(server, config):
    with Router(server.url, config) as router:
        assert "idle" in repr(router)
        router.execute(r.Ping())
        assert "connected" in repr(router)


# ---------------------------------------------------------------------------
# One pump, several streams
#
# A sync session serialises its callers under one lock, so the multiplexing
# paths below are only reachable when the pump answers more than one stream in
# a single turn. These drive them directly with a scripted pump.
# ---------------------------------------------------------------------------


def scripted(session, *batches):
    """Replace the session's I/O turn with a fixed script of event batches."""
    turns = iter(batches)
    session._pump = lambda pathid=0, deadline=None: next(turns)


def test_events_for_another_stream_are_kept_until_that_stream_asks(server, config):
    with Session.connect(server.url, config=config) as session:
        scripted(
            session,
            [
                machine.Ready(b"\x11" * 16),  # no stream of its own: nothing to keep
                machine.Completed(9, r.Ping(), b"theirs"),
                machine.Completed(7, r.Ping(), b"mine"),
            ],
        )
        assert bytes(session._await(7, None)) == b"mine"
        assert bytes(session._await(9, None)) == b"theirs"  # from the inbox, no pump


def test_a_failure_that_belongs_to_no_stream_reaches_whoever_is_waiting(server, config):
    """A session-wide failure has nobody else to go to."""
    with Session.connect(server.url, config=config) as session:
        scripted(session, [machine.Failed(None, None, ProtocolError("the session is done for"))])
        with pytest.raises(ProtocolError, match="the session is done for"):
            session._await(7, None)


def test_an_event_this_version_does_not_know_is_passed_over(server, config):
    """Forward compatibility: an unrecognised event is not a reason to fail."""

    class Curious(machine.Event):
        streamid = 7

    with Session.connect(server.url, config=config) as session:
        scripted(session, [Curious()], [machine.Completed(7, r.Ping(), b"done")])
        assert bytes(session._await(7, None)) == b"done"
