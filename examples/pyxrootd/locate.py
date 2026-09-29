#!/usr/bin/env python3
"""Find where a file lives with locate(), and read the answer's fields.

On a single data server the answer is that server; behind a redirector it is
every server holding the file.

Usage: python locate.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import sys

from xrdclient.compat import client  # was: from XRootD import client
from xrdclient.compat.client.flags import MkDirFlags, OpenFlags

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

fs = client.FileSystem(URL)
fs.mkdir(WORK, MkDirFlags.MAKEPATH)
with client.File() as f:
    f.open(f"{URL}/{WORK}/here.txt", OpenFlags.NEW)
    f.write(b"x")

status, locations = fs.locate(f"{WORK}/here.txt", OpenFlags.NONE)
print("locate: ok", status.ok, "locations", len(locations.locations))
for location in locations:
    print("  address", location.address)
    print("  is a server", location.type == client.flags.LocationType.SERVER_ONLINE)
    print("  can write", location.accesstype == client.flags.AccessType.READ_WRITE)
    print("  is_server", location.is_server, "is_manager", location.is_manager)

status, locations = fs.deeplocate(f"{WORK}/here.txt", OpenFlags.NONE)
print("deeplocate: ok", status.ok, "locations", len(locations.locations))

status, locations = fs.locate(f"{WORK}/nowhere.txt", OpenFlags.NONE)
print("missing: ok", status.ok, "errno", status.errno, "response", locations)

fs.rm(f"{WORK}/here.txt")
fs.rmdir(WORK)
