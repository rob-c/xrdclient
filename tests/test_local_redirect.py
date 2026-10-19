"""A redirect to ``file://``: the file read from this machine's disk, as XrdCl does."""

from __future__ import annotations

import os
import stat as stat_module

import pytest

import xrdclient
from xrdclient.errors import NotFoundError, ProtocolError, UnsupportedError
from xrdclient.proto import constants as c
from xrdclient.proto import requests as r
from xrdclient.session import SESSIONS, local
from xrdclient.testing import FakeServer
from xrdclient.transport.sync import SocketTransport

PAYLOAD = bytes(range(256)) * 8192  # 2 MiB: large enough for the bulk path to be tried


@pytest.fixture
def on_disk(tmp_path):
    path = tmp_path / "cached.root"
    path.write_bytes(PAYLOAD)
    return str(path)


@pytest.fixture
def remote():
    with FakeServer(files={"/f": b"the remote copy"}) as srv:
        yield srv


def _redirect(srv: FakeServer, opcode: int, path: str, host: str = "localhost") -> None:
    srv.redirects[opcode] = (f"file://{host}{path}", -1, "cache=1")


def test_an_open_redirected_to_local_disk_reads_the_local_file(remote, on_disk, config):
    _redirect(remote, c.kXR_open, on_disk)
    with xrdclient.File(remote.url.with_path("/f"), config) as fh:
        assert fh.size == len(PAYLOAD)
        assert fh.read(4, 256) == PAYLOAD[256:260]
        assert fh.read() == PAYLOAD
        assert fh.readv([(0, 3), (1000, 5)]) == [PAYLOAD[:3], PAYLOAD[1000:1005]]
        buffer = bytearray(10)
        assert fh.readinto(buffer, 20) == 10 and bytes(buffer) == PAYLOAD[20:30]
        assert fh.stat(refresh=True).st_size == len(PAYLOAD)
        assert fh.endpoint == "localhost:0"
    assert c.kXR_read not in remote.seen


def test_the_buffered_api_reads_through_a_local_redirect(remote, on_disk, config):
    _redirect(remote, c.kXR_open, on_disk)
    with xrdclient.open(remote.url.with_path("/f"), "rb", config=config) as handle:
        assert handle.read(5) == PAYLOAD[:5]


def test_a_stat_redirected_to_local_disk_is_answered_from_it(remote, on_disk, config):
    _redirect(remote, c.kXR_stat, on_disk, host="")
    with xrdclient.FileSystem(remote.url, config) as fs:
        info = fs.stat("/f")
    assert info.st_size == len(PAYLOAD) and stat_module.S_ISREG(info.st_mode)


def test_the_local_file_is_never_pooled(remote, on_disk, config):
    _redirect(remote, c.kXR_open, on_disk)
    before = len(SESSIONS)
    with xrdclient.File(remote.url.with_path("/f"), config) as fh:
        router = fh._router
    assert len(SESSIONS) <= before + 1  # the manager's connection, at most
    assert router._session is None


def test_writing_through_a_local_redirect_is_refused(remote, on_disk, config):
    _redirect(remote, c.kXR_open, on_disk)
    fh = xrdclient.File(remote.url.with_path("/f"), config)
    with pytest.raises(UnsupportedError, match="writing through a file:// redirect"):
        fh.open("w")
    with open(on_disk, "rb") as written:
        assert written.read() == PAYLOAD


def test_what_a_daemon_would_do_is_refused_by_name(remote, on_disk, config):
    _redirect(remote, c.kXR_open, on_disk)
    with xrdclient.File(remote.url.with_path("/f"), config) as fh:
        with pytest.raises(UnsupportedError, match="only open, read, vector read"):
            fh.checksum("adler32")
        with pytest.raises(UnsupportedError, match="no data paths"):
            fh.bind_data_path()


def test_a_local_file_that_is_not_there_is_not_found(remote, tmp_path, config):
    _redirect(remote, c.kXR_open, str(tmp_path / "gone"))
    with pytest.raises(NotFoundError):
        xrdclient.File(remote.url.with_path("/f"), config).open()


def test_a_stat_of_a_missing_local_file_is_not_found(remote, tmp_path, config):
    _redirect(remote, c.kXR_stat, str(tmp_path / "gone"))
    with xrdclient.FileSystem(remote.url, config) as fs, pytest.raises(NotFoundError):
        fs.stat("/f")


