"""The filesystem family: one base, three schemes, and what they share.

:class:`~xrdclient.client.BaseFileSystem` holds the helpers written in terms
of the protocol operations; ``root://``, WebDAV and S3 implement those
operations as siblings. ``FileSystem(url)`` still picks the implementation,
and ``isinstance(fs, FileSystem)`` still holds for every one of them. The
cancel and raw-listing forms of the protocol calls, and :meth:`abort`, are
checked here too, on the wire, against the in-tree servers.
"""

from __future__ import annotations

import errno
import threading
import time
from collections.abc import Callable

import pytest

import xrdclient
from xrdclient.client import BaseFileSystem, FileSystem
from xrdclient.config import Config
from xrdclient.errors import AbortedError, UnsupportedError
from xrdclient.http import HTTPClient, HTTPFileSystem
from xrdclient.proto import constants as c
from xrdclient.s3 import Credentials, S3FileSystem
from xrdclient.testing import FakeDAVServer, FakeS3Server, FakeServer

CONFIG = Config(request_timeout=30, connect_retries=0)

#: How long an aborted call may take to notice. Generous for a loaded
#: machine; the point is that it is nowhere near the request timeout.
PROMPT = 10.0


@pytest.fixture
def server():
    with FakeServer(files={"/d/a.root": b"abc", "/d/b.root": b"defg"}, dirs=["/d/sub"]) as srv:
        yield srv


@pytest.fixture
def fs(server):
    with FileSystem(server.url.with_path("/d"), CONFIG) as filesystem:
        yield filesystem


@pytest.fixture
def dav():
    with FakeDAVServer(files={"/d/a.root": b"abc"}, dirs=["/d/sub"]) as srv:
        yield srv


# ---------------------------------------------------------------------------
# The hierarchy
# ---------------------------------------------------------------------------


def test_the_base_is_abstract():
    with pytest.raises(TypeError, match="abstract"):
        # Instantiating the abstract base is exactly the refusal under test.
        BaseFileSystem()  # type: ignore[abstract]


def test_root_is_the_filesystem_class_itself(fs):
    assert type(fs) is FileSystem
    assert isinstance(fs, BaseFileSystem)


def test_webdav_is_a_sibling_that_still_reads_as_a_filesystem(dav):
    with xrdclient.FileSystem(dav.url, CONFIG) as fs:
        assert isinstance(fs, HTTPFileSystem) and type(fs) is HTTPFileSystem
        assert isinstance(fs, FileSystem) and isinstance(fs, BaseFileSystem)
        # Registered, not inherited: none of the binary protocol's code is in it.
        assert FileSystem not in type(fs).__mro__
        assert issubclass(HTTPFileSystem, FileSystem)
        assert fs.read_bytes("/d/a.root") == b"abc"


def test_s3_is_still_a_webdav_filesystem_and_so_a_filesystem():
    with FakeS3Server(objects={"k": b"v"}) as bucket:
        fs = xrdclient.FileSystem(
            bucket.url, CONFIG, credentials=Credentials("a", "b"), endpoint=bucket.endpoint
        )
        with fs:
            assert isinstance(fs, S3FileSystem) and type(fs) is S3FileSystem
            assert isinstance(fs, HTTPFileSystem) and isinstance(fs, FileSystem)
            assert issubclass(S3FileSystem, FileSystem)


def test_a_root_subclass_is_built_as_itself(server):
    class Mine(FileSystem):
        pass

    with Mine(server.url, CONFIG) as fs:
        assert type(fs) is Mine
        assert fs.exists("/d/a.root")


def test_the_shared_helpers_come_from_the_base(fs, dav):
    with xrdclient.FileSystem(dav.url, CONFIG) as web:
        for name in ("walk", "glob", "rmtree", "list_archive", "read_text", "exists"):
            assert getattr(type(fs), name) is getattr(BaseFileSystem, name)
            assert getattr(type(web), name) is getattr(BaseFileSystem, name)


def test_the_spellings_follow_whatever_a_subclass_overrides(dav):
    with xrdclient.FileSystem(dav.url, CONFIG) as fs:
        # Once ``hardlink = link`` bound the root:// link here, and failed on
        # the router a WebDAV filesystem does not have.
        with pytest.raises(UnsupportedError, match="link"):
            fs.hardlink("/d/a.root", "/d/b.root")
        fs.move("/d/a.root", "/d/c.root")
        fs.unlink("/d/c.root")
        assert not fs.exists("/d/c.root")


def test_a_webdav_endpoint_has_no_raw_listing(dav):
    with xrdclient.FileSystem(dav.url, CONFIG) as fs, pytest.raises(UnsupportedError):
        fs.dirlist("/d")


# ---------------------------------------------------------------------------
# Raw listing
# ---------------------------------------------------------------------------


def test_dirlist_is_the_answer_scandir_parses(fs, server):
    raw = fs.dirlist(".")
    assert raw.startswith(b".\n")
    lines = [line for line in raw.rstrip(b"\x00").decode().split("\n") if line]
    names = lines[2::2]
    assert sorted(names) == ["a.root", "b.root", "sub"]
    assert server.seen[-1] == c.kXR_dirlist


def test_dirlist_without_stat_is_names_alone(fs):
    raw = fs.dirlist("/d", stat=False)
    assert sorted(raw.rstrip(b"\x00").decode().split()) == ["a.root", "b.root", "sub"]


# ---------------------------------------------------------------------------
# Cancelling a staging request
# ---------------------------------------------------------------------------


