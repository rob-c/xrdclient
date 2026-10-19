"""xrdclient's HTTP side against xrdhttp's shared conformance suites.

The behaviour every HTTP storage client owes a user - stat, listings,
collections, renames, ranged reads, uploads, digests, redirects,
third-party copy, tape staging, macaroons, S3 - is checked once, in
:mod:`xrdhttp.testing.conformance`, and run here through :class:`XrdAdapter`:
the same operations through xrdclient's public API (``FileSystem``,
``open``, ``third_party``, the tape verbs). xgfalclient runs the same
suites through gfal2's API. What only xrdclient promises - its error
classes and XrdCl status codes, ``Config``, pathlib/fsspec shapes,
pyxrootd compatibility - stays in the other ``test_http*`` modules.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from xrdhttp.testing import StorageServer
from xrdhttp.testing.conformance import (
    Adapter,
    ChecksumSuite,
    Conformance,
    ConnectionSuite,
    CopySuite,
    Kind,
    NamespaceSuite,
    Progress,
    ReadSuite,
    RedirectSuite,
    S3Suite,
    Stat,
    TapeSuite,
    TokenSuite,
    UploadSuite,
)

import xrdclient
import xrdclient.s3.fs as s3fs
from xrdclient.config import Config
from xrdclient.errors import BusyError, ProtocolError, RedirectLimitError, UnsupportedError
from xrdclient.http import HTTPFileSystem, macaroon, open_http, tape, third_party
from xrdclient.s3 import Credentials, S3FileSystem
from xrdclient.testing import FakeDAVServer
from xrdclient.url import parse


class XrdAdapter(Adapter):
    """The neutral operations, through xrdclient's public HTTP API."""

    def __init__(self) -> None:
        """No filesystem yet: one is made per endpoint, on first use."""
        self.config = Config()
        self.filesystems: dict[str, HTTPFileSystem] = {}

    def _at(self, url: str) -> tuple[HTTPFileSystem, str]:
        """The filesystem for ``url``'s endpoint, and the path on it."""
        parsed = parse(url)
        origin = str(parsed.with_path("/"))
        if origin not in self.filesystems:
            made = xrdclient.FileSystem(origin, self.config)
            assert isinstance(made, HTTPFileSystem)
            self.filesystems[origin] = made
        return self.filesystems[origin], parsed.path

    def kind(self, error: BaseException) -> Kind:
        """xrdclient's classes: a busy collection is a full one, a limit is a loop."""
        if isinstance(error, BusyError):
            return Kind.NOT_EMPTY
        if isinstance(error, RedirectLimitError):
            return Kind.REDIRECTS
        if isinstance(error, UnsupportedError):
            return Kind.UNSUPPORTED
        if isinstance(error, ProtocolError):
            return Kind.PROTOCOL
        return super().kind(error)

    def close(self) -> None:
        """Close every filesystem's pool."""
        for filesystem in self.filesystems.values():
            filesystem.close()

    def _reconfigure(self, config: Config) -> None:
        """Use ``config`` from now on: the filesystems made with the old one are closed."""
        self.close()
        self.filesystems.clear()
        self.config = config

    def authorise(self, token: str) -> None:
        """``Config(token=...)``, as ``BEARER_TOKEN`` would set it."""
        self._reconfigure(self.config.evolve(token=token))

    def time_limit(self, seconds: float) -> None:
        """``request_timeout``."""
        self._reconfigure(self.config.evolve(request_timeout=seconds))

    # -- the namespace -----------------------------------------------------------------

    def stat(self, url: str) -> Stat:
        """``FileSystem.stat``."""
        filesystem, path = self._at(url)
        info = filesystem.stat(path)
        return Stat(info.st_size, info.is_dir(), info.st_mtime)

    def listdir(self, url: str) -> list[str]:
        """``FileSystem.listdir``."""
        filesystem, path = self._at(url)
        return filesystem.listdir(path)

    def mkdir(self, url: str) -> None:
        """``FileSystem.mkdir``."""
        filesystem, path = self._at(url)
        filesystem.mkdir(path)

    def makedirs(self, url: str) -> None:
        """``FileSystem.makedirs``."""
        filesystem, path = self._at(url)
        filesystem.makedirs(path, exist_ok=True)

    def rmdir(self, url: str) -> None:
        """``FileSystem.rmdir``."""
        filesystem, path = self._at(url)
        filesystem.rmdir(path)

    def unlink(self, url: str) -> None:
        """``FileSystem.remove``."""
        filesystem, path = self._at(url)
        filesystem.remove(path)

    def rename(self, source: str, destination: str) -> None:
        """``FileSystem.rename``."""
        filesystem, path = self._at(source)
        filesystem.rename(path, parse(destination).path)

    # -- data ----------------------------------------------------------------------------

    def read(self, url: str) -> bytes:
        """``FileSystem.read_bytes``."""
        filesystem, path = self._at(url)
        return filesystem.read_bytes(path)

    def pread(self, url: str, offset: int, size: int) -> bytes:
        """A seek and reads on an unbuffered file, until ``size`` bytes or the end."""
        got = bytearray()
        with open_http(parse(url), "rb", buffering=0, config=self.config) as handle:
            handle.seek(offset)
            while len(got) < size and (chunk := handle.read(size - len(got))):
                got += chunk
        return bytes(got)

    def write(self, url: str, data: bytes) -> None:
        """One write to a file opened ``wb``."""
        filesystem, path = self._at(url)
        with filesystem.open(path, "wb") as handle:
            handle.write(data)

    def stream(self, url: str, chunks: Sequence[bytes]) -> None:
        """Writes larger than ``chunk_size``, which xrdclient streams as a chunked ``PUT``."""
        config = self.config.evolve(chunk_size=1)
        with open_http(parse(url), "wb", buffering=0, config=config) as handle:
            for chunk in chunks:
                handle.write(chunk)

    def checksum(self, url: str, algorithm: str) -> str:
        """``FileSystem.checksum``."""
        filesystem, path = self._at(url)
        return filesystem.checksum(path, algorithm).value

    # -- third-party copy --------------------------------------------------------------

    def copy(
        self, source: str, destination: str, *, push: bool = False, progress: Progress | None = None
    ) -> None:
        """``xrdclient.http.third_party``, unverified (the suites check the bytes)."""

        def report(done: int, total: int | None) -> None:
            """Each marker's running total."""
            if progress is not None:
                progress(done)

        third_party(
            source,
            destination,
            config=self.config,
            mode="push" if push else "pull",
            verify=False,
            progress=report,
        )

    # -- the WLCG tape REST API -----------------------------------------------------

    def _tape(self, urls: Sequence[str]) -> tuple[HTTPFileSystem, list[str]]:
        """The filesystem of the first URL, and every URL's path."""
        filesystem, _ = self._at(urls[0])
        return filesystem, [parse(url).path for url in urls]

    def stage(self, urls: Sequence[str]) -> str:
        """``FileSystem.prepare``."""
        filesystem, paths = self._tape(urls)
        return filesystem.prepare(paths, stage=True)

    def poll(self, request: str, urls: Sequence[str]) -> list[bool]:
        """``FileSystem.query_prepare``; a file the site reports an error for fails."""
        filesystem, paths = self._tape(urls)
        found = filesystem.query_prepare(request, paths)
        for status in found:
            if status.error:
                raise FileNotFoundError(status.error)
        return [status.online for status in found]

    def release(self, request: str, urls: Sequence[str]) -> None:
        """``xrdclient.http.tape.release``."""
        filesystem, paths = self._tape(urls)
        tape.release(filesystem.client, filesystem.url, request, paths)

    def abort(self, request: str, urls: Sequence[str]) -> None:
        """``FileSystem.cancel_prepare``."""
        filesystem, paths = self._tape(urls)
        filesystem.cancel_prepare(request, paths)

    def on_tape(self, urls: Sequence[str]) -> list[bool]:
        """``FileSystem.archive_info``."""
        filesystem, paths = self._tape(urls)
        return [status.on_tape for status in filesystem.archive_info(paths)]

    # -- tokens ---------------------------------------------------------------------------

    def macaroon(self, url: str, activities: Sequence[str]) -> str:
        """``xrdclient.http.macaroon``."""
        caveats = [f"activity:{','.join(activities)}"]
        return macaroon(url, caveats=caveats, validity="PT10M", config=self.config)


