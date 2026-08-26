"""``xrd-datasets`` - a machine-learning datasets site, built and checked.

    $ xrd-datasets build /srv/datasets --only "mnist*" --only iris
    $ xrd-datasets verify /srv/datasets
    $ xrd-datasets site /srv/datasets --base-url https://data.example.org

``build`` converts every dataset whose licence allows redistribution into a
ROOT file, or explicit publisher-split ROOT shards when the complete result
cannot use ROOT's 32-bit file layout, under one directory - see
:mod:`xrd.root.datasets` for what is on
offer - and writes an ``index.json`` beside them saying what each file is,
where it came from, its canonical licence terms, what was transformed, and
what its checksum is. ``verify`` reopens every file and refuses to bless a
directory that no longer matches its index. ``site`` puts a browsable page
and ready-to-serve nginx and BriX configuration next to the files, so the
directory can go on the web as it stands. Verification checks the recorded
branch schemas and decodes every entry of every readable branch in bounded
batches; an empty or wholly NULL/non-finite/all-bits-set payload is a failure.

The index is what makes the directory a catalogue: point ``XRD_CATALOGUE``
at wherever it is served and ``xrd.ml.load("mnist")`` finds the file by
name, on any machine, over whichever protocol the site speaks.
"""

# The generated HTML and CSS remain readable as literal, inspectable assets;
# wrapping individual style rules would only add whitespace to every site.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import concurrent.futures
import faulthandler
import fnmatch
import html
import json
import math
import os
import signal
import string
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .._log import get_logger
from ..config import Config
from ..crypto import checksum_file
from ..errors import XRootDError
from ..root import open_root
from ..root.datasets import (
    DATASETS,
    SOURCE_SIZE_CEILING,
    Large,
    convert,
    licence_url,
    redistributable,
)
from ..root.writer import create
from ..types import human_bytes
from . import ERROR, OK, common_flags, config_from, dumps, fail

__all__ = ["main"]

PROGRAM = "xrd-datasets"
_log = get_logger(__name__)

_VERIFY_BATCH_BYTES = 8 * 1024 * 1024
_VERIFY_MAX_ENTRIES = 10_000
_ALL_BITS_SET = frozenset((-1, 0xFF, 0xFFF, 0xFFFF, 0xFFFFFF, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF))
_NULL_TEXT = frozenset(("", "null", "none", "nan", "n/a"))
_FF_TEXT = frozenset(("fff", "0xff", "0xfff", "0xffff", "-1"))


# ---------------------------------------------------------------------------
# Choosing
# ---------------------------------------------------------------------------


def _chosen(
    only: Sequence[str],
    everything: bool,
    large: bool = False,
    allow_oversize: bool = False,
) -> list[str]:
    """The dataset names a command was asked for, licence gate applied.

    ``--only`` narrows by glob; without ``--all``, whatever the licence does
    not allow onto a mirror is left out - loudly, when it was asked for by
    name, because silently skipping what somebody typed is how a build lies.
    """
    requested = _requested(only)
    over = _oversize(requested)
    if over and only and not allow_oversize:
        raise ValueError(
            f"{', '.join(over)} reaches the strict 2 GB per-dataset source ceiling; "
            "this catalogue publishes complete source payloads below 2,000,000,000 bytes "
            "by default; pass --allow-oversize after provisioning sufficient storage"
        )
    matched = _matching_size(requested, large=large, allow_oversize=allow_oversize)
    if not matched:
        raise ValueError(_no_match(only, large=large))
    if everything:
        return matched
    allowed = [name for name in matched if redistributable(DATASETS[name].licence)]
    if not allowed:
        withheld = ", ".join(matched)
        raise ValueError(
            f"the licence of {withheld} does not allow redistribution; "
            "--all converts it anyway, for a directory you serve only to yourself"
        )
    return allowed


def _requested(only: Sequence[str]) -> list[str]:
    """Dataset names selected by the optional glob expressions."""
    return [
        name
        for name in sorted(DATASETS)
        if not only or any(fnmatch.fnmatchcase(name, pattern) for pattern in only)
    ]


def _oversize(names: Sequence[str]) -> list[str]:
    """Selected datasets beyond the default complete-source ceiling."""
    return [name for name in names if not DATASETS[name].within_source_ceiling()]


def _matching_size(names: Sequence[str], *, large: bool, allow_oversize: bool) -> list[str]:
    """Apply the source-size ceiling and optional large-dataset filter."""
    return [
        name
        for name in names
        if (allow_oversize or DATASETS[name].within_source_ceiling())
        and (not large or DATASETS[name].large_source(allow_oversize=allow_oversize))
    ]


def _no_match(only: Sequence[str], *, large: bool) -> str:
    kind = "large dataset" if large else "dataset"
    wanted = ", ".join(only) if only else "the selection"
    return f"no {kind} matches {wanted}; try `xrd-datasets list`"


