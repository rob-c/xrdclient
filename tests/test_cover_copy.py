"""The copy engine's recovery and refusal paths that an ordinary copy never takes.

A local source that goes stale under a read, a span that stops because its
sibling failed, a retry that the deadline or the target forbids, a ZIP member
that is not there, a lite third-party copy whose pull fails: each is driven
here through the seam it already has, so it runs the same on every platform.
"""

from __future__ import annotations

import errno
import io
import os
import struct
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import IO, Any, cast

import pytest
from xrdcore import fanout

import xrdclient
from xrdclient.config import Config
from xrdclient.copy import _endpoints, _local, engine, replicas
from xrdclient.errors import ConnectionError as XRDConnectionError
from xrdclient.errors import TimeoutError as XRDTimeoutError
from xrdclient.io import open_url
from xrdclient.io.raw import XRootDRawIO
from xrdclient.proto import constants as c
from xrdclient.session import deadline as dl
from xrdclient.testing import FakeServer, error, frame
from xrdclient.testing.server import _Connection, _h_query_fctl
from xrdclient.url import XRootDURL, parse

CONFIG = Config(auth_order=("unix",), retry_backoff=0.0, connect_retries=2)


# ---------------------------------------------------------------------------
# _RecoveringLocalReader
# ---------------------------------------------------------------------------


class _Flaky:
    """A file handle that raises ``errno`` for its first ``failures`` operations."""

    def __init__(self, inner: io.FileIO, failures: int = 1, code: int = errno.ESTALE) -> None:
        self.inner = inner
        self.failures = failures
        self.code = code
        self.closed = False

    def _maybe_fail(self) -> None:
        if self.failures:
            self.failures -= 1
            raise OSError(self.code, os.strerror(self.code))

    def fileno(self) -> int:
        return self.inner.fileno()

    def read(self, size: int = -1) -> bytes:
        self._maybe_fail()
        return self.inner.read(size)

    def readinto(self, buffer: memoryview) -> int:
        self._maybe_fail()
        return self.inner.readinto(buffer)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self.inner.seek(offset, whence)

    def close(self) -> None:
        self.closed = True
        self.inner.close()


class _NoDescriptor:
    """A handle whose descriptor cannot be had: ``fstat`` of it fails."""

    def __init__(self) -> None:
        self.closed = False

    def fileno(self) -> int:
        raise OSError(errno.EBADF, "no descriptor")

    def close(self) -> None:
        self.closed = True


def _opener(monkeypatch: pytest.MonkeyPatch, handles: Sequence[Callable[[str], Any]]) -> list[str]:
    """Make the reader's opens answer ``handles`` in turn; the paths it opened."""
    opened: list[str] = []
    queue = list(handles)

    def open_unbuffered(path: str) -> Any:
        opened.append(path)
        return queue.pop(0)(path)

    monkeypatch.setattr(_local, "_open_unbuffered", open_unbuffered)
    return opened


def _real(path: str) -> io.FileIO:
    return open(path, "rb", buffering=0)


def _source(tmp_path: Path, data: bytes = b"x" * 10000) -> str:
    path = tmp_path / "source.bin"
    path.write_bytes(data)
    return str(path)


def _refuse(code: int) -> Callable[[str], Any]:
    def refuse(path: str) -> Any:
        raise OSError(code, os.strerror(code), path)

    return refuse


def test_a_transient_open_failure_is_retried(monkeypatch, tmp_path):
    path = _source(tmp_path)
    opened = _opener(monkeypatch, [_refuse(errno.EAGAIN), _real])
    reader = _local._RecoveringLocalReader(path, CONFIG)
    assert reader.read(4) == b"xxxx"
    reader.close()
    assert opened == [path, path]


def test_a_descriptor_that_cannot_be_stat_is_closed(monkeypatch, tmp_path):
    handle = _NoDescriptor()
    _opener(monkeypatch, [lambda path: handle])
    with pytest.raises(OSError, match="no descriptor"):
        _local._RecoveringLocalReader(_source(tmp_path), CONFIG)
    assert handle.closed


def test_a_stale_read_reopens_and_then_reads_small(monkeypatch, tmp_path):
    path = _source(tmp_path)
    flaky: list[_Flaky] = []

    def first(name: str) -> _Flaky:
        flaky.append(_Flaky(_real(name)))
        return flaky[0]

    _opener(monkeypatch, [first, _real])
    reader = _local._RecoveringLocalReader(path, CONFIG)
    # The failed attempt is retried on a fresh handle, capped at 4 KiB.
    assert len(reader.read()) == 4096
    assert flaky[0].closed
    assert len(reader.read(8192)) == 4096
    buffer = bytearray(8192)
    assert reader.readinto(buffer) == 10000 - 8192
    assert reader.seek(0) == 0
    reader.close()


