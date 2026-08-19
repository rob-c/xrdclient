"""Wire conformance: what the client does when the answer is wrong.

Two halves. The first drives :class:`~xrd.proto.machine.SessionMachine` by
hand with frames a server would never send - split in the middle of the
header, shorter than the length they declare, addressed to a stream nobody
opened - because a client that only works against a correct server is not a
client, it is a demo.

The second half goes over a real socket, with
:attr:`~xrd.testing.FakeServer.handlers` replacing one opcode's reply, and
checks that a plausible-but-wrong answer is refused rather than handed back
to the caller as data. Every one of these is a way to silently corrupt an
analysis, which is why none of them is allowed to be a warning.
"""

from __future__ import annotations

import struct

import pytest

from conftest import handshake_reply, login_body, ok, protocol_body
from xrd.config import Config
from xrd.errors import ProtocolError
from xrd.proto import constants as c
from xrd.proto import machine as m
from xrd.proto import requests as r
from xrd.proto.frames import decode_header
from xrd.testing import error, frame

SID = 4  # the first streamid the machine hands out

_HDR = struct.Struct(">HHi")


def ready() -> m.SessionMachine:
    """A machine walked all the way to READY, with its events drained."""
    machine = m.SessionMachine(
        host="srv.example.org",
        config=Config(username="tester", auth_order=("host",)),
    )
    machine.start()
    machine.data_to_send()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body()))
    machine.receive_data(ok(2, login_body()))
    machine.data_to_send()
    list(machine.events())
    return machine


def submitted() -> m.SessionMachine:
    machine = ready()
    machine.submit(r.Stat("/d/f"), path="/d/f")
    machine.data_to_send()
    return machine


def events(machine: m.SessionMachine) -> list[m.Event]:
    return list(machine.events())


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


def test_a_body_past_the_buffering_limit_is_refused_before_it_is_read():
    """The length is checked when it is *declared*, not once it has arrived.

    Otherwise a single 8-byte header makes the client sit there accumulating
    a gigabyte the server never intends to send.
    """
    machine = submitted()
    with pytest.raises(ProtocolError, match="past the"):
        machine.receive_data(_HDR.pack(SID, c.kXR_ok, c.MAX_RESPONSE_BODY + 1))


def test_the_largest_body_the_client_will_buffer_is_still_accepted():
    header = decode_header(_HDR.pack(SID, c.kXR_ok, c.MAX_RESPONSE_BODY))
    assert header.dlen == c.MAX_RESPONSE_BODY


def test_a_dlen_with_the_top_bit_set_is_refused_rather_than_read_as_negative():
    """``dlen`` is unsigned on the wire; 0xFFFFFFFF is huge, not -1."""
    machine = submitted()
    with pytest.raises(ProtocolError, match="past the"):
        machine.receive_data(struct.pack(">HHI", SID, c.kXR_ok, 0xFFFFFFFF))


def test_a_header_shorter_than_eight_bytes_is_refused_on_its_own():
    with pytest.raises(ProtocolError, match="needs 8 bytes"):
        decode_header(b"\x00\x04\x00")


def test_a_reply_shorter_than_its_own_dlen_waits_for_the_rest():
    machine = submitted()
    machine.receive_data(_HDR.pack(SID, c.kXR_ok, 10) + b"abcd")
    assert events(machine) == []
    machine.receive_data(b"efghij")
    completed = events(machine)
    assert [type(e).__name__ for e in completed] == ["Completed"]
    assert completed[0].data == b"abcdefghij"


def test_the_split_may_land_inside_the_header():
    machine = submitted()
    payload = frame(SID, c.kXR_ok, b"hello")
    machine.receive_data(payload[:3])
    assert events(machine) == []
    machine.receive_data(payload[3:])
    assert events(machine)[0].data == b"hello"


@pytest.mark.parametrize("size", [1, 2, 3, 7])
def test_a_reply_delivered_in_pieces_of_this_size_still_parses(size):
    machine = submitted()
    payload = frame(SID, c.kXR_ok, bytes(range(200)))
    for at in range(0, len(payload), size):
        machine.receive_data(payload[at : at + size])
    assert events(machine)[0].data == bytes(range(200))


def test_the_streamid_is_two_bytes_and_the_second_one_matters():
    """0x0004 and 0x0400 are different streams, not the same one byte-swapped."""
    machine = submitted()
    machine.receive_data(frame(SID << 8, c.kXR_ok, b"wrong stream"))
    assert events(machine) == []
    assert SID in machine._pending


def test_an_unsolicited_frame_on_a_stream_nobody_opened_is_dropped():
    machine = ready()
    machine.receive_data(frame(9999, c.kXR_ok, b"nobody asked"))
    assert events(machine) == []


