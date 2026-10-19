"""``XRootD.client.tape``: staging from tape the way the bindings' ``TapeClient`` does.

The bindings drive the WLCG Tape REST API through XrdCl's HTTP plug-in: every
:class:`TapeClient` call is a :meth:`FileSystem.prepare` or
:meth:`FileSystem.query` whose arguments the plug-in recognises - an opaque
``tape.discover`` query, a stage entry spelt ``xrdclhttp.tape.stage:{json}``
- and turns into a request to the API. :class:`TapeClient` here is the same
code, making the same calls. The :class:`FileSystem` it makes them on is the
compat one, taught the plug-in's side over ``http(s)://`` and ``dav(s)://``
using :mod:`xrdclient.http.tape`. A ``root://`` endpoint gets the plain
compat filesystem: its ``kXR_prepare`` and ``kXR_query``, and whatever tape
support the server has, exactly as with the bindings.

Two halves, top and bottom: :class:`FileSystem` and the ``_tape_*`` helpers
are the plug-in's side (decode the XrdCl-encoded request, call
:mod:`xrdclient.http.tape`); :class:`TapeClient` is the bindings' side
(encode the request XrdCl's way and send it through a ``FileSystem``).
Keeping that split means a ``TapeClient`` call takes the same path as code
that calls ``FileSystem.query``/``prepare`` by hand, so both stay in step.
Every value that ends up in a newline-separated query is checked for line
breaks first: one would let a caller splice extra lines into the request.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from ...http import tape as rest
from ...http.dav import HTTPFileSystem
from . import _args
from ._dispatch import call
from .filesystem import Callback
from .filesystem import FileSystem as _FileSystem
from .flags import PrepareFlags, QueryCode
from .responses import (
    TapeArchiveInfo,
    TapeEndpoint,
    TapeStageResponse,
    TapeStageStatus,
    XRootDStatus,
)

__all__ = ["FileSystem", "TapeClient"]

#: How XrdCl's HTTP plug-in marks a stage entry that carries options
#: (``diskLifetime``, ``targetedMetadata``): this prefix, then a JSON object.
_STRUCTURED_STAGE_PREFIX = "xrdclhttp.tape.stage:"

#: The schemes whose URLs name a server rather than a file on this machine.
_NATIVE = ("root", "roots", "xroot", "xroots")
#: The schemes a ``FileSystem`` is opened on; ``dav(s)`` is mapped to these.
_ENDPOINTS = (*_NATIVE, "http", "https")
#: The schemes accepted in a file URL, rewritten to the endpoint's spelling.
_OPERATIONS = (*_ENDPOINTS, "dav", "davs")


class FileSystem(_FileSystem):
    """The compat :class:`~.filesystem.FileSystem`, answering tape requests over HTTP.

    On an ``http(s)`` or ``dav(s)`` endpoint, the queries and prepares that
    XrdCl's HTTP plug-in reads as Tape REST API calls become those calls;
    everything else - and everything on ``root://`` - is the plain method.
    """

    def query(self, querycode: int, arg: str, timeout: float = 0, callback: Callback = None) -> Any:
        """``(status, bytes)``; a tape query over HTTP answers with the API's JSON."""
        native = self.native
        # Only an HTTP filesystem has the plug-in's side; a query that is not
        # a tape verb falls through to the ordinary one even there.
        if isinstance(native, HTTPFileSystem):
            operation = _tape_query(native, int(querycode), str(arg))
            if operation is not None:
                return call(operation, _same, timeout=_args.u16(timeout), callback=callback)
        return super().query(querycode, arg, timeout, callback)

    def prepare(
        self,
        files: Sequence[str],
        flags: int,
        priority: int = 0,
        timeout: float = 0,
        callback: Callback = None,
    ) -> Any:
        """``(status, bytes)``; over HTTP, stage, cancel or release through the API."""
        native = self.native
        wanted = _args.u16(flags, "flags")
        # Checked for the bindings' ``OverflowError`` even though the Tape
        # REST API has no priority to send it as.
        _args.u16(priority, "priority")
        entries = [str(entry) for entry in files]
        if isinstance(native, HTTPFileSystem):
            operation: Callable[[], bytes] = functools.partial(
                _tape_prepare, native, entries, wanted
            )
        elif wanted & PrepareFlags.CANCEL:
            # The request id leads the list: native.cancel_prepare sends it as
            # the handle it is, where a plain prepare would take it for a path.
            operation = functools.partial(_cancel_prepare, native, entries)
        else:
            # XrdCl sends a full URL in the list as it is, and the server
            # reads its path; the native request wants the path to begin with.
            paths = [_url_path(entry) for entry in entries]
            return super().prepare(paths, flags, priority, timeout, callback)
        return call(operation, _same, timeout=_args.u16(timeout), callback=callback)


