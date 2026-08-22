#!/usr/bin/env python3
"""Measure and gate Python maintainability metrics.

Radon supplies cyclomatic complexity and Halstead Volume, Complexipy supplies
cognitive complexity, and the small AST visitors below make the NPath and
nesting definitions visible and testable.  Run ``python tools/maintainability.py
--help`` from the repository root for the human and machine-readable reports.
"""

from __future__ import annotations

import argparse
import ast
import copy
import csv
import fnmatch
import io
import json
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from complexipy import code_complexity
from radon.complexity import cc_visit_ast
from radon.metrics import h_visit_ast

METRICS = ("ccn", "cognitive", "npath", "halstead_volume", "max_nesting")
NPATH_SATURATION = 1_000_000_000_000


@dataclass(frozen=True)
class FunctionMetrics:
    key: str
    path: str
    name: str
    line: int
    end_line: int
    ccn: int
    cognitive: int
    npath: int
    halstead_volume: float
    max_nesting: int


@dataclass(frozen=True)
class FileMetrics:
    path: str
    lines: int
    functions: int
    ccn: int
    cognitive: int
    npath: int
    halstead_volume: float
    max_nesting: int


@dataclass(frozen=True)
class Report:
    files: tuple[FileMetrics, ...]
    functions: tuple[FunctionMetrics, ...]


@dataclass(frozen=True)
class Violation:
    key: str
    metric: str
    actual: float
    limit: float


@dataclass(frozen=True)
class FunctionNode:
    name: str
    node: ast.AST


@dataclass(frozen=True)
class Policy:
    root: Path
    paths: tuple[str, ...]
    exclude: tuple[str, ...]
    limits: Mapping[str, float]


def _saturating_add(left: int, right: int) -> int:
    return min(NPATH_SATURATION, left + right)


def _saturating_multiply(left: int, right: int) -> int:
    return min(NPATH_SATURATION, left * right)


def _product(values: Iterable[int]) -> int:
    result = 1
    for value in values:
        result = _saturating_multiply(result, value)
    return result


def _sum(values: Iterable[int]) -> int:
    result = 0
    for value in values:
        result = _saturating_add(result, value)
    return result


def _npath_expression(node: ast.AST | None) -> int:
    if node is None:
        return 1
    if isinstance(node, ast.BoolOp):
        # Short-circuiting can stop after any operand.
        return _sum(_npath_expression(value) for value in node.values)
    if isinstance(node, ast.IfExp):
        alternatives = _saturating_add(
            _npath_expression(node.body), _npath_expression(node.orelse)
        )
        return _saturating_multiply(_npath_expression(node.test), alternatives)
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        return _npath_comprehension(node)
    if isinstance(node, ast.Lambda):
        return 1
    return _product(_npath_expression(child) for child in ast.iter_child_nodes(node))


def _npath_comprehension(node: ast.AST) -> int:
    paths = 1
    generators = getattr(node, "generators", ())
    for generator in generators:
        branch = _saturating_add(1, _npath_expression(generator.iter))
        for condition in generator.ifs:
            branch = _saturating_multiply(branch, _npath_expression(condition) + 1)
        paths = _saturating_multiply(paths, branch)
    if isinstance(node, ast.DictComp):
        values = (node.key, node.value)
    else:
        values = (node.elt,)
    return _saturating_multiply(paths, _product(_npath_expression(value) for value in values))


def _npath_block(statements: Sequence[ast.stmt]) -> int:
    return _product(_npath_statement(statement) for statement in statements)


def _npath_if(node: ast.If) -> int:
    alternatives = _saturating_add(_npath_block(node.body), _npath_block(node.orelse))
    return _saturating_multiply(_npath_expression(node.test), alternatives)


def _npath_loop(node: Any) -> int:
    # The extra path is the loop terminating without another iteration.
    alternatives = _sum((_npath_block(node.body), _npath_block(node.orelse), 1))
    condition = getattr(node, "test", None) or getattr(node, "iter", None)
    return _saturating_multiply(_npath_expression(condition), alternatives)


def _npath_try(node: Any) -> int:
    success = _saturating_multiply(_npath_block(node.body), _npath_block(node.orelse))
    handled = _sum(_npath_block(handler.body) for handler in node.handlers)
    alternatives = _saturating_add(success, handled)
    return _saturating_multiply(alternatives, _npath_block(node.finalbody))


