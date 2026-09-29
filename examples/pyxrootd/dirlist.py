#!/usr/bin/env python3
"""List a directory three ways: names only, with stat information, and recursively.

Usage: python dirlist.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import sys

from xrdclient.compat import client  # was: from XRootD import client
from xrdclient.compat.client.flags import DirListFlags, MkDirFlags, OpenFlags, StatInfoFlags

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

fs = client.FileSystem(URL)
FILES = {"a.txt": b"a" * 10, "b.txt": b"b" * 20, "sub/c.txt": b"c" * 30, "sub/deeper/d.txt": b"d"}

status, _ = fs.mkdir(f"{WORK}/sub/deeper", MkDirFlags.MAKEPATH)
assert status.ok, status.message
for name, data in FILES.items():
    with client.File() as f:
        f.open(f"{URL}/{WORK}/{name}", OpenFlags.NEW)
        f.write(data)

# Names only: each entry's statinfo is None.
status, listing = fs.dirlist(WORK)
print("plain: ok", status.ok, "parent", listing.parent == WORK + "/", "size", listing.size)
for entry in sorted(listing, key=lambda e: e.name):
    print("  ", entry.name, entry.statinfo)

# With DirListFlags.STAT every entry carries a StatInfo.
status, listing = fs.dirlist(WORK, DirListFlags.STAT)
print("stat: ok", status.ok)
for entry in sorted(listing, key=lambda e: e.name):
    kind = "dir " if entry.statinfo.flags & StatInfoFlags.IS_DIR else "file"
    size = "-" if kind == "dir " else entry.statinfo.size
    print("  ", kind, entry.name, size)

# RECURSIVE walks the tree; names are relative to the directory listed.
status, listing = fs.dirlist(WORK, DirListFlags.RECURSIVE | DirListFlags.STAT)
print("recursive: ok", status.ok)
for entry in sorted(listing, key=lambda e: e.name):
    if not entry.statinfo.flags & StatInfoFlags.IS_DIR:
        print("  ", entry.name, entry.statinfo.size)

# Clean up, deepest first.
for name in sorted(FILES, key=lambda n: -n.count("/")):
    fs.rm(f"{WORK}/{name}")
for directory in ("sub/deeper", "sub", ""):
    fs.rmdir(f"{WORK}/{directory}".rstrip("/"))
