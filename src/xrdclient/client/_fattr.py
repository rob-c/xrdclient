"""What a refused extended attribute raises, for files and filesystems alike.

``kXR_fattr`` can answer ``kXR_ok`` for the request as a whole and still carry
a failure for one attribute inside it, as a code beside that attribute's name.
xrootd puts a ``kXR_*`` error number there (``kXR_AttrNotFound``, 3027); some
servers put an ``errno`` instead, so both are understood. Either way the
exception is the one the rest of the package raises for that condition, so
``except FileExistsError`` means the same thing here as anywhere else.
"""

from __future__ import annotations

import errno
import os

from ..errors import (
    AttrNotFoundError,
    ExistsError,
    IOError_,
    NotFoundError,
    PermissionError_,
    ServerError,
    kXR_AttrNotFound,
    kXR_IOError,
    kXR_ItExists,
    kXR_NotAuthorized,
    kXR_NotFound,
    raise_for_status,
)
from ..proto import responses as rp

__all__ = ["MissingAttributeError", "check", "missing"]

#: The ``errno`` values a server may use instead, and what each one means.
_ERRNO: dict[int, tuple[type[ServerError], int]] = {
    errno.EEXIST: (ExistsError, kXR_ItExists),
    errno.ENODATA: (AttrNotFoundError, kXR_AttrNotFound),
    errno.ENOENT: (NotFoundError, kXR_NotFound),
    errno.EACCES: (PermissionError_, kXR_NotAuthorized),
}

#: Where the protocol's own error numbers begin; anything below is an errno.
_FIRST_KXR = 3000


class MissingAttributeError(AttrNotFoundError, KeyError):  # type: ignore[misc, valid-type]
    """An attribute that is not there: an ``OSError`` like every other failure.

    Also a :class:`KeyError`, which is what :meth:`File.getxattr` raised before
    it raised what :meth:`FileSystem.getxattr` does, so ``except KeyError``
    written against it still catches this.
    """

    def __init__(self, code: int, message: str, *, path: str | None = None) -> None:
        AttrNotFoundError.__init__(self, code, message, path=path)


def missing(name: str, path: str) -> ServerError:
    """The error for an attribute that is not there."""
    return MissingAttributeError(kXR_AttrNotFound, f"no attribute {name!r}", path=path)


def check(result: rp.FattrResult, path: str) -> None:
    """Raise for the first attribute the server refused, if any."""
    for item in result.items:
        if item.code >= _FIRST_KXR:
            raise_for_status(item.code, f"attribute {item.name!r}", path=path)
        if item.code:
            kind, code = _ERRNO.get(item.code, (IOError_, kXR_IOError))
            raise kind(code, f"{os.strerror(item.code)}: attribute {item.name!r}", path=path)
