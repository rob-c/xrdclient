"""Every ``http``/``https``/``dav``/``davs`` request: this package's policy over xrdhttp's session.

Built on :mod:`http.client` because the whole point of this package is that a
plain interpreter can talk to a storage element: no ``requests``, no
``httpx``, no wheels. The machinery every grid client needs - persistent
connections keyed by endpoint and TLS context, one retry when a pooled
connection turns out to have gone stale, the redirect loop, ``Expect:
100-continue``, retries with ``Retry-After`` - is
:class:`xrdhttp.session.Session`, the session xgfalclient and xrdarc use too.
What is this package's own, and lives here, is the policy it is run with:

* **credentials** - bearer tokens found exactly as ``root://``'s ``ztn``
  finds them (:func:`bearer_token`; macaroons and SciTokens are just bearer
  tokens), X.509 proxies in the TLS context ``roots://`` gets
  (:func:`~xrdclient.transport.base.tls_context`), a ``401`` answered once by
  acquiring a credential, and a request signer for S3;
* **where credentials may follow a redirect** - RFC 9110 section 15.4's
  advice and the browsers' practice: only to the same origin, or to a domain
  the user named in :attr:`Config.trusted_redirect_domains`
  (:func:`carries_credentials`); a ``COPY``'s ``TransferHeader*`` within the
  site (:func:`relays_transfer_headers`);
* **errors** - HTTP statuses mapped onto the ``kXR_*`` codes the ``root://``
  side raises (:data:`_ERRORS`, overridable per verb), which is what lets
  one ``except`` clause serve ``root://`` and ``davs://`` alike;
* **retries** - a request that may be sent twice (:data:`_RETRYABLE`, no
  ``If-*`` condition) is sent once more after a connection failure, at once,
  or after a ``429``/``503`` as long as its ``Retry-After`` asks (capped at
  :attr:`Config.wait_cap`). A timeout is not repeated.

Where it sits: :class:`HTTPClient` is the one object that owns sockets for
every HTTP request the package makes. :mod:`.dav` (the
:class:`~xrdclient.FileSystem` verbs), :mod:`.file` (ranged reads and
streamed writes), :mod:`.tpc` (WLCG third-party ``COPY``), :mod:`.tape` (the
WLCG Tape REST API) and :mod:`xrdclient.s3` all borrow one. Start reading at
:meth:`HTTPClient.open`, then :meth:`HTTPClient._prepare` (one hop's headers).

How it differs from the two clients users compare it with:

* **davix** (gfal2's HTTP layer) also pools connections, but re-sends every
  header - the ``Authorization`` it was given included - to whatever host a
  redirect names. Here credentials stay with their origin unless the user
  says otherwise.
* **XrdCl** reaches HTTP through a plug-in (XrdClHttp, itself on davix) that
  reports failures as XRootD status codes; :data:`_ERRORS` does the same
  mapping here.

What must not break: request targets are encoded exactly once
(:func:`wire_path`), bearer tokens never appear in a request target
(:func:`request_target`), and an error response is always read before it is
raised so that the pooled connection stays usable (the session does that).
"""

from __future__ import annotations

import ssl
from collections.abc import Callable, Collection
from dataclasses import dataclass, field

from xrdhttp import redirects as _policy
from xrdhttp import session as _http
from xrdhttp.connection import HTTPMessage, IncompleteRead
from xrdhttp.redirects import wire_path

