"""How a redirect rewrites the request it answered: paths, tokens and ``tried=``.

Everything here is a pure function over a request or a string - no
connection, no lock, no state - which is why it lives apart from
:class:`~xrdclient.session.router.Router`, the stateful part that decides
*when* to follow a redirect or go back to the manager. The router calls these
to decide *what* the re-issued request says:

- :func:`_path_fields` snapshots the fields a redirect token may be folded
  into, :func:`_retarget` re-applies this hop's token to them, and
  :func:`_repath` / :func:`_redirect_path` handle a redirect that names a new
  path (a negative-port URL, see :func:`_redirect_url`).
- :func:`tried` / :func:`with_tried` build the ``tried=``/``triedrc=`` CGI
  (vocabulary in :data:`TRIED_REASONS` and :data:`LOST`) that tells a manager
  which data servers already failed. :mod:`xrdclient.client.file` uses them
  for its own reopen-elsewhere path.

:mod:`xrdclient.session.router` re-exports every name, so imports from there
keep working.
"""

from __future__ import annotations

from ..errors import (
    ProtocolError,
    kXR_FSError,
    kXR_IOError,
    kXR_NotFound,
    kXR_ServerError,
)
from ..proto import constants as c
from ..proto import requests as r
from ..proto.frames import Request
from ..proto.responses import RedirectInfo
from ..url import ROOT_SCHEMES, XRootDURL, parse

#: The fields of a request that name something the server resolves as a path,
#: and so may carry opaque CGI - checked in order, subclasses first. A
#: symlink's first field is the text the link will hold, not a path: a token
#: appended there would end up inside the link. Anything not listed names its
#: path, if it has one, in ``path``.
_PATH_FIELDS: tuple[tuple[type[Request], tuple[str, ...]], ...] = (
    (r.Symlink, ("dst",)),
    (r.Mv, ("src", "dst")),
    (r.Statx, ("paths",)),
    (r.Prepare, ("paths",)),
)

#: A request's path fields as they were before any redirect touched them.
PathFields = dict[str, "str | list[str]"]


def _path_fields(request: Request) -> PathFields:
    """Snapshot the fields a redirect token may be folded into.

    A cancelling ``kXR_prepare`` names a request id rather than paths, and a
    request by handle has an empty path: neither has anywhere for a token.
    """
    if isinstance(request, r.Prepare) and request.options & c.kXR_cancel:
        return {}
    names = next((f for kind, f in _PATH_FIELDS if isinstance(request, kind)), ("path",))
    found: PathFields = {}
    for name in names:
        value = getattr(request, name, None)
        if isinstance(value, (str, list)):
            found[name] = list(value) if isinstance(value, list) else value
    return found


def _with_token(path: str, token: str) -> str:
    """``path`` with ``token`` appended as CGI - ``&`` if it already has a query."""
    if not token or not path:
        return path
    return f"{path}{'&' if '?' in path else '?'}{token}"


def _retarget(request: Request, original: PathFields, token: str) -> None:
    """Put this hop's redirect token on the request's paths.

    Built from ``original`` rather than from what the request says now: each
    redirector's token is a capability for the server it points at, so the
    previous hop's is stale by the time the next one arrives, and appending
    would pile them up - two values for one key, and the wrong one first. The
    caller's own CGI is in ``original`` and survives every hop.
    """
    for name, value in original.items():
        if isinstance(value, list):
            setattr(request, name, [_with_token(p, token) for p in value])
        else:
            setattr(request, name, _with_token(value, token))


def _as_url(host: str) -> str:
    """A negative-port host field as a URL, reading a bare ``host[:port]`` as XRootD.

    XrdCl's ``URL::FromString`` takes a string with no scheme to be a
    ``root://`` one, so a server that names only ``host:port`` - or a host
    with no port, which then gets the default - is followed rather than
    refused.
    """
    return host if "://" in host else f"root://{host}"


