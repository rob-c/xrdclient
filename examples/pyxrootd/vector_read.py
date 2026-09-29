#!/usr/bin/env python3
"""Read several scattered ranges in one request with vector_read.

Usage: python vector_read.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import sys

from xrdclient.compat import client  # was: from XRootD import client
from xrdclient.compat.client.flags import MkDirFlags, OpenFlags

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

fs = client.FileSystem(URL)
fs.mkdir(WORK, MkDirFlags.MAKEPATH)
path = f"{URL}/{WORK}/vector.txt"

with client.File() as f:
    f.open(path, OpenFlags.NEW)
    f.write(b"".join(b"record %04d\n" % n for n in range(1000)))

with client.File() as f:
    f.open(path)
    # (offset, length) pairs; every record is 12 bytes.
    ranges = [(0, 11), (12 * 500, 11), (12 * 999, 11), (12 * 42 + 7, 4)]
    status, response = f.vector_read(chunks=ranges)
    print("vector_read:", status.ok, "total", response.size, "chunks", len(response.chunks))
    for chunk in response.chunks:
        print(f"  offset {chunk.offset:>6} length {chunk.length:>2} -> {chunk.buffer!r}")

    # A range past the end of the file is an error for the whole request.
    status, response = f.vector_read(chunks=[(0, 4), (10**6, 4)])
    print("past the end: ok", status.ok, "errno", status.errno, "response", response)

fs.rm(f"{WORK}/vector.txt")
fs.rmdir(WORK)
