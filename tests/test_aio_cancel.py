"""Cancelling a coroutine abandons the request its worker is waiting on.

:mod:`xrdclient.aio` runs each call on a worker thread inside a
:class:`~xrdcore.cancel.CancelScope`, and cancels the scope when the
awaiting coroutine is cancelled. These tests hold a request on the in-tree
server - a handler that takes the request and does not answer - cancel the
coroutine waiting for it, and check what the design promises: the coroutine
raises :class:`asyncio.CancelledError` at once, the worker thread is back
within a fraction of a second (a pool of *one* worker serves the next call),
the connection carries on serving requests, and the answer that turns up
late for the abandoned stream is dropped rather than handed to anyone.

They synchronise on what the server has been asked, never on sleeps: a test
waits for the request to arrive before it cancels it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import struct
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from typing import Any, TypeVar

import pytest
from xrdcore import cancel as _cancel
from xrdcore.fanout import Pool

import xrdclient
from tests.conftest import frame
from xrdclient.config import Config
from xrdclient.errors import TimeoutError as XrdTimeoutError
from xrdclient.proto import constants as c
from xrdclient.session import bulk as bulk_module
from xrdclient.testing import FakeDAVServer, FakeServer
from xrdclient.testing.server import _Connection

T = TypeVar("T")

#: Long enough that a pass never comes near it; a hang fails rather than sticks.
PATIENCE = 5.0

#: How soon a cancelled worker must be free again, by the brief's measure.
PROMPT = 0.5

#: Enough to fill the bulk pipeline several times over at the chunk below.
DATA = bytes(range(256)) * 8192  # 2 MiB

#: The smallest chunk the bulk plane takes, so a short file is many frames.
CHUNK = 1 << 18


def run(coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


async def until(predicate: Callable[[], object], timeout: float = PATIENCE) -> None:
    """Wait, without blocking the loop, for ``predicate`` to hold."""
    give_up = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < give_up, "timed out waiting for the condition"
        await asyncio.sleep(0.005)


@pytest.fixture
def aio(monkeypatch):
    """The facade with a pool of its own, of one worker unless a test says more.

    One worker is the sharpest test of "the worker is freed": every call
    after a cancellation needs the very thread the cancelled call was on.
    """
    import xrdclient.aio as module

    monkeypatch.setattr(module, "_pool", Pool("xrd-aio", module._workers))
    with xrdclient.config.override(aio_workers=1):
        module._pool.executor()
    yield module
    module._pool.shutdown()


class _Held:
    """A server handler that takes each request and answers none of them.

    Keeps the connection and stream id of each, so a test can answer one
    late - from its own thread, while the server's is idle waiting for the
    next request, so the two never write the socket at once.
    """

    def __init__(self) -> None:
        self.asked: list[tuple[_Connection, int]] = []

    def __call__(self, conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
        self.asked.append((conn, sid))
        return iter(())

    def answer(self, index: int, status: int = c.kXR_ok, body: bytes = b"") -> None:
        conn, sid = self.asked[index]
        conn.sock.sendall(frame(sid, status, body))


def _asked_more_than(holder: _Held, count: int) -> bool:
    """Whether more than ``count`` requests have reached ``holder``."""
    return len(holder.asked) > count


@pytest.fixture
def held():
    """A server whose pings are held, and a file to stat between them."""
    holder = _Held()
    with FakeServer(files={"/f": b"x" * 7}) as srv:
        srv.handlers[c.kXR_ping] = holder
        yield srv, holder


def _logins(srv: FakeServer) -> int:
    return srv.seen.count(c.kXR_login)


async def _cancelled(task: asyncio.Task[object]) -> float:
    """Cancel ``task``; how long its ``await`` took to raise."""
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return time.monotonic() - started


# ---------------------------------------------------------------------------
# One request, abandoned
# ---------------------------------------------------------------------------


def test_a_cancelled_request_frees_its_worker_and_keeps_the_connection(aio, held):
    srv, holder = held

    async def main() -> None:
        async with aio.FileSystem(srv.url) as fs:
            assert (await fs.stat("/f")).size == 7
            logins = _logins(srv)
            for round_ in range(6):
                task = asyncio.ensure_future(fs.ping())
                # With one worker, this ping reaching the server at all means
                # the last cancelled one gave its thread back.
                await until(functools.partial(_asked_more_than, holder, round_), PROMPT)
                assert await _cancelled(task) < PROMPT
            async with asyncio.timeout(PROMPT):
                assert (await fs.stat("/f")).size == 7
            session = fs.sync._router.session
            assert not session.broken
            assert _logins(srv) == logins  # the same connection throughout

    run(main())


def test_the_late_answer_to_an_abandoned_request_is_dropped(aio, held, caplog):
    srv, holder = held

    async def main() -> None:
        async with aio.FileSystem(srv.url) as fs:
            await fs.stat("/f")
            session = fs.sync._router.session
            task = asyncio.ensure_future(fs.ping())
            await until(lambda: holder.asked)
            await _cancelled(task)
            # Its stream id is held until the answer is off the wire...
            await until(lambda: not session.machine.idle() and session._inbox == {})
            abandoned = holder.asked[0][1]
            holder.answer(0)
            with caplog.at_level(logging.DEBUG, logger="xrdclient.proto.machine"):
                # ...which the next request's reader takes off it, and drops.
                assert (await fs.stat("/f")).size == 7
            assert f"late kXR_ok on abandoned stream {abandoned} dropped" in caplog.text
            assert session.machine.idle()
            assert not session.broken

    run(main())


def test_the_reader_role_passes_on_when_its_holder_is_cancelled(aio, held, monkeypatch):
    srv, holder = held
    monkeypatch.setattr(aio, "_pool", Pool("xrd-aio", aio._workers))

    async def main() -> None:
        with xrdclient.config.override(aio_workers=2):
            async with aio.FileSystem(srv.url) as fs:
                await fs.stat("/f")
                first = asyncio.ensure_future(fs.ping())
                await until(lambda: len(holder.asked) == 1)
                second = asyncio.ensure_future(fs.ping())
                await until(lambda: len(holder.asked) == 2)
                await _cancelled(first)
                holder.answer(1)
                async with asyncio.timeout(PATIENCE):
                    await second
                holder.answer(0)  # late, and dropped
                assert (await fs.stat("/f")).size == 7

    run(main())
    aio._pool.shutdown()


def test_a_cancelled_follower_leaves_the_reader_be(aio, held, monkeypatch):
    srv, holder = held
    monkeypatch.setattr(aio, "_pool", Pool("xrd-aio", aio._workers))

    async def main() -> None:
        with xrdclient.config.override(aio_workers=2):
            async with aio.FileSystem(srv.url) as fs:
                await fs.stat("/f")
                first = asyncio.ensure_future(fs.ping())
                await until(lambda: len(holder.asked) == 1)
                second = asyncio.ensure_future(fs.ping())
                await until(lambda: len(holder.asked) == 2)
                await _cancelled(second)
                holder.answer(0)
                async with asyncio.timeout(PATIENCE):
                    await first
                holder.answer(1)
                assert (await fs.stat("/f")).size == 7

    run(main())
    aio._pool.shutdown()


def test_an_asyncio_timeout_abandons_the_request_too(aio, held):
    srv, _holder = held

    async def main() -> None:
        async with aio.FileSystem(srv.url) as fs:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.2):
                    await fs.ping()
            async with asyncio.timeout(PROMPT):
                assert (await fs.stat("/f")).size == 7

    run(main())


def test_a_request_parked_by_kxr_wait_is_released_not_abandoned(aio, held):
    srv, holder = held

    def busy(conn, sid, params, body):
        holder.asked.append((conn, sid))
        yield frame(sid, c.kXR_wait, struct.pack(">i", 30) + b"busy\x00")

    srv.handlers[c.kXR_ping] = busy

    async def main() -> None:
        async with aio.FileSystem(srv.url) as fs:
            await fs.stat("/f")
            session = fs.sync._router.session
            task = asyncio.ensure_future(fs.ping())
            await until(lambda: holder.asked and session._parked)
            assert await _cancelled(task) < PROMPT
            # kXR_wait was the server's last word: nothing late is coming, so
            # the id goes straight back rather than waiting for an answer.
            await until(session.machine.idle, PROMPT)
            assert (await fs.stat("/f")).size == 7

    run(main())


def test_nothing_is_sent_for_a_call_cancelled_before_it_starts(held):
    srv, holder = held
    scope = _cancel.CancelScope()
    scope.cancel()
    fs = xrdclient.FileSystem(srv.url)
    try:
        with pytest.raises(_cancel.Cancelled, match="connecting"):
            _cancel.within(scope, lambda: fs.ping())
        fs.stat("/f")  # connected now, outside the scope
        with pytest.raises(_cancel.Cancelled, match="Ping was cancelled"):
            _cancel.within(scope, fs.ping)
    finally:
        fs.close()
    assert holder.asked == []


def test_a_request_waiting_its_turn_behind_a_bulk_transfer_is_cancelled(aio, monkeypatch):
    gate = _ReadGate(stall_at=2 * CHUNK)
    monkeypatch.setattr(aio, "_pool", Pool("xrd-aio", aio._workers))
    with FakeServer(files={"/f": DATA}) as srv, gate:
        srv.handlers[c.kXR_read] = gate

        async def main() -> None:
            with xrdclient.config.override(aio_workers=3):
                async with aio.FileSystem(srv.url, _bulk_config()) as fs:
                    fh = await fs.open("/f", "rb", buffering=0)
                    reading = asyncio.ensure_future(fh.pread(len(DATA), 0))
                    await until(gate.stalled.is_set)
                    waiting = asyncio.ensure_future(fs.stat("/f"))
                    session = fs.sync._router.session
                    await until(lambda: session._parked)
                    assert await _cancelled(waiting) < PROMPT
                    gate.release.set()
                    async with asyncio.timeout(PATIENCE):
                        assert await reading == DATA
                    await fh.close()

        run(main())
    aio._pool.shutdown()


# ---------------------------------------------------------------------------
# Bulk transfers: the reader owns the wire
# ---------------------------------------------------------------------------


class _ReadGate:
    """A ``kXR_read`` handler that serves :data:`DATA`, then stops at an offset.

    ``torn`` sends only the header and half the body of the read at
    ``stall_at`` before stopping; ``resume`` then lets the rest of that one
    frame go, while ``release`` lets the server carry on altogether. The
    server's thread is the one held, which is fine: a bulk transfer has the
    connection to itself.
    """

    def __init__(self, stall_at: int, *, torn: bool = False) -> None:
        self.stall_at = stall_at
        self.torn = torn
        self.stalled = threading.Event()
        self.resume = threading.Event()
        self.release = threading.Event()

    def __call__(self, conn, sid, params, body):
        offset, length = struct.unpack(">qi", params[4:16])
        data = DATA[offset : offset + length]
        if offset != self.stall_at or self.release.is_set():
            yield frame(sid, c.kXR_ok, data)
            return
        whole = frame(sid, c.kXR_ok, data)
        if self.torn:
            half = 8 + len(data) // 2
            yield whole[:half]
            self.stalled.set()
            self.resume.wait(PATIENCE)
            yield whole[half:]
        else:
            self.stalled.set()
        self.release.wait(PATIENCE)
        if not self.torn:
            yield whole

    def __enter__(self) -> _ReadGate:
        return self

    def __exit__(self, *exc: object) -> None:
        self.resume.set()
        self.release.set()


def _bulk_config(**changes: object) -> Config:
    """Small chunks two deep, so a short file is a long pipelined transfer."""
    return Config(bulk_chunk=CHUNK, bulk_depth=2, readahead=0).evolve(**changes)


def _opened(aio, srv, **changes: object):
    return aio.open(srv.url.with_path("/f"), "rb", buffering=0, config=_bulk_config(**changes))


def test_a_bulk_read_cancelled_between_frames_leaves_the_connection_in_step(aio):
    gate = _ReadGate(stall_at=2 * CHUNK)
    with FakeServer(files={"/f": DATA}) as srv, gate:
        srv.handlers[c.kXR_read] = gate

        async def main() -> None:
            async with _opened(aio, srv) as fh:
                session = fh.file.session
                task = asyncio.ensure_future(fh.pread(len(DATA), 0))
                await until(gate.stalled.is_set)
                assert await _cancelled(task) < PROMPT
                await until(lambda: session._bulk_owner is None, PROMPT)
                assert not session.broken and not session.closed
                gate.release.set()  # the replies still owed arrive, late
                async with asyncio.timeout(PATIENCE):
                    assert await fh.pread(1024, 4096) == DATA[4096 : 4096 + 1024]
                await until(session.machine.idle)
                assert await fh.pread(len(DATA), 0) == DATA  # the plane again
                assert fh.file.session is session

        run(main())


def test_a_bulk_read_cancelled_mid_frame_finishes_the_frame_first(aio):
    gate = _ReadGate(stall_at=2 * CHUNK, torn=True)
    with FakeServer(files={"/f": DATA}) as srv, gate:
        srv.handlers[c.kXR_read] = gate

        async def main() -> None:
            async with _opened(aio, srv) as fh:
                session = fh.file.session
                task = asyncio.ensure_future(fh.pread(len(DATA), 0))
                await until(gate.stalled.is_set)
                await _cancelled(task)
                # Let the reader see the cancellation part-way through the
                # frame, then let the frame's rest arrive within the grace.
                await asyncio.sleep(3 * _cancel.POLL)
                gate.resume.set()
                await until(lambda: session._bulk_owner is None, PROMPT)
                assert not session.broken and not session.closed
                gate.release.set()
                async with asyncio.timeout(PATIENCE):
                    assert await fh.pread(1024, 0) == DATA[:1024]

        run(main())


def test_a_bulk_read_stalled_mid_frame_costs_the_connection_not_the_file(aio):
    gate = _ReadGate(stall_at=2 * CHUNK, torn=True)
    with FakeServer(files={"/f": DATA}) as srv, gate:
        srv.handlers[c.kXR_read] = gate

        async def main() -> None:
            async with _opened(aio, srv) as fh:
                session = fh.file.session
                logins = _logins(srv)
                task = asyncio.ensure_future(fh.pread(len(DATA), 0))
                await until(gate.stalled.is_set)
                assert await _cancelled(task) < PROMPT
                # Nothing can find the next header on a wire stopped mid-frame,
                # so once the grace is up the connection is closed outright.
                await until(lambda: session.closed, bulk_module.CANCEL_GRACE + PROMPT)
                assert session.broken
                gate.release.set()
                async with asyncio.timeout(PATIENCE):
                    # The file re-opens on a fresh connection for the next read.
                    assert await fh.pread(len(DATA), 0) == DATA
                assert _logins(srv) == logins + 1
                assert fh.file.session is not session

        run(main())


def test_a_bulk_read_inside_a_scope_still_times_out():
    gate = _ReadGate(stall_at=2 * CHUNK)
    with FakeServer(files={"/f": DATA}) as srv, gate:
        srv.handlers[c.kXR_read] = gate
        config = _bulk_config(request_timeout=0.3, recover_handles=False)
        with xrdclient.open(srv.url.with_path("/f"), "rb", buffering=0, config=config) as fh:
            started = time.monotonic()
            with pytest.raises(XrdTimeoutError, match="bulk read timed out"):
                _cancel.within(_cancel.CancelScope(), lambda: fh.read(len(DATA)))
            assert time.monotonic() - started < PATIENCE
            assert fh.file.session.broken


@pytest.mark.parametrize(
    ("bulk", "options"),
    [(True, {}), (False, {}), (False, {"max_rate": 1e12})],
    # The bulk plane's workers, the spans' fan-out, and the single paced
    # stream whose read-ahead runs on a thread of its own.
    ids=["bulk-plane", "spans", "read-ahead-pump"],
)
def test_a_cancelled_copy_frees_its_worker(aio, tmp_path, bulk, options):
    gate = _ReadGate(stall_at=2 * CHUNK)
    target = tmp_path / "copied"
    with FakeServer(files={"/f": DATA}) as srv, gate:
        srv.handlers[c.kXR_read] = gate

        async def main() -> None:
            config = _bulk_config(chunk_size=CHUNK, in_flight=2, bulk=bulk)
            task = asyncio.ensure_future(
                aio.copy(
                    srv.url.with_path("/f"), str(target), config=config, verify=False, **options
                )
            )
            await until(gate.stalled.is_set)
            assert await _cancelled(task) < PROMPT
            # The one worker is back: whichever thread was reading saw the
            # scope too, so the copy did not wait for its stalled read.
            async with asyncio.timeout(PROMPT + 1.0):
                assert await aio.exists(str(tmp_path))

        run(main())


# ---------------------------------------------------------------------------
# HTTP: nothing more is sent
# ---------------------------------------------------------------------------


def test_an_http_request_is_not_sent_for_a_cancelled_call():
    scope = _cancel.CancelScope()
    with FakeDAVServer(files={"/d/a.root": b"hello"}) as dav:
        fs = xrdclient.FileSystem(dav.url)
        try:
            assert fs.stat("/d/a.root").size == 5
            scope.cancel()
            with pytest.raises(_cancel.Cancelled, match="was cancelled"):
                _cancel.within(scope, lambda: fs.stat("/d/a.root"))
        finally:
            fs.close()


# ---------------------------------------------------------------------------
# The scope itself
# ---------------------------------------------------------------------------


def test_a_scope_is_cancelled_by_its_own_flag_or_its_parents():
    outer = _cancel.CancelScope()
    inner = _cancel.CancelScope(outer)
    assert not inner.cancelled and repr(inner) == "CancelScope(live)"
    outer.cancel()
    assert inner.cancelled and repr(inner) == "CancelScope(cancelled)"
    alone = _cancel.CancelScope()
    alone.cancel()
    alone.cancel()  # idempotent
    assert alone.cancelled


def test_a_scope_is_in_force_only_inside_within():
    scope = _cancel.CancelScope()
    assert _cancel.current() is None and not _cancel.cancelled()
    assert _cancel.bounded(None) is None and _cancel.bounded(7.0) == 7.0
    _cancel.check()  # nothing in force, nothing raised

    def inside() -> tuple[object, float | None, float | None]:
        return _cancel.current(), _cancel.bounded(None), _cancel.bounded(0.01)

    assert _cancel.within(scope, inside) == (scope, _cancel.POLL, 0.01)
    assert _cancel.current() is None
    scope.cancel()
    with pytest.raises(_cancel.Cancelled, match="the copy was cancelled"):
        _cancel.within(scope, lambda: _cancel.check("the copy"))
    assert not issubclass(_cancel.Cancelled, Exception)