from .._tokens import TOKEN_FIELDS, discover_token, url_token
from ..config import Config
from ..errors import (
    AbortedError,
    ProtocolError,
    RedirectLimitError,
    TransientError,
    _error_for_status,
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
from ..errors import ConnectionError as XRDConnectionError
from ..errors import TimeoutError as XRDTimeoutError
from ..log import get_logger, redact
from ..transport.base import tls_context
from ..url import XRootDURL, parse

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

#: Sent with every request: who is asking, and that any answer will do.
_STANDING = {"User-Agent": "xrd/1.0 (pure python)", "Accept": "*/*"}

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
MAX_BODY = _http.MAX_BODY

#: Every status below 400 is an answer unless a request says otherwise.
_SUCCESS = range(400)


@dataclass(slots=True)
class Response:
    """A finished HTTP response: status line, headers, and the whole body."""

    status: int
    reason: str
    headers: HTTPMessage
    body: bytes = b""
    url: str = ""

    def header(self, name: str, default: str = "") -> str:
        """The value of header ``name`` (case-insensitive), or ``default``.

        A header sent more than once comes back as its first occurrence,
        which is :meth:`email.message.Message.get`'s rule.
        """
        return self.headers.get(name, default)

    @property
    def content_length(self) -> int | None:
        """The declared ``Content-Length``, or ``None`` when absent or malformed.

        ``isdigit`` rather than ``int()`` in a ``try``: a signed or spaced
        value is not a valid length under RFC 9110 section 8.6, and a server
        that sends one is better treated as having declared nothing.
        """
        raw = self.headers.get("Content-Length")
        return int(raw) if raw is not None and raw.isdigit() else None

    def text(self, encoding: str = "utf-8") -> str:
        """The body decoded as ``encoding``, undecodable bytes replaced.

        Used for error bodies and XML, where a stray byte must not turn a
        diagnostic into a ``UnicodeDecodeError``.
        """
        return self.body.decode(encoding, "replace")


def bearer_token(config: Config, url: XRootDURL | None = None) -> str | None:
    """The token to present, found exactly as ``root://``'s ``ztn`` finds it.

    A token in the URL wins because it was written at the call site; then the
    explicit :attr:`~xrdclient.Config.token` (or an explicitly set
    :attr:`~xrdclient.Config.token_file`), then the WLCG Bearer Token
    Discovery order: ``$BEARER_TOKEN``, ``$BEARER_TOKEN_FILE``,
    ``$XDG_RUNTIME_DIR/bt_u<euid>``, ``/tmp/bt_u<euid>``. See
    :mod:`xrdclient._tokens`; a token file that is not private to this
    user raises :class:`~xrdclient.errors.InsecureCredentialError`.
    """
    return discover_token(config, url)


def _context(config: Config, *, system_ca: bool = False) -> ssl.SSLContext:
    """A TLS context — the same one ``roots://`` gets.

    ``davs://`` and ``roots://`` must trust the same CAs and present the same
    X.509 proxy, so this defers to :func:`xrdclient.transport.base.tls_context`
    rather than building a second context that could drift from it - and
    that shares one context between every connection made with the same
    settings and credential, which is what lets the session pool by context.
    ``system_ca`` adds the operating system's CAs to a grid CA directory; see
    :meth:`HTTPClient.system_ca_for`.
    """
    return tls_context(config, system_ca=system_ca)


class _Errors(_http.Errors):
    """This package's words for what the session reports.

    ``table`` overrides :data:`_ERRORS` for one request, because WebDAV
    reuses statuses: a ``405`` from ``MKCOL`` means the collection is already
    there, while everywhere else it means the server does not implement the
    verb.
    """

    def __init__(self, table: dict[int, int] | None = None) -> None:
        """Map statuses through ``table`` first, then :data:`_ERRORS`."""
        self.table = table

    def status(self, answer: _http.Answer) -> Exception:
        """The ``kXR_*`` exception the status means (see :func:`status_code`)."""
        message = f"HTTP {answer.status} {answer.reason}"
        code = status_code(answer.status, self.table)
        return _error_for_status(code, message, path=parse(answer.url).path)

    def transport(self, failure: _http.Failure) -> Exception:
        """A timeout, a body cut short, or a connection that failed - this package's errors.

        A body that ends before its declared ``Content-Length`` is a dropped
        connection, not a shorter answer: a :class:`TransientError`, which a
        caller that resumes (:mod:`.file`) resumes.
        """
        what = f"{failure.method} {redact(failure.url)}"
        error = failure.error
        if isinstance(error, IncompleteRead):
            return TransientError(
                f"connection closed {error.expected} bytes short of the declared "
                f"Content-Length for {what}"
            )
        if isinstance(error, TimeoutError):
            return XRDTimeoutError(f"{what} timed out")
        return XRDConnectionError(f"{what} failed: {error}")

    def redirect(
        self, method: str, url: str, problem: str, error: Exception | None = None
    ) -> Exception:
        """:class:`RedirectLimitError` for one too many; a ``Location`` not HTTP is malformed."""
        if error is None:
            return RedirectLimitError(f"{problem} for {redact(url)}")
        return ProtocolError(f"{method} {redact(url)}: {problem}")

    def closed(self, aborted: bool) -> Exception:
        """:class:`AbortedError`: only :meth:`HTTPClient.abort` closes the session."""
        return AbortedError("this HTTP client was aborted")


_DEFAULT_ERRORS = _Errors()


def _session_url(url: XRootDURL) -> str:
    """``url`` as the session carries it: this package's scheme, the path wire-encoded.

    The scheme stays ``dav``/``davs``/``https`` so a hop knows which trust
    anchors it gets (:meth:`HTTPClient.system_ca_for`); the path is encoded
    once (:func:`wire_path`), which :func:`~xrdclient.url.parse` undoes once,
    so a hop parsed back is the URL it was; and the query goes as written,
    tokens included - :meth:`HTTPClient._prepare` takes them out of the
    request target and into the ``Authorization`` header.
    """
    query = f"?{url.cgi}" if url.cgi else ""
    return f"{url.scheme}://{url.netloc}{wire_path(url.path or '/')}{query}"


@dataclass(slots=True)
class HTTPClient:
    """Connections to one or more HTTP endpoints, reused between requests.

        >>> client = HTTPClient(config)
        >>> client.request("HEAD", parse("https://dav.example.org/store/f")).status
        200

    Safe to share between threads (the session underneath is): parallel
    streams each get a connection of their own.
    """

    config: Config = field(default_factory=Config)
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
    #: Whether a TLS connection also trusts the operating system's CAs, on
    #: top of a grid CA directory. ``None`` decides by scheme: ``https://`` -
    #: the web's scheme, and the one every S3 endpoint is reached by - does;
    #: ``davs://``, the grid's, keeps to the IGTF CAs as gfal2 and XrdCl do.
    #: See :func:`~xrdclient.transport.base.tls_context`.
    system_ca: bool | None = None
    #: The pooled session every request goes through, made with the client.
    _session: _http.Session = field(init=False, repr=False)
    #: Set by :meth:`abort`, and never cleared.
    _aborted: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        """Make the session, with this package's policy (see the module documentation)."""
        config = self.config
        retry = _http.RetryPolicy(
            retries=1,
            statuses=frozenset({429, 503}),
            transport=True,
            backoff=0.0,
            cap=config.wait_cap,
            methods=_RETRYABLE,
        )
        policy = _http.Policy(
            headers=_STANDING,
            connect_timeout=config.connect_timeout,
            timeout=config.request_timeout,
            retry=retry,
            redirects=_http.RedirectPolicy(config.redirect_limit, self._carries, self._relays),
            # Verification off is the user's explicit choice in this package.
            warn_unverified=False,
        )
        self._session = _http.Session(tls=self._tls, policy=policy, errors=_DEFAULT_ERRORS)

    def system_ca_for(self, url: XRootDURL) -> bool:
        """Whether a connection to ``url`` trusts the system CAs as well."""
        if not url.use_tls:
            return False
        if self.system_ca is not None:
            return self.system_ca
        return url.scheme == "https"

    def _tls(self, url: str) -> ssl.SSLContext:
        """The session's TLS factory: one hop's context, by the scheme that hop has.

        A ``davs://`` request must not ride a connection whose server was
        vouched for by a commercial CA, and the session pools by context, so
        the two never share a connection.
        """
        parsed = parse(url)
        return _context(self.config, system_ca=self.system_ca_for(parsed))

    def _trusted(self) -> tuple[str, ...]:
        """The domains credentials may follow a redirect to, from here or the config."""
        if self.trusted_redirect_domains is not None:
            return self.trusted_redirect_domains
        return tuple(self.config.trusted_redirect_domains)

    def _carries(self, origin: str, target: str) -> bool:
        """:func:`carries_credentials` with this client's trusted domains, logged.

        The debug line is the only trace a user gets when a redirect to
        another host fails with ``401`` because their token stayed behind.
        """
        source, destination = parse(origin), parse(target)
        trusted = carries_credentials(source, destination, self._trusted())
        if not trusted:
            _log.debug("not sending credentials for %s to %s", source.host, destination.host)
        return trusted

    def _relays(self, origin: str, target: str) -> bool:
        """:func:`relays_transfer_headers` over the session's URL strings."""
        return relays_transfer_headers(parse(origin), parse(target))

    def close(self) -> None:
        """Drop every idle connection; the client stays usable. Idempotent."""
        self._session.clear()

    def idle_count(self) -> int:
        """How many connections wait to be reused (tests pin pooling with it)."""
        return self._session.idle_count()

    def abort(self) -> None:
        """Shut every connection down now, and refuse every request after.

        Safe from another thread: it wakes a thread blocked on a connection -
        a request in flight, or a response being streamed - to an end of
        stream. Such a request, and every later one, raises
        :class:`~xrdclient.errors.AbortedError`. Idempotent.
        """
        self._aborted = True
        self._session.abort()

    def __enter__(self) -> HTTPClient:
        """Use the client as a context manager; :meth:`close` runs on exit."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close every pooled connection; exceptions are never suppressed."""
        self.close()

    def __repr__(self) -> str:
        """A summary that shows the pool size and nothing that could be a secret."""
        return f"HTTPClient({self.idle_count()} connection(s))"

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def _prepare(self, hop: _http.Hop) -> None:
        """One hop's credentials and request target, just before it is sent.

        RFC 6750 section 2.1: the token goes in ``Authorization``, never in
        the target, where proxies and access logs would keep it. Where the
        caller's credentials may not go (see :func:`carries_credentials`),
        only a token in the hop's own URL is presented, because whoever wrote
        that URL chose to hand it over. A signer signs last, for the host it
        is given, so its signature covers the final headers.
        """
        url = parse(hop.url)
        token = bearer_token(self.config, url) if hop.credentials else url_token(url)
        if token:
            hop.headers["Authorization"] = f"Bearer {token}"
        if self.signer is not None:
            body = hop.body if isinstance(hop.body, bytes) else None
            hop.headers.update(self.signer(hop.method, url, hop.headers, body))
        hop.url = f"{url.scheme}://{url.netloc}{request_target(url)}"

    def _reauth(self, url: str, response: _http.Response) -> _http.Auth | None:
        """The session's ``401`` hook: credentials to repeat the hop with, if any were found."""
        if self._ask_for_credentials(parse(url), response.header("WWW-Authenticate")):
            return _http.Auth(prepare=self._prepare)
        return None

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
    ) -> _http.Response:
        """Issue a request and hand back the *unread* response.

        The connection stays busy until the caller reads and closes it, which
        is what makes a ranged ``GET`` a stream rather than a buffer.
        ``expect`` replaces the default success set (every status below 400)
        rather than adding to it: ``MKCOL`` lists ``201`` alone, because
        RFC 4918 section 9.3.1 defines no other success for it. ``timeout``
        is the longest any one read may wait, in place of
        :attr:`Config.request_timeout`; zero or less is no limit.
        """
        return self._session.request(
            method,
            _session_url(url),
            body=body,
            headers=headers,
            auth=_http.Auth(prepare=self._prepare),
            expect=expect or _SUCCESS,
            timeout=self.config.request_timeout if timeout is None else timeout,
            reauth=self._reauth,
            errors=_Errors(errors) if errors else None,
        )

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
        payload = response.body(MAX_BODY)
        # A body already buffered reads whole after an abort; the request was
        # still in progress when it came, so it fails.
        if self._aborted:
            raise AbortedError(f"{method} {url} was aborted")
        return Response(response.status, response.reason, response.headers, payload, str(url))

    def _ask_for_credentials(self, url: XRootDURL, challenge: str) -> bool:
        """Ask for what a ``401`` says is needed. ``True`` if worth retrying.

        HTTP has no security trailer, so the challenge stands in for one:
        ``WWW-Authenticate: Bearer`` wants a token, and an ``https`` endpoint
        that says anything else is asking for the X.509 proxy it would have
        wanted in the handshake. Plain ``http`` can only ever use a token.
        ``challenge`` is the answer's ``WWW-Authenticate``. The new credential
        changes the ``Authorization`` header and the context the handshake
        presents - a new context, so a new pool.
        """
        from ..auth import supply

        bearer = "bearer" in challenge.lower()
        wanted = ["ztn"] if bearer or not url.use_tls else ["gsi", "ztn"]
        for name in wanted:
            config = supply(name, self.config, host=url.host)
            if config is not None:
                self.config = config
                self.close()
                return True
        return False

    def begin_upload(
        self, method: str, url: XRootDURL, headers: dict[str, str]
    ) -> tuple[_http.Upload, XRootDURL]:
        """Open a chunked upload of unknown length, following early redirects.

        The headers go out with ``Expect: 100-continue``, so a server that
        redirects the upload does it before any of the body is committed to
        the wrong host. Returns the upload to stream the chunks into and the
        URL they are going to. A server that refuses before the body is sent
        is raised here, as its status means.
        """
        upload = self._session.upload(
            method,
            _session_url(url),
            None,
            headers=headers,
            auth=_http.Auth(prepare=self._prepare),
            timeout=self.config.request_timeout,
        )
        target = parse(upload.url)
        early = upload.early
        if early is not None:
            status, reason = early.status, early.reason
            early.close()
            check_status(status, reason, target, (), None)
            raise ProtocolError(
                f"{target.host} answered {method} with {status} {reason} before the body was sent"
            )
        return upload, target


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
    query = url.cgi_except(TOKEN_FIELDS)
    return f"{target}?{query}" if query else target


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


