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

This module is the server's state, socket and dispatcher; the per-opcode
handlers are in :mod:`xrdclient.testing._server`, one module per area of the
protocol, and are re-exported here. Names a test patches on this module -
``_checksum``, ``CHECKPOINT_CAPACITY``, the ``_HANDLERS`` table - are defined
here and read through it at call time, so such a patch still takes effect.

Threading and lifetime: :class:`socketserver.ThreadingTCPServer` gives every
accepted connection its own thread, which runs one :class:`_Connection` for
the life of the socket. All connections share the one :class:`FakeServer`'s
dicts; handlers mutate them without locking, which the GIL makes safe for the
single-operation updates they do. The registries that must be consistent
across threads - live sockets, logged-in sessions, parked data paths - are
guarded by ``_live_lock``. Every thread is a daemon, so a test that forgets
:meth:`FakeServer.stop` cannot hang interpreter exit.

Where to start reading: :meth:`_Connection._serve_request` (one request,
including where its data and reply travel for a bound data path), then
:meth:`_Connection._dispatch` (the fault knobs, in the order they apply),
then the handler module for the opcode in question.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import posixpath
import socket
import socketserver
import struct
import threading
import zlib
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Self, cast

from ..proto import constants as c
from ..url import XRootDURL, parse

# The request handlers live in ``_server``, one module per area of the
# protocol (see its package docstring for the map). They are re-exported here
# under their old names - ``as`` marks each one as a deliberate re-export -
# because tests reach for them as ``xrdclient.testing.server._h_open`` and so
# on, and because the ``_HANDLERS`` table below is built from them.
from ._server.files import _Checkpoint as _Checkpoint
from ._server.files import _checkpoint_exec as _checkpoint_exec
from ._server.files import _colocate as _colocate
from ._server.files import _h_chkpoint as _h_chkpoint
from ._server.files import _h_clone as _h_clone
from ._server.files import _h_close as _h_close
from ._server.files import _h_open as _h_open
from ._server.files import _h_pgread as _h_pgread
from ._server.files import _h_pgwrite as _h_pgwrite
from ._server.files import _h_read as _h_read
from ._server.files import _h_readv as _h_readv
from ._server.files import _h_sync as _h_sync
from ._server.files import _h_write as _h_write
from ._server.files import _h_writev as _h_writev
from ._server.files import _splice as _splice
from ._server.files import _target_refusal as _target_refusal
from ._server.files import _template_refusal as _template_refusal
from ._server.namespace import _h_chmod as _h_chmod
from ._server.namespace import _h_dirlist as _h_dirlist
from ._server.namespace import _h_link as _h_link
from ._server.namespace import _h_mkdir as _h_mkdir
from ._server.namespace import _h_mv as _h_mv
from ._server.namespace import _h_readlink as _h_readlink
from ._server.namespace import _h_rm as _h_rm
from ._server.namespace import _h_rmdir as _h_rmdir
from ._server.namespace import _h_setattr as _h_setattr
from ._server.namespace import _h_stat as _h_stat
from ._server.namespace import _h_statx as _h_statx
from ._server.namespace import _h_symlink as _h_symlink
from ._server.namespace import _h_truncate as _h_truncate
from ._server.namespace import _two_paths as _two_paths
from ._server.queries import _QUERIES as _QUERIES
from ._server.queries import _config_value as _config_value
from ._server.queries import _h_locate as _h_locate
from ._server.queries import _h_prepare as _h_prepare
from ._server.queries import _h_query as _h_query
from ._server.queries import _h_query_fctl as _h_query_fctl
from ._server.queries import _q_ckscan as _q_ckscan
from ._server.queries import _q_cksum as _q_cksum
from ._server.queries import _q_config as _q_config
from ._server.queries import _q_prep as _q_prep
from ._server.queries import _q_space as _q_space
from ._server.queries import _q_stats as _q_stats
from ._server.queries import _q_visa as _q_visa
from ._server.session import _h_auth as _h_auth
from ._server.session import _h_bind as _h_bind
from ._server.session import _h_endsess as _h_endsess
from ._server.session import _h_login as _h_login
from ._server.session import _h_ping as _h_ping
from ._server.session import _h_protocol as _h_protocol
from ._server.session import _h_set as _h_set
from ._server.wire import _NULL_HANDLE as _NULL_HANDLE
from ._server.wire import _REQ as _REQ
from ._server.wire import _RESP as _RESP
from ._server.wire import _clean as _clean
from ._server.wire import _error as _error
from ._server.wire import _frame as _frame
from ._server.wire import _NotFound as _NotFound
from ._server.wire import error as error
from ._server.wire import frame as frame
from ._server.wire import pgwrite_cse as pgwrite_cse
from ._server.xattrs import _FATTR_EDITS as _FATTR_EDITS
from ._server.xattrs import _fattr_del as _fattr_del
from ._server.xattrs import _fattr_edit as _fattr_edit
from ._server.xattrs import _fattr_get as _fattr_get
from ._server.xattrs import _fattr_list as _fattr_list
from ._server.xattrs import _fattr_path as _fattr_path
from ._server.xattrs import _fattr_reply as _fattr_reply
from ._server.xattrs import _fattr_set as _fattr_set
from ._server.xattrs import _fattr_walk as _fattr_walk
from ._server.xattrs import _h_fattr as _h_fattr

