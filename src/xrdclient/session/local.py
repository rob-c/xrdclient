"""A file a server redirected to local disk, served without a server.

XRootD can answer an open with ``kXR_redirect`` to ``file://localhost/path``:
"this file is on a disk you can read yourself". XCache does it for a file
already in a cache on the same node, and disk-local deployments do it for
storage mounted on the worker. XrdCl follows such a redirect with its
``LocalFileHandler``, reading the file through the operating system instead
of through the daemon; this module is that handler.

It is a stand-in for a :class:`~xrdclient.session.sync.Session`, not a
separate file API: a :class:`~xrdclient.session.router.Router` that follows
the redirect puts a :class:`LocalFile` where its session was, and every
request the :class:`~xrdclient.client.File` above it sends - ``kXR_open``,
``kXR_read``, ``kXR_readv``, ``kXR_stat``, ``kXR_close`` - is answered here,
in the wire format the file already parses, from :func:`os.pread` and
:func:`os.fstat`. The ``File`` API is therefore exactly the one it always
was. What a local file cannot sensibly do is refused by name: writing (a
redirect that turned a remote write into a write on this machine's disk is
not one to honour silently), and the server-side operations - checksums,
attributes, page reads - that have no daemon to run them.

Whether to follow such a redirect at all is :attr:`Config.local_redirect
<xrdclient.Config.local_redirect>`, checked by the router before anything
here is touched; see :func:`target_path` and :func:`check_follow`.
"""

from __future__ import annotations

import errno
import os
import stat as stat_module
import struct
import weakref
from typing import NoReturn

from ..config import Config
from ..errors import (
    ProtocolError,
    UnsupportedError,
    _error_for_status,
    kXR_IOError,
    kXR_isDirectory,
    kXR_NotAuthorized,
    kXR_NotFound,
    kXR_Unsupported,
)
from ..proto import constants as c
from ..proto import requests as r
from ..proto.frames import Request
from .bulk import BulkUnsupported
from .sync import Result

__all__ = ["LocalFile", "check_follow", "target_path"]

#: The one handle a local file hands out. Any four bytes would do: there is
#: one file per :class:`LocalFile`, and the handle is never sent anywhere.
HANDLE = b"\x00\x00\x00\x01"

#: Open options that would write. A redirect is followed only for reading.
_WRITES = c.kXR_delete | c.kXR_new | c.kXR_open_updt | c.kXR_open_apnd | c.kXR_open_wrto

#: A ``kXR_readv`` reply's per-segment header: handle, length, offset.
_SEGMENT = struct.Struct(">4siq")

#: What an :class:`OSError` from the local filesystem is, as an XRootD error.
_ERRNO_CODES = {
    errno.ENOENT: kXR_NotFound,
    errno.EACCES: kXR_NotAuthorized,
    errno.EPERM: kXR_NotAuthorized,
    errno.EISDIR: kXR_isDirectory,
}


def target_path(url: str) -> str | None:
    """The local path a redirect's ``file://`` URL names, or ``None`` if it is not one.

    Only a URL for *this* host is a local file: no host at all, or
    ``localhost`` - what XrdCl's ``URL::IsLocalFile`` accepts. Anything after
    a ``?`` is CGI for a daemon there is none of, and is dropped.
    """
    scheme, sep, rest = url.partition("://")
    if not sep or scheme.lower() != "file":
        return None
    rest = rest.partition("?")[0]
    if rest.lower().startswith("localhost/"):
        rest = rest[len("localhost") :]
    if not rest.startswith("/"):
        raise ProtocolError(f"redirected to {url}, a file on another host")
    return rest


def check_follow(url: str, config: Config, peer_is_local: bool) -> None:
    """Refuse a ``file://`` redirect the configuration does not allow.

    ``"never"`` refuses every one. ``"local"`` - the default - refuses one
    from a server that is not on this machine: only a server running here
    can know what a local path holds, and one elsewhere that could make this
    client read any local file could have a copy job upload it anywhere.
    ``"always"`` follows them all, as XrdCl does.
    """
    policy = config.local_redirect.lower()
    if policy == "always" or (policy == "local" and peer_is_local):
        return
    why = (
        "local_redirect is 'never'"
        if policy == "never"
        else "the server that sent it is not on this host (set local_redirect='always' "
        "to follow one from anywhere)"
    )
    raise ProtocolError(f"refusing the redirect to {url}: {why}")


def _stat_line(info: os.stat_result) -> bytes:
    """``info`` as the ``id size flags mtime`` line a ``kXR_stat`` answers with."""
    flags = 0
    if stat_module.S_ISDIR(info.st_mode):
        flags |= c.kXR_isDir
    elif not stat_module.S_ISREG(info.st_mode):
        flags |= c.kXR_other
    if info.st_mode & 0o444:
        flags |= c.kXR_readable
    if info.st_mode & 0o222:
        flags |= c.kXR_writable
    if info.st_mode & 0o111:
        flags |= c.kXR_xset
    ident = (info.st_dev << 32) ^ info.st_ino
    return f"{ident} {info.st_size} {flags} {int(info.st_mtime)}".encode() + b"\x00"


def _refuse(error: OSError, path: str) -> NoReturn:
    """Raise ``error`` as the server error the same failure would have been."""
    code = _ERRNO_CODES.get(error.errno or 0, kXR_IOError)
    raise _error_for_status(code, error.strerror or str(error), path=path)


