"""HTTP, HTTPS and WebDAV: the client, the file objects, and the namespace."""

from __future__ import annotations

import base64
import io
import json
import os
from collections.abc import Callable
from typing import Any

import pytest
from xrdhttp.connection import IncompleteRead
from xrdhttp.webdav import pick_digest as _pick_digest

import xrdclient
from xrdclient.config import Config
from xrdclient.crypto import checksum_bytes
from xrdclient.errors import (
    AbortedError,
    BusyError,
    NotFoundError,
    ProtocolError,
    RedirectLimitError,
    TransientError,
    UnsupportedError,
)
from xrdclient.errors import (
    ConnectionError as XRDConnectionError,
)
from xrdclient.errors import (
    TimeoutError as XRDTimeoutError,
)
from xrdclient.flags import LocateFlags, PrepareFlags, QueryCode
from xrdclient.http import (
    HTTPClient,
    HTTPFileSystem,
    bearer_token,
    digest,
    macaroon,
    open_http,
    tape,
)
from xrdclient.http.client import check_status, request_target
from xrdclient.http.dav import _parse, _pick_repr_digest
from xrdclient.http.tpc import _follow, _remote_url
from xrdclient.testing import FakeDAVServer
from xrdclient.testing.http import Reply
from xrdclient.url import parse

BODY = b"hello world"


def test_an_http_gateway_timeout_is_a_retryable_error():
    with pytest.raises(TransientError, match="HTTP 504 Gateway Time-out"):
        check_status(504, "Gateway Time-out", parse("https://data.example/file"), (), None)


@pytest.fixture
def dav():
    """A running WebDAV endpoint holding one file and one empty collection."""
    with FakeDAVServer(files={"/d/a.root": BODY}, dirs=["/d/sub"]) as server:
        yield server


@pytest.fixture
def fs(dav):
    filesystem = xrdclient.FileSystem(dav.url)
    try:
        yield filesystem
    finally:
        filesystem.close()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scheme", ["http", "https", "dav", "davs", "webdav"])
def test_every_http_spelling_reaches_the_webdav_implementation(scheme):
    with xrdclient.FileSystem(f"{scheme}://dav.example.org/store") as fs:
        assert isinstance(fs, HTTPFileSystem)


def test_a_root_url_still_gets_the_binary_implementation():
    with xrdclient.FileSystem("root://eos.example.org") as fs:
        assert not isinstance(fs, HTTPFileSystem)


def test_open_dispatches_on_the_scheme(dav):
    with xrdclient.open(dav.url / "d/a.root") as fh:
        assert fh.read() == BODY
    with xrdclient.open(dav.url / "d/a.root", "r") as text:
        assert text.read() == BODY.decode()


def test_a_path_object_works_over_webdav(dav):
    path = xrdclient.XRootDPath(dav.url / "d/a.root")
    assert path.name == "a.root"
    assert path.read_bytes() == BODY
    assert path.parent.is_dir()
    assert sorted(p.name for p in path.parent.iterdir()) == ["a.root", "sub"]
    path.close()


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


def test_a_token_in_the_url_wins_over_the_config(tmp_path):
    config = Config(token="from-config", token_file=None)
    assert bearer_token(config, parse("https://h/p?authz=Bearer%20from-url")) == "from-url"
    assert bearer_token(config, parse("https://h/p")) == "from-config"


def test_a_token_file_is_read_when_there_is_no_token(tmp_path, monkeypatch):
    monkeypatch.delenv("BEARER_TOKEN", raising=False)
    token = tmp_path / "bt"
    token.write_text("from-file\n")
    token.chmod(0o600)
    assert bearer_token(Config(token_file=str(token))) == "from-file"
    assert bearer_token(Config(token_file=str(tmp_path / "missing"))) is None


def test_the_environment_is_the_last_resort(monkeypatch):
    monkeypatch.setenv("BEARER_TOKEN", "from-env")
    assert bearer_token(Config(token_file=None)) == "from-env"


def test_the_request_target_is_encoded_and_keeps_no_secrets():
    url = parse("https://h/store/a b?authz=secret&cks.type=adler32")
    assert request_target(url) == "/store/a%20b?cks.type=adler32"


def test_the_request_target_encodes_a_url_path_once_and_keeps_an_escaped_slash():
    """A URL's escapes are decoded once; ``%2F`` stays a slash inside a segment."""
    url = parse("https://h/resolve/refs%2Fconvert/a%zz b%41")
    assert request_target(url) == "/resolve/refs%2Fconvert/a%25zz%20bA"


def test_a_path_argument_is_a_name_and_every_percent_in_it_is_escaped():
    url = parse("https://h/").with_path("/d/a%41b c")
    assert request_target(url) == "/d/a%2541b%20c"


def test_the_request_target_preserves_a_signed_query_byte_for_byte():
    query = "name=a+b%2Ac&Policy=eyJ__&Signature=abc%7Edef&authz=SECRET"
    assert request_target(parse(f"https://cdn.example/p?{query}")) == (
        "/p?name=a+b%2Ac&Policy=eyJ__&Signature=abc%7Edef"
    )


def test_a_url_query_token_authenticates_too(dav):
    dav.require_token = "s3cr3t"
    with xrdclient.open(dav.url.evolve(path="/d/a.root", query={"authz": "s3cr3t"}), "rb") as fh:
        assert fh.read() == BODY


def test_statuses_become_the_exceptions_python_programmers_expect(dav):
    client = HTTPClient(Config())
    with pytest.raises(FileNotFoundError):
        client.request("GET", dav.url / "d/missing")
    dav.no_dav = True
    with pytest.raises(UnsupportedError):
        client.request("PROPFIND", dav.url / "d", headers={"Depth": "0"})
    client.close()


def test_the_redirect_budget_is_finite(dav):
    dav.redirects["/d/a.root"] = str(dav.url / "d/a.root")
    with HTTPClient(Config(redirect_limit=0)) as client, pytest.raises(RedirectLimitError):
        client.request("GET", dav.url / "d/a.root")


def test_a_connection_is_pooled_and_reused(dav):
    with HTTPClient(Config()) as client:
        client.request("HEAD", dav.url / "d/a.root")
        client.request("HEAD", dav.url / "d/a.root")
        assert dav.connections == 1
        assert client.idle_count() == 1
        assert "1 connection" in repr(client)


def test_a_dropped_connection_is_retried_once_not_raised(dav):
    """A connection that fails under a repeatable request is tried once more, at once."""
    with HTTPClient(Config()) as client:
        dav.fault("GET", drop=True)
        assert client.request("GET", dav.url / "d/a.root").body == BODY
        dav.fault("GET", drop=True, times=2)
        with pytest.raises(XRDConnectionError):
            client.request("GET", dav.url / "d/a.root")


@pytest.mark.parametrize("status", [429, 503])
def test_a_busy_answer_is_retried_after_what_it_asks(dav, status, monkeypatch):
    """New in 0.4.0: ``429``/``503`` repeat a repeatable request once, after ``Retry-After``."""
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)
    with HTTPClient(Config()) as client:
        dav.fault("GET", status=status, headers={"Retry-After": "2"})
        assert client.request("GET", dav.url / "d/a.root").body == BODY
        assert [wait for wait in slept if wait] == [2.0]  # the fake's own sleeps are zero
        dav.fault("POST", status=status)
        with pytest.raises(BusyError):
            client.request("POST", dav.url / "d/a.root", body=b"")


def test_a_failed_request_leaves_the_connection_fit_to_reuse(dav):
    """An undrained error response would strand the pooled connection."""
    with HTTPClient(Config()) as client:
        with pytest.raises(FileNotFoundError):
            client.request("GET", dav.url / "d/missing")
        assert client.idle_count() == 1
        assert client.request("GET", dav.url / "d/a.root").body == BODY
        assert dav.connections == 1


def test_an_unreachable_endpoint_raises_this_packages_error():
    dead = FakeDAVServer()
    dead.start()
    url = dead.url
    dead.stop()
    with HTTPClient(Config(connect_timeout=1.0)) as client, pytest.raises(XRDConnectionError):
        client.request("HEAD", url / "anything")


# ---------------------------------------------------------------------------
# File objects
# ---------------------------------------------------------------------------


def test_reading_again_after_eof_does_not_request_an_unsatisfiable_range(dav):
    with open_http(dav.url / "d/a.root", "rb", buffering=0) as fh:
        assert fh.read() == BODY
        assert fh.read() == b""
    assert dav.seen.count(("GET", "/d/a.root")) == 1


