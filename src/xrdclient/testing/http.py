"""A WebDAV server that runs in the test process.

:class:`FakeDAVServer` is to :mod:`xrdclient.http` what
:class:`~xrdclient.testing.FakeServer` is to the binary protocol: enough of a real
storage element - ranged ``GET``, chunked ``PUT``, ``PROPFIND``, ``MKCOL``,
``MOVE``, ``DELETE``, RFC 3230 and RFC 9530 digests, macaroon minting,
third-party ``COPY``, the WLCG tape REST API - to test a client against, with
knobs for the awkward cases (a server that redirects, one that demands a
token, one that has no WebDAV at all, one that ignores ranges, one whose
third-party copy fails after answering ``202``).

    >>> with FakeDAVServer(files={"/d/a.root": b"hello"}) as srv:
    ...     xrdclient.FileSystem(srv.url).listdir("/d")
    ['a.root']

Since 0.4.0 the server itself is xrdhttp's shared fake storage element,
:class:`xrdhttp.testing.StorageServer`, in its XrdCl style - the same one
xgfalclient's suite runs against - and every switch documented there
(faults, ``tokens``, ``redirect_prefixes``, S3, ...) works here.
:class:`FakeDAVServer` is the thin preset that keeps this package's
vocabulary, the one :class:`~xrdclient.testing.FakeServer` shares: a
namespace held in :attr:`~FakeDAVServer.files` and
:attr:`~FakeDAVServer.dirs`, :attr:`~FakeDAVServer.url` as an
:class:`~xrdclient.url.XRootDURL`, the request log as
:attr:`~FakeDAVServer.seen` (and ``targets``, ``headers``, ``bodies``),
:attr:`~FakeDAVServer.handlers` keyed by method, and the tape state under
its flat names (``nearline``, ``staged``, ``cancelled``, ``released``,
``no_tape``, ``sitename``). :data:`Handler` and :data:`Reply` are
xrdhttp's.

This module also re-exports, under their pre-0.4.0 names, xrdsec's generic
test HTTPS server (:class:`FakeHTTPSServer`, its :class:`Request`,
:class:`Response` and :class:`Fault`, which the storage element is built on
too) and xrdhttp's reply helpers (:func:`ranged_reply`,
:func:`propfind_reply`, :func:`multistatus`, :func:`dav_entries`,
:class:`DAVEntry`, :data:`BOUNDARY`), for a fake of some other HTTP service.
"""

from __future__ import annotations

import posixpath
import ssl

from xrdhttp.testing import BOUNDARY as BOUNDARY
from xrdhttp.testing import DAVEntry as DAVEntry
from xrdhttp.testing import Handler as Handler
from xrdhttp.testing import MemoryStore, StorageServer, Tape
from xrdhttp.testing import Reply as Reply
from xrdhttp.testing import dav_entries as dav_entries
from xrdhttp.testing import multistatus as multistatus
from xrdhttp.testing import propfind_reply as propfind_reply
from xrdhttp.testing import ranged_reply as ranged_reply
from xrdsec.testing.https import FakeHTTPSServer as FakeHTTPSServer
from xrdsec.testing.https import Fault as Fault
from xrdsec.testing.https import PeerIdentity as PeerIdentity
from xrdsec.testing.https import Request as Request
from xrdsec.testing.https import Response as Response
from xrdsec.testing.https import Route as Route
from xrdsec.testing.https import TLSServer as TLSServer
from xrdsec.testing.https import peer_identity as peer_identity
from xrdsec.testing.https import read_body as read_body

from ..url import XRootDURL, parse

__all__ = [
    "BOUNDARY",
    "DAVEntry",
    "FakeDAVServer",
    "FakeHTTPSServer",
    "Fault",
    "Handler",
    "PeerIdentity",
    "Reply",
    "Request",
    "Response",
    "Route",
    "TLSServer",
    "dav_entries",
    "multistatus",
    "peer_identity",
    "propfind_reply",
    "ranged_reply",
    "read_body",
]


