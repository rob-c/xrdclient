"""Bounded-memory conversion of explicitly licensed Hub Parquet datasets.

The generated :mod:`._hub_tables` declarations record every source shard,
discovery-time byte estimate, split and scalar feature.  Hugging Face's
``refs/convert/parquet`` exports are publisher-managed derived artefacts and
may be regenerated without changing their URL, so this reader treats the
Parquet footer and recorded schema as the integrity boundary rather than
mistaking an old byte count for an immutable checksum.  It scans text columns
once to choose lossless fixed-width ROOT branches, then streams Arrow record
batches into one TTree per publisher split.  Nested media features never reach
this module; the catalogue generator refuses those until they have
purpose-built adapters.
"""

from __future__ import annotations

import importlib
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from ._hub_tables import HUB_OPEN

Rows = Iterator[tuple[int, dict[str, Any]]]
Loaded = tuple[tuple[str, ...], dict[str, Any], Rows]

# Fixed-width byte leaves can represent substantially more than 32 KiB.  Keep
# a defensive per-value ceiling so a corrupt Parquet cell cannot allocate an
# unbounded branch, but admit the multi-megabyte conversations, structures and
# embedded media published by the registered Hub datasets.
TEXT_LIMIT = 16 * 1024 * 1024
_BY_NAME = {item["name"]: item for item in HUB_OPEN}
_COLUMN_ALIASES: dict[str, dict[str, str]] = {
    "hub_alexandrainst_nordjylland_news_summarization": {"text": "document"},
}
_DERIVED_LENGTHS: dict[str, frozenset[str]] = {
    "hub_alexandrainst_nordjylland_news_summarization": frozenset({"text_len", "summary_len"}),
}


def _safe_branches(features: Sequence[Mapping[str, Any]]) -> list[str]:
    """Make source columns and derived length/target columns unambiguous."""
    used = {"index", "label", "target"}
    result = []
    for feature in features:
        base = re.sub(r"[^0-9A-Za-z]+", "_", str(feature["branch"])).strip("_").lower()
        base = base or "field"
        candidate = base
        number = 1
        while candidate in used or (feature["dtype"] == "string" and f"{candidate}_length" in used):
            number += 1
            candidate = f"{base}_{number}"
        result.append(candidate)
        used.add(candidate)
        if feature["dtype"] == "string":
            used.add(f"{candidate}_length")
    return result


def _roles(item: Mapping[str, Any], split: str) -> tuple[str, ...]:
    roles = item["split_roles"].get(split)
    if not roles:
        raise ValueError(f"the Hub dataset {item['label']} has no Parquet shards for {split}")
    return tuple(roles)


def _existing_source(
    source: str, available: set[str], aliases: Mapping[str, str]
) -> str | None:
    if source in available:
        return source
    actual = aliases.get(source)
    if actual in available:
        return actual
    return None


def _resolved_source(
    source: str,
    available: set[str],
    aliases: Mapping[str, str],
    derivable: frozenset[str],
) -> tuple[str | None, bool]:
    actual = _existing_source(source, available, aliases)
    if actual is not None:
        return actual, False
    if source not in derivable:
        return None, False
    base = source.removesuffix("_len")
    actual = _existing_source(base, available, aliases)
    return actual, actual is not None


def _schema_sources(item: Mapping[str, Any]) -> list[str]:
    sources = []
    for feature in item["features"]:
        sources.append(str(feature["source"]))
    return sources


def _resolved_sources(
    sources: Sequence[str],
    available: set[str],
    aliases: Mapping[str, str],
    derivable: frozenset[str],
) -> list[tuple[str, str | None, bool]]:
    resolved = []
    for source in sources:
        resolved.append((source, *_resolved_source(source, available, aliases, derivable)))
    return resolved


def _missing_sources(resolved: Sequence[tuple[str, str | None, bool]]) -> list[str]:
    missing = []
    for source, actual, _derived in resolved:
        if actual is None:
            missing.append(source)
    return missing


def _completed_schema(
    resolved: Sequence[tuple[str, str | None, bool]],
) -> tuple[dict[str, str], frozenset[str]]:
    plan = {}
    derived = set()
    for source, actual, made in resolved:
        plan[source] = str(actual)
        if made:
            derived.add(source)
    return plan, frozenset(derived)


def _schema_plan(
    item: Mapping[str, Any], names: Sequence[str]
) -> tuple[dict[str, str], frozenset[str]]:
    """Resolve registered fields against a compatible publisher schema revision."""
    aliases = _COLUMN_ALIASES.get(item["name"], {})
    derivable = _DERIVED_LENGTHS.get(item["name"], frozenset())
    available = set(names)
    resolved = _resolved_sources(_schema_sources(item), available, aliases, derivable)
    missing = _missing_sources(resolved)
    if missing:
        raise ValueError(
            f"the Hub dataset {item['label']} is missing recorded columns {', '.join(missing)}; "
            f"the shard contains {', '.join(names)}"
        )
    return _completed_schema(resolved)


