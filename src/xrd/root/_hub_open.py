"""Bounded-memory conversion of explicitly licensed Hub Parquet datasets.

The generated :mod:`._hub_tables` declarations fix every source shard, byte
count, split and scalar feature.  This reader scans text columns once to choose
lossless fixed-width ROOT branches, then streams Arrow record batches into one
TTree per publisher split.  Nested media features never reach this module;
the catalogue generator refuses those until they have purpose-built adapters.
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

TEXT_LIMIT = 32_768
_BY_NAME = {item["name"]: item for item in HUB_OPEN}


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


def _books(
    item: Mapping[str, Any], paths: Mapping[str, Path], split: str
) -> Iterator[tuple[str, Any]]:
    parquet = importlib.import_module("pyarrow.parquet")
    expected = [feature["source"] for feature in item["features"]]
    for role in _roles(item, split):
        if role not in paths:
            raise ValueError(f"the Hub dataset {item['label']} is missing its {role} shard")
        book = parquet.ParquetFile(paths[role])
        names = list(book.schema_arrow.names)
        if names != expected:
            raise ValueError(
                f"the Hub dataset {item['label']} {role} columns are {', '.join(names)}, "
                f"not the recorded {', '.join(expected)}"
            )
        yield role, book


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


def _text_widths(item: Mapping[str, Any], paths: Mapping[str, Path], split: str) -> dict[str, int]:
    text = [feature for feature in item["features"] if feature["dtype"] == "string"]
    widths = {feature["source"]: 1 for feature in text}
    if not text:
        return widths
    columns = list(widths)
    index = 0
    for _role, book in _books(item, paths, split):
        for batch in book.iter_batches(batch_size=4096, columns=columns):
            values = batch.to_pydict()
            for name in columns:
                for offset, value in enumerate(values[name]):
                    raw = _encoded(
                        value,
                        dataset=item["label"],
                        field=name,
                        index=index + offset,
                    )
                    widths[name] = max(widths[name], len(raw))
            index += batch.num_rows
    return widths


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
    for _role, book in _books(item, paths, split):
        for batch in book.iter_batches(batch_size=4096, columns=sources):
            for row in _batch_rows(item, batch, branches, widths, target_at, index):
                yield row
                index += 1


def _batch_rows(
    item: Mapping[str, Any],
    batch: Any,
    branches: Sequence[str],
    widths: Mapping[str, int],
    target_at: int,
    first: int,
) -> Rows:
    values = batch.to_pydict()
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
