"""WebDAV: the namespace half of an HTTP storage element.

``PROPFIND`` is a directory listing, ``MKCOL`` is ``mkdir``, ``MOVE`` is
``rename``, ``DELETE`` is both ``unlink`` and ``rmdir``, and RFC 9530's
``Want-Repr-Digest`` (or RFC 3230's ``Want-Digest``) is ``checksum``.
:class:`HTTPFileSystem` puts those behind the same method names
:class:`~xrdclient.FileSystem` uses, so code that walks a ``root://`` tree
walks a ``davs://`` one unchanged.

XML is parsed with :mod:`xml.etree`, which never fetches an external entity;
a document that carries a DTD at all is rejected before parsing rather than
trusted to be harmless.

Specs and behaviour matched: RFC 4918 for the verbs and the ``207
Multi-Status`` body (sections 9.1 ``PROPFIND``, 9.3 ``MKCOL``, 9.6 ``DELETE``,
9.9 ``MOVE``), RFC 9530 and RFC 3230 for digests, dCache's macaroon endpoint
for :func:`macaroon`, and the WLCG Tape REST API (:mod:`.tape`) for
:meth:`HTTPFileSystem.prepare` and friends. Where a server's answer is
ambiguous the choice is gfal2/davix's, so that a script ported from
``gfal-*`` sees the same result: ``mkdir -p`` climbs from the target rather
than descending from the root, a missing property reads as ``0``.

Everything that is pure parsing - multistatus, dates, ``Digest`` values - is
in :mod:`xrdhttp.webdav`, shared with xgfalclient; this module adds the requests
and the :class:`~xrdclient.FileSystem` surface. A verb the protocol has no
counterpart for raises :class:`~xrdclient.errors.UnsupportedError`
(``kXR_Unsupported``) rather than pretending: a silent no-op ``chmod`` would
let a caller believe its permissions were set.
"""

from __future__ import annotations

import base64
import binascii
import posixpath
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import replace
from typing import IO, Any

from xrdcore.safexml import UnsafeXML, fromstring
from xrdhttp.webdav import DAV as _DAV
from xrdhttp.webdav import WANT_DIGEST as _WANT_DIGEST
from xrdhttp.webdav import as_hex as _as_hex
from xrdhttp.webdav import epoch, resource
from xrdhttp.webdav import href_path as _href_path
from xrdhttp.webdav import pick_digest as _pick_digest
from xrdhttp.webdav import text as _text

from ..client._fsbase import BaseFileSystem
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
from ..log import get_logger, redact
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
from .client import HTTPClient, Response, absolute_url
from .file import open_http

__all__ = ["HTTPFileSystem", "digest", "macaroon", "propfind"]

_log = get_logger(__name__)

#: Only the properties that map onto :class:`~xrdclient.StatInfo`. Asking for the
#: whole property set makes a listing several times larger for no gain.
_PROPFIND = (
    b'<?xml version="1.0" encoding="utf-8"?>\n'
    b'<D:propfind xmlns:D="DAV:"><D:prop>'
    b"<D:resourcetype/><D:getcontentlength/><D:getlastmodified/>"
    b"<D:creationdate/><D:displayname/>"
    b"</D:prop></D:propfind>"
)

# The RFC 3230 digest names, and the parsing of a ``Digest`` answer, are
# shared with xgfalclient: see :mod:`xrdhttp.webdav`. ``_epoch`` is
# the name :mod:`xrdclient.s3.fs` reads ``Last-Modified`` with.
_epoch = epoch

#: RFC 9530's names (IANA's "Hash Algorithms for HTTP Digest Fields") where
#: they differ from ours; anything else is asked for by its own name.
_REPR_DIGEST = {"adler32": "adler", "sha1": "sha", "sha256": "sha-256", "sha512": "sha-512"}


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------


def _parse(payload: bytes) -> ET.Element:
    """Parse a multistatus body, refusing anything that carries a DTD."""
    try:
        return fromstring(payload)
    except UnsafeXML as exc:
        raise ProtocolError("refusing a WebDAV response with a document type declaration") from exc
    except ET.ParseError as exc:
        raise ProtocolError(f"malformed WebDAV response: {exc}") from exc


