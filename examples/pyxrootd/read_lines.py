#!/usr/bin/env python3
"""Read a text file by lines: readline, readlines, and iterating over the File.

Usage: python read_lines.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import sys

from xrdclient.compat import client  # was: from XRootD import client
from xrdclient.compat.client.flags import MkDirFlags, OpenFlags

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

fs = client.FileSystem(URL)
fs.mkdir(WORK, MkDirFlags.MAKEPATH)
path = f"{URL}/{WORK}/lines.txt"

with client.File() as f:
    f.open(path, OpenFlags.NEW)
    f.write(b"first line\nsecond line\n\nfourth, after an empty one\nno newline at the end")

with client.File() as f:
    f.open(path)
    # readline keeps its own cursor, the way a Python file does.
    print("readline:", repr(f.readline()))
    print("readline:", repr(f.readline()))
    print("readline at offset 6:", repr(f.readline(offset=6)))
    print("readline, 4 bytes:", repr(f.readline(offset=0, size=4)))

with client.File() as f:
    f.open(path)
    lines = f.readlines()
    print("readlines:", len(lines), "lines")
    for line in lines:
        print("  ", repr(line))

with client.File() as f:
    f.open(path)
    for number, line in enumerate(f, 1):
        print(f"line {number}: {line!r}")

fs.rm(f"{WORK}/lines.txt")
fs.rmdir(WORK)
