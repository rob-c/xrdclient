"""XrdCl behaviours the compatibility layer reproduces from native support.

Request timeouts that expire the request, ``FollowRedirects``, the host
list a callback is given, the ``LOCATE``/``MERGE``/``CHUNKED`` listings,
``ReadRecovery``, log masks and the environment keys. The first half pins
each down against :class:`~xrdclient.testing.FakeServer`; the second runs the
same calls through the official ``XRootD.client`` against a real ``xrootd``
(with a fake manager in front of it where a redirect is needed) and
requires the answers to match.
"""

from __future__ import annotations

import contextlib
import gc
import json
import logging
import os
import queue
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from xrdclient import _log
from xrdclient.compat import client
from xrdclient.compat.client import _channels, _convert, _dispatch, env
from xrdclient.compat.client.flags import DirListFlags
from xrdclient.config import Config
from xrdclient.proto import constants as c
from xrdclient.proto.responses import RedirectInfo
from xrdclient.session.router import Router, trace
from xrdclient.testing import FakeServer
from xrdclient.testing.server import frame

CONFIG = Config(username="tester", auth_order=("host",), require_tls=False, data_streams=0)

F = DirListFlags


@pytest.fixture(autouse=True)
def _test_config(monkeypatch):
    monkeypatch.setattr(env, "config", lambda: CONFIG)
    monkeypatch.setattr(env, "_request_timeout", 0)
    yield
    gc.collect()
    _channels.close_all()


@pytest.fixture
def data():
    files = {"/d/a": b"alpha", "/d/b": b"bee", "/d/c": b"sea!"}
    with FakeServer(files=files) as srv:
        yield srv


@pytest.fixture
def manager(data):
    with FakeServer(flags=c.kXR_isManager) as srv:
        yield srv


def _url(server: FakeServer) -> str:
    return f"root://{server.url.netloc}/"


def _redirect(manager: FakeServer, opcode: int, to: FakeServer, token: str = "tok=1") -> None:
    host, port = to.address
    manager.redirects[opcode] = (host, port, token)


def ok(pair: tuple[Any, Any]) -> Any:
    status, response = pair
    assert status.ok, status.message
    return response


class Heard:
    """A callback that keeps what it is called with."""

    def __init__(self) -> None:
        self.calls: queue.Queue[tuple[Any, Any, Any]] = queue.Queue()

    def __call__(self, status: Any, response: Any, hosts: Any) -> None:
        self.calls.put((status, response, hosts))

    def next(self) -> tuple[Any, Any, Any]:
        return self.calls.get(timeout=20)


def _hosts(hosts: Any) -> list[tuple[str, int, int, bool]]:
    return [(str(h.url), h.protocol, h.flags, h.load_balancer) for h in hosts]


def _slow(seconds: float):
    def handler(conn, sid, params, body):
        time.sleep(seconds)
        yield frame(sid, c.kXR_ok)

    return handler


# -- timeouts ----------------------------------------------------------------------


def test_a_callback_with_a_timeout_expires(data):
    data.handlers[c.kXR_ping] = _slow(2.5)
    heard = Heard()
    started = time.monotonic()
    assert client.FileSystem(_url(data)).ping(timeout=1, callback=heard).ok
    status, response, hosts = heard.next()
    assert time.monotonic() - started < 2.4
    assert (status.code, status.message, response) == (206, "[ERROR] Operation expired", None)
    # XrdCl's host list names the server even though it never answered.
    assert _hosts(hosts) == [(_url(data), 0, 0, False)]


def test_a_put_request_timeout_is_every_calls_deadline(data):
    fs = client.FileSystem(_url(data))
    ok(fs.ping())
    assert env.EnvPutInt("RequestTimeout", 1)
    try:
        assert not _dispatch.quick(0, None)
        data.handlers[c.kXR_dirlist] = _in_parts(b"a\n", b"b\x00")
        data.handlers[c.kXR_ping] = _slow(2.5)
        heard = Heard()
        fs.dirlist("/d", F.CHUNKED, callback=heard)
        assert heard.next()[0].ok  # a streaming call is given it too
        status, _ = fs.ping()
        assert status.code == 206
    finally:
        env.EnvDelInt("RequestTimeout")
    assert env.request_timeout() == 0 and _dispatch.quick(0, None)