__all__ = ["FakeServer", "error", "frame", "from_directory", "main", "pgwrite_cse"]

#: Requests whose body is payload rather than a path, so nothing about them
#: belongs in :attr:`FakeServer.arguments`. The read side is here for the same
#: reason as the write side: a read's body is its path id and its read list,
#: never a name, and decoding one as text invents an argument nobody sent.
_DATA_REQUESTS = frozenset(
    {
        c.kXR_write,
        c.kXR_writev,
        c.kXR_clone,
        c.kXR_pgwrite,
        c.kXR_read,
        c.kXR_readv,
        c.kXR_pgread,
        c.kXR_auth,
        c.kXR_login,
        c.kXR_sigver,
    }
)

#: What :attr:`FakeServer.handlers` entries look like.
Handler = Callable[["_Connection", int, bytes, bytes], Iterator[bytes]]


def _endpoint(server: socketserver.BaseServer) -> tuple[str, int]:
    """The bound ``(host, port)``. These servers are always AF_INET, which is
    narrower than the address family ``socketserver`` is typed for.
    """
    host, port = cast("tuple[str, int]", server.server_address)
    return host, port


class FakeServer:
    """An in-memory XRootD server listening on an ephemeral loopback port.

    Everything a test arranges or inspects is a public attribute: the
    namespace (``files``, ``dirs``, ``links``, ``xattrs`` ...), the request
    logs (``seen``, ``opened``, ``arguments``, ``reads`` ...) and the fault
    knobs (``redirects``, ``waits``, ``chunk_reads``, ``auth_rounds``,
    ``pgwrite_corrupt``, ``handlers``). Knobs are read per request, so a
    test may change them while a client is connected. The port is bound on
    first use and kept until :meth:`stop`.
    """

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
        """Hold ``files`` and ``dirs``; bind nothing until the address is first needed.

        ``sec`` is the security trailer the login reply carries (``""`` for
        none, so no authentication); ``flags`` and ``version`` are what
        ``kXR_protocol`` reports, which is how a test impersonates a
        redirector (``kXR_isManager``) or an older server. ``sessid`` seeds
        the session ids :meth:`next_sessid` hands out. All of these are
        keyword-only so a test reads as a description of the server it wants.
        """
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
        #: ``(path, offset, length)`` of every ordinary read, in order. This
        #: makes range-sensitive clients testable without changing replies.
        self.reads: list[tuple[str, int, int]] = []
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
        #: What a ``kXR_query`` of type ``kXR_Qopaqug`` - XrdCl's ``Fcntl`` -
        #: answers: called with the path of the file the query names and the
        #: bytes it carried. ``None`` is a stock xrootd, which has no storage
        #: plug-in to take it and refuses with 3013.
        self.fctl: Callable[[str, bytes], bytes] | None = None
        #: Whether opens against a template file (``kXR_samefs``,
        #: ``kXR_dup``) are served, as by storage that can place and clone
        #: files (``XrdSfs::hasFICL``); off, they are refused with 3013.
        self.colocates = True
        #: New file to the template it was opened against, for every open
        #: that named one.
        self.colocated: dict[str, str] = {}

        #: Every connected socket, so :meth:`disconnect` can sever them all.
        self._live: set[socket.socket] = set()
        #: Guards ``_live``, ``_sessions``, ``_parked`` and the session counter,
        #: which connection threads update concurrently.
        self._live_lock = threading.Lock()
        #: Logged-in connections by session id, so ``kXR_bind`` can find the
        #: one a data path is asking to join.
        self._sessions: dict[bytes, _Connection] = {}
        #: Wake-ups for bound data-path threads that have handed their socket
        #: to the control link and are waiting to exit; set on disconnect.
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

    def start(self) -> Self:
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
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            finally:
                sock.close()

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

    def __enter__(self) -> Self:
        """Start serving; the ``with`` block is the server's lifetime."""
        return self.start()

    def __exit__(self, *exc: object) -> None:
        """Stop, release the port and drop every connection, raised or not."""
        self.stop()

    def __repr__(self) -> str:
        """Where it listens and how much it holds - for a failing test's output."""
        # Reading the address would bind the port; a repr must not do that.
        bound = _endpoint(self._bound) if self._bound else self._last
        where = f"{bound[0]}:{bound[1]}" if bound else "unbound"
        return f"FakeServer({where}, files={len(self.files)}, dirs={len(self.dirs)})"


