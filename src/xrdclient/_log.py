"""Logging helpers.

Everything logs under the ``xrdclient.`` hierarchy through a filter that redacts
credential material, so enabling DEBUG never leaks a token into a log file.

The same filter can mute parts of the hierarchy at some levels and not
others (:func:`mute`) - what XrdCl's per-level topic masks do, and what the
compatibility layer's ``SetLogMask`` is built on.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

__all__ = ["get_logger", "mute", "redact"]

_PATTERNS = (
    re.compile(r"(authz=)[^&\s'\"]+", re.I),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/-]+=*", re.I),
    re.compile(r"(eyJ[A-Za-z0-9_-]{4,})\.[A-Za-z0-9._-]+"),
    re.compile(r"((?:token|password|secret|keytab|cred)['\"]?\s*[=:]\s*)\S+", re.I),
)


def redact(text: str) -> str:
    """Replace credential material in ``text`` with ``<redacted>``."""
    for pat in _PATTERNS:
        text = pat.sub(r"\1<redacted>", text)
    return text


class _RedactingFilter(logging.Filter):
    """Interpolate first, then redact the result.

    Redacting the format string and the arguments separately misses anything
    that only looks like a credential once they are joined - ``"%s=%s"`` with
    ``("token", secret)`` sails straight through - and it can corrupt the
    format string itself, since ``"keytab: %s"`` is exactly the shape of a
    secret assignment. Formatting up front has neither problem, and handlers
    call :meth:`~logging.LogRecord.getMessage` anyway.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if _muted and _is_muted(record):
            return False
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True  # a broken format string is logging's problem, not ours
        cleaned = redact(message)
        if cleaned != message or record.args:
            record.msg = cleaned
            record.args = ()
        return True


#: ``(lowest level, level above the highest) -> logger prefixes`` whose
#: records in that band of levels are dropped. Replaced, never mutated, so
#: the filter reads it without a lock.
_muted: dict[tuple[int, int], tuple[str, ...]] = {}


def mute(low: int, high: int, loggers: Iterable[str]) -> None:
    """Drop records from ``loggers`` with a level from ``low`` up to ``high``.

    Each name covers its logger and every logger below it, and replaces what
    was muted in that band before; no names un-mutes it.
    """
    global _muted
    prefixes = tuple(f"{name}." for name in loggers)
    changed = {band: names for band, names in _muted.items() if band != (low, high)}
    if prefixes:
        changed[(low, high)] = prefixes
    _muted = changed


def _is_muted(record: logging.LogRecord) -> bool:
    name = f"{record.name}."
    return any(
        low <= record.levelno < high and name.startswith(prefixes)
        for (low, high), prefixes in _muted.items()
    )


_filter = _RedactingFilter()
_root = logging.getLogger("xrdclient")
_root.addFilter(_filter)


def get_logger(name: str) -> logging.Logger:
    """A logger under ``xrdclient.`` with redaction applied."""
    log = logging.getLogger(name if name.startswith("xrdclient") else f"xrdclient.{name}")
    log.addFilter(_filter)
    return log
