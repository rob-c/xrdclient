"""HTTP and WebDAV conformance: what happens when the server misbehaves.

A storage element's HTTP door is written by someone else, and half of them
are proxies. So the questions here are not "does a listing work" - that is
:mod:`test_http` - but "what does the client do with an answer no correct
server would send": a multistatus with no ``href`` in it, an entry pointing
at ``/etc/passwd``, a ranged ``GET`` answered short, a redirect with a token
in its ``Location``, a DTD hidden past the first kilobyte.

Every one of these is served by :class:`~xrdclient.testing.FakeDAVServer` through
its :attr:`~xrdclient.testing.FakeDAVServer.handlers` hook, so the hostile answer
arrives over a real socket, through the real ``http.client``, exactly as it
would from a real endpoint.
"""

from __future__ import annotations

import base64
import io
import json

import pytest

import xrdclient
from xrdclient.config import Config
from xrdclient.crypto import checksum_bytes
from xrdclient.errors import ProtocolError, ServerError, UnsupportedError
from xrdclient.http import HTTPClient, digest, macaroon, open_http, propfind
from xrdclient.testing import FakeDAVServer

BODY = b"hello world"
XML = '<?xml version="1.0" encoding="utf-8"?>'


@pytest.fixture
def dav():
    with FakeDAVServer(files={"/d/a.root": BODY}, dirs=["/d/sub"]) as server:
        yield server


@pytest.fixture
def fs(dav):
    filesystem = xrdclient.FileSystem(dav.url)
    try:
        yield filesystem
    finally:
        filesystem.close()


@pytest.fixture
def watch(dav):
    """Install a recorder for one verb: the headers of every such request."""

    def install(method):
        seen: list[dict[str, str]] = []

        def handler(_method, _path, headers):
            seen.append(headers)
            return None  # and let the real implementation answer it

        dav.handlers[method] = handler
        return seen

    return install


def canned(body, status=207, **headers):
    """A handler that answers every request of its verb the same way."""
    sent = {name.replace("_", "-"): value for name, value in headers.items()}
    sent.setdefault("Content-Type", 'text/xml; charset="utf-8"')

    def handler(_method, _path, _headers):
        return status, body, sent

    return handler


def multistatus(*responses):
    body = XML + '<D:multistatus xmlns:D="DAV:">' + "".join(responses) + "</D:multistatus>"
    return body.encode()


def entry(href, *, kind="", size=None, status="HTTP/1.1 200 OK", props=""):
    """One ``<D:response>``, spelled out so a test can bend any part of it."""
    length = f"<D:getcontentlength>{size}</D:getcontentlength>" if size is not None else ""
    return (
        f"<D:response><D:href>{href}</D:href><D:propstat><D:prop>"
        f"<D:resourcetype>{kind}</D:resourcetype>{length}{props}"
        f"</D:prop><D:status>{status}</D:status></D:propstat></D:response>"
    )


COLLECTION = "<D:collection/>"


# ---------------------------------------------------------------------------
# The shape of a multistatus
# ---------------------------------------------------------------------------


def test_a_response_with_no_href_is_refused(dav, fs):
    """Without an href there is nothing to say the properties are *about*."""
    dav.handlers["PROPFIND"] = canned(
        multistatus(
            "<D:response><D:propstat><D:prop><D:resourcetype/></D:prop>"
            "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
        )
    )
    with pytest.raises(ProtocolError, match="no href"):
        fs.stat("/d/a.root")


def test_an_href_is_reduced_to_the_path_it_names(dav):
    dav.handlers["PROPFIND"] = canned(
        multistatus(entry("https://other.example.org:8443/d/a.root", size=5))
    )
    assert propfind(dav.url / "d/a.root")[0][0] == "/d/a.root"


def test_a_percent_encoded_href_is_decoded(dav, fs):
    dav.handlers["PROPFIND"] = canned(
        multistatus(entry("/d/", kind=COLLECTION), entry("/d/a%20b.root", size=3))
    )
    assert [e.name for e in fs.scandir("/d")] == ["a b.root"]


def test_the_collection_itself_is_dropped_however_its_href_is_spelled(dav, fs):
    dav.handlers["PROPFIND"] = canned(
        multistatus(entry("/d", kind=COLLECTION), entry("/d/", kind=COLLECTION))
    )
    assert fs.scandir("/d") == []


