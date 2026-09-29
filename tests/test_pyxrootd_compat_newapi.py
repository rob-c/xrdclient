"""The newer ``XRootD.client`` API, on the compat layer.

Upstream PyXRootD grew typed exceptions (``XRootDError`` and friends,
``status.exception()``, ``raise_on_error``) and a WLCG Tape REST client
(``XRootD.client.tape.TapeClient``) after the 6.1 release installed here.
The first half of this file is upstream's own ``tests/test_responses.py``
and ``tests/test_tape.py`` expectations, pinned against compat; the second
drives the same :class:`TapeClient` end to end against the fake WebDAV
server's tape API and the fake ``root://`` server, and checks the helper
names (``flags.enum``, ``utils.CallbackWrapper``, ``finalize``) against the
installed bindings wherever those have them.
"""

from __future__ import annotations

import gc
import json
import sys
from typing import Any, ClassVar

import pytest

import xrdclient
from xrdclient.compat import client
from xrdclient.compat.client import _channels, _status, env, tape
from xrdclient.compat.client.flags import PrepareFlags, QueryCode, enum
from xrdclient.compat.client.responses import (
    HostInfo,
    HostList,
    TapeArchiveInfo,
    TapeEndpoint,
    TapeStageFileStatus,
    TapeStageResponse,
    TapeStageStatus,
    XRootDAuthorizationError,
    XRootDChecksumError,
    XRootDError,
    XRootDNotFoundError,
    XRootDOperationError,
    XRootDStatus,
    XRootDTimeoutError,
    raise_on_error,
)
from xrdclient.config import Config
from xrdclient.testing import FakeDAVServer, FakeServer

CONFIG = Config(username="tester", auth_order=("host",), require_tls=False, data_streams=0)


@pytest.fixture(autouse=True)
def _test_config(monkeypatch):
    monkeypatch.setattr(env, "config", lambda: CONFIG)
    yield
    # Dropped filesystems close their connections when collected: have that
    # happen while this test's server is still up.
    gc.collect()
    _channels.close_all()


def status(code, ok=False, shellcode=0, message="error", errno=0):
    return XRootDStatus(
        {
            "message": message,
            "ok": ok,
            "error": not ok,
            "fatal": False,
            "status": 0 if ok else 1,
            "code": code,
            "shellcode": shellcode,
            "errno": errno,
        }
    )


# -- exceptions: upstream tests/test_responses.py ----------------------------


@pytest.mark.parametrize(
    ("code", "name"),
    [
        (XRootDStatus.errNotFound, "errNotFound"),
        (XRootDStatus.errPipelineFailed, "errPipelineFailed"),
        (XRootDStatus.errTlsError, "errTlsError"),
        (999, None),
    ],
)
def test_status_error_name(code, name):
    assert status(code).error_name == name


def test_a_status_without_a_code_has_no_error_name():
    assert XRootDStatus({"ok": False}).error_name is None


@pytest.mark.parametrize(
    ("code", "exception_type"),
    [
        (XRootDStatus.errNotFound, XRootDNotFoundError),
        (XRootDStatus.errAuthFailed, XRootDAuthorizationError),
        (XRootDStatus.errLoginFailed, XRootDAuthorizationError),
        (XRootDStatus.errSocketTimeout, XRootDTimeoutError),
        (XRootDStatus.errOperationExpired, XRootDTimeoutError),
        (XRootDStatus.errCheckSumError, XRootDChecksumError),
        (XRootDStatus.errUnknown, XRootDOperationError),
    ],
)
def test_status_exceptions(code, exception_type):
    assert isinstance(status(code).exception(), exception_type)


@pytest.mark.parametrize(
    ("errno", "exception_type"),
    [
        (3011, XRootDNotFoundError),
        (3010, XRootDAuthorizationError),
        (3030, XRootDAuthorizationError),
        (3034, XRootDTimeoutError),
        (3035, XRootDTimeoutError),
        (3019, XRootDChecksumError),
        (3012, XRootDOperationError),
    ],
)
def test_server_error_exceptions(errno, exception_type):
    error = status(XRootDStatus.errErrorResponse, shellcode=54, errno=errno)
    assert isinstance(error.exception(), exception_type)