def test_the_shells_request_timeout_counts_too(monkeypatch):
    monkeypatch.setenv("XRD_REQUESTTIMEOUT", "7")
    env._settle()
    assert env.request_timeout() == 7
    monkeypatch.delenv("XRD_REQUESTTIMEOUT")
    env._settle()
    assert env.request_timeout() == 0


# -- FollowRedirects ----------------------------------------------------------------


def test_a_filesystem_that_does_not_follow_answers_with_the_redirect(manager, data):
    fs = client.FileSystem(_url(manager))
    assert fs.set_property("FollowRedirects", "maybe")  # anything but "true" is false
    assert fs.get_property("FollowRedirects") == "false"
    _redirect(manager, c.kXR_stat, data)
    status, response = fs.stat("/d/a")
    host, port = data.address
    assert (status.status, status.code, status.shellcode, response) == (1, 401, 54, None)
    assert status.message == f"[ERROR] Unhandled redirect: root://{host}:{port}/?tok=1"
    assert c.kXR_stat not in data.seen
    assert fs.set_property("FollowRedirects", "true")
    _redirect(manager, c.kXR_stat, data)
    assert ok(fs.stat("/d/a")).size == 5


def test_a_file_that_does_not_follow_fails_its_open(manager, data):
    f = client.File()
    assert f.set_property("FollowRedirects", "false")
    _redirect(manager, c.kXR_open, data, token="")
    status, _ = f.open(_url(manager) + "/d/a")
    host, port = data.address
    assert (status.code, status.message) == (
        401,
        f"[ERROR] Unhandled redirect: root://{host}:{port}/",
    )
    assert f.open(_url(manager) + "/d/a")[0].code == 401  # a failed open stays failed


def test_a_files_properties_change_an_open_file(data):
    f = client.File()
    ok(f.open(_url(data) + "/d/a"))
    assert f.set_property("FollowRedirects", "false")
    assert f.native._router.follow_redirects is False
    assert f.set_property("ReadRecovery", "no")
    assert (f.get_property("ReadRecovery"), f.native.recover_handles) == ("false", False)
    assert f.set_property("WriteRecovery", "false") and f.get_property("WriteRecovery") == "false"
    assert not f.set_property("Nonsense", "true")
    ok(f.close())


def test_follow_redirects_over_http_is_only_a_property():
    fs = client.FileSystem("https://127.0.0.1:1/")
    assert fs.set_property("FollowRedirects", "false")
    assert fs.get_property("FollowRedirects") == "false"


@pytest.mark.parametrize(
    ("target", "url"),
    [
        (RedirectInfo("ds", 1095, "a=1"), "root://ds:1095/?a=1"),
        (RedirectInfo("ds", 0), "root://ds:1094/"),
        (RedirectInfo("root://ds:2000//store/f", -1, "x=2"), "root://ds:2000//store/f?x=2"),
    ],
)
def test_an_unfollowed_redirect_names_where_it_points_as_xrdcl_does(target, url):
    assert _convert.redirect_url(target) == url


# -- the host list -------------------------------------------------------------------


def test_a_callback_hears_every_server_the_request_went_through(manager, data):
    _redirect(manager, c.kXR_stat, data)
    heard = Heard()
    client.FileSystem(_url(manager)).stat("/d/a", callback=heard)
    status, response, hosts = heard.next()
    host, port = data.address
    assert status.ok and response.size == 5
    assert _hosts(hosts) == [
        (_url(manager), 0, c.kXR_isManager, True),
        (f"root://{host}:{port}//d/a?tok=1", 0, c.kXR_isServer, False),
    ]


def test_a_meta_manager_supersedes_the_first_load_balancer(data):
    with (
        FakeServer(flags=c.kXR_isManager) as first,
        FakeServer(flags=c.kXR_isManager | c.kXR_attrMeta) as meta,
        FakeServer(flags=c.kXR_isManager) as plain,
    ):
        _redirect(first, c.kXR_stat, meta)
        _redirect(meta, c.kXR_stat, data)
        heard = Heard()
        client.FileSystem(_url(first)).stat("/d/a", callback=heard)
        assert [h.load_balancer for h in heard.next()[2]] == [False, True, False]
        _redirect(first, c.kXR_stat, plain)
        _redirect(plain, c.kXR_stat, data)
        client.FileSystem(_url(first)).stat("/d/a", callback=heard)
        assert [h.load_balancer for h in heard.next()[2]] == [True, False, False]