def _origin(url: XRootDURL) -> str:
    """``url``'s origin as xrdhttp's policy reads one: ``http(s)://host:port``.

    The policy needs nothing past the authority, and only ``http``/``https``
    (and the grid's ``dav``/``davs``) are schemes it knows, so the wire
    scheme is spelt out here rather than handing over whatever this package
    called the URL.
    """
    return f"{'https' if url.use_tls else 'http'}://{url.netloc}"


def carries_credentials(
    origin: XRootDURL, target: XRootDURL, trusted: Collection[str] = ()
) -> bool:
    """Whether a redirect from ``origin`` to ``target`` may carry credentials.

    The rule browsers, ``curl`` and ``requests`` follow
    (:func:`xrdhttp.redirects.carries_credentials`): only to the same scheme,
    host and port, and never from HTTPS down to plain HTTP. A host in one of
    the ``trusted`` domains (see :attr:`HTTPClient.trusted_redirect_domains`)
    is let through whatever its port - that is how a site whose head node
    hands off to its data nodes is opted in - but still not over a downgrade.
    """
    return _policy.carries_credentials(_origin(origin), _origin(target), trusted)


def relays_transfer_headers(origin: XRootDURL, target: XRootDURL) -> bool:
    """Whether a redirect may carry a ``COPY``'s ``TransferHeader*`` headers.

    They follow within the site that sent the redirect - the origin's parent
    domain, unless that is a public suffix such as ``ac.uk`` - even down to
    ``http``, because EOS hands every ``COPY`` to a disk server of its own
    over plain ``http`` and a copy whose headers stayed behind fails there
    with ``401``; never anywhere else. The caller's own ``Authorization``
    keeps the stricter :func:`carries_credentials`. The rule, and why, is
    :func:`xrdhttp.redirects.relays_transfer_headers`.
    """
    return _policy.relays_transfer_headers(_origin(origin), _origin(target))


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
    # ``expect`` replaces the default success set (every status below 400)
    # rather than adding to it, as for :meth:`HTTPClient.open`.
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