def _books(
    item: Mapping[str, Any], paths: Mapping[str, Path], split: str
) -> Iterator[tuple[str, Any, dict[str, str], frozenset[str]]]:
    parquet = importlib.import_module("pyarrow.parquet")
    for role in _roles(item, split):
        if role not in paths:
            raise ValueError(f"the Hub dataset {item['label']} is missing its {role} shard")
        book = parquet.ParquetFile(paths[role])
        names = list(book.schema_arrow.names)
        try:
            plan, derived = _schema_plan(item, names)
        except ValueError as error:
            raise ValueError(f"{error} ({role})") from None
        yield role, book, plan, derived


def _derived_text_lengths(values: Sequence[Any], *, dataset: str, field: str) -> list[int]:
    result = []
    for value in values:
        if value is None:
            result.append(-1)
        elif isinstance(value, str):
            result.append(len(value))
        else:
            raise ValueError(f"the Hub dataset {dataset} cannot derive {field} from non-text data")
    return result


def _source_batches(
    item: Mapping[str, Any],
    book: Any,
    sources: Sequence[str],
    plan: Mapping[str, str],
    derived: frozenset[str],
) -> Iterator[tuple[Any, dict[str, Sequence[Any]]]]:
    columns = list(dict.fromkeys(plan[source] for source in sources))
    for batch in book.iter_batches(batch_size=4096, columns=columns):
        raw = batch.to_pydict()
        values: dict[str, Sequence[Any]] = {}
        for source in sources:
            actual = plan[source]
            values[source] = (
                _derived_text_lengths(raw[actual], dataset=item["label"], field=source)
                if source in derived
                else raw[actual]
            )
        yield batch, values


def _encoded(value: Any, *, dataset: str, field: str, index: int) -> bytes:
    if value is None:
        return b""
    if not isinstance(value, str):
        raise ValueError(
            f"row {index} of {dataset} has a non-text value in recorded string field {field}"
        )
    raw = value.encode("utf-8")
    if len(raw) > TEXT_LIMIT:
        raise ValueError(
            f"row {index} of {dataset} has {len(raw)} bytes in {field}, above the "
            f"lossless Hub text limit of {TEXT_LIMIT}"
        )
    return raw


def _text_sources(item: Mapping[str, Any]) -> list[str]:
    result = []
    for feature in item["features"]:
        if feature["dtype"] == "string":
            result.append(str(feature["source"]))
    return result


def _initial_widths(sources: Sequence[str]) -> dict[str, int]:
    result = {}
    for source in sources:
        result[source] = 1
    return result


def _update_text_widths(
    item: Mapping[str, Any],
    columns: Sequence[str],
    widths: dict[str, int],
    values: Mapping[str, Sequence[Any]],
    first: int,
) -> None:
    for name in columns:
        widths[name] = max(widths[name], _batch_text_width(item, name, values[name], first))


def _book_text_widths(
    item: Mapping[str, Any],
    book: Any,
    columns: Sequence[str],
    widths: dict[str, int],
    plan: Mapping[str, str],
    derived: frozenset[str],
    first: int,
) -> int:
    for batch, values in _source_batches(item, book, columns, plan, derived):
        _update_text_widths(item, columns, widths, values, first)
        first += batch.num_rows
    return first


def _text_widths(item: Mapping[str, Any], paths: Mapping[str, Path], split: str) -> dict[str, int]:
    columns = _text_sources(item)
    widths = _initial_widths(columns)
    if not columns:
        return widths
    index = 0
    for _role, book, plan, derived in _books(item, paths, split):
        index = _book_text_widths(item, book, columns, widths, plan, derived, index)
    return widths


def _batch_text_width(
    item: Mapping[str, Any], name: str, values: Sequence[Any], first: int
) -> int:
    width = 1
    for offset, value in enumerate(values):
        raw = _encoded(value, dataset=item["label"], field=name, index=first + offset)
        width = max(width, len(raw))
    return width


def _class(value: Any, feature: Mapping[str, Any], dataset: str, index: int) -> int:
    classes = tuple(str(item) for item in feature["classes"])
    if value is None:
        return -1
    if isinstance(value, str):
        try:
            return classes.index(value)
        except ValueError:
            raise ValueError(
                f"row {index} of {dataset} labels {feature['source']} as {value!r}, and the "
                f"recorded classes are {', '.join(classes)}"
            ) from None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(
            f"row {index} of {dataset} has an invalid ClassLabel in {feature['source']}"
        ) from None
    if result < 0 or result >= len(classes):
        raise ValueError(
            f"row {index} of {dataset} labels {feature['source']} as {result}, and there are "
            f"{len(classes)} recorded classes"
        )
    return result


