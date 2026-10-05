"""Contracts for the request and transfer cores used by both client facades."""

from __future__ import annotations

import io
import threading
from http.client import BadStatusLine
from types import SimpleNamespace

import pytest

from xrdclient.copy import _pipeline as pipeline
from xrdclient.http import _connection, expect
from xrdclient.http import _engine as http


@pytest.mark.parametrize("status", [303, 307])
def test_redirect_engine_preserves_or_discards_body_by_status(status):
    requests = []
    state = http.Request("PUT", "origin", b"payload")

    def send(request):
        requests.append((request.method, request.url, request.body))
        return SimpleNamespace(status=status if len(requests) == 1 else 200)

    response = http.redirects(
        state,
        send,
        lambda request, response: "target" if response.status != 200 else None,
        1,
        lambda: RuntimeError("redirect limit"),
    )
    assert response.status == 200
    assert requests == [
        ("PUT", "origin", b"payload"),
        ("GET", "target", None) if status == 303 else ("PUT", "target", b"payload"),
    ]


def test_redirect_budget_has_no_extra_request():
    calls = []

    def send(request):
        calls.append(request.url)
        return SimpleNamespace(status=307)

    with pytest.raises(RuntimeError, match="redirect limit"):
        http.redirects(
            http.Request("GET", "origin"),
            send,
            lambda request, response: "target",
            2,
            lambda: RuntimeError("redirect limit"),
        )
    assert calls == ["origin", "target", "target"]


@pytest.mark.parametrize("error", [OSError("gone"), BadStatusLine("bad")])
@pytest.mark.parametrize("retry", [False, True])
def test_request_retries_at_most_once_and_closes_every_failed_exchange(error, retry):
    acquired, aborted = [], []

    def acquire():
        item = object()
        acquired.append(item)
        return item

    def send(item):
        raise error

    with pytest.raises(ValueError, match="translated") as caught:
        http.attempt(
            acquire,
            send,
            aborted.append,
            lambda item, error: retry,
            lambda error: ValueError("translated"),
        )
    assert caught.value.__cause__ is error
    assert aborted == acquired
    assert len(acquired) == (2 if retry else 1)


@pytest.mark.parametrize("error", [RuntimeError("callback"), KeyboardInterrupt()])
def test_callback_or_interrupt_is_not_wrapped_or_replayed(error):
    aborted = []

    def send(item):
        raise error

    with pytest.raises(type(error)) as caught:
        http.attempt(
            lambda: "exchange",
            send,
            aborted.append,
            lambda item, error: pytest.fail("must not retry"),
            lambda error: pytest.fail("must not translate"),
        )
    assert caught.value is error
    assert aborted == ["exchange"]


def test_successful_exchange_is_not_aborted():
    assert (
        http.attempt(
            lambda: "exchange",
            lambda item: item,
            lambda item: pytest.fail("must not abort"),
            lambda item, error: False,
            lambda error: error,
        )
        == "exchange"
    )


@pytest.mark.parametrize("depth", [1, 2, 4])
@pytest.mark.parametrize("recycle", [False, True])
def test_pipeline_handles_partial_writes_without_losing_or_duplicating_bytes(depth, recycle):
    source = b"abcdefghijklmnopqrstuvwxyz"
    target = bytearray()
    progress = []

    class PartialWriter:
        def write(self, view):
            accepted = min(len(view), 3)
            target.extend(view[:accepted])
            return accepted

    assert pipeline.pump(
        io.BytesIO(source),
        PartialWriter(),
        len(source),
        8,
        lambda done, total: progress.append((done, total)),
        None,
        depth,
        recycle=recycle,
    ) == len(source)
    assert target == source
    assert progress == [(8, 26), (16, 26), (24, 26), (26, 26)]


@pytest.mark.parametrize("count", [-1, 0, 10])
def test_invalid_write_counts_raise_the_adapter_error(count):
    failure = RuntimeError("destination did not accept the chunk")
    writer = SimpleNamespace(write=lambda view: count)
    with pytest.raises(RuntimeError) as caught:
        pipeline.write_all(writer, memoryview(b"abc"), lambda: failure)
    assert caught.value is failure


def test_zero_write_default_is_an_io_error():
    with pytest.raises(OSError, match="destination stopped accepting"):
        pipeline.write_all(SimpleNamespace(write=lambda view: 0), memoryview(b"abc"))


