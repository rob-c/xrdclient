"""Open-file request encoders: open/close, reads, writes, vectors, pages, checkpoints, xattrs.

Part of :mod:`xrdclient.proto.requests`, which re-exports every class here;
import them from there. Everything in this module (apart from ``Open``
itself, which produces the handle) addresses an open file by its 4-byte
handle, and these are the requests on the data path: the ones
:mod:`xrdclient.client.file` and the bulk plane send by the thousand, so
several encode their header from a precomputed ``struct`` layout in
``_requests_base`` instead of building it field by field. ``kXR_fattr`` is
here too because it shares the handle-or-path addressing and its body
builders. Session, namespace and query encoders live in the sibling
``_requests_*`` modules.

Layouts follow nginx-xrootd ``wire_core_requests.h``; cross-checked against
go-hep ``xrootd/xrdproto/<op>`` and XRootD.jl ``Wire/requests.jl``. Every
``params`` method writes exactly 16 bytes (header offsets 4..19).

The hook contract (``params``, ``header_params``, ``payload``,
``path_data``, ``reply_cap``, ``trailer``) is documented once, on
:class:`~xrdclient.proto.frames.Request`; the docstrings below say only what
each opcode puts in them. What a maintainer must not break: a
``header_params`` override must pack byte-for-byte what its ``params`` writes
(``tests`` compare the two for every class), and :attr:`Request.idempotent`
decides whether the session may replay a request after a reconnect - setting
it on something that changes server state turns a dropped link into a
silently doubled write.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..errors import ProtocolError
from . import constants as c
from ._requests_base import (
    _HANDLE_PARAMS,
    _OPEN_PARAMS,
    _READ_PARAMS,
    _WRITE_PARAMS,
    _byte_view,
    _encode_path,
    _handle,
)
from .buffer import Writer
from .frames import Payload, Request, encode

#: The ``kXR_open`` option bits that change something: create, replace,
#: truncate, or open for writing. An open carrying none of them only reads.
_OPEN_WRITES = c.kXR_delete | c.kXR_new | c.kXR_open_updt | c.kXR_open_wrto | c.kXR_open_apnd


class Open(Request):
    """``kXR_open``.

    A read-only open may be replayed; one that writes may not. Re-opening a
    file for reading has no effect the first open did not already have, so a
    connection that dropped during or right after it can simply be dialled
    again - which is what lets a read survive a flaky link. An open that
    creates, truncates or writes can leave the namespace changed even when
    its answer never arrived, so it is never retried blind; the copy engine
    restarts those deliberately instead. :attr:`idempotent` is therefore set
    per instance, from the flags, rather than fixed for the type.
    """

    __slots__ = ("fhtemplt", "idempotent", "mode", "options", "optiont", "path")
    opcode = c.kXR_open
    signed = True

    def __init__(
        self,
        path: str,
        options: int,
        mode: int = 0,
        *,
        optiont: int = 0,
        fhtemplt: bytes = c.NULL_FHANDLE,
    ) -> None:
        """Open ``path`` (opaque ``?cgi`` included) with ``kXR_open`` option bits.

        ``mode`` is the ``kXR_ur``-style permission set the server applies
        only when the open creates the file. ``optiont`` and ``fhtemplt`` are
        the protocol 5.2 template-file extension (``kXR_PROTCLONEVERSION``);
        :mod:`xrdclient.client._handle` refuses to send them to an older
        server, as XrdCl does, rather than let it misread the bytes.
        """
        self.path = path
        self.options = options
        self.idempotent = not options & _OPEN_WRITES
        self.mode = mode
        #: ``kXR_dup`` / ``kXR_samefs``: what to do with the file ``fhtemplt``
        #: names, a handle open on the same connection.
        self.optiont = optiont
        self.fhtemplt = fhtemplt

    def params(self, w: Writer) -> None:
        """``mode[2] options[2] optiont[2] reserved[6] fhtemplt[4]``."""
        w.u16(self.mode).u16(self.options).u16(self.optiont).zeros(6).padded(self.fhtemplt, 4)

    def header_params(self) -> bytes:
        """The same 16 bytes from one ``struct`` pack: every file operation opens."""
        return _OPEN_PARAMS.pack(self.mode, self.options, self.optiont, self.fhtemplt)

    def payload(self) -> bytes:
        """The path, unterminated: ``dlen`` delimits it."""
        return _encode_path(self.path)

    def __repr__(self) -> str:
        """Path and flags in the radix ``xrdfs``/XrdCl logs print them in."""
        return f"Open(path={self.path!r}, options=0x{self.options:04x}, mode=0o{self.mode:o})"


class Close(Request):
    """``kXR_close``: release a handle; the server flushes and drops it.

    Not idempotent: handles belong to the connection that opened them, so a
    close replayed on a fresh connection would name a handle that does not
    exist there (or, worse, one that a later open was given).
    """

    __slots__ = ("fhandle",)
    opcode = c.kXR_close
    idempotent = False

    def __init__(self, fhandle: bytes) -> None:
        """Close the file the 4-byte ``fhandle`` from ``kXR_open`` names."""
        self.fhandle = fhandle

    def params(self, w: Writer) -> None:
        """``fhandle[4] reserved[12]``."""
        w.padded(self.fhandle, 4).zeros(12)

    def header_params(self) -> bytes:
        """The handle-then-zeros layout ``Sync`` and ``Clone`` share."""
        return _HANDLE_PARAMS.pack(_handle(self.fhandle))


class Read(Request):
    """``kXR_read``: one contiguous range of an open file.

    The answer may arrive as several ``kXR_oksofar`` chunks before the final
    ``kXR_ok``; the state machine concatenates them, bounded by
    :meth:`reply_cap`. With a ``pathid`` the data comes back on that bound
    socket, hence :attr:`reply_on_path`.
    """

    __slots__ = ("fhandle", "length", "offset", "pathid")
    opcode = c.kXR_read
    reply_on_path = True

    def __init__(self, fhandle: bytes, offset: int, length: int, pathid: int = 0) -> None:
        """Read ``length`` bytes at ``offset``; ``pathid`` 0 answers on the control link."""
        self.fhandle = fhandle
        self.offset = offset
        self.length = length
        self.pathid = pathid

    def params(self, w: Writer) -> None:
        """``fhandle[4] offset[8] rlen[4]``, both signed as the spec declares them."""
        w.padded(self.fhandle, 4).i64(self.offset).i32(self.length)

    def header_params(self) -> bytes:
        """Single-pack fast path: reads are the most frequent frame there is."""
        return _READ_PARAMS.pack(_handle(self.fhandle), self.offset, self.length)

    def payload(self) -> bytes:
        """The optional ``read_args`` block that names a bound path."""
        # ``read_args``: the path to answer on, then seven reserved bytes.
        # Absent entirely when there is no path, because older servers size
        # the request from ``dlen`` and reject a body they did not ask for.
        return Writer().u8(self.pathid).zeros(7).bytes() if self.pathid else b""

    def reply_cap(self) -> int:
        """Exactly the length asked for: a server never owes more than that."""
        return self.length

    def __repr__(self) -> str:
        """Range only; the handle is meaningless outside its connection."""
        return f"Read(offset={self.offset}, length={self.length})"


class Write(Request):
    """``kXR_write``: one contiguous range, data carried in the body.

    With a ``pathid`` the data travels on the bound socket while the header
    (whose ``dlen`` still counts it) goes on the control link - see
    :meth:`Request.path_data`. Never replayed: a write whose answer was lost
    may already be on disk, and the caller decides whether rewriting is safe.
    """

    __slots__ = ("data", "fhandle", "offset", "pathid")
    opcode = c.kXR_write
    signed = True
    idempotent = False

    def __init__(self, fhandle: bytes, offset: int, data: Payload, pathid: int = 0) -> None:
        """Write ``data`` at ``offset``; the buffer is borrowed, not copied."""
        self.fhandle = fhandle
        self.offset = offset
        #: The bytes to write - a caller's buffer is kept as a byte view of
        #: itself rather than copied, so the payload reaches the socket call
        #: as the very memory it was handed in. The caller must leave it
        #: alone until the write has been answered, as with ``sendall``.
        self.data = data if isinstance(data, bytes) else _byte_view(data)
        self.pathid = pathid

    def params(self, w: Writer) -> None:
        """``fhandle[4] offset[8] pathid[1] reserved[3]``."""
        w.padded(self.fhandle, 4).i64(self.offset).u8(self.pathid).zeros(3)

    def header_params(self) -> bytes:
        """Single-pack fast path for the bulk-upload hot loop."""
        return _WRITE_PARAMS.pack(_handle(self.fhandle), self.offset, self.pathid)

    def payload(self) -> Payload:
        """The data, unless a bound path carries it instead."""
        return b"" if self.pathid else self.data

    def path_data(self) -> Payload:
        """The data when it goes over ``pathid``; never both this and :meth:`payload`."""
        return self.data if self.pathid else b""

    def __repr__(self) -> str:
        """Offset and size, never the data itself - it may be anything the user owns."""
        return f"Write(offset={self.offset}, len={len(self.data)})"


class Sync(Request):
    """``kXR_sync``: ask the server to ``fsync`` the open file.

    Marked non-idempotent only because it is meaningless on a fresh
    connection: the handle it names died with the old one.
    """

    __slots__ = ("fhandle",)
    opcode = c.kXR_sync
    idempotent = False

    def __init__(self, fhandle: bytes) -> None:
        """Sync the file ``fhandle`` names."""
        self.fhandle = fhandle

    def params(self, w: Writer) -> None:
        """``fhandle[4] reserved[12]``."""
        w.padded(self.fhandle, 4).zeros(12)

    def header_params(self) -> bytes:
        """The shared handle-only layout."""
        return _HANDLE_PARAMS.pack(_handle(self.fhandle))


class ReadV(Request):
    """``kXR_readv`` - many scattered ranges in one round trip.

    Payload is an array of ``readahead_list``:
    ``fhandle[4] rlen[4] roffset[8]``. Each entry names its own handle, so
    one request may read from several files open on the same connection.
    Servers cap the entry count (1024 in XRootD) and the total size;
    splitting a larger vector is the caller's job
    (:mod:`xrdclient.client._vector`, ``READV_MAX_*``).
    """

    __slots__ = ("chunks", "pathid")
    opcode = c.kXR_readv
    reply_on_path = True

    def __init__(self, chunks: Sequence[tuple[bytes, int, int]], pathid: int = 0) -> None:
        """Read every ``(fhandle, offset, length)`` in ``chunks``, in order."""
        #: ``(fhandle, offset, length)`` triples.
        self.chunks = list(chunks)
        self.pathid = pathid

    def params(self, w: Writer) -> None:
        """``reserved[15] pathid[1]`` - the path id sits in the last byte."""
        w.zeros(15).u8(self.pathid)

    def payload(self) -> bytes:
        """The ``readahead_list`` array; note length precedes offset here."""
        w = Writer()
        for fhandle, offset, length in self.chunks:
            w.padded(fhandle, 4).i32(length).i64(offset)
        return w.bytes()

    def reply_cap(self) -> int:
        """Every byte asked for plus one 16-byte descriptor per chunk."""
        # Every chunk comes back behind its own ``read_list`` descriptor.
        wanted = sum(length for _, _, length in self.chunks)
        return c.READ_LIST_ENTRY_LEN * len(self.chunks) + wanted

    def __repr__(self) -> str:
        """Chunk count only: a vector of thousands would flood a log line."""
        return f"ReadV(chunks={len(self.chunks)})"


class WriteV(Request):
    """``kXR_writev``.

    ``dlen`` covers the array of ``write_list`` (``fhandle[4] wlen[4]
    offset[8]``) and nothing else - servers check that it divides by 16 and
    answer ``kXR_ArgInvalid`` when it does not. The concatenated data streams
    after the frame as a trailer.
    """

    __slots__ = ("chunks", "options")
    opcode = c.kXR_writev
    signed = True
    idempotent = False

    def __init__(self, chunks: Sequence[tuple[bytes, int, bytes]], sync: bool = False) -> None:
        """Write every ``(fhandle, offset, data)``; ``sync`` fsyncs once all are done."""
        #: ``(fhandle, offset, data)`` triples.
        self.chunks = list(chunks)
        self.options = c.kXR_wv_doSync if sync else 0

    def params(self, w: Writer) -> None:
        """``options[1] reserved[15]``; ``kXR_wv_doSync`` is the only option bit."""
        w.u8(self.options).zeros(15)

    def payload(self) -> bytes:
        """The ``write_list`` descriptors alone - the data is the trailer."""
        w = Writer()
        for fhandle, offset, data in self.chunks:
            w.padded(fhandle, 4).i32(len(data)).i64(offset)
        return w.bytes()

    def trailer(self) -> bytes:
        """Every chunk's data, concatenated in descriptor order."""
        return b"".join(data for _, _, data in self.chunks)

    def __repr__(self) -> str:
        """Chunk count only, as for :class:`ReadV`."""
        return f"WriteV(chunks={len(self.chunks)})"


