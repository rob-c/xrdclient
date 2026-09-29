"""One HTTP/1.1 connection pool, spelled the way the rest of the package is.

Built on :mod:`http.client` because the whole point of this package is that a
plain interpreter can talk to a storage element: no ``requests``, no
``httpx``, no wheels. What this adds on top of the stdlib is the part every
grid client needs anyway - persistent connections keyed by endpoint, bearer
tokens (macaroons and SciTokens are just bearer tokens), X.509 proxies,
redirect following across hosts, one retry when a pooled connection turns out
to have gone stale, and HTTP status codes translated into the same exceptions
the ``root://`` side raises.
"""

from __future__ import annotations

import contextlib
import http.client
import os
import ssl
import urllib.parse
import weakref
from collections.abc import Callable, Collection
from dataclasses import dataclass, field

from .._compat import SLOTS, TIMEOUTS
from .._log import get_logger
from ..config import Config
from ..errors import (
    ConnectionError as XRDConnectionError,
)
from ..errors import (
    RedirectLimitError,
    TransientError,
    kXR_ArgInvalid,
    kXR_FileLocked,
    kXR_ItExists,
    kXR_NoSpace,
    kXR_NotAuthorized,
    kXR_NotFound,
    kXR_ReqTimedOut,
    kXR_ServerError,
    kXR_Unsupported,
    raise_for_status,
)
from ..errors import (
    TimeoutError as XRDTimeoutError,
)
from ..transport.base import tls_context
from ..url import XRootDURL, parse, quote_path

__all__ = [
    "HTTPClient",
    "Response",
    "Signer",
    "absolute_url",
    "bearer_token",
    "carries_credentials",
    "check_status",
    "status_code",
    "wire_path",
]

#: What signs a request that needs more than a header of standing credentials:
#: ``(method, url, headers, body) -> headers to add``. The body is ``None``
#: when it is being streamed and so cannot be hashed up front.
Signer = Callable[[str, "XRootDURL", "dict[str, str]", "bytes | None"], "dict[str, str]"]

_log = get_logger(__name__)

#: Everything the client sends is safe to repeat, so every verb may be retried
#: once against a fresh connection. ``PUT`` and ``DELETE`` are idempotent by
#: definition; ``POST`` is not, and is excluded.
_RETRYABLE = frozenset(
    {"GET", "HEAD", "PUT", "DELETE", "OPTIONS", "PROPFIND", "MKCOL", "MOVE", "COPY"}
)

_REDIRECTS = frozenset({301, 302, 303, 307, 308})

#: HTTP status to the ``kXR_*`` code that means the same thing, so one status
#: table feeds the one exception table the whole package already has.
_ERRORS: dict[int, int] = {
    400: kXR_ArgInvalid,
    401: kXR_NotAuthorized,
    403: kXR_NotAuthorized,
    404: kXR_NotFound,
    405: kXR_Unsupported,
    409: kXR_NotFound,  # RFC 4918: the parent collection is missing
    410: kXR_NotFound,
    412: kXR_ItExists,  # a failed If-None-Match is "it is already there"
    416: kXR_ArgInvalid,
    423: kXR_FileLocked,
    429: kXR_FileLocked,
    501: kXR_Unsupported,
    503: kXR_FileLocked,
    504: kXR_ReqTimedOut,
    507: kXR_NoSpace,
}

#: What a body is truncated to when a caller asks for the whole thing. A
#: PROPFIND against a huge collection is the realistic way to blow up memory.
MAX_BODY = 1 << 26

#: How much of an abandoned body is read out to keep its connection. Less
#: than this costs less than the handshake a new connection would; more is
#: cheaper to throw away with the socket.
DRAIN_LIMIT = 64 * 1024

#: Query fields that are bearer tokens. They are presented in the
#: ``Authorization`` header and never sent in a request target.
_TOKEN_FIELDS = ("authz", "access_token")

#: Request headers that are credentials, lower-cased. ``TransferHeader*`` is
#: a whole family: a ``COPY`` asks the server to pass each one on to the far
#: side, and the one that matters is the far side's token.
_CREDENTIAL_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})
_CREDENTIAL_PREFIX = "transferheader"


@dataclass(**SLOTS)
class Response:
    """A finished HTTP response: status line, headers, and the whole body."""

    status: int
    reason: str
    headers: http.client.HTTPMessage
    body: bytes = b""
    url: str = ""

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name, default)

    @property
    def content_length(self) -> int | None:
        raw = self.headers.get("Content-Length")
        return int(raw) if raw is not None and raw.isdigit() else None

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, "replace")