def test_a_redirect_from_a_data_server_names_no_load_balancer(data):
    with FakeServer() as server:
        _redirect(server, c.kXR_stat, data)
        heard = Heard()
        client.FileSystem(_url(server)).stat("/d/a", callback=heard)
        assert [h.load_balancer for h in heard.next()[2]] == [False, False]


def test_a_redirect_back_to_the_same_server_is_one_host(data):
    _redirect(data, c.kXR_stat, data)
    heard = Heard()
    client.FileSystem(_url(data)).stat("/d/a", callback=heard)
    assert len(heard.next()[2].hosts) == 1


def test_a_retry_at_the_manager_is_a_host_again(manager, closed_port):
    host, port = closed_port
    manager.redirects[c.kXR_stat] = (host, port, "")
    manager.add_file("/d/a", b"alpha")
    from xrdclient.proto import requests as r

    router = Router(manager.url, CONFIG.evolve(retry_backoff=0, connect_retries=2), sticky=False)
    with trace() as hops:
        router.execute(r.Stat("/d/a"), path="/d/a")
    assert [(h.endpoint, h.redirected) for h in hops] == [
        (f"{manager.address[0]}:{manager.address[1]}", False),
        (f"{host}:{port}", True),
        (f"{manager.address[0]}:{manager.address[1]}", False),
    ]
    assert hops[1].flags == 0  # never answered
    router.close()


def test_a_file_names_the_open_url_then_the_data_server(manager, data):
    _redirect(manager, c.kXR_open, data)
    f = client.File()
    heard = Heard()
    opened = _url(manager) + "/d/a"
    assert f.open(opened, callback=heard).ok
    status, _, hosts = heard.next()
    host, port = data.address
    reached = f"root://{host}:{port}//d/a?tok=1"
    assert status.ok and [str(h.url) for h in hosts] == [str(client.URL(opened)), reached]
    assert (f.get_property("LastURL"), f.get_property("DataServer")) == (reached, f"{host}:{port}")
    f.read(callback=heard)  # off the bulk plane or not, it is the data server's answer
    assert _hosts(heard.next()[2]) == [(reached, 0, c.kXR_isServer, False)]
    ok(f.close())


def test_a_hop_names_the_path_its_request_named():
    from xrdclient.session.router import _base_path

    assert _base_path({"paths": ["/a"], "dst": "", "path": "/x?y=1"}) == "/x"
    assert _base_path({}) == _base_path(None) == ""


def test_a_nested_trace_shares_its_list():
    with trace() as outer, trace() as inner:
        assert outer is inner


# -- ReadRecovery ------------------------------------------------------------------------


@pytest.mark.parametrize("recover", ["true", "false"])
def test_read_recovery_decides_whether_a_lost_server_is_survived(data, recover):
    f = client.File()
    assert f.set_property("ReadRecovery", recover)
    ok(f.open(_url(data) + "/d/a"))
    assert ok(f.read(0, 5)) == b"alpha"
    data.disconnect()
    time.sleep(0.2)
    status, response = f.read(0, 5)
    if recover == "true":
        assert (status.ok, response, f.native.recoveries) == (True, b"alpha", 1)
    else:
        assert not status.ok and f.native.recoveries == 0


# -- dirlist ------------------------------------------------------------------------------


def _names(listing: Any) -> list[str]:
    return [entry.name for entry in listing.dirlist]


def test_merge_sorts_the_listing_and_drops_repeats(data):
    data.handlers[c.kXR_dirlist] = _in_parts(b"z\na\nz\nm\x00")
    fs = client.FileSystem(_url(data))
    assert _names(ok(fs.dirlist("/d"))) == ["z", "a", "z", "m"]
    assert _names(ok(fs.dirlist("/d", F.MERGE))) == ["a", "m", "z"]


def test_chunked_without_a_callback_is_refused(data):
    status, response = client.FileSystem(_url(data)).dirlist("/d", F.CHUNKED | F.STAT)
    assert (status.code, status.message, response) == (13, "[ERROR] Operation not supported", None)


def _in_parts(*parts: bytes):
    def handler(conn, sid, params, body):
        for piece in parts[:-1]:
            yield frame(sid, c.kXR_oksofar, piece)
        yield frame(sid, c.kXR_ok, parts[-1])

    return handler


