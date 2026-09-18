"""S3 object storage as one more thing this client can read and write.

Sites increasingly put the same data behind an S3 endpoint - Ceph's RGW,
MinIO, or AWS itself - and a job that reads one file over ``root://`` and
another over ``s3://`` should not need a second client, a second credential
store, or a wheel from PyPI. :class:`S3FileSystem` is the same
:class:`~xrdclient.FileSystem` surface over the same :class:`~xrdclient.http.HTTPClient`,
with AWS Signature Version 4 on every request and object listings in place of
``PROPFIND``.

    >>> fs = xrdclient.FileSystem("s3://my-bucket")           # doctest: +SKIP
    >>> for entry in fs.scandir("/runs/2024"):          # doctest: +SKIP
    ...     print(entry.name, entry.stat.st_size)

An object store is not a filesystem, and this does not pretend otherwise: a
directory is a common prefix rather than a thing that exists, so ``mkdir``
creates nothing and ``remove`` cannot tell a key that was there from one that
was not. Both are documented where they are defined, and neither is papered
over with a round trip that would only be a guess.
"""

from __future__ import annotations

import io
import os
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from typing import IO, Any, Literal, overload

from ..config import Config
from ..errors import (
    BusyError,
    ExistsError,
    NotFoundError,
    ProtocolError,
    ServerError,
    UnsupportedError,
    kXR_FileLocked,
    kXR_ItExists,
    kXR_Unsupported,
)
from ..flags import DirListFlags, PrepareFlags, StatInfoFlags
from ..http.client import HTTPClient, Response, request_target
from ..http.dav import HTTPFileSystem, _epoch
from ..http.file import HTTPRawIO, open_http
from ..io.raw import OpenBinaryMode, OpenTextMode
from ..types import ChecksumInfo, DirEntry, PrepareStatus, StatInfo
from ..url import XRootDURL, parse
from .sigv4 import DEFAULT_REGION, Credentials, hash_payload, sign

__all__ = ["S3FileSystem", "S3RawIO", "open_s3", "MIN_PART_SIZE"]

#: The smallest part S3 accepts in a multipart upload, except for the last
#: one. A streamed write holds this much before it can send anything.
MIN_PART_SIZE = 5 << 20

#: Where AWS itself answers, when no endpoint names somewhere else.
AWS_SUFFIX = "amazonaws.com"


def region_from_env() -> str:
    """``AWS_REGION``, then ``AWS_DEFAULT_REGION``, then AWS's own default."""
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION


def endpoint_from_env() -> str:
    """``AWS_ENDPOINT_URL``: a self-hosted implementation, or nothing."""
    return os.environ.get("AWS_ENDPOINT_URL", "")


def _list_params(prefix: str, token: str) -> dict[str, str]:
    params = {"delimiter": "/", "prefix": prefix}
    if token:
        params["continuation-token"] = token
    return params


def _directory_entries(listing: ET.Element, prefix: str, parent: str) -> list[DirEntry]:
    entries = []
    for common in _elements(listing, "CommonPrefixes"):
        name = _text(common, "Prefix")[len(prefix) :].rstrip("/")
        if name:
            entries.append(_directory_entry(name, parent))
    return entries


def _directory_entry(name: str, parent: str) -> DirEntry:
    return DirEntry(
        name=name,
        parent=parent,
        stat=StatInfo(
            flags=StatInfoFlags.IS_DIR | StatInfoFlags.IS_READABLE,
            path=f"{parent.rstrip('/')}/{name}",
        ),
    )


def _object_entries(
    listing: ET.Element, prefix: str, parent: str, algorithm: str
) -> list[DirEntry]:
    entries = []
    for content in _elements(listing, "Contents"):
        key = _text(content, "Key")
        name = key[len(prefix) :]
        if name and "/" not in name:  # omit the prefix's own marker
            entries.append(_object_entry(content, key, name, parent, algorithm))
    return entries


def _object_entry(
    content: ET.Element, key: str, name: str, parent: str, algorithm: str
) -> DirEntry:
    tag = _etag(_text(content, "ETag"))
    checksum = ChecksumInfo("md5", tag) if algorithm and _is_md5(tag) else None
    return DirEntry(
        name=name,
        parent=parent,
        stat=StatInfo(
            id=tag,
            st_size=int(_text(content, "Size") or 0),
            flags=StatInfoFlags.IS_READABLE | StatInfoFlags.IS_WRITABLE,
            st_mtime=_iso8601(_text(content, "LastModified")),
            path=f"/{key}",
        ),
        checksum=checksum,
    )


