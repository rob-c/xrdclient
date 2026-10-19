"""Uploading a stream of pieces, and calling bulk transfers off.

:meth:`File.write_pieces` is the public face of the bulk plane's writer -
the one an upload pipeline with its own reader and progress reporting
drives - and ``cancel=`` is how a bulk download, stream or upload is
stopped from outside, between pieces or in the middle of one.
"""

from __future__ import annotations

import os
import struct
import threading
import time
from collections.abc import Iterator
from dataclasses import replace

import pytest
from xrdcore import cancel as _cancel

from xrdclient.client import bulk
from xrdclient.client.file import File
from xrdclient.config import Config
from xrdclient.errors import AbortedError, ProtocolError, TransientError
from xrdclient.flags import OpenFlags
from xrdclient.proto import constants as c
from xrdclient.session.bulk import BulkUnsupported
from xrdclient.session.sync import Session
from xrdclient.testing import FakeServer, error, frame
from xrdclient.testing import server as S

PAYLOAD = bytes(range(256)) * 1024  # 256 KiB


@pytest.fixture
def cfg() -> Config:
    return Config(
        username="tester",
        auth_order=("host",),
        require_tls=False,
        data_streams=0,
        chunk_size=16 << 10,
        bulk_chunk=16 << 10,
        bulk_depth=3,
        bulk_workers=3,
        readahead=0,
    )


@pytest.fixture
def srv():
    with FakeServer(files={"/data/big.bin": PAYLOAD}, dirs=["/up"]) as server:
        yield server


def _pieces(data: bytes = PAYLOAD, size: int = 16 << 10) -> list[bytes]:
    return [data[at : at + size] for at in range(0, len(data), size)]


def _writer(server, cfg: Config, path: str = "/up/w.bin") -> File:
    handle = File(server.url.with_path(path), cfg)
    handle.open(OpenFlags.NEW | OpenFlags.UPDATE)
    return handle


def _nth_write(nth: int, answer):
    calls = {"n": 0}

    def handler(conn, sid, params, body):
        calls["n"] += 1
        if calls["n"] == nth:
            yield answer(sid)
            return
        yield from S._h_write(conn, sid, params, body)

    return handler


def _wait(sid: int) -> bytes:
    return frame(sid, c.kXR_wait, struct.pack(">i", 0) + b"busy\x00")


def _silent(conn, sid, params, body):
    return iter(())


# ---------------------------------------------------------------------------
# File.write_pieces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bulk_on", [True, False], ids=["plane", "serial"])
def test_pieces_land_back_to_back_and_are_acknowledged(srv, cfg, bulk_on):
    acked: list[tuple[int, int]] = []
    checks = []
    with _writer(srv, replace(cfg, bulk=bulk_on)) as handle:
        done = handle.write_pieces(
            _pieces(),
            100,
            acknowledged=lambda at, n: acked.append((at, n)),
            check=lambda: checks.append(1),
        )
        assert handle.size == 100 + len(PAYLOAD)
    assert done == len(PAYLOAD)
    assert srv.contents("/up/w.bin") == bytes(100) + PAYLOAD
    assert sorted(acked) == [(100 + at, 16 << 10) for at in range(0, len(PAYLOAD), 16 << 10)]
    assert len(checks) == len(_pieces())


def test_a_piece_the_server_will_not_take_now_is_handed_back(srv, cfg):
    srv.handlers[c.kXR_write] = _nth_write(2, _wait)
    waited: list[tuple[int, int]] = []
    acked: list[int] = []
    pieces = _pieces()
    with _writer(srv, cfg) as handle:
        done = handle.write_pieces(
            pieces, acknowledged=lambda at, n: acked.append(n), waited=lambda *s: waited.append(s)
        )
        assert waited == [(16 << 10, 16 << 10)]
        assert done == len(PAYLOAD) - (16 << 10) == sum(acked)
        # Sending it again is the caller's; it still has the bytes.
        handle.write(pieces[1], 16 << 10)
    assert srv.contents("/up/w.bin") == PAYLOAD


def test_without_a_wait_callback_a_wait_fails_the_upload(srv, cfg):
    srv.handlers[c.kXR_write] = _nth_write(2, _wait)
    with _writer(srv, cfg) as handle:
        with pytest.raises(BulkUnsupported):
            handle.write_pieces(_pieces())
        assert not handle.session.broken  # settled, not torn


def test_a_reply_that_is_no_answer_to_a_write_is_a_protocol_error(srv, cfg):
    redirect = struct.pack(">i", 1094) + b"elsewhere.example\x00"
    srv.handlers[c.kXR_write] = _nth_write(1, lambda sid: frame(sid, c.kXR_redirect, redirect))
    handle = _writer(srv, cfg)
    with pytest.raises(ProtocolError):
        handle.write_pieces(_pieces(), waited=lambda *s: None)
    assert handle.session.broken  # out of step: never used again
    with pytest.raises(TransientError):
        handle.close()  # a writer's close the server never heard is not a success


