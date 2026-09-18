"""A fake XRootD server, backed by a dictionary.

:class:`FakeServer` speaks enough of the binary protocol to exercise a real
client end to end - handshake, login, the namespace, file I/O, vector and
paged I/O, checksums, xattrs - over a loopback socket, with no daemon, no
configuration file and no privileges::

    with FakeServer(files={"/data/a.root": b"hello"}) as server:
        fs = xrdclient.FileSystem(server.url)
        assert fs.read_bytes("/data/a.root") == b"hello"

It is deliberately a *fake*, not a simulator: it stores files in memory and
authorises everything. What it is faithful about is the wire format, which is
the part a client can get wrong. Redirects, waits and chunked responses can be
injected (:attr:`redirects`, :attr:`waits`, :attr:`chunk_reads`) so the
awkward paths get exercised too.

TLS is not implemented; point a client at it with ``root://``, not
``roots://``.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import posixpath
import socket
import socketserver
import struct
import threading
import time
import zlib
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import parse_qs

from .._compat import zip_strict
from ..errors import kXR_ArgInvalid, kXR_FileNotOpen, kXR_NoSpace, kXR_Unsupported
from ..proto import constants as c
from ..proto.buffer import Reader, Writer
from ..url import XRootDURL, parse

__all__ = ["FakeServer", "frame", "error", "pgwrite_cse", "from_directory", "main"]

#: Requests whose body is payload rather than a path, so nothing about them
#: belongs in :attr:`FakeServer.arguments`. The read side is here for the same
#: reason as the write side: a read's body is its path id and its read list,
#: never a name, and decoding one as text invents an argument nobody sent.
_DATA_REQUESTS = frozenset(
    {
        c.kXR_write, c.kXR_writev, c.kXR_clone, c.kXR_pgwrite,
        c.kXR_read, c.kXR_readv, c.kXR_pgread,
        c.kXR_auth, c.kXR_login, c.kXR_sigver,
    }
)

#: What :attr:`FakeServer.handlers` entries look like.
Handler = Callable[["_Connection", int, bytes, bytes], Iterator[bytes]]

_REQ = struct.Struct(">HH16sI")
_RESP = struct.Struct(">HHI")
_NULL_HANDLE = b"\x00\x00\x00\x00"


def frame(streamid: int, status: int, body: bytes = b"") -> bytes:
    """One complete response frame: the 8-byte header and its body.

    Public because :attr:`FakeServer.handlers` is: answering badly means
    building the frame yourself.
    """
    return _RESP.pack(streamid, status, len(body)) + body


def error(streamid: int, code: int, message: str) -> bytes:
    """A ``kXR_error`` frame carrying a server error code and its text."""
    return frame(streamid, c.kXR_error, struct.pack(">i", code) + message.encode() + b"\x00")


def pgwrite_cse(streamid: int, corrupt: Sequence[int]) -> bytes:
    """A ``kXR_pgwrite`` reply naming the pages that arrived corrupt.

    A ``kXR_status`` frame whose trailer is ``cseCRC[4] dlFirst[2] dlLast[2]``
    and then one big-endian ``int64`` file offset per bad page.
    ``dlFirst``/``dlLast`` say how much of the first and last page the request
    covered - a whole page each here, which is what a write of whole pages
    means. Public for the same reason :func:`frame` is.
    """
    trailer = struct.pack(">IHH", 0, c.kXR_pgPageSZ, c.kXR_pgPageSZ) + b"".join(
        struct.pack(">q", off) for off in corrupt
    )
    status = (
        struct.pack(">I", 0)
        + struct.pack(">H", streamid)
        + bytes([c.kXR_pgwrite - c.kXR_1stRequest, c.kXR_FinalResult])
        + bytes(4)
        + struct.pack(">i", len(trailer))
        + struct.pack(">q", 0)
    )
    return frame(streamid, c.kXR_status, status) + trailer


_frame = frame
_error = error


def _clean(path: str) -> str:
    """Strip any CGI and normalise, the way a server resolves a path."""
    base = path.partition("?")[0]
    return posixpath.normpath("/" + base.lstrip("/")) if base else "/"


def _endpoint(server: socketserver.BaseServer) -> tuple[str, int]:
    """The bound ``(host, port)``. These servers are always AF_INET, which is
    narrower than the address family ``socketserver`` is typed for.
    """
    host, port = cast("tuple[str, int]", server.server_address)
    return host, port


@dataclass
class _Checkpoint:
    """One open checkpoint: what to put back, and how much has been journaled."""

    path: str
    snapshot: bytearray = field(default_factory=bytearray)
    used: int = 0


class _NotFound(Exception):
    """Raised inside a handler to produce a ``kXR_NotFound``."""

    def __init__(self, path: str, code: int = 3011, message: str = "no such file or directory"):
        self.path = path
        self.code = code
        self.message = message


class FakeServer:
    """An in-memory XRootD server listening on an ephemeral loopback port."""

    def __init__(
        self,
        *,
        files: dict[str, bytes] | None = None,
        dirs: Iterable[str] = (),
        sec: str = "",
        host: str = "127.0.0.1",
        port: int = 0,
        flags: int = c.kXR_isServer,
        sessid: bytes = b"\x11" * 16,
        version: int = 0x0500_0000,
    ) -> None:
        #: Path to contents. Mutated by the client, readable by the test.
        self.files: dict[str, bytearray] = {}
        #: Directories that exist, including the parents of every file.
        self.dirs: set[str] = {"/"}
        #: Files whose bytes are on tape rather than on disk. They stat as
        #: ``kXR_offline``, which is how a caller learns a read would wait.
        self.nearline: set[str] = set()
        #: Symbolic links: link path to the target it names.
        self.links: dict[str, str] = {}
        #: Extended attributes, per path.
        self.xattrs: dict[str, dict[str, bytes]] = {}
        #: Modes set by ``kXR_chmod``, per path.
        self.modes: dict[str, int] = {}
        #: Times set by ``kXR_setattr``: ``path -> (atime_ns, mtime_ns)``. The
        #: mtime is what a later stat reports, so a test can watch one move.
        self.times: dict[str, tuple[int, int]] = {}
        #: Owners set by ``kXR_setattr``: ``path -> (uid, gid)``. No stat field
        #: carries them, so this is where a test looks.
        self.owners: dict[str, tuple[int, int]] = {}
        #: Values ``kXR_query`` config lookups answer with. ``xrdfs.ext`` is
        #: how a server says which vendor opcodes it implements, and this one
        #: implements all four; the reply repeats the key, as the server that
        #: introduced it does.
        self.config_values: dict[str, str] = {
            "version": "v5.6.0",
            "role": "server",
            "xrdfs.ext": "xrdfs.ext=setattr,symlink,readlink,link",
        }
        #: The ``oss.*`` space token ``kXR_Qspace`` answers with.
        self.space = (
            "oss.cgroup=public&oss.space=2000000&oss.free=1500000"
            "&oss.maxf=1400000&oss.used=500000&oss.quota=-1"
        )
        #: Directives ``kXR_set`` carried, in order.
        self.properties: list[str] = []
        #: Staging requests taken, ``handle -> the paths it named``.
        self.prepared: dict[str, list[str]] = {}
        #: Checksums a client asked the server to stop computing.
        self.cancelled_checksums: list[str] = []
        #: Staging requests withdrawn by handle.
        self.cancelled_prepares: list[str] = []
        #: Paths a ``kXR_prepare`` asked to drop from disk, in order.
        self.evicted: list[str] = []
        #: ``(options byte, optionX)`` of every ``kXR_prepare``, in order.
        self.prepare_options: list[tuple[int, int]] = []
        #: Opcodes seen, in order - what a test asserts round trips on.
        self.seen: list[int] = []
        #: Raw path argument of every ``kXR_open``, CGI included. The opaque
        #: data is the whole protocol for third-party copy, so it is kept.
        self.opened: list[str] = []
        #: ``(opcode, argument)`` for every request that named one, again with
        #: the CGI left on - which is how a test checks that opaque data
        #: reached the operation it was meant for.
        self.arguments: list[tuple[int, str]] = []
        #: ``(offset, length, reqflags)`` of every ``kXR_pgwrite``, in order.
        #: The flags are what tell a retransmission (``kXR_pgRetry``) from a
        #: first attempt.
        self.pgwrites: list[tuple[int, int, int]] = []
        #: ``opcode -> (host, port, token)``, consumed once each.
        self.redirects: dict[int, tuple[str, int, str]] = {}
        #: ``opcode -> count`` of ``kXR_wait`` replies to send first.
        self.waits: dict[int, int] = {}
        #: Split read responses into ``kXR_oksofar`` chunks of this size.
        self.chunk_reads = 0
        #: Rounds of ``kXR_authmore`` to demand before accepting a credential.
        self.auth_rounds = 0
        #: ``file offset -> count`` of ``kXR_pgwrite`` pages to report as having
        #: arrived with a broken CRC, consumed once per report. The data is
        #: stored regardless, as a real server stores it; what the client is
        #: being made to do is come back for the page with ``kXR_pgRetry``.
        self.pgwrite_corrupt: dict[int, int] = {}
        #: ``opcode -> handler`` overrides, tried before the built-in ones.
        #: A handler takes ``(connection, streamid, params, body)`` and yields
        #: raw frames, so it can answer with anything at all - including
        #: something no real server would send.
        self.handlers: dict[int, Handler] = {}
        #: Serve a request that *arrived* on a bound data path, rather than
        #: only the payload and reply of one asked for on the control link.
        #: A gateway that keys its sub-streams on the connection a request came
        #: in on behaves this way; a stock daemon does not, which is why the
        #: default is off. Turn it on only for a client that then routes by
        #: arrival throughout: the standard split would have this connection's
        #: own thread and its owner's both reading the same socket.
        self.serves_arrivals = False

        self._live: set[object] = set()
        self._live_lock = threading.Lock()
        #: Logged-in connections by session id, so ``kXR_bind`` can find the
        #: one a data path is asking to join.
        self._sessions: dict[bytes, _Connection] = {}
        self._parked: set[threading.Event] = set()
        self._sessions_issued = 0

        self.sec = sec
        self.flags = flags
        self.sessid = sessid
        self.version = version

        for path, data in (files or {}).items():
            self.add_file(path, data)
        for path in dirs:
            self.add_dir(path)

        self._wanted = (host, port)
        self._last: tuple[str, int] | None = None
        self._bound: _TCPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Contents
    # ------------------------------------------------------------------

    def add_dir(self, path: str) -> None:
        """Create a directory and every parent of it."""
        target = _clean(path)
        while target not in self.dirs:
            self.dirs.add(target)
            target = posixpath.dirname(target) or "/"

    def add_file(self, path: str, data: bytes = b"") -> None:
        """Create or replace a file, creating its parents."""
        target = _clean(path)
        self.files[target] = bytearray(data)
        self.add_dir(posixpath.dirname(target))

    def contents(self, path: str) -> bytes:
        """What the server currently holds at ``path``."""
        return bytes(self.files[_clean(path)])

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def _server(self) -> _TCPServer:
        """The listening socket, bound on first use.

        Binding lazily means a server built only to hold contents - a
        fixture that a test never connects to - never claims a port.
        """
        if self._bound is None:
            self._bound = _TCPServer(self._wanted, _Handler)
            self._bound.fake = self
        return self._bound

    @property
    def address(self) -> tuple[str, int]:
        """Where the server listens; reading it before ``start`` binds the port.

        After :meth:`stop` it reports where the server *was*, rather than
        quietly claiming a fresh port to answer the question.
        """
        if self._bound is None and self._last is not None:
            return self._last
        return _endpoint(self._server)

    @property
    def url(self) -> XRootDURL:
        """``root://host:port/`` - what a client connects to."""
        host, port = self.address
        return parse(f"root://{host}:{port}/")

    def start(self) -> FakeServer:
        """Serve in a background thread. Idempotent."""
        if self._thread is None:
            # A short poll interval keeps stop() from costing half a second,
            # which is the difference between a fast test suite and a slow one.
            self._thread = threading.Thread(
                target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
            )
            self._thread.start()
        return self

    def next_sessid(self) -> bytes:
        """A fresh session id, distinct per login as a real server's is."""
        with self._live_lock:
            self._sessions_issued += 1
            return self.sessid[:15] + bytes([self._sessions_issued & 0xFF])

    def disconnect(self) -> None:
        """Drop every live connection, as a restarting server would.

        The listening socket stays open, so a client that reconnects gets
        served again - which is exactly the reconnect path worth testing.
        """
        with self._live_lock:
            live, self._live = set(self._live), set()
            parked, self._parked = set(self._parked), set()
        for waiting in parked:
            waiting.set()
        for sock in live:
            try:
                sock.shutdown(socket.SHUT_RDWR)  # type: ignore[attr-defined]
            except OSError:
                pass

    def stop(self) -> None:
        """Stop serving and release the port. Idempotent."""
        if self._bound is None:
            return
        self._last = _endpoint(self._bound)
        if self._thread is not None:
            self._bound.shutdown()
            self._thread = None
        self._bound.server_close()
        self._bound = None
        self.disconnect()

    def __enter__(self) -> FakeServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def __repr__(self) -> str:
        # Reading the address would bind the port; a repr must not do that.
        bound = _endpoint(self._bound) if self._bound else self._last
        where = f"{bound[0]}:{bound[1]}" if bound else "unbound"
        return f"FakeServer({where}, files={len(self.files)}, dirs={len(self.dirs)})"