def _number(value: Any, dtype: str, dataset: str, field: str, index: int) -> int | float:
    if dtype.startswith("float"):
        return _float_number(value, dtype, dataset, field, index)
    if dtype == "bool":
        return _bool_number(value, dtype, dataset, field, index)
    return _integer_number(value, dtype, dataset, field, index)


def _invalid_number(dtype: str, dataset: str, field: str, index: int) -> ValueError:
    return ValueError(f"row {index} of {dataset} has an invalid {dtype} value in {field}")


def _float_number(value: Any, dtype: str, dataset: str, field: str, index: int) -> float:
    if value is None:
        return math.nan
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        raise _invalid_number(dtype, dataset, field, index) from None


def _bool_number(value: Any, dtype: str, dataset: str, field: str, index: int) -> int:
    if value is None:
        return -1
    if isinstance(value, bool):
        return int(value)
    raise _invalid_number(dtype, dataset, field, index)


def _integer_number(value: Any, dtype: str, dataset: str, field: str, index: int) -> int:
    if value is None:
        return -1
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise _invalid_number(dtype, dataset, field, index) from None
    if -(1 << 63) <= result < 1 << 63:
        return result
    raise _invalid_number(dtype, dataset, field, index)


def _columns(
    item: Mapping[str, Any], branches: Sequence[str], widths: Mapping[str, int]
) -> dict[str, Any]:
    columns: dict[str, Any] = {}
    for feature, branch in zip(item["features"], branches):
        dtype = feature["dtype"]
        if dtype == "string":
            columns[branch] = ("B", widths[feature["source"]])
            columns[f"{branch}_length"] = "i"
        elif dtype.startswith("float"):
            columns[branch] = "d"
        elif dtype == "classlabel" or dtype == "bool":
            columns[branch] = "i"
        else:
            columns[branch] = "q"
    target = item["features"][item["target"]]
    columns["label" if target["dtype"] == "classlabel" else "target"] = (
        "i" if target["dtype"] == "classlabel" else "d"
    )
    columns["index"] = "q"
    return columns


def _entries(
    item: Mapping[str, Any],
    paths: Mapping[str, Path],
    split: str,
    branches: Sequence[str],
    widths: Mapping[str, int],
) -> Rows:
    sources = [feature["source"] for feature in item["features"]]
    target_at = int(item["target"])
    index = 0
    for _role, book, plan, derived in _books(item, paths, split):
        for batch, values in _source_batches(item, book, sources, plan, derived):
            for row in _batch_rows(item, batch, values, branches, widths, target_at, index):
                yield row
                index += 1


def _batch_rows(
    item: Mapping[str, Any],
    batch: Any,
    values: Mapping[str, Sequence[Any]],
    branches: Sequence[str],
    widths: Mapping[str, int],
    target_at: int,
    first: int,
) -> Rows:
    for offset in range(batch.num_rows):
        yield 0, _converted_row(item, values, offset, branches, widths, target_at, first)
        first += 1


def _converted_row(
    item: Mapping[str, Any],
    values: Mapping[str, Sequence[Any]],
    offset: int,
    branches: Sequence[str],
    widths: Mapping[str, int],
    target_at: int,
    index: int,
) -> dict[str, Any]:
    row: dict[str, Any] = {}
    converted = []
    for feature, branch in zip(item["features"], branches):
        made = _feature_value(
            item, feature, values[feature["source"]][offset], widths, index, row, branch
        )
        row[branch] = made
        converted.append(made)
    _set_target(row, item["features"][target_at], converted[target_at])
    row["index"] = index
    return row


def _feature_value(
    item: Mapping[str, Any],
    feature: Mapping[str, Any],
    value: Any,
    widths: Mapping[str, int],
    index: int,
    row: dict[str, Any],
    branch: str,
) -> Any:
    dtype = feature["dtype"]
    if dtype == "string":
        raw = _encoded(value, dataset=item["label"], field=feature["source"], index=index)
        row[f"{branch}_length"] = len(raw)
        return raw + bytes(widths[feature["source"]] - len(raw))
    if dtype == "classlabel":
        return _class(value, feature, item["label"], index)
    return _number(value, dtype, item["label"], feature["source"], index)


def _set_target(row: dict[str, Any], target: Mapping[str, Any], value: Any) -> None:
    if target["dtype"] == "classlabel":
        row["label"] = value
    else:
        row["target"] = float(value)


def load(name: str, paths: Mapping[str, Path], split: str) -> Loaded:
    """Stream one generated Hub dataset into a split-specific rows tree."""
    item = _BY_NAME.get(name)
    if item is None:
        raise ValueError(f"there is no open Hub dataset converter {name!r}")
    branches = _safe_branches(item["features"])
    widths = _text_widths(item, paths, split)
    columns = _columns(item, branches, widths)
    return ("rows",), columns, _entries(item, paths, split, branches, widths)
