"""``XRootD.client.env``: XrdCl's settings and log controls, by XrdCl's names.

``EnvPutInt("RequestTimeout", 60)`` is how pyxrootd code tunes the client, and
here it tunes this one: every compat object is built with a
:class:`~xrdclient.config.Config` carrying the settings put here, translated
to the native field they mean (:func:`config`). A setting nobody put keeps the
native default rather than XrdCl's, so a key that is merely *read* - as
``CopyProcess.add_job``'s defaults read ``CPChunkSize`` - changes nothing.

The keys, their defaults and the way they are stored follow XrdCl's
``DefaultEnv``: the settings it registers answer ``EnvGetInt`` and
``EnvGetString`` with their defaults before anything is put, and take a
value from the process environment as ``XRD_<KEY>`` - which then wins over a
put, :func:`EnvPutInt` returning ``False``. A key XrdCl does not register is
stored and read back like any other, but the environment is not consulted for
it. ``EnvGetDefault`` answers from XrdCl's table of defaults, as a string.

:data:`EFFECTS` says, for every key XrdCl registers, what it does here - or
why it does nothing.

``SetLogLevel`` and ``SetLogMask`` drive the ``xrdclient`` logger hierarchy:
each XrdCl topic is a set of this package's loggers (:data:`TOPICS`), and a
mask mutes the rest at that level.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from ... import _log
from ...config import Config

__all__ = [
    "EFFECTS",
    "TOPICS",
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
    "request_timeout",
]

#: The integer settings XrdCl registers, with its defaults (``DefaultEnv``).
_INTS: dict[str, int] = {
    "ConnectionWindow": 120,
    "ConnectionRetry": 5,
    "RequestTimeout": 1800,
    "StreamTimeout": 60,
    "SubStreamsPerChannel": 1,
    "TimeoutResolution": 15,
    "StreamErrorWindow": 1800,
    "RunForkHandler": 1,
    "RedirectLimit": 16,
    "WorkerThreads": 3,
    "CPChunkSize": 8388608,
    "CPParallelChunks": 4,
    "DataServerTTL": 300,
    "LoadBalancerTTL": 1200,
    "CPInitTimeout": 600,
    "CPTPCTimeout": 1800,
    "CPTimeout": 0,
    "TCPKeepAlive": 0,
    "TCPKeepAliveTime": 7200,
    "TCPKeepAliveInterval": 75,
    "TCPKeepProbes": 9,
    "MultiProtocol": 0,
    "ParallelEvtLoop": 10,
    "MetalinkProcessing": 1,
    "LocalMetalinkFile": 0,
    "XCpBlockSize": 134217728,
    "NoDelay": 0,
    "AioSignal": 0,
    "PreferIPv4": 0,
    "MaxMetalinkWait": 60,
    "PreserveLocateTried": 1,
    "NotAuthorizedRetryLimit": 3,
    "PreserveXAttrs": 0,
    "NoTlsOK": 0,
    "TlsNoData": 0,
    "TlsMetalink": 0,
    "ZipMtlnCksum": 0,
    "IPNoShuffle": 0,
    "WantTlsOnNoPgrw": 0,
    "RetryWrtAtLBLimit": 3,
    "XRateThreshold": 0,
    "CpRetry": 0,
    "CpUsePgWrtRd": 1,
}

#: The string settings XrdCl registers, with its defaults.
_STRINGS: dict[str, str] = {
    "ClientMonitor": "",
    "ClientMonitorParam": "",
    "NetworkStack": "IPAuto",
    "PlugIn": "",
    "PlugInConfDir": "",
    "ReadRecovery": "true",
    "WriteRecovery": "true",
    "OpenRecovery": "true",
    "GlfnRedirector": "",
    "TlsDbgLvl": "OFF",
    "CpTarget": "",
    "CpRetryPolicy": "force",
}

#: What ``EnvGetDefault`` answers from: XrdCl's own table, which is not quite
#: the registered set - it spells one key differently, leaves three of the
#: copy settings out, and has four strings nothing registers.
_DEFAULTS: dict[str, str] = {
    **{
        key.lower(): str(value)
        for key, value in _INTS.items()
        if key not in ("TCPKeepProbes", "CpRetry", "CpUsePgWrtRd")
    },
    "tcpkeepaliveprobes": "9",
    **{key.lower(): value for key, value in _STRINGS.items() if key != "CpRetryPolicy"},
    "cpretrypolicy": "force",
    "pollerpreference": "built-in",
    "clconfdir": "",
    "defaultclconffile": "",
}

#: The native :class:`Config` field each XrdCl key sets, and how to convert.
_FIELDS: dict[str, tuple[str, Any]] = {
    "connectionwindow": ("connect_timeout", float),
    "connectionretry": ("connect_retries", int),
    # XrdCl's request timeout is the whole request's; the native ceiling on
    # a whole operation is the stall deadline. Each compat call is also given
    # it as a deadline of its own - see :func:`request_timeout`.
    "requesttimeout": ("stall_deadline", float),
    # XrdCl's stream timeout is how long a connection with requests
    # outstanding may stay silent; natively that is the read timeout.
    "streamtimeout": ("request_timeout", float),
    "redirectlimit": ("redirect_limit", int),
    "cpchunksize": ("chunk_size", int),
    "cpparallelchunks": ("in_flight", int),
    # XrdCl counts the control stream among its substreams; the native
    # setting counts only the extra ones.
    "substreamsperchannel": ("data_streams", lambda n: max(int(n) - 1, 0)),
    "dataserverttl": ("pool_idle_ttl", float),
    "readrecovery": ("recover_handles", lambda value: str(value) == "true"),
    "metalinkprocessing": ("metalink_processing", lambda value: bool(int(value))),
    "tlsmetalink": ("tls_metalink", lambda value: bool(int(value))),
    "maxmetalinkwait": ("max_metalink_wait", float),
    "zipmtlncksum": ("zip_metalink_checksum", lambda value: bool(int(value))),
}

#: What each key XrdCl registers does here. The ones with a native equivalent
#: set it; the rest are stored and read back, and say why they change nothing.
EFFECTS: dict[str, str] = {
    "ConnectionWindow": "Config.connect_timeout",
    "ConnectionRetry": "Config.connect_retries",
    "RequestTimeout": "a deadline on every call given no timeout, and Config.stall_deadline",
    "StreamTimeout": "Config.request_timeout, how long a connection may stay silent",
    "SubStreamsPerChannel": "Config.data_streams, less the control stream",
    "RedirectLimit": "Config.redirect_limit",
    "CPChunkSize": "Config.chunk_size",
    "CPParallelChunks": "Config.in_flight",
    "DataServerTTL": "Config.pool_idle_ttl: how long an idle connection is kept",
    "ReadRecovery": "Config.recover_handles, which CopyProcess's sources use",
    "CPInitTimeout": "CopyProcess.add_job's inittimeout default",
    "CPTPCTimeout": "CopyProcess.add_job's tpctimeout default",
    "CPTimeout": "CopyProcess.add_job's cptimeout default",
    "XRateThreshold": "CopyProcess.add_job's xrateThreshold default",
    "CpRetry": "CopyProcess.add_job's retry default",
    "CpRetryPolicy": "CopyProcess.add_job's retryPolicy default",
    "TimeoutResolution": "none: deadlines are exact here, not checked on a timer tick",
    "StreamErrorWindow": "none: a failed connection is not remembered as failed",
    "LoadBalancerTTL": "none: the pool keeps managers and data servers alike",
    "WorkerThreads": "none: there are no worker threads to size",
    "ParallelEvtLoop": "none: there is no event loop to run in parallel",
    "RunForkHandler": "none: there is no fork handler",
    "TCPKeepAlive": "none: TCP keepalive is always on",
    "TCPKeepAliveTime": "none: the operating system's keepalive timing applies",
    "TCPKeepAliveInterval": "none: the operating system's keepalive timing applies",
    "TCPKeepProbes": "none: the operating system's keepalive timing applies",
    "NoDelay": "none: TCP_NODELAY is always set",
    "NetworkStack": "none: connections take whichever address the resolver gives first",
    "PreferIPv4": "none: connections take whichever address the resolver gives first",
    "IPNoShuffle": "none: addresses are tried in the resolver's order",
    "MultiProtocol": "none: every connection negotiates its own protocol",
    "MetalinkProcessing": "Config.metalink_processing",
    "LocalMetalinkFile": "none: metalinks are not supported",
    "MaxMetalinkWait": "Config.max_metalink_wait",
    "TlsMetalink": "Config.tls_metalink",
    "ZipMtlnCksum": "Config.zip_metalink_checksum",
    "XCpBlockSize": "none: extreme copy is not supported",
    "AioSignal": "none: there is no POSIX AIO",
    "PreserveLocateTried": "none: a redirect's tried= CGI is not kept",
    "NotAuthorizedRetryLimit": "none: a refusal is not retried",
    "RetryWrtAtLBLimit": "none: a failed write is not retried at the manager",
    "PreserveXAttrs": "none: copies do not carry extended attributes",
    "CpUsePgWrtRd": "none: copies choose page reads and writes themselves",
    "NoTlsOK": "none: TLS is always available",
    "TlsNoData": "none: a TLS connection encrypts everything",
    "WantTlsOnNoPgrw": "none: TLS is used when the URL or server asks for it",
    "TlsDbgLvl": "none: TLS is logged under the TlsMsg topic",
    "WriteRecovery": "none: a file open for writing is never re-opened",
    "OpenRecovery": "none: a failed open is not retried elsewhere",
    "ClientMonitor": "none: there are no monitoring plug-ins",
    "ClientMonitorParam": "none: there are no monitoring plug-ins",
    "PlugIn": "none: there are no client plug-ins",
    "PlugInConfDir": "none: there are no client plug-ins",
    "GlfnRedirector": "none: global logical file names are not redirected",
    "CpTarget": "none: copy targets are given per job",
}

_lock = threading.Lock()
_ints: dict[str, int] = {}
_strings: dict[str, str] = {}

#: The deadline every compat call without a ``timeout`` of its own is given,
#: once ``RequestTimeout`` has been put or is in the environment; 0 for none.
_request_timeout = 0


def _key(key: str) -> str:
    return key.lower()


def _registered(key: str, table: dict[str, Any]) -> str | None:
    """``key`` as XrdCl registers it in ``table``, or ``None``."""
    lowered = _key(key)
    return next((name for name in table if name.lower() == lowered), None)


def _from_shell(key: str, table: dict[str, Any]) -> str | None:
    """The environment's ``XRD_<KEY>``, for a key XrdCl imports from it."""
    name = _registered(key, table)
    return None if name is None else os.environ.get(f"XRD_{name.upper()}")


