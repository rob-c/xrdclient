"""Namespace request encoders: asking about paths and changing them.

Part of :mod:`xrdclient.proto.requests`, which re-exports every class here;
import them from there. Everything in this module addresses a *path* in the
server's namespace - the read side (``kXR_stat`` and its ``kXR_vfs`` form,
``kXR_statx``, ``kXR_dirlist``, ``kXR_locate``) and the mutations
(``kXR_mkdir``, ``kXR_rm``/``kXR_rmdir``, ``kXR_mv`` and the link requests
built on its layout, ``kXR_readlink``, ``kXR_setattr``/``kXR_chmod``,
``kXR_truncate``). ``Stat`` and ``Truncate`` also accept an open file's handle,
but they are namespace operations on the wire and in XrdCl, so they live here
rather than with file I/O. Server queries and staging are in
``_requests_query``, open-file traffic in ``_requests_io``.

Layouts follow nginx-xrootd ``wire_core_requests.h``; cross-checked against
go-hep ``xrootd/xrdproto/<op>`` and XRootD.jl ``Wire/requests.jl``. Every
``params`` method writes exactly 16 bytes (header offsets 4..19).

Paths go in the body unterminated - ``dlen`` delimits them - and may carry
``?cgi`` opaque data, which the server strips before the namespace sees the
path. Every mutation here is ``idempotent = False``: replaying a ``kXR_rm``
after a dropped link would turn a success into ``kXR_NotFound``, and a
replayed ``kXR_mv`` can move a file a second client created meanwhile. The
read side may be replayed freely. The hook contract is documented on
:class:`~xrdclient.proto.frames.Request`.
"""

from __future__ import annotations

from collections.abc import Sequence

from . import constants as c
from ._requests_base import _STAT_PARAMS, _encode_path, _handle
from .buffer import Writer
from .frames import Request


class Stat(Request):
    """``kXR_stat``: size, flags, mtime and id of one path or open file.

    With a handle and an empty path the server stats the open file itself,
    as XrdCl's ``File::Stat`` does - the answer then holds even if the path
    has been renamed or removed since the open. Stat is the request every
    existence check sends, hence the single-pack :meth:`header_params`.
    """

    __slots__ = ("fhandle", "options", "path")
    opcode = c.kXR_stat

    def __init__(self, path: str = "", options: int = 0, fhandle: bytes = c.NULL_FHANDLE) -> None:
        """Stat ``path``, or the open file ``fhandle`` names when ``path`` is empty."""
        self.path = path
        self.options = options
        self.fhandle = fhandle

    def params(self, w: Writer) -> None:
        """``options[1] reserved[11] fhandle[4]``; ``kXR_vfs`` is the only option."""
        w.u8(self.options).zeros(11).padded(self.fhandle, 4)

    def header_params(self) -> bytes:
        """The same 16 bytes from one ``struct`` pack."""
        return _STAT_PARAMS.pack(self.options, _handle(self.fhandle))

    def payload(self) -> bytes:
        """The path; empty when the handle addresses the file."""
        return _encode_path(self.path)

    def __repr__(self) -> str:
        """Type, path and options; the handle is per connection and says nothing."""
        # By type, not by name: ``StatVFS`` is a ``Stat`` with an option set,
        # and a log line saying so is the point of the repr.
        return f"{type(self).__name__}(path={self.path!r}, options=0x{self.options:02x})"


class StatVFS(Stat):
    """``kXR_stat`` with ``kXR_vfs`` - filesystem space.

    The reply is a different text body (node counts and free space, parsed
    by ``parse_statvfs``), so this is a type of its own rather than a flag a
    caller might forget to check when parsing.
    """

    __slots__ = ()

    def __init__(self, path: str) -> None:
        """Ask for the space of the filesystem holding ``path``."""
        super().__init__(path, options=c.kXR_vfs)


class Statx(Request):
    """``kXR_statx`` - flag-only stat of many paths at once.

    The parameter area is all reserved; the reply is one ``kXR_*`` flag byte
    per path, in request order, so the caller pairs them up by position.
    """

    __slots__ = ("paths",)
    opcode = c.kXR_statx

    def __init__(self, paths: Sequence[str]) -> None:
        """Stat every path in ``paths``; none may contain a newline."""
        self.paths = list(paths)

    def payload(self) -> bytes:
        """The paths joined by ``\\n``, the separator the spec defines."""
        return "\n".join(self.paths).encode("utf-8")

    def __repr__(self) -> str:
        """Every path - a statx is small enough that the list is the useful part."""
        return f"Statx(paths={self.paths!r})"


