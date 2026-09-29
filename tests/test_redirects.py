"""Redirects the way a federation hands them out.

A manager (redirector) answers a namespace request with "ask that data
server", and XrdCl's rule is that the redirect is for that request only: the
next request on the same :class:`~xrdclient.FileSystem` starts again at the
manager, because the next path may well live somewhere else. A
:class:`~xrdclient.File`, by contrast, stays where its open landed - the handle
exists nowhere else.
"""

from __future__ import annotations

import socket
import threading

import pytest

import xrdclient
from xrdclient.config import Config
from xrdclient.errors import ConnectionError as XrdConnectionError
from xrdclient.errors import ProtocolError, TransientError
from xrdclient.proto import constants as c
from xrdclient.proto import requests as r
from xrdclient.proto import responses as rp
from xrdclient.session.pool import SESSIONS
from xrdclient.session.router import Router, _path_fields, _redirect_path, _repath, _retarget
from xrdclient.session.sync import RedirectRequired, Session
from xrdclient.testing import FakeServer


def _logins(server: FakeServer) -> int:
    return server.seen.count(c.kXR_login)


# ---------------------------------------------------------------------------
# A FileSystem goes back to its manager for every request
# ---------------------------------------------------------------------------


def test_each_namespace_request_starts_at_the_manager(config):
    """``stat /a`` redirected to ds1 must not send ``stat /b`` to ds1 too."""
    with (
        FakeServer() as manager,
        FakeServer(files={"/a": b"A"}) as ds1,
        FakeServer(files={"/b": b"BB"}) as ds2,
        xrdclient.FileSystem(manager.url, config) as fs,
    ):
        manager.redirects[c.kXR_stat] = (*ds1.address, "")
        assert fs.stat("/a").st_size == 1
        manager.redirects[c.kXR_stat] = (*ds2.address, "")
        assert fs.stat("/b").st_size == 2
        assert fs.endpoint == f"{manager.address[0]}:{manager.address[1]}"
        assert manager.seen.count(c.kXR_stat) == 2


def test_a_redirected_request_reuses_the_pooled_data_server_connection(config):
    """The hop's session is pooled, so the second redirect there does not log in."""
    with (
        FakeServer() as manager,
        FakeServer(files={"/a": b"A"}) as ds,
        xrdclient.FileSystem(manager.url, config) as fs,
    ):
        for _ in range(2):
            manager.redirects[c.kXR_stat] = (*ds.address, "")
            fs.stat("/a")
        assert _logins(ds) == 1


def test_a_redirect_back_to_the_manager_uses_its_own_connection(server, config):
    with xrdclient.FileSystem(server.url, config) as fs:
        server.redirects[c.kXR_stat] = (*server.address, "tok=1")
        fs.stat("/data/a.root")
    assert _logins(server) == 1


def test_a_file_opened_through_a_filesystem_stays_on_its_data_server(config):
    with (
        FakeServer() as manager,
        FakeServer(files={"/f": b"hello"}) as ds,
        xrdclient.FileSystem(manager.url, config) as fs,
    ):
        fs.stat("/")
        manager.redirects[c.kXR_open] = (*ds.address, "")
        with fs.open("/f", "rb") as fh:
            assert fh.read() == b"hello"
        assert c.kXR_read in ds.seen
        assert fs.endpoint == f"{manager.address[0]}:{manager.address[1]}"
        # The manager's connection survived the file's redirect.
        fs.stat("/")
        assert _logins(manager) == 1


def test_a_filesystem_touch_through_a_redirect(config):
    with (
        FakeServer(dirs=["/"]) as manager,
        FakeServer(dirs=["/"]) as ds,
        xrdclient.FileSystem(manager.url, config) as fs,
    ):
        manager.redirects[c.kXR_open] = (*ds.address, "")
        fs.touch("/new")
        assert "/new" in ds.files
        assert "/new" not in manager.files


def test_a_lent_router_hands_its_own_connection_on_to_the_pinned_one(config):
    with FakeServer() as manager, FakeServer(files={"/f": b"x"}) as ds:
        home = Router(manager.url, config, sticky=False)
        lent = home.lend()
        manager.redirects[c.kXR_open] = (*ds.address, "")
        lent.execute(r.Open("/f", c.kXR_open_read), path="/f")
        pinned = lent.pin()
        assert pinned.endpoint == f"{ds.address[0]}:{ds.address[1]}"
        assert pinned._loan is None and not lent.connected
        pinned.close()
        home.close()


