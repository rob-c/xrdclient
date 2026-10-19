"""Extended-attribute (``kXR_fattr``) handlers of the fake XRootD server.

One request with four subcodes - list, get, set, delete - each with its own
reply shape: a list is bare ``name\\0 [len value]`` records (or, recursively,
``relpath:name`` entries), the others a counted, per-attribute coded reply
built by ``_fattr_reply``. The per-subcode edits are dispatched through
``_FATTR_EDITS``. Attributes live in
:attr:`FakeServer.xattrs <xrdclient.testing.server.FakeServer.xattrs>`.
Registered by opcode in ``xrdclient.testing.server._HANDLERS``.

The request body is parsed per attribute as ``rc[2] name\0 [vlen[4] value]``.
The protocol proper sends all the names and then all the values; for the
single-attribute requests :class:`xrdclient.proto.Fattr` builds, the two
layouts are byte-for-byte the same, and a multi-attribute test would need
this parser changed first.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

from ...errors import kXR_AttrNotFound, kXR_ItExists
from ...proto import constants as c
from ...proto.buffer import Reader, Writer
from .wire import _clean, _frame, _NotFound

if TYPE_CHECKING:
    from ..server import _Connection


def _h_fattr(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_fattr``: ``params`` is ``fhandle[4] subcode[1] numattr[1] options[1]``.

    A recursive list (``kXR_fattrRecurse``) only applies to a directory; on
    a file it falls through to an ordinary list. Getting or listing
    on a path that has no attributes yet creates an empty store for it,
    which is invisible to a client.
    """
    subcode, numattr, options = params[4], params[5], params[6]
    reader = Reader(body, "kXR_fattr")
    path = _fattr_path(conn, params, reader)
    if subcode == c.kXR_fattrList and options & c.kXR_fattrRecurse and path in conn.s.dirs:
        yield _frame(sid, c.kXR_ok, _fattr_walk(conn, path))
        return
    store = conn.s.xattrs.setdefault(path, {})
    if subcode == c.kXR_fattrList:
        yield _frame(sid, c.kXR_ok, _fattr_list(store, bool(options & c.kXR_fattrAData)))
        return
    yield _frame(sid, c.kXR_ok, _fattr_edit(store, reader, subcode, numattr, options))


def _fattr_path(conn: _Connection, params: bytes, reader: Reader) -> str:
    """The path the request names, by body or by open handle; must exist."""
    path = reader.cstring()
    if not path:
        path = conn.handles.get(params[:4], "")
    path = _clean(path)
    if path not in conn.s.files and path not in conn.s.dirs:
        raise _NotFound(path)
    return path


def _fattr_edit(
    store: dict[str, bytes], reader: Reader, subcode: int, numattr: int, options: int
) -> bytes:
    """Apply a set, get or delete to each named attribute; the coded reply."""
    items: list[tuple[str, int, bytes | None]] = []
    edit = _FATTR_EDITS.get(subcode)
    for _ in range(max(numattr, 1)):
        if reader.remaining < 3:
            break
        reader.u16()  # the rc placeholder a request carries, always zero
        name = reader.cstring()
        if edit is not None:
            items.append(edit(store, name, reader, options))
    return _fattr_reply(items)


def _fattr_walk(conn: _Connection, path: str) -> bytes:
    """The recursive reply: a flat run of ``relpath:name`` entries with no
    header, and only regular files carry them - directories in the way are
    walked through rather than reported."""
    prefix = path.rstrip("/") + "/"
    entries = bytearray()
    for name in sorted(conn.s.files):
        if not name.startswith(prefix):
            continue
        for attribute in sorted(conn.s.xattrs.get(name, {})):
            entries += f"{name[len(prefix) :]}:{attribute}".encode() + b"\x00"
    return bytes(entries)


def _fattr_list(store: dict[str, bytes], values: bool) -> bytes:
    """A list reply is bare ``name\\0 [len value]`` records: no count header
    and no per-attribute code, unlike the other subcodes."""
    reply = Writer()
    for name in sorted(store):
        reply.text(name, nul=True)
        if values:
            reply.i32(len(store[name])).raw(store[name])
    return reply.bytes()


def _fattr_set(
    store: dict[str, bytes], name: str, reader: Reader, options: int
) -> tuple[str, int, bytes | None]:
    """Set ``name``; with ``kXR_fattrIsNew`` an existing one is ``kXR_ItExists``, kept as is.

    The value is consumed from ``reader`` even when refused, so the next
    attribute in the body still parses from the right place.
    """
    value = reader.bytes(reader.i32())
    if options & c.kXR_fattrIsNew and name in store:
        return (name, kXR_ItExists, None)
    store[name] = bytes(value)
    return (name, 0, None)


def _fattr_get(
    store: dict[str, bytes], name: str, reader: Reader, options: int
) -> tuple[str, int, bytes | None]:
    """Get ``name``: its value, or ``kXR_AttrNotFound`` with an empty value.

    A missing attribute still carries a zero-length value in the reply,
    which a client must not mistake for an attribute set to ``b""``.
    """
    current = store.get(name)
    return (name, 0 if current is not None else kXR_AttrNotFound, current or b"")


def _fattr_del(
    store: dict[str, bytes], name: str, reader: Reader, options: int
) -> tuple[str, int, bytes | None]:
    """Delete ``name``; one that was not there is ``kXR_AttrNotFound``."""
    return (name, 0 if store.pop(name, None) is not None else kXR_AttrNotFound, None)


#: Subcode to the per-attribute edit; each returns ``(name, rc, value)``
#: where ``value`` is ``None`` for subcodes whose reply carries none.
_FATTR_EDITS: dict[
    int, Callable[[dict[str, bytes], str, Reader, int], tuple[str, int, bytes | None]]
] = {
    c.kXR_fattrSet: _fattr_set,
    c.kXR_fattrGet: _fattr_get,
    c.kXR_fattrDel: _fattr_del,
}


def _fattr_reply(items: list[tuple[str, int, bytes | None]]) -> bytes:
    """The coded reply: ``nerrs[1] nattr[1]``, then ``rc[2] name\0 [vlen[4] value]`` each.

    The request as a whole succeeds (``kXR_ok``) even when every attribute
    failed; per-attribute ``rc`` codes are how failures are reported,
    which is the part of the protocol client code most often gets wrong.
    """
    w = Writer().u8(sum(1 for _, code, _ in items if code)).u8(len(items))
    for name, code, value in items:
        w.u16(code).text(name, nul=True)
        if value is not None:
            w.i32(len(value)).raw(value)
    return w.bytes()
