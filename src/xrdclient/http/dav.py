"""WebDAV: the namespace half of an HTTP storage element.

``PROPFIND`` is a directory listing, ``MKCOL`` is ``mkdir``, ``MOVE`` is
``rename``, ``DELETE`` is both ``unlink`` and ``rmdir``, and RFC 3230's
``Want-Digest`` is ``checksum``. :class:`HTTPFileSystem` puts those behind the
same method names :class:`~xrdclient.FileSystem` uses, so code that walks a
``root://`` tree walks a ``davs://`` one unchanged.

XML is parsed with :mod:`xml.etree`, which never fetches an external entity;
a document that carries a DTD at all is rejected before parsing rather than
trusted to be harmless.
"""

from __future__ import annotations

import email.utils
import posixpath
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import replace
from typing import IO, Any

from .._log import get_logger
from ..client.filesystem import FileSystem
from ..config import Config
from ..errors import (
    NotFoundError,
    ProtocolError,
    ServerError,
    UnsupportedError,
    kXR_ItExists,
    kXR_NotFound,
    kXR_Unsupported,
)
from ..flags import (
    DirListFlags,
    LocateFlags,
    PrepareFlags,
    QueryCode,
    StatInfoFlags,
    prepare_flags,
)
from ..types import (
    ChecksumInfo,
    DirEntry,
    LocationInfo,
    PrepareStatus,
    ProtocolInfo,
    SpaceInfo,
    StatInfo,
    VFSInfo,
)
from ..url import XRootDURL, parse
from . import tape
from .client import HTTPClient
from .file import open_http

__all__ = ["HTTPFileSystem", "digest", "macaroon", "propfind"]

_log = get_logger(__name__)

_DAV = "{DAV:}"

#: Only the properties that map onto :class:`~xrdclient.StatInfo`. Asking for the
#: whole property set makes a listing several times larger for no gain.
_PROPFIND = (
    b'<?xml version="1.0" encoding="utf-8"?>\n'
    b'<D:propfind xmlns:D="DAV:"><D:prop>'
    b"<D:resourcetype/><D:getcontentlength/><D:getlastmodified/>"
    b"<D:creationdate/><D:displayname/>"
    b"</D:prop></D:propfind>"
)

#: RFC 3230 spells some of these differently from :mod:`hashlib`.
_WANT_DIGEST = {
    "adler32": "adler32",
    "crc32": "crc32",
    "crc32c": "crc32c",
    "md5": "md5",
    "sha1": "sha",
    "sha256": "sha-256",
    "sha512": "sha-512",
    "unixcksum": "unixcksum",
}

#: Digests XRootD and dCache send as hex; the rest arrive base64 per RFC 3230.
_HEX_DIGESTS = frozenset({"adler32", "crc32", "crc32c", "unixcksum", "cksum"})

#: Hex widths :mod:`hashlib` cannot report, because these are not hashes.
#: Both encodings of a ``crc64`` are in the wild - sixteen hex digits over
#: ``root://`` and WebDAV, base64 of eight big-endian bytes under RFC 9530 -
#: and they are different lengths, so the value itself says which it is.
_HEX_WIDTHS = {"crc64": 16, "crc64nvme": 16}


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------


def _parse(payload: bytes) -> ET.Element:
    """Parse a multistatus body, refusing anything that carries a DTD."""
    if b"<!DOCTYPE" in payload[:1024] or b"<!ENTITY" in payload[:4096]:
        raise ProtocolError("refusing a WebDAV response with a document type declaration")
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ProtocolError(f"malformed WebDAV response: {exc}") from exc


def _text(prop: ET.Element | None, name: str) -> str:
    if prop is None:
        return ""
    found = prop.find(_DAV + name)
    return (found.text or "").strip() if found is not None else ""


def _href_path(href: str) -> str:
    """The server-side path a ``<D:href>`` names, however it was written."""
    raw = urllib.parse.urlsplit(href).path if "://" in href else href
    return urllib.parse.unquote(raw) or "/"


