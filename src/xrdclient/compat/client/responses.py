"""The response objects of ``XRootD.client.responses``.

Every one is a :class:`Struct` - a bag of attributes built from a ``dict`` -
because that is what the bindings hand back, and code written against them
reaches for ``info.size`` and ``vars(info)`` and ``repr(info)`` alike. The
attribute names, their types and the ``repr`` are the bindings' own, checked
against them on a live server; see ``tests/test_pyxrootd_compat.py``.

The class-level annotations on each :class:`Struct` subclass document the
fields for readers and type checkers only - nothing enforces them at run
time, exactly as with the bindings, whose objects are built from whatever
dict the C++ side produced. :mod:`._convert` is where native results become
these objects; keep the two in step when a field is added.
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
        """Carry ``status``; the message is the status's own, as in the bindings."""
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
        """Take ``entries`` as attributes, in their order; no field is required."""
        self.__dict__.update(entries)

    def __repr__(self) -> str:
        """``<name: value, ...>`` - the bindings' format, which doctests and logs match."""
        fields = ", ".join(f"{key}: {value!r}" for key, value in self.__dict__.items())
        return f"<{fields}>"

    def __eq__(self, other: object) -> bool:
        """Equal when the same kind with the same fields; a ``StatInfo`` never equals a dict."""
        if type(other) is not type(self):
            return NotImplemented
        return self.__dict__ == other.__dict__

    # Defining ``__eq__`` leaves ``__hash__`` as None: unhashable, being
    # mutable, like the bindings' own objects.


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
    # attributes, so ``vars(status)`` and the ``repr`` stay the eight fields.
    # The hundreds group them as ``XrdClStatus.hh`` does: 0-99 generic,
    # 100s socket, 200s protocol, 300s the client's own checks, 400s what a
    # server answered, 500s a negative response from a handler.
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
        """The message alone (``[ERROR] Server responded with an error: ...``)."""
        return self.message

    def __getitem__(self, key: str) -> Any:
        """``status["ok"]``: dict-style access, for code written against dict statuses."""
        return self.__dict__[key]

    def __contains__(self, key: object) -> bool:
        """``"errno" in status``, the other half of the dict-like reading."""
        return key in self.__dict__

    @property
    def error_name(self) -> str | None:
        """The symbolic name of ``code`` (``"errNotFound"``), or ``None`` for one XrdCl lacks."""
        # -1 is no XrdCl code, so a status without one has no name either.
        return self._ERROR_NAMES.get(getattr(self, "code", -1))

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
    """``version`` and ``hostinfo``, both as the server sent them.

    Byte-swapped, as the bindings report them; see :func:`._convert.protocol_info`.
    """

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
    """``kXR_stat`` with ``kXR_vfs``: space across the servers behind a path.

    ``free_*`` are in megabytes and ``utilization_*`` percentages, as the
    server reports them; ``*_staging`` describe the staging (tape-backed) pool.
    """

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
        """The entries in the order the server listed them."""
        return iter(self.dirlist)


class Location(Struct):
    """One server that has a file: ``address``, ``type``, ``accesstype``.

    ``type`` is a :class:`~.flags.LocationType` and ``accesstype`` an
    :class:`~.flags.AccessType`, as integers; ``is_manager``/``is_server``
    are the same fact as booleans, which the bindings also provide.
    """

    address: str
    type: int
    accesstype: int
    is_manager: bool
    is_server: bool


class LocationInfo(Struct):
    """Every :class:`Location` a locate found; iterable."""

    locations: list[Location]

    def __iter__(self) -> Iterator[Location]:
        """The locations, in the order the redirector gave them."""
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
        """The chunks in request order, whatever order the server sent them."""
        return iter(self.chunks)


class HostInfo(Struct):
    """One server a request passed through on its way to an answer.

    ``protocol`` and ``flags`` are 0 for a server that redirected before it
    could say (or was never reached); ``load_balancer`` marks the manager
    that redirected the request.
    """

    url: URL
    protocol: int
    flags: int
    load_balancer: bool


class HostList(Struct):
    """The servers a request visited, handed to a callback as its third argument."""

    hosts: list[HostInfo]

    def __iter__(self) -> Iterator[HostInfo]:
        """The hosts from the first contacted to the one that answered."""
        return iter(self.hosts)


# Built after the class body because a comprehension inside it cannot see the
# class namespace. ``errPipelineError`` and ``errPipelineFailed`` share 17;
# the later name in the class body wins, which is the one XrdCl now uses.
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
    """The :class:`XRootDError` subclass for a failed status's ``code`` and ``errno``.

    The server's ``errno`` only counts when ``code`` says the server answered
    (``errErrorResponse``): a client-side failure's ``errno`` is an OS number
    and could collide with nothing meaningful here. Anything unmatched is a
    plain :class:`XRootDOperationError`.
    """
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
        """Take one archive-info entry; ``locality`` and ``error`` default to ``None``.

        The Tape REST API omits whichever of the two does not apply, and the
        bindings' object has both attributes regardless.
        """
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
        """Take the stage-status JSON, turning each ``files`` entry into an object."""
        entries = dict(status)
        entries["files"] = [TapeStageFileStatus(f) for f in entries.get("files", [])]
        super().__init__(entries)

    @staticmethod
    def _normalize_path(path: str) -> str:
        """The path a site reports, for a path or a full ``https://`` URL.

        Sites echo the bare path back in ``files``; a caller often holds the
        URL. A doubled leading slash (``root://h//p``'s spelling) is folded
        to one so either form finds the entry.
        """
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
