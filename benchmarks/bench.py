#!/usr/bin/env python3
"""Measure this client against ``xrdcp`` and the official bindings.

    python benchmarks/bench.py                 # start a local xrootd and use it
    python benchmarks/bench.py --url root://host//store/tmp
    python benchmarks/bench.py --size 512 --repeat 5 --json results.json

Every case is run ``--repeat`` times and the *best* time is reported, because
the interesting quantity is what the code can do, not what the machine was
doing at the same time. Loopback numbers say nothing about a wide-area
transfer; what they do show is where the client's own overhead lives, which is
the only part this repository can change.

Contenders, each skipped if absent: this library (``xrd``), the official
python bindings (``XRootD.client``), and the stock ``xrdcp`` binary, which
sets the bar because it is C++ with no interpreter in the loop.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

import xrdclient
from xrdclient.client.file import File
from xrdclient.config import Config
from xrdclient.flags import OpenFlags

CONFIG = Config(auth_order=("unix", "host"))

try:
    from XRootD import client as bindings
except ImportError:  # pragma: no cover - optional
    bindings = None


def _mib(count: int) -> str:
    return f"{count / (1 << 20):.1f} MiB"


class Timer:
    """Best-of-N timing, reported as both seconds and throughput."""

    def __init__(self, repeat: int) -> None:
        self.repeat = repeat
        self.rows: list[dict[str, object]] = []

    def run(self, case: str, who: str, fn: Callable[[], int]) -> None:
        times: list[float] = []
        moved = 0
        for _ in range(self.repeat):
            start = time.perf_counter()
            moved = fn()
            times.append(time.perf_counter() - start)
        best = min(times)
        row = {
            "case": case,
            "client": who,
            # Six places, not the two the printout shows: a metadata call on
            # loopback lands in tens of microseconds, and rounding that to
            # milliseconds turns every ratio below into a division by zero.
            "seconds": round(best, 6),
            "median": round(statistics.median(times), 6),
            "bytes": moved,
            "rate_mib_s": round(moved / best / (1 << 20), 1) if moved else None,
            "ops_s": round(1 / best, 1) if not moved else None,
        }
        self.rows.append(row)
        rate = f"{row['rate_mib_s']:>8} MiB/s" if moved else f"{row['ops_s']:>8} ops/s"
        print(f"  {case:<24} {who:<12} {best * 1000:8.2f} ms {rate}")

    def report(self) -> None:
        """Print each case's contenders relative to the fastest of them."""
        print("\nrelative to the best in each case (1.00 is fastest):")
        cases: dict[str, list[dict[str, object]]] = {}
        for row in self.rows:
            cases.setdefault(str(row["case"]), []).append(row)
        for case, rows in cases.items():
            best = min(float(r["seconds"]) for r in rows) or 1e-9  # type: ignore[arg-type]
            parts = " ".join(
                f"{r['client']}={float(r['seconds']) / best:.2f}x" for r in rows  # type: ignore[arg-type]
            )
            print(f"  {case:<24} {parts}")


@contextmanager
def endpoint(url: str | None) -> Iterator[tuple[str, str]]:
    """Yield ``(server_url, directory)``, starting a daemon if none was given."""
    if url:
        parsed = xrdclient.parse(url)
        base = f"{parsed.scheme}://{parsed.host}:{parsed.port}/"
        with xrdclient.FileSystem(base, CONFIG) as fs:
            fs.mkdir(parsed.path, parents=True, exist_ok=True)
        yield base, parsed.path
        return
    import _xrootd

    if not _xrootd.available():
        sys.exit("no xrootd binary on PATH and no --url given")
    root = tempfile.mkdtemp(prefix="xrdbench-")
    try:
        with _xrootd.RealServer(root) as server:
            with xrdclient.FileSystem(server.url, CONFIG) as fs:
                fs.mkdir(server.path("bench"))
            yield server.url, server.path("bench")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def full_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}//{path.lstrip('/')}"


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def bench_read(timer: Timer, base: str, remote: str, size: int) -> None:
    url = full_url(base, remote)

    def ours() -> int:
        with xrdclient.open(url, "rb", config=CONFIG) as fh:
            return len(fh.read())

    timer.run("read whole file", "xrd", ours)

    if bindings is not None:

        def theirs() -> int:
            handle = bindings.File()
            handle.open(url)
            try:
                status, data = handle.read()
                assert status.ok, status.message
                return len(data)
            finally:
                handle.close()

        timer.run("read whole file", "bindings", theirs)

    if shutil.which("xrdcp"):

        def tool() -> int:
            subprocess.run(
                ["xrdcp", "-f", "-s", url, "/dev/null"], check=True, capture_output=True
            )
            return size

        timer.run("read whole file", "xrdcp", tool)


