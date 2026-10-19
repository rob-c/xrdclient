"""The WLCG Tape REST API - staging over HTTP, on :mod:`xrdhttp.tape`.

``root://`` stages with ``kXR_prepare`` and asks how it is going with
``kXR_QPrep``; the HTTP side of the same storage element does both over a
small JSON API rooted at ``/api/v1``, which is what FTS and Rucio drive when
they bring a dataset back from tape. This module is that API for this
package, and :class:`~xrdclient.http.dav.HTTPFileSystem` puts the same three
method names on top of it, so a caller that knows one scheme knows the other.

The API lives at the *server* root rather than under the export path, because
it names files in its request bodies rather than in the URL.

Since 0.4.0 the API's vocabulary is :mod:`xrdhttp.tape`'s: the reading of the
site's discovery document, the request bodies, the reading of the answers,
and the matching of a reply's entries to the paths asked about - shared with
xgfalclient's HTTP plugin, which imports :class:`TapeEndpoint`,
:func:`listed_endpoints`, :func:`newest_endpoint`, :func:`api_version`,
:func:`collapse`, :func:`entry_for` and :func:`json_text` by this name (they
*are* xrdhttp's objects). What stays here is the request side in this
package's terms: each function takes a borrowed :class:`HTTPClient` (never
closed) and an :class:`~xrdclient.XRootDURL`, raises this package's errors
through it, and answers :class:`~xrdclient.PrepareStatus` - the type a
``root://`` staging query answers - rather than xrdhttp's
:class:`~xrdhttp.tape.FileStatus`, field for field.

The verbs, as the WLCG Tape REST API v1 specification defines them (the
document the WLCG tape working group published for dCache, EOS-CTA and
StoRM): ``POST stage`` starts a request and answers its id (``201`` with a
``Location``); ``GET stage/<id>`` reports it; ``DELETE stage/<id>`` and
``POST stage/<id>/cancel`` withdraw all or part of it; ``POST
release/<id>`` lets staged files go; ``POST archiveinfo`` reports where
files are without staging anything.

Leniency is deliberate throughout: deployments predate the final
specification and still differ (field names, boolean spellings, locality
words), and a strict reader would report a staged file as missing. Each
tolerance is documented in :mod:`xrdhttp.tape`, where it is applied, with
the spelling that needs it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any

from xrdhttp import tape as _lib
from xrdhttp.tape import (
    API,
    VERSION,
    WELL_KNOWN,
    TapeEndpoint,
    api_version,
    collapse,
    entry_for,
    json_text,
    listed_endpoints,
    newest_endpoint,
)

from ..types import PrepareStatus
from ..url import XRootDURL
from .client import HTTPClient

__all__ = [
    "API",
    "VERSION",
    "WELL_KNOWN",
    "TapeEndpoint",
    "api_version",
    "archive_entries",
    "archive_info",
    "cancel",
    "cancel_files",
    "collapse",
    "discover",
    "entry_for",
    "json_text",
    "listed_endpoints",
    "newest_endpoint",
    "release",
    "request_status",
    "stage",
    "stage_files",
    "status",
]

#: JSON, for a body this client sends and a body it expects back.
_JSON = {"Content-Type": "application/json"}


def _at(base: XRootDURL, *parts: str) -> XRootDURL:
    """The API URL ``/api/v1/<parts>`` on ``base``'s server; ``base``'s own path is dropped."""
    return base.with_path(_lib.api_path(*parts))


def _statuses(found: list[_lib.FileStatus]) -> list[PrepareStatus]:
    """xrdhttp's per-file answers as this package's :class:`~xrdclient.PrepareStatus`.

    The two types have the same fields by design (both are ``kXR_QPrep``'s
    shape), so the conversion is field for field and loses nothing.
    """
    return [PrepareStatus(**dataclasses.asdict(item)) for item in found]


def stage(client: HTTPClient, base: XRootDURL, paths: Sequence[str], *, lifetime: str = "") -> str:
    """Ask for these files to be brought online. Returns the request id.

    ``lifetime`` is an ISO 8601 duration - ``"P1D"`` for a day - and asks the
    site to keep the files on disk that long once they arrive. Left empty, the
    site's own policy decides.
    """
    return stage_files(client, base, _lib.stage_entries(paths, lifetime))


def stage_files(client: HTTPClient, base: XRootDURL, files: Sequence[Mapping[str, Any]]) -> str:
    """:func:`stage` with each file's entry spelt out. Returns the request id.

    Each entry is the specification's: a ``path``, and optionally its own
    ``diskLifetime`` and ``targetedMetadata`` (a JSON object keyed by the
    site's name for itself). The id is the body's ``requestId``, or the last
    segment of the ``Location`` a server that puts it only there answers.
    """
    target = _at(base, "stage")
    res = client.request(
        "POST", target, body=_lib.stage_body(files), headers=_JSON, expect=(200, 201)
    )
    return _lib.request_id(_lib.parse_document(res.body, target), res.header("Location"))


def status(
    client: HTTPClient, base: XRootDURL, handle: str, paths: Sequence[str]
) -> list[PrepareStatus]:
    """How the staging request ``handle`` is going, one entry per path.

    A path the server's answer leaves out is reported as one the request never
    named, rather than dropped (see :func:`xrdhttp.tape.stage_statuses`).
    """
    return _statuses(_lib.stage_statuses(request_status(client, base, handle), paths))


def request_status(client: HTTPClient, base: XRootDURL, handle: str) -> Any:
    """The staging request ``handle``'s status document, as the site wrote it.

    The specification's shape is ``{"id", "createdAt", "startedAt",
    "completedAt", "files": [...]}``; :func:`status` reads the files out of it.
    """
    target = _at(base, "stage", handle)
    return _lib.parse_document(client.request("GET", target, expect=(200,)).body, target)


def cancel(client: HTTPClient, base: XRootDURL, handle: str) -> None:
    """Withdraw a staging request, files and all."""
    client.request("DELETE", _at(base, "stage", handle), expect=(200, 202, 204))


def cancel_files(client: HTTPClient, base: XRootDURL, handle: str, paths: Sequence[str]) -> None:
    """Withdraw some of the files of staging request ``handle``, leaving the rest."""
    _paths(client, _at(base, "stage", handle, "cancel"), paths)


def release(client: HTTPClient, base: XRootDURL, handle: str, paths: Sequence[str]) -> None:
    """Tell the site these files of request ``handle`` need not stay on disk for it."""
    _paths(client, _at(base, "release", handle), paths)


def _paths(client: HTTPClient, target: XRootDURL, paths: Sequence[str]) -> None:
    """``POST`` ``{"paths": [...]}`` to ``target``: the body of cancel and release."""
    body = _lib.paths_body(paths)
    client.request("POST", target, body=body, headers=_JSON, expect=(200, 202, 204))


def discover(client: HTTPClient, base: XRootDURL) -> dict[str, str]:
    """The tape endpoint the site advertises: ``{"uri", "version", "sitename"}``.

    The well-known document lists every endpoint the site runs; this picks
    XrdClHttp's choice, the newest one speaking at most :data:`VERSION`, or
    the first when none does (:func:`xrdhttp.tape.choose_endpoint`). A dict
    rather than xrdhttp's :class:`TapeEndpoint` because that is what this
    function and the pyxrootd ``tape.discover`` query have always answered.
    """
    target = base.with_path(WELL_KNOWN)
    document = _lib.parse_document(client.request("GET", target, expect=(200,)).body, target)
    chosen = _lib.choose_endpoint(document, str(target))
    return {"uri": chosen.uri, "version": chosen.version, "sitename": chosen.sitename}


def archive_info(client: HTTPClient, base: XRootDURL, paths: Sequence[str]) -> list[PrepareStatus]:
    """Where each of these files lives, without asking for any of it to move.

    The locality word is read by :func:`xrdhttp.tape.locality`: ``DISK`` or
    ``ONLINE`` is online, ``TAPE`` or ``NEARLINE`` on tape, either half of a
    compound counts, and a word naming neither is a file the site cannot give.
    """
    return _statuses(_lib.archive_statuses(archive_entries(client, base, paths), paths))


def archive_entries(client: HTTPClient, base: XRootDURL, paths: Sequence[str]) -> list[Any]:
    """The ``archiveinfo`` reply's entries, as the site wrote them."""
    target = _at(base, "archiveinfo")
    res = client.request("POST", target, body=_lib.paths_body(paths), headers=_JSON, expect=(200,))
    return _lib.entries(_lib.parse_document(res.body, target))
