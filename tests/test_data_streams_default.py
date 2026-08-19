"""The on-by-default data sub-streams.

A file opened over the binary protocol now binds a data sub-stream at open and
moves its bulk reads and writes over it, so a plain ``File.open`` is already
multi-stream. Three things have to hold:

* the default is one extra stream, and ``XRD_SUBSTREAMSPERCHANNEL`` sets it (the
  official client counts the control link, so its 1 is our 0);
* "arrival" routing puts the *whole* request frame on the bound data socket,
  not just a write's payload, so a server that keys a sub-stream on the
  connection a request came in on (BriX) serves it there;
* it is best-effort - a server that will not serve the bound op leaves the
  transfer byte-exact by falling back, first to the standard split and then to
  the control link.

Both kinds of server are here: the shared ``server`` fixture is the standard,
push-only kind, and ``arrival_server`` is one that answers what arrives on the
path, which is what a BriX gateway does.
"""
from __future__ import annotations

import struct

import pytest

import xrd
from conftest import handshake_reply, login_body, ok, protocol_body
from xrd.config import Config
from xrd.flags import OpenFlags
from xrd.proto import constants as c
from xrd.proto import machine as m
from xrd.proto import requests as r
from xrd.testing import error


@pytest.fixture
def arrival_server():
    """A server of the gateway kind: it serves what arrives on a data path.

    The stock daemon reads only a write's payload off a bound socket and sends
    only bulk replies back down it; this one is asked *on* it, and the request
    it answers is the one it read there.
    """
    from xrd.testing import FakeServer

    with FakeServer(files={"/data/a.root": b"hello world"}) as srv:
        srv.serves_arrivals = True
        yield srv

# --------------------------------------------------------------------------
# The default
# --------------------------------------------------------------------------


def test_the_default_is_one_extra_stream():
    assert Config().data_streams == 1


def test_the_substreams_env_sets_the_extra_count(monkeypatch):
    # The official client's total includes the control link, so 1 is "no extra".
    monkeypatch.setenv("XRD_SUBSTREAMSPERCHANNEL", "1")
    assert Config().data_streams == 0
    monkeypatch.setenv("XRD_SUBSTREAMSPERCHANNEL", "4")
    assert Config().data_streams == 3
    monkeypatch.setenv("XRD_SUBSTREAMSPERCHANNEL", "0")
    assert Config().data_streams == 0


# --------------------------------------------------------------------------
# Arrival routing in the machine
# --------------------------------------------------------------------------


def _ready() -> m.SessionMachine:
    machine_ = m.SessionMachine(
        host="srv.example.org",
        config=Config(username="tester", auth_order=("host",)),
    )
    machine_.start()
    machine_.data_to_send()
    machine_.receive_data(handshake_reply())
    machine_.receive_data(ok(1, protocol_body()))
    machine_.receive_data(ok(2, login_body()))
    machine_.data_to_send()
    list(machine_.events())
    return machine_


def test_an_arrival_read_puts_the_whole_frame_on_the_path():
    machine_ = _ready()
    machine_.submit(r.Read(b"fh01", 0, 5, 1), arrive_on_path=True)
    # Nothing on the control link; the request frame is on the data socket.
    assert machine_.data_to_send() == b""
    frame = machine_.path_data_to_send(1)
    assert frame, "the read frame was not routed onto the path"
    assert struct.unpack(">H", frame[2:4])[0] == c.kXR_read


def test_an_arrival_write_carries_header_and_data_on_the_path():
    machine_ = _ready()
    machine_.submit(r.Write(b"fh01", 0, b"bulk", 1), arrive_on_path=True)
    assert machine_.data_to_send() == b""
    frame = machine_.path_data_to_send(1)
    assert struct.unpack(">H", frame[2:4])[0] == c.kXR_write
    assert frame.endswith(b"bulk"), "the write payload must ride the same socket"


