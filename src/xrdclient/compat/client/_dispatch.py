"""How every compat call runs: inline, under a deadline, or handed to a callback.

The bindings give each operation the same three shapes, and this module is
the one place that knows them:

- no callback, no timeout: run it, return ``(status, response)``;
- ``timeout=`` seconds: the same, inside a native :func:`~xrdclient.deadline`,
  so the request itself *expires* when the time is up, as XrdCl's does - its
  stream id is abandoned, nothing more is sent for it (no retry, no redirect
  followed, no wait sat out), and the caller gets ``errOperationExpired``;
- ``callback=``: return a status at once and call
  ``callback(status, response, hostlist)`` from a worker thread when the
  answer arrives, exactly as the bindings do. A timeout given with a
  callback is a deadline for that worker, counted from the call.

The host list a callback gets is every server the request passed through -
see :func:`~xrdclient.session.router.trace`.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import queue
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

from ...session.deadline import deadline, expiring_at
from ...session.router import Hop, trace
from . import env
from ._status import OK, errInvalidArgs, failure, from_exception, guard, status, stOK, suContinue
from .responses import HostList, XRootDStatus

__all__ = ["CONTINUE", "call", "no_answer", "now", "quick", "stream", "Hosts"]

T = TypeVar("T")

#: How a caller turns the servers a request passed through into a ``HostList``.
Hosts = Callable[[list[Hop]], HostList]

#: The status of a partial answer, with more to come: XrdCl's ``suContinue``.
CONTINUE = status(suContinue, level=stOK)

#: Where callbacks run. Threads rather than asyncio because the bindings'
#: callbacks run on threads, and code written for them takes locks
#: accordingly.
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="xrd-compat")


def no_answer(value: object) -> None:
    """The ``convert`` of an operation whose response is ``None``."""
    del value


def _no_hosts(hops: list[Hop]) -> HostList:
    del hops
    return HostList({"hosts": []})


def quick(timeout: object, callback: object) -> bool:
    """Whether a call can go straight to :func:`now`.

    No callback, and no time limit - neither the call's own ``timeout`` nor
    the ``RequestTimeout`` every call gets once it has been put.
    """
    return (
        callback is None and timeout.__class__ is int and not timeout and not env.request_timeout()
    )


def now(operation: Callable[..., T], convert: Callable[[T], Any], *args: Any) -> Any:
    """``(status, response)`` for ``operation(*args)``, run here and waited for.

    The shape nearly every call takes - no timeout, no callback - with nothing
    between the caller and the native call but the ``try``.
    """
    try:
        value = operation(*args)
    except Exception as exc:
        guard(exc)
        return from_exception(exc), None
    return OK, convert(value)


def call(
    operation: Callable[[], T],
    convert: Callable[[T], Any] = no_answer,
    *,
    timeout: float = 0,
    callback: Callable[[XRootDStatus, Any, HostList], object] | None = None,
    hosts: Hosts = _no_hosts,
) -> Any:
    """Run ``operation`` in whichever shape the caller asked for.

    ``convert`` turns the native result into the bindings' response object;
    ``hosts`` builds a callback's third argument from the servers the request
    went through. A call without a ``timeout`` is given ``RequestTimeout``,
    once that has been put, as XrdCl gives it.
    """
    timeout = timeout or env.request_timeout()
    if callback is not None:
        _submit(callback, _deliver, (operation, convert, callback, hosts, _expiry(timeout)))
        return OK
    if not timeout:
        return now(operation, convert)
    with deadline(timeout):
        return now(operation, convert)


def stream(
    operation: Callable[[Callable[[Any], None]], Any],
    *,
    timeout: float = 0,
    callback: Callable[[XRootDStatus, Any, HostList], object],
    hosts: Hosts = _no_hosts,
) -> XRootDStatus:
    """:func:`call` with a callback, for an operation that answers in parts.

    ``operation(part)`` calls ``part(response)`` with each part of its answer
    but the last, which it returns. Each part reaches ``callback`` with the
    status XrdCl gives a partial answer, :data:`CONTINUE`, and the last with
    the final one. The callbacks run on a worker thread, in order, and never
    inside the native call: a callback that makes a call of its own on the
    same connection cannot find it busy with the request still answering.
    """
    limit = timeout or env.request_timeout()
    _submit(callback, _deliver_parts, (operation, callback, hosts, _expiry(limit)))
    return OK


def _expiry(timeout: float) -> float | None:
    """When a callback's call runs out of time, counted from now."""
    return time.monotonic() + timeout if timeout else None


def _submit(callback: object, job: Callable[..., None], args: tuple[Any, ...]) -> None:
    """Run ``job(*args)`` on a worker, once ``callback`` is known to be callable."""
    if not callable(callback):
        raise TypeError("callback must be callable function, class or lambda")
    try:
        _POOL.submit(job, *args)
    except RuntimeError:
        # The pool has shut down: the interpreter is exiting, and a caller
        # cleaning up still gets its answer, on a thread of its own.
        threading.Thread(target=job, args=args, daemon=True).start()


@contextlib.contextmanager
def _within(expiry: float | None) -> Iterator[None]:
    """The deadline a callback's call was given, on the worker running it."""
    if expiry is None:
        yield
        return
    with expiring_at(expiry):
        yield


def _outcome(produce: Callable[[], Any]) -> tuple[XRootDStatus, Any]:
    """``(status, response)`` for ``produce()``, run on a worker thread.

    A mistake in the arguments raises in the caller's thread with the
    bindings; here the caller has already gone, so it arrives as an
    ``errInvalidArgs`` status instead of vanishing into the pool.
    """
    try:
        return OK, produce()
    except (TypeError, ValueError) as exc:
        return failure(errInvalidArgs, str(exc)), None
    except Exception as exc:
        return from_exception(exc), None


def _deliver(
    operation: Callable[[], T],
    convert: Callable[[T], Any],
    callback: Callable[[XRootDStatus, Any, HostList], object],
    hosts: Hosts,
    expiry: float | None,
) -> None:
    """Run ``operation`` on a worker and hand its outcome to ``callback``."""
    with trace() as hops, _within(expiry):
        status, response = _outcome(lambda: convert(operation()))
    callback(status, response, hosts(hops))


def _deliver_parts(
    operation: Callable[[Callable[[Any], None]], Any],
    callback: Callable[[XRootDStatus, Any, HostList], object],
    hosts: Hosts,
    expiry: float | None,
) -> None:
    """Run ``operation`` beside this worker, handing each part on as it comes."""
    parts: queue.SimpleQueue[tuple[XRootDStatus, Any, HostList]] = queue.SimpleQueue()
    threading.Thread(target=_produce, args=(operation, hosts, expiry, parts), daemon=True).start()
    while True:
        status, response, hostlist = parts.get()
        callback(status, response, hostlist)
        if status is not CONTINUE:
            return


def _produce(
    operation: Callable[[Callable[[Any], None]], Any],
    hosts: Hosts,
    expiry: float | None,
    parts: queue.SimpleQueue[tuple[XRootDStatus, Any, HostList]],
) -> None:
    """Run a streaming ``operation``, queueing each part and then its outcome."""
    with trace() as hops, _within(expiry):

        def part(response: Any) -> None:
            parts.put((CONTINUE, response, hosts(hops)))

        status, response = _outcome(lambda: operation(part))
    parts.put((status, response, hosts(hops)))