def _stat_of(response: ET.Element, path: str) -> StatInfo:
    """One ``<D:response>`` as a :class:`~xrdclient.StatInfo`."""
    found = resource(response)
    flags = StatInfoFlags.IS_READABLE | (
        StatInfoFlags.IS_DIR if found.is_dir else StatInfoFlags.NONE
    )
    return StatInfo(
        id=_href_path(_text(response, "href")),
        st_size=found.size,
        flags=flags,
        st_mtime=found.mtime,
        st_ctime=found.ctime,
        path=path,
    )


def propfind(
    url: XRootDURL,
    *,
    depth: int = 0,
    config: Config | None = None,
    client: HTTPClient | None = None,
) -> list[tuple[str, StatInfo]]:
    """``PROPFIND`` ``url``, as ``(path, stat)`` pairs in document order.

    ``depth`` is RFC 4918's ``Depth`` header: ``0`` for the resource itself,
    ``1`` for a collection and its members (the resource itself included,
    usually first - callers filter it). ``infinity`` is never sent; servers
    commonly refuse it, and a recursive walk is :meth:`HTTPFileSystem.walk`.
    ``client`` is borrowed if given, else one is made and closed here.
    """
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
    # A ``200`` is accepted beside ``207`` for servers that answer a
    # one-resource PROPFIND with a plain OK; the body is the same XML.
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
    """The server's own checksum, negotiated with ``Want-Repr-Digest``.

    >>> digest("https://dav.example.org/store/f.root", "adler32")
    ChecksumInfo(algorithm='adler32', value='9f1c0a3b')

    Both generations of the negotiation are asked at once, so an old server
    and a new one each answer in one round trip: RFC 9530's
    ``Want-Repr-Digest``, answered by a ``Repr-Digest`` dictionary of
    base64 byte sequences, and RFC 3230's ``Want-Digest``, answered by a
    ``Digest`` list. A server that sends both is taken at its RFC 9530
    word, the one with a defined encoding.
    """
    cfg = config or Config()
    target = parse(url)
    wanted = (algorithm or cfg.preferred_checksum).lower()
    owned = client or HTTPClient(cfg)
    # ``=10`` is RFC 9530's preference weight (1-10, highest first); only
    # one algorithm is asked for, so its weight only has to be non-zero.
    asked = {
        "Want-Repr-Digest": f"{_REPR_DIGEST.get(wanted, wanted)}=10",
        "Want-Digest": _WANT_DIGEST.get(wanted, wanted),
    }
    # HEAD, not GET: a server computes (or looks up) the digest without
    # sending the representation it describes.
    try:
        response = owned.request("HEAD", target, headers=asked)
    finally:
        if client is None:
            owned.close()
    value = _offered_digest(response, wanted)
    if not value:
        raise UnsupportedError(kXR_Unsupported, f"no {wanted} digest offered", path=target.path)
    return ChecksumInfo(algorithm=wanted, value=value)


def _offered_digest(response: Response, wanted: str) -> str:
    """The ``wanted`` digest a response carries, as hex; ``""`` for none.

    ``Repr-Digest`` first, then ``Digest``, then - for MD5 alone - RFC 1864's
    ``Content-MD5``, a bare base64 digest with no algorithm in front of it,
    which can only be read once we already know what it is.
    """
    representation = ", ".join(response.headers.get_all("Repr-Digest") or [])
    value = _pick_repr_digest(representation, wanted)
    if not value:
        value = _pick_digest(response.header("Digest"), wanted)
    if not value and wanted == "md5":
        raw = response.header("Content-MD5")
        value = _as_hex(raw.strip(), "md5") if raw else ""
    return value


def _pick_repr_digest(field: str, algorithm: str) -> str:
    """The requested algorithm's value out of a ``Repr-Digest`` field, as hex.

    The field is an RFC 8941 dictionary - ``sha-256=:<base64>:, adler=:...:``
    - and a member may carry parameters, which say nothing about the value.
    Our own names (``adler32``) and RFC 3230's (``sha``) are matched too, for
    servers that have not caught up with the registry.
    """
    names = {
        algorithm,
        _REPR_DIGEST.get(algorithm, algorithm),
        _WANT_DIGEST.get(algorithm, algorithm),
    }
    for key, value in _sf_members(field):
        if key in names:
            return _sf_digest(value, algorithm)
    return ""