def test_an_entry_outside_the_collection_is_refused(dav, fs):
    """A listing names what is in a directory, not what the server fancies."""
    dav.handlers["PROPFIND"] = canned(
        multistatus(entry("/d/", kind=COLLECTION), entry("/etc/passwd", size=1))
    )
    with pytest.raises(ProtocolError, match="not in it"):
        fs.scandir("/d")


def test_an_entry_from_a_deeper_level_is_refused(dav, fs):
    dav.handlers["PROPFIND"] = canned(
        multistatus(entry("/d/", kind=COLLECTION), entry("/d/sub/deep.root", size=1))
    )
    with pytest.raises(ProtocolError, match="not in it"):
        fs.scandir("/d")


def test_a_dot_dot_in_an_href_does_not_reach_the_parent(dav, fs):
    dav.handlers["PROPFIND"] = canned(
        multistatus(entry("/d/", kind=COLLECTION), entry("/d/../secret", size=1))
    )
    with pytest.raises(ProtocolError, match="not in it"):
        fs.scandir("/d")


def test_the_first_propstat_that_succeeded_is_the_one_believed(dav, fs):
    body = multistatus(
        "<D:response><D:href>/d/a.root</D:href>"
        "<D:propstat><D:prop><D:getcontentlength>999</D:getcontentlength></D:prop>"
        "<D:status>HTTP/1.1 404 Not Found</D:status></D:propstat>"
        "<D:propstat><D:prop><D:resourcetype/>"
        "<D:getcontentlength>5</D:getcontentlength></D:prop>"
        "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
    )
    dav.handlers["PROPFIND"] = canned(body)
    assert fs.stat("/d/a.root").st_size == 5


def test_properties_the_server_said_it_could_not_read_are_not_used(dav, fs):
    dav.handlers["PROPFIND"] = canned(
        multistatus(entry("/d/a.root", size=999, status="HTTP/1.1 403 Forbidden"))
    )
    assert fs.stat("/d/a.root").st_size == 0


def test_a_length_that_is_not_a_number_is_zero_rather_than_a_crash(dav, fs):
    dav.handlers["PROPFIND"] = canned(multistatus(entry("/d/a.root", size="huge")))
    assert fs.stat("/d/a.root").st_size == 0


def test_a_negative_length_is_not_believed_either(dav, fs):
    dav.handlers["PROPFIND"] = canned(multistatus(entry("/d/a.root", size=-1)))
    assert fs.stat("/d/a.root").st_size == 0


def test_a_collection_is_a_directory_and_everything_else_is_a_file(dav, fs):
    dav.handlers["PROPFIND"] = canned(multistatus(entry("/d/x", kind=COLLECTION)))
    assert fs.stat("/d/x").is_dir()
    dav.handlers["PROPFIND"] = canned(multistatus(entry("/d/x", size=1)))
    assert fs.stat("/d/x").is_file()


def test_both_date_shapes_webdav_uses_are_understood(dav, fs):
    dav.handlers["PROPFIND"] = canned(
        multistatus(
            entry(
                "/d/a.root",
                size=1,
                props=(
                    "<D:getlastmodified>Tue, 15 Nov 1994 12:45:26 GMT</D:getlastmodified>"
                    "<D:creationdate>1994-11-05T08:15:30Z</D:creationdate>"
                ),
            )
        )
    )
    info = fs.stat("/d/a.root")
    assert info.st_mtime == 784903526
    assert info.st_ctime == 784023330


def test_a_date_nobody_can_read_is_zero_not_an_error(dav, fs):
    dav.handlers["PROPFIND"] = canned(
        multistatus(
            entry("/d/a.root", size=1, props="<D:getlastmodified>whenever</D:getlastmodified>")
        )
    )
    assert fs.stat("/d/a.root").st_mtime == 0


def test_a_multistatus_in_somebody_elses_namespace_says_nothing(dav, fs):
    """``DAV:`` is the namespace; an element that merely looks right is not."""
    body = (
        XML + '<multistatus xmlns="urn:not-dav"><response><href>/d/a.root</href>'
        "</response></multistatus>"
    ).encode()
    dav.handlers["PROPFIND"] = canned(body)
    with pytest.raises(FileNotFoundError):
        fs.stat("/d/a.root")


