#!/usr/bin/env python3
"""Head to head with the official XRootD Python bindings, case by case, as a gate.

    python benchmarks/compare.py                      # loopback, print the table
    python benchmarks/compare.py --rtt 2 --rounds 15  # through a 2 ms round-trip proxy
    python benchmarks/compare.py --gate               # exit 1 if any case is lost

Every case is run by every contender in the same round, in a rotating order,
for ``--rounds`` rounds, so drift in the machine lands on all of them alike.
A case is *won* by this library when its median beats the official bindings'
median and a one-sided sign test over the paired rounds agrees at ``--alpha``:
not "faster once", but faster reliably enough that a coin would not have
produced the same record. ``--gate`` turns a case that is not won into a
failing exit status, which is what makes "faster" a property the build keeps
rather than a number someone measured once.

The contenders, each skipped when absent:

``xrdclient``
    the native API, written the way its documentation says to use it;
``compat``
    ``xrdclient.compat.client``, running the official bindings' own calls;
``official``
    ``XRootD.client``, the same calls, on ``libXrdCl``.

Latency cases time one operation at a time and report the median; throughput
cases move a fixed amount of data and report MiB/s. ``--rtt`` puts a relay in
front of the server that holds every packet for half the round trip each way,
which is what a real network adds and loopback hides: on loopback the cost is
per-call overhead, across a network it is how many requests are in flight.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

import xrdclient  # noqa: E402
from xrdclient.client.file import File  # noqa: E402
from xrdclient.compat import client as compat  # noqa: E402
from xrdclient.compat.client import env as compat_env  # noqa: E402
from xrdclient.config import Config  # noqa: E402

try:
    from XRootD import client as official
except ImportError:  # pragma: no cover - the bindings are optional
    official = None

CONFIG = Config(auth_order=("unix", "host"))
compat_env.config = lambda: CONFIG  # compat objects use the same settings

MiB = 1 << 20

# ---------------------------------------------------------------------------
# A server, and a relay that adds a round trip
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def server(size: int) -> Iterator[tuple[str, Path]]:
    """A stock xrootd exporting '/' from a sandbox, with the fixtures in it."""
    base = Path(tempfile.mkdtemp(prefix="xrdcmp"))
    data = base / "data"
    (data / "bench" / "many").mkdir(parents=True)
    (data / "bench" / "big.bin").write_bytes(os.urandom(size))
    (data / "bench" / "small.bin").write_bytes(os.urandom(64 << 10))
    for n in range(1000):
        (data / "bench" / "many" / f"f{n:04d}").write_bytes(b"x")
    port = _free_port()
    (base / "xrootd.cfg").write_text(
        f"all.export /\nall.adminpath {base}\nall.pidpath {base}\noss.localroot {data}\n"
    )
    proc = subprocess.Popen(
        ["xrootd", "-c", str(base / "xrootd.cfg"), "-p", str(port), "-l", str(base / "log")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _await_port(port)
        yield f"root://127.0.0.1:{port}", data
    finally:
        proc.terminate()
        proc.wait(10)
        shutil.rmtree(base, ignore_errors=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _await_port(port: int) -> None:
    for _ in range(200):
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.2):
            return
        time.sleep(0.05)
    raise RuntimeError(f"xrootd did not come up on {port}")


class Relay:
    """A TCP relay that delays every chunk by half a round trip, each way.

    Order is kept and bandwidth is not limited: the relay models latency and
    nothing else, so a client that keeps many requests in flight gains what it
    would gain on a real network.
    """

    def __init__(self, upstream: int, rtt_ms: float) -> None:
        self.upstream, self.delay = upstream, rtt_ms / 2000.0
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            upstream = socket.create_connection(("127.0.0.1", self.upstream))
            for a, b in ((client, upstream), (upstream, client)):
                a.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        import queue

        pending: queue.Queue[tuple[float, bytes]] = queue.Queue()

        def sender() -> None:
            while True:
                due, data = pending.get()
                if not data:
                    with contextlib.suppress(OSError):
                        dst.shutdown(socket.SHUT_WR)
                    return
                wait = due - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                try:
                    dst.sendall(data)
                except OSError:
                    return

        threading.Thread(target=sender, daemon=True).start()
        while True:
            try:
                data = src.recv(1 << 20)
            except OSError:
                data = b""
            pending.put((time.monotonic() + self.delay, data))
            if not data:
                return

    def close(self) -> None:
        self.listener.close()


# ---------------------------------------------------------------------------
# Cases: one function per contender, each returning bytes moved (0 for latency)
# ---------------------------------------------------------------------------


@dataclass
class Case:
    """One thing measured, done by each contender its own idiomatic way."""

    name: str
    kind: str  # "latency" (per operation) or "throughput" (bytes per second)
    contenders: dict[str, Callable[[], int]] = field(default_factory=dict)
    #: For a latency case, how many operations one timed run performs.
    ops: int = 1


def _ok(pair: tuple[object, object]) -> object:
    status, response = pair
    if not status.ok:  # type: ignore[attr-defined]
        raise RuntimeError(status.message)  # type: ignore[attr-defined]
    return response


def _repeat(fn: Callable[[], object], count: int) -> int:
    """Call ``fn`` ``count`` times: a latency case, which moves no bytes."""
    for _ in range(count):
        fn()
    return 0


def _repeat_ok(fn: Callable[[], object], count: int) -> int:
    """``_repeat`` for the bindings, whose calls answer ``(status, response)``."""
    for _ in range(count):
        _ok(fn())  # type: ignore[arg-type]
    return 0


def _case(name: str, kind: str, ops: int = 1, **fns: Callable[[], int]) -> Case:
    """A case; the ``official`` contender is dropped when the bindings are absent."""
    if official is None:
        fns.pop("official", None)
    return Case(name, kind, fns, ops)


def cases(url: str, size: int) -> list[Case]:
    """Every case, bound to the server at ``url``."""
    return [*_latency_cases(url), *_read_cases(url, size), *_write_cases(url, size)]


def _latency_cases(url: str) -> list[Case]:
    """One operation at a time, ``ops`` of them per timed run."""
    small, many = f"{url}//bench/small.bin", "/bench/many"
    fs = xrdclient.FileSystem(url, CONFIG)
    cfs = compat.FileSystem(url)
    ofs = official.FileSystem(url) if official else None
    n, m = 200, 50
    return [
        _case(
            "ping",
            "latency",
            n,
            xrdclient=lambda: _repeat(fs.ping, n),
            compat=lambda: _repeat_ok(cfs.ping, n),
            official=lambda: _repeat_ok(ofs.ping, n),
        ),
        _case(
            "stat",
            "latency",
            n,
            xrdclient=lambda: _repeat(lambda: fs.stat("/bench/small.bin"), n),
            compat=lambda: _repeat_ok(lambda: cfs.stat("/bench/small.bin"), n),
            official=lambda: _repeat_ok(lambda: ofs.stat("/bench/small.bin"), n),
        ),
        _case(
            "open+close",
            "latency",
            m,
            xrdclient=lambda: _repeat(lambda: _open_close_native(small), m),
            compat=lambda: _repeat(lambda: _open_close_bindings(compat, small), m),
            official=lambda: _repeat(lambda: _open_close_bindings(official, small), m),
        ),
        _case(
            "4KiB read on open file",
            "latency",
            n,
            xrdclient=lambda: _reads_native(small, 4096, n),
            compat=lambda: _reads_bindings(compat, small, 4096, n),
            official=lambda: _reads_bindings(official, small, 4096, n),
        ),
        _case(
            "dirlist 1000 + stat",
            "latency",
            5,
            xrdclient=lambda: _repeat(lambda: fs.scandir(many), 5),
            compat=lambda: _repeat_ok(lambda: cfs.dirlist(many, 1), 5),
            official=lambda: _repeat_ok(lambda: ofs.dirlist(many, 1), 5),
        ),
    ]


def _read_cases(url: str, size: int) -> list[Case]:
    """Moving the big file's bytes to the client, MiB/s."""
    big = f"{url}//bench/big.bin"
    out = [
        _case(
            "read whole file",
            "throughput",
            xrdclient=lambda: _read_all_native(big),
            compat=lambda: _read_all_bindings(compat, big),
            official=lambda: _read_all_bindings(official, big),
        )
    ]
    for chunk in (64 << 10, 1 * MiB):
        count = min(size // chunk, 512)
        out.append(
            _case(
                f"{count} x {chunk >> 10}KiB reads",
                "throughput",
                xrdclient=lambda c=chunk, k=count: _reads_native(big, c, k),
                compat=lambda c=chunk, k=count: _reads_bindings(compat, big, c, k),
                official=lambda c=chunk, k=count: _reads_bindings(official, big, c, k),
            )
        )
    ranges = [(i * (size // 1024), 4096) for i in range(1024)]
    out.append(
        _case(
            "readv 1024 x 4KiB",
            "throughput",
            xrdclient=lambda: _readv_native(big, ranges),
            compat=lambda: _readv_bindings(compat, big, ranges),
            official=lambda: _readv_bindings(official, big, ranges),
        )
    )
    return out


def _write_cases(url: str, size: int) -> list[Case]:
    """Moving bytes to the server, and a whole-file copy back, MiB/s."""
    big = f"{url}//bench/big.bin"
    payload = os.urandom(size)
    scratch = Path(tempfile.mkdtemp(prefix="xrdcmp-local"))
    return [
        _case(
            "write whole file",
            "throughput",
            xrdclient=lambda: _write_native(f"{url}//bench/w-native.bin", payload),
            compat=lambda: _write_bindings(compat, f"{url}//bench/w-compat.bin", payload),
            official=lambda: _write_bindings(official, f"{url}//bench/w-official.bin", payload),
        ),
        _case(
            "write 1MiB chunks",
            "throughput",
            xrdclient=lambda: _write_stream_native(f"{url}//bench/s-native.bin", payload),
            compat=lambda: _write_chunks_bindings(compat, f"{url}//bench/s-compat.bin", payload),
            official=lambda: _write_chunks_bindings(
                official, f"{url}//bench/s-official.bin", payload
            ),
        ),
        _case(
            "copy download",
            "throughput",
            xrdclient=lambda: (
                xrdclient.copy(big, str(scratch / "n.bin"), config=CONFIG, verify=False).size
            ),
            compat=lambda: _copy_bindings(compat, big, str(scratch / "c.bin"), size),
            official=lambda: _copy_bindings(official, big, str(scratch / "o.bin"), size),
        ),
    ]


def _open_close_native(url: str) -> None:
    handle = File(xrdclient.parse(url), CONFIG)
    handle.open()
    handle.close()


def _open_close_bindings(module: object, url: str) -> None:
    handle = module.File()  # type: ignore[attr-defined]
    _ok(handle.open(url))
    handle.close()


def _reads_native(url: str, chunk: int, count: int) -> int:
    with File(xrdclient.parse(url), CONFIG) as handle:  # ``with`` opens it for reading
        return sum(len(handle.read(chunk, (i * chunk) % max(handle.size, 1))) for i in range(count))


def _reads_bindings(module: object, url: str, chunk: int, count: int) -> int:
    handle = module.File()  # type: ignore[attr-defined]
    _ok(handle.open(url))
    try:
        size = _ok(handle.stat()).size  # type: ignore[attr-defined]
        return sum(len(_ok(handle.read((i * chunk) % max(size, 1), chunk))) for i in range(count))  # type: ignore[arg-type]
    finally:
        handle.close()


def _read_all_native(url: str) -> int:
    with xrdclient.open(url, "rb", config=CONFIG) as fh:
        return len(fh.read())


def _read_all_bindings(module: object, url: str) -> int:
    handle = module.File()  # type: ignore[attr-defined]
    _ok(handle.open(url))
    try:
        return len(_ok(handle.read()))  # type: ignore[arg-type]
    finally:
        handle.close()


def _readv_native(url: str, ranges: list[tuple[int, int]]) -> int:
    with File(xrdclient.parse(url), CONFIG) as handle:
        return sum(len(piece) for piece in handle.readv(ranges))


def _readv_bindings(module: object, url: str, ranges: list[tuple[int, int]]) -> int:
    handle = module.File()  # type: ignore[attr-defined]
    _ok(handle.open(url))
    try:
        return int(_ok(handle.vector_read(ranges)).size)  # type: ignore[attr-defined]
    finally:
        handle.close()


def _write_native(url: str, payload: bytes) -> int:
    handle = File(xrdclient.parse(url), CONFIG)
    handle.open("delete update")
    try:
        handle.write(payload, 0)
    finally:
        handle.close()
    return len(payload)


def _write_stream_native(url: str, payload: bytes) -> int:
    with xrdclient.open(url, "wb", config=CONFIG) as fh:
        view = memoryview(payload)
        for offset in range(0, len(payload), MiB):
            fh.write(view[offset : offset + MiB])
    return len(payload)


def _write_bindings(module: object, url: str, payload: bytes) -> int:
    flags = module.flags.OpenFlags  # type: ignore[attr-defined]
    handle = module.File()  # type: ignore[attr-defined]
    _ok(handle.open(url, flags.DELETE | flags.UPDATE))
    try:
        _ok(handle.write(payload))
    finally:
        handle.close()
    return len(payload)


def _write_chunks_bindings(module: object, url: str, payload: bytes) -> int:
    flags = module.flags.OpenFlags  # type: ignore[attr-defined]
    handle = module.File()  # type: ignore[attr-defined]
    _ok(handle.open(url, flags.DELETE | flags.UPDATE))
    try:
        for offset in range(0, len(payload), MiB):
            _ok(handle.write(payload[offset : offset + MiB], offset))
    finally:
        handle.close()
    return len(payload)


def _copy_bindings(module: object, source: str, target: str, size: int) -> int:
    process = module.CopyProcess()  # type: ignore[attr-defined]
    process.add_job(source, target, force=True)
    _ok((process.prepare(), None))
    status, _ = process.run()
    _ok((status, None))
    return size


# ---------------------------------------------------------------------------
# Measuring, and deciding
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    """What one case came to: per-contender samples and the verdict."""

    case: Case
    samples: dict[str, list[float]]

    def median(self, who: str) -> float:
        return statistics.median(self.samples[who])

    def rate(self, who: str, moved: int) -> str:
        seconds = self.median(who)
        if self.case.kind == "latency":
            return f"{seconds / self.case.ops * 1e6:9.1f} us/op"
        return f"{moved / seconds / MiB:9.1f} MiB/s"

    def wins(self, who: str, alpha: float) -> tuple[bool, float]:
        """Whether ``who`` beats ``official``: a lower median and a sign test."""
        ours, theirs = self.samples[who], self.samples["official"]
        better = sum(a < b for a, b in zip(ours, theirs))
        p = _sign_test(better, len(ours))
        return self.median(who) < self.median("official") and p <= alpha, p


def _sign_test(successes: int, trials: int) -> float:
    """One-sided p-value of ``successes`` or more wins in ``trials`` fair coin flips."""
    return sum(math.comb(trials, k) for k in range(successes, trials + 1)) / 2**trials


def measure(case: Case, rounds: int, moved: dict[str, int]) -> Outcome:
    names = list(case.contenders)
    samples: dict[str, list[float]] = {n: [] for n in names}
    for fn in case.contenders.values():  # warm every path once, untimed
        fn()
    for r in range(rounds):
        order = names[r % len(names) :] + names[: r % len(names)]
        for who in order:
            start = time.perf_counter()
            moved[who] = case.contenders[who]()
            samples[who].append(time.perf_counter() - start)
    return Outcome(case, samples)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--size", type=int, default=128, help="MiB in the big file (default 128)")
    parser.add_argument("--rounds", type=int, default=9, help="paired rounds per case (default 9)")
    parser.add_argument("--rtt", type=float, default=0.0, help="simulated round trip, ms")
    parser.add_argument("--alpha", type=float, default=0.05, help="sign-test threshold")
    parser.add_argument("--only", default="", help="comma-separated case names to run")
    parser.add_argument("--gate", action="store_true", help="exit 1 unless every case is won")
    parser.add_argument("--json", type=Path, help="write the raw samples here")
    return parser.parse_args(argv)


@contextlib.contextmanager
def _target(size: int, rtt: float) -> Iterator[str]:
    """A server's URL - behind a relay adding ``rtt`` ms of round trip when that is not zero."""
    with server(size) as (url, _data):
        relay = Relay(int(url.rsplit(":", 1)[1]), rtt) if rtt else None
        yield f"root://127.0.0.1:{relay.port}" if relay else url
        if relay:
            relay.close()


def _selected(every: list[Case], only: str) -> list[Case]:
    """The cases ``--only`` names, or all of them when it is empty."""
    wanted = {n.strip() for n in only.split(",") if n.strip()}
    return [case for case in every if not wanted or case.name in wanted]


def _verdicts(case: Case, outcome: Outcome, alpha: float) -> tuple[list[str], list[str]]:
    """Each of ours against the official bindings: the verdict texts, and what was not won."""
    verdicts, lost = [], []
    for who in ("xrdclient", "compat"):
        won, p = outcome.wins(who, alpha)
        ratio = outcome.median("official") / outcome.median(who)
        verdicts.append(f"{who} {ratio:.2f}x{'' if won else ' LOST'} (p={p:.3f})")
        if not won:
            lost.append(f"{case.name}: {who}")
    return verdicts, lost


def _run_case(case: Case, rounds: int, alpha: float, lost: list[str]) -> dict[str, object]:
    """Measure one case and print its row; what was not won goes on ``lost``."""
    moved: dict[str, int] = {}
    outcome = measure(case, rounds, moved)
    cells = "".join(f"{outcome.rate(w, moved[w]):>16}" for w in ("xrdclient", "compat", "official"))
    verdicts, not_won = _verdicts(case, outcome, alpha)
    lost.extend(not_won)
    print(f"  {case.name:<24}{cells}   {'; '.join(verdicts)}")
    return {
        "case": case.name,
        "kind": case.kind,
        "ops": case.ops,
        "moved": moved,
        "samples": outcome.samples,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if official is None:
        sys.exit("the official XRootD bindings are not installed: nothing to compare with")

    size = args.size * MiB
    lost: list[str] = []
    report: list[dict[str, object]] = []
    with _target(size, args.rtt) as target:
        print(f"{target}  big file {args.size} MiB  rtt {args.rtt} ms  rounds {args.rounds}\n")
        print(f"  {'case':<24}{'xrdclient':>16}{'compat':>16}{'official':>16}   verdict")
        for case in _selected(cases(target, size), args.only):
            report.append(_run_case(case, args.rounds, args.alpha, lost))
    if args.json:
        args.json.write_text(json.dumps({"rtt_ms": args.rtt, "cases": report}, indent=1))
    if lost:
        print("\nnot won:\n  " + "\n  ".join(lost))
    else:
        print("\nevery case won by both xrdclient and compat")
    return 1 if (args.gate and lost) else 0


if __name__ == "__main__":
    raise SystemExit(main())
