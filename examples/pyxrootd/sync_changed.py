#!/usr/bin/env python3
"""A real workflow: mirror a remote dataset locally, fetching only what changed.

1. discover the files with glob,
2. ask the server for each one's adler32,
3. compare with the checksums recorded at the last sync,
4. copy just the new and changed files, in parallel, with CopyProcess,
5. record the new checksums.

It runs the sync twice, changing one file and adding another in between, to
show that the second pass copies two files and not five.

Usage: python sync_changed.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import zlib

from xrdclient.compat import client  # was: from XRootD import client
from xrdclient.compat.client.flags import MkDirFlags, OpenFlags, QueryCode

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

fs = client.FileSystem(URL)


def put(name: str, data: bytes) -> None:
    """Write a whole file on the server, replacing what was there."""
    with client.File() as f:
        status, _ = f.open(f"{URL}/{WORK}/dataset/{name}", OpenFlags.DELETE)
        if not status.ok:
            raise SystemExit(f"cannot write {name}: {status.message}")
        f.write(data)


def remote_checksum(path: str) -> str:
    """The server's adler32 for ``path``, as 8 hex digits."""
    status, response = fs.query(QueryCode.CHECKSUM, path)
    if not status.ok:
        raise SystemExit(f"no checksum for {path}: {status.message}")
    return response.decode().rstrip("\x00").split()[1]


def sync(mirror: str) -> None:
    """Bring ``mirror`` up to date with the dataset on the server."""
    manifest_path = os.path.join(mirror, "manifest.json")
    try:
        with open(manifest_path) as handle:
            manifest = json.load(handle)
    except FileNotFoundError:
        manifest = {}

    process = client.CopyProcess()
    wanted = {}
    for url in sorted(client.glob(f"{URL}/{WORK}/dataset/*.dat")):
        name = url.rsplit("/", 1)[1]
        wanted[name] = remote_checksum(f"{WORK}/dataset/{name}")
        if manifest.get(name) == wanted[name]:
            print(f"   {name}: unchanged")
            continue
        print(f"   {name}: {'changed' if name in manifest else 'new'}, fetching")
        process.add_job(url, os.path.join(mirror, name), force=True)

    process.parallel(4)
    process.prepare()
    status, results = process.run()
    failed = [r for r in results if not r["status"].ok]
    print(f"   run ok={status.ok}: copied {len(results)} file(s), {len(failed)} failed")

    # Check what landed against what the server said, then remember it.
    for name, checksum in wanted.items():
        with open(os.path.join(mirror, name), "rb") as handle:
            assert f"{zlib.adler32(handle.read()):08x}" == checksum, name
    with open(manifest_path, "w") as handle:
        json.dump(wanted, handle)


fs.mkdir(f"{WORK}/dataset", MkDirFlags.MAKEPATH)
for n in range(5):
    put(f"part{n}.dat", f"part {n}\n".encode() * 1000)

with tempfile.TemporaryDirectory() as mirror:
    print("first sync:")
    sync(mirror)

    # The server caches each checksum against the file's size and its
    # modification time, which has one-second resolution: a rewrite of the
    # same size within the same second would be answered from the stale
    # cache. Real reprocessing takes longer than a second; this script does
    # not, so it waits for the clock to move on.
    time.sleep(1.1)
    put("part3.dat", b"part 3, reprocessed\n" * 1000)
    put("part5.dat", b"part 5, new\n" * 1000)

    print("second sync:")
    sync(mirror)
    print("mirror holds:", sorted(os.listdir(mirror)))

for n in range(6):
    fs.rm(f"{WORK}/dataset/part{n}.dat")
fs.rmdir(f"{WORK}/dataset")
fs.rmdir(WORK)