def _list(args: argparse.Namespace, config: Config) -> int:
    names = _chosen(args.only, True, args.large, args.allow_oversize)
    if args.json:
        print(
            dumps(
                [
                    {
                        "name": name,
                        "title": DATASETS[name].title,
                        "licence": DATASETS[name].licence,
                        "licence_url": licence_url(DATASETS[name].licence),
                        "redistributable": redistributable(DATASETS[name].licence),
                        **DATASETS[name].provenance(),
                        "transformation": DATASETS[name].transformation_summary(),
                        "large": DATASETS[name].file_backed(),
                        "source_bytes": DATASETS[name].source_payload_bytes(),
                        "modality": DATASETS[name].modality,
                        "task": DATASETS[name].task,
                    }
                    for name in names
                ]
            )
        )
        return OK
    for name in names:
        spec = DATASETS[name]
        flags = []
        if spec.file_backed():
            flags.append("large")
        if not redistributable(spec.licence):
            flags.append("not redistributable")
        gate = f"  [{', '.join(flags)}]" if flags else ""
        print(f"{name:<18} {spec.title}{gate}")
    return OK


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def _diagnostic_seconds(text: str) -> float:
    """A positive heartbeat interval accepted by ``--diagnostics``."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of seconds: {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("diagnostic interval must be positive")
    return value


class _Diagnostics:
    """Timestamped build state plus an on-demand all-thread stack dump."""

    def __init__(self, interval: float | None, out: Path, source_cache: Path) -> None:
        self.interval = interval
        self.out = out
        self.source_cache = source_cache
        self._active: dict[str, dict[str, Any]] = {}
        self._phase = "starting"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._signal = False

    @property
    def enabled(self) -> bool:
        return self.interval is not None

    def __enter__(self) -> _Diagnostics:
        if not self.enabled:
            return self
        self._install_stack_dump()
        assert self.interval is not None
        self._emit(
            f"diagnostics enabled: pid={os.getpid()}, heartbeat={self.interval:g}s; "
            f"send SIGUSR1 to dump every Python thread"
        )
        self._thread = threading.Thread(target=self._heartbeats, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if self._signal:
            faulthandler.unregister(signal.SIGUSR1)

    def phase(self, description: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._phase = description
        self._emit(description)

    def begin(self, name: str, partial: Path, source_bytes: int) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._active[name] = {
                "partial": partial,
                "started": time.monotonic(),
                "split": "opening",
                "rows": 0,
            }
        self._emit(f"{name}: started; declared source={human_bytes(source_bytes)}")

    def split(self, name: str, split: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            state = self._active[name]
            state["split"] = split
            state["rows"] = 0
        self._emit(f"{name}: {split}: fetching sources and converting")

    def output(self, name: str, path: Path) -> None:
        """Track the particular atomic file currently receiving rows."""
        if self.enabled:
            with self._lock:
                self._active[name]["partial"] = path

    def rows(self, name: str, split: str, rows: int) -> None:
        if not self.enabled:
            return
        with self._lock:
            state = self._active.get(name)
            if state is not None and state["split"] == split:
                state["rows"] = rows

    def progress_for(self, name: str, split: str) -> Callable[[int], None]:
        def report(rows: int) -> None:
            self.rows(name, split, rows)

        return report

    def split_done(self, name: str, split: str, rows: int) -> None:
        if self.enabled:
            self._emit(f"{name}: {split}: finished {rows:,} rows")

    def finalizing(self, name: str, output: Path) -> None:
        if not self.enabled:
            return
        with self._lock:
            state = self._active[name]
            state["partial"] = output
            state["split"] = "final ROOT readback and checksum"
        self._emit(f"{name}: ROOT file closed; reading it back and checksumming")

    def finish(self, name: str, problem: str | None = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            state = self._active.pop(name, None)
        elapsed = time.monotonic() - state["started"] if state is not None else 0.0
        outcome = f"failed: {problem}" if problem is not None else "completed"
        self._emit(f"{name}: {outcome} after {elapsed:.1f}s")

    def _install_stack_dump(self) -> None:
        try:
            faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
        except (AttributeError, OSError, RuntimeError, ValueError):
            self._emit("SIGUSR1 stack dumps are unavailable on this platform")
        else:
            self._signal = True

    def _heartbeats(self) -> None:
        assert self.interval is not None
        while not self._stop.wait(self.interval):
            self._heartbeat()

    def _heartbeat(self) -> None:
        with self._lock:
            active = [(name, dict(state)) for name, state in self._active.items()]
            phase = self._phase
        if not active:
            self._emit(f"heartbeat: {phase}")
            return
        cache = self._cache_parts()
        for name, state in active:
            partial = state["partial"]
            size = self._path_size(partial)
            elapsed = time.monotonic() - state["started"]
            self._emit(
                f"heartbeat: {name}: split={state['split']}, rows={state['rows']:,}, "
                f"output={human_bytes(size)}, elapsed={elapsed:.1f}s{cache}"
            )

    def _cache_parts(self) -> str:
        parts = sorted(self.source_cache.glob("*.part"))
        if not parts:
            return ""
        shown = ", ".join(
            f"{path.name}={human_bytes(self._path_size(path))}" for path in parts[:3]
        )
        suffix = f", +{len(parts) - 3} more" if len(parts) > 3 else ""
        return f", downloads=[{shown}{suffix}]"

    @staticmethod
    def _path_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    @staticmethod
    def _emit(message: str) -> None:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[{stamp}] {PROGRAM}: {message}", file=sys.stderr, flush=True)


def _chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            yield chunk


def _trees_in(path: Path) -> dict[str, int]:
    """Tree name to row count, read back out of a finished file."""
    trees, _schemas = _root_layout(path)
    return trees


def _root_layout(path: Path) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    """Read the row counts and exact branch schemas from one finished ROOT file."""
    with open_root(str(path)) as back:
        names = back.trees()
        trees = {name: back[name].num_entries for name in names}
        schemas = {name: _tree_schema(back[name]) for name in names}
    return trees, schemas


def _tree_schema(tree: Any) -> dict[str, Any]:
    """The type and shape contract every branch advertises to readers."""
    return {
        name: {
            "type": branch.typename,
            "length": branch.length,
            "jagged": branch.is_jagged,
        }
        for name, branch in tree.branches.items()
    }


def _checked_layout(path: Path, expected: Mapping[str, int]) -> dict[str, dict[str, Any]]:
    """Read back a new output and reject row counts unlike those just written."""
    trees, schemas = _root_layout(path)
    if trees != expected:
        raise ValueError(f"{path.name} wrote trees {trees}, not the recorded {dict(expected)}")
    return schemas


def _entry(name: str, path: Path, trees: dict[str, int]) -> dict[str, Any]:
    spec = DATASETS[name]
    return {
        "name": name,
        "title": spec.title,
        "licence": spec.licence,
        "licence_url": licence_url(spec.licence),
        "redistributable": redistributable(spec.licence),
        **spec.provenance(),
        "transformation": spec.transformation_summary(),
        "large": spec.file_backed(),
        "source_bytes": spec.source_payload_bytes(),
        "modality": spec.modality,
        "task": spec.task,
        "file": path.name,
        "download": path.name,
        "bytes": path.stat().st_size,
        "adler32": checksum_file("adler32", _chunks(path)),
        "splits": list(spec.splits),
        "trees": trees,
        "schemas": _checked_layout(path, trees),
        "rows": sum(trees.values()),
    }


def _part_path(path: Path, split: str) -> Path:
    """The stable file name for one split of a sharded dataset."""
    safe = quote(split, safe="-_.")
    return path.with_name(f"{path.stem}--{safe}{path.suffix}")


def _sharded_entry(
    name: str, parts: Sequence[tuple[str, Path, dict[str, int]]]
) -> dict[str, Any]:
    """One catalogue record backed by independently streamable ROOT files."""
    spec = DATASETS[name]
    files: list[dict[str, Any]] = [
        {
            "split": split,
            "file": path.name,
            "download": path.name,
            "bytes": path.stat().st_size,
            "adler32": checksum_file("adler32", _chunks(path)),
            "trees": trees,
            "schemas": _checked_layout(path, trees),
            "rows": sum(trees.values()),
        }
        for split, path, trees in parts
    ]
    entry = {
        "name": name,
        "title": spec.title,
        "licence": spec.licence,
        "licence_url": licence_url(spec.licence),
        "redistributable": redistributable(spec.licence),
        **spec.provenance(),
        "transformation": spec.transformation_summary(),
        "large": spec.file_backed(),
        "source_bytes": spec.source_payload_bytes(),
        "modality": spec.modality,
        "task": spec.task,
        "files": files,
        "bytes": sum(item["bytes"] for item in files),
        "rows": sum(item["rows"] for item in files),
        "trees": {
            f"{item['split']}/{tree}": rows
            for item in files
            for tree, rows in item["trees"].items()
        },
    }
    return entry


def _temporary_output(path: Path) -> Path:
    """Reserve a conversion-owned temporary beside its atomic destination."""
    held = tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent, delete=False
    )
    partial = Path(held.name)
    held.close()
    partial.chmod(0o644)  # the published replacement must be readable by nginx
    return partial


def _write_splits(
    name: str,
    path: Path,
    splits: Sequence[str],
    *,
    base: str | None,
    compression: str | None,
    source_cache: Path,
    allow_oversize: bool,
    config: Config,
    diagnostics: _Diagnostics,
) -> dict[str, int]:
    """Atomically write a selected run of splits to one ROOT file."""
    partial = _temporary_output(path)
    spec = DATASETS[name]
    prefix = "" if isinstance(spec, Large) and spec.split_files else None
    diagnostics.output(name, partial)
    try:
        with create(str(partial), compression=compression, config=config) as out:
            trees: dict[str, int] = {}
            for split in splits:
                diagnostics.split(name, split)
                made = convert(
                    name,
                    out,
                    split=split,
                    prefix=prefix,
                    base=base,
                    source_cache=source_cache,
                    allow_oversize=allow_oversize,
                    progress=diagnostics.progress_for(name, split),
                    config=config,
                )
                trees.update(made)
                diagnostics.split_done(name, split, sum(made.values()))
        partial.replace(path)
        return trees
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _convert_one(
    name: str,
    path: Path,
    *,
    base: str | None,
    compression: str | None,
    source_cache: Path,
    allow_oversize: bool,
    config: Config,
    diagnostics: _Diagnostics,
) -> dict[str, Any]:
    """Convert one logical dataset atomically into one or more ROOT files."""
    spec = DATASETS[name]
    diagnostics.begin(name, path, spec.source_payload_bytes())
    if isinstance(spec, Large) and spec.split_files:
        parts = []
        for split in spec.splits:
            part = _part_path(path, split)
            trees = _write_splits(
                name,
                part,
                (split,),
                base=base,
                compression=compression,
                source_cache=source_cache,
                allow_oversize=allow_oversize,
                config=config,
                diagnostics=diagnostics,
            )
            parts.append((split, part, trees))
        diagnostics.finalizing(name, parts[0][1])
        return _sharded_entry(name, parts)
    trees = _write_splits(
        name,
        path,
        spec.splits,
        base=base,
        compression=compression,
        source_cache=source_cache,
        allow_oversize=allow_oversize,
        config=config,
        diagnostics=diagnostics,
    )
    diagnostics.finalizing(name, path)
    return _entry(name, path, trees)


def _build(args: argparse.Namespace, config: Config) -> int:
    out = Path(args.directory)
    out.mkdir(parents=True, exist_ok=True)
    source_cache = (
        Path(args.source_cache) if args.source_cache else out.parent / f".{out.name}-sources"
    )
    with _Diagnostics(args.diagnostics, out, source_cache) as diagnostics:
        return _run_build(args, config, out, source_cache, diagnostics)


def _run_build(
    args: argparse.Namespace,
    config: Config,
    out: Path,
    source_cache: Path,
    diagnostics: _Diagnostics,
) -> int:
    """Select, resume, convert and index while diagnostics observe each phase."""
    names = _chosen(args.only, args.all, args.large, args.allow_oversize)

    candidates = [name for name in names if _dataset_exists(out, name) and not args.force]
    diagnostics.phase(f"validating {len(candidates)} existing outputs")
    entries = _kept_entries(candidates, out, args, diagnostics)
    kept = list(entries)
    todo = [name for name in names if name not in entries]
    diagnostics.phase(f"converting {len(todo)} pending datasets with {args.jobs} worker(s)")
    failed = _convert_pending(todo, entries, out, source_cache, args, config, diagnostics)

    diagnostics.phase(f"writing index for {len(entries)} completed datasets")
    ordered = [entries[name] for name in sorted(entries)]
    _write_index(out, ordered)
    if args.json:
        print(dumps({"converted": sorted(todo), "kept": sorted(kept), "failed": failed}))
    return ERROR if failed else OK


def _dataset_paths(out: Path, name: str) -> list[tuple[str | None, Path]]:
    """Every expected output path for one logical catalogue dataset."""
    spec = DATASETS[name]
    path = out / f"{name}.root"
    if isinstance(spec, Large) and spec.split_files:
        return [(split, _part_path(path, split)) for split in spec.splits]
    return [(None, path)]


def _dataset_exists(out: Path, name: str) -> bool:
    """Whether every file needed to resume this dataset is present."""
    return all(path.exists() for _split, path in _dataset_paths(out, name))


def _kept_entries(
    kept: Sequence[str],
    out: Path,
    args: argparse.Namespace,
    diagnostics: _Diagnostics,
) -> dict[str, dict[str, Any]]:
    """Index completed files retained from an earlier build."""
    entries: dict[str, dict[str, Any]] = {}
    for position, name in enumerate(kept, 1):
        diagnostics.phase(f"validating existing output {position}/{len(kept)}: {name}")
        try:
            paths = _dataset_paths(out, name)
            if paths[0][0] is None:
                path = paths[0][1]
                entries[name] = _entry(name, path, _trees_in(path))
            else:
                parts = [
                    (str(split), path, _trees_in(path)) for split, path in paths
                ]
                entries[name] = _sharded_entry(name, parts)
        except Exception as exc:
            problem = f"{type(exc).__name__}: {exc}"
            print(
                f"{PROGRAM}: {name}: existing output is unreadable ({problem}); rebuilding",
                file=sys.stderr,
            )
            continue
        if not args.quiet and not args.json:
            print(f"{name}: kept, {human_bytes(entries[name]['bytes'])}")
    return entries


def _convert_pending(
    todo: Sequence[str],
    entries: dict[str, dict[str, Any]],
    out: Path,
    source_cache: Path,
    args: argparse.Namespace,
    config: Config,
    diagnostics: _Diagnostics,
) -> dict[str, str]:
    """Convert pending datasets concurrently and collect recoverable failures."""
    failed: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        running = {
            pool.submit(
                _convert_one,
                name,
                out / f"{name}.root",
                base=args.base,
                compression=args.compression,
                source_cache=source_cache,
                allow_oversize=args.allow_oversize,
                config=config,
                diagnostics=diagnostics,
            ): name
            for name in todo
        }
        for future in concurrent.futures.as_completed(running):
            name = running[future]
            try:
                entries[name] = future.result()
            except Exception as exc:
                problem = f"{type(exc).__name__}: {exc}"
                failed[name] = problem
                diagnostics.finish(name, problem)
                print(f"{PROGRAM}: {name}: {problem}", file=sys.stderr)
            else:
                diagnostics.finish(name)
                _show_converted(name, entries[name], args)
    return failed


def _show_converted(name: str, made: dict[str, Any], args: argparse.Namespace) -> None:
    """Report one successful conversion in human-readable mode."""
    if not args.quiet and not args.json:
        print(f"{name}: {made['rows']} rows, {human_bytes(made['bytes'])}")


def _write_index(out: Path, entries: list[dict[str, Any]]) -> None:
    document = {
        "format": 2,
        "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "datasets": entries,
    }
    (out / "index.json").write_text(json.dumps(document, indent=2) + "\n")
    lines = [
        f"{file['adler32']}  {file['bytes']:>12}  {file['file']}"
        for made in entries
        for file in _entry_files(made)
    ]
    (out / "MANIFEST").write_text("\n".join(lines) + "\n" if lines else "")


# ---------------------------------------------------------------------------
# Verifying
# ---------------------------------------------------------------------------


def _verify(args: argparse.Namespace, config: Config) -> int:
    out = Path(args.directory)
    index = json.loads((out / "index.json").read_text())
    problems = {
        made["name"]: problem
        for made in index["datasets"]
        if (problem := _verify_entry(out, made)) is not None
    }
    _show_verification(index["datasets"], problems, args)
    return ERROR if problems else OK


def _verify_entry(out: Path, made: dict[str, Any]) -> str | None:
    """Why one generated file disagrees with its index entry, if it does."""
    for file in _entry_files(made):
        problem = _verify_file(out, file)
        if problem is not None:
            return f"{file['file']}: {problem}" if made.get("files") else problem
    return None


def _entry_files(made: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize one-file and split-file catalogue entries."""
    return list(made.get("files") or [made])