def test_a_body_that_is_not_a_multistatus_at_all_says_nothing(dav, fs):
    dav.handlers["PROPFIND"] = canned((XML + "<html><body>hi</body></html>").encode())
    with pytest.raises(FileNotFoundError):
        fs.stat("/d/a.root")


def test_a_body_that_is_not_xml_is_a_protocol_error(dav, fs):
    dav.handlers["PROPFIND"] = canned(b"<not xml at all", status=200)
    with pytest.raises(ProtocolError, match="malformed"):
        fs.stat("/d/a.root")


def test_an_empty_body_is_a_protocol_error(dav, fs):
    dav.handlers["PROPFIND"] = canned(b"")
    with pytest.raises(ProtocolError, match="malformed"):
        fs.stat("/d/a.root")


def test_a_status_that_is_not_a_multistatus_is_not_parsed(dav, fs):
    """``PROPFIND`` answering ``204`` means the body is not a listing."""
    dav.handlers["PROPFIND"] = canned(b"", status=204)
    with pytest.raises(OSError):
        fs.stat("/d/a.root")


def test_a_body_past_the_cap_is_truncated_rather_than_buffered(dav, monkeypatch):
    """A PROPFIND on a huge collection is how a client's memory gets eaten."""
    monkeypatch.setattr("xrdclient.http.client.MAX_BODY", 64)
    dav.handlers["PROPFIND"] = canned(
        multistatus(*[entry(f"/d/f{i}.root", size=i) for i in range(200)])
    )
    with pytest.raises(ProtocolError, match="malformed"):
        propfind(dav.url / "d", depth=1)


def test_a_listing_of_thousands_of_entries_still_arrives_whole(dav, fs):
    body = multistatus(
        entry("/d/", kind=COLLECTION), *[entry(f"/d/f{i}.root", size=i) for i in range(2000)]
    )
    dav.handlers["PROPFIND"] = canned(body)
    entries = fs.scandir("/d")
    assert len(entries) == 2000
    assert entries[1999].name == "f1999.root"


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


def _bait(dav):
    return f"http://{dav.address[0]}:{dav.address[1]}/secret"


def test_an_external_entity_is_never_fetched(dav, fs):
    """The classic XXE: the entity points back at a URL we would notice."""
    body = (
        f'{XML}<!DOCTYPE D:multistatus [<!ENTITY xxe SYSTEM "{_bait(dav)}">]>'
        '<D:multistatus xmlns:D="DAV:"><D:response><D:href>&xxe;</D:href>'
        "</D:response></D:multistatus>"
    ).encode()
    dav.handlers["PROPFIND"] = canned(body)
    with pytest.raises(ProtocolError, match="document type"):
        fs.stat("/d/a.root")
    assert ("GET", "/secret") not in dav.seen


def test_a_document_type_hidden_past_the_first_kilobyte_is_still_refused(dav, fs):
    padding = "<!--" + "x" * 2000 + "-->"
    body = (
        f"{XML}{padding}<!DOCTYPE D:multistatus [<!ENTITY xxe SYSTEM \"{_bait(dav)}\">]>"
        '<D:multistatus xmlns:D="DAV:"/>'
    ).encode()
    dav.handlers["PROPFIND"] = canned(body)
    with pytest.raises(ProtocolError, match="document type"):
        fs.stat("/d/a.root")
    assert ("GET", "/secret") not in dav.seen


def test_an_entity_declared_past_the_guard_still_fetches_nothing(dav, fs):
    """Belt and braces: :mod:`xml.etree` resolves no external entity either."""
    padding = "<!--" + "x" * 5000 + "-->"
    body = (
        f"{XML}{padding}<!DOCTYPE D:multistatus [<!ENTITY xxe SYSTEM \"{_bait(dav)}\">]>"
        '<D:multistatus xmlns:D="DAV:"><D:response><D:href>&xxe;</D:href>'
        "</D:response></D:multistatus>"
    ).encode()
    dav.handlers["PROPFIND"] = canned(body)
    with pytest.raises(ProtocolError, match="malformed"):
        fs.stat("/d/a.root")
    assert ("GET", "/secret") not in dav.seen