def _heard_parts(fs: Any, flags: int) -> list[tuple[int, str, list[str], bool]]:
    heard = Heard()
    assert fs.dirlist("/d", flags, callback=heard).ok
    seen = []
    while True:
        status, listing, hosts = heard.next()
        stat = bool(listing.dirlist) and listing.dirlist[0].statinfo is not None
        seen.append((status.code, status.message, _names(listing), stat))
        assert hosts.hosts and listing.parent == "/d/"
        if status.code != 1:
            return seen


def test_chunked_hands_each_part_to_the_callback(data):
    data.handlers[c.kXR_dirlist] = _in_parts(b"z\ny\n", b"x\ny\n", b"w\x00")
    fs = client.FileSystem(_url(data))
    assert _heard_parts(fs, F.CHUNKED) == [
        (1, "[SUCCESS] Continue", ["z", "y"], False),
        (1, "[SUCCESS] Continue", ["x", "y"], False),
        (0, "[SUCCESS] ", ["w"], False),
    ]
    # Merged: each part sorted, less what an earlier part already had.
    assert [names for _, _, names, _ in _heard_parts(fs, F.CHUNKED | F.MERGE)] == [
        ["y", "z"],
        ["x"],
        ["w"],
    ]


def test_chunked_with_stat_reads_every_part_as_stat_lines(data):
    line = b"1 5 0 1700000000"
    data.handlers[c.kXR_dirlist] = _in_parts(
        b".\n0 0 0 0\na\n" + line + b"\n", b"b\n" + line + b"\n", b"c\n" + line + b"\x00"
    )
    parts = _heard_parts(client.FileSystem(_url(data)), F.CHUNKED | F.STAT)
    assert [(names, stat) for _, _, names, stat in parts] == [
        (["a"], True),
        (["b"], True),
        (["c"], True),
    ]


def test_chunked_on_a_listing_in_one_piece_is_one_final_answer(data):
    parts = _heard_parts(client.FileSystem(_url(data)), F.CHUNKED)
    assert parts == [(0, "[SUCCESS] ", ["a", "b", "c"], False)]


def test_chunked_on_an_empty_answer_is_an_empty_listing(data):
    data.handlers[c.kXR_dirlist] = _in_parts(b"")
    assert _heard_parts(client.FileSystem(_url(data)), F.CHUNKED) == [(0, "[SUCCESS] ", [], False)]


def test_chunked_recursive_is_delivered_whole(data):
    parts = _heard_parts(client.FileSystem(_url(data)), F.CHUNKED | F.RECURSIVE)
    assert parts == [(0, "[SUCCESS] ", ["a", "b", "c"], True)]


def test_a_chunked_failure_reaches_the_callback(data):
    heard = Heard()
    client.FileSystem(_url(data)).dirlist("/nope", F.CHUNKED, callback=heard)
    status, response, _ = heard.next()
    assert (status.code, status.errno, response) == (400, 3011, None)


def test_a_callback_that_lists_again_from_inside_a_part_does_not_deadlock(data):
    data.handlers[c.kXR_dirlist] = _in_parts(b"z\n", b"y\x00")
    fs = client.FileSystem(_url(data))
    answers: queue.Queue[Any] = queue.Queue()

    def again(status: Any, response: Any, hosts: Any) -> None:
        answers.put((status.code, fs.stat("/d/a")[0].ok))

    fs.dirlist("/d", F.CHUNKED, callback=again)
    assert [answers.get(timeout=20) for _ in range(2)] == [(1, True), (0, True)]


def test_an_invalid_filesystem_refuses_a_chunked_listing_as_any_other():
    heard = Heard()
    assert client.FileSystem("root://").dirlist("/d", F.CHUNKED, callback=heard).ok
    assert heard.next()[0].code == 13


def test_a_listing_names_the_server_that_answered_it(manager, data):
    _redirect(manager, c.kXR_dirlist, data)
    listing = ok(client.FileSystem(_url(manager)).dirlist("/d"))
    host, port = data.address
    assert {entry.hostaddr for entry in listing.dirlist} == {f"{host}:{port}"}


def _locating(manager: FakeServer, *where: str) -> None:
    def locate(conn, sid, params, body):
        assert body.startswith(b"*")  # asked where it could be, as XrdCl asks
        yield frame(sid, c.kXR_ok, " ".join(where).encode() + b"\x00")

    manager.handlers[c.kXR_locate] = locate


