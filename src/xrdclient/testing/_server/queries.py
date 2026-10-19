"""Query, locate and prepare handlers of the fake XRootD server.

``kXR_query`` is a family of questions keyed by its infotype - checksum,
config values, checksum cancellation, statistics, space, prepare status,
visa - dispatched through the ``_QUERIES`` table, with XrdCl's ``Fcntl``
(``kXR_Qopaqug``) taken first by ``_h_query_fctl``. ``kXR_locate`` and
``kXR_prepare`` (staging, cancellation, eviction) sit alongside because they
are the other "ask the server about a file" requests. Registered by opcode in
``xrdclient.testing.server._HANDLERS``.

The checksum itself comes from ``xrdclient.testing.server._checksum``, looked
up at call time so a test can patch it there.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

from ...proto import constants as c
from .wire import _clean, _error, _frame, _NotFound

if TYPE_CHECKING:
    from ..server import _Connection


#: What a name answers when ``config_values`` does not say. This fake takes
#: part in the ``XrdOucTPC`` rendezvous (it accepts the keyed opens and both
#: syncs), so it says so to XrdCl's ``CheckTPC`` - ``tpc`` is a non-zero
#: number - and, like a stock server with no ``ofs.tpc fcreds``, echoes
#: ``tpcdlg``: no TPC lite.
_ADVERTISED = {"tpc": "1", "tpcdlg": "tpcdlg"}


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
    return _ADVERTISED.get(name, "")


def _q_cksum(conn: _Connection, sid: int, args: str) -> Iterator[bytes]:
    """``kXR_Qcksum``: ``"<algorithm> <value>"``, as xrootd's checksum manager answers.

    The algorithm comes from a ``cks.type=`` CGI on the path - the way a
    client picks one when the server has several - and defaults to adler32,
    the configured default of almost every grid storage element.
    """
    # Through the facade, at call time: tests patch ``_checksum`` there.
    from .. import server

    path = _clean(args)
    algorithm = "adler32"
    for pair in args.partition("?")[2].split("&"):
        if pair.startswith("cks.type="):
            algorithm = pair.split("=", 1)[1]
    value = server._checksum(algorithm, bytes(conn._file(path)))
    yield _frame(sid, c.kXR_ok, f"{algorithm} {value}".encode() + b"\x00")


def _q_config(conn: _Connection, sid: int, args: str) -> Iterator[bytes]:
    """``kXR_Qconfig``: one value per name asked, in order.

    An unknown name answers an empty line here, so a client's "not
    configured" branch is exercised by simply asking for something unset.
    Names are separated by newlines, or by spaces as XrdCl's ``CheckTPCLite``
    sends them (``tpc tpcdlg``) and ``XrdXrootdProtocol::do_Qconf`` reads
    them; a test that set an answer for the whole question at once - the
    multi-name ``config_values`` key - is given that answer as it stands.
    """
    if args in conn.s.config_values:
        yield _frame(sid, c.kXR_ok, conn.s.config_values[args].encode() + b"\x00")
        return
    names = [name for line in args.split("\n") for name in line.split(" ")]
    values = [_config_value(conn, n) for n in names]
    yield _frame(sid, c.kXR_ok, "\n".join(values).encode() + b"\x00")


def _q_ckscan(conn: _Connection, sid: int, args: str) -> Iterator[bytes]:
    """``kXR_Qckscan``: XrdCl's checksum *cancel*; recorded so a test can see it was sent."""
    conn.s.cancelled_checksums.append(_clean(args))
    yield _frame(sid, c.kXR_ok)


def _q_stats(conn: _Connection, sid: int, args: str) -> Iterator[bytes]:
    """``kXR_QStats``: a minimal XML statistics document echoing the selector asked for."""
    yield _frame(sid, c.kXR_ok, f'<statistics sel="{args}"/>'.encode() + b"\x00")


