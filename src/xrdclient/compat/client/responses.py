"""The response objects of ``XRootD.client.responses``.

Every one is a :class:`Struct` - a bag of attributes built from a ``dict`` -
because that is what the bindings hand back, and code written against them
reaches for ``info.size`` and ``vars(info)`` and ``repr(info)`` alike. The
attribute names, their types and the ``repr`` are the bindings' own, checked
against them on a live server; see ``tests/test_pyxrootd_compat.py``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, ClassVar
from urllib.parse import urlparse

from .url import URL

__all__ = [
    "ChunkInfo",
    "DirectoryList",
    "HostInfo",
    "HostList",
    "ListEntry",
    "Location",
    "LocationInfo",
    "ProtocolInfo",
    "StatInfo",
    "StatInfoVFS",
    "Struct",
    "TapeArchiveInfo",
    "TapeEndpoint",
    "TapeStageFileStatus",
    "TapeStageResponse",
    "TapeStageStatus",
    "VectorReadInfo",
    "XRootDAuthorizationError",
    "XRootDChecksumError",
    "XRootDError",
    "XRootDNotFoundError",
    "XRootDOperationError",
    "XRootDStatus",
    "XRootDTimeoutError",
    "raise_on_error",
]


class XRootDError(RuntimeError):
    """What an unsuccessful :class:`XRootDStatus` raises; ``.status`` is that status."""

    def __init__(self, status: XRootDStatus) -> None:
        self.status = status
        RuntimeError.__init__(self, str(status))


class XRootDNotFoundError(XRootDError):
    """The requested file or resource was not found."""


class XRootDAuthorizationError(XRootDError):
    """Authentication or authorization failed."""


class XRootDTimeoutError(XRootDError):
    """The request timed out or expired."""


class XRootDChecksumError(XRootDError):
    """The request failed checksum validation."""


class XRootDOperationError(XRootDError):
    """Any other unsuccessful operation."""


class Struct:
    """A ``dict`` turned into attributes, printed the way the bindings print it."""

    def __init__(self, entries: dict[str, Any]) -> None:
        self.__dict__.update(entries)

    def __repr__(self) -> str:
        fields = ", ".join(f"{key}: {value!r}" for key, value in self.__dict__.items())
        return f"<{fields}>"

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self.__dict__ == other.__dict__

    __hash__ = None  # type: ignore[assignment]  # mutable, like the bindings'


class XRootDStatus(Struct):
    """The outcome of a request: ``ok``, ``error``, ``fatal``, ``code``, ``errno``.

    ``code`` is XrdCl's error type (400 for "the server said no") and
    ``errno`` the server's own ``kXR_*`` number, so ported code that compares
    either keeps working. Indexing works as well as attribute access,
    because the bindings hand some statuses out as plain dicts - the ones
    inside an xattr result, and :meth:`FileSystem.cat`'s - and code written
    against those says ``status["ok"]``.
    """

    status: int
    code: int
    errno: int
    message: str
    shellcode: int
    error: bool
    fatal: bool
    ok: bool

    # XrdCl::XRootDStatus's ``code`` values, by the bindings' names. Class
    # attributes, so ``vars(status)`` and the ``repr`` stay the six fields.
    suDone = 0
    suContinue = 1
    suRetry = 2
    suPartial = 3
    suAlreadyDone = 4
    suNotStarted = 5

    errNone = 0
    errRetry = 1
    errUnknown = 2
    errInvalidOp = 3
    errFcntl = 4
    errPoll = 5
    errConfig = 6
    errInternal = 7
    errUnknownCommand = 8
    errInvalidArgs = 9
    errInProgress = 10
    errUninitialized = 11
    errOSError = 12
    errNotSupported = 13
    errDataError = 14
    errNotImplemented = 15
    errNoMoreReplicas = 16
    errPipelineError = 17  # the older spelling of errPipelineFailed
    errPipelineFailed = 17

    errInvalidAddr = 101
    errSocketError = 102
    errSocketTimeout = 103
    errSocketDisconnected = 104
    errPollerError = 105
    errSocketOptError = 106
    errStreamDisconnect = 107
    errConnectionError = 108
    errInvalidSession = 109
    errTlsError = 110

    errInvalidMessage = 201
    errHandShakeFailed = 202
    errLoginFailed = 203
    errAuthFailed = 204
    errQueryNotSupported = 205
    errOperationExpired = 206
    errOperationInterrupted = 207
    errThresholdExceeded = 208

    errNoMoreFreeSIDs = 301
    errInvalidRedirectURL = 302
    errInvalidResponse = 303
    errNotFound = 304
    errCheckSumError = 305
    errRedirectLimit = 306
    errCorruptedHeader = 307

    errErrorResponse = 400
    errRedirect = 401
    errLocalError = 402

    errResponseNegative = 500

    #: ``code`` to its name; filled in below the class, from the names above.
    _ERROR_NAMES: ClassVar[dict[int, str]] = {}

    def __str__(self) -> str:
        return self.message

    def __getitem__(self, key: str) -> Any:
        return self.__dict__[key]

    def __contains__(self, key: object) -> bool:
        return key in self.__dict__

    @property
    def error_name(self) -> str | None:
        """The symbolic name of ``code`` (``"errNotFound"``), or ``None`` for one XrdCl lacks."""
        return self._ERROR_NAMES.get(getattr(self, "code", None))  # type: ignore[arg-type]

    def exception(self) -> XRootDError | None:
        """The exception this status stands for, or ``None`` if it is OK.

        Chosen by ``code`` and, when the server said no (``errErrorResponse``),
        by the server's own ``errno`` - the bindings' rules exactly.
        """
        if self.ok:
            return None
        return _exception_type(getattr(self, "code", None), getattr(self, "errno", None))(self)

    def raise_on_error(self) -> XRootDStatus:
        """Raise :meth:`exception` if there is one; otherwise return this status."""
        error = self.exception()
        if error:
            raise error
        return self


class ProtocolInfo(Struct):
    """``version`` and ``hostinfo``, both as the server sent them."""

    version: int
    hostinfo: int


class StatInfo(Struct):
    """What ``kXR_stat`` said about one path, in the bindings' vocabulary.

    ``mode`` is the server's octal string (``"0644"``) and ``modeoctstr`` its
    ``rw-r--r--`` rendering - the names are the bindings', not a mix-up here.
    Both are empty, as are ``owner`` and ``group``, against a server too old
    to send them, and ``extended`` says which case this is.
    """

    id: str
    size: int
    flags: int
    mtime: int
    modtime: int
    modtimestr: str
    ctime: int
    atime: int
    mode: str
    modeoctstr: str
    owner: str
    group: str
    extended: bool
    haschecksum: bool
    checksum: str


class StatInfoVFS(Struct):
    """``kXR_stat`` with ``kXR_vfs``: space across the servers behind a path."""

    nodes_rw: int
    free_rw: int
    utilization_rw: int
    nodes_staging: int
    free_staging: int
    utilization_staging: int


class ListEntry(Struct):
    """One name in a :class:`DirectoryList`; ``statinfo`` is ``None`` without ``STAT``."""

    name: str
    hostaddr: str
    statinfo: StatInfo | None


class DirectoryList(Struct):
    """A listing: ``size`` entries under ``parent``, iterable as :class:`ListEntry`."""

    size: int
    parent: str
    dirlist: list[ListEntry]

    def __iter__(self) -> Iterator[ListEntry]:
        return iter(self.dirlist)


class Location(Struct):
    """One server that has a file: ``address``, ``type``, ``accesstype``."""

    address: str
    type: int
    accesstype: int
    is_manager: bool
    is_server: bool


class LocationInfo(Struct):
    """Every :class:`Location` a locate found; iterable."""

    locations: list[Location]

    def __iter__(self) -> Iterator[Location]:
        return iter(self.locations)


class ChunkInfo(Struct):
    """One piece of a vector read: ``offset``, ``length`` and its ``buffer``."""

    offset: int
    length: int
    buffer: bytes


class VectorReadInfo(Struct):
    """The answer to :meth:`File.vector_read`; iterable as :class:`ChunkInfo`."""

    size: int
    chunks: list[ChunkInfo]

    def __iter__(self) -> Iterator[ChunkInfo]:
        return iter(self.chunks)


class HostInfo(Struct):
    """One server a request passed through on its way to an answer."""

    url: URL
    protocol: int
    flags: int
    load_balancer: bool


class HostList(Struct):
    """The servers a request visited, handed to a callback as its third argument."""

    hosts: list[HostInfo]

    def __iter__(self) -> Iterator[HostInfo]:
        return iter(self.hosts)


XRootDStatus._ERROR_NAMES = {
    value: name
    for name, value in vars(XRootDStatus).items()
    if name.startswith("err") and isinstance(value, int)
}

# XRootD protocol error numbers an errErrorResponse status can carry.
_kXR_NotAuthorized = 3010
_kXR_NotFound = 3011
_kXR_ChkSumErr = 3019
_kXR_AuthFailed = 3030
_kXR_ReqTimedOut = 3034
_kXR_TimerExpired = 3035

#: Most specific first: (exception, XrdCl codes, server errnos) - the bindings' order.
_EXCEPTIONS: tuple[tuple[type[XRootDError], tuple[int, ...], tuple[int, ...]], ...] = (
    (XRootDNotFoundError, (XRootDStatus.errNotFound,), (_kXR_NotFound,)),
    (
        XRootDAuthorizationError,
        (XRootDStatus.errAuthFailed, XRootDStatus.errLoginFailed),
        (_kXR_NotAuthorized, _kXR_AuthFailed),
    ),
    (
        XRootDTimeoutError,
        (XRootDStatus.errSocketTimeout, XRootDStatus.errOperationExpired),
        (_kXR_ReqTimedOut, _kXR_TimerExpired),
    ),
    (XRootDChecksumError, (XRootDStatus.errCheckSumError,), (_kXR_ChkSumErr,)),
)


def _exception_type(code: object, errno: object) -> type[XRootDError]:
    server = code == XRootDStatus.errErrorResponse
    for kind, codes, errnos in _EXCEPTIONS:
        if code in codes or (server and errno in errnos):
            return kind
    return XRootDOperationError


def raise_on_error(status: XRootDStatus | Mapping[str, Any]) -> XRootDStatus:
    """Raise the mapped exception if ``status`` is not OK; return it as an :class:`XRootDStatus`.

    ``status`` is a status or the raw ``dict`` some bindings calls hand out.
    """
    if not isinstance(status, XRootDStatus):
        status = XRootDStatus(dict(status))
    return status.raise_on_error()


# -- the WLCG Tape REST API ----------------------------------------------------


class TapeEndpoint(Struct):
    """The Tape REST API endpoint discovery chose: ``uri``, ``version``, ``sitename``."""

    uri: str
    version: str
    sitename: str


class TapeArchiveInfo(Struct):
    """Where one file lives: the ``url`` asked about, its ``locality``, or an ``error``."""

    url: str
    path: str
    locality: str | None
    error: str | None

    def __init__(self, info: dict[str, Any]) -> None:
        payload: dict[str, Any] = {"locality": None, "error": None}
        payload.update(info)
        super().__init__(payload)


class TapeStageResponse(Struct):
    """The answer to a stage request: its ``requestId``."""

    requestId: str

    @property
    def request_id(self) -> str:
        """``requestId``, spelt the Python way."""
        return self.requestId


class TapeStageFileStatus(Struct):
    """One file of a stage request: ``path``, ``state``, and ``onDisk`` if the site said."""

    path: str

    @property
    def on_disk(self) -> Any:
        """``onDisk`` if the site reported it, else whether ``state`` is ``COMPLETED``."""
        if hasattr(self, "onDisk"):
            return self.onDisk
        return getattr(self, "state", "") == "COMPLETED"


class TapeStageStatus(Struct):
    """A stage request's progress: ``id``, timestamps, and a ``files`` list."""

    id: str
    files: list[TapeStageFileStatus]

    def __init__(self, status: Mapping[str, Any]) -> None:
        entries = dict(status)
        entries["files"] = [TapeStageFileStatus(f) for f in entries.get("files", [])]
        super().__init__(entries)

    @staticmethod
    def _normalize_path(path: str) -> str:
        parsed = urlparse(path)
        if parsed.scheme and parsed.netloc:
            path = parsed.path or "/"
        if path.startswith("//"):
            path = path[1:]
        return path

    def file_status(self, path: str) -> TapeStageFileStatus | None:
        """The entry for ``path`` - a path or a full URL - or ``None``."""
        path = self._normalize_path(path)
        for status in self.files:
            if status.path == path:
                return status
        return None

    def is_on_disk(self, path: str) -> bool:
        """Whether ``path`` has been staged onto disk."""
        status = self.file_status(path)
        return bool(status and status.on_disk)
