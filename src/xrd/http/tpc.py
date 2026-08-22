"""HTTP third-party copy - the ``COPY`` dialect WLCG storage speaks.

The bytes never touch this process: one endpoint is told to fetch from (or
push to) the other, and this client only brokers the rendezvous and watches
the result. That is what FTS and Rucio do for every ``https://`` transfer on
the grid, and it is the same idea as :func:`xrd.copy.third_party` on the
``root://`` side, spelled in headers instead of opaque CGI.

Two things about this protocol bite everyone who meets it for the first time,
and both are handled here:

* the transfer's *outcome* is in the response body, not the status line - a
  ``202 Accepted`` means the copy started, and a copy that then failed still
  arrived as a ``202``;
* the body is a stream of performance markers that only ends when the
  transfer does, so reading it is how you wait, and the last line says
  ``success:`` or ``failure:``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from .._compat import SLOTS
from .._log import get_logger
from ..config import Config
from ..copy.engine import CopyResult
from ..errors import ProtocolError, kXR_ServerError, raise_for_status
from ..url import XRootDURL, parse
from .client import HTTPClient, bearer_token, status_code

__all__ = ["third_party", "Marker"]

_log = get_logger(__name__)

#: Longest line we will believe is a performance marker. A server that sends
#: more than this is not speaking this protocol, and reading it unbounded is
#: how a client turns a confused endpoint into an out-of-memory kill.
MAX_LINE = 1 << 16

#: A ``failure:`` line usually quotes the status the far side got. Reusing it
#: means a 403 from the source raises the same
#: :class:`~xrd.errors.PermissionError` a direct ``GET`` would have raised,
#: rather than a generic "the copy failed".
_STATUS = re.compile(r"\b([45]\d\d)\b")

#: The query keys that carry a token. They are moved into
#: ``TransferHeaderAuthorization`` rather than left in the URL handed to the
#: far side, which is where :func:`~xrd.http.client.request_target` puts them
#: for a direct request too.
_TOKEN_KEYS = ("authz", "access_token")


class _Lines(Protocol):
    """Anything the marker reader can pull lines from - an HTTP response."""

    def readline(self, limit: int = ..., /) -> bytes: ...


@dataclass(frozen=True, **SLOTS)
class Marker:
    """One performance marker: how far one stripe of the transfer has got.

    Servers send one block per stripe every few seconds, which is both the
    progress report and the keepalive that stops an idle-connection reaper
    from killing a long transfer.
    """

    index: int
    transferred: int
    stripes: int = 1
    timestamp: int = 0


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
) -> CopyResult:
    """Ask one HTTP endpoint to transfer ``source`` to ``target`` directly.

        >>> third_party("davs://a.example/store/f.root", "davs://b.example/store/f.root")
        CopyResult(...)

    In the default ``"pull"`` mode the ``COPY`` goes to the destination,
    which fetches from the source; ``"push"`` sends it to the source instead,
    for a destination that cannot make outbound connections. The token for
    the *far* endpoint travels in ``TransferHeaderAuthorization``, and is
    taken from that URL's ``authz`` query parameter, then ``remote_token``,
    then the ambient token - so a pair of pre-signed URLs just works:

        >>> third_party(f"{src}?authz={read_token}", f"{dst}?authz={write_token}")

    ``delegate=False`` sends ``Credential: none``, which is what tells a
    server not to go looking for a delegated X.509 credential it was never
    given; pass ``True`` when the endpoints have one and you want it used.

    The call returns when the transfer is over, because the response body is
    only complete when the far side says ``success:`` - a ``failure:`` raises
    the exception the quoted status maps to.
    """
    cfg = _copy_config(config, timeout)
    su, du = parse(source), parse(target)
    _require_http(su, du)
    near, far = (du, su) if mode == "pull" else (su, du)
    headers = _copy_headers(
        far, cfg, mode, overwrite, remote_token, delegate, verify, streams, transfer_headers
    )
    owned = client or HTTPClient(cfg)
    started = time.monotonic()
    _log.debug("COPY %s %s -> %s", mode, su, du)
    try:
        size = _run_copy(owned, near, headers, progress)
    finally:
        if client is None:
            owned.close()
    return CopyResult(source=str(su), target=str(du), size=size, seconds=time.monotonic() - started)


def _copy_config(config: Config | None, timeout: float | None) -> Config:
    made = config or Config()
    return made.evolve(request_timeout=timeout) if timeout is not None else made


def _require_http(source: XRootDURL, target: XRootDURL) -> None:
    for url in (source, target):
        if not url.is_http:
            raise ValueError(f"HTTP third-party copy needs two http(s) endpoints, not {url.scheme}")


def _copy_headers(
    far: XRootDURL,
    config: Config,
    mode: Literal["pull", "push"],
    overwrite: bool,
    explicit_token: str | None,
    delegate: bool,
    verify: bool | None,
    streams: int | None,
    transfer_headers: Mapping[str, str] | None,
) -> dict[str, str]:
    headers = {
        "Source" if mode == "pull" else "Destination": _remote_url(far),
        "Overwrite": "T" if overwrite else "F",
        # Without this a server that supports delegation waits for one.
        "Credential": "gridsite" if delegate else "none",
    }
    _add_token(headers, _remote_token(far, explicit_token, config))
    _add_verification(headers, verify)
    _add_streams(headers, streams)
    _add_transfer_headers(headers, transfer_headers)
    return headers


def _add_token(headers: dict[str, str], token: str | None) -> None:
    if token:
        headers["TransferHeaderAuthorization"] = f"Bearer {token}"


def _add_verification(headers: dict[str, str], verify: bool | None) -> None:
    if verify is not None:
        headers["RequireChecksumVerification"] = "true" if verify else "false"


def _add_streams(headers: dict[str, str], streams: int | None) -> None:
    if streams is not None:
        headers["X-Number-Of-Streams"] = str(streams)


def _add_transfer_headers(
    headers: dict[str, str], transfer_headers: Mapping[str, str] | None
) -> None:
    for name, value in (transfer_headers or {}).items():
        headers[f"TransferHeader{name}"] = value


def _run_copy(
    client: HTTPClient,
    near: XRootDURL,
    headers: dict[str, str],
    progress: Callable[[int, int | None], None] | None,
) -> int:
    response = client.open("COPY", near, headers=headers, expect=(200, 201, 202))
    try:
        return _follow(response, near, progress)
    finally:
        response.close()


def _remote_url(url: XRootDURL) -> str:
    """The far endpoint as the near one must see it: absolute, no token.

    ``dav``/``davs`` are this package's spelling; on the wire they are
    ``http``/``https``, and a server handed ``davs://`` will not resolve it.
    """
    return url.evolve(query={k: v for k, v in url.query.items() if k not in _TOKEN_KEYS}).http_url


def _remote_token(url: XRootDURL, explicit: str | None, config: Config) -> str | None:
    """What authorises the far endpoint: its own URL first, then the ambient."""
    for key in _TOKEN_KEYS:
        from_url = url.query.get(key)
        if from_url:
            return from_url.removeprefix("Bearer ").strip()
    return explicit or bearer_token(config)


def _follow(
    stream: _Lines,
    url: XRootDURL,
    progress: Callable[[int, int | None], None] | None,
) -> int:
    """Read markers until the outcome, and return the bytes transferred.

    Each stripe reports its own running total, so the transfer's progress is
    their sum - and a marker for a stripe that has already reported replaces
    that stripe's contribution rather than adding to it.
    """
    stripes: dict[int, int] = {}
    block: dict[str, str] = {}
    while True:
        text = _marker_line(stream, url)
        finished, block = _consume_line(text, url, block, stripes, progress)
        if finished:
            return sum(stripes.values())


def _marker_line(stream: _Lines, url: XRootDURL) -> str:
    line = stream.readline(MAX_LINE)
    if not line:
        raise ProtocolError(
            f"{url.host} closed the connection before reporting the outcome of the copy"
        )
    return line.decode("utf-8", "replace").strip()


def _consume_line(
    text: str,
    url: XRootDURL,
    block: dict[str, str],
    stripes: dict[int, int],
    progress: Callable[[int, int | None], None] | None,
) -> tuple[bool, dict[str, str]]:
    lowered = text.lower()
    if lowered.startswith(("success", "failure", "failed")):
        _outcome(text, url)
        return True, block
    if lowered == "perf marker":
        return False, {}
    if lowered == "end":
        _record_marker(block, stripes, progress)
    elif ":" in text:
        name, _, value = text.partition(":")
        block[name.strip().lower()] = value.strip()
    return False, block


def _record_marker(
    block: dict[str, str],
    stripes: dict[int, int],
    progress: Callable[[int, int | None], None] | None,
) -> None:
    marker = _marker(block)
    if marker is None:
        return
    stripes[marker.index] = marker.transferred
    if progress is not None:
        progress(sum(stripes.values()), None)


def _marker(block: dict[str, str]) -> Marker | None:
    """A finished ``Perf Marker`` block, or ``None`` if it said nothing useful."""
    try:
        transferred = int(block["stripe bytes transferred"])
    except (KeyError, ValueError):
        return None
    return Marker(
        index=_int(block.get("stripe index"), 0),
        transferred=transferred,
        stripes=_int(block.get("total stripe count"), 1),
        timestamp=_int(block.get("timestamp"), 0),
    )


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _outcome(text: str, url: XRootDURL) -> None:
    """Raise unless ``text`` is the success line."""
    if text.lower().startswith("success"):
        return
    detail = text.partition(":")[2].strip() or text
    found = _STATUS.search(detail)
    # The far side already classified this; raise what a direct request
    # would have raised rather than flattening it to "server error".
    code = status_code(int(found.group(1))) if found else kXR_ServerError
    raise_for_status(code, f"third-party copy failed: {detail}", path=url.path)
