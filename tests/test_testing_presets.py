"""The WebDAV and S3 fakes as presets of xrdhttp's shared storage element.

What the fakes *do* is tested in xrdhttp's suite (``tests/test_testing_*``);
these check the names this package keeps on top: the flat tape, token and
S3 knobs, mapped onto the shared server's state.
"""

from __future__ import annotations

from xrdclient.s3 import Credentials, S3FileSystem
from xrdclient.testing import FakeDAVServer, FakeS3Server
from xrdclient.testing.s3 import _Knob


def test_the_dav_fake_names_its_token_and_tape_state():
    dav = FakeDAVServer()
    assert dav.require_token is None
    dav.require_token = "T"
    assert (dav.require_token, dav.tokens) == ("T", {"T"})
    dav.require_token = None
    assert dav.tokens == set()
    assert not dav.no_tape and dav.sitename == "fake-site"
    dav.sitename = "SITE"
    assert dav.tape is not None and dav.tape.sitename == "SITE"
    dav.no_tape = True
    assert dav.no_tape and dav.tape is None
    assert dav.nearline == set()  # asking for the tape state brings a fresh tape back
    assert dav.tape is not None and dav.sitename == "fake-site"
    dav.no_tape = False
    assert dav.staged == {} and dav.cancelled == set() and dav.released == set()


def test_the_s3_fake_names_its_dialects_knobs():
    assert isinstance(FakeS3Server.forbidden, _Knob)
    with FakeS3Server(access_key="") as bucket:
        bucket.page_size = 7
        assert bucket.dialect.page_size == 7
        with S3FileSystem(
            bucket.url, credentials=Credentials("k", "s"), endpoint=bucket.endpoint
        ) as fs:
            fs.write_bytes("/k", b"v")
        assert bucket.etags == {}
        bucket.dialect.etags["test-bucket/k"] = "abc-2"
        bucket.dialect.etags["other/k"] = "def-1"
        assert bucket.etags == {"k": "abc-2"}