def test_reading_at_the_end_after_seeking_there_is_eof_without_a_request(dav):
    with open_http(dav.url / "d/a.root", "rb", buffering=0) as fh:
        assert fh.seek(0, io.SEEK_END) == len(BODY)
        assert fh.read() == b""
        assert fh.read(4) == b""
    assert ("GET", "/d/a.root") not in dav.seen


def test_reading_past_the_end_is_eof_even_though_the_server_says_416(dav):
    with open_http(dav.url / "d/a.root", "rb", buffering=0) as fh:
        assert fh.seek(len(BODY) + 10) == len(BODY) + 10
        assert fh.read(4) == b""
        assert fh.read() == b""
        fh.seek(0)
        assert fh.read() == BODY


def test_a_body_cut_short_of_its_promise_is_an_error_not_a_download(dav):
    """A connection dropped mid-body must not pass for a complete transfer."""
    dav.truncate_at = 5
    with (
        HTTPClient(Config()) as client,
        pytest.raises(TransientError, match="short of the declared Content-Length"),
    ):
        client.request("GET", dav.url / "d/a.root")


def test_a_streaming_read_cut_short_names_the_bytes_it_kept(dav):
    """With retries off, a dropped read surfaces, and its error says how far it got."""
    dav.truncate_at = 5
    config = Config(connect_retries=0)
    with open_http(dav.url / "d/a.root", "rb", config=config) as fh:
        assert fh.read(5) == b"hello"
        with pytest.raises(TransientError, match="short of the declared Content-Length") as caught:
            fh.read()
    assert caught.value.committed == 5


def test_a_cut_stream_recovers_with_a_ranged_get(dav):
    """A GET dropped mid-body is resumed from where it got to, on its own.

    The server cuts every response to five bytes, so the read only finishes
    by reconnecting with ``Range`` and picking up each time - which is what a
    flaky link needs and what a bare read now does without being told to."""
    dav.truncate_at = 5
    config = Config(connect_retries=20, retry_backoff=0.0)
    with open_http(dav.url / "d/a.root", "rb", config=config) as fh:
        assert fh.read() == b"hello world"
    ranged = [h.get("Range") for m, h in zip(dav.seen, dav.headers, strict=False) if m[0] == "GET"]
    assert any(r for r in ranged)  # it resumed with at least one ranged GET


def test_text_mode_iterates_lines(dav):
    dav.add_file("/d/lines.txt", b"one\ntwo\n")
    with xrdclient.open(dav.url / "d/lines.txt", "r") as fh:
        assert list(fh) == ["one\n", "two\n"]


def test_a_read_abandoned_mid_body_does_not_corrupt_the_next_request(fs, dav):
    """A POST is never retried, so it must not be sent down a connection
    that still has the rest of an abandoned body waiting on it."""
    dav.add_file("/d/big.bin", b"x" * (4 << 20))
    fh = fs.open("/d/big.bin", "rb", buffering=0)
    assert fh.read(10) == b"x" * 10
    fh.seek(100)
    assert fs.prepare(["/d/big.bin"])
    assert fh.read(10) == b"x" * 10
    fh.close()  # abandons the second body the same way
    assert fs.prepare(["/d/big.bin"])


def test_a_small_remainder_is_drained_and_the_connection_kept(fs, dav):
    dav.add_file("/d/small.bin", bytes(range(200)))
    with fs.open("/d/small.bin", "rb", buffering=0) as fh:
        assert fh.read(10) == bytes(range(10))
        fh.seek(100)
        assert fh.read(10) == bytes(range(100, 110))
        assert dav.connections == 1


def test_exclusive_creation_refuses_an_existing_resource(dav):
    with pytest.raises(FileExistsError):
        open_http(dav.url / "d/a.root", "xb")
    with open_http(dav.url / "d/fresh.bin", "xb") as fh:
        fh.write(b"new")
    assert dav.contents("/d/fresh.bin") == b"new"
    # A conditional PUT sent twice would fail its own condition.
    assert dav.seen.count(("PUT", "/d/fresh.bin")) == 1


def test_what_http_cannot_do_says_so(dav):
    with pytest.raises(UnsupportedError, match="append"):
        open_http(dav.url / "d/a.root", "ab")
    with pytest.raises(UnsupportedError, match="partial update"):
        open_http(dav.url / "d/a.root", "r+b")
    with pytest.raises(ValueError):
        open_http(dav.url / "d/a.root", "r", buffering=0)
    raw = open_http(dav.url / "d/a.root", "rb", buffering=0)
    # The type matters, not just the message: Python 3.10's TextIOWrapper asks
    # for a descriptor to look up a console encoding, and forgives only this.
    with pytest.raises(io.UnsupportedOperation, match="descriptor"):
        raw.fileno()
    assert "HTTPRawIO" in repr(raw)
    raw.close()


def test_closing_twice_is_harmless(dav):
    fh = open_http(dav.url / "d/twice.bin", "wb")
    fh.write(b"x")
    fh.close()
    fh.close()
    assert dav.contents("/d/twice.bin") == b"x"


# ---------------------------------------------------------------------------
# WebDAV namespace
# ---------------------------------------------------------------------------


def test_stat_reads_the_properties(fs):
    info = fs.stat("/d/a.root")
    assert info.st_size == len(BODY)
    assert info.is_file()
    assert info.path == "/d/a.root"
    assert fs.stat("/d").is_dir()
    assert fs.getsize("/d/a.root") == len(BODY)


def test_a_missing_path_is_a_missing_file(fs):
    with pytest.raises(FileNotFoundError):
        fs.stat("/d/nope")
    assert not fs.exists("/d/nope")
    assert not fs.isdir("/d/nope")
    assert not fs.isfile("/d/nope")


def test_a_plain_http_server_is_stated_with_head(dav, fs):
    """No WebDAV at all still answers the question a stat asks."""
    dav.no_dav = True
    info = fs.stat("/d/a.root")
    assert info.st_size == len(BODY)
    assert ("HEAD", "/d/a.root") in dav.seen


def test_listing_walks_and_globs(fs, dav):
    dav.add_file("/d/sub/deep.txt", b"deep")
    assert sorted(fs.listdir("/d")) == ["a.root", "sub"]
    assert [e.name for e in fs.scandir("/d") if e.is_dir()] == ["sub"]
    assert sorted(root for root, _, _ in fs.walk("/d")) == ["/d", "/d/sub"]
    assert list(fs.glob("*.root", root="/d")) == ["/d/a.root"]


def _grid_namespace(dav: FakeDAVServer) -> None:
    """A storage element's top levels: there, but refusing MKCOL with 403.

    EOS answers MKCOL on ``/eos/`` like this, and dCache on ``/pnfs/``; a
    ``makedirs`` that started from the root could never get past them.
    """
    dav.add_dir("/eos/lhcb/user")

    def refuse_the_top(method: str, path: str, headers: dict[str, str]) -> Reply | None:
        refused = ("/eos", "/eos/lhcb", "/eos/ro")
        return (403, b"FORBIDDEN", {}) if path.rstrip("/") in refused else None

    dav.handlers["MKCOL"] = refuse_the_top


def test_makedirs_climbs_only_as_far_as_the_missing_parents(fs, dav):
    _grid_namespace(dav)
    fs.makedirs("/eos/lhcb/user/r/me/run1")
    assert {"/eos/lhcb/user/r", "/eos/lhcb/user/r/me", "/eos/lhcb/user/r/me/run1"} <= dav.dirs
    mkcols = [path for method, path in dav.seen if method == "MKCOL"]
    assert "/eos" not in mkcols and "/eos/lhcb" not in mkcols
    # target, then up while 409, then back down: the gfal2 order
    assert mkcols == [
        "/eos/lhcb/user/r/me/run1",
        "/eos/lhcb/user/r/me",
        "/eos/lhcb/user/r",
        "/eos/lhcb/user/r/me",
        "/eos/lhcb/user/r/me/run1",
    ]


def test_makedirs_of_a_directory_that_is_there_is_one_request(fs, dav):
    _grid_namespace(dav)
    fs.makedirs("/eos/lhcb/user", exist_ok=True)
    assert [s for s in dav.seen if s[0] == "MKCOL"] == [("MKCOL", "/eos/lhcb/user")]


def test_exist_ok_forgives_a_403_on_a_collection_that_is_there(fs, dav):
    """EOS says 403, not 405, for a collection the caller cannot write in."""
    _grid_namespace(dav)
    fs.mkdir("/eos/lhcb", exist_ok=True)
    fs.makedirs("/eos/lhcb", exist_ok=True)
    with pytest.raises(PermissionError):
        fs.mkdir("/eos/lhcb")
    with pytest.raises(PermissionError):
        fs.mkdir("/eos/ro", exist_ok=True)  # refused, and not there