class _TCPServer(socketserver.ThreadingTCPServer):
    """The listening socket, with a back-reference to the fake it serves.

    ``allow_reuse_address`` lets the next test bind a port this one just
    released; ``daemon_threads`` keeps a client's idle connection from
    holding the interpreter open.
    """

    allow_reuse_address = True
    daemon_threads = True
    fake: FakeServer


class _Handler(socketserver.BaseRequestHandler):
    """The ``socketserver`` hook: hand each accepted socket to a :class:`_Connection`."""

    def handle(self) -> None:
        """Run the connection's protocol loop on this (per-connection) thread."""
        server, sock = self.server, self.request
        # Always so: _Handler serves only a _TCPServer, which accepts TCP sockets.
        assert isinstance(server, _TCPServer) and isinstance(sock, socket.socket)
        _Connection(server.fake, sock).run()


class _Connection:
    """One client connection's worth of protocol.

    Per-connection state is what xrootd keeps per link: open handles, open
    checkpoints, authentication progress and the session id. A connection
    that sends ``kXR_bind`` to join another becomes one of its data paths - it then
    *shares* its owner's handle table, and its socket is read and written by
    the owner's thread for the requests that route data through it.
    """

    def __init__(self, server: FakeServer, sock: socket.socket) -> None:
        """Wrap ``sock`` for ``server``; nothing is read until :meth:`run`."""
        self.s = server
        self.sock = sock
        self.rfile = sock.makefile("rb")
        #: Open file handles (4-byte, big-endian counters) to their paths.
        self.handles: dict[bytes, str] = {}
        #: The open checkpoint per handle, if any.
        self.checkpoints: dict[bytes, _Checkpoint] = {}
        self.next_handle = 1
        #: ``kXR_auth`` requests seen, against ``FakeServer.auth_rounds``.
        self.auth_seen = 0
        #: The session id this link's login was given; ``b""`` before login.
        self.sessid = b""
        #: Data connections bound to this one, by path id.
        self.paths: dict[int, _Connection] = {}
        self.next_pathid = 1
        #: The connection this one is a data path of, once bound.
        self.bound_to: _Connection | None = None
        #: Which link the reply to the request in hand goes out on.
        self.route = 0
        #: Set to let a parked data-path thread finish (see ``_serve_request``).
        self.unpark = threading.Event()

    # -- plumbing -------------------------------------------------------

    def run(self) -> None:
        """Serve until the client goes, the link is parked, or the server stops.

        Socket errors and malformed frames end the connection quietly: a
        client hanging up mid-request is a normal event for a test server,
        and the test that caused it asserts on the client side.
        """
        with self.s._live_lock:
            self.s._live.add(self.sock)
        try:
            self._serve()
        except (OSError, ValueError):
            pass
        finally:
            self._teardown()

    def _serve(self) -> None:
        """Handshake, then answer requests until the link is done with."""
        # The client's 20-byte initial handshake (five int32s ending in 4
        # and 2012) is read but not checked; the answer is the 8-byte
        # handshake reply on stream 0. The real role and version come later,
        # from kXR_protocol, which is what clients act on.
        if len(self.rfile.read(20) or b"") < 20:
            return
        self._send(_frame(0, c.kXR_ok, struct.pack(">ii", 0, c.ROOTD_PQ)))
        while True:
            header = self.rfile.read(c.REQUEST_HDRLEN)
            if not header or len(header) < c.REQUEST_HDRLEN:
                return
            if not self._serve_request(header):
                return

    def _serve_request(self, header: bytes) -> bool:
        """Answer one request; false once this thread must stop reading."""
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
            return False
        return bool(opcode != c.kXR_endsess)

    def _teardown(self) -> None:
        """Forget this link everywhere it was registered, then close it.

        A data path detaches from its owner so that later routing cannot
        pick a closed socket; the session is dropped so a ``kXR_bind`` to it
        is refused, as it would be after a real server lost the link.
        """
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
            self.sock.close()
        except OSError:
            pass

    def _send(self, data: bytes) -> None:
        """Write a frame to the link the current request's reply is routed to."""
        target = self.paths.get(self.route, self)
        target.sock.sendall(data)

    def _dispatch(self, sid: int, opcode: int, params: bytes, body: bytes) -> Iterator[bytes]:
        """The frames that answer one request, after the fault knobs have had their say.

        Order matters and is pinned by tests: a pending redirect wins (and is
        consumed), then the argument is logged, then a pending ``kXR_wait``,
        then a test-installed handler, then the built-in one. An opcode with
        no handler is ``kXR_Unsupported`` (3013), as from a server that
        predates it.
        """
        if opcode == c.kXR_sigver:
            return  # the signature prefixes the next frame; nothing to answer

        target = self.s.redirects.pop(opcode, None)
        if target is not None:
            host, port, token = target
            # kXR_redirect body: port[4] then "host[?opaque]", NUL-terminated.
            where = f"{host}{'?' + token if token else ''}".encode()
            yield _frame(sid, c.kXR_redirect, struct.pack(">i", port) + where + b"\x00")
            return

        self._record_argument(opcode, body)

        if self.s.waits.get(opcode, 0):
            self.s.waits[opcode] -= 1
            # kXR_wait body: seconds[4] then a message. Zero seconds keeps
            # the suite fast while still making the client go round again.
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
        """The ``"<id> <size> <flags> <mtime>"`` text a stat reply carries for ``path``.

        The id is a hash of the path - stable within a run, which is all a
        client may assume of it. Directories report 4096 bytes, as a POSIX
        filesystem under xrootd does. ``_NotFound`` if nothing is there.
        """
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
        """The sorted names directly inside ``path``: files, directories and links."""
        # Listing a link lists what it points at, as readdir(3) through a
        # link to a directory does; the links themselves are entries.
        path = self.s.links.get(path, path)
        if path not in self.s.dirs:
            raise _NotFound(path)
        prefix = path.rstrip("/") + "/"
        names = {
            entry[len(prefix) :].split("/", 1)[0]
            for entry in list(self.s.files) + list(self.s.dirs) + list(self.s.links)
            if entry.startswith(prefix) and entry != path
        }
        return sorted(names)

    def _file(self, path: str) -> bytearray:
        """The live, mutable contents at ``path`` - not a copy; ``_NotFound`` if absent."""
        try:
            return self.s.files[path]
        except KeyError:
            raise _NotFound(path) from None


