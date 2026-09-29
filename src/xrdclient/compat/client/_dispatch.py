"""How every compat call runs: inline, bounded by ``timeout``, or handed to a callback.

The bindings give each operation the same three shapes, and this module is
the one place that knows them:

- no callback, no timeout: run it, return ``(status, response)``;
- ``timeout=`` seconds: the same, but a caller still waiting when the time is
  up gets ``errOperationExpired`` back, which is what XrdCl reports to its
  caller in that case. The request itself is not recalled - XrdCl cannot do
  that either, and a server may already be acting on it;
- ``callback=``: return a status at once and call
  ``callback(status, response, hostlist)`` from a worker thread when the
  answer arrives, exactly as the bindings do.
"""

from __future__ import annotations

import concurrent.futures
import threading
from collections.abc import Callable
from typing import Any, TypeVar

from ._status import OK, errInvalidArgs, errOperationExpired, failure, from_exception, guard
from .responses import HostList, XRootDStatus

__all__ = ["call", "no_answer", "now"]

T = TypeVar("T")

#: Where callbacks and timed calls run. Threads rather than asyncio because
#: the bindings' callbacks run on threads, and code written for them takes
#: locks accordingly.
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="xrd-compat")


def no_answer(value: object) -> None:
    """The ``convert`` of an operation whose response is ``None``."""
    del value


def _no_hosts() -> HostList:
    return HostList({"hosts": []})


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
    hosts: Callable[[], HostList] = _no_hosts,
) -> Any:
    """Run ``operation`` in whichever shape the caller asked for.

    ``convert`` turns the native result into the bindings' response object;
    ``hosts`` says which servers answered, for a callback's third argument.
    """
    if callback is not None:
        if not callable(callback):
            raise TypeError("callback must be callable function, class or lambda")
        try:
            _POOL.submit(_deliver, operation, convert, callback, hosts)
        except RuntimeError:
            # The pool has shut down: the interpreter is exiting, and a
            # caller cleaning up still gets its answer, on a thread of its own.
            threading.Thread(
                target=_deliver, args=(operation, convert, callback, hosts), daemon=True
            ).start()
        return OK
    if not timeout:
        return now(operation, convert)
    return _bounded(operation, convert, timeout)


def _bounded(
    operation: Callable[[], T], convert: Callable[[T], Any], timeout: float
) -> tuple[XRootDStatus, Any]:
    """``(status, response)`` for a call given ``timeout`` seconds to answer."""
    try:
        value = _POOL.submit(operation).result(timeout)
    except concurrent.futures.TimeoutError:
        return failure(errOperationExpired, f"no answer within {timeout:g}s"), None
    except Exception as exc:
        guard(exc)
        return from_exception(exc), None
    return OK, convert(value)


def _deliver(
    operation: Callable[[], T],
    convert: Callable[[T], Any],
    callback: Callable[[XRootDStatus, Any, HostList], object],
    hosts: Callable[[], HostList],
) -> None:
    """Run ``operation`` on a worker and hand its outcome to ``callback``.

    A mistake in the arguments raises in the caller's thread with the
    bindings; here the caller has already gone, so it arrives as an
    ``errInvalidArgs`` status instead of vanishing into the pool.
    """
    try:
        status, response = OK, convert(operation())
    except (TypeError, ValueError) as exc:
        status, response = failure(errInvalidArgs, str(exc)), None
    except Exception as exc:
        status, response = from_exception(exc), None
    callback(status, response, hosts())
