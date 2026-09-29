"""The per-request fast paths, held to the slow paths they stand in for.

Every request pays for these, so each is a shortcut past something general:
a precomputed ``struct`` layout instead of a :class:`Writer`, a reply taken
straight off the wire instead of through the framing buffer, a remembered
pool identity instead of a fresh hash. The general path is still there, and
these tests pin that the shortcut says exactly what it would have said.
"""

from __future__ import annotations

import struct

import pytest

from conftest import frame, ok
from test_machine import SID, drain, kinds, ready, status_frame
from xrdclient.config import Config
from xrdclient.errors import ProtocolError
from xrdclient.flags import StatInfoFlags
from xrdclient.proto import constants as c
from xrdclient.proto import machine as m
from xrdclient.proto import requests as r
from xrdclient.proto import responses as rp
from xrdclient.proto.buffer import Writer
from xrdclient.proto.frames import Request, encode, header_fields
from xrdclient.session import pool
from xrdclient.session.router import _path_fields
from xrdclient.session.sync import Session
from xrdclient.transport.memory import pipe
from xrdclient.url import parse

# --------------------------------------------------------------------------
# Precomputed parameter layouts
# --------------------------------------------------------------------------

_HANDLES = (b"\x01\x02\x03\x04", b"\x01", b"\x01\x02\x03\x04\x05\x06")


def _written(request: Request) -> bytes:
    w = Writer()
    request.params(w)
    return w.bytes()


@pytest.mark.parametrize("fhandle", _HANDLES)
@pytest.mark.parametrize(
    "build",
    [
        lambda h: r.Ping(),
        lambda h: r.Stat("/a", c.kXR_vfs, h),
        lambda h: r.StatVFS("/a"),
        lambda h: r.Open("/a", c.kXR_open_read | c.kXR_retstat, 0o644),
        lambda h: r.Close(h),
        lambda h: r.Sync(h),
        lambda h: r.Clone(h, []),
        lambda h: r.Read(h, (1 << 40) + 3, 4096, 2),
        lambda h: r.PgRead(h, 1 << 33, 65536),
        lambda h: r.Write(h, 1 << 35, b"data", 3),
    ],
)
def test_a_precomputed_layout_packs_what_params_writes(build, fhandle):
    request = build(fhandle)
    assert bytes(request.header_params()) == _written(request)
    assert len(request.header_params()) == 16


def test_a_subclass_with_its_own_params_is_not_given_its_parents_layout():
    class Tagged(r.Stat):
        __slots__ = ()

        def params(self, w: Writer) -> None:
            w.raw(b"T" * 16)

    assert bytes(Tagged("/a").header_params()) == b"T" * 16
    assert encode(Tagged("/a"), 9)[4:20] == b"T" * 16


def test_a_mixin_params_ahead_of_the_parent_is_not_given_its_layout():
    """The MRO decides whose ``params`` a class has, not the class's own dict."""

    class Mixin:
        def params(self, w: Writer) -> None:
            w.zeros(15).u8(7)

    class Mixed(Mixin, r.Read):
        __slots__ = ()

    request = Mixed(b"abcd", 0, 1)
    assert bytes(request.header_params()) == _written(request) == bytes(15) + b"\x07"
    assert encode(request, 9)[4:20] == bytes(15) + b"\x07"


def test_a_mixin_without_params_keeps_the_layout():
    class Mixin:
        pass

    class Mixed(Mixin, r.Read):
        __slots__ = ()

    assert Mixed.header_params is r.Read.header_params


@pytest.mark.parametrize("fhandle", [memoryview(b"abcd"), bytearray(b"abcd"), "abcd"])
@pytest.mark.parametrize(
    "build",
    [
        lambda h: r.Stat("", 0, h),
        lambda h: r.Close(h),
        lambda h: r.Read(h, 5, 6),
        lambda h: r.PgRead(h, 5, 6),
        lambda h: r.Write(h, 5, b"d"),
    ],
)
def test_a_handle_the_writer_took_is_packed_the_same(build, fhandle):
    """``Writer.padded`` took any bytes-like or text handle; so must ``4s``."""
    request = build(fhandle)
    assert bytes(request.header_params()) == _written(request)
    assert len(encode(request, 1)) >= 24


