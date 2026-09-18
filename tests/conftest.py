"""Shared fixtures: hand-built server frames and a scripted peer."""

from __future__ import annotations

import struct

import pytest

from xrdclient.config import Config
from xrdclient.proto import constants as c

_RESP = struct.Struct(">HHI")


def frame(streamid: int, status: int, body: bytes = b"") -> bytes:
    """A complete ``ServerResponseHdr`` plus body."""
    return _RESP.pack(streamid, status, len(body)) + body


def ok(streamid: int, body: bytes = b"") -> bytes:
    return frame(streamid, c.kXR_ok, body)


def error(streamid: int, code: int, message: str) -> bytes:
    return frame(streamid, c.kXR_error, struct.pack(">i", code) + message.encode() + b"\x00")


def handshake_reply() -> bytes:
    """What a server sends back for the 20-byte handshake."""
    return ok(0, struct.pack(">ii", 0, c.ROOTD_PQ))


def protocol_body(
    version: int = 0x0500_0000,
    flags: int = 0,
    *,
    security: bool = False,
    level: int = c.kXR_secNone,
    overrides: dict[int, int] | None = None,
) -> bytes:
    body = struct.pack(">iI", version, flags)
    if security or overrides:
        items = overrides or {}
        body += bytes([ord("S"), 0, 0, 0, level, len(items)])
        for opcode, value in items.items():
            body += bytes([opcode - c.kXR_1stRequest, value])
    return body


def login_body(sessid: bytes = b"\x11" * 16, sec: str = "") -> bytes:
    return sessid + (sec.encode() + b"\x00" if sec else b"")


def stat_line(size: int = 1024, flags: int = 0, mtime: int = 1_700_000_000) -> bytes:
    return f"id0 {size} {flags} {mtime}".encode() + b"\x00"


@pytest.fixture(autouse=True)
def _no_dotfile(tmp_path_factory, monkeypatch):
    """Keep whoever is running the tests out of them.

    ``Config.from_file`` reads ``~/.xrdrc`` when nothing says otherwise, so a
    developer with one would get different results from CI. Pointing
    ``$XRD_CONFIG`` at an empty file is the same as having no file at all,
    and a test about the search order just sets the variable itself.
    """
    empty = tmp_path_factory.mktemp("dotfile") / "config.ini"
    empty.write_text("")
    monkeypatch.setenv("XRD_CONFIG", str(empty))


@pytest.fixture(autouse=True)
def _a_short_data_stream_probe(monkeypatch):
    """Do not spend the suite's time learning what the fake server is.

    A file asks its server once per connection whether it serves a request
    that arrived on a data path, and :class:`~xrdclient.testing.FakeServer` is the
    standard push-only kind that never will - so every connection would sit
    out a whole ``data_stream_timeout`` to be told what this suite already
    knows. A quarter of a second is still an age on loopback, and a test
    about the timeout itself sets its own.
    """
    monkeypatch.setenv("XRD_SUBSTREAMTIMEOUT", "0.25")


@pytest.fixture(autouse=True)
def _no_pooled_connections():
    """Never let one test's connection be handed to the next.

    Every :class:`~xrdclient.testing.FakeServer` gets an ephemeral port, and the
    kernel hands those out again: a connection left in the pool by a test
    whose server has since stopped would match a later test's server by
    address and be reused, dead. Production has the same hazard on a server
    restart and answers it with ``pool_idle_ttl``; a test suite can simply not
    share.
    """
    from xrdclient.session import SESSIONS

    SESSIONS.clear()
    yield
    SESSIONS.clear()


@pytest.fixture
def config() -> Config:
    """A config that never reaches the network or the local filesystem.

    Automatic data sub-streams are off here so the suite exercises the plain
    control-link and the manual :meth:`~xrdclient.File.bind_data_path` API directly;
    the on-by-default behaviour has its own coverage in
    ``test_data_streams_default.py``.
    """
    return Config(
        username="tester", auth_order=("host",), require_tls=False, data_streams=0
    )


@pytest.fixture
def server():
    """A running :class:`~xrdclient.testing.FakeServer` on loopback.

    Pre-populated so the common case - "read a file, list a directory" - needs
    no setup in the test itself.
    """
    from xrdclient.testing import FakeServer

    with FakeServer(files={"/data/a.root": b"hello world"}, dirs=["/data/empty"]) as srv:
        yield srv


@pytest.fixture(scope="session")
def real_server(tmp_path_factory):
    """A genuine ``xrootd`` daemon, or a skip where none is installed.

    Session-scoped because starting one costs a second and every interop test
    wants the same daemon; :func:`sandbox` gives each test its own directory
    inside it so they still cannot collide.
    """
    import _xrootd

    if not _xrootd.available():
        pytest.skip("no xrootd binary on PATH")
    with _xrootd.RealServer(tmp_path_factory.mktemp("export")) as srv:
        yield srv


@pytest.fixture
def sandbox(real_server, request):
    """A fresh directory in the real server's export, named for the test."""
    path = f"{real_server.path()}/{request.node.name[:80]}"
    from xrdclient import FileSystem

    with FileSystem(real_server.url, _REAL_CONFIG) as fs:
        fs.mkdir(path, parents=True)
    return path


#: The real daemon needs no credentials for a loopback unix login, but the
#: default auth order would still try gsi first and wait on a proxy that is
#: not there. Timeouts are short because a local daemon that has not answered
#: in ten seconds is not going to.
_REAL_CONFIG = Config(auth_order=("unix", "host"), request_timeout=10.0, connect_timeout=10.0)


@pytest.fixture
def fs(server, config):
    """A :class:`~xrdclient.FileSystem` pointed at the ``server`` fixture."""
    from xrdclient import FileSystem

    filesystem = FileSystem(server.url, config)
    try:
        yield filesystem
    finally:
        filesystem.close()


@pytest.fixture
def closed_port() -> tuple[str, int]:
    """An address nothing is listening on: bound, read, and released."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()