def _q_space(conn: _Connection, sid: int, args: str) -> Iterator[bytes]:
    """``kXR_Qspace``: the ``oss.*`` key-value string in :attr:`FakeServer.space`."""
    yield _frame(sid, c.kXR_ok, conn.s.space.encode() + b"\x00")


def _q_prep(conn: _Connection, sid: int, args: str) -> Iterator[bytes]:
    """``kXR_QPrep``: the status of a staging request, as the JSON document EOS/CTA send.

    ``args`` is the request id then one path per line. Each file's state is
    computed now from the fake's contents - :attr:`nearline` membership is
    what "on tape, not yet online" means - so a test moves a file online by
    discarding it from that set between two polls. An id this server never
    issued is refused with ``kXR_ArgInvalid`` (3000).
    """
    handle, *wanted = args.split("\n")
    if handle not in conn.s.prepared:
        yield _error(sid, 3000, f"prepare request {handle} is not one of ours")
        return
    asked = conn.s.prepared[handle]
    yield _frame(
        sid,
        c.kXR_ok,
        json.dumps(
            {
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
            }
        ).encode()
        + b"\x00",
    )


def _q_visa(conn: _Connection, sid: int, args: str) -> Iterator[bytes]:
    """``kXR_Qvisa``: a fixed placeholder; only that the query round-trips is tested."""
    yield _frame(sid, c.kXR_ok, b"visa\x00")


#: ``kXR_query`` infotype to the answer for it. Anything else - notably
#: ``kXR_Qxattr`` and the opaque types other than ``kXR_Qopaqug`` - is refused
#: as unsupported, which is how a stock server without the plug-in answers.
_QUERIES: dict[int, Callable[[_Connection, int, str], Iterator[bytes]]] = {
    c.kXR_Qcksum: _q_cksum,
    c.kXR_Qconfig: _q_config,
    c.kXR_Qckscan: _q_ckscan,
    c.kXR_QStats: _q_stats,
    c.kXR_Qspace: _q_space,
    c.kXR_QPrep: _q_prep,
    c.kXR_Qvisa: _q_visa,
}


def _h_query(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_query`` by infotype (``params[0:2]``); the argument is the body's first string."""
    infotype = struct.unpack(">H", params[:2])[0]
    args = body.split(b"\x00", 1)[0].decode("utf-8", "replace")
    query = _QUERIES.get(infotype)
    if query is None:
        yield _error(sid, 3013, f"query {infotype} is not supported")
        return
    yield from query(conn, sid, args)


def _h_query_fctl(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_query``, with ``kXR_Qopaqug`` - XrdCl's ``Fcntl`` - taken first.

    As xrootd's ``do_Qfh``: the handle is checked before anything is asked of
    the storage.
    """
    if struct.unpack(">H", params[:2])[0] != c.kXR_Qopaqug:
        yield from _h_query(conn, sid, params, body)
        return
    plugin = conn.s.fctl
    path = conn.handles.get(params[4:8])
    if path is None:
        yield _error(sid, 3004, "query does not refer to an open file")
    elif plugin is None:
        yield _error(sid, 3013, "fctl operation not supported")
    else:
        yield _frame(sid, c.kXR_ok, plugin(path, body))


def _h_locate(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_locate``: this server, as a writable data server (``Sw``), holds the path.

    The reply's entry format is ``<type><access><host:port>``: ``S`` for a
    data server (``M`` would be a manager the client must ask again), ``w``
    for read-write. A single fake has no cluster, so it always names itself.
    """
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
    """``kXR_prepare``: take a staging (or eviction) request, or cancel one.

    ``params[0]`` is the options byte (``kXR_stage``, ``kXR_cancel`` ...)
    and ``params[4:6]`` the extended ``optionX`` word that carries
    ``kXR_evict``; both are recorded for tests that check flag encoding.
    The request id is always ``prep-0001``: tests that need several
    concurrent ids are not ones this fake is for. Nothing is actually
    staged - a nearline file stays nearline until the test says otherwise.
    """
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
