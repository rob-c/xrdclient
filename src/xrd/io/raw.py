"""``io.RawIOBase`` over an XRootD file handle.

Implementing the raw layer, rather than a bespoke file-like class, is what
makes everything else in Python work for free: :class:`io.BufferedReader`
gives read-ahead, :class:`io.TextIOWrapper` gives decoding and line
iteration, and any library that accepts a binary file object accepts this.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Literal

from ..client.file import File
from ..errors import NotFoundError
from ..flags import Access, OpenFlags, flags_for_mode, parse_mode

if TYPE_CHECKING:
    from _typeshed import ReadableBuffer, WriteableBuffer

#: ``parse_mode`` and ``flags_for_mode`` live in :mod:`xrd.flags`, which has
#: no imports of its own; they are re-exported here because this is where
#: mode strings are turned into open requests.
__all__ = ["XRootDRawIO", "flags_for_mode", "parse_mode", "OpenBinaryMode", "OpenTextMode"]

#: The mode strings that mean bytes, spelled the ways people actually spell
#: them. :func:`parse_mode` accepts any permutation at runtime; these exist so
#: that a literal mode gives the caller a precisely typed file object back.
OpenBinaryMode = Literal[
    "rb", "br", "wb", "bw", "xb", "bx", "ab", "ba",
    "rb+", "r+b", "br+", "wb+", "w+b", "bw+",
    "xb+", "x+b", "bx+", "ab+", "a+b", "ba+",
]  # fmt: skip

#: The mode strings that mean text - the default, exactly as for the builtin.
OpenTextMode = Literal[
    "r", "w", "x", "a", "rt", "tr", "wt", "tw", "xt", "tx", "at", "ta",
    "r+", "rt+", "r+t", "w+", "wt+", "w+t",
    "x+", "xt+", "x+t", "a+", "at+", "a+t",
]  # fmt: skip


class XRootDRawIO(io.RawIOBase):
    """Unbuffered binary I/O on a remote file."""

    def __init__(
        self,
        file: File,
        mode: str = "rb",
        *,
        posc: bool = False,
        opened: bool = False,
    ) -> None:
        super().__init__()
        base, _, updating = parse_mode(mode)
        self._file = file
        self._mode = mode
        self._base = base
        self._readable = base == "r" or updating
        self._writable = base in "wxa" or updating
        self._pos = 0
        if not opened:
            self._open(file, mode, posc=posc)
        if base == "a":
            self._pos = file.size

    @staticmethod
    def _open(file: File, mode: str, *, posc: bool) -> None:
        """Open ``file``, creating it where the mode says to.

        ``kXR_open_apnd`` opens for appending but does not create, while
        Python's ``"a"`` creates a missing file and appends to an existing
        one. No single flag combination does both - ``kXR_new`` is refused
        outright when the file is there - so the missing file is created on
        the second attempt.
        """
        access = Access.OWNER_READ | Access.OWNER_WRITE | Access.GROUP_READ
        flags = flags_for_mode(mode, posc=posc)
        try:
            file.open(flags, access)
        except NotFoundError:
            if not flags & OpenFlags.APPEND:
                raise
            file.open(flags | OpenFlags.NEW, access)

    # -- capabilities ---------------------------------------------------

    def readable(self) -> bool:
        return self._readable

    def writable(self) -> bool:
        return self._writable

    def seekable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def name(self) -> str:
        return str(self._file.url)

    @property
    def file(self) -> File:
        """The underlying handle, for vector and paged operations."""
        return self._file

    # -- position -------------------------------------------------------

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._pos + offset
        elif whence == io.SEEK_END:
            target = self._file.size + offset
        else:
            raise ValueError(f"invalid whence {whence!r}")
        if target < 0:
            raise OSError(22, "negative seek position", self.name)
        self._pos = target
        return target

    # -- data -----------------------------------------------------------

    def readinto(self, buffer: WriteableBuffer) -> int:
        self._check_readable()
        view = memoryview(buffer).cast("B")
        if not view:
            return 0
        # Straight into the caller's buffer: for a read worth pipelining this
        # is the whole transfer with nothing copied behind it.
        count = self._file.readinto(view, self._pos)
        self._pos += count
        return count

    def readall(self) -> bytes:
        self._check_readable()
        data = self._file.read(-1, self._pos)
        self._pos += len(data)
        return data

    def write(self, data: ReadableBuffer) -> int:
        self._check_writable()
        payload = bytes(data)
        if not payload:
            return 0
        written = self._file.write(payload, self._pos)
        self._pos += written
        return written

    def truncate(self, size: int | None = None) -> int:
        self._check_writable()
        target = self._pos if size is None else size
        self._file.truncate(target)
        return target

    def flush(self) -> None:
        if self._writable and not self.closed and self._file.is_open:
            self._file.sync()

    def close(self) -> None:
        if self.closed:
            return
        try:
            # The base class flushes first, so the handle must still be open.
            super().close()
        finally:
            self._file.close()

    # -- checks ---------------------------------------------------------

    def _check_readable(self) -> None:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        if not self._readable:
            raise io.UnsupportedOperation(f"{self.name} is not open for reading")

    def _check_writable(self) -> None:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        if not self._writable:
            raise io.UnsupportedOperation(f"{self.name} is not open for writing")

    def __repr__(self) -> str:
        return f"XRootDRawIO({self.name!r}, mode={self._mode!r}, pos={self._pos})"