class S3FileSystem(HTTPFileSystem):
    """:class:`~xrdclient.FileSystem` over S3, addressed as ``s3://bucket/key``.

        >>> fs = S3FileSystem("s3://my-bucket")                     # doctest: +SKIP
        >>> fs.write_bytes("/runs/a.root", b"...")                  # doctest: +SKIP

    Constructed for you when :class:`~xrdclient.FileSystem` is handed an ``s3`` URL.

    Credentials come from ``credentials``, or are discovered the way every S3
    tool discovers them: the environment first, then ``~/.aws/credentials``.
    Finding none is not an error - the requests go out unsigned, which is
    exactly what a public bucket wants.

    Without ``endpoint`` the bucket is addressed at AWS in the virtual-hosted
    form; with one - the argument, or ``AWS_ENDPOINT_URL`` - it is addressed
    path-style, which is what every self-hosted implementation understands.
    """

    def __init__(
        self,
        url: str | XRootDURL,
        config: Config | None = None,
        *,
        credentials: Credentials | None = None,
        region: str = "",
        endpoint: str = "",
    ) -> None:
        self.url = parse(url)
        if not self.url.host:
            raise ValueError(f"{str(self.url)!r} names no bucket; an S3 URL is s3://bucket/key")
        self.config = config or Config()
        #: The bucket every path here lives in.
        self.bucket = self.url.host
        #: What signs the requests, or ``None`` for an unsigned - public - one.
        self.credentials = credentials if credentials is not None else Credentials.discover()
        #: The region the signature is scoped to.
        self.region = region or region_from_env()
        where = endpoint or endpoint_from_env()
        self._endpoint = parse(where) if where else None
        self.client = HTTPClient(self.config, signer=self._sign)
        #: There is no XRootD session behind this one either.
        self._router = None  # type: ignore[assignment]

    # -- plumbing ------------------------------------------------------

    def __repr__(self) -> str:
        return f"S3FileSystem({str(self.url.evolve(query={}))!r})"

    @property
    def endpoint(self) -> str:
        """``host:port`` of wherever the bucket is actually served."""
        base = self._bucket_url()
        return f"{base.host}:{base.port}"

    def _key(self, path: str) -> str:
        """The object key ``path`` names, which is a path without its slash."""
        return self._abs(path).lstrip("/")

    def _bucket_url(self, query: dict[str, str] | None = None) -> XRootDURL:
        """The URL of the bucket itself, where a listing is asked for."""
        if self._endpoint is not None:
            return self._endpoint.evolve(path=f"/{self.bucket}", query=query or {})
        return XRootDURL(
            scheme="https",
            host=f"{self.bucket}.s3.{self.region}.{AWS_SUFFIX}",
            port=443,
            path="/",
            query=query or {},
        )

    def _url(self, path: str, *, collection: bool = False) -> XRootDURL:
        """Where the object lives. ``collection`` has no meaning here: a key
        that ends in a slash is a different key, not the same one as a
        directory."""
        base = self._bucket_url()
        key = self._key(path)
        return base.evolve(path=f"{base.path.rstrip('/')}/{key}")

    def _sign(
        self, method: str, url: XRootDURL, headers: dict[str, str], body: bytes | None
    ) -> dict[str, str]:
        """Sign one request, or leave it alone when there is nothing to sign."""
        if self.credentials is None:
            return {}
        return sign(
            method,
            request_target(url),
            _host_header(url),
            headers,
            hash_payload(body),
            credentials=self.credentials,
            region=self.region,
        )

    def _unsupported(self, what: str) -> ServerError:
        return UnsupportedError(kXR_Unsupported, f"{what} has no S3 equivalent")

    # -- interrogation -------------------------------------------------

    def ping(self) -> None:
        """``HEAD`` on the bucket: is it there, and are we allowed in?"""
        self.client.request("HEAD", self._bucket_url(), expect=(200,))

    def stat(self, path: str, *, follow_symlinks: bool = True) -> StatInfo:
        """``HEAD`` on the object, falling back to asking about the prefix.

        A key that is not there may still be a directory - S3 says so by
        having other keys below it - and the bucket root always is.
        """
        if not follow_symlinks:
            raise self._unsupported("stat(follow_symlinks=False)")
        key = self._key(path)
        if key:
            try:
                return self._stat_object(key)
            except NotFoundError:
                if not self._has_children(key):
                    raise
        return StatInfo(
            flags=StatInfoFlags.IS_DIR | StatInfoFlags.IS_READABLE,
            path=f"/{key}",
        )

    def _stat_object(self, key: str) -> StatInfo:
        response = self.client.request("HEAD", self._url(key))
        return StatInfo(
            id=_etag(response.header("ETag")),
            st_size=response.content_length or 0,
            flags=StatInfoFlags.IS_READABLE | StatInfoFlags.IS_WRITABLE,
            st_mtime=_epoch(response.header("Last-Modified")),
            path=f"/{key}",
        )

    def _has_children(self, key: str) -> bool:
        """Does anything live under this prefix? One key is enough to know."""
        listing = self._list({"prefix": f"{key}/", "max-keys": "1"})
        return bool(_elements(listing, "Contents") or _elements(listing, "CommonPrefixes"))

    def _list(self, params: dict[str, str]) -> ET.Element:
        """One ``ListObjectsV2`` page."""
        query = {"list-type": "2", **params}
        response = self.client.request("GET", self._bucket_url(query), expect=(200,))
        return _xml(response, self.bucket)

    def scandir(
        self,
        path: str = "",
        *,
        stat: bool = True,
        online: bool = False,
        algorithm: str = "",
        flags: DirListFlags | int | str | None = None,
    ) -> list[DirEntry]:
        """``ListObjectsV2`` with a delimiter, so a prefix reads as a directory.

        Every page a listing needs is fetched; sizes and modification times
        come back with it, so ``stat``, ``online`` and ``flags`` are accepted
        for the sake of one call that works anywhere, and turn nothing off. ``algorithm``
        may be ``md5``, which S3 has already computed for any object that was
        uploaded in one piece and reports as its ``ETag``; anything else it
        does not have.
        """
        if algorithm and algorithm.lower() != "md5":
            raise self._unsupported(f"a {algorithm} checksum per listing entry")
        prefix = self._key(path)
        prefix = f"{prefix}/" if prefix else ""
        here = f"/{prefix.rstrip('/')}"
        entries: list[DirEntry] = []
        token = ""
        while True:
            listing = self._list(_list_params(prefix, token))
            entries.extend(_directory_entries(listing, prefix, here))
            entries.extend(_object_entries(listing, prefix, here, algorithm))
            if _text(listing, "IsTruncated") != "true":
                return entries
            token = _text(listing, "NextContinuationToken")

    def checksum(self, path: str, algorithm: str | None = None) -> ChecksumInfo:
        """The object's ``ETag``, which is its MD5 - if it has one.

        S3 computes no other digest on the way in, and an object uploaded in
        parts has an ETag that is a digest of digests: a fine identity, but
        not the MD5 of anything, so it is refused rather than returned.
        """
        if algorithm and algorithm.lower() != "md5":
            raise self._unsupported(f"a {algorithm} checksum")
        tag = self._stat_object(self._key(path)).id
        if not _is_md5(tag):
            raise self._unsupported("a checksum of a multipart upload")
        return ChecksumInfo("md5", tag)

    # -- mutation ------------------------------------------------------

    def mkdir(
        self, path: str, mode: int | str = 0o755, *, parents: bool = False, exist_ok: bool = False
    ) -> None:
        """Nothing, successfully - unless folder markers are turned on.

        A prefix exists exactly when a key under it does, so by default there
        is nothing to create and nothing that could already be there. Writing
        the zero-length ``dir/`` marker some consoles show would create an
        object every other reader would then have to learn to ignore.

        :attr:`~xrdclient.config.Config.s3_folder_markers` opts into exactly that,
        for a gateway that treats the marker as a directory: the marker is
        written, an empty directory then exists, and ``mkdir`` gets its POSIX
        meaning back - a name already taken, by an object or (without
        ``exist_ok``) by a directory, is refused. Listings hide the markers
        either way. ``parents`` never matters here: every prefix above the
        marker is a valid place for it.
        """
        if self.config.s3_folder_markers:
            self._write_marker(path, exist_ok=exist_ok)

    def makedirs(self, path: str, mode: int | str = 0o755, exist_ok: bool = False) -> None:
        """Nothing, successfully - or the leaf's marker, see :meth:`mkdir`.

        One marker is enough: a prefix exists whenever a key lives under it,
        and the leaf's marker is under every ancestor.
        """
        if self.config.s3_folder_markers:
            self._write_marker(path, exist_ok=exist_ok)

    def _write_marker(self, path: str, *, exist_ok: bool) -> None:
        """The zero-length ``dir/`` object that makes an empty prefix exist."""
        key = self._key(path)
        if not key:
            return  # the bucket root is a directory already
        try:
            self._stat_object(key)
        except NotFoundError:
            pass
        else:
            raise ExistsError(kXR_ItExists, "an object already has this name", path=self._abs(path))
        if not exist_ok and self._has_children(key):
            raise ExistsError(kXR_ItExists, "directory exists", path=self._abs(path))
        self.client.request(
            "PUT",
            self._marker_url(key),
            body=b"",
            headers={"Content-Length": "0"},
            expect=(200,),
        )

    def _marker_url(self, key: str) -> XRootDURL:
        """Where the marker lives: the key with the slash S3 lets a key keep."""
        base = self._bucket_url()
        return base.evolve(path=f"{base.path.rstrip('/')}/{key}/")

    def rmdir(self, path: str) -> None:
        """Refuse a prefix that still has keys under it; otherwise nothing.

        The emptiness check is the half of ``rmdir`` S3 can honour, and it is
        the half that stops a caller deleting a tree by accident. With folder
        markers on, the empty directory's marker goes too, which is the other
        half.
        """
        if self.listdir(path):
            raise BusyError(kXR_FileLocked, "directory not empty", path=self._abs(path))
        key = self._key(path)
        if self.config.s3_folder_markers and key:
            self.client.request("DELETE", self._marker_url(key), expect=(200, 202, 204))

    def remove(self, path: str) -> None:
        """``DELETE`` the object.

        S3 answers the same way whether or not the key was there, so this
        cannot raise :class:`~xrdclient.errors.NotFoundError` the way the other
        backends do: removing a key twice is not an error here.
        """
        self.client.request("DELETE", self._url(path), expect=(200, 202, 204))

    unlink = remove

    def rename(self, src: str, dst: str) -> None:
        """Server-side copy, then delete: S3 has no move.

        Both halves happen at the endpoint, so nothing travels to this
        process and back, but they are two operations - an interruption
        between them leaves the object at both names.
        """
        source = urllib.parse.quote(f"/{self.bucket}/{self._key(src)}", safe="/")
        self.client.request(
            "PUT",
            self._url(dst),
            body=b"",
            headers={"x-amz-copy-source": source, "Content-Length": "0"},
            expect=(200,),
        )
        self.remove(src)

    move = rename

    # -- I/O -----------------------------------------------------------

    def open(
        self,
        path: str,
        mode: str = "rb",
        *,
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
        posc: bool = False,
    ) -> IO[Any]:
        """Open an object with :func:`open`'s signature.

        ``posc`` is accepted for symmetry and ignored: an object appears at
        its key when the upload completes and not before, which is what
        persist-on-successful-close asks for anyway.
        """
        return open_http(
            self._url(path),
            mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
            config=self.config,
            client=self.client,
            raw=S3RawIO,
        )

    # -- what an object store does not have ----------------------------

    def touch(self, path: str, *, exist_ok: bool = True) -> None:
        """``PUT`` an empty object."""
        if not exist_ok and self.exists(path):
            raise ExistsError(kXR_ItExists, "file exists", path=self._abs(path))
        self.client.request(
            "PUT", self._url(path), body=b"", headers={"Content-Length": "0"}, expect=(200,)
        )

    def extensions(self) -> frozenset[str]:
        """No vendor opcodes: there is no XRootD server on the other end."""
        return frozenset()

    # The tape API :class:`~xrdclient.http.HTTPFileSystem` speaks lives at
    # ``/api/v1`` on a WebDAV endpoint; on an S3 one that path is a key like
    # any other, so these are refused rather than sent somewhere they would
    # only be misread.

    def prepare(
        self,
        paths: Sequence[str],
        *,
        stage: bool | None = None,
        evict: bool = False,
        notify: bool = False,
        fresh: bool = False,
        priority: int = 0,
        flags: PrepareFlags | int | str | None = None,
    ) -> str:
        raise self._unsupported("staging from tape")

    def query_prepare(self, handle: str, paths: Sequence[str]) -> list[PrepareStatus]:
        raise self._unsupported("staging from tape")

    def cancel_prepare(self, handle: str) -> None:
        raise self._unsupported("staging from tape")

    def archive_info(self, paths: Sequence[str]) -> list[PrepareStatus]:
        raise self._unsupported("asking where a file is")