def _same(value: bytes) -> bytes:
    """The ``convert`` for an answer that already is the bindings' response."""
    return value


def _encoded(document: Any) -> bytes:
    """A JSON answer as the bytes the plug-in's query returns."""
    return json.dumps(document).encode()


def _cancel_prepare(native: Any, entries: list[str]) -> bytes:
    """Cancel on ``root://``: the first entry is the request id, the rest ignored."""
    native.cancel_prepare(entries[0] if entries else "")
    return b""


def _url_path(entry: str) -> str:
    """``entry``'s path if it is a full URL (``//`` collapsed), else ``entry``."""
    parsed = urlparse(entry)
    if not (parsed.scheme and parsed.netloc):
        return entry
    path = parsed.path or "/"
    return "/" + path.lstrip("/")


def _path(native: HTTPFileSystem, entry: str) -> str:
    """The path the API is told about for ``entry``, a path or a full URL."""
    return native._abs(_url_path(entry))


def _discover(native: HTTPFileSystem, lines: list[str]) -> bytes:
    """``tape.discover``: the endpoint document from ``/.well-known/wlcg-tape-rest-api``."""
    return _encoded(rest.discover(native.client, native.url))


def _archive_info(native: HTTPFileSystem, lines: list[str]) -> bytes:
    """``tape.archiveinfo``: one URL or path per line, answered with the API's JSON list."""
    paths = [_path(native, line) for line in lines]
    return _encoded(rest.archive_entries(native.client, native.url, paths))


def _stage_delete(native: HTTPFileSystem, lines: list[str]) -> bytes:
    """``tape.stage_delete``: drop the whole request named on the first line."""
    rest.cancel(native.client, native.url, "".join(lines[:1]))
    return b""


#: The opaque queries XrdCl's HTTP plug-in reads as tape calls, by first line.
_OPAQUE: dict[str, Callable[[HTTPFileSystem, list[str]], bytes]] = {
    "tape.discover": _discover,
    "tape.archiveinfo": _archive_info,
    "tape.stage_delete": _stage_delete,
}


def _tape_query(native: HTTPFileSystem, code: int, arg: str) -> Callable[[], bytes] | None:
    """The tape call ``query(code, arg)`` stands for, or ``None`` if it is not one."""
    first, _, tail = arg.partition("\n")
    # A ``PREPARE`` query is the stage status of the request id it names;
    # an ``OPAQUE`` one is a tape call if its first line is one of the verbs.
    if code == QueryCode.PREPARE:
        return lambda: _encoded(rest.request_status(native.client, native.url, first))
    handler = _OPAQUE.get(first) if code == QueryCode.OPAQUE else None
    if handler is None:
        return None
    lines = tail.split("\n") if tail else []
    return lambda: handler(native, lines)


def _tape_prepare(native: HTTPFileSystem, files: list[str], flags: int) -> bytes:
    """Stage ``files``, or cancel or release the paths after a request id."""
    if flags & (PrepareFlags.CANCEL | PrepareFlags.EVICT):
        # XrdCl's convention for both: the request id first, then the files.
        handle, *paths = files or [""]
        act = rest.cancel_files if flags & PrepareFlags.CANCEL else rest.release
        act(native.client, native.url, handle, [_path(native, p) for p in paths])
        return b""
    entries = [_stage_spec(native, entry) for entry in files]
    return rest.stage_files(native.client, native.url, entries).encode()


