"""HTTP, HTTPS and WebDAV: the client, the file objects, and the namespace."""

from __future__ import annotations

import io
import json
import os

import pytest

import xrdclient
from xrdclient.config import Config
from xrdclient.crypto import checksum_bytes
from xrdclient.errors import (
    ConnectionError as XRDConnectionError,
)
from xrdclient.errors import (
    NotFoundError,
    ProtocolError,
    RedirectLimitError,
    TransientError,
    UnsupportedError,
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
from xrdclient.http.dav import _parse, _pick_digest
from xrdclient.http.tpc import _follow, _remote_url
from xrdclient.testing import FakeDAVServer
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


def test_a_token_is_presented_as_a_bearer_header(dav):
    dav.require_token = "s3cr3t"
    with xrdclient.FileSystem(dav.url) as anonymous, pytest.raises(PermissionError):
        anonymous.stat("/d/a.root")
    with xrdclient.FileSystem(dav.url, Config(token="s3cr3t")) as authorised:
        assert authorised.stat("/d/a.root").st_size == len(BODY)


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


def test_a_redirect_is_followed(dav):
    dav.add_file("/d/b.root", b"redirected")
    dav.redirects["/d/a.root"] = str(dav.url / "d/b.root")
    with xrdclient.FileSystem(dav.url) as fs:
        assert fs.read_bytes("/d/a.root") == b"redirected"
    assert ("GET", "/d/b.root") in dav.seen


def test_the_redirect_budget_is_finite(dav):
    dav.redirects["/d/a.root"] = str(dav.url / "d/a.root")
    with HTTPClient(Config(redirect_limit=0)) as client:
        with pytest.raises(RedirectLimitError):
            client.request("GET", dav.url / "d/a.root")


def test_a_connection_is_pooled_and_reused(dav):
    with HTTPClient(Config()) as client:
        client.request("HEAD", dav.url / "d/a.root")
        first = client.connection(dav.url)
        client.request("HEAD", dav.url / "d/a.root")
        assert client.connection(dav.url) is first
        assert "1 connection" in repr(client)


def test_a_stale_pooled_connection_is_retried_not_raised(dav):
    """A keep-alive socket the server has since dropped must not surface."""
    with HTTPClient(Config()) as client:
        client.request("HEAD", dav.url / "d/a.root")
        client.connection(dav.url).sock.close()  # the server went away
        assert client.request("GET", dav.url / "d/a.root").body == BODY


def test_a_failed_request_leaves_the_connection_fit_to_reuse(dav):
    """An undrained error response would strand the pooled connection."""
    with HTTPClient(Config()) as client:
        with pytest.raises(FileNotFoundError):
            client.request("GET", dav.url / "d/missing")
        conn = client.connection(dav.url)
        assert client.request("GET", dav.url / "d/a.root").body == BODY
        assert client.connection(dav.url) is conn


def test_an_unreachable_endpoint_raises_this_packages_error():
    dead = FakeDAVServer()
    dead.start()
    url = dead.url
    dead.stop()
    with HTTPClient(Config(connect_timeout=1.0)) as client:
        with pytest.raises(XRDConnectionError):
            client.request("HEAD", url / "anything")


# ---------------------------------------------------------------------------
# File objects
# ---------------------------------------------------------------------------


def test_reading_is_a_get_and_seeking_is_a_ranged_get(dav):
    with open_http(dav.url / "d/a.root", "rb") as fh:
        assert fh.read(5) == b"hello"
        fh.seek(6)
        assert fh.read() == b"world"
        assert fh.seek(0, io.SEEK_END) == len(BODY)


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


def test_a_server_that_ignores_ranges_still_gives_the_right_bytes(dav):
    dav.ignore_ranges = True
    with open_http(dav.url / "d/a.root", "rb") as fh:
        fh.seek(6)
        assert fh.read() == b"world"


def test_a_body_cut_short_of_its_promise_is_an_error_not_a_download(dav):
    """A connection dropped mid-body must not pass for a complete transfer."""
    dav.truncate_at = 5
    with HTTPClient(Config()) as client:
        with pytest.raises(TransientError, match="short of the declared Content-Length"):
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
    ranged = [h.get("Range") for m, h in zip(dav.seen, dav.headers) if m[0] == "GET"]
    assert any(r for r in ranged)  # it resumed with at least one ranged GET


def test_text_mode_iterates_lines(dav):
    dav.add_file("/d/lines.txt", b"one\ntwo\n")
    with xrdclient.open(dav.url / "d/lines.txt", "r") as fh:
        assert list(fh) == ["one\n", "two\n"]


def test_a_small_write_is_one_put_with_a_length(dav):
    with open_http(dav.url / "d/small.bin", "wb") as fh:
        fh.write(b"payload")
    assert dav.contents("/d/small.bin") == b"payload"
    assert dav.seen.count(("PUT", "/d/small.bin")) == 1


def test_a_large_write_streams_as_a_chunked_put(dav):
    payload = bytes(range(256)) * 8
    with open_http(dav.url / "d/big.bin", "wb", config=Config(chunk_size=64)) as fh:
        fh.write(payload)
    assert dav.contents("/d/big.bin") == payload


def test_an_empty_write_does_not_end_a_streamed_upload(dav):
    """A zero-length chunk is the end-of-body marker in chunked encoding."""
    with open_http(dav.url / "d/w.bin", "wb", buffering=0, config=Config(chunk_size=4)) as fh:
        for piece in (b"AAAAAA", b"", b"BBBB"):
            fh.write(piece)
        assert fh.write(b"") == 0
    assert dav.contents("/d/w.bin") == b"AAAAAABBBB"


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
        (conn,) = fs.client._pool.values()
        sock = conn.sock
        fh.seek(100)
        assert fh.read(10) == bytes(range(100, 110))
        assert conn.sock is sock


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


def test_the_collection_itself_is_not_one_of_its_children(fs):
    assert "d" not in fs.listdir("/d")


def test_mkdir_is_mkcol_with_pathlib_semantics(fs, dav):
    fs.mkdir("/d/new")
    assert "/d/new" in dav.dirs
    with pytest.raises(FileExistsError):
        fs.mkdir("/d/new")
    fs.mkdir("/d/new", exist_ok=True)
    fs.makedirs("/d/deep/er/still", exist_ok=True)
    assert "/d/deep/er/still" in dav.dirs
    with pytest.raises(FileNotFoundError):
        fs.mkdir("/d/absent/child")


def _grid_namespace(dav):
    """A storage element's top levels: there, but refusing MKCOL with 403.

    EOS answers MKCOL on ``/eos/`` like this, and dCache on ``/pnfs/``; a
    ``makedirs`` that started from the root could never get past them.
    """
    dav.add_dir("/eos/lhcb/user")

    def refuse_the_top(method, path, headers):
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


def test_remove_rename_and_touch(fs, dav):
    fs.touch("/d/t.txt")
    assert dav.contents("/d/t.txt") == b""
    with pytest.raises(FileExistsError):
        fs.touch("/d/t.txt", exist_ok=False)
    fs.rename("/d/t.txt", "/d/renamed.txt")
    assert "/d/renamed.txt" in dav.files
    fs.remove("/d/renamed.txt")
    assert "/d/renamed.txt" not in dav.files
    with pytest.raises(FileNotFoundError):
        fs.remove("/d/renamed.txt")


def test_rmdir_keeps_the_posix_promise_that_it_is_empty(fs, dav):
    with pytest.raises(OSError, match="not empty"):
        fs.rmdir("/d")
    fs.rmdir("/d/sub")
    assert "/d/sub" not in dav.dirs


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
    for call in (
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
    ):
        with pytest.raises(UnsupportedError):
            call()


def test_the_unsupported_overrides_keep_the_signature_they_replace(fs):
    """Passing the real arguments must say 'no WebDAV equivalent', not TypeError."""
    for call in (
        lambda: fs.locate("/d/a.root", flags=LocateFlags.REFRESH),
        lambda: fs.prepare(["/d/a.root"], flags=PrepareFlags.EVICT, priority=1),
        lambda: fs.query(QueryCode.CONFIG, "version"),
        lambda: fs.statvfs(),
        lambda: fs.query_stats("io"),
        lambda: fs.query_space(),
        lambda: fs.stat("/d/a.root", follow_symlinks=False),
        lambda: fs.utime("/d/a.root", (1, 2), ns=None),
    ):
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


def test_a_hex_digest_comes_back_verbatim(dav):
    result = digest(dav.url / "d/a.root", "adler32")
    assert result.value == checksum_bytes("adler32", BODY)
    assert str(result).startswith("adler32:")


def test_a_base64_digest_is_decoded_to_hex(dav):
    assert digest(dav.url / "d/a.root", "md5").value == checksum_bytes("md5", BODY)
    assert digest(dav.url / "d/a.root", "sha256").value == checksum_bytes("sha256", BODY)


def test_a_base64_crc64_is_decoded_even_though_hashlib_never_heard_of_it(dav):
    """A CRC is not a hash, so the width that tells hex from base64 has to be
    written down rather than asked of :mod:`hashlib`."""
    assert digest(dav.url / "d/a.root", "crc64").value == checksum_bytes("crc64", BODY)


def test_a_hex_crc64_is_left_as_it_is():
    """Sixteen hex digits is the ``root://`` and WebDAV spelling; twelve
    characters of base64 is the RFC 9530 one, and they cannot be confused."""
    value = checksum_bytes("crc64", BODY)
    assert _pick_digest(f"crc64={value}", "crc64") == value


def test_a_server_that_offers_no_digest_says_so(dav, fs):
    dav.digests = False
    with pytest.raises(UnsupportedError, match="digest"):
        fs.checksum("/d/a.root")


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
    assert result.checksum.value == checksum_bytes("adler32", BODY)


def test_a_copy_into_webdav_verifies_itself(dav, tmp_path):
    source = tmp_path / "up.bin"
    source.write_bytes(b"uploaded")
    result = xrdclient.copy(source, dav.url / "d/up.bin")
    assert dav.contents("/d/up.bin") == b"uploaded"
    assert result.verified
    with pytest.raises(FileExistsError):
        xrdclient.copy(source, dav.url / "d/up.bin", overwrite=False)


# ---------------------------------------------------------------------------
# Macaroons
# ---------------------------------------------------------------------------


def test_a_macaroon_is_minted_and_usable_as_a_token(dav):
    token = macaroon(dav.url / "d", caveats=["activity:DOWNLOAD"], validity="PT10M")
    assert token == dav.macaroon
    dav.require_token = token
    with xrdclient.FileSystem(dav.url, Config(token=token)) as fs:
        assert fs.stat("/d/a.root").st_size == len(BODY)


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
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>'
        b'<D:multistatus xmlns:D="DAV:"/>'
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


def test_a_pull_moves_the_bytes_without_them_passing_through_us(dav, elsewhere):
    """The whole point: one COPY from us, and the data goes server to server."""
    result = xrdclient.third_party(
        dav.url / "d/a.root", elsewhere.url / "d/copy.root", verify=False
    )
    assert elsewhere.contents("/d/copy.root") == BODY
    assert elsewhere.seen == [("COPY", "/d/copy.root")]
    assert ("GET", "/d/a.root") in dav.seen
    assert result.size == len(BODY)


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
    assert tpc._minted(far, config, None, write=False, timeout=None) == "m"
    assert tpc._minted(far, config, None, write=True, timeout=300) == "m"
    assert requests[0][1:] == (["activity:LIST,DOWNLOAD"], "PT30M")
    assert requests[1][1:] == (["activity:LIST,DOWNLOAD,MANAGE,UPLOAD,DELETE"], "PT20M")
    # Nothing to mint: plain http, a token already in hand, or a far end that cannot.
    assert tpc._minted(xrdclient.url.parse("http://se/f"), config, None, write=False,
                       timeout=None) is None
    assert tpc._minted(far, xrdclient.Config(token="t"), None, write=False, timeout=None) is None

    def refuse(*args, **kwargs):
        raise PermissionError("no macaroons here")

    monkeypatch.setattr(dav_module, "macaroon", refuse)
    assert tpc._minted(far, config, None, write=False, timeout=None) is None


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
    elsewhere.handlers["COPY"] = lambda method, path, headers: seen.append(headers) or None
    dav.redirects["/d/r.root"] = str(elsewhere.url / "d/r.root")
    original = client_module.relays_transfer_headers
    client_module.relays_transfer_headers = lambda origin, target: True
    try:
        xrdclient.http.third_party(
            elsewhere.url / "d/a.root", dav.url / "d/r.root",
            remote_token="for-the-far-side", verify=False,
            config=xrdclient.Config(token="mine"),
        )
    except OSError:
        pass  # the fake cannot finish a copy it was redirected into; the headers are the point
    finally:
        client_module.relays_transfer_headers = original
    assert seen and seen[0].get("TransferHeaderAuthorization") == "Bearer for-the-far-side"
    assert "Authorization" not in seen[0]


def test_the_copy_asks_for_it_the_way_the_wlcg_dialect_says(dav, elsewhere):
    xrdclient.third_party(dav.url / "d/a.root", elsewhere.url / "d/copy.root")
    headers = elsewhere.copies[-1]
    assert headers["Source"] == f"http://{dav.url.netloc}/d/a.root"
    assert headers["Overwrite"] == "T"
    # Without this a server that supports delegation waits for a credential
    # that a token-authenticated transfer is never going to send.
    assert headers["Credential"] == "none"
    assert "Destination" not in headers


def test_a_push_hands_the_destination_to_the_source_instead(dav, elsewhere):
    """For a destination that cannot open outbound connections."""
    from xrdclient.http import third_party

    third_party(dav.url / "d/a.root", elsewhere.url / "d/pushed.root", mode="push")
    assert elsewhere.contents("/d/pushed.root") == BODY
    assert dav.copies[-1]["Destination"] == f"http://{elsewhere.url.netloc}/d/pushed.root"
    assert not elsewhere.copies


def test_a_failure_after_the_202_is_still_a_failure(dav, elsewhere):
    """The status line says Accepted; only the body says what happened."""
    with pytest.raises(xrdclient.errors.NotFoundError, match="third-party copy failed"):
        xrdclient.third_party(dav.url / "d/missing.root", elsewhere.url / "d/copy.root")
    assert "/d/copy.root" not in elsewhere.files


def test_a_failure_that_quotes_no_status_is_still_raised(dav, elsewhere):
    elsewhere.tpc_failure = "the pool node went away"
    with pytest.raises(OSError, match="the pool node went away"):
        xrdclient.third_party(dav.url / "d/a.root", elsewhere.url / "d/copy.root")


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


def test_the_ambient_token_is_used_when_the_url_carries_none(dav, elsewhere):
    dav.require_token = elsewhere.require_token = "ambient"
    xrdclient.third_party(
        dav.url / "d/a.root",
        elsewhere.url / "d/copy.root",
        config=Config(token="ambient"),
    )
    assert elsewhere.copies[-1]["TransferHeaderAuthorization"] == "Bearer ambient"


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


def test_saying_nothing_about_checksums_leaves_the_server_policy_alone(dav, elsewhere):
    xrdclient.third_party(dav.url / "d/a.root", elsewhere.url / "d/copy.root")
    assert "RequireChecksumVerification" not in elsewhere.copies[-1]


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


def test_progress_follows_the_performance_markers(dav, elsewhere):
    from xrdclient.http import third_party

    elsewhere.tpc_markers = 4
    seen = []
    third_party(
        dav.url / "d/a.root",
        elsewhere.url / "d/copy.root",
        progress=lambda done, total: seen.append(done),
    )
    assert seen == sorted(seen) and len(seen) == 4
    assert seen[-1] == len(BODY)


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


def test_markers_without_a_byte_count_are_ignored_not_fatal():
    body = "Perf Marker\nTimestamp: 1\nEnd\n" + _perf(0, 7) + "success\n"
    assert _read(body) == 7


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


def test_an_https_endpoint_gets_a_tls_connection():
    """Nothing is dialled here - only the kind of connection is at issue."""
    import http.client as stdlib

    with HTTPClient(Config()) as client:
        secure = client._connect(parse("https://h.example/f.root"))
        plain = client._connect(parse("http://h.example/f.root"))
    assert isinstance(secure, stdlib.HTTPSConnection)
    assert isinstance(plain, stdlib.HTTPConnection)
    assert not isinstance(plain, stdlib.HTTPSConnection)


def test_discarding_a_connection_nobody_pooled_is_quiet():
    with HTTPClient(Config()) as client:
        client._discard(parse("http://h.example/f.root"))


def test_a_timeout_is_reported_as_a_timeout_not_a_connection_failure():

    from xrdclient.http.client import _wrap

    wrapped = _wrap(TimeoutError("slow"), "GET", parse("http://h.example/f"))
    assert isinstance(wrapped, XRDTimeoutError)
    assert "timed out" in str(wrapped)


def test_a_digest_that_is_neither_hex_nor_base64_is_passed_through():
    from xrdclient.http.dav import _as_hex, _is_hex

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

    with HTTPRawIO(dav.url / "d/a.root", "rb", config=Config()) as raw:
        with pytest.raises(io.UnsupportedOperation, match="not writable"):
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


def test_prepare_stages_over_the_tape_api_and_returns_the_request_id(fs, dav):
    dav.nearline.add("/d/a.root")
    handle = fs.prepare(["/d/a.root"])
    assert dav.staged[handle] == ["/d/a.root"]
    assert ("POST", "/api/v1/stage") in dav.seen


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
    for call in (
        lambda: fs.prepare(["/d/a.root"]),
        lambda: fs.query_prepare("stage-0001", ["/d/a.root"]),
        lambda: fs.archive_info(["/d/a.root"]),
        lambda: fs.cancel_prepare("stage-0001"),
    ):
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
    dav.handlers["POST"] = lambda *_: (200, b'{"files": [{"path": "/d/a.root", '
                                            b'"locality": "LOST"}]}', {})
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
        client.abandon(response)
        assert response.isclosed()
        assert not client._pool
        # Not retried, so this only works on a connection with nothing left
        # on it; the fake refuses the empty request, which is an answer.
        stage = dav.url / "api/v1/stage"
        assert client.request("POST", stage, body=b"{}", expect=(400,)).status == 400


def test_abandoning_a_short_remainder_keeps_the_connection(dav):
    dav.add_file("/d/small.bin", bytes(200))
    with HTTPClient(Config()) as client:
        response = client.open("GET", dav.url / "d/small.bin")
        response.read(10)
        (conn,) = client._pool.values()
        client.abandon(response)
        assert response.isclosed()
        assert list(client._pool.values()) == [conn]


def test_abandoning_a_finished_response_is_harmless(dav):
    with HTTPClient(Config()) as client:
        response = client.open("GET", dav.url / "d/a.root")
        response.read()
        client.abandon(response)
        client.abandon(response)
        assert len(client._pool) == 1


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
