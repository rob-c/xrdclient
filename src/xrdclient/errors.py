"""Exception hierarchy, joined to :mod:`xrdcore.errors`.

Every failure raises. Server-reported errors map onto the ``OSError`` subclass
a local filesystem would have raised, so ``except FileNotFoundError`` works
identically against ``/tmp/x`` and ``root://host//store/x``.

Where to start: :func:`raise_for_status` is the single place a ``kXR_error``
response becomes an exception; ``_CODE_CLASSES`` picks the class and
:data:`KXR_ERRNO` mirrors ``XProtocol::toErrno`` so ``errno`` values match
what XrdCl's callers see. ``ConnectionError`` and ``TimeoutError`` shadow the
builtins on purpose - each *subclasses* its builtin, so either spelling of
``except`` catches them.

How this joins the libraries (docs/errors.md has the whole picture): since
0.4.0 the vocabulary every library on the stack raises - xrdcore, xrdsec,
xrdhttp - is :mod:`xrdcore.errors`, and the names here that mean the same
thing *are* those classes, not copies or subclasses of them::

    XRootDError is xrdcore.errors.XrdError
    ProtocolError, MetalinkError, ConnectionError, TransientError,
    TimeoutError, AuthenticationError, CredentialError, TokenExpiredError,
    InsecureCredentialError                 is the xrdcore class of that name

So ``except xrdclient.errors.CredentialError`` catches the
:class:`~xrdcore.errors.CredentialError` xrdsec raises, ``except
xrdclient.XRootDError`` catches everything any library raises, and nothing
is translated or re-raised on the way. A subclass the other way round
(``class CredentialError(XRootDError, xrdcore.errors.CredentialError)``)
would not do that: an ``except`` only catches its own class and those below
it. The errors only XRootD has - :class:`ServerError` and its ``OSError``
family, :class:`NoMechanismError`, :class:`PageIntegrityError`,
:class:`AbortedError`, ... - are defined here and join the hierarchy by
inheritance, by multiple inheritance where they are also a builtin.

What must not break: every exception here pickles with all its attributes
(``__reduce__`` + :func:`rebuild`, which is xrdcore's; ``tests/test_errors.py``
pins it), because errors cross process boundaries under fsspec/dask and
``concurrent.futures``. A new exception with keyword-only or extra state
needs its own ``__reduce__``.
"""

from __future__ import annotations

import builtins
import errno as _errno
from collections.abc import Callable

from xrdcore.errors import (
    AuthenticationError,
    ConnectionError,
    CredentialError,
    InsecureCredentialError,
    MetalinkError,
    ProtocolError,
    TimeoutError,
    TokenExpiredError,
    TransientError,
    rebuild,
)
from xrdcore.errors import XrdError as XRootDError

#: What ``__reduce__`` returns here, the same shape as xrdcore's own errors:
#: :func:`rebuild` and the arguments it is called with.
_Reduced = tuple[Callable[..., BaseException], tuple[object, ...]]

__all__ = [
    "KXR_ERRNO",
    "AbortedError",
    "AuthenticationError",
    "ChecksumMismatchError",
    "ConnectionError",
    "CredentialError",
    "InsecureCredentialError",
    "MetalinkError",
    "NoMechanismError",
    "PageIntegrityError",
    "ProtocolError",
    "RedirectLimitError",
    "ServerError",
    "TLSRequiredError",
    "TimeoutError",
    "TokenExpiredError",
    "TooLargeError",
    "TransientError",
    "WaitLimitError",
    "XRootDError",
    "errno_for",
    "kxr_for",
    "raise_for_status",
]


class NoMechanismError(AuthenticationError):
    """The server offered no mechanism this client can satisfy."""

    def __init__(self, offered: list[str], tried: dict[str, str] | None = None) -> None:
        """``offered`` is the server's ``kXR_login`` security list in its order.

        ``tried`` maps each mechanism this client attempted to why it failed;
        both go into the message because "login failed" alone gives a user
        nothing to act on.
        """
        self.offered = offered
        self.tried = tried or {}
        detail = "; ".join(f"{k}: {v}" for k, v in self.tried.items())
        msg = (
            "Login failed. No available authentication method succeeded "
            f"(server methods: {', '.join(offered) or 'none'})"
        )
        super().__init__(f"{msg} [{detail}]" if detail else msg)

    def __reduce__(self) -> _Reduced:
        """Pickle by the offered/tried lists; the message is rebuilt from them."""
        return rebuild, (type(self), (self.offered, self.tried), {})


