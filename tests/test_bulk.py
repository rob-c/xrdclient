"""The bulk data plane: the fast read path and the transfer built on it.

Everything here runs against :class:`~xrd.testing.FakeServer`, so what is
being checked is the client's framing and accounting rather than any one
server's generosity. The point of these tests is that the fast path and the
ordinary path are indistinguishable in their results - same bytes, same
lengths, same errors - however differently they get there.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time

import pytest

import xrd
from xrd.client.bulk import BulkResult, download, stream
from xrd.client.file import File
from xrd.config import Config
from xrd.errors import ProtocolError, XRootDError
from xrd.proto import requests as r
from xrd.proto.machine import SessionMachine, State
from xrd.session.bulk import BulkReader
from xrd.testing import FakeServer

#: Big enough that the transfer splits into several requests at the chunk
#: sizes these tests use, small enough to stay instant.
PAYLOAD = bytes(range(256)) * 4096  # 1 MiB, and every byte position distinct


@pytest.fixture
def cfg() -> Config:
    """Fast-path settings small enough to exercise the pipeline on 1 MiB."""
    return Config(
        username="tester",
        auth_order=("host",),
        require_tls=False,
        data_streams=0,
        bulk_chunk=64 << 10,
        bulk_depth=4,
        bulk_workers=3,
    )


@pytest.fixture
def bulk_server():
    with FakeServer(files={"/data/big.bin": PAYLOAD, "/data/tiny.bin": b"abc"}) as srv:
        yield srv


def _url(server, path: str = "/data/big.bin"):
    """The server's URL pointed at ``path``."""
    return server.url.with_path(path)


def _plain() -> Config:
    """The same settings with the fast path off, for a side-by-side check."""
    return Config(
        username="tester",
        auth_order=("host",),
        require_tls=False,
        data_streams=0,
        bulk=False,
    )


def _opened(server, config: Config, path: str = "/data/big.bin") -> File:
    handle = File(_url(server, path), config)
    handle.open("r")
    return handle


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


def test_into_fills_the_whole_buffer(bulk_server, cfg):
    """A pipelined read lands every byte, in the caller's own buffer."""
    with _opened(bulk_server, cfg) as handle:
        buffer = bytearray(len(PAYLOAD))
        with handle.session.bulk(handle.handle, chunk=64 << 10, depth=4) as reader:
            got = reader.into(memoryview(buffer), 0)
    assert got == len(PAYLOAD)
    assert bytes(buffer) == PAYLOAD


def test_into_reads_from_an_offset(bulk_server, cfg):
    with _opened(bulk_server, cfg) as handle:
        buffer = bytearray(4096)
        with handle.session.bulk(handle.handle, chunk=1024, depth=3) as reader:
            got = reader.into(memoryview(buffer), 8192)
    assert got == 4096
    assert bytes(buffer) == PAYLOAD[8192:12288]


def test_into_stops_short_at_end_of_file(bulk_server, cfg):
    """Asking past the end is a short read, not an error."""
    with _opened(bulk_server, cfg) as handle:
        buffer = bytearray(len(PAYLOAD) + 4096)
        with handle.session.bulk(handle.handle, chunk=64 << 10, depth=4) as reader:
            got = reader.into(memoryview(buffer), 0)
    assert got == len(PAYLOAD)
    assert bytes(buffer[:got]) == PAYLOAD


def test_stream_hands_back_pieces_in_file_order(bulk_server, cfg):
    """Order matters to a consumer that cannot seek, so the reader keeps it."""
    with _opened(bulk_server, cfg) as handle:
        offsets, joined = [], bytearray()
        with handle.session.bulk(handle.handle, chunk=32 << 10, depth=4) as reader:
            for offset, view in reader.stream(0, len(PAYLOAD)):
                offsets.append(offset)
                joined += view
    assert offsets == sorted(offsets)
    assert offsets[0] == 0
    assert bytes(joined) == PAYLOAD