def _is_wildcard_case(case: Any) -> bool:
    pattern = case.pattern
    return isinstance(pattern, ast.MatchAs) and pattern.pattern is None and pattern.name is None


def _npath_match(node: Any) -> int:
    alternatives = _sum(_npath_block(case.body) for case in node.cases)
    if not any(_is_wildcard_case(case) for case in node.cases):
        alternatives = _saturating_add(alternatives, 1)
    guards = _product(_npath_expression(case.guard) for case in node.cases)
    return _product((_npath_expression(node.subject), guards, alternatives))


def _npath_statement(node: ast.stmt) -> int:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return 1
    if isinstance(node, ast.If):
        return _npath_if(node)
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        return _npath_loop(node)
    return _npath_other_statement(node)


def _npath_other_statement(node: ast.stmt) -> int:
    if isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
        return _npath_try(node)
    if hasattr(ast, "Match") and isinstance(node, ast.Match):
        return _npath_match(node)
    if isinstance(node, (ast.With, ast.AsyncWith)):
        contexts = _product(_npath_expression(item.context_expr) for item in node.items)
        return _saturating_multiply(contexts, _npath_block(node.body))
    return _product(_npath_expression(child) for child in ast.iter_child_nodes(node))


def npath_complexity(node: ast.AST) -> int:
    """Return structural execution paths for one function, excluding nested definitions."""

    return _npath_block(getattr(node, "body", ()))


_NESTING_BLOCKS = (
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.With,
    ast.AsyncWith,
)
if hasattr(ast, "TryStar"):
    _NESTING_BLOCKS += (ast.TryStar,)
if hasattr(ast, "Match"):
    _NESTING_BLOCKS += (ast.Match,)


def _max_children(nodes: Iterable[ast.AST], depth: int) -> int:
    return max((_nesting(child, depth) for child in nodes), default=depth)


def _nesting_if(node: ast.If, depth: int) -> int:
    current = depth + 1
    found = max(current, _nesting(node.test, current), _max_children(node.body, current))
    if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
        return max(found, _nesting_if(node.orelse[0], depth))
    return max(found, _max_children(node.orelse, current))


def _nesting_comprehension(node: ast.AST, depth: int) -> int:
    found = depth
    for generator in getattr(node, "generators", ()):
        depth += 1
        found = max(found, depth, _nesting(generator.iter, depth))
        found = max(found, _max_children(generator.ifs, depth))
    values = (node.key, node.value) if isinstance(node, ast.DictComp) else (node.elt,)
    return max(found, _max_children(values, depth))


def _nesting(node: ast.AST, depth: int) -> int:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
        return depth
    if isinstance(node, ast.If):
        return _nesting_if(node, depth)
    if isinstance(node, _NESTING_BLOCKS):
        current = depth + 1
        return max(current, _max_children(ast.iter_child_nodes(node), current))
    if isinstance(node, ast.IfExp):
        current = depth + 1
        return max(current, _max_children(ast.iter_child_nodes(node), current))
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        return _nesting_comprehension(node, depth)
    return _max_children(ast.iter_child_nodes(node), depth)


def max_nesting_depth(node: ast.AST) -> int:
    """Return lexical control-flow depth; a top-level control block has depth one."""

    return _max_children(getattr(node, "body", ()), 0)


class _FunctionCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.functions: list[FunctionNode] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        name = node.name
        qualified = ".".join((*self.scope, name))
        self.functions.append(FunctionNode(qualified, node))
        self.scope.append(name)
        self.generic_visit(node)
        self.scope.pop()


