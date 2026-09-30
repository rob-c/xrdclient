"""XrdCl's copy controls: several sources, a dynamic source, rate limits, timeouts, coerce.

Each is a keyword of :func:`xrdclient.copy`, a flag of ``xrd-cp`` named after
``xrdcp``'s, and an ``add_job`` keyword of the compat ``CopyProcess``. The
tests at the end run the official bindings beside the compat layer on real
daemons and require the same statuses, results and handler calls.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import pytest

import xrdclient
from xrdclient.cli import cp
from xrdclient.compat import client
from xrdclient.compat.client import _status, env
from xrdclient.copy import (
    CopyTimeoutError,
    NoMoreReplicasError,
    RateThresholdError,
    limits,
    replicas,
)
from xrdclient.copy.engine import CopyResult
from xrdclient.flags import OpenFlags
from xrdclient.proto import constants as c
from xrdclient.testing import FakeServer, error, frame
from xrdclient.testing import server as fake

PAYLOAD = bytes(range(256)) * 64  # 16 KiB


def _config(**changes: Any) -> xrdclient.Config:
    return xrdclient.Config(
        username="tester", auth_order=("host",), require_tls=False, data_streams=0, **changes
    )


CONFIG = _config()


# ---------------------------------------------------------------------------
# Pace: the arithmetic, on a clock the test turns
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 6))
        self.now += seconds


def _pace(clock: Clock, **limits_: Any) -> limits.Pace:
    return limits.Pace(limits.Limits(**limits_), clock=clock, sleep=clock.sleep)


def test_nothing_is_limited_by_default():
    assert not limits.Limits().active
    clock = Clock()
    pace = _pace(clock)
    assert not pace.limited
    pace.chunk(1 << 30)
    pace.check()
    assert clock.slept == []


def test_the_rate_cap_sleeps_off_what_a_chunk_puts_the_copy_ahead():
    clock = Clock()
    pace = _pace(clock, max_rate=100.0)
    pace.chunk(50)  # no time has passed yet: nothing to measure against
    clock.now += 0.25
    pace.chunk(50)  # 100 bytes in 0.25 s, at 100 B/s: 0.75 s early
    assert clock.slept == [0.75]
    clock.now += 2
    pace.chunk(50)  # behind, not ahead: no sleep
    assert clock.slept == [0.75]


def test_the_threshold_is_judged_every_interval_plus_one_chunks():
    """XrdCl counts ``parallelChunks`` down, judges at zero, then starts again."""
    clock = Clock()
    pace = _pace(clock, min_rate=1000.0, interval=2)
    clock.now += 1
    pace.chunk(10)
    pace.chunk(10)  # far too slow, but only the third chunk is judged
    with pytest.raises(RateThresholdError, match="dropped below requested threshold"):
        pace.chunk(10)


def test_a_fast_enough_copy_passes_the_threshold_and_is_judged_again_later():
    clock = Clock()
    pace = _pace(clock, min_rate=10.0, interval=1)
    clock.now += 1
    for _ in range(4):
        pace.chunk(100)
    clock.now += 1000
    pace.chunk(1)  # the countdown restarted after the second chunk: not judged
    with pytest.raises(RateThresholdError):
        pace.chunk(1)


def test_the_threshold_needs_time_to_have_passed():
    clock = Clock()
    pace = _pace(clock, min_rate=10.0, interval=0)
    pace.chunk(1)  # elapsed is zero: nothing can be judged yet


def test_the_timeout_counts_from_the_start_of_the_copy():
    clock = Clock()
    pace = _pace(clock, timeout=5.0)
    clock.now += 4
    pace.begin()
    pace.chunk(1)
    pace.check()
    clock.now += 1.5
    with pytest.raises(CopyTimeoutError, match="CPTimeout exceeded"):
        pace.check()
    with pytest.raises(CopyTimeoutError):
        pace.chunk(1)


def test_watch_measures_deltas_from_where_the_reports_start():
    clock = Clock()
    pace = _pace(clock, max_rate=10.0)
    heard: list[tuple[int, int | None]] = []
    report = pace.watch(lambda done, total: heard.append((done, total)), start=1000)
    clock.now += 1
    report(1005, 2000)  # five bytes moved, not a thousand and five
    assert clock.slept == []
    report(1030, 2000)  # thirty in a second, at ten a second: two more seconds
    assert clock.slept == [2.0]
    assert heard == [(1005, 2000), (1030, 2000)]


def test_watch_turns_a_limit_into_something_no_retry_loop_catches():
    clock = Clock()
    report = _pace(clock, timeout=1.0).watch(None)
    clock.now += 2
    with pytest.raises(limits._Stop) as caught:
        report(1, None)
    assert isinstance(caught.value.error, CopyTimeoutError)
    assert not isinstance(caught.value, OSError)


def test_the_limit_errors_are_transient_as_xrdcl_retries_them():
    assert issubclass(CopyTimeoutError, xrdclient.errors.TimeoutError)
    assert issubclass(RateThresholdError, xrdclient.errors.TransientError)


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def _slow_reads(server: FakeServer, delay: float) -> None:
    """Make every ``kXR_read`` on ``server`` take ``delay`` seconds."""

    def slow(conn, sid, params, body):
        time.sleep(delay)
        yield from fake._h_read(conn, sid, params, body)

    server.handlers[c.kXR_read] = slow


def test_max_rate_caps_the_throughput(tmp_path):
    source = tmp_path / "s.bin"
    source.write_bytes(PAYLOAD)
    started = time.monotonic()
    result = xrdclient.copy(source, tmp_path / "t.bin", chunk_size=2048, max_rate=32768)
    elapsed = time.monotonic() - started
    assert (tmp_path / "t.bin").read_bytes() == PAYLOAD
    # 16 KiB at 32 KiB/s: half a second, give or take the last chunk's sleep.
    assert 0.4 <= elapsed < 5
    assert result.rate <= 32768 * 1.1


def test_a_download_that_runs_out_of_time_fails(tmp_path):
    with FakeServer(files={"/f": PAYLOAD}) as srv:
        _slow_reads(srv, 0.3)
        with pytest.raises(CopyTimeoutError, match="CPTimeout"):
            xrdclient.copy(srv.url / "f", tmp_path / "t", chunk_size=1024, timeout=0.5,
                           config=_config(in_flight=1))


def test_a_download_slower_than_the_threshold_fails(tmp_path):
    with FakeServer(files={"/f": PAYLOAD}) as srv:
        _slow_reads(srv, 0.05)
        with pytest.raises(RateThresholdError):
            xrdclient.copy(srv.url / "f", tmp_path / "t", chunk_size=1024, min_rate=1 << 30,
                           config=_config(in_flight=1))


def test_a_timeout_before_the_first_byte_fails_before_moving_one(tmp_path):
    source = tmp_path / "s.bin"
    source.write_bytes(PAYLOAD)
    with pytest.raises(CopyTimeoutError):
        xrdclient.copy(source, tmp_path / "t.bin", timeout=1e-9)
    assert not (tmp_path / "t.bin").exists()


def test_a_paced_copy_is_one_stream_of_chunks(tmp_path):
    """Limits are XrdCl's per chunk, so the bulk plane and spans are not used."""
    seen: list[int] = []
    with FakeServer(files={"/f": PAYLOAD}) as srv:
        xrdclient.copy(srv.url / "f", tmp_path / "t", chunk_size=4096, timeout=60,
                       progress=lambda done, total: seen.append(done), config=CONFIG)
    assert seen == [4096, 8192, 12288, 16384]
    assert (tmp_path / "t").read_bytes() == PAYLOAD