class S3RawIO(HTTPRawIO):
    """The HTTP raw layer, uploading the way S3 wants it.

    There is no chunked ``PUT`` in S3: an object arrives either in one request
    with a length, or as a multipart upload whose parts are requests of their
    own. So a write small enough to hold goes out in one ``PUT``, exactly as
    it does over WebDAV, and anything larger becomes a multipart upload with
    parts of at least :data:`MIN_PART_SIZE` - which is what the protocol
    requires of every part but the last.
    """

    def __init__(
        self,
        url: str | XRootDURL,
        mode: str = "rb",
        *,
        config: Config | None = None,
        client: HTTPClient | None = None,
    ) -> None:
        # Before the base class, which can fail and then close this object.
        self._upload_id = ""
        self._parts: list[str] = []
        self._pending = bytearray()
        super().__init__(url, mode, config=config, client=client)

    def __repr__(self) -> str:
        return f"S3RawIO({str(self.url)!r}, mode={self.mode!r})"

    @property
    def _streaming(self) -> bool:
        return bool(self._upload_id)

    def _stream_after(self) -> int:
        return max(self.config.chunk_size, MIN_PART_SIZE)

    def _begin_upload(self) -> None:
        """Ask for a multipart upload, and hand it what is already buffered."""
        response = self.client.request(
            "POST",
            self._with({"uploads": ""}),
            body=b"",
            headers={"Content-Length": "0"},
            expect=(200,),
        )
        self._upload_id = _text(_xml(response, self.url.path), "UploadId")
        if not self._upload_id:
            raise ProtocolError(f"{self.url.host} began an upload without naming it")
        self._pending += self._buffer
        self._buffer = bytearray()
        self._drain(final=False)

    def _send_chunk(self, payload: bytes) -> None:
        self._pending += payload
        self._drain(final=False)

    def _drain(self, *, final: bool) -> None:
        """Send a part, once there is one worth sending."""
        if len(self._pending) >= MIN_PART_SIZE or (final and self._pending):
            body, self._pending = bytes(self._pending), bytearray()
            number = len(self._parts) + 1
            response = self.client.request(
                "PUT",
                self._with({"partNumber": str(number), "uploadId": self._upload_id}),
                body=body,
                headers={"Content-Length": str(len(body))},
                expect=(200,),
            )
            self._parts.append(_etag(response.header("ETag")))

    def _finish_upload(self) -> None:
        """Send the tail and the manifest; abandon the upload if either fails.

        An abandoned multipart upload keeps its parts - and their storage
        cost - until something deletes them, so a failure here aborts rather
        than leaving them for a lifecycle rule to find.
        """
        upload = self._upload_id
        try:
            self._drain(final=True)
            body = _manifest(self._parts)
            response = self.client.request(
                "POST",
                self._with({"uploadId": upload}),
                body=body,
                headers={"Content-Length": str(len(body)), "Content-Type": "application/xml"},
                expect=(200,),
            )
            # S3 answers 200 and *then* reports a failure in the body, so that
            # it can keep the connection alive while it assembles the object.
            if b"<Error" in response.body:
                raise ProtocolError(f"{self.url.host} failed to complete a multipart upload")
        except BaseException:
            self._abort(upload)
            raise
        finally:
            self._upload_id = ""

    def _abort(self, upload: str) -> None:
        """Throw the parts away. A failure here is not worth masking the one
        that led to it, so it is swallowed."""
        try:
            self.client.request("DELETE", self._with({"uploadId": upload}), expect=(200, 202, 204))
        except (OSError, ServerError):  # pragma: no cover - best effort only
            pass

    def _with(self, params: dict[str, str]) -> XRootDURL:
        return self.url.evolve(query={**self.url.query, **params})