def test_stream_bounds_its_memory(bulk_server, cfg):
    """Whatever the file's length, the reader holds ``chunk * depth``."""
    with _opened(bulk_server, cfg) as handle:
        with handle.session.bulk(handle.handle, chunk=16 << 10, depth=3) as reader:
            assert sum(len(b) for b in reader._bufs) == 3 * (16 << 10)
            total = sum(len(view) for _, view in reader.stream(0, len(PAYLOAD)))
    assert total == len(PAYLOAD)


def test_a_view_is_only_good_until_the_next_piece(bulk_server, cfg):
    """The buffers rotate, which is the contract the docstring promises."""
    with _opened(bulk_server, cfg) as handle:
        with handle.session.bulk(handle.handle, chunk=16 << 10, depth=2) as reader:
            pieces = reader.stream(0, len(PAYLOAD))
            _, first = next(pieces)
            copied = bytes(first)
            for _ in range(4):
                next(pieces)
            # The same slot has been reused by now; the copy is what survives.
            assert copied == PAYLOAD[: len(copied)]
            pieces.close()


def test_bulk_needs_an_idle_connection(bulk_server, cfg):
    """Two readers on one connection would take each other's replies."""
    with _opened(bulk_server, cfg) as handle:
        with handle.session.bulk(handle.handle, chunk=1024, depth=1):
            with pytest.raises(XRootDError):
                with handle.session.bulk(handle.handle, chunk=1024, depth=1):
                    pass  # pragma: no cover - the entry is what raises


def test_reader_rejects_impossible_geometry(bulk_server, cfg):
    with _opened(bulk_server, cfg) as handle:
        with pytest.raises(ValueError):
            BulkReader(handle.session, handle.handle, chunk=0, depth=1)
        with pytest.raises(ValueError):
            BulkReader(handle.session, handle.handle, chunk=1024, depth=0)


# ---------------------------------------------------------------------------
# Stream id leasing
# ---------------------------------------------------------------------------


def test_leased_ids_are_not_handed_out_twice():
    machine = SessionMachine(host="h")
    machine.state = State.READY
    leased = machine.lease_sids(4)
    assert len(set(leased)) == 4
    again = machine.lease_sids(4)
    assert not set(leased) & set(again)
    machine.release_sids(leased)
    machine.release_sids(again)


def test_framing_needs_a_leased_id():
    """A request framed on an id the machine still owns could collide."""
    machine = SessionMachine(host="h")
    machine.state = State.READY
    request = r.Read(b"abcd", 0, 1024)
    with pytest.raises(ProtocolError):
        machine.frame_for(request, 9999)
    (sid,) = machine.lease_sids(1)
    assert machine.frame_for(request, sid).startswith(sid.to_bytes(2, "big"))


def test_idle_is_false_with_a_request_outstanding():
    machine = SessionMachine(host="h")
    machine.state = State.READY
    assert machine.idle()
    machine.submit(r.Read(b"abcd", 0, 16))
    assert not machine.idle()


# ---------------------------------------------------------------------------
# The transfer
# ---------------------------------------------------------------------------


def test_download_writes_the_file(tmp_path, bulk_server, cfg):
    target = tmp_path / "out.bin"
    result = download(_url(bulk_server), target, config=cfg)
    assert isinstance(result, BulkResult)
    assert result.size == len(PAYLOAD)
    assert target.read_bytes() == PAYLOAD


def test_download_fans_out(tmp_path, bulk_server, cfg):
    """Several connections, each writing its own span at its own offset."""
    target = tmp_path / "out.bin"
    result = download(_url(bulk_server), target, config=cfg, workers=3)
    assert result.workers == 3
    assert target.read_bytes() == PAYLOAD


def test_download_to_a_file_descriptor(tmp_path, bulk_server, cfg):
    target = tmp_path / "fd.bin"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        download(_url(bulk_server), fd, config=cfg)
    finally:
        os.close(fd)
    assert target.read_bytes() == PAYLOAD


