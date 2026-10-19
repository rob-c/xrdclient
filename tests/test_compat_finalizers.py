"""Compat objects dropped without a close: let go of off the collector's thread.

The collector runs a finalizer on whichever thread allocated at the wrong
moment - possibly one holding the session pool's lock. A close from there
that needs that lock never returns, so what a dropped compat object owes
the server is queued, and settled on an ordinary thread.
"""

from __future__ import annotations

import gc
import threading
import time
from typing import Any

import pytest

from xrdclient import _abandoned
from xrdclient.compat import client
from xrdclient.compat.client import _channels, env
from xrdclient.config import Config
from xrdclient.proto import constants as c
from xrdclient.session.pool import SESSIONS
from xrdclient.session.router import Router
from xrdclient.testing import FakeServer

CONFIG = Config(username="tester", auth_order=("host",), require_tls=False, data_streams=0)


@pytest.fixture(autouse=True)
def _test_config(monkeypatch):
    monkeypatch.setattr(env, "config", lambda: CONFIG)
    yield
    gc.collect()
    _abandoned.reap()
    _channels.close_all()


@pytest.fixture
def srv():
    with FakeServer(files={"/d/f": b"data"}) as server:
        yield server


def _collect_holding(lock: threading.Lock, drop: list[Any]) -> bool:
    """Drop the objects and collect on a thread that holds ``lock``; whether it returned."""
    done = threading.Event()

    def collect() -> None:
        with lock:
            drop.clear()
            gc.collect()
        done.set()

    threading.Thread(target=collect, daemon=True).start()
    return done.wait(10)


def test_a_file_dropped_open_is_closed_later_not_by_the_collector(srv):
    f = client.File()
    status, _ = f.open(f"root://{srv.url.netloc}//d/f", timeout=5)
    assert status.ok
    native = f.native
    closes = srv.seen.count(c.kXR_close)
    held = [f]
    del f
    assert _collect_holding(SESSIONS._lock, held), "collecting an open file blocked"
    assert native is not None
    _abandoned.reap()
    # The background reaper may have picked the file up first and still be
    # mid-close on its own thread, so wait (bounded) for the close to land
    # rather than asserting the instant this thread's reap() returns.
    deadline = time.monotonic() + 10
    while (native.is_open or srv.seen.count(c.kXR_close) == closes) and (
        time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert not native.is_open
    assert srv.seen.count(c.kXR_close) == closes + 1  # a writer is still committed


def test_the_reaper_closes_what_nobody_reaps(srv, monkeypatch):
    monkeypatch.setattr(_abandoned, "INTERVAL", 0.01)
    monkeypatch.setattr(_abandoned, "_reaper", None)
    f = client.File()
    assert f.open(f"root://{srv.url.netloc}//d/f")[0].ok
    native = f.native
    del f
    gc.collect()
    deadline = time.monotonic() + 10
    while native is not None and native.is_open and time.monotonic() < deadline:
        time.sleep(0.01)
    assert native is not None and not native.is_open


def test_a_closed_file_leaves_nothing_to_close(srv):
    f = client.File()
    assert f.open(f"root://{srv.url.netloc}//d/f")[0].ok
    assert f.close()[0].ok
    del f
    gc.collect()
    assert _abandoned.reap() == 0


def test_one_failing_close_does_not_strand_the_rest():
    class Fails:
        def close(self) -> None:
            raise OSError("gone")

    closed = []

    class Works:
        def close(self) -> None:
            closed.append(True)

    _abandoned.adopt(Fails())
    _abandoned.adopt(Works())
    assert _abandoned.reap() == 2
    assert closed == [True]


def test_the_reaper_is_started_once_per_process(monkeypatch):
    started = []
    monkeypatch.setattr(_abandoned, "_reaper", None)
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self.name))
    _abandoned.ensure_reaper()
    _abandoned.ensure_reaper()
    assert started == ["xrd-finalizer"]


def test_a_forked_child_forgets_its_parents_queue(monkeypatch):
    monkeypatch.setattr(_abandoned, "_queue", _abandoned._queue)
    monkeypatch.setattr(_abandoned, "_reaper", _abandoned._reaper)

    class Parents:
        def close(self) -> None:
            raise AssertionError("the child must not close the parent's file")

    _abandoned.adopt(Parents())
    _abandoned._forget()
    assert _abandoned._reaper is None
    assert _abandoned.reap() == 0


def _closed_soon(router: Router) -> bool:
    """Whether ``router`` lets go of its connection within a few seconds."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        _abandoned.reap()
        if not router.connected:
            return True
        time.sleep(0.01)
    return False


def test_a_dropped_filesystem_releases_its_connection_without_the_collector_blocking(srv):
    fs = client.FileSystem(f"root://{srv.url.netloc}")
    assert fs.ping()[0].ok
    router = fs.native._router
    held: list[object] = [fs]
    del fs
    assert _collect_holding(SESSIONS._lock, held), "collecting a filesystem blocked"
    # Queued, and closed on an ordinary thread.
    assert _closed_soon(router)