class Dirlist(Request):
    """``kXR_dirlist``: the names in a directory, optionally with stat data.

    Large listings come back as several ``kXR_oksofar`` chunks; the state
    machine joins them and ``parse_dirlist`` splits the result.
    """

    __slots__ = ("options", "path")
    opcode = c.kXR_dirlist

    def __init__(self, path: str, options: int | None = None) -> None:
        """List ``path``; ``options`` defaults to ``kXR_dstat``.

        ``kXR_dstat`` makes the server send each entry's stat line beside its
        name, as XrdCl's ``DirListFlags::Stat`` does - without it a listing
        that needs sizes costs one ``kXR_stat`` per entry. Pass ``0`` for
        bare names; ``kXR_dcksm`` adds checksums on servers that support it.
        """
        self.path = path
        self.options = c.kXR_dstat if options is None else options

    def params(self, w: Writer) -> None:
        """``reserved[15] options[1]``."""
        w.zeros(15).u8(self.options)

    def payload(self) -> bytes:
        """The directory path."""
        return _encode_path(self.path)

    def __repr__(self) -> str:
        """Path and options."""
        return f"Dirlist(path={self.path!r}, options=0x{self.options:02x})"


class Locate(Request):
    """``kXR_locate``: which servers hold a path, as a redirector sees it.

    The reply lists the next hop only (managers and servers); a deep locate,
    which follows managers down to data servers, is the client's recursion,
    not the protocol's.
    """

    __slots__ = ("options", "path")
    opcode = c.kXR_locate

    def __init__(self, path: str, options: int = 0) -> None:
        """Locate ``path`` with ``kXR_*Loc`` option bits (refresh, nowait, prefname)."""
        self.path = path
        self.options = options

    def params(self, w: Writer) -> None:
        """``options[2] reserved[14]``."""
        w.u16(self.options).zeros(14)

    def payload(self) -> bytes:
        """The path to locate."""
        return _encode_path(self.path)

    def __repr__(self) -> str:
        """Path and options."""
        return f"Locate(path={self.path!r}, options=0x{self.options:04x})"


class Mkdir(Request):
    """``kXR_mkdir``.

    Not idempotent even though "create if absent" sounds it: without
    ``mkpath`` a replay after a lost success answers ``kXR_ItExists``,
    turning success into failure.
    """

    __slots__ = ("mkpath", "mode", "path")
    opcode = c.kXR_mkdir
    signed = True
    idempotent = False

    def __init__(self, path: str, mode: int = 0o755, mkpath: bool = False) -> None:
        """Make ``path``; ``mkpath`` creates missing parents too (``mkdir -p``)."""
        self.path = path
        self.mode = mode
        self.mkpath = mkpath

    def params(self, w: Writer) -> None:
        """``options[1] reserved[13] mode[2]``; ``kXR_mkdirpath`` is the one option."""
        w.u8(c.kXR_mkdirpath if self.mkpath else 0).zeros(13).u16(self.mode)

    def payload(self) -> bytes:
        """The directory path."""
        return _encode_path(self.path)

    def __repr__(self) -> str:
        """Path, octal mode and whether parents are made."""
        return f"Mkdir(path={self.path!r}, mode=0o{self.mode:o}, mkpath={self.mkpath})"


class Rm(Request):
    """``kXR_rm``: remove a file. The parameter area is all reserved."""

    __slots__ = ("path",)
    opcode = c.kXR_rm
    signed = True
    idempotent = False

    def __init__(self, path: str) -> None:
        """Remove ``path``."""
        self.path = path

    def payload(self) -> bytes:
        """The path to remove."""
        return _encode_path(self.path)

    def __repr__(self) -> str:
        """Type and path."""
        return f"{type(self).__name__}(path={self.path!r})"  # ``Rmdir`` is one of these


class Rmdir(Rm):
    """``kXR_rmdir``: identical framing to ``kXR_rm``, only the opcode differs."""

    __slots__ = ()
    opcode = c.kXR_rmdir


class Mv(Request):
    """``kXR_mv`` - the payload is ``"<src> <dst>"``, split by ``arg1len``."""

    __slots__ = ("dst", "src")
    opcode = c.kXR_mv
    signed = True
    idempotent = False

    def __init__(self, src: str, dst: str) -> None:
        """Rename ``src`` to ``dst``; both may contain spaces, ``arg1len`` disambiguates."""
        self.src = src
        self.dst = dst

    def params(self, w: Writer) -> None:
        """``reserved[14] arg1len[2]`` - the byte length of ``src``.

        Before ``arg1len`` existed servers split at the first space, which
        broke paths containing one; sending the length makes the split exact.
        """
        w.zeros(14).u16(len(self.src.encode("utf-8")))

    def payload(self) -> bytes:
        """``src``, one space, ``dst``."""
        return f"{self.src} {self.dst}".encode()

    def __repr__(self) -> str:
        """Type, source and destination (``Symlink``/``Link`` share it)."""
        return f"{type(self).__name__}(src={self.src!r}, dst={self.dst!r})"


