"""The benchmark harness, without the benchmarks.

``benchmarks/bench.py`` needs a daemon and several minutes to say anything
useful, which is not a unit test. What is testable is the arithmetic it
reports with, and that is worth pinning: a harness that quietly reports the
mean instead of the best, or divides by the wrong size, produces numbers that
look plausible and are wrong.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parent.parent / "benchmarks" / "bench.py"


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("xrd_bench", BENCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_url_gets_the_doubled_slash_a_server_expects(bench):
    assert bench.full_url("root://h:1094/", "/store/f.root") == "root://h:1094//store/f.root"
    assert bench.full_url("root://h:1094", "store/f.root") == "root://h:1094//store/f.root"


def test_the_best_run_is_the_one_reported(bench):
    timer = bench.Timer(repeat=3)
    delays = iter([0.03, 0.01, 0.03])

    def work():
        import time

        end = time.perf_counter() + next(delays)
        while time.perf_counter() < end:
            pass
        return 1 << 20

    timer.run("case", "xrd", work)
    row = timer.rows[0]
    assert row["seconds"] < row["median"]
    assert row["bytes"] == 1 << 20
    assert row["rate_mib_s"] == pytest.approx(1 / row["seconds"], rel=0.01)
    assert row["ops_s"] is None


def test_a_case_that_moves_nothing_is_reported_in_operations(bench):
    timer = bench.Timer(repeat=1)
    timer.run("stat", "xrd", lambda: 0)
    row = timer.rows[0]
    assert row["rate_mib_s"] is None and row["ops_s"] > 0


def test_the_summary_ranks_each_case_against_its_own_best(bench, capsys):
    timer = bench.Timer(repeat=1)
    timer.rows = [
        {"case": "read", "client": "xrd", "seconds": 2.0},
        {"case": "read", "client": "bindings", "seconds": 1.0},
        {"case": "stat", "client": "xrd", "seconds": 0.5},
    ]
    timer.report()
    out = capsys.readouterr().out
    assert "xrd=2.00x bindings=1.00x" in out
    assert "stat" in out and "xrd=1.00x" in out.split("stat")[1]