def _shell_int(key: str) -> int | None:
    raw = _from_shell(key, _INTS)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None  # XrdCl leaves a key it cannot parse as it was


def EnvPutInt(key: str, value: int) -> bool:
    """Set an integer setting; ``False`` when the environment's ``XRD_<KEY>`` wins."""
    if _shell_int(key) is not None:
        return False
    with _lock:
        _ints[_key(key)] = int(value)
    _settle()
    return True


def EnvPutString(key: str, value: str) -> bool:
    """Set a string setting; ``False`` when the environment's ``XRD_<KEY>`` wins."""
    if _from_shell(key, _STRINGS) is not None:
        return False
    with _lock:
        _strings[_key(key)] = str(value)
    return True


def EnvGetInt(key: str) -> int | None:
    """The setting: the environment's, then a put one, then the registered default."""
    shell = _shell_int(key)
    if shell is not None:
        return shell
    with _lock:
        if _key(key) in _ints:
            return _ints[_key(key)]
    name = _registered(key, _INTS)
    return None if name is None else _INTS[name]


def EnvGetString(key: str) -> str | None:
    """The setting: the environment's, then a put one, then the registered default."""
    shell = _from_shell(key, _STRINGS)
    if shell is not None:
        return shell
    with _lock:
        if _key(key) in _strings:
            return _strings[_key(key)]
    name = _registered(key, _STRINGS)
    return None if name is None else _STRINGS[name]


