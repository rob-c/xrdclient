"""Session-level handlers of the fake XRootD server.

Protocol negotiation, login (which issues the session id ``kXR_bind`` later
looks up), the ``kXR_authmore`` rounds :attr:`FakeServer.auth_rounds
<xrdclient.testing.server.FakeServer.auth_rounds>` asks for, binding a data
path to its control link, ping, ``kXR_endsess`` and ``kXR_set``. These are the
requests that change what a *connection* is rather than what the namespace
holds. Registered by opcode in ``xrdclient.testing.server._HANDLERS``.

Authentication is a formality here: whatever credential arrives is accepted
once the configured number of ``kXR_authmore`` rounds have passed. What the
fake is faithful to is the *shape* of the exchange - the security trailer on
the login reply that tells a client it must authenticate, and the extra
round trips - because that is what client code drives.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from typing import TYPE_CHECKING

from ...proto import constants as c
from .wire import _error, _frame

if TYPE_CHECKING:
    from ..server import _Connection


def _h_protocol(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_protocol``: the server's protocol version and role flags.

    The reply is ``pval[4] flags[4]`` with no security-requirements
    extension, so a client sees no request-signing demand unless a test
    installs a handler that sends one.
    """
    yield _frame(sid, c.kXR_ok, struct.pack(">iI", conn.s.version, conn.s.flags))


def _h_bind(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_bind``: make this connection a data path of the session it names.

    ``params[:16]`` is the session id the control link's login was given;
    the reply is the one-byte path id later reads and writes use to route
    data here. A session id that matches no login is refused with
    ``kXR_NotAuthorized`` (3010): a data path cannot join a session it
    cannot prove it belongs to.
    """
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
    """``kXR_login``: issue a session id, plus the security trailer if one is configured.

    The reply is ``sessid[16]`` followed - only when
    :attr:`~xrdclient.testing.server.FakeServer.sec` is set - by the
    ``&P=...`` text that obliges the client to ``kXR_auth`` next. The
    session is registered so a later ``kXR_bind`` can find it.
    """
    trailer = conn.s.sec.encode() + b"\x00" if conn.s.sec else b""
    conn.sessid = conn.s.next_sessid()
    with conn.s._live_lock:
        conn.s._sessions[conn.sessid] = conn
    yield _frame(sid, c.kXR_ok, conn.sessid + trailer)


def _h_auth(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_auth``: ask for more, ``auth_rounds`` times per connection, then accept.

    The ``kXR_authmore`` payload is a fixed placeholder; the credential is
    never inspected, since what is under test is the client's multi-round
    loop, not a mechanism's cryptography (that has its own unit tests).
    """
    conn.auth_seen += 1
    if conn.auth_seen <= conn.s.auth_rounds:
        yield _frame(sid, c.kXR_authmore, b"challenge")
    else:
        yield _frame(sid, c.kXR_ok)


def _h_ping(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_ping``: an empty ``kXR_ok`` - a liveness probe and nothing more."""
    yield _frame(sid, c.kXR_ok)


def _h_endsess(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_endsess``: acknowledge; the connection loop then stops reading this link.

    Whatever session id the request names, it is this connection that ends,
    which is the only case the client produces (closing its own session).
    """
    yield _frame(sid, c.kXR_ok)


def _h_set(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """``kXR_set``: record the directive (``appid ...``, ``monitor ...``) and accept it."""
    conn.s.properties.append(body.split(b"\x00", 1)[0].decode("utf-8", "replace").strip())
    yield _frame(sid, c.kXR_ok)
