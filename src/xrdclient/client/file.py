"""An open remote file.

:class:`File` is the low-level handle: every method is one protocol
operation, with no buffering and no implicit position. The file-like objects
in :mod:`xrdclient.io` are built on top of it, and that is what most code should
use. Reach for :class:`File` when you want vector reads, paged I/O with
per-page checksums, extended attributes on a handle, or checkpoints.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from ..errors import ChecksumMismatchError, UnsupportedError, kXR_Unsupported
from ..flags import ChkPointCode
from ..proto import requests as r
from ..proto import responses as rp
from ..types import CheckpointInfo
from . import _fattr
from ._handle import _NOTHING_AHEAD as _NOTHING_AHEAD
from ._handle import _Landing as _Landing
from ._handle import _transient as _transient
from ._reading import _Reading
from ._vector import CLONE_MAX_RANGES as CLONE_MAX_RANGES
from ._vector import READV_MAX_BYTES as READV_MAX_BYTES
from ._vector import READV_MAX_CHUNKS as READV_MAX_CHUNKS
from ._vector import READV_MAX_ELEMENT as READV_MAX_ELEMENT
from ._vector import WRITEV_MAX_ELEMENT as WRITEV_MAX_ELEMENT
from ._vector import _batches as _batches
from ._vector import _pieces as _pieces
from ._vector import _write_batches as _write_batches
from ._writing import _Writing

__all__ = ["Checkpoint", "File"]


class File(_Reading, _Writing):
    """One open file handle on one data server.

    The lifecycle - open, re-open, close, abort - is
    :class:`~xrdclient.client._handle._Handle`'s; the reads and writes are
    those of its two halves. What is left here is what belongs to the file
    as a whole: its extended attributes, checkpoints, and verification.
    """

    # ------------------------------------------------------------------
    # Extended attributes on the handle
    # ------------------------------------------------------------------

    def getxattr(self, name: str) -> bytes:
        """One attribute's value; :class:`~xrdclient.errors.AttrNotFoundError` if absent.

        The same error :meth:`FileSystem.getxattr` raises, so a caller need
        not know whether it asked a path or an open handle.
        """
        result = self._execute(lambda handle: r.Fattr.get("", name, fhandle=handle))
        for item in rp.parse_fattr(result.data).items:
            if item.code == 0 and item.value is not None:
                return item.value
        raise _fattr.missing(name, self.url.path)

    def setxattr(self, name: str, value: bytes) -> None:
        """Set one attribute, raising if the server refused it."""
        res = self._router.execute(
            r.Fattr.set("", name, value, fhandle=self.handle), path=self.url.path
        )
        _fattr.check(rp.parse_fattr(res.data, values=False), self.url.path)

    def listxattr(self) -> list[str]:
        """Names of every extended attribute on the open file (``kXR_fattr`` list)."""
        result = self._execute(lambda handle: r.Fattr.list("", fhandle=handle))
        return [i.name for i in rp.parse_fattr_list(result.data).items]

    def removexattr(self, name: str) -> None:
        """Remove one attribute, raising if the server refused - one absent, say."""
        res = self._router.execute(
            r.Fattr.delete("", name, fhandle=self.handle), path=self.url.path
        )
        _fattr.check(rp.parse_fattr(res.data, values=False), self.url.path)

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------

    @contextmanager
    def checkpoint(self) -> Iterator[Checkpoint]:
        """Transactional writes: commit on clean exit, roll back on error.

            >>> with fh.checkpoint() as cp:
            ...     fh.write(payload, offset)
            ...     cp.query().free
            65536

        Every :meth:`write`, :meth:`pgwrite` and :meth:`truncate` inside the
        block goes to the server as ``kXR_ckpXeq`` and so is undone by the
        rollback; :meth:`writev` is not one of the three the server can undo
        and refuses. Requires server-side ``kXR_chkpoint`` support; a server
        without it raises :class:`~xrdclient.errors.UnsupportedError` on entry,
        before any write has happened.

        Checkpoints do not nest - the server keeps one per handle.
        """
        if self._checkpoint:
            raise UnsupportedError(kXR_Unsupported, f"{self.url} already has a checkpoint open")
        self._router.execute(r.ChkPoint(self.handle, int(ChkPointCode.BEGIN)))
        self._checkpoint = True
        try:
            yield Checkpoint(self)
        except BaseException:
            self._checkpoint = False
            self._router.execute(r.ChkPoint(self.handle, int(ChkPointCode.ROLLBACK)))
            self._stat = None  # the file is back to a size this handle never saw
            raise
        else:
            self._checkpoint = False
            self._router.execute(r.ChkPoint(self.handle, int(ChkPointCode.COMMIT)))

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify(self, expected: str, algorithm: str = "adler32") -> None:
        """Compare the server's checksum against ``expected``."""
        actual = self.checksum(algorithm)
        if actual.value.lower() != expected.lower():
            raise ChecksumMismatchError(algorithm, expected.lower(), actual.value.lower())


class Checkpoint:
    """The checkpoint a :meth:`File.checkpoint` block is writing into.

    There is nothing to do with it in the common case - the writes go through
    the file as usual - but the server will only journal so much, and this is
    where you ask how much of that is left.
    """

    __slots__ = ("file",)

    def __init__(self, file: File) -> None:
        """Bound to ``file``, whose handle must stay open for the checkpoint's life."""
        self.file = file

    def query(self) -> CheckpointInfo:
        """``kXR_ckpQuery`` - how much room the journal has left."""
        result = self.file._router.execute(
            r.ChkPoint(self.file.handle, int(ChkPointCode.QUERY)),
            path=self.file.url.path,
        )
        return rp.parse_checkpoint(result.data)

    def __repr__(self) -> str:
        """``Checkpoint('<url>')``, naming the file being journalled."""
        return f"Checkpoint({str(self.file.url)!r})"