def _stat_of(response: ET.Element, path: str) -> StatInfo:
    """One ``<D:response>`` as a :class:`~xrdclient.StatInfo`."""
    prop = None
    for propstat in response.findall(_DAV + "propstat"):
        status = _text(propstat, "status")
        if " 200 " in status or not status:
            prop = propstat.find(_DAV + "prop")
            break
    resourcetype = prop.find(_DAV + "resourcetype") if prop is not None else None
    is_dir = resourcetype is not None and resourcetype.find(_DAV + "collection") is not None

    size = _text(prop, "getcontentlength")
    modified = _text(prop, "getlastmodified")
    flags = StatInfoFlags.IS_READABLE | (StatInfoFlags.IS_DIR if is_dir else StatInfoFlags.NONE)
    return StatInfo(
        id=_href_path(_text(response, "href")),
        st_size=int(size) if size.isdigit() else 0,
        flags=flags,
        st_mtime=_epoch(modified),
        st_ctime=_epoch(_text(prop, "creationdate")),
        path=path,
    )


def _epoch(stamp: str) -> int:
    """Seconds since the epoch from either date form WebDAV uses."""
    if not stamp:
        return 0
    try:
        return int(email.utils.parsedate_to_datetime(stamp).timestamp())
    except (TypeError, ValueError):
        pass
    try:  # ISO 8601, which is what <D:creationdate> carries
        from datetime import datetime

        return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def propfind(
    url: XRootDURL,
    *,
    depth: int = 0,
    config: Config | None = None,
    client: HTTPClient | None = None,
) -> list[tuple[str, StatInfo]]:
    """``PROPFIND`` ``url``, as ``(path, stat)`` pairs in document order."""
    owned = client or HTTPClient(config or Config())
    try:
        response = owned.request(
            "PROPFIND",
            url,
            body=_PROPFIND,
            headers={"Depth": str(depth), "Content-Type": 'text/xml; charset="utf-8"'},
            expect=(207, 200),
        )
    finally:
        if client is None:
            owned.close()
    root = _parse(response.body)
    found = []
    for entry in root.findall(_DAV + "response"):
        href = _text(entry, "href")
        if not href:
            raise ProtocolError(f"{url.host} sent a WebDAV response with no href")
        path = _href_path(href)
        found.append((path, _stat_of(entry, path)))
    return found


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------


def digest(
    url: str | XRootDURL,
    algorithm: str | None = None,
    *,
    config: Config | None = None,
    client: HTTPClient | None = None,
) -> ChecksumInfo:
    """The server's own checksum, negotiated with RFC 3230 ``Want-Digest``.

        >>> digest("https://dav.example.org/store/f.root", "adler32")
        ChecksumInfo(algorithm='adler32', value='9f1c0a3b')
    """
    cfg = config or Config()
    target = parse(url)
    wanted = (algorithm or cfg.preferred_checksum).lower()
    owned = client or HTTPClient(cfg)
    try:
        response = owned.request(
            "HEAD", target, headers={"Want-Digest": _WANT_DIGEST.get(wanted, wanted)}
        )
    finally:
        if client is None:
            owned.close()
    value = _pick_digest(response.header("Digest"), wanted)
    if not value and wanted == "md5":
        # RFC 1864's Content-MD5 is a bare base64 digest with no algorithm in
        # front of it, so it can only be read once we already know what it is.
        raw = response.header("Content-MD5")
        value = _as_hex(raw.strip(), "md5") if raw else ""
    if not value:
        raise UnsupportedError(
            kXR_Unsupported, f"no {wanted} digest offered", path=target.path
        )
    return ChecksumInfo(algorithm=wanted, value=value)


def _pick_digest(header: str, algorithm: str) -> str:
    """The requested algorithm's value out of a ``Digest:`` header, as hex."""
    wire = _WANT_DIGEST.get(algorithm, algorithm)
    for item in header.split(","):
        name, _, raw = item.strip().partition("=")
        if name.strip().lower() not in (wire, algorithm):
            continue
        return _as_hex(raw.strip(), algorithm)
    return ""