def test_a_subclass_that_only_inherits_keeps_the_layout():
    class Quiet(r.Stat):
        __slots__ = ()

    assert Quiet.header_params is r.Stat.header_params


# --------------------------------------------------------------------------
# A reply straight off the wire
# --------------------------------------------------------------------------


def _read(machine: m.SessionMachine, length: int = 8) -> int:
    return machine.submit(r.Read(b"\0\0\0\1", 0, length))


def test_a_whole_reply_in_one_read_completes_without_buffering():
    machine = ready()
    sid = _read(machine)
    machine.receive_data(ok(sid, b"12345678"))
    (done,) = machine.drain()
    assert isinstance(done, m.Completed) and done.data == b"12345678"
    assert not machine._framers[0].buffer


def test_two_replies_in_one_read_both_complete():
    machine = ready()
    first, second = _read(machine), _read(machine)
    machine.receive_data(ok(first, b"a" * 8) + ok(second, b"b" * 8))
    assert [e.data for e in machine.drain()] == [b"a" * 8, b"b" * 8]


def test_a_reply_split_across_reads_is_reassembled():
    machine = ready()
    sid = _read(machine)
    wire = ok(sid, b"12345678")
    machine.receive_data(wire[:5])
    assert machine.drain() == []
    machine.receive_data(wire[5:11])
    assert machine.drain() == []
    # A whole-looking frame after a partial one belongs to the buffer.
    machine.receive_data(wire[11:])
    assert [e.data for e in machine.drain()] == [b"12345678"]


def test_a_header_waiting_on_its_body_sends_the_next_read_to_the_buffer():
    machine = ready()
    sid = _read(machine)
    wire = ok(sid, b"12345678")
    machine.receive_data(wire[:8])
    assert machine._framers[0].dlen == 8 and not machine._framers[0].buffer
    machine.receive_data(wire[8:])
    assert [e.data for e in machine.drain()] == [b"12345678"]


def test_a_status_trailer_owed_sends_the_next_read_to_the_buffer():
    machine = ready()
    sid = machine.submit(r.PgRead(b"\0\0\0\1", 0, 4))
    wire = status_frame(sid, c.kXR_pgread, c.kXR_FinalResult, b"abcd", info=bytes(8))
    trailer_at = len(wire) - 4
    machine.receive_data(wire[:trailer_at])
    assert machine._framers[0].need_trailer == 4
    machine.receive_data(wire[trailer_at:])
    assert kinds(drain(machine)) == ["Completed"]


def test_an_oversized_header_is_refused_on_the_fast_path_too():
    machine = ready()
    _read(machine)
    with pytest.raises(ProtocolError, match="past the"):
        machine.receive_data(struct.pack(">HHI", SID, c.kXR_ok, c.MAX_RESPONSE_BODY + 1))


def test_header_fields_reads_at_an_offset():
    assert header_fields(b"xx" + struct.pack(">HHI", 7, c.kXR_ok, 3), 2) == (7, c.kXR_ok, 3)


def test_drain_hands_over_every_event_and_keeps_none():
    machine = ready()
    sid = machine.submit(r.Ping())
    machine.receive_data(ok(sid))
    assert kinds(machine.drain()) == ["Completed"]
    assert machine.drain() == []
    assert machine.next_event() is None


def test_has_path_data_says_whether_a_data_path_has_bytes_queued():
    machine = ready()
    assert not machine.has_path_data
    machine.submit(r.Write(b"\0\0\0\1", 0, b"payload", 2))
    assert machine.has_path_data
    assert machine.path_data_to_send(2) == b"payload"
    assert not machine.has_path_data


def test_an_unexpected_status_fails_only_its_request():
    machine = ready()
    sid = machine.submit(r.Ping())
    machine.receive_data(struct.pack(">HHI", sid, 4321, 0))
    (failed,) = machine.drain()
    assert isinstance(failed, m.Failed) and "unexpected response status" in str(failed.error)
    assert machine.in_flight == 0


# --------------------------------------------------------------------------
# The pool's identity, remembered
# --------------------------------------------------------------------------


def test_the_identity_of_one_config_is_worked_out_once(monkeypatch):
    config = Config(username="alice")
    url = parse("root://h.example//a")
    first = pool._identity(url, config)
    monkeypatch.setattr(pool, "_digest", lambda *_: pytest.fail("hashed again"))
    assert pool._identity(url, config) == first


