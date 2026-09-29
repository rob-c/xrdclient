"""The corners of several modules that no broader test happens to walk into.

Each test here pins one behaviour a caller can see - a fallback that has to
leave the destination as it found it, a server that answers a ``HEAD``
without a length, a listing that has to step over a dangling link - rather
than a line number. They are grouped by the module whose behaviour they pin.
"""

from __future__ import annotations

import base64
import http.server
import io
import json
import os
import stat as _stat
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field, fields, replace
from typing import ClassVar

import pytest

import xrdclient
from xrdclient import FileSystem, diagnose
from xrdclient.auth.base import Offer
from xrdclient.auth.ztn import TokenCredential
from xrdclient.config import Config, _ambient, override
from xrdclient.copy import engine
from xrdclient.errors import (
    ChecksumMismatchError,
    NotFoundError,
    UnsupportedError,
    kXR_Unsupported,
)
from xrdclient.flags import StatInfoFlags
from xrdclient.http.file import HTTPRawIO
from xrdclient.proto import constants as c

BODY = b"hello world"


def _jwt(exp: int) -> str:
    """An unsigned JWT carrying only ``exp`` - all a lifetime check reads."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    claims = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{header}.{claims}.sig"


# ---------------------------------------------------------------------------
# auth.ztn and the doctor's summary of it
# ---------------------------------------------------------------------------


def test_a_token_with_the_life_the_server_wants_is_used():
    token = _jwt(int(time.time()) + 3600)
    cred = TokenCredential.available(
        Offer("ztn", "300:4096:"), Config(token=token), username="", host="h"
    )
    assert cred is not None and cred.token == token


def test_the_doctor_says_a_token_can_prove_who_you_are():
    config = replace(Config(), auth_order=("ztn",), token=_jwt(int(time.time()) + 3600))
    check = next(check for check in diagnose(config=config) if check.name == "auth")
    assert (check.state, check.detail) == ("ok", "ztn can prove who you are")


# ---------------------------------------------------------------------------
# config: the ambient defaults, on a class shaped unlike Config
# ---------------------------------------------------------------------------


def test_the_ambient_defaults_leave_what_is_not_a_default_alone():
    """A class variable, two required fields and a ``field(default=...)``."""

    @dataclass(frozen=True)
    @_ambient
    class Shaped:
        kind: ClassVar[str] = "shaped"
        username: str
        token: str = field(repr=False)
        pool_size: int = field(default=2)

    assert [f.name for f in fields(Shaped)] == ["username", "token", "pool_size"]
    assert Shaped.kind == "shaped"
    assert Shaped("u", "t").pool_size == 2
    with override(pool_size=7, token="ambient"):
        assert Shaped("u", "t").pool_size == 7
        assert Shaped("u", "t", pool_size=3).pool_size == 3
        with pytest.raises(TypeError):
            Shaped("u")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# copy.engine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("overwrite", [False, True])
def test_a_bulk_download_declined_falls_back_to_the_pump(server, config, tmp_path, overwrite):
    """A ``kXR_wait`` on the bulk path hands the file to the pump.

    An exclusive create has to still be exclusive when the pump makes the
    file again, so the bulk path's empty file is gone by then.
    """
    server.waits[c.kXR_read] = 1
    target = tmp_path / "out.root"
    result = xrdclient.copy(server.url / "data/a.root", target, overwrite=overwrite, config=config)
    assert target.read_bytes() == BODY and result.size == len(BODY)
    assert server.waits[c.kXR_read] == 0


def test_a_bulk_download_to_a_stream_falls_back_too(server, config):
    server.waits[c.kXR_read] = 1
    sink = io.BytesIO()
    xrdclient.copy(server.url / "data/a.root", sink, config=config)
    assert sink.getvalue() == BODY


def test_a_download_that_insists_on_verifying_fails_when_it_cannot(server, config, monkeypatch):
    def refuse(*args, **kwargs):
        raise UnsupportedError(kXR_Unsupported, "this server cannot checksum")

    monkeypatch.setattr(engine, "_server_checksum", refuse)
    sink = io.BytesIO()
    with pytest.raises(UnsupportedError):
        xrdclient.copy(server.url / "data/a.root", sink, verify=True, config=config)
    assert sink.getvalue() == b""  # asked first, so nothing was moved


def test_a_download_whose_bytes_disagree_with_the_servers_checksum_fails(
    server, config, monkeypatch
):
    monkeypatch.setattr("xrdclient.testing.server._checksum", lambda algorithm, data: "deadbeef")
    with pytest.raises(ChecksumMismatchError) as caught:
        xrdclient.copy(server.url / "data/a.root", io.BytesIO(), config=config)
    assert caught.value.expected == "deadbeef"


# ---------------------------------------------------------------------------
# easy: the local half of the verbs
# ---------------------------------------------------------------------------


def test_a_verb_takes_a_path_object(server, config):
    with xrdclient.Path(server.url.with_path("/data/a.root"), config) as path:
        assert xrdclient.size(path, config=config) == len(BODY)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes here")
def test_a_local_stat_of_something_neither_file_nor_directory(tmp_path):
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    flags = xrdclient.stat(str(pipe)).flags
    assert flags & StatInfoFlags.OTHER and not flags & StatInfoFlags.IS_DIR


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root can read and write anything"
)
def test_a_local_stat_reports_this_users_access_not_the_mode(tmp_path):
    write_only, read_only = tmp_path / "w", tmp_path / "r"
    for path, mode in ((write_only, _stat.S_IWUSR), (read_only, _stat.S_IRUSR)):
        path.write_bytes(b"x")
        path.chmod(mode)
    flags = xrdclient.stat(str(write_only)).flags
    assert flags & StatInfoFlags.IS_WRITABLE and not flags & StatInfoFlags.IS_READABLE
    flags = xrdclient.stat(str(read_only)).flags
    assert flags & StatInfoFlags.IS_READABLE and not flags & StatInfoFlags.IS_WRITABLE


def test_a_local_mkdir_without_parents_needs_them(tmp_path):
    xrdclient.mkdir(str(tmp_path / "one"), parents=False)
    assert (tmp_path / "one").is_dir()
    with pytest.raises(FileNotFoundError):
        xrdclient.mkdir(str(tmp_path / "a" / "b"), parents=False)


def test_removing_a_missing_local_file_is_an_error_unless_forgiven(tmp_path):
    with pytest.raises(FileNotFoundError):
        xrdclient.remove(str(tmp_path / "gone"))
    xrdclient.remove(str(tmp_path / "gone"), missing_ok=True)


def test_a_local_move_that_cannot_rename_says_why(tmp_path):
    """Only a cross-device rename becomes a copy; a missing source is just missing."""
    with pytest.raises(FileNotFoundError):
        xrdclient.move(str(tmp_path / "gone"), str(tmp_path / "there"))
    assert not (tmp_path / "there").exists()


# ---------------------------------------------------------------------------
# http.file: a resource whose HEAD declares no length
# ---------------------------------------------------------------------------


class _NoLengthOnHead(http.server.BaseHTTPRequestHandler):
    """A server that sends a body on ``GET`` but no ``Content-Length`` on ``HEAD``.

    What a gateway streaming a generated resource does: it cannot say how
    long the thing is until it has made it.
    """

    protocol_version = "HTTP/1.1"

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.end_headers()

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def no_length() -> Iterator[str]:
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _NoLengthOnHead)
    thread = threading.Thread(target=httpd.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/f"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_an_undeclared_length_is_not_taken_for_an_end(no_length):
    with HTTPRawIO(no_length, "rb", config=Config()) as raw:
        assert raw.size == 0
        assert raw.read() == BODY


# ---------------------------------------------------------------------------
# testing.server: the fake behaving as stock xrootd does
# ---------------------------------------------------------------------------


def test_a_stat_listing_leaves_out_a_dangling_link(server, config):
    server.links["/data/dangling"] = "/nowhere"
    with FileSystem(server.url, config) as fs:
        assert "dangling" in fs.listdir("/data")
        assert "dangling" not in [entry.name for entry in fs.scandir("/data")]


def test_removing_a_directory_that_is_not_there_is_not_found(server, config):
    with FileSystem(server.url, config) as fs, pytest.raises(NotFoundError):
        fs.rmdir("/nope")