def test_makedirs_with_no_root_to_stop_at_says_what_is_missing(fs, dav):
    dav.handlers["MKCOL"] = lambda method, path, headers: (409, b"no parent", {})
    with pytest.raises(FileNotFoundError):
        fs.makedirs("/x/y")


def test_exist_ok_does_not_forgive_a_resource_on_the_name(fs):
    # MKCOL answers 405 for a collection *and* for a plain resource already
    # on the name; only the former is what ``exist_ok`` promises to forgive.
    with pytest.raises(FileExistsError):
        fs.mkdir("/d/a.root", exist_ok=True)
    with pytest.raises(FileExistsError):
        fs.makedirs("/d/a.root", exist_ok=True)


def test_touch_makes_an_empty_resource_and_can_refuse_an_existing_one(fs, dav):
    fs.touch("/d/t.txt")
    assert dav.contents("/d/t.txt") == b""
    with pytest.raises(FileExistsError):
        fs.touch("/d/t.txt", exist_ok=False)


def test_rmtree_clears_a_whole_collection(fs, dav):
    dav.add_file("/d/sub/deep.txt", b"deep")
    fs.rmtree("/d")
    assert "/d" not in dav.dirs
    assert not [p for p in dav.files if p.startswith("/d/")]


def test_whole_file_helpers_come_for_free(fs, dav):
    fs.write_text("/d/t.txt", "hello")
    assert fs.read_text("/d/t.txt") == "hello"
    assert fs.read_bytes("/d/t.txt") == b"hello"
    assert fs.write_bytes("/d/b.bin", b"\x00\x01") == 2


def test_open_accepts_posc_for_symmetry_and_ignores_it(fs, dav):
    """The signature matches ``FileSystem.open``; HTTP has no such header."""
    with fs.open("/d/posc.txt", "wb", posc=True) as fh:
        fh.write(b"body")
    assert dav.contents("/d/posc.txt") == b"body"


def test_no_vendor_opcode_survives_the_crossing_to_http(fs):
    """A caller that guards on :meth:`extensions` gets one code path for both
    schemes, so this answers rather than refusing."""
    assert fs.extensions() == frozenset()


def test_what_webdav_has_no_answer_for_says_so(fs):
    calls: tuple[Callable[[], object], ...] = (
        lambda: fs.statvfs("/"),
        lambda: fs.chmod("/d/a.root", 0o644),
        lambda: fs.truncate("/d/a.root", 0),
        lambda: fs.locate("/d/a.root"),
        lambda: fs.query(0),
        lambda: fs.query_config("version"),
        lambda: fs.protocol(),
        lambda: fs.xattrs("/d/a.root"),
        lambda: fs.listxattr("/d/a.root"),
        lambda: fs.listxattr_tree("/d"),
        lambda: fs.getxattr("/d/a.root", "x"),
        lambda: fs.setxattr("/d/a.root", "x", b"1"),
        lambda: fs.removexattr("/d/a.root", "x"),
        lambda: fs.evict(["/d/a.root"]),
        lambda: fs.deep_locate("/d/a.root"),
        lambda: fs.symlink("/d/a.root", "/d/latest"),
        lambda: fs.link("/d/a.root", "/d/second"),
        lambda: fs.readlink("/d/latest"),
        lambda: fs.lstat("/d/a.root"),
        lambda: fs.utime("/d/a.root"),
        lambda: fs.chown("/d/a.root", 0, 0),
        lambda: fs.checksum_cancel("/d/a.root"),
        lambda: fs.query_stats(),
        lambda: fs.query_space("/d"),
        lambda: fs.set_property("appid t"),
        lambda: fs.appid("t"),
    )
    for call in calls:
        with pytest.raises(UnsupportedError):
            call()


def test_the_unsupported_overrides_keep_the_signature_they_replace(fs):
    """Passing the real arguments must say 'no WebDAV equivalent', not TypeError."""
    calls: tuple[Callable[[], object], ...] = (
        lambda: fs.locate("/d/a.root", flags=LocateFlags.REFRESH),
        lambda: fs.prepare(["/d/a.root"], flags=PrepareFlags.EVICT, priority=1),
        lambda: fs.query(QueryCode.CONFIG, "version"),
        lambda: fs.statvfs(),
        lambda: fs.query_stats("io"),
        lambda: fs.query_space(),
        lambda: fs.stat("/d/a.root", follow_symlinks=False),
        lambda: fs.utime("/d/a.root", (1, 2), ns=None),
    )
    for call in calls:
        with pytest.raises(UnsupportedError):
            call()


def test_nothing_over_webdav_is_a_symbolic_link(fs, dav):
    """The question is answerable - the answer is simply always no."""
    assert fs.is_symlink("/d/a.root") is False
    assert fs.is_symlink("/d/absent.root") is False


def test_statx_is_a_stat_per_path(fs):
    assert [info.st_size for info in fs.statx(["/d/a.root", "/d"])] == [len(BODY), 0]


def test_ping_and_repr(fs, dav):
    fs.ping()
    assert ("OPTIONS", "/") in dav.seen
    assert repr(fs).startswith("HTTPFileSystem('http://")
    assert fs.endpoint == f"{dav.address[0]}:{dav.address[1]}"


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------


def test_a_base64_crc64_is_decoded_even_though_hashlib_never_heard_of_it(dav):
    """A CRC is not a hash, so the width that tells hex from base64 has to be
    written down rather than asked of :mod:`hashlib`."""
    assert digest(dav.url / "d/a.root", "crc64").value == checksum_bytes("crc64", BODY)


def test_a_hex_crc64_is_left_as_it_is():
    """Sixteen hex digits is the ``root://`` and WebDAV spelling; twelve
    characters of base64 is the RFC 9530 one, and they cannot be confused."""
    value = checksum_bytes("crc64", BODY)
    assert _pick_digest(f"crc64={value}", "crc64") == value


def test_a_digest_per_listing_entry_is_a_binary_protocol_luxury(fs):
    """WebDAV lists and digests in separate requests, so it says so rather
    than quietly issuing one request per entry."""
    with pytest.raises(UnsupportedError, match="checksum per listing entry"):
        fs.scandir("/d", algorithm="adler32")


def test_an_unrelated_digest_in_the_header_is_not_mistaken_for_ours():
    assert _pick_digest("md5=aGk=,adler32=00010203", "adler32") == "00010203"
    assert _pick_digest("sha-256=aGk=", "adler32") == ""


def test_a_copy_over_webdav_verifies_itself(dav, tmp_path):
    result = xrdclient.copy(dav.url / "d/a.root", tmp_path / "a.root")
    assert (tmp_path / "a.root").read_bytes() == BODY
    assert result.checksum is not None
    assert result.checksum.value == checksum_bytes("adler32", BODY)


def test_a_copy_into_webdav_verifies_itself(dav, tmp_path):
    source = tmp_path / "up.bin"
    source.write_bytes(b"uploaded")
    result = xrdclient.copy(source, dav.url / "d/up.bin")
    assert dav.contents("/d/up.bin") == b"uploaded"
    assert result.verified
    with pytest.raises(FileExistsError):
        xrdclient.copy(source, dav.url / "d/up.bin", overwrite=False)


def test_a_copy_into_a_missing_collection_makes_the_parents(dav, tmp_path):
    """A ``root://`` write carries ``kXR_mkpath``; WebDAV ``PUT`` makes no
    parents and answers 409. The copy makes them first, from a clean namespace
    (a failed ``PUT`` leaves EOS unable to), so the write lands."""
    source = tmp_path / "deep.bin"
    source.write_bytes(b"payload")
    result = xrdclient.copy(source, dav.url / "fresh/sub/deep.bin", verify=False)
    assert result.size == len(b"payload")
    assert {"/fresh", "/fresh/sub"} <= dav.dirs
    assert dav.contents("/fresh/sub/deep.bin") == b"payload"
    # Into the top collection there is no parent to make: no MKCOL at all.
    dav.clear()
    xrdclient.copy(source, dav.url / "top.bin", verify=False)
    assert [method for method, _ in dav.seen if method == "MKCOL"] == []