def _shrunk(server: FakeServer, keep: int) -> None:
    """A file that has shrunk since it was opened: reads stop at ``keep``."""

    def short(conn, sid, params, body):
        offset, length = __import__("struct").unpack(">qi", params[4:16])
        data = conn._file(conn._path(params, b"", at=slice(0, 4)))[offset:keep][:length]
        yield frame(sid, c.kXR_ok, bytes(data))

    server.handlers[c.kXR_read] = short


def test_a_dynamic_source_is_read_to_its_end_not_its_size(tmp_path):
    with FakeServer(files={"/f": PAYLOAD}) as srv:
        _shrunk(srv, 5000)
        with pytest.raises(xrdclient.errors.XRootDError, match="incomplete"):
            xrdclient.copy(srv.url / "f", tmp_path / "a", chunk_size=1024, config=CONFIG,
                           verify=False)
        seen: list[tuple[int, int | None]] = []
        result = xrdclient.copy(srv.url / "f", tmp_path / "b", chunk_size=1024, config=CONFIG,
                                dynamic_source=True, verify=False,
                                progress=lambda done, total: seen.append((done, total)))
    assert (tmp_path / "b").read_bytes() == PAYLOAD[:5000]
    assert result.size == 5000
    assert {total for _, total in seen} == {None}


def test_a_dynamic_source_stops_at_the_first_short_read(tmp_path):
    """As XrdCl's dynamic source does, rather than asking again for more."""
    reads: list[int] = []

    class Growing:
        def __init__(self) -> None:
            self.data = bytearray(b"x" * 2500)

        def read(self, size: int) -> bytes:  # pragma: no cover - readinto is used
            raise AssertionError

        def readinto(self, buffer):
            reads.append(len(buffer))
            chunk = bytes(self.data[:len(buffer)])
            del self.data[:len(buffer)]
            buffer[: len(chunk)] = chunk
            self.data += b"y" * 10  # the writer keeps appending
            return len(chunk)

    target = tmp_path / "t"
    result = xrdclient.copy(Growing(), target, chunk_size=1000, dynamic_source=True,
                            config=_config(in_flight=1))
    # 1000, then 1000, then the 520 left - short, so the copy stops there,
    # with ten more bytes already appended behind it.
    assert result.size == len(target.read_bytes()) == 2520
    assert reads == [1000, 1000, 1000]


def test_a_dynamic_resume_continues_from_the_partial_target(tmp_path):
    source = tmp_path / "s"
    source.write_bytes(PAYLOAD)
    target = tmp_path / "t"
    target.write_bytes(PAYLOAD[:1000])
    result = xrdclient.copy(source, target, resume=True, dynamic_source=True, chunk_size=4096)
    assert target.read_bytes() == PAYLOAD
    assert result.resumed_at == 1000


def _opens(server: FakeServer) -> list[int]:
    """Record the options of every ``kXR_open`` ``server`` answers."""
    options: list[int] = []

    def spy(conn, sid, params, body):
        options.append(int.from_bytes(params[2:4], "big"))
        yield from fake._h_open(conn, sid, params, body)

    server.handlers[c.kXR_open] = spy
    return options


@pytest.mark.parametrize("coerce", [False, True])
def test_coerce_opens_a_remote_target_with_force(tmp_path, coerce):
    source = tmp_path / "s"
    source.write_bytes(PAYLOAD)
    with FakeServer() as dst:
        opened = _opens(dst)
        xrdclient.copy(source, dst.url / "t", coerce=coerce, config=CONFIG, verify=False)
        xrdclient.copy(source, dst.url / "u", coerce=coerce, config=_config(parallel_chunks=2),
                       chunk_size=1024, verify=False)
        assert dst.contents("/t") == dst.contents("/u") == PAYLOAD
    forced = [bool(o & OpenFlags.FORCE) for o in opened]
    assert forced and set(forced) == {coerce}