def bearer_token(config: Config, url: XRootDURL | None = None) -> str | None:
    """The token to present, in the order a grid user expects it to be found.

    A token in the URL wins because it was written at the call site; then the
    explicit :attr:`~xrdclient.Config.token`, then the token file that
    ``BEARER_TOKEN_FILE`` (or :attr:`~xrdclient.Config.token_file`) names, then
    ``$BEARER_TOKEN``.
    """
    from_url = _url_token(url) if url is not None else None
    if from_url:
        return from_url
    if config.token:
        return config.token
    if config.token_file:
        try:
            with open(config.token_file, encoding="utf-8") as fh:
                return fh.read().strip() or None
        except OSError:
            _log.debug("token file %s is unreadable", config.token_file)
    return os.environ.get("BEARER_TOKEN") or None


def _context(config: Config) -> ssl.SSLContext:
    """A TLS context — the same one ``roots://`` gets.

    ``davs://`` and ``roots://`` must trust the same CAs and present the same
    X.509 proxy, so this defers to :func:`xrdclient.transport.base.tls_context`
    rather than building a second context that could drift from it.
    """
    return tls_context(config)


@dataclass(**SLOTS)
class HTTPClient:
    """Connections to one or more HTTP endpoints, reused between requests.

        >>> client = HTTPClient(config)
        >>> client.request("HEAD", parse("https://dav.example.org/store/f")).status
        200

    Not thread-safe, exactly like the :mod:`http.client` connections it holds:
    give each thread its own.
    """

    config: Config = field(default_factory=Config)
    _pool: dict[tuple[str, str, int], http.client.HTTPConnection] = field(default_factory=dict)
    #: Called with ``(method, url, headers, body)`` just before a request goes
    #: out, and answers with the headers that authorise it. ``None`` is an
    #: endpoint that wants no such thing; :mod:`xrdclient.s3` is what sets one.
    signer: Signer | None = None
    #: Domains a redirect may carry credentials to beyond the origin they
    #: were meant for: an entry matches that host and every host under it,
    #: and ``"*"`` matches anything. ``None`` takes
    #: :attr:`Config.trusted_redirect_domains`, which is empty unless set -
    #: the rule browsers and ``requests`` follow; see :func:`carries_credentials`.
    trusted_redirect_domains: tuple[str, ...] | None = None
    #: The connection each response not yet finished with is being read
    #: from, so that :meth:`abandon` can find it after a redirect.
    _serving: weakref.WeakKeyDictionary[http.client.HTTPResponse, http.client.HTTPConnection] = (
        field(default_factory=weakref.WeakKeyDictionary, repr=False)
    )

    def _trusted(self) -> tuple[str, ...]:
        """The domains credentials may follow a redirect to, from here or the config."""
        if self.trusted_redirect_domains is not None:
            return self.trusted_redirect_domains
        return tuple(self.config.trusted_redirect_domains)

    def close(self) -> None:
        """Drop every pooled connection. Idempotent."""
        for conn in self._pool.values():
            conn.close()
        self._pool.clear()

    def __enter__(self) -> HTTPClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"HTTPClient({len(self._pool)} connection(s))"

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------

    def connection(self, url: XRootDURL) -> http.client.HTTPConnection:
        """The pooled connection for ``url``'s endpoint, opened on demand."""
        key = _key(url)
        conn = self._pool.get(key)
        if conn is None:
            conn = self._connect(url)
            self._pool[key] = conn
        return conn

    def ready(self, url: XRootDURL, timeout: float | None = None) -> http.client.HTTPConnection:
        """The pooled connection for ``url``, connected and set to read.

        The handshake is bounded by :attr:`Config.connect_timeout`, which
        is all that setting is for. Every read after it waits up to
        ``timeout``, or :attr:`Config.request_timeout` - a transfer that is
        slow but alive is not cut off at the connect window. The read
        timeout is set on every request, so one request's override does not
        outlive it on the pooled connection.
        """
        conn = self.connection(url)
        if conn.sock is None:
            conn.connect()
        wait = self.config.request_timeout if timeout is None else timeout
        # Zero or less is "no limit" here, as it is for the other deadlines;
        # a socket given zero would be non-blocking instead.
        conn.sock.settimeout(wait if wait > 0 else None)
        return conn

    def _connect(self, url: XRootDURL) -> http.client.HTTPConnection:
        scheme, host, port = _key(url)
        timeout = self.config.connect_timeout
        if scheme == "https":
            return http.client.HTTPSConnection(
                host, port, timeout=timeout, context=_context(self.config)
            )
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def _discard(self, url: XRootDURL) -> None:
        conn = self._pool.pop(_key(url), None)
        if conn is not None:
            conn.close()

    def abandon(self, response: http.client.HTTPResponse) -> None:
        """Close a response that will not be read to the end, cleanly.

        Closing a response does not close its connection, and one given up
        mid-body leaves the rest on the socket, where the next request on
        that connection would read it as its status line - and a ``POST`` is
        not retried, so it fails outright. So a short remainder is read out,
        which keeps the connection; anything more costs the connection
        instead, and the pool reconnects on the next request. Idempotent.
        """
        conn = self._serving.pop(response, None)
        try:
            if not response.isclosed() and not _drain(response) and conn is not None:
                self._forget(conn)
        finally:
            response.close()

    def _forget(self, conn: http.client.HTTPConnection) -> None:
        """Close ``conn`` and take it out of the pool, wherever it is keyed."""
        conn.close()
        for key in [k for k, pooled in self._pool.items() if pooled is conn]:
            del self._pool[key]

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def headers_for(
        self,
        url: XRootDURL,
        extra: dict[str, str] | None = None,
        *,
        credentials: bool = True,
    ) -> dict[str, str]:
        """Standing headers for ``url``, with ``extra`` layered on top.

        ``credentials=False`` is a request to a host the credentials were not
        meant for: the configured token and every credential header in
        ``extra`` are left out. A token in ``url``'s own query is still
        presented, because whoever wrote that URL chose to hand it over.
        """
        headers = {"User-Agent": "xrd/1.0 (pure python)", "Accept": "*/*"}
        token = bearer_token(self.config, url) if credentials else _url_token(url)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if extra:
            headers.update(extra if credentials else _without_credentials(extra))
        return headers

    def sign(
        self, method: str, url: XRootDURL, headers: dict[str, str], body: bytes | None
    ) -> dict[str, str]:
        """Add whatever authorises this request, in place. A no-op without a
        :attr:`signer`, which is every endpoint that takes a bearer token."""
        if self.signer is not None:
            headers.update(self.signer(method, url, headers, body))
        return headers

    def open(
        self,
        method: str,
        url: XRootDURL,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        expect: tuple[int, ...] = (),
        errors: dict[int, int] | None = None,
        timeout: float | None = None,
    ) -> http.client.HTTPResponse:
        """Issue a request and hand back the *unread* response.

        The connection stays busy until the caller reads and closes it, which
        is what makes a ranged ``GET`` a stream rather than a buffer.
        ``timeout`` is the longest any one read of it may wait, in place of
        :attr:`Config.request_timeout` (see :meth:`ready`).
        """
        target = url
        asked = False
        # Credentials stay with the origin they were meant for. Once a hop
        # leaves it they stay behind for the rest of the chain, even if a
        # later hop comes back: the server that sent it there is not one
        # the caller vouched for.
        trusted = True
        for _ in range(self.config.redirect_limit + 1):
            response = self._once(
                method, target, body, headers, credentials=trusted, timeout=timeout
            )
            if response.status == 401 and not asked:
                # One shot at asking, and only ever the first time: a second
                # 401 with the credential in hand means it was refused, not
                # absent, and no amount of typing fixes that.
                asked = True
                if self._ask_for_credentials(target, response):
                    # The user was asked for this host by name, so what they
                    # typed is theirs to send to it.
                    url, trusted = target, True
                    response.read(MAX_BODY)
                    response.close()
                    continue
            if response.status in _REDIRECTS and response.getheader("Location"):
                location = response.getheader("Location", "")
                response.read()
                response.close()
                target = _redirect(target, location)
                trusted = trusted and self._carries_credentials(url, target)
                if response.status == 303:
                    method, body = "GET", None
                _log.debug("%s redirected to %s", method, target)
                continue
            return _checked(response, target, expect, errors)
        raise RedirectLimitError(f"more than {self.config.redirect_limit} redirects for {url}")

    def request(
        self,
        method: str,
        url: XRootDURL,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        expect: tuple[int, ...] = (),
        errors: dict[int, int] | None = None,
    ) -> Response:
        """Issue a request and read the whole response.

        A body that ends before its declared ``Content-Length`` is a dropped
        connection, not a shorter answer, and is raised as the
        :class:`~xrdclient.errors.TransientError` it is rather than returned as if
        the server had said less. (A body over :data:`MAX_BODY` is truncated
        deliberately, which is different: nothing was lost on the wire.)
        """
        response = self.open(method, url, body=body, headers=headers, expect=expect, errors=errors)
        try:
            payload = b"" if method == "HEAD" else response.read(MAX_BODY)
            if method != "HEAD" and len(payload) < MAX_BODY and response.length:
                raise TransientError(
                    f"connection closed {response.length} bytes short of the "
                    f"declared Content-Length for {method} {url}"
                )
        finally:
            response.close()
        return Response(response.status, response.reason, response.msg, payload, str(url))

    def _ask_for_credentials(
        self, url: XRootDURL, response: http.client.HTTPResponse
    ) -> bool:
        """Ask for what a ``401`` says is needed. ``True`` if worth retrying.

        HTTP has no security trailer, so the challenge stands in for one:
        ``WWW-Authenticate: Bearer`` wants a token, and an ``https`` endpoint
        that says anything else is asking for the X.509 proxy it would have
        wanted in the handshake. Plain ``http`` can only ever use a token.
        """
        from ..auth import supply

        challenge = (response.getheader("WWW-Authenticate") or "").lower()
        wanted = ["ztn"] if "bearer" in challenge or not url.use_tls else ["gsi", "ztn"]
        for name in wanted:
            config = supply(name, self.config, host=url.host)
            if config is not None:
                self.config = config
                # Both halves of the request change with the answer: the
                # Authorization header, and the certificate the context
                # presents - and a pooled connection has the old one.
                self.close()
                return True
        return False

    def _carries_credentials(self, origin: XRootDURL, target: XRootDURL) -> bool:
        trusted = carries_credentials(origin, target, self._trusted())
        if not trusted:
            _log.debug("not sending credentials for %s to %s", origin.host, target.host)
        return trusted

    def _once(
        self,
        method: str,
        url: XRootDURL,
        body: bytes | None,
        headers: dict[str, str] | None,
        *,
        credentials: bool = True,
        timeout: float | None = None,
    ) -> http.client.HTTPResponse:
        """One request, retried once if a pooled connection had gone stale.

        A timeout is not retried: a stale connection fails at once, and a
        server that has not answered in the time allowed would only be
        given it twice.
        """
        target = request_target(url)
        # A signer signs for the host it is given, so its signature is no
        # use anywhere else and is added whoever that host is.
        standing = self.headers_for(url, headers, credentials=credentials)
        sent = self.sign(method, url, standing, body)
        # A conditional request is not safe to repeat: the first attempt may
        # have been applied, which makes the second one fail its condition.
        repeatable = method in _RETRYABLE and not any(k.lower().startswith("if-") for k in sent)
        for attempt in (0, 1):
            try:
                conn = self.ready(url, timeout)
                conn.request(method, target, body=body, headers=sent)
                response = conn.getresponse()
                self._serving[response] = conn
                return response
            except (http.client.HTTPException, OSError) as exc:
                self._discard(url)
                if attempt == 0 and repeatable and not isinstance(exc, TIMEOUTS):
                    _log.debug("retrying %s %s after %s", method, url, exc)
                    continue
                raise _wrap(exc, method, url) from exc
        raise AssertionError("unreachable")  # pragma: no cover