def test_locate_lists_on_every_server_and_merge_folds_them(manager, data):
    with FakeServer(files={"/d/b": b"bee", "/d/z": b"zed"}) as other:
        a, b = (f"{s.address[0]}:{s.address[1]}" for s in (data, other))
        _locating(manager, f"Sr{a}", f"Sw{b}")
        fs = client.FileSystem(_url(manager))
        together = ok(fs.dirlist("/d", F.LOCATE))
        assert [(e.name, e.hostaddr) for e in together.dirlist] == [
            ("a", a),
            ("b", a),
            ("c", a),
            ("b", b),
            ("z", b),
        ]
        assert together.parent == "/d/" and together.size == 5
        merged = ok(fs.dirlist("/d", F.LOCATE | F.MERGE))
        assert [(e.name, e.hostaddr) for e in merged.dirlist] == [
            ("a", a),
            ("b", a),
            ("c", a),
            ("z", b),
        ]


def test_locate_with_some_servers_failing_is_partial(manager, data, closed_port):
    a = f"{data.address[0]}:{data.address[1]}"
    dead = f"{closed_port[0]}:{closed_port[1]}"
    _locating(manager, f"Sr{a}", f"Sr{dead}")
    status, listing = client.FileSystem(_url(manager)).dirlist("/d", F.LOCATE)
    assert (status.status, status.code, status.ok, status.message) == (0, 3, True, "[SUCCESS] ")
    assert _names(listing) == ["a", "b", "c"]
    _locating(manager, f"Sr{dead}")
    status, listing = client.FileSystem(_url(manager)).dirlist("/d", F.LOCATE)
    assert (status.code, status.fatal) == (108, True)
    assert (listing.parent, listing.dirlist) == ("/d/", [])


def test_locate_finding_nothing_says_so_as_xrdcl_does(manager):
    _locating(manager)
    status, listing = client.FileSystem(_url(manager)).dirlist("/d", F.LOCATE)
    assert (status.code, status.errno, listing) == (400, 3011, None)
    assert status.message == (
        "[ERROR] Server responded with an error: [3011] No valid location found\n"
    )


def test_locate_that_fails_at_the_manager_is_that_failure(manager):
    status, _ = client.FileSystem(_url(manager)).dirlist("/nope", F.LOCATE)
    assert (status.code, status.errno) == (400, 3011)


def test_locate_on_a_data_server_is_a_plain_listing(data):
    fs = client.FileSystem(_url(data))
    assert _names(ok(fs.dirlist("/d", F.LOCATE))) == ["a", "b", "c"]
    assert c.kXR_locate not in data.seen


def test_locate_with_a_callback_is_not_done(manager, data):
    manager.add_file("/d/m", b"")
    heard = Heard()
    client.FileSystem(_url(manager)).dirlist("/d", F.LOCATE, callback=heard)
    status, listing, _ = heard.next()
    assert status.ok and _names(listing) == ["m"] and c.kXR_locate not in manager.seen


def test_locate_on_an_invalid_filesystem_is_refused():
    status, _ = client.FileSystem("root://").dirlist("/d", F.LOCATE)
    assert status.code == 13


def test_locate_over_http_is_a_plain_listing(monkeypatch):
    class Web:
        """A filesystem with no XRootD router, as over HTTP."""

        endpoint = "web:443"

        def scandir(self, path: str, stat: bool) -> list[Any]:
            return []

        def close(self) -> None:
            pass

    fs = client.FileSystem("root://127.0.0.1:1/")
    monkeypatch.setattr(fs, "native", Web())
    listing = ok(fs.dirlist("/d", F.LOCATE))
    assert (listing.parent, listing.dirlist) == ("/d/", [])


# -- log masks -------------------------------------------------------------------------------


