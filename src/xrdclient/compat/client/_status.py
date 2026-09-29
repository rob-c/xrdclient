"""Exceptions in, ``XRootDStatus`` out.

The native API raises; the bindings return a status. This module is the one
place that turns the first into the second, so the numbers ported code
compares - XrdCl's ``code``, the server's ``errno``, the ``shellcode`` a
script exits with - are decided once and match what the bindings report for
the same failure (see ``XrdCl/XrdClStatus.hh`` for the tables).

Only failures of the *operation* become a status. A ``TypeError`` or
``ValueError`` is a mistake in the calling code, and the bindings raise those
too - an I/O call on a closed file is ``ValueError`` in both - so they are
left to propagate rather than being dressed up as a server's answer.
"""

from __future__ import annotations

from ... import errors as e
from .responses import XRootDStatus

__all__ = ["OK", "UnsupportedURLError", "failure", "from_exception", "guard", "status"]

# XrdCl::XRootDStatus::status
stOK = 0
stError = 1
stFatal = 3

# XrdCl::XRootDStatus::code - the ones a native failure can map to.
errNone = 0
errUnknown = 2
errInvalidOp = 3
errInvalidArgs = 9
errOSError = 12
errNotSupported = 13
errDataError = 14
errNotImplemented = 15
errInvalidAddr = 101
errSocketTimeout = 103
errConnectionError = 108
errTlsError = 110
errLoginFailed = 203
errAuthFailed = 204
errOperationExpired = 206
errOperationInterrupted = 207
errInvalidResponse = 303
errCheckSumError = 305
errRedirectLimit = 306
errErrorResponse = 400
errLocalError = 402

#: XrdCl's words for each code, which lead every status message.
_DESCRIPTIONS = {
    errNone: "",
    errUnknown: "Unknown error",
    errInvalidOp: "Invalid operation",
    errInvalidArgs: "Invalid arguments",
    errOSError: "OS Error",
    errNotSupported: "Operation not supported",
    errDataError: "Received corrupted data",
    errNotImplemented: "Operation is not implemented",
    errInvalidAddr: "Invalid address",
    errSocketTimeout: "Socket timeout",
    errConnectionError: "Connection error",
    errTlsError: "TLS error",
    errLoginFailed: "Login failed",
    errAuthFailed: "Auth failed",
    errOperationExpired: "Operation expired",
    errOperationInterrupted: "Operation interrupted",
    errInvalidResponse: "Invalid response",
    errCheckSumError: "CheckSum error",
    errRedirectLimit: "Redirect limit has been reached",
    errErrorResponse: "Server responded with an error",
    errLocalError: "Local error",
}


class UnsupportedURLError(Exception):
    """An operation on a filesystem whose URL did not parse, as XrdCl refuses it."""


#: Most specific first: ``isinstance`` picks the first class that matches.
_CLASS_CODES: tuple[tuple[type[BaseException], int, int], ...] = (
    (e.ChecksumMismatchError, errCheckSumError, stError),
    (e.PageIntegrityError, errDataError, stError),
    (e.RedirectLimitError, errRedirectLimit, stError),
    (e.AuthenticationError, errAuthFailed, stFatal),
    (e.TimeoutError, errOperationExpired, stError),
    (e.ConnectionError, errConnectionError, stFatal),
    (e.ProtocolError, errInvalidResponse, stFatal),
    (NotImplementedError, errNotImplemented, stError),
    (UnsupportedURLError, errNotSupported, stError),
)


def status(
    code: int = errNone, *, errno: int = 0, message: str = "", level: int | None = None
) -> XRootDStatus:
    """A status with XrdCl's fields, message and shell code filled in."""
    if level is None:
        level = stOK if code == errNone else stError
    return XRootDStatus(
        {
            "status": level,
            "code": code,
            "errno": errno,
            "message": _message(level, code, errno, message),
            "shellcode": 0 if level == stOK else code // 100 + 50,
            "error": level != stOK,
            "fatal": level == stFatal,
            "ok": level == stOK,
        }
    )


def _message(level: int, code: int, errno: int, detail: str) -> str:
    """XrdCl's rendering: ``[ERROR] <what>: [<errno>] <detail>``."""
    if level == stOK:
        return "[SUCCESS] "
    prefix = "[FATAL] " if level == stFatal else "[ERROR] "
    text = prefix + _DESCRIPTIONS.get(code, "Unknown error")
    if code == errErrorResponse:
        return f"{text}: [{errno}] {detail}\n"
    return f"{text}: {detail}\n" if detail else text


#: The status of everything that went right; statuses are never mutated.
OK = status()


def failure(code: int, message: str = "", *, errno: int = 0) -> XRootDStatus:
    """An error status for something this layer refuses before sending."""
    return status(code, errno=errno, message=message)


def from_exception(exc: BaseException) -> XRootDStatus:
    """The status the bindings would have reported for ``exc``."""
    if isinstance(exc, e.ServerError):
        return status(errErrorResponse, errno=exc.code, message=exc.message)
    for cls, code, level in _CLASS_CODES:
        if isinstance(exc, cls):
            return status(code, message=str(exc), level=level)
    if isinstance(exc, OSError):
        # A local file - the ``file://`` end of a copy - failing the way the
        # operating system says it did.
        detail = exc.strerror or str(exc)
        return status(errOSError, errno=exc.errno or 0, message=detail)
    return status(errUnknown, message=str(exc))


def guard(exc: BaseException) -> None:
    """Re-raise what the bindings would also raise; return for the rest."""
    if isinstance(exc, (TypeError, ValueError)) and not isinstance(exc, OSError):
        raise exc