class LocalFile:
    """One local file, answering the requests a :class:`~xrdclient.client.File` sends.

    Opened lazily by the ``kXR_open`` it is asked to answer, read-only, and
    closed by ``kXR_close`` or :meth:`close` - or by the collector, since a
    router that is dropped unclosed must not leak the descriptor either.
    """

    #: Never put in the connection pool: it is not a connection.
    poolable = False
    protocol = None
    handshake_version = 0

    def __init__(self, path: str, config: Config) -> None:
        """Remember ``path``; nothing is opened until the ``kXR_open`` arrives.

        ``path`` comes from :func:`target_path`, after :func:`check_follow`
        has allowed it. ``config`` is kept because callers read a session's.
        """
        self.path = path
        self.config = config
        self._fd: int | None = None
        self._finalizer: weakref.finalize[..., LocalFile] | None = None
        self._done = False

    # -- what a session is asked about --------------------------------------

    @property
    def data_paths(self) -> list[int]:
        """Always empty: a local file has no data streams to bind."""
        return []

    @property
    def endpoint(self) -> str:
        """The ``file://`` URL, used where a session would give ``host:port``."""
        return f"file://localhost{self.path}"

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` (or a ``kXR_close``) has run."""
        return self._done

    @property
    def broken(self) -> bool:
        """Never: with no wire there is nothing to fall out of step."""
        return False

    def ask_arrival_routing(self) -> bool:
        """``False``: there are no data paths for a request to arrive on."""
        return False

    def bind_data_path(self) -> int:
        """Refused: ``kXR_bind`` needs a daemon; callers fall back to unbound reads."""
        raise UnsupportedError(kXR_Unsupported, f"{self.endpoint} is a local file: no data paths")

    def bulk(self, handle: bytes, *, chunk: int, depth: int) -> None:
        """No bulk data plane: the ordinary reads are already a ``pread`` each."""
        raise BulkUnsupported(f"{self.endpoint} is read from local disk")

    def mark_broken(self) -> None:
        """Nothing to mark: a local file has no wire to fall out of step."""

    # -- requests ------------------------------------------------------------

    def execute(self, request: Request, **_: object) -> Result:
        """Answer ``request`` from the local file, as a server would have."""
        if isinstance(request, r.Open):
            return self._open(request)
        if isinstance(request, r.Stat) and type(request) is r.Stat:
            return self._stat(request)
        if isinstance(request, r.Read):
            return Result(self._pread(request.offset, request.length))
        if isinstance(request, r.ReadV):
            return Result(self._readv(request.chunks))
        if isinstance(request, r.Close):
            self.close()
            return Result(b"")
        raise UnsupportedError(
            kXR_Unsupported,
            f"{type(request).__name__} is not available on {self.endpoint}: a file a server "
            "redirected to local disk is read through the operating system, and only "
            "open, read, vector read, stat and close are served",
        )

    def _open(self, request: r.Open) -> Result:
        """Answer ``kXR_open``: open read-only, and append a stat line if asked.

        Any write option is refused rather than downgraded. A repeat open
        reuses the descriptor - the router re-sends ``kXR_open`` after a
        recovery, and one file per :class:`LocalFile` is all there is. With
        ``kXR_retstat`` the body is laid out as the server's: the 4-byte
        handle, 8 bytes of compression info (always zero here), then the
        ``kXR_stat`` text line.
        """
        if request.options & _WRITES:
            raise UnsupportedError(
                kXR_Unsupported,
                f"the server redirected this open to the local file {self.path}, and "
                "writing through a file:// redirect is not supported",
            )
        if self._fd is None:
            try:
                fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            except OSError as exc:
                _refuse(exc, self.path)
            self._fd = fd
            self._finalizer = weakref.finalize(self, os.close, fd)
        body = HANDLE
        if request.options & c.kXR_retstat:
            body += bytes(8) + _stat_line(os.fstat(self._fd))
        return Result(body)

    def _stat(self, request: r.Stat) -> Result:
        """Answer ``kXR_stat``: ``fstat`` the open file, else ``stat`` the path."""
        try:
            info = os.fstat(self._fd) if self._fd is not None else os.stat(self.path)
        except OSError as exc:
            _refuse(exc, self.path)
        return Result(_stat_line(info))

    def _descriptor(self) -> int:
        """The open descriptor; a read before ``kXR_open`` is refused, as a server would."""
        if self._fd is None:
            raise UnsupportedError(kXR_Unsupported, f"{self.endpoint} is not open")
        return self._fd

    def _pread(self, offset: int, length: int) -> bytes:
        """Answer ``kXR_read`` with one ``pread``; short at end of file, as on the wire.

        ``pread`` keeps no file position, so concurrent reads from several
        threads need no lock.
        """
        fd = self._descriptor()
        try:
            return os.pread(fd, length, offset)
        except OSError as exc:
            _refuse(exc, self.path)

    def _readv(self, chunks: list[tuple[bytes, int, int]]) -> bytes:
        """Answer ``kXR_readv``: each segment's 16-byte header, then its bytes.

        The header carries the *actual* length read, so a segment cut short
        by end of file is reported the way a server reports it, and the
        vector-read parser above handles both identically.
        """
        parts: list[bytes] = []
        for _, offset, length in chunks:
            data = self._pread(offset, length)
            parts += (_SEGMENT.pack(HANDLE, len(data), offset), data)
        return b"".join(parts)

    def close(self) -> None:
        """Close the descriptor. Idempotent."""
        self._done = True
        finalizer, self._finalizer, self._fd = self._finalizer, None, None
        if finalizer is not None:
            finalizer()

    def __repr__(self) -> str:
        """Path and whether it is still open."""
        return f"LocalFile({self.path!r}, {'closed' if self._done else 'open'})"
