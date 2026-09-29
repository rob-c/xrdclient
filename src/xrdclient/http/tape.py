"""The WLCG Tape REST API - staging over HTTP.

``root://`` stages with ``kXR_prepare`` and asks how it is going with
``kXR_QPrep``; the HTTP side of the same storage element does both over a
small JSON API rooted at ``/api/v1``, which is what FTS and Rucio drive when
they bring a dataset back from tape. This module is that API, and
:class:`~xrdclient.http.dav.HTTPFileSystem` puts the same three method names on top
of it, so a caller that knows one scheme knows the other.

The API lives at the *server* root rather than under the export path, because
it names files in its request bodies rather than in the URL.
"""

from __future__ import annotations

import json
import posixpath
from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import ProtocolError
from ..types import PrepareStatus
from ..url import XRootDURL
from .client import HTTPClient

__all__ = [
    "archive_entries",
    "archive_info",
    "cancel",
    "cancel_files",
    "discover",
    "release",
    "request_status",
    "stage",
    "stage_files",
    "status",
]

#: Where the API is rooted. Fixed by the WLCG specification, not by the site.
API = "/api/v1"

#: Where a site describes its tape endpoints, per the same specification.
WELL_KNOWN = "/.well-known/wlcg-tape-rest-api"

#: The API version this module speaks, and so the endpoint discovery prefers.
VERSION = "v1"

#: JSON, for a body this client sends and a body it expects back.
_JSON = {"Content-Type": "application/json"}

#: The states of a staging request that mean the bytes are not coming.
_GIVEN_UP = ("FAILED", "CANCELLED")


def _at(base: XRootDURL, *parts: str) -> XRootDURL:
    return base.with_path(posixpath.join(API, *parts))


def _flag(value: object) -> bool:
    """One JSON field as a boolean, whichever way the server spelt it.

    Implementations have written these as ``true``, as ``1`` and as ``"1"``,
    and all three mean yes; a client that only understood one would report a
    staged file as still on tape.
    """
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def _document(payload: bytes, url: object) -> Any:
    try:
        return json.loads(payload.decode("utf-8", "replace") or "{}")
    except ValueError as exc:
        raise ProtocolError(f"{url} did not answer with JSON: {payload[:120]!r}") from exc


def _entries(document: Any) -> list[Any]:
    """The file list of a reply, whether it is the whole body or a member.

    The specification puts it under ``files``; some servers answer with the
    bare array, and one release of the standard called it ``responses``.
    """
    if isinstance(document, dict):
        for key in ("files", "responses"):
            found = document.get(key)
            if found is not None:
                return list(found)
        return []
    return list(document or [])


def _ordered(found: dict[str, PrepareStatus], paths: Sequence[str]) -> list[PrepareStatus]:
    """One status per path asked about, in that order - the ``kXR_QPrep`` shape.

    A file the reply says nothing about is reported as one the request never
    named, rather than quietly dropped: the caller asked about it, and silence
    is an answer it would have to guess at.
    """
    if not paths:
        return list(found.values())
    return [
        found.get(path, PrepareStatus(path=path, error="not part of this request"))
        for path in paths
    ]


def stage(
    client: HTTPClient, base: XRootDURL, paths: Sequence[str], *, lifetime: str = ""
) -> str:
    """Ask for these files to be brought online. Returns the request id.

    ``lifetime`` is an ISO 8601 duration - ``"P1D"`` for a day - and asks the
    site to keep the files on disk that long once they arrive. Left empty, the
    site's own policy decides.
    """
    files: list[dict[str, str]] = [{"path": path} for path in paths]
    if lifetime:
        for entry in files:
            entry["diskLifetime"] = lifetime
    return stage_files(client, base, files)


def stage_files(client: HTTPClient, base: XRootDURL, files: Sequence[Mapping[str, Any]]) -> str:
    """:func:`stage` with each file's entry spelt out. Returns the request id.

    Each entry is the specification's: a ``path``, and optionally its own
    ``diskLifetime`` and ``targetedMetadata`` (a JSON object keyed by the
    site's name for itself).
    """
    target = _at(base, "stage")
    res = client.request(
        "POST", target, body=json.dumps({"files": list(files)}).encode(), headers=_JSON,
        expect=(200, 201),
    )
    document = _document(res.body, target)
    handle = str(document.get("requestId", "")) if isinstance(document, dict) else ""
    # Some servers put the id only in the Location they redirect a poller to.
    return handle or res.header("Location").rstrip("/").rpartition("/")[2]