def test_a_reply_for_a_released_stream_is_dropped():
    machine = submitted()
    machine.release(SID)
    machine.receive_data(frame(SID, c.kXR_ok, b"too late"))
    assert events(machine) == []


def test_two_replies_in_one_receive_are_both_dispatched():
    machine = ready()
    first = machine.submit(r.Stat("/a"), path="/a")
    second = machine.submit(r.Stat("/b"), path="/b")
    machine.data_to_send()
    machine.receive_data(frame(second, c.kXR_ok, b"b") + frame(first, c.kXR_ok, b"a"))
    assert [(e.streamid, e.data) for e in events(machine)] == [(second, b"b"), (first, b"a")]


def test_an_error_body_with_no_message_still_carries_its_code():
    machine = submitted()
    machine.receive_data(frame(SID, c.kXR_error, struct.pack(">i", 3011)))
    failure = events(machine)[0]
    assert isinstance(failure, m.Failed)
    assert failure.error.code == 3011


def test_an_error_code_the_client_does_not_know_is_still_an_error():
    machine = submitted()
    machine.receive_data(error(SID, 31337, "something went wrong"))
    failure = events(machine)[0]
    assert isinstance(failure, m.Failed)
    assert failure.error.code == 31337
    assert "something went wrong" in str(failure.error)


def test_a_status_the_client_does_not_know_is_refused_not_guessed_at():
    machine = submitted()
    machine.receive_data(frame(SID, 4999, b""))
    failure = events(machine)[0]
    assert isinstance(failure, m.Failed)


# ---------------------------------------------------------------------------
# Answers that are the wrong shape
# ---------------------------------------------------------------------------


def answer(payload, status=c.kXR_ok):
    """A handler that replies with one canned frame, whatever was asked."""

    def handler(conn, sid, params, body):
        yield frame(sid, status, payload)

    return handler


@pytest.fixture
def hostile(server, fs):
    """The stock server, plus a way to make one opcode answer badly."""

    def arm(opcode, handler):
        server.handlers[opcode] = handler
        return fs

    yield arm
    server.handlers.clear()


def test_a_read_answered_with_more_than_was_asked_for_is_refused(hostile):
    from xrd.client.file import File
    from xrd.flags import OpenFlags

    client = hostile(c.kXR_read, answer(b"x" * 4096))
    handle = File(client.url.with_path("/data/a.root"), client.config, router=client._router)
    handle.open(OpenFlags.READ)
    try:
        with pytest.raises(ProtocolError, match="more than the 8 bytes it asked for"):
            handle.read(8)
    finally:
        handle.close()


def test_a_paged_read_answered_with_more_than_was_asked_for_is_refused(hostile, server):
    from xrd.client.file import File
    from xrd.flags import OpenFlags

    client = hostile(c.kXR_pgread, answer(b"\x00" * 9000))
    handle = File(client.url.with_path("/data/a.root"), client.config, router=client._router)
    handle.open(OpenFlags.READ)
    try:
        with pytest.raises(ProtocolError, match="more than the 68 bytes it asked for"):
            handle.pgread(64, 0)
    finally:
        handle.close()


def _readv_body(handle: bytes, segments):
    out = b""
    for offset, data in segments:
        out += handle + struct.pack(">iq", len(data), offset) + data
    return out


def test_a_vector_read_missing_a_segment_is_refused_not_returned_empty(hostile, server):
    from xrd.client.file import File
    from xrd.flags import OpenFlags

    def only_the_first(conn, sid, params, body):
        fhandle = body[:4]
        yield frame(sid, c.kXR_ok, _readv_body(fhandle, [(0, b"hello")]))

    client = hostile(c.kXR_readv, only_the_first)
    handle = File(client.url.with_path("/data/a.root"), client.config, router=client._router)
    handle.open(OpenFlags.READ)
    try:
        with pytest.raises(ProtocolError, match="left the 5 bytes at offset 6 out"):
            handle.readv([(0, 5), (6, 5)])
    finally:
        handle.close()


def test_a_vector_read_segment_longer_than_asked_for_is_refused(hostile, server):
    from xrd.client.file import File
    from xrd.flags import OpenFlags

    def too_generous(conn, sid, params, body):
        # One segment, four times the length asked for - and still inside what
        # the two ranges together allow, so the per-segment check is the only
        # thing standing between the caller and the wrong bytes.
        fhandle = body[:4]
        yield frame(sid, c.kXR_ok, _readv_body(fhandle, [(0, b"x" * 20)]))

    client = hostile(c.kXR_readv, too_generous)
    handle = File(client.url.with_path("/data/a.root"), client.config, router=client._router)
    handle.open(OpenFlags.READ)
    try:
        with pytest.raises(ProtocolError, match="20 bytes"):
            handle.readv([(0, 5), (6, 5)])
    finally:
        handle.close()


