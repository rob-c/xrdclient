#!/usr/bin/env python3
"""Ask the server for a file's checksum, and for its configuration, with query().

The answer to a query is the server's raw reply, as bytes.

Usage: python checksum_query.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import sys
import zlib

from xrdclient.compat import client  # was: from XRootD import client
from xrdclient.compat.client.flags import MkDirFlags, OpenFlags, QueryCode

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

fs = client.FileSystem(URL)
fs.mkdir(WORK, MkDirFlags.MAKEPATH)

data = b"checksum me\n" * 1000
with client.File() as f:
    f.open(f"{URL}/{WORK}/sum.dat", OpenFlags.NEW)
    f.write(data)

status, response = fs.query(QueryCode.CHECKSUM, f"{WORK}/sum.dat")
print("checksum: ok", status.ok, "reply", response)
name, value = response.decode().rstrip("\x00").split()
print("algorithm", name, "matches zlib.adler32:", int(value, 16) == zlib.adler32(data))

# A specific algorithm is asked for with the cks.type CGI.
status, response = fs.query(QueryCode.CHECKSUM, f"{WORK}/sum.dat?cks.type=crc32")
print("crc32: ok", status.ok, "reply", response)

# The checksum of a file that is not there is an error, not an empty answer.
status, response = fs.query(QueryCode.CHECKSUM, f"{WORK}/missing.dat")
print("missing: ok", status.ok, "errno", status.errno, "response", response)

# kXR_Qconfig answers one line per name asked for.
status, response = fs.query(QueryCode.CONFIG, "chksum\nreadv_iov_max\nreadv_ior_max")
print("config: ok", status.ok)
for line in response.decode().splitlines():
    print("  ", line)

fs.rm(f"{WORK}/sum.dat")
fs.rmdir(WORK)
