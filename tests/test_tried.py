"""Going back to the manager after a data server fails: ``tried=`` and ``triedrc=``.

XrdCl tells a manager which servers have already failed a request, so that
``cmsd`` sends it somewhere else - ``tried`` lists them, ``triedrc`` says
why. The manager here is a :class:`~xrdclient.testing.FakeServer` flagging
itself ``kXR_isManager``; what it was told is in its ``arguments`` and
``opened`` records, CGI and all.
"""

from __future__ import annotations

import socket
import struct

import pytest

import xrdclient
from xrdclient.errors import NotFoundError, RedirectLimitError
from xrdclient.proto import constants as c
from xrdclient.session.router import tried, with_tried
from xrdclient.testing import FakeServer
from xrdclient.testing import server as fake_server

MANAGER = c.kXR_isManager | c.kXR_isServer


def dead_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


@pytest.fixture
def quick(config):
    return config.evolve(retry_backoff=0.0, connect_retries=3)


def _stats(server: FakeServer) -> list[str]:
    return [arg for opcode, arg in server.arguments if opcode == c.kXR_stat]


# -- the CGI ------------------------------------------------------------------


def test_with_tried_adds_hosts_and_a_reason():
    assert with_tried("/f", ["a", "b"], "ioerr") == "/f?tried=a,b&triedrc=ioerr"
    assert with_tried("/f?x=1", ["a"]) == "/f?x=1&tried=a"


def test_with_tried_merges_the_callers_own_and_replaces_a_stale_reason():
    path = "/f?tried=z,a&triedrc=enoent&&x=1"
    assert with_tried(path, ["a", "b"], "srverr") == "/f?x=1&tried=z,a,b&triedrc=srverr"


def test_with_tried_leaves_a_path_alone_with_no_hosts():
    assert with_tried("/f?x=1", []) == "/f?x=1"
    assert with_tried("", ["a"]) == ""
    assert tried(["a"], "a") == ["a"]


# -- a connection lost at the data server --------------------------------------


def test_a_dead_data_server_is_named_to_the_manager(quick):
    with FakeServer(files={"/f": b"abc"}, flags=MANAGER) as manager:
        manager.redirects[c.kXR_stat] = ("127.0.0.1", dead_port(), "cap=1")
        with xrdclient.FileSystem(manager.url, quick) as fs:
            assert fs.stat("/f").st_size == 3
    assert _stats(manager) == ["/f?tried=127.0.0.1&triedrc=srverr"]


def test_tried_accumulates_over_every_trip_back(quick):
    second = dead_port()

    def redirect_again(conn, sid, params, body):
        del manager.handlers[c.kXR_stat]  # once
        yield fake_server.frame(sid, c.kXR_redirect, struct.pack(">i", second) + b"127.0.0.1\x00")

    with FakeServer(files={"/f": b"abc"}, flags=MANAGER) as manager:
        manager.redirects[c.kXR_stat] = ("localhost", dead_port(), "")
        manager.handlers[c.kXR_stat] = redirect_again
        with xrdclient.FileSystem(manager.url, quick) as fs:
            assert fs.stat("/f").st_size == 3
    assert _stats(manager) == [
        "/f?tried=localhost&triedrc=srverr",
        "/f?tried=localhost,127.0.0.1&triedrc=srverr",
    ]


def test_an_open_whose_data_server_dies_returns_to_the_manager(quick):
    """A sticky (file) router the redirect moved is moved back home."""
    with FakeServer(files={"/f": b"abc"}, flags=MANAGER) as manager:
        manager.redirects[c.kXR_open] = ("127.0.0.1", dead_port(), "")
        with xrdclient.File(manager.url.with_path("/f"), quick) as fh:
            assert fh.read(3) == b"abc"
            assert fh.endpoint == f"{manager.address[0]}:{manager.address[1]}"
    assert manager.opened == ["/f?tried=127.0.0.1&triedrc=srverr"]


# -- an error from the data server ------------------------------------------------


def _refresh_recording(manager: FakeServer, seen: list[int]):
    def recording(conn, sid, params, body):
        seen.append(struct.unpack(">H", params[2:4])[0])
        yield from fake_server._h_open(conn, sid, params, body)

    manager.handlers[c.kXR_open] = recording


def test_a_missing_file_at_the_data_server_is_looked_up_afresh(quick):
    options: list[int] = []
    with FakeServer(files={"/f": b"abc"}, flags=MANAGER) as manager, FakeServer() as empty:
        _refresh_recording(manager, options)
        manager.redirects[c.kXR_open] = (*empty.address, "")
        with xrdclient.File(manager.url.with_path("/f"), quick) as fh:
            assert fh.read(3) == b"abc"
    assert manager.opened == ["/f?tried=127.0.0.1&triedrc=enoent"]
    assert options[-1] & c.kXR_refresh


