"""Exception hierarchy.

Every failure raises. Server-reported errors map onto the ``OSError`` subclass
a local filesystem would have raised, so ``except FileNotFoundError`` works
identically against ``/tmp/x`` and ``root://host//store/x``.
"""

from __future__ import annotations

import builtins
import errno as _errno

__all__ = [
    "XRootDError",
    "ProtocolError",
    "ConnectionError",
    "TimeoutError",
    "TransientError",
    "AuthenticationError",
    "NoMechanismError",
    "CredentialError",
    "TokenExpiredError",
    "ServerError",
    "TLSRequiredError",
    "RedirectLimitError",
    "WaitLimitError",
    "ChecksumMismatchError",
    "PageIntegrityError",
    "TooLargeError",
    "raise_for_status",
]


class XRootDError(Exception):
    """Base of every error this package raises."""


def _rebuild(cls: type, args: tuple, kwargs: dict) -> BaseException:  # type: ignore[type-arg]
    """Unpickle helper: these exceptions carry more than ``args``."""
    return cls(*args, **kwargs)  # type: ignore[no-any-return]


class ProtocolError(XRootDError):
    """A malformed frame, an unexpected opcode, or a version mismatch."""


class ConnectionError(XRootDError, builtins.ConnectionError):
    """The transport failed."""


class TransientError(ConnectionError):
    """A retryable failure. Records progress so a caller can resume."""

    def __init__(self, message: str, *, attempts: int = 0, committed: int = 0) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.committed = committed

    def __reduce__(self) -> tuple:  # type: ignore[type-arg]
        state = {"attempts": self.attempts, "committed": self.committed}
        return _rebuild, (type(self), (self.args[0],), state)


class TimeoutError(TransientError, builtins.TimeoutError):
    """An operation exceeded its deadline.

    A timeout is a :class:`TransientError` because that is what it is - the
    request may well succeed on the next attempt - so code that retries on
    ``TransientError`` catches it, and code that cares specifically about
    slowness can still catch the builtin :class:`TimeoutError`.
    """


class AuthenticationError(XRootDError):
    """Authentication could not be completed."""


class NoMechanismError(AuthenticationError):
    """The server offered no mechanism this client can satisfy."""

    def __init__(self, offered: list[str], tried: dict[str, str] | None = None) -> None:
        self.offered = offered
        self.tried = tried or {}
        detail = "; ".join(f"{k}: {v}" for k, v in self.tried.items())
        msg = f"no usable authentication mechanism (server offered: {', '.join(offered) or 'none'})"
        super().__init__(f"{msg} [{detail}]" if detail else msg)

    def __reduce__(self) -> tuple:  # type: ignore[type-arg]
        return _rebuild, (type(self), (self.offered, self.tried), {})


class CredentialError(AuthenticationError):
    """A credential was missing, unreadable, or malformed."""


class TokenExpiredError(CredentialError):
    """A bearer token is expired; the server would have rejected it."""


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
        super().__init__(f"{algorithm} mismatch: expected {expected}, got {actual}")
        self.algorithm = algorithm
        self.expected = expected
        self.actual = actual

    def __reduce__(self) -> tuple:  # type: ignore[type-arg]
        return _rebuild, (type(self), (self.algorithm, self.expected, self.actual), {})


class PageIntegrityError(XRootDError):
    """A ``kXR_pgwrite`` page kept arriving corrupt, past the retry budget.

    Not a :class:`ChecksumMismatchError`: nothing was computed here and found
    wanting. The server checksummed the page as it arrived, said it did not
    match what was sent with it, and went on saying so after the page had
    been retransmitted - so the wire, not the data, is what is broken.
    """

    def __init__(self, offset: int, retries: int, *, path: str | None = None) -> None:
        self.offset = offset
        self.retries = retries
        self.path = path
        where = f" of {path}" if path else ""
        super().__init__(
            f"the page at offset {offset}{where} was still corrupt on arrival after "
            f"{retries} retransmissions"
        )

    def __reduce__(self) -> tuple:  # type: ignore[type-arg]
        return _rebuild, (type(self), (self.offset, self.retries), {"path": self.path})


class TooLargeError(XRootDError):
    """A whole-file read that would not fit in memory, refused before it starts.

    Only a read that never said how much it wanted raises this - ``read()``
    with no argument, :meth:`~xrdclient.FileSystem.read_bytes`,
    :meth:`~xrdclient.XRootDPath.read_text`. ``read(n)`` is a caller who knows what
    they are asking for and is always honoured, and so is a larger
    :attr:`~xrdclient.Config.max_read_size`.
    """

    def __init__(self, size: int, limit: int, *, path: str | None = None) -> None:
        self.size = size
        self.limit = limit
        self.path = path
        what = f"{path} is" if path else "that is"
        super().__init__(
            f"{what} {size} bytes, over the {limit} byte ceiling on reading a whole file "
            f"into memory: read it in pieces (for block in file: ...), copy it to disk "
            f"with xrdclient.copy(), or raise config.max_read_size"
        )

    def __reduce__(self) -> tuple:  # type: ignore[type-arg]
        return _rebuild, (type(self), (self.size, self.limit), {"path": self.path})


