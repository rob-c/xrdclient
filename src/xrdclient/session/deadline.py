"""Per-call deadlines: a request that runs out of time expires.

    with xrdclient.deadline(5):
        fs.stat("/store/run7/events.root")   # OperationExpiredError after 5 s

This is XrdCl's per-request ``timeout``. When the time is up the request
*expires* where it stands: the call raises :class:`OperationExpiredError`, the
stream id it went out on is abandoned - a reply that turns up later is taken
off the wire and dropped, never mistaken for another request's - and nothing
more is done on its behalf: no retry, no reconnect, no redirect followed, no
``kXR_wait`` sat out. The connection itself is left as it was, because a slow
answer is not a broken one; the next request on it goes out as usual.

A deadline covers everything inside the block, in this thread or task and
whatever runs in it; blocks nest, and an inner one can shorten the time left
but never extend it. It is kept in a :mod:`contextvars` variable, so a
request made on another thread is bound only by a deadline set there.

Where :attr:`~xrdclient.Config.stall_deadline` is a ceiling a transfer should
never reach and is fatal to the connection when it is reached, this is the
caller's own budget for one call, and costs the connection nothing.
"""

from __future__ import annotations

import contextvars
import time
from collections.abc import Iterator
from contextlib import contextmanager

from ..errors import TimeoutError as XrdTimeoutError

__all__ = ["OperationExpiredError", "deadline", "expiring_at", "remaining", "check"]


class OperationExpiredError(XrdTimeoutError):
    """The call's :func:`deadline` passed before its answer arrived.

    XrdCl's ``errOperationExpired``. A :class:`~xrdclient.errors.TimeoutError`,
    so code that catches timeouts catches this too - but unlike the others it
    is never retried: the caller said how long it would wait, and that time
    has gone.
    """

    def __init__(self, message: str = "operation expired") -> None:
        super().__init__(message)


#: When the innermost deadline in force here runs out, on the monotonic
#: clock, or ``None`` outside every :func:`deadline` block.
_expiry: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "xrdclient_deadline", default=None
)


@contextmanager
def deadline(seconds: float) -> Iterator[float]:
    """Expire every request made inside the block after ``seconds``.

    Yields the absolute expiry on :func:`time.monotonic`'s clock, which
    :func:`expiring_at` takes to carry the same deadline onto another thread.
    """
    with expiring_at(time.monotonic() + float(seconds)) as when:
        yield when


@contextmanager
def expiring_at(when: float) -> Iterator[float]:
    """:func:`deadline` by its absolute expiry rather than a duration."""
    outer = _expiry.get()
    if outer is not None and outer < when:
        when = outer
    token = _expiry.set(when)
    try:
        yield when
    finally:
        _expiry.reset(token)


def remaining() -> float | None:
    """Seconds left before the deadline in force, or ``None`` without one.

    Zero or less once it has passed.
    """
    when = _expiry.get()
    return None if when is None else when - time.monotonic()


def check(what: str = "the request") -> None:
    """Raise :class:`OperationExpiredError` if the deadline in force has passed."""
    left = remaining()
    if left is not None and left <= 0:
        raise OperationExpiredError(f"{what} expired before it could complete")