def _key(url: XRootDURL) -> tuple[str, str, int]:
    scheme = "https" if url.use_tls else "http"
    return (scheme, url.host, url.port)


def _checked(
    response: http.client.HTTPResponse,
    url: XRootDURL,
    expect: tuple[int, ...],
    errors: dict[int, int] | None,
) -> http.client.HTTPResponse:
    """``response``, unless its status is one the caller did not ask for."""
    try:
        check_status(response.status, response.reason, url, expect, errors)
    except Exception:
        # Drain before unwinding: an error response left unread makes the
        # pooled connection unusable, and the next request on it would be
        # silently re-sent on a fresh one.
        try:
            response.read(MAX_BODY)
        finally:
            response.close()
        raise
    return response


def request_target(url: XRootDURL) -> str:
    """The origin-form request target: percent-encoded path plus query.

    The path is a plain name and is encoded exactly once (see
    :func:`wire_path`). The query goes exactly as written (see
    :attr:`XRootDURL.cgi`): a CDN's signed redirect covers the precise
    spelling - ``+`` is not interchangeable with ``%20`` there, nor ``%7E``
    with ``~`` - less the bearer tokens, which never appear in an HTTP
    request target.
    """
    target = wire_path(url.path or "/")
    query = url.cgi_except(_TOKEN_FIELDS)
    return f"{target}?{query}" if query else target