class RedirectLimitError(XRootDError):
    """The redirect budget was exhausted."""


class WaitLimitError(TransientError):
    """The server kept answering ``kXR_wait`` past the budget for it.

    Transient, because the answer is "not now" rather than "no" - but not a
    transport failure: the connection is fine and reconnecting would only ask
    a busy server the same question over a new socket.
    """


class ChecksumMismatchError(XRootDError):
    """A computed checksum did not match the expected one."""

    def __init__(self, algorithm: str, expected: str, actual: str) -> None:
        """Hold both digests and the algorithm, for code that wants to compare them.

        The message tells the user to check the *source*: a mismatch after a
        copy almost always means the source changed or was corrupt, not that
        retrying the transfer will help.
        """
        super().__init__(
            f"The {algorithm} checksum does not match: expected {expected}, got {actual}. "
            "Check the source file before copying it again."
        )
        self.algorithm = algorithm
        self.expected = expected
        self.actual = actual

    def __reduce__(self) -> _Reduced:
        """Pickle by the three digest fields; the message is rebuilt from them."""
        return rebuild, (type(self), (self.algorithm, self.expected, self.actual), {})


class PageIntegrityError(XRootDError):
    """A ``kXR_pgwrite`` page kept arriving corrupt, past the retry budget.

    Not a :class:`ChecksumMismatchError`: nothing was computed here and found
    wanting. The server checksummed the page as it arrived, said it did not
    match what was sent with it, and went on saying so after the page had
    been retransmitted - so the wire, not the data, is what is broken.
    """

    def __init__(self, offset: int, retries: int, *, path: str | None = None) -> None:
        """``offset`` is the first byte of the bad page; ``retries`` the retransmissions spent."""
        self.offset = offset
        self.retries = retries
        self.path = path
        where = f" of {path}" if path else ""
        super().__init__(
            f"the page at offset {offset}{where} was still corrupt on arrival after "
            f"{retries} retransmissions"
        )

    def __reduce__(self) -> _Reduced:
        """Pickle with ``path`` intact (it is keyword-only, so ``args`` cannot carry it)."""
        return rebuild, (type(self), (self.offset, self.retries), {"path": self.path})


class TooLargeError(XRootDError):
    """A whole-file read that would not fit in memory, refused before it starts.

    Only a read that never said how much it wanted raises this - ``read()``
    with no argument, :meth:`~xrdclient.FileSystem.read_bytes`,
    :meth:`~xrdclient.XRootDPath.read_text`. ``read(n)`` is a caller who knows what
    they are asking for and is always honoured, and so is a larger
    :attr:`~xrdclient.Config.max_read_size`.
    """

    def __init__(self, size: int, limit: int, *, path: str | None = None) -> None:
        """``size`` is the file's length, ``limit`` the ceiling it would have broken."""
        self.size = size
        self.limit = limit
        self.path = path
        what = f"{path} is" if path else "that is"
        super().__init__(
            f"{what} {size} bytes, over the {limit} byte ceiling on reading a whole file "
            f"into memory: read it in pieces (for block in file: ...), copy it to disk "
            f"with xrdclient.copy(), or raise config.max_read_size"
        )

    def __reduce__(self) -> _Reduced:
        """Pickle with ``path`` intact (it is keyword-only, so ``args`` cannot carry it)."""
        return rebuild, (type(self), (self.size, self.limit), {"path": self.path})


