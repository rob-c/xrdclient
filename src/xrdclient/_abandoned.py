"""Closing what the garbage collector found still open - off the collector's thread.

A compat ``XRootD.client.File`` dropped while open is closed, as the bindings
close one, and a compat or fsspec filesystem dropped without ``close`` lets
its connections go. But the collector runs a finalizer on whichever thread allocated at
the wrong moment: one that may hold the session pool's lock, a router's, or
be half-way through a request on the very connection the close would go out
on. A close sends ``kXR_close`` and waits for the answer, which there is a
deadlock - the class of bug :class:`~xrdclient.session.router.Router` already
avoids by handing its connection to the pool's queue rather than releasing
it in place.

This is the same arrangement for things that must *say* something on the
way out. A finalizer only :func:`adopt`\\ s the object - one ``deque.append``,
which takes no lock of ours and does no I/O - and the close happens on an
ordinary thread: the next compat call that :func:`reap`\\ s, or the reaper
thread, which wakes every :data:`INTERVAL` seconds. A file opened for writing
is therefore still committed, a moment later than its last reference went.

What may be adopted is anything whose ``close`` is safe to call from an
ordinary thread at any later time: a native :class:`~xrdclient.File`, a
:class:`~xrdclient.FileSystem` of any scheme. The reaper is started by the
code that makes such things (:func:`ensure_reaper`), never by a finalizer.
"""

from __future__ import annotations

import atexit
import os
import threading
import weakref
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol, cast

from .log import get_logger

__all__ = ["Closable", "adopt", "ensure_reaper", "on_collect", "reap"]

_log = get_logger(__name__)

#: How often the reaper looks for abandoned objects when nobody else does.
#: Short, because what waits here may be a writer whose file is not committed
#: until its close arrives; and cheap, because an empty round is one
#: ``popleft`` that raises.
INTERVAL = 0.5


class Closable(Protocol):
    """What can be adopted: anything with a ``close``."""

    def close(self) -> None:
        """Release the object's resources; must be safe to call from the reaper thread."""


#: Objects handed in by finalizers, waiting for an ordinary thread to close
#: them. A deque because its ``append`` and ``popleft`` need no lock of ours.
_queue: deque[Closable] = deque()

#: The reaper thread and the process it was started in.
_reaper: tuple[int, threading.Thread] | None = None


class _ExitFlag(Protocol):
    """:class:`weakref.finalize`'s writable ``atexit`` switch.

    Through a protocol because typeshed before mypy 2.4 declared ``finalize``
    with empty ``__slots__``, which made the documented assignment an error.
    """

    atexit: bool


def on_collect(owner: object, release: Callable[..., None], *args: Any) -> None:
    """Call ``release(*args)`` when ``owner`` is collected - but not at exit.

    ``release`` runs inside the garbage collector, so it must only
    :func:`adopt`. Not at interpreter exit: whether this module's own exit
    hook would still run after it depends on which was registered first, and
    the session pool's exit hook ends every session then anyway.
    """
    finalizer = weakref.finalize(owner, release, *args)
    cast("_ExitFlag", finalizer).atexit = False


def adopt(item: Closable) -> None:
    """Queue ``item`` to be closed soon, on another thread. Safe in a finalizer."""
    _queue.append(item)


def reap() -> int:
    """Close everything :func:`adopt` queued, quietly; how many there were.

    Quietly, because whatever the object was connected to may be gone by
    now, and there is nobody left to tell: its owner let go of it.
    """
    count = 0
    while True:
        try:
            item = _queue.popleft()
        except IndexError:
            return count
        count += 1
        try:
            item.close()
        except Exception:  # one bad close must not strand the rest
            _log.debug("closing an abandoned %s failed", type(item).__name__, exc_info=True)


def ensure_reaper() -> None:
    """Start the reaper thread, once per process. Never from a finalizer.

    Starting a thread takes locks of the interpreter's own, so this is called
    from ordinary code - the open that made something worth closing later.
    """
    global _reaper
    pid = os.getpid()
    if _reaper is not None and _reaper[0] == pid:
        return
    thread = threading.Thread(target=_reap_forever, name="xrd-finalizer", daemon=True)
    _reaper = (pid, thread)
    thread.start()


def _reap_forever() -> None:
    """The reaper's loop: :func:`reap` every :data:`INTERVAL` seconds, until exit."""
    pause = threading.Event()
    while True:
        reap()
        pause.wait(INTERVAL)


def _forget() -> None:
    """In a forked child: drop the parent's queue, which is not the child's to close.

    A close sent from the child would go out on a descriptor it shares with
    its parent, ending the parent's handle under it. The reaper did not come
    across the fork; the child's next open starts its own.
    """
    global _queue, _reaper
    _queue = deque()
    _reaper = None


# Whatever was dropped just before exit is still closed - a writer committed -
# rather than left for the server to time out.
atexit.register(reap)
os.register_at_fork(after_in_child=_forget)
