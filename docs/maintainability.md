# Maintainability metrics

The maintainability test turns five complementary views of function complexity
into one report and one absolute gate. It scans the handwritten Python under
`src`, `tools`, `tests`, `benchmarks` and `examples`. Nothing here is
generated, so `maintainability.json` excludes nothing.

The tool is the chain's, in
[xrd-devtools](https://github.com/rob-c/xrd-devtools) (`xrddevtools
maintainability`); this repository keeps its policy, `maintainability.json`.
Install the development tools and an xrd-devtools checkout (see
[Testing](testing.md#pre-commit-quality-gates)) and run a report from the
repository root:

```console
$ python -m pip install -e ".[dev]" -e ../xrd-devtools
$ xrddevtools maintainability hotspots
$ xrddevtools maintainability report --top 50
$ xrddevtools maintainability report src/xrdclient/root --top 0
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
$ xrddevtools maintainability report --format json --output build/maintainability.json
$ xrddevtools maintainability report --format csv --output build/maintainability.csv
$ xrddevtools maintainability hotspots --format json --output build/hotspots.json
$ xrddevtools maintainability hotspots --format csv --output build/hotspots.csv
```

## What the five numbers mean

| Metric | What it exposes | Standard limit |
| --- | --- | ---: |
| CCN | Independent control-flow decisions: branches, loops, handlers and Boolean decisions | 10 |
| Cognitive Complexity | The reading burden of breaks in linear flow, with extra weight for nesting | 15 |
| NPath | The number of structurally possible execution paths through a function | 200 |
| Halstead Volume | Implementation vocabulary and length, `length × log2(vocabulary)` | 500 |
| Maximum nesting | The deepest lexical stack of control-flow blocks | 4 |

`xrddevtools.maintainability.STANDARD_LIMITS` pins these exact values, and
the gate refuses a policy that is looser or excludes anything. Do not raise a
limit to make the burn-down pass: split or simplify the
function, run its behavioral tests, then rerun `hotspots` and `check`.

CCN and Halstead Volume come from
[Radon](https://radon.readthedocs.io/en/stable/), while Cognitive Complexity
comes from [Complexipy](https://github.com/rohaquinlop/complexipy). The tool
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

CI and `tests/test_maintainability.py` (xrd-devtools' plugin, in a few
lines) run the same check:

```console
$ xrddevtools maintainability check
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

## No grandfathered code

Every gate applies to old code exactly as to new; none has a snapshot, a
baseline or an ignore list:

- **ruff** selects `E`, `F`, `W`, `I`, `UP`, `B`, `C4`, `SIM`, `RUF` and the
  focused security rules, with `ignore = []` and no per-file ignores, and
  `TID251` with a banned-API list: the APIs whose job xrdcore, xrdsec or
  xrdhttp does (raw `http.client`, hand-built TLS contexts, a bare
  `ThreadPoolExecutor`, unguarded XML parsers, `gssapi`, `botocore`, ...),
  each entry naming its replacement. CONTRIBUTING.md explains the list.
- **mypy** runs `--strict` over `src`, `tests`, `tools`, `benchmarks` and
  `examples`. Test modules may leave fixtures and returns unannotated
  (`disallow_untyped_defs` is off for `tests.*`), but their bodies are
  checked. Only third-party modules with no types at all are allowed to be
  missing: the official `XRootD` bindings. fsspec has none either,
  but the adapter subclasses it, so `typings/fsspec` carries stubs for the
  part of it this project uses.
- **complexipy** (`[tool.complexipy]`: max 15, `failed = true`) over the
  same five trees, run by CI beside `xrddevtools maintainability check`.
- **conformance**: `xrddevtools conformance` holds this `pyproject.toml` (and
  every other repository's of the chain) to the one standard - the ruff
  selection, mypy strict over every tree, complexipy 15, 100 % with the
  shared exclusions, warnings as errors - and fails on a suppression comment
  without its code and reason, on any snapshot/baseline file, or on a
  managed block of `pyproject.toml` or `CONTRIBUTING.md` that is not what
  `xrddevtools sync` writes from the standard kept in xrd-devtools (so the
  standard is changed there, never by hand here);
  `xrddevtools dupes` fails on a function copied into another repository.
- **coverage** is 100 % line and branch over the whole package; see
  [Testing](testing.md#coverage).

Suppression comments (`# noqa`, `# type: ignore`, `# pragma: no cover`,
`# fmt: off`) are fixed rather than written. The few left are irreducible,
each with its error code and the reason beside it:

- four `# type: ignore[overload-overlap]` on `open_url`, `open_http` and
  `open_s3`: their `buffering=0` overload returns the raw layer, which no
  "int except zero" type can keep apart from the buffered overload -
  typeshed's own `open` carries the same ignore;
- in the tests, a handful that pass a wrongly typed value, or instantiate an
  abstract class, on purpose, to prove the runtime refuses it.

## Module size

The limits above are per function; they say nothing about a module that has
grown to hold several jobs. When one does, split it along its seams into a
facade plus private submodules, keeping every import path - the conventions,
and the map of the packages already split that way, are in
[Module map](architecture.md).

## Take-over scorecard

The function-level gate above asks "is any one function too complicated?".
Someone inheriting this code asks a wider question: *how much of it can I
read, trust and change without its author?* The take-over scorecard
(`xrddevtools.scorecard`, in xrd-devtools since 0.4.0) answers it
with twelve project-level measurements. Each is something that predicts the
cost of a hand-over, and each has a published threshold from a widely used
tool behind it:

| Measurement | Threshold source | Better is |
| --- | --- | --- |
| Maintainability Index (Radon MI, SLOC-weighted) | Radon / Visual Studio MI | higher |
| Mean cyclomatic complexity | McCabe (1976) | lower |
| Functions with CCN > 10 | McCabe; flake8-mccabe guidance | lower |
| Functions with cognitive complexity > 15 | SonarSource rule `python:S3776` | lower |
| Functions with > 50 statements | pylint `too-many-statements` (R0915) | lower |
| Functions with > 5 parameters (excluding `self`/`cls`) | pylint `too-many-arguments` (R0913) | lower |
| Functions nested > 5 blocks deep | pylint `too-many-nested-blocks` (R1702) | lower |
| Modules over 1000 code lines | pylint `too-many-lines` (C0302) | lower |
| Comment + docstring lines per code line | — | higher |
| Docstring coverage (module, public class/function) | interrogate-style | higher |
| Fully type-annotated functions | mypy `--strict` | higher |
| Duplicated lines (repeated 8-line windows) | CPD / SonarQube duplication | lower |

`@typing.overload` stubs are not counted as functions: they only declare
signatures for the implementation that follows.

### "Industry average", measured rather than asserted

Nobody publishes an industry average for all twelve, so the tool measures
one. `xrddevtools scorecard baseline` runs exactly the same static analysis over a
reference corpus of 39 widely used, actively maintained open-source Python
libraries. The corpus is requests, urllib3, botocore, click, jinja2, pygments,
rich, pip, pytest, coverage, cryptography, docutils, fsspec and 26 more; the
list is `REFERENCE_CORPUS` in the tool. It keeps every project's numbers in
`scorecard_baseline.json` beside the tool (`src/xrddevtools/` in
xrd-devtools). Each library's own vendored code and test
directories are excluded, as they are for this project. Nothing is imported or
executed, so any installed distribution can be measured safely.

`xrddevtools scorecard score` places a project against that corpus. For every
measurement it reports the project's value, the corpus median and the
project's percentile within the corpus, oriented so that 100 always means
"easier to take over than every reference project". The headline **Takeover
Ease Index** is the mean of the twelve percentiles; 50 is the corpus average
by construction.

```console
$ xrddevtools scorecard score src/xrdclient --name xrdclient    # against the committed baseline
$ xrddevtools scorecard score ../xgfalclient/src --name xgfalclient
$ xrddevtools scorecard measure src/xrdclient               # raw numbers as JSON
$ xrddevtools scorecard baseline --output ../xrd-devtools/src/xrddevtools/scorecard_baseline.json   # re-measure
```

### The gate

`tests/test_scorecard.py` fails unless **every one** of the twelve
measurements beats the corpus median, not merely the index. It is a few
lines loading xrd-devtools' scorecard plugin, and xrdcore, xrdsec, xrdhttp,
xgfalclient and xrdarc carry the same few lines for their own source trees
(xrdwms is not gated yet). Re-run
`baseline` when the corpus's installed versions move on and commit the
result, so that the bar moves with the industry instead of with this project.

### What it cannot tell you

These are structural proxies. A high explanation density is only worth
something if the comments are true and say *why*. The review standard in
`CONTRIBUTING.md` still applies, and padding a file to move a number defeats
the purpose. Hand-over risk that lives outside the code is not measured: bus
factor, undocumented operational knowledge, how hard real-server interop
environments are to reproduce. The architecture map (`docs/architecture.md`),
`docs/testing.md` and the interop harnesses are there for those.