class AbortedError(XRootDError, builtins.OSError):
    """The operation was called off by :meth:`~xrdclient.File.abort` (or a cancel hook).

    Raised by the call that was blocked when the abort came - it is woken,
    rather than left to its timeout - and by every call made on the aborted
    object afterwards. ``errno`` is ``ECANCELED``, the number XrdCl's
    callers see for a cancelled job (``kXR_Cancelled`` maps to it), so
    ``except OSError`` code that switches on ``errno`` reads it correctly.

    Deliberately *not* a :class:`TransientError`: code that retries on a
    transient failure must not retry what someone asked to stop.
    """

    errno = _errno.ECANCELED

    def __init__(self, message: str = "the operation was aborted") -> None:
        """Set ``errno``/``strerror`` exactly as ``OSError(ECANCELED, message)`` would.

        ``args`` is pinned to the same pair so ``repr`` and pickling agree with
        what a local cancelled system call would have produced.
        """
        builtins.OSError.__init__(self, _errno.ECANCELED, message)
        self.args = (_errno.ECANCELED, message)

    def __str__(self) -> str:
        """Just the message, without the ``[Errno 125]`` prefix ``OSError`` would add."""
        return str(self.strerror)

    def __reduce__(self) -> _Reduced:
        """Pickle from the message alone; ``errno`` is fixed for this class."""
        return rebuild, (type(self), (self.strerror,), {})


class ServerError(XRootDError):
    """The server returned a ``kXR_error`` response.

    Subclasses that also derive from ``OSError`` carry a matching ``errno``.
    """

    code: int = 0
    #: This client's own explanation, when it has one the server could not
    #: give - why a login fell back to ``unix``, say. Shown by ``str()`` but
    #: kept out of :attr:`message`, which stays the server's words exactly.
    hint: str = ""

    def __init__(self, code: int, message: str, *, path: str | None = None) -> None:
        """``code`` is the ``kXR_*`` error number, ``message`` the server's own text.

        ``path`` is what the request named, added by the caller because the
        server's message often omits it.
        """
        self.code = code
        self.message = message
        self.path = path
        XRootDError.__init__(self, self._describe())

    def explain(self, hint: str) -> None:
        """Attach :attr:`hint`, so that ``str()`` carries it."""
        self.hint = hint
        if not isinstance(self, OSError):
            self.args = (self._describe(),)

    def _describe(self) -> str:
        """The user-facing sentence: summary, path, server text, code, then hint.

        The plain-language summary leads because that is what a user acts on;
        the raw server text and the numeric code follow for the bug report.
        """
        summary = _USER_ERRORS.get(self.code, "The server could not complete the request.")
        where = f" [{self.path}]" if self.path else ""
        note = f" ({self.hint})" if self.hint else ""
        detail = f" Server: {self.message}" if self.message else ""
        return f"{summary}{where}{detail} (XRootD error {self.code}){note}"

    def __reduce__(self) -> _Reduced:
        """Pickle by ``(code, message)`` plus ``path``, for every subclass alike.

        ``OSError.__reduce__`` would round-trip through ``(errno, strerror)``
        and lose both the kXR code and the path; the generated ``OSError``
        subclasses inherit this method, so they pickle the same way.
        """
        return rebuild, (type(self), (self.code, self.message), {"path": self.path})


class _ServerOSError(ServerError, OSError):
    """A :class:`ServerError` that is also the natural ``OSError``.

    Each subclass names its builtin base and sets ``errno``, so ``except
    FileNotFoundError`` code sees what it would locally.
    """

    errno: int

    def __init__(self, code: int, message: str, *, path: str | None = None) -> None:
        """Fill both halves: the ``kXR`` fields and the ``OSError`` triple.

        ``OSError.__init__`` gets ``(errno, message, path)`` so ``.filename`` is
        set; ``args`` is then cut back to ``(errno, message)`` so the path is not
        printed twice.
        """
        self.code = code
        self.message = message
        self.path = path
        OSError.__init__(self, self.errno, message, path)
        self.args = (self.errno, message)

    def __str__(self) -> str:
        """The plain-language summary, not ``OSError``'s ``[Errno 2] ...: 'path'`` form."""
        return self._describe()


class NotFoundError(_ServerOSError, FileNotFoundError):
    """The path does not exist (``kXR_NotFound``); also a :class:`FileNotFoundError`."""

    errno = _errno.ENOENT