def test_download_reports_progress(tmp_path, bulk_server, cfg):
    seen: list[tuple[int, int | None]] = []
    lock = threading.Lock()

    def note(done: int, total: int | None) -> None:
        with lock:
            seen.append((done, total))

    download(
        _url(bulk_server), tmp_path / "p.bin", config=cfg, progress=note
    )
    assert seen
    assert seen[-1][0] == len(PAYLOAD)
    assert {total for _, total in seen} == {len(PAYLOAD)}


def test_download_of_a_small_file_uses_one_connection(tmp_path, bulk_server, cfg):
    """A worker that would not get a whole chunk is not worth its connection."""
    result = download(_url(bulk_server, "/data/tiny.bin"), tmp_path / "t.bin", config=cfg)
    assert result.workers == 1
    assert (tmp_path / "t.bin").read_bytes() == b"abc"


def test_download_refuses_to_report_a_short_transfer(tmp_path, bulk_server, cfg):
    """Told the file is longer than it is, the transfer fails rather than lies."""
    with pytest.raises(XRootDError, match="incomplete"):
        download(
            _url(bulk_server, "/data/tiny.bin"),
            tmp_path / "s.bin",
            config=cfg,
            size=len(b"abc") + 4096,
        )


def test_stream_is_in_order(bulk_server, cfg):
    joined = bytearray()
    result = stream(_url(bulk_server), joined.extend, config=cfg)
    assert result.size == len(PAYLOAD)
    assert bytes(joined) == PAYLOAD


def test_stream_stays_ordered_across_workers(bulk_server, cfg):
    """More than one connection still hands the writer the file's own order."""
    joined = bytearray()
    result = stream(_url(bulk_server), joined.extend, config=cfg, workers=3)
    assert result.workers == 3
    assert bytes(joined) == PAYLOAD


# ---------------------------------------------------------------------------
# The paths that use it
# ---------------------------------------------------------------------------


def test_readinto_matches_the_event_path(bulk_server, cfg):
    """The fast path and the ordinary path return the same bytes."""
    with _opened(bulk_server, cfg) as handle:
        fast = bytearray(len(PAYLOAD))
        assert handle.readinto(fast, 0) == len(PAYLOAD)
    with _opened(bulk_server, _plain()) as handle:
        ordinary = bytearray(len(PAYLOAD))
        assert handle.readinto(ordinary, 0) == len(PAYLOAD)
    assert bytes(fast) == bytes(ordinary) == PAYLOAD


def test_open_read_is_unchanged_by_the_fast_path(bulk_server, cfg):
    with xrd.open(_url(bulk_server), "rb", buffering=0, config=cfg) as fh:
        assert fh.read() == PAYLOAD


def test_copy_to_a_local_file_uses_the_fast_path(tmp_path, bulk_server, cfg):
    target = tmp_path / "copied.bin"
    result = xrd.copy(_url(bulk_server), target, config=cfg)
    assert result.size == len(PAYLOAD)
    assert target.read_bytes() == PAYLOAD


def test_copy_into_a_stream_is_ordered(tmp_path, bulk_server, cfg):
    target = tmp_path / "streamed.bin"
    with open(target, "wb") as fh:
        xrd.copy(_url(bulk_server), fh, config=cfg)
    assert target.read_bytes() == PAYLOAD


def test_copy_respects_no_overwrite(tmp_path, bulk_server, cfg):
    target = tmp_path / "there.bin"
    target.write_bytes(b"keep me")
    with pytest.raises(FileExistsError):
        xrd.copy(_url(bulk_server), target, config=cfg, overwrite=False)
    assert target.read_bytes() == b"keep me"