class _Kept(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.names: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.names.append(record.name)


@pytest.fixture
def kept(monkeypatch):
    monkeypatch.setattr(_log, "_muted", {})
    root = logging.getLogger("xrdclient")
    before = root.level
    root.setLevel(logging.DEBUG)  # through setLevel, which clears the loggers' caches
    handler = _Kept()
    root.addHandler(handler)
    yield handler
    root.removeHandler(handler)
    root.setLevel(before)


def _emit(level: int = logging.DEBUG) -> None:
    for name in ("xrdclient.session.sync", "xrdclient.client.file", "xrdclient.transport.sync"):
        _log.get_logger(name).log(level, "hello")


def test_a_log_mask_keeps_only_its_topics_at_its_level(kept):
    client.SetLogMask("Debug", "FileMsg|XRootDMsg")
    _emit()
    _emit(logging.INFO)
    assert kept.names == [
        "xrdclient.session.sync",
        "xrdclient.client.file",
        "xrdclient.session.sync",
        "xrdclient.client.file",
        "xrdclient.transport.sync",
    ]


@pytest.mark.parametrize(
    ("mask", "left"),
    [
        ("All|^FileMsg", ["xrdclient.session.sync", "xrdclient.transport.sync"]),
        ("None", []),
        ("", ["xrdclient.session.sync", "xrdclient.client.file", "xrdclient.transport.sync"]),
        ("None|AsyncSockMsg|ZipMsg|^Nonsense", ["xrdclient.transport.sync"]),
    ],
)
def test_a_log_mask_is_read_as_xrdcl_reads_it(kept, mask, left):
    client.SetLogMask("All", mask)
    _emit(logging.WARNING)
    assert kept.names == left


def test_a_log_mask_for_a_level_xrdcl_lacks_is_ignored(kept):
    client.SetLogMask("Chatty", "None")
    _emit()
    assert len(kept.names) == 3


def test_every_topic_names_loggers_under_this_package():
    for bit, loggers in env.TOPICS.values():
        assert bit and all(name.startswith("xrdclient.") for name in loggers)


def test_unmuting_forgets_the_band(monkeypatch):
    monkeypatch.setattr(_log, "_muted", {})
    _log.mute(10, 20, ["xrdclient.a"])
    _log.mute(10, 20, [])
    assert _log._muted == {}


# -- environment keys -----------------------------------------------------------------------


def test_every_registered_key_says_what_it_does():
    assert set(env.EFFECTS) == set(env._INTS) | set(env._STRINGS)
    assert all(env.EFFECTS[key] for key in env.EFFECTS)


def test_a_put_to_a_key_xrdcl_does_not_register_ignores_the_shell(monkeypatch):
    monkeypatch.setattr(env, "_ints", {})
    monkeypatch.setenv("XRD_NOSUCHKEY", "4")
    assert env.EnvPutInt("NoSuchKey", 3) is True
    assert env.EnvGetInt("NoSuchKey") == 3
    assert env.EnvGetDefault("NoSuchKey") is None


# -- parity with the official bindings -----------------------------------------------------


@pytest.fixture
def official():
    return pytest.importorskip("XRootD.client", reason="the official bindings are not installed")


def _plain(text: str) -> str:
    """A URL or message with XrdCl's per-open request id taken out."""
    return re.sub(r"[?&]?xrdcl\.requuid=[0-9A-F-]+&?", "", text)


def _callback_answer(call: Any) -> tuple[Any, Any, Any]:
    heard = Heard()
    call(heard)
    return heard.next()


@pytest.fixture
def fronted(real_server, sandbox, monkeypatch):
    """A fake manager in front of the real daemon, and a file behind it."""
    from conftest import _REAL_CONFIG

    monkeypatch.setattr(env, "config", lambda: _REAL_CONFIG)
    Path(sandbox, "f").write_bytes(b"payload")
    with FakeServer(flags=c.kXR_isManager) as mgr:
        yield mgr, real_server, f"{sandbox}/f"


def _point(mgr: FakeServer, real: Any, *opcodes: int) -> None:
    for opcode in opcodes:
        mgr.redirects[opcode] = ("127.0.0.1", real.port, "tok=1")


@pytest.mark.interop
@pytest.mark.parity
def test_a_redirected_stat_hears_the_same_hosts(official, fronted):
    mgr, real, path = fronted
    answers = []
    for module in (official, client):
        _point(mgr, real, c.kXR_stat)
        fs = module.FileSystem(_url(mgr))
        status, info, hosts = _callback_answer(lambda cb, fs=fs: fs.stat(path, callback=cb))
        answers.append((status.code, info.size, _hosts(hosts)))
    assert answers[0] == answers[1]
    assert answers[1][2][0][3] is True  # the manager is the load balancer


@pytest.mark.interop
@pytest.mark.parity
def test_an_unfollowed_redirect_is_the_same_answer(official, fronted):
    mgr, real, path = fronted
    answers = []
    for module in (official, client):
        fs = module.FileSystem(_url(mgr))
        fs.set_property("FollowRedirects", "no")
        _point(mgr, real, c.kXR_stat, c.kXR_open)
        stat = fs.stat(path)
        f = module.File()
        f.set_property("FollowRedirects", "false")
        opened = f.open(_url(mgr) + path)
        answers.append(
            [
                fs.get_property("FollowRedirects"),
                *[(s.status, s.code, s.shellcode, _plain(s.message), r) for s, r in (stat, opened)],
            ]
        )
    assert answers[0] == answers[1]


@pytest.mark.interop
@pytest.mark.parity
def test_a_redirected_open_is_the_same_journey(official, fronted):
    mgr, real, path = fronted
    answers = []
    for module in (official, client):
        _point(mgr, real, c.kXR_open)
        f = module.File()
        status, _, opened = _callback_answer(lambda cb, f=f: f.open(_url(mgr) + path, callback=cb))
        _, data, read = _callback_answer(lambda cb, f=f: f.read(callback=cb))
        answers.append(
            (
                status.ok,
                [(_plain(u), p, fl, lb) for u, p, fl, lb in _hosts(opened)],
                data,
                [(_plain(u), p, fl, lb) for u, p, fl, lb in _hosts(read)],
                f.get_property("DataServer"),
                _plain(f.get_property("LastURL")),
            )
        )
        f.close()
    assert answers[0] == answers[1]


def _order(names: list[str], flags: int) -> list[str]:
    """A merged listing's order is XrdCl's to decide; a plain one's is the server's."""
    return names if flags & F.MERGE else sorted(names)


def _listings(module: Any, url: str, path: str) -> list[Any]:
    fs = module.FileSystem(url)
    out: list[Any] = []
    for flags in (0, F.MERGE, F.LOCATE, F.LOCATE | F.MERGE, F.CHUNKED, F.MERGE | F.STAT):
        status, listing = fs.dirlist(path, flags)
        out.append(
            (
                status.code,
                status.message,
                listing and (listing.parent, _order(_names(listing), flags)),
                listing and {e.hostaddr for e in listing.dirlist},
            )
        )
    for flags in (F.CHUNKED, F.CHUNKED | F.STAT, F.CHUNKED | F.MERGE, F.LOCATE):
        heard = Heard()
        fs.dirlist(path, flags, callback=heard)
        parts = []
        while True:
            status, listing, _ = heard.next()
            parts.append((status.code, status.message, listing.size, _names(listing)[:3]))
            if status.code != 1:
                break
        out.append(parts)
    return out


@pytest.mark.interop
@pytest.mark.parity
def test_listings_by_flag_are_the_bindings_listings(official, real_server, sandbox, monkeypatch):
    from conftest import _REAL_CONFIG

    monkeypatch.setattr(env, "config", lambda: _REAL_CONFIG)
    big = Path(sandbox, "big")
    big.mkdir()
    for i in range(3000):
        (big / f"file_with_a_rather_long_name_{i:05d}").touch()
    ours = _listings(client, real_server.url, str(big))
    theirs = _listings(official, real_server.url, str(big))
    assert ours == theirs
    assert len(ours[-4]) > 2  # the listing did come in parts


def _local_root(base: Path, name: str, files: dict[str, bytes]) -> Any:
    """A real daemon whose ``/data`` lives under a root of its own."""
    import _xrootd

    root = base / name
    for rel, payload in files.items():
        (root / "data" / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / "data" / rel).write_bytes(payload)

    class LocalRoot(_xrootd.RealServer):
        def start(self) -> Any:
            self._config.write_text(
                _xrootd.CONFIG.format(port=self.port, root="/data", admin=self._admin)
                + f"oss.localroot {root}\n"
            )
            with self._log.open("wb") as handle:
                self._proc = subprocess.Popen(
                    [str(_xrootd.XROOTD), "-c", str(self._config), "-n", name],
                    cwd=str(self._admin),
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                )
            self._wait()
            return self

    return LocalRoot(root)


def _started(stack: contextlib.ExitStack, make: Any) -> Any:
    """A daemon started, trying again on a fresh port if another process took its one."""
    for attempt in range(3):
        server = make()
        try:
            server.start()
        except RuntimeError:
            if attempt == 2:
                raise
            continue
        stack.callback(server.stop)
        return server
    raise AssertionError("unreachable")  # pragma: no cover


def _described(entry: Any) -> tuple[str, str, int | None]:
    return entry.name, entry.hostaddr, entry.statinfo and entry.statinfo.size


@pytest.mark.interop
@pytest.mark.parity
def test_a_located_merged_listing_is_the_bindings(official, real_server, tmp_path, monkeypatch):
    from conftest import _REAL_CONFIG

    monkeypatch.setattr(env, "config", lambda: _REAL_CONFIG)
    with contextlib.ExitStack() as stack:
        first = _started(stack, lambda: _local_root(tmp_path, "one", {"d/x": b"1", "d/y": b"22"}))
        second = _started(stack, lambda: _local_root(tmp_path, "two", {"d/y": b"22", "d/z": b"3"}))
        mgr = stack.enter_context(FakeServer(flags=c.kXR_isManager))
        _locating(mgr, f"Sr127.0.0.1:{first.port}", f"Sr127.0.0.1:{second.port}")
        answers = []
        for module in (official, client):
            fs = module.FileSystem(_url(mgr))
            seen = []
            for flags in (F.LOCATE, F.LOCATE | F.MERGE, F.LOCATE | F.MERGE | F.STAT):
                status, listing = fs.dirlist("/data/d", flags)
                seen.append(
                    (
                        status.code,
                        listing.parent,
                        [_described(entry) for entry in listing.dirlist],
                    )
                )
            answers.append(seen)
        assert answers[0] == answers[1]
        assert [name for name, _, _ in answers[1][1][2]] == ["x", "y", "z"]


_OFFICIAL_TIMEOUT = """
import json, sys, threading
from XRootD import client
fs = client.FileSystem(sys.argv[1])
status, _ = fs.stat("/d/a", timeout=1)
done = threading.Event()
heard = []
def cb(st, resp, hosts):
    heard.append((st.code, st.message, [(str(h.url), h.protocol, h.flags, h.load_balancer)
                                        for h in hosts]))
    done.set()
fs.stat("/d/a", timeout=1, callback=cb)
done.wait(30)
print(json.dumps([[status.status, status.code, status.message], heard[0]]))
"""


@pytest.mark.parity
def test_an_expired_request_is_reported_as_the_bindings_report_it(official, data):
    def never(conn, sid, params, body):
        time.sleep(3)
        yield frame(sid, c.kXR_ok, b"0 5 0 0\x00")

    data.handlers[c.kXR_stat] = never
    url = _url(data)
    run = subprocess.run(
        [sys.executable, "-c", _OFFICIAL_TIMEOUT, url],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "XRD_TIMEOUTRESOLUTION": "1"},
    )
    theirs = json.loads(run.stdout)
    fs = client.FileSystem(url)
    status, _ = fs.stat("/d/a", timeout=1)
    heard, _, hosts = _callback_answer(lambda cb: fs.stat("/d/a", timeout=1, callback=cb))
    ours = [
        [status.status, status.code, status.message],
        [heard.code, heard.message, _hosts(hosts)],
    ]
    assert json.loads(json.dumps(ours)) == theirs


