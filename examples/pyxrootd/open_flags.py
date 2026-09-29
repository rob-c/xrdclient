#!/usr/bin/env python3
"""What each OpenFlags value does: READ, NEW, DELETE, UPDATE, MAKEPATH.

Usage: python open_flags.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import sys

from xrdclient.compat import client  # was: from XRootD import client
from xrdclient.compat.client.flags import AccessMode, OpenFlags

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

fs = client.FileSystem(URL)
path = f"{URL}/{WORK}/deep/er/flags.txt"


def attempt(label, flags, mode=0, data=None):
    """Open with ``flags``, optionally write, and say what happened."""
    with client.File() as f:
        status, _ = f.open(path, flags, mode)
        wrote = None
        if status.ok and data is not None:
            wrote = f.write(data)[0].ok
        size = fs.stat(f"{WORK}/deep/er/flags.txt")[1]
        print(f"{label:<24} ok={status.ok!s:<5} errno={status.errno:<5} wrote={wrote}", end="")
        print(f" size={size.size if size else None}")


attempt("READ, nothing there", OpenFlags.READ)
attempt("NEW, no parent", OpenFlags.NEW, data=b"x")
attempt(
    "NEW | MAKEPATH", OpenFlags.NEW | OpenFlags.MAKEPATH, AccessMode.UR | AccessMode.UW, b"hello"
)
attempt("NEW when it exists", OpenFlags.NEW)
attempt("READ, then write", OpenFlags.READ, data=b"nope")
attempt("UPDATE, write at 0", OpenFlags.UPDATE, data=b"J")
attempt("DELETE truncates", OpenFlags.DELETE)
attempt("DELETE, then write", OpenFlags.DELETE, data=b"fresh content")

with client.File() as f:
    f.open(path)
    print("content:", f.read()[1])

fs.rm(f"{WORK}/deep/er/flags.txt")
for directory in ("deep/er", "deep", ""):
    fs.rmdir(f"{WORK}/{directory}".rstrip("/"))
