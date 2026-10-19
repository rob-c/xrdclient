"""Corners of the async facade, the pyxrootd layer and the HTTP client that no other test reaches.

Each is reached through the module's own seam - a module attribute, a fake
server, a raw socket that answers the way a misbehaving server would - so
the line runs on every platform the suite does.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import socket
import threading
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from xrdhttp.connection import HTTPResponse

import xrdclient
import xrdclient.aio
from xrdclient import aio
from xrdclient.compat.client import env
from xrdclient.compat.client.copyprocess import _names_path
from xrdclient.compat.client.filesystem import _key
from xrdclient.config import Config
from xrdclient.errors import ProtocolError, RedirectLimitError
from xrdclient.http import HTTPClient
from xrdclient.http.file import _skip
from xrdclient.testing import FakeDAVServer, FakeServer
from xrdclient.url import parse

# -- the async facade ---------------------------------------------------------


def test_a_call_cancelled_before_it_started_never_runs(monkeypatch):
    """Nothing was produced, so there is nothing for ``release`` to release."""
    pending: concurrent.futures.Future[int] = concurrent.futures.Future()
    released: list[int] = []

    def never_started(func: object, args: object) -> concurrent.futures.Future[int]:
        return pending

    monkeypatch.setattr(aio, "_submit", never_started)

    async def main() -> None:
        task = asyncio.ensure_future(aio._run_owned(lambda: 1, released.append))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert pending.cancelled()
    assert released == []


@pytest.fixture
def root() -> Iterator[FakeServer]:
    with FakeServer(files={"/data/a.root": b"hello world"}, dirs=["/data"]) as srv:
        yield srv


def test_an_async_file_writes_a_stream_of_pieces(root):
    async def main() -> int:
        async with xrdclient.aio.open(root.url / "data/p.bin", "wb") as handle:
            assert isinstance(handle, aio.AsyncFile)
            return await handle.write_pieces([b"ab", b"cd", b"ef"])

    assert asyncio.run(main()) == 6
    assert root.contents("/data/p.bin") == b"abcdef"


def test_an_async_file_and_filesystem_abort_from_the_event_loop(root):
    async def main() -> None:
        async with aio.AsyncFileSystem(root.url) as filesystem:
            listing = await filesystem.dirlist("/data")
            assert b"a.root" in listing
            filesystem.abort()
        async with xrdclient.aio.open(root.url / "data/a.root", "rb") as handle:
            assert isinstance(handle, aio.AsyncFile)
            handle.abort()

    asyncio.run(main())


# -- the pyxrootd layer -------------------------------------------------------


def test_the_compat_settings_build_a_native_config():
    assert isinstance(env.config(), Config)


def test_a_job_end_that_is_not_a_url_names_no_path():
    assert _names_path("root://h:port//p", "/p") is False


def test_an_entry_without_stat_information_sorts_before_one_with():
    bare = SimpleNamespace(name="a", statinfo=None)
    stated = SimpleNamespace(name="a", statinfo=SimpleNamespace(size=1, flags=2))
    assert _key(bare) == ("a", 0, 0, 0)
    assert sorted([stated, bare], key=_key) == [bare, stated]


# -- the HTTP client ----------------------------------------------------------


class _AnswersEarly:
    """An HTTP server that answers an upload with ``reply`` before reading its body."""

    def __init__(self, reply: bytes) -> None:
        self.reply = reply
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        conn, _ = self.listener.accept()
        with conn:
            received = b""
            while b"\r\n\r\n" not in received:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                received += chunk
            conn.sendall(self.reply)

    def close(self) -> None:
        self.thread.join(10)
        self.listener.close()


def test_an_upload_answered_with_success_before_its_body_is_a_protocol_error():
    server = _AnswersEarly(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    try:
        with HTTPClient(Config()) as client, pytest.raises(ProtocolError, match="before the body"):
            client.begin_upload("PUT", parse(f"http://127.0.0.1:{server.port}/f"), {})
    finally:
        server.close()


def test_an_upload_redirected_past_the_limit_is_refused():
    with FakeDAVServer(dirs=["/d"]) as head:
        head.early_redirects = True
        head.redirects["/d/f.bin"] = str(head.url / "d/g.bin")
        with (
            HTTPClient(Config(redirect_limit=0)) as client,
            pytest.raises(RedirectLimitError, match="more than 0 redirects"),
        ):
            client.begin_upload("PUT", head.url / "d/f.bin", {})


def _response(raw: bytes) -> tuple[HTTPResponse, socket.socket]:
    """A real response over a socket pair, the peer having sent ``raw`` and hung up."""
    ours, theirs = socket.socketpair()
    with theirs:
        theirs.sendall(raw)
    response = HTTPResponse(ours)
    response.begin()
    return response, ours


def test_skipping_past_the_end_of_a_short_body_stops_there():
    response, sock = _response(b"HTTP/1.0 200 OK\r\n\r\nabc")
    with sock:
        _skip(response, 10)
        assert response.read() == b""
        response.close()


# -- the fake DAV server --------------------------------------------------------


def test_a_repr_digest_nobody_knows_is_not_offered():
    with FakeDAVServer(files={"/d/a.root": b"x"}) as dav, HTTPClient(Config()) as client:
        headers = {"Want-Repr-Digest": "bogus=5, sha-256=0"}
        answer = client.request("GET", dav.url / "d/a.root", headers=headers, expect=(200,))
    assert "Repr-Digest" not in answer.headers