def test_turning_the_fast_path_off_still_copies(tmp_path, bulk_server):
    target = tmp_path / "slow.bin"
    xrd.copy(_url(bulk_server), target, config=_plain())
    assert hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(PAYLOAD).digest()


# ---------------------------------------------------------------------------
# Losing the server in the middle
# ---------------------------------------------------------------------------


def test_download_survives_a_server_that_drops_the_connection(tmp_path, bulk_server, cfg):
    """A worker that loses its link re-opens and carries on from its own mark.

    The server drops every live connection once, part way through, exactly as
    a restarting data server would. The transfer is expected to finish with
    the right bytes rather than to fail, and to say that it restarted.
    """
    dropped = threading.Event()

    def cut(done: int, total: int | None) -> None:
        if not dropped.is_set() and done > len(PAYLOAD) // 4:
            dropped.set()
            bulk_server.disconnect()

    target = tmp_path / "survivor.bin"
    result = download(_url(bulk_server), target, config=cfg, workers=2, progress=cut)
    assert dropped.is_set()
    assert result.size == len(PAYLOAD)
    assert target.read_bytes() == PAYLOAD


def test_stream_survives_a_dropped_connection(bulk_server, cfg):
    """The ordered path resumes too, and the writer still sees one clean file."""
    dropped = threading.Event()
    joined = bytearray()

    def cut(done: int, total: int | None) -> None:
        if not dropped.is_set() and done > len(PAYLOAD) // 4:
            dropped.set()
            bulk_server.disconnect()

    result = stream(_url(bulk_server), joined.extend, config=cfg, workers=1, progress=cut)
    assert dropped.is_set()
    assert result.size == len(PAYLOAD)
    assert bytes(joined) == PAYLOAD


def test_a_transfer_gives_up_when_its_recovery_budget_runs_out(tmp_path, bulk_server, cfg):
    """A server that never comes back is an error, not an unbounded retry.

    The budget is a length of time rather than a count of attempts, so the
    check is that the failure arrives inside it rather than after some number
    of tries.
    """
    stubborn = Config(
        username="tester",
        auth_order=("host",),
        require_tls=False,
        data_streams=0,
        bulk_chunk=64 << 10,
        bulk_depth=2,
        bulk_recovery=1.0,
        connect_retries=1,
        connect_timeout=1.0,
        request_timeout=2.0,
    )
    url = _url(bulk_server)
    bulk_server.stop()  # the port is gone for good
    started = time.monotonic()
    with pytest.raises(XRootDError):
        download(url, tmp_path / "never.bin", config=stubborn)
    assert time.monotonic() - started < 30.0


def test_an_outage_longer_than_the_old_retry_count_is_survived(tmp_path, bulk_server, cfg):
    """The budget is what carries a transfer over a server that is slow to return.

    The server is dropped repeatedly - more times than any attempt count would
    have allowed - and the transfer is still expected to finish.
    """
    drops = {"n": 0}

    def cut(done: int, total: int | None) -> None:
        if drops["n"] < 6 and done > (drops["n"] + 1) * len(PAYLOAD) // 10:
            drops["n"] += 1
            bulk_server.disconnect()

    patient = Config(
        username="tester",
        auth_order=("host",),
        require_tls=False,
        data_streams=0,
        bulk_chunk=32 << 10,
        bulk_depth=2,
        bulk_recovery=30.0,
        retry_backoff=0.01,
    )
    target = tmp_path / "patient.bin"
    result = download(url := _url(bulk_server), target, config=patient, workers=1, progress=cut)
    del url
    assert drops["n"] >= 4
    assert result.restarts >= 4
    assert target.read_bytes() == PAYLOAD


def test_a_verified_copy_compares_both_ends(tmp_path, bulk_server, cfg):
    """The fast path is an out-of-order transfer, so it verifies by comparison."""
    target = tmp_path / "verified.bin"
    result = xrd.copy(_url(bulk_server), target, config=cfg, verify=True, algorithm="adler32")
    assert target.read_bytes() == PAYLOAD
    assert result.checksum is not None
    assert result.verified