# ``buffering=0`` overlaps the buffered overload the same way it does for the
# builtin; see :func:`xrdclient.io.open_url`.
@overload
def open_s3(  # type: ignore[overload-overlap]
    url: str | XRootDURL,
    mode: OpenBinaryMode = ...,
    *,
    buffering: Literal[0],
    encoding: None = ...,
    errors: None = ...,
    newline: None = ...,
    config: Config | None = ...,
    credentials: Credentials | None = ...,
    region: str = ...,
    endpoint: str = ...,
) -> S3RawIO: ...


@overload
def open_s3(
    url: str | XRootDURL,
    mode: OpenBinaryMode = ...,
    *,
    buffering: int = ...,
    encoding: None = ...,
    errors: None = ...,
    newline: None = ...,
    config: Config | None = ...,
    credentials: Credentials | None = ...,
    region: str = ...,
    endpoint: str = ...,
) -> IO[bytes]: ...


@overload
def open_s3(
    url: str | XRootDURL,
    mode: OpenTextMode,
    *,
    buffering: int = ...,
    encoding: str | None = ...,
    errors: str | None = ...,
    newline: str | None = ...,
    config: Config | None = ...,
    credentials: Credentials | None = ...,
    region: str = ...,
    endpoint: str = ...,
) -> IO[str]: ...