def test_a_billion_laughs_never_starts_expanding(dav, fs):
    laughs = "".join(
        f'<!ENTITY lol{i} "&lol{i - 1};&lol{i - 1};&lol{i - 1};&lol{i - 1};">'
        for i in range(1, 12)
    )
    body = (
        f'{XML}<!DOCTYPE lolz [<!ENTITY lol "lol">{laughs}]>'
        '<D:multistatus xmlns:D="DAV:"><D:response><D:href>&lol11;</D:href>'
        "</D:response></D:multistatus>"
    ).encode()
    dav.handlers["PROPFIND"] = canned(body)
    with pytest.raises(ProtocolError, match="document type"):
        fs.stat("/d/a.root")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_a_ranged_get_asks_for_what_it_still_needs(dav, watch):
    seen = watch("GET")
    with open_http(dav.url / "d/a.root", "rb") as fh:
        fh.seek(6)
        assert fh.read() == b"world"
    assert seen[-1]["Range"] == "bytes=6-"


def test_the_first_read_asks_for_no_range_at_all(dav, watch):
    seen = watch("GET")
    with open_http(dav.url / "d/a.root", "rb") as fh:
        assert fh.read() == BODY
    assert "Range" not in seen[0]


def test_a_server_that_ignores_the_range_is_skipped_forward_not_trusted(dav):
    dav.ignore_ranges = True
    with open_http(dav.url / "d/a.root", "rb") as fh:
        fh.seek(6)
        assert fh.read(3) == b"wor"
        assert fh.read() == b"ld"


def test_an_answer_shorter_than_the_file_is_a_short_read_not_a_hang(dav):
    dav.handlers["GET"] = canned(b"hell", status=206, Content_Type="application/octet-stream")
    with open_http(dav.url / "d/a.root", "rb") as fh:
        assert fh.read() == b"hell"


def test_a_resource_that_declares_no_length_has_size_zero(dav):
    dav.handlers["HEAD"] = canned(b"", status=200)
    with open_http(dav.url / "d/a.root", "rb", buffering=0) as fh:
        assert fh.size == 0
        assert fh.seek(0, io.SEEK_END) == 0


def test_seeking_past_the_end_reads_nothing(dav):
    with open_http(dav.url / "d/a.root", "rb") as fh:
        fh.seek(1000)
        assert fh.read() == b""


def test_a_negative_seek_is_refused(dav):
    with open_http(dav.url / "d/a.root", "rb") as fh:
        with pytest.raises(OSError, match="negative seek"):
            fh.seek(-1)


def test_seeking_backwards_starts_a_fresh_get(dav):
    with open_http(dav.url / "d/a.root", "rb", buffering=0) as fh:
        assert fh.read(5) == b"hello"
        fh.seek(0)
        assert fh.read(5) == b"hello"
    assert dav.seen.count(("GET", "/d/a.root")) == 2


def test_seeking_where_we_already_are_costs_nothing(dav):
    with open_http(dav.url / "d/a.root", "rb", buffering=0) as fh:
        fh.read(5)
        assert fh.seek(5) == 5
        assert fh.read() == b" world"
    assert dav.seen.count(("GET", "/d/a.root")) == 1


def test_a_seek_to_a_known_place_asks_the_server_nothing(dav):
    """Only SEEK_END needs the size, and only the size costs a request."""
    with open_http(dav.url / "d/a.root", "rb", buffering=0) as fh:
        fh.read(2)
        fh.seek(0)
        fh.seek(3, io.SEEK_CUR)
    assert [method for method, _ in dav.seen] == ["GET"]


def test_a_seek_to_the_end_asks_once_and_keeps_the_bytes_straight(dav):
    with open_http(dav.url / "d/a.root", "rb", buffering=0) as fh:
        assert fh.read(5) == b"hello"
        assert fh.seek(0, io.SEEK_END) == len(BODY)
        assert fh.seek(-5, io.SEEK_END) == 6
        assert fh.read() == b"world"
        assert fh.seek(0, io.SEEK_END) == len(BODY)
    assert dav.seen.count(("HEAD", "/d/a.root")) == 1


def test_an_unknown_whence_is_refused(dav):
    with open_http(dav.url / "d/a.root", "rb", buffering=0) as fh:
        with pytest.raises(ValueError, match="whence"):
            fh.seek(0, 7)


