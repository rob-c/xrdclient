"""``Expect: 100-continue``: an upload a server redirects before reading it.

EOS's head node answers a ``PUT`` with ``307`` to the disk server that will
hold the file and hangs up without reading the body. A client that had
started sending found the connection gone under it - ``SSL: BAD_LENGTH`` on
real EOS - and never saw the redirect, so every upload over a few kilobytes
to EOS, RAL's Echo or NCBJ failed. :class:`FakeDAVServer`'s
``early_redirects`` is that head node.
"""

from __future__ import annotations

import os

import pytest

import xrdclient
from xrdclient.config import Config
from xrdclient.http import HTTPClient, expect
from xrdclient.testing import FakeDAVServer

BIG = os.urandom(expect.EXPECT_OVER * 4 + 7)


@pytest.fixture
def head():
    """The head node: redirects every upload, reads none of them."""
    with FakeDAVServer(dirs=["/d"]) as server:
        server.early_redirects = True
        yield server


@pytest.fixture
def disk():
    """The disk server the head node sends uploads to."""
    with FakeDAVServer(dirs=["/d"]) as server:
        yield server


def test_a_small_body_goes_straight_out(disk):
    """Under :data:`EXPECT_OVER` the round trip buys nothing."""
    with HTTPClient(Config()) as client:
        client.request("PUT", disk.url / "d/s.bin", body=b"tiny", expect=(201,))
    assert "Expect" not in disk.headers[0]
    assert disk.contents("/d/s.bin") == b"tiny"


def test_a_streamed_upload_follows_the_redirect_before_its_first_chunk(head, disk):
    """``open(..., "wb")`` past one chunk: a chunked ``PUT`` of unknown length."""
    head.redirects["/d/big.bin"] = str(disk.url / "d/big.bin")
    data = os.urandom(3 * 1024 * 1024 + 11)
    config = Config(chunk_size=1024 * 1024)
    with xrdclient.open(str(head.url / "d/big.bin"), "wb", config=config) as fh:
        for start in range(0, len(data), 256 * 1024):
            fh.write(data[start : start + 256 * 1024])
    assert disk.contents("/d/big.bin") == data
    assert head.headers[0]["Expect"] == "100-continue"
    assert head.headers[0]["Transfer-Encoding"] == "chunked"


def test_a_streamed_upload_refused_up_front_raises_what_the_server_said(head):
    head.early_redirects = False
    head.handlers["PUT"] = lambda method, path, headers: (403, b"quota", {})
    target, config = str(head.url / "d/big.bin"), Config(chunk_size=1024)
    with pytest.raises(PermissionError), xrdclient.open(target, "wb", config=config) as fh:
        fh.write(os.urandom(4096))


def test_the_copy_engine_uploads_through_an_early_redirect(head, disk, tmp_path):
    source = tmp_path / "src.bin"
    source.write_bytes(BIG)
    head.redirects["/d/c.bin"] = str(disk.url / "d/c.bin")
    xrdclient.copy(str(source), str(head.url / "d/c.bin"))
    assert disk.contents("/d/c.bin") == BIG
