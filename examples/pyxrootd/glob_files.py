#!/usr/bin/env python3
"""Expand wildcards on the server with glob and iglob, as the ``glob`` module does locally.

Usage: python glob_files.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import sys

from xrdclient.compat import client  # was: from XRootD import client
from xrdclient.compat.client.flags import MkDirFlags, OpenFlags

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

fs = client.FileSystem(URL)
NAMES = ["run1/a.root", "run1/b.root", "run1/notes.txt", "run2/c.root", "run10/d.root"]
for name in NAMES:
    fs.mkdir(f"{WORK}/{name.rsplit('/', 1)[0]}", MkDirFlags.MAKEPATH)
    with client.File() as f:
        f.open(f"{URL}/{WORK}/{name}", OpenFlags.NEW)
        f.write(b"data")

base = f"{URL}/{WORK}"


def short(urls):
    """The part of each URL below the working directory, sorted."""
    return sorted(u.split(WORK, 1)[1] for u in urls)


print("run*:           ", short(client.glob(f"{base}/run*")))
print("run?/*.root:    ", short(client.glob(f"{base}/run?/*.root")))
print("run*/*.root:    ", short(client.glob(f"{base}/run*/*.root")))
print("run[12]/*:      ", short(client.glob(f"{base}/run[12]/*")))
print("iglob run1/*.txt:", short(client.iglob(f"{base}/run1/*.txt")))
print("no match:       ", client.glob(f"{base}/run*/*.csv"))

# A directory that is not there raises by default, or reads as empty.
try:
    client.glob(f"{base}/nowhere/*")
except RuntimeError:
    print("missing directory: RuntimeError")
print("missing, raise_error=False:", client.glob(f"{base}/nowhere/*", raise_error=False))

for name in NAMES:
    fs.rm(f"{WORK}/{name}")
for directory in ("run1", "run2", "run10", ""):
    fs.rmdir(f"{WORK}/{directory}".rstrip("/"))