def _sf_members(field: str) -> list[tuple[str, str]]:
    """``(key, bare value)`` for each member of a structured-field dictionary."""
    members = []
    for member in _split_unquoted(field, ","):
        key, _, rest = member.partition("=")
        if key.strip():
            members.append((key.strip().lower(), _split_unquoted(rest, ";")[0].strip()))
    return members


def _split_unquoted(value: str, separator: str) -> list[str]:
    """``value`` split at each ``separator`` that is not inside a quoted string."""
    parts: list[str] = []
    current: list[str] = []
    # RFC 8941 strings are ``"..."`` with ``\\`` and ``\"`` escapes; a
    # separator inside one is data. ``escaped`` can only be set inside quotes.
    quoted = escaped = False
    for char in value:
        if char == separator and not quoted:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = quoted
        elif char == '"':
            quoted = not quoted
    parts.append("".join(current))
    return parts


def _sf_digest(value: str, algorithm: str) -> str:
    """One ``Repr-Digest`` value as hex: an RFC 8941 byte sequence, by the RFC.

    A quoted string or a bare token is read the way a ``Digest`` value is -
    some servers put the hex form there - rather than thrown away.
    """
    if len(value) >= 2 and value[0] == value[-1] == ":":
        try:
            return base64.b64decode(value[1:-1], validate=True).hex()
        except (binascii.Error, ValueError):
            return ""
    return _as_hex(value.strip('"'), algorithm) if value else ""


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
    import json  # deferred: only macaroon minting needs it, and imports cost

    cfg = config or Config()
    target = parse(url)
    owned = client or HTTPClient(cfg)
    # dCache's request format (also implemented by XRootD's XrdMacaroons):
    # a POST of ``application/macaroon-request`` to the resource itself.
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