def _data_path(opcode: int, params: bytes) -> int:
    """The path a request's *data* travels on, if it named one.

    ``kXR_write`` and ``kXR_pgwrite`` carry a path id in ``params[12]``
    (``pathid``); 0 is the control link itself.
    """
    return params[12] if opcode in (c.kXR_write, c.kXR_pgwrite) else 0


def _reply_path(opcode: int, params: bytes, body: bytes) -> int:
    """The path a request's *reply* travels on, if it named one.

    A read names it in its optional ``read_args`` body (first byte,
    ``pathid``); ``kXR_readv`` in ``params[15]``. Everything else is
    answered on the link the request came in on.
    """
    if opcode in (c.kXR_read, c.kXR_pgread):
        return body[0] if body else 0
    return params[15] if opcode == c.kXR_readv else 0


# --------------------------------------------------------------------------
# What tests may patch, and the dispatch table. The handlers in ``_server``
# read these through this module when they run, never at import.
# --------------------------------------------------------------------------


#: What a checkpoint on this server may journal, in bytes. Small enough that a
#: test can fill it, large enough that no ordinary one does.
CHECKPOINT_CAPACITY = 1 << 20


#: Opcode to built-in handler. :attr:`FakeServer.handlers` is consulted
#: first, so a test overrides an entry without patching this table.
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
    c.kXR_query: _h_query_fctl,
    c.kXR_locate: _h_locate,
    c.kXR_prepare: _h_prepare,
    c.kXR_fattr: _h_fattr,
    c.kXR_setattr: _h_setattr,
    c.kXR_symlink: _h_symlink,
    c.kXR_link: _h_link,
    c.kXR_readlink: _h_readlink,
}


def _checksum(algorithm: str, data: bytes) -> str:
    """The checksum a ``kXR_Qcksum`` or ``kXR_dcksm`` listing reports, as lowercase hex.

    An algorithm the library cannot compute falls back to adler32 rather
    than failing, so a test about the *request* need not care what the
    value is. Tests patch this (here, on the facade) to force a mismatch.
    Imported lazily so ``xrdclient.testing`` stays cheap to import.
    """
    from xrdcore.checksum import checksum_bytes

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


def _block() -> None:
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

    server = from_directory(
        args.directory, host=args.host, port=args.port, pattern=args.pattern
    ).start()
    host, port = server.address
    # Flushed as they go: this is a program whose next act is to block, and an
    # unflushed buffer would leave the terminal with nothing to connect to.
    print(
        f"serving {len(server.files) // 2} files on root://{host}:{port}/ with no login", flush=True
    )
    for path in sorted(server.files):
        print(f"  root://{host}:{port}/{path}  {len(server.files[path]):,} bytes", flush=True)
    try:
        wait()
    except KeyboardInterrupt:
        print("stopping", flush=True)
    finally:
        server.stop()
    return 0
