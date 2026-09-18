"""Logged-in connections, kept for the next caller who wants the same one.

Bringing up an XRootD session is expensive out of all proportion to what it
carries: a handshake, a protocol exchange, usually a TLS negotiation and then
an authentication round trip or three, all before the first ``kXR_stat``. A
script that opens a :class:`~xrdclient.client.FileSystem` per file - which is what
``fsspec``, the CLI and most one-liners do - pays for that every time.

So a connection that is finished with is not closed: it is put here, and the
next :class:`~xrdclient.session.router.Router` asking for the same server as the
same person picks it up instead of dialling. What "the same person" means is
the whole subtlety, and it is :func:`_identity`'s job to be strict about it -
handing one user's authenticated connection to another is the one bug this
module must not have.

Idle connections are not free either: the server holds session state for each
one. :attr:`~xrdclient.Config.pool_idle_ttl` bounds how long an unused connection is
kept and :attr:`~xrdclient.Config.pool_size` how many per server, and setting the
latter to zero turns pooling off outright.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import threading
import time

from .._log import get_logger
from ..config import Config
from ..url import XRootDURL
from .sync import Session

__all__ = ["SessionPool", "SESSIONS"]

_log = get_logger(__name__)

#: Everything about a :class:`~xrdclient.Config` that decides who the server thinks
#: it is talking to. ``prompter`` is deliberately absent: an answer given to a
#: prompt is remembered by :mod:`xrdclient.auth.prompt` per endpoint and mechanism
#: for the life of the process, so two configs that differ only in where the
#: question would be asked still log in as the same person.
_IDENTITY_FIELDS = (
    "username",
    "token",
    "token_file",
    "keytab",
    "proxy",
    "ca_path",
    "ca_file",
    "auth_order",
    "verify_tls",
    "require_tls",
)

#: Where and as whom: scheme, host, port, the URL's own user, and the digest.
Key = tuple[str, str, int, str]


def _identity(url: XRootDURL, config: Config) -> str:
    """A digest standing in for whoever this connection will log in as.

    A digest and not the values themselves because this ends up in a
    dictionary key, and dictionary keys end up in reprs, logs and tracebacks -
    one of these fields is a bearer token. Comparing digests answers the only
    question the pool has ("same credentials?") and answers nothing else.
    """
    digest = hashlib.sha256()
    digest.update(repr(url.username or config.username).encode())
    for name in _IDENTITY_FIELDS:
        digest.update(b"\x00")
        digest.update(repr(getattr(config, name)).encode())
    return digest.hexdigest()


def _key(url: XRootDURL, config: Config) -> Key:
    return (
        "roots" if url.use_tls or config.require_tls else "root",
        url.host,
        url.port,
        _identity(url, config),
    )


def _cannot_pool(session: Session, config: Config) -> bool:
    return config.pool_size <= 0 or session.closed or session.broken


def _prune(bucket: list[tuple[float, Session]], cutoff: float) -> list[tuple[float, Session]]:
    expired = [entry for entry in bucket if entry[0] < cutoff]
    if expired:
        bucket[:] = [entry for entry in bucket if entry[0] >= cutoff]
    return expired


def _keep(bucket: list[tuple[float, Session]], session: Session, maximum: int) -> bool:
    if len(bucket) >= maximum:
        return False
    bucket.append((time.monotonic(), session))
    return True


def _close_entries(entries: list[tuple[float, Session]]) -> None:
    for _, session in entries:
        session.close()


class SessionPool:
    """A bounded, thread-safe cache of idle sessions.

    There is one of these per process - :data:`SESSIONS` - but it is an
    ordinary object rather than module state so that a test, or an application
    that wants its connections kept separate from a library's, can have its
    own.
    """

    def __init__(self) -> None:
        self._idle: dict[Key, list[tuple[float, Session]]] = {}
        self._lock = threading.Lock()

    def acquire(self, url: XRootDURL, config: Config) -> Session | None:
        """A live session for ``url``, or ``None`` if the caller must dial.

        Newest first: the connection idle for the shortest time is the one the
        server and every firewall between here and it is least likely to have
        given up on.
        """
        if config.pool_size <= 0:
            return None
        cutoff = time.monotonic() - config.pool_idle_ttl
        key = _key(url, config)
        found: Session | None = None
        stale: list[Session] = []
        with self._lock:
            bucket = self._idle.get(key, [])
            while bucket:
                when, session = bucket.pop()
                if session.closed or session.broken:
                    continue
                if when < cutoff:
                    stale.append(session)
                    continue
                found = session
                break
            if not bucket:
                self._idle.pop(key, None)
        # Outside the lock: closing writes a kXR_endsess and can block, and no
        # other caller should wait on a connection that is already nobody's.
        for session in stale:
            session.close()
        if found is not None:
            _log.debug("reusing the pooled connection to %s", found.endpoint)
        return found

    def release(self, session: Session, url: XRootDURL, config: Config) -> bool:
        """Take ``session`` back, or say ``False`` and leave it to the caller.

        The caller closes what is refused, which is why this returns a bool
        rather than swallowing it: a connection nobody owns is a descriptor
        leak, and the pool refuses more often than it accepts - when pooling
        is off, when the server is already gone, when the bucket is full.
        """
        if _cannot_pool(session, config):
            return False
        cutoff = time.monotonic() - config.pool_idle_ttl
        key = _key(url, config)
        with self._lock:
            bucket = self._idle.setdefault(key, [])
            expired = _prune(bucket, cutoff)
            kept = _keep(bucket, session, config.pool_size)
        _close_entries(expired)
        if kept:
            _log.debug("keeping the connection to %s for the next caller", session.endpoint)
        return kept

    def forget(self) -> None:
        """Let go of every idle connection without closing any of them.

        For a forked child, which inherits its parent's sockets and must not
        use them: two processes taking turns on one XRootD session read each
        other's replies. Closing them here would be worse than using them -
        the descriptor is shared, so a ``kXR_endsess`` from the child ends
        the parent's session too. The child dials its own instead.
        """
        # The lock may have been held by a thread that does not exist on this
        # side of the fork, so it is replaced rather than taken.
        self._idle = {}
        self._lock = threading.Lock()

    def clear(self) -> None:
        """Close everything idle. Idempotent."""
        with self._lock:
            buckets, self._idle = self._idle, {}
        for bucket in buckets.values():
            for _, session in bucket:
                session.close()

    def __len__(self) -> int:
        """How many connections are being held open right now."""
        with self._lock:
            return sum(len(bucket) for bucket in self._idle.values())

    def __repr__(self) -> str:
        return f"SessionPool({len(self)} idle)"


#: The pool every :class:`~xrdclient.session.router.Router` uses.
SESSIONS = SessionPool()

# A pooled connection has a session on the server holding resources for it.
# Ending them on the way out is the difference between a tidy shutdown and one
# the server has to time out.
atexit.register(SESSIONS.clear)

# A child gets none of them: see SessionPool.forget. This is why a DataLoader
# with workers, a multiprocessing Pool or a forking server can share nothing
# but the name of a file, and why none of them has to be told to.
os.register_at_fork(after_in_child=SESSIONS.forget)
