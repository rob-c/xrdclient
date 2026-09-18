"""An S3 endpoint that runs in the test process.

:class:`FakeS3Server` is to :mod:`xrdclient.s3` what :class:`FakeDAVServer` is to
:mod:`xrdclient.http`: enough of an object store - ``ListObjectsV2`` with a
delimiter and continuation tokens, ranged ``GET``, ``PUT``, server-side copy,
multipart upload, ``DELETE`` - to test a client against, with knobs for the
cases a real bucket will not produce on demand.

    >>> with FakeS3Server(objects={"d/a.root": b"hello"}) as srv:
    ...     xrdclient.s3.S3FileSystem(srv.url, endpoint=srv.endpoint).listdir("/d")
    ['a.root']

Signatures are checked here rather than trusted: the verification is written
out again from the specification, against the request as it arrived on the
wire, so a client that signs the wrong string is caught rather than agreed
with.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..url import XRootDURL, parse
from .http import _endpoint

__all__ = ["FakeS3Server"]

#: The stamp every object claims to have been written at, in the two spellings
#: S3 uses for it: ISO 8601 in a listing, RFC 5322 in a header.
_MODIFIED = "2024-01-02T03:04:05.000Z"
_MODIFIED_EPOCH = 1704164645


class FakeS3Server:
    """An S3 endpoint on loopback, holding one bucket, populated from a dict."""

    def __init__(
        self,
        *,
        bucket: str = "test-bucket",
        objects: dict[str, bytes] | None = None,
        access_key: str = "AKIAIOSFODNN7EXAMPLE",
        secret_key: str = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        #: The one bucket this endpoint serves.
        self.bucket = bucket
        #: Key to contents. Mutated by the client, readable by the test.
        self.objects: dict[str, bytes] = dict(objects or {})
        #: What a signature must be made with; ``""`` accepts anything.
        self.access_key = access_key
        self.secret_key = secret_key
        #: ``(method, path, query)`` for every request served, in order.
        self.seen: list[tuple[str, str, str]] = []
        #: Keys per listing page, which is how a test reaches the second one.
        self.page_size = 1000
        #: Key to ``ETag`` for the objects whose tag is not a plain MD5 - the
        #: ones that arrived as a multipart upload, whose tag is a digest of
        #: digests with the part count on the end.
        self.etags: dict[str, str] = {}
        #: Multipart uploads in flight: id to ``{part number: bytes}``.
        self.uploads: dict[str, dict[int, bytes]] = {}
        #: Uploads that were abandoned, so a test can see that they were.
        self.aborted: list[str] = []
        #: Begin a multipart upload without naming it, as nothing sane does.
        self.nameless_uploads = False
        #: Report a failure in the body of an otherwise successful completion,
        #: which is exactly how S3 reports one.
        self.complete_fails = False
        #: Refuse every request, whatever it is signed with.
        self.forbidden = False

        self._wanted = (host, port)
        self._last: tuple[str, int] | None = None
        self._bound: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- contents ------------------------------------------------------

    def contents(self, key: str) -> bytes:
        return self.objects[key.lstrip("/")]

    # -- lifecycle -----------------------------------------------------

    @property
    def _server(self) -> ThreadingHTTPServer:
        if self._bound is None:
            self._bound = _Server(self._wanted, _Handler)
            self._bound.fake = self
        return self._bound

    @property
    def address(self) -> tuple[str, int]:
        if self._bound is None and self._last is not None:
            return self._last
        return _endpoint(self._server)

    @property
    def endpoint(self) -> str:
        """``http://host:port`` - what :class:`~xrdclient.s3.S3FileSystem` talks to."""
        host, port = self.address
        return f"http://{host}:{port}"

    @property
    def url(self) -> XRootDURL:
        """``s3://bucket/`` - what the bucket is called."""
        return parse(f"s3://{self.bucket}/")

    def start(self) -> FakeS3Server:
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

    def __enter__(self) -> FakeS3Server:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def __repr__(self) -> str:
        bound = _endpoint(self._bound) if self._bound else self._last
        where = f"{bound[0]}:{bound[1]}" if bound else "unbound"
        return f"FakeS3Server({where}, bucket={self.bucket!r}, objects={len(self.objects)})"


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    fake: FakeS3Server

    def handle_error(self, request: object, address: object) -> None:
        """A client that hangs up mid-request is normal here, not a failure."""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, fmt: str, *args: object) -> None:
        """Quiet: a test's output is the test's own."""

    @property
    def fake(self) -> FakeS3Server:
        return self.server.fake

    # -- plumbing ------------------------------------------------------

    def _parts(self) -> tuple[str, str, dict[str, str]]:
        """``(bucket, key, query)`` of this request."""
        path, _, query = self.path.partition("?")
        plain = urllib.parse.unquote(path).lstrip("/")
        bucket, _, key = plain.partition("/")
        return bucket, key, dict(urllib.parse.parse_qsl(query, keep_blank_values=True))

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length)

    def _send(self, status: int, body: bytes = b"", **headers: str) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, code: str) -> None:
        body = f"<Error><Code>{code}</Code></Error>".encode()
        self._send(status, body, Content_Type="application/xml")

    def _gate(self, body: bytes) -> bool:
        """Record the request, and check what authorises it."""
        path, _, query = self.path.partition("?")
        self.fake.seen.append((self.command, path, query))
        if self.fake.forbidden:
            self._error(403, "AccessDenied")
            return False
        bucket = self._parts()[0]
        if bucket != self.fake.bucket:
            self._error(404, "NoSuchBucket")
            return False
        if self.fake.access_key and not self._signed(body):
            self._error(403, "SignatureDoesNotMatch")
            return False
        return True

    def _signed(self, body: bytes) -> bool:
        """Recompute the signature from the request, and see if it agrees.

        Written out from the specification rather than borrowed from
        :mod:`xrdclient.s3.sigv4`: a check that calls the code under test would
        agree with it however wrong it was.
        """
        header = self.headers.get("Authorization", "")
        algorithm, _, rest = header.partition(" ")
        fields = dict(
            item.strip().split("=", 1) for item in rest.split(",") if "=" in item
        )
        if algorithm != "AWS4-HMAC-SHA256" or not fields.keys() >= {
            "Credential",
            "SignedHeaders",
            "Signature",
        }:
            return False
        key, _, scope = fields["Credential"].partition("/")
        stamp = self.headers.get("x-amz-date", "")
        if key != self.fake.access_key or scope.split("/")[::3] != [stamp[:8], "aws4_request"]:
            return False

        names = fields["SignedHeaders"].split(";")
        hashed = self.headers.get("x-amz-content-sha256", "")
        if hashed != "UNSIGNED-PAYLOAD" and hashed != hashlib.sha256(body).hexdigest():
            return False
        path, _, query = self.path.partition("?")
        canonical = "\n".join(
            [
                self.command,
                urllib.parse.quote(urllib.parse.unquote(path), safe="/"),
                _sorted_query(query),
                "".join(f"{name}:{self._header(name)}\n" for name in names),
                ";".join(names),
                hashed,
            ]
        )
        to_sign = "\n".join(
            [
                algorithm,
                stamp,
                scope,
                hashlib.sha256(canonical.encode()).hexdigest(),
            ]
        )
        derived = f"AWS4{self.fake.secret_key}".encode()
        for step in scope.split("/"):
            derived = hmac.new(derived, step.encode(), hashlib.sha256).digest()
        expected = hmac.new(derived, to_sign.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, fields["Signature"])

    def _header(self, name: str) -> str:
        """One signed header's value, whitespace-folded as the rules say."""
        if name == "host":
            return self.headers.get("Host", "")
        return " ".join(self.headers.get(name, "").split())

    # -- verbs ---------------------------------------------------------

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._gate(b""):
            return
        _bucket, key, query = self._parts()
        if not key:
            self._list(query)
            return
        data = self.fake.objects.get(key)
        if data is None:
            self._error(404, "NoSuchKey")
            return
        extra = {
            "ETag": f'"{self.fake.etags.get(key) or _md5(data)}"',
            "Last-Modified": formatdate(_MODIFIED_EPOCH, usegmt=True),
            "Accept-Ranges": "bytes",
        }
        span = self.headers.get("Range")
        if span:
            start = int(span.split("=", 1)[1].partition("-")[0] or 0)
            piece = data[start:]
            extra["Content-Range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"
            self._send(206, piece, **extra)
            return
        self._send(200, data, **extra)

    def _list(self, query: dict[str, str]) -> None:
        """``ListObjectsV2``: one page of keys and common prefixes."""
        if query.get("list-type") != "2":
            self._send(200, b"", Content_Type="text/plain")  # a bare HEAD on the bucket
            return
        prefix = query.get("prefix", "")
        delimiter = query.get("delimiter", "")
        limit = min(int(query.get("max-keys", self.fake.page_size)), self.fake.page_size)
        start = query.get("continuation-token", "")

        keys = sorted(k for k in self.fake.objects if k.startswith(prefix) and k > start)
        contents, prefixes, last = _listing_page(keys, prefix, delimiter, limit)
        truncated = bool(last) and last != keys[-1]
        self._send(
            200,
            _listing(
                self.fake.bucket,
                prefix,
                contents,
                prefixes,
                self.fake.objects,
                self.fake.etags,
                last if truncated else "",
            ),
            Content_Type="application/xml",
        )

    def do_PUT(self) -> None:
        body = self._body()
        if not self._gate(body):
            return
        _bucket, key, query = self._parts()
        upload = query.get("uploadId")
        if upload is not None:
            self._part(upload, int(query.get("partNumber", 0)), body)
            return
        source = self.headers.get("x-amz-copy-source")
        if source:
            self._copy(key, source)
            return
        self.fake.objects[key] = body
        self.fake.etags.pop(key, None)
        self._send(200, b"", ETag=f'"{_md5(body)}"')

    def _copy(self, key: str, source: str) -> None:
        """Server-side copy, which is what makes ``rename`` possible."""
        _bucket, _, name = urllib.parse.unquote(source).lstrip("/").partition("/")
        data = self.fake.objects.get(name)
        if data is None:
            self._error(404, "NoSuchKey")
            return
        self.fake.objects[key] = data
        self._send(
            200,
            f"<CopyObjectResult><ETag>&quot;{_md5(data)}&quot;</ETag>"
            "</CopyObjectResult>".encode(),
            Content_Type="application/xml",
        )

    def _part(self, upload: str, number: int, body: bytes) -> None:
        parts = self.fake.uploads.get(upload)
        if parts is None or not number:
            self._error(404, "NoSuchUpload")
            return
        parts[number] = body
        self._send(200, b"", ETag=f'"{_md5(body)}"')

    def do_POST(self) -> None:
        body = self._body()
        if not self._gate(body):
            return
        _bucket, key, query = self._parts()
        if "uploads" in query:
            self._begin(key)
        elif "uploadId" in query:
            self._complete(key, query["uploadId"], body)
        else:
            self._error(400, "MalformedPOSTRequest")

    def _begin(self, key: str) -> None:
        handle = f"upload-{len(self.fake.uploads) + 1:04d}"
        self.fake.uploads[handle] = {}
        named = "" if self.fake.nameless_uploads else f"<UploadId>{handle}</UploadId>"
        self._send(
            200,
            f"<InitiateMultipartUploadResult><Key>{key}</Key>{named}"
            "</InitiateMultipartUploadResult>".encode(),
            Content_Type="application/xml",
        )

    def _complete(self, key: str, upload: str, body: bytes) -> None:
        parts = self.fake.uploads.pop(upload, None)
        if parts is None:
            self._error(404, "NoSuchUpload")
            return
        if self.fake.complete_fails:
            # 200 with a failure inside it: S3 answers before it knows, so
            # that it can hold the connection open while it assembles.
            self._send(200, b"<Error><Code>InternalError</Code></Error>")
            return
        wanted = [int(n.text or 0) for n in _find(body, "PartNumber")]
        self.fake.objects[key] = b"".join(parts[n] for n in wanted)
        digests = b"".join(hashlib.md5(parts[n]).digest() for n in wanted)
        tag = f"{hashlib.md5(digests).hexdigest()}-{len(wanted)}"
        self.fake.etags[key] = tag
        self._send(
            200,
            f"<CompleteMultipartUploadResult><Key>{key}</Key>"
            f"<ETag>&quot;{tag}&quot;</ETag></CompleteMultipartUploadResult>".encode(),
            Content_Type="application/xml",
        )

    def do_DELETE(self) -> None:
        if not self._gate(b""):
            return
        _bucket, key, query = self._parts()
        upload = query.get("uploadId")
        if upload is not None:
            self.fake.uploads.pop(upload, None)
            self.fake.aborted.append(upload)
            self._send(204)
            return
        # Deleting a key that is not there is a success, as it is in S3.
        self.fake.objects.pop(key, None)
        self._send(204)


def _listing_page(
    keys: list[str], prefix: str, delimiter: str, limit: int
) -> tuple[list[str], list[str], str]:
    """Select one S3 page, coalescing keys beneath a common prefix."""
    contents: list[str] = []
    prefixes: list[str] = []
    taken = 0
    last = ""
    for key in keys:
        if key <= last:
            continue  # inside a group already rolled past, below
        tail = key[len(prefix) :]
        if delimiter and delimiter in tail:
            group = prefix + tail.split(delimiter, 1)[0] + delimiter
            prefixes.append(group)
            # Past the whole group, so the next page cannot name it again:
            # a common prefix appears in exactly one page of a listing.
            last = max(k for k in keys if k.startswith(group))
        else:
            contents.append(key)
            last = key
        taken += 1
        if taken >= limit:
            break
    return contents, prefixes, last


def _sorted_query(query: str) -> str:
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    return "&".join(
        f"{urllib.parse.quote(name, safe='')}={urllib.parse.quote(value, safe='')}"
        for name, value in sorted(pairs)
    )


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _find(body: bytes, name: str) -> list[ET.Element]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    return [node for node in root.iter() if node.tag.rpartition("}")[2] == name]


def _listing(
    bucket: str,
    prefix: str,
    contents: list[str],
    prefixes: list[str],
    objects: dict[str, bytes],
    etags: dict[str, str],
    token: str,
) -> bytes:
    """A ``ListObjectsV2`` result, namespaced the way a real one is."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
        f"<Name>{bucket}</Name><Prefix>{prefix}</Prefix>",
        f"<IsTruncated>{'true' if token else 'false'}</IsTruncated>",
    ]
    if token:
        parts.append(f"<NextContinuationToken>{token}</NextContinuationToken>")
    for key in contents:
        data = objects[key]
        parts.append(
            f"<Contents><Key>{key}</Key><Size>{len(data)}</Size>"
            f"<LastModified>{_MODIFIED}</LastModified>"
            f"<ETag>&quot;{etags.get(key) or _md5(data)}&quot;</ETag>"
            f"<StorageClass>STANDARD</StorageClass></Contents>"
        )
    for group in prefixes:
        parts.append(f"<CommonPrefixes><Prefix>{group}</Prefix></CommonPrefixes>")
    parts.append("</ListBucketResult>")
    return "".join(parts).encode()