def test_a_refused_piece_raises_the_servers_error(srv, cfg):
    srv.handlers[c.kXR_write] = _nth_write(1, lambda sid: error(sid, 3009, "full"))
    with _writer(srv, cfg) as handle, pytest.raises(OSError, match="full"):
        handle.write_pieces(_pieces())


def test_a_declined_plane_falls_back_before_any_piece_is_taken(srv, cfg, monkeypatch):
    def declined(self, handle, *, chunk, depth):
        raise BulkUnsupported("busy wire")

    monkeypatch.setattr(Session, "bulk", declined)
    taken: list[int] = []

    def produce() -> Iterator[bytes]:
        for piece in _pieces():
            taken.append(len(piece))
            yield piece

    with _writer(srv, cfg) as handle:
        assert handle.write_pieces(produce()) == len(PAYLOAD)
    assert srv.contents("/up/w.bin") == PAYLOAD


def test_a_check_that_raises_stops_the_upload(srv, cfg):
    def stop():
        raise RuntimeError("enough")

    with _writer(srv, cfg) as handle, pytest.raises(RuntimeError, match="enough"):
        handle.write_pieces(_pieces(), check=stop)


def test_a_lost_connection_is_transient_and_an_abort_is_an_abort(srv, cfg):
    srv.handlers[c.kXR_write] = _silent
    handle = _writer(srv, cfg)
    threading.Timer(0.2, lambda: handle.session.close()).start()
    with pytest.raises(TransientError):
        handle.write_pieces(_pieces())
    other = _writer(srv, cfg, "/up/x.bin")
    threading.Timer(0.2, other.abort).start()
    with pytest.raises(AbortedError):
        other.write_pieces(_pieces())
    other.close()


# ---------------------------------------------------------------------------
# Cancelling bulk downloads
# ---------------------------------------------------------------------------


def test_a_download_cancelled_between_pieces_stops(srv, cfg, tmp_path):
    stop = threading.Event()

    def progress(done: int, total: int | None) -> None:
        if done >= 32 << 10:
            stop.set()

    with pytest.raises(AbortedError):
        bulk.download(
            srv.url / "data/big.bin", tmp_path / "out", config=cfg, progress=progress, cancel=stop
        )


def test_a_download_blocked_on_the_network_is_woken_by_its_cancel(srv, cfg, tmp_path):
    reads = {"n": 0}

    def stall_after_a_few(conn, sid, params, body):
        reads["n"] += 1
        if reads["n"] > 2:
            return iter(())
        return S._h_read(conn, sid, params, body)

    srv.handlers[c.kXR_read] = stall_after_a_few
    started = time.monotonic()
    deadline = started + 0.5
    with pytest.raises(AbortedError):
        bulk.download(
            srv.url / "data/big.bin",
            tmp_path / "out",
            config=replace(cfg, request_timeout=60, bulk_workers=1),
            cancel=lambda: time.monotonic() > deadline,
        )
    assert time.monotonic() - started < 10


def test_a_multi_worker_download_blocked_on_the_network_is_woken(srv, cfg, tmp_path):
    srv.handlers[c.kXR_read] = _silent
    started = time.monotonic()
    deadline = started + 0.3
    with pytest.raises(AbortedError):
        bulk.download(
            srv.url / "data/big.bin",
            tmp_path / "out",
            config=replace(cfg, request_timeout=60),
            cancel=lambda: time.monotonic() > deadline,
        )
    assert time.monotonic() - started < 10


def test_an_ordered_stream_can_be_cancelled(srv, cfg):
    stop = threading.Event()
    got: list[int] = []

    def writer(view: memoryview) -> None:
        got.append(len(view))
        stop.set()

    with pytest.raises(AbortedError):
        bulk.stream(srv.url / "data/big.bin", writer, config=cfg, cancel=stop)


def test_a_stream_to_a_pipe_carries_the_cancel(srv, cfg):
    read_end, write_end = os.pipe()
    stop = threading.Event()
    stop.set()
    try:
        with pytest.raises(AbortedError):
            bulk.download(srv.url / "data/big.bin", write_end, config=cfg, cancel=stop)
    finally:
        os.close(read_end)
        os.close(write_end)


def test_an_uncancelled_download_is_untouched(srv, cfg, tmp_path):
    result = bulk.download(
        srv.url / "data/big.bin", tmp_path / "out", config=cfg, cancel=lambda: False
    )
    assert result.size == len(PAYLOAD)
    assert (tmp_path / "out").read_bytes() == PAYLOAD


def test_a_handle_opened_after_the_cancel_is_aborted_at_once(srv, cfg):
    transfer = bulk._Transfer(
        srv.url / "data/big.bin", cfg, bulk._Options(depth=1), total=0, chunk=1
    )
    transfer.stop_now()
    handle = File(srv.url / "data/big.bin", cfg)
    transfer.track(handle)
    with pytest.raises(AbortedError):
        handle.open("r")