def _as_hex(value: str, algorithm: str) -> str:
    """RFC 3230 sends most digests base64-encoded; storage sends some as hex."""
    if algorithm in _HEX_DIGESTS or _is_hex(value, algorithm):
        return value.lower()
    try:
        import base64

        return base64.b64decode(value, validate=True).hex()
    except (ValueError, TypeError):
        return value.lower()  # already hex, as XRootD's HTTP layer sends it


def _is_hex(value: str, algorithm: str) -> bool:
    """Is this the hex form of an ``algorithm`` digest?

    Base64 cannot be mistaken for it: this algorithm's digest is a fixed
    number of bytes, and its base64 form is never as long as its hex form.
    """
    import hashlib

    wanted = _HEX_WIDTHS.get(algorithm, 0)
    if not wanted:
        try:
            wanted = 2 * hashlib.new(algorithm).digest_size
        except ValueError:
            return False
    return len(value) == wanted and all(ch in "0123456789abcdefABCDEF" for ch in value)


# ---------------------------------------------------------------------------
# Macaroons
# ---------------------------------------------------------------------------


def macaroon(
    url: str | XRootDURL,
    *,
    caveats: Sequence[str] = (),
    validity: str = "PT1H",
    config: Config | None = None,
    client: HTTPClient | None = None,
) -> str:
    """Mint a macaroon for ``url`` and return it as a bearer token.

        >>> token = macaroon(base, caveats=["activity:DOWNLOAD"], validity="PT10M")
        >>> xrdclient.copy(base / "f.root", "/tmp/f.root", config=Config(token=token))

    ``validity`` is an ISO 8601 duration, the spelling dCache and XRootD both
    use. The result is a plain string on purpose: a macaroon is just a bearer
    token, so it goes wherever :attr:`~xrdclient.Config.token` goes.
    """
    import json

    cfg = config or Config()
    target = parse(url)
    owned = client or HTTPClient(cfg)
    request = json.dumps({"caveats": list(caveats), "validity": validity}).encode()
    try:
        response = owned.request(
            "POST",
            target,
            body=request,
            headers={"Content-Type": "application/macaroon-request"},
            expect=(200, 201),
        )
    finally:
        if client is None:
            owned.close()
    try:
        token = json.loads(response.body)["macaroon"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ProtocolError(f"{target.host} returned no macaroon") from exc
    if not token:
        raise ProtocolError(f"{target.host} returned an empty macaroon")
    return str(token)


# ---------------------------------------------------------------------------
# The filesystem
# ---------------------------------------------------------------------------


class HTTPFileSystem(FileSystem):
    """:class:`~xrdclient.FileSystem` over WebDAV.

        >>> fs = HTTPFileSystem("davs://dav.example.org")
        >>> for entry in fs.scandir("/store/user/me"):
        ...     print(entry.name, entry.stat.st_size)

    Constructed for you when :class:`~xrdclient.FileSystem` is handed an ``http``,
    ``https``, ``dav``, ``davs``, or ``webdav`` URL.
    """

    def __init__(self, url: str | XRootDURL, config: Config | None = None) -> None:
        self.url = parse(url)
        self.config = config or Config()
        self.client = HTTPClient(self.config)
        #: There is no XRootD session behind this one; the inherited methods
        #: that would need one are overridden to say so.
        self._router = None  # type: ignore[assignment]

    # -- plumbing ------------------------------------------------------

    def _abs(self, path: str) -> str:
        base, sep, cgi = path.partition("?")
        if not base.startswith("/"):
            base = posixpath.join(self.url.path or "/", base)
        return posixpath.normpath(base) + (sep + cgi if sep else "")

    def _url(self, path: str, *, collection: bool = False) -> XRootDURL:
        target = self._abs(path)
        if collection and not target.endswith("/"):
            target += "/"
        return self.url.with_path(target)

    @property
    def endpoint(self) -> str:
        return f"{self.url.host}:{self.url.port}"

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> HTTPFileSystem:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"HTTPFileSystem({str(self.url.with_path('/'))!r})"

    def __fspath__(self) -> str:
        return self.url.path

    # -- interrogation -------------------------------------------------

    def ping(self) -> None:
        """``OPTIONS``: does the endpoint answer, and does it speak DAV?"""
        self.client.request("OPTIONS", self._url("/"), expect=(200, 204))

    def stat(self, path: str, *, follow_symlinks: bool = True) -> StatInfo:
        """``PROPFIND`` with ``Depth: 0``, falling back to ``HEAD``."""
        if not follow_symlinks:
            # WebDAV has no links to not follow: a collection member is what
            # the server says it is, and there is nothing behind it.
            raise self._unsupported("stat(follow_symlinks=False)")
        target = self._url(path)
        try:
            found = propfind(target, depth=0, client=self.client)
        except UnsupportedError:
            return self._stat_via_head(target)
        if not found:
            raise NotFoundError(kXR_NotFound, "no such file or directory", path=target.path)
        return replace(found[0][1], path=target.path)

    def _stat_via_head(self, target: XRootDURL) -> StatInfo:
        """What a plain HTTP server - no WebDAV at all - can still tell us."""
        response = self.client.request("HEAD", target)
        return StatInfo(
            st_size=response.content_length or 0,
            flags=StatInfoFlags.IS_READABLE,
            st_mtime=_epoch(response.header("Last-Modified")),
            path=target.path,
        )

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
        except NotFoundError:
            return False
        return True

    def isdir(self, path: str) -> bool:
        try:
            return self.stat(path).is_dir()
        except NotFoundError:
            return False

    def isfile(self, path: str) -> bool:
        try:
            return self.stat(path).is_file()
        except NotFoundError:
            return False

    def getsize(self, path: str) -> int:
        return self.stat(path).st_size

    def scandir(
        self,
        path: str = "",
        *,
        stat: bool = True,
        online: bool = False,
        algorithm: str = "",
        flags: DirListFlags | int | str | None = None,
    ) -> list[DirEntry]:
        """``PROPFIND`` with ``Depth: 1``. The collection itself is dropped.

        A ``PROPFIND`` answers with the properties whether or not anyone
        wanted them, so ``stat`` and ``online`` are accepted - the same call
        works against any endpoint - and change nothing here.

        ``algorithm`` is refused rather than dropped: a `PROPFIND` carries no
        digest, and a listing whose checksums are all ``None`` looks exactly
        like a listing that was never asked for them.
        """
        if algorithm:
            raise self._unsupported("a checksum per listing entry")
        target = self._url(path or "/", collection=True)
        here = target.path.rstrip("/") or "/"
        entries = []
        for child, info in propfind(target, depth=1, client=self.client):
            trimmed = child.rstrip("/") or "/"
            if trimmed == here:
                continue  # PROPFIND depth 1 includes the collection itself
            # A listing names what is in this collection and nothing else. An
            # href pointing anywhere else - up, sideways, or two levels down -
            # would otherwise become a DirEntry claiming to live here.
            if posixpath.dirname(trimmed) != here:
                raise ProtocolError(f"PROPFIND on {here} listed {trimmed}, which is not in it")
            entries.append(DirEntry(name=posixpath.basename(trimmed), parent=here, stat=info))
        return entries

    def listdir(self, path: str = "") -> list[str]:
        return [entry.name for entry in self.scandir(path)]

    def iterdir(self, path: str = ""):  # type: ignore[no-untyped-def]
        yield from self.scandir(path)

    # ``walk``, ``glob``, ``rmtree`` and the whole-file helpers are inherited:
    # they are written in terms of ``scandir``, ``open`` and ``remove``, which
    # this class provides.

    # -- mutation ------------------------------------------------------

    def mkdir(
        self, path: str, mode: int | str = 0o755, *, parents: bool = False, exist_ok: bool = False
    ) -> None:
        """``MKCOL``. ``mode`` has no HTTP equivalent and is ignored."""
        target = self._url(path, collection=True)
        if parents:
            for parent in _ancestors(target.path):
                self._mkcol(self.url.with_path(parent + "/"), exist_ok=True)
        self._mkcol(target, exist_ok=exist_ok)

    def _mkcol(self, target: XRootDURL, *, exist_ok: bool) -> None:
        # RFC 4918: MKCOL answers 405 when the collection is already there,
        # which is "exists", not "the server cannot do this". A plain
        # resource on the name answers 405 too, and ``exist_ok`` does not
        # forgive that - what exists must be a collection.
        try:
            self.client.request(
                "MKCOL", target, expect=(201,), errors={405: kXR_ItExists}
            )
        except FileExistsError:
            if not exist_ok or not self.isdir(target.path):
                raise

    def makedirs(self, path: str, mode: int | str = 0o755, exist_ok: bool = False) -> None:
        self.mkdir(path, mode, parents=True, exist_ok=exist_ok)

    def remove(self, path: str) -> None:
        """``DELETE``."""
        self.client.request("DELETE", self._url(path), expect=(200, 202, 204))

    unlink = remove

    def rmdir(self, path: str) -> None:
        """``DELETE`` on a collection. WebDAV deletes it whether or not it is
        empty, so the emptiness check POSIX promises is made here."""
        if self.listdir(path):
            from ..errors import BusyError, kXR_FileLocked

            raise BusyError(kXR_FileLocked, "directory not empty", path=self._abs(path))
        self.client.request("DELETE", self._url(path, collection=True), expect=(200, 202, 204))

    def rename(self, src: str, dst: str) -> None:
        """``MOVE`` within the same endpoint."""
        destination = str(self.url.with_path(self._abs(dst)))
        self.client.request(
            "MOVE",
            self._url(src),
            headers={"Destination": destination, "Overwrite": "T"},
            expect=(201, 204),
        )

    move = rename

    def touch(self, path: str, *, exist_ok: bool = True) -> None:
        """Create an empty resource, like ``touch``."""
        if not exist_ok and self.exists(path):
            from ..errors import ExistsError

            raise ExistsError(kXR_ItExists, "file exists", path=self._abs(path))
        self.client.request(
            "PUT",
            self._url(path),
            body=b"",
            headers={"Content-Length": "0"},
            expect=(200, 201, 204),
        )

    def checksum(self, path: str, algorithm: str | None = None) -> ChecksumInfo:
        return digest(self._url(path), algorithm, config=self.config, client=self.client)

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
        """Open a resource with :func:`open`'s signature.

        ``posc`` is accepted for symmetry with :meth:`FileSystem.open` and
        ignored: a PUT is atomic at the server's discretion, and there is no
        HTTP header that asks for persist-on-successful-close.
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
        )

    # -- what HTTP simply does not have --------------------------------

    def _unsupported(self, what: str) -> ServerError:
        return UnsupportedError(kXR_Unsupported, f"{what} has no WebDAV equivalent")

    def statvfs(self, path: str = "/") -> VFSInfo:
        raise self._unsupported("statvfs")

    def statx(self, paths: Sequence[str]) -> list[StatInfo]:
        return [self.stat(path) for path in paths]

    def chmod(self, path: str, mode: int | str) -> None:
        raise self._unsupported("chmod")

    def utime(
        self,
        path: str,
        times: tuple[float, float] | None = None,
        *,
        ns: tuple[int, int] | None = None,
    ) -> None:
        # RFC 4918 makes getlastmodified a live property: the server owns it,
        # and a PROPPATCH of one is within its rights to refuse.
        raise self._unsupported("utime")

    def chown(self, path: str, uid: int = -1, gid: int = -1) -> None:
        raise self._unsupported("chown")

    def truncate(self, path: str, size: int) -> None:
        raise self._unsupported("truncate by path")

    def symlink(self, target: str, link: str) -> None:
        raise self._unsupported("symlink")

    def link(self, src: str, dst: str) -> None:
        raise self._unsupported("link")

    def readlink(self, path: str) -> str:
        raise self._unsupported("readlink")

    def lstat(self, path: str) -> StatInfo:
        raise self._unsupported("lstat")

    def is_symlink(self, path: str) -> bool:
        """Nothing a WebDAV server exposes is a link, so nothing is."""
        return False

    def locate(
        self,
        path: str,
        *,
        create: bool = False,
        refresh: bool = False,
        no_wait: bool = False,
        add_peers: bool = False,
        prefer_name: bool = False,
        flags: LocateFlags | int | str | None = None,
    ) -> list[LocationInfo]:
        raise self._unsupported("locate")

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
        """Stage these files, over the WLCG tape API. Returns the request id.

        Only staging: the API has no equivalent of the other ``kXR_prepare``
        flags, and ``priority`` is the site's business rather than the
        caller's. An endpoint with no tape behind it answers ``404``, which
        arrives as a :class:`~xrdclient.errors.NotFoundError` naming the API path.
        """
        options = prepare_flags(stage=stage, evict=evict, notify=notify, fresh=fresh, flags=flags)
        if options & ~PrepareFlags.STAGE:
            raise self._unsupported(f"{PrepareFlags(options & ~PrepareFlags.STAGE)!r}")
        return tape.stage(self.client, self.url, [self._abs(p) for p in paths])

    def query_prepare(self, handle: str, paths: Sequence[str]) -> list[PrepareStatus]:
        """How the staging request ``handle`` is going, one entry per path."""
        return tape.status(self.client, self.url, handle, [self._abs(p) for p in paths])

    def archive_info(self, paths: Sequence[str]) -> list[PrepareStatus]:
        """Where each of these files lives, without asking for any of it to move."""
        return tape.archive_info(self.client, self.url, [self._abs(p) for p in paths])

    def query(self, code: QueryCode | int | str, args: str = "") -> bytes:
        raise self._unsupported("query")

    def query_config(self, *names: str) -> dict[str, str]:
        raise self._unsupported("config query")

    def extensions(self) -> frozenset[str]:
        """None, and no round trip needed to know it.

        The vendor opcodes are opcodes; HTTP has no verb for any of them, so
        the question has one answer here and it is worth answering rather than
        refusing - a caller that guards its `symlink` on this gets the same
        code path over `root://` and `davs://`.
        """
        return frozenset()

    def protocol(self) -> ProtocolInfo:
        raise self._unsupported("protocol handshake")

    def deep_locate(self, path: str, *, create: bool = False) -> list[LocationInfo]:
        raise self._unsupported("locate")

    def evict(self, paths: Sequence[str]) -> str:
        raise self._unsupported("evicting by path - the tape API releases a request")

    def cancel_prepare(self, handle: str) -> None:
        """Withdraw a staging request, files and all."""
        tape.cancel(self.client, self.url, handle)

    def checksum_cancel(self, path: str) -> None:
        raise self._unsupported("cancelling a checksum")

    def query_stats(self, selectors: str = "a") -> str:
        raise self._unsupported("server statistics")

    def query_space(self, path: str = "/") -> SpaceInfo:
        raise self._unsupported("space query")

    def set_property(self, directive: str) -> None:
        raise self._unsupported("setting a server property")

    def appid(self, name: str) -> None:
        raise self._unsupported("setting a server property")

    def getxattr(self, path: str, name: str) -> bytes:
        raise self._unsupported("extended attributes")

    def setxattr(self, path: str, name: str, value: bytes, *, create_only: bool = False) -> None:
        raise self._unsupported("extended attributes")

    def removexattr(self, path: str, name: str) -> None:
        raise self._unsupported("extended attributes")

    def listxattr(self, path: str) -> list[str]:
        raise self._unsupported("extended attributes")

    def listxattr_tree(self, path: str) -> dict[str, list[str]]:
        raise self._unsupported("extended attributes")

    def xattrs(self, path: str) -> dict[str, bytes]:
        raise self._unsupported("extended attributes")


def _ancestors(path: str) -> list[str]:
    """Every parent of ``path``, shallowest first, excluding the root."""
    parts = [p for p in path.strip("/").split("/") if p]
    return ["/" + "/".join(parts[: i + 1]) for i in range(len(parts) - 1)]
