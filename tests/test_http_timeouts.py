"""HTTP timeouts: connecting, waiting for an answer, and a long ``COPY``.

``connect_timeout`` bounds the handshake and nothing else; every read after
it waits up to ``request_timeout``, so a transfer that is slow but alive is
not cut off at the connect window. A third-party copy's ``timeout=`` is the
longest it will wait between two performance markers.
"""

from __future__ import annotations

import time

import pytest

import xrdclient
from xrdclient.config import Config
from xrdclient.errors import TimeoutError as XRDTimeoutError
from xrdclient.http import HTTPClient, third_party
from xrdclient.testing import FakeDAVServer

BODY = b"hello world"

#: Longer than the short timeout each test sets, and short enough to keep
#: the suite quick.
DELAY = 0.5
SHORT = 0.15


@pytest.fixture
def dav():
    with FakeDAVServer(files={"/d/a.root": BODY}) as server:
        yield server


@pytest.fixture
def slow(dav):
    """The endpoint, answering every ``GET`` only after :data:`DELAY`."""

    def later(method, path, headers):
        time.sleep(DELAY)

    dav.handlers["GET"] = later
    return dav


def _read(url, config):
    with xrdclient.FileSystem(url, config=config) as fs, fs.open("/d/a.root", "rb") as handle:
        return handle.read()


def test_a_slow_answer_waits_for_the_request_timeout_not_the_connect_one(slow):
    config = Config(connect_timeout=SHORT, request_timeout=10.0)
    assert _read(slow.url, config) == BODY


def test_a_request_timeout_shorter_than_the_answer_times_out(slow):
    config = Config(connect_timeout=10.0, request_timeout=SHORT)
    started = time.monotonic()
    with pytest.raises(XRDTimeoutError):
        _read(slow.url, config)
    # One wait, not two: a timeout is not a stale connection, and asking
    # again would only double how long the caller is kept waiting.
    assert time.monotonic() - started < 2 * DELAY
    assert [seen for seen in slow.seen if seen[0] == "GET"] == [("GET", "/d/a.root")]


@pytest.fixture
def elsewhere():
    """A destination whose ``COPY`` pauses before every marker."""
    with FakeDAVServer(dirs=["/d"]) as server:
        server.tpc_markers = 3
        server.tpc_marker_gap = DELAY / 2
        yield server


def test_a_copy_timeout_bounds_the_gap_between_markers_not_the_copy(dav, elsewhere):
    config = Config(connect_timeout=SHORT, request_timeout=SHORT)
    # The whole copy takes 4 gaps, longer than the timeout; each gap does not.
    third_party(dav.url / "d/a.root", elsewhere.url / "d/b.root", config=config, timeout=DELAY)
    assert elsewhere.contents("/d/b.root") == BODY


def test_a_copy_that_goes_quiet_for_longer_than_its_timeout_times_out(dav, elsewhere):
    config = Config(request_timeout=10.0)
    with pytest.raises(XRDTimeoutError):
        third_party(dav.url / "d/a.root", elsewhere.url / "d/b.root", config=config, timeout=SHORT)


def test_a_copy_timeout_applies_to_a_borrowed_client_and_only_to_the_copy(dav, elsewhere):
    with HTTPClient(Config(request_timeout=10.0)) as client:
        with pytest.raises(XRDTimeoutError):
            third_party(
                dav.url / "d/a.root", elsewhere.url / "d/b.root", client=client, timeout=SHORT
            )
        # The copy's connection, markers still pending on it, was let go.
        assert not client._pool
        # The next request on the client waits as long as its own config says.
        elsewhere.handlers["HEAD"] = lambda method, path, headers: time.sleep(2 * SHORT)
        assert client.request("HEAD", elsewhere.url / "d").status == 200