def test_verification_catches_a_target_that_does_not_match(tmp_path, bulk_server, cfg):
    """A destination that is not what the server holds must not pass."""
    from xrd.errors import ChecksumMismatchError

    target = tmp_path / "tampered.bin"
    xrd.copy(_url(bulk_server), target, config=cfg)
    target.write_bytes(PAYLOAD[:-1] + b"\x00")  # one byte different, same length
    with pytest.raises(ChecksumMismatchError):
        xrd.copy(
            _url(bulk_server),
            target,
            config=cfg,
            verify=True,
            algorithm="adler32",
            resume=True,
        )


def test_a_failed_worker_does_not_strand_the_others(bulk_server, cfg):
    """An ordered stream whose worker dies must fail, not hang.

    The writer refuses the piece that would come after the gap, which is what
    a worker waiting its turn behind a dead one would otherwise wait for
    forever.
    """
    calls = {"n": 0}

    def explode(view: memoryview) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("the writer gave up")

    with pytest.raises(RuntimeError, match="gave up"):
        stream(_url(bulk_server), explode, config=cfg, workers=3)


def test_a_single_connection_transfer_opens_the_file_once(tmp_path, bulk_server, cfg):
    """The length comes from the open, so nothing opens the file to ask.

    An extra open is a wasted round trip on any server and a second
    authorisation on one that checks a token, so the handle the size was read
    from is the handle the first worker reads through.
    """
    bulk_server.opened.clear()
    download(_url(bulk_server), tmp_path / "once.bin", config=cfg, workers=1)
    assert len(bulk_server.opened) == 1


def test_a_fanned_out_transfer_opens_once_per_connection(tmp_path, bulk_server, cfg):
    """Three workers means three opens, not four: the first reuses the probe."""
    bulk_server.opened.clear()
    download(_url(bulk_server), tmp_path / "thrice.bin", config=cfg, workers=3)
    assert len(bulk_server.opened) == 3


def test_an_unverifiable_copy_does_not_digest_the_file(tmp_path, bulk_server, cfg, monkeypatch):
    """A server that cannot checksum is found out before a gigabyte is hashed.

    The digest exists only to be compared with the server's answer, so asking
    for that answer first is what lets the transfer skip the hashing entirely
    rather than do it and throw it away.
    """
    from xrd.copy import engine

    hashed = 0
    real = engine.new_checksum

    class Counting:
        """A digest that reports how much was actually put through it."""

        def __init__(self, algorithm: str) -> None:
            self._inner = real(algorithm)

        def update(self, data) -> None:
            nonlocal hashed
            hashed += len(data)
            self._inner.update(data)

        def hexdigest(self) -> str:
            return self._inner.hexdigest()

    def counting(algorithm: str):
        return Counting(algorithm)

    def refuses(*_: object, **__: object):
        raise OSError("this server cannot checksum")

    monkeypatch.setattr(engine, "new_checksum", counting)
    monkeypatch.setattr(engine, "_server_checksum", refuses)
    target = tmp_path / "unverifiable.bin"
    with open(target, "wb") as fh:
        result = xrd.copy(_url(bulk_server), fh, config=cfg)  # verification on by default
    assert target.read_bytes() == PAYLOAD
    assert result.checksum is None
    assert hashed == 0, f"{hashed} bytes were digested for a comparison that cannot happen"


def test_a_verifiable_stream_is_still_digested(tmp_path, bulk_server, cfg):
    """Where the server does answer, the digest is taken and compared."""
    target = tmp_path / "verified-stream.bin"
    with open(target, "wb") as fh:
        result = xrd.copy(
            _url(bulk_server), fh, config=cfg, verify=True, algorithm="adler32"
        )
    assert target.read_bytes() == PAYLOAD
    assert result.checksum is not None
    assert result.verified
