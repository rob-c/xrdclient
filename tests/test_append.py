"""Append mode lands every write at the end of the file, as ``O_APPEND`` does.

The position a stream reports is a convenience for the caller; in ``"a"`` it
has no say over where the bytes go. Writing where the file ended when it was
opened, rather than where it ends now, overwrites whatever arrived since - a
seek of the caller's own, or another writer's append.
"""

from __future__ import annotations

import io

import pytest

from xrdclient.client.file import File
from xrdclient.io import XRootDRawIO, open_url
from xrdclient.testing import FakeServer


@pytest.fixture
def srv():
    with FakeServer(files={"/log": b"HELLO"}) as server:
        yield server


def test_a_seek_does_not_move_an_append(srv):
    with open_url(f"{srv.url}/log", "a+b") as stream:
        stream.seek(0)
        stream.write(b"XY")
    assert srv.contents("/log") == b"HELLOXY"


def test_an_append_lands_after_what_another_writer_appended(srv):
    stream = open_url(f"{srv.url}/log", "ab")
    srv.files["/log"] += b"6789"
    stream.write(b"Z")
    stream.close()
    assert srv.contents("/log") == b"HELLO6789Z"


def test_the_position_after_an_append_is_the_new_end(srv, config):
    handle = File(f"{srv.url}/log", config)
    with XRootDRawIO(handle, "ab+") as raw:
        raw.seek(1)
        assert raw.write(b"!!") == 2
        assert raw.tell() == 7
        raw.seek(0)
        assert raw.read() == b"HELLO!!"


def test_a_file_that_grew_after_open_is_read_to_its_end(srv):
    with open_url(f"{srv.url}/log", "rb") as stream:
        srv.files["/log"] += b" WORLD"
        assert stream.read() == b"HELLO WORLD"


def test_a_raw_readall_reads_what_arrived_after_open(srv, config):
    handle = File(f"{srv.url}/log", config)
    with XRootDRawIO(handle, "rb") as raw:
        raw.seek(2)
        srv.files["/log"] += b"!"
        assert raw.readall() == b"LLO!"
        assert raw.readall() == b""


def test_append_matches_the_builtin(srv, tmp_path):
    """The same sequence of calls, remote and local, ends in the same bytes."""
    local = tmp_path / "log"
    local.write_bytes(b"HELLO")
    for stream in (open(local, "a+b"), open_url(f"{srv.url}/log", "a+b")):
        with stream:
            stream.write(b"1")
            stream.seek(0, io.SEEK_SET)
            stream.write(b"2")
            stream.flush()
            stream.seek(0)
            assert stream.read() == b"HELLO12"
    assert srv.contents("/log") == local.read_bytes()


@pytest.mark.interop
def test_append_on_a_real_server_ignores_the_position(real_server, sandbox):
    """Against the genuine daemon, which opens the file ``kXR_open_apnd``."""
    import xrdclient
    from conftest import _REAL_CONFIG

    path = f"{sandbox}/log"
    with xrdclient.FileSystem(real_server.url, _REAL_CONFIG) as fs:
        fs.write_bytes(path, b"HELLO")
        with fs.open(path, "a+b") as stream:
            stream.seek(0)
            stream.write(b"XY")
        with open(path, "ab") as local:  # another writer, straight to disk
            local.write(b"--")
        with fs.open(path, "ab") as stream:
            stream.write(b"Z")
        assert fs.read_bytes(path) == b"HELLOXY--Z"