def test_without_arrival_the_split_is_unchanged():
    # The standard model still stands for the manual bind_data_path API.
    machine_ = _ready()
    machine_.submit(r.Write(b"fh01", 0, b"bulk", 1))
    assert b"bulk" not in machine_.data_to_send()
    assert machine_.path_data_to_send(1) == b"bulk"


# --------------------------------------------------------------------------
# End to end against a standard (push-only) server: the fallback
# --------------------------------------------------------------------------


def _fast_multistream() -> Config:
    # data_streams on, with a tiny data-stream timeout so the bound attempt
    # against a server that will not serve it fails fast into the control-link
    # fallback instead of waiting out the default probe window.
    return Config(
        username="tester",
        auth_order=("host",),
        require_tls=False,
        data_streams=1,
        data_stream_timeout=0.3,
    )


def _mute_config_queries(server):
    """Leave the arrival question unanswerable, so the trial decides.

    The session first *asks* whether arrivals are served (``kXR_Qconfig
    brix.substreams``), and the fake answers - so the tests about the
    trial-and-timeout fallback need a server whose answer is an error,
    which settles nothing.
    """

    def refuse(conn, sid, params, body):
        yield error(sid, 3006, "queries not served here")

    server.handlers[c.kXR_query] = refuse


def test_a_default_open_binds_a_stream(server):
    with xrd.File(f"{server.url}//data/a.root", _fast_multistream()) as fh:
        assert fh._data_paths, "open did not bind an automatic data stream"


def test_a_multistream_read_is_byte_exact_via_the_standard_split(server):
    # The fake server is push-only and says so when asked, so the read stays
    # on a data path via the standard split rather than giving up on one, and
    # the bytes are the bytes either way.
    with xrd.File(f"{server.url}//data/a.root", _fast_multistream()) as fh:
        assert fh.read() == b"hello world"
        assert fh._router.session.arrives_on_path is False
        assert fh._multistream is True, "the standard split still works here"
        assert fh._data_paths, "the file gave up a data path it could have kept"


def test_a_stock_server_answers_the_question_and_keeps_its_path(server):
    # The server disclaims arrival routing when asked, so no request is ever
    # abandoned on the data socket and the socket never has to be sacrificed.
    with xrd.File(f"{server.url}//data/a.root", _fast_multistream()) as fh:
        first = list(fh._data_paths)
        assert fh.read() == b"hello world"
        assert fh._router.session.arrives_on_path is False
        assert fh._data_paths == first, "the ask should have spared the socket"


def test_a_server_that_advertises_but_will_not_serve_falls_back(server):
    # The advertisement is taken at its word, and the word is checked: the
    # trial that follows fails, the socket goes, and the read is byte-exact
    # via the standard split on a replacement path.
    server.config_values["brix.substreams"] = "rw"
    with xrd.File(f"{server.url}//data/a.root", _fast_multistream()) as fh:
        assert fh.read() == b"hello world"
        assert fh._router.session.arrives_on_path is False


def test_the_stream_left_waiting_is_released_with_the_socket(server):
    # With no answer to the question, the trial decides. The request that
    # went down the abandoned socket may still be answered there, so the
    # socket goes; what it was carrying must not be left in flight for the
    # rest of the session, and its id is free to use again.
    _mute_config_queries(server)
    with xrd.File(f"{server.url}//data/a.root", _fast_multistream()) as fh:
        first = list(fh._data_paths)
        assert fh.read() == b"hello world"
        machine = fh._router.session._m
        assert machine.in_flight == 0, "the abandoned request is still pending"
        assert fh._data_paths != first, "the dead path was reused"
        assert fh._data_paths, "no replacement was bound"