def test_a_server_errno_counts_only_on_a_server_error():
    assert type(status(XRootDStatus.errUnknown, errno=3011).exception()) is XRootDOperationError


def test_raise_on_error():
    ok = status(XRootDStatus.errNone, ok=True, message="ok")
    assert raise_on_error(ok) is ok
    assert ok.exception() is None
    assert isinstance(raise_on_error(ok.__dict__), XRootDStatus)

    with pytest.raises(XRootDNotFoundError) as excinfo:
        status(XRootDStatus.errNotFound).raise_on_error()
    assert excinfo.value.status.code == XRootDStatus.errNotFound
    with pytest.raises(XRootDOperationError):
        raise_on_error(vars(status(XRootDStatus.errUnknown)))


def test_the_exception_carries_its_status_and_says_its_message():
    failed = _status.failure(_status.errInvalidArgs, "bad")
    error = failed.exception()
    assert isinstance(error, XRootDError) and isinstance(error, RuntimeError)
    assert error.status is failed and str(error) == str(failed) == failed.message


def test_the_code_names_leave_the_status_fields_and_repr_alone():
    ok = _status.status()
    assert sorted(vars(ok)) == [
        "code",
        "errno",
        "error",
        "fatal",
        "message",
        "ok",
        "shellcode",
        "status",
    ]
    assert "errNotFound" not in repr(ok) and ok.suDone == 0


@pytest.fixture
def srv():
    with FakeServer(files={"/d/a": b"x"}) as server:
        yield server


def test_a_real_failure_raises_the_exception_its_errno_names(srv):
    fs = client.FileSystem(f"root://{srv.url.netloc}/")
    found, _ = fs.stat("/d/a")
    missing, _ = fs.stat("/d/none")
    assert found.raise_on_error() is found
    with pytest.raises(XRootDNotFoundError, match="3011"):
        missing.raise_on_error()


def test_the_package_exports_the_exceptions_as_upstream_does():
    assert client.XRootDError is XRootDError
    assert client.XRootDNotFoundError is XRootDNotFoundError
    assert client.XRootDAuthorizationError is XRootDAuthorizationError
    assert client.XRootDTimeoutError is XRootDTimeoutError
    assert client.XRootDChecksumError is XRootDChecksumError
    assert client.XRootDOperationError is XRootDOperationError
    assert client.raise_on_error is raise_on_error
    assert client.TapeClient is tape.TapeClient
    assert client.__version__ == xrdclient.__version__


# -- the tape client: upstream tests/test_tape.py ------------------------------


def _ok(ok=True):
    return XRootDStatus(
        {
            "ok": ok,
            "message": "",
            "error": not ok,
            "fatal": False,
            "status": 0,
            "code": 0,
            "shellcode": 0,
            "errno": 0,
        }
    )


def _buffer(value):
    return value.encode("utf-8") + b"\0"


class FakeFileSystem:
    """What ``tape.FileSystem`` is replaced by: records calls, answers as XrdClHttp."""

    instances: ClassVar[list[FakeFileSystem]] = []

    def __init__(self, url):
        self.url = url
        self.calls = []
        FakeFileSystem.instances.append(self)

    def query(self, query_code, arg, timeout=0):
        self.calls.append(("query", query_code, arg, timeout))
        if query_code == QueryCode.PREPARE:
            return _ok(), _buffer(
                json.dumps(
                    {
                        "id": arg,
                        "createdAt": 1,
                        "startedAt": 2,
                        "files": [{"path": "/store/file", "state": "COMPLETED"}],
                    }
                )
            )
        verb, _, rest = arg.partition("\n")
        answer = self.OPAQUE.get(verb) if query_code == QueryCode.OPAQUE else None
        return (_ok(), answer(rest)) if answer else (_ok(False), "")

    @staticmethod
    def _archive_info(rest):
        results = [
            {"path": p, "error": "not found"}
            if "missing" in p
            else {"path": p, "locality": "DISK_AND_TAPE"}
            for p in rest.split("\n")
        ]
        return _buffer(json.dumps(results))

    OPAQUE: ClassVar[dict[str, Any]] = {
        "tape.discover": lambda rest: _buffer(
            json.dumps({"uri": "https://tape.example.org/api/v1", "version": "v1", "sitename": "x"})
        ),
        "tape.archiveinfo": lambda rest: FakeFileSystem._archive_info(rest),
        "tape.stage_delete": lambda rest: "",
    }

    def prepare(self, files, flags, priority=0, timeout=0):
        self.calls.append(("prepare", files, flags, priority, timeout))
        if flags == PrepareFlags.STAGE:
            return _ok(), _buffer("request-1")
        return _ok(), ""


