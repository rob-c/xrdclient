"""One connection per server, shared by every compat file opened on it.

XrdCl multiplexes all the files a process opens on one server over a single
channel, and some requests depend on it: ``kXR_clone`` copies between two
handles only when both belong to the same session. So compat files on a
``root://`` server open through one native :class:`FileSystem` per server
and settings, borrowing its connection the way the files a native
filesystem opens do - and a file whose open is redirected moves to the data
server on a connection of its own, leaving the shared one where it was.
"""

from __future__ import annotations

import atexit
import contextlib
import threading

from ...client.filesystem import FileSystem
from ...config import Config
from ...session.router import Router
from ...url import parse

__all__ = ["close_all", "router_for"]

#: Guards :data:`_channels`; opens on many threads (callbacks) race to make one.
_lock = threading.Lock()
#: One native filesystem per (scheme, host:port, settings). ``Config`` is in the
#: key because a put setting (``env.EnvPutInt``) must not reuse a connection
#: built under the old ones; it is frozen, so it hashes by value.
_channels: dict[tuple[str, str, Config], FileSystem] = {}


def router_for(url: str, config: Config) -> Router | None:
    """A router for a file at ``url`` on its server's shared channel.

    ``None`` for a URL that is not ``root://``-family, which the native file
    opens by itself.
    """
    parsed = parse(url)
    if not parsed.is_root:
        return None
    key = (parsed.scheme, parsed.netloc, config)
    with _lock:
        channel = _channels.get(key)
        if channel is None:
            channel = _channels[key] = FileSystem(parsed.with_path("/"), config)
    # A lent router starts on the shared connection and, if the open is
    # redirected, moves to the data server alone - the channel stays put.
    return channel._router.lend()


@atexit.register
def close_all() -> None:
    """Close every shared channel; the next open on a server makes a new one."""
    with _lock:
        channels = list(_channels.values())
        _channels.clear()
    for channel in channels:
        with contextlib.suppress(Exception):
            channel.close()
