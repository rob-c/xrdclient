"""Contracts for shared write policies and library-backed storage/PKI parsing."""

from __future__ import annotations

import struct
from collections import deque
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any, cast

import pytest

from xrdclient.config import Config
from xrdclient.errors import ProtocolError, ServerError
from xrdclient.proto import constants as c
from xrdclient.proto.machine import SessionMachine, State
from xrdclient.session.bulk import BulkReader, BulkUnsupported
from xrdclient.session.sync import Session


class _Wire:
    def __init__(
        self, statuses: Iterable[int] = (), *, reverse: bool = False, lost: bool = False
    ) -> None:
        self.statuses = iter(statuses)
        self.reverse, self.lost = reverse, lost
        self.packets: deque[bytes] = deque()
        self.buffer = bytearray()
        self.sent: list[bytes] = []

    def send_buffers(self, parts: Iterable[bytes | memoryview]) -> None:
        # One request's frame: its header (with a short body joined to it)
        # and, for a long write, the caller's payload as a view of its own.
        for part in parts:
            self.send(part)

    def send(self, part: bytes | memoryview) -> None:
        self.sent.append(bytes(part))
        if isinstance(part, memoryview) or self.lost:
            return
        sid = int.from_bytes(part[:2], "big")
        status = next(self.statuses, c.kXR_ok)
        body = (3009).to_bytes(4, "big") + b"full\0" if status == c.kXR_error else b""
        self.packets.append(struct.pack(">HHI", sid, status, len(body)) + body)
        if status == c.kXR_oksofar:
            self.packets.append(struct.pack(">HHI", sid, c.kXR_ok, 0))

    def receive_into(self, view: memoryview) -> int:
        if not self.buffer:
            packets = list(self.packets)
            self.packets.clear()
            self.buffer.extend(b"".join(reversed(packets) if self.reverse else packets))
        count = min(len(view), len(self.buffer))
        view[:count] = self.buffer[:count]
        del self.buffer[:count]
        return count


def _writer(statuses: Iterable[int] = (), **wire_options: bool) -> tuple[BulkReader, Any]:
    machine = SessionMachine(host="test")
    machine.state = State.READY
    session = SimpleNamespace(
        machine=machine,
        transport=_Wire(statuses, **wire_options),
        config=Config(request_timeout=0.01),
        broken=False,
    )
    session.mark_broken = lambda: setattr(session, "broken", True)
    # A stand-in with only what the reader touches, in place of a real Session.
    return BulkReader(cast(Session, session), bytes(4), chunk=4, depth=2), session


def _pieces(*values: bytes) -> list[memoryview]:
    return [memoryview(value) for value in values]


def test_write_acknowledgements_report_ranges_in_reply_order():
    writer, session = _writer(reverse=True)
    acknowledged = []
    checked = []
    moved = writer.write_chunks(
        _pieces(b"abcd", b"ef", b"", b"gh"),
        10,
        acknowledged=lambda at, count: acknowledged.append((at, count)),
        check=lambda: checked.append(True),
    )
    assert moved == 8
    assert acknowledged == [(14, 2), (10, 4), (16, 2)]
    assert len(checked) == 3
    assert not writer._owed and not writer._leased and not session.broken


def test_wait_only_defers_the_declined_range_and_does_not_replay_it():
    writer, session = _writer([c.kXR_wait, c.kXR_ok])
    waited, acknowledged = [], []
    moved = writer.write_chunks(
        _pieces(b"abcd", b"ef"),
        20,
        waited=lambda at, count: waited.append((at, count)),
        acknowledged=lambda at, count: acknowledged.append((at, count)),
        strict_replies=True,
    )
    assert (moved, waited, acknowledged) == (2, [(20, 4)], [(24, 2)])
    assert len(session.transport.sent) == 2  # two writes; a short body rides with its header
    assert not session.broken and not writer._owed


def test_native_wait_fallback_remains_available():
    writer, session = _writer([c.kXR_wait, c.kXR_ok])
    with pytest.raises(BulkUnsupported, match="falls back"):
        writer.write_from(memoryview(b"abcdef"), 0)
    assert not session.broken and not writer._owed


def test_refusal_stops_new_writes_but_reports_other_acknowledgements():
    writer, session = _writer([c.kXR_error, c.kXR_ok])
    acknowledged = []
    with pytest.raises(ServerError, match="full"):
        writer.write_chunks(
            _pieces(b"abcd", b"ef", b"gh"),
            acknowledged=lambda at, count: acknowledged.append((at, count)),
        )
    assert acknowledged == [(4, 2)]
    assert len(session.transport.sent) == 2  # two writes; a short body rides with its header
    assert not writer._owed and not writer._leased and not session.broken


@pytest.mark.parametrize("strict", [False, True])
def test_error_status_is_not_mistaken_for_an_unexpected_reply(strict):
    writer, session = _writer([c.kXR_error])
    with pytest.raises(ServerError, match="full"):
        writer.write_chunks(_pieces(b"x"), strict_replies=strict)
    assert not session.broken


def test_strict_unexpected_reply_breaks_the_session():
    writer, session = _writer([c.kXR_redirect])
    with pytest.raises(ProtocolError, match="unexpected reply to a write"):
        writer.write_chunks(_pieces(b"x"), strict_replies=True)
    assert session.broken


def test_partial_acknowledgement_is_not_double_counted():
    writer, _ = _writer([c.kXR_oksofar])
    assert writer.write_chunks(_pieces(b"abcd")) == 4


def test_empty_and_oversized_write_chunks_release_their_leases():
    writer, session = _writer()
    assert writer.write_chunks(_pieces(b"")) == 0
    with pytest.raises(ValueError, match="chunk size"):
        writer.write_chunks(_pieces(b"12345"))
    assert not writer._leased and not session.broken


@pytest.mark.parametrize("hook", ["acknowledged", "waited", "check"])
def test_callback_failures_keep_their_cause_and_settle_the_session(hook):
    writer, session = _writer([c.kXR_wait] if hook == "waited" else [])
    failure = RuntimeError(hook)

    def fail(*arguments):
        raise failure

    callbacks: dict[str, Any] = {hook: fail}
    with pytest.raises(RuntimeError) as caught:
        writer.write_chunks(_pieces(b"x", b"y"), **callbacks)
    assert caught.value is failure
    assert not writer._leased and not writer._owed and not session.broken


def test_failed_cleanup_does_not_mask_cancellation():
    writer, session = _writer(lost=True)
    failure = RuntimeError("cancelled")

    def cancel():
        raise failure

    with pytest.raises(RuntimeError) as caught:
        writer.write_chunks(_pieces(b"x"), check=cancel)
    assert caught.value is failure and session.broken


def test_an_iterator_failure_happens_before_stream_ids_are_leased():
    writer, session = _writer()

    class Broken:
        def __iter__(self):
            raise RuntimeError("source unavailable")

    with pytest.raises(RuntimeError, match="source unavailable"):
        writer.write_chunks(Broken())
    assert not writer._leased and not session.broken