def test_a_write_only_file_can_neither_be_read_nor_seeked(dav):
    fh = open_http(dav.url / "d/w.bin", "wb", buffering=0)
    try:
        with pytest.raises(io.UnsupportedOperation, match="not readable"):
            fh.readinto(bytearray(4))
        with pytest.raises(io.UnsupportedOperation, match="not seekable"):
            fh.seek(0)
        assert not fh.seekable()
    finally:
        fh.close()


def test_reading_a_collection_gets_whatever_the_server_serves_for_it(dav):
    """A GET on a collection is HTML, not a listing; it is not an error."""
    with open_http(dav.url / "d", "rb") as fh:
        assert fh.read() == b""


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def test_a_streamed_upload_that_is_redirected_says_where_to_put_it(dav):
    dav.handlers["PUT"] = canned(b"", status=307, Location="/d/elsewhere.bin")
    fh = open_http(dav.url / "d/big.bin", "wb", config=Config(chunk_size=8))
    fh.write(b"x" * 64)
    with pytest.raises(ProtocolError, match="PUT that URL directly"):
        fh.close()


def test_a_streamed_upload_that_is_refused_raises_what_the_status_means(dav):
    dav.handlers["PUT"] = canned(b"full", status=507)
    fh = open_http(dav.url / "d/big.bin", "wb", config=Config(chunk_size=8))
    fh.write(b"x" * 64)
    with pytest.raises(ServerError, match="507"):
        fh.close()


def test_a_failed_upload_still_closes_the_file(dav):
    dav.handlers["PUT"] = canned(b"nope", status=500)
    fh = open_http(dav.url / "d/big.bin", "wb", buffering=0, config=Config(chunk_size=8))
    fh.write(b"x" * 64)
    with pytest.raises(ServerError):
        fh.close()
    assert fh.closed
    fh.close()  # and the second close does not try again
    assert dav.seen.count(("PUT", "/d/big.bin")) == 1


def test_a_file_opened_for_writing_and_never_written_to_creates_it_empty(dav):
    with open_http(dav.url / "d/empty.bin", "wb"):
        pass
    assert dav.contents("/d/empty.bin") == b""


def test_a_write_of_exactly_the_chunk_size_is_still_one_put(dav):
    """The switch to streaming is *past* the buffer, not at it."""
    with open_http(dav.url / "d/edge.bin", "wb", buffering=0, config=Config(chunk_size=8)) as fh:
        fh.write(b"12345678")
    assert dav.contents("/d/edge.bin") == b"12345678"
    assert "chunked" not in str(dav.bodies)


def test_text_mode_writes_go_through_the_same_put(dav):
    with xrdclient.open(dav.url / "d/t.txt", "w", encoding="utf-8") as fh:
        fh.write("héllo\n")
    assert dav.contents("/d/t.txt") == "héllo\n".encode()
    with xrdclient.open(dav.url / "d/t.txt", "r", encoding="utf-8") as fh:
        assert fh.read() == "héllo\n"


def test_an_exclusive_create_that_loses_the_race_is_an_exists_error(dav):
    """The ``If-None-Match`` is the real guard; the HEAD is only a shortcut."""
    dav.handlers["PUT"] = canned(b"exists", status=412)
    fh = open_http(dav.url / "d/race.bin", "xb", config=Config(chunk_size=8))
    fh.write(b"x" * 64)
    with pytest.raises(FileExistsError):
        fh.close()


# ---------------------------------------------------------------------------
# Redirects
# ---------------------------------------------------------------------------


@pytest.fixture
def elsewhere():
    with FakeDAVServer(files={"/d/a.root": b"from the other one"}, dirs=["/d"]) as server:
        yield server


def test_a_redirect_across_hosts_is_followed(dav, elsewhere):
    dav.redirects["/d/a.root"] = str(elsewhere.url / "d/a.root")
    with HTTPClient(Config()) as client:
        assert client.request("GET", dav.url / "d/a.root").body == b"from the other one"
    assert ("GET", "/d/a.root") in elsewhere.seen


def test_a_relative_location_is_resolved_against_the_url_that_sent_it(dav):
    dav.add_file("/d/b.root", b"second")
    dav.redirects["/d/a.root"] = "b.root"
    with HTTPClient(Config()) as client:
        assert client.request("GET", dav.url / "d/a.root").body == b"second"