@pytest.fixture
def fake(monkeypatch):
    FakeFileSystem.instances = []
    monkeypatch.setattr(tape, "FileSystem", FakeFileSystem)
    return FakeFileSystem.instances


ROOT = "root://xrootd.example.org/store/file"


def test_discover_uses_opaque_query(fake):
    status, endpoint = tape.TapeClient(timeout=5).discover(ROOT)
    assert status.ok and endpoint.uri == "https://tape.example.org/api/v1"
    assert fake[0].url == "root://xrootd.example.org"
    assert fake[0].calls == [("query", QueryCode.OPAQUE, "tape.discover", 5)]


def test_a_failed_discovery_has_no_endpoint(fake, monkeypatch):
    monkeypatch.setattr(FakeFileSystem, "query", lambda self, *a: (_ok(False), ""))
    assert tape.TapeClient().discover(ROOT)[1] == ""


def test_stage_uses_prepare_and_returns_request_id(fake):
    status, response = tape.TapeClient(timeout=7).stage(ROOT, [{"path": "/store/file"}])
    assert status.ok and response.requestId == response.request_id == "request-1"
    assert fake[0].calls == [("prepare", ["/store/file"], PrepareFlags.STAGE, 0, 7)]


def test_stage_derives_endpoint_from_file_urls(fake):
    status, response = tape.TapeClient(timeout=7).stage([{"url": ROOT}])
    assert status.ok and response.requestId == "request-1"
    assert fake[0].url == "root://xrootd.example.org"
    assert fake[0].calls == [("prepare", [ROOT], PrepareFlags.STAGE, 0, 7)]
    tape.TapeClient().stage((ROOT,))
    assert fake[1].calls[0][1] == [ROOT]


def test_stage_applies_global_disk_lifetime_and_metadata(fake):
    status, response = tape.TapeClient(timeout=7).stage(
        ROOT,
        [ROOT],
        disk_lifetime=3600,
        targeted_metadata={"test-site": {"activity": "analysis"}},
    )
    assert status.ok and response.request_id == "request-1"
    assert fake[0].calls == [
        (
            "prepare",
            [
                'xrdclhttp.tape.stage:{"diskLifetime": "PT3600S", "targetedMetadata": '
                '{"test-site": {"activity": "analysis"}}, '
                '"url": "root://xrootd.example.org/store/file"}',
            ],
            PrepareFlags.STAGE,
            0,
            7,
        )
    ]


def test_stage_accepts_file_metadata(fake):
    tape.TapeClient(timeout=7).stage(
        ROOT,
        [
            {
                "path": "/store/file",
                "diskLifetime": "PT1H",
                "targeted_metadata": {"activity": "analysis"},
            },
        ],
    )
    assert fake[0].calls[0][1] == [
        'xrdclhttp.tape.stage:{"diskLifetime": "PT1H", "path": "/store/file", '
        '"targetedMetadata": {"activity": "analysis"}}',
    ]