def test_copy_tree_into_webdav_makes_its_directories_up_front(dav, tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "top.txt").write_text("t")
    (tmp_path / "a" / "x.txt").write_text("x")
    (tmp_path / "a" / "b" / "y.txt").write_text("y")
    results = xrdclient.copy_tree(str(tmp_path), dav.url / "tree", verify=False, workers=2)
    assert len(results) == 3
    assert {"/tree", "/tree/a", "/tree/a/b"} <= dav.dirs
    assert dav.contents("/tree/a/b/y.txt") == b"y"
    # The directories are made once, before any file - not an MKCOL per PUT.
    mkcols = [path for method, path in dav.seen if method == "MKCOL"]
    assert mkcols and all(not path.endswith(".txt") for path in mkcols)
    assert len(mkcols) == len(set(mkcols))


def test_a_dry_run_tree_into_webdav_makes_nothing(dav, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.txt").write_text("x")
    xrdclient.copy_tree(str(tmp_path), dav.url / "planned", dry_run=True)
    assert "/planned" not in dav.dirs
    assert [method for method, _ in dav.seen if method == "MKCOL"] == []


def test_an_empty_tree_into_webdav_makes_nothing(dav, tmp_path):
    assert xrdclient.copy_tree(str(tmp_path), dav.url / "empty") == []
    assert "/empty" not in dav.dirs


# ---------------------------------------------------------------------------
# Macaroons
# ---------------------------------------------------------------------------


def test_a_server_that_mints_nothing_is_an_error(dav):
    dav.macaroon = ""
    with pytest.raises(ProtocolError, match="macaroon"):
        macaroon(dav.url / "d")


# ---------------------------------------------------------------------------
# XML hardening
# ---------------------------------------------------------------------------


def test_a_document_type_declaration_is_refused_before_parsing():
    """The entity-expansion attacks all need a DTD; none of them get one."""
    payload = (
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><D:multistatus xmlns:D="DAV:"/>'
    )
    with pytest.raises(ProtocolError, match="document type"):
        _parse(payload)


def test_malformed_xml_is_a_protocol_error():
    with pytest.raises(ProtocolError, match="malformed"):
        _parse(b"<D:multistatus")


# ---------------------------------------------------------------------------
# Third-party copy
# ---------------------------------------------------------------------------


@pytest.fixture
def elsewhere():
    """A second endpoint, so a third-party copy has two of them."""
    with FakeDAVServer(dirs=["/d"]) as server:
        yield server


def test_a_pull_is_verified_against_both_servers_by_default(dav, elsewhere):
    xrdclient.third_party(dav.url / "d/a.root", elsewhere.url / "d/v.root")
    assert elsewhere.contents("/d/v.root") == BODY
    assert ("HEAD", "/d/v.root") in elsewhere.seen  # its digest, asked for afterwards


def test_the_far_end_gets_a_minted_token_when_nothing_else_authorises_it(
    dav, elsewhere, monkeypatch
):
    """gfal2's ``RETRIEVE_BEARER_TOKEN``: a proxy cannot be shown to the other server."""
    from xrdclient.http import tpc

    asked = []

    def mint(far, config, client, *, write, timeout):
        asked.append((far.path, write))
        return "minted-for-this-copy"

    monkeypatch.setattr(tpc, "_minted", mint)
    xrdclient.third_party(dav.url / "d/a.root", elsewhere.url / "d/m.root", verify=False)
    assert asked == [("/d/a.root", False)]  # a pull reads the far end
    headers = elsewhere.copies[-1]
    assert headers["TransferHeaderAuthorization"] == "Bearer minted-for-this-copy"
    assert headers["Credential"] == "none"


def test_a_refused_copy_leaves_no_empty_destination(dav, elsewhere):
    """EOS names the destination, then refuses the COPY with 401."""

    def name_then_refuse(method, path, headers):
        elsewhere.add_file(path, b"")
        return (401, b"Unauthorized", {})

    elsewhere.handlers["COPY"] = name_then_refuse
    with pytest.raises(PermissionError):
        xrdclient.third_party(dav.url / "d/a.root", elsewhere.url / "d/refused.root")
    assert "/d/refused.root" not in elsewhere.files


def test_an_empty_file_already_there_survives_a_copy_refused_for_it(dav, elsewhere):
    """``Overwrite: F`` meeting an existing file: 412, and the file is not ours to remove."""
    elsewhere.add_file("/d/taken.root", b"")
    with pytest.raises(FileExistsError):
        xrdclient.third_party(
            dav.url / "d/a.root", elsewhere.url / "d/taken.root", overwrite=False, verify=False
        )
    assert "/d/taken.root" in elsewhere.files


def test_a_refused_copy_without_cleanup_leaves_what_the_server_named(dav, elsewhere):
    def name_then_refuse(method, path, headers):
        elsewhere.add_file(path, b"")
        return (401, b"Unauthorized", {})

    elsewhere.handlers["COPY"] = name_then_refuse
    with pytest.raises(PermissionError):
        xrdclient.http.third_party(
            dav.url / "d/a.root", elsewhere.url / "d/refused.root", cleanup=False
        )
    assert "/d/refused.root" in elsewhere.files


def test_a_minted_token_is_scoped_to_what_the_copy_does(monkeypatch):
    from xrdclient.http import dav as dav_module
    from xrdclient.http import tpc

    requests = []

    def macaroon(url, *, caveats, validity, config, client):
        requests.append((str(url), caveats, validity))
        return "m"

    monkeypatch.setattr(dav_module, "macaroon", macaroon)
    far = xrdclient.url.parse("https://se.example.org/store/f.root")
    config = xrdclient.Config(request_timeout=600)
    with HTTPClient(config) as client:
        assert tpc._minted(far, config, client, write=False, timeout=None) == "m"
        assert tpc._minted(far, config, client, write=True, timeout=300) == "m"
        assert requests[0][1:] == (["activity:LIST,DOWNLOAD"], "PT30M")
        assert requests[1][1:] == (["activity:LIST,DOWNLOAD,MANAGE,UPLOAD,DELETE"], "PT20M")
        # Nothing to mint: plain http, a far-end token in the URL, or a far end that cannot.
        assert (
            tpc._minted(
                xrdclient.url.parse("http://se/f"), config, client, write=False, timeout=None
            )
            is None
        )
        # A token written into the far URL is the far end's to accept, so it stands.
        for key in ("authz", "access_token"):
            far_with_token = xrdclient.url.parse(f"https://se/f?{key}=X")
            assert tpc._minted(far_with_token, config, client, write=False, timeout=None) is None
        # But an *ambient* token no longer suppresses the mint: it is refused on the
        # delegated leg, so a macaroon is minted with it instead and used there.
        assert (
            tpc._minted(far, xrdclient.Config(token="t"), client, write=False, timeout=None) == "m"
        )

        def refuse(*args, **kwargs):
            raise PermissionError("no macaroons here")

        monkeypatch.setattr(dav_module, "macaroon", refuse)
        assert tpc._minted(far, config, client, write=False, timeout=None) is None


@pytest.mark.parametrize(
    ("origin", "target", "relayed"),
    [
        # EOS's head node to its own disk server, over its internal plain http
        ("https://eoslhcb.cern.ch/eos/f", "http://st-096-hh151a57.cern.ch:8443/eos/f", True),
        ("https://webdav.echo.stfc.ac.uk:1094/f", "https://gw3.echo.stfc.ac.uk/f", True),
        # anywhere else: never
        ("https://eoslhcb.cern.ch/eos/f", "https://evil.example.org/f", False),
        ("https://eoslhcb.cern.ch/eos/f", "https://cern.ch.evil.org/f", False),
        # a bare domain or an address has no site to share
        ("https://cern.ch/f", "https://st.cern.ch/f", False),
        ("https://10.0.0.1/f", "https://10.0.0.2/f", False),
        ("https://10.0.0.1/f", "https://10.0.0.1/g", True),
        # a host directly under a public suffix: its "site" is a whole registry
        ("https://se.ac.uk/f", "https://other-university.ac.uk/f", False),  # curated
        ("https://se.ac.uk/f", "https://se.ac.uk/g", True),  # but the host itself, yes
        ("https://se.ac.uk/f", "http://se.ac.uk:8443/g", True),
        ("https://dav.ernet.in/f", "https://x.ernet.in/f", False),  # curated, not generic
        ("https://storage.edu.cn/f", "https://x.edu.cn/f", False),
        ("https://dcache.ac.ir/f", "https://x.ac.ir/f", False),  # generic: ac.<cc>
        ("https://grid.or.at/f", "https://x.or.at/f", False),
        ("https://door.gob.cl/f", "https://x.gob.cl/f", False),
        # one level further down, the parent is the site again
        ("https://dav.physics.ox.ac.uk/f", "https://se2.physics.ox.ac.uk/f", True),
        ("https://dav.physics.ox.ac.uk/f", "https://other.ox.ac.uk/f", False),
        ("https://eosatlas.cern.ch/eos/f", "https://st-1.cern.ch/eos/f", True),
        ("https://door.desy.de/f", "https://dcache-door-2.desy.de/f", True),
    ],
)
def test_transfer_headers_follow_a_redirect_only_within_the_site(origin, target, relayed):
    from xrdclient.http.client import carries_credentials, relays_transfer_headers

    parse = xrdclient.url.parse
    assert relays_transfer_headers(parse(origin), parse(target)) is relayed
    if target.startswith("http://"):
        # the caller's own Authorization still never follows a downgrade
        assert not carries_credentials(parse(origin), parse(target), ("cern.ch",))


def test_a_copy_redirected_within_the_site_keeps_its_transfer_headers(dav, elsewhere):
    """Not the caller's Authorization: that one stays behind as before."""
    import xrdclient.http.client as client_module

    seen = []

    def record(method: str, path: str, headers: dict[str, str]) -> Reply | None:
        seen.append(headers)
        return None

    elsewhere.handlers["COPY"] = record
    dav.redirects["/d/r.root"] = str(elsewhere.url / "d/r.root")
    original = client_module.relays_transfer_headers
    client_module.relays_transfer_headers = lambda origin, target: True
    try:
        xrdclient.http.third_party(
            elsewhere.url / "d/a.root",
            dav.url / "d/r.root",
            remote_token="for-the-far-side",
            verify=False,
            config=xrdclient.Config(token="mine"),
        )
    except OSError:
        pass  # the fake cannot finish a copy it was redirected into; the headers are the point
    finally:
        client_module.relays_transfer_headers = original
    assert seen and seen[0].get("TransferHeaderAuthorization") == "Bearer for-the-far-side"
    assert "Authorization" not in seen[0]


def test_the_copy_says_it_may_overwrite(dav, elsewhere):
    """``Overwrite: T`` unless asked not to; gfal2 deletes the target first instead."""
    xrdclient.third_party(dav.url / "d/a.root", elsewhere.url / "d/copy.root")
    assert elsewhere.copies[-1]["Overwrite"] == "T"


def test_a_failure_after_the_202_is_still_a_failure(dav, elsewhere):
    """The status line says Accepted; only the body says what happened."""
    with pytest.raises(xrdclient.errors.NotFoundError, match="third-party copy failed"):
        xrdclient.third_party(dav.url / "d/missing.root", elsewhere.url / "d/copy.root")
    assert "/d/copy.root" not in elsewhere.files


def test_refusing_to_overwrite_reaches_the_destination(dav, elsewhere):
    elsewhere.add_file("/d/taken.root", b"mine")
    with pytest.raises(xrdclient.errors.ExistsError):
        xrdclient.third_party(dav.url / "d/a.root", elsewhere.url / "d/taken.root", overwrite=False)
    assert elsewhere.contents("/d/taken.root") == b"mine"
    assert elsewhere.copies[-1]["Overwrite"] == "F"


def test_the_source_token_travels_in_a_transfer_header(dav, elsewhere):
    """A pre-signed source URL is how a grid transfer is usually authorised."""
    dav.require_token = "src-token"
    source = f"{dav.url / 'd/a.root'}?authz=src-token"
    xrdclient.third_party(source, elsewhere.url / "d/copy.root")
    headers = elsewhere.copies[-1]
    assert headers["TransferHeaderAuthorization"] == "Bearer src-token"
    # The token authorises the far side's GET, so it belongs in that header
    # and not in the URL this endpoint is about to log.
    assert "authz" not in headers["Source"]
    assert elsewhere.contents("/d/copy.root") == BODY


def test_the_optional_knobs_reach_the_wire(dav, elsewhere):
    from xrdclient.http import third_party

    third_party(
        dav.url / "d/a.root",
        elsewhere.url / "d/copy.root",
        delegate=True,
        verify=True,
        streams=4,
        transfer_headers={"X-Rucio-Id": "abc"},
    )
    headers = elsewhere.copies[-1]
    assert headers["Credential"] == "gridsite"
    assert headers["RequireChecksumVerification"] == "true"
    assert headers["X-Number-Of-Streams"] == "4"
    assert headers["TransferHeaderX-Rucio-Id"] == "abc"


def test_the_copy_turns_off_the_servers_own_checksum_verification_by_default(dav, elsewhere):
    """``RequireChecksumVerification: false`` by default, as gfal2 sends.

    Left absent, the destination defaults to verifying the source's checksum
    in band with a ``HEAD``; a source that answers a checksum query but puts
    no digest in that ``HEAD`` (CNAF's StoRM) then fails the whole copy. So
    the server-side check is turned off and the copy is verified end to end
    afterwards instead (test_a_pull_is_verified_against_both_servers_by_default).
    """
    xrdclient.third_party(dav.url / "d/a.root", elsewhere.url / "d/copy.root")
    assert elsewhere.copies[-1]["RequireChecksumVerification"] == "false"


def test_a_pull_from_a_checksumless_head_source_still_succeeds(dav, elsewhere):
    """The CNAF-StoRM case: no digest in the source HEAD must not fail the copy.

    The destination here refuses any COPY that asks it to verify the source
    checksum (as dCache does when the source HEAD carries none); the client's
    default of ``RequireChecksumVerification: false`` is what lets it through.
    """

    def refuse_server_verification(method, path, headers):
        if method == "COPY" and headers.get("RequireChecksumVerification") == "true":
            return (500, b"failure: no checksum in HEAD response\n", {})
        return None

    elsewhere.handlers["COPY"] = refuse_server_verification
    xrdclient.third_party(dav.url / "d/a.root", elsewhere.url / "d/copy.root", verify=False)
    assert elsewhere.contents("/d/copy.root") == BODY


def test_verify_true_still_forces_server_side_verification(dav, elsewhere):
    """A caller who knows both ends can answer a HEAD digest can still ask for it."""
    from xrdclient.http import third_party

    third_party(dav.url / "d/a.root", elsewhere.url / "d/copy.root", verify=True)
    assert elsewhere.copies[-1]["RequireChecksumVerification"] == "true"


def test_an_ambient_token_mints_a_macaroon_for_the_far_leg(dav, elsewhere, monkeypatch):
    """DiracX: the user's token is good for the far end directly, not delegated.

    So over ``https`` the ambient token is used to mint a macaroon at the far
    end, and that - not the bare token - rides in the transfer header. A
    far-end token in the URL is still used exactly as given.
    """
    from xrdclient.http import dav as dav_module
    from xrdclient.http import tpc

    minted = []

    def macaroon(url, *, caveats, validity, config, client):
        minted.append((url.path, config.token))
        return "macaroon-for-the-far-leg"

    monkeypatch.setattr(dav_module, "macaroon", macaroon)
    copies = []

    def run_copy(*args: Any) -> int:
        copies.append(args[2])
        return 0

    monkeypatch.setattr(tpc, "_run_copy", run_copy)
    ambient = Config(token="diracx-token")
    xrdclient.http.third_party(
        "https://src.example/d/a.root", "https://dst.example/d/b.root", config=ambient
    )
    assert minted == [("/d/a.root", "diracx-token")]
    assert copies[-1]["TransferHeaderAuthorization"] == "Bearer macaroon-for-the-far-leg"
    xrdclient.http.third_party(
        "https://src.example/d/a.root?authz=given", "https://dst.example/d/b.root", config=ambient
    )
    assert len(minted) == 1
    assert copies[-1]["TransferHeaderAuthorization"] == "Bearer given"


def test_an_endpoint_without_third_party_copy_says_so(dav, elsewhere):
    elsewhere.no_tpc = True
    with pytest.raises(UnsupportedError):
        xrdclient.third_party(dav.url / "d/a.root", elsewhere.url / "d/copy.root")


def test_the_http_dialect_refuses_a_url_it_cannot_send_as_a_header(dav):
    """``xrdclient.third_party`` dispatches; this one is reached directly."""
    from xrdclient.http import third_party

    with pytest.raises(ValueError, match="not root"):
        third_party("root://a.example//store/f", dav.url / "d/copy.root")


def test_a_borrowed_client_is_used_and_left_open(dav, elsewhere):
    """The caller owns what the caller passed, connections included."""
    from xrdclient.http import third_party

    with HTTPClient(Config()) as client:
        third_party(
            dav.url / "d/a.root", elsewhere.url / "d/copy.root", client=client, timeout=30.0
        )
        assert elsewhere.contents("/d/copy.root") == BODY
        # Still usable, which it would not be had the copy closed it.
        assert client.request("HEAD", elsewhere.url / "d/copy.root").status == 200


def test_the_scheme_is_translated_for_the_far_side():
    """``davs://`` is this package's spelling; no server resolves it."""
    assert _remote_url(parse("davs://a.example/store/f?authz=t")) == "https://a.example:443/store/f"
    assert _remote_url(parse("dav://a.example/store/f")) == "http://a.example:80/store/f"


# -- the marker reader, driven directly -------------------------------------


def _read(body: str, progress=None):
    return _follow(io.BytesIO(body.encode()), parse("https://h.example/f"), progress)


def _perf(index: int, done: int, stripes: int = 1) -> str:
    return (
        f"Perf Marker\nTimestamp: 1360017414\nStripe Index: {index}\n"
        f"Stripe Bytes Transferred: {done}\nTotal Stripe Count: {stripes}\nEnd\n"
    )


def test_the_stripes_are_summed_and_the_latest_one_wins():
    """Each stripe reports its own running total, not an increment."""
    body = _perf(0, 100, 2) + _perf(1, 50, 2) + _perf(0, 400, 2) + "success: Created\n"
    assert _read(body) == 450


def test_a_body_that_stops_before_the_outcome_is_a_protocol_error():
    """A transfer whose connection died is not a transfer that succeeded."""
    with pytest.raises(ProtocolError, match="before reporting the outcome"):
        _read(_perf(0, 10))


def test_blank_lines_between_marker_blocks_are_not_the_end_of_anything():
    """Servers pad the stream; a blank line is not an outcome."""
    assert _read("\n" + _perf(0, 9) + "\n\nsuccess: Created\n") == 9


def test_a_marker_with_unreadable_numbers_keeps_its_byte_count():
    body = (
        "Perf Marker\nStripe Index: nonsense\nStripe Bytes Transferred: 12\n"
        "Total Stripe Count: also nonsense\nEnd\nsuccess: Created\n"
    )
    assert _read(body) == 12


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("failure: rejected: HTTP 403 not yours", xrdclient.errors.PermissionError_),
        ("failure: HTTP 507 no space left", xrdclient.errors.NoSpaceError),
        ("failure: something broke", OSError),
        ("failed: the source hung up", OSError),
    ],
)
def test_the_failure_line_is_mapped_to_the_exception_it_describes(line, expected):
    with pytest.raises(expected):
        _read(_perf(0, 1) + line + "\n")