def EnvDelInt(key: str) -> bool:
    """Forget a put integer; ``False`` when the environment's ``XRD_<KEY>`` holds it."""
    if _shell_int(key) is not None:
        return False
    with _lock:
        _ints.pop(_key(key), None)
    _settle()
    return True


def EnvDelString(key: str) -> bool:
    """Forget a put string; ``False`` when the environment's ``XRD_<KEY>`` holds it."""
    if _from_shell(key, _STRINGS) is not None:
        return False
    with _lock:
        _strings.pop(_key(key), None)
    return True


def EnvGetDefault(key: str) -> str | None:
    """XrdCl's built-in default for ``key``, as a string, or ``None`` for one it has none of."""
    return _DEFAULTS.get(_key(key))


def request_timeout() -> int:
    """The deadline, in seconds, a compat call given no ``timeout`` gets; 0 for none.

    XrdCl gives every request ``RequestTimeout`` seconds when the caller
    does not say, and expires it after that. Here that happens once the key
    has been put, or set as ``XRD_REQUESTTIMEOUT``: until then a call runs
    under the native :attr:`~xrdclient.Config.stall_deadline` alone.
    """
    return _request_timeout


def _settle() -> None:
    """Recompute what depends on the settings put: the default call deadline."""
    global _request_timeout
    shell = _shell_int("RequestTimeout")
    with _lock:
        put = _ints.get("requesttimeout")
    chosen = shell if shell is not None else put
    _request_timeout = max(int(chosen or 0), 0)


def _build_config() -> Config:
    """Build a native configuration without going through the patchable API hook."""
    with _lock:
        put: dict[str, Any] = {**_strings, **_ints}
    changes = {
        _FIELDS[key][0]: _FIELDS[key][1](value) for key, value in put.items() if key in _FIELDS
    }
    return Config(**changes)