class _TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    fake: FakeServer


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        _Connection(self.server.fake, self.request).run()  # type: ignore[attr-defined]


class _Connection:
    """One client connection's worth of protocol."""

    def __init__(self, server: FakeServer, sock: object) -> None:
        self.s = server
        self.sock = sock
        self.rfile = sock.makefile("rb")  # type: ignore[attr-defined]
        self.handles: dict[bytes, str] = {}
        self.checkpoints: dict[bytes, _Checkpoint] = {}
        self.next_handle = 1
        self.auth_seen = 0
        self.sessid = b""
        #: Data connections bound to this one, by path id.
        self.paths: dict[int, _Connection] = {}
        self.next_pathid = 1
        #: The connection this one is a data path of, once bound.
        self.bound_to: _Connection | None = None
        #: Which link the reply to the request in hand goes out on.
        self.route = 0
        self.unpark = threading.Event()

    # -- plumbing -------------------------------------------------------

    def run(self) -> None:
        with self.s._live_lock:
            self.s._live.add(self.sock)
        try:
            if len(self.rfile.read(20) or b"") < 20:
                return
            self._send(_frame(0, c.kXR_ok, struct.pack(">ii", 0, c.ROOTD_PQ)))
            while True:
                header = self.rfile.read(c.REQUEST_HDRLEN)
                if not header or len(header) < c.REQUEST_HDRLEN:
                    return
                sid, opcode, params, dlen = _REQ.unpack(header)
                # dlen counts the data even when the data is on a bound path,
                # so where to read it from is decided by the request, not by
                # which socket the header arrived on.
                source = self.paths.get(_data_path(opcode, params), self)
                body = source.rfile.read(dlen) if dlen else b""
                self.s.seen.append(opcode)
                self.route = _reply_path(opcode, params, body)
                for chunk in self._dispatch(sid, opcode, params, body):
                    self._send(chunk)
                if self.bound_to is not None and not self.s.serves_arrivals:
                    # A bound connection is the other end's to read and write:
                    # its own thread must never take a byte off it again.
                    with self.s._live_lock:
                        self.s._parked.add(self.unpark)
                    self.unpark.wait()
                    return
                if opcode == c.kXR_endsess:
                    return
        except (OSError, ValueError):
            pass
        finally:
            with self.s._live_lock:
                self.s._live.discard(self.sock)
                self.s._parked.discard(self.unpark)
                if self.sessid:
                    self.s._sessions.pop(self.sessid, None)
            if self.bound_to is not None:
                self.bound_to.paths.pop(
                    next((k for k, v in self.bound_to.paths.items() if v is self), 0), None
                )
            try:
                self.rfile.close()
                self.sock.close()  # type: ignore[attr-defined]
            except OSError:
                pass

    def _send(self, data: bytes) -> None:
        target = self.paths.get(self.route, self)
        target.sock.sendall(data)  # type: ignore[attr-defined]

    def _dispatch(self, sid: int, opcode: int, params: bytes, body: bytes) -> Iterator[bytes]:
        if opcode == c.kXR_sigver:
            return  # the signature prefixes the next frame; nothing to answer

        target = self.s.redirects.pop(opcode, None)
        if target is not None:
            host, port, token = target
            where = f"{host}{'?' + token if token else ''}".encode()
            yield _frame(sid, c.kXR_redirect, struct.pack(">i", port) + where + b"\x00")
            return

        self._record_argument(opcode, body)

        if self.s.waits.get(opcode, 0):
            self.s.waits[opcode] -= 1
            yield _frame(sid, c.kXR_wait, struct.pack(">i", 0) + b"try again\x00")
            return

        handler = self.s.handlers.get(opcode) or _HANDLERS.get(opcode)
        if handler is None:
            yield _error(sid, 3013, f"request {opcode} is not supported")
            return
        try:
            yield from handler(self, sid, params, body)
        except _NotFound as missing:
            yield _error(sid, missing.code, f"{missing.message}: {missing.path}")

    def _record_argument(self, opcode: int, body: bytes) -> None:
        """Remember the textual argument of a non-data request, when present."""
        if opcode in _DATA_REQUESTS:
            return
        text = body.split(b"\x00", 1)[0].decode("utf-8", "replace")
        if text:
            self.s.arguments.append((opcode, text))

    # -- namespace helpers ----------------------------------------------

    def _path(self, params: bytes, body: bytes, at: slice = slice(12, 16)) -> str:
        """The path a request names, by body or by open handle."""
        text = body.split(b"\x00", 1)[0].decode("utf-8", "replace")
        if text:
            return _clean(text)
        handle = params[at]
        if handle != _NULL_HANDLE and handle in self.handles:
            return self.handles[handle]
        raise _NotFound("", 3001, "no path and no handle")

    def _stat_line(self, path: str, *, follow: bool = True) -> bytes:
        pointed_at = self.s.links.get(path)
        if pointed_at is not None:
            if not follow:
                # A link describes itself: "something else", as long as its
                # target's name. This is what kXR_statNoFollow asks for.
                flags = c.kXR_other | c.kXR_readable
                stamp = self._mtime(path)
                return f"{abs(hash(path)) % 10**9} {len(pointed_at)} {flags} {stamp}".encode()
            path = pointed_at
        if path in self.s.dirs:
            flags = c.kXR_isDir | c.kXR_readable | c.kXR_writable
            size = 4096
        elif path in self.s.files:
            flags = c.kXR_readable | c.kXR_writable
            if path in self.s.nearline:
                flags |= c.kXR_offline
            size = len(self.s.files[path])
        else:
            raise _NotFound(path)
        return f"{abs(hash(path)) % 10**9} {size} {flags} {self._mtime(path)}".encode()

    def _mtime(self, path: str) -> int:
        """Whole seconds, as the stat line carries them - a ``kXR_setattr``
        moves this, and everything else keeps the one fixed stamp that makes
        the rest of the suite's output reproducible."""
        return self.s.times.get(path, (0, 1700000000 * 10**9))[1] // 10**9

    def _children(self, path: str) -> list[str]:
        if path not in self.s.dirs:
            raise _NotFound(path)
        prefix = path.rstrip("/") + "/"
        names = {
            entry[len(prefix) :].split("/", 1)[0]
            for entry in list(self.s.files) + list(self.s.dirs)
            if entry.startswith(prefix) and entry != path
        }
        return sorted(names)

    def _file(self, path: str) -> bytearray:
        try:
            return self.s.files[path]
        except KeyError:
            raise _NotFound(path) from None


