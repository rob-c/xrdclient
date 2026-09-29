#!/usr/bin/env python3
"""Write and read a file: open, write, read(offset, size), stat, sync, truncate, close.

The ``with`` block closes the file however it is left.

Usage: python file_io.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import sys

from xrdclient.compat import client  # was: from XRootD import client
from xrdclient.compat.client.flags import MkDirFlags, OpenFlags

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

fs = client.FileSystem(URL)
fs.mkdir(WORK, MkDirFlags.MAKEPATH)
path = f"{URL}/{WORK}/io.bin"

with client.File() as f:
    status, _ = f.open(path, OpenFlags.NEW | OpenFlags.UPDATE)
    print("open:", status.ok, "is_open", f.is_open())

    status, _ = f.write(b"The quick brown fox ")
    print("write:", status.ok)
    status, _ = f.write(b"jumps over the lazy dog\n", offset=20)
    print("write at 20:", status.ok)
    # str is accepted and written as UTF-8.
    status, _ = f.write("and naps.\n", offset=44)
    print("write str:", status.ok)

    status, _ = f.sync()
    print("sync:", status.ok)

    status, data = f.read()
    print("read all:", status.ok, data)
    status, data = f.read(4, 5)
    print("read(4, 5):", data)
    status, data = f.read(1000, 10)
    print("read past the end:", status.ok, data)

    status, info = f.stat()
    print("stat:", status.ok, "size", info.size)

    status, _ = f.truncate(9)
    print("truncate(9):", status.ok, f.read()[1])
    print("stat(force=True) size", f.stat(force=True)[1].size)

print("closed by with:", not f.is_open())

# Reopen read-only: writing is refused, reading works.
f = client.File()
status, _ = f.open(path, OpenFlags.READ)
status, _ = f.write(b"nope")
print("write on read-only: ok", status.ok, "errno", status.errno)
print("read-only read:", f.read()[1])
status, _ = f.close()
print("close:", status.ok)

fs.rm(f"{WORK}/io.bin")
fs.rmdir(WORK)