class ExistsError(_ServerOSError, FileExistsError):
    """The path already exists; also a :class:`FileExistsError`."""

    errno = _errno.EEXIST


class PermissionError_(_ServerOSError, builtins.PermissionError):
    """Not authorised; also the builtin :class:`PermissionError`."""

    errno = _errno.EACCES


class IsADirectoryError_(_ServerOSError, IsADirectoryError):
    """A directory where a file was needed; also an :class:`IsADirectoryError`."""

    errno = _errno.EISDIR


class NotADirectoryError_(_ServerOSError, NotADirectoryError):
    """A file where a directory was needed; also a :class:`NotADirectoryError`."""

    errno = _errno.ENOTDIR


class NoSpaceError(_ServerOSError, OSError):
    """The server is out of space (``ENOSPC``)."""

    errno = _errno.ENOSPC


class IOError_(_ServerOSError, OSError):
    """A server-side I/O failure (``EIO``)."""

    errno = _errno.EIO


class UnsupportedError(_ServerOSError, OSError):
    """The server does not support the request (``ENOSYS``)."""

    errno = _errno.ENOSYS


class ReadOnlyError(_ServerOSError, OSError):
    """The target is read-only (``EROFS``)."""

    errno = _errno.EROFS


class QuotaError(_ServerOSError, OSError):
    """A quota would be exceeded (``EDQUOT``)."""

    errno = _errno.EDQUOT


class AttrNotFoundError(_ServerOSError, OSError):
    """No such extended attribute (``ENODATA``)."""

    errno = _errno.ENODATA


class BusyError(_ServerOSError, OSError):
    """The target is busy (``EBUSY``)."""

    errno = _errno.EBUSY


class InvalidArgumentError(_ServerOSError, OSError):
    """The server rejected an argument (``EINVAL``)."""

    errno = _errno.EINVAL


# Named as the builtins they stand for, as they were when generated; the
# trailing underscore only keeps the builtins usable in this module.
PermissionError_.__name__ = "PermissionError"
IsADirectoryError_.__name__ = "IsADirectoryError"
NotADirectoryError_.__name__ = "NotADirectoryError"
IOError_.__name__ = "IOError"


class ServerTimeoutError(ServerError, TimeoutError):
    """A server saying "that took too long".

    The other :class:`TimeoutError` is this client giving up on a socket; this
    one is an answer that arrived, saying the request ran out of time at the
    far end. One ``except TimeoutError`` covers both, and both are transient:
    the next attempt may well be the one that fits.
    """

    errno = _errno.ETIMEDOUT

    def __init__(self, code: int, message: str, *, path: str | None = None) -> None:
        """Initialise as a :class:`TransientError` and an ``OSError`` with ``ETIMEDOUT``.

        ``TransientError.__init__`` is called explicitly (not via ``super``) so
        ``attempts``/``committed`` exist and retry code that reads them works.
        """
        self.code = code
        self.message = message
        self.path = path
        TransientError.__init__(self, message)
        self.filename = path
        self.args = (_errno.ETIMEDOUT, message)

    def __str__(self) -> str:
        """The plain-language summary rather than ``OSError``'s errno-prefixed form."""
        return self._describe()


class TLSRequiredError(ServerError, builtins.PermissionError):
    """A server refusing to do this in the clear.

    A plain :class:`PermissionError` says "you may not"; this one says "not
    like *this*" - the operation is fine, the connection is not, and retrying
    over TLS is the whole fix. ``except PermissionError`` still catches it.
    """

    errno = _errno.EACCES

    def __init__(self, code: int, message: str, *, path: str | None = None) -> None:
        """Initialise as a ``PermissionError`` with ``EACCES`` and the path as ``filename``."""
        self.code = code
        self.message = message
        self.path = path
        builtins.PermissionError.__init__(self, _errno.EACCES, message, path)
        self.args = (_errno.EACCES, message)

    def _describe(self) -> str:
        """The base description plus the one thing that fixes it: use TLS."""
        return (
            ServerError._describe(self)
            + " - the server wants TLS for this operation; connect with roots:// (or davs://)"
        )

    def __str__(self) -> str:
        """The summary plus the ``roots://`` hint from :meth:`_describe`."""
        return self._describe()