def config() -> Config:
    """A native :class:`Config` with every setting put here applied to it."""
    return _build_config()


# -- logging -------------------------------------------------------------------

#: XrdCl's log levels, and the ``logging`` level each one means here.
_LEVELS = {
    "dump": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

#: XrdCl's topics (``XrdClConstants.hh``), each the loggers of this package that
#: do that topic's work. A topic with no loggers has no counterpart here -
#: there is no poller, task manager or job manager - and masking it changes
#: nothing. ``ZipMsg`` is left out as XrdCl's own mask parser leaves it out.
TOPICS: dict[str, tuple[int, tuple[str, ...]]] = {
    "AppMsg": (
        0x1,
        (
            "xrdclient.cli",
            "xrdclient.compat",
            "xrdclient.easy",
            "xrdclient.io",
            "xrdclient.path",
            "xrdclient.aio",
            "xrdclient.fsspec_impl",
        ),
    ),
    "UtilityMsg": (0x2, ("xrdclient.copy", "xrdclient.config", "xrdclient.url")),
    "FileMsg": (0x4, ("xrdclient.client.file", "xrdclient.client.bulk", "xrdclient.http.file")),
    "PollerMsg": (0x8, ()),
    "PostMasterMsg": (0x10, ("xrdclient.session.pool",)),
    "XRootDTransportMsg": (0x20, ("xrdclient.proto", "xrdclient.auth")),
    "TaskMgrMsg": (0x40, ()),
    "XRootDMsg": (
        0x80,
        ("xrdclient.session.sync", "xrdclient.session.router", "xrdclient.session.bulk"),
    ),
    "FileSystemMsg": (
        0x100,
        ("xrdclient.client.filesystem", "xrdclient.http.dav", "xrdclient.s3"),
    ),
    "AsyncSockMsg": (0x200, ("xrdclient.transport",)),
    "JobMgrMsg": (0x400, ()),
    "PlugInMgrMsg": (0x800, ("xrdclient.http.client", "xrdclient.http.tpc")),
    "ExDbgMsg": (0x1000, ()),
    "TlsMsg": (0x2000, ("xrdclient.crypto",)),
}

#: Every bit a mask can have set.
_ALL = (1 << 64) - 1

#: XrdCl's level names for a mask, and the band of ``logging`` levels each
#: covers. Dump is XrdCl's level below Debug, which nothing here logs at.
_BANDS = {
    "Error": (logging.ERROR, logging.CRITICAL + 1),
    "Warning": (logging.WARNING, logging.ERROR),
    "Info": (logging.INFO, logging.WARNING),
    "Debug": (logging.DEBUG, logging.INFO),
    "Dump": (1, logging.DEBUG),
}


def SetLogLevel(level: str) -> None:
    """Set this library's log level from XrdCl's word for it (``"Debug"``...)."""
    wanted = _LEVELS.get(str(level).lower())
    if wanted is None:
        raise ValueError(f"unknown log level {level!r}; one of {sorted(_LEVELS)}")
    logging.getLogger("xrdclient").setLevel(wanted)


def SetLogMask(level: str, mask: str) -> None:
    """Log only the topics ``mask`` names at ``level``, as XrdCl's ``SetLogMask``.

    ``mask`` is XrdCl's: topic names joined by ``|`` - ``"FileMsg|XRootDMsg"``
    - where ``All`` and ``None`` reset the set, ``^Topic`` takes one out,
    an unknown name is ignored and an empty mask means every topic.
    ``level`` is ``Error``, ``Warning``, ``Info``, ``Debug``, ``Dump`` or
    ``All``; any other is ignored, as XrdCl ignores it.
    """
    if level == "All":
        bands = list(_BANDS.values())
    elif level in _BANDS:
        bands = [_BANDS[level]]
    else:
        return
    enabled = _translate(mask)
    muted = [logger for bit, loggers in TOPICS.values() if not enabled & bit for logger in loggers]
    for low, high in bands:
        _log.mute(low, high, muted)


def _translate(mask: str) -> int:
    """XrdCl's ``MaskTranslator``: a ``|``-joined topic list as a bit mask."""
    if mask == "":
        return _ALL
    result = 0
    for word in str(mask).split("|"):
        if word in ("All", "None"):
            result = _ALL if word == "All" else 0
            continue
        disable = word.startswith("^")
        topic = TOPICS.get(word[1:] if disable else word)
        if topic is None:
            continue
        result = result & ~topic[0] if disable else result | topic[0]
    return result


_settle()