def test_coerce_carries_into_a_resumed_upload(tmp_path):
    source = tmp_path / "s"
    source.write_bytes(PAYLOAD)
    with FakeServer(files={"/t": PAYLOAD[:100]}) as dst:
        opened = _opens(dst)
        xrdclient.copy(source, dst.url / "t", coerce=True, resume=True, config=CONFIG,
                       verify=False)
        assert dst.contents("/t") == PAYLOAD
    assert any(o & OpenFlags.FORCE for o in opened)


def test_a_forced_open_that_fails_releases_its_handle(tmp_path):
    source = tmp_path / "s"
    source.write_bytes(PAYLOAD)
    with FakeServer(files={"/t": b"x"}) as dst:
        with pytest.raises(FileExistsError):
            xrdclient.copy(source, dst.url / "t", coerce=True, overwrite=False, config=CONFIG)


# ---------------------------------------------------------------------------
# Several sources
# ---------------------------------------------------------------------------


def _locator(*answers: str) -> FakeServer:
    """A redirector that says where the file is, and holds nothing itself."""
    server = FakeServer(files={"/f": PAYLOAD})

    def answer(conn, sid, params, body):
        conn.s.locate_options = int.from_bytes(params[0:2], "big")
        yield frame(sid, c.kXR_ok, " ".join(answers).encode() + b"\x00")

    server.handlers[c.kXR_locate] = answer
    return server


def _where(server: FakeServer, kind: str = "Sr") -> str:
    host, port = server.address
    return f"{kind}{host}:{port}"


def _reads(server: FakeServer) -> int:
    return sum(op in (c.kXR_read, c.kXR_pgread, c.kXR_readv) for op in server.seen)


def test_several_replicas_each_send_part_of_the_file(tmp_path):
    with FakeServer(files={"/f": PAYLOAD}) as a, FakeServer(files={"/f": PAYLOAD}) as b:
        with _locator(_where(a), _where(b)) as red:
            seen: list[tuple[int, int | None]] = []
            result = xrdclient.copy(
                red.url / "f", tmp_path / "t", sources=2, chunk_size=1024, config=CONFIG,
                progress=lambda done, total: seen.append((done, total)),
            )
            assert red.locate_options == 0x0101  # kXR_compress | kXR_prefname
        assert (tmp_path / "t").read_bytes() == PAYLOAD
        assert _reads(a) > 0 and _reads(b) > 0
        assert _reads(red) == 0
    assert result.size == len(PAYLOAD) and result.verified
    assert seen[-1] == (len(PAYLOAD), len(PAYLOAD))


def test_a_replica_that_will_not_open_is_passed_over(tmp_path, closed_port):
    host, port = closed_port
    with FakeServer(files={"/f": PAYLOAD}) as a:
        with _locator(f"Sr{host}:{port}", _where(a)) as red:
            xrdclient.copy(red.url / "f", tmp_path / "t", sources=2, chunk_size=1024,
                           config=_config(connect_timeout=2))
    assert (tmp_path / "t").read_bytes() == PAYLOAD


def test_a_replica_that_fails_part_way_hands_its_block_on(tmp_path):
    with FakeServer(files={"/f": PAYLOAD}) as a, FakeServer(files={"/f": PAYLOAD}) as b:
        answered = []

        def flaky(conn, sid, params, body):
            answered.append(1)
            if len(answered) > 2:
                yield error(sid, 3007, "disk on fire")
                return
            time.sleep(0.02)
            yield from fake._h_read(conn, sid, params, body)

        a.handlers[c.kXR_read] = flaky
        with _locator(_where(a), _where(b)) as red:
            xrdclient.copy(red.url / "f", tmp_path / "t", sources=2, chunk_size=1024,
                           config=CONFIG)
    assert (tmp_path / "t").read_bytes() == PAYLOAD


def test_a_replica_shorter_than_the_file_is_given_up_on(tmp_path):
    with FakeServer(files={"/f": PAYLOAD}) as a, FakeServer(files={"/f": PAYLOAD}) as b:
        _shrunk(b, 1000)
        with _locator(_where(a), _where(b)) as red:
            xrdclient.copy(red.url / "f", tmp_path / "t", sources=2, chunk_size=512,
                           config=CONFIG)
    assert (tmp_path / "t").read_bytes() == PAYLOAD


def test_when_every_replica_fails_there_are_no_more_to_try(tmp_path):
    with FakeServer(files={"/f": PAYLOAD}) as a:
        a.handlers[c.kXR_read] = lambda conn, sid, params, body: iter([error(sid, 3007, "no")])
        with _locator(_where(a)) as red:
            with pytest.raises(NoMoreReplicasError, match=r"No more replicas to try: .*no"):
                xrdclient.copy(red.url / "f", tmp_path / "t", sources=3, chunk_size=1024,
                               config=CONFIG)


def test_when_no_replica_opens_there_are_none_to_try(tmp_path, closed_port):
    host, port = closed_port
    with _locator(f"Sr{host}:{port}") as red:
        with pytest.raises(NoMoreReplicasError):
            xrdclient.copy(red.url / "f", tmp_path / "t", sources=2,
                           config=_config(connect_timeout=2))
    with _locator() as red, pytest.raises(NoMoreReplicasError, match=r"try$"):
        xrdclient.copy(red.url / "f", tmp_path / "u", sources=2, config=CONFIG)