def test_a_marker_stream_may_be_empty_and_still_succeed():
    """Small transfers finish before the first marker is due."""
    assert _read("success: Created\n") == 0


# ---------------------------------------------------------------------------
# The fake server itself
# ---------------------------------------------------------------------------


def test_the_fake_records_what_it_served(dav, fs):
    fs.stat("/d/a.root")
    assert ("PROPFIND", "/d/a.root") in dav.seen


def test_the_fake_binds_lazily_and_stops_idempotently():
    server = FakeDAVServer(files={"/a": b"x"})
    assert "unbound" in repr(server)
    server.stop()  # nothing bound, nothing to release
    server.start()
    was = server.address
    server.stop()
    server.stop()
    assert server.address == was
    assert f"{was[0]}:{was[1]}" in repr(server)


def test_a_line_that_is_not_a_field_is_ignored(dav):
    """Servers pad marker blocks with prose; only ``name: value`` counts."""
    body = "Perf Marker\nnonsense without a colon\n" + _perf(0, 12).split("\n", 1)[1]
    assert _read(body + "success: Created\n") == 12


def test_a_response_body_can_be_read_as_text(dav):
    with HTTPClient(Config()) as client:
        response = client.request("GET", dav.url / "d/a.root")
    assert response.text() == "hello world"