def test_a_stale_readinto_reopens_and_fills_only_4kib(monkeypatch, tmp_path):
    path = _source(tmp_path)
    _opener(monkeypatch, [lambda name: _Flaky(_real(name)), _real])
    reader = _local._RecoveringLocalReader(path, CONFIG)
    buffer = bytearray(8192)
    assert reader.readinto(buffer) == 4096
    reader.close()


def test_a_reopen_that_finds_another_file_is_refused(monkeypatch, tmp_path):
    path = _source(tmp_path)
    replaced = tmp_path / "other.bin"
    replaced.write_bytes(b"y" * 20)

    def other(name: str) -> io.FileIO:
        return _real(str(replaced))

    _opener(monkeypatch, [lambda name: _Flaky(_real(name)), other])
    reader = _local._RecoveringLocalReader(path, CONFIG)
    with pytest.raises(OSError, match="local source changed while recovering") as caught:
        reader.read(10)
    assert caught.value.errno == errno.ESTALE


def test_a_reopen_whose_descriptor_fails_is_closed_and_raises(monkeypatch, tmp_path):
    path = _source(tmp_path)
    broken = _NoDescriptor()
    _opener(monkeypatch, [lambda name: _Flaky(_real(name)), lambda name: broken])
    reader = _local._RecoveringLocalReader(path, CONFIG)
    with pytest.raises(OSError, match="no descriptor"):
        reader.read(10)
    assert broken.closed


def test_a_read_that_keeps_failing_gives_up_after_the_retries(monkeypatch, tmp_path):
    path = _source(tmp_path)
    handles = [lambda name: _Flaky(_real(name), failures=10) for _ in range(4)]
    _opener(monkeypatch, handles)
    reader = _local._RecoveringLocalReader(path, CONFIG)
    with pytest.raises(OSError) as caught:
        reader.read(10)
    reader.close()
    assert caught.value.errno == errno.ESTALE


# ---------------------------------------------------------------------------
# _sync_local_target
# ---------------------------------------------------------------------------


