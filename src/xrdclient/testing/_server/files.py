"""Open-file and data I/O handlers of the fake XRootD server.

``kXR_open`` with stock xrootd's creation rules and its template forms
(``kXR_samefs``/``kXR_dup``), close, and every request that moves bytes:
read (optionally chunked into ``kXR_oksofar``), write, sync, readv, writev,
clone, pgread/pgwrite (with injectable CRC failures) and checkpoints. They are
grouped because they share open-handle state on the connection and the
``_splice`` write primitive.

``CHECKPOINT_CAPACITY`` and the ``_HANDLERS`` table a ``kXR_ckpXeq`` re-enters
belong to :mod:`xrdclient.testing.server` and are read from it at call time,
so a test that patches them there changes what these handlers do.

Handles are 4-byte big-endian counters per connection - shared with every
data path bound to it, so a read arriving on a sub-stream finds the file
opened on the control link. Data that a request carries *beyond* its counted
``dlen`` (``kXR_writev``, a checkpointed write) is read straight off the
connection's socket by the handler, which is why a handler that refuses such
a request must still drain it.
"""

from __future__ import annotations

import posixpath
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ...errors import kXR_ArgInvalid, kXR_FileNotOpen, kXR_NoSpace, kXR_Unsupported
from ...proto import constants as c
from ...proto.buffer import Reader, Writer
from .wire import _REQ, _clean, _error, _frame, _NotFound, pgwrite_cse

if TYPE_CHECKING:
    from ..server import _Connection


@dataclass
class _Checkpoint:
    """One open checkpoint: what to put back, and how much has been journaled.

    A real server journals the bytes each checkpointed write overwrites; this
    one snapshots the whole file at ``kXR_ckpBegin``, which gives the same
    rollback result for the file sizes tests use and is far simpler.
    """

    #: The file the checkpoint protects, as stored in ``FakeServer.files``.
    path: str
    #: The file's bytes at ``kXR_ckpBegin``, restored on rollback.
    snapshot: bytearray = field(default_factory=bytearray)
    #: Payload bytes journaled so far, against ``CHECKPOINT_CAPACITY``.
    used: int = 0


