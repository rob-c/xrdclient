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

#: ``parse_mode`` and ``flags_for_mode`` live in :mod:`xrdclient.flags`, which has
#: no imports of its own; they are re-exported here because this is where
#: mode strings are turned into open requests.
__all__ = ["OpenBinaryMode", "OpenTextMode", "XRootDRawIO", "flags_for_mode", "parse_mode"]

#: The mode strings that mean bytes, spelled the ways people actually spell
#: them. :func:`parse_mode` accepts any permutation at runtime; these exist so
#: that a literal mode gives the caller a precisely typed file object back.
OpenBinaryMode = Literal[
    "rb",
    "br",
    "wb",
    "bw",
    "xb",
    "bx",
    "ab",
    "ba",
    "rb+",
    "r+b",
    "br+",
    "wb+",
    "w+b",
    "bw+",
    "xb+",
    "x+b",
    "bx+",
    "ab+",
    "a+b",
    "ba+",
]

#: The mode strings that mean text - the default, exactly as for the builtin.
OpenTextMode = Literal[
    "r",
    "w",
    "x",
    "a",
    "rt",
    "tr",
    "wt",
    "tw",
    "xt",
    "tx",
    "at",
    "ta",
    "r+",
    "rt+",
    "r+t",
    "w+",
    "wt+",
    "w+t",
    "x+",
    "xt+",
    "x+t",
    "a+",
    "at+",
    "a+t",
]


class XRootDRawIO(io.RawIOBase):
    """Unbuffered binary I/O on a remote file.

    Every :meth:`readinto` and :meth:`write` is a positioned request on the
    underlying :class:`~xrdclient.client.file.File` (``kXR_read`` /
    ``kXR_write`` at an explicit offset): the position lives here, not on the
    server, which is what makes :meth:`seek` free. The object owns the handle
    and closes it with itself; :attr:`file` lends it out for vector and paged
    reads, which have no :mod:`io` equivalent.

    Normally reached through :func:`~xrdclient.io.open_url` with
    ``buffering=0``, or underneath its buffered layers.
    """

    def __init__(
        self,
        file: File,
        mode: str = "rb",
        *,
        posc: bool = False,
        opened: bool = False,
    ) -> None:
        """Wrap ``file``, opening it for ``mode`` unless ``opened`` says it already is.

        ``mode`` is a builtin :func:`open` mode string, mapped to XRootD open
        flags by :func:`~xrdclient.flags.flags_for_mode`. ``posc`` adds
        persist-on-successful-close (``kXR_posc``): the server deletes the
        file if the handle is lost before :meth:`close`. ``opened`` is for a
        caller that opened the handle itself with flags of its choosing. In
        ``"a"`` mode the position starts at the end, as for a local file.
        """
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
        """Whether the mode allows reading (``r`` or any ``+``)."""
        return self._readable

    def writable(self) -> bool:
        """Whether the mode allows writing (``w``, ``x``, ``a`` or any ``+``)."""
        return self._writable

    def seekable(self) -> bool:
        """Always true: every request carries its own offset."""
        return True

    def isatty(self) -> bool:
        """Always false; asked by :class:`io.TextIOWrapper` to pick line buffering."""
        return False

    @property
    def mode(self) -> str:
        """The mode string as given to the constructor."""
        return self._mode

    @property
    def name(self) -> str:
        """The file's URL, as the handle reports it."""
        return str(self._file.url)

    @property
    def file(self) -> File:
        """The underlying handle, for vector and paged operations."""
        return self._file

    # -- position -------------------------------------------------------

    def tell(self) -> int:
        """The client-side position; no request is made."""
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Move the position, as :meth:`io.IOBase.seek` does, without a round trip.

        ``SEEK_END`` uses the size the handle last saw (from the open, or a
        later stat), so it can be stale if another client is writing.
        Seeking past the end is allowed - a write there leaves a hole, as on
        a local file - and a negative result is ``EINVAL``.
        """
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
        """Read up to ``len(buffer)`` bytes at the position; 0 means end of file."""
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
        """Everything from the position to the end of the file as it is now.

        The size the open reported is only what the file was then: another
        writer may have appended since, and a read "to the end" that stopped
        at the old end would return a truncated file without saying so. So
        the handle is stat'ed afresh, which is one round trip against a read
        that is usually many.
        """
        self._check_readable()
        self._file.stat(refresh=True)
        data = self._file.read(-1, self._pos)
        self._pos += len(data)
        return data

    def write(self, data: ReadableBuffer) -> int:
        """Write ``data`` at the position (the current end in ``"a"`` mode).

        Returns the count the server accepted, which a buffered writer above
        uses to know what is left to send; a refused write raises instead.
        """
        self._check_writable()
        payload = _flat(data)
        if not payload:
            return 0
        if self._base == "a":
            # ``O_APPEND``: every write goes to the end of the file as it is
            # now, whatever the position says. The end cached at open is
            # wrong after a seek of the caller's own and after another
            # writer's append, and writing there overwrites either.
            self._pos = self._file.stat(refresh=True).st_size
        written = self._file.write(payload, self._pos)
        self._pos += written
        return written

    def truncate(self, size: int | None = None) -> int:
        """``kXR_truncate`` to ``size`` (default: the position); the position stays."""
        self._check_writable()
        target = self._pos if size is None else size
        self._file.truncate(target)
        return target

    def flush(self) -> None:
        """``kXR_sync`` a writable, open handle: data the server holds reaches storage.

        A no-op otherwise, because :class:`io.IOBase` calls ``flush`` from
        ``close`` and from finalisers, where a closed handle must not raise.
        """
        if self._writable and not self.closed and self._file.is_open:
            self._file.sync()

    def close(self) -> None:
        """Flush, then close the remote handle; a second call does nothing."""
        if self.closed:
            return
        try:
            # The base class flushes first, so the handle must still be open.
            super().close()
        finally:
            self._file.close()

    # -- checks ---------------------------------------------------------

    def _check_readable(self) -> None:
        """The errors the builtin raises for a closed or write-only file."""
        if self.closed:
            raise ValueError("I/O operation on closed file")
        if not self._readable:
            raise io.UnsupportedOperation(f"{self.name} is not open for reading")

    def _check_writable(self) -> None:
        """The errors the builtin raises for a closed or read-only file."""
        if self.closed:
            raise ValueError("I/O operation on closed file")
        if not self._writable:
            raise io.UnsupportedOperation(f"{self.name} is not open for writing")

    def __repr__(self) -> str:
        """URL, mode and position, for debugging."""
        return f"XRootDRawIO({self.name!r}, mode={self._mode!r}, pos={self._pos})"


def _flat(data: ReadableBuffer) -> memoryview:
    """``data`` as a flat run of bytes, copied only if it is not one already.

    A buffered writer hands over a view of its own buffer, and a large write
    a view of the caller's; both go to the wire from where they are.
    """
    view = memoryview(data)
    try:
        return view.cast("B")
    except TypeError:  # not C-contiguous: a strided view has to be gathered
        return memoryview(view.tobytes())
