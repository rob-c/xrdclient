"""Names over HTTP and S3: what a listing hands back is what ``open`` takes.

The rule under test: a path given to the API is a plain name, encoded
exactly once on its way onto the wire, and a name that comes back in a
listing is decoded exactly once. Anything else lets a name that happens to
contain a ``%`` escape reach a different file.
"""

from __future__ import annotations

import pytest

import xrdclient
from xrdclient.http.client import absolute_url, request_target, wire_path
from xrdclient.http.tpc import _remote_url
from xrdclient.s3 import Credentials, S3FileSystem
from xrdclient.testing import FakeDAVServer, FakeS3Server
from xrdclient.url import parse

#: Names that each break one naive way of encoding a path: an existing
#: escape, a space, a fragment and a query delimiter, a name outside ASCII,
#: the character a form decoder reads as a space, and an unreserved one.
#: ``a%2Fb`` is not here: ``%2F`` is an escaped slash inside a segment, kept
#: as it is both ways (see ``test_an_escaped_slash_stays_one``).
AWKWARD = ["a%41b", "a b", "a#b", "a?b", "日本", "a+b", "a~b", "100%", "a%2541b"]

CREDENTIALS = Credentials("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")


def _body(name: str) -> bytes:
    return f"contents of {name}".encode()


def test_an_existing_escape_in_a_name_is_encoded_like_any_other_percent():
    assert wire_path("/d/a%41b") == "/d/a%2541b"
    assert wire_path("/d/a b/日本") == "/d/a%20b/%E6%97%A5%E6%9C%AC"
    assert wire_path("/d/a+b#c?d~e") == "/d/a%2Bb%23c%3Fd~e"


def test_the_request_target_encodes_the_path_once_and_keeps_the_query():
    url = parse("https://h/d/a%2541b?x=a+b")  # the name ``a%41b``, as a URL spells it
    assert url.path == "/d/a%41b"
    assert request_target(url) == "/d/a%2541b?x=a+b"


def test_a_url_for_another_server_encodes_the_path_once():
    url = parse("davs://h/?x=h:1").with_path("/d/a%41b c")
    assert absolute_url(url) == "https://h:443/d/a%2541b%20c?x=h:1"
    assert _remote_url(parse("dav://h/a%2541b?authz=t")) == "http://h:80/a%2541b"


def test_a_redirect_location_is_decoded_once():
    """A ``Location`` is a real URL, so its escapes are escapes."""
    with FakeDAVServer(files={"/d/a b%41": b"moved"}, dirs=["/d"]) as dav:
        dav.redirects["/d/start"] = "/d/a%20b%2541"
        with xrdclient.FileSystem(dav.url) as fs, fs.open("/d/start", "rb") as handle:
            assert handle.read() == b"moved"


@pytest.fixture
def dav():
    files = {f"/d/{name}": _body(name) for name in AWKWARD}
    # The file a name with a live escape would reach if it were not encoded.
    files["/d/aAb"] = b"WRONG FILE"
    with FakeDAVServer(files=files, dirs=["/d"]) as server:
        yield server


def test_every_listed_dav_name_opens_the_file_it_names(dav):
    with xrdclient.FileSystem(dav.url) as fs:
        names = fs.listdir("/d")
        assert sorted(names) == sorted([*AWKWARD, "aAb"])
        for name in AWKWARD:
            with fs.open(f"/d/{name}", "rb") as handle:
                assert handle.read() == _body(name), name
            assert fs.stat(f"/d/{name}").size == len(_body(name))


def test_a_dav_name_is_written_renamed_and_removed_as_given(dav):
    with xrdclient.FileSystem(dav.url) as fs:
        for name in AWKWARD:
            with fs.open(f"/d/new-{name}", "wb") as handle:
                handle.write(b"new")
            assert dav.contents(f"/d/new-{name}") == b"new"
            fs.rename(f"/d/new-{name}", f"/d/moved-{name}")
            assert dav.contents(f"/d/moved-{name}") == b"new"
            fs.remove(f"/d/moved-{name}")
            assert f"/d/moved-{name}" not in dav.files


@pytest.fixture
def s3():
    objects = {f"d/{name}": _body(name) for name in AWKWARD}
    objects["d/aAb"] = b"WRONG FILE"
    with (
        FakeS3Server(objects=objects) as server,
        S3FileSystem(server.url, credentials=CREDENTIALS, endpoint=server.endpoint) as fs,
    ):
        yield server, fs


def test_every_listed_s3_key_opens_the_object_it_names(s3):
    _server, fs = s3
    names = [entry.name for entry in fs.scandir("/d")]
    assert sorted(names) == sorted([*AWKWARD, "aAb"])
    for name in AWKWARD:
        with fs.open(f"/d/{name}", "rb") as handle:
            assert handle.read() == _body(name), name


def test_an_s3_key_is_written_and_renamed_as_given(s3):
    server, fs = s3
    for name in AWKWARD:
        with fs.open(f"/d/new-{name}", "wb") as handle:
            handle.write(b"new")
        fs.rename(f"/d/new-{name}", f"/d/moved-{name}")
        assert server.objects[f"d/moved-{name}"] == b"new"
        assert f"d/new-{name}" not in server.objects


def test_an_escaped_slash_stays_one():
    """``%2F`` is a slash inside one segment, as a git ref is in a download URL."""
    url = parse("https://hf.example/d/resolve/refs%2Fconvert%2Fparquet/x.parquet")
    assert url.path == "/d/resolve/refs%2Fconvert%2Fparquet/x.parquet"
    assert request_target(url) == "/d/resolve/refs%2Fconvert%2Fparquet/x.parquet"
