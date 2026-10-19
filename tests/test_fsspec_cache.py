"""fsspec's named caches, and an fsspec filesystem let go of by the collector."""

from __future__ import annotations

import gc
import io
import threading
import time

import pytest

pytest.importorskip("fsspec", reason="optional dependency: fsspec is not installed")

from xrdclient import _abandoned
from xrdclient.fsspec_impl import XRootDFileSystem
from xrdclient.session.pool import SESSIONS
from xrdclient.testing import FakeServer

BODY = bytes(range(256)) * 64  # 16 KiB, so a small block size makes several blocks


@pytest.fixture
def big():
    with FakeServer(files={"/d/big": BODY}) as srv:
        yield srv


@pytest.fixture
def xfs(big):
    XRootDFileSystem.clear_instance_cache()
    filesystem = XRootDFileSystem(str(big.url))
    try:
        yield filesystem
    finally:
        filesystem.close()
        XRootDFileSystem.clear_instance_cache()


def test_no_cache_type_keeps_the_io_stack(xfs):
    with xfs.open("/d/big", "rb") as fh:
        assert isinstance(fh, io.BufferedReader)
        assert fh.read() == BODY


@pytest.mark.parametrize(
    # Not "mmap": fsspec's MMapCache leaves its temporary file for the collector.
    "cache_type",
    ["none", "bytes", "readahead", "blockcache", "first", "all", "background"],
)
def test_each_named_cache_reads_the_file_exactly(xfs, cache_type):
    with xfs.open("/d/big", "rb", block_size=1000, cache_type=cache_type) as fh:
        assert fh.cache.name == cache_type
        assert fh.read(10) == BODY[:10]
        assert fh.seek(5000) == 5000
        assert fh.read(3000) == BODY[5000:8000]
        assert fh.seek(-6, io.SEEK_END) == len(BODY) - 6
        assert fh.read() == BODY[-6:]
        assert fh.read() == b""
        fh.seek(-10, io.SEEK_CUR)
        assert fh.tell() == len(BODY) - 10
        assert fh.readable() and fh.seekable()


def test_cache_options_reach_the_cache(xfs):
    with xfs.open("/d/big", "rb", cache_type="blockcache", cache_options={"maxblocks": 3}) as fh:
        assert fh.cache.maxblocks == 3
        assert fh.blocksize == xfs.blocksize == fh.cache.blocksize
        assert fh.read() == BODY


@pytest.mark.parametrize(("cache_type", "fetches"), [("none", 2), ("bytes", 1)])
def test_the_cache_decides_how_often_the_server_is_asked(xfs, cache_type, fetches, monkeypatch):
    with xfs.open("/d/big", "rb", block_size=1000, cache_type=cache_type) as fh:
        asked = []
        read = fh._raw.read

        def counted(size: int) -> bytes:
            asked.append(size)
            return bytes(read(size))

        monkeypatch.setattr(fh._raw, "read", counted)
        assert fh.read(4) == BODY[:4]
        fh.seek(0)
        assert fh.read(4) == BODY[:4]
        assert len(asked) == fetches


def test_text_mode_reads_through_a_named_cache(xfs, big):
    big.add_file("/d/t.txt", b"one\ntwo\n")
    with xfs.open("/d/t.txt", "r", cache_type="bytes") as text:
        assert text.readlines() == ["one\n", "two\n"]


def test_a_write_ignores_the_cache_it_cannot_use(xfs):
    with xfs.open("/d/new", "wb", cache_type="blockcache") as fh:
        fh.write(b"written")
    assert xfs.cat_file("/d/new") == b"written"


def test_an_unknown_cache_type_is_refused_by_name(xfs):
    with pytest.raises(ValueError, match=r"unknown cache_type 'lru'.*blockcache"):
        xfs.open("/d/big", "rb", cache_type="lru")


def test_bad_cache_options_close_the_file_they_were_for(xfs, monkeypatch):
    closed = []
    real = xfs._fs.open

    def tracking(*args, **kwargs):
        raw = real(*args, **kwargs)
        original = raw.close

        def close() -> None:
            closed.append(True)
            original()

        monkeypatch.setattr(raw, "close", close)
        return raw

    monkeypatch.setattr(xfs._fs, "open", tracking)
    with pytest.raises(TypeError):
        xfs.open("/d/big", "rb", cache_type="blockcache", cache_options={"nonsense": 1})
    assert closed


def test_a_bad_whence_or_a_negative_seek_is_refused(xfs):
    with xfs.open("/d/big", "rb", cache_type="bytes") as fh:
        with pytest.raises(ValueError, match="invalid whence"):
            fh.seek(0, 7)
        with pytest.raises(ValueError, match="negative seek"):
            fh.seek(-1)


def test_a_file_shorter_than_its_size_ends_the_fetch(xfs):
    with xfs.open("/d/big", "rb", cache_type="none") as fh:
        fh.size = len(BODY) + 100  # the file shrank since it was opened
        assert fh._fetch(len(BODY) - 4, len(BODY) + 100) == BODY[-4:]


def test_a_closed_filesystem_leaves_nothing_to_queue(big):
    filesystem = XRootDFileSystem(str(big.url), skip_instance_cache=True)
    filesystem.ls("/d")
    filesystem.close()
    _abandoned.reap()
    del filesystem
    gc.collect()
    assert _abandoned.reap() == 0


def test_closing_a_cached_file_twice_is_harmless(xfs):
    fh = xfs.open("/d/big", "rb", cache_type="bytes")
    fh.close()
    fh.close()
    assert fh.closed


# -- dropped without close -------------------------------------------------


def test_a_dropped_filesystem_queues_its_connection_without_taking_a_lock(big):
    """Collected on a thread that holds the pool's lock: a close there would deadlock."""
    filesystem = XRootDFileSystem(str(big.url), skip_instance_cache=True)
    assert filesystem.ls("/d", detail=False) == ["/d/big"]
    assert filesystem._fs is not None
    router = filesystem._fs._router
    done = threading.Event()

    def collect_holding_the_pool_lock() -> None:
        nonlocal filesystem
        with SESSIONS._lock:
            del filesystem
            gc.collect()
        done.set()

    worker = threading.Thread(target=collect_holding_the_pool_lock, daemon=True)
    worker.start()
    worker.join(10)
    assert done.is_set(), "collecting an unclosed filesystem blocked on the pool"
    # Queued, and closed on an ordinary thread - this one, or the reaper.
    deadline = time.monotonic() + 10
    while router.connected and time.monotonic() < deadline:
        _abandoned.reap()
        time.sleep(0.01)
    assert not router.connected
