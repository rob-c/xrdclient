"""``kXR_bind`` data paths: the wire, the machine, the session and the file.

A data path is a second connection that carries a file's bytes while the
first one keeps carrying its questions. The protocol splits one request
across two sockets, so the tests here are mostly about who sends what where.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

import xrdclient
from conftest import error, handshake_reply, login_body, ok, protocol_body
from xrdclient.config import Config
from xrdclient.errors import ConnectionError as XrdConnectionError
from xrdclient.errors import ProtocolError, ServerError
from xrdclient.flags import OpenFlags
from xrdclient.proto import constants as c
from xrdclient.proto import machine as m
from xrdclient.proto import requests as r
from xrdclient.proto import responses as rp
from xrdclient.proto.frames import encode
from xrdclient.session import Session
from xrdclient.testing import FakeServer

_HDR = struct.Struct(">HH16sI")


def parts(request, streamid: int = 4):
    """``(params, dlen, body)`` of an encoded request."""
    frame = encode(request, streamid)
    _, _, params, dlen = _HDR.unpack(frame[:24])
    return params, dlen, frame[24:]


# --------------------------------------------------------------------------
# The wire
# --------------------------------------------------------------------------


def test_a_read_without_a_path_asks_for_nothing_extra():
    _, dlen, body = parts(r.Read(b"fh01", 0, 100))
    assert (dlen, body) == (0, b"")


def test_a_read_on_a_path_carries_read_args():
    params, dlen, body = parts(r.Read(b"fh01", 0, 100, 3))
    assert body == b"\x03" + bytes(7)
    assert dlen == 8
    # The handle, offset and length still say what to read.
    assert params[:4] == b"fh01"
    assert struct.unpack(">qi", params[4:16]) == (0, 100)


def test_a_write_without_a_path_puts_its_data_in_the_frame():
    params, dlen, body = parts(r.Write(b"fh01", 7, b"payload"))
    assert (params[12], dlen, body) == (0, 7, b"payload")


def test_a_write_on_a_path_declares_its_data_and_sends_it_elsewhere():
    request = r.Write(b"fh01", 7, b"payload", 2)
    params, dlen, body = parts(request)
    assert params[12] == 2
    # dlen still counts the data: the server sizes the read off the path from
    # the header it got on the control link.
    assert dlen == len(b"payload")
    assert body == b""
    assert request.path_data() == b"payload"


def test_a_paged_write_on_a_path_splits_the_same_way():
    request = r.PgWrite(b"fh01", 0, b"pages", pathid=5)
    params, dlen, body = parts(request)
    assert (params[12], dlen, body) == (5, 5, b"")
    assert request.path_data() == b"pages"
    assert r.PgWrite(b"fh01", 0, b"pages").path_data() == b""


def test_the_reading_opcodes_are_answered_on_the_path_and_the_writing_ones_are_not():
    assert r.Read(b"fh01", 0, 1).reply_on_path
    assert r.ReadV([(b"fh01", 0, 1)]).reply_on_path
    assert r.PgRead(b"fh01", 0, 1).reply_on_path
    assert not r.Write(b"fh01", 0, b"x").reply_on_path
    assert not r.PgWrite(b"fh01", 0, b"x").reply_on_path
    assert not r.Ping().reply_on_path
    assert r.Ping().pathid == 0
    assert r.Ping().path_data() == b""


def test_readv_names_its_path_in_the_last_parameter_byte():
    params, _, _ = parts(r.ReadV([(b"fh01", 0, 4)], 9))
    assert params[15] == 9


def test_a_bind_reply_is_one_path_id():
    assert rp.parse_bind(b"\x07") == 7


def test_a_bind_reply_of_zero_is_refused():
    with pytest.raises(ProtocolError, match="control link"):
        rp.parse_bind(b"\x00")


# --------------------------------------------------------------------------
# The machine
# --------------------------------------------------------------------------


def machine(**kwargs) -> m.SessionMachine:
    return m.SessionMachine(
        host="srv.example.org",
        config=Config(username="tester", auth_order=("host",)),
        **kwargs,
    )


def bind_up(sessid: bytes = b"\x11" * 16) -> m.SessionMachine:
    """A machine brought all the way up as a data connection."""
    machine_ = machine(bind_to=sessid)
    machine_.start()
    machine_.data_to_send()
    machine_.receive_data(handshake_reply())
    machine_.receive_data(ok(1, protocol_body()))
    return machine_


def test_a_data_connection_binds_instead_of_logging_in():
    machine_ = machine(bind_to=b"\x11" * 16)
    machine_.start()
    machine_.data_to_send()
    machine_.receive_data(handshake_reply())
    machine_.receive_data(ok(1, protocol_body()))
    assert machine_.state is m.State.BIND
    sent = machine_.data_to_send()
    opcode = struct.unpack(">H", sent[2:4])[0]
    assert opcode == c.kXR_bind
    assert sent[4:20] == b"\x11" * 16


def test_a_bound_connection_is_ready_with_a_path_id():
    machine_ = bind_up()
    machine_.data_to_send()
    machine_.receive_data(ok(2, b"\x04"))
    assert machine_.state is m.State.READY
    assert machine_.pathid == 4
    assert machine_.session_id == b""  # a data path has no session of its own


def test_a_server_that_refuses_the_bind_fails_the_connection():
    machine_ = bind_up()
    machine_.data_to_send()
    machine_.receive_data(error(2, 3010, "no sess"))
    failure = [e for e in machine_.events() if isinstance(e, m.Failed)]
    assert isinstance(failure[0].error, ServerError)


def ready_machine() -> m.SessionMachine:
    machine_ = machine()
    machine_.start()
    machine_.data_to_send()
    machine_.receive_data(handshake_reply())
    machine_.receive_data(ok(1, protocol_body()))
    machine_.receive_data(ok(2, login_body()))
    machine_.data_to_send()
    list(machine_.events())
    return machine_


def test_the_data_of_a_write_is_queued_for_its_path_and_not_for_the_link():
    machine_ = ready_machine()
    machine_.submit(r.Write(b"fh01", 0, b"bulk", 1))
    assert b"bulk" not in machine_.data_to_send()
    assert machine_.path_data_to_send(1) == b"bulk"
    assert machine_.path_data_to_send(1) == b""


def test_a_resent_request_puts_its_data_back_on_the_path():
    machine_ = ready_machine()
    sid = machine_.submit(r.Write(b"fh01", 0, b"bulk", 1))
    machine_.data_to_send()
    machine_.path_data_to_send(1)
    machine_.resume(sid)
    assert machine_.path_data_to_send(1) == b"bulk"


def test_two_links_are_framed_apart():
    """Half a frame on each socket must not be spliced into one."""
    machine_ = ready_machine()
    control = machine_.submit(r.Ping())
    data = machine_.submit(r.Read(b"fh01", 0, 4, 1))
    reply = ok(data, b"abcd")
    machine_.receive_data(reply[:5], pathid=1)
    machine_.receive_data(ok(control), pathid=0)
    assert [type(e).__name__ for e in machine_.events()] == ["Completed"]
    machine_.receive_data(reply[5:], pathid=1)
    done = list(machine_.events())
    assert isinstance(done[0], m.Completed)
    assert done[0].data == b"abcd"


def test_losing_a_data_path_costs_only_the_requests_on_it():
    machine_ = ready_machine()
    kept = machine_.submit(r.Ping())
    lost = machine_.submit(r.Read(b"fh01", 0, 4, 1))
    machine_.receive_data(b"", pathid=1)
    events = list(machine_.events())
    assert isinstance(events[0], m.Failed) and events[0].streamid == lost
    assert isinstance(events[1], m.PathLost) and events[1].pathid == 1
    assert machine_.state is m.State.READY
    machine_.receive_data(ok(kept))
    assert isinstance(next(iter(machine_.events())), m.Completed)


def test_forgetting_a_path_frees_what_was_waiting_on_it():
    # A caller that gives up on a path and closes its socket says so here. The
    # requests that went down it can never be answered now, so they are not
    # left in flight - and their stream ids come back, which a session doing
    # this once per file would otherwise run out of.
    machine_ = ready_machine()
    kept = machine_.submit(r.Ping())
    lost = machine_.submit(r.Read(b"fh01", 0, 4, 1))
    also = machine_.submit(r.Write(b"fh01", 0, b"bulk", 1))
    machine_.forget_path(1)
    assert machine_.in_flight == 1, "only the request on the control link is left"
    assert machine_.path_data_to_send(1) == b"", "bytes queued for a dead path went out"
    assert not list(machine_.events()), "forgetting a path is not news to anyone"
    reused = {machine_.submit(r.Ping()), machine_.submit(r.Ping())}
    assert reused == {lost, also}, "the ids were not put back"
    machine_.receive_data(ok(kept))
    assert isinstance(next(iter(machine_.events())), m.Completed)


def test_a_path_that_dies_with_nothing_on_it_is_only_reported():
    machine_ = ready_machine()
    machine_.receive_data(b"", pathid=2)
    assert [type(e).__name__ for e in machine_.events()] == ["PathLost"]


# --------------------------------------------------------------------------
# The session
# --------------------------------------------------------------------------


def _open(session, path: str, options: int = c.kXR_open_read) -> bytes:
    return rp.parse_open(session.execute(r.Open(path, options)).data)[0]


@pytest.fixture
def session(server, config):
    with Session.connect(server.url, config=config) as live:
        yield live


def test_a_session_binds_a_second_connection(session):
    assert not session.has_data_path
    pathid = session.bind_data_path()
    assert pathid == 1
    assert session.data_paths == [1]
    assert session.has_data_path
    assert "1 data path" in repr(session)
    assert session.bind_data_path() == 2
    assert "2 data paths" in repr(session)


def test_a_read_over_a_path_returns_the_same_bytes(server, session):
    pathid = session.bind_data_path()
    handle = _open(session, "/data/a.root")
    result = session.execute(r.Read(handle, 0, 5, pathid))
    assert result.data == b"hello"
    # The request itself went out on the control link, where the server saw it.
    assert c.kXR_read in server.seen


def test_a_write_over_a_path_reaches_the_file(server, session):
    pathid = session.bind_data_path()
    handle = _open(session, "/data/w.root", c.kXR_new | c.kXR_open_updt)
    session.execute(r.Write(handle, 0, b"bulk bytes", pathid))
    assert bytes(server.files["/data/w.root"]) == b"bulk bytes"


def test_a_vector_read_over_a_path_comes_back_on_it(session):
    pathid = session.bind_data_path()
    handle = _open(session, "/data/a.root")
    result = session.execute(r.ReadV([(handle, 0, 5)], pathid))
    assert [segment.data for segment in rp.parse_readv(result.data)] == [b"hello"]


def test_a_paged_read_over_a_path_comes_back_on_it(session):
    from xrdclient.crypto.crc32c import unpack_pages

    pathid = session.bind_data_path()
    handle = _open(session, "/data/a.root")
    result = session.execute(r.PgRead(handle, 0, 5, pathid=pathid))
    assert unpack_pages(result.data, 0)[0] == b"hello"


def test_a_request_naming_a_path_that_is_not_bound_is_refused(session):
    with pytest.raises(ValueError, match="data path 3 is not bound"):
        session.execute(r.Read(b"fh01", 0, 4, 3))


def test_a_closed_session_binds_nothing(server, config):
    session = Session.connect(server.url, config=config)
    session.close()
    with pytest.raises(XrdConnectionError, match="is closed"):
        session.bind_data_path()


def test_a_session_with_no_id_binds_nothing(session):
    session._m.session_id = b""
    with pytest.raises(xrdclient.errors.XRootDError, match=r"gave this session no id"):
        session.bind_data_path()


def test_a_server_that_does_not_know_the_session_refuses_the_bind(server, session):
    session._m.session_id = b"\x99" * 16
    with pytest.raises(ServerError, match="no such session"):
        session.bind_data_path()


def test_a_server_that_hands_out_the_same_path_twice_is_refused(server, session):
    session.bind_data_path()

    def one_path(conn, sid, params, body):
        yield xrdclient.testing.server.frame(sid, c.kXR_ok, b"\x01")

    server.handlers[c.kXR_bind] = one_path
    with pytest.raises(ProtocolError, match="twice"):
        session.bind_data_path()
    assert session.data_paths == [1]


def test_a_server_that_answers_a_bind_with_zero_is_refused(server, session):
    def no_path(conn, sid, params, body):
        yield xrdclient.testing.server.frame(sid, c.kXR_ok, b"\x00")

    server.handlers[c.kXR_bind] = no_path
    with pytest.raises(ProtocolError, match="control link"):
        session.bind_data_path()


def test_a_session_closes_the_connections_it_bound(session):
    session.bind_data_path()
    transport = session._paths[1]
    session.close()
    assert transport.closed
    assert session.data_paths == []


def test_losing_a_path_leaves_the_session_usable(server, session):
    pathid = session.bind_data_path()
    handle = _open(session, "/data/a.root")
    server.disconnect()
    with pytest.raises(XrdConnectionError):
        session.execute(r.Read(handle, 0, 5, pathid))
    # The path is gone and known to be gone; nothing pretends it is still there.
    assert session.data_paths == []
    # A second report of the same loss finds nothing left to close, and says so
    # by doing nothing rather than by raising.
    session._close_path(pathid)
    assert session.data_paths == []


# --------------------------------------------------------------------------
# The file
# --------------------------------------------------------------------------


def test_a_file_moves_its_reads_onto_a_path(server, config):
    with xrdclient.File(f"{server.url}//data/a.root", config) as fh:
        assert fh.data_path == 0
        assert fh.bind_data_path() == 1
        assert fh.data_path == 1
        # Idempotent: a handle already bound keeps the path it has.
        assert fh.bind_data_path() == 1
        assert fh.read(5, 0) == b"hello"
        assert fh.readv([(0, 5)]) == [b"hello"]
        assert fh.pgread(5, 0).data == b"hello"


def test_a_file_moves_its_writes_onto_a_path(server, config):
    fh = xrdclient.File(f"{server.url}//data/w.root", config)
    fh.open(OpenFlags.NEW | OpenFlags.UPDATE)
    try:
        fh.bind_data_path()
        assert fh.write(b"bulk", 0) == 4
        fh.pgwrite(b"pages", 8)
    finally:
        fh.close()
    assert bytes(server.files["/data/w.root"]) == b"bulk\x00\x00\x00\x00pages"


def test_a_re_opened_file_does_not_believe_in_its_old_path(server, config):
    with xrdclient.File(f"{server.url}//data/a.root", config) as fh:
        fh.bind_data_path()
        server.disconnect()
        assert fh.read(5, 0) == b"hello"
        assert fh.recoveries == 1
        assert fh.data_path == 0


def test_an_opened_file_reaches_the_path_through_the_facade(server, config):
    with xrdclient.open(f"{server.url}//data/a.root", "rb", config=config) as fh:
        assert fh.raw.file.bind_data_path() == 1
        assert fh.read() == b"hello world"


def test_the_async_facade_binds_a_path_too(server, config):
    import xrdclient.aio

    async def go():
        async with xrdclient.aio.open(f"{server.url}//data/a.root", "rb", config=config) as fh:
            assert fh.data_path == 0
            assert await fh.bind_data_path() == 1
            assert fh.data_path == 1
            assert await fh.read() == b"hello world"

    asyncio.run(go())


def test_an_http_file_has_no_data_path():
    import xrdclient.aio
    from xrdclient.testing import FakeDAVServer

    async def go(url):
        async with xrdclient.aio.open(url, "rb") as fh:
            assert fh.data_path == 0
            with pytest.raises(xrdclient.errors.UnsupportedError, match="bind_data_path"):
                await fh.bind_data_path()

    with FakeDAVServer(files={"/f.txt": b"body"}) as dav:
        asyncio.run(go(f"{dav.url}/f.txt"))


def test_a_second_file_on_the_same_server_gets_its_own_path(server, config):
    with FakeServer(files={"/d/a": b"a" * 16}) as other:
        with xrdclient.File(f"{other.url}//d/a", config) as first:
            with xrdclient.File(f"{other.url}//d/a", config) as second:
                assert first.bind_data_path() == 1
                assert second.bind_data_path() == 1  # its own session, its own numbering
                assert first.read(4, 0) == b"aaaa"
                assert second.read(4, 4) == b"aaaa"