def test_a_failure_after_a_cancel_is_reported_as_the_cancel(srv, cfg, monkeypatch):
    transfer = bulk._Transfer(
        srv.url / "data/big.bin", cfg, bulk._Options(depth=1), total=0, chunk=1
    )
    transfer.aborted = True

    def broken() -> None:
        raise ValueError("what the abort left behind")

    with pytest.raises(AbortedError) as caught:
        bulk._run([broken], transfer)
    assert isinstance(caught.value.__cause__, ValueError)
    transfer.aborted = False
    with pytest.raises(ValueError):
        bulk._run([broken], transfer)


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------


def test_an_upload_copies_a_local_file(srv, cfg, tmp_path):
    local = tmp_path / "in.bin"
    local.write_bytes(PAYLOAD)
    seen: list[tuple[int, int | None]] = []
    result = bulk.upload(
        local, srv.url / "up/u.bin", config=cfg, progress=lambda d, t: seen.append((d, t))
    )
    assert result.size == len(PAYLOAD) and result.workers == 1
    assert srv.contents("/up/u.bin") == PAYLOAD
    assert seen[-1] == (len(PAYLOAD), len(PAYLOAD))
    assert "upload" in bulk.__all__


def test_an_upload_resends_what_the_server_could_not_take(srv, cfg, tmp_path):
    local = tmp_path / "in.bin"
    local.write_bytes(PAYLOAD)
    srv.handlers[c.kXR_write] = _nth_write(3, _wait)
    with open(local, "rb") as fh:
        fh.seek(0)
        result = bulk.upload(fh.fileno(), srv.url / "up/u.bin", config=cfg, posc=True)
    assert result.size == len(PAYLOAD)
    assert srv.contents("/up/u.bin") == PAYLOAD


def test_an_upload_from_a_pipe_cannot_resend_and_says_so(srv, cfg):
    read_end, write_end = os.pipe()
    os.write(write_end, PAYLOAD[: 32 << 10])
    os.close(write_end)
    srv.handlers[c.kXR_write] = _nth_write(1, _wait)
    try:
        with pytest.raises(BulkUnsupported):
            bulk.upload(read_end, srv.url / "up/p.bin", config=cfg)
    finally:
        os.close(read_end)


def test_an_upload_may_refuse_to_replace(srv, cfg, tmp_path):
    local = tmp_path / "in.bin"
    local.write_bytes(b"x")
    bulk.upload(local, srv.url / "up/once.bin", config=cfg)
    with pytest.raises(FileExistsError):
        bulk.upload(local, srv.url / "up/once.bin", config=cfg, overwrite=False)


def test_an_upload_can_be_cancelled(srv, cfg, tmp_path):
    local = tmp_path / "in.bin"
    local.write_bytes(PAYLOAD)
    with pytest.raises(AbortedError):
        bulk.upload(local, srv.url / "up/c.bin", config=cfg, cancel=lambda: True)


def test_an_upload_stalled_on_the_network_is_woken_by_its_cancel(srv, cfg, tmp_path):
    local = tmp_path / "in.bin"
    local.write_bytes(PAYLOAD)
    srv.handlers[c.kXR_write] = _silent
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    started = time.monotonic()
    with pytest.raises(AbortedError):
        bulk.upload(
            local,
            srv.url / "up/s.bin",
            config=replace(cfg, request_timeout=60),
            cancel=stop,
        )
    assert time.monotonic() - started < 10


def test_an_upload_failing_after_its_cancel_reports_the_cancel(srv, cfg, tmp_path, monkeypatch):
    local = tmp_path / "in.bin"
    local.write_bytes(PAYLOAD)
    stop = threading.Event()

    def failing(self, pieces, offset=0, **kwargs):
        stop.set()  # the cancel comes, and the stream breaks under it
        raise ProtocolError("torn by the abort")

    monkeypatch.setattr(File, "write_pieces", failing)
    with pytest.raises(AbortedError):
        bulk.upload(local, srv.url / "up/f.bin", config=cfg, cancel=stop)
    with pytest.raises(ProtocolError):
        bulk.upload(local, srv.url / "up/g.bin", config=cfg)


# ---------------------------------------------------------------------------
# The hook itself
# ---------------------------------------------------------------------------


def test_a_hook_that_raises_counts_as_a_cancel():
    fired = threading.Event()

    def broken() -> bool:
        raise RuntimeError("cannot say")

    with _cancel.watching(broken, fired.set, interval=0.01):
        assert fired.wait(5)


def test_no_hook_watches_nothing():
    with _cancel.watching(None, lambda: pytest.fail("called")):
        pass
    assert _cancel.as_check(None) is None
    event = threading.Event()
    assert _cancel.as_check(event) == event.is_set