class _NestedDefinitionStripper(ast.NodeTransformer):
    def _replace(self, node: ast.AST) -> ast.Pass:
        return ast.copy_location(ast.Pass(), node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        return self._replace(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        return self._replace(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        return self._replace(node)


def _isolated_module(node: ast.AST) -> ast.Module:
    root = copy.deepcopy(node)
    stripper = _NestedDefinitionStripper()
    root.body = [stripper.visit(statement) for statement in root.body]  # type: ignore[attr-defined]
    module = ast.Module(body=[root], type_ignores=[])
    return ast.fix_missing_locations(module)


def _library_metrics(node: ast.AST) -> tuple[int, int, float]:
    module = _isolated_module(node)
    cyclomatic_blocks = cc_visit_ast(module)
    ccn = cyclomatic_blocks[0].complexity

    cognitive_report = code_complexity(ast.unparse(module))
    cognitive = cognitive_report.functions[0].complexity

    halstead_report = h_visit_ast(module)
    volume = halstead_report.functions[0][1].volume if halstead_report.functions else 0.0
    return int(ccn), int(cognitive), round(float(volume), 3)


def _function_keys(path: str, functions: Sequence[FunctionNode]) -> Iterator[str]:
    seen: dict[str, int] = {}
    for function in functions:
        base = f"{path}::{function.name}"
        seen[base] = seen.get(base, 0) + 1
        yield base if seen[base] == 1 else f"{base}#{seen[base]}"


def analyze_source(source: str, path: str = "<memory>") -> tuple[FunctionMetrics, ...]:
    tree = ast.parse(source, filename=path)
    collector = _FunctionCollector()
    collector.visit(tree)
    functions = collector.functions
    result = []
    for key, function in zip(_function_keys(path, functions), functions):
        node = function.node
        ccn, cognitive, volume = _library_metrics(node)
        result.append(
            FunctionMetrics(
                key=key,
                path=path,
                name=function.name,
                line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                ccn=ccn,
                cognitive=cognitive,
                npath=npath_complexity(node),
                halstead_volume=volume,
                max_nesting=max_nesting_depth(node),
            )
        )
    return tuple(result)


def _file_rollup(path: str, source: str, functions: Sequence[FunctionMetrics]) -> FileMetrics:
    def maximum(metric: str) -> Any:
        return max((getattr(function, metric) for function in functions), default=0)

    return FileMetrics(
        path=path,
        lines=len(source.splitlines()),
        functions=len(functions),
        ccn=maximum("ccn"),
        cognitive=maximum("cognitive"),
        npath=maximum("npath"),
        halstead_volume=round(sum(function.halstead_volume for function in functions), 3),
        max_nesting=maximum("max_nesting"),
    )


def load_policy(config_path: Path) -> Policy:
    config_path = config_path.resolve()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if data.get("schema") != 1:
        raise ValueError(f"unsupported maintainability policy schema in {config_path}")
    limits = data["limits"]
    missing = set(METRICS) - set(limits)
    if missing:
        raise ValueError(f"policy is missing limits for: {', '.join(sorted(missing))}")
    root = config_path.parent
    return Policy(
        root=root,
        paths=tuple(data["paths"]),
        exclude=tuple(data.get("exclude", ())),
        limits={metric: float(limits[metric]) for metric in METRICS},
    )


def _is_excluded(path: str, patterns: Sequence[str]) -> bool:
    return any(path == pattern or fnmatch.fnmatch(path, pattern) for pattern in patterns)


def iter_python_files(policy: Policy, requested: Sequence[str] = ()) -> Iterator[Path]:
    candidates = requested or policy.paths
    discovered = set()
    for raw_path in candidates:
        path = Path(raw_path)
        if not path.is_absolute():
            path = policy.root / path
        if path.is_file() and path.suffix == ".py":
            paths = (path,)
        elif path.is_dir():
            paths = path.rglob("*.py")
        else:
            raise FileNotFoundError(raw_path)
        for candidate in paths:
            relative = candidate.resolve().relative_to(policy.root).as_posix()
            if relative not in discovered and not _is_excluded(relative, policy.exclude):
                discovered.add(relative)
                yield candidate


def analyze(policy: Policy, requested: Sequence[str] = ()) -> Report:
    files = []
    functions = []
    for path in sorted(iter_python_files(policy, requested), key=lambda item: item.as_posix()):
        relative = path.resolve().relative_to(policy.root).as_posix()
        source = path.read_text(encoding="utf-8")
        measured = analyze_source(source, relative)
        files.append(_file_rollup(relative, source, measured))
        functions.extend(measured)
    return Report(tuple(files), tuple(functions))


def violations(
    report: Report,
    limits: Mapping[str, float],
) -> tuple[Violation, ...]:
    result = []
    for function in report.functions:
        for metric in METRICS:
            actual = float(getattr(function, metric))
            limit = float(limits[metric])
            if actual > limit:
                result.append(Violation(function.key, metric, actual, limit))
    return tuple(result)


def violation_counts(failures: Sequence[Violation]) -> dict[str, int]:
    counts = dict.fromkeys(METRICS, 0)
    for failure in failures:
        counts[failure.metric] += 1
    return counts


def _count_text(failures: Sequence[Violation]) -> str:
    counts = violation_counts(failures)
    return ", ".join(f"{metric}={counts[metric]}" for metric in METRICS)


def hotspot_functions(
    report: Report, limits: Mapping[str, float]
) -> tuple[FunctionMetrics, ...]:
    result = []
    for function in report.functions:
        if any(float(getattr(function, metric)) > float(limits[metric]) for metric in METRICS):
            result.append(function)
    return tuple(result)


def _normalised_score(function: FunctionMetrics, limits: Mapping[str, float]) -> float:
    return max(float(getattr(function, metric)) / float(limits[metric]) for metric in METRICS)


def _column_widths(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> list[int]:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    return widths


def _render_row(row: Sequence[str], widths: Sequence[int]) -> str:
    return "  ".join(value.ljust(width) for value, width in zip(row, widths))


def _table(rows: Sequence[Sequence[Any]], headers: Sequence[str]) -> str:
    text_rows = [[str(value) for value in row] for row in rows]
    widths = _column_widths(text_rows, headers)
    header = _render_row(headers, widths)
    rule = "  ".join("-" * width for width in widths)
    body = [_render_row(row, widths) for row in text_rows]
    return "\n".join((header, rule, *body))


def render_table(report: Report, limits: Mapping[str, float], top: int) -> str:
    file_rows = [
        (
            file.path,
            file.lines,
            file.functions,
            file.ccn,
            file.cognitive,
            file.npath,
            f"{file.halstead_volume:.1f}",
            file.max_nesting,
        )
        for file in report.files
    ]
    file_table = _table(
        file_rows,
        ("file", "lines", "funcs", "max CCN", "max cog", "max NPath", "Halstead V", "nest"),
    )
    ranked = sorted(
        report.functions,
        key=lambda function: (_normalised_score(function, limits), function.key),
        reverse=True,
    )
    if top:
        ranked = ranked[:top]
    function_rows = [
        (
            function.key,
            function.line,
            function.ccn,
            function.cognitive,
            function.npath,
            f"{function.halstead_volume:.1f}",
            function.max_nesting,
        )
        for function in ranked
    ]
    function_table = _table(
        function_rows, ("function", "line", "CCN", "cog", "NPath", "Halstead V", "nest")
    )
    label = "all functions" if not top else f"top {min(top, len(ranked))} functions"
    return (
        "Files (function maxima; Halstead V is the file total)\n"
        f"{file_table}\n\n{label}\n{function_table}"
    )


def render_json(report: Report) -> str:
    document = {
        "schema": 1,
        "npath_saturation": NPATH_SATURATION,
        "files": [asdict(file) for file in report.files],
        "functions": [asdict(function) for function in report.functions],
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def render_csv(report: Report) -> str:
    output = io.StringIO()
    fields = tuple(FunctionMetrics.__dataclass_fields__)
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(asdict(function) for function in report.functions)
    return output.getvalue()


def _ranked_hotspots(
    report: Report, limits: Mapping[str, float]
) -> tuple[FunctionMetrics, ...]:
    return tuple(
        sorted(
            hotspot_functions(report, limits),
            key=lambda function: (_normalised_score(function, limits), function.key),
            reverse=True,
        )
    )


def _failed_metrics(function: FunctionMetrics, limits: Mapping[str, float]) -> list[str]:
    return [
        metric
        for metric in METRICS
        if float(getattr(function, metric)) > float(limits[metric])
    ]


def render_hotspots_table(report: Report, limits: Mapping[str, float]) -> str:
    hotspots = _ranked_hotspots(report, limits)
    rows = [
        (
            function.key,
            function.line,
            function.ccn,
            function.cognitive,
            function.npath,
            f"{function.halstead_volume:.1f}",
            function.max_nesting,
            ",".join(_failed_metrics(function, limits)),
        )
        for function in hotspots
    ]
    table = _table(
        rows,
        ("function", "line", "CCN", "cog", "NPath", "Halstead V", "nest", "failed"),
    )
    failures = violations(report, limits)
    return (
        f"Current hotspots: {len(hotspots)} functions, {len(failures)} metric violations\n"
        f"By metric: {_count_text(failures)}\n"
        f"{table}"
    )


def _hotspot_document(report: Report, limits: Mapping[str, float]) -> dict[str, Any]:
    functions = []
    for function in _ranked_hotspots(report, limits):
        measured = asdict(function)
        measured["failed_metrics"] = _failed_metrics(function, limits)
        functions.append(measured)
    failures = violations(report, limits)
    return {
        "schema": 1,
        "limits": dict(limits),
        "summary": {
            "files_scanned": len(report.files),
            "functions_scanned": len(report.functions),
            "hotspot_functions": len(functions),
            "metric_violations": len(failures),
            "violations_by_metric": violation_counts(failures),
        },
        "functions": functions,
    }


def render_hotspots_json(report: Report, limits: Mapping[str, float]) -> str:
    return json.dumps(_hotspot_document(report, limits), indent=2, sort_keys=True) + "\n"


def render_hotspots_csv(report: Report, limits: Mapping[str, float]) -> str:
    output = io.StringIO()
    fields = (*FunctionMetrics.__dataclass_fields__, "failed_metrics")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for function in _ranked_hotspots(report, limits):
        row = asdict(function)
        row["failed_metrics"] = ",".join(_failed_metrics(function, limits))
        writer.writerow(row)
    return output.getvalue()


def _write_or_print(content: str, output: Path | None) -> None:
    if output is None:
        print(content, end="" if content.endswith("\n") else "\n")
    else:
        output.write_text(content, encoding="utf-8")


def _format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.3f}"


def _check(report: Report, policy: Policy) -> int:
    failures = violations(report, policy.limits)
    if not failures:
        print(
            f"maintainability: {len(report.functions)} functions in {len(report.files)} files pass"
        )
        return 0
    hotspot_count = len(hotspot_functions(report, policy.limits))
    print(
        f"Maintainability violations: {len(failures)} metrics across "
        f"{hotspot_count} functions",
        file=sys.stderr,
    )
    print(f"By metric: {_count_text(failures)}", file=sys.stderr)
    for failure in failures:
        print(
            f"  {failure.key}: {failure.metric}={_format_number(failure.actual)} "
            f"> limit {_format_number(failure.limit)}",
            file=sys.stderr,
        )
    print(
        "Run `python tools/maintainability.py hotspots` for the ranked drill-down.",
        file=sys.stderr,
    )
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("maintainability.json"), help="policy JSON path"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    report = subparsers.add_parser("report", help="show per-file and per-function measurements")
    report.add_argument(
        "paths", nargs="*", help="optional files/directories instead of policy paths"
    )
    report.add_argument("--format", choices=("table", "json", "csv"), default="table")
    report.add_argument("--output", type=Path, help="write the report instead of stdout")
    report.add_argument("--top", type=int, default=25, help="table function count; zero means all")

    hotspots = subparsers.add_parser(
        "hotspots", help="list every function which exceeds an absolute limit"
    )
    hotspots.add_argument(
        "paths", nargs="*", help="optional files/directories instead of policy paths"
    )
    hotspots.add_argument("--format", choices=("table", "json", "csv"), default="table")
    hotspots.add_argument("--output", type=Path, help="write the hotspots instead of stdout")

    check = subparsers.add_parser("check", help="fail if any function exceeds an absolute limit")
    check.add_argument(
        "paths", nargs="*", help="optional files/directories instead of policy paths"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    policy = load_policy(args.config)
    report = analyze(policy, args.paths)
    if args.command == "check":
        return _check(report, policy)
    if args.command == "hotspots":
        if args.format == "json":
            content = render_hotspots_json(report, policy.limits)
        elif args.format == "csv":
            content = render_hotspots_csv(report, policy.limits)
        else:
            content = render_hotspots_table(report, policy.limits) + "\n"
        _write_or_print(content, args.output)
        return 0
    if args.format == "json":
        content = render_json(report)
    elif args.format == "csv":
        content = render_csv(report)
    else:
        content = render_table(report, policy.limits, args.top) + "\n"
    _write_or_print(content, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
