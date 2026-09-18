"""Logging helpers.

Everything logs under the ``xrdclient.`` hierarchy through a filter that redacts
credential material, so enabling DEBUG never leaks a token into a log file.
"""

from __future__ import annotations

import logging
import re

__all__ = ["get_logger", "redact"]

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
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True  # a broken format string is logging's problem, not ours
        cleaned = redact(message)
        if cleaned != message or record.args:
            record.msg = cleaned
            record.args = ()
        return True


_filter = _RedactingFilter()
_root = logging.getLogger("xrdclient")
_root.addFilter(_filter)


def get_logger(name: str) -> logging.Logger:
    """A logger under ``xrdclient.`` with redaction applied."""
    log = logging.getLogger(name if name.startswith("xrdclient") else f"xrdclient.{name}")
    log.addFilter(_filter)
    return log
