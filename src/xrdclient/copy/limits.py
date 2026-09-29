"""How fast, how slow and how long a copy may go.

XrdCl's classic copy job (``XrdClClassicCopyJob.cc``, ``Run``) measures the
transfer each time a chunk arrives: it sleeps when the bytes so far are ahead
of ``xrate``, fails the job when a copy running for longer than ``cpTimeout``
takes another chunk, and - once every ``parallelChunks + 1`` chunks - fails it
when the rate since the start has fallen below ``xrateThreshold``.
:class:`Pace` is that bookkeeping, fed by the engine's progress reports, which
come once per chunk on every path the engine takes.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .._compat import SLOTS
from ..errors import TimeoutError, TransientError

__all__ = ["CopyTimeoutError", "RateThresholdError", "Pace", "Limits"]

#: Called with ``(bytes_done, total_or_None)`` after every chunk.
Progress = Callable[[int, "int | None"], None]


class CopyTimeoutError(TimeoutError):
    """The copy ran for longer than its ``timeout`` (XrdCl's ``cpTimeout``)."""


class RateThresholdError(TransientError):
    """The transfer rate fell below ``min_rate`` (XrdCl's ``xrateThreshold``).

    Transient, as XrdCl treats it: a retry may well find a faster path.
    """


class _Stop(Exception):
    """One of the errors above, on its way out through code that retries.

    The bulk data plane restarts a span on a :class:`TransientError`, which
    is right for a dropped connection and wrong for a limit the caller set:
    wrapped in something no retry loop catches, the limit ends the copy.
    """

    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


@dataclass(frozen=True, **SLOTS)
class Limits:
    """What a caller asked of the copy's pace; every field off by default."""

    #: Bytes per second the transfer may not exceed (XrdCl's ``xrate``).
    max_rate: float | None = None
    #: Bytes per second below which the transfer fails (``xrateThreshold``).
    min_rate: float | None = None
    #: Seconds the whole copy may take (``cpTimeout``).
    timeout: float | None = None
    #: Chunks between two checks of ``min_rate`` (``parallelChunks``).
    interval: int = 4

    @property
    def active(self) -> bool:
        return bool(self.max_rate or self.min_rate or self.timeout)


class Pace:
    """The clocks one copy is measured by, and what it does about them."""

    def __init__(
        self,
        limits: Limits,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._started = self._flowing = clock()
        self._moved = 0
        self._countdown = limits.interval

    @property
    def limited(self) -> bool:
        """Whether there is anything to measure at all."""
        return self._limits.active

    def check(self) -> None:
        """Fail if the copy has run out of time; XrdCl asks between phases."""
        timeout = self._limits.timeout
        if timeout and self._clock() - self._started > timeout:
            raise CopyTimeoutError("CPTimeout exceeded.")

    def begin(self) -> None:
        """The data starts to flow: the rates are measured from here."""
        self._flowing = self._clock()

    def chunk(self, length: int) -> None:
        """One more chunk of ``length`` bytes has arrived."""
        with self._lock:
            self.check()
            self._throttle(length)
            self._judge(length)
            self._moved += length

    def _throttle(self, length: int) -> None:
        """Sleep off whatever this chunk puts the copy ahead of ``max_rate``."""
        rate = self._limits.max_rate
        if not rate:
            return
        elapsed = self._clock() - self._flowing
        ahead = (self._moved + length) / rate - elapsed
        if elapsed > 0 and ahead > 0:
            self._sleep(ahead)

    def _judge(self, length: int) -> None:
        """Every ``interval + 1`` chunks, fail a copy slower than ``min_rate``."""
        rate = self._limits.min_rate
        if not rate:
            return
        elapsed = self._clock() - self._flowing
        if self._countdown == 0 and elapsed > 0 and self._moved + length < rate * elapsed:
            raise RateThresholdError("The transfer rate dropped below requested threshold!")
        self._countdown = self._countdown - 1 if self._countdown > 0 else self._limits.interval

    def watch(self, progress: Progress | None, *, start: int = 0) -> Progress:
        """``progress``, with every chunk it hears about measured first.

        ``start`` is where the reports begin - the offset a resumed copy
        reports from - which is not bytes this copy moved.
        """
        seen = start

        def report(done: int, total: int | None) -> None:
            nonlocal seen
            try:
                self.chunk(done - seen)
            except (CopyTimeoutError, RateThresholdError) as exc:
                raise _Stop(exc) from None
            seen = done
            if progress is not None:
                progress(done, total)

        return report