def test_pinning_a_router_that_never_connected_pins_nothing(config, server):
    pinned = Router(server.url, config).pin()
    assert not pinned.connected and pinned._loan is None
    pinned.close()


def test_a_lent_router_that_stayed_home_only_borrows(config, server):
    home = Router(server.url, config, sticky=False)
    lent = home.lend()
    lent.execute(r.Ping())
    pinned = lent.pin()
    assert pinned._session is home._session and pinned._loan is home._loan is not None
    pinned.close()
    lent.close()
    assert home.connected
    home.close()


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


def test_a_redirect_in_one_thread_does_not_close_anothers_session():
    """The race from the review: a redirected stat against a busy mkdir."""
    config = Config(
        username="tester", auth_order=("host",), require_tls=False, data_streams=0, pool_size=0
    )
    errors: list[str] = []
    stop = threading.Event()
    with (
        FakeServer(files={"/a": b"A"}, dirs=["/"]) as manager,
        FakeServer(files={"/a": b"A"}) as ds,
        xrdclient.FileSystem(manager.url, config) as fs,
    ):

        def busy() -> None:
            i = 0
            while not stop.is_set():
                i += 1
                try:
                    fs.mkdir(f"/d{i}")
                except Exception as exc:  # pragma: no cover - the failure being tested
                    errors.append(repr(exc))

        worker = threading.Thread(target=busy)
        worker.start()
        try:
            for _ in range(50):
                manager.redirects[c.kXR_stat] = (*ds.address, "")
                try:
                    fs.stat("/a")
                except Exception as exc:  # pragma: no cover - the failure being tested
                    errors.append("stat: " + repr(exc))
        finally:
            stop.set()
            worker.join()
    assert errors == []


def test_a_retry_drops_only_the_session_that_failed(server, config):
    """Another thread's fresh session must survive a stale failure."""
    router = Router(server.url, config)
    stale = router.session
    router._drop(stale)
    fresh = router.session
    assert fresh is not stale
    router._drop(stale)  # a second thread reporting the same old failure
    assert router._session is fresh and not fresh.closed
    router.close()


def test_a_borrowed_session_is_let_go_of_not_closed(server, config):
    home = Router(server.url, config, sticky=False)
    lent = home.lend()
    shared = lent.session
    lent._drop(shared)
    assert not shared.closed and lent._session is None
    home.close()


def test_a_dropped_hop_goes_back_to_the_manager(config):
    """A data server that vanishes mid-request sends the retry home."""
    with FakeServer() as manager, FakeServer(files={"/a": b"A"}) as ds:
        fs = xrdclient.FileSystem(manager.url, Config(**{**_plain(config), "retry_backoff": 0}))
        manager.redirects[c.kXR_stat] = (*ds.address, "")
        ds.handlers[c.kXR_stat] = _hang_up
        with pytest.raises(FileNotFoundError):
            # Home again, the manager (which has no /a) answers for itself.
            fs.stat("/a")
        assert manager.seen.count(c.kXR_stat) == 2
        fs.close()


def test_a_non_idempotent_request_is_not_retried_after_a_hop_fails(config):
    with FakeServer(dirs=["/"]) as manager, FakeServer(dirs=["/"]) as ds:
        fs = xrdclient.FileSystem(manager.url, config)
        manager.redirects[c.kXR_mkdir] = (*ds.address, "")
        ds.handlers[c.kXR_mkdir] = _hang_up
        with pytest.raises(TransientError):
            fs.mkdir("/x")
        fs.close()


def _plain(config: Config) -> dict[str, object]:
    return {
        "username": config.username,
        "auth_order": config.auth_order,
        "require_tls": False,
        "data_streams": 0,
    }


def _hang_up(conn, sid, params, body):  # type: ignore[no-untyped-def]
    """Cut the connection the way a crashed data server does."""
    conn.sock.shutdown(socket.SHUT_RDWR)
    conn.sock.close()
    return iter(())


# ---------------------------------------------------------------------------
# The redirect token (opaque CGI)
# ---------------------------------------------------------------------------


