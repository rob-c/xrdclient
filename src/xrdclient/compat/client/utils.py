"""``XRootD.client.utils``: the helpers pyxrootd code subclasses and waits on.

:class:`AsyncResponseHandler` is the usual way pyxrootd code turns a
callback call back into a blocking one; :class:`CopyProgressHandler` is the
base class :meth:`~.copyprocess.CopyProcess.run` reports to; and
:class:`CallbackWrapper` is the bindings' internal adapter, kept because
some code constructs it directly. All three are shaped exactly like the
bindings' so subclasses written for them keep working.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from .responses import HostInfo, HostList, XRootDStatus
from .url import URL

__all__ = ["AsyncResponseHandler", "CallbackWrapper", "CopyProgressHandler"]


class CallbackWrapper:
    """The bindings' adapter from a raw ``(status, response, hosts)`` to the typed ones.

    ``responsetype`` builds the response object from the raw answer (``None``
    leaves it as it is); the status becomes an :class:`XRootDStatus` and the
    hosts a :class:`HostList`, then ``callback`` gets all three. Compat
    calls already hand callbacks the typed objects, which pass through.
    """

    def __init__(self, callback: Callable[..., object], responsetype: Any) -> None:
        """Wrap ``callback``; a non-callable is a ``TypeError`` now, as in the bindings."""
        if not callable(callback):
            raise TypeError("callback must be callable function, class or lambda")
        self.callback = callback
        self.responsetype = responsetype

    def __call__(self, status: Any, response: Any, *argv: Any) -> None:
        """Convert the raw answer and pass it on; the pieces stay readable as attributes.

        The host list is optional (``*argv``), as in the bindings' wrapper; a
        call without one gets an empty ``HostList``.
        """
        self.status = status if isinstance(status, XRootDStatus) else XRootDStatus(dict(status))
        self.response = response
        if self.responsetype and self.response:
            self.response = self.responsetype(response)
        self.hostlist = _host_list(argv[0] if argv else [])
        self.callback(self.status, self.response, self.hostlist)


def _host_list(hosts: Any) -> HostList:
    """A ``HostList`` from one already built or a list of host dicts or ``HostInfo``s."""
    if isinstance(hosts, HostList):
        return hosts
    return HostList({"hosts": [h if isinstance(h, HostInfo) else HostInfo(dict(h)) for h in hosts]})


class AsyncResponseHandler:
    """A callback that can be waited for: pass it as ``callback=``, then ``wait()``."""

    def __init__(self) -> None:
        """Nothing received yet: ``wait()`` blocks until the callback is called."""
        self.__done = threading.Event()
        self.status: XRootDStatus | None = None
        self.response: Any = None
        self.hostlist: HostList | None = None

    def __call__(self, status: XRootDStatus, response: Any, hostlist: HostList) -> None:
        """Record the answer, then release :meth:`wait` - in that order, so it sees it."""
        self.status, self.response, self.hostlist = status, response, hostlist
        self.__done.set()

    def wait(self) -> tuple[XRootDStatus, Any, HostList]:
        """Block until the answer has arrived, then return ``(status, response, hostlist)``."""
        self.__done.wait()
        status, hostlist = self.status, self.hostlist
        # Both are set before the event is: the answer is whole once it fires.
        assert status is not None and hostlist is not None
        return status, self.response, hostlist


class CopyProgressHandler:
    """Subclass this and pass it to :meth:`CopyProcess.run` to watch the jobs.

    It does nothing by itself; override whichever of the four methods you need.
    """

    def begin(self, jobId: int, total: int, source: URL, target: URL) -> None:
        """Job ``jobId`` of ``total`` is about to copy ``source`` to ``target``."""

    def end(self, jobId: int, results: dict[str, Any]) -> None:
        """Job ``jobId`` finished; ``results["status"]`` says how."""

    def update(self, jobId: int, processed: int, total: int) -> None:
        """Job ``jobId`` has moved ``processed`` of ``total`` bytes."""

    def should_cancel(self, jobId: int) -> bool:
        """Return ``True`` to stop job ``jobId``."""
        return False