# kXR_* server error codes (src/protocols/root/protocol/opcodes.h).
kXR_ArgInvalid = 3000
kXR_ArgMissing = 3001
kXR_ArgTooLong = 3002
kXR_FileLocked = 3003
kXR_FileNotOpen = 3004
kXR_FSError = 3005
kXR_InvalidRequest = 3006
kXR_IOError = 3007
kXR_NoMemory = 3008
kXR_NoSpace = 3009
kXR_NotAuthorized = 3010
kXR_NotFound = 3011
kXR_ServerError = 3012
kXR_Unsupported = 3013
kXR_noserver = 3014
kXR_NotFile = 3015
kXR_isDirectory = 3016
kXR_Cancelled = 3017
kXR_ItExists = 3018
kXR_ChkSumErr = 3019
kXR_inProgress = 3020
kXR_overQuota = 3021
kXR_SigVerErr = 3022
kXR_DecryptErr = 3023
kXR_Overloaded = 3024
kXR_fsReadOnly = 3025
kXR_BadPayload = 3026
kXR_AttrNotFound = 3027
kXR_TLSRequired = 3028
kXR_noReplicas = 3029
kXR_AuthFailed = 3030
kXR_Impossible = 3031
kXR_Conflict = 3032
kXR_TooManyErrs = 3033
kXR_ReqTimedOut = 3034
kXR_TimerExpired = 3035

_CODE_NAMES = {v: k for k, v in list(globals().items()) if k.startswith("kXR_")}

# Plain-language summaries; the exact server response remains in ``message``.
_USER_ERRORS: dict[int, str] = {
    kXR_ArgInvalid: "A setting is invalid. Check the command and configuration.",
    kXR_ArgMissing: "A required setting is missing. Check the command and configuration.",
    kXR_ArgTooLong: "A setting or path is too long. Use a shorter value.",
    kXR_FileLocked: "The file is locked. Try again later or contact the storage administrator.",
    kXR_FileNotOpen: "The file is no longer open. Reopen it before continuing.",
    kXR_FSError: "The storage reported a failure. Check its status or contact the administrator.",
    kXR_InvalidRequest: "The server could not accept this request. Check the command and settings.",
    kXR_IOError: "The server could not read or write the file. Check the storage status.",
    kXR_NoMemory: "The server is short of memory. Try again later or contact its administrator.",
    kXR_NoSpace: "Storage is full. Free space or choose another destination.",
    kXR_NotAuthorized: "Access denied. Check your credentials and permission to use this path.",
    kXR_NotFound: "File or folder not found. Check the path.",
    kXR_ServerError: (
        "The server reported a failure. Check its status or contact its administrator."
    ),
    kXR_Unsupported: "This operation is not supported by the server. Check its available features.",
    kXR_noserver: "No storage server is available. Check the endpoint or try again later.",
    kXR_NotFile: "This path is not the required file type. Check the path.",
    kXR_isDirectory: "This path names a folder, not a file. Choose a file path.",
    kXR_Cancelled: "The operation was canceled.",
    kXR_ItExists: (
        "The destination already exists. Choose another path or explicitly enable overwrite."
    ),
    kXR_ChkSumErr: "The file's checksum does not match. Check the source before copying it again.",
    kXR_inProgress: "The operation is still running. Wait before checking again.",
    kXR_overQuota: "Your storage quota is full. Free space or ask for a larger quota.",
    kXR_SigVerErr: "The server could not verify the request signature. Check your credentials.",
    kXR_DecryptErr: "The server could not read the encrypted request. Check your credentials.",
    kXR_Overloaded: "The server is busy. Try again later.",
    kXR_fsReadOnly: "This storage is read-only. Choose a writable destination.",
    kXR_BadPayload: "The server could not read the request data. Check client and server versions.",
    kXR_AttrNotFound: "The requested file metadata is missing. Check the attribute name.",
    kXR_TLSRequired: "The server requires an encrypted connection.",
    kXR_noReplicas: "No accessible copy of this file was found. Contact the storage administrator.",
    kXR_AuthFailed: "Login failed. Check that your proxy or token is valid for this service.",
    kXR_Impossible: "The server cannot perform this request. Check the command and settings.",
    kXR_Conflict: "The request conflicts with another operation. Check the file before retrying.",
    kXR_TooManyErrs: (
        "The server stopped after repeated failures. Contact the storage administrator."
    ),
    kXR_ReqTimedOut: "The server request timed out. Check its availability or try again later.",
    kXR_TimerExpired: "The server request timed out. Check its availability or try again later.",
}