def _stage_spec(native: HTTPFileSystem, entry: str) -> dict[str, Any]:
    """One stage entry, plain or ``xrdclhttp.tape.stage:{json}``, as the API spells it."""
    if not entry.startswith(_STRUCTURED_STAGE_PREFIX):
        return {"path": _path(native, entry)}
    fields = json.loads(entry[len(_STRUCTURED_STAGE_PREFIX) :])
    spec: dict[str, Any] = {"path": _path(native, fields.get("url") or fields.get("path", ""))}
    for key in ("diskLifetime", "targetedMetadata"):
        if key in fields:
            spec[key] = fields[key]
    return spec


def _response_text(response: str | bytes) -> str:
    """An answer as text; ``kXR_prepare`` answers carry a trailing NUL, which goes."""
    if isinstance(response, bytes):
        response = response.decode("utf-8")
    return response.rstrip("\0")


def _reject_line_breaks(value: str, name: str) -> None:
    """``ValueError`` if ``value`` would split into more than one line of a request.

    Requests to the plug-in are newline-separated lists, so a request id or
    URL holding ``\n`` would smuggle in another entry - a cancel aimed at a
    file the caller never named.
    """
    if "\n" in value or "\r" in value:
        raise ValueError(f"{name} must not contain line breaks")


def _normalize_disk_lifetime(value: object) -> str | None:
    """Seconds as ``PT<n>S``, an ISO 8601 duration as it is, ``None`` as ``None``.

    The duration string is passed on unparsed: the site validates it, and
    what it accepts is the site's to decide.
    """
    if value is None:
        return None
    # ``bool`` is an ``int`` subclass; ``True`` seconds is a caller's mistake.
    if isinstance(value, bool):
        raise ValueError("diskLifetime must be an ISO-8601 duration or seconds")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("diskLifetime seconds must not be negative")
        return f"PT{value}S"
    if not isinstance(value, str) or not value:
        raise ValueError("diskLifetime must be an ISO-8601 duration or seconds")
    return value


def _normalize_targeted_metadata(targeted_metadata: object) -> dict[str, Any] | None:
    """Targeted metadata as a dict: given as one, or as its JSON text; else ``ValueError``."""
    if targeted_metadata is None:
        return None
    if isinstance(targeted_metadata, str):
        targeted_metadata = json.loads(targeted_metadata)
    if not isinstance(targeted_metadata, dict):
        raise ValueError("targetedMetadata must be a JSON object")
    return targeted_metadata


def _url_list(urls: str | Iterable[str]) -> list[str]:
    """``urls`` as a non-empty list of single-line URLs, or ``ValueError``."""
    urls = [urls] if isinstance(urls, str) else list(urls)
    if not urls:
        raise ValueError("urls must not be empty")
    for url in urls:
        _reject_line_breaks(url, "url")
    return urls


def _is_url(value: str) -> bool:
    """Whether ``value`` is a full URL (scheme and host) rather than a bare path."""
    parsed = urlparse(value)
    return bool(parsed.scheme and parsed.netloc)