def test_managers_are_followed_down_to_their_servers(tmp_path, closed_port):
    host, port = closed_port
    with FakeServer(files={"/f": PAYLOAD}) as a, FakeServer(files={"/f": PAYLOAD}) as b:
        with _locator(_where(b), _where(a)) as lower:
            with _locator(_where(lower, "Mr"), f"Mr{host}:{port}", _where(lower, "Mr")) as top:
                found = replicas.locate(top.url / "f", _config(connect_timeout=2))
    assert sorted(u.port for u in found) == sorted([a.address[1], b.address[1]])
    assert all(u.path == "/f" for u in found)


def test_a_file_the_redirector_cannot_find_fails_before_the_target_is_touched(tmp_path):
    with FakeServer() as red:
        with pytest.raises(xrdclient.errors.NotFoundError):
            xrdclient.copy(red.url / "f", tmp_path / "t", sources=2, config=CONFIG)
    assert not (tmp_path / "t").exists()


def test_a_write_failure_is_the_copys_not_the_replicas(tmp_path):
    """A reader thread that cannot write stops the others, and its error is raised."""
    with FakeServer(files={"/f": PAYLOAD}) as a, FakeServer(files={"/f": PAYLOAD}) as b:
        with _locator(_where(a), _where(b)) as red:
            calls = []

            def refuse(done, total):
                calls.append(threading.current_thread() is threading.main_thread())
                if not calls[-1]:
                    raise OSError(28, "No space left on device")
                time.sleep(0.05)

            with pytest.raises(OSError, match="No space"):
                xrdclient.copy(red.url / "f", tmp_path / "t", sources=2, chunk_size=1024,
                               config=CONFIG, progress=refuse)


def test_a_limit_ends_a_copy_from_several_sources(tmp_path):
    with FakeServer(files={"/f": PAYLOAD}) as a, FakeServer(files={"/f": PAYLOAD}) as b:
        _slow_reads(a, 0.2)
        _slow_reads(b, 0.2)
        with _locator(_where(a), _where(b)) as red:
            with pytest.raises(CopyTimeoutError):
                xrdclient.copy(red.url / "f", tmp_path / "t", sources=2, chunk_size=1024,
                               config=CONFIG, timeout=0.3)


def test_an_empty_file_from_several_sources(tmp_path):
    with FakeServer(files={"/f": b""}) as a:
        with _locator(_where(a)) as red:
            result = xrdclient.copy(red.url / "f", tmp_path / "t", sources=2, config=CONFIG,
                                    verify=False)
    assert result.size == 0 and (tmp_path / "t").read_bytes() == b""


def test_several_sources_into_a_remote_target(tmp_path):
    with FakeServer(files={"/f": PAYLOAD}) as a, FakeServer() as dst:
        with _locator(_where(a)) as red:
            xrdclient.copy(red.url / "f", dst.url / "t", sources=2, chunk_size=4096,
                           config=CONFIG, verify=False)
        assert dst.contents("/t") == PAYLOAD


def test_several_sources_cannot_continue_a_partial_copy(tmp_path):
    with _locator() as red, pytest.raises(NotImplementedError):
        xrdclient.copy(red.url / "f", tmp_path / "t", sources=2, resume=True, config=CONFIG)


def test_several_sources_need_a_root_source_and_a_target_at_offsets(tmp_path, server):
    """Anything else is copied from the one source, as before."""
    source = tmp_path / "s"
    source.write_bytes(PAYLOAD)
    xrdclient.copy(source, tmp_path / "t", sources=4)
    assert (tmp_path / "t").read_bytes() == PAYLOAD
    import io

    sink = io.BytesIO()
    xrdclient.copy(server.url / "data/a.root", sink, sources=4, config=CONFIG)
    assert sink.getvalue() == b"hello world"


def test_several_sources_to_an_http_target_read_one(server):
    from xrdclient.testing import FakeDAVServer

    with FakeDAVServer() as dav:
        xrdclient.copy(server.url / "data/a.root", dav.url / "x", sources=2, config=CONFIG,
                       verify=False)
        assert dav.contents("/x") == b"hello world"


def test_blocks_come_back_to_whoever_asks_next():
    blocks = replicas._Blocks(10, 4)
    assert blocks.take() == (0, 4)
    got: list[Any] = []

    def waiter_takes_twice() -> None:
        got.append(blocks.take())
        got.append(blocks.take())

    waiter = threading.Thread(target=waiter_takes_twice)
    assert blocks.take() == (4, 4)
    assert blocks.take() == (8, 2)
    waiter.start()
    time.sleep(0.05)
    assert got == []  # nothing pending, but three blocks are out
    blocks.done(0, 4, 1)  # three bytes of it are still to read
    blocks.done(4, 4, 4)
    blocks.done(8, 2, 2)
    time.sleep(0.05)
    assert got == [(1, 3)]  # and the waiter waits on itself to finish it
    blocks.done(1, 3, 3)
    waiter.join(5)
    assert got == [(1, 3), None]
    blocks.stop()
    assert blocks.take() is None


# ---------------------------------------------------------------------------
# Third-party copy
# ---------------------------------------------------------------------------


def test_third_party_coerce_forces_the_destination_open(server):
    with FakeServer() as dst:
        opened = _opens(dst)
        xrdclient.third_party(server.url / "data/a.root", dst.url / "p", coerce=True, verify=False)
    assert any(o & OpenFlags.FORCE for o in opened)


