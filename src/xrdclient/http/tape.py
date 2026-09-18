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
from collections.abc import Sequence
from typing import Any

from ..errors import ProtocolError
from ..types import PrepareStatus
from ..url import XRootDURL
from .client import HTTPClient

__all__ = ["stage", "status", "cancel", "archive_info"]

#: Where the API is rooted. Fixed by the WLCG specification, not by the site.
API = "/api/v1"

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
    target = _at(base, "stage")
    res = client.request(
        "POST", target, body=json.dumps({"files": files}).encode(), headers=_JSON,
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
    target = _at(base, "stage", handle)
    res = client.request("GET", target, expect=(200,))
    found = {}
    for entry in _entries(_document(res.body, target)):
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


def cancel(client: HTTPClient, base: XRootDURL, handle: str) -> None:
    """Withdraw a staging request, files and all."""
    client.request("DELETE", _at(base, "stage", handle), expect=(200, 202, 204))


def archive_info(
    client: HTTPClient, base: XRootDURL, paths: Sequence[str]
) -> list[PrepareStatus]:
    """Where each of these files lives, without asking for any of it to move."""
    target = _at(base, "archiveinfo")
    res = client.request(
        "POST", target, body=json.dumps({"paths": list(paths)}).encode(), headers=_JSON,
        expect=(200,),
    )
    found = {}
    for entry in _entries(_document(res.body, target)):
        path = str(entry.get("path", ""))
        found[path] = _locality(path, str(entry.get("locality", "")), entry)
    return _ordered(found, paths)


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