def status(
    client: HTTPClient, base: XRootDURL, handle: str, paths: Sequence[str]
) -> list[PrepareStatus]:
    """How the staging request ``handle`` is going, one entry per path."""
    found = {}
    for entry in _entries(request_status(client, base, handle)):
        state = str(entry.get("state", ""))
        online = _flag(entry.get("onDisk")) or state == "COMPLETED"
        path = str(entry.get("path", ""))
        found[path] = PrepareStatus(
            path=path,
            exists=True,  # the request named it, and the server took the request
            # Not online yet and not given up on means the bytes are still
            # where staging fetches them from, which is the tape.
            on_tape=not online and state not in _GIVEN_UP,
            online=online,
            requested=True,
            has_request_id=True,
            requested_at=str(entry.get("startedAt", "") or ""),
            error=str(entry.get("error", "") or ""),
            state=state,
        )
    return _ordered(found, paths)


def request_status(client: HTTPClient, base: XRootDURL, handle: str) -> Any:
    """The staging request ``handle``'s status document, as the site wrote it.

    The specification's shape is ``{"id", "createdAt", "startedAt",
    "completedAt", "files": [...]}``; :func:`status` reads the files out of it.
    """
    target = _at(base, "stage", handle)
    return _document(client.request("GET", target, expect=(200,)).body, target)


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
    body = json.dumps({"paths": list(paths)}).encode()
    client.request("POST", target, body=body, headers=_JSON, expect=(200, 202, 204))


def discover(client: HTTPClient, base: XRootDURL) -> dict[str, str]:
    """The tape endpoint the site advertises: ``{"uri", "version", "sitename"}``.

    The well-known document lists every endpoint the site runs; this picks
    the one speaking :data:`VERSION`, or the first when none says so.
    """
    target = base.with_path(WELL_KNOWN)
    document = _document(client.request("GET", target, expect=(200,)).body, target)
    endpoints = document.get("endpoints") if isinstance(document, dict) else None
    if not endpoints:
        raise ProtocolError(f"{target} lists no tape endpoints")
    chosen = next((e for e in endpoints if e.get("version") == VERSION), endpoints[0])
    return {
        "uri": str(chosen.get("uri", "")),
        "version": str(chosen.get("version", "")),
        "sitename": str(document.get("sitename", "")),
    }


def archive_info(
    client: HTTPClient, base: XRootDURL, paths: Sequence[str]
) -> list[PrepareStatus]:
    """Where each of these files lives, without asking for any of it to move."""
    found = {}
    for entry in archive_entries(client, base, paths):
        path = str(entry.get("path", ""))
        found[path] = _locality(path, str(entry.get("locality", "")), entry)
    return _ordered(found, paths)


def archive_entries(client: HTTPClient, base: XRootDURL, paths: Sequence[str]) -> list[Any]:
    """The ``archiveinfo`` reply's entries, as the site wrote them."""
    target = _at(base, "archiveinfo")
    res = client.request(
        "POST", target, body=json.dumps({"paths": list(paths)}).encode(), headers=_JSON,
        expect=(200,),
    )
    return _entries(_document(res.body, target))


def _locality(path: str, word: str, entry: Any) -> PrepareStatus:
    """One ``archiveinfo`` entry, read from the locality word it answers with.

    Deployments differ over the vocabulary - ``DISK``/``TAPE`` in the
    specification, ``ONLINE``/``NEARLINE`` in the storage systems it describes
    - and both spell the third case as a compound, so this reads the two
    halves rather than matching whole words. Anything that names neither is a
    file the site cannot give you: lost, unavailable, or not there at all.
    """
    upper = word.upper()
    online = "DISK" in upper or "ONLINE" in upper
    on_tape = "TAPE" in upper or "NEARLINE" in upper
    return PrepareStatus(
        path=path,
        exists=online or on_tape,
        on_tape=on_tape,
        online=online,
        error=str(entry.get("error", "") or "") or ("" if online or on_tape else word.lower()),
        state=upper,
    )