def _verify_file(out: Path, made: dict[str, Any]) -> str | None:
    """Why one physical ROOT file disagrees with its recorded metadata."""
    path = out / made["file"]
    if not path.exists():
        return "the file is missing"
    size = path.stat().st_size
    if size != made["bytes"]:
        return f"{size} bytes on disk, {made['bytes']} in the index"
    if checksum_file("adler32", _chunks(path)) != made["adler32"]:
        return "the checksum does not match the index"
    try:
        return _verify_root_payload(path, made)
    except Exception as exc:
        return f"the ROOT payload cannot be loaded ({type(exc).__name__}: {exc})"


@dataclass
class _PayloadHealth:
    """Whether a decoded branch contains anything beyond common missing sentinels."""

    values: int = 0
    nulls: int = 0
    all_bits_set: int = 0
    ordinary: int = 0

    def observe(self, value: Any) -> None:
        self.values += 1
        kind = _sentinel_kind(value)
        if kind == "null":
            self.nulls += 1
        elif kind == "ff":
            self.all_bits_set += 1
        else:
            self.ordinary += 1

    def problem(self) -> str | None:
        if self.ordinary:
            return None
        if self.nulls and not self.all_bits_set:
            return "only NULL, zero or non-finite values"
        if self.all_bits_set and not self.nulls:
            return "only 0xff/all-bits-set values"
        return "only NULL/non-finite and all-bits-set sentinels"