class FakeDAVServer(StorageServer):
    """An HTTP/WebDAV endpoint on loopback, populated from a dict.

    Every knob is a plain public attribute set after construction and read
    per request, so a test can change the server's behaviour between two
    calls of the client - the first attempt redirected, the second served.
    The port is bound on first use and released by :meth:`stop`; serving
    threads are daemons, so a leaked server cannot hang interpreter exit.
    """

    def __init__(
        self,
        *,
        files: dict[str, bytes] | None = None,
        dirs: list[str] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        tls: ssl.SSLContext | None = None,
    ) -> None:
        """Hold ``files`` (and the empty collections ``dirs``); nothing is bound yet.

        Parents of every file and directory are created, as a real
        namespace has them. ``port=0`` lets the kernel choose, so the suite's
        parallel workers never collide on a port. The macaroon minted is the
        fixed ``macaroon-abc123``, and the tape API is on.
        """
        self._memory = MemoryStore(files, dirs or ())
        super().__init__(self._memory, tls=tls, host=host, port=port, name="xrdclient-fake-dav")
        self.macaroon = "macaroon-abc123"
        self.tape = Tape()

    # -- the namespace -------------------------------------------------------------------

    @property
    def files(self) -> dict[str, bytes]:
        """Path to contents. Mutated by the client, readable (and writable) by the test."""
        return self._memory.files

    @property
    def dirs(self) -> set[str]:
        """Collections that exist, including every parent of every file."""
        return self._memory.dirs

    @property
    def url(self) -> XRootDURL:
        """``http://host:port/`` - or ``https://`` with TLS - what a client connects to."""
        return parse(f"{self.base}/")

    # -- the request log -----------------------------------------------------------------

    @property
    def seen(self) -> list[tuple[str, str]]:
        """``(method, path)`` for every request served, in order; paths decoded and normalised."""
        # The namespace key: no trailing slash, ``//`` and ``.`` folded, as stored.
        return [
            (request.method, posixpath.normpath("/" + request.path.strip("/")))
            for request in list(self.requests)
        ]

    @property
    def targets(self) -> list[str]:
        """The raw request target of every request served, query and all."""
        return [request.target for request in list(self.requests)]

    @property
    def headers(self) -> list[dict[str, str]]:
        """The headers of every request served, in order."""
        return [request.headers for request in list(self.requests)]

    @property
    def bodies(self) -> list[bytes]:
        """The body of every request served, in order (``b""`` for none)."""
        return [request.body for request in list(self.requests)]

    # -- authorisation -------------------------------------------------------------------

    @property
    def require_token(self) -> str | None:
        """The bearer token to demand; ``None`` accepts anything."""
        return next(iter(self.tokens), None)

    @require_token.setter
    def require_token(self, token: str | None) -> None:
        """Demand ``token`` (and only it); ``None`` stops demanding one."""
        self.tokens = {token} if token else set()

    # -- the WLCG tape API ---------------------------------------------------------------

    @property
    def _tape(self) -> Tape:
        """The tape state, made afresh if a test turned the API off and on again."""
        if self.tape is None:
            self.tape = Tape()
        return self.tape

    @property
    def no_tape(self) -> bool:
        """Answer ``/api/v1`` at all - ``True`` is a site with no tape."""
        return self.tape is None

    @no_tape.setter
    def no_tape(self, absent: bool) -> None:
        """Take the tape away, or put a fresh one back."""
        self.tape = None if absent else Tape()

    @property
    def nearline(self) -> set[str]:
        """Files on tape only: staging one stays ``SUBMITTED`` until the test discards it here."""
        return self._tape.nearline

    @property
    def staged(self) -> dict[str, list[str]]:
        """Request id to the paths it named, for the requests not yet withdrawn."""
        return self._tape.staged()

    @property
    def cancelled(self) -> set[tuple[str, str]]:
        """``(request id, path)`` for each file withdrawn from a request."""
        return {(handle, path) for handle, paths in self._tape.cancelled for path in paths}

    @property
    def released(self) -> set[tuple[str, str]]:
        """``(request id, path)`` for each file released from a request."""
        return {(handle, path) for handle, paths in self._tape.released for path in paths}

    @property
    def sitename(self) -> str:
        """The name the well-known discovery document gives the site."""
        return self._tape.sitename

    @sitename.setter
    def sitename(self, name: str) -> None:
        """Rename the site."""
        self._tape.sitename = name

    def __repr__(self) -> str:
        """Where it listens and how much it holds; never binds a port to say so."""
        return f"FakeDAVServer({self._where()}, files={len(self.files)}, dirs={len(self.dirs)})"
