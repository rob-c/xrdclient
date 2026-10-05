"""A KCM daemon in a thread: Heimdal's KCM protocol over a Unix socket.

This is the protocol ``sssd-kcm`` (RHEL 9's default credential cache) and
Heimdal's ``kcm`` speak, and MIT's ``cc_kcm.c`` is a client of: each
request is a 4-byte big-endian length, then major version 2, minor version
0, a 16-bit opcode and the op's arguments; each reply is a 4-byte length,
a 32-bit status (a com_err code, 0 for success), and the op's results.
Names are NUL-terminated strings; principals and credentials are in the
version 4 FILE ccache layout; UUIDs are 16 bytes; integers are big-endian.

It keeps caches in memory - ``{name: _Cache}`` - and implements the ops a
client uses to create, fill, list and read a cache, including MIT's two
extensions (``GET_CRED_LIST``, ``REPLACE``). ``heimdal=True`` refuses those
the way Heimdal's daemon does (``KRB5_FCC_INTERNAL``), so the client's
fallback to the UUID walk is exercised too. It is a test double, faithful to
the wire format - MIT's own ``kinit``/``klist``/``kvno`` run against it in
``tests/test_krb5_kcm.py`` - but it does no access control by peer uid,
which is the one thing a real daemon adds.
"""

from __future__ import annotations

import itertools
import os
import socket
import struct
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

# com_err codes, from MIT's krb5.h.
KRB5_CC_END = -1765328242
KRB5_CC_NOTFOUND = -1765328243
KRB5_CC_IO = -1765328191
KRB5_FCC_NOFILE = -1765328189
KRB5_FCC_INTERNAL = -1765328188
KRB5_CC_NOSUPP = -1765328137

# Opcodes, from Heimdal's kcm.h (and MIT's src/include/kcm.h).
OP = {
    "NOOP": 0,
    "GET_NAME": 1,
    "RESOLVE": 2,
    "GEN_NEW": 3,
    "INITIALIZE": 4,
    "DESTROY": 5,
    "STORE": 6,
    "RETRIEVE": 7,
    "GET_PRINCIPAL": 8,
    "GET_CRED_UUID_LIST": 9,
    "GET_CRED_BY_UUID": 10,
    "REMOVE_CRED": 11,
    "SET_FLAGS": 12,
    "GET_CACHE_UUID_LIST": 18,
    "GET_CACHE_BY_UUID": 19,
    "GET_DEFAULT_CACHE": 20,
    "SET_DEFAULT_CACHE": 21,
    "GET_KDC_OFFSET": 22,
    "SET_KDC_OFFSET": 23,
    "GET_CRED_LIST": 13001,
    "REPLACE": 13002,
}
NAMES = {number: name for name, number in OP.items()}


@dataclass
class _Cache:
    principal: bytes = b""
    creds: dict[bytes, bytes] = field(default_factory=dict)  # uuid -> marshalled cred
    offset: int = 0
    uuid: bytes = field(default_factory=lambda: uuid.uuid4().bytes)


class _Args:
    """A cursor over a request's arguments."""

    def __init__(self, data: bytes) -> None:
        self.data, self.pos = data, 0

    def name(self) -> str:
        end = self.data.index(b"\0", self.pos)
        out, self.pos = self.data[self.pos : end].decode(), end + 1
        return out

    def take(self, count: int) -> bytes:
        out, self.pos = self.data[self.pos : self.pos + count], self.pos + count
        return out

    def u32(self) -> int:
        return int(struct.unpack(">I", self.take(4))[0])

    def i32(self) -> int:
        return int(struct.unpack(">i", self.take(4))[0])

    def principal(self) -> bytes:
        start = self.pos
        self.take(4)
        count = self.u32()
        for _ in range(count + 1):
            self.take(self.u32())
        return self.data[start : self.pos]

    def rest(self) -> bytes:
        out, self.pos = self.data[self.pos :], len(self.data)
        return out