def test_a_local_target_that_is_not_a_regular_file_is_left_alone(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    _local._sync_local_target(parse(str(fifo)), 10)


def test_a_local_target_shorter_after_sync_than_expected_fails(tmp_path):
    target = tmp_path / "target.bin"
    target.write_bytes(b"12345")
    with pytest.raises(OSError, match="5 bytes after sync; expected 6") as caught:
        _local._sync_local_target(parse(str(target)), 6)
    assert caught.value.errno == errno.EIO


# ---------------------------------------------------------------------------
# Spans in parallel
# ---------------------------------------------------------------------------

SPANNED = Config(auth_order=("unix",), parallel_chunks=2)


def test_parallel_spans_report_their_progress_together(tmp_path):
    source = _source(tmp_path, os.urandom(64 << 10))
    seen: list[tuple[int, int | None]] = []
    result = xrdclient.copy(
        source,
        str(tmp_path / "target.bin"),
        chunk_size=4096,
        config=SPANNED,
        progress=lambda done, total: seen.append((done, total)),
    )
    assert result.size == 64 << 10
    assert seen[-1] == (64 << 10, 64 << 10)
    assert (tmp_path / "target.bin").read_bytes() == Path(source).read_bytes()


class _Gated:
    """A span's source: the first span fails; the others wait for the halt, then read.

    The first span fails only once another has started (``others``), so the
    other is mid-loop when the halt comes - not still queued, where the
    fan-out would refuse to start it at all and its own stop check would
    never be reached.
    """

    def __init__(
        self, inner: IO[bytes], halt: list[threading.Event], others: threading.Event
    ) -> None:
        self.inner = inner
        self.halt = halt
        self.others = others
        self.start: int | None = None

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if self.start is None:
            self.start = offset
            if offset:
                self.others.set()
        return self.inner.seek(offset, whence)

    def read(self, size: int = -1) -> bytes:
        if self.start == 0:
            assert self.others.wait(10)
            raise OSError(errno.EIO, "the first span's source failed")
        if self.start is not None:
            assert self.halt[0].wait(10)
        return self.inner.read(size)


def test_a_span_stops_once_another_span_has_failed(monkeypatch, tmp_path):
    source = _source(tmp_path, b"z" * (64 << 10))
    halt: list[threading.Event] = []
    others = threading.Event()
    real_run_all = fanout.run_all
    real_reader = engine._reader

    def run_all(calls: Sequence[Callable[[], int]], **kwargs: Any) -> list[int]:
        stop = kwargs["stop"]
        assert isinstance(stop, threading.Event)
        halt.append(stop)
        return real_run_all(calls, **kwargs)

    def reader(url: XRootDURL, config: Config, stack: ExitStack) -> tuple[Any, int | None]:
        handle, size = real_reader(url, config, stack)
        return _Gated(handle, halt, others), size

    stopped: list[BaseException] = []

    class Stopped(fanout.Stopped):
        def __init__(self, *args: object) -> None:
            super().__init__(*args)
            stopped.append(self)

    monkeypatch.setattr(fanout, "run_all", run_all)
    monkeypatch.setattr(fanout, "Stopped", Stopped)
    monkeypatch.setattr(engine, "_reader", reader)
    with pytest.raises(OSError, match="first span's source failed"):
        xrdclient.copy(source, str(tmp_path / "target.bin"), chunk_size=4096, config=SPANNED)
    assert stopped and "another span failed" in str(stopped[0])


# ---------------------------------------------------------------------------
# Whether a dropped transfer is tried again
# ---------------------------------------------------------------------------


def _job(target: XRootDURL | None, *, overwrite: bool = True) -> engine._Job:
    source = parse("root://h:1094//f")
    return engine._Job(source, target, source, target, CONFIG, 4096, overwrite)


def test_a_stall_or_a_busy_server_is_not_retried():
    with pytest.raises(XRDTimeoutError):
        engine._retry_transfer(_job(parse("/tmp/t")), XRDTimeoutError("stalled"), 1, resume=False)


def test_no_retry_once_the_deadline_has_passed(monkeypatch):
    monkeypatch.setattr(dl, "remaining", lambda: 0.0)
    dropped = XRDConnectionError("dropped")
    with pytest.raises(XRDConnectionError):
        engine._retry_transfer(_job(parse("/tmp/t")), dropped, 1, resume=False)


def test_an_open_stream_target_cannot_be_redone():
    assert engine._can_retry(_job(None), resume=True) is False


# ---------------------------------------------------------------------------
# A ZIP member that is not there
# ---------------------------------------------------------------------------


def _zip(**members: bytes) -> bytes:
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_probing_a_missing_zip_member_finds_nothing():
    with FakeServer(files={"/a.zip": _zip(present=b"hello")}) as srv:
        assert _endpoints._probe(parse(f"{srv.url}/a.zip?xrdcl.unzip=present"), CONFIG) == (
            5,
            1_700_000_000,
        )
        assert _endpoints._probe(parse(f"{srv.url}/a.zip?xrdcl.unzip=absent"), CONFIG) is None


# ---------------------------------------------------------------------------
# Several replicas: a reader part-way through a block when the copy fails
# ---------------------------------------------------------------------------


def test_a_reader_stops_between_chunks_once_the_copy_has_failed():
    fetch = replicas._Fetch([], io.BytesIO(), CONFIG, 4, None)
    blocks = replicas._Blocks(16, 8)
    blocks.stop()
    # Never read from: the copy has failed before the first chunk.
    unread = cast("XRootDRawIO", io.BytesIO(b"never read"))
    with pytest.raises(fanout.Stopped, match="another reader failed"):
        fetch._span(unread, 0, 8, blocks)


# ---------------------------------------------------------------------------
# A TPC-lite copy whose pull fails
# ---------------------------------------------------------------------------


def _lite_destination(dst: FakeServer) -> None:
    """Answer ``tpc tpcdlg`` as a delegating (lite) destination; fail the pull."""
    syncs: list[int] = []

    def query(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
        infotype = struct.unpack(">H", params[:2])[0]
        if infotype == c.kXR_Qconfig and body.startswith(b"tpc tpcdlg"):
            yield frame(sid, c.kXR_ok, b"1\ngsi\x00")
            return
        yield from _h_query_fctl(conn, sid, params, body)

    def sync(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
        syncs.append(sid)
        yield frame(sid, c.kXR_ok) if len(syncs) == 1 else error(sid, 3007, "pull failed")

    dst.handlers[c.kXR_query] = query
    dst.handlers[c.kXR_sync] = sync


def test_a_failed_lite_pull_has_no_source_handle_to_close():
    with (
        FakeServer(files={"/data/a.root": b"hello world"}) as src,
        FakeServer(dirs=["/"]) as dst,
    ):
        _lite_destination(dst)
        with pytest.raises(xrdclient.XRootDError, match="pull failed"):
            xrdclient.third_party(
                src.url / "data/a.root", dst.url / "b.root", delegate=True, verify=False
            )
        assert not [p for p in src.opened if "tpc.key" in p]


# ---------------------------------------------------------------------------
# io: a writer's buffer, and a strided write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("buffering", "config"),
    [(4096, CONFIG), (-1, Config(auth_order=("unix",), bulk=False))],
    ids=["explicit-buffering", "no-bulk-plane"],
)
def test_a_writer_keeps_the_buffer_it_was_given(buffering, config):
    with FakeServer(dirs=["/"]) as srv:
        with open_url(f"{srv.url}/w.bin", "wb", buffering=buffering, config=config) as out:
            assert isinstance(out, io.BufferedWriter)
            out.write(b"payload")
        assert srv.files["/w.bin"] == b"payload"


def test_a_strided_view_is_gathered_before_it_is_sent():
    with FakeServer(dirs=["/"]) as srv:
        with open_url(f"{srv.url}/s.bin", "wb", buffering=0, config=CONFIG) as raw:
            assert raw.write(memoryview(b"abcdef")[::2]) == 3
        assert srv.files["/s.bin"] == b"ace"
