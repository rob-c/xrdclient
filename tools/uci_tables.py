#!/usr/bin/env python3
"""Generate schema declarations for selected UCI normalized CSV tables."""

from __future__ import annotations

import argparse
import csv
import io
import json
import keyword
import pprint
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

AGENT = "PyXRootDClient UCI normalized-table generator"
MISSING = {"", "*", "?", "NA", "na", "N/A", "nan", "NaN", "NaNN", "null"}


def _get(url: str) -> bytes:
    for attempt in range(3):
        request = urllib.request.Request(url, headers={"User-Agent": AGENT})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read()
        except OSError:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)
    raise AssertionError("the retry loop always returns or raises")


def _identifier(value: str, *, fallback: str) -> str:
    value = re.sub(r"[^0-9A-Za-z]+", "_", value.strip()).strip("_").lower()
    if not value:
        value = fallback
    if value[0].isdigit():
        value = f"v_{value}"
    if keyword.iskeyword(value) or value == "index":
        value = f"source_{value}"
    return value


def _unique(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result = []
    for name in names:
        count = seen.get(name, 0) + 1
        seen[name] = count
        result.append(name if count == 1 else f"{name}_{count}")
    return result


def _variable(item: dict[str, Any], header: str, index: int) -> dict[str, Any]:
    variables = item["variables"]
    named = [
        variable
        for variable in variables
        if str(variable.get("name", "")).strip().casefold() == header.strip().casefold()
    ]
    if len(named) == 1:
        return named[0]
    if index < len(variables):
        return variables[index]
    return {"name": header, "role": "Feature", "type": "Continuous"}


def _categories(values: list[str]) -> list[str]:
    return sorted({value for value in values if value not in MISSING})


def _integer_role(values: list[str]) -> str:
    """Keep a declared integer narrow only when every measured value is one."""
    measured = [value for value in values if value not in MISSING]
    try:
        for value in measured:
            int(value)
    except ValueError:
        try:
            for value in measured:
                float(value)
        except ValueError:
            return ""
        return "d"
    return "i"


def _real_values(values: list[str]) -> bool:
    try:
        for value in values:
            if value not in MISSING:
                float(value)
    except ValueError:
        return False
    return True


def _class_names(values: list[str]) -> tuple[list[str], dict[str, int]]:
    raw = _categories(values)
    canonical = sorted({value.removesuffix(".") for value in raw})
    names = _unique(
        [
            _identifier(
                ("positive" if value == "+" else "negative" if value == "-" else value)
                .replace("<=", "le_")
                .replace(">=", "ge_")
                .replace("<", "lt_")
                .replace(">", "gt_"),
                fallback=f"class_{at}",
            )
            for at, value in enumerate(canonical)
        ]
    )
    numbered = {value: at for at, value in enumerate(canonical)}
    return names, {value: numbered[value.removesuffix(".")] for value in raw}


def _corrected_item(item: dict[str, Any]) -> dict[str, Any]:
    if item["id"] == 22:
        # The normalized King-Rook-vs-King-Pawn CSV omits its class header,
        # while the API calls the final board-square feature the target.
        variables = [{**variable} for variable in item["variables"]]
        variables[-1]["role"] = "Feature"
        variables.append({"name": "class", "role": "Target", "type": "Categorical"})
        return {**item, "variables": variables}
    return item


def _source_rows(item: dict[str, Any], raw: bytes) -> tuple[list[str], list[list[str]]]:
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
    if not rows:
        raise ValueError(f"UCI {item['id']} has an empty normalized CSV")
    headers, source_rows = rows[0], [row for row in rows[1:] if any(cell.strip() for cell in row)]
    widths = {len(row) for row in source_rows}
    if len(item["variables"]) == max(widths, default=0) and len(headers) + 1 == max(
        widths, default=0
    ):
        headers = [*headers, str(item["variables"][-1]["name"])]
    return headers, source_rows


def _logical_rows(
    item: dict[str, Any], headers: list[str], rows: list[list[str]]
) -> list[list[str]]:
    body = []
    pending: list[str] = []
    for row in rows:
        row = _trim_short_row(row, len(headers))
        pending.extend(row)
        if len(pending) == len(headers):
            body.append(pending)
            pending = []
        elif len(pending) > len(headers):
            break
    # A continuation line consumes an additional source row; reaching the
    # declared width is what finishes one logical record.
    if not body or pending:
        raise ValueError(f"UCI {item['id']} is not a rectangular normalized CSV")
    if any(len(row) != len(headers) for row in body):
        raise ValueError(f"UCI {item['id']} is not a rectangular normalized CSV")
    return body


def _trim_short_row(row: list[str], width: int) -> list[str]:
    return row[:-1] if len(row) < width and row[-1:] == [""] else row


def _target_indices(item: dict[str, Any], headers: list[str]) -> list[int]:
    return [
        at
        for at, header in enumerate(headers)
        if str(_variable(item, header, at).get("role", "")).lower() == "target"
    ]


def _categorical_target(item: dict[str, Any], headers: list[str], targets: list[int]) -> bool:
    if len(targets) != 1:
        return False
    variable = _variable(item, headers[targets[0]], targets[0])
    return str(variable.get("type", "")).lower().strip() in {"binary", "categorical"}


def _numeric_field(
    item: dict[str, Any], header: str, name: str, kind: str, values: list[str]
) -> tuple[str, str]:
    if kind == "integer":
        role = _integer_role(values)
        if role:
            return name, role
        description = "integer"
    else:
        if _real_values(values):
            return name, "d"
        description = "real"
    raise ValueError(f"UCI {item['id']} has a non-numeric value in {description} column {header!r}")


def _column(
    item: dict[str, Any],
    header: str,
    name: str,
    at: int,
    values: list[str],
    targets: list[int],
    categorical_target: bool,
) -> tuple[tuple[str, str], list[str], list[str], dict[str, int], int]:
    kind = str(_variable(item, header, at).get("type", "Continuous")).lower().strip()
    if at in targets and categorical_target:
        classes, labels = _class_names(values)
        return (name, "label"), [], classes, labels, 0
    if at in targets:
        return (name, "target"), [], [], {}, 0
    if kind in {"binary", "categorical"}:
        code = f"{name}_code"
        return (name, code), _categories(values), [], {}, 0
    if kind in {"integer", "continuous", "real"}:
        return _numeric_field(item, header, name, kind, values), [], [], {}, 0
    text_size = max((len(value.encode()) for value in values), default=0)
    return (name, "text"), [], [], {}, text_size


def _field_declarations(
    item: dict[str, Any], headers: list[str], body: list[list[str]], targets: list[int]
) -> tuple[list[tuple[str, str]], dict[str, list[str]], list[str], dict[str, int], int]:
    names = _unique(
        [_identifier(header, fallback=f"field_{at}") for at, header in enumerate(headers)]
    )
    categorical = _categorical_target(item, headers, targets)

    fields: list[tuple[str, str]] = []
    codes: dict[str, list[str]] = {}
    classes: list[str] = []
    labels: dict[str, int] = {}
    text_size = 0
    for at, (header, name) in enumerate(zip(headers, names)):
        values = [row[at].strip() for row in body]
        field, categories, found_classes, found_labels, width = _column(
            item, header, name, at, values, targets, categorical
        )
        fields.append(field)
        if categories:
            codes[field[1]] = categories
        classes = found_classes or classes
        labels = found_labels or labels
        text_size = max(text_size, width)
    return fields, codes, classes, labels, text_size


def _validate_targets(
    item: dict[str, Any], targets: list[int], fields: list[tuple[str, str]]
) -> None:
    if not targets:
        raise ValueError(f"UCI {item['id']} has no target in its variable metadata")
    if sum(role == "label" for _, role in fields) > 1:
        raise ValueError(f"UCI {item['id']} has more than one categorical target")


def _schema(item: dict[str, Any], raw: bytes) -> dict[str, Any]:
    item = _corrected_item(item)
    headers, source_rows = _source_rows(item, raw)
    body = _logical_rows(item, headers, source_rows)
    targets = _target_indices(item, headers)
    fields, codes, classes, labels, text_size = _field_declarations(item, headers, body, targets)
    _validate_targets(item, targets, fields)
    return {
        "name": _identifier(item["name"], fallback=f"uci_{item['id']}"),
        "label": item["name"],
        "title": (
            f"{len(body):,} rows of {item['name']}, {len(headers) - len(targets)} input fields"
        ),
        "source": item["repository_url"],
        "creators": item.get("creators", []),
        "origin": (
            item["dataset_doi"]
            if str(item.get("dataset_doi", "")).startswith(("http://", "https://"))
            else f"https://doi.org/{item['dataset_doi']}"
            if item.get("dataset_doi")
            else item["repository_url"]
        ),
        "repository": "UCI Machine Learning Repository",
        "citation": item.get("citation") or "",
        "url": item["data_url"],
        "classes": classes,
        "fields": fields,
        "labels": labels,
        "codes": codes,
        "text_size": text_size,
        "continuations": item["id"] == 32,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    inventory = json.loads(args.inventory.read_text())
    by_id = {item["id"]: item for item in inventory["datasets"]}
    manifest = json.loads(args.manifest.read_text())
    wanted = {
        item["uci_id"]
        for item in manifest["datasets"]
        if not item["implemented"] and by_id[item["uci_id"]]["data_url"] is not None
    }
    tables = []
    skipped = {}
    for uci_id in sorted(wanted):
        try:
            tables.append(_schema(by_id[uci_id], _get(by_id[uci_id]["data_url"])))
        except ValueError as exc:
            skipped[uci_id] = str(exc)
    rendered = pprint.pformat(tuple(tables), width=100, sort_dicts=False)
    refused = pprint.pformat(skipped, width=100, sort_dicts=True)
    args.output.write_text(
        '"""Generated declarations for selected UCI normalized CSV datasets."""\n\n'
        "from __future__ import annotations\n\n"
        "from typing import Any\n\n"
        f"UCI_TABLES: tuple[dict[str, Any], ...] = {rendered}\n\n"
        f"UCI_TABLES_SKIPPED: dict[int, str] = {refused}\n"
    )
    print(
        f"wrote {len(tables)} normalized table declarations to {args.output}; "
        f"{len(skipped)} malformed tables skipped"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