def wire_path(path: str) -> str:
    """A plain path percent-encoded, exactly once, for an HTTP request.

    Every byte but ``/`` and RFC 3986's unreserved characters is escaped -
    a ``%`` included. By the time a path gets here it is a name: a path
    argument is one as given, and an ``http(s)://`` URL's path was decoded
    once when it was parsed. Keeping an existing ``%41`` as an escape would
    send a file literally called ``a%41b`` to ``aAb``, and a name read back
    from a listing would not open the file it came from. The escaping is
    also exactly what S3 signs as its canonical URI, so the signature and
    the wire agree.
    """
    return quote_path(path)


def absolute_url(url: XRootDURL) -> str:
    """``url`` as another HTTP server must be handed it, in a header.

    ``dav``/``davs`` become ``http``/``https``, which is all a server
    resolves; the path is encoded by :func:`wire_path`, since a raw space
    ends the URL and a name outside Latin-1 cannot be sent at all; and the
    query goes as it was written.
    """
    scheme = "https" if url.use_tls else "http"
    query = f"?{url.cgi}" if url.cgi else ""
    return f"{scheme}://{url.netloc}{wire_path(url.path or '/')}{query}"


def carries_credentials(
    origin: XRootDURL, target: XRootDURL, trusted: Collection[str] = ()
) -> bool:
    """Whether a redirect from ``origin`` to ``target`` may carry credentials.

    The rule browsers, ``curl`` and ``requests`` follow: only to the same
    scheme, host and port, and never from HTTPS down to plain HTTP. A host
    in one of the ``trusted`` domains (see
    :attr:`HTTPClient.trusted_redirect_domains`) is let through whatever its
    port - that is how a site whose head node hands off to its data nodes is
    opted in - but still not over a downgrade.
    """
    if origin.use_tls and not target.use_tls:
        return False
    if _key(origin) == _key(target):
        return True
    return any(_in_domain(target.host, domain) for domain in trusted)