@pytest.mark.parametrize(
    ("end", "opcode", "when"),
    [
        ("src", c.kXR_open, "tpc.stage=placement"),  # the source's placement
        ("dst", c.kXR_open, "tpc.key"),  # the destination's open
        ("dst", c.kXR_sync, ""),  # arming the pull
        ("src", c.kXR_open, "tpc.key"),  # the source's keyed open
    ],
)
def test_third_party_init_timeout_bounds_each_step_of_the_set_up(server, end, opcode, when):
    with FakeServer() as dst:
        slowed = server if end == "src" else dst
        answer = {c.kXR_open: fake._h_open, c.kXR_sync: fake._h_sync}[opcode]
        delays = []

        def late(conn, sid, params, body):
            if when in body.decode(errors="replace") and not delays:
                delays.append(1)
                time.sleep(0.4)
            yield from answer(conn, sid, params, body)

        slowed.handlers[opcode] = late
        with pytest.raises(CopyTimeoutError, match="init_timeout"):
            xrdclient.third_party(
                server.url / "data/a.root", dst.url / "p", init_timeout=0.2, verify=False,
            )
    assert delays == [1]


def test_third_party_without_an_init_timeout_waits(server):
    with FakeServer() as dst:
        answer = fake._h_open

        def late(conn, sid, params, body):
            time.sleep(0.3)
            yield from answer(conn, sid, params, body)

        dst.handlers[c.kXR_open] = late
        result = xrdclient.third_party(server.url / "data/a.root", dst.url / "p", verify=False)
    assert result.size == 11


# ---------------------------------------------------------------------------
# xrd-cp
# ---------------------------------------------------------------------------


def test_the_xrdcp_flags_become_copy_keywords(monkeypatch, tmp_path):
    source = tmp_path / "s"
    source.write_bytes(PAYLOAD)
    calls: list[dict[str, Any]] = []
    real = cp.copy

    def spy(src, dst, **kwargs):
        calls.append(kwargs)
        return real(src, dst, **kwargs)

    monkeypatch.setattr(cp, "copy", spy)
    argv = [str(source), str(tmp_path / "t"), "-q", "--sources", "2", "--xrate", "10M",
            "--xrate-threshold", "10k", "--cptimeout", "30", "-Z", "-F"]
    assert cp.main(argv) == 0
    (options,) = calls
    assert {k: options[k] for k in ("sources", "max_rate", "min_rate", "timeout",
                                    "dynamic_source", "coerce")} == {
        "sources": 2, "max_rate": 10 << 20, "min_rate": 10 << 10, "timeout": 30.0,
        "dynamic_source": True, "coerce": True,
    }
    assert (tmp_path / "t").read_bytes() == PAYLOAD


@pytest.mark.parametrize(
    ("argv", "complaint"),
    [
        (["--sources", "0"], "--sources is how many"),
        (["--sources", "33"], "--sources is how many"),
        (["--xrate", "1k"], "at least 10k"),
        (["--xrate-threshold", "100"], "at least 10k"),
        (["--cptimeout", "0"], "--cptimeout is how long"),
        (["-c", "--sources", "2"], "--continue carries one partial"),
        (["--tpc", "--xrate", "1M"], "--tpc moves no data through this process, so --xrate"),
        (["--tpc", "-Z"], "--dynamic-src"),
    ],
)
def test_the_xrdcp_flags_are_held_to_xrdcps_ranges(argv, complaint, capsys, tmp_path):
    assert cp.main([*argv, "root://h//a", str(tmp_path / "b")]) == 2
    assert complaint in capsys.readouterr().err


def test_xrd_cp_tpc_passes_coerce_on(monkeypatch):
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cp, "third_party", lambda s, t, **kw: seen.update(kw) or
                        CopyResult(str(s), str(t), 0, 0.0))
    assert cp.main(["--tpc", "-F", "-q", "root://a//x", "root://b//y"]) == 0
    assert seen["coerce"] is True


# ---------------------------------------------------------------------------
# The compat CopyProcess, on fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def compat_config(monkeypatch):
    monkeypatch.setattr(env, "config", lambda: CONFIG)
    yield
    from xrdclient.compat.client import _channels

    _channels.close_all()


class Recorder(client.utils.CopyProgressHandler):
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    def begin(self, jobId, total, source, target):
        self.events.append(("begin", jobId, total))

    def update(self, jobId, processed, total):
        self.events.append(("update", jobId, processed, total))

    def end(self, jobId, results):
        self.events.append(("end", jobId, _plain(results)))


def _plain(results: dict[str, Any]) -> dict[str, Any]:
    status = results["status"]
    return {**results, "status": (status.code, status.errno, status.shellcode, status.message)}


def _run(*jobs: tuple[str, str, dict[str, Any]]) -> tuple[Any, list[Any], list[Any]]:
    process = client.CopyProcess()
    for source, target, keywords in jobs:
        process.add_job(source, target, **keywords)
    assert process.prepare().ok
    handler = Recorder()
    status, results = process.run(handler)
    return status, [_plain(r) for r in results], handler.events


def test_add_job_takes_xrdcl_defaults_from_the_environment(compat_config, monkeypatch):
    settings = {"CPInitTimeout": 600, "XRateThreshold": 1234}
    monkeypatch.setattr(env, "EnvGetInt", lambda key: settings.get(key, 0))
    process = client.CopyProcess()
    process.add_job("root://h//a", "/tmp/b")
    process.add_job("root://h//a", "/tmp/b", xrateThreshold=0, inittimeout=5, cptimeout=7)
    first, second = process._CopyProcess__jobs
    assert (first.inittimeout, first.cptimeout, first.xrateThreshold) == (600, 0, 1234)
    assert (second.inittimeout, second.cptimeout, second.xrateThreshold) == (5, 7, 1234)


