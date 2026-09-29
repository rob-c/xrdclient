#!/usr/bin/env python3
"""Timeouts: per call with ``timeout=``, and for the process with EnvPutInt.

The per-call ``timeout`` is in seconds, and ``0`` means the default
(``RequestTimeout``). A server that accepts the connection and then says
nothing is the case timeouts exist for; this script makes one with a bare
socket that never answers.

Usage: python timeouts.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import socket
import sys
import time

from xrdclient.compat import client  # was: from XRootD import client
from xrdclient.compat.client.flags import MkDirFlags

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

# Process-wide settings must be in place before the first connection is made.
client.EnvPutInt("RequestTimeout", 60)
client.EnvPutInt("ConnectionWindow", 3)
client.EnvPutInt("ConnectionRetry", 1)
client.EnvPutInt("TimeoutResolution", 1)
print("RequestTimeout is", client.EnvGetInt("RequestTimeout"))

fs = client.FileSystem(URL)

# A generous timeout on a healthy server changes nothing.
status, _ = fs.mkdir(WORK, MkDirFlags.MAKEPATH, timeout=30)
print("mkdir with timeout=30:", status.ok)
status, info = fs.stat(WORK, timeout=30)
print("stat with timeout=30:", status.ok)
status, _ = fs.ping(timeout=30)
print("ping with timeout=30:", status.ok)

# A server that never answers: the call gives up instead of hanging.
silent = socket.create_server(("127.0.0.1", 0))
port = silent.getsockname()[1]
started = time.monotonic()
status, info = client.FileSystem(f"root://127.0.0.1:{port}").stat("/anything", timeout=3)
elapsed = time.monotonic() - started
print("silent server: ok", status.ok, "error", status.error, "info", info)
print("gave up in under 30 s:", elapsed < 30)
silent.close()

fs.rmdir(WORK)
