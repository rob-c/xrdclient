#!/usr/bin/env python3
"""Stream a file in fixed-size chunks with readchunks, and check what came back.

Usage: python read_chunks.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import hashlib
import sys

from xrdclient.compat import client  # was: from XRootD import client
from xrdclient.compat.client.flags import MkDirFlags, OpenFlags

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

fs = client.FileSystem(URL)
fs.mkdir(WORK, MkDirFlags.MAKEPATH)
path = f"{URL}/{WORK}/chunks.bin"

payload = bytes(range(256)) * 4000  # 1 024 000 bytes
with client.File() as f:
    f.open(path, OpenFlags.NEW)
    f.write(payload)

with client.File() as f:
    f.open(path)
    sizes = []
    digest = hashlib.sha256()
    for chunk in f.readchunks(offset=0, chunksize=256 * 1024):
        sizes.append(len(chunk))
        digest.update(chunk)
    print("chunks:", sizes)
    print("intact:", digest.hexdigest() == hashlib.sha256(payload).hexdigest())

    tail = list(f.readchunks(offset=1_000_000, chunksize=10_000))
    print("from offset 1000000:", [len(c) for c in tail], tail[0][:4])

fs.rm(f"{WORK}/chunks.bin")
fs.rmdir(WORK)