def test_prepare_cancel_sends_the_request_id_then_the_paths(fs, server):
    assert fs.prepare(["a.root", "b.root"], cancel=True, reqid="req-7") == ""
    assert server.cancelled_prepares[-1] == "req-7\n/d/a.root\n/d/b.root"
    assert server.prepare_options[-1][0] == c.kXR_cancel


def test_cancel_prepare_takes_the_paths_too(fs, server):
    fs.cancel_prepare("req-8", ["a.root"])
    assert server.cancelled_prepares[-1] == "req-8\n/d/a.root"
    fs.cancel_prepare("req-9")
    assert server.cancelled_prepares[-1] == "req-9"


def test_extra_prepare_flags_ride_along_with_a_cancel(fs, server):
    fs.prepare([], cancel=True, reqid="req-1", flags="notify")
    assert server.prepare_options[-1][0] == c.kXR_cancel | c.kXR_notify


@pytest.mark.parametrize(
    "kwargs",
    [{"cancel": True}, {"cancel": True, "reqid": ""}, {"reqid": "req-1"}],
    ids=["no-reqid", "empty-reqid", "reqid-without-cancel"],
)
def test_a_cancel_must_name_its_request(fs, server, kwargs):
    with pytest.raises(ValueError, match="reqid"):
        fs.prepare(["a.root"], **kwargs)
    assert c.kXR_prepare not in server.seen


def test_a_webdav_cancel_goes_to_the_tape_api(dav):
    with xrdclient.FileSystem(dav.url, CONFIG) as fs:
        handle = fs.prepare(["/d/a.root"])
        fs.cancel_prepare(handle, ["/d/a.root"])
        assert (handle, "/d/a.root") in dav.cancelled
        assert handle in dav.staged  # only those files were withdrawn
        assert fs.prepare([], cancel=True, reqid=handle) == ""
        assert handle not in dav.staged
        with pytest.raises(ValueError, match="reqid"):
            fs.prepare(["/d/a.root"], reqid=handle)


def test_s3_refuses_a_cancel_as_it_refuses_staging():
    with FakeS3Server() as bucket:
        fs = S3FileSystem(bucket.url, CONFIG, credentials=Credentials("a", "b"))
        with fs:
            with pytest.raises(UnsupportedError):
                fs.prepare(["k"], cancel=True, reqid="r")
            with pytest.raises(UnsupportedError):
                fs.cancel_prepare("r", ["k"])


# ---------------------------------------------------------------------------
# Abort
# ---------------------------------------------------------------------------


def _in_thread(call: Callable[[], object]) -> tuple[threading.Thread, list[object]]:
    """Run ``call`` in a thread; the box holds its outcome once it finishes."""
    box: list[object] = []

    def run() -> None:
        try:
            box.append(call())
        except BaseException as exc:
            box.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker, box


def _abort_while_blocked(
    fs: BaseFileSystem, call: Callable[[], object], entered: threading.Event
) -> object:
    worker, box = _in_thread(call)
    assert entered.wait(PROMPT), "the request never reached the server"
    started = time.monotonic()
    fs.abort()
    worker.join(PROMPT)
    assert not worker.is_alive(), "the abort did not wake the blocked call"
    assert time.monotonic() - started < PROMPT
    return box[0]


def test_abort_wakes_a_call_blocked_on_a_silent_server(fs, server):
    entered, release = threading.Event(), threading.Event()

    def silent(conn, sid, params, body):
        entered.set()
        release.wait(PROMPT * 3)
        return
        yield

    server.handlers[c.kXR_stat] = silent
    try:
        failure = _abort_while_blocked(fs, lambda: fs.stat("a.root"), entered)
    finally:
        release.set()
    assert isinstance(failure, AbortedError)
    assert failure.errno == errno.ECANCELED
    # Spent: every later call is refused at once, and asking again is harmless.
    with pytest.raises(AbortedError):
        fs.listdir()
    fs.abort()
    with pytest.raises(AbortedError):
        fs.ping()


def test_an_idle_filesystem_aborted_refuses_what_follows(fs):
    assert fs.exists("a.root")
    fs.abort()
    with pytest.raises(AbortedError) as caught:
        fs.stat("a.root")
    assert isinstance(caught.value, OSError)
    fs.close()  # still releases cleanly


def test_abort_wakes_a_blocked_webdav_request(dav):
    entered, release = threading.Event(), threading.Event()

    def silent(method, path, headers):
        entered.set()
        release.wait(PROMPT * 3)
        return None

    fs = xrdclient.FileSystem(dav.url, CONFIG)
    try:
        assert fs.exists("/d/a.root")  # a pooled connection, then a stall on it
        dav.handlers["PROPFIND"] = silent
        failure = _abort_while_blocked(fs, lambda: fs.stat("/d/a.root"), entered)
    finally:
        release.set()
        fs.close()
    assert isinstance(failure, AbortedError)
    with pytest.raises(AbortedError):
        fs.read_bytes("/d/a.root")
    fs.abort()


def test_an_aborted_webdav_read_of_a_body_is_an_abort(dav, monkeypatch):
    fs = xrdclient.FileSystem(dav.url, CONFIG)
    try:
        real = HTTPClient.open

        def open_then_abort(self, *args, **kwargs):
            response = real(self, *args, **kwargs)
            self.abort()  # between the headers and the body
            return response

        monkeypatch.setattr(HTTPClient, "open", open_then_abort)
        with pytest.raises(AbortedError):
            fs.stat("/d/a.root")
    finally:
        fs.close()


def test_aborting_a_client_with_a_connection_never_opened_is_harmless(dav):
    client = HTTPClient(CONFIG)
    client.abort()
    with pytest.raises(AbortedError):
        client.request("HEAD", dav.url.with_path("/d/a.root"))
    client.close()