class ServerError(XRootDError):
    """The server returned a ``kXR_error`` response.

    Subclasses that also derive from ``OSError`` carry a matching ``errno``.
    """

    code: int = 0

    def __init__(self, code: int, message: str, *, path: str | None = None) -> None:
        self.code = code
        self.message = message
        self.path = path
        XRootDError.__init__(self, self._describe())

    def _describe(self) -> str:
        name = _CODE_NAMES.get(self.code, f"kXR_unknown({self.code})")
        where = f" [{self.path}]" if self.path else ""
        return f"{name}: {self.message}{where}"

    def __reduce__(self) -> tuple:  # type: ignore[type-arg]
        # ``OSError.__reduce__`` would round-trip through ``(errno, strerror)``
        # and lose both the kXR code and the path.
        return _rebuild, (type(self), (self.code, self.message), {"path": self.path})


def _oserror(name: str, base: type[OSError], eno: int) -> type[ServerError]:
    """Build a ServerError subclass that is also the natural OSError."""

    def __init__(self: ServerError, code: int, message: str, *, path: str | None = None) -> None:
        self.code = code
        self.message = message
        self.path = path
        base.__init__(self, eno, message, path)
        self.args = (eno, message)

    def __str__(self: ServerError) -> str:
        return self._describe()

    return type(name, (ServerError, base), {"__init__": __init__, "__str__": __str__, "errno": eno})


NotFoundError = _oserror("NotFoundError", FileNotFoundError, _errno.ENOENT)
ExistsError = _oserror("ExistsError", FileExistsError, _errno.EEXIST)
PermissionError_ = _oserror("PermissionError", builtins.PermissionError, _errno.EACCES)
IsADirectoryError_ = _oserror("IsADirectoryError", IsADirectoryError, _errno.EISDIR)
NotADirectoryError_ = _oserror("NotADirectoryError", NotADirectoryError, _errno.ENOTDIR)
NoSpaceError = _oserror("NoSpaceError", OSError, _errno.ENOSPC)
IOError_ = _oserror("IOError", OSError, _errno.EIO)
UnsupportedError = _oserror("UnsupportedError", OSError, _errno.ENOSYS)
ReadOnlyError = _oserror("ReadOnlyError", OSError, _errno.EROFS)
QuotaError = _oserror("QuotaError", OSError, _errno.EDQUOT)
AttrNotFoundError = _oserror("AttrNotFoundError", OSError, _errno.ENODATA)
BusyError = _oserror("BusyError", OSError, _errno.EBUSY)
InvalidArgumentError = _oserror("InvalidArgumentError", OSError, _errno.EINVAL)


class ServerTimeoutError(ServerError, TimeoutError):
    """A server saying "that took too long".

    The other :class:`TimeoutError` is this client giving up on a socket; this
    one is an answer that arrived, saying the request ran out of time at the
    far end. One ``except TimeoutError`` covers both, and both are transient:
    the next attempt may well be the one that fits.
    """

    errno = _errno.ETIMEDOUT

    def __init__(self, code: int, message: str, *, path: str | None = None) -> None:
        self.code = code
        self.message = message
        self.path = path
        TransientError.__init__(self, message)
        self.filename = path
        self.args = (_errno.ETIMEDOUT, message)

    def __str__(self) -> str:
        return self._describe()


class TLSRequiredError(ServerError, builtins.PermissionError):
    """A server refusing to do this in the clear.

    A plain :class:`PermissionError` says "you may not"; this one says "not
    like *this*" - the operation is fine, the connection is not, and retrying
    over TLS is the whole fix. ``except PermissionError`` still catches it.
    """

    errno = _errno.EACCES

    def __init__(self, code: int, message: str, *, path: str | None = None) -> None:
        self.code = code
        self.message = message
        self.path = path
        builtins.PermissionError.__init__(self, _errno.EACCES, message, path)
        self.args = (_errno.EACCES, message)

    def _describe(self) -> str:
        return (
            ServerError._describe(self)
            + " - the server wants TLS for this operation; connect with roots:// (or davs://)"
        )

    def __str__(self) -> str:
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


def raise_for_status(code: int, message: str, *, path: str | None = None) -> None:
    """Raise the exception that best represents a ``kXR_error`` response."""
    if code == 0:
        return
    if code == kXR_ChkSumErr:
        raise ChecksumMismatchError("crc32c", "<server>", message or "<client>")
    cls = _CODE_CLASSES.get(code, ServerError)
    raise cls(code, message, path=path)
