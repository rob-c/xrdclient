# Maintainability metrics

The maintainability test turns five complementary views of function complexity
into one report and one absolute gate. It scans the handwritten Python under
`src`, `tools`, `tests` and `benchmarks`. Nothing here is generated, so
`maintainability.json` excludes nothing.

Install the development tools and run a report from the repository root:

```console
$ python -m pip install -e ".[dev]"
$ python tools/maintainability.py hotspots
$ python tools/maintainability.py report --top 50
$ python tools/maintainability.py report src/xrd/root --top 0
```

`hotspots` is the immediate drill-down: it lists every function which exceeds
at least one limit, ranks the worst first and names each failed metric. The
broader `report` table has a row for every file and ranks functions even when
they are below the limits. File CCN, cognitive complexity, NPath and nesting
values are the maximum function value; file Halstead Volume is the sum of its
function volumes. `--top 0` prints every function.

For a complete record suitable for a dashboard, spreadsheet or historical
comparison, use JSON or CSV:

```console
$ python tools/maintainability.py report --format json --output build/maintainability.json
$ python tools/maintainability.py report --format csv --output build/maintainability.csv
$ python tools/maintainability.py hotspots --format json --output build/hotspots.json
$ python tools/maintainability.py hotspots --format csv --output build/hotspots.csv
```

## What the five numbers mean

| Metric | What it exposes | Standard limit |
| --- | --- | ---: |
| CCN | Independent control-flow decisions: branches, loops, handlers and Boolean decisions | 10 |
| Cognitive Complexity | The reading burden of breaks in linear flow, with extra weight for nesting | 15 |
| NPath | The number of structurally possible execution paths through a function | 200 |
| Halstead Volume | Implementation vocabulary and length, `length × log2(vocabulary)` | 500 |
| Maximum nesting | The deepest lexical stack of control-flow blocks | 4 |

`test_project_policy_keeps_the_readability_limits_strict` pins these exact
values. Do not raise a limit to make the burn-down pass: split or simplify the
function, run its behavioral tests, then rerun `hotspots` and `check`.

CCN and Halstead Volume come from
[Radon](https://radon.readthedocs.io/en/stable/), while Cognitive Complexity
comes from [Complexipy](https://github.com/rohaquinlop/complexipy). The project
owns the two short AST implementations for NPath and nesting so their behavior
is inspectable and covered by unit tests.

This project's NPath calculation multiplies independent sequential choices and
adds mutually exclusive alternatives. It includes short-circuit Boolean
expressions, conditional expressions, loop termination, exception handlers,
`match` cases and comprehensions. Values saturate at `1,000,000,000,000` because
the gate is interested in “far over 200,” not an enormous integer's last digit.

Maximum nesting counts `if`, loops, `try`, `with`, `match`, conditional
expressions and comprehension generators. The first top-level block has depth
one, and an `elif` remains at the same level as its `if`. A nested function is
measured as its own function and is excluded from its parent's metrics.

These are warning signals, not quality grades. A high result tells a reviewer
where decomposition, a data-driven dispatch table, early returns or smaller
helpers may make the code easier to reason about. It does not prove that a
function is wrong, nor should a low result override a poor name or unclear
domain model.

## Absolute enforcement

CI and `tests/test_maintainability.py` run the same check:

```console
$ python tools/maintainability.py check
Maintainability violations: N metrics across M functions
```

`maintainability.json` holds the scan paths, exclusions and standard limits.
There is no baseline, allowance list or accepted-hotspot backlog. Every function
is compared with the same five limits, including existing code, and `check`
remains red until all violations have been refactored below those limits. Run
`hotspots` after each refactoring pass to see the complete remaining drill-down.
The first line of either command is the burn-down score: refactoring is complete
only when both the function count and metric-violation count reach zero.
The second line breaks the remaining failures down by metric, making it clear
whether a refactor reduced decision count, path count, vocabulary or nesting.
