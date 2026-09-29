#!/usr/bin/env python3
"""Ask a server the basics: ping, protocol, stat of a file and a directory, statvfs.

Every call returns a ``(status, response)`` pair; ``status.ok`` says whether
the response is there.

Usage: python stat_and_ping.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import sys

from xrdclient.compat import client  # was: from XRootD import client
from xrdclient.compat.client.flags import MkDirFlags, OpenFlags, StatInfoFlags

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

fs = client.FileSystem(URL)

status, response = fs.ping()
print("ping:", status.ok, response)

status, protocol = fs.protocol()
print("protocol: ok", status.ok, "version", hex(protocol.version), "hostinfo", protocol.hostinfo)

status, _ = fs.mkdir(WORK, MkDirFlags.MAKEPATH)
assert status.ok, status.message

with client.File() as f:
    status, _ = f.open(f"{URL}/{WORK}/hello.txt", OpenFlags.NEW)
    assert status.ok, status.message
    f.write(b"hello, world\n")

status, info = fs.stat(f"{WORK}/hello.txt")
print("file: size", info.size, "is dir", bool(info.flags & StatInfoFlags.IS_DIR))
print("file: readable", bool(info.flags & StatInfoFlags.IS_READABLE))
print(
    "file: has a modification time", info.modtime > 0, "and a string for it", bool(info.modtimestr)
)

status, info = fs.stat(WORK)
print("dir: is dir", bool(info.flags & StatInfoFlags.IS_DIR))

status, vfs = fs.statvfs(WORK)
print(
    "statvfs: ok",
    status.ok,
    "has rw nodes",
    vfs.nodes_rw >= 0,
    "free is a number",
    vfs.free_rw >= 0,
)

fs.rm(f"{WORK}/hello.txt")
fs.rmdir(WORK)