class FakeKcm:
    """A KCM daemon on ``path``, serving until :meth:`stop`.

    ``requests`` records every ``(opcode name, cache name or "")`` served,
    so a test can say which ops a client used.
    """

    def __init__(self, path: Path, *, heimdal: bool = False, default: str = "1000") -> None:
        self.path = Path(path)
        self.heimdal = heimdal
        self.default = default
        self.caches: dict[str, _Cache] = {}
        self.requests: list[tuple[str, str]] = []
        self.fail: dict[str, int] = {}  # opcode name -> status to answer with
        self.raw: dict[str, bytes] = {}  # opcode name -> results to send instead, status 0
        self._counter = itertools.count(1)
        self._lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None

    # -- the store a test fills directly --------------------------------

    def load(self, name: str, ccache: bytes) -> None:
        """Fill cache ``name`` from a FILE-format (version 4) cache image."""
        from xrdclient.auth.kerberos import ccache as fmt

        reader = fmt._Reader(ccache)
        assert reader.u16() == fmt.CCACHE_VERSION_4
        header = reader.take(reader.u16())
        offset = fmt._kdc_offset(header)
        start = reader.pos
        fmt._read_principal(reader)
        cache = _Cache(principal=ccache[start : reader.pos], offset=int(offset))
        while not reader.exhausted:
            begin = reader.pos
            fmt._read_entry(reader, fmt.CCACHE_VERSION_4, 0.0)
            cache.creds[uuid.uuid4().bytes] = ccache[begin : reader.pos]
        self.caches[name] = cache

    # -- serving ---------------------------------------------------------

    def start(self) -> FakeKcm:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.path))
        listener.listen(8)
        self._listener = listener
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            # On Linux close() alone doesn't wake another thread's accept().
            with suppress(OSError):
                listener.shutdown(socket.SHUT_RDWR)
            listener.close()
        if self._thread is not None:
            self._thread.join(5)
        if self.path.exists():
            os.unlink(self.path)

    def __enter__(self) -> FakeKcm:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _accept(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while self._listener is not None:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            while True:
                request = _recv_frame(conn)
                if request is None:
                    return
                status, results = self.answer(request)
                reply = struct.pack(">i", status) + results
                # heim-ipc's frame: the reply's length and a transport status, then the reply.
                conn.sendall(struct.pack(">II", len(reply), 0) + reply)

    def answer(self, request: bytes) -> tuple[int, bytes]:
        """The status and results for one request body."""
        major, _minor, opcode = struct.unpack(">BBH", request[:4])
        name = NAMES.get(opcode, str(opcode))
        if major != 2:
            return KRB5_CC_IO, b""
        if name in self.raw:
            return 0, self.raw[name]
        if name in self.fail:
            return self.fail[name], b""
        handler = getattr(self, f"_op_{name.lower()}", None)
        if handler is None or (self.heimdal and opcode >= 13000):
            self.requests.append((name, ""))
            return KRB5_FCC_INTERNAL, b""
        with self._lock:
            args = _Args(request[4:])
            try:
                result: tuple[int, bytes] = handler(args)
            except KeyError:
                result = KRB5_FCC_NOFILE, b""
        return result

    def _named(self, args: _Args, op: str) -> _Cache:
        name = args.name()
        self.requests.append((op, name))
        return self.caches[name]

    # -- ops: (args) -> (status, results) ---------------------------------

    def _op_noop(self, args: _Args) -> tuple[int, bytes]:
        self.requests.append(("NOOP", ""))
        return 0, b""

    def _op_gen_new(self, args: _Args) -> tuple[int, bytes]:
        self.requests.append(("GEN_NEW", ""))
        return 0, f"{self.default}:{next(self._counter)}".encode() + b"\0"

    def _op_initialize(self, args: _Args) -> tuple[int, bytes]:
        name = args.name()
        self.requests.append(("INITIALIZE", name))
        self.caches[name] = _Cache(principal=args.principal())
        return 0, b""

    def _op_destroy(self, args: _Args) -> tuple[int, bytes]:
        name = args.name()
        self.requests.append(("DESTROY", name))
        del self.caches[name]
        return 0, b""

    def _op_store(self, args: _Args) -> tuple[int, bytes]:
        cache = self._named(args, "STORE")
        cache.creds[uuid.uuid4().bytes] = args.rest()
        return 0, b""

    def _op_retrieve(self, args: _Args) -> tuple[int, bytes]:
        self._named(args, "RETRIEVE")
        return KRB5_CC_NOSUPP, b""  # what sssd answers; MIT then walks the cache

    def _op_remove_cred(self, args: _Args) -> tuple[int, bytes]:
        self._named(args, "REMOVE_CRED")
        return KRB5_CC_NOSUPP, b""

    def _op_set_flags(self, args: _Args) -> tuple[int, bytes]:
        self._named(args, "SET_FLAGS")
        return 0, b""

    def _op_get_principal(self, args: _Args) -> tuple[int, bytes]:
        return 0, self._named(args, "GET_PRINCIPAL").principal

    def _op_get_cred_uuid_list(self, args: _Args) -> tuple[int, bytes]:
        return 0, b"".join(self._named(args, "GET_CRED_UUID_LIST").creds)

    def _op_get_cred_by_uuid(self, args: _Args) -> tuple[int, bytes]:
        cache = self._named(args, "GET_CRED_BY_UUID")
        cred = cache.creds.get(args.take(16))
        return (0, cred) if cred is not None else (KRB5_CC_END, b"")

    def _op_get_cred_list(self, args: _Args) -> tuple[int, bytes]:
        creds = list(self._named(args, "GET_CRED_LIST").creds.values())
        return 0, struct.pack(">I", len(creds)) + b"".join(
            struct.pack(">I", len(cred)) + cred for cred in creds
        )

    def _op_replace(self, args: _Args) -> tuple[int, bytes]:
        name = args.name()
        self.requests.append(("REPLACE", name))
        offset = args.i32()
        cache = _Cache(principal=args.principal(), offset=offset)
        for _ in range(args.u32()):
            cache.creds[uuid.uuid4().bytes] = args.take(args.u32())
        self.caches[name] = cache
        return 0, b""

    def _op_get_cache_uuid_list(self, args: _Args) -> tuple[int, bytes]:
        self.requests.append(("GET_CACHE_UUID_LIST", ""))
        return 0, b"".join(cache.uuid for cache in self.caches.values())

    def _op_get_cache_by_uuid(self, args: _Args) -> tuple[int, bytes]:
        self.requests.append(("GET_CACHE_BY_UUID", ""))
        wanted = args.take(16)
        for name, cache in self.caches.items():
            if cache.uuid == wanted:
                return 0, name.encode() + b"\0"
        return KRB5_FCC_NOFILE, b""

    def _op_get_default_cache(self, args: _Args) -> tuple[int, bytes]:
        self.requests.append(("GET_DEFAULT_CACHE", ""))
        return 0, self.default.encode() + b"\0"

    def _op_set_default_cache(self, args: _Args) -> tuple[int, bytes]:
        self.default = args.name()
        self.requests.append(("SET_DEFAULT_CACHE", self.default))
        return 0, b""

    def _op_get_kdc_offset(self, args: _Args) -> tuple[int, bytes]:
        return 0, struct.pack(">i", self._named(args, "GET_KDC_OFFSET").offset)

    def _op_set_kdc_offset(self, args: _Args) -> tuple[int, bytes]:
        cache = self._named(args, "SET_KDC_OFFSET")
        cache.offset = args.i32()
        return 0, b""


def _recv_exactly(conn: socket.socket, count: int) -> bytes | None:
    data = b""
    while len(data) < count:
        chunk = conn.recv(count - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def _recv_frame(conn: socket.socket) -> bytes | None:
    header = _recv_exactly(conn, 4)
    if header is None:
        return None
    return _recv_exactly(conn, struct.unpack(">I", header)[0])