def test_a_303_turns_the_next_request_into_a_get(dav):
    calls = []

    def once(method, path, headers):
        calls.append(method)
        return (303, b"", {"Location": "/d/a.root"}) if len(calls) == 1 else None

    dav.handlers["PUT"] = once
    with HTTPClient(Config()) as client:
        assert client.request("PUT", dav.url / "d/x.bin", body=b"ignored").body == BODY
    assert dav.seen[-1] == ("GET", "/d/a.root")


def test_a_redirect_with_nowhere_to_go_is_not_followed(dav):
    dav.handlers["GET"] = canned(b"", status=307)
    with HTTPClient(Config()) as client:
        assert client.request("GET", dav.url / "d/a.root").status == 307
    assert dav.seen.count(("GET", "/d/a.root")) == 1


def test_a_redirected_put_carries_its_body_to_the_new_place(dav):
    dav.redirects["/d/x.bin"] = "/d/y.bin"
    with open_http(dav.url / "d/x.bin", "wb") as fh:
        fh.write(b"payload")
    assert dav.contents("/d/y.bin") == b"payload"
    assert "/d/x.bin" not in dav.files


def test_a_token_in_a_redirect_target_becomes_a_header_not_a_query(dav, elsewhere):
    """Redirecting with a signed URL is how every real door hands off."""
    elsewhere.require_token = "SIGNED"
    dav.redirects["/d/a.root"] = f"{elsewhere.url / 'd/a.root'}?authz=SIGNED"
    with HTTPClient(Config()) as client:
        assert client.request("GET", dav.url / "d/a.root").body == b"from the other one"
    assert elsewhere.targets == ["/d/a.root"]


def test_the_redirect_budget_counts_hops_not_hosts(dav, elsewhere):
    dav.redirects["/d/a.root"] = str(elsewhere.url / "d/a.root")
    elsewhere.redirects["/d/a.root"] = str(dav.url / "d/a.root")
    with HTTPClient(Config(redirect_limit=1)) as client:
        with pytest.raises(xrdclient.errors.RedirectLimitError):
            client.request("GET", dav.url / "d/a.root")


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def test_a_401_is_the_permission_error_python_expects(dav, fs):
    dav.require_token = "T"
    with pytest.raises(PermissionError):
        fs.stat("/d/a.root")


def test_a_token_never_appears_in_a_request_target(dav):
    dav.require_token = "SECRET"
    url = dav.url.evolve(path="/d/a.root", query={"authz": "SECRET"})
    with HTTPClient(Config()) as client:
        assert client.request("GET", url).body == BODY
    assert dav.targets == ["/d/a.root"]


def test_other_query_parameters_do_reach_the_server(dav):
    url = dav.url.evolve(path="/d/a.root", query={"authz": "S", "xrdclient.k": "1"})
    with HTTPClient(Config()) as client:
        client.request("GET", url)
    assert dav.targets == ["/d/a.root?xrdclient.k=1"]


def test_the_token_is_presented_on_every_request_of_a_walk(dav, watch):
    dav.require_token = "T"
    seen = watch("PROPFIND")
    with xrdclient.FileSystem(dav.url, Config(token="T")) as filesystem:
        assert [root for root, _, _ in filesystem.walk("/d")] == ["/d", "/d/sub"]
    assert len(seen) >= 2
    assert all(h["Authorization"] == "Bearer T" for h in seen)


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("algorithm", ["adler32", "crc32c", "md5", "sha256"])
def test_every_digest_the_server_offers_comes_back_as_hex(dav, algorithm):
    assert digest(dav.url / "d/a.root", algorithm).value == checksum_bytes(algorithm, BODY)


@pytest.mark.parametrize(
    ("algorithm", "wire"),
    [("adler32", "adler32"), ("md5", "md5"), ("sha256", "sha-256"), ("sha512", "sha-512")],
)
def test_the_request_names_the_algorithm_the_way_rfc3230_does(dav, watch, algorithm, wire):
    seen = watch("HEAD")
    try:
        digest(dav.url / "d/a.root", algorithm)
    except UnsupportedError:
        pass  # the fake only offers some of them; the request is the point
    assert seen[0]["Want-Digest"] == wire