class TapeClient:
    """Synchronous client for the WLCG Tape REST API.

    :param timeout: seconds each operation may take; zero is the configured default
    """

    def __init__(self, timeout: int = 0) -> None:
        """Remember ``timeout``; every call makes its own :class:`FileSystem`."""
        self.timeout = timeout

    def _filesystem_url(self, url: str) -> str:
        """The endpoint a file URL is served from: scheme and host, ``dav(s)`` as ``http(s)``.

        Anything that is not a server URL is returned untouched, so the
        :class:`FileSystem` built on it reports it invalid the bindings' way.
        """
        parsed = urlparse(url)
        scheme = {"davs": "https", "dav": "http"}.get(parsed.scheme.lower(), parsed.scheme.lower())
        if scheme in _ENDPOINTS and parsed.netloc:
            return f"{scheme}://{parsed.netloc}"
        return url

    def _operation_url(self, url: str) -> str:
        """A file URL rewritten onto its endpoint's scheme, path kept."""
        parsed = urlparse(url)
        if parsed.scheme.lower() not in _OPERATIONS or not parsed.netloc:
            return url
        # With a netloc, urlparse's path is empty or starts with "/".
        return self._filesystem_url(url) + (parsed.path or "/")

    def _filesystem(self, url: str) -> Any:
        """A tape-aware :class:`FileSystem` for ``url``'s endpoint."""
        return FileSystem(self._filesystem_url(url))

    def _stage_entry(
        self, entry: str, disk_lifetime: object = None, targeted_metadata: object = None
    ) -> str:
        """A structured stage entry: the prefix and sorted JSON, the plug-in's format.

        Sorted keys make the same request produce the same bytes every time.
        """
        payload: dict[str, Any] = {}
        if _is_url(entry):
            payload["url"] = self._operation_url(entry)
        else:
            payload["path"] = entry
        if disk_lifetime is not None:
            payload["diskLifetime"] = _normalize_disk_lifetime(disk_lifetime)
        if targeted_metadata is not None:
            payload["targetedMetadata"] = targeted_metadata
        return _STRUCTURED_STAGE_PREFIX + json.dumps(payload, sort_keys=True)

    def _file_entry(
        self, item: str | Mapping[str, Any], disk_lifetime: object, targeted_metadata: object
    ) -> tuple[str, object, dict[str, Any] | None]:
        """One file of a stage call: its name, lifetime and metadata, defaults applied."""
        if isinstance(item, str):
            return item, disk_lifetime, _normalize_targeted_metadata(targeted_metadata)
        entry = item.get("path", "") or item.get("url", "")
        if not entry:
            raise ValueError("stage file entries must contain path or url")
        lifetime = item.get("diskLifetime", item.get("disk_lifetime", disk_lifetime))
        metadata = item.get("targetedMetadata", item.get("targeted_metadata", targeted_metadata))
        return entry, lifetime, _normalize_targeted_metadata(metadata)

    def _normalize_stage_files(
        self,
        files: Iterable[str | Mapping[str, Any]],
        disk_lifetime: object = None,
        targeted_metadata: object = None,
    ) -> list[str]:
        """The prepare list for a stage call: one line per file.

        A file with no options goes as its plain URL or path; one with a
        lifetime or metadata goes as a structured entry, the only form that
        can carry them.
        """
        normalized = []
        targeted_metadata = _normalize_targeted_metadata(targeted_metadata)
        for item in files:
            entry, lifetime, metadata = self._file_entry(item, disk_lifetime, targeted_metadata)
            _reject_line_breaks(entry, "stage file")
            if lifetime is None and metadata is None:
                normalized.append(self._operation_url(entry) if _is_url(entry) else entry)
            else:
                normalized.append(self._stage_entry(entry, lifetime, metadata))
        return normalized

    def _derive_url(self, files: Sequence[str | Mapping[str, Any]]) -> str:
        """The endpoint URL implied by a file list: its first entry's URL, or ``""``."""
        if not files:
            return ""
        first = files[0]
        if isinstance(first, str):
            return first
        return str(first.get("url", ""))

    def _prepare_paths(
        self, url: str, request_id: str, paths: str | Iterable[str], flags: int
    ) -> XRootDStatus:
        """A cancel or release: ``prepare`` with the request id leading the list."""
        _reject_line_breaks(request_id, "request_id")
        if isinstance(paths, str):
            paths = [paths]
        files = [request_id]
        files.extend(self._operation_url(path) if _is_url(path) else path for path in paths)
        status: XRootDStatus
        status, _ = self._filesystem(url).prepare(files, flags, timeout=self.timeout)
        return status

    def discover(self, url: str) -> tuple[XRootDStatus, Any]:
        """``(status, TapeEndpoint)``: the Tape REST API endpoint serving ``url``."""
        status, endpoint = self._filesystem(url).query(
            QueryCode.OPAQUE, "tape.discover", self.timeout
        )
        # Two checks, because an empty JSON document (``{}``) is no endpoint
        # either; both leave the bindings' falsy response in place.
        if endpoint:
            endpoint = json.loads(_response_text(endpoint))
        if endpoint:
            endpoint = TapeEndpoint(endpoint)
        return status, endpoint

    def stage(
        self,
        url: Any,
        files: Any = None,
        disk_lifetime: object = None,
        targeted_metadata: object = None,
    ) -> tuple[XRootDStatus, Any]:
        """``(status, TapeStageResponse)``: ask for ``files`` to be brought onto disk.

        ``files`` are URLs, paths, or dicts with ``url`` or ``path`` and
        optionally ``diskLifetime`` and ``targeted_metadata``; given no
        ``files``, ``url`` is that list and names the endpoint by its first URL.
        """
        if files is None:
            if isinstance(url, str):
                raise ValueError("files must be provided when url is a string")
            files = list(url)
            url = self._derive_url(files)
            if not _is_url(url):
                raise ValueError("url must be provided when file entries do not contain URLs")
        elif isinstance(files, (str, dict)):
            files = [files]
        else:
            files = list(files)
        status, response = self._filesystem(url).prepare(
            self._normalize_stage_files(files, disk_lifetime, targeted_metadata),
            PrepareFlags.STAGE,
            timeout=self.timeout,
        )
        if response:
            response = TapeStageResponse({"requestId": _response_text(response)})
        return status, response

    def stage_status(self, url: str, request_id: str) -> tuple[XRootDStatus, Any]:
        """``(status, TapeStageStatus)`` for a stage request made earlier."""
        _reject_line_breaks(request_id, "request_id")
        status, response = self._filesystem(url).query(QueryCode.PREPARE, request_id, self.timeout)
        if response:
            response = TapeStageStatus(json.loads(_response_text(response)))
        return status, response

    def stage_cancel(self, url: str, request_id: str, paths: str | Iterable[str]) -> XRootDStatus:
        """Withdraw ``paths`` from stage request ``request_id``."""
        return self._prepare_paths(url, request_id, paths, PrepareFlags.CANCEL)

    def stage_delete(self, url: str, request_id: str) -> XRootDStatus:
        """Withdraw stage request ``request_id`` altogether."""
        _reject_line_breaks(request_id, "request_id")
        status: XRootDStatus
        status, _ = self._filesystem(url).query(
            QueryCode.OPAQUE, f"tape.stage_delete\n{request_id}", self.timeout
        )
        return status

    def release(self, url: str, request_id: str, paths: str | Iterable[str]) -> XRootDStatus:
        """Say ``paths`` of request ``request_id`` no longer need to stay on disk."""
        return self._prepare_paths(url, request_id, paths, PrepareFlags.EVICT)

    def archive_info(self, urls: str | Iterable[str]) -> tuple[XRootDStatus, list[TapeArchiveInfo]]:
        """``(status, [TapeArchiveInfo])``: where each of ``urls`` lives."""
        urls = _url_list(urls)
        operation_urls = [self._operation_url(url) for url in urls]
        status, results = self._filesystem(urls[0]).query(
            QueryCode.OPAQUE, "tape.archiveinfo\n{}".format("\n".join(operation_urls)), self.timeout
        )
        results = json.loads(_response_text(results)) if results else []
        # The site answers with paths; callers look results up by the URL
        # they asked about. ``strict=False`` because a failed query answers
        # with fewer entries (none) than were asked about.
        for original, result in zip(urls, results, strict=False):
            result["url"] = original
        return status, [TapeArchiveInfo(r) for r in results]