# --------------------------------------------------------------------------
# Handlers. Each yields the frames to send back.
# --------------------------------------------------------------------------


def _h_protocol(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    yield _frame(sid, c.kXR_ok, struct.pack(">iI", conn.s.version, conn.s.flags))


def _data_path(opcode: int, params: bytes) -> int:
    """The path a request's *data* travels on, if it named one."""
    return params[12] if opcode in (c.kXR_write, c.kXR_pgwrite) else 0


def _reply_path(opcode: int, params: bytes, body: bytes) -> int:
    """The path a request's *reply* travels on, if it named one."""
    if opcode in (c.kXR_read, c.kXR_pgread):
        return body[0] if body else 0
    return params[15] if opcode == c.kXR_readv else 0


def _h_bind(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    owner = conn.s._sessions.get(params[:16])
    if owner is None:
        yield _error(sid, 3010, "no such session")
        return
    pathid = owner.next_pathid
    owner.next_pathid += 1
    owner.paths[pathid] = conn
    conn.bound_to = owner
    # The same dict, not a copy: a file opened on the control link after this
    # bind is still the file a request arriving here names.
    conn.handles = owner.handles
    yield _frame(sid, c.kXR_ok, bytes([pathid]))


def _h_login(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    trailer = conn.s.sec.encode() + b"\x00" if conn.s.sec else b""
    conn.sessid = conn.s.next_sessid()
    with conn.s._live_lock:
        conn.s._sessions[conn.sessid] = conn
    yield _frame(sid, c.kXR_ok, conn.sessid + trailer)


def _h_auth(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    conn.auth_seen += 1
    if conn.auth_seen <= conn.s.auth_rounds:
        yield _frame(sid, c.kXR_authmore, b"challenge")
    else:
        yield _frame(sid, c.kXR_ok)


def _h_ping(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    yield _frame(sid, c.kXR_ok)


def _h_endsess(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    yield _frame(sid, c.kXR_ok)


def _h_stat(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    if params[0] & c.kXR_vfs:
        yield _frame(sid, c.kXR_ok, b"1 1000000 50 1 500000 20\x00")
        return
    path = conn._path(params, body)
    follow = not params[0] & c.kXR_statNoFollow
    yield _frame(sid, c.kXR_ok, conn._stat_line(path, follow=follow) + b"\x00")


def _h_statx(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
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
    args = body.split(b"\x00", 1)[0].decode()
    path = _clean(args)
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
        line = conn._stat_line(full)
        if algorithm:
            # A directory has nothing to digest, and the server says so in the
            # token rather than leaving it out.
            value = (
                _checksum(algorithm, bytes(conn.s.files[full]))
                if full in conn.s.files
                else "none"
            )
            line += f" [ {algorithm}:{value} ]".encode()
        out += name.encode() + b"\n" + line + b"\n"
    yield _frame(sid, c.kXR_ok, bytes(out) + b"\x00")


def _h_mkdir(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
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
    path = _clean(body.split(b"\x00", 1)[0].decode())
    if path in conn.s.dirs:
        yield _error(sid, 3016, f"is a directory: {path}")
        return
    conn._file(path)
    del conn.s.files[path]
    conn.s.xattrs.pop(path, None)
    yield _frame(sid, c.kXR_ok)


def _h_rmdir(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    path = _clean(body.split(b"\x00", 1)[0].decode())
    if path not in conn.s.dirs:
        raise _NotFound(path)
    if conn._children(path):
        yield _error(sid, 3005, f"directory not empty: {path}")
        return
    conn.s.dirs.discard(path)
    yield _frame(sid, c.kXR_ok)


def _h_mv(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    src, dst = _two_paths(params, body)
    conn.s.files[dst] = conn._file(src)
    del conn.s.files[src]
    conn.s.add_dir(posixpath.dirname(dst))
    yield _frame(sid, c.kXR_ok)


def _h_chmod(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    path = _clean(body.split(b"\x00", 1)[0].decode())
    if path not in conn.s.files and path not in conn.s.dirs:
        raise _NotFound(path)
    conn.s.modes[path] = struct.unpack(">H", params[14:16])[0]
    yield _frame(sid, c.kXR_ok)


def _h_truncate(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    size = struct.unpack(">q", params[4:12])[0]
    path = conn._path(params, body, at=slice(0, 4))
    data = conn._file(path)
    if size < len(data):
        del data[size:]
    else:
        data.extend(bytes(size - len(data)))
    yield _frame(sid, c.kXR_ok)


def _h_set(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    conn.s.properties.append(body.split(b"\x00", 1)[0].decode("utf-8", "replace").strip())
    yield _frame(sid, c.kXR_ok)


def _h_open(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    _mode, options = struct.unpack(">HH", params[:4])
    raw = body.split(b"\x00", 1)[0].decode()
    conn.s.opened.append(raw)
    path = _clean(raw)
    exists = path in conn.s.files
    if path in conn.s.dirs:
        yield _error(sid, 3016, f"is a directory: {path}")
        return
    if options & c.kXR_new and exists:
        yield _error(sid, 3018, f"already exists: {path}")
        return
    # Only kXR_new and kXR_delete create; kXR_open_updt and kXR_open_apnd on
    # their own are "open what is there", exactly as stock xrootd has it. The
    # fake used to create for any write flag, which let a client that never
    # asked for creation pass here and fail against a real server.
    if not exists:
        if not options & (c.kXR_new | c.kXR_delete):
            raise _NotFound(path)
        parent = posixpath.dirname(path)
        if parent not in conn.s.dirs and not options & c.kXR_mkpath:
            yield _error(sid, 3011, f"no such file or directory: {parent}")
            return
        conn.s.add_file(path)
    elif options & c.kXR_delete:
        conn.s.files[path] = bytearray()

    handle = struct.pack(">I", conn.next_handle)
    conn.next_handle += 1
    conn.handles[handle] = path
    reply = handle
    if options & c.kXR_retstat:
        reply += bytes(8) + conn._stat_line(path) + b"\x00"
    yield _frame(sid, c.kXR_ok, reply)


def _h_close(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    conn.checkpoints.pop(params[:4], None)
    if conn.handles.pop(params[:4], None) is None:
        yield _error(sid, 3004, "file is not open")
        return
    yield _frame(sid, c.kXR_ok)


def _h_read(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    offset, length = struct.unpack(">qi", params[4:16])
    data = conn._file(conn._path(params, b"", at=slice(0, 4)))[offset : offset + length]
    step = conn.s.chunk_reads
    if step and len(data) > step:
        for start in range(0, len(data) - step, step):
            yield _frame(sid, c.kXR_oksofar, bytes(data[start : start + step]))
        last = ((len(data) - 1) // step) * step
        yield _frame(sid, c.kXR_ok, bytes(data[last:]))
        return
    yield _frame(sid, c.kXR_ok, bytes(data))


def _h_write(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    offset = struct.unpack(">q", params[4:12])[0]
    _splice(conn._file(conn._path(params, b"", at=slice(0, 4))), offset, body)
    yield _frame(sid, c.kXR_ok)


def _h_sync(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    conn._path(params, b"", at=slice(0, 4))
    yield _frame(sid, c.kXR_ok)


def _h_readv(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    out = Writer()
    reader = Reader(body, "kXR_readv")
    while reader.remaining >= c.READ_LIST_ENTRY_LEN:
        handle = reader.bytes(4)
        length = reader.i32()
        offset = reader.i64()
        data = conn._file(conn.handles[handle])[offset : offset + length]
        out.raw(handle).i32(len(data)).i64(offset).raw(bytes(data))
    yield _frame(sid, c.kXR_ok, out.bytes())


def _h_writev(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    # dlen counts the write_list and nothing else, so the body is exactly N
    # descriptors and the data for them arrives afterwards, uncounted.
    if len(body) % c.READ_LIST_ENTRY_LEN:
        yield _error(sid, kXR_ArgInvalid, "Write vector is invalid")
        return
    reader = Reader(body, "kXR_writev")
    entries: list[tuple[bytes, int, int]] = []
    while reader.remaining >= c.READ_LIST_ENTRY_LEN:
        handle = reader.bytes(4)
        length = reader.i32()
        offset = reader.i64()
        entries.append((handle, offset, length))
    for handle, offset, length in entries:
        _splice(conn._file(conn.handles[handle]), offset, conn.rfile.read(length))
    yield _frame(sid, c.kXR_ok)


def _h_clone(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_clone``: copy ranges between two open handles, without the wire."""
    if not body or len(body) % c.CLONE_ITEM_LEN:
        yield _error(sid, kXR_ArgInvalid, "Clone list is invalid")
        return
    target = conn._file(conn._path(params, b"", at=slice(0, 4)))
    reader = Reader(body, "kXR_clone")
    while reader.remaining >= c.CLONE_ITEM_LEN:
        handle = reader.bytes(4)
        reader.bytes(4)
        offset, length, at = reader.i64(), reader.i64(), reader.i64()
        if handle not in conn.handles:
            yield _error(sid, 3004, "file is not open")
            return
        source = conn._file(conn.handles[handle])
        _splice(target, at, bytes(source[offset : offset + length]))
    yield _frame(sid, c.kXR_ok)


def _h_pgread(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    from ..crypto.crc32c import pack_pages

    offset, length = struct.unpack(">qi", params[4:16])
    data = bytes(conn._file(conn._path(params, b"", at=slice(0, 4)))[offset : offset + length])
    packed = pack_pages(data, offset)
    status = (
        struct.pack(">I", 0)
        + struct.pack(">H", sid)
        + bytes([c.kXR_pgread - c.kXR_1stRequest, c.kXR_FinalResult])
        + bytes(4)
        + struct.pack(">i", len(packed))
        + struct.pack(">q", offset)
    )
    yield _frame(sid, c.kXR_status, status) + packed


def _h_pgwrite(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    from ..crypto.crc32c import page_span, unpack_pages

    offset = struct.unpack(">q", params[4:12])[0]
    data, corrupt = unpack_pages(body, offset)
    conn.s.pgwrites.append((offset, len(data), params[13]))
    # A real server stores what it was sent and reports the pages whose CRC
    # did not survive; it does not throw the whole request away. The client
    # is expected to come back for those pages with kXR_pgRetry.
    _splice(conn._file(conn._path(params, b"", at=slice(0, 4))), offset, data)

    injected = []
    at = offset
    while at < offset + len(data):
        if conn.s.pgwrite_corrupt.get(at, 0):
            conn.s.pgwrite_corrupt[at] -= 1
            injected.append(at)
        at += page_span(at, offset + len(data) - at)
    corrupt = tuple(sorted({*corrupt, *injected}))

    if corrupt:
        yield pgwrite_cse(sid, corrupt)
        return
    yield _frame(sid, c.kXR_ok)


#: What a checkpoint on this server may journal, in bytes. Small enough that a
#: test can fill it, large enough that no ordinary one does.
CHECKPOINT_CAPACITY = 1 << 20


def _h_chkpoint(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    handle, subcode = params[:4], params[15]
    open_checkpoint = conn.checkpoints.get(handle)

    if subcode == c.kXR_ckpBegin:
        path = conn._path(params, b"", at=slice(0, 4))
        conn.checkpoints[handle] = _Checkpoint(path, bytearray(conn._file(path)))
        yield _frame(sid, c.kXR_ok)
        return

    if open_checkpoint is None:
        yield _error(sid, kXR_FileNotOpen, "no checkpoint is open on this handle")
        return
    if subcode == c.kXR_ckpQuery:
        yield _frame(sid, c.kXR_ok, struct.pack(">II", CHECKPOINT_CAPACITY, open_checkpoint.used))
    elif subcode == c.kXR_ckpCommit:
        del conn.checkpoints[handle]
        yield _frame(sid, c.kXR_ok)
    elif subcode == c.kXR_ckpRollback:
        del conn.checkpoints[handle]
        conn._file(open_checkpoint.path)[:] = open_checkpoint.snapshot
        yield _frame(sid, c.kXR_ok)
    elif subcode == c.kXR_ckpXeq:
        yield from _checkpoint_exec(conn, sid, body, open_checkpoint)
    else:
        yield _error(sid, kXR_ArgInvalid, f"unknown checkpoint subcode {subcode}")


def _checkpoint_exec(
    conn: _Connection, sid: int, body: bytes, state: _Checkpoint
) -> Iterator[bytes]:
    """Run the request embedded in a ``kXR_ckpXeq``.

    Its 24-byte header is the body; whatever data it carries follows the frame
    uncounted, the way ``kXR_writev`` streams its own, so the handler for the
    embedded opcode reads it off the socket itself.
    """
    if len(body) != c.REQUEST_HDRLEN:
        yield _error(sid, kXR_ArgInvalid, "checkpoint payload is not one request header")
        return
    _, opcode, inner, dlen = _REQ.unpack(body)
    # Drain the embedded request's data first, whatever happens next: leaving
    # it on the socket would make the following request read this one's bytes.
    data = conn.rfile.read(dlen)
    if opcode not in (c.kXR_write, c.kXR_pgwrite, c.kXR_truncate):
        yield _error(sid, kXR_Unsupported, f"{c.request_name(opcode)} is not checkpointable")
        return
    state.used += dlen
    if state.used > CHECKPOINT_CAPACITY:
        yield _error(sid, kXR_NoSpace, "checkpoint is full")
        return
    yield from _HANDLERS[opcode](conn, sid, inner, data)


def _h_symlink(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    target, link = _two_paths(params, body)
    if target not in conn.s.files and target not in conn.s.dirs:
        raise _NotFound(target)
    conn.s.links[link] = target
    yield _frame(sid, c.kXR_ok)


def _h_setattr(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    flags, at_s, at_ns, mt_s, mt_ns, uid, gid = struct.unpack(">iqqqqii", body[:44])
    path = _clean(body[44:].split(b"\x00", 1)[0].decode())
    if path not in conn.s.files and path not in conn.s.dirs and path not in conn.s.links:
        raise _NotFound(path)
    if flags & c.kXR_sa_times:
        was = conn.s.times.get(path, (0, 1700000000 * 10**9))
        kept = []
        for (s, ns), before in zip_strict(((at_s, at_ns), (mt_s, mt_ns)), was):
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
    src, dst = _two_paths(params, body)
    # A hard link is the same bytes under a second name, and a bytearray is
    # shared by reference - which is exactly the semantics wanted.
    conn.s.files[dst] = conn._file(src)
    conn.s.add_dir(posixpath.dirname(dst))
    yield _frame(sid, c.kXR_ok)


def _h_readlink(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
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


def _config_value(conn: _Connection, name: str) -> str:
    """One ``kXR_Qconfig`` answer. ``brix.substreams`` tracks whether this
    server actually serves arrivals, so the advertisement and the behaviour
    cannot drift apart in a test; ``config_values`` still overrides it, for
    the test about a server that lies."""
    value = conn.s.config_values.get(name)
    if value is not None:
        return value
    if name == "brix.substreams" and conn.s.serves_arrivals:
        return "rw"
    return ""


def _h_query(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    infotype = struct.unpack(">H", params[:2])[0]
    args = body.split(b"\x00", 1)[0].decode("utf-8", "replace")
    if infotype == c.kXR_Qcksum:
        path = _clean(args)
        algorithm = "adler32"
        for pair in args.partition("?")[2].split("&"):
            if pair.startswith("cks.type="):
                algorithm = pair.split("=", 1)[1]
        value = _checksum(algorithm, bytes(conn._file(path)))
        yield _frame(sid, c.kXR_ok, f"{algorithm} {value}".encode() + b"\x00")
    elif infotype == c.kXR_Qconfig:
        names = args.split("\n")
        values = [_config_value(conn, n) for n in names]
        yield _frame(sid, c.kXR_ok, "\n".join(values).encode() + b"\x00")
    elif infotype == c.kXR_Qckscan:
        conn.s.cancelled_checksums.append(_clean(args))
        yield _frame(sid, c.kXR_ok)
    elif infotype == c.kXR_QStats:
        yield _frame(sid, c.kXR_ok, f'<statistics sel="{args}"/>'.encode() + b"\x00")
    elif infotype == c.kXR_Qspace:
        yield _frame(sid, c.kXR_ok, conn.s.space.encode() + b"\x00")
    elif infotype == c.kXR_QPrep:
        handle, *wanted = args.split("\n")
        if handle not in conn.s.prepared:
            yield _error(sid, 3000, f"prepare request {handle} is not one of ours")
            return
        asked = conn.s.prepared[handle]
        yield _frame(sid, c.kXR_ok, json.dumps({
            "request_id": handle,
            "responses": [
                {
                    "path": _clean(path),
                    "path_exists": _clean(path) in conn.s.files,
                    "on_tape": _clean(path) in conn.s.nearline,
                    "online": _clean(path) in conn.s.files
                    and _clean(path) not in conn.s.nearline,
                    "requested": _clean(path) in asked,
                    "has_reqid": _clean(path) in asked,
                    "req_time": "",
                    "error_text": "" if _clean(path) in conn.s.files else "no such file",
                }
                for path in wanted
            ],
        }).encode() + b"\x00")
    elif infotype == c.kXR_Qvisa:
        yield _frame(sid, c.kXR_ok, b"visa\x00")
    else:
        yield _error(sid, 3013, f"query {infotype} is not supported")


def _h_locate(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    asked = body.split(b"\x00", 1)[0].decode()
    # A ``*`` in front is create mode: the question is where the file could
    # go, so one that is not there yet is the normal case, not a miss.
    create = asked.startswith("*")
    path = _clean(asked[1:] if create else asked)
    if not create and path not in conn.s.files and path not in conn.s.dirs:
        raise _NotFound(path)
    host, port = conn.s.address
    yield _frame(sid, c.kXR_ok, f"Sw{host}:{port}".encode() + b"\x00")


def _h_prepare(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    conn.s.prepare_options.append((params[0], int.from_bytes(params[4:6], "big")))
    if params[0] & c.kXR_cancel:
        conn.s.cancelled_prepares.append(body.split(b"\x00", 1)[0].decode().strip())
        yield _frame(sid, c.kXR_ok)
        return
    handle = "prep-0001"
    paths = [_clean(p) for p in body.decode().split("\n") if p.strip()]
    conn.s.prepared[handle] = paths
    if conn.s.prepare_options[-1][1] & c.kXR_evict:  # optionX, not the options byte
        conn.s.evicted.extend(paths)
    yield _frame(sid, c.kXR_ok, handle.encode() + b"\x00")


def _h_fattr(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    subcode, numattr, options = params[4], params[5], params[6]
    reader = Reader(body, "kXR_fattr")
    path = reader.cstring()
    if not path:
        path = conn.handles.get(params[:4], "")
    path = _clean(path)
    if path not in conn.s.files and path not in conn.s.dirs:
        raise _NotFound(path)
    if subcode == c.kXR_fattrList and options & c.kXR_fattrRecurse and path in conn.s.dirs:
        # The recursive reply is a flat run of ``relpath:name`` entries with no
        # header, and only regular files carry them - directories in the way
        # are walked through rather than reported.
        prefix = path.rstrip("/") + "/"
        entries = bytearray()
        for name in sorted(conn.s.files):
            if not name.startswith(prefix):
                continue
            for attribute in sorted(conn.s.xattrs.get(name, {})):
                entries += f"{name[len(prefix) :]}:{attribute}".encode() + b"\x00"
        yield _frame(sid, c.kXR_ok, bytes(entries))
        return

    store = conn.s.xattrs.setdefault(path, {})

    if subcode == c.kXR_fattrList:
        names = sorted(store)
        values = bool(options & c.kXR_fattrAData)
        reply = _fattr_reply([(n, 0, store[n] if values else None) for n in names])
        yield _frame(sid, c.kXR_ok, reply)
        return

    items: list[tuple[str, int, bytes | None]] = []
    for _ in range(max(numattr, 1)):
        if reader.remaining < 3:
            break
        reader.u16()
        name = reader.cstring()
        if subcode == c.kXR_fattrSet:
            value = reader.bytes(reader.i32())
            if options & c.kXR_fattrIsNew and name in store:
                items.append((name, 17, None))
                continue
            store[name] = bytes(value)
            items.append((name, 0, None))
        elif subcode == c.kXR_fattrGet:
            current = store.get(name)
            items.append((name, 0 if current is not None else 61, current or b""))
        elif subcode == c.kXR_fattrDel:
            items.append((name, 0 if store.pop(name, None) is not None else 61, None))
    yield _frame(sid, c.kXR_ok, _fattr_reply(items))


_HANDLERS = {
    c.kXR_protocol: _h_protocol,
    c.kXR_login: _h_login,
    c.kXR_bind: _h_bind,
    c.kXR_auth: _h_auth,
    c.kXR_ping: _h_ping,
    c.kXR_endsess: _h_endsess,
    c.kXR_stat: _h_stat,
    c.kXR_statx: _h_statx,
    c.kXR_dirlist: _h_dirlist,
    c.kXR_mkdir: _h_mkdir,
    c.kXR_rm: _h_rm,
    c.kXR_rmdir: _h_rmdir,
    c.kXR_mv: _h_mv,
    c.kXR_chmod: _h_chmod,
    c.kXR_truncate: _h_truncate,
    c.kXR_set: _h_set,
    c.kXR_open: _h_open,
    c.kXR_close: _h_close,
    c.kXR_read: _h_read,
    c.kXR_write: _h_write,
    c.kXR_sync: _h_sync,
    c.kXR_readv: _h_readv,
    c.kXR_writev: _h_writev,
    c.kXR_clone: _h_clone,
    c.kXR_pgread: _h_pgread,
    c.kXR_pgwrite: _h_pgwrite,
    c.kXR_chkpoint: _h_chkpoint,
    c.kXR_query: _h_query,
    c.kXR_locate: _h_locate,
    c.kXR_prepare: _h_prepare,
    c.kXR_fattr: _h_fattr,
    c.kXR_setattr: _h_setattr,
    c.kXR_symlink: _h_symlink,
    c.kXR_link: _h_link,
    c.kXR_readlink: _h_readlink,
}


def _splice(data: bytearray, offset: int, payload: bytes) -> None:
    """Write ``payload`` at ``offset``, zero-filling any hole.

    Writing nothing does nothing, as for :func:`os.pwrite`: an empty write
    past the end does not extend the file to reach it.
    """
    if not payload:
        return
    if offset > len(data):
        data.extend(bytes(offset - len(data)))
    data[offset : offset + len(payload)] = payload


def _fattr_reply(items: list[tuple[str, int, bytes | None]]) -> bytes:
    w = Writer().u8(sum(1 for _, code, _ in items if code)).u8(len(items))
    for name, code, value in items:
        w.u16(code).text(name, nul=True)
        if value is not None:
            w.i32(len(value)).raw(value)
    return w.bytes()


def _checksum(algorithm: str, data: bytes) -> str:
    from ..crypto.checksum import checksum_bytes

    try:
        return checksum_bytes(algorithm, data)
    except ValueError:
        return f"{zlib.adler32(data):08x}"


def from_directory(
    directory: str | os.PathLike[str],
    *,
    host: str = "127.0.0.1",
    port: int = 1094,
    pattern: str = "*",
) -> FakeServer:
    """A server holding every file in ``directory`` that matches ``pattern``.

        >>> server = from_directory("datasets", port=21094)   # doctest: +SKIP
        >>> with server:                                      # doctest: +SKIP
        ...     xrdroot.open_root(f"{server.url}mnist.root")  # a separate package

    Each file is offered twice: under its name alone, and under the absolute
    path it has on this machine - so both ``root://host:port//mnist.root`` and
    ``root://host:port//home/you/datasets/mnist.root`` reach the same bytes.
    The files are read into memory, because that is what this server holds.
    """
    root = pathlib.Path(directory).resolve()
    files = {}
    for path in sorted(root.glob(pattern)):
        if path.is_file():
            data = path.read_bytes()
            files[f"/{path.name}"] = data
            files[path.as_posix()] = data
    return FakeServer(files=files, host=host, port=port)


def _block() -> None:  # pragma: no cover - waits for a signal that ends it
    """Wait until the terminal interrupts us."""
    threading.Event().wait()


def main(argv: Sequence[str] | None = None, *, wait: Callable[[], None] = _block) -> int:
    """Share a directory over ``root://`` on an unprivileged port, no login::

        $ python -m xrdclient.testing datasets --port 21094

    Anyone who can reach the port can read - and write - everything served,
    which is why the default is loopback. It is a demonstration and a test
    fixture, not a storage element.
    """
    parser = argparse.ArgumentParser(
        prog="python -m xrdclient.testing",
        description="Serve a directory over root:// with no authentication.",
    )
    parser.add_argument("directory", help="the directory whose files to serve")
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on")
    parser.add_argument("--port", type=int, default=1094, help="port to listen on")
    parser.add_argument("--pattern", default="*", help="which files to take, as a glob")
    args = parser.parse_args(argv)

    server = from_directory(args.directory, host=args.host, port=args.port,
                            pattern=args.pattern).start()
    host, port = server.address
    # Flushed as they go: this is a program whose next act is to block, and an
    # unflushed buffer would leave the terminal with nothing to connect to.
    print(f"serving {len(server.files) // 2} files on root://{host}:{port}/ with no login",
          flush=True)
    for path in sorted(server.files):
        print(f"  root://{host}:{port}/{path}  {len(server.files[path]):,} bytes", flush=True)
    try:
        wait()
    except KeyboardInterrupt:
        print("stopping", flush=True)
    finally:
        server.stop()
    return 0