def test_our_digest_is_picked_out_of_a_crowded_header(dav):
    dav.handlers["HEAD"] = canned(
        b"", status=200, Digest="md5=aGk=, adler32=00010203 , sha-256=aGk="
    )
    assert digest(dav.url / "d/a.root", "adler32").value == "00010203"


def test_an_algorithm_name_in_capitals_is_still_ours(dav):
    dav.handlers["HEAD"] = canned(b"", status=200, Digest="ADLER32=DEADBEEF")
    assert digest(dav.url / "d/a.root", "adler32").value == "deadbeef"


def test_a_content_md5_is_understood_even_though_it_names_nothing(dav):
    """RFC 1864 predates RFC 3230, and dCache still sends it."""
    raw = base64.b64encode(bytes.fromhex(checksum_bytes("md5", BODY))).decode()
    dav.handlers["HEAD"] = canned(b"", status=200, Content_MD5=raw)
    assert digest(dav.url / "d/a.root", "md5").value == checksum_bytes("md5", BODY)


def test_a_content_md5_is_not_offered_up_as_some_other_algorithm(dav):
    dav.handlers["HEAD"] = canned(b"", status=200, Content_MD5="aGk=")
    with pytest.raises(UnsupportedError, match="sha256"):
        digest(dav.url / "d/a.root", "sha256")


def test_a_digest_that_is_not_base64_is_taken_as_the_hex_it_looks_like(dav):
    value = checksum_bytes("sha256", BODY)
    dav.handlers["HEAD"] = canned(b"", status=200, Digest=f"sha-256={value}")
    assert digest(dav.url / "d/a.root", "sha256").value == value


def test_an_empty_digest_header_says_the_server_offered_nothing(dav):
    dav.handlers["HEAD"] = canned(b"", status=200, Digest="")
    with pytest.raises(UnsupportedError, match="digest"):
        digest(dav.url / "d/a.root", "adler32")


# ---------------------------------------------------------------------------
# What the client asks for
# ---------------------------------------------------------------------------


def test_a_stat_asks_for_depth_zero_and_a_listing_for_depth_one(dav, fs, watch):
    seen = watch("PROPFIND")
    fs.stat("/d/a.root")
    fs.scandir("/d")
    assert [h["Depth"] for h in seen] == ["0", "1"]


def test_the_propfind_asks_only_for_the_properties_it_can_use(dav, fs):
    fs.stat("/d/a.root")
    body = dav.bodies[-1]
    assert b"<D:getcontentlength/>" in body and b"<D:resourcetype/>" in body
    assert b"allprop" not in body


def test_a_listing_asks_for_the_collection_with_a_trailing_slash(dav, fs):
    fs.scandir("/d")
    assert dav.targets[-1] == "/d/"


def test_a_stat_asks_for_the_resource_without_one(dav, fs):
    fs.stat("/d/a.root")
    assert dav.targets[-1] == "/d/a.root"


def test_a_rename_names_the_destination_absolutely(dav, fs, watch):
    seen = watch("MOVE")
    fs.rename("/d/a.root", "/d/b.root")
    assert seen[0]["Destination"].endswith("/d/b.root")
    assert seen[0]["Destination"].startswith("http://")
    assert dav.contents("/d/b.root") == BODY


def test_a_path_that_climbs_out_of_itself_is_normalised_before_it_is_sent(dav, fs):
    fs.stat("/d/sub/../a.root")
    assert dav.targets[-1] == "/d/a.root"


# ---------------------------------------------------------------------------
# Macaroons
# ---------------------------------------------------------------------------


def test_the_macaroon_request_says_what_it_is_asking_for(dav):
    macaroon(dav.url / "d", caveats=["activity:DOWNLOAD,LIST"], validity="PT10M")
    asked = json.loads(dav.bodies[-1])
    assert asked == {"caveats": ["activity:DOWNLOAD,LIST"], "validity": "PT10M"}


def test_a_macaroon_response_that_is_not_json_is_an_error(dav):
    dav.handlers["POST"] = canned(b"not json", status=200)
    with pytest.raises(ProtocolError, match="macaroon"):
        macaroon(dav.url / "d")


def test_a_macaroon_response_without_the_key_is_an_error(dav):
    dav.handlers["POST"] = canned(b'{"uri": {}}', status=200)
    with pytest.raises(ProtocolError, match="macaroon"):
        macaroon(dav.url / "d")


