"""``XRootD.client.env``: XrdCl's settings, by XrdCl's names.

``EnvPutInt("RequestTimeout", 60)`` is how pyxrootd code tunes the client, and
here it tunes this one: every compat object is built with a
:class:`~xrdclient.config.Config` carrying the settings put here, translated
to the native field they mean (:func:`config`). A setting nobody put keeps the
native default rather than XrdCl's, so a key that is merely *read* - as
``CopyProcess.add_job``'s defaults read ``CPChunkSize`` - changes nothing.

As with XrdCl, a value already in the process environment as ``XRD_<KEY>``
wins over a put: :func:`EnvPutInt` returns ``False`` and leaves it alone.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from ...config import Config

__all__ = [
    "EnvDelInt",
    "EnvDelString",
    "EnvGetDefault",
    "EnvGetInt",
    "EnvGetString",
    "EnvPutInt",
    "EnvPutString",
    "SetLogLevel",
    "SetLogMask",
    "config",
]

#: XrdCl's own defaults, for the keys pyxrootd code commonly reads.
_DEFAULTS: dict[str, int | str] = {
    "ConnectionWindow": 120,
    "ConnectionRetry": 5,
    "RequestTimeout": 1800,
    "StreamTimeout": 60,
    "StreamErrorWindow": 1800,
    "TimeoutResolution": 15,
    "SubStreamsPerChannel": 1,
    "RedirectLimit": 16,
    "CPChunkSize": 8388608,
    "CPParallelChunks": 4,
    "CPInitTimeout": 600,
    "CPTPCTimeout": 1800,
    "CPTimeout": 0,
    "XRateThreshold": 0,
    "CpRetry": 0,
    "CpRetryPolicy": "force",
    "PollerPreference": "built-in",
}

#: The native :class:`Config` field each XrdCl key sets, and how to convert.
_FIELDS: dict[str, tuple[str, Any]] = {
    "connectionwindow": ("connect_timeout", float),
    "connectionretry": ("connect_retries", int),
    "requesttimeout": ("request_timeout", float),
    "streamtimeout": ("stream_timeout", float),
    "redirectlimit": ("redirect_limit", int),
    "cpchunksize": ("chunk_size", int),
    "cpparallelchunks": ("in_flight", int),
    # XrdCl counts the control stream among its substreams; the native
    # setting counts only the extra ones.
    "substreamsperchannel": ("data_streams", lambda n: max(int(n) - 1, 0)),
}

_lock = threading.Lock()
_ints: dict[str, int] = {}
_strings: dict[str, str] = {}

#: XrdCl's log levels, and the ``logging`` level each one means here.
_LEVELS = {
    "dump": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def _key(key: str) -> str:
    return key.lower()


def _from_shell(key: str) -> str | None:
    return os.environ.get(f"XRD_{key.upper()}")


def EnvPutInt(key: str, value: int) -> bool:
    """Set an integer setting; ``False`` when the shell's ``XRD_<KEY>`` wins."""
    if _from_shell(key) is not None:
        return False
    with _lock:
        _ints[_key(key)] = int(value)
    return True


def EnvPutString(key: str, value: str) -> bool:
    """Set a string setting; ``False`` when the shell's ``XRD_<KEY>`` wins."""
    if _from_shell(key) is not None:
        return False
    with _lock:
        _strings[_key(key)] = str(value)
    return True


def EnvGetInt(key: str) -> int | None:
    """The setting's value: the shell's, then a put one, then XrdCl's default."""
    shell = _from_shell(key)
    if shell is not None:
        try:
            return int(shell)
        except ValueError:
            return None
    with _lock:
        if _key(key) in _ints:
            return _ints[_key(key)]
    default = EnvGetDefault(key)
    return default if isinstance(default, int) else None


def EnvGetString(key: str) -> str | None:
    """The setting's value: the shell's, then a put one, then XrdCl's default."""
    shell = _from_shell(key)
    if shell is not None:
        return shell
    with _lock:
        if _key(key) in _strings:
            return _strings[_key(key)]
    default = EnvGetDefault(key)
    return default if isinstance(default, str) else None


def EnvDelInt(key: str) -> bool:
    """Forget a put integer; ``False`` when the shell's ``XRD_<KEY>`` holds it."""
    if _from_shell(key) is not None:
        return False
    with _lock:
        _ints.pop(_key(key), None)
    return True


def EnvDelString(key: str) -> bool:
    """Forget a put string; ``False`` when the shell's ``XRD_<KEY>`` holds it."""
    if _from_shell(key) is not None:
        return False
    with _lock:
        _strings.pop(_key(key), None)
    return True


def EnvGetDefault(key: str) -> int | str | None:
    """XrdCl's built-in default for ``key``, or ``None`` for one it has none of."""
    lowered = _key(key)
    return next((v for k, v in _DEFAULTS.items() if k.lower() == lowered), None)


def SetLogLevel(level: str) -> None:
    """Set this library's log level from XrdCl's word for it (``"Debug"``...)."""
    wanted = _LEVELS.get(str(level).lower())
    if wanted is None:
        raise ValueError(f"unknown log level {level!r}; one of {sorted(_LEVELS)}")
    logging.getLogger("xrdclient").setLevel(wanted)


def SetLogMask(level: str, mask: str) -> None:
    """Accepted for compatibility. Topic masks have no equivalent here.

    Everything this library logs is under the ``xrdclient.`` logger
    hierarchy, which is how a topic is chosen - ``logging.getLogger(
    "xrdclient.session")`` - rather than by a mask.
    """
    del level, mask


def config() -> Config:
    """A native :class:`Config` with every setting put here applied to it."""
    with _lock:
        put: dict[str, Any] = {**_ints, **_strings}
    changes = {
        _FIELDS[key][0]: _FIELDS[key][1](value) for key, value in put.items() if key in _FIELDS
    }
    return Config(**changes)
