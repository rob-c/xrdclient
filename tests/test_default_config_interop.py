"""Writes and reads with the configuration a user actually gets.

The rest of the suite mostly pins ``data_streams=0``, and that hid a write
that hung against stock xrootd: by default a file binds a data sub-stream, a
stock daemon answers a write whose payload came down that stream *on the
stream*, and the client was listening on the control link for the whole
``request_timeout``. Everything here uses ``Config()`` as shipped, bar the
login order a loopback daemon needs and a request timeout short enough that a
regression fails in seconds rather than minutes - the time bound on each test
is what actually catches it.
"""

from __future__ import annotations

import time

import pytest

import xrdclient
from xrdclient.client.file import File
from xrdclient.config import Config
from xrdclient.flags import OpenFlags

pytestmark = pytest.mark.interop

#: What a loopback daemon needs and nothing else. A write that is not answered
#: would take ``request_timeout``; each test finishes well inside it.
_DEFAULT = Config(auth_order=("unix", "host"), request_timeout=20.0, connect_timeout=10.0)
#: Far below the request timeout, far above what loopback needs.
_QUICK = 5.0
BLOB = bytes(range(256)) * 1024  # 256 KiB, every byte value present


def _url(server, path: str) -> str:
    return f"{server.url.rstrip('/')}//{path.lstrip('/')}"


def test_the_shipped_default_binds_a_data_stream():
    # The premise of this file: if this ever stops being true, the tests below
    # no longer exercise the default they claim to.
    assert _DEFAULT.data_streams == Config().data_streams >= 1


def test_a_default_file_writes_and_reads_back_promptly(real_server, sandbox):
    path = f"{sandbox}/w.root"
    started = time.monotonic()
    fh = File(_url(real_server, path), _DEFAULT)
    fh.open(OpenFlags.NEW | OpenFlags.UPDATE)
    try:
        assert fh._data_paths, "the default open bound no data stream"
        assert fh.write(BLOB, 0) == len(BLOB)
        assert fh.write(b"tail", len(BLOB)) == 4
        fh.pgwrite(b"pages", len(BLOB) + 4)
    finally:
        fh.close()
    with File(_url(real_server, path), _DEFAULT) as back:
        assert back.read() == BLOB + b"tail" + b"pages"
    assert time.monotonic() - started < _QUICK


def test_a_default_open_url_round_trips_promptly(real_server, sandbox):
    path = f"{sandbox}/io.root"
    started = time.monotonic()
    with xrdclient.open(_url(real_server, path), "wb", config=_DEFAULT) as out:
        out.write(BLOB)
    with xrdclient.open(_url(real_server, path), "r+b", config=_DEFAULT) as out:
        out.write(b"head")
    with xrdclient.open(_url(real_server, path), "rb", config=_DEFAULT) as back:
        assert back.read() == b"head" + BLOB[4:]
    assert time.monotonic() - started < _QUICK


def test_a_manually_bound_path_still_writes_promptly(real_server, sandbox):
    # The manual API names a path for reads; a write must not be split onto
    # it, or the daemon answers where nobody is listening.
    path = f"{sandbox}/manual.root"
    started = time.monotonic()
    fh = File(_url(real_server, path), _DEFAULT)
    fh.open(OpenFlags.NEW | OpenFlags.UPDATE)
    try:
        assert fh.bind_data_path()
        assert fh.write(b"bound", 0) == 5
        fh.pgwrite(b"pages", 5)
        assert fh.read(10, 0) == b"boundpages"
    finally:
        fh.close()
    assert time.monotonic() - started < _QUICK


def test_a_default_copy_goes_both_ways_promptly(real_server, sandbox, tmp_path):
    local = tmp_path / "up.root"
    local.write_bytes(BLOB)
    started = time.monotonic()
    xrdclient.copy(str(local), _url(real_server, f"{sandbox}/up.root"), config=_DEFAULT)
    down = tmp_path / "down.root"
    xrdclient.copy(_url(real_server, f"{sandbox}/up.root"), str(down), config=_DEFAULT)
    assert down.read_bytes() == BLOB
    assert time.monotonic() - started < _QUICK
