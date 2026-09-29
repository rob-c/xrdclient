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
"""

from __future__ import annotations

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

_STRUCTURED_STAGE_PREFIX = "xrdclhttp.tape.stage:"

#: The schemes whose URLs name a server rather than a file on this machine.
_NATIVE = ("root", "roots", "xroot", "xroots")
_ENDPOINTS = (*_NATIVE, "http", "https")
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
        _args.u16(priority, "priority")
        entries = [str(entry) for entry in files]
        if isinstance(native, HTTPFileSystem):
            operation: Callable[[], bytes] = lambda: _tape_prepare(native, entries, wanted)  # noqa: E731
        elif wanted & PrepareFlags.CANCEL:
            # The request id leads the list: native.cancel_prepare sends it as
            # the handle it is, where a plain prepare would take it for a path.
            operation = lambda: _cancel_prepare(native, entries)  # noqa: E731
        else:
            # XrdCl sends a full URL in the list as it is, and the server
            # reads its path; the native request wants the path to begin with.
            paths = [_url_path(entry) for entry in entries]
            return super().prepare(paths, flags, priority, timeout, callback)
        return call(operation, _same, timeout=_args.u16(timeout), callback=callback)


def _same(value: bytes) -> bytes:
    return value


def _encoded(document: Any) -> bytes:
    return json.dumps(document).encode()


def _cancel_prepare(native: Any, entries: list[str]) -> bytes:
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
    return _encoded(rest.discover(native.client, native.url))


def _archive_info(native: HTTPFileSystem, lines: list[str]) -> bytes:
    paths = [_path(native, line) for line in lines]
    return _encoded(rest.archive_entries(native.client, native.url, paths))


def _stage_delete(native: HTTPFileSystem, lines: list[str]) -> bytes:
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
    if isinstance(response, bytes):
        response = response.decode("utf-8")
    return response.rstrip("\0")


def _reject_line_breaks(value: str, name: str) -> None:
    if "\n" in value or "\r" in value:
        raise ValueError(f"{name} must not contain line breaks")


def _normalize_disk_lifetime(value: object) -> str | None:
    """Seconds as ``PT<n>S``, an ISO 8601 duration as it is, ``None`` as ``None``."""
    if value is None:
        return None
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
    parsed = urlparse(value)
    return bool(parsed.scheme and parsed.netloc)


class TapeClient:
    """Synchronous client for the WLCG Tape REST API.

    :param timeout: seconds each operation may take; zero is the configured default
    """

    def __init__(self, timeout: int = 0) -> None:
        self.timeout = timeout

    def _filesystem_url(self, url: str) -> str:
        parsed = urlparse(url)
        scheme = {"davs": "https", "dav": "http"}.get(parsed.scheme.lower(), parsed.scheme.lower())
        if scheme in _ENDPOINTS and parsed.netloc:
            return f"{scheme}://{parsed.netloc}"
        return url

    def _operation_url(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in _OPERATIONS or not parsed.netloc:
            return url
        # With a netloc, urlparse's path is empty or starts with "/".
        return self._filesystem_url(url) + (parsed.path or "/")

    def _filesystem(self, url: str) -> Any:
        return FileSystem(self._filesystem_url(url))

    def _stage_entry(
        self, entry: str, disk_lifetime: object = None, targeted_metadata: object = None
    ) -> str:
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
        if not files:
            return ""
        first = files[0]
        if isinstance(first, str):
            return first
        return str(first.get("url", ""))

    def _prepare_paths(
        self, url: str, request_id: str, paths: str | Iterable[str], flags: int
    ) -> XRootDStatus:
        _reject_line_breaks(request_id, "request_id")
        if isinstance(paths, str):
            paths = [paths]
        files = [request_id]
        files.extend(self._operation_url(path) if _is_url(path) else path for path in paths)
        status, _ = self._filesystem(url).prepare(files, flags, timeout=self.timeout)
        return status  # type: ignore[no-any-return]

    def discover(self, url: str) -> tuple[XRootDStatus, Any]:
        """``(status, TapeEndpoint)``: the Tape REST API endpoint serving ``url``."""
        status, endpoint = self._filesystem(url).query(
            QueryCode.OPAQUE, "tape.discover", self.timeout
        )
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
        status, _ = self._filesystem(url).query(
            QueryCode.OPAQUE, f"tape.stage_delete\n{request_id}", self.timeout
        )
        return status  # type: ignore[no-any-return]

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
        for original, result in zip(urls, results):
            result["url"] = original
        return status, [TapeArchiveInfo(r) for r in results]
