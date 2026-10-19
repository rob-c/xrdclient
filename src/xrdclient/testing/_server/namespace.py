"""Namespace handlers of the fake XRootD server.

Everything that asks about or changes *names* rather than file contents:
stat and statx, directory listings (with the ``kXR_dstat``/``kXR_dcksm``
forms), mkdir, rm, rmdir, mv, chmod and truncate, plus the vendor
extensions ``xrdfs.ext`` advertises - symlink, setattr, hard link and
readlink. They work on :class:`~xrdclient.testing.server.FakeServer`'s
``files``/``dirs``/``links`` maps through the connection's ``_path``,
``_stat_line`` and ``_children`` helpers. Registered by opcode in
``xrdclient.testing.server._HANDLERS``.

Error codes follow what stock xrootd maps each POSIX failure to - ``ENOENT``
to ``kXR_NotFound`` (3011), ``EEXIST`` and ``ENOTEMPTY`` to ``kXR_ItExists``
(3018), ``EISDIR`` to ``kXR_isDirectory`` (3016), ``ENOTDIR`` to
``kXR_FSError`` (3005) - because the client's error mapping, and gfal2's
errno compatibility above it, are tested against these numbers.
"""

from __future__ import annotations

import posixpath
import struct
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from ...proto import constants as c
from .wire import _clean, _error, _frame, _NotFound

if TYPE_CHECKING:
    from ..server import _Connection