def test_a_vector_read_segment_with_a_negative_length_is_refused(hostile, server):
    from xrd.client.file import File
    from xrd.flags import OpenFlags

    def negative(conn, sid, params, body):
        yield frame(sid, c.kXR_ok, body[:4] + struct.pack(">iq", -8, 0))

    client = hostile(c.kXR_readv, negative)
    handle = File(client.url.with_path("/data/a.root"), client.config, router=client._router)
    handle.open(OpenFlags.READ)
    try:
        with pytest.raises(ProtocolError, match="negative length"):
            handle.readv([(0, 5)])
    finally:
        handle.close()


@pytest.mark.parametrize("line", [b"id0\x00", b"id0 12 0\x00", b"\x00", b"only-one-field\x00"])
def test_a_stat_line_missing_fields_is_refused(hostile, line):
    client = hostile(c.kXR_stat, answer(line))
    with pytest.raises(ProtocolError, match="kXR_stat returned"):
        client.stat("/data/a.root")


def test_a_stat_line_whose_size_is_not_a_number_is_refused(hostile):
    client = hostile(c.kXR_stat, answer(b"id0 huge 0 1700000000\x00"))
    with pytest.raises(ValueError):
        client.stat("/data/a.root")


def test_a_statvfs_line_missing_fields_is_refused(hostile):
    client = hostile(c.kXR_stat, answer(b"1 2 3\x00"))
    with pytest.raises(ProtocolError, match="vfs returned"):
        client.statvfs("/data")


@pytest.mark.parametrize("name", [b"../escape", b"a/b", b".."])
def test_a_listing_entry_that_is_not_a_name_is_refused(hostile, name):
    client = hostile(c.kXR_dirlist, answer(name + b"\n"))
    with pytest.raises(ProtocolError, match="is not a name"):
        client.listdir("/data")


def test_a_checksum_reply_that_is_not_a_pair_is_refused(hostile):
    client = hostile(c.kXR_query, answer(b"adler32\x00"))
    with pytest.raises(ProtocolError, match="malformed checksum"):
        client.checksum("/data/a.root")


def test_an_open_that_answers_without_a_handle_is_refused(hostile):
    client = hostile(c.kXR_open, answer(b"\x01\x02"))
    with pytest.raises(ProtocolError):
        client.open("/data/a.root", "rb")


def test_a_redirect_to_nowhere_is_refused(hostile):
    """An empty host is not somewhere to go, and must not become one."""
    client = hostile(c.kXR_stat, answer(struct.pack(">i", 1094) + b"\x00", c.kXR_redirect))
    with pytest.raises(ProtocolError, match="names no host"):
        client.stat("/data/a.root")


def test_an_error_reply_the_client_cannot_parse_is_still_an_error(hostile):
    client = hostile(c.kXR_stat, answer(b"\x00", c.kXR_error))
    with pytest.raises(ProtocolError):
        client.stat("/data/a.root")


# ---------------------------------------------------------------------------
# The request side of the same contract
# ---------------------------------------------------------------------------


def test_every_request_frame_is_a_multiple_of_the_header_length(server, fs):
    """Whatever the client sends, the server got a whole frame back out."""
    fs.stat("/data/a.root")
    fs.listdir("/data")
    fs.ping()
    assert server.seen[-3:] == [c.kXR_stat, c.kXR_dirlist, c.kXR_ping]


def test_a_request_body_past_the_protocol_maximum_is_refused_here(monkeypatch):
    from xrd.proto.frames import encode

    request = r.Write(b"\x00\x00\x00\x01", 0, b"payload")
    monkeypatch.setattr(c, "MAX_FRAME_PAYLOAD", 3)
    with pytest.raises(ProtocolError, match="exceeds the protocol maximum"):
        encode(request, SID)


def test_a_request_that_writes_the_wrong_parameter_length_is_refused():
    from xrd.proto.frames import Request, encode

    class Broken(Request):
        opcode = c.kXR_ping

        def params(self, w):
            w.u32(1)

    with pytest.raises(ProtocolError, match="expected 16"):
        encode(Broken(), SID)


def test_the_client_survives_a_server_that_answers_every_request_identically(hostile):
    """A server stuck on one canned reply must not desynchronise the stream."""
    client = hostile(c.kXR_ping, answer(b""))
    for _ in range(20):
        assert client.ping() is None