class Clone(Request):
    """``kXR_clone`` - copy byte ranges between two open files, server-side.

    The parameter area is the destination handle; the payload is an array of
    ``clone_item``: ``src_fhandle[4] reserved[4] offset[8] length[8]
    target_offset[8]``, all big-endian, one per range. Protocol 5.2 and later
    only; nothing crosses the client, which is the point.
    """

    __slots__ = ("fhandle", "items")
    opcode = c.kXR_clone
    signed = True
    idempotent = False

    def __init__(self, fhandle: bytes, items: Sequence[tuple[bytes, int, int, int]]) -> None:
        """Clone ``items`` into the file ``fhandle`` names."""
        #: The destination handle: where every range is written.
        self.fhandle = fhandle
        #: ``(src_fhandle, offset, length, target_offset)`` quadruples.
        self.items = list(items)

    def params(self, w: Writer) -> None:
        """``fhandle[4] reserved[12]`` - the destination handle."""
        w.padded(self.fhandle, 4).zeros(12)

    def header_params(self) -> bytes:
        """The shared handle-only layout."""
        return _HANDLE_PARAMS.pack(_handle(self.fhandle))

    def payload(self) -> bytes:
        """One 32-byte ``clone_item`` per range (``CLONE_ITEM_LEN``)."""
        w = Writer()
        for source, offset, length, target in self.items:
            w.padded(source, 4).zeros(4).i64(offset).i64(length).i64(target)
        return w.bytes()

    def __repr__(self) -> str:
        """Range count only."""
        return f"Clone(ranges={len(self.items)})"


