#!/usr/bin/env python3
"""Run XRootD's own Python binding tests against ``xrdclient.compat``.

The strongest evidence that the compat layer is a drop-in is the upstream
suite for the bindings passing on it unchanged, apart from the imports. This
tool makes that check repeatable:

1. copy ``python/tests`` from an XRootD source checkout twice into a
   temporary directory;
2. in one copy, rewrite only the import lines that name ``XRootD`` so they
   name ``xrdclient.compat`` instead (``from XRootD import client`` becomes
   ``from xrdclient.compat import client``, ``XRootD.client.flags`` becomes
   ``xrdclient.compat.client.flags``), printing every line it changes;
3. run pytest on both copies - the untouched one on the official bindings,
   the rewritten one on this library - each against the stock ``xrootd``
   the suite's own ``conftest.py`` starts;
4. compare the outcome of every test.

It exits non-zero if any test that passes on the official bindings does not
pass on the compat layer. Tests that fail on both (the suite may be newer
than the installed bindings) are reported but are not held against it, and
tests that pass on the compat layer but not on the bindings - a test file
for API newer than the installed release will not even import there - are
reported as improvements.

    python tools/pyxrootd_upstream.py [~/src/dev/xrootd] [--python PY] [-k EXPR]

It needs ``xrootd`` and ``xrdfs`` on ``PATH`` (the upstream ``conftest.py``
starts the one and polls it with the other), the official bindings
importable, and ``pytest``.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from xrdcore import safexml

#: This checkout; its ``src`` goes first on the runs' ``PYTHONPATH`` so the
#: compat copy tests this tree, not an installed xrdclient.
REPO = Path(__file__).resolve().parent.parent

#: An import statement that names the official package: ``from XRootD ...``,
#: ``import XRootD.client...``. Anything else that mentions ``XRootD`` - a
#: docstring, a string, an attribute - is left exactly as it is.
IMPORT_LINE = re.compile(r"^(\s*)(from|import)\s+XRootD(?=[.\s]|$)")


def rewrite_line(line: str) -> str:
    """One line, with an ``XRootD`` import pointed at ``xrdclient.compat``."""
    match = IMPORT_LINE.match(line)
    if match is None:
        return line
    indent, keyword = match.group(1), match.group(2)
    rest = line[match.end() :]
    if keyword == "import" and not rest.startswith("."):
        # A bare ``import XRootD`` must keep binding the name ``XRootD``.
        return f"{indent}import xrdclient.compat as XRootD{rest}"
    return f"{indent}{keyword} xrdclient.compat{rest}"


def rewrite_tree(root: Path) -> list[tuple[str, int, str, str]]:
    """Rewrite every ``.py`` file under ``root``; return what changed.

    Each change is ``(relative path, line number, old, new)``, printed by
    :func:`run_both` so a reviewer can confirm that only imports moved -
    the whole value of the run rests on the tests themselves being upstream's.
    Files are only rewritten when something changed, and line endings are
    kept (``keepends``) so nothing else in a file differs.
    """
    changes = []
    for path in sorted(root.rglob("*.py")):
        lines = path.read_text().splitlines(keepends=True)
        new = [rewrite_line(line) for line in lines]
        for number, (old, line) in enumerate(zip(lines, new, strict=False), 1):
            if old != line:
                changes.append((str(path.relative_to(root)), number, old.rstrip(), line.rstrip()))
        if new != lines:
            path.write_text("".join(new))
    return changes


def run_pytest(python: str, tests: Path, junit: Path, extra: list[str], timeout: float) -> str:
    """Run the suite in ``tests``; return pytest's last summary line.

    ``-o addopts=`` drops any options the upstream suite or this repo's
    configuration would add (coverage, xdist), so both runs are identical
    except for the code under test. ``--continue-on-collection-errors``
    matters: a test module importing API the installed bindings lack must
    not stop the rest of the official run. A non-zero pytest exit is
    expected (failing tests) and is read from the JUnit report, not raised;
    only ``timeout`` aborts, via :class:`subprocess.TimeoutExpired`.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO / "src"), env.get("PYTHONPATH")]))
    command = [
        python,
        "-m",
        "pytest",
        "-o",
        "addopts=",
        "-p",
        "no:cacheprovider",
        "--continue-on-collection-errors",
        f"--junitxml={junit}",
        "-q",
        *extra,
        str(tests),
    ]
    proc = subprocess.run(
        command, cwd=tests, env=env, capture_output=True, text=True, timeout=timeout
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    return lines[-1].strip("= ") if lines else f"pytest exited {proc.returncode}: {proc.stderr}"


def outcomes(junit: Path) -> dict[str, str]:
    """``{test id: passed|failed|error|skipped}`` from a JUnit XML report.

    The id is ``classname::name``, which is identical across the two copies
    because they share a directory layout. A missing report (pytest crashed
    before writing it) yields no outcomes, which :func:`main` treats as a
    failed comparison rather than a clean pass.
    """
    result: dict[str, str] = {}
    if not junit.exists():
        return result
    for case in safexml.fromstring(junit.read_bytes()).iter("testcase"):
        name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
        state = "passed"
        for child in case:
            if child.tag in ("failure", "error", "skipped"):
                state = {"failure": "failed"}.get(child.tag, child.tag)
        result[name] = state
    return result


def improvements(theirs: dict[str, str], ours: dict[str, str]) -> list[str]:
    """Tests that pass on compat and not on the bindings, or not there at all.

    A module the bindings cannot import is one ``error`` entry named after
    the file, where compat has each of its tests; those count here too.
    """
    return sorted(n for n, state in ours.items() if state == "passed" and theirs.get(n) != "passed")


def uncollected(theirs: dict[str, str], ours: dict[str, str]) -> list[str]:
    """Modules that erred as a whole on the bindings but whose tests ran on compat."""
    modules = {n for n, state in theirs.items() if state == "error" and n not in ours}
    return sorted(m for m in modules if any(n.startswith(f"{m}::") for n in ours))


# Both copies live under one scratch directory with the same relative layout
# (``*/tests``), so pytest computes the same test ids for each run.
def run_both(
    upstream: Path, python: str, extra: list[str], timeout: float
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Copy the suite twice, rewrite one copy's imports, run both; summaries and outcomes."""
    with tempfile.TemporaryDirectory(prefix="pyxrootd-upstream-") as scratch:
        base = Path(scratch)
        official, compat = base / "official" / "tests", base / "compat" / "tests"
        ignore = shutil.ignore_patterns("__pycache__", ".pytest_cache")
        shutil.copytree(upstream, official, ignore=ignore)
        shutil.copytree(upstream, compat, ignore=ignore)

        changes = rewrite_tree(compat)
        print(f"rewrote {len(changes)} import line(s) in the compat copy:")
        for name, number, old, new in changes:
            print(f"  {name}:{number}")
            print(f"    - {old}")
            print(f"    + {new}")

        summaries, results = {}, {}
        for label, tests in (("official", official), ("compat", compat)):
            junit = base / f"{label}.xml"
            print(f"\nrunning the suite on {label} ...", flush=True)
            summaries[label] = run_pytest(python, tests, junit, extra, timeout)
            results[label] = outcomes(junit)
            print(f"  {label}: {summaries[label]}")
    return summaries, results


def print_differences(theirs: dict[str, str], ours: dict[str, str]) -> None:
    """Every test whose outcome differs between the two runs."""
    differ = sorted(
        name for name in theirs.keys() | ours.keys() if theirs.get(name) != ours.get(name)
    )
    if not differ:
        print(f"\nevery one of {len(theirs)} outcomes is the same on both")
        return
    print("\nper-test differences (official -> compat):")
    for name in differ:
        print(f"  {name}: {theirs.get(name, 'absent')} -> {ours.get(name, 'absent')}")


def print_both_bad(theirs: dict[str, str], ours: dict[str, str], modules: list[str]) -> None:
    """Tests that pass on neither side - not held against compat."""
    both_bad = sorted(
        name
        for name, state in theirs.items()
        if state in ("failed", "error") and ours.get(name) != "passed" and name not in modules
    )
    if both_bad:
        print("not passing on the official bindings either (not held against compat):")
        for name in both_bad:
            print(f"  {name}: {theirs[name]}")


def print_classified(theirs: dict[str, str], ours: dict[str, str]) -> list[str]:
    """Report what fails on both, and what passes on compat only; return the latter."""
    better, modules = improvements(theirs, ours), uncollected(theirs, ours)
    print_both_bad(theirs, ours, modules)
    if better:
        print("improvements - passing on compat only (API newer than the bindings):")
        for name in modules:
            print(f"  {name}: cannot be collected on the official bindings")
        for name in better:
            print(f"  {name}: {theirs.get(name, 'absent')} -> passed")
    return better


def main(argv: list[str] | None = None) -> int:
    """Run both suites and compare; 0 only if nothing regressed and tests ran.

    Exit 2 means the source checkout has no ``python/tests``; exit 1 means at
    least one test passing on the official bindings did not pass on compat,
    or that the official run produced no outcomes at all (so there was
    nothing to compare against).
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "source",
        nargs="?",
        default=os.path.expanduser("~/src/dev/xrootd"),
        help="an XRootD source checkout (default ~/src/dev/xrootd)",
    )
    parser.add_argument("--python", default=sys.executable, help="interpreter to run pytest with")
    parser.add_argument("-k", dest="select", help="pytest -k expression, passed to both runs")
    parser.add_argument("--timeout", type=float, default=1800.0, help="per run, in seconds")
    args = parser.parse_args(argv)

    upstream = Path(args.source).expanduser() / "python" / "tests"
    if not upstream.is_dir():
        print(f"pyxrootd_upstream: no python/tests in {args.source}", file=sys.stderr)
        return 2
    extra = ["-k", args.select] if args.select else []

    summaries, results = run_both(upstream, args.python, extra, args.timeout)

    theirs, ours = results["official"], results["compat"]
    print_differences(theirs, ours)
    better = print_classified(theirs, ours)

    # A regression is judged against the official run only: compat-only
    # failures of tests that also fail upstream are reported, not counted.
    regressions = [
        n for n, state in theirs.items() if state == "passed" and ours.get(n) != "passed"
    ]
    print(
        f"\nsummary: official {summaries['official']} | compat {summaries['compat']}"
        f" | {len(regressions)} regression(s) | {len(better)} improvement(s)"
    )
    return 1 if regressions or not theirs else 0


if __name__ == "__main__":
    sys.exit(main())