def test_each_hop_replaces_the_previous_hops_token(config):
    with (
        FakeServer(files={"/x": b"x"}) as last,
        FakeServer() as middle,
        FakeServer() as front,
        xrdclient.FileSystem(front.url, config) as fs,
    ):
        front.redirects[c.kXR_stat] = (*middle.address, "k=1")
        middle.redirects[c.kXR_stat] = (*last.address, "k=2")
        fs.stat("/x?mine=1")
        assert (c.kXR_stat, "/x?mine=1&k=2") in last.arguments


def test_a_hop_without_a_token_drops_the_previous_one():
    request = r.Stat("/x?mine=1")
    original = _path_fields(request)
    _retarget(request, original, "k=1")
    _retarget(request, original, "")
    assert request.path == "/x?mine=1"


def test_mv_carries_the_token_on_both_paths():
    request = r.Mv("/a", "/b?own=1")
    _retarget(request, _path_fields(request), "k=1")
    assert (request.src, request.dst) == ("/a?k=1", "/b?own=1&k=1")


def test_a_symlink_target_is_text_not_a_path():
    request = r.Symlink("../target", "/link")
    _retarget(request, _path_fields(request), "k=1")
    assert (request.src, request.dst) == ("../target", "/link?k=1")


@pytest.mark.parametrize("kind", [r.Statx, r.Prepare])
def test_many_path_requests_carry_the_token_on_each(kind):
    request = kind(["/a", "/b?own=1"])
    _retarget(request, _path_fields(request), "k=1")
    assert request.paths == ["/a?k=1", "/b?own=1&k=1"]


def test_a_request_by_handle_gets_no_token():
    request = r.Stat(fhandle=b"\x00\x00\x00\x01")
    _retarget(request, _path_fields(request), "k=1")
    assert request.path == ""


def test_a_redirected_rename_reaches_the_data_server_with_its_token(config):
    with (
        FakeServer(files={"/a": b"x"}) as ds,
        FakeServer() as manager,
        xrdclient.FileSystem(manager.url, config) as fs,
    ):
        manager.redirects[c.kXR_mv] = (*ds.address, "k=1")
        fs.rename("/a", "/b")
        assert "/b" in ds.files
        assert (c.kXR_mv, "/a?k=1 /b?k=1") in ds.arguments


# ---------------------------------------------------------------------------
# A negative port: the host field is a URL
# ---------------------------------------------------------------------------


def test_a_negative_port_redirect_names_a_url(config):
    with (
        FakeServer(files={"/f": b"hi"}) as target,
        FakeServer() as front,
        Router(front.url, config) as router,
    ):
        where = f"root://{target.address[0]}:{target.address[1]}/"
        front.redirects[c.kXR_stat] = (where, -1, "k=1")
        request = r.Stat("/f")
        router.execute(request, path="/f")
        assert router.endpoint == f"{target.address[0]}:{target.address[1]}"
        assert router.url.scheme == "root"
        assert request.path == "/f?k=1"


def test_a_negative_port_url_without_a_port_takes_the_default():
    target = rp.RedirectInfo("roots://ds.example.org/", -1)
    here = xrdclient.url.parse("root://mgr.example.org:2094//")
    moved = Router._destination(here, target)
    assert (moved.scheme, moved.host, moved.port) == ("roots", "ds.example.org", 1094)


def test_a_negative_port_to_another_protocol_is_refused():
    target = rp.RedirectInfo("https://ds.example.org:443/f", -1)
    here = xrdclient.url.parse("root://mgr.example.org//")
    with pytest.raises(ProtocolError, match="https"):
        Router._destination(here, target)


def test_a_negative_port_with_a_bare_host_is_read_as_xrootd():
    """XrdCl's ``URL::FromString`` takes a scheme-less string to be ``root://``."""
    here = xrdclient.url.parse("roots://mgr.example.org:2094//")
    moved = Router._destination(here, rp.RedirectInfo("ds.example.org", -1094))
    assert (moved.scheme, moved.host, moved.port) == ("root", "ds.example.org", 1094)
    moved = Router._destination(here, rp.RedirectInfo("ds.example.org:3094", -1))
    assert (moved.scheme, moved.host, moved.port) == ("root", "ds.example.org", 3094)


def test_a_negative_port_that_names_no_host_is_refused():
    here = xrdclient.url.parse("root://mgr.example.org//")
    with pytest.raises(ProtocolError, match="no host"):
        Router._destination(here, rp.RedirectInfo("/elsewhere", -1))


