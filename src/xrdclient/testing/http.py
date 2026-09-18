"""A WebDAV server that runs in the test process.

:class:`FakeDAVServer` is to :mod:`xrdclient.http` what
:class:`~xrdclient.testing.FakeServer` is to the binary protocol: enough of a real
storage element - ranged ``GET``, chunked ``PUT``, ``PROPFIND``, ``MKCOL``,
``MOVE``, ``DELETE``, RFC 3230 digests, macaroon minting, third-party
``COPY`` - to test a client against, with knobs for the awkward cases (a
server that redirects, one that demands a token, one that has no WebDAV at
all, one that ignores ranges, one whose third-party copy fails after
answering ``202``).

    >>> with FakeDAVServer(files={"/d/a.root": b"hello"}) as srv:
    ...     xrdclient.FileSystem(srv.url).listdir("/d")
    ['a.root']
"""

from __future__ import annotations

import json
import posixpath
import socketserver
import threading
import urllib.parse
from collections.abc import Callable
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, cast

from ..crypto import checksum_bytes
from ..url import XRootDURL, parse

__all__ = ["FakeDAVServer", "Handler", "Reply"]

#: One canned answer: ``(status, body, headers)``.
Reply = tuple[int, bytes, dict[str, str]]

#: What :attr:`FakeDAVServer.handlers` holds. It is handed ``(method, path,
#: headers)`` and either answers the request itself or returns ``None`` to let
#: the real implementation have it.
Handler = Callable[[str, str, dict[str, str]], Optional[Reply]]

_DIGESTS = ("adler32", "md5", "sha256", "crc32c", "crc64")


def _clean(path: str) -> str:
    """The stored form of a path: absolute, unquoted, no trailing slash."""
    plain = urllib.parse.unquote(urllib.parse.urlsplit(path).path)
    normal = posixpath.normpath("/" + plain.strip("/")) if plain.strip("/") else "/"
    return normal


def _endpoint(server: socketserver.BaseServer) -> tuple[str, int]:
    """The bound ``(host, port)``. These servers are always AF_INET, which is
    narrower than the address family ``socketserver`` is typed for.
    """
    host, port = cast("tuple[str, int]", server.server_address)
    return host, port