def test_an_io_error_at_the_data_server_is_reported_as_ioerr(quick):
    def broken(conn, sid, params, body):
        yield fake_server.error(sid, 3007, "disk on fire")

    with (
        FakeServer(files={"/f": b"abc"}, flags=MANAGER) as manager,
        FakeServer(files={"/f": b"abc"}) as ds,
        xrdclient.FileSystem(manager.url, quick) as fs,
    ):
        ds.handlers[c.kXR_stat] = broken
        manager.redirects[c.kXR_stat] = (*ds.address, "")
        assert fs.stat("/f").st_size == 3
    assert _stats(manager) == ["/f?tried=127.0.0.1&triedrc=ioerr"]


def test_an_error_after_a_plain_servers_redirect_is_the_callers(quick):
    """No load balancer - the redirector does not flag itself a manager."""
    with FakeServer(files={"/f": b"abc"}) as redirector, FakeServer() as empty:
        redirector.redirects[c.kXR_stat] = (*empty.address, "")
        with xrdclient.FileSystem(redirector.url, quick) as fs, pytest.raises(NotFoundError):
            fs.stat("/f")
    assert _stats(redirector) == []


def test_a_request_that_is_not_safe_to_repeat_is_not_sent_back(quick):
    with FakeServer(files={"/f": b"abc"}, flags=MANAGER) as manager, FakeServer() as empty:
        manager.redirects[c.kXR_rm] = (*empty.address, "")
        with xrdclient.FileSystem(manager.url, quick) as fs, pytest.raises(NotFoundError):
            fs.remove("/f")
    assert "/f" in manager.files


def test_the_redirect_limit_ends_it_with_the_servers_own_error(quick):
    with FakeServer(files={"/f": b"abc"}, flags=MANAGER) as manager, FakeServer() as empty:
        manager.redirects[c.kXR_stat] = (*empty.address, "")
        settings = quick.evolve(redirect_limit=1)
        with xrdclient.FileSystem(manager.url, settings) as fs, pytest.raises(NotFoundError):
            fs.stat("/f")


def test_a_redirect_loop_still_hits_the_limit(quick):
    with FakeServer(files={"/f": b"abc"}, flags=MANAGER) as manager:

        def bounce(conn, sid, params, body):
            yield fake_server.frame(
                sid, c.kXR_redirect, struct.pack(">i", manager.address[1]) + b"127.0.0.1\x00"
            )

        manager.handlers[c.kXR_stat] = bounce
        with xrdclient.FileSystem(manager.url, quick) as fs, pytest.raises(RedirectLimitError):
            fs.stat("/f")


# -- a re-opened file ---------------------------------------------------------


def test_a_reopen_after_the_data_server_vanishes_names_it(quick):
    with FakeServer(files={"/f": b"abcdef"}, flags=MANAGER) as manager:
        ds = FakeServer(files={"/f": b"abcdef"}).start()
        manager.redirects[c.kXR_open] = (*ds.address, "")
        with xrdclient.File(manager.url.with_path("/f"), quick) as fh:
            assert fh.read(3, 0) == b"abc"
            ds.stop()
            assert fh.read(3, 3) == b"def"
            assert fh.recoveries == 1
    assert manager.opened == ["/f?tried=127.0.0.1&triedrc=srverr"]


def test_a_file_opened_on_its_own_server_names_nobody_on_reopen(quick):
    with FakeServer(files={"/f": b"abcdef"}) as srv:
        with xrdclient.File(srv.url.with_path("/f"), quick) as fh:
            fh.read(3, 0)
            fh._reopen()
        assert srv.opened == ["/f", "/f"]


def test_a_reopen_another_thread_already_did_is_not_repeated(quick):
    with FakeServer(files={"/f": b"abcdef"}) as srv:
        with xrdclient.File(srv.url.with_path("/f"), quick) as fh:
            seen = fh.recoveries
            first = fh._reopen(seen)
            assert fh._reopen(seen) == first
            assert fh.recoveries == 1
        assert srv.opened.count("/f") == 2


def test_a_reopen_that_fails_leaves_the_file_closed(quick, monkeypatch):
    with FakeServer(files={"/f": b"abcdef"}) as srv:
        fh = xrdclient.File(srv.url.with_path("/f"), quick)
        fh.open()
        srv.files.clear()
        with pytest.raises(NotFoundError):
            fh._reopen()
        assert not fh.is_open
        fh.close()