def test_a_negative_port_with_a_bare_host_is_followed(config):
    with (
        FakeServer(files={"/f": b"hi"}) as target,
        FakeServer() as front,
        Router(front.url, config) as router,
    ):
        front.redirects[c.kXR_stat] = (f"{target.address[0]}:{target.address[1]}", -1, "")
        request = r.Stat("/f")
        router.execute(request, path="/f")
        assert request.path == "/f"
        assert c.kXR_stat in target.seen


def test_a_negative_port_url_with_a_path_replaces_the_request_path(config):
    """XrdCl rewrites the request from the new URL's path (``RewriteCGIAndPath``)."""
    with (
        FakeServer(files={"/g": b"hi"}) as target,
        FakeServer() as front,
        Router(front.url, config) as router,
    ):
        where = f"root://{target.address[0]}:{target.address[1]}//g"
        front.redirects[c.kXR_stat] = (where, -1, "k=1")
        request = r.Stat("/f?mine=1")
        router.execute(request, path="/f")
        assert request.path == "/g?mine=1&k=1"
        assert (c.kXR_stat, "/g?mine=1&k=1") in target.arguments


def test_a_negative_port_path_moves_a_rename_destination(config):
    """For ``kXR_mv`` XrdCl rewrites the path after the space: the destination."""
    with (
        FakeServer(files={"/a": b"A"}) as target,
        FakeServer() as front,
        Router(front.url, config) as router,
    ):
        where = f"root://{target.address[0]}:{target.address[1]}//c"
        front.redirects[c.kXR_mv] = (where, -1, "")
        request = r.Mv("/a", "/b")
        router.execute(request, path="/a")
        assert (request.src, request.dst) == ("/a", "/c")


@pytest.mark.parametrize(
    ("host", "port", "path"),
    [
        ("root://ds//g/h", -1, "/g/h"),
        ("root://ds:3094//g", -1, "/g"),
        ("ds:3094//g", -1, "/g"),
        ("root://ds/", -1, ""),
        ("root://ds", -1, ""),
        ("ds", -1, ""),
        ("ds", 1094, ""),
        ("ds", 0, ""),
    ],
)
def test_only_a_url_naming_a_path_moves_the_request(host, port, path):
    assert _redirect_path(rp.RedirectInfo(host, port)) == path


def test_a_redirect_path_leaves_requests_without_one_alone():
    by_handle = {"path": ""}
    _repath(by_handle, "/g")
    assert by_handle == {"path": ""}
    many: dict[str, str | list[str]] = {"paths": ["/a", "/b"]}
    _repath(many, "/g")
    assert many == {"paths": ["/a", "/b"]}


def test_a_positive_port_keeps_the_scheme():
    target = rp.RedirectInfo("ds.example.org", 0)
    here = xrdclient.url.parse("roots://mgr.example.org:2094//")
    moved = Router._destination(here, target)
    assert (moved.scheme, moved.host, moved.port) == ("roots", "ds.example.org", 2094)


# ---------------------------------------------------------------------------
# Odds and ends
# ---------------------------------------------------------------------------


def test_a_hop_connection_that_cannot_be_pooled_is_closed(config, monkeypatch):
    closed: list[Session] = []
    real = Session.close

    def spy(self: Session) -> None:
        closed.append(self)
        real(self)

    monkeypatch.setattr(Session, "close", spy)
    no_pool = Config(**{**_plain(config), "pool_size": 0})
    with FakeServer() as manager, FakeServer(files={"/a": b"A"}) as ds:
        with xrdclient.FileSystem(manager.url, no_pool) as fs:
            manager.redirects[c.kXR_stat] = (*ds.address, "")
            fs.stat("/a")
            assert [s.endpoint for s in closed] == [f"{ds.address[0]}:{ds.address[1]}"]
    assert len(SESSIONS) == 0


def test_redirect_required_still_names_the_target():
    assert "redirected" in str(RedirectRequired(rp.RedirectInfo("h", 1094)))


def test_a_pinned_router_is_not_reconnected(server, config):
    with Router(server.url, config) as router:
        router.execute(r.Ping())
        pinned = router.pin()
        router.session.close()
        with pytest.raises(XrdConnectionError):
            pinned.execute(r.Ping())
