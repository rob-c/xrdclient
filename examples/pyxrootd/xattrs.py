#!/usr/bin/env python3
"""Extended attributes: set, get, list and delete, on a path and on an open File.

Each call takes a list and answers with one tuple per attribute - ``(name,
status)`` or ``(name, value, status)`` - so one missing name does not fail the
rest. The per-attribute status is a plain dict with the XRootDStatus fields.

Usage: python xattrs.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import sys

from xrdclient.compat import client  # was: from XRootD import client
from xrdclient.compat.client.flags import MkDirFlags, OpenFlags

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

fs = client.FileSystem(URL)
fs.mkdir(WORK, MkDirFlags.MAKEPATH)
path = f"{WORK}/tagged.dat"

print("-- on a path, through the FileSystem")
with client.File() as f:
    f.open(f"{URL}/{path}", OpenFlags.NEW)
    f.write(b"payload")

status, results = fs.set_xattr(path, [("user.run", "2024A"), ("user.owner", "physics")])
print("set_xattr:", status.ok, [(name, st["ok"]) for name, st in results])

status, results = fs.get_xattr(path, ["user.run", "user.owner", "user.absent"])
print("get_xattr:", status.ok)
for name, value, st in results:
    print(f"   {name}: {value!r} ok={st['ok']} errno={st['errno']}")

status, results = fs.list_xattr(path)
print("list_xattr:", status.ok, sorted((name, value) for name, value, _ in results))

status, results = fs.del_xattr(path, ["user.owner"])
print("del_xattr:", status.ok, [(name, st["ok"]) for name, st in results])
status, results = fs.list_xattr(path)
print("after del:", sorted(name for name, _, _ in results))

print("-- on an open File")
with client.File() as f:
    f.open(f"{URL}/{path}", OpenFlags.UPDATE)
    status, results = f.set_xattr(attrs=[("user.checked", "yes")])
    print("set_xattr:", status.ok, [(name, st["ok"]) for name, st in results])
    status, results = f.get_xattr(attrs=["user.checked", "user.run"])
    print("get_xattr:", [(name, value) for name, value, _ in results])
    status, results = f.list_xattr()
    print("list_xattr:", sorted(name for name, _, _ in results))
    status, results = f.del_xattr(attrs=["user.checked", "user.run"])
    print("del_xattr:", [(name, st["ok"]) for name, st in results])
    print("left:", f.list_xattr()[1])

fs.rm(path)
fs.rmdir(WORK)