def test_the_compat_threshold_status_is_xrdcls(compat_config, tmp_path):
    with FakeServer(files={"/f": PAYLOAD}) as srv:
        _slow_reads(srv, 0.02)
        root = f"root://{srv.url.netloc}/"
        status, results, events = _run(
            (root + "/f", str(tmp_path / "t"),
             {"chunksize": 1024, "parallelchunks": 1, "xrateThreshold": 1 << 30, "retry": 1}),
        )
    assert results == [{"status": (208, 0, 52, "[ERROR] Threshold exceeded: The transfer "
                                   "rate dropped below requested threshold!")}]
    assert status.code == 208
    # Two attempts, each reporting its first chunk and failing on the second.
    assert [e[0] for e in events] == ["begin", "update", "update", "end"]


def test_the_compat_cptimeout_status_is_xrdcls(compat_config, tmp_path):
    with FakeServer(files={"/f": PAYLOAD * 64}) as srv:
        root = f"root://{srv.url.netloc}/"
        _, results, events = _run(
            (root + "/f", str(tmp_path / "t"), {"chunksize": 256 << 10, "cptimeout": 1,
                                                    "xrate": 256 << 10}),
        )
    assert results == [{"status": (206, 0, 52, "[ERROR] Operation expired: CPTimeout exceeded.")}]
    assert [e[2] for e in events if e[0] == "update"] == [256 << 10, 512 << 10]


def test_the_compat_no_more_replicas_status_is_xrdcls(compat_config, tmp_path, closed_port):
    host, port = closed_port
    with _locator(f"Sr{host}:{port}") as red:
        root = f"root://{red.url.netloc}/"
        _, results, _ = _run((root + "/f", str(tmp_path / "t"), {"sourcelimit": 2}))
    assert results == [{"status": (16, 0, 50, "[ERROR] No more replicas to try:  (source)")}]


def test_the_compat_sources_dynamic_and_coerce(compat_config, tmp_path):
    with FakeServer(files={"/f": PAYLOAD}) as a, FakeServer(files={"/f": PAYLOAD}) as b:
        _shrunk(a, 1000)
        with _locator(_where(b)) as red, FakeServer() as dst:
            opened = _opens(dst)
            rroot, aroot = f"root://{red.url.netloc}/", f"root://{a.url.netloc}/"
            _, results, events = _run(
                (rroot + "/f", str(tmp_path / "multi"), {"sourcelimit": 2, "chunksize": 4096}),
                (aroot + "/f", str(tmp_path / "dyn"), {"dynamicsource": True}),
                (str(tmp_path / "multi"), f"root://{dst.url.netloc}//c", {"coerce": True}),
                (rroot + "/f", str(tmp_path / "multi"), {"sourcelimit": 2, "cont": True}),
            )
    assert [r["status"][0] for r in results] == [0, 0, 0, _status.errNotImplemented]
    assert results[0]["size"] == len(PAYLOAD) and results[1]["size"] == 1000
    assert (tmp_path / "multi").read_bytes() == PAYLOAD
    assert (tmp_path / "dyn").read_bytes() == PAYLOAD[:1000]
    assert [e for e in events if e[0] == "update" and e[1] == 2] == [("update", 2, 1000, 0)]
    assert any(o & OpenFlags.FORCE for o in opened)


def test_the_compat_tpc_takes_inittimeout_and_coerce(compat_config, server):
    with FakeServer() as dst:
        opened = _opens(dst)
        original = fake._h_sync

        def late(conn, sid, params, body):
            time.sleep(1.2)
            yield from original(conn, sid, params, body)

        src, dstroot = f"root://{server.url.netloc}/", f"root://{dst.url.netloc}/"
        _, results, _ = _run((src + "/data/a.root", dstroot + "/p",
                              {"thirdparty": "only", "coerce": True}))
        dst.handlers[c.kXR_sync] = late
        _, late_results, _ = _run((src + "/data/a.root", dstroot + "/q",
                                   {"thirdparty": "only", "inittimeout": 1}))
    assert results[0]["status"][0] == 0
    assert any(o & OpenFlags.FORCE for o in opened)
    assert late_results[0]["status"][0] == _status.errOperationExpired


# ---------------------------------------------------------------------------
# Beside the official bindings, on real daemons
# ---------------------------------------------------------------------------


@pytest.fixture
def theirs():
    return pytest.importorskip("XRootD.client", reason="the official bindings are not installed")


@pytest.fixture
def real(real_server, sandbox, monkeypatch):
    from conftest import _REAL_CONFIG

    monkeypatch.setattr(env, "config", lambda: _REAL_CONFIG)
    yield real_server, sandbox
    from xrdclient.compat.client import _channels

    _channels.close_all()


def _both(theirs, jobs_for) -> list[tuple[Any, list[Any], list[Any]]]:
    """Run the jobs ``jobs_for(side)`` gives through each library; the outcomes."""
    outcomes = []
    for side, module in (("theirs", theirs), ("ours", client)):
        process = module.CopyProcess()
        for source, target, keywords in jobs_for(side):
            process.add_job(source, target, **keywords)
        assert process.prepare().ok
        handler = _Events()
        status, results = process.run(handler)
        outcomes.append((_plain_status(status), [_plain(r) for r in results], handler.events))
    return outcomes


class _Events:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    def begin(self, jobId, total, source, target):
        self.events.append(("begin", jobId, total))

    def update(self, jobId, processed, total):
        self.events.append(("update", jobId, processed, total))

    def end(self, jobId, results):
        self.events.append(("end", jobId, results["status"].code))

    def should_cancel(self, jobId):
        return False


