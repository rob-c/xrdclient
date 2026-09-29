#!/usr/bin/env python3
"""Change the namespace: mkdir (with and without MAKEPATH), mv, truncate, chmod, rm, rmdir.

Usage: python namespace_ops.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import sys

from xrdclient.compat import client  # was: from XRootD import client
from xrdclient.compat.client.flags import AccessMode, MkDirFlags, OpenFlags, StatInfoFlags

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

fs = client.FileSystem(URL)


def show(label: str, status: client.responses.XRootDStatus) -> None:
    """One line per call: whether it worked, and XrdCl's error number if not."""
    print(f"{label:<28} ok={status.ok} errno={status.errno}")


# Without MAKEPATH the parent must exist; with it, the whole path is made.
status, _ = fs.mkdir(f"{WORK}/a/b/c")
show("mkdir a/b/c (no parent)", status)
status, _ = fs.mkdir(f"{WORK}/a/b/c", MkDirFlags.MAKEPATH)
show("mkdir a/b/c MAKEPATH", status)
status, _ = fs.mkdir(f"{WORK}/a/b/c")
show("mkdir a/b/c again", status)

with client.File() as f:
    f.open(f"{URL}/{WORK}/a/data.bin", OpenFlags.NEW)
    f.write(b"0123456789" * 10)

status, _ = fs.mv(f"{WORK}/a/data.bin", f"{WORK}/a/b/moved.bin")
show("mv data.bin -> b/moved.bin", status)
status, info = fs.stat(f"{WORK}/a/b/moved.bin")
print("  moved size", info.size)

status, _ = fs.truncate(f"{WORK}/a/b/moved.bin", 42)
show("truncate to 42", status)
print("  size now", fs.stat(f"{WORK}/a/b/moved.bin")[1].size)

status, _ = fs.chmod(f"{WORK}/a/b/moved.bin", AccessMode.UR | AccessMode.UW | AccessMode.GR)
show("chmod 0640", status)
_, info = fs.stat(f"{WORK}/a/b/moved.bin")
print("  readable", bool(info.flags & StatInfoFlags.IS_READABLE))
print("  writable", bool(info.flags & StatInfoFlags.IS_WRITABLE))

status, _ = fs.rmdir(f"{WORK}/a/b")
show("rmdir b (not empty)", status)

status, _ = fs.rm(f"{WORK}/a/b/moved.bin")
show("rm b/moved.bin", status)
status, _ = fs.rm(f"{WORK}/a/b/moved.bin")
show("rm b/moved.bin again", status)

for directory in ("a/b/c", "a/b", "a", ""):
    status, _ = fs.rmdir(f"{WORK}/{directory}".rstrip("/"))
    show(f"rmdir {directory or '.'}", status)