def test_a_macaroon_response_that_is_a_list_is_an_error(dav):
    dav.handlers["POST"] = canned(b"[1, 2, 3]", status=200)
    with pytest.raises(ProtocolError, match="macaroon"):
        macaroon(dav.url / "d")


# ---------------------------------------------------------------------------
# The write verbs, and what a door says no with
# ---------------------------------------------------------------------------


def test_exclusive_create_refuses_a_file_that_is_already_there(fs, dav):
    with pytest.raises(FileExistsError):
        with fs.open("/d/a.root", "xb") as handle:
            handle.write(b"mine now")
    assert dav.contents("/d/a.root") == BODY


def test_exclusive_create_still_loses_the_race_gracefully(fs, dav):
    """The ``If-None-Match`` on the PUT is what closes the check-then-write gap."""
    handle = fs.open("/d/race.root", "xb")
    dav.add_file("/d/race.root", b"someone else got there first")
    handle.write(b"mine now")
    with pytest.raises(FileExistsError):
        handle.close()
    assert dav.contents("/d/race.root") == b"someone else got there first"


def test_exclusive_create_succeeds_where_nothing_is_there_yet(fs, dav):
    with fs.open("/d/new.root", "xb") as handle:
        handle.write(b"mine now")
    assert dav.contents("/d/new.root") == b"mine now"


def test_writing_into_a_collection_that_does_not_exist_is_a_missing_path(fs):
    """RFC 4918 answers 409 for a missing parent; POSIX calls that ENOENT."""
    with pytest.raises(FileNotFoundError):
        with fs.open("/nowhere/x.root", "wb") as handle:
            handle.write(b"data")


def test_renaming_a_collection_takes_its_contents_with_it(fs, dav):
    dav.add_file("/d/sub/child.root", b"child")
    fs.rename("/d/sub", "/d/moved")
    assert fs.isdir("/d/moved") and not fs.exists("/d/sub")
    assert dav.contents("/d/moved/child.root") == b"child"


def test_renaming_something_that_is_not_there_says_so(fs):
    with pytest.raises(FileNotFoundError):
        fs.rename("/d/absent.root", "/d/b.root")


def test_renaming_over_an_existing_resource_replaces_it(fs, dav):
    dav.add_file("/d/b.root", b"older")
    fs.rename("/d/a.root", "/d/b.root")
    assert dav.contents("/d/b.root") == BODY


# ---------------------------------------------------------------------------
# A door that wants a token on every verb
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attempt",
    [
        pytest.param(lambda fs: fs.ping(), id="OPTIONS"),
        pytest.param(lambda fs: fs.remove("/d/a.root"), id="DELETE"),
        pytest.param(lambda fs: fs.mkdir("/d/new"), id="MKCOL"),
        pytest.param(lambda fs: fs.rename("/d/a.root", "/d/b.root"), id="MOVE"),
        pytest.param(lambda fs: fs.listdir("/d"), id="PROPFIND"),
    ],
)
def test_every_verb_goes_through_the_same_gate(fs, dav, attempt):
    """An unauthenticated client is refused whatever it asks for."""
    dav.require_token = "the-right-one"
    with pytest.raises(PermissionError):
        attempt(fs)


def test_third_party_copy_is_refused_at_the_gate_as_well(dav):
    dav.require_token = "the-right-one"
    with pytest.raises(PermissionError):
        xrdclient.third_party(dav.url / "d/a.root", dav.url / "d/copy.root")


# ---------------------------------------------------------------------------
# Third-party copy, when the far side is the problem
# ---------------------------------------------------------------------------


def test_a_pull_from_a_host_that_is_not_listening_is_reported(dav, closed_port):
    host, port = closed_port
    with pytest.raises(ServerError, match="cannot reach"):
        xrdclient.third_party(f"http://{host}:{port}/d/a.root", dav.url / "d/pulled.root")


def test_a_push_of_a_file_the_source_does_not_have_is_reported(dav, closed_port):
    host, port = closed_port
    from xrdclient.http import third_party

    with pytest.raises(ServerError, match="not here to push"):
        third_party(dav.url / "d/absent.root", f"http://{host}:{port}/d/x.root", mode="push")