def test_stage_preserves_empty_targeted_metadata_and_parses_json_text(fake):
    client_ = tape.TapeClient(timeout=7)
    client_.stage(ROOT, [ROOT], targeted_metadata={})
    client_.stage(ROOT, ["/store/file"], targeted_metadata='{"a": 1}')
    assert fake[0].calls[0][1] == [
        'xrdclhttp.tape.stage:{"targetedMetadata": {}, '
        '"url": "root://xrootd.example.org/store/file"}',
    ]
    assert fake[1].calls[0][1] == [
        'xrdclhttp.tape.stage:{"path": "/store/file", "targetedMetadata": {"a": 1}}'
    ]


@pytest.mark.parametrize(
    "bad",
    [
        lambda c: c.stage(ROOT),
        lambda c: c.stage(ROOT, [{"path": "/store/file", "targeted_metadata": ["analysis"]}]),
        lambda c: c.stage(ROOT, [{}]),
        lambda c: c.stage(ROOT, [{"path": "/store/file\nother"}]),
        lambda c: c.stage(ROOT, [ROOT + "\nother"]),
        lambda c: c.stage([{"path": "/store/file"}]),
        lambda c: c.stage([]),
        lambda c: c.stage_status(ROOT, "request-1\nother"),
        lambda c: c.stage_delete(ROOT, "request-1\nother"),
        lambda c: c.stage_cancel(ROOT, "request-1\rother", ["/store/file"]),
        lambda c: c.release(ROOT, "request-1\nother", ["/store/file"]),
        lambda c: c.archive_info([ROOT + "\nother"]),
        lambda c: c.archive_info([]),
    ],
)
def test_bad_arguments_are_refused_before_any_request(fake, bad):
    with pytest.raises(ValueError):
        bad(tape.TapeClient())
    assert all(not fs.calls for fs in fake)


@pytest.mark.parametrize("disk_lifetime", [-1, True, 1.5, ""])
def test_stage_rejects_invalid_disk_lifetime(fake, disk_lifetime):
    with pytest.raises(ValueError):
        tape.TapeClient().stage(ROOT, ["/store/file"], disk_lifetime=disk_lifetime)


def test_default_timeout_uses_xrootd_environment_default(fake):
    tape.TapeClient().discover(ROOT)
    assert fake[0].calls == [("query", QueryCode.OPAQUE, "tape.discover", 0)]


def test_stage_status_uses_prepare_query(fake):
    status, response = tape.TapeClient(timeout=3).stage_status(ROOT, "request-1")
    assert status.ok and response.id == "request-1"
    first = response.files[0]
    assert first.path == "/store/file" and not hasattr(first, "onDisk") and first.on_disk
    assert response.file_status("/store/file") is first
    assert response.file_status("root://xrootd.example.org//store/file") is first
    assert response.is_on_disk("/store/file") and not response.is_on_disk("/store/missing")
    assert fake[0].calls == [("query", QueryCode.PREPARE, "request-1", 3)]


def test_cancel_delete_release_and_archive_info(fake):
    c = tape.TapeClient(timeout=11)
    assert c.stage_cancel(ROOT, "request-1", ["/store/file"]).ok
    assert c.stage_delete(ROOT, "request-1").ok
    assert c.release(ROOT, "request-1", ["/store/file"]).ok
    status, info = c.archive_info([ROOT, "root://xrootd.example.org/store/missing"])
    assert status.ok
    assert (info[0].locality, info[0].error, info[0].url) == ("DISK_AND_TAPE", None, ROOT)
    assert (info[1].error, info[1].locality) == ("not found", None)
    assert [fs.calls for fs in fake] == [
        [("prepare", ["request-1", "/store/file"], PrepareFlags.CANCEL, 0, 11)],
        [("query", QueryCode.OPAQUE, "tape.stage_delete\nrequest-1", 11)],
        [("prepare", ["request-1", "/store/file"], PrepareFlags.EVICT, 0, 11)],
        [
            (
                "query",
                QueryCode.OPAQUE,
                f"tape.archiveinfo\n{ROOT}\nroot://xrootd.example.org/store/missing",
                11,
            )
        ],
    ]


def test_a_failed_archive_info_is_an_empty_list(fake, monkeypatch):
    monkeypatch.setattr(FakeFileSystem, "query", lambda self, *a: (_ok(False), b""))
    status, infos = tape.TapeClient().archive_info(ROOT)
    assert not status.ok and infos == []


