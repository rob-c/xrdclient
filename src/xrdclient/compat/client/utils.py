"""``XRootD.client.utils``: the helpers pyxrootd code subclasses and waits on."""

from __future__ import annotations

import threading
from typing import Any

from .responses import HostList, XRootDStatus
from .url import URL

__all__ = ["AsyncResponseHandler", "CopyProgressHandler"]


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