@overload
def open_s3(
    url: str | XRootDURL,
    mode: str,
    *,
    buffering: int = ...,
    encoding: str | None = ...,
    errors: str | None = ...,
    newline: str | None = ...,
    config: Config | None = ...,
    credentials: Credentials | None = ...,
    region: str = ...,
    endpoint: str = ...,
) -> IO[Any]: ...


def open_s3(
    url: str | XRootDURL,
    mode: str = "rb",
    *,
    buffering: int = -1,
    encoding: str | None = None,
    errors: str | None = None,
    newline: str | None = None,
    config: Config | None = None,
    credentials: Credentials | None = None,
    region: str = "",
    endpoint: str = "",
) -> IO[Any] | io.RawIOBase:
    """Open an ``s3://`` URL like :func:`open`.

        >>> with open_s3("s3://my-bucket/runs/a.root") as fh:   # doctest: +SKIP
        ...     header = fh.read(4)

    The filesystem this needs is built, used and closed around the file, so
    the connection goes back when the file does.
    """
    target = parse(url)
    fs = S3FileSystem(
        target.evolve(path="/"),
        config,
        credentials=credentials,
        region=region,
        endpoint=endpoint,
    )
    try:
        stream = fs.open(
            target.path,
            mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )
    except BaseException:
        fs.close()
        raise
    _close_with(stream, fs.close)
    return stream