def test_session_failures_in_this_packages_words():
    from xrdhttp.session import Failure

    from xrdclient.http.client import _DEFAULT_ERRORS

    wrapped = _DEFAULT_ERRORS.transport(Failure("GET", "http://h.example/f", TimeoutError("x")))
    assert isinstance(wrapped, XRDTimeoutError)
    assert "timed out" in str(wrapped)
    failed = _DEFAULT_ERRORS.transport(Failure("GET", "http://h/f", ConnectionResetError("x")))
    assert type(failed) is XRDConnectionError
    cut = _DEFAULT_ERRORS.transport(Failure("GET", "http://h/f", IncompleteRead(b"", 5)))
    assert isinstance(cut, TransientError) and "5 bytes short" in str(cut)
    assert isinstance(
        _DEFAULT_ERRORS.redirect("GET", "http://h/", "x", ValueError()), ProtocolError
    )


# ---------------------------------------------------------------------------
# RFC 9530 Repr-Digest
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("algorithm", ["sha256", "sha512", "md5", "adler32", "crc32c", "crc64"])
def test_an_rfc_9530_digest_is_negotiated(dav, algorithm):
    dav.legacy_digests = False  # only a Repr-Digest can answer
    assert digest(dav.url / "d/a.root", algorithm).value == checksum_bytes(algorithm, BODY)


def test_both_generations_are_asked_for_at_once(dav):
    digest(dav.url / "d/a.root", "sha256")
    asked = dav.headers[-1]
    assert asked["Want-Repr-Digest"] == "sha-256=10"
    assert asked["Want-Digest"] == "sha-256"


def test_an_rfc_3230_server_is_still_understood(dav):
    dav.repr_digests = False
    assert digest(dav.url / "d/a.root", "adler32").value == checksum_bytes("adler32", BODY)


def test_repr_digest_wins_when_a_server_sends_both(dav):
    """RFC 9530 defines the encoding; RFC 3230 left it to each server."""
    good = base64.b64encode(bytes.fromhex(checksum_bytes("sha256", BODY))).decode()
    dav.handlers["HEAD"] = lambda method, path, headers: (
        200,
        b"",
        {"Digest": "sha-256=AAAA", "Repr-Digest": f"sha-256=:{good}:"},
    )
    assert digest(dav.url / "d/a.root", "sha256").value == checksum_bytes("sha256", BODY)


def test_a_repr_digest_without_our_algorithm_falls_back_to_digest(dav):
    dav.handlers["HEAD"] = lambda method, path, headers: (
        200,
        b"",
        {"Digest": "adler32=0000abcd", "Repr-Digest": "sha-512=:AAAA:"},
    )
    assert digest(dav.url / "d/a.root", "adler32").value == "0000abcd"


#: Digests of the right size for their algorithm, as RFC 9530 byte sequences.
SHA256 = "ab" * 32
SHA256_SF = base64.b64encode(bytes.fromhex(SHA256)).decode()
SHA1 = "cd" * 20
SHA1_SF = base64.b64encode(bytes.fromhex(SHA1)).decode()


@pytest.mark.parametrize(
    ("field", "algorithm", "expected"),
    [
        (f"sha-256=:{SHA256_SF}:", "sha256", SHA256),
        (f"sha-512=:AA==:, sha-256=:{SHA256_SF}:", "sha256", SHA256),
        (f"sha-256=:{SHA256_SF}:;source=cache", "sha256", SHA256),  # parameters say nothing
        ('note="a, b; c", adler=:AAAAAQ==:', "adler32", "00000001"),
        ('note="say \\"hi\\", ok", crc32c=:AAAAAg==:', "crc32c", "00000002"),
        ("adler32=:AAAAAw==:", "adler32", "00000003"),  # our own name
        (f"sha=:{SHA1_SF}:", "sha1", SHA1),  # RFC 3230's
        ("ADLER=:AAAABA==:", "adler32", "00000004"),
        ("adler=0000abcd", "adler32", "0000abcd"),  # a hex token, as some servers send
        ('adler="0000abcd"', "adler32", "0000abcd"),
        # StoRM WebDAV 1.13: the base64 of the hex *text*, not of the digest.
        ("adler=:NDU4MjVmZTI=:", "adler32", "45825fe2"),
        ("adler=:NDU4MjVGRTI=:", "adler32", "45825fe2"),
        ("sha-256=:aGk=:", "sha256", ""),  # two bytes are no SHA-256: no answer
        ("adler=:AAAAAAE=:", "adler32", ""),  # five bytes are no adler32 either
        ("sha-256=:not base64!:", "sha256", ""),  # malformed: no value at all
        ("sha-256", "sha256", ""),  # a bare key is ``true``, not a digest
        ("=:aGk=:, , sha-512=:aGk=:", "sha256", ""),
        ("", "sha256", ""),
    ],
)
def test_repr_digest_fields_are_read_as_structured_dictionaries(field, algorithm, expected):
    assert _pick_repr_digest(field, algorithm) == expected


