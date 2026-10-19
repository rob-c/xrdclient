"""Logging helpers: the ``xrdclient`` logger hierarchy over xrdcore's credential redaction.

Everything logs under the ``xrdclient.`` hierarchy through a filter that
redacts credential material, so enabling DEBUG never leaks a token into a log
file. Since 0.4.0 the redaction itself - the patterns, :func:`redact`,
:class:`RedactingFilter` and the per-level :func:`mute` table - is
:mod:`xrdcore.log`, shared with every library on the stack, and the names
here are the same objects. What stays in this module is xrdclient's naming:
:func:`get_logger` puts a logger under ``xrdclient.`` (``get_logger("tool")``
is ``xrdclient.tool``) and gives it xrdcore's one shared filter
(:data:`xrdcore.log.FILTER`), which is also on the ``xrdclient`` logger itself.

:func:`mute` is what XrdCl's per-level topic masks map onto and what the
compatibility layer's ``SetLogMask`` is built on. Its table is module state
in :mod:`xrdcore.log` (``xrdcore.log._muted``), so one call through any name
reaches every filter. (The pre-0.4.0 private name ``xrdclient._log`` is
gone; the package's own modules import this one.)

What a maintainer must not break: the filter instance is xrdcore's, never a
second one - two filters would redact twice and mute in only half the
loggers.
"""

from __future__ import annotations

import logging

from xrdcore.log import FILTER, RedactingFilter, mute, redact

__all__ = ["RedactingFilter", "get_logger", "mute", "redact"]

#: The one filter instance on every ``xrdclient`` logger: xrdcore's
#: :data:`~xrdcore.log.FILTER`. Tests reach it by this name.
_filter = FILTER
_root = logging.getLogger("xrdclient")
_root.addFilter(_filter)


def get_logger(name: str) -> logging.Logger:
    """A logger under ``xrdclient.`` with redaction and muting applied.

    A name that does not already start with ``xrdclient`` is put under it, so
    ``"transport"`` and ``"xrdclient.transport"`` are the same logger. The
    shared filter is added once however often a logger is asked for.
    """
    log = logging.getLogger(name if name.startswith("xrdclient") else f"xrdclient.{name}")
    log.addFilter(_filter)
    return log