@pytest.mark.parametrize("scheme", ["root", "roots", "xroot", "xroots"])
def test_native_xrootd_urls_preserve_protocol(fake, scheme):
    url = f"{scheme}://xrootd.example.org:1094/store/file"
    status, response = tape.TapeClient().stage(url, [url])
    assert status.ok and response.request_id == "request-1"
    assert fake[0].url == f"{scheme}://xrootd.example.org:1094"
    assert fake[0].calls == [("prepare", [url], PrepareFlags.STAGE, 0, 0)]


@pytest.mark.parametrize(
    ("given", "endpoint", "operation"),
    [
        ("davs://h:8443/a", "https://h:8443", "https://h:8443/a"),
        ("dav://h/a", "http://h", "http://h/a"),
        ("HTTPS://h", "https://h", "https://h/"),
        ("root://h/rel?x", "root://h", "root://h/rel"),
        ("file:///tmp/a", "file:///tmp/a", "file:///tmp/a"),
        ("gsiftp://h/a", "gsiftp://h/a", "gsiftp://h/a"),
    ],
)
def test_urls_are_split_into_endpoint_and_operation_as_upstream(given, endpoint, operation):
    c = tape.TapeClient()
    assert (c._filesystem_url(given), c._operation_url(given)) == (endpoint, operation)


def test_native_xrootd_failure_does_not_retry_with_https(fake, monkeypatch):
    def failing(self, files, flags, priority=0, timeout=0):
        self.calls.append(("prepare", files, flags, priority, timeout))
        return _ok(False), ""

    monkeypatch.setattr(FakeFileSystem, "prepare", failing)
    url = "root://xrootd.example.org:8444/store/file"
    status, response = tape.TapeClient().stage(url, [url])
    assert not status.ok and response == ""
    assert len(fake) == 1 and fake[0].url == "root://xrootd.example.org:8444"


def test_convenience_methods_accept_single_string_and_dict_inputs(fake):
    c = tape.TapeClient()
    assert c.stage(ROOT, ROOT)[1].request_id == "request-1"
    assert c.stage(ROOT, {"path": "/store/file", "diskLifetime": 3600})[1].request_id
    assert c.stage_cancel(ROOT, "request-1", "/store/file").ok
    assert c.release(ROOT, "request-1", ROOT).ok
    status, infos = c.archive_info(ROOT)
    assert status.ok and len(infos) == 1 and infos[0].url == ROOT
    assert fake[3].calls[0][1] == ["request-1", ROOT]


def test_a_failed_stage_status_has_no_status_document(fake, monkeypatch):
    monkeypatch.setattr(FakeFileSystem, "query", lambda self, *a: (_ok(False), ""))
    assert tape.TapeClient().stage_status(ROOT, "request-1")[1] == ""


def test_the_small_helpers_take_what_upstream_s_do():
    assert tape._response_text("id\0") == "id"
    assert tape._normalize_disk_lifetime(None) is None
    assert tape._normalize_disk_lifetime("P1D") == "P1D"


def test_the_tape_responses_behave_as_upstream():
    assert TapeEndpoint({"uri": "u"}).uri == "u"
    assert vars(TapeArchiveInfo({"path": "/p"})) == {"locality": None, "error": None, "path": "/p"}
    assert TapeStageResponse({"requestId": "r"}).request_id == "r"
    assert TapeStageFileStatus({"path": "/p", "onDisk": False}).on_disk is False
    assert TapeStageFileStatus({"path": "/p"}).on_disk is False
    assert TapeStageStatus({"id": "r"}).files == []
    root_path = TapeStageStatus({"files": [{"path": "/", "onDisk": True}]})
    assert root_path.is_on_disk("root://h") and root_path.file_status("//") is not None


# -- the tape client over the fake WebDAV server's tape API -------------------


@pytest.fixture
def dav():
    with FakeDAVServer(files={"/d/a.root": b"a", "/d/b.root": b"b"}, dirs=["/d"]) as server:
        yield server


