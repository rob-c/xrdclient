#!/usr/bin/env python3
"""Run code that says ``from XRootD import client`` - unmodified - on xrdclient.

For code that cannot be edited (a vendored library, someone else's tool),
``xrdclient.compat.install()`` makes the name ``XRootD`` itself resolve to the
compat layer. It must run before anything imports the real bindings, and it
refuses if they are already imported rather than mixing the two.

The two lines marked ``compat only`` are the whole change; without them this
is an ordinary PyXRootD script.

Usage: python install_hook.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import sys

import xrdclient.compat  # compat only

xrdclient.compat.install()  # compat only

# From here on, the file is exactly what a PyXRootD user wrote.
from XRootD import client  # noqa: E402
from XRootD.client.flags import MkDirFlags, OpenFlags  # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

fs = client.FileSystem(URL)
status, _ = fs.mkdir(WORK, MkDirFlags.MAKEPATH)
print("mkdir:", status.ok)

with client.File() as f:
    status, _ = f.open(f"{URL}/{WORK}/hooked.txt", OpenFlags.NEW)
    print("open:", status.ok)
    f.write(b"written through whichever client answers to XRootD\n")

with client.File() as f:
    f.open(f"{URL}/{WORK}/hooked.txt")
    print("read back:", f.read()[1])

status, info = fs.stat(f"{WORK}/hooked.txt")
print("stat: size", info.size)

fs.rm(f"{WORK}/hooked.txt")
fs.rmdir(WORK)
