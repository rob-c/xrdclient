#!/usr/bin/env python3
"""Asynchronous calls: pass ``callback=`` and the call returns at once.

The callback is called later, from another thread, with ``(status, response,
hostlist)``. :class:`client.utils.AsyncResponseHandler` is a ready-made
callback that lets the caller ``wait()`` for that answer; a plain function
works too.

Usage: python async_callbacks.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import sys
import threading

from xrdclient.compat import client  # was: from XRootD import client
from xrdclient.compat.client.flags import DirListFlags, MkDirFlags, OpenFlags
from xrdclient.compat.client.utils import AsyncResponseHandler

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

fs = client.FileSystem(URL)

# The handler form: submit, do something else, then wait.
handler = AsyncResponseHandler()
status = fs.mkdir(WORK, MkDirFlags.MAKEPATH, callback=handler)
print("mkdir submitted:", status.ok)
status, response, hostlist = handler.wait()
print("mkdir answered:", status.ok, response, "hosts", len(hostlist.hosts) > 0)

# Several requests in flight at once, each with its own handler.
files = [f"{URL}/{WORK}/f{n}.txt" for n in range(4)]
opened = []
for n, path in enumerate(files):
    f = client.File()
    handler = AsyncResponseHandler()
    f.open(path, OpenFlags.NEW, callback=handler)
    opened.append((f, handler, n))
for f, handler, n in opened:
    status, _, _ = handler.wait()
    print(f"open f{n}:", status.ok)
    # Keep the buffer referenced until the write has been answered: the
    # official bindings do not hold on to it, and a temporary can be freed
    # and reused while the request is still in flight.
    data = f"file number {n}\n".encode()
    handler = AsyncResponseHandler()
    f.write(data, callback=handler)
    print(f"write f{n}:", handler.wait()[0].ok)
    handler = AsyncResponseHandler()
    f.close(callback=handler)
    print(f"close f{n}:", handler.wait()[0].ok)

# The function form: any callable taking (status, response, hostlist).
done = threading.Event()
seen = []


def listed(status, response, hostlist):
    seen.append(sorted(entry.name for entry in response) if status.ok else status.errno)
    done.set()


fs.dirlist(WORK, DirListFlags.STAT, callback=listed)
done.wait(60)
print("dirlist via a function:", seen[0])

# Reading asynchronously from an open file.
with client.File() as f:
    f.open(files[2])
    handler = AsyncResponseHandler()
    f.read(0, 100, callback=handler)
    status, data, _ = handler.wait()
    print("async read:", status.ok, data)

# A failure arrives through the callback too, not as an exception.
handler = AsyncResponseHandler()
fs.stat(f"{WORK}/missing", callback=handler)
status, response, _ = handler.wait()
print("async stat of a missing file: ok", status.ok, "errno", status.errno, "response", response)

for n in range(len(files)):
    fs.rm(f"{WORK}/f{n}.txt")
fs.rmdir(WORK)