def _plain_status(status: Any) -> tuple[int, int, int]:
    return status.code, status.errno, status.shellcode


def _big(real_server, sandbox, name: str, size: int) -> tuple[str, bytes]:
    data = os.urandom(size)
    with open(f"{sandbox}/{name}", "wb") as handle:
        handle.write(data)
    return f"{real_server.url.rstrip('/')}//{sandbox.lstrip('/')}/{name}", data


@pytest.mark.interop
@pytest.mark.parity
def test_threshold_and_cptimeout_end_as_with_the_bindings(theirs, real, tmp_path):
    """The bindings pass ``add_job``'s last keywords to XrdCl in the wrong order:
    their ``retry`` is XrdCl's ``xrateThreshold``, their ``xrateThreshold`` its
    ``xrate`` and their ``xrate`` its ``retry``. Each side is given what makes
    XrdCl do the same thing."""
    server, sandbox = real
    url, _ = _big(server, sandbox, "big", 8 << 20)

    def jobs(side):
        swapped = side == "theirs"
        threshold = {"retry": 1 << 40} if swapped else {"xrateThreshold": 1 << 40}
        slow = {"xrateThreshold": 1 << 20} if swapped else {"xrate": 1 << 20}
        return [
            (url, str(tmp_path / f"{side}-a"), dict(chunksize=1 << 20, **threshold)),
            (url, str(tmp_path / f"{side}-b"), dict(chunksize=1 << 20, cptimeout=1, **slow)),
        ]

    mine_theirs, mine_ours = _both(theirs, jobs)
    assert mine_theirs == mine_ours
    _, results, events = mine_ours
    assert [r["status"][0] for r in results] == [208, 206]
    assert [e[2] for e in events if e[0] == "update" and e[1] == 1] == [1 << 20, 2 << 20,
                                                                        3 << 20, 4 << 20]


@pytest.mark.interop
@pytest.mark.parity
def test_xrate_caps_both_libraries_alike(theirs, real, tmp_path):
    server, sandbox = real
    url, data = _big(server, sandbox, "capped", 3 << 20)
    timings = []

    def jobs(side):
        cap = {"xrateThreshold": 1 << 20} if side == "theirs" else {"xrate": 1 << 20}
        return [(url, str(tmp_path / side), dict(chunksize=512 << 10, **cap))]

    for side, module in (("theirs", theirs), ("ours", client)):
        process = module.CopyProcess()
        for source, target, keywords in jobs(side):
            process.add_job(source, target, **keywords)
        process.prepare()
        started = time.monotonic()
        status, _ = process.run()
        timings.append(time.monotonic() - started)
        assert status.ok and (tmp_path / side).read_bytes() == data
    # 3 MiB at 1 MiB/s is three seconds, less the first chunk's head start.
    assert all(2.3 <= t < 6 for t in timings), timings
    assert abs(timings[0] - timings[1]) < 1.0, timings


@pytest.mark.interop
@pytest.mark.parity
def test_xrd_cp_xrate_keeps_pace_with_xrdcp(real, tmp_path):
    import shutil
    import subprocess

    if shutil.which("xrdcp") is None:
        pytest.skip("no xrdcp on PATH")
    server, sandbox = real
    url, data = _big(server, sandbox, "cli", 2 << 20)
    started = time.monotonic()
    subprocess.run(["xrdcp", "-s", "-f", "--xrate", "1M", url, str(tmp_path / "x")], check=True,
                   timeout=60)
    theirs_took = time.monotonic() - started
    started = time.monotonic()
    assert cp.main(["-q", "-f", "--xrate", "1M", "--chunk-size", "512k", url,
                    str(tmp_path / "y")]) == 0
    ours_took = time.monotonic() - started
    assert (tmp_path / "x").read_bytes() == (tmp_path / "y").read_bytes() == data
    assert 1.0 <= theirs_took < 5 and 1.0 <= ours_took < 5
    assert abs(theirs_took - ours_took) < 1.0


@pytest.mark.interop
@pytest.mark.parity
def _held(theirs, sandbox: str, base: str, side: str) -> Any:
    """A file of the old contents, held open for reading by another client."""
    with open(f"{sandbox}/{side}-held", "wb") as handle:
        handle.write(b"old")
    held = theirs.File()
    assert held.open(f"{base}/{side}-held")[0].ok
    return held


def test_coerce_ignores_the_file_lock_as_with_the_bindings(theirs, real, tmp_path):
    """A file another client has open for reading cannot be replaced - unless coerced."""
    server, sandbox = real
    source = tmp_path / "src"
    source.write_bytes(PAYLOAD)
    base = f"{server.url.rstrip('/')}//{sandbox.lstrip('/')}"
    holders = [_held(theirs, sandbox, base, side) for side in ("theirs", "ours")]

    def jobs(side):
        return [
            (str(source), f"{base}/{side}-held", {"force": True}),
            (str(source), f"{base}/{side}-held", {"force": True, "coerce": True}),
        ]

    try:
        mine_theirs, mine_ours = _both(theirs, jobs)
    finally:
        _ = [held.close() for held in holders]
    # The messages name each side's own file; the rest must agree.
    assert [r["status"][:3] for r in mine_theirs[1]] == [r["status"][:3] for r in mine_ours[1]]
    assert mine_theirs[2] == mine_ours[2]
    assert [r["status"][:2] for r in mine_ours[1]] == [(400, 3003), (0, 0)]
    with open(f"{sandbox}/ours-held", "rb") as handle:
        assert handle.read() == PAYLOAD