def test_a_server_that_will_not_serve_it_is_asked_once_per_connection(server, monkeypatch):
    # Finding this out costs a whole data_stream_timeout, so it is asked of the
    # server, not of every file opened on it.
    from xrd.session.sync import Session

    asked = []
    execute = Session.execute

    def counting(self, request, *, path="", on_chunk=None, arrive_on_path=False):
        if arrive_on_path:
            asked.append(request)
        return execute(
            self, request, path=path, on_chunk=on_chunk, arrive_on_path=arrive_on_path
        )

    monkeypatch.setattr(Session, "execute", counting)
    config = _fast_multistream()
    sessions = []
    for _ in range(3):
        with xrd.File(f"{server.url}//data/a.root", config) as fh:
            assert fh.read() == b"hello world"
            sessions.append(fh._router.session)
    assert len({id(s) for s in sessions}) == 1, "the pool handed out a new session"
    assert len(asked) == 1, f"{len(asked)} files paid the same timeout"


# --------------------------------------------------------------------------
# End to end against a server that does serve what arrives on the path
# --------------------------------------------------------------------------


def test_a_server_that_serves_the_arrival_is_asked_there(arrival_server):
    with xrd.File(f"{arrival_server.url}//data/a.root", _fast_multistream()) as fh:
        assert fh.read() == b"hello world"
        assert fh._router.session.arrives_on_path is True
        assert fh._data_paths, "the path that served the read was given up"
        assert fh._multistream is True


def test_an_arrival_server_takes_the_write_on_the_same_socket(arrival_server):
    # Header and payload travel together here, which is the whole point: the
    # server reads both off the connection the request came in on.
    path = "/data/arr_w.root"
    fh = xrd.File(f"{arrival_server.url}/{path}", _fast_multistream())
    fh.open(OpenFlags.NEW | OpenFlags.UPDATE)
    try:
        assert fh.write(b"payload", 0) == 7
    finally:
        fh.close()
    assert bytes(arrival_server.files[path]) == b"payload"
    assert arrival_server.serves_arrivals is True


# --------------------------------------------------------------------------
# Servers that will not bind at all
# --------------------------------------------------------------------------


def test_a_server_that_refuses_to_bind_stays_on_the_control_link(server):
    def refuse(conn, sid, params, body):
        yield error(sid, 3013, "no sub-streams here")

    server.handlers[c.kXR_bind] = refuse
    with xrd.File(f"{server.url}//data/a.root", _fast_multistream()) as fh:
        assert not fh._data_paths, "a refused bind must leave no path behind"
        assert fh.read() == b"hello world"


def test_a_bind_that_hands_back_no_path_id_is_no_path(server, monkeypatch):
    # A server answering the bind but naming no path leaves nothing to send
    # down; the file must notice rather than route to path 0 by accident.
    from xrd.session.sync import Session

    monkeypatch.setattr(Session, "bind_data_path", lambda self: 0)
    with xrd.File(f"{server.url}//data/a.root", _fast_multistream()) as fh:
        assert not fh._data_paths
        assert fh.read() == b"hello world"


def test_a_file_that_cannot_rebind_finishes_on_the_control_link(server, monkeypatch):
    # The arrival attempt costs the path it was sent on. If a replacement
    # cannot be had, the read still has to happen - on the control link.
    from xrd.errors import XRootDError
    from xrd.session.sync import Session

    _mute_config_queries(server)
    real = Session.bind_data_path
    bound = []

    def once(self):
        bound.append(1)
        if len(bound) > 1:
            raise XRootDError("no more sub-streams")
        return real(self)

    monkeypatch.setattr(Session, "bind_data_path", once)
    with xrd.File(f"{server.url}//data/a.root", _fast_multistream()) as fh:
        assert fh._data_paths, "the first bind should have worked"
        assert fh.read() == b"hello world"
        assert not fh._data_paths, "the abandoned path was not let go"


def test_a_multistream_write_is_byte_exact_via_fallback(server):
    path = "/data/ms_w.root"
    fh = xrd.File(f"{server.url}/{path}", _fast_multistream())
    fh.open(OpenFlags.NEW | OpenFlags.UPDATE)
    try:
        assert fh.write(b"payload", 0) == 7
    finally:
        fh.close()
    assert bytes(server.files[path]) == b"payload"