def test_legacy_writer_without_a_return_value_is_supported():
    received = []
    pipeline.write_all(
        SimpleNamespace(write=lambda view: received.append(bytes(view))), memoryview(b"abc")
    )
    assert received == [b"abc"]


def test_xrd_retained_views_are_not_overwritten_by_read_ahead():
    views = []

    class RetainingWriter:
        def write(self, view):
            views.append(view)
            return len(view)

    pipeline.pump(io.BytesIO(b"abcdefghijkl"), RetainingWriter(), None, 3, None, None, 2)
    assert b"".join(views) == b"abcdefghijkl"


def test_stop_while_waiting_for_a_buffer_does_not_issue_another_read():
    ahead = pipeline.ReadAhead(io.BytesIO(b"abc"), 3, 2)

    class StoppingQueue:
        def get(self):
            ahead._stop.set()
            return bytearray(3)

    ahead._buffers = StoppingQueue()
    ahead._read()
    assert list(ahead._drain()) == []


def test_pre_stopped_reader_never_reads():
    ahead = pipeline.ReadAhead(io.BytesIO(b"abc"), 3, 2)
    ahead._stop.set()
    with ahead as pieces:
        assert list(pieces) == []


@pytest.mark.parametrize("failure_at", ["writer", "progress", "digest"])
def test_consumer_failure_preserves_exception_and_joins_reader(failure_at):
    failure = RuntimeError(failure_at)

    def failed(*args):
        raise failure

    writer = SimpleNamespace(write=failed if failure_at == "writer" else lambda view: len(view))
    digest = SimpleNamespace(update=failed if failure_at == "digest" else lambda view: None)
    progress = failed if failure_at == "progress" else None
    with pytest.raises(RuntimeError) as caught:
        pipeline.pump(
            io.BytesIO(b"a" * 100),
            writer,
            None,
            4,
            progress,
            digest,
            2,
            thread_name="shared-engine-test",
        )
    assert caught.value is failure
    assert not any(thread.name == "shared-engine-test" for thread in threading.enumerate())


@pytest.mark.parametrize("kind", ["os_cause", "connect_timeout", "other"])
def test_connection_error_translation_preserves_os_cause_and_codes(kind):
    from urllib3.exceptions import ConnectTimeoutError, HTTPError

    cause = OSError("socket failure")
    failure = ConnectTimeoutError("slow") if kind == "connect_timeout" else HTTPError("broken")
    if kind == "os_cause":
        failure.__cause__ = cause

    def connect():
        raise failure

    with pytest.raises(OSError) as caught:
        _connection._connect(connect)
    if kind == "os_cause":
        assert caught.value is cause
    elif kind == "connect_timeout":
        assert isinstance(caught.value, TimeoutError)
    else:
        assert caught.value.errno == 5


def test_https_connection_delegates_to_the_shared_connector(monkeypatch):
    calls = []
    monkeypatch.setattr(_connection._HTTPSConnection, "connect", lambda self: calls.append(self))
    conn = _connection.HTTPSConnection("localhost")
    conn.connect()
    assert calls == [conn]


def test_interim_replay_closes_socket_once_and_reads_after_prefix():
    calls = []
    sock = SimpleNamespace(close=lambda: calls.append("closed"), recv_into=lambda view: 0)
    replay = expect._Replay(b"abc", sock)
    assert replay.readable()
    view = bytearray(4)
    assert replay.readinto(view) == 3
    assert replay.readinto(view) == 0
    replay.close()
    replay.close()
    assert calls == ["closed"]


@pytest.mark.parametrize("data", [b"", b"H"])
def test_interim_header_distinguishes_disconnect_from_partial_header(data):
    incoming = iter([data, b""])
    timeouts = []
    sock = SimpleNamespace(
        gettimeout=lambda: 5,
        settimeout=timeouts.append,
        recv=lambda count: next(incoming, b""),
    )
    if data:
        assert expect._read_head(sock, 1) == data
    else:
        with pytest.raises(ConnectionError):
            expect._read_head(sock, 1)
    assert timeouts[-1] == 5


def test_interim_header_size_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(expect, "_HEAD_LIMIT", 0)
    sock = SimpleNamespace(
        gettimeout=lambda: 5, settimeout=lambda value: None, recv=lambda count: b"H"
    )
    from http.client import LineTooLong

    with pytest.raises(LineTooLong):
        expect._read_head(sock, 1)