def _in_domain(host: str, domain: str) -> bool:
    domain = domain.lower().strip(".")
    return domain == "*" or host == domain or host.endswith("." + domain)


def _url_token(url: XRootDURL) -> str | None:
    """The bearer token ``url`` carries in its own query, if any."""
    for name in _TOKEN_FIELDS:
        value = url.query.get(name)
        if value:
            return value.removeprefix("Bearer ").strip()
    return None


def _without_credentials(headers: dict[str, str]) -> dict[str, str]:
    """``headers`` less any that would authenticate the request."""
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _CREDENTIAL_HEADERS
        and not name.lower().startswith(_CREDENTIAL_PREFIX)
    }


def _drain(response: http.client.HTTPResponse) -> bool:
    """Read out a short remainder. ``True`` if the response is now finished."""
    if response.length is None or response.length > DRAIN_LIMIT:
        return False
    with contextlib.suppress(http.client.HTTPException, OSError):
        response.read()
    return response.isclosed()


def _redirect(current: XRootDURL, location: str) -> XRootDURL:
    """Resolve a ``Location`` against the URL that produced it.

    A ``Location`` is a URL a server encoded; :func:`~xrdclient.url.parse`
    decodes its path once, and :func:`request_target` encodes it once again
    when it is followed.
    """
    if "://" in location:
        target = parse(location)
    else:
        base = f"{_key(current)[0]}://{current.netloc}{wire_path(current.path)}"
        joined = urllib.parse.urljoin(base, location)
        target = parse(joined).evolve(username=current.username, password=current.password)
    return target


def check_status(
    status: int,
    reason: str,
    url: XRootDURL,
    expect: tuple[int, ...],
    errors: dict[int, int] | None,
) -> None:
    """Turn a status the caller did not ask for into the right exception.

    ``errors`` overrides the table per verb, because WebDAV reuses statuses:
    a ``405`` from ``MKCOL`` means the collection is already there, while
    everywhere else it means the server does not implement the verb.
    """
    if status in expect if expect else status < 400:
        return
    raise_for_status(status_code(status, errors), f"HTTP {status} {reason}", path=url.path)


def status_code(status: int, errors: dict[int, int] | None = None) -> int:
    """The ``kXR_*`` code an HTTP status means.

    Separate from :func:`check_status` because a status does not always
    arrive in a status line: a third-party copy reports its outcome in the
    response body, and quotes the status the far side gave it.
    """
    code = (errors or {}).get(status) or _ERRORS.get(status)
    if code is not None:
        return code
    return kXR_ServerError if status >= 500 else kXR_ArgInvalid


def _wrap(exc: Exception, method: str, url: XRootDURL) -> Exception:
    """Present a transport failure as this package's error, not the stdlib's."""
    if isinstance(exc, TIMEOUTS):
        return XRDTimeoutError(f"{method} {url} timed out")
    return XRDConnectionError(f"{method} {url} failed: {exc}")