_CODE_CLASSES: dict[int, type[ServerError]] = {
    kXR_ArgInvalid: InvalidArgumentError,
    kXR_ArgMissing: InvalidArgumentError,
    kXR_ArgTooLong: InvalidArgumentError,
    kXR_InvalidRequest: InvalidArgumentError,
    kXR_Impossible: InvalidArgumentError,
    kXR_FileLocked: BusyError,
    kXR_inProgress: BusyError,
    kXR_Overloaded: BusyError,
    kXR_Conflict: BusyError,
    kXR_FileNotOpen: InvalidArgumentError,
    kXR_FSError: IOError_,
    kXR_IOError: IOError_,
    kXR_ServerError: IOError_,
    kXR_TooManyErrs: IOError_,
    kXR_NoMemory: NoSpaceError,
    kXR_NoSpace: NoSpaceError,
    kXR_overQuota: QuotaError,
    kXR_NotAuthorized: PermissionError_,
    kXR_AuthFailed: PermissionError_,
    kXR_TLSRequired: TLSRequiredError,
    kXR_SigVerErr: PermissionError_,
    kXR_DecryptErr: PermissionError_,
    kXR_BadPayload: InvalidArgumentError,
    kXR_NotFound: NotFoundError,
    kXR_noserver: NotFoundError,
    kXR_noReplicas: NotFoundError,
    kXR_ReqTimedOut: ServerTimeoutError,
    kXR_TimerExpired: ServerTimeoutError,
    kXR_Unsupported: UnsupportedError,
    kXR_NotFile: NotADirectoryError_,
    kXR_isDirectory: IsADirectoryError_,
    kXR_ItExists: ExistsError,
    kXR_fsReadOnly: ReadOnlyError,
    kXR_AttrNotFound: AttrNotFoundError,
    kXR_Cancelled: InvalidArgumentError,
}


def _platform_errno(name: str, fallback: int) -> int:
    """An ``errno`` constant this platform may lack, falling back as XProtocol.hh does."""
    return int(getattr(_errno, name, fallback))


_EAUTH = _platform_errno("EAUTH", _platform_errno("EBADE", _errno.EACCES))
_EBADRQC = _platform_errno("EBADRQC", _platform_errno("EBADRPC", _errno.EINVAL))
_ENOATTR = _platform_errno("ENOATTR", _errno.ENODATA)
_ETIME = _platform_errno("ETIME", _errno.ETIMEDOUT)