def _h_open(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_open``: allocate a handle, after stock xrootd's refusals.

    ``params`` is ``mode[2] options[2] optiont[2] reserved[6] fhtemplt[4]``;
    the template handle only counts when ``optiont`` asks for ``kXR_dup`` or
    ``kXR_samefs``. The reply is the 4-byte handle, and with
    ``kXR_retstat`` 8 reserved bytes (``cpsize``/``cptype``, unused) and the
    stat line, which saves the client a round trip. The raw path, CGI
    included, is recorded in ``opened`` - third-party copy is driven
    entirely through that CGI.
    """
    _mode, options, optiont = struct.unpack(">HHH", params[:6])
    raw = body.split(b"\x00", 1)[0].decode()
    conn.s.opened.append(raw)
    template = params[12:16] if optiont & (c.kXR_dup | c.kXR_samefs) else None
    path = _clean(raw)
    refusal = _template_refusal(conn, sid, options, optiont, template) if template else b""
    refusal = refusal or _target_refusal(conn, sid, path, options)
    if refusal:
        yield refusal
        return
    if template is not None:
        _colocate(conn, path, conn.handles[template], dup=bool(optiont & c.kXR_dup))
    handle = struct.pack(">I", conn.next_handle)
    conn.next_handle += 1
    conn.handles[handle] = path
    reply = handle
    if options & c.kXR_retstat:
        reply += bytes(8) + conn._stat_line(path) + b"\x00"
    yield _frame(sid, c.kXR_ok, reply)


def _target_refusal(conn: _Connection, sid: int, path: str, options: int) -> bytes:
    """Why ``path`` cannot be opened with ``options``, or ``b""`` once it is ready."""
    exists = path in conn.s.files
    if path in conn.s.dirs:
        return _error(sid, 3016, f"is a directory: {path}")
    if options & c.kXR_new and exists:
        return _error(sid, 3018, f"already exists: {path}")
    # Only kXR_new and kXR_delete create; kXR_open_updt and kXR_open_apnd on
    # their own are "open what is there", exactly as stock xrootd has it. The
    # fake used to create for any write flag, which let a client that never
    # asked for creation pass here and fail against a real server.
    if not exists:
        if not options & (c.kXR_new | c.kXR_delete):
            raise _NotFound(path)
        parent = posixpath.dirname(path)
        if parent not in conn.s.dirs and not options & c.kXR_mkpath:
            return _error(sid, 3011, f"no such file or directory: {parent}")
        conn.s.add_file(path)
    elif options & c.kXR_delete:
        conn.s.files[path] = bytearray()
    return b""


def _template_refusal(
    conn: _Connection, sid: int, options: int, optiont: int, template: bytes
) -> bytes:
    """Why xrootd would refuse an open against ``template``, or ``b""``.

    The checks, their order and their words are ``do_Open``'s.
    """
    dup = optiont & c.kXR_dup
    if not conn.s.colocates:
        what = "file cloning" if dup else "colocating with a specified file"
        return _error(sid, 3013, f"{what} is not supported")
    writes = not options & c.kXR_open_read and options & (c.kXR_open_updt | c.kXR_open_wrto)
    if dup and not writes:
        return _error(sid, 3000, "cloned file is not being opened R/W")
    if not options & c.kXR_new:
        return _error(sid, 3000, "file must be opened as a new file in order to colocate")
    if template not in conn.handles:
        return _error(sid, 3004, "file template does not refer to an open file")
    return b""


def _colocate(conn: _Connection, path: str, source: str, *, dup: bool) -> None:
    """Note where ``path`` was placed, and give it ``source``'s bytes for a dup."""
    conn.s.colocated[path] = source
    if dup:
        conn.s.files[path] = bytearray(conn.s.files[source])


def _h_close(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_close``: release the handle; an open checkpoint is committed by closing.

    Closing an unknown handle is ``kXR_FileNotOpen`` (3004), which a client's
    double-close must cope with.
    """
    conn.checkpoints.pop(params[:4], None)
    if conn.handles.pop(params[:4], None) is None:
        yield _error(sid, 3004, "file is not open")
        return
    yield _frame(sid, c.kXR_ok)


def _h_read(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_read`` at ``params[4:12]`` for ``params[12:16]`` bytes; short at end of file.

    With :attr:`FakeServer.chunk_reads` set the answer is split into
    ``kXR_oksofar`` frames ending in a ``kXR_ok``, as a server streaming a
    large read does - the client must reassemble without assuming a reply
    is one frame. Every read is logged in ``reads`` for range-sensitive tests.
    """
    offset, length = struct.unpack(">qi", params[4:16])
    path = conn._path(params, b"", at=slice(0, 4))
    conn.s.reads.append((path, offset, length))
    data = conn._file(path)[offset : offset + length]
    step = conn.s.chunk_reads
    if step and len(data) > step:
        for start in range(0, len(data) - step, step):
            yield _frame(sid, c.kXR_oksofar, bytes(data[start : start + step]))
        # The final kXR_ok carries the tail, never an empty body: start of
        # the last whole-or-partial chunk, so each frame is at most ``step``.
        last = ((len(data) - 1) // step) * step
        yield _frame(sid, c.kXR_ok, bytes(data[last:]))
        return
    yield _frame(sid, c.kXR_ok, bytes(data))


def _h_write(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_write`` of the body at ``params[4:12]``; a write past the end leaves a zero hole.

    The body may have been read from a bound data path - the dispatcher
    decides that from ``params[12]`` before this runs.
    """
    offset = struct.unpack(">q", params[4:12])[0]
    _splice(conn._file(conn._path(params, b"", at=slice(0, 4))), offset, body)
    yield _frame(sid, c.kXR_ok)


def _h_sync(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_sync``: nothing to flush in memory, but the handle must still be valid."""
    conn._path(params, b"", at=slice(0, 4))
    yield _frame(sid, c.kXR_ok)


def _h_readv(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_readv``: each 16-byte ``readahead_list`` entry answered in order, in one frame.

    Each element is echoed back as ``fhandle[4] rlen[4] offset[8]`` then the
    bytes, with ``rlen`` the length actually read (short at end of file) -
    the client must take the lengths from the reply, not from its request.
    """
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
    """``kXR_writev``: the descriptors are the body, their data follows on the socket.

    A body that is not a whole number of 16-byte descriptors is refused
    before any data is read, as xrootd does; the client then has a broken
    stream, which is the point - such a request is a client bug.
    """
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
        # One item: source fhandle[4], reserved[4], source offset[8],
        # length[8], destination offset[8].
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
    """``kXR_pgread``: a ``kXR_status`` frame, then the data with a CRC32C per page.

    The status body is ``crc32c[4] streamid[2] requestid[1] resptype[1]
    reserved[4] dlen[4]`` plus pgread's ``offset[8]``; ``dlen`` counts the
    paged payload, which follows the frame. The status CRC is left zero.
    Pages are aligned to file offsets (4 KiB), so the first and last may be
    partial - :func:`~xrdclient.crypto.crc32c.pack_pages` handles that.
    """
    from ...crypto.crc32c import pack_pages

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
    """``kXR_pgwrite``: store the pages, and report any with a bad CRC via ``kXR_status``.

    Pages are "corrupt" if their CRC really fails or if
    :attr:`FakeServer.pgwrite_corrupt` says to pretend so for that offset;
    either way the reply is :func:`~xrdclient.testing.server.pgwrite_cse`'s
    list of offsets and the client must resend those pages. ``params[13]``
    is the request flags byte, logged so a test can tell a ``kXR_pgRetry``
    resend from a first attempt.
    """
    from ...crypto.crc32c import page_span, unpack_pages

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


def _h_chkpoint(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_chkpoint``: begin, query, commit, roll back, or execute under a checkpoint.

    The subcode is ``params[15]``. ``kXR_ckpQuery`` answers ``maxsize[4]
    useCnt[4]`` - capacity and bytes journaled - so a client can tell
    whether its next write still fits. Only one checkpoint per handle, as
    in xrootd; a second ``kXR_ckpBegin`` replaces the first here.
    """
    # Through the facade, at call time: tests patch ``CHECKPOINT_CAPACITY`` there.
    from .. import server

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
        yield _frame(
            sid, c.kXR_ok, struct.pack(">II", server.CHECKPOINT_CAPACITY, open_checkpoint.used)
        )
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
    # Through the facade, at call time: tests patch ``CHECKPOINT_CAPACITY`` there.
    from .. import server

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
    if state.used > server.CHECKPOINT_CAPACITY:
        yield _error(sid, kXR_NoSpace, "checkpoint is full")
        return
    yield from server._HANDLERS[opcode](conn, sid, inner, data)


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
