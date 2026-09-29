#!/usr/bin/env python3
"""Run every example here twice - on the official bindings and on the compat layer.

Each example in this directory is written the way a PyXRootD user writes code,
with one line changed::

    from xrdclient.compat import client   # was: from XRootD import client

This runner proves the change is the whole port. It starts a stock ``xrootd``
exporting a throwaway sandbox, then runs each example

* once with that line turned back into the original import, so it runs on the
  official bindings (skipped, cleanly, where ``XRootD`` is not importable), and
* once exactly as written, on ``xrdclient.compat``,

and compares what the two printed, after masking what legitimately differs
between runs: the port, temporary paths, timestamps. The result is a table::

    example              official  compat  same output
    stat_and_ping.py     ok        ok      yes

It exits non-zero if any compat run fails, or prints something the official
run did not. ``--compat-only`` skips the official runs, for a machine without
the bindings.

    python examples/pyxrootd/run_all.py [--compat-only] [-k NAME] [-j JOBS]
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

#: The ported import, and the one it replaced. ``from xrdclient.compat.client.flags
#: import ...`` and the other submodules port by the same substitution.
COMPAT_IMPORT = "from xrdclient.compat import client"
OFFICIAL_IMPORT = "from XRootD import client"

#: A line an example needs only on the compat layer - ``install_hook.py``'s
#: ``xrdclient.compat.install()`` - says so, and the official variant drops it.
COMPAT_ONLY = "# compat only"

#: Enough of a storage element for every example: one export rooted in the
#: sandbox, checksums on (so ``QueryCode.CHECKSUM`` answers), and a short
#: admin path because a Unix socket path is limited to ~104 bytes.
SERVER_CONFIG = """\
all.export /
all.adminpath {admin}
all.pidpath {admin}
oss.localroot {data}
xrootd.chksum max 2 adler32 crc32
"""


def examples() -> list[Path]:
    """Every example script, in name order."""
    return sorted(p for p in HERE.glob("*.py") if p.name != Path(__file__).name)


def to_official(source: str) -> str:
    """The example as it was before the port: the original import back in place.

    Only import lines change. ``from xrdclient.compat import client`` becomes
    ``from XRootD import client``, ``from xrdclient.compat.client.X import``
    becomes ``from XRootD.client.X import``, the ``# was:`` note goes, and a
    line marked ``# compat only`` is dropped.
    """
    out = []
    for line in source.splitlines(keepends=True):
        if line.rstrip().endswith(COMPAT_ONLY):
            continue
        stripped = line.lstrip()
        if stripped.startswith(("from xrdclient.compat", "import xrdclient.compat")):
            code = line.split("#", 1)[0].rstrip()
            code = code.replace(COMPAT_IMPORT, OFFICIAL_IMPORT)
            code = code.replace("from xrdclient.compat.client", "from XRootD.client")
            line = code + "\n"
        out.append(line)
    return "".join(out)


# -- the server ---------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _await_port(port: int, proc: subprocess.Popen[bytes], log: Path, wait: float) -> None:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.5):
            return
        time.sleep(0.1)
    tail = log.read_text(errors="replace")[-2000:] if log.exists() else ""
    raise RuntimeError(f"xrootd did not come up on port {port}:\n{tail}")


@contextlib.contextmanager
def server(xrootd: str) -> Iterator[tuple[str, Path]]:
    """A stock xrootd exporting '/' from a fresh sandbox: ``(url, local root)``."""
    # Short, under /tmp: the admin socket lives here.
    base = Path(tempfile.mkdtemp(prefix="xrdex", dir="/tmp")).resolve()
    data = base / "data"
    data.mkdir()
    port = _free_port()
    cfg, log = base / "xrootd.cfg", base / "xrootd.log"
    cfg.write_text(SERVER_CONFIG.format(admin=base, data=data))
    proc = subprocess.Popen(
        [xrootd, "-c", str(cfg), "-p", str(port), "-l", str(log), "-n", "ex"],
        cwd=base,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _await_port(port, proc, log, 180.0)
        yield f"root://127.0.0.1:{port}", data
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)
        shutil.rmtree(base, ignore_errors=True)


# -- running and comparing ----------------------------------------------------


@dataclass
class Run:
    """One example on one client: how it ended and what it printed."""

    state: str  # "ok", "FAIL", "skipped"
    stdout: str = ""
    stderr: str = ""
    note: str = ""


@dataclass
class Result:
    """Both runs of one example."""

    name: str
    official: Run
    compat: Run
    diff: list[str] = field(default_factory=list)

    @property
    def same(self) -> str:
        if self.official.state == "skipped" or "FAIL" in (self.official.state, self.compat.state):
            return "-"
        return "yes" if not self.diff else "NO"

    @property
    def bad(self) -> bool:
        return self.compat.state != "ok" or self.same == "NO"


def normalise(text: str, url: str, local: Path) -> str:
    """Mask what differs between two correct runs: port, temp paths, clocks."""
    port = url.rsplit(":", 1)[1]
    text = text.replace(url, "root://HOST")
    text = re.sub(rf"(\[[^\]\s]*\]|[\w.-]+):{port}\b", "HOST", text)
    text = text.replace(str(local), "LOCALROOT")
    text = re.sub(r"(/private)?(/var/folders|/tmp)/[^\s'\"),\]]*", "TMPPATH", text)
    text = re.sub(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", "TIME", text)
    return re.sub(r"\b1[6-9]\d{8}\b", "EPOCH", text)


def run_one(python: str, script: Path, url: str, work: str, local: Path, timeout: float) -> Run:
    """Run a script against the server, from a scratch working directory."""
    leftover = local / work.lstrip("/")
    shutil.rmtree(leftover, ignore_errors=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO / "src"), env.get("PYTHONPATH")]))
    env["PYTHONUNBUFFERED"] = "1"
    with tempfile.TemporaryDirectory(prefix="xrdex-run") as cwd:
        try:
            proc = subprocess.run(
                [python, str(script), url, work],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""
            return Run("FAIL", out, "", f"timed out after {timeout:.0f}s")
    out, err = normalise(proc.stdout, url, local), normalise(proc.stderr, url, local)
    if proc.returncode != 0:
        return Run("FAIL", out, err, f"exit status {proc.returncode}")
    if leftover.exists():
        shutil.rmtree(leftover, ignore_errors=True)
        return Run("FAIL", out, err, f"left {work} behind on the server")
    return Run("ok", out, err)


def compare(
    script: Path,
    python: str,
    url: str,
    local: Path,
    official: bool,
    timeout: float,
    scratch: Path,
) -> Result:
    """Both runs of one example, one after the other in the same directory."""
    work = f"/pyxrootd-examples/{script.stem}"
    if official:
        copy = scratch / script.name
        copy.write_text(to_official(script.read_text()))
        theirs = run_one(python, copy, url, work, local, timeout)
    else:
        theirs = Run("skipped")
    ours = run_one(python, script, url, work, local, timeout)
    result = Result(script.name, theirs, ours)
    if theirs.state == "ok" and ours.state == "ok" and theirs.stdout != ours.stdout:
        result.diff = list(
            difflib.unified_diff(
                theirs.stdout.splitlines(),
                ours.stdout.splitlines(),
                "official",
                "compat",
                lineterm="",
            )
        )
    return result


def have_official(python: str) -> bool:
    """Whether ``python`` can import the official bindings."""
    probe = subprocess.run([python, "-c", "import XRootD.client"], capture_output=True, timeout=120)
    return probe.returncode == 0


def table(results: list[Result]) -> str:
    """The summary: one row per example."""
    width = max([len("example"), *(len(r.name) for r in results)])
    rows = [f"{'example':<{width}}  {'official':<8}  {'compat':<6}  same output"]
    rows.append("-" * len(rows[0]))
    rows += [
        f"{r.name:<{width}}  {r.official.state:<8}  {r.compat.state:<6}  {r.same}" for r in results
    ]
    return "\n".join(rows)


def report(results: list[Result]) -> None:
    """What went wrong, in enough detail to act on."""
    for r in results:
        for side, run in (("official", r.official), ("compat", r.compat)):
            if run.state == "FAIL":
                print(f"\n== {r.name} ({side}): {run.note}")
                print(run.stdout.rstrip())
                print(run.stderr.rstrip()[-3000:])
        if r.diff:
            print(f"\n== {r.name}: the output differs")
            print("\n".join(r.diff))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--compat-only", action="store_true", help="skip the official runs")
    parser.add_argument("-k", dest="only", action="append", help="only examples matching this")
    parser.add_argument("-j", dest="jobs", type=int, default=4, help="examples run at once")
    parser.add_argument("--python", default=sys.executable, help="interpreter to run them with")
    parser.add_argument("--timeout", type=float, default=300.0, help="per run, in seconds")
    parser.add_argument("-v", "--verbose", action="store_true", help="print each run's output")
    args = parser.parse_args(argv)

    xrootd = shutil.which("xrootd")
    if xrootd is None:
        print("run_all: no xrootd binary on PATH", file=sys.stderr)
        return 2
    scripts = [s for s in examples() if not args.only or any(k in s.name for k in args.only)]
    official = not args.compat_only and have_official(args.python)
    if not args.compat_only and not official:
        print("run_all: the official XRootD bindings are not importable; skipping those runs")

    with server(xrootd) as (url, local), tempfile.TemporaryDirectory() as scratch:
        with ThreadPoolExecutor(max(args.jobs, 1)) as pool:
            results = list(
                pool.map(
                    lambda s: compare(
                        s, args.python, url, local, official, args.timeout, Path(scratch)
                    ),
                    scripts,
                )
            )
    if args.verbose:
        for r in results:
            print(f"\n== {r.name}\n{r.compat.stdout.rstrip()}")
    report(results)
    print()
    print(table(results))
    failed = [r.name for r in results if r.bad]
    if failed:
        print(f"\n{len(failed)} of {len(results)} failed or differ: {', '.join(failed)}")
        return 1
    ran = "official and compat" if official else "compat only"
    print(f"\nall {len(results)} examples passed ({ran})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
