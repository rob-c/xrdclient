from __future__ import annotations

import ast
from pathlib import Path

from tools import maintainability as mt


def _function(source: str) -> ast.FunctionDef:
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def test_all_five_metrics_are_reported_per_function() -> None:
    source = """
def choices(x):
    if x > 0:
        for value in range(x):
            if value % 2:
                return value
    else:
        return -x
    return 0
"""
    (measured,) = mt.analyze_source(source, "sample.py")

    assert measured.key == "sample.py::choices"
    assert measured.ccn == 4
    assert measured.cognitive == 7
    assert measured.npath == 5
    assert measured.halstead_volume > 0
    assert measured.max_nesting == 3


def test_npath_multiplies_independent_branches() -> None:
    node = _function(
        """
def independent(a, b):
    if a:
        use(a)
    if b:
        use(b)
"""
    )

    assert mt.npath_complexity(node) == 4
    assert mt.max_nesting_depth(node) == 1


def test_elif_does_not_inflate_lexical_nesting() -> None:
    node = _function(
        """
def select(value):
    if value == 1:
        return "one"
    elif value == 2:
        return "two"
    else:
        return "other"
"""
    )

    assert mt.max_nesting_depth(node) == 1
    assert mt.npath_complexity(node) == 3


def test_nested_functions_are_scored_separately() -> None:
    source = """
def outer(x):
    def inner(y):
        if y:
            return y + 1
        return 0
    return inner(x)
"""
    outer, inner = mt.analyze_source(source, "nested.py")

    assert outer.key == "nested.py::outer"
    assert outer.ccn == 1
    assert outer.cognitive == 0
    assert outer.npath == 1
    assert inner.key == "nested.py::outer.inner"
    assert inner.ccn == 2
    assert inner.cognitive == 1
    assert inner.npath == 2


def test_absolute_limits_apply_to_every_function() -> None:
    limits = {
        "ccn": 1,
        "cognitive": 0,
        "npath": 1,
        "halstead_volume": 1_000_000,
        "max_nesting": 1,
    }
    functions = mt.analyze_source("def f():\n    if ready():\n        return 1\n", "x.py")
    report = mt.Report((), functions)

    failures = mt.violations(report, limits)

    assert {failure.metric for failure in failures} == {"ccn", "cognitive", "npath"}
    assert {failure.limit for failure in failures} == {0, 1}
    assert mt.violation_counts(failures) == {
        "ccn": 1,
        "cognitive": 1,
        "npath": 1,
        "halstead_volume": 0,
        "max_nesting": 0,
    }


def test_hotspot_report_contains_only_functions_over_a_limit() -> None:
    functions = mt.analyze_source(
        "def good():\n    return 1\n\ndef bad(x):\n    if x:\n        return 1\n",
        "x.py",
    )
    limits = dict.fromkeys(mt.METRICS, 1_000_000)
    limits["ccn"] = 1
    report = mt.Report((), functions)

    rendered = mt.render_hotspots_table(report, limits)

    assert "x.py::bad" in rendered
    assert "x.py::good" not in rendered


def test_policy_file_discovery_honours_exclusions(tmp_path: Path) -> None:
    (tmp_path / "code").mkdir()
    included = tmp_path / "code" / "included.py"
    excluded = tmp_path / "code" / "generated.py"
    included.write_text("def included():\n    pass\n", encoding="utf-8")
    excluded.write_text("def generated():\n    pass\n", encoding="utf-8")
    policy = mt.Policy(
        root=tmp_path,
        paths=("code",),
        exclude=("code/generated.py",),
        limits=dict.fromkeys(mt.METRICS, 1),
    )

    assert list(mt.iter_python_files(policy)) == [included]


def test_project_policy_keeps_the_readability_limits_strict() -> None:
    root = Path(__file__).resolve().parents[1]
    policy = mt.load_policy(root / "maintainability.json")

    assert policy.limits == {
        "ccn": 10,
        "cognitive": 15,
        "npath": 200,
        "halstead_volume": 500,
        "max_nesting": 4,
    }


def test_project_has_no_functions_over_maintainability_limits() -> None:
    root = Path(__file__).resolve().parents[1]
    policy = mt.load_policy(root / "maintainability.json")
    report = mt.analyze(policy)
    failures = mt.violations(report, policy.limits)

    details = "\n".join(
        f"{failure.key}: {failure.metric}={failure.actual:g} > {failure.limit:g}"
        for failure in failures
    )
    hotspots = mt.hotspot_functions(report, policy.limits)
    counts = mt.violation_counts(failures)
    count_text = ", ".join(f"{metric}={count}" for metric, count in counts.items())
    assert not failures, (
        f"{len(hotspots)} functions exceed {len(failures)} maintainability limits; "
        f"burn both counts down to zero ({count_text}):\n"
        f"{details}\n"
        "Run `python tools/maintainability.py hotspots` for the ranked view."
    )