def test_storms_repr_digest_matches_its_digest_header(dav):
    """CNAF's StoRM sent both; the copy failed on ``expected 45825fe2, got 3435...``."""
    value = checksum_bytes("adler32", BODY)
    text = base64.b64encode(value.encode()).decode()
    dav.handlers["HEAD"] = lambda method, path, headers: (
        200,
        b"",
        {"Digest": f"adler32={value}", "Repr-Digest": f"adler=:{text}:"},
    )
    assert digest(dav.url / "d/a.root", "adler32").value == value


def test_a_repr_digest_of_the_wrong_size_gives_way_to_digest(dav):
    dav.handlers["HEAD"] = lambda method, path, headers: (
        200,
        b"",
        {"Digest": "adler32=0000abcd", "Repr-Digest": "adler=:AAAAAAE=:"},
    )
    assert digest(dav.url / "d/a.root", "adler32").value == "0000abcd"


def test_a_repr_digest_split_over_two_header_lines_is_one_field(dav):
    """RFC 9110: repeated field lines are one comma-joined field."""
    value = base64.b64encode(bytes.fromhex(checksum_bytes("md5", BODY))).decode()
    # Two spellings of the name are two keys here, and two lines on the wire.
    dav.handlers["HEAD"] = lambda method, path, headers: (
        200,
        b"",
        {"Repr-Digest": "sha-512=:AA==:", "repr-digest": f"md5=:{value}:"},
    )
    assert digest(dav.url / "d/a.root", "md5").value == checksum_bytes("md5", BODY)


def test_a_digest_that_is_neither_hex_nor_base64_is_passed_through():
    from xrdhttp.webdav import as_hex as _as_hex
    from xrdhttp.webdav import is_hex as _is_hex

    assert _as_hex("Not-A-Digest!", "md5") == "not-a-digest!"
    assert _is_hex("abcd", "no-such-algorithm") is False


def test_a_macaroon_can_be_minted_over_a_connection_that_is_already_open(dav):
    """The caller's client is borrowed, so minting must not close it."""
    with HTTPClient(Config()) as client:
        assert macaroon(dav.url / "d", client=client) == dav.macaroon
        assert client.request("HEAD", dav.url / "d/a.root").status == 200


def test_a_relative_path_is_resolved_against_the_endpoint(dav):
    with xrdclient.FileSystem(dav.url / "d") as fs:
        assert fs.stat("a.root").st_size == len(BODY)
        assert os.fspath(fs) == "/d"


def test_iterdir_is_scandir_one_at_a_time(fs):
    assert sorted(entry.name for entry in fs.iterdir("/d")) == ["a.root", "sub"]


def test_a_read_only_http_file_refuses_to_write(dav):
    """The raw layer says so itself; the buffer above it never gets the chance."""
    from xrdclient.http.file import HTTPRawIO

    with (
        HTTPRawIO(dav.url / "d/a.root", "rb", config=Config()) as raw,
        pytest.raises(io.UnsupportedOperation, match="not writable"),
    ):
        raw.write(b"nope")


def test_writes_after_the_stream_has_started_go_down_the_wire(dav):
    """Once the ``PUT`` is open there is no buffer left to grow."""
    from xrdclient.http.file import HTTPRawIO

    raw = HTTPRawIO(dav.url / "d/forced.bin", "wb", config=Config())
    raw._begin_upload()  # nothing buffered yet: the empty flush is a no-op
    raw.write(b"first ")
    raw.write(b"second")
    raw.close()
    assert dav.contents("/d/forced.bin") == b"first second"


def test_binary_mode_and_an_encoding_are_contradictory(dav):
    with pytest.raises(ValueError, match="binary mode doesn't take an encoding"):
        open_http(dav.url / "d/a.root", "rb", encoding="utf-8")


# ---------------------------------------------------------------------------
# Staging: the WLCG tape REST API
# ---------------------------------------------------------------------------


def test_the_friendly_keywords_arrive_here_too(fs, dav):
    """``stat`` and ``online`` are free in a ``PROPFIND``; ``evict`` has no API."""
    dav.nearline.add("/d/a.root")
    assert [e.name for e in fs.scandir("/d", stat=False, online=True)] == ["a.root", "sub"]
    assert fs.prepare(["/d/a.root"], stage=True)
    with pytest.raises(UnsupportedError, match="EVICT"):
        fs.prepare(["/d/a.root"], evict=True)


def test_a_staged_file_reads_as_on_tape_until_it_arrives(fs, dav):
    dav.nearline.add("/d/a.root")
    handle = fs.prepare(["/d/a.root"])
    waiting = fs.query_prepare(handle, ["/d/a.root"])[0]
    assert (waiting.on_tape, waiting.online, waiting.state) == (True, False, "SUBMITTED")
    assert not waiting
    dav.nearline.discard("/d/a.root")
    arrived = fs.query_prepare(handle, ["/d/a.root"])[0]
    assert (arrived.on_tape, arrived.online, arrived.state) == (False, True, "COMPLETED")
    assert arrived and arrived.requested_at == "1"


def test_a_file_the_site_has_not_got_fails_its_staging_request(fs, dav):
    handle = fs.prepare(["/d/gone.root"])
    report = fs.query_prepare(handle, ["/d/gone.root"])[0]
    assert (report.online, report.on_tape, report.state) == (False, False, "FAILED")
    assert report.error == "no such file"


def test_a_lifetime_asks_the_site_to_keep_the_file_that_long(fs, dav):
    tape.stage(fs.client, fs.url, ["/d/a.root"], lifetime="P1D")
    assert b'"diskLifetime": "P1D"' in dav.bodies[-1]


def test_asking_about_no_paths_reports_on_the_whole_request(fs, dav):
    handle = fs.prepare(["/d/a.root"])
    assert [report.path for report in fs.query_prepare(handle, [])] == ["/d/a.root"]


def test_a_path_the_request_never_named_is_said_to_be_missing_not_dropped(fs):
    handle = fs.prepare(["/d/a.root"])
    stranger = fs.query_prepare(handle, ["/d/other.root"])[0]
    assert (stranger.path, stranger.error) == ("/d/other.root", "not part of this request")


def test_a_staging_request_can_be_withdrawn(fs, dav):
    handle = fs.prepare(["/d/a.root"])
    fs.cancel_prepare(handle)
    assert handle not in dav.staged
    with pytest.raises(NotFoundError):
        fs.query_prepare(handle, ["/d/a.root"])


def test_polling_a_request_nobody_made_is_a_not_found(fs):
    with pytest.raises(NotFoundError):
        fs.query_prepare("stage-9999", ["/d/a.root"])
    with pytest.raises(NotFoundError):
        fs.cancel_prepare("stage-9999")


def test_archive_info_says_where_each_file_is_without_moving_any(fs, dav):
    dav.nearline.add("/d/a.root")
    dav.add_file("/d/b.root", BODY)
    on_tape, on_disk, gone = fs.archive_info(["/d/a.root", "/d/b.root", "/d/none.root"])
    assert (on_tape.on_tape, on_tape.online, on_tape.state) == (True, False, "TAPE")
    assert (on_disk.online, on_disk.state) == (True, "DISK")
    assert (gone.exists, gone.error) == (False, "no such file")
    assert dav.staged == {} and str(on_disk) == "/d/b.root: online"


def test_a_site_with_no_tape_answers_the_api_with_a_not_found(fs, dav):
    dav.no_tape = True
    calls: tuple[Callable[[], object], ...] = (
        lambda: fs.prepare(["/d/a.root"]),
        lambda: fs.query_prepare("stage-0001", ["/d/a.root"]),
        lambda: fs.archive_info(["/d/a.root"]),
        lambda: fs.cancel_prepare("stage-0001"),
    )
    for call in calls:
        with pytest.raises(NotFoundError, match="api/v1"):
            call()


def test_staging_nothing_is_refused_by_the_server(fs):
    with pytest.raises(xrdclient.errors.XRootDError):
        fs.prepare([])


def test_a_non_staging_prepare_flag_says_webdav_has_no_such_thing(fs):
    with pytest.raises(UnsupportedError, match="FRESH"):
        fs.prepare(["/d/a.root"], flags=PrepareFlags.STAGE | PrepareFlags.FRESH)
    with pytest.raises(UnsupportedError, match="tape API releases a request"):
        fs.evict(["/d/a.root"])


@pytest.mark.parametrize("verb", ["POST", "GET", "DELETE"])
def test_an_endpoint_of_the_api_that_does_not_exist_is_a_not_found(fs, verb):
    with pytest.raises(NotFoundError):
        fs.client.request(verb, fs.url.with_path("/api/v1/nonsense"), body=b"{}", expect=(200,))


