#!/usr/bin/env python3
"""Copy files with CopyProcess, watching progress through a CopyProgressHandler.

Two jobs: an upload of a local file and a download of it again, each checked
end to end with adler32 - the source's checksum is compared with the
target's once the bytes are across.

Usage: python copy_process.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import os
import sys
import tempfile

from xrdclient.compat import client  # was: from XRootD import client
from xrdclient.compat.client.flags import MkDirFlags

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"


class Progress(client.utils.CopyProgressHandler):
    """Print the start and the end of each job, and whether it saw progress."""

    def __init__(self) -> None:
        self.updates: dict[int, tuple[int, int]] = {}

    def begin(self, jobId, total, source, target):
        print(f"job {jobId} of {total}: {source.protocol} -> {target.protocol}")

    def update(self, jobId, processed, total):
        # How many updates arrive depends on the chunking; the last one does not.
        self.updates[jobId] = (processed, total)

    def end(self, jobId, results):
        processed, total = self.updates.get(jobId, (0, 0))
        print(f"job {jobId} done: {results['status']} last update {processed}/{total}")

    def should_cancel(self, jobId):
        return False


fs = client.FileSystem(URL)
fs.mkdir(WORK, MkDirFlags.MAKEPATH)

with tempfile.TemporaryDirectory() as local:
    source = os.path.join(local, "source.bin")
    back = os.path.join(local, "back.bin")
    payload = os.urandom(3 * 1024 * 1024 + 17)
    with open(source, "wb") as out:
        out.write(payload)

    process = client.CopyProcess()
    process.add_job(
        source,
        f"{URL}/{WORK}/uploaded.bin",
        checksummode="end2end",
        checksumtype="adler32",
        force=True,
    )
    process.add_job(
        f"{URL}/{WORK}/uploaded.bin",
        back,
        checksummode="end2end",
        checksumtype="adler32",
    )
    status = process.prepare()
    print("prepare:", status.ok)

    status, results = process.run(Progress())
    print("run:", status.ok)
    for number, result in enumerate(results, 1):
        print(f"result {number}: ok={result['status'].ok}")

    with open(back, "rb") as copied:
        print("round trip intact:", copied.read() == payload)

    # A job whose source is missing fails on its own; run() still reports it.
    process = client.CopyProcess()
    process.add_job(f"{URL}/{WORK}/missing.bin", os.path.join(local, "never.bin"))
    process.prepare()
    status, results = process.run()
    print("missing source: ok", results[0]["status"].ok, "errno", results[0]["status"].errno)

fs.rm(f"{WORK}/uploaded.bin")
fs.rmdir(WORK)