_OFFICIAL_ENV = """
import json, sys
from XRootD.client import env
keys = sys.argv[1:]
print(json.dumps([[env.EnvGetInt(k), env.EnvGetString(k), env.EnvGetDefault(k)] for k in keys]))
"""


@pytest.mark.parity
def test_every_key_reads_as_the_bindings_read_it(official, monkeypatch):
    for key in list(os.environ):
        if key.startswith("XRD_"):
            monkeypatch.delenv(key)
    monkeypatch.setattr(env, "_ints", {})
    monkeypatch.setattr(env, "_strings", {})
    keys = [
        *env._INTS,
        *env._STRINGS,
        "PollerPreference",
        "ClConfDir",
        "DefaultClConfFile",
        "TCPKeepAliveProbes",
        "requesttimeout",
        "NoSuchKey",
    ]
    run = subprocess.run(
        [sys.executable, "-c", _OFFICIAL_ENV, *keys],
        capture_output=True,
        text=True,
        timeout=120,
        env=dict(os.environ),
    )
    theirs = json.loads(run.stdout)
    ours = [[env.EnvGetInt(k), env.EnvGetString(k), env.EnvGetDefault(k)] for k in keys]
    assert ours == theirs


def test_a_file_whose_connection_has_gone_still_names_its_server(data):
    f = client.File()
    ok(f.open(_url(data) + "/d/a"))
    session, f.native._router._session = f.native._router._session, None
    try:
        hosts = f._File__hosts([])  # type: ignore[attr-defined]
        assert [(h.protocol, h.flags) for h in hosts] == [(0, 0)]
    finally:
        f.native._router._session = session
    ok(f.close())