@pytest.fixture
def http(dav):
    return str(dav.url.with_path("/")).rstrip("/")


def test_discovery_reads_the_well_known_document(dav, http):
    status, endpoint = tape.TapeClient().discover(http + "/d/a.root")
    assert status.ok, status.message
    assert (endpoint.version, endpoint.sitename) == ("v1", "fake-site")
    assert endpoint.uri.endswith("/api/v1")


def test_a_stage_request_goes_from_submitted_to_on_disk(dav, http):
    dav.nearline.add("/d/a.root")
    c = tape.TapeClient(timeout=30)
    status, response = c.stage(http + "/d/a.root", [http + "/d/a.root", "/d/b.root"])
    assert status.ok, status.message
    handle = response.request_id
    assert dav.staged[handle] == ["/d/a.root", "/d/b.root"]
    status, progress = c.stage_status(http, handle)
    assert status.ok and progress.id == handle
    assert not progress.is_on_disk(http + "/d/a.root") and progress.is_on_disk("/d/b.root")
    dav.nearline.discard("/d/a.root")
    assert c.stage_status(http, handle)[1].is_on_disk("/d/a.root")


def test_lifetime_and_metadata_reach_the_api_body(dav, http):
    c = tape.TapeClient()
    status, _ = c.stage(
        http,
        [{"url": "dav" + http[4:] + "//d/a.root"}],
        disk_lifetime=60,
        targeted_metadata={"fake-site": {"activity": "x"}},
    )
    assert status.ok, status.message
    assert json.loads(dav.bodies[-1]) == {
        "files": [
            {
                "path": "/d/a.root",
                "diskLifetime": "PT60S",
                "targetedMetadata": {"fake-site": {"activity": "x"}},
            }
        ]
    }


def test_a_lifetime_alone_is_all_the_api_body_carries(dav, http):
    assert tape.TapeClient().stage(http, [{"path": "/d/a.root", "diskLifetime": "P1D"}])[0].ok
    assert json.loads(dav.bodies[-1]) == {"files": [{"path": "/d/a.root", "diskLifetime": "P1D"}]}


def test_cancel_release_and_delete_reach_the_api(dav, http):
    c = tape.TapeClient()
    handle = c.stage(http, ["/d/a.root", "/d/b.root"])[1].request_id
    assert c.stage_cancel(http, handle, [http + "/d/a.root"]).ok
    files = {f.path: f for f in c.stage_status(http, handle)[1].files}
    assert files["/d/a.root"].state == "CANCELLED" and files["/d/b.root"].on_disk
    assert c.release(http, handle, "/d/b.root").ok
    assert dav.cancelled == {(handle, "/d/a.root")} and dav.released == {(handle, "/d/b.root")}
    assert c.stage_delete(http, handle).ok
    assert handle not in dav.staged
    for gone in (c.stage_cancel(http, handle, "/d/a.root"), c.release(http, handle, "/d/a.root")):
        assert isinstance(gone.exception(), XRootDNotFoundError)


def test_archive_info_names_each_file_by_the_url_asked_about(dav, http):
    dav.nearline.add("/d/a.root")
    status, infos = tape.TapeClient().archive_info([http + "/d/a.root", "/d/none"])
    assert status.ok, status.message
    assert [(i.url, i.locality, i.error) for i in infos] == [
        (http + "/d/a.root", "TAPE", None),
        ("/d/none", "LOST", "no such file"),
    ]


def test_a_site_without_tape_answers_not_found(dav, http):
    dav.no_tape = True
    c = tape.TapeClient()
    status, endpoint = c.discover(http)
    assert not status.ok and endpoint is None
    with pytest.raises(XRootDNotFoundError):
        c.stage(http, ["/d/a.root"])[0].raise_on_error()


