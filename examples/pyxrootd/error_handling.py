#!/usr/bin/env python3
"""What a failure looks like: the fields of XRootDStatus, and a script's exit code.

Nothing raises. A failed call returns a status with ``ok`` false, and the
fields say why: ``errno`` is the server's error number (3011 is kXR_NotFound),
``code`` is XrdCl's (400 is errErrorResponse: the server said no),
``shellcode`` is what ``xrdcp`` would exit with.

Usage: python error_handling.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import sys

from xrdclient.compat import client  # was: from XRootD import client
from xrdclient.compat.client.flags import MkDirFlags, OpenFlags

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

fs = client.FileSystem(URL)
fs.mkdir(WORK, MkDirFlags.MAKEPATH)


def explain(label, status):
    """Every field a script might branch on."""
    print(label)
    print(f"   ok={status.ok} error={status.error} fatal={status.fatal}")
    print(f"   status={status.status} code={status.code} errno={status.errno}")
    print(f"   shellcode={status.shellcode}")
    print(f"   message mentions the path: {'missing' in status.message}")


status, info = fs.stat(f"{WORK}/missing.root")
explain("stat of a missing file", status)
print("   response:", info)

f = client.File()
status, _ = f.open(f"{URL}/{WORK}/missing.root")
explain("open of a missing file", status)
print("   is_open:", f.is_open())
# Using a File that is not open is a programming error, and that does raise.
try:
    f.read()
except ValueError as exc:
    print("read on a File that is not open: ValueError:", exc)

# Not every "already there" is an error: this server lets mkdir succeed.
status, _ = fs.mkdir(WORK)
print("mkdir of an existing directory: ok", status.ok, "errno", status.errno)

with client.File() as f:
    f.open(f"{URL}/{WORK}/exists.txt", OpenFlags.NEW)
status, _ = client.File().open(f"{URL}/{WORK}/exists.txt", OpenFlags.NEW)
print("NEW over an existing file: errno", status.errno, "code", status.code)

# The status converts to text for a log line.
status, _ = fs.rm(f"{WORK}/missing.root")
print("str(status) starts with:", str(status).split("]", 1)[0] + "]")

# The idiom most scripts use.
status, _ = fs.stat(f"{WORK}/missing.root")
if not status.ok:
    print(f"giving up: exit code would be {status.shellcode}")

fs.rm(f"{WORK}/exists.txt")
fs.rmdir(WORK)
