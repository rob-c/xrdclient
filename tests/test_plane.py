"""The bulk data plane for everyday I/O: writes, small reads, vector reads.

Every data operation on a :class:`~xrdclient.File` now frames its requests and
reads its replies directly on the connection it borrows, with several in
flight at once. What has to hold is what held on the event path:

* the bytes are the bytes, whatever the size and however the server splits
  or orders its answers;
* a refusal anywhere in a window of pipelined requests is raised, and the
  replies still owed are taken off the wire first, so the connection is fit
  for the next request;
* a server that answers with a conversation rather than data - ``kXR_wait``,
  say - is handed back to the event path, which knows how to hold one;
* a read-only handle reads ahead only while the reads are sequential.
"""

from __future__ import annotations

import io
import struct
from dataclasses import replace

import pytest

from xrdclient.client import file as file_module
from xrdclient.client.file import File
from xrdclient.config import Config
from xrdclient.crypto.sigver import Signer
from xrdclient.errors import ProtocolError, TransientError, XRootDError
from xrdclient.flags import OpenFlags
from xrdclient.io import _write_window, open_url
from xrdclient.io.raw import XRootDRawIO
from xrdclient.proto import constants as c
from xrdclient.session.sync import Session
from xrdclient.testing import server as S

#: Long enough to span several small chunks, with every position telling
#: where it came from, so a piece written or read in the wrong place shows.
PAYLOAD = bytes((i * 7 + i // 251) % 256 for i in range(50_000))


@pytest.fixture
def small(config: Config) -> Config:
    """Chunks small enough that one call is a window of several requests."""
    return replace(config, chunk_size=4096, bulk_chunk=4096, bulk_depth=3, readahead=256)


def _writer(server, cfg: Config, path: str = "/data/w.bin") -> File:
    handle = File(server.url.with_path(path), cfg)
    handle.open(OpenFlags.NEW | OpenFlags.UPDATE)
    return handle


def _nth(opcode_handler, nth: int, answer):
    """A handler that gives the ``nth`` call ``answer`` and serves the rest."""
    calls = {"n": 0}

    def handler(conn, sid, params, body):
        calls["n"] += 1
        if calls["n"] == nth:
            yield from answer(conn, sid, params, body)
            return
        yield from opcode_handler(conn, sid, params, body)

    return handler


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def test_a_write_bigger_than_the_window_lands_byte_exact(server, small):
    with _writer(server, small) as handle:
        assert handle.write(PAYLOAD, 10) == len(PAYLOAD)
        assert handle.size == 10 + len(PAYLOAD)
    assert server.contents("/data/w.bin") == bytes(10) + PAYLOAD
    assert server.seen.count(c.kXR_write) == -(-len(PAYLOAD) // 4096)


def test_a_write_takes_any_flat_buffer(server, small):
    with _writer(server, small) as handle:
        assert handle.write(bytearray(b"abc"), 0) == 3
        assert handle.write(memoryview(b"defg")[1:], 3) == 3
    assert server.contents("/data/w.bin") == b"abcefg"


def test_a_refused_write_in_the_window_is_raised_after_the_rest_are_answered(server, small):
    def refuse(conn, sid, params, body):
        yield S.error(sid, 3007, "disk on fire")

    server.handlers[c.kXR_write] = _nth(S._h_write, 2, refuse)
    with _writer(server, small) as handle:
        with pytest.raises(XRootDError, match="disk on fire"):
            handle.write(PAYLOAD, 0)
        # Every other reply was taken off the wire before the error was
        # raised, so the connection answers the next request correctly.
        assert not handle.session.broken
        assert handle.write(b"after", 0) == 5
    assert server.contents("/data/w.bin")[:5] == b"after"


def test_a_write_the_server_asks_to_wait_for_is_rewritten_on_the_event_path(server, small):
    server.waits[c.kXR_write] = 1
    with _writer(server, small) as handle:
        assert handle.write(PAYLOAD, 0) == len(PAYLOAD)
        assert not handle.session.broken
    assert server.contents("/data/w.bin") == PAYLOAD


def test_an_acknowledgement_in_instalments_is_still_one_acknowledgement(server, small):
    def in_parts(conn, sid, params, body):
        yield S.frame(sid, c.kXR_oksofar)
        yield from S._h_write(conn, sid, params, body)

    server.handlers[c.kXR_write] = in_parts
    with _writer(server, small) as handle:
        assert handle.write(PAYLOAD, 0) == len(PAYLOAD)
    assert server.contents("/data/w.bin") == PAYLOAD


def test_a_signing_session_sends_each_write_as_one_whole_frame(server, small):
    """A signature covers the payload, so the frame cannot be sent in two."""
    with _writer(server, small) as handle:
        handle.session.machine.signer = Signer(b"k" * 32, c.kXR_secNone)
        assert handle.write(PAYLOAD, 0) == len(PAYLOAD)
    assert server.contents("/data/w.bin") == PAYLOAD


def test_writing_nothing_sends_nothing(server, small):
    with _writer(server, small) as handle:
        with handle.session.bulk(handle.handle, chunk=4096, depth=1) as channel:
            assert channel.write_from(memoryview(b""), 0) == 0
        assert handle.write(b"", 0) == 0
    assert server.seen.count(c.kXR_write) == 0


def test_a_write_on_a_lost_connection_is_transient(server, small, monkeypatch):
    with _writer(server, small) as handle:
        transport = type(handle.session.transport)
        monkeypatch.setattr(transport, "send", lambda self, data: _lost())
        with pytest.raises(TransientError, match="went away"):
            handle.write(PAYLOAD, 0)
        monkeypatch.undo()
        with pytest.raises(TransientError):
            handle.close()


def _lost() -> None:
    from xrdclient.errors import ConnectionError as XrdConnectionError

    raise XrdConnectionError("peer went away")


def test_the_event_path_reports_a_lost_writer_the_same_way(server, small, monkeypatch):
    with _writer(server, replace(small, bulk=False)) as handle:
        monkeypatch.setattr(type(handle.session.transport), "send", lambda self, data: _lost())
        with pytest.raises(TransientError, match="went away"):
            handle.write(PAYLOAD, 0)
        monkeypatch.undo()
        with pytest.raises(TransientError):
            handle.close()


def test_the_event_path_still_writes_when_the_plane_is_off(server, small):
    with _writer(server, replace(small, bulk=False)) as handle:
        assert handle.write(PAYLOAD, 0) == len(PAYLOAD)
    assert server.contents("/data/w.bin") == PAYLOAD


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@pytest.fixture
def big(server):
    server.add_file("/data/p.bin", PAYLOAD)
    return server


def _reader(server, cfg: Config) -> File:
    handle = File(server.url.with_path("/data/p.bin"), cfg)
    handle.open()
    return handle


def test_a_small_read_the_server_asks_to_wait_for_is_read_on_the_event_path(big, small):
    big.waits[c.kXR_read] = 1
    with _reader(big, small) as handle:
        assert handle.read(100, 7) == PAYLOAD[7:107]
        assert not handle.session.broken


def test_a_read_past_the_end_is_short(big, small):
    with _reader(big, small) as handle:
        assert handle.read(100, len(PAYLOAD) - 10) == PAYLOAD[-10:]
        assert handle.read(100, len(PAYLOAD)) == b""


def test_the_event_path_stops_at_the_end_of_the_file(big, small):
    with _reader(big, replace(small, bulk=False, readahead=0)) as handle:
        assert handle.read(3 * 4096, len(PAYLOAD) - 5000) == PAYLOAD[-5000:]


def test_sequential_reads_are_served_from_the_readahead(big, small):
    with _reader(big, small) as handle:
        got = b"".join(handle.read(64, offset) for offset in range(0, 64 * 40, 64))
        assert got == PAYLOAD[: 64 * 40]
    # The first read is on its own; the second starts where it ended and
    # fetches the window beyond, which then answers the reads after it, and
    # the window doubles as the reads carry on.
    assert big.seen.count(c.kXR_read) < 10


def test_random_reads_never_read_ahead(big, small):
    offsets = [9000, 100, 30000, 5, 20000, 70]
    with _reader(big, small) as handle:
        for offset in offsets:
            assert handle.read(10, offset) == PAYLOAD[offset : offset + 10]
        # A window that grew and was then abandoned starts small again.
        assert handle._window == small.readahead
    assert big.seen.count(c.kXR_read) == len(offsets)


def test_a_sequential_scan_ends_cleanly_at_the_end_of_the_file(big, small):
    size = 1000
    with _reader(big, small) as handle:
        start = len(PAYLOAD) - 3 * size
        pieces = [handle.read(size, start + i * size) for i in range(5)]
    assert b"".join(pieces) == PAYLOAD[start:]
    assert pieces[-2:] == [b"", b""]


def test_a_writable_handle_never_reads_ahead(big, small):
    with File(big.url.with_path("/data/p.bin"), small) as handle:
        handle.close()
    handle = File(big.url.with_path("/data/p.bin"), small)
    handle.open(OpenFlags.UPDATE)
    with handle:
        for offset in range(0, 640, 64):
            assert handle.read(64, offset) == PAYLOAD[offset : offset + 64]
        assert handle._ahead == file_module._NOTHING_AHEAD
    assert big.seen.count(c.kXR_read) == 10


def test_readahead_can_be_turned_off(big, small):
    with _reader(big, replace(small, readahead=0)) as handle:
        for offset in range(0, 640, 64):
            assert handle.read(64, offset) == PAYLOAD[offset : offset + 64]
    assert big.seen.count(c.kXR_read) == 10


def test_a_read_ahead_that_is_declined_falls_back_whole(big, small):
    with _reader(big, small) as handle:
        assert handle.read(64, 0) == PAYLOAD[:64]
        big.waits[c.kXR_read] = 1
        assert handle.read(64, 64) == PAYLOAD[64:128]
        assert handle._ahead == file_module._NOTHING_AHEAD


def test_a_reopened_handle_forgets_what_it_read_ahead(big, small):
    with _reader(big, small) as handle:
        handle.read(64, 0)
        handle.read(64, 64)
        assert handle._ahead != file_module._NOTHING_AHEAD
        handle._reopen()
        assert handle._ahead == file_module._NOTHING_AHEAD
        assert handle.read(64, 128) == PAYLOAD[128:192]


def test_a_read_that_lands_short_hands_back_only_what_arrived(big, small):
    with _reader(big, small) as handle:
        assert handle._plane_read(200, len(PAYLOAD) - 50) == PAYLOAD[-50:]


def test_the_landing_buffer_forgives_a_close_while_still_exported():
    landing = file_module._Landing(bytes(8))
    view = landing.getbuffer()[2:]
    landing.close()  # a failed read's slices can outlive it; that is not an error
    view.release()


def test_a_lost_connection_is_transient_even_when_the_transport_says_otherwise():
    from xrdclient.errors import ConnectionError as XrdConnectionError

    plain = XrdConnectionError("gone")
    wrapped = file_module._transient(plain)
    assert isinstance(wrapped, TransientError)
    assert wrapped.__cause__ is plain
    already = TransientError("gone too")
    assert file_module._transient(already) is already


def test_a_stream_reuses_its_buffers_from_one_call_to_the_next(big, small):
    with _reader(big, small) as handle:
        with handle.session.bulk(handle.handle, chunk=4096, depth=2) as reader:
            first = b"".join(bytes(v) for _, v in reader.stream(0, 10_000))
            buffers = list(reader._bufs)
            second = b"".join(bytes(v) for _, v in reader.stream(10_000, 10_000))
            assert reader._bufs == buffers
    assert first + second == PAYLOAD[:20_000]


# ---------------------------------------------------------------------------
# The data paths a file shares with its connection
# ---------------------------------------------------------------------------


def _multistream(config: Config) -> Config:
    return replace(config, data_streams=1, data_stream_timeout=0.3)


def test_a_pooled_connection_lends_its_data_path_to_the_next_file(big, config):
    cfg = _multistream(config)
    sessions = []
    for _ in range(3):
        with _reader(big, cfg) as handle:
            assert handle._data_paths
            assert handle.read(10, 0) == PAYLOAD[:10]
            sessions.append(handle.session)
    assert len({id(s) for s in sessions}) == 1
    # One bind, then the path is reused rather than a socket more per open.
    assert big.seen.count(c.kXR_bind) == 1
    assert len(sessions[0].data_paths) == 1


def test_a_split_read_the_data_path_will_not_carry_falls_back(big, config, monkeypatch):
    execute = Session.execute

    def no_split(self, request, **kwargs):
        if getattr(request, "pathid", 0) and not kwargs.get("arrive_on_path"):
            raise XRootDError("not on this path")
        return execute(self, request, **kwargs)

    monkeypatch.setattr(Session, "execute", no_split)
    with _reader(big, replace(_multistream(config), bulk=False)) as handle:
        assert handle.read(10, 3) == PAYLOAD[3:13]
        assert handle._multistream is False


def test_a_split_read_whose_recovery_fails_reports_what_went_wrong(big, config, monkeypatch):
    execute = Session.execute

    def lost(self, request, **kwargs):
        if getattr(request, "pathid", 0) and not kwargs.get("arrive_on_path"):
            raise TransientError("the path went away")
        return execute(self, request, **kwargs)

    handle = _reader(big, replace(_multistream(config), bulk=False))
    monkeypatch.setattr(Session, "execute", lost)
    monkeypatch.setattr(File, "_do_open", _refuse_reopen)
    with pytest.raises(XRootDError, match="no second open"):
        handle.read(10, 0)
    assert not handle.is_open


def _refuse_reopen(self: File) -> bytes:
    raise XRootDError("no second open")


# ---------------------------------------------------------------------------
# Vector reads
# ---------------------------------------------------------------------------


def _ranges(count: int, length: int = 16) -> list[tuple[int, int]]:
    step = len(PAYLOAD) // count
    return [(i * step, length) for i in range(count)]


def _expected(ranges: list[tuple[int, int]]) -> list[bytes]:
    return [PAYLOAD[offset : offset + length] for offset, length in ranges]


def test_a_vector_read_too_big_for_one_request_is_pipelined(big, small):
    ranges = _ranges(1500, 8)
    with _reader(big, small) as handle:
        assert handle.readv(ranges) == _expected(ranges)
    assert big.seen.count(c.kXR_readv) == 2


def _readv_answer(transform):
    """A ``kXR_readv`` handler whose segments pass through ``transform`` first."""

    def handler(conn, sid, params, body):
        (frame,) = list(S._h_readv(conn, sid, params, body))
        segments = []
        at, reply = 0, frame[8:]
        while at < len(reply):
            length = struct.unpack(">i", reply[at + 4 : at + 8])[0]
            segments.append(reply[at : at + 16 + length])
            at += 16 + length
        yield S.frame(sid, c.kXR_ok, b"".join(transform(segments)))

    return handler


def test_a_vector_reply_out_of_the_order_asked_is_matched_by_offset(big, small):
    big.handlers[c.kXR_readv] = _readv_answer(lambda segments: segments[::-1])
    ranges = _ranges(20)
    with _reader(big, small) as handle:
        assert handle.readv(ranges) == _expected(ranges)


def test_a_vector_reply_with_a_segment_too_many_is_refused(big, small):
    """One more segment than was asked for cannot fit the reply it was sized for."""
    big.handlers[c.kXR_readv] = _readv_answer(lambda segments: segments + segments[:1])
    with _reader(big, small) as handle, pytest.raises(ProtocolError, match="more than"):
        handle.readv(_ranges(20))


def test_a_vector_reply_missing_a_segment_is_refused(big, small):
    big.handlers[c.kXR_readv] = _readv_answer(lambda s: s[:-1])
    with _reader(big, small) as handle, pytest.raises(ProtocolError, match="left the"):
        handle.readv(_ranges(20))


def test_a_vector_reply_in_instalments_is_put_back_together(big, small):
    def in_parts(conn, sid, params, body):
        (frame,) = list(S._h_readv(conn, sid, params, body))
        reply = frame[8:]
        yield S.frame(sid, c.kXR_oksofar, reply[:21])
        yield S.frame(sid, c.kXR_ok, reply[21:])

    big.handlers[c.kXR_readv] = in_parts
    ranges = _ranges(20)
    with _reader(big, small) as handle:
        assert handle.readv(ranges) == _expected(ranges)


@pytest.mark.parametrize(
    "reply, message",
    [
        (b"\x00" * 10, "segment header"),
        (struct.pack(">4siq", b"\x00" * 4, 100, 0) + b"abcd", "declares 100 bytes"),
        (struct.pack(">4siq", b"\x00" * 4, -1, 0), "negative length"),
    ],
    ids=["torn-header", "overlong", "negative"],
)
def test_a_malformed_vector_reply_is_refused(big, small, reply, message):
    def malformed(conn, sid, params, body):
        yield S.frame(sid, c.kXR_ok, reply)

    big.handlers[c.kXR_readv] = malformed
    with _reader(big, small) as handle, pytest.raises(ProtocolError, match=message):
        handle.readv([(0, 4)])


def test_a_vector_read_the_server_asks_to_wait_for_is_read_on_the_event_path(big, small):
    big.waits[c.kXR_readv] = 1
    ranges = _ranges(20)
    with _reader(big, small) as handle:
        assert handle.readv(ranges) == _expected(ranges)
        assert not handle.session.broken


def _lose_the_first_send(monkeypatch, handle: File) -> None:
    transport = type(handle.session.transport)
    real = transport.send
    calls = {"n": 0}

    def once(self, data):
        calls["n"] += 1
        if calls["n"] == 1:
            _lost()
        real(self, data)

    monkeypatch.setattr(transport, "send", once)


def test_a_vector_read_on_a_lost_connection_is_read_again_elsewhere(big, small, monkeypatch):
    ranges = _ranges(20)
    with _reader(big, replace(small, recover_handles=True)) as handle:
        _lose_the_first_send(monkeypatch, handle)
        assert handle.readv(ranges) == _expected(ranges)
        assert handle.recoveries == 1


def test_a_vector_read_on_a_lost_connection_fails_where_it_cannot_recover(
    big, small, monkeypatch
):
    with _reader(big, replace(small, recover_handles=False)) as handle:
        _lose_the_first_send(monkeypatch, handle)
        with pytest.raises(TransientError, match="went away"):
            handle.readv(_ranges(20))


def test_a_gather_of_nothing_sends_nothing(big, small):
    with _reader(big, small) as handle:
        with handle.session.bulk(handle.handle, chunk=1, depth=1) as channel:
            assert channel.gather([]) == []
    assert big.seen.count(c.kXR_readv) == 0


# ---------------------------------------------------------------------------
# The file objects
# ---------------------------------------------------------------------------


def test_a_writer_buffers_a_whole_window_by_default(server, small):
    raw = XRootDRawIO(File(server.url.with_path("/data/win.bin"), small), "wb")
    try:
        assert _write_window(raw, 1 << 20, -1) == max(1 << 20, 4096 * 3)
        assert _write_window(raw, 4096, 4096) == 4096
        raw.file.config = replace(small, bulk=False)
        assert _write_window(raw, 1 << 20, -1) == 1 << 20
    finally:
        raw.close()


def test_a_buffered_writer_streams_through_the_plane(server, small):
    with open_url(f"{server.url}//data/s.bin", "wb", config=small) as fh:
        view = memoryview(PAYLOAD)
        for at in range(0, len(PAYLOAD), 1000):
            fh.write(view[at : at + 1000])
    assert server.contents("/data/s.bin") == PAYLOAD


def test_a_raw_writer_gathers_a_strided_buffer(server, small):
    with open_url(f"{server.url}//data/strided.bin", "wb", buffering=0, config=small) as raw:
        assert raw.write(memoryview(b"abcdef")[::2]) == 3
        assert raw.write(b"") == 0
    assert server.contents("/data/strided.bin") == b"ace"


def test_reading_a_whole_file_object_is_byte_exact(big, small):
    with open_url(f"{big.url}//data/p.bin", "rb", config=small) as fh:
        assert fh.read() == PAYLOAD
    with open_url(f"{big.url}//data/p.bin", "rb", config=small) as fh:
        assert b"".join(iter(lambda: fh.read(777), b"")) == PAYLOAD
    assert isinstance(fh, io.BufferedReader)