@pytest.mark.interop
@pytest.mark.parity
def test_a_dynamic_source_ends_as_with_the_bindings(theirs, real, tmp_path):
    """A file that grows while it is copied: read until a read comes back short."""
    server, sandbox = real
    outcomes, copies = [], []
    for side, module in (("theirs", theirs), ("ours", client)):
        path = f"{sandbox}/{side}-growing"
        with open(path, "wb") as handle:
            handle.write(b"a" * (1 << 20))
        url = f"{server.url.rstrip('/')}//{path.lstrip('/')}"
        stop = threading.Event()

        def grow(path=path, stop=stop):
            with open(path, "ab") as handle:
                for _ in range(3):
                    if stop.wait(0.3):
                        return
                    handle.write(b"b" * (1 << 20))
                    handle.flush()

        writer = threading.Thread(target=grow)
        process = module.CopyProcess()
        process.add_job(url, str(tmp_path / side), dynamicsource=True, chunksize=512 << 10,
                        **({"xrateThreshold": 1 << 20} if side == "theirs"
                           else {"xrate": 1 << 20}))
        process.prepare()
        writer.start()
        handler = _Events()
        status, results = process.run(handler)
        stop.set()
        writer.join()
        outcomes.append((_plain_status(status), {e[3] for e in handler.events
                                                 if e[0] == "update"}))
        copies.append((results[0]["size"], (tmp_path / side).stat().st_size))
    assert outcomes[0] == outcomes[1] == ((0, 0, 0), {0})
    # Each read all the file there was: more than when it started.
    assert all(size == on_disk > 1 << 20 for size, on_disk in copies), copies


class _Rooted:
    """A real ``xrootd`` exporting ``/`` from ``root``: the same path on every replica."""

    def __init__(self, root: str) -> None:
        import _xrootd

        self.server = _xrootd.RealServer(root)

    def __enter__(self) -> Any:
        import subprocess

        import _xrootd

        srv = self.server
        srv._config.write_text(
            f"xrd.port {srv.port}\nall.export /\noss.localroot {srv.root}\n"
            f"all.adminpath {srv._admin}\nall.pidpath {srv._admin}\n"
            "xrootd.chksum max 2 adler32\n"
        )
        with srv._log.open("wb") as handle:
            srv._proc = subprocess.Popen(
                [str(_xrootd.XROOTD), "-c", str(srv._config), "-n", "test"],
                cwd=str(srv._admin), stdout=handle, stderr=subprocess.STDOUT,
            )
        srv._wait()
        return srv

    def __exit__(self, *exc: object) -> None:
        self.server.stop()


@pytest.mark.interop
@pytest.mark.parity
def test_several_real_replicas_as_with_the_bindings(theirs, real_server, tmp_path, monkeypatch):
    """Two daemons with the same file behind a redirector that answers ``kXR_locate``."""
    from conftest import _REAL_CONFIG

    monkeypatch.setattr(env, "config", lambda: _REAL_CONFIG)
    data = os.urandom(6 << 20)
    with _Rooted(str(tmp_path / "r1")) as one, _Rooted(str(tmp_path / "r2")) as two:
        for srv in (one, two):
            (srv.root / "f").write_bytes(data)
        with _locator(f"Sr127.0.0.1:{one.port}", f"Sr127.0.0.1:{two.port}") as red:
            url = f"root://{red.url.netloc}//f"
            (tmp_path / "theirs-c").write_bytes(data[:1000])  # a partial copy
            (tmp_path / "ours-c").write_bytes(data[:1000])
            outcome = _both(theirs, lambda side: [
                (url, str(tmp_path / side), {"sourcelimit": 2, "chunksize": 1 << 20}),
                (url, str(tmp_path / f"{side}-c"), {"sourcelimit": 2, "cont": True}),
            ])
    theirs_, ours = outcome
    assert (tmp_path / "theirs").read_bytes() == (tmp_path / "ours").read_bytes() == data
    assert theirs_[1][0] == ours[1][0] == {"size": len(data), "status": (0, 0, 0, "[SUCCESS] ")}
    assert theirs_[1][1]["status"][0] == ours[1][1]["status"][0] == _status.errNotImplemented
    assert _last_update(outcome[0]) == _last_update(ours) == (len(data), len(data))


def _last_update(outcome: tuple[Any, list[Any], list[Any]]) -> tuple[int, int]:
    """The first job's last ``(processed, total)``."""
    return [e[2:] for e in outcome[2] if e[0] == "update" and e[1] == 1][-1]


@pytest.mark.interop
@pytest.mark.parity
def test_several_fake_replicas_share_the_reading_as_with_the_bindings(theirs, tmp_path,
                                                                      monkeypatch):
    """Both libraries spread the reads over both replicas."""
    from conftest import _REAL_CONFIG

    monkeypatch.setattr(env, "config", lambda: _REAL_CONFIG)
    data = os.urandom(4 << 20)
    shares = []
    for side in ("theirs", "ours"):
        with FakeServer(files={"/f": data}) as a, FakeServer(files={"/f": data}) as b:
            # Slow enough that neither server can finish the file before
            # the other has opened it.
            _slow_reads(a, 0.02)
            _slow_reads(b, 0.02)
            with _locator(_where(a), _where(b)) as red:
                url = f"root://{red.url.netloc}//f"
                module = theirs if side == "theirs" else client
                process = module.CopyProcess()
                process.add_job(url, str(tmp_path / side), sourcelimit=2, chunksize=256 << 10)
                process.prepare()
                status, results = process.run()
                assert status.ok and results[0]["size"] == len(data)
                shares.append((_reads(a) > 0, _reads(b) > 0))
        assert (tmp_path / side).read_bytes() == data
    assert shares == [(True, True), (True, True)]