def _redirect_url(base: XRootDURL, host: str) -> XRootDURL:
    """Where a ``kXR_redirect`` with a negative port points.

    A negative port says the host field is a whole URL - one the server sends
    only to a client that could take it - rather than a host name. A URL for
    another protocol (``https://``, say) is a redirect this client cannot
    follow on the same request, so it is refused by name rather than dialled
    as if it were XRootD.
    """
    target = parse(_as_url(host))
    if target.scheme not in ROOT_SCHEMES:
        raise ProtocolError(f"redirected to a {target.scheme} URL, which is not XRootD: {host}")
    if not target.host:
        raise ProtocolError(f"kXR_redirect with a negative port names no host: {host!r}")
    return base.evolve(scheme=target.scheme, host=target.host, port=target.port)


def _redirect_path(target: RedirectInfo) -> str:
    """The path a redirect tells the request to use instead, or ``""``.

    Only a negative port's URL can carry one - ``root://ds//real/name``, as
    ``XrdOfs`` sends for a file whose storage is elsewhere - and XrdCl puts
    it in place of the request's own (``RewriteCGIAndPath`` with the new
    URL's path). A URL that stops at its host, or at the slash after it,
    names no path, and the request keeps its own.
    """
    if target.port >= 0:
        return ""
    url = _as_url(target.host)
    _, _, path = url.partition("://")[2].partition("/")
    return parse(url).path if path else ""


def _repath(original: PathFields, path: str) -> None:
    """Put ``path`` in place of the request's path, keeping the caller's CGI.

    The field that names where the request acts - a rename's destination,
    as XrdCl rewrites it, and a request's only path otherwise. A request by
    handle has no path to replace, and one naming a list of paths cannot
    have them all become one, so both are left as they are.
    """
    for name in reversed(original):
        value = original[name]
        if isinstance(value, str) and value:
            _, sep, query = value.partition("?")
            original[name] = f"{path}{sep}{query}"
            return


def _base_path(original: PathFields | None) -> str:
    """The path a request names, CGI off, from its snapshot of path fields."""
    for value in (original or {}).values():
        if isinstance(value, str) and value:
            return value.partition("?")[0]
    return ""


#: What XrdCl's ``XRootDMsgHandler::UpdateTriedCGI`` reports as ``triedrc``
#: for the error a data server answered with, so that the manager (``cmsd``)
#: knows why it is being asked again: ``enoent`` makes it look the file up
#: afresh rather than trust its cache, the rest make it avoid the server.
TRIED_REASONS = {
    kXR_NotFound: "enoent",
    kXR_IOError: "ioerr",
    kXR_FSError: "fserr",
    kXR_ServerError: "srverr",
}

#: The reason given when a data server did not answer at all - a dropped or
#: refused connection. XrdCl reports a stream failure as a server error.
LOST = "srverr"


def tried(hosts: list[str], host: str) -> list[str]:
    """``hosts`` with ``host`` added once, in the order they failed."""
    return hosts if host in hosts else [*hosts, host]


def with_tried(path: str, hosts: list[str], reason: str = "") -> str:
    """``path`` with ``tried=`` (and ``triedrc=``) CGI naming ``hosts``.

    What XrdCl sends a manager when it comes back after a data server
    failed: ``tried`` lists the servers, comma-joined, so the manager routes
    around them, and ``triedrc`` says why (:data:`TRIED_REASONS`). A ``tried``
    already in the caller's own CGI is merged rather than repeated - two
    values for one key leave which one wins to the server - and a stale
    ``triedrc`` is replaced.
    """
    if not hosts or not path:
        return path
    base, _, query = path.partition("?")
    kept, earlier = _without_tried(query)
    names: list[str] = []
    for host in [*earlier, *hosts]:
        names = tried(names, host)
    kept.append(f"tried={','.join(names)}")
    if reason:
        kept.append(f"triedrc={reason}")
    return f"{base}?{'&'.join(kept)}"


def _without_tried(query: str) -> tuple[list[str], list[str]]:
    """``query``'s parameters other than ``tried``/``triedrc``, and the hosts ``tried`` named."""
    kept: list[str] = []
    earlier: list[str] = []
    for item in filter(None, query.split("&")):
        key, _, value = item.partition("=")
        if key == "tried":
            earlier.extend(filter(None, value.split(",")))
        elif key != "triedrc":
            kept.append(item)
    return kept, earlier