class HTTPFileSystem(BaseFileSystem):
    """:class:`~xrdclient.FileSystem` over WebDAV.

        >>> fs = HTTPFileSystem("davs://dav.example.org")
        >>> for entry in fs.scandir("/store/user/me"):
        ...     print(entry.name, entry.stat.st_size)

    Constructed for you when :class:`~xrdclient.FileSystem` is handed an ``http``,
    ``https``, ``dav``, ``davs``, or ``webdav`` URL.

    A sibling of the ``root://`` :class:`~xrdclient.FileSystem` under
    :class:`~xrdclient.client.BaseFileSystem`, not a subclass: it inherits
    the shared helpers (``walk``, ``glob``, ``rmtree``, ...) and none of the
    binary protocol's wire code. It is registered as a virtual subclass of
    ``FileSystem``, so ``isinstance(fs, FileSystem)`` holds as it always has.
    """

    def __init__(self, url: str | XRootDURL, config: Config | None = None) -> None:
        """Bind to the endpoint ``url`` names; nothing is connected until first use.

        ``url``'s path is the base relative paths resolve against. The
        filesystem owns its :class:`HTTPClient` and closes it in
        :meth:`close`; files opened from it borrow that client.
        """
        self.url = parse(url)
        self.config = config or Config()
        self.client = HTTPClient(self.config)
        #: There is no XRootD session behind this one. Kept, as ``None``, for
        #: code that looked for one on any filesystem before the hierarchy
        #: was split.
        self._router = None

    # -- plumbing ------------------------------------------------------

    def _abs(self, path: str) -> str:
        """``path`` made absolute against the base URL's path and normalised.

        A ``?query`` on the path is kept as written - it may carry an
        opaque-to-us CGI the server wants - and ``..`` is resolved here, so
        the server never sees one.
        """
        base, sep, cgi = path.partition("?")
        if not base.startswith("/"):
            base = posixpath.join(self.url.path or "/", base)
        return posixpath.normpath(base) + (sep + cgi if sep else "")

    def _url(self, path: str, *, collection: bool = False) -> XRootDURL:
        """The full URL for ``path``; ``collection`` adds the trailing slash.

        RFC 4918 section 5.2 lets a server treat ``/dir`` as ``/dir/`` but
        does not require it, and many answer with a redirect instead; asking
        for the slash form up front saves that round trip.
        """
        target = self._abs(path)
        if collection and not target.endswith("/"):
            target += "/"
        return self.url.with_path(target)

    @property
    def endpoint(self) -> str:
        """``host:port``, as :attr:`FileSystem.endpoint` spells it for ``root://``."""
        return f"{self.url.host}:{self.url.port}"

    def close(self) -> None:
        """Close the pooled connections. Idempotent; the object stays usable."""
        self.client.close()

    def abort(self) -> None:
        """Stop every request on this filesystem now, and refuse every one after.

        Thread-safe and idempotent. Every connection the client holds - idle
        in its pool, or carrying a response being read - is shut down, which
        wakes a thread blocked on it, and that request and every later one
        raise :class:`~xrdclient.errors.AbortedError`. See
        :meth:`HTTPClient.abort <xrdclient.http.client.HTTPClient.abort>`.
        A file opened from this filesystem shares its client, and so its fate.
        """
        self.client.abort()

    def __repr__(self) -> str:
        """The endpoint and the base URL's query, with credentials redacted.

        The base path is left out. A ``repr`` is logged and printed in
        tracebacks, so tokens and signed-URL parameters in the query are
        replaced by ``<redacted>`` (:func:`xrdclient.log.redact`).
        """
        return f"HTTPFileSystem({redact(str(self.url.with_path('/')))!r})"

    def __fspath__(self) -> str:
        """The base path, as the ``root://`` filesystem answers it."""
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

    def dirlist(
        self,
        path: str = "",
        *,
        stat: bool = True,
        online: bool = False,
        flags: DirListFlags | int | str | None = None,
    ) -> bytes:
        """Refused: a ``PROPFIND`` answer is XML, not a ``kXR_dirlist`` body.

        Handing the XML back would give a caller written for the binary
        protocol something it cannot parse, and translating it would only be
        :meth:`scandir` with an extra step. Use :meth:`scandir`.
        """
        raise self._unsupported("a raw kXR_dirlist answer (use scandir)")

    # ``walk``, ``glob``, ``rmtree``, the listing helpers and the whole-file
    # ones are the base class's: they are written in terms of ``scandir``,
    # ``open`` and ``remove``, which this class provides.

    # -- mutation ------------------------------------------------------

    def mkdir(
        self, path: str, mode: int | str = 0o755, *, parents: bool = False, exist_ok: bool = False
    ) -> None:
        """``MKCOL``. ``mode`` has no HTTP equivalent and is ignored."""
        target = self._url(path, collection=True)
        if not parents:
            self._mkcol(target, exist_ok=exist_ok)
            return
        # gfal2's mkdir_rec, not a walk down from the root: try the target,
        # and only climb while the server says the parent is missing. A grid
        # storage element refuses MKCOL on the top of its namespace (EOS's
        # ``/eos/``, dCache's ``/pnfs/``) with 403 even though it exists, so
        # starting at the root cannot work there; this never goes near it.
        missing: list[XRootDURL] = []
        current = target
        while True:
            try:
                self._mkcol(current, exist_ok=exist_ok if current is target else True)
                break
            except FileNotFoundError:
                parent = posixpath.dirname(current.path.rstrip("/"))
                if parent in ("", "/"):
                    raise
                missing.append(current)
                current = self.url.with_path(parent + "/")
        for child in reversed(missing):
            self._mkcol(child, exist_ok=exist_ok if child is target else True)

    def _mkcol(self, target: XRootDURL, *, exist_ok: bool) -> None:
        """One ``MKCOL``; an existing *collection* is forgiven when ``exist_ok``.

        A missing parent arrives as ``409 Conflict`` (RFC 4918 section
        9.3.1), which :data:`~xrdclient.http.client._ERRORS` turns into the
        :class:`FileNotFoundError` :meth:`mkdir` climbs on.
        """
        # RFC 4918: MKCOL answers 405 when the collection is already there,
        # which is "exists", not "the server cannot do this". A plain
        # resource on the name answers 405 too, and ``exist_ok`` does not
        # forgive that - what exists must be a collection.
        try:
            self.client.request("MKCOL", target, expect=(201,), errors={405: kXR_ItExists})
        except FileExistsError:
            if not exist_ok or not self.isdir(target.path):
                raise
        except PermissionError:
            # EOS and dCache answer MKCOL on a collection that is there but
            # not the caller's to write with 403, not 405: still "exists".
            if not exist_ok or not self.isdir(target.path):
                raise

    def remove(self, path: str) -> None:
        """``DELETE``."""
        self.client.request("DELETE", self._url(path), expect=(200, 202, 204))

    def rmdir(self, path: str) -> None:
        """``DELETE`` on a collection. WebDAV deletes it whether or not it is
        empty, so the emptiness check POSIX promises is made here."""
        if self.listdir(path):
            from ..errors import BusyError, kXR_FileLocked

            raise BusyError(kXR_FileLocked, "directory not empty", path=self._abs(path))
        self.client.request("DELETE", self._url(path, collection=True), expect=(200, 202, 204))

    def rename(self, src: str, dst: str) -> None:
        """``MOVE`` within the same endpoint."""
        # The ``http(s)`` form, percent-encoded and without the query: this
        # is a header the server resolves, so ``davs://`` means nothing to it,
        # a raw space ends it, a name outside Latin-1 cannot be sent at all,
        # and a token in our own URL has no business being quoted back.
        destination = absolute_url(self.url.without_query().with_path(self._abs(dst)))
        self.client.request(
            "MOVE",
            self._url(src),
            headers={"Destination": destination, "Overwrite": "T"},
            expect=(201, 204),
        )

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
        """The server-side checksum of ``path``; see :func:`digest`.

        ``None`` takes :attr:`Config.preferred_checksum`. The file is never
        read by the client: an endpoint that offers no digest of that kind
        raises :class:`~xrdclient.errors.UnsupportedError` rather than
        letting a caller mistake "not computed" for "no match".
        """
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

    # Everything below raises ``kXR_Unsupported``, the code XrdCl itself
    # reports for an operation a server does not implement, so a caller's
    # ``except UnsupportedError`` behaves the same over either protocol.

    def _unsupported(self, what: str) -> ServerError:
        """The error for ``what`` - returned, so call sites read ``raise ...``."""
        return UnsupportedError(kXR_Unsupported, f"{what} has no WebDAV equivalent")

    def statvfs(self, path: str = "/") -> VFSInfo:
        """Refused: RFC 4331's quota properties are not a ``statvfs``.

        They give bytes used and available for one collection and nothing of
        inodes or block size, so any ``VFSInfo`` built from them would be
        mostly invented.
        """
        raise self._unsupported("statvfs")

    def statx(self, paths: Sequence[str]) -> list[StatInfo]:
        """One :meth:`stat` per path, in order; the first failure propagates.

        WebDAV has no multi-resource ``PROPFIND`` short of a ``Depth: 1`` on
        a common parent, which could list thousands of unrelated names.
        """
        return [self.stat(path) for path in paths]

    def chmod(self, path: str, mode: int | str) -> None:
        """Refused: permissions are the server's ACLs, with no WebDAV verb."""
        raise self._unsupported("chmod")

    def utime(
        self,
        path: str,
        times: tuple[float, float] | None = None,
        *,
        ns: tuple[int, int] | None = None,
    ) -> None:
        """Refused: the modification time is the server's to set."""
        # RFC 4918 makes getlastmodified a live property: the server owns it,
        # and a PROPPATCH of one is within its rights to refuse.
        raise self._unsupported("utime")

    def chown(self, path: str, uid: int = -1, gid: int = -1) -> None:
        """Refused: ownership follows the authenticated identity, not numbers."""
        raise self._unsupported("chown")

    def truncate(self, path: str, size: int) -> None:
        """Refused: HTTP can only replace a resource whole (a ``PUT``)."""
        raise self._unsupported("truncate by path")

    def symlink(self, target: str, link: str) -> None:
        """Refused: grid WebDAV servers expose no links (RFC 4437 is not deployed)."""
        raise self._unsupported("symlink")

    def link(self, src: str, dst: str) -> None:
        """Refused: there is no hard link in WebDAV; ``COPY`` duplicates bytes."""
        raise self._unsupported("link")

    def readlink(self, path: str) -> str:
        """Refused, for the reason :meth:`symlink` is."""
        raise self._unsupported("readlink")

    def lstat(self, path: str) -> StatInfo:
        """Refused rather than aliased to :meth:`stat`, as ``stat(follow_symlinks=False)`` is."""
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
        """Refused: an HTTP redirect is decided per request, not listed ahead.

        The signature is :meth:`FileSystem.locate`'s and stays so that the two
        filesystems are interchangeable; the options are therefore unused.
        """
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
        cancel: bool = False,
        reqid: str | None = None,
    ) -> str:
        """Stage these files, over the WLCG tape API. Returns the request id.

        Only staging: the API has no equivalent of the other ``kXR_prepare``
        flags, and ``priority`` is the site's business rather than the
        caller's. An endpoint with no tape behind it answers ``404``, which
        arrives as a :class:`~xrdclient.errors.NotFoundError` naming the API path.

        ``cancel=True`` withdraws request ``reqid`` - only ``paths`` of it,
        when there are any - as :meth:`cancel_prepare` does, and answers with
        the empty string; a request id without ``cancel``, or a cancel
        without one, is a :class:`ValueError`, as over ``root://``.
        """
        if cancel or reqid is not None:
            if not (cancel and reqid):
                raise ValueError("a cancel names its request: pass cancel=True and its reqid")
            self.cancel_prepare(reqid, paths)
            return ""
        options = prepare_flags(stage=stage, evict=evict, notify=notify, fresh=fresh, flags=flags)
        # Anything but STAGE (evict, notify, fresh...) has no Tape REST field.
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
        """Refused: ``kXR_query`` is an XRootD opcode with no HTTP verb."""
        raise self._unsupported("query")

    def query_config(self, *names: str) -> dict[str, str]:
        """Refused: an HTTP server publishes no ``kXR_Qconfig`` table."""
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
        """Refused: there is no ``kXR_protocol`` handshake to report on."""
        raise self._unsupported("protocol handshake")

    def deep_locate(self, path: str, *, create: bool = False) -> list[LocationInfo]:
        """Refused, for the reason :meth:`locate` is."""
        raise self._unsupported("locate")

    def evict(self, paths: Sequence[str]) -> str:
        """Refused: the Tape REST API's ``release`` names a request id, not paths."""
        raise self._unsupported("evicting by path - the tape API releases a request")

    def cancel_prepare(self, handle: str, paths: Sequence[str] = ()) -> None:
        """Withdraw a staging request, files and all - or only ``paths`` of it."""
        if paths:
            tape.cancel_files(self.client, self.url, handle, [self._abs(p) for p in paths])
            return
        tape.cancel(self.client, self.url, handle)

    def checksum_cancel(self, path: str) -> None:
        """Refused: a ``HEAD`` with ``Want-Repr-Digest`` has nothing to cancel."""
        raise self._unsupported("cancelling a checksum")

    def query_stats(self, selectors: str = "a") -> str:
        """Refused: XRootD's ``kXR_Qstats`` report has no HTTP counterpart."""
        raise self._unsupported("server statistics")

    def query_space(self, path: str = "/") -> SpaceInfo:
        """Refused, for the reason :meth:`statvfs` is."""
        raise self._unsupported("space query")

    def set_property(self, directive: str) -> None:
        """Refused: ``kXR_set`` is session state, and HTTP has no session."""
        raise self._unsupported("setting a server property")

    def appid(self, name: str) -> None:
        """Refused, for the reason :meth:`set_property` is."""
        raise self._unsupported("setting a server property")

    # Extended attributes: RFC 4918 dead properties (PROPPATCH) look like
    # them, but nothing guarantees a server maps them onto the file's own
    # attributes, and XrdCl's ``kXR_fattr`` semantics (namespaces,
    # create-only) cannot be promised over them. All six are refused alike.

    def getxattr(self, path: str, name: str) -> bytes:
        """Refused; see the note above."""
        raise self._unsupported("extended attributes")

    def setxattr(self, path: str, name: str, value: bytes, *, create_only: bool = False) -> None:
        """Refused; see the note above."""
        raise self._unsupported("extended attributes")

    def removexattr(self, path: str, name: str) -> None:
        """Refused; see the note above."""
        raise self._unsupported("extended attributes")

    def listxattr(self, path: str) -> list[str]:
        """Refused; see the note above."""
        raise self._unsupported("extended attributes")

    def listxattr_tree(self, path: str) -> dict[str, list[str]]:
        """Refused; see the note above."""
        raise self._unsupported("extended attributes")

    def xattrs(self, path: str) -> dict[str, bytes]:
        """Refused; see the note above."""
        raise self._unsupported("extended attributes")


# Not a subclass - see the class docstring - but still a FileSystem to every
# ``isinstance`` and ``issubclass`` that asks, S3's subclass of it included.
FileSystem.register(HTTPFileSystem)