def test_the_remembered_identity_is_per_url_user_and_per_config():
    config = Config(username="alice")
    plain = pool._identity(parse("root://h.example//a"), config)
    other = pool._identity(parse("root://bob@h.example//a"), config)
    assert plain != other
    assert pool._identity(parse("root://h.example//a"), Config(username="carol")) != plain
    # An equal config is the same person, remembered or not.
    assert pool._identity(parse("root://h.example//a"), Config(username="alice")) == plain


def test_a_config_whose_id_was_reused_is_not_mistaken_for_the_old_one(monkeypatch):
    config = Config(username="alice")
    url = parse("root://h.example//a")
    stranger = Config(username="mallory")
    remembered = pool._Known(stranger, pool._context(config), (), (), "not-alice")
    monkeypatch.setitem(pool._DIGESTS, (id(config), ""), remembered)
    assert pool._identity(url, config) != "not-alice"


def test_the_identity_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(pool, "_DIGESTS", {})
    configs = [Config(username=f"u{n}") for n in range(pool._DIGESTS_MAX + 1)]
    for config in configs:
        pool._identity(parse("root://h.example//a"), config)
    assert len(pool._DIGESTS) <= pool._DIGESTS_MAX


# --------------------------------------------------------------------------
# Stat flags, remembered
# --------------------------------------------------------------------------


def test_stat_flags_are_the_same_whether_remembered_or_not(monkeypatch):
    monkeypatch.setattr(rp, "_FLAGS", {})
    built = rp.parse_stat(b"1 2 19 3")
    again = rp.parse_stat(b"1 2 19 3")
    assert built.flags is again.flags
    assert built.flags == StatInfoFlags(19)


def test_the_stat_flag_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(rp, "_FLAGS", {n: StatInfoFlags(0) for n in range(1000, 1256)})
    assert rp.parse_stat(b"1 2 19 3").flags == StatInfoFlags(19)
    assert 19 not in rp._FLAGS


# --------------------------------------------------------------------------
# The session driver's quieter branches, over an in-memory pipe
# --------------------------------------------------------------------------


def _session():  # type: ignore[no-untyped-def]
    """A ready session on a memory pipe, and the server's end of it."""
    transport, peer = pipe()
    return Session(transport, ready(), Config()), peer


def test_a_request_with_no_data_path_never_arrives_on_one():
    session, _ = _session()
    assert session._use_arrival_path(r.Read(b"\0\0\0\1", 0, 8)) is False
    assert session.arrives_on_path is None  # nothing was asked


def test_the_first_request_for_a_path_asks_how_the_server_routes():
    session, peer = _session()
    session._paths[2], _ = pipe()
    peer.send(ok(SID, b"1"))  # brix.substreams=1: arrival is honoured
    assert session._use_arrival_path(r.Read(b"\0\0\0\1", 0, 8, 2)) is True
    assert session.arrives_on_path is True


def test_a_body_in_instalments_needs_no_chunk_callback():
    session, peer = _session()
    peer.send(frame(SID, c.kXR_oksofar, b"abc") + ok(SID, b"def"))
    assert session.execute(r.Read(b"\0\0\0\1", 0, 6)).data == b"abcdef"


def test_a_flush_skips_a_data_path_with_nothing_queued():
    session, _ = _session()
    (idle, _), (busy, _) = pipe(), pipe()
    session._paths[2], session._paths[3] = idle, busy
    session._m.submit(r.Write(b"\0\0\0\1", 0, b"payload", 3))
    session._flush()
    assert busy.sent() == b"payload"
    assert idle.sent() == b""


def test_a_reply_parked_for_another_stream_is_left_where_it_is():
    session, peer = _session()
    parked: list[m.Event] = [m.Completed(99, r.Ping(), b"")]
    session._inbox[99] = parked
    peer.send(ok(SID, b"pong"))
    assert session.execute(r.Ping()).data == b"pong"
    assert session._inbox == {99: parked}


def test_a_request_with_no_path_field_offers_nothing_to_retarget():
    assert _path_fields(r.Ping()) == {}


def test_a_cancelling_prepare_names_no_path_for_a_token():
    assert _path_fields(r.Prepare(["req-id"], c.kXR_cancel)) == {}
