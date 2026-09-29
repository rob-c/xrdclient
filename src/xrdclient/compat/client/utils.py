"""``XRootD.client.utils``: the helpers pyxrootd code subclasses and waits on."""

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
        if not callable(callback):
            raise TypeError("callback must be callable function, class or lambda")
        self.callback = callback
        self.responsetype = responsetype

    def __call__(self, status: Any, response: Any, *argv: Any) -> None:
        self.status = status if isinstance(status, XRootDStatus) else XRootDStatus(dict(status))
        self.response = response
        if self.responsetype and self.response:
            self.response = self.responsetype(response)
        self.hostlist = _host_list(argv[0] if argv else [])
        self.callback(self.status, self.response, self.hostlist)


def _host_list(hosts: Any) -> HostList:
    if isinstance(hosts, HostList):
        return hosts
    return HostList({"hosts": [h if isinstance(h, HostInfo) else HostInfo(dict(h)) for h in hosts]})


class AsyncResponseHandler:
    """A callback that can be waited for: pass it as ``callback=``, then ``wait()``."""

    def __init__(self) -> None:
        self.__done = threading.Event()
        self.status: XRootDStatus | None = None
        self.response: Any = None
        self.hostlist: HostList | None = None

    def __call__(self, status: XRootDStatus, response: Any, hostlist: HostList) -> None:
        self.status, self.response, self.hostlist = status, response, hostlist
        self.__done.set()

    def wait(self) -> tuple[XRootDStatus | None, Any, HostList | None]:
        """Block until the answer has arrived, then return ``(status, response, hostlist)``."""
        self.__done.wait()
        return self.status, self.response, self.hostlist


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
