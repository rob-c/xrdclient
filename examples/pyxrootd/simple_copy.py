#!/usr/bin/env python3
"""The one-call copy: FileSystem.copy, for when CopyProcess is more than needed.

Usage: python simple_copy.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import os
import sys
import tempfile

from xrdclient.compat import client  # was: from XRootD import client
from xrdclient.compat.client.flags import MkDirFlags

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

fs = client.FileSystem(URL)
fs.mkdir(WORK, MkDirFlags.MAKEPATH)

with tempfile.TemporaryDirectory() as local:
    source = os.path.join(local, "up.txt")
    with open(source, "w") as handle:
        handle.write("uploaded with FileSystem.copy\n" * 100)

    status, _ = fs.copy(source, f"{URL}/{WORK}/copied.txt")
    print("upload:", status.ok)

    # The target exists now: without force the copy is refused.
    status, _ = fs.copy(source, f"{URL}/{WORK}/copied.txt")
    print("upload again: ok", status.ok)
    status, _ = fs.copy(source, f"{URL}/{WORK}/copied.txt", force=True)
    print("upload again, force=True:", status.ok)

    # Server to server, then back down.
    status, _ = fs.copy(f"{URL}/{WORK}/copied.txt", f"{URL}/{WORK}/second.txt")
    print("remote to remote:", status.ok)
    target = os.path.join(local, "down.txt")
    status, _ = fs.copy(f"{URL}/{WORK}/second.txt", target)
    print("download:", status.ok)

    with open(source) as a, open(target) as b:
        print("identical:", a.read() == b.read())

for name in ("copied.txt", "second.txt"):
    fs.rm(f"{WORK}/{name}")
fs.rmdir(WORK)