def _h_stat(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_stat`` by path or open handle: ``"<id> <size> <flags> <mtime>"``.

    With ``kXR_vfs`` it is a filesystem question instead, answered with a
    fixed six-field space line (read-write nodes, free MB, utilisation %,
    then the same three for staging nodes). ``kXR_statNoFollow`` describes
    a symbolic link itself rather than its target.
    """
    if params[0] & c.kXR_vfs:
        yield _frame(sid, c.kXR_ok, b"1 1000000 50 1 500000 20\x00")
        return
    path = conn._path(params, body)
    follow = not params[0] & c.kXR_statNoFollow
    yield _frame(sid, c.kXR_ok, conn._stat_line(path, follow=follow) + b"\x00")


def _h_statx(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_statx``: one flag byte per newline-separated path, in request order.

    A path that does not exist is not an error here - it answers
    ``kXR_other`` - because the request is a batch and one missing name
    must not lose the answers for the rest.
    """
    out = bytearray()
    for raw in body.split(b"\x00", 1)[0].decode().split("\n"):
        path = _clean(raw)
        if path in conn.s.dirs:
            out.append(c.kXR_isDir)
        elif path in conn.s.files:
            offline = c.kXR_offline if path in conn.s.nearline else 0
            out.append(c.kXR_readable | c.kXR_writable | offline)
        else:
            out.append(c.kXR_other)
    yield _frame(sid, c.kXR_ok, bytes(out))


def _h_dirlist(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_dirlist``: names, or with ``kXR_dstat`` name/stat-line pairs.

    The plain form is newline-separated names. The ``kXR_dstat`` form opens
    with a ``"."`` entry for the directory itself - which is how a client
    recognises that the server honoured the flag - then alternates name and
    stat line; ``kXR_dcksm`` appends ``[ <algorithm>:<value> ]`` to each
    line. The options live in ``params[15]``. Everything goes in one
    response; :attr:`FakeServer.chunk_reads` only splits reads.
    """
    # Through the facade, at call time: tests patch ``_checksum`` there.
    from .. import server

    args = body.split(b"\x00", 1)[0].decode()
    path = _clean(args)
    path = conn.s.links.get(path, path)
    names = conn._children(path)
    if not params[15] & c.kXR_dstat:
        yield _frame(sid, c.kXR_ok, "\n".join(names).encode() + b"\x00")
        return
    algorithm = ""
    if params[15] & c.kXR_dcksm:
        query = parse_qs(args.partition("?")[2])
        algorithm = query.get("cks.type", ["adler32"])[0]
    out = bytearray(b".\n" + conn._stat_line(path) + b"\n")
    for name in names:
        full = posixpath.join(path, name)
        try:
            line = conn._stat_line(full)
        except _NotFound:
            # A dangling link has nothing to describe, and stock xrootd
            # leaves it out of a stat'ed listing rather than failing it.
            continue
        if algorithm:
            # A directory has nothing to digest, and the server says so in the
            # token rather than leaving it out.
            value = (
                server._checksum(algorithm, bytes(conn.s.files[full]))
                if full in conn.s.files
                else "none"
            )
            line += f" [ {algorithm}:{value} ]".encode()
        out += name.encode() + b"\n" + line + b"\n"
    yield _frame(sid, c.kXR_ok, bytes(out) + b"\x00")


def _h_mkdir(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_mkdir``; ``kXR_mkdirpath`` creates missing parents, as ``mkdir -p`` does.

    An existing directory is ``kXR_ItExists`` even with ``kXR_mkdirpath``,
    so a client that wants ``-p``'s "already there is fine" must say so
    itself.
    """
    path = _clean(body.split(b"\x00", 1)[0].decode())
    if path in conn.s.dirs or path in conn.s.files:
        yield _error(sid, 3018, f"already exists: {path}")
        return
    parent = posixpath.dirname(path)
    if not params[0] & c.kXR_mkdirpath and parent not in conn.s.dirs:
        yield _error(sid, 3011, f"no such file or directory: {parent}")
        return
    conn.s.add_dir(path)
    yield _frame(sid, c.kXR_ok)


def _h_rm(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_rm``: unlink a file or a link, never a directory; the file's xattrs go with it."""
    path = _clean(body.split(b"\x00", 1)[0].decode())
    if path in conn.s.links:
        # unlink(2) on a link removes the link, never what it points at.
        del conn.s.links[path]
        yield _frame(sid, c.kXR_ok)
        return
    if path in conn.s.dirs:
        yield _error(sid, 3016, f"is a directory: {path}")
        return
    conn._file(path)
    del conn.s.files[path]
    conn.s.xattrs.pop(path, None)
    yield _frame(sid, c.kXR_ok)


def _h_rmdir(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_rmdir``: remove an empty directory, with rmdir(2)'s refusals."""
    path = _clean(body.split(b"\x00", 1)[0].decode())
    if path in conn.s.links:
        # rmdir(2) never follows a link, so it is not a directory; stock
        # xrootd reports ENOTDIR as kXR_FSError.
        yield _error(sid, 3005, f"not a directory: {path}")
        return
    if path not in conn.s.dirs:
        raise _NotFound(path)
    if conn._children(path):
        # ENOTEMPTY, which stock xrootd reports as kXR_ItExists.
        yield _error(sid, 3018, f"directory not empty: {path}")
        return
    conn.s.dirs.discard(path)
    yield _frame(sid, c.kXR_ok)


def _h_mv(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_mv``: rename a *file*, replacing any file at the destination.

    Only files move: a directory source is ``kXR_NotFound``, which keeps the
    fake small and has never been needed - client code that renames trees
    does so through a real server in the interop suite. The destination's
    parents are created, more forgiving than xrootd, so tests need not
    arrange them.
    """
    src, dst = _two_paths(params, body)
    conn.s.files[dst] = conn._file(src)
    del conn.s.files[src]
    conn.s.add_dir(posixpath.dirname(dst))
    yield _frame(sid, c.kXR_ok)


def _h_chmod(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_chmod``: record the ``kXR_ur``.. mode bits from ``params[14:16]`` in ``modes``.

    The mode is stored, not enforced: this server authorises everything,
    and stat lines do not carry the mode, so ``modes`` is where a test looks.
    """
    path = _clean(body.split(b"\x00", 1)[0].decode())
    if path not in conn.s.files and path not in conn.s.dirs:
        raise _NotFound(path)
    conn.s.modes[path] = struct.unpack(">H", params[14:16])[0]
    yield _frame(sid, c.kXR_ok)


def _h_truncate(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_truncate`` by path or by open handle (``params[0:4]``) to ``params[4:12]`` bytes.

    Growing pads with zeros, as ftruncate(2) does. The handle form is what
    ``File.truncate`` sends; the path form is ``FileSystem.truncate``.
    """
    size = struct.unpack(">q", params[4:12])[0]
    path = conn._path(params, body, at=slice(0, 4))
    data = conn._file(path)
    if size < len(data):
        del data[size:]
    else:
        data.extend(bytes(size - len(data)))
    yield _frame(sid, c.kXR_ok)


def _h_symlink(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """The ``symlink`` vendor extension: ``"<target> <link>"``, the target required to exist.

    Requiring the target is stricter than symlink(2); dangling links are
    made in tests by deleting the target afterwards, which keeps a typo in
    a test from silently creating one.
    """
    target, link = _two_paths(params, body)
    if target not in conn.s.files and target not in conn.s.dirs:
        raise _NotFound(target)
    conn.s.links[link] = target
    yield _frame(sid, c.kXR_ok)


def _h_setattr(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """The ``setattr`` vendor extension: times and/or ownership, as utimensat/chown do.

    The body is ``flags[4] atime_s[8] atime_ns[8] mtime_s[8] mtime_ns[8]
    uid[4] gid[4]`` then the path. ``flags`` selects which halves apply, and
    a nanosecond field may carry utimensat's ``UTIME_OMIT``/``UTIME_NOW``
    sentinels in place of a value.
    """
    flags, at_s, at_ns, mt_s, mt_ns, uid, gid = struct.unpack(">iqqqqii", body[:44])
    path = _clean(body[44:].split(b"\x00", 1)[0].decode())
    if path not in conn.s.files and path not in conn.s.dirs and path not in conn.s.links:
        raise _NotFound(path)
    if flags & c.kXR_sa_times:
        was = conn.s.times.get(path, (0, 1700000000 * 10**9))
        kept = []
        for (s, ns), before in zip(((at_s, at_ns), (mt_s, mt_ns)), was, strict=True):
            if ns == c.UTIME_OMIT:
                kept.append(before)
            elif ns == c.UTIME_NOW:
                # The server's clock, not the client's, which is the whole
                # point of having a value that means "now".
                kept.append(int(time.time() * 10**9))
            else:
                kept.append(s * 10**9 + ns)
        conn.s.times[path] = (kept[0], kept[1])
    if flags & c.kXR_sa_owner:
        conn.s.owners[path] = (uid, gid)
    yield _frame(sid, c.kXR_ok)


def _h_link(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """The ``link`` vendor extension: a hard link, ``"<existing> <new>"``."""
    src, dst = _two_paths(params, body)
    # A hard link is the same bytes under a second name, and a bytearray is
    # shared by reference - which is exactly the semantics wanted.
    conn.s.files[dst] = conn._file(src)
    conn.s.add_dir(posixpath.dirname(dst))
    yield _frame(sid, c.kXR_ok)


def _h_readlink(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """The ``readlink`` vendor extension: the target text, unresolved, as readlink(2) gives it."""
    path = _clean(body.split(b"\x00", 1)[0].decode())
    try:
        target = conn.s.links[path]
    except KeyError:
        raise _NotFound(path, 3011, "not a symbolic link") from None
    yield _frame(sid, c.kXR_ok, target.encode() + b"\x00")


def _two_paths(params: bytes, body: bytes) -> tuple[str, str]:
    """``"<first> <second>"``, split at the ``arg1len`` the params carry."""
    split = struct.unpack(">H", params[14:16])[0]
    text = body.split(b"\x00", 1)[0].decode()
    return _clean(text[:split]), _clean(text[split + 1 :])