@pytest.mark.parametrize(
    ("document", "chosen"),
    [
        ({"endpoints": [{"uri": "a", "version": "v0"}, {"uri": "b", "version": "v1"}]}, "b"),
        ({"endpoints": [{"uri": "a"}]}, "a"),
    ],
)
def test_discovery_prefers_the_version_this_client_speaks(dav, http, document, chosen):
    dav.handlers["GET"] = lambda *_: (200, json.dumps(document).encode(), {})
    status, endpoint = tape.TapeClient().discover(http)
    assert status.ok and (endpoint.uri, endpoint.sitename) == (chosen, "")


@pytest.mark.parametrize("body", [b"[]", b"{}", b'{"endpoints": []}'])
def test_a_discovery_document_without_endpoints_is_a_protocol_error(dav, http, body):
    dav.handlers["GET"] = lambda *_: (200, body, {})
    status, endpoint = tape.TapeClient().discover(http)
    assert status.code == XRootDStatus.errInvalidResponse and endpoint is None
    assert "lists no tape endpoints" in status.message


def test_the_tape_filesystem_is_the_plain_one_for_everything_else(dav, http):
    fs = tape.FileSystem(http)
    for code, arg in ((QueryCode.CHECKSUM, "/d/a.root"), (QueryCode.OPAQUE, "other")):
        status, _ = fs.query(code, arg)
        assert status.code == XRootDStatus.errNotSupported or not status.ok
    assert fs.stat("/d/a.root")[0].ok


def test_tape_requests_over_http_take_callbacks_and_odd_arguments(dav, http):
    from xrdclient.compat.client.utils import AsyncResponseHandler

    fs = tape.FileSystem(http)
    handler = AsyncResponseHandler()
    assert fs.prepare(["/d/a.root"], PrepareFlags.STAGE, callback=handler).ok
    status, handle, _ = handler.wait()
    assert status.ok and handle.decode() in dav.staged
    # A cancel naming no request, and a delete naming none, reach the API as such.
    assert not fs.prepare([], PrepareFlags.CANCEL, timeout=5)[0].ok
    assert not fs.query(QueryCode.OPAQUE, "tape.stage_delete")[0].ok


def test_a_root_endpoint_goes_through_the_servers_own_prepare_and_query():
    with FakeServer(files={"/d/a": b"x"}) as srv:
        url = f"root://{srv.url.netloc}/d/a"
        c = tape.TapeClient()
        status, response = c.stage(url, [url])
        assert status.ok, status.message
        assert response.request_id == "prep-0001" and srv.prepared["prep-0001"] == ["/d/a"]
        status, progress = c.stage_status(url, "prep-0001")
        assert status.ok and progress.request_id == "prep-0001"
        assert c.stage_cancel(url, "prep-0001", "/d/a").ok
        assert srv.cancelled_prepares == ["prep-0001"]
        # As with the bindings, the request id leads an eviction's path list.
        assert c.release(url, "prep-0001", url).ok
        assert srv.evicted[-1] == "/d/a"
        gc.collect()


# -- helper names --------------------------------------------------------------


def test_enum_builds_a_namespace_with_a_reverse_mapping():
    colours = enum(RED=1, BLUE=2)
    assert (colours.RED, colours.reverse_mapping) == (1, {1: "RED", 2: "BLUE"})
    assert PrepareFlags.CANCEL == 1 and PrepareFlags.reverse_mapping[1] == "CANCEL"


def test_callback_wrapper_converts_raw_arguments():
    from xrdclient.compat.client.utils import CallbackWrapper

    got: list[Any] = []
    wrapper = CallbackWrapper(lambda *args: got.append(args), TapeEndpoint)
    wrapper(vars(_status.OK), {"uri": "u"}, [{"url": "root://h", "protocol": 1}])
    st, response, hosts = got[0]
    assert isinstance(st, XRootDStatus) and st.ok and response.uri == "u"
    assert [h.url for h in hosts] == ["root://h"]
    wrapper(_status.OK, None)
    assert got[1][1] is None and got[1][2].hosts == []
    typed = HostList({"hosts": [HostInfo({"url": "x"})]})
    CallbackWrapper(lambda *args: got.append(args), None)(_status.OK, b"raw", typed)
    assert got[2][1:] == (b"raw", typed)
    with pytest.raises(TypeError):
        CallbackWrapper("not callable", None)