def test_never_refuses_a_local_redirect(remote, on_disk, config):
    _redirect(remote, c.kXR_open, on_disk)
    settings = config.evolve(local_redirect="never")
    with pytest.raises(ProtocolError, match="local_redirect is 'never'"):
        xrdclient.File(remote.url.with_path("/f"), settings).open()


def test_by_default_only_a_server_on_this_host_may_send_one(remote, on_disk, config, monkeypatch):
    monkeypatch.setattr(SocketTransport, "peer_is_local", property(lambda self: False))
    _redirect(remote, c.kXR_open, on_disk)
    with pytest.raises(ProtocolError, match="not on this host"):
        xrdclient.File(remote.url.with_path("/f"), config).open()
    _redirect(remote, c.kXR_open, on_disk)
    with xrdclient.File(remote.url.with_path("/f"), config.evolve(local_redirect="always")) as fh:
        assert fh.read(3, 0) == PAYLOAD[:3]


def test_a_file_url_on_another_host_is_refused(remote, on_disk, config):
    _redirect(remote, c.kXR_open, on_disk, host="elsewhere.example")
    with pytest.raises(ProtocolError, match="a file on another host"):
        xrdclient.File(remote.url.with_path("/f"), config).open()


def test_a_redirect_to_another_protocol_is_still_refused(remote, config):
    remote.redirects[c.kXR_open] = ("https://elsewhere.example/f", -1, "")
    with pytest.raises(ProtocolError, match="not XRootD"):
        xrdclient.File(remote.url.with_path("/f"), config).open()


# -- the local file itself --------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "path"),
    [
        ("file:///data/f", "/data/f"),
        ("file://localhost/data/f?x=1", "/data/f"),
        ("FILE://LOCALHOST/data/f", "/data/f"),
        ("root://host//f", None),
        ("host:1094", None),
    ],
)
def test_the_local_path_a_redirect_names(url, path):
    assert local.target_path(url) == path


def test_a_stat_line_describes_what_is_there(tmp_path):
    script = tmp_path / "run"
    script.write_bytes(b"#!/bin/sh\n")
    script.chmod(0o755)
    line = local._stat_line(os.stat(script)).decode()
    flags = int(line.split()[2])
    assert flags & c.kXR_xset and flags & c.kXR_readable and flags & c.kXR_writable
    assert int(local._stat_line(os.stat(tmp_path)).split()[2]) & c.kXR_isDir
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    assert int(local._stat_line(os.stat(fifo)).split()[2]) & c.kXR_other
    script.chmod(0o000)
    assert int(local._stat_line(os.stat(script)).split()[2]) == 0
    script.chmod(0o600)


def test_a_local_file_answers_like_a_session(on_disk, config):
    handle = local.LocalFile(on_disk, config)
    assert not handle.broken and not handle.closed and handle.ask_arrival_routing() is False
    handle.mark_broken()
    with pytest.raises(UnsupportedError, match="is not open"):
        handle.execute(r.Read(local.HANDLE, 0, 1))
    assert handle.execute(r.Open(on_disk, 0)).data == local.HANDLE
    assert handle.execute(r.Open(on_disk, 0)).data == local.HANDLE  # a second open is harmless
    assert handle.execute(r.Read(local.HANDLE, 0, 4)).data == PAYLOAD[:4]
    assert "open" in repr(handle)
    handle.execute(r.Close(local.HANDLE))
    handle.close()
    assert handle.closed and "closed" in repr(handle)


def test_a_read_error_on_local_disk_is_an_io_error(on_disk, config, monkeypatch):
    handle = local.LocalFile(on_disk, config)
    handle.execute(r.Open(on_disk, 0))

    def failing(*args):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "pread", failing)
    from xrdclient.errors import ServerError

    with pytest.raises(ServerError) as caught:
        handle.execute(r.Read(local.HANDLE, 0, 1))
    assert caught.value.code == 3007
    handle.close()


def test_a_local_file_with_no_reader_left_closes_itself(on_disk, config):
    import gc

    handle = local.LocalFile(on_disk, config)
    handle.execute(r.Open(on_disk, 0))
    fd = handle._fd
    assert fd is not None
    del handle
    gc.collect()
    with pytest.raises(OSError):
        os.fstat(fd)