#: ``XProtocol::toErrno`` (XRootD 5.9): each ``kXR_*`` error code as the
#: ``errno`` XrdCl reports for it. :func:`errno_for` reads it; a code that is
#: not listed is ``ENOMSG``, as there. Platform-specific numbers (``EAUTH``,
#: ``EBADRQC``, ``ENOATTR``, ``ETIME``) fall back as XProtocol.hh's do on a
#: platform without them.
KXR_ERRNO: dict[int, int] = {
    kXR_ArgInvalid: _errno.EINVAL,
    kXR_ArgMissing: _errno.EINVAL,
    kXR_ArgTooLong: _errno.ENAMETOOLONG,
    kXR_FileLocked: _errno.EDEADLK,
    kXR_FileNotOpen: _errno.EBADF,
    kXR_FSError: _errno.ENODEV,
    kXR_InvalidRequest: _EBADRQC,
    kXR_IOError: _errno.EIO,
    kXR_NoMemory: _errno.ENOMEM,
    kXR_NoSpace: _errno.ENOSPC,
    kXR_NotAuthorized: _errno.EACCES,
    kXR_NotFound: _errno.ENOENT,
    kXR_ServerError: _errno.EFAULT,
    kXR_Unsupported: _errno.ENOTSUP,
    kXR_noserver: _errno.EHOSTUNREACH,
    kXR_NotFile: _errno.ENOTBLK,
    kXR_isDirectory: _errno.EISDIR,
    kXR_Cancelled: _errno.ECANCELED,
    kXR_ItExists: _errno.EEXIST,
    kXR_ChkSumErr: _errno.EDOM,
    kXR_inProgress: _errno.EINPROGRESS,
    kXR_overQuota: _errno.EDQUOT,
    kXR_SigVerErr: _errno.EILSEQ,
    kXR_DecryptErr: _errno.ERANGE,
    kXR_Overloaded: _errno.EUSERS,
    kXR_fsReadOnly: _errno.EROFS,
    kXR_BadPayload: _errno.EINVAL,
    kXR_AttrNotFound: _ENOATTR,
    kXR_TLSRequired: _errno.EPROTOTYPE,
    kXR_noReplicas: _errno.EADDRNOTAVAIL,
    kXR_AuthFailed: _EAUTH,
    kXR_Impossible: _errno.EIDRM,
    kXR_Conflict: _errno.ENOTTY,
    kXR_TooManyErrs: _errno.ETOOMANYREFS,
    kXR_ReqTimedOut: _errno.ETIMEDOUT,
    kXR_TimerExpired: _ETIME,
}


def _inverse() -> dict[int, int]:
    """``errno`` to ``kXR_*``: the first code :data:`KXR_ERRNO` gives each number.

    The table is many-to-one in three places (``EINVAL`` is three codes), and
    the first - lowest - code is the one ``XProtocol::mapError`` answers with.
    ``mapError`` also knows a few numbers no code maps *to*: ``EPERM`` is
    "not authorised" like ``EACCES``, ``ENOBUFS`` is "no memory", and
    ``ENOTEMPTY`` is "it exists", as a directory that will not go away is.
    """
    table: dict[int, int] = {}
    for code, number in KXR_ERRNO.items():
        table.setdefault(number, code)
    table.update(
        {
            _errno.EPERM: kXR_NotAuthorized,
            _errno.ENOBUFS: kXR_NoMemory,
            _errno.ENOTEMPTY: kXR_ItExists,
        }
    )
    return table


_ERRNO_KXR = _inverse()


def errno_for(code: int) -> int:
    """The ``errno`` for a ``kXR_*`` error code, as XrdCl reports it (``XProtocol::toErrno``).

        >>> errno_for(kXR_NotFound) == errno.ENOENT
        True

    ``ENOMSG`` for a code the protocol does not define, which is what XrdCl
    says when it cannot say anything better.
    """
    return KXR_ERRNO.get(int(code), _errno.ENOMSG)


def kxr_for(number: int, default: int = kXR_FSError) -> int:
    """The ``kXR_*`` code for an ``errno`` (``XProtocol::mapError``), :func:`errno_for` reversed.

    A negative number is read as its magnitude, as ``mapError`` reads a
    POSIX ``-errno`` return. One with no code of its own is ``default`` -
    ``kXR_FSError``, as in XRootD, unless the caller has a better answer.
    """
    return _ERRNO_KXR.get(abs(int(number)), default)


def raise_for_status(code: int, message: str, *, path: str | None = None) -> None:
    """Raise the exception that best represents a ``kXR_error`` response."""
    if code != 0:
        raise _error_for_status(code, message, path=path)


def _error_for_status(code: int, message: str, *, path: str | None = None) -> XRootDError:
    """The exception :func:`raise_for_status` raises for a non-zero ``code``."""
    if code == kXR_ChkSumErr:
        return ChecksumMismatchError("crc32c", "<server>", message or "<client>")
    cls = _CODE_CLASSES.get(code, ServerError)
    return cls(code, message, path=path)
