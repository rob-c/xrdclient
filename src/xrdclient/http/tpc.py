"""HTTP third-party copy - the ``COPY`` dialect WLCG storage speaks.

The bytes never touch this process: one endpoint is told to fetch from (or
push to) the other, and this client only brokers the rendezvous and watches
the result. That is what FTS and Rucio do for every ``https://`` transfer on
the grid, and it is the same idea as :func:`xrdclient.copy.third_party` on the
``root://`` side, spelled in headers instead of opaque CGI.

Two things about this protocol bite everyone who meets it for the first time,
and both are handled here:

* the transfer's *outcome* is in the response body, not the status line - a
  ``202 Accepted`` means the copy started, and a copy that then failed still
  arrived as a ``202``;
* the body is a stream of performance markers that only ends when the
  transfer does, so reading it is how you wait, and the last line says
  ``success:`` or ``failure:``.

Since 0.4.0 what every client of the protocol shares is
:mod:`xrdhttp.tpc`'s: reading that stream (:func:`follow_markers`, with its
:class:`Marker` and :class:`Outcome` - xgfalclient's HTTP plugin imports them
by this module's name and drives the same ``COPY`` the way davix does), the
request's headers (:func:`xrdhttp.tpc.copy_headers`), and the far side's
token rules. What stays here is the driver in this package's terms -
:class:`~xrdclient.XRootDURL` endpoints, a borrowed or owned
:class:`HTTPClient`, the macaroon minted with this client's own credential,
cleaning up a destination a failed copy left behind, and the exception an
outcome raises.

The protocol is the WLCG HTTP-TPC one (the "HttpTpcTechnical" page of the
WLCG DOMA TPC working group, implemented by dCache, XRootD's XrdHttpTpc,
StoRM WebDAV and EOS): a WebDAV ``COPY`` (RFC 4918 section 9.8) whose
``Source:`` (pull) or ``Destination:`` (push) names a URL on *another*
server, with headers prefixed ``TransferHeader`` passed on - prefix
stripped - in the request the active side makes to the passive one. The
header set and the token rules here follow gfal2's HTTP plug-in, so an FTS
job and this function present the same request:

* ``Overwrite: T``/``F`` - RFC 4918 section 10.6, honoured for the target;
* ``Credential: none``/``gridsite`` - whether the active side should use a
  delegated X.509 proxy (GridSite delegation) or only the bearer token;
* ``RequireChecksumVerification`` and ``X-Number-Of-Streams`` - extensions
  (dCache's and XrdHttpTpc's) that a server without them ignores; the first
  is always sent, ``false`` unless asked for, as gfal2 sends it.

The markers themselves borrow GridFTP's ``Perf Marker`` block format; see
:func:`follow_markers`. Start reading at :func:`third_party`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Literal, Protocol

from xrdhttp import tpc as _lib
from xrdhttp.redirects import query_token
from xrdhttp.tpc import (
    MAX_LINE,
    READ_ACTIVITIES,
    WRITE_ACTIVITIES,
    CopyOptions,
    Marker,
    Outcome,
    OutcomeKind,
    follow_markers,
)

from ..config import Config
from ..copy.engine import CopyResult
from ..errors import ProtocolError, kXR_ServerError, raise_for_status
from ..errors import TimeoutError as XRDTimeoutError
from ..log import get_logger
from ..url import XRootDURL, parse
from .client import HTTPClient, absolute_url, bearer_token, status_code

__all__ = [
    "MAX_LINE",
    "READ_ACTIVITIES",
    "WRITE_ACTIVITIES",
    "Marker",
    "Outcome",
    "OutcomeKind",
    "follow_markers",
    "third_party",
]

_log = get_logger(__name__)


class _Lines(Protocol):
    """Anything the marker reader can pull lines from - an HTTP response."""

    def readline(self, limit: int = ..., /) -> bytes:
        """One line, at most ``limit`` bytes, ``b""`` at end of stream."""


def third_party(
    source: str | XRootDURL,
    target: str | XRootDURL,
    *,
    config: Config | None = None,
    client: HTTPClient | None = None,
    overwrite: bool = True,
    mode: Literal["pull", "push"] = "pull",
    remote_token: str | None = None,
    delegate: bool = False,
    verify: bool | None = None,
    streams: int | None = None,
    transfer_headers: Mapping[str, str] | None = None,
    progress: Callable[[int, int | None], None] | None = None,
    timeout: float | None = None,
    mint_token: bool = True,
    cleanup: bool = True,
) -> CopyResult:
    """Ask one HTTP endpoint to transfer ``source`` to ``target`` directly.

        >>> third_party("davs://a.example/store/f.root", "davs://b.example/store/f.root")
        CopyResult(...)

    In the default ``"pull"`` mode the ``COPY`` goes to the destination,
    which fetches from the source; ``"push"`` sends it to the source instead,
    for a destination that cannot make outbound connections. The token for
    the *far* endpoint travels in ``TransferHeaderAuthorization``, and is
    taken from that URL's ``authz`` query parameter, then ``remote_token`` -
    so a pair of pre-signed URLs just works, each token used as given:

        >>> third_party(f"{src}?authz={read_token}", f"{dst}?authz={write_token}")

    With neither, and an ``https`` far end, a macaroon is minted there with
    this client's own credential - its X.509 proxy or its ambient bearer
    token - scoped
    to the one file and to what the transfer needs of it (``LIST,DOWNLOAD``
    to read, ``LIST,DOWNLOAD,MANAGE,UPLOAD,DELETE`` to write) for twice the
    ``timeout`` plus ten minutes. That is gfal2's ``RETRIEVE_BEARER_TOKEN``,
    and it is how a grid user with only a proxy gets EOS and dCache to copy
    between each other: neither can present the user's certificate to the
    other. The ambient token is minted with rather than forwarded because
    an identity-mapped token (DiracX) that the far end accepts directly is
    still refused on the server-to-server leg. ``mint_token=False`` skips
    the mint; a far end that cannot mint (no macaroon support) is logged and
    the copy goes ahead with the ambient token, if there is one.

    ``verify`` sets ``RequireChecksumVerification``; left at ``None`` it is
    sent as ``false``, as gfal2 sends it, because a source that puts no digest
    in its ``HEAD`` would otherwise fail the copy at a destination that checks
    in band (see :func:`xrdhttp.tpc.add_verification`). :func:`xrdclient.third_party`
    compares the two ends' checksums itself afterwards.

    ``delegate=False`` sends ``Credential: none``, which is what tells a
    server not to go looking for a delegated X.509 credential it was never
    given; pass ``True`` when the endpoints have one and you want it used.

    The call returns when the transfer is over, because the response body is
    only complete when the far side says ``success:`` - a ``failure:`` raises
    the exception the quoted status maps to. ``timeout`` is the longest to
    wait between two performance markers (by default
    :attr:`Config.request_timeout`), not a limit on the whole transfer: a
    copy that keeps reporting progress is alive however long it runs.

    A failed copy that leaves an empty destination behind has it removed
    (see :func:`_discard_if_ours`), unless ``cleanup=False`` - gfal2's
    ``transfer_cleanup`` off - leaves it to the caller.
    """
    cfg = config or Config()
    su, du = parse(source), parse(target)
    _require_http(su, du)
    # ``near`` receives the COPY and does the work; ``far`` is only ever
    # contacted by ``near``, so it is the one the token in
    # TransferHeaderAuthorization must be good for.
    near, far = _lib.ends(mode, su, du)
    owned = client or HTTPClient(cfg)
    # A delegated proxy authorises the far side by itself; minting on top
    # would only cost a round trip.
    if remote_token is None and mint_token and not delegate:
        remote_token = _minted(far, cfg, owned, write=mode == "push", timeout=timeout)
    options = CopyOptions(
        mode,
        overwrite,
        _remote_token(far, remote_token, cfg),
        delegate,
        verify,
        streams,
        transfer_headers,
    )
    # The near side's own Authorization is added by HTTPClient like any
    # request's; every header built here is about the far side.
    headers = _lib.copy_headers(_remote_url(far), options)
    started = time.monotonic()
    _log.debug("COPY %s %s -> %s", mode, su, du)
    try:
        size = _run_copy(owned, near, headers, progress, timeout)
    except OSError as exc:
        _discard_if_ours(du, cfg, exc, overwrite=overwrite, cleanup=cleanup)
        raise
    finally:
        if client is None:
            owned.close()
    return CopyResult(source=str(su), target=str(du), size=size, seconds=time.monotonic() - started)


def _discard_if_ours(
    target: XRootDURL, config: Config, exc: OSError, *, overwrite: bool, cleanup: bool
) -> None:
    """After a failed ``COPY``: remove an empty destination this copy left behind.

    A target that names the file before refusing the copy (EOS does, even
    when the refusal is the ``COPY``'s own 401) is left holding an empty
    file that looks like a finished copy of an empty one, so a failure
    normally removes an empty destination (see
    :func:`xrdclient.copy.tpc.discard_if_empty`). The exception is the
    refusal of a destination that was already there: with ``overwrite``
    off, ``Overwrite: F`` meeting an existing file answers 412
    (:class:`FileExistsError`), and that file - empty or not - was never
    this copy's to remove. ``config`` is the one the ``COPY`` itself went
    out with, which in pull mode is the destination's. ``cleanup=False``
    (gfal2's ``transfer_cleanup`` off) removes nothing at all.
    """
    if not cleanup or (not overwrite and isinstance(exc, FileExistsError)):
        return
    from ..copy.tpc import discard_if_empty

    discard_if_empty(target, config)


def _minted(
    far: XRootDURL, config: Config, client: HTTPClient, *, write: bool, timeout: float | None
) -> str | None:
    """A macaroon for ``far``, when nothing else will authorise it; else ``None``.

    A far-end token written at the call site - its URL's ``authz``, or an
    explicit ``remote_token`` the caller passed (which, when given, means this
    is never reached) - is the far end's to accept, so it is used as given and
    no macaroon is minted. But the *ambient* token, the user's own, is not: it
    authorises the control request to the near side, while the far leg is a
    server-to-server transfer the active SE makes with whatever
    ``TransferHeaderAuthorization`` carries, and an identity-mapped token
    (a DiracX token, say - accepted for a direct read or write, and to mint a
    macaroon, but not for the delegated leg) is refused there. So a macaroon
    scoped to this one file is minted with it, exactly as the proxy path does;
    if the far end cannot mint one, the ambient token is forwarded after all
    (:func:`_remote_token` falls back to it).
    """
    if not far.use_tls or query_token(far.query):
        return None
    from .dav import macaroon

    # gfal2's lifetime and scope (xrdhttp.tpc.token_minutes and
    # macaroon_caveats): long enough to outlast a slow transfer, short enough
    # that a leaked token is not a standing credential, and only the
    # activities the transfer needs of the one file.
    minutes = _lib.token_minutes(timeout or config.request_timeout)
    try:
        return macaroon(
            far,
            caveats=_lib.macaroon_caveats(write),
            validity=f"PT{minutes}M",
            config=config,
            client=client,
        )
    except (OSError, ProtocolError) as exc:
        # Not fatal: an endpoint without macaroon support may still accept
        # the copy on delegation or on a token it trusts by other means.
        _log.debug("no macaroon from %s, copying without one: %s", far.host, exc)
        return None


def _require_http(source: XRootDURL, target: XRootDURL) -> None:
    """Refuse a non-HTTP endpoint before any request is made.

    A mixed ``root://``-to-``https://`` copy is :mod:`xrdclient.copy`'s
    business; there is no ``COPY`` a ``root://`` server would understand.
    """
    for url in (source, target):
        if not url.is_http:
            raise ValueError(f"HTTP third-party copy needs two http(s) endpoints, not {url.scheme}")


def _run_copy(
    client: HTTPClient,
    near: XRootDURL,
    headers: dict[str, str],
    progress: Callable[[int, int | None], None] | None,
    timeout: float | None,
) -> int:
    """Issue the ``COPY`` and follow it to its outcome.

    ``timeout`` is passed per request rather than folded into a config,
    because a borrowed client keeps its own - and it bounds each read, which
    is the gap between two markers: a long transfer that keeps reporting is
    alive, however long it takes.
    """
    response = client.open("COPY", near, headers=headers, expect=(200, 201, 202), timeout=timeout)
    # Closing reads out a short rest of the markers, and drops the connection
    # rather than leave a longer one on the socket for the next request.
    with response:
        return _follow(response, near, progress)


def _remote_url(url: XRootDURL) -> str:
    """The far endpoint as the near one must see it: absolute, no token.

    ``dav``/``davs`` are this package's spelling; on the wire they are
    ``http``/``https``, and a server handed ``davs://`` will not resolve it.
    The path is percent-encoded and the rest of the query kept exactly as
    written (:func:`xrdhttp.tpc.strip_tokens`), since the near side makes a
    request of its own with this string and a signed URL covers its spelling.
    """
    return _lib.strip_tokens(absolute_url(url))


def _remote_token(url: XRootDURL, explicit: str | None, config: Config) -> str | None:
    """What authorises the far endpoint: its own URL first, then ``explicit``, then the ambient.

    The URL's ``authz=``/``access_token=`` (a ``Bearer`` prefix dropped) and
    ``explicit`` are :func:`xrdhttp.tpc.remote_token`'s rule; the ambient
    token is this package's discovery (:func:`~xrdclient.http.bearer_token`),
    looked up only when neither is there, so an unreadable token file does
    not fail a copy that never needed it.
    """
    return _lib.remote_token(url.query, explicit) or bearer_token(config)


def _follow(
    stream: _Lines,
    url: XRootDURL,
    progress: Callable[[int, int | None], None] | None,
) -> int:
    """Read markers until the outcome, and return the bytes transferred.

    Silence for longer than the read timeout is a copy that has stopped
    reporting, raised as this package's timeout like any other request's.
    """

    def report(total: int, marker: Marker | None) -> None:
        """Forward a real marker's running total; keepalive blocks are dropped.

        The size is reported as ``None``: a ``COPY`` never says how big the
        file is, only how much has moved.
        """
        if marker is not None and progress is not None:
            progress(total, None)

    try:
        outcome = follow_markers(stream.readline, report)
    except TimeoutError as exc:
        raise XRDTimeoutError(
            f"{url.host} sent nothing for longer than the timeout during a copy"
        ) from exc
    if outcome.kind == "closed":
        raise ProtocolError(
            f"{url.host} closed the connection before reporting the outcome of the copy"
        )
    _outcome(outcome.line, url)
    return outcome.transferred


def _outcome(text: str, url: XRootDURL) -> None:
    """Raise unless ``text`` is the success line."""
    if text.lower().startswith("success"):
        return
    detail, quoted = _lib.failure_detail(text)
    # The far side already classified this; raise what a direct request
    # would have raised - a 403 from the source is the PermissionError a
    # direct GET would have been - rather than flattening it to "server error".
    code = status_code(quoted) if quoted is not None else kXR_ServerError
    raise_for_status(code, f"third-party copy failed: {detail}", path=url.path)
