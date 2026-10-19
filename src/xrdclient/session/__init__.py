"""Connection drivers: one connection, and the router that moves between them.

Layering, bottom up: :mod:`.sync` drives one
:class:`~xrdclient.proto.machine.SessionMachine` over one transport and
multiplexes concurrent requests on it; :mod:`.bulk` borrows such a session
for zero-copy pipelined reads and writes; :mod:`.pool` keeps logged-in
sessions for reuse, keyed by server *and* identity; :mod:`.router` follows
redirects, reconnects and retries on top of the pool, with :mod:`._rewrite`
doing the request rewriting; :mod:`.local` stands in for a session after a
``file://`` redirect; :mod:`.deadline` carries the caller's time limit to all
of them. The client layer (:mod:`xrdclient.client`) only ever talks to a
:class:`Router`.
"""

from __future__ import annotations

from .pool import SESSIONS, SessionPool
from .router import Router
from .sync import RedirectRequired, Result, Session

__all__ = ["SESSIONS", "RedirectRequired", "Result", "Router", "Session", "SessionPool"]