class XrdS3Adapter(XrdAdapter):
    """The same operations over a bucket: :class:`~xrdclient.s3.S3FileSystem`, path-style."""

    def __init__(self, server: StorageServer) -> None:
        """Sign with the keys ``server``'s S3 dialect starts with; parts of a kilobyte or two."""
        super().__init__()
        assert server.s3 is not None
        self.credentials = Credentials(server.s3.access_key, server.s3.secret_key)
        self.endpoint = server.base
        self.config = Config(chunk_size=1000)  # the suite writes kilobyte pieces
        self.buckets: dict[str, S3FileSystem] = {}

    def url(self, server: StorageServer, path: str) -> str:
        """``s3://bucket/key``: the endpoint is the filesystem's, not the URL's."""
        return "s3:/" + path

    def _at(self, url: str) -> tuple[HTTPFileSystem, str]:
        """Refused: every operation here goes through :meth:`_bucket`."""
        raise AssertionError(f"{url} is not a WebDAV URL")

    def _bucket(self, url: str) -> tuple[S3FileSystem, str]:
        """The filesystem for ``url``'s bucket, and the key as a path."""
        parsed = parse(url)
        if parsed.host not in self.buckets:
            self.buckets[parsed.host] = S3FileSystem(
                f"s3://{parsed.host}",
                credentials=self.credentials,
                endpoint=self.endpoint,
                config=self.config,
            )
        return self.buckets[parsed.host], parsed.path

    def close(self) -> None:
        """Close every bucket's filesystem."""
        for filesystem in self.buckets.values():
            filesystem.close()

    def stat(self, url: str) -> Stat:
        """``S3FileSystem.stat``."""
        filesystem, path = self._bucket(url)
        info = filesystem.stat(path)
        return Stat(info.st_size, info.is_dir(), info.st_mtime)

    def listdir(self, url: str) -> list[str]:
        """``S3FileSystem.listdir``."""
        filesystem, path = self._bucket(url)
        return filesystem.listdir(path)

    def unlink(self, url: str) -> None:
        """``S3FileSystem.remove``."""
        filesystem, path = self._bucket(url)
        filesystem.remove(path)

    def rename(self, source: str, destination: str) -> None:
        """``S3FileSystem.rename``."""
        filesystem, path = self._bucket(source)
        filesystem.rename(path, parse(destination).path)

    def read(self, url: str) -> bytes:
        """``S3FileSystem.read_bytes``."""
        filesystem, path = self._bucket(url)
        return filesystem.read_bytes(path)

    def pread(self, url: str, offset: int, size: int) -> bytes:
        """A seek and a read."""
        filesystem, path = self._bucket(url)
        with filesystem.open(path, "rb") as handle:
            handle.seek(offset)
            return bytes(handle.read(size))

    def write(self, url: str, data: bytes) -> None:
        """One write to an object opened ``wb``."""
        filesystem, path = self._bucket(url)
        with filesystem.open(path, "wb") as handle:
            handle.write(data)

    def stream(self, url: str, chunks: Sequence[bytes]) -> None:
        """Unbuffered writes, which go up in parts past the (shrunk) part size."""
        filesystem, path = self._bucket(url)
        with filesystem.open(path, "wb", buffering=0) as handle:
            for chunk in chunks:
                handle.write(chunk)

    def checksum(self, url: str, algorithm: str) -> str:
        """``S3FileSystem.checksum``."""
        filesystem, path = self._bucket(url)
        return filesystem.checksum(path, algorithm).value