def _close_with(stream: Any, done: Callable[[], None]) -> None:
    """Give ``stream`` one more thing to do when it closes.

    The stream holds the only reference to a filesystem built for it alone,
    and a file that has been closed should not be holding a connection open.
    """
    closer = stream.close

    def close() -> None:
        try:
            closer()
        finally:
            done()

    stream.close = close


# ---------------------------------------------------------------------------
# The XML S3 answers with
# ---------------------------------------------------------------------------


def _xml(response: Response, what: str) -> ET.Element:
    """Parse an S3 answer, which is XML in a namespace nobody needs."""
    try:
        return ET.fromstring(response.body)
    except ET.ParseError as exc:
        raise ProtocolError(f"{what}: unparsable S3 answer: {exc}") from exc


def _elements(parent: ET.Element, name: str) -> list[ET.Element]:
    """Every child with this local name, whatever namespace it is in."""
    return [child for child in parent if child.tag.rpartition("}")[2] == name]


def _text(parent: ET.Element, name: str) -> str:
    """The text of the first such child, or ``""``."""
    found = _elements(parent, name)
    return (found[0].text or "").strip() if found else ""


def _etag(value: str) -> str:
    """An ``ETag`` without the quotes S3 wraps it in."""
    return value.strip().strip('"')


def _is_md5(value: str) -> bool:
    """Is this ETag a digest of the object, rather than of its parts?"""
    return len(value) == 32 and all(c in "0123456789abcdef" for c in value.lower())


def _iso8601(stamp: str) -> int:
    """``2024-01-02T03:04:05.000Z`` as seconds since the epoch, or 0."""
    from datetime import datetime

    try:
        return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _manifest(etags: list[str]) -> bytes:
    """The XML that names the parts, in order, to finish an upload."""
    parts = "".join(
        f"<Part><PartNumber>{n}</PartNumber><ETag>&quot;{tag}&quot;</ETag></Part>"
        for n, tag in enumerate(etags, start=1)
    )
    return f"<CompleteMultipartUpload>{parts}</CompleteMultipartUpload>".encode()


def _host_header(url: XRootDURL) -> str:
    """What the ``Host`` header will say, which is what gets signed."""
    default = 443 if url.use_tls else 80
    return url.host if url.port == default else f"{url.host}:{url.port}"
