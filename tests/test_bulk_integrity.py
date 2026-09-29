"""A bulk read that ends early must not leave its replies on the wire.

The bulk reader keeps several reads in flight on stream ids it leased from the
session machine. When one of them ends the read early - ``kXR_wait``, an
error, a consumer that stops iterating - the others are still coming back.
Handing their ids back before those replies arrive lets the next request reuse
one, and then a stray chunk is accepted as that request's answer: the bytes a
caller gets back are simply wrong. These tests pin down that the reader either
takes every owed reply off the wire before the connection is used again, or
marks the connection broken so that nothing ever uses it again.
"""

from __future__ import annotations

import struct
from collections.abc import Callable, Iterator

import pytest

from xrdclient.client.file import File
from xrdclient.config import Config
from xrdclient.errors import ProtocolError, XRootDError
from xrdclient.errors import TimeoutError as XrdTimeoutError
from xrdclient.proto import constants as c
from xrdclient.testing import FakeServer
from xrdclient.testing import server as S

#: Several bulk chunks long, with every byte position telling where it came
#: from, so a chunk answered in the wrong place cannot go unnoticed.
PAYLOAD = bytes((i * 7 + i // 251) % 256 for i in range(2 << 20))

#: Small enough to keep several reads in flight on :data:`PAYLOAD`.
CHUNK = 256 << 10


def _config(**overrides: object) -> Config:
    settings: dict[str, object] = {
        "username": "tester",
        "auth_order": ("host",),
        "require_tls": False,
        "data_streams": 0,
        "bulk_chunk": CHUNK,
        "bulk_depth": 4,
    }
    settings.update(overrides)
    return Config(**settings)  # type: ignore[arg-type]


Answer = Callable[[int], Iterator[bytes]]


def _misbehave_on(nth: int, answer: Answer, *, then: dict[int, Answer] | None = None):
    """A read handler that gives the ``nth`` read ``answer`` and serves the rest.

    ``then`` maps later call numbers to answers of their own, for the tests
    that need the replies owed after the first failure to go wrong too.
    """
    special = {nth: answer, **(then or {})}
    calls = {"n": 0}

    def handler(conn, sid, params, body):
        calls["n"] += 1
        chosen = special.get(calls["n"])
        if chosen is not None:
            yield from chosen(sid)
            return
        yield from S._h_read(conn, sid, params, body)

    return handler


def _wait(sid: int) -> Iterator[bytes]:
    yield S.frame(sid, c.kXR_wait, struct.pack(">i", 0) + b"later\x00")


def _io_error(sid: int) -> Iterator[bytes]:
    yield S.error(sid, 3007, "transient io error")


def _silence(sid: int) -> Iterator[bytes]:
    """Never answer at all, so whoever is waiting for this reply stalls."""
    return
    yield  # pragma: no cover - makes this a generator


def _elsewhere(sid: int) -> Iterator[bytes]:
    """Answer on a stream id nobody asked on."""
    yield S.frame(sid + 1000, c.kXR_ok, b"x" * 16)


def _too_much(sid: int) -> Iterator[bytes]:
    """Answer with more bytes than any read in this file asked for."""
    yield S.frame(sid, c.kXR_ok, b"x" * (CHUNK + 1))


@pytest.fixture
def server():
    with FakeServer(files={"/f": PAYLOAD, "/g": bytes(range(256)) * 16}) as srv:
        yield srv


def _open(server, path: str = "/f", **overrides: object) -> File:
    handle = File(server.url.with_path(path), _config(**overrides))
    handle.open("r")
    return handle


def _still_answers_correctly(handle: File) -> None:
    """Small reads go through the event path and must get their own bytes."""
    for offset in (0, 3 * CHUNK + 17, len(PAYLOAD) - 4096):
        assert handle.read(4096, offset) == PAYLOAD[offset : offset + 4096]


def test_a_wait_mid_pipeline_does_not_corrupt_the_fallback(server):
    """The reported bug: kXR_wait on the second chunk, then the event path."""
    server.handlers[c.kXR_read] = _misbehave_on(2, _wait)
    with _open(server) as handle:
        assert handle.read(len(PAYLOAD), 0) == PAYLOAD
        _still_answers_correctly(handle)
        # The owed replies were drained, so the connection is still good.
        assert not handle.session.broken


def test_an_error_mid_pipeline_leaves_the_connection_clean(server):
    server.handlers[c.kXR_read] = _misbehave_on(2, _io_error)
    with _open(server) as handle:
        with pytest.raises(XRootDError):
            handle.read(len(PAYLOAD), 0)
        _still_answers_correctly(handle)
        assert not handle.session.broken
    with _open(server, "/g") as other:
        assert other.read(4096, 0) == bytes(range(256)) * 16


def test_an_abandoned_stream_drains_what_it_asked_for(server):
    """A consumer that stops early still owes the wire every in-flight reply."""
    server.chunk_reads = 32 << 10  # replies in kXR_oksofar instalments
    with _open(server) as handle:
        with handle.session.bulk(handle.handle, chunk=CHUNK, depth=4) as reader:
            pieces = reader.stream(0, len(PAYLOAD))
            offset, view = next(pieces)
            assert bytes(view) == PAYLOAD[offset : offset + len(view)]
            pieces.close()
        _still_answers_correctly(handle)
        assert not handle.session.broken


def test_a_consumer_that_raises_is_settled_by_the_session(server):
    """Leaving the lending block settles the reader even if its iterator lives on."""
    with _open(server) as handle:
        pieces = None
        with pytest.raises(RuntimeError):
            with handle.session.bulk(handle.handle, chunk=CHUNK, depth=4) as reader:
                pieces = reader.stream(0, len(PAYLOAD))
                next(pieces)
                raise RuntimeError("the consumer gave up")
        _still_answers_correctly(handle)
        assert not handle.session.broken
        # Closing the orphaned iterator afterwards finds nothing left to do.
        pieces.close()
        _still_answers_correctly(handle)


def _read_breaks(server, expected: type[Exception], **overrides: object) -> File:
    """Read the whole file, expect ``expected``, and hand back the handle.

    The handle is deliberately not closed: its connection is broken, and a
    broken connection is dropped rather than spoken to again. It is one that
    cannot be re-opened, so the failure reaches the caller rather than being
    recovered from on a fresh connection.
    """
    handle = _open(server, **{"recover_handles": False, **overrides})
    with pytest.raises(expected):
        handle.read(len(PAYLOAD), 0)
    assert handle.session.broken
    return handle


def test_a_stall_while_draining_breaks_the_connection(server):
    """If the owed replies never come, the connection is never reused.

    The error is the stall, not ``BulkUnsupported``: falling back onto this
    connection would read whatever it still has queued as the answer.
    """
    server.handlers[c.kXR_read] = _misbehave_on(2, _wait, then={3: _silence})
    _read_breaks(server, XrdTimeoutError, request_timeout=1.0)


def test_a_read_only_handle_rereads_a_stalled_bulk_read_elsewhere(server):
    """The stall still breaks the connection, but a reader can start over.

    Every read carries its offset, so a handle that can be re-opened reads
    the whole range again on a fresh connection and the caller gets the file.
    """
    server.handlers[c.kXR_read] = _misbehave_on(2, _wait, then={3: _silence})
    handle = _open(server, request_timeout=1.0)
    broken = handle.session
    try:
        assert handle.read(len(PAYLOAD), 0) == PAYLOAD
        assert broken.broken
        assert handle.recoveries == 1
    finally:
        handle.close()


def test_a_stray_reply_while_draining_breaks_the_connection(server):
    server.handlers[c.kXR_read] = _misbehave_on(2, _wait, then={3: _elsewhere})
    _read_breaks(server, ProtocolError)


@pytest.mark.parametrize("answer", [_elsewhere, _too_much], ids=["stray", "oversized"])
def test_a_torn_frame_breaks_the_connection(server, answer):
    """A frame the reader could not take whole leaves the wire out of step."""
    server.handlers[c.kXR_read] = _misbehave_on(2, answer)
    handle = _read_breaks(server, ProtocolError)
    # The ids stay leased, so no later request can be matched to a reply that
    # is still somewhere on the wire.
    assert handle.session.machine._leased


def test_a_wait_in_a_stream_drains_before_raising(server):
    """``stream`` settles the same way ``into`` does."""
    from xrdclient.session.bulk import BulkUnsupported

    server.handlers[c.kXR_read] = _misbehave_on(3, _wait)
    with _open(server) as handle:
        with pytest.raises(BulkUnsupported):
            with handle.session.bulk(handle.handle, chunk=CHUNK, depth=4) as reader:
                for _ in reader.stream(0, len(PAYLOAD)):
                    pass
        _still_answers_correctly(handle)
        assert not handle.session.broken


def test_a_reader_runs_one_read_at_a_time(server):
    """A second read while a stream is suspended would mix two sets of replies."""
    with _open(server) as handle:
        with handle.session.bulk(handle.handle, chunk=CHUNK, depth=2) as reader:
            pieces = reader.stream(0, len(PAYLOAD))
            next(pieces)
            with pytest.raises(ProtocolError):
                reader.into(memoryview(bytearray(4096)), 0)
            assert sum(len(view) for _, view in pieces) == len(PAYLOAD) - CHUNK
        _still_answers_correctly(handle)