def bench_chunked_read(timer: Timer, base: str, remote: str, size: int) -> None:
    """Many small reads: the case where per-request overhead is the cost."""
    url = full_url(base, remote)
    chunk, count = 64 << 10, 256

    def ours() -> int:
        with File(xrdclient.parse(url), CONFIG) as handle:
            return sum(len(handle.read(chunk, i * chunk)) for i in range(count))

    timer.run(f"{count} x {chunk >> 10}KiB reads", "xrd", ours)

    if bindings is not None:

        def theirs() -> int:
            handle = bindings.File()
            handle.open(url)
            try:
                return sum(len(handle.read(i * chunk, chunk)[1]) for i in range(count))
            finally:
                handle.close()

        timer.run(f"{count} x {chunk >> 10}KiB reads", "bindings", theirs)


def bench_vector_read(timer: Timer, base: str, remote: str) -> None:
    url = full_url(base, remote)
    ranges = [(i * (1 << 20), 128 << 10) for i in range(8)]

    def ours() -> int:
        with File(xrdclient.parse(url), CONFIG) as handle:
            return sum(len(piece) for piece in handle.readv(ranges))

    timer.run("vector read 8x128KiB", "xrd", ours)

    if bindings is not None:

        def theirs() -> int:
            handle = bindings.File()
            handle.open(url)
            try:
                status, result = handle.vector_read(ranges)
                assert status.ok, status.message
                return sum(len(bytes(chunk.buffer)) for chunk in result.chunks)
            finally:
                handle.close()

        timer.run("vector read 8x128KiB", "bindings", theirs)


def bench_write(timer: Timer, base: str, directory: str, payload: bytes) -> None:
    def ours() -> int:
        target = full_url(base, f"{directory}/w-xrd.root")
        with xrdclient.open(target, "wb", config=CONFIG) as fh:
            fh.write(payload)
        return len(payload)

    timer.run("write whole file", "xrd", ours)

    if bindings is not None:

        def theirs() -> int:
            handle = bindings.File()
            from XRootD.client.flags import OpenFlags as TheirFlags

            handle.open(
                full_url(base, f"{directory}/w-bindings.root"),
                TheirFlags.DELETE | TheirFlags.UPDATE,
            )
            try:
                status, _ = handle.write(payload)
                assert status.ok, status.message
                return len(payload)
            finally:
                handle.close()

        timer.run("write whole file", "bindings", theirs)


def bench_metadata(timer: Timer, base: str, directory: str, remote: str) -> None:
    url = full_url(base, remote)

    with xrdclient.FileSystem(base, CONFIG) as fs:
        timer.run("stat", "xrd", lambda: (fs.stat(remote), 0)[1])
        timer.run("listdir", "xrd", lambda: (fs.listdir(directory), 0)[1])

    if bindings is not None:
        theirs = bindings.FileSystem(base)
        timer.run("stat", "bindings", lambda: (theirs.stat(remote), 0)[1])
        timer.run("listdir", "bindings", lambda: (theirs.dirlist(directory), 0)[1])

    if shutil.which("xrdfs"):

        def tool() -> int:
            subprocess.run(["xrdfs", base, "stat", remote], check=True, capture_output=True)
            return 0

        timer.run("stat", "xrdfs", tool)

    del url


def bench_copy(timer: Timer, base: str, remote: str, size: int, scratch: Path) -> None:
    url = full_url(base, remote)

    def ours() -> int:
        result = xrdclient.copy(url, str(scratch / "xrdclient.root"), config=CONFIG, verify=False)
        return result.size

    timer.run("copy to local disk", "xrd", ours)

    if bindings is not None:

        def theirs() -> int:
            process = bindings.CopyProcess()
            process.add_job(url, str(scratch / "bindings.root"), force=True)
            process.prepare()
            status = process.run()[0]
            assert status.ok, status.message
            return size

        timer.run("copy to local disk", "bindings", theirs)

    if shutil.which("xrdcp"):

        def tool() -> int:
            subprocess.run(
                ["xrdcp", "-f", "-s", url, str(scratch / "xrdcp.root")],
                check=True,
                capture_output=True,
            )
            return size

        timer.run("copy to local disk", "xrdcp", tool)


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="an existing endpoint and directory to use")
    parser.add_argument("--size", type=int, default=64, help="test file size in MiB")
    parser.add_argument("--repeat", type=int, default=3, help="runs per case; the best one counts")
    parser.add_argument("--json", help="also write the raw numbers here")
    args = parser.parse_args(argv)

    size = args.size << 20
    payload = os.urandom(1 << 20) * args.size
    timer = Timer(args.repeat)

    with endpoint(args.url) as (base, directory), tempfile.TemporaryDirectory() as scratch:
        remote = f"{directory}/bench.root"
        print(f"endpoint {base}, {_mib(size)} in {remote}\n")
        handle = File(xrdclient.parse(full_url(base, remote)), CONFIG)
        handle.open(OpenFlags.NEW | OpenFlags.DELETE | OpenFlags.UPDATE | OpenFlags.MAKEPATH)
        with handle:
            handle.write(payload, 0)

        bench_read(timer, base, remote, size)
        bench_chunked_read(timer, base, remote, size)
        bench_vector_read(timer, base, remote)
        bench_write(timer, base, directory, payload)
        bench_metadata(timer, base, directory, remote)
        bench_copy(timer, base, remote, size, Path(scratch))

    timer.report()
    if args.json:
        Path(args.json).write_text(json.dumps(timer.rows, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