def test_a_body_that_is_not_json_is_refused_at_both_ends(fs, dav):
    dav.handlers["POST"] = lambda *_: (200, b"<html>not json</html>", {})
    with pytest.raises(ProtocolError, match="did not answer with JSON"):
        fs.archive_info(["/d/a.root"])
    del dav.handlers["POST"]
    with pytest.raises(xrdclient.errors.XRootDError):
        fs.client.request(
            "POST", fs.url.with_path("/api/v1/stage"), body=b"{oops", expect=(200, 201)
        )


def test_the_request_id_can_arrive_only_in_the_location_header(fs, dav):
    """Some implementations answer 201 with an empty body and a Location."""
    dav.handlers["POST"] = lambda *_: (201, b"{}", {"Location": "/api/v1/stage/from-header/"})
    assert fs.prepare(["/d/a.root"]) == "from-header"


@pytest.mark.parametrize("spelling", ["true", "1", 1, True])
def test_a_disk_flag_counts_however_the_server_spelt_it(fs, dav, spelling):
    body = json.dumps({"files": [{"path": "/d/a.root", "onDisk": spelling}]}).encode()
    dav.handlers["GET"] = lambda *_: (200, body, {})
    assert fs.query_prepare("stage-0001", ["/d/a.root"])[0].online


@pytest.mark.parametrize("shape", ['{"responses": %s}', "%s"])
def test_a_reply_that_names_its_file_list_differently_is_still_read(fs, dav, shape):
    entries = json.dumps([{"path": "/d/a.root", "locality": "DISK_AND_TAPE"}])
    dav.handlers["POST"] = lambda *_: (200, (shape % entries).encode(), {})
    both = fs.archive_info(["/d/a.root"])[0]
    assert (both.online, both.on_tape) == (True, True)


def test_a_locality_that_is_neither_disk_nor_tape_is_a_file_you_cannot_have(fs, dav):
    dav.handlers["POST"] = lambda *_: (
        200,
        b'{"files": [{"path": "/d/a.root", "locality": "LOST"}]}',
        {},
    )
    lost = fs.archive_info(["/d/a.root"])[0]
    assert (lost.exists, lost.error, lost.state) == (False, "lost", "LOST")


def test_an_empty_reply_body_leaves_every_path_unaccounted_for(fs, dav):
    dav.handlers["POST"] = lambda *_: (200, b"", {})
    assert fs.archive_info(["/d/a.root"])[0].error == "not part of this request"


# -- abandoning a response ---------------------------------------------------


def test_abandoning_a_large_body_closes_its_connection(dav):
    dav.add_file("/d/big.bin", b"x" * (4 << 20))
    with HTTPClient(Config()) as client:
        response = client.open("GET", dav.url / "d/big.bin")
        assert response.read(10) == b"x" * 10
        response.close()
        assert response.closed
        assert client.idle_count() == 0
        # Not retried, so this only works on a connection with nothing left
        # on it; the fake refuses the empty request, which is an answer.
        stage = dav.url / "api/v1/stage"
        assert client.request("POST", stage, body=b"{}", expect=(400,)).status == 400


def test_abandoning_a_short_remainder_keeps_the_connection(dav):
    dav.add_file("/d/small.bin", bytes(200))
    with HTTPClient(Config()) as client:
        response = client.open("GET", dav.url / "d/small.bin")
        response.read(10)
        response.close()
        assert response.closed
        assert client.idle_count() == 1


def test_a_body_that_arrives_after_an_abort_is_still_the_abort(dav, monkeypatch):
    """The whole body may already sit in a buffer; the request was aborted all the same."""
    from xrdhttp import session

    read = session.Response.body
    with HTTPClient(Config()) as client:

        def then_abort(self: session.Response, limit: int = session.MAX_BODY) -> bytes:
            data = read(self, limit)
            client.abort()  # as another thread would, once the bytes are in
            return data

        monkeypatch.setattr(session.Response, "body", then_abort)
        with pytest.raises(AbortedError, match="was aborted"):
            client.request("GET", dav.url / "d/a.root")


def test_a_read_that_stalls_is_a_timeout_not_resumed(dav):
    """A server frozen mid-body is slow, not gone: the read is not resumed."""
    from xrdclient.http.file import HTTPRawIO

    dav.fault("GET", stall=3.0)
    with (
        HTTPRawIO(dav.url / "d/a.root", "rb", config=Config(request_timeout=0.3)) as raw,
        pytest.raises(XRDTimeoutError),
    ):
        raw.read(5)
    assert dav.methods().count("GET") == 1


def test_abandoning_a_finished_response_is_harmless(dav):
    with HTTPClient(Config()) as client:
        response = client.open("GET", dav.url / "d/a.root")
        response.read()
        response.close()
        response.close()
        assert client.idle_count() == 1


# -- URLs handed to another server -------------------------------------------


def test_a_rename_to_a_name_with_a_space_arrives_intact(fs, dav):
    fs.rename("/d/a.root", "/d/a b.root")
    assert dav.contents("/d/a b.root") == BODY


def test_a_rename_to_a_name_outside_latin_1_arrives_intact(fs, dav):
    fs.rename("/d/a.root", "/d/данные.root")
    assert dav.contents("/d/данные.root") == BODY


def test_a_rename_names_the_destination_in_http_not_dav(dav):
    seen = []
    dav.handlers["MOVE"] = lambda method, path, headers: seen.append(headers["Destination"])
    token = dav.url.evolve(scheme="dav", query={"authz": "T"})
    with xrdclient.FileSystem(token) as filesystem:
        filesystem.rename("/d/a.root", "/d/b c.root")
    assert seen == [f"http://{dav.url.netloc}/d/b%20c.root"]


def test_a_third_party_copy_names_a_source_with_a_space_intact(dav, elsewhere):
    dav.add_file("/d/a b.root", BODY)
    xrdclient.third_party(dav.url / "d/a b.root", elsewhere.url / "d/c d.root")
    assert elsewhere.copies[-1]["Source"] == f"{dav.url.http_url}d/a%20b.root"
    assert elsewhere.contents("/d/c d.root") == BODY


def test_the_far_url_is_percent_encoded():
    assert _remote_url(parse("davs://a.example/a b/ü?authz=t&x=h:1")) == (
        "https://a.example:443/a%20b/%C3%BC?x=h:1"
    )


def test_http_reprs_redact_tokens_in_the_query():
    """Reprs reach logs and tracebacks, so a URL's credentials must not show."""
    from xrdclient.http.dav import HTTPFileSystem

    fs = HTTPFileSystem("https://example.org/data?authz=SECRETTOKEN")
    assert "SECRETTOKEN" not in repr(fs)


# ---------------------------------------------------------------------------
# A COPY refused after its transfer token stayed behind on a redirect (RAL)
# ---------------------------------------------------------------------------


def test_a_refused_copy_says_its_token_stayed_behind():
    from xrdclient.errors import PermissionError_, kXR_NotAuthorized
    from xrdclient.http.tpc import _explain_withheld

    refused = PermissionError_(kXR_NotAuthorized, "HTTP 401 Unauthorized", path="/f")
    _explain_withheld(refused, ["other.example.org", "ceph-svc22.gridpp.rl.ac.uk"])
    assert "redirected to ceph-svc22.gridpp.rl.ac.uk" in str(refused)
    assert str(refused).endswith("XRD_TRUSTEDREDIRECTDOMAINS=gridpp.rl.ac.uk)")


@pytest.mark.parametrize("hosts", [[], ["ceph.example.org"]])
def test_other_failures_keep_their_own_words(hosts):
    from xrdclient.errors import NotFoundError, PermissionError_, kXR_NotAuthorized, kXR_NotFound
    from xrdclient.http.tpc import _explain_withheld

    missing = NotFoundError(kXR_NotFound, "HTTP 404", path="/f")
    _explain_withheld(missing, ["ceph.example.org"])
    assert missing.hint == ""
    refused = PermissionError_(kXR_NotAuthorized, "HTTP 401", path="/f")
    _explain_withheld(refused, [])
    assert refused.hint == ""
    plain = PermissionError("local")
    _explain_withheld(plain, hosts)
    assert str(plain) == "local"


def test_a_bare_host_is_its_own_domain():
    from xrdclient.errors import PermissionError_, kXR_NotAuthorized
    from xrdclient.http.tpc import _explain_withheld

    refused = PermissionError_(kXR_NotAuthorized, "HTTP 403", path="/f")
    _explain_withheld(refused, ["localhost"])
    assert refused.hint.endswith("XRD_TRUSTEDREDIRECTDOMAINS=localhost")