def _sentinel_kind(value: Any) -> str | None:
    """Classify one scalar as ordinary, null-like, or an all-bits-set sentinel."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return None if value else "null"
    if isinstance(value, float):
        return _float_sentinel(value)
    if isinstance(value, int):
        return _integer_sentinel(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _byte_sentinel(value)
    if isinstance(value, str):
        return _text_sentinel(value)
    return None


def _float_sentinel(value: float) -> str | None:
    """Finite nonzero floats are payload; zero and non-finite values are null-like."""
    return None if math.isfinite(value) and value != 0 else "null"


def _integer_sentinel(value: int) -> str | None:
    """Recognize zero and common all-bits-set integer widths."""
    if value == 0:
        return "null"
    return "ff" if value in _ALL_BITS_SET else None


def _text_sentinel(value: str) -> str | None:
    """Recognize conventional textual missing and hexadecimal sentinel spellings."""
    normalized = value.strip().lower()
    if normalized in _NULL_TEXT:
        return "null"
    return "ff" if normalized in _FF_TEXT else None


def _byte_sentinel(value: bytes | bytearray | memoryview) -> str | None:
    """Classify one byte string without materializing a second copy."""
    if not value or all(byte == 0 for byte in value):
        return "null"
    return "ff" if all(byte == 0xFF for byte in value) else None


def _observe_values(health: _PayloadHealth, values: Any) -> None:
    """Walk nested decoded values into one branch-health summary."""
    nested: Sequence[Any]
    if isinstance(values, Mapping):
        nested = list(values.values())
    elif isinstance(values, Sequence) and not isinstance(
        values, (str, bytes, bytearray, memoryview)
    ):
        nested = values
    else:
        health.observe(values)
        return
    if not nested:
        health.observe(None)
        return
    for value in nested:
        _observe_values(health, value)
        if health.ordinary:
            return


def _payload_branches(tree: Any) -> list[str]:
    """Prefer tensor-like data branches over labels and bookkeeping scalars."""
    readable = tree.readable()
    wide = [name for name in readable if tree[name].length > 1 or tree[name].is_jagged]
    if wide:
        return wide
    payload = [name for name in readable if name.rsplit(".", 1)[-1] not in {"index", "label"}]
    return payload or readable


def _verification_step(branch: Any) -> int:
    """Entries per read, capped so a wide image or waveform stays bounded in memory."""
    kind = getattr(branch.column, "kind", "")
    if kind == "values":
        return 256
    if branch.is_jagged:
        return 1024
    itemsize = max(int(getattr(branch.column, "itemsize", 1)), 1)
    entry_bytes = max(itemsize * int(branch.length), 1)
    return max(1, min(_VERIFY_MAX_ENTRIES, _VERIFY_BATCH_BYTES // entry_bytes))


def _decoded_length(branch: Any, values: Any, entries: int) -> tuple[int, int]:
    """Actual and expected decoded scalar counts for one branch batch."""
    actual = len(values)
    kind = getattr(branch.column, "kind", "")
    expected = entries if branch.is_jagged or kind == "values" else entries * branch.length
    return actual, expected


def _scan_branch(branch: Any, entries: int, health: _PayloadHealth | None) -> None:
    """Decode every entry of one branch and validate each returned batch shape."""
    step = _verification_step(branch)
    for start in range(0, entries, step):
        stop = min(start + step, entries)
        values = branch.array(start, stop)
        actual, expected = _decoded_length(branch, values, stop - start)
        if actual != expected:
            raise ValueError(
                f"decoded {actual} values for entries {start}:{stop}, expected {expected}"
            )
        if health is not None and not health.ordinary:
            _observe_values(health, values)


def _schema_problem(tree: Any, expected: Any) -> str | None:
    """Describe a branch type/shape disagreement against the build-time manifest."""
    actual = _tree_schema(tree)
    if actual == expected:
        return None
    return f"schema is {actual}, not the indexed {expected}"


def _scan_tree(tree: Any, payload: set[str]) -> list[tuple[str, _PayloadHealth]]:
    """Decode all branches in a tree and return health for its payload branches."""
    if tree.unreadable:
        raise ValueError(f"has unreadable branches {tree.unreadable}")
    health: list[tuple[str, _PayloadHealth]] = []
    for name in tree.readable():
        branch = tree[name]
        if branch.num_entries != tree.num_entries:
            raise ValueError(
                f"branch {name} has {branch.num_entries} entries, tree has {tree.num_entries}"
            )
        state = _PayloadHealth() if name in payload and tree.num_entries else None
        _scan_branch(branch, tree.num_entries, state)
        if state is not None:
            health.append((name, state))
    return health


def _sentinel_problem(health: Sequence[tuple[str, _PayloadHealth]]) -> str | None:
    """Reject a physical file in which every ML payload branch is sentinel-only."""
    bad = [(name, state.problem()) for name, state in health]
    if any(problem is None for _name, problem in bad):
        return None
    details = ", ".join(f"{name}: {problem}" for name, problem in bad[:4])
    suffix = f", and {len(bad) - 4} more" if len(bad) > 4 else ""
    return f"every payload branch is sentinel-only ({details}{suffix})"


def _scan_indexed_trees(
    path: Path, back: Any, names: Sequence[str], expected: Mapping[str, Any]
) -> tuple[str | None, list[tuple[str, _PayloadHealth]]]:
    """Schema-check and exhaustively decode the indexed trees in one open file."""
    health: list[tuple[str, _PayloadHealth]] = []
    for name in names:
        tree = back[name]
        problem = _schema_problem(tree, expected.get(name))
        if problem is not None:
            return f"tree {name} {problem}", health
        _log.info("verifying %s tree %s: %d entries", path.name, name, tree.num_entries)
        for branch, state in _scan_tree(tree, set(_payload_branches(tree))):
            health.append((f"{name}.{branch}", state))
    return None, health


def _verify_root_payload(path: Path, made: Mapping[str, Any]) -> str | None:
    """Check schemas, decode every basket, and reject an empty or sentinel-only file."""
    expected_schemas = made.get("schemas")
    if expected_schemas is None:
        return "the index has no branch schemas; rerun xrd-datasets build once to refresh it"
    with open_root(str(path)) as back:
        names = back.trees()
        actual_trees = {name: back[name].num_entries for name in names}
        if actual_trees != made["trees"]:
            return f"the trees are {actual_trees}, not the indexed {made['trees']}"
        problem, health = _scan_indexed_trees(path, back, names, expected_schemas)
        if problem is not None:
            return problem
    if not sum(actual_trees.values()):
        return "the ROOT file contains no data rows"
    if not health:
        return "the ROOT file contains no readable payload branches"
    return _sentinel_problem(health)


def _show_verification(
    entries: Sequence[dict[str, Any]], problems: dict[str, str], args: argparse.Namespace
) -> None:
    """Render verification results for people or scripts."""
    if args.json:
        print(dumps({"checked": len(entries), "problems": problems}))
        return
    for name, what in problems.items():
        print(f"{name}: {what}", file=sys.stderr)
    if not args.quiet:
        print(
            f"{len(entries) - len(problems)} of {len(entries)} files match the index "
            "and load completely"
        )


# ---------------------------------------------------------------------------
# The site
# ---------------------------------------------------------------------------


def _root_url(base: str, given: str | None) -> str:
    """Where the same directory answers to ``root://``.

    A site nearly always serves both planes off one host, so the default is
    that host with the scheme swapped; ``--root-url`` is for the deployment
    where the native protocol lives somewhere else, behind its own name or
    port.
    """
    if given:
        return given.rstrip("/")
    host = base.split("://", 1)[-1].split("/", 1)[0]
    return f"root://{host}"


def _human_decimal(size: int) -> str:
    value = float(size)
    for unit in ("B", "kB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{size} B"  # pragma: no cover


def _credit(made: dict[str, Any]) -> str:
    """A short, honest credit which never promotes an archive host to author."""
    creators = made.get("creators") or []
    if creators:
        return "Creators: " + ", ".join(html.escape(str(name)) for name in creators)
    publisher = made.get("publisher")
    if publisher:
        return "Publisher: " + html.escape(str(publisher))
    return "Creator attribution: see linked dataset record"


def _origin_link(made: dict[str, Any]) -> str:
    origin = html.escape(made.get("origin") or made["source"], quote=True)
    label = "Canonical origin" if made.get("origin_kind") == "canonical" else "Dataset record"
    return f'<a href="{origin}" rel="noopener">{label}</a>'


def _repository_link(made: dict[str, Any]) -> str:
    origin = html.escape(made.get("origin") or made["source"], quote=True)
    source = html.escape(made["source"], quote=True)
    repository = html.escape(made.get("repository") or "Source repository")
    if source != origin:
        return f'<a href="{source}" rel="noopener">Source: {repository}</a>'
    return f"<span>Repository: {repository}</span>"


def _mirror_links(made: dict[str, Any]) -> list[str]:
    links = []
    for mirror in made.get("mirrors") or []:
        name = html.escape(str(mirror["name"]))
        url = html.escape(str(mirror["url"]), quote=True)
        links.append(f'<a href="{url}" rel="noopener">Mirror: {name}</a>')
    return links


def _citation_link(made: dict[str, Any]) -> str:
    citation = made.get("citation")
    if citation and str(citation).startswith(("http://", "https://")):
        url = html.escape(str(citation), quote=True)
        return f'<a href="{url}" rel="cite noopener">Citation</a>'
    return ""


def _provenance_links(made: dict[str, Any], *, parent: str = " · ") -> str:
    """Canonical origin, serving repository, explicit mirrors, and citation links."""
    links = [_origin_link(made), _repository_link(made), *_mirror_links(made)]
    links.extend(filter(None, (_citation_link(made),)))
    return parent.join(links)


def _card_download(made: dict[str, Any], name: str, detail: str) -> str:
    """A direct download for one file, or an honest link to every shard."""
    files = made.get("files")
    if files:
        return f'<a class="button primary" href="{detail}">{len(files)} ROOT shards</a>'
    download = html.escape(made.get("download") or made["file"], quote=True)
    return f'<a class="button primary" href="{download}" download>Download {name}.root</a>'


def _catalogue_cards(entries: Sequence[dict[str, Any]]) -> str:
    cards = []
    for made in entries:
        name = html.escape(made["name"])
        title = html.escape(made["title"])
        terms = html.escape(made.get("licence_url", ""), quote=True)
        licence = html.escape(made["licence"])
        transformation = html.escape(made.get("transformation", ""))
        detail = f"datasets/{quote(made['name'])}.html"
        modality = html.escape(made.get("modality", "dataset"))
        task = html.escape(made.get("task", "machine learning"))
        searchable = html.escape(
            " ".join(
                str(made.get(key, ""))
                for key in (
                    "name",
                    "title",
                    "licence",
                    "modality",
                    "task",
                    "transformation",
                    "creators",
                    "publisher",
                    "repository",
                )
            ).lower(),
            quote=True,
        )
        licence_link = (
            f'<a href="{terms}" rel="license">{licence}</a>' if terms else f"<span>{licence}</span>"
        )
        cards.append(
            f'''<article class="dataset-card" data-search="{searchable}">
  <div class="card-top"><span class="pill">{modality}</span><span class="size">{_human_decimal(made["bytes"])}</span></div>
  <h3><a href="{detail}">{name}</a></h3>
  <p class="card-title">{title}</p>
  <p class="credit">{_credit(made)}</p>
  <div class="tags"><span>{task}</span><span>{made["rows"]:,} rows</span></div>
  <p class="transform">{transformation}</p>
  <div class="provenance"><span>{_provenance_links(made, parent=" · ")}</span>{licence_link}</div>
  <div class="card-actions">{_card_download(made, name, detail)}<a class="button" href="{detail}">Details</a></div>
</article>'''
        )
    return "\n".join(cards)


def _json_value(made: dict[str, Any], preferred: str, fallback: str) -> Any:
    return made.get(preferred) or made[fallback]


def _json_same_as(made: dict[str, Any]) -> list[str]:
    return [made["source"], *(mirror["url"] for mirror in made.get("mirrors") or [])]


def _json_publisher(name: Any) -> dict[str, str] | None:
    return {"@type": "Organization", "name": str(name)} if name else None


def _json_credits(made: dict[str, Any]) -> dict[str, Any]:
    creators = [{"@type": "Person", "name": name} for name in made.get("creators", [])]
    values = (
        ("creator", creators),
        ("publisher", _json_publisher(made.get("publisher"))),
        ("citation", made.get("citation")),
    )
    return {key: value for key, value in values if value}


def _dataset_json_ld(base: str, made: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {
        "@type": "Dataset",
        "name": made["title"],
        "alternateName": made["name"],
        "url": f"{base}/datasets/{quote(made['name'])}.html",
        "isBasedOn": _json_value(made, "origin", "source"),
        "license": _json_value(made, "licence_url", "licence"),
        "description": made.get("transformation", ""),
        "keywords": [made.get("modality", "dataset"), made.get("task", "machine learning")],
        "distribution": [_json_distribution(base, file) for file in _entry_files(made)],
    }
    item.update(_json_credits(made))
    item["sameAs"] = _json_same_as(made)
    return item


def _json_distribution(base: str, file: dict[str, Any]) -> dict[str, Any]:
    """Schema.org metadata for one physical ROOT distribution."""
    item = {
        "@type": "DataDownload",
        "contentUrl": f"{base}/{_json_value(file, 'download', 'file')}",
        "encodingFormat": "application/x-root",
        "contentSize": file["bytes"],
    }
    if file.get("split"):
        item["name"] = str(file["split"])
    return item


def _catalogue_json_ld(title: str, base: str, built: str, entries: Sequence[dict[str, Any]]) -> str:
    document = {
        "@context": "https://schema.org",
        "@type": "DataCatalog",
        "name": title,
        "url": f"{base}/",
        "dateModified": built,
        "description": "Open machine-learning datasets converted to ROOT and streamed by PyXRootD.",
        "dataset": [_dataset_json_ld(base, made) for made in entries],
    }
    return json.dumps(document, separators=(",", ":")).replace("</", "<\\/")


def _citation_detail(made: dict[str, Any]) -> str:
    citation = str(made.get("citation") or "")
    if not citation or citation.startswith(("http://", "https://")):
        return ""
    return f"<section><h2>Published citation or credit</h2><p>{html.escape(citation)}</p></section>"


def _detail_downloads(made: dict[str, Any], name: str) -> str:
    """Download buttons for every physical file behind a catalogue entry."""
    links = []
    for file in _entry_files(made):
        download = html.escape(file.get("download") or file["file"], quote=True)
        label = html.escape(str(file.get("split") or f"{name}.root"))
        links.append(f'<a class="download" href="../{download}" download>{label}</a>')
    return "".join(links)


def _load_example(made: dict[str, Any], name: str) -> str:
    """A catalogue load expression, selecting a shard when one is required."""
    files = made.get("files") or []
    option = f', split="{files[0]["split"]}"' if files else ""
    return f'xrd.ml.load("{name}"{option})'


def _detail_page(title: str, base: str, made: dict[str, Any]) -> str:
    name = html.escape(made["name"])
    heading = html.escape(made["title"])
    licence_url = html.escape(made.get("licence_url", ""), quote=True)
    licence = html.escape(made["licence"])
    transformation = html.escape(made.get("transformation", ""))
    canonical = f"{base}/datasets/{quote(made['name'])}.html"
    structured = _catalogue_json_ld(title, base, "", [made])
    terms = f'<a href="{licence_url}" rel="license">{licence}</a>' if licence_url else licence
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{heading} as a ROOT dataset | {html.escape(title)}</title>
<meta name="description" content="Download and stream {heading} as a provenance-rich ROOT file with PyXRootD.">
<meta name="robots" content="index,follow"><link rel="canonical" href="{canonical}">
<meta property="og:type" content="website"><meta property="og:title" content="{heading} as ROOT">
<meta property="og:url" content="{canonical}"><script type="application/ld+json">{structured}</script>
<style>{_DETAIL_STYLE}</style></head><body><main>
<a class="back" href="../index.html">← All datasets</a><p class="eyebrow">PyXRootD dataset archive</p>
<h1>{heading}</h1><p class="name">Catalogue name: <code>{name}</code></p>
<dl><div><dt>Modality</dt><dd>{html.escape(made.get("modality", "dataset"))}</dd></div>
<div><dt>ML task</dt><dd>{html.escape(made.get("task", "machine learning"))}</dd></div>
<div><dt>Published source payload</dt><dd>{_human_decimal(made.get("source_bytes", 0))}</dd></div>
<div><dt>ROOT result</dt><dd>{_human_decimal(made["bytes"])}, {made["rows"]:,} rows</dd></div>
<div><dt>Creator credit</dt><dd>{_credit(made)}</dd></div>
<div><dt>Source repository</dt><dd>{html.escape(made.get("repository") or "See source")}</dd></div>
<div><dt>Canonical licence</dt><dd>{terms}</dd></div></dl>
<section><h2>Transformation into ROOT</h2><p>{transformation}</p></section>
<div class="actions">{_detail_downloads(made, name)}
{_provenance_links(made)}</div>
{_citation_detail(made)}
<section><h2>Stream it with PyXRootD</h2><pre><code>export XRD_CATALOGUE={html.escape(base)}
python -c 'import xrd.ml; print({_load_example(made, name)})'</code></pre></section>
</main></body></html>'''


def _site(args: argparse.Namespace, config: Config) -> int:
    out = Path(args.directory)
    index = json.loads((out / "index.json").read_text())
    base = args.base_url.rstrip("/") if args.base_url else "https://data.example.org"
    entries = index["datasets"]
    description = (
        "Open machine-learning datasets converted to ROOT files, with canonical licences, "
        "provenance and fast remote streaming through PyXRootD."
    )
    has_oversize = any(made.get("source_bytes", 0) >= SOURCE_SIZE_CEILING for made in entries)
    values = {
        "title": args.title,
        "description": description,
        "base_url": base,
        "canonical": f"{base}/",
        "root_url": _root_url(base, args.root_url),
        "built": index["built"],
        "count": str(len(entries)),
        "plural": "" if len(entries) == 1 else "s",
        "source_total": _human_decimal(sum(made.get("source_bytes", 0) for made in entries)),
        "size_policy_value": "No cap" if has_oversize else "&lt; 2 GB",
        "size_policy_label": (
            "explicit oversized build" if has_oversize else "default per-dataset ceiling"
        ),
        "cards": _catalogue_cards(entries),
        "json_ld": _catalogue_json_ld(args.title, base, index["built"], entries),
        # ``</`` would end the page's own script block early if a title ever
        # contained it; JSON does not need the slash, so it goes.
        "payload": json.dumps(entries).replace("</", "<\\/"),
        "root": str(out.resolve()),
    }
    written = []
    for filename, template in _SITE_FILES.items():
        (out / filename).write_text(string.Template(template).substitute(values))
        written.append(filename)
    details = out / "datasets"
    details.mkdir(exist_ok=True)
    for made in entries:
        filename = f"datasets/{made['name']}.html"
        (out / filename).write_text(_detail_page(args.title, base, made))
        written.append(filename)
    urls = [f"{base}/", *(f"{base}/datasets/{quote(made['name'])}.html" for made in entries)]
    sitemap = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    sitemap += "".join(f"  <url><loc>{html.escape(url)}</loc></url>\n" for url in urls)
    sitemap += "</urlset>\n"
    (out / "sitemap.xml").write_text(sitemap)
    (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n")
    written.extend(("sitemap.xml", "robots.txt"))
    if args.json:
        print(dumps({"written": written}))
    elif not args.quiet:
        print(f"wrote {', '.join(written)} in {out}")
    return OK


_PAGE = """\
<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<style>
  :root { color-scheme: light dark; --line: #8884; }
  body { font: 16px/1.5 system-ui, sans-serif; max-width: 90rem;
         margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.6rem; }
  code, pre { font: 0.85rem/1.5 ui-monospace, monospace; }
  pre { border: 1px solid var(--line); border-radius: 6px; padding: 0.8rem;
        overflow-x: auto; }
  input { font: inherit; width: 100%; padding: 0.4rem 0.6rem; margin: 1rem 0;
          border: 1px solid var(--line); border-radius: 6px;
          background: transparent; color: inherit; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 0.35rem 0.6rem;
           border-bottom: 1px solid var(--line); vertical-align: top; }
  td.n { text-align: right; white-space: nowrap; }
  .muted { opacity: 0.65; font-size: 0.85rem; }
  .dataset { min-width: 12rem; }
  .provenance { min-width: 11rem; }
  .transform { min-width: 22rem; }
  .download { min-width: 12rem; }
  .block { display: block; }
  .lede { font-size: 1.05rem; }
  h2 { font-size: 1.15rem; margin-top: 2rem; }
  .ways td { vertical-align: top; }
  .ways td:first-child { white-space: nowrap; font-weight: 600; }
  .ways pre { margin: 0.4rem 0 0; }
</style>
<body>
<h1>$title</h1>
<p class="lede">$count dataset$plural, converted to ROOT files and streamed over
<b>XRootD</b> &mdash; the high-performance data-access protocol built in
high-energy physics, and the one the OSG and the WLCG move petabytes of
physics data over every day, across hundreds of sites worldwide. This is that
machinery pointed at machine-learning data: the same protocol, the same
client, the same wide-area performance.</p>

<p>Stream one straight into a training loop. Nothing is downloaded first:</p>
<pre>pip install pyxrootdclient
export XRD_CATALOGUE=$base_url

python -c '
import xrd.ml
data = xrd.ml.load("mnist")
for images, labels in data.train.batches(256):
    ...'</pre>

<p>A minibatch is a read of the baskets it needs and nothing else, so the loop
starts at the first batch rather than at the end of a download, and a dataset
larger than the machine is not a problem. Reading the same data over and over,
or from further away than you would like? <code>xrd.ml.load("mnist",
cache=True)</code> pulls the file once into <code>~/.cache/xrd</code>, checks
it against this catalogue, and reads from your own disk ever after.</p>

<h2>Three ways to the same bytes</h2>
<table class="ways">
<tbody>
<tr><td><code>root://</code></td>
    <td>The native protocol: parallel, resumable, vector reads, checksums on
    the wire. Public and read-only.
    <pre>xrd.ml.load("$root_url//mnist.root")</pre></td></tr>
<tr><td><code>https://</code></td>
    <td>Range requests, for anything that speaks HTTP &mdash; a browser, a
    notebook, <code>curl</code>, a batch node behind a proxy.
    <pre>xrd.ml.load("$base_url/mnist.root")</pre></td></tr>
<tr><td>browser</td>
    <td>Every dataset row below has a direct ROOT download. Click one and you
    have the file; nothing here needs a login, an account or a token.</td></tr>
</tbody>
</table>

<p class="muted">Served by <a href="https://github.com/rob-c/PyXRootDClient"
>PyXRootDClient</a> against a BriX-Cache endpoint, read-only on every plane.
Each file carries its origin, licence terms and conversion summary in its
<code>about</code> key; the same is recorded in <a href="index.json"
>index.json</a>, which is how the name lookup above works. The table links to
the publisher, the canonical licence terms, a summary of the conversion, and
the resulting ROOT download. Built $built.</p>
<input id="q" type="search"
       placeholder="filter by name, title, origin, licence or transformation"
       aria-label="filter">
<table>
<thead><tr><th>dataset</th><th>origin &amp; canonical licence</th>
<th>transformation into ROOT</th><th>ROOT download</th></tr></thead>
<tbody id="rows"></tbody>
</table>
<script>
const DATASETS = $payload;
const human = n => {
  for (const unit of ["B", "KiB", "MiB", "GiB"]) {
    if (n < 1024 || unit === "GiB") return n.toFixed(unit === "B" ? 0 : 1) + " " + unit;
    n /= 1024;
  }
};
const rows = document.getElementById("rows");
const show = wanted => {
  rows.textContent = "";
  for (const d of DATASETS) {
    const text = (d.name + " " + d.title + " " + (d.origin || d.source) + " " +
                  d.licence + " " +
                  (d.transformation || "")).toLowerCase();
    if (wanted && !text.includes(wanted)) continue;
    const tr = rows.insertRow();
    const dataset = tr.insertCell();
    dataset.className = "dataset";
    const name = document.createElement("strong");
    name.className = "block";
    name.textContent = d.name;
    dataset.appendChild(name);
    const title = document.createElement("span");
    title.textContent = d.title;
    dataset.appendChild(title);

    const provenance = tr.insertCell();
    provenance.className = "provenance";
    const origin = document.createElement("a");
    origin.href = d.origin || d.source;
    origin.textContent = d.origin_kind === "canonical" ? "Canonical origin" : "Dataset record";
    origin.className = "block";
    provenance.appendChild(origin);
    const terms = d.licence_url ? document.createElement("a") : document.createElement("span");
    if (d.licence_url) terms.href = d.licence_url;
    terms.textContent = d.licence;
    terms.className = "block muted";
    provenance.appendChild(terms);

    const transformation = tr.insertCell();
    transformation.className = "transform";
    transformation.textContent = d.transformation || "Converted to ROOT; no summary recorded.";

    const result = tr.insertCell();
    result.className = "download";
    for (const file of (d.files || [d])) {
      const download = document.createElement("a");
      download.href = file.download || file.file;
      download.download = file.download || file.file;
      download.textContent = "Download " + (file.split || file.download || file.file);
      download.className = "block";
      result.appendChild(download);
    }
    const detail = document.createElement("span");
    detail.className = "muted";
    detail.textContent = d.rows.toLocaleString() + " rows · " + human(d.bytes);
    result.appendChild(detail);
  }
};
document.getElementById("q").addEventListener("input",
  event => show(event.target.value.trim().toLowerCase()));
show("");
</script>
</body>
</html>
"""

_DETAIL_STYLE = """
:root{color-scheme:dark;--ink:#ecfdf9;--muted:#9bb8b4;--panel:#102a2d;--line:#31575a;--aqua:#52e4c4;--gold:#ffc857}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 80% 0,#17494c 0,transparent 38%),#071719;color:var(--ink);font:16px/1.65 Inter,ui-sans-serif,system-ui,sans-serif}
main{width:min(920px,calc(100% - 2rem));margin:0 auto;padding:4rem 0 6rem}.back,a{color:var(--aqua)}.eyebrow{text-transform:uppercase;letter-spacing:.18em;color:var(--gold);font-weight:800;margin-top:4rem}h1{font-size:clamp(2.4rem,7vw,5.5rem);line-height:.96;margin:.4rem 0 1rem;letter-spacing:-.05em}.name{color:var(--muted)}code,pre{font:14px/1.6 ui-monospace,SFMono-Regular,monospace}pre{padding:1.2rem;border:1px solid var(--line);border-radius:18px;background:#061214;overflow:auto}dl{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:18px;overflow:hidden;margin:3rem 0}dl div{background:var(--panel);padding:1rem}dt{color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.1em}dd{margin:.3rem 0 0;font-weight:700}section{margin:3rem 0}.actions{display:flex;gap:1rem;align-items:center;flex-wrap:wrap}.download{background:var(--aqua);color:#03201b;text-decoration:none;font-weight:900;padding:.9rem 1.2rem;border-radius:99px}
"""

_PAGE_V2 = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>$title | Open ML datasets streamed as ROOT</title>
<meta name="description" content="$description"><meta name="robots" content="index,follow,max-image-preview:large">
<meta name="keywords" content="machine learning datasets, ROOT files, XRootD, PyTorch, open science, physics datasets">
<link rel="canonical" href="$canonical"><meta name="theme-color" content="#071719">
<meta property="og:type" content="website"><meta property="og:title" content="$title">
<meta property="og:description" content="$description"><meta property="og:url" content="$canonical">
<meta name="twitter:card" content="summary_large_image"><script type="application/ld+json">$json_ld</script>
<style>
:root{color-scheme:dark;--bg:#061416;--ink:#effffb;--muted:#96b8b3;--panel:#0d292c;--panel2:#103438;--line:#285255;--aqua:#51e5c3;--gold:#ffc857;--coral:#ff7869;--shadow:0 24px 80px #0008}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:radial-gradient(circle at 76% -8%,#1a5e5b 0,transparent 32%),radial-gradient(circle at 5% 30%,#35244e 0,transparent 24%),var(--bg);color:var(--ink);font:16px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}a{color:inherit}header,main,footer{width:min(1240px,calc(100% - 2rem));margin-inline:auto}.nav{display:flex;align-items:center;justify-content:space-between;padding:1.2rem 0}.brand{font-weight:900;letter-spacing:-.03em;text-decoration:none}.brand i{color:var(--aqua);font-style:normal}.nav-links{display:flex;gap:1rem;color:var(--muted);font-size:.9rem}.nav-links a{text-decoration:none}.hero{padding:5rem 0 3rem;display:grid;grid-template-columns:1.08fr .92fr;gap:3rem;align-items:center}.eyebrow{text-transform:uppercase;letter-spacing:.2em;color:var(--gold);font-size:.77rem;font-weight:900}.hero h1{font-size:clamp(3.5rem,7.7vw,7.2rem);line-height:.86;letter-spacing:-.07em;margin:.7rem 0 1.5rem;max-width:9ch}.gradient{background:linear-gradient(100deg,var(--aqua),#8cbcff 56%,var(--gold));-webkit-background-clip:text;background-clip:text;color:transparent}.lede{max-width:62ch;color:#c3d9d5;font-size:1.13rem}.metrics{display:flex;gap:2rem;margin:2rem 0;flex-wrap:wrap}.metric strong{display:block;font-size:1.45rem}.metric span{color:var(--muted);font-size:.82rem}.hero-actions,.card-actions{display:flex;gap:.7rem;flex-wrap:wrap}.button{display:inline-flex;align-items:center;justify-content:center;padding:.75rem 1rem;border:1px solid var(--line);border-radius:999px;text-decoration:none;font-weight:800;font-size:.88rem;background:#ffffff08}.button.primary{background:var(--aqua);color:#03201b;border-color:var(--aqua)}.terminal{background:#051011d9;border:1px solid #3c6668;border-radius:24px;box-shadow:var(--shadow);overflow:hidden;transform:rotate(1deg)}.terminal-bar{padding:.8rem 1rem;background:#ffffff09;color:var(--muted);font-size:.78rem}.dots{color:var(--coral);letter-spacing:.25em}.terminal pre{margin:0;padding:1.3rem;max-height:540px;overflow:auto;font:12.5px/1.6 ui-monospace,SFMono-Regular,monospace;color:#d8fff6}.terminal .comment{color:#79a49e}.section{padding:5rem 0}.section-head{display:flex;justify-content:space-between;gap:2rem;align-items:end;margin-bottom:2rem}.section h2{font-size:clamp(2.2rem,5vw,4rem);line-height:1;letter-spacing:-.05em;margin:0}.section-copy{color:var(--muted);max-width:55ch}.protocols{display:grid;grid-template-columns:repeat(3,1fr);gap:1rem}.protocol{padding:1.4rem;border:1px solid var(--line);border-radius:20px;background:linear-gradient(145deg,#153638aa,#0b2426aa)}.protocol b{color:var(--aqua);font:800 1rem ui-monospace,monospace}.protocol p{color:var(--muted)}.protocol code{font-size:.78rem;word-break:break-all}.catalogue-tools{position:sticky;top:.7rem;z-index:3;padding:.7rem;background:#061416df;backdrop-filter:blur(15px);border:1px solid var(--line);border-radius:18px;margin:2rem 0}.catalogue-tools input{width:100%;padding:1rem 1.2rem;border:0;background:transparent;color:var(--ink);font:inherit;outline:none}.dataset-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1rem}.dataset-card{display:flex;flex-direction:column;padding:1.25rem;border:1px solid var(--line);border-radius:22px;background:linear-gradient(155deg,#123438e8,#091d20e8);min-height:390px;transition:transform .2s,border-color .2s}.dataset-card:hover{transform:translateY(-4px);border-color:var(--aqua)}.card-top,.provenance{display:flex;justify-content:space-between;gap:.7rem;align-items:center}.pill,.tags span{padding:.27rem .55rem;border-radius:999px;background:#51e5c31b;color:var(--aqua);font-size:.73rem}.size{font-size:.75rem;color:var(--muted)}.dataset-card h3{font:800 1.15rem ui-monospace,monospace;margin:1.1rem 0 .25rem}.dataset-card h3 a{text-decoration:none}.card-title{font-weight:750;margin:.2rem 0 .8rem}.tags{display:flex;gap:.4rem;flex-wrap:wrap}.tags span{background:#fff1;color:#c7dcd8}.transform{color:var(--muted);font-size:.86rem;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}.provenance{font-size:.78rem;margin-top:auto;padding:1rem 0}.provenance a{color:var(--gold)}.card-actions .button{font-size:.75rem;padding:.58rem .72rem}.empty{display:none;text-align:center;color:var(--muted);padding:3rem}.index-note{font-size:.82rem;color:var(--muted);margin-top:2rem}footer{padding:4rem 0;border-top:1px solid var(--line);color:var(--muted);display:flex;justify-content:space-between;gap:2rem}code{font-family:ui-monospace,SFMono-Regular,monospace}@media(max-width:980px){.hero{grid-template-columns:1fr;padding-top:3rem}.terminal{transform:none}.dataset-grid{grid-template-columns:repeat(2,1fr)}.protocols{grid-template-columns:1fr}}@media(max-width:620px){.nav-links{display:none}.dataset-grid{grid-template-columns:1fr}.section-head,footer{display:block}.hero h1{font-size:4rem}.metrics{gap:1rem}.metric{min-width:42%}}
</style>
</head><body>
<header><nav class="nav"><a class="brand" href="#top">Py<i>XRootD</i> / Open Data</a><div class="nav-links"><a href="#quickstart">Quick start</a><a href="#catalogue">Datasets</a><a href="index.json">JSON API</a><a href="sitemap.xml">Sitemap</a></div></nav>
<section class="hero" id="top"><div><p class="eyebrow">Open science · streamed at physics scale</p><h1>Train on data <span class="gradient">without waiting.</span></h1><p class="lede">$count open machine-learning dataset$plural converted into provenance-rich ROOT files and served by PyXRootD. Start at the first minibatch, stream only the baskets you need, and keep the original source and canonical licence one click away.</p><div class="metrics"><div class="metric"><strong>$count</strong><span>ready-to-stream datasets</span></div><div class="metric"><strong>$source_total</strong><span>published source payload</span></div><div class="metric"><strong>$size_policy_value</strong><span>$size_policy_label</span></div></div><div class="hero-actions"><a class="button primary" href="#quickstart">Train a classifier</a><a class="button" href="#catalogue">Explore datasets</a></div></div>
<div class="terminal" id="quickstart"><div class="terminal-bar"><span class="dots">● ● ●</span> &nbsp; from empty venv to a PyTorch classifier</div><pre><code><span class="comment"># 1. Create an isolated environment</span>
python3 -m venv .venv
source .venv/bin/activate

<span class="comment"># 2. Install PyXRootD and PyTorch</span>
python -m pip install pyxrootdclient torch
export XRD_CATALOGUE=$base_url

<span class="comment"># 3. Stream Fashion-MNIST and train</span>
python - &lt;&lt;'PY'
import torch
from torch import nn
import xrd.ml

data = xrd.ml.load("fashion_mnist")
model = nn.Sequential(
    nn.Flatten(), nn.Linear(28 * 28, 128),
    nn.ReLU(), nn.Linear(128, 10),
)
optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
loss_fn = nn.CrossEntropyLoss()

for images, labels in data.train.batches(256):
    logits = model(images.float())
    loss = loss_fn(logits, labels.long())
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()

print(f"last minibatch loss: {loss.item():.3f}")
PY</code></pre></div></section></header>
<main><section class="section"><div class="section-head"><div><p class="eyebrow">One archive, three routes</p><h2>Move less. Begin sooner.</h2></div><p class="section-copy">ROOT baskets let a training loop fetch selected columns and minibatches rather than copying a monolithic archive. XRootD is the wide-area data layer used across the WLCG and OSG; here PyXRootD points that machinery at ML. Add <code>cache=True</code> when repeated epochs should pull the file into <code>~/.cache/xrd</code> once.</p></div><div class="protocols"><article class="protocol"><b>root:// native</b><p>Parallel, resumable vector reads and checksums over the protocol built for globally distributed HEP analysis.</p><code>xrd.ml.load("$root_url//mnist.root")</code></article><article class="protocol"><b>https:// ranges</b><p>Works through browsers, notebooks and ordinary proxies, while nginx handles byte-range reads.</p><code>xrd.ml.load("$base_url/mnist.root")</code></article><article class="protocol"><b>catalogue lookup</b><p>Use a stable dataset name; <a href="index.json">index.json</a> resolves the file and records its checksum and provenance.</p><code>xrd.ml.load("mnist", cache=True)</code></article></div></section>
<section class="section" id="catalogue"><div class="section-head"><div><p class="eyebrow">The catalogue</p><h2>Open data, inspectable lineage.</h2></div><p class="section-copy">Every result separates its canonical origin, or best available dataset record, and credited creators from the repository or mirror serving the registered bytes. It also links the canonical licence, exact transformation into ROOT, and direct download. The cards are rendered in HTML for people and indexers; search only hides what is already on the page.</p></div><div class="catalogue-tools"><input id="q" type="search" placeholder="Search name, creator, repository, task, licence or transformation…" aria-label="Filter datasets"></div><div class="dataset-grid" id="cards">$cards</div><p class="empty" id="empty">No dataset matches that search.</p><p class="index-note">Built $built · Machine-readable metadata: <a href="index.json">index.json</a> · Google-compatible <a href="sitemap.xml">sitemap</a> · every ROOT file includes the same provenance in its <code>about</code> key.</p></section></main>
<footer><strong>PyXRootDClient</strong><span>Pure-Python access to XRootD, HTTPS ranges and ROOT data for training anywhere.</span><a href="https://github.com/rob-c/PyXRootDClient">Source on GitHub</a></footer>
<script>const q=document.getElementById("q"),cards=[...document.querySelectorAll(".dataset-card")],empty=document.getElementById("empty");q.addEventListener("input",()=>{const wanted=q.value.trim().toLowerCase();let shown=0;cards.forEach(card=>{const yes=!wanted||card.dataset.search.includes(wanted);card.hidden=!yes;if(yes)shown+=1});empty.style.display=shown?"none":"block"});</script>
</body></html>
"""

_NGINX = """\
# $title - static hosting for any stock nginx.
#
# Drop this into /etc/nginx/conf.d/ (adjust the port and server_name) and the
# directory serves as it stands: the page, the index, and the files, with the
# range requests xrd.ml relies on handled by nginx itself.
server {
    listen 8080;
    server_name _;

    root $root;
    index index.html;
    charset utf-8;

    types {
        text/html html;
        application/json json;
        application/xml xml;
        text/plain txt;
        application/x-root root;
    }
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;

    # Large source archives may be retained here for resumable rebuilds. They
    # are inputs, not published datasets, even when somebody guesses the path.
    location ~ (^|/)\\. { deny all; }

    location / {
        # Training loops read from browsers, notebooks and batch nodes alike.
        add_header Access-Control-Allow-Origin * always;
        add_header X-Content-Type-Options nosniff always;
        # ROOT files are already compressed; recompressing wastes the CPU.
        gzip off;
        try_files $$uri $$uri/ =404;
    }

    location ~ \\.root$$ {
        add_header Access-Control-Allow-Origin * always;
        add_header Accept-Ranges bytes always;
        add_header X-Content-Type-Options nosniff always;
        expires 7d;
        gzip off;
    }
}
"""

_BRIX = """\
# $title - the same directory over root://, WebDAV and S3, via BriX.
#
# A complete configuration for a BriX-built nginx (nginx-xrootd). Read-only
# on every plane: nothing here takes a write. Start it with
#     nginx -c $root/brix.conf
# or install xrd-datasets.service alongside this file.
worker_processes auto;
daemon off;
error_log stderr info;
pid /run/xrd-datasets-nginx.pid;

events { worker_connections 1024; }

http {
    access_log off;

    # The browsable page and plain HTTP downloads.
    server {
        listen 8080;
        root $root;
        index index.html;
        charset utf-8;
        types { text/html html; application/json json; application/xml xml;
                text/plain txt; application/x-root root; }
        location / {
            add_header Access-Control-Allow-Origin * always;
            add_header X-Content-Type-Options nosniff always;
            gzip off;
        }
        location ~ \\.root$$ {
            add_header Access-Control-Allow-Origin * always;
            add_header Accept-Ranges bytes always;
            add_header X-Content-Type-Options nosniff always;
            expires 7d;
            gzip off;
        }
    }

    # WebDAV, for davs:// clients and anything that PROPFINDs.
    server {
        listen 8008;
        location / {
            brix_webdav          on;
            brix_webdav_auth     none;
            brix_storage_backend posix:$root;
        }
    }
}

stream {
    # The native protocol: root://host:1094//mnist.root
    server {
        listen 1094;
        brix_root            on;
        brix_export          $root;
        brix_storage_backend posix:$root;
        brix_auth            none;
    }
}
"""

_UNIT = """\
# $title - serve the datasets directory with BriX.
#
#     cp xrd-datasets.service /etc/systemd/system/
#     systemctl enable --now xrd-datasets
[Unit]
Description=$title
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/sbin/nginx -c $root/brix.conf
Restart=on-failure
# Read-only serving deserves a read-only view of the machine.
ProtectSystem=strict
ReadOnlyPaths=$root
ReadWritePaths=/run
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
"""

_README = """\
# $title

$count machine-learning dataset$plural, converted to ROOT files by
`xrd-datasets build`, indexed in `index.json`, and checksummed in
`MANIFEST`. Every file records its original publisher, canonical licence
terms and the transformation into ROOT in its own `about` key, so that
provenance stays with it wherever it is copied. The web table links those
details directly beside the resulting ROOT download.

They are served over XRootD, the data-access protocol high-energy physics
built for globally distributed analysis and runs across the OSG and the
WLCG. Training data streams the same way the physics does.

## Use it

    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install pyxrootdclient torch
    export XRD_CATALOGUE=$base_url

    python -c 'import xrd.ml; print(xrd.ml.load("iris"))'

Any dataset name in the table on the site works; `index.json` is the
catalogue that resolves it. A whole URL works too, on either plane:

    xrd.ml.load("$root_url//iris.root")     # native, streamed
    xrd.ml.load("$base_url/iris.root")      # HTTP ranges

Nothing is downloaded: a minibatch reads the baskets it needs. To keep a
local copy anyway - the same data read many times, or a slow link -

    xrd.ml.load("iris", cache=True)

pulls it once into `~/.cache/xrd`, checks it against this catalogue, and
reads from disk after that.

## Serve it

* `nginx.conf` - static hosting on any stock nginx, port 8080.
* `brix.conf` - the same directory over root:// (1094), WebDAV (8008) and
  plain HTTP (8080) with a BriX-built nginx.
* `xrd-datasets.service` - the systemd unit that runs the BriX flavour.

## Keep it honest

    xrd-datasets verify $root

reopens every file and compares it, byte for byte, with `index.json`.
Rebuild and redeploy with `xrd-datasets build` + `xrd-datasets site`; a
file that has not changed is kept, not reconverted.

Built $built.
"""

#: What ``site`` writes, next to the files it describes.
_SITE_FILES = {
    "index.html": _PAGE_V2,
    "nginx.conf": _NGINX,
    "brix.conf": _BRIX,
    "xrd-datasets.service": _UNIT,
    "README.md": _README,
}


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Build, check and publish a directory of ML datasets as ROOT files.",
    )
    subs = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name: str, handler: Callable[..., int], help_text: str) -> argparse.ArgumentParser:
        sub = subs.add_parser(name, help=help_text, description=help_text)
        sub.set_defaults(handler=handler)
        common_flags(sub)
        return sub

    def gates(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--only",
            action="append",
            default=[],
            metavar="GLOB",
            help="just the datasets matching this (repeatable)",
        )
        sub.add_argument(
            "--all",
            action="store_true",
            help="include datasets whose licence does not allow redistribution",
        )
        sub.add_argument(
            "--large",
            action="store_true",
            help="select every disk-backed large source, regardless of its origin",
        )
        sub.add_argument(
            "--allow-oversize",
            "--no-size-limit",
            action="store_true",
            help=(
                "opt in to registered datasets at or above 2 GB; requires explicitly "
                "provisioned source, temporary and output storage"
            ),
        )

    listing = command("list", _list, "name every dataset this tool can build")
    gates(listing)

    build = command("build", _build, "convert the datasets and write the index")
    build.add_argument("directory", help="where the files and index.json go")
    gates(build)
    build.add_argument("-j", "--jobs", type=int, default=4, help="conversions in flight at once")
    build.add_argument(
        "-f", "--force", action="store_true", help="reconvert files that are already there"
    )
    build.add_argument(
        "--base", metavar="URL", help="fetch the source data from this mirror instead"
    )
    build.add_argument(
        "--source-cache",
        metavar="DIRECTORY",
        help="retain large source archives here (default: a hidden sibling of DIRECTORY)",
    )
    build.add_argument(
        "--compression",
        default="zlib",
        metavar="NAME",
        help="zlib, lzma, lz4, zstd or none (default zlib)",
    )
    build.add_argument(
        "--diagnostics",
        nargs="?",
        const=30.0,
        type=_diagnostic_seconds,
        metavar="SECONDS",
        help=(
            "emit timestamped phase, row, download and output heartbeats; optional "
            "interval defaults to 30 seconds and SIGUSR1 dumps every Python thread"
        ),
    )

    verify = command(
        "verify", _verify, "checksum, schema-check and fully decode every indexed ROOT file"
    )
    verify.add_argument("directory", help="a directory that build wrote")

    site = command("site", _site, "write the page and the server configuration")
    site.add_argument("directory", help="a directory that build wrote")
    site.add_argument("--base-url", metavar="URL", help="where this directory will be served from")
    site.add_argument(
        "--root-url",
        metavar="URL",
        help="the public root:// endpoint (default: the base URL's host)",
    )
    site.add_argument(
        "--title", default="Open datasets, as ROOT files", help="what the page calls itself"
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if getattr(args, "compression", None) == "none":
        args.compression = None
    config = config_from(args)
    try:
        return int(args.handler(args, config))
    except (XRootDError, OSError, ValueError) as exc:
        return fail(PROGRAM, exc)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