class PgRead(Request):
    """``kXR_pgread`` - read with a per-4KiB-page CRC32c.

    The reply interleaves a 4-byte CRC32c before every page of data (pages
    aligned to absolute file offsets, so the first and last may be short);
    :func:`xrdclient.crypto.crc32c.unpack_pages` checks and strips them.
    ``retry`` sets ``kXR_pgRetry``, telling the server this re-reads pages
    that failed their checksum rather than reading afresh.
    """

    __slots__ = ("fhandle", "length", "offset", "pathid", "reqflags")
    opcode = c.kXR_pgread
    reply_on_path = True

    def __init__(
        self, fhandle: bytes, offset: int, length: int, retry: bool = False, pathid: int = 0
    ) -> None:
        """Read ``length`` data bytes at ``offset`` (the reply is longer: CRCs)."""
        self.fhandle = fhandle
        self.offset = offset
        self.length = length
        self.reqflags = c.kXR_pgRetry if retry else 0
        self.pathid = pathid

    def params(self, w: Writer) -> None:
        """``fhandle[4] offset[8] length[4]`` - the same layout as ``kXR_read``."""
        w.padded(self.fhandle, 4).i64(self.offset).i32(self.length)

    def header_params(self) -> bytes:
        """Reuses the ``kXR_read`` layout, which it shares byte for byte."""
        return _READ_PARAMS.pack(_handle(self.fhandle), self.offset, self.length)

    def payload(self) -> bytes:
        """``pathid[1] reqflags[1] reserved[2]``, or nothing when both are zero."""
        if not self.reqflags and not self.pathid:
            return b""
        return Writer().u8(self.pathid).u8(self.reqflags).zeros(2).bytes()

    def reply_cap(self) -> int:
        """The data plus 4 bytes of CRC for every page the range touches."""
        # A CRC in front of every page, and an unaligned read straddles one
        # page more than its length alone accounts for.
        pages = -(-(self.offset % c.kXR_pgPageSZ + self.length) // c.kXR_pgPageSZ)
        return self.length + (c.kXR_pgUnitSZ - c.kXR_pgPageSZ) * pages

    def __repr__(self) -> str:
        """Range only, as for :class:`Read`."""
        return f"PgRead(offset={self.offset}, length={self.length})"


class PgWrite(Request):
    """``kXR_pgwrite`` - write with a per-page CRC32c prefix on each page.

    The server verifies every page and answers with the offsets of those that
    failed (a ``kXR_status`` body), which the client retransmits with
    ``retry`` set, up to ``PGW_MAX_RETRY`` times per page.
    """

    __slots__ = ("data", "fhandle", "offset", "pathid", "reqflags")
    opcode = c.kXR_pgwrite
    signed = True
    idempotent = False

    def __init__(
        self, fhandle: bytes, offset: int, data: bytes, retry: bool = False, pathid: int = 0
    ) -> None:
        """Write pre-interleaved ``data`` at ``offset`` (a file offset, not a wire one)."""
        self.fhandle = fhandle
        self.offset = offset
        #: Already CRC-interleaved payload; build it with :func:`pack_pages`.
        self.data = data
        self.reqflags = c.kXR_pgRetry if retry else 0
        self.pathid = pathid

    def params(self, w: Writer) -> None:
        """``fhandle[4] offset[8] pathid[1] reqflags[1] reserved[2]``."""
        w.padded(self.fhandle, 4).i64(self.offset).u8(self.pathid).u8(self.reqflags).zeros(2)

    def payload(self) -> bytes:
        """The interleaved pages, unless a bound path carries them."""
        return b"" if self.pathid else self.data

    def path_data(self) -> bytes:
        """The interleaved pages when ``pathid`` carries them."""
        return self.data if self.pathid else b""

    def reply_cap(self) -> int:
        """The ``cse`` header plus an 8-byte offset for every page sent."""
        # Nothing comes back but the offsets of the pages that arrived
        # corrupt, and at worst that is all of them. An unaligned first page
        # is shorter than a unit, so it can be one more than the division.
        pages = -(-len(self.data) // c.kXR_pgUnitSZ) + 1
        return c.PGW_CSE_HDRLEN + 8 * pages

    def __repr__(self) -> str:
        """Offset and wire size (CRCs included), never the data."""
        return f"PgWrite(offset={self.offset}, len={len(self.data)})"


class ChkPoint(Request):
    """``kXR_chkpoint`` - transactional write checkpointing.

    ``kXR_ckpBegin`` snapshots the file, ``kXR_ckpXeq`` runs a write or a
    truncate inside the checkpoint, and ``kXR_ckpCommit`` / ``kXR_ckpRollback``
    keep or undo everything since the begin. Protocol 5 servers only.
    :meth:`execute` builds the ``kXR_ckpXeq`` form; the others need no body.
    """

    __slots__ = ("data", "fhandle", "subcode", "tail")
    opcode = c.kXR_chkpoint
    signed = True
    idempotent = False

    #: The three the server knows how to undo, and so the three it will run.
    EXECUTABLE = frozenset({c.kXR_write, c.kXR_pgwrite, c.kXR_truncate})

    def __init__(self, fhandle: bytes, subcode: int, data: bytes = b"", tail: bytes = b"") -> None:
        """A raw checkpoint frame; prefer :meth:`execute` for ``kXR_ckpXeq``.

        ``data`` is the counted body and ``tail`` the uncounted trailer; only
        ``kXR_ckpXeq`` uses either.
        """
        self.fhandle = fhandle
        self.subcode = subcode
        self.data = data
        self.tail = tail

    @classmethod
    def execute(cls, fhandle: bytes, inner: Request) -> ChkPoint:
        """``kXR_ckpXeq`` - run ``inner`` as part of the open checkpoint.

        The embedded request is split the way the wire wants it: its 24-byte
        header is the payload ``dlen`` counts, and its data follows the frame
        uncounted, exactly as ``kXR_writev`` streams its own. The embedded
        header carries stream id 0 - the answer comes back on the outer
        frame's.
        """
        if inner.opcode not in cls.EXECUTABLE:
            raise ProtocolError(
                f"a checkpoint can execute a write or a truncate, "
                f"not {c.request_name(inner.opcode)}"
            )
        frame = encode(inner, 0)
        return cls(fhandle, c.kXR_ckpXeq, frame[: c.REQUEST_HDRLEN], frame[c.REQUEST_HDRLEN :])

    def params(self, w: Writer) -> None:
        """``fhandle[4] reserved[11] opcode[1]``; the spec's ``opcode`` is the subcode."""
        w.padded(self.fhandle, 4).zeros(11).u8(self.subcode)

    def payload(self) -> bytes:
        """The embedded request header for ``kXR_ckpXeq``, else empty."""
        return self.data

    def trailer(self) -> bytes:
        """The embedded request's own body, outside ``dlen`` and the signature."""
        return self.tail

    def __repr__(self) -> str:
        """Subcode only."""
        return f"ChkPoint(subcode={self.subcode})"


class Fattr(Request):
    """``kXR_fattr`` - extended attributes.

    Body layout (go-hep ``xrdproto/fattr``): ``path\\0`` then, per attribute,
    a 2-byte rc placeholder, ``name\\0``, and for ``SET`` a 4-byte length
    followed by the value. The request addresses either a path (with a null
    handle) or an open file (with an empty path), never both; the classmethod
    builders produce one-attribute bodies, which is all the client sends.
    Get and list are replayable; set and delete change state and are not.
    """

    __slots__ = ("body", "fhandle", "idempotent", "numattr", "options", "subcode")
    opcode = c.kXR_fattr
    signed = True

    def __init__(
        self,
        subcode: int,
        body: bytes,
        numattr: int = 0,
        options: int = 0,
        fhandle: bytes = c.NULL_FHANDLE,
    ) -> None:
        """A raw frame; use :meth:`get`, :meth:`set`, :meth:`delete` or :meth:`list`.

        ``numattr`` must match the names in ``body`` - the server walks the
        body by that count, and XRootD rejects more than ``kXR_fattrMaxVars``.
        """
        self.subcode = subcode
        self.numattr = numattr
        self.options = options
        self.fhandle = fhandle
        self.body = body
        self.idempotent = subcode in (c.kXR_fattrGet, c.kXR_fattrList)

    def params(self, w: Writer) -> None:
        """``fhandle[4] subcode[1] numattr[1] options[1] reserved[9]``."""
        w.padded(self.fhandle, 4).u8(self.subcode).u8(self.numattr).u8(self.options).zeros(9)

    def payload(self) -> bytes:
        """The prebuilt body; see the class docstring for its layout."""
        return self.body

    # -- body builders --------------------------------------------------

    @staticmethod
    def _path_name(path: str, name: str) -> bytes:
        """``path\\0`` then one ``rc[2] name\\0`` entry; the server fills ``rc`` in."""
        return Writer().text(path, nul=True).u16(0).text(name, nul=True).bytes()

    @classmethod
    def get(cls, path: str, name: str, fhandle: bytes = c.NULL_FHANDLE) -> Fattr:
        """``kXR_fattrGet`` of one attribute, by path or (empty path) by handle."""
        return cls(c.kXR_fattrGet, cls._path_name(path, name), numattr=1, fhandle=fhandle)

    @classmethod
    def delete(cls, path: str, name: str, fhandle: bytes = c.NULL_FHANDLE) -> Fattr:
        """``kXR_fattrDel`` of one attribute."""
        return cls(c.kXR_fattrDel, cls._path_name(path, name), numattr=1, fhandle=fhandle)

    @classmethod
    def set(
        cls,
        path: str,
        name: str,
        value: bytes,
        create_only: bool = False,
        fhandle: bytes = c.NULL_FHANDLE,
    ) -> Fattr:
        """``kXR_fattrSet`` of one attribute; the value list follows the name list.

        ``create_only`` sets ``kXR_fattrIsNew``, making the server refuse to
        overwrite an existing attribute (``XATTR_CREATE`` semantics).
        """
        body = cls._path_name(path, name) + Writer().u32(len(value)).raw(value).bytes()
        return cls(
            c.kXR_fattrSet,
            body,
            numattr=1,
            options=c.kXR_fattrIsNew if create_only else 0,
            fhandle=fhandle,
        )

    @classmethod
    def list(
        cls,
        path: str,
        values: bool = False,
        fhandle: bytes = c.NULL_FHANDLE,
        recurse: bool = False,
    ) -> Fattr:
        """``kXR_fattrList``: the names, or with ``values`` (``kXR_fattrAData``) the pairs.

        ``recurse`` is the nginx-xrootd extension that lists every file
        under a directory; its reply has a different shape
        (``parse_fattr_tree``), so only ask a server known to support it.
        """
        options = (c.kXR_fattrAData if values else 0) | (c.kXR_fattrRecurse if recurse else 0)
        return cls(
            c.kXR_fattrList,
            Writer().text(path, nul=True).bytes(),
            options=options,
            fhandle=fhandle,
        )

    def __repr__(self) -> str:
        """Subcode and count; names and values may be private."""
        return f"Fattr(subcode={self.subcode}, numattr={self.numattr})"