class Symlink(Mv):
    """``kXR_symlink`` (vendor extension) - point ``dst`` at ``src``.

    Framed like ``kXR_mv``: ``"<target> <link>"`` split by ``arg1len``.
    """

    opcode = c.kXR_symlink


class Link(Mv):
    """``kXR_link`` (vendor extension) - hard-link ``dst`` to ``src``."""

    opcode = c.kXR_link


class Readlink(Request):
    """``kXR_readlink`` (vendor extension) - what a symbolic link points at."""

    __slots__ = ("path",)
    opcode = c.kXR_readlink
    signed = True

    def __init__(self, path: str) -> None:
        """Read the target of the link at ``path``."""
        self.path = path

    def payload(self) -> bytes:
        """The link's path; ``parse_readlink`` decodes the reply."""
        return _encode_path(self.path)

    def __repr__(self) -> str:
        """The link's path."""
        return f"Readlink(path={self.path!r})"


class Setattr(Request):
    """``kXR_setattr`` (vendor extension) - timestamps and ownership.

    The body is a 44-byte big-endian block - ``flags``, ``atime`` and ``mtime``
    as ``(seconds, nanoseconds)`` pairs, ``uid``, ``gid`` - followed by the
    path, the same layout XRootD.jl encodes and nginx-xrootd decodes. The
    nanosecond fields carry ``utimensat``'s ``UTIME_NOW`` and ``UTIME_OMIT``,
    which is how "now" and "leave that one alone" are said.
    """

    __slots__ = ("atime", "flags", "gid", "mtime", "path", "uid")
    opcode = c.kXR_setattr
    signed = True
    idempotent = False

    def __init__(
        self,
        path: str,
        flags: int,
        atime: tuple[int, int] = (0, 0),
        mtime: tuple[int, int] = (0, 0),
        owner: tuple[int, int] = (-1, -1),
    ) -> None:
        """One ``kXR_setattr``; ``flags`` says which of the fields the server applies.

        ``owner`` is ``(uid, gid)``, paired as the two times are, with
        ``chown(2)``'s ``-1`` for "leave this one alone" - a zero there would
        hand the file to root. Fields outside ``flags`` are still encoded
        (the block is fixed-size) but ignored by the server.
        """
        self.path = path
        self.flags = flags
        self.atime = atime
        self.mtime = mtime
        self.uid, self.gid = owner

    def payload(self) -> bytes:
        """``flags[4] atime[8+8] mtime[8+8] uid[4] gid[4]`` then ``path\\0``.

        The path is NUL-terminated here, unlike every other path body: that
        is the layout the nginx-xrootd decoder expects after the fixed block.
        """
        w = Writer()
        w.i32(self.flags)
        for seconds, nanoseconds in (self.atime, self.mtime):
            w.i64(seconds).i64(nanoseconds)
        w.i32(self.uid).i32(self.gid)
        return w.bytes() + _encode_path(self.path) + b"\x00"

    def __repr__(self) -> str:
        """Every field: a setattr's log line is only useful with the values it set."""
        return (
            f"Setattr(path={self.path!r}, flags=0x{self.flags:02x}, "
            f"atime={self.atime!r}, mtime={self.mtime!r}, uid={self.uid}, gid={self.gid})"
        )


class Chmod(Request):
    """``kXR_chmod``: set permission bits (``kXR_ur``... encoding, i.e. POSIX octal)."""

    __slots__ = ("mode", "path")
    opcode = c.kXR_chmod
    signed = True
    idempotent = False

    def __init__(self, path: str, mode: int) -> None:
        """Set ``path``'s mode to ``mode``."""
        self.path = path
        self.mode = mode

    def params(self, w: Writer) -> None:
        """``reserved[14] mode[2]``."""
        w.zeros(14).u16(self.mode)

    def payload(self) -> bytes:
        """The path."""
        return _encode_path(self.path)

    def __repr__(self) -> str:
        """Path and octal mode."""
        return f"Chmod(path={self.path!r}, mode=0o{self.mode:o})"


class Truncate(Request):
    """``kXR_truncate`` - by path, or by handle when ``fhandle`` is set."""

    __slots__ = ("fhandle", "path", "size")
    opcode = c.kXR_truncate
    signed = True
    idempotent = False

    def __init__(self, path: str = "", size: int = 0, fhandle: bytes = c.NULL_FHANDLE) -> None:
        """Set the length of ``path`` (or, with an empty path, of ``fhandle``) to ``size``."""
        self.path = path
        self.size = size
        self.fhandle = fhandle

    def params(self, w: Writer) -> None:
        """``fhandle[4] offset[8] reserved[4]``; ``offset`` is the new length."""
        w.padded(self.fhandle, 4).i64(self.size).zeros(4)

    def payload(self) -> bytes:
        """The path, empty when the handle addresses the file."""
        return _encode_path(self.path)

    def __repr__(self) -> str:
        """Path and size."""
        return f"Truncate(path={self.path!r}, size={self.size})"