class FakeDAVServer:
    """An HTTP/WebDAV endpoint on loopback, populated from a dict."""

    def __init__(
        self,
        *,
        files: dict[str, bytes] | None = None,
        dirs: list[str] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        #: Path to contents. Mutated by the client, readable by the test.
        self.files: dict[str, bytes] = {}
        #: Collections that exist, including every parent of every file.
        self.dirs: set[str] = {"/"}
        #: ``(method, path)`` for every request served, in order.
        self.seen: list[tuple[str, str]] = []
        #: Method to :data:`Handler`, for serving an answer this server would
        #: never produce on its own - a hostile multistatus, a short body, a
        #: digest in a shape nobody sensible would send.
        self.handlers: dict[str, Handler] = {}
        #: The body of every request served, in order.
        self.bodies: list[bytes] = []
        #: The raw request target of every request served, query and all.
        self.targets: list[str] = []
        #: Bearer token to demand; ``None`` accepts anything.
        self.require_token: str | None = None
        #: ``path -> location`` sent as a 307 before the real answer.
        self.redirects: dict[str, str] = {}
        #: Refuse ``PROPFIND``, the way a plain HTTP server would.
        self.no_dav = False
        #: Answer a ranged ``GET`` with the whole body, as some caches do.
        self.ignore_ranges = False
        #: Promise the full ``Content-Length`` on a ``GET`` but send only
        #: this many bytes and drop the connection - a server dying mid-reply.
        self.truncate_at: int | None = None
        #: Answer ``Want-Digest`` at all.
        self.digests = True
        #: What a macaroon request mints.
        self.macaroon = "macaroon-abc123"
        #: Refuse ``COPY``, the way an endpoint without third-party copy would.
        self.no_tpc = False
        #: Report this in the ``failure:`` line instead of doing the transfer.
        self.tpc_failure: str | None = None
        #: How many performance markers a ``COPY`` sends before the outcome.
        self.tpc_markers = 2
        #: The headers of every ``COPY`` served, in order.
        self.copies: list[dict[str, str]] = []
        #: Files that live on tape rather than on disk. A staging request for
        #: one of them stays ``SUBMITTED`` until the test discards it here,
        #: which is how a test watches a file come online.
        self.nearline: set[str] = set()
        #: Request id to the paths it named, for the WLCG tape API.
        self.staged: dict[str, list[str]] = {}
        #: Answer ``/api/v1`` at all - ``False`` is a site with no tape.
        self.no_tape = False

        for path, data in (files or {}).items():
            self.add_file(path, data)
        for path in dirs or []:
            self.add_dir(path)

        self._wanted = (host, port)
        self._last: tuple[str, int] | None = None
        self._bound: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- contents ------------------------------------------------------

    def add_dir(self, path: str) -> None:
        target = _clean(path)
        while target not in self.dirs:
            self.dirs.add(target)
            target = posixpath.dirname(target) or "/"

    def add_file(self, path: str, data: bytes = b"") -> None:
        target = _clean(path)
        self.files[target] = bytes(data)
        self.add_dir(posixpath.dirname(target))

    def contents(self, path: str) -> bytes:
        return self.files[_clean(path)]

    # -- lifecycle -----------------------------------------------------

    @property
    def _server(self) -> ThreadingHTTPServer:
        if self._bound is None:
            self._bound = _Server(self._wanted, _Handler)
            self._bound.fake = self
        return self._bound

    @property
    def address(self) -> tuple[str, int]:
        """Where the server listens; reading it before ``start`` binds a port."""
        if self._bound is None and self._last is not None:
            return self._last
        return _endpoint(self._server)

    @property
    def url(self) -> XRootDURL:
        """``http://host:port/`` - what a client connects to."""
        host, port = self.address
        return parse(f"http://{host}:{port}/")

    def start(self) -> FakeDAVServer:
        """Serve in a background thread. Idempotent."""
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
            )
            self._thread.start()
        return self

    def stop(self) -> None:
        """Stop serving and release the port. Idempotent."""
        if self._bound is None:
            return
        self._last = _endpoint(self._bound)
        if self._thread is not None:
            self._bound.shutdown()
            self._thread = None
        self._bound.server_close()
        self._bound = None

    def __enter__(self) -> FakeDAVServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def __repr__(self) -> str:
        bound = _endpoint(self._bound) if self._bound else self._last
        where = f"{bound[0]}:{bound[1]}" if bound else "unbound"
        return f"FakeDAVServer({where}, files={len(self.files)}, dirs={len(self.dirs)})"


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    fake: FakeDAVServer

    def handle_error(self, request: object, address: object) -> None:
        """A client that hangs up mid-request is normal here, not a failure."""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    # -- plumbing ------------------------------------------------------

    def log_message(self, fmt: str, *args: object) -> None:
        """Quiet: a test's output is the test's own."""

    @property
    def fake(self) -> FakeDAVServer:
        return self.server.fake

    @property
    def target(self) -> str:
        return _clean(self.path)

    def _body(self) -> bytes:
        """Read the request body, chunked or not, recording it as we go."""
        payload = self._read_body()
        self.fake.bodies.append(payload)
        return payload

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks: list[bytes] = []
            while True:
                size = int(self.rfile.readline().split(b";")[0] or b"0", 16)
                if not size:
                    self.rfile.readline()  # the trailing CRLF of the last chunk
                    return b"".join(chunks)
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length)

    def _send(self, status: int, body: bytes = b"", **headers: str) -> None:
        self._reply(status, body, {n.replace("_", "-"): v for n, v in headers.items()})

    def _reply(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        """Send one response, with the header names exactly as given."""
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        cut = self.fake.truncate_at if self.command == "GET" else None
        if cut is not None and cut < len(body):
            self.wfile.write(body[:cut])
            self.wfile.flush()
            self.close_connection = True
            return
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _gate(self) -> bool:
        """Record the request; answer it here if a knob says to."""
        self.fake.seen.append((self.command, self.target))
        self.fake.targets.append(self.path)
        handler = self.fake.handlers.get(self.command)
        if handler is not None:
            canned = handler(self.command, self.target, dict(self.headers.items()))
            if canned is not None:
                self._body()  # drain it, or the next request reads this one's tail
                self._reply(*canned)
                return False
        wanted = self.fake.require_token
        if wanted and self.headers.get("Authorization") != f"Bearer {wanted}":
            self._body()  # drain it, or the next request reads this one's tail
            self._send(401, b"no token")
            return False
        location = self.fake.redirects.pop(self.target, None)
        if location:
            self._body()
            self._send(307, b"", Location=location)
            return False
        return True

    # -- verbs ---------------------------------------------------------

    def do_OPTIONS(self) -> None:
        if not self._gate():
            return
        self._send(
            200,
            b"",
            DAV="1,2",
            Allow="GET,HEAD,PUT,DELETE,PROPFIND,MKCOL,MOVE,COPY,OPTIONS",
        )

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._gate():
            return
        rest = self._api
        if rest is not None:
            self._tape(rest)
            return
        path = self.target
        if path in self.fake.dirs:
            self._send(200, b"", Content_Type="text/html")
            return
        self._get_file(path)

    def _get_file(self, path: str) -> None:
        """Serve one file, including digest and byte-range response headers."""
        data = self.fake.files.get(path)
        if data is None:
            self._send(404, b"not found")
            return
        extra = self._get_headers(data)
        span = self.headers.get("Range")
        if span and not self.fake.ignore_ranges:
            self._send_range(span, data, extra)
            return
        self._send(200, data, **extra)

    def _get_headers(self, data: bytes) -> dict[str, str]:
        """Metadata headers for a successful file response."""
        extra = {"Last-Modified": formatdate(0, usegmt=True), "Accept-Ranges": "bytes"}
        wanted = self.headers.get("Want-Digest")
        if wanted and self.fake.digests:
            offered = _digest_header(wanted, data)
            if offered:
                extra["Digest"] = offered
        return extra

    def _send_range(self, span: str, data: bytes, extra: dict[str, str]) -> None:
        """Serve the requested inclusive HTTP byte range."""
        start, end = _range(span, len(data))
        piece = data[start:end]
        extra["Content-Range"] = f"bytes {start}-{start + len(piece) - 1}/{len(data)}"
        self._send(206, piece, **extra)

    def do_PUT(self) -> None:
        if not self._gate():
            return
        path = self.target
        body = self._body()
        if self.headers.get("If-None-Match") == "*" and path in self.fake.files:
            self._send(412, b"exists")
            return
        if posixpath.dirname(path) not in self.fake.dirs:
            self._send(409, b"no parent collection")
            return
        created = path not in self.fake.files
        self.fake.add_file(path, body)
        self._send(201 if created else 204)

    def do_DELETE(self) -> None:
        if not self._gate():
            return
        rest = self._api
        if rest is not None:
            self._tape(rest)
            return
        path = self.target
        if self.fake.files.pop(path, None) is not None:
            self._send(204)
        elif path in self.fake.dirs:
            self.fake.dirs.discard(path)
            for name in [f for f in self.fake.files if f.startswith(path + "/")]:
                del self.fake.files[name]
            self._send(204)
        else:
            self._send(404, b"not found")

    def do_MKCOL(self) -> None:
        if not self._gate():
            return
        path = self.target
        if path in self.fake.dirs or path in self.fake.files:
            self._send(405, b"already exists")
        elif posixpath.dirname(path) not in self.fake.dirs:
            self._send(409, b"no parent collection")
        else:
            self.fake.dirs.add(path)
            self._send(201)

    def do_MOVE(self) -> None:
        if not self._gate():
            return
        source = self.target
        destination = _clean(self.headers.get("Destination", ""))
        if source in self.fake.files:
            existed = destination in self.fake.files
            self.fake.add_file(destination, self.fake.files.pop(source))
            self._send(204 if existed else 201)
        elif source in self.fake.dirs:
            self.fake.dirs.discard(source)
            self.fake.add_dir(destination)
            for name in [f for f in self.fake.files if f.startswith(source + "/")]:
                self.fake.add_file(destination + name[len(source) :], self.fake.files.pop(name))
            self._send(201)
        else:
            self._send(404, b"not found")

    def do_COPY(self) -> None:
        """Third-party copy: fetch from ``Source`` or push to ``Destination``.

        The transfer really happens - over HTTP, against whichever server the
        header names, which may well be this one - because the point of the
        exercise is that the client never sees the bytes.
        """
        if not self._gate():
            return
        self.fake.copies.append(dict(self.headers.items()))
        if self.fake.no_tpc:
            self._send(501, b"no third-party copy here")
            return
        source = self.headers.get("Source")
        destination = self.headers.get("Destination")
        if bool(source) == bool(destination):
            self._send(400, b"exactly one of Source and Destination is required")
            return
        if self.headers.get("Overwrite", "T") == "F" and self.target in self.fake.files:
            self._send(412, b"exists")
            return

        # The outcome goes in the body, so everything from here answers 202.
        try:
            size = self._transfer(source, destination)
        except _TPCFailure as exc:
            self._markers(0, failure=str(exc))
        else:
            self._markers(size, failure=self.fake.tpc_failure)

    def _transfer(self, source: str | None, destination: str | None) -> int:
        """Move the bytes, and return how many there were."""
        if self.fake.tpc_failure:
            return 0
        token = self.headers.get("TransferHeaderAuthorization")
        headers = {"Authorization": token} if token else {}
        if source:
            pulled = _fetch("GET", source, headers)
            self.fake.add_file(self.target, pulled)
            return len(pulled)
        stored = self.fake.files.get(self.target)
        if stored is None:
            raise _TPCFailure(f"HTTP 404 {self.target} is not here to push")
        _fetch("PUT", destination or "", headers, stored)
        return len(stored)

    def _markers(self, size: int, *, failure: str | None) -> None:
        """A chunked stream of performance markers, then the outcome line."""
        self.send_response(202)
        self.send_header("Content-Type", "text/perf-marker")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        count = max(self.fake.tpc_markers, 0)
        for step in range(1, count + 1):
            done = size * step // count if count else size
            self._chunk(
                f"Perf Marker\nTimestamp: {step}\nStripe Index: 0\n"
                f"Stripe Bytes Transferred: {done}\nTotal Stripe Count: 1\nEnd\n"
            )
        self._chunk(f"failure: {failure}\n" if failure else "success: Created\n")
        self.wfile.write(b"0\r\n\r\n")

    def _chunk(self, text: str) -> None:
        payload = text.encode()
        self.wfile.write(b"%x\r\n%s\r\n" % (len(payload), payload))
        self.wfile.flush()

    def do_POST(self) -> None:
        if not self._gate():
            return
        rest = self._api
        if rest is not None:
            self._tape(rest)
            return
        self._body()
        if self.headers.get("Content-Type") == "application/macaroon-request":
            body = b'{"macaroon": "%s", "uri": {}}' % self.fake.macaroon.encode()
            self._send(200, body, Content_Type="application/json")
            return
        self._send(405, b"not allowed")

    def do_PROPFIND(self) -> None:
        if not self._gate():
            return
        self._body()
        if self.fake.no_dav:
            self._send(405, b"no webdav here")
            return
        path = self.target
        depth = self.headers.get("Depth", "1")
        if path in self.fake.dirs:
            children = self._children(path) if depth != "0" else []
            entries = [(path, True, 0), *children]
        elif path in self.fake.files:
            entries = [(path, False, len(self.fake.files[path]))]
        else:
            self._send(404, b"not found")
            return
        self._send(207, _multistatus(entries), Content_Type='text/xml; charset="utf-8"')

    def _children(self, path: str) -> list[tuple[str, bool, int]]:
        prefix = path.rstrip("/") + "/"
        seen: dict[str, tuple[str, bool, int]] = {}
        for name, data in self.fake.files.items():
            if name.startswith(prefix) and "/" not in name[len(prefix) :]:
                seen[name] = (name, False, len(data))
        for name in self.fake.dirs:
            if name.startswith(prefix) and "/" not in name[len(prefix) :]:
                seen[name] = (name, True, 0)
        return [seen[key] for key in sorted(seen)]

    # -- the WLCG tape API ---------------------------------------------

    @property
    def _api(self) -> list[str] | None:
        """What follows ``/api/v1`` in the target, or ``None`` if it is a file."""
        parts = self.target.strip("/").split("/")
        return parts[2:] if parts[:2] == ["api", "v1"] else None

    def _tape(self, rest: list[str]) -> None:
        """Serve one request to the API, whichever verb it arrived as."""
        body = self._body() if self.command == "POST" else b""
        if self.fake.no_tape:
            self._json(404, {"message": "there is no tape behind this endpoint"})
        elif self.command == "POST":
            self._tape_post(rest, body)
        elif self.command == "DELETE":
            self._tape_delete(rest)
        else:
            self._tape_get(rest)

    def _tape_post(self, rest: list[str], body: bytes) -> None:
        try:
            document = json.loads(body or b"{}")
        except ValueError:
            self._json(400, {"message": "that is not a JSON document"})
            return
        if rest == ["stage"]:
            paths = [str(entry.get("path", "")) for entry in document.get("files", [])]
            if not paths:
                self._json(400, {"message": "a staging request names at least one file"})
                return
            handle = f"stage-{len(self.fake.staged) + 1:04d}"
            self.fake.staged[handle] = paths
            self._json(201, {"requestId": handle}, Location=f"/api/v1/stage/{handle}")
        elif rest == ["archiveinfo"]:
            given = [str(path) for path in document.get("paths", [])]
            self._json(200, [self._locality(path) for path in given])
        else:
            self._json(404, {"message": "no such endpoint"})

    def _tape_get(self, rest: list[str]) -> None:
        paths = self.fake.staged.get(rest[1]) if rest[:1] == ["stage"] and len(rest) == 2 else None
        if paths is None:
            self._json(404, {"message": "no such staging request"})
            return
        self._json(200, {"files": [self._staging(path) for path in paths]})

    def _tape_delete(self, rest: list[str]) -> None:
        known = rest[:1] == ["stage"] and len(rest) == 2
        if known and self.fake.staged.pop(rest[1], None) is not None:
            self._send(204)
        else:
            self._json(404, {"message": "no such staging request"})

    def _staging(self, given: str) -> dict[str, object]:
        """One file of a staging request, as its poll reply describes it."""
        path = _clean(given)
        if path not in self.fake.files:
            return {"path": given, "state": "FAILED", "onDisk": False, "error": "no such file"}
        if path in self.fake.nearline:
            return {"path": given, "state": "SUBMITTED", "onDisk": False, "startedAt": 1}
        return {"path": given, "state": "COMPLETED", "onDisk": True, "startedAt": 1}

    def _locality(self, given: str) -> dict[str, str]:
        """One file of an ``archiveinfo`` reply, in the specification's words."""
        path = _clean(given)
        if path not in self.fake.files:
            return {"path": given, "locality": "LOST", "error": "no such file"}
        return {"path": given, "locality": "TAPE" if path in self.fake.nearline else "DISK"}

    def _json(self, status: int, document: object, **headers: str) -> None:
        body = json.dumps(document).encode()
        self._send(status, body, Content_Type="application/json", **headers)


class _TPCFailure(Exception):
    """What the ``failure:`` line will say. Never reaches the status line."""


def _fetch(method: str, url: str, headers: dict[str, str], body: bytes | None = None) -> bytes:
    """One request to the other endpoint of a third-party copy."""
    import http.client

    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise _TPCFailure(f"HTTP 400 cannot speak {parts.scheme or '<none>'} to the far side")
    conn = http.client.HTTPConnection(parts.hostname or "", parts.port or 80, timeout=10)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    try:
        conn.request(method, target, body=body, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        if response.status >= 400:
            raise _TPCFailure(f"HTTP {response.status} {response.reason} from {parts.netloc}")
        return payload
    except OSError as exc:
        raise _TPCFailure(f"HTTP 500 cannot reach {parts.netloc}: {exc}") from exc
    finally:
        conn.close()


def _range(header: str, size: int) -> tuple[int, int]:
    """``bytes=start-`` or ``bytes=start-end``, resolved against ``size``."""
    span = header.split("=", 1)[1]
    start_s, _, end_s = span.partition("-")
    start = int(start_s or 0)
    end = int(end_s) + 1 if end_s else size
    return start, min(end, size)


def _digest_header(wanted: str, data: bytes) -> str:
    """An RFC 3230 ``Digest`` value for the first algorithm we both know."""
    import base64

    for token in wanted.split(","):
        name = token.split(";")[0].strip().lower()
        algorithm = {"sha-256": "sha256", "sha-512": "sha512", "sha": "sha1"}.get(name, name)
        if algorithm not in _DIGESTS:
            continue
        value = checksum_bytes(algorithm, data)
        if algorithm in ("adler32", "crc32c"):
            return f"{name}={value}"
        return f"{name}=" + base64.b64encode(bytes.fromhex(value)).decode()
    return ""


def _multistatus(entries: list[tuple[str, bool, int]]) -> bytes:
    """A ``<D:multistatus>`` body for ``(path, is_collection, size)`` triples."""
    parts = ['<?xml version="1.0" encoding="utf-8"?>', '<D:multistatus xmlns:D="DAV:">']
    for path, is_dir, size in entries:
        href = urllib.parse.quote(path) + ("/" if is_dir and path != "/" else "")
        kind = "<D:collection/>" if is_dir else ""
        length = "" if is_dir else f"<D:getcontentlength>{size}</D:getcontentlength>"
        parts.append(
            f"<D:response><D:href>{href}</D:href><D:propstat>"
            f"<D:prop><D:resourcetype>{kind}</D:resourcetype>{length}"
            f"<D:getlastmodified>{formatdate(0, usegmt=True)}</D:getlastmodified>"
            f"<D:displayname>{posixpath.basename(path) or '/'}</D:displayname>"
            "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
        )
    parts.append("</D:multistatus>")
    return "".join(parts).encode()