def test_progress_handler_wrapper_converts_and_tolerates_no_handler():
    from xrdclient.compat.client.copyprocess import ProgressHandlerWrapper
    from xrdclient.compat.client.utils import CopyProgressHandler

    seen: list[Any] = []

    class Handler(CopyProgressHandler):
        def begin(self, jobId, total, source, target):
            seen.append((source.hostname, target.path))

        def end(self, jobId, results):
            seen.append(results["status"])

        def update(self, jobId, processed, total):
            seen.append((processed, total))

        def should_cancel(self, jobId):
            return True

    wrapped = ProgressHandlerWrapper(Handler())
    wrapped.begin(1, 1, "root://h//a", "root://g//b")
    wrapped.end(1, {"status": vars(_status.OK)})
    wrapped.update(1, 5, 10)
    assert wrapped.should_cancel(1) is True
    assert seen[0] == ("h", "/b") and isinstance(seen[1], XRootDStatus) and seen[2] == (5, 10)
    bare = ProgressHandlerWrapper(None)
    results = {"status": _status.OK}
    bare.begin(1, 1, "a", "b")
    bare.end(1, results)
    bare.update(1, 0, 0)
    assert results["status"] is _status.OK and bare.should_cancel(1) is False


def test_finalize_closes_open_files_and_the_shared_channels():
    from xrdclient.compat.client import finalize

    with FakeServer(files={"/d/a": b"x"}) as srv:
        f = client.File()
        assert f.open(f"root://{srv.url.netloc}//d/a")[0].ok
        assert _channels._channels
        finalize.finalize()
        assert not f.is_open() and not _channels._channels
    assert client.finalize is finalize


def _forget_xrootd(monkeypatch):
    for name in [n for n in sys.modules if n.split(".")[0] == "XRootD"]:
        monkeypatch.delitem(sys.modules, name)


def test_install_maps_every_upstream_submodule(monkeypatch):
    from xrdclient import compat

    _forget_xrootd(monkeypatch)
    compat.install()
    from XRootD.client import finalize  # noqa: F401
    from XRootD.client import tape as installed_tape
    from XRootD.client._version import __version__

    assert installed_tape is tape and __version__ == xrdclient.__version__
    _forget_xrootd(monkeypatch)


# -- parity with the installed bindings ---------------------------------------


@pytest.fixture
def official():
    return pytest.importorskip("XRootD.client", reason="the official bindings are not installed")


@pytest.mark.parity
def test_every_code_name_the_bindings_have_has_their_number(official):
    from XRootD.client.responses import XRootDStatus as Theirs

    names = [n for n in vars(Theirs) if n[:3] in ("err", "suD", "suC", "suR", "suP", "suA", "suN")]
    assert names and {n: getattr(XRootDStatus, n) for n in names} == {
        n: getattr(Theirs, n) for n in names
    }


@pytest.mark.parity
def test_enum_is_the_bindings_enum(official):
    from XRootD.client.flags import enum as theirs

    ours_ns, theirs_ns = enum(A=1, B=4), theirs(A=1, B=4)
    assert ours_ns.reverse_mapping == theirs_ns.reverse_mapping
    assert type(ours_ns).__name__ == type(theirs_ns).__name__
    assert ours_ns.__name__ == theirs_ns.__name__ == "Enum"


@pytest.mark.parity
def test_callback_wrapper_hands_over_what_the_bindings_do(official):
    from XRootD.client.utils import CallbackWrapper as Theirs

    from xrdclient.compat.client.utils import CallbackWrapper as Ours

    raw = {
        "message": "",
        "ok": True,
        "error": False,
        "fatal": False,
        "status": 0,
        "code": 0,
        "shellcode": 0,
        "errno": 0,
    }
    results = []
    for cls in (Theirs, Ours):
        got: list[Any] = []
        cls(lambda *args, got=got: got.append(args), None)(dict(raw), b"x", [])
        st, response, hosts = got[0]
        results.append((vars(st), response, list(hosts)))
    assert results[0] == results[1]