class Xrd(Conformance):
    """xrdclient's preset of the fake (XrdCl style, in memory) and its adapter."""

    def make_server(self, root: Path) -> StorageServer:
        """:class:`~xrdclient.testing.FakeDAVServer`."""
        return FakeDAVServer()

    def make_adapter(self, server: StorageServer) -> Adapter:
        """:class:`XrdAdapter`."""
        return XrdAdapter()


class TestNamespace(Xrd, NamespaceSuite):
    """Stat, listings, collections, removal, renames."""


class TestRead(Xrd, ReadSuite):
    """Whole, ranged and resumed reads."""


class TestUpload(Xrd, UploadSuite):
    """Sized, streamed and redirected uploads."""


class TestChecksum(Xrd, ChecksumSuite):
    """Server-side digests."""


class TestRedirect(Xrd, RedirectSuite):
    """Redirects and the token across them."""


class TestConnection(Xrd, ConnectionSuite):
    """Keep-alive, stale connections, failures, tokens."""


class TestCopy(Xrd, CopySuite):
    """HTTP third-party copy."""


class TestTape(Xrd, TapeSuite):
    """The WLCG Tape REST API."""


class TestToken(Xrd, TokenSuite):
    """Macaroons."""


class TestS3(Xrd, S3Suite):
    """A bucket as a filesystem, and multipart uploads."""

    @pytest.fixture(autouse=True)
    def _small_parts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Parts of a kilobyte and a half, so the suite's large object needs several."""
        monkeypatch.setattr(s3fs, "MIN_PART_SIZE", 1500)

    def make_adapter(self, server: StorageServer) -> Adapter:
        """:class:`XrdS3Adapter`."""
        return XrdS3Adapter(server)
