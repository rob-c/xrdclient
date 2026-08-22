#!/usr/bin/env python3
"""Select the smallest self-contained UCI archives from an inventory."""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import re
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

AGENT = "PyXRootDClient UCI catalogue selection"
DATA_SUFFIXES = {
    ".arff",
    ".bmp",
    ".csv",
    ".data",
    ".dat",
    ".gif",
    ".h5",
    ".hdf5",
    ".jpeg",
    ".jpg",
    ".json",
    ".mat",
    ".mp3",
    ".npy",
    ".npz",
    ".parquet",
    ".png",
    ".sav",
    ".tsv",
    ".wav",
    ".xls",
    ".xlsx",
    ".xml",
}
DOCUMENTS = {
    "abstract",
    "citation",
    "description",
    "index",
    "license",
    "licence",
    "readme",
}


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


def _numeric_line_count(lines: list[str]) -> int:
    pattern = r"(?:^|[,;\t ])[-+]?(?:\d+\.?\d*|\.\d+)"
    return sum(bool(re.search(pattern, line)) for line in lines[:20])


def _delimited_line_count(lines: list[str]) -> int:
    marks = (",", ";", "\t")
    return sum(any(mark in line for mark in marks) for line in lines[:20])


def _text_data(name: str, raw: bytes) -> bool:
    stem = Path(name).stem.lower().replace("_", " ").replace("-", " ")
    words = set(stem.split())
    if words & DOCUMENTS or name.lower().endswith(".names"):
        return False
    try:
        text = raw[: 64 * 1024].decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    numeric = _numeric_line_count(lines)
    delimited = _delimited_line_count(lines)
    return numeric >= min(2, len(lines)) or delimited >= min(2, len(lines))


def _inspect(dataset: dict[str, Any]) -> dict[str, Any]:
    raw = _get(dataset["archive_url"])
    members: list[dict[str, Any]] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for info in archive.infolist():
            if info.is_dir() or info.filename.startswith("__MACOSX/"):
                continue
            suffix = Path(info.filename).suffix.lower()
            held = archive.read(info)
            data = suffix in DATA_SUFFIXES or (
                suffix in {"", ".txt"} and _text_data(info.filename, held)
            )
            members.append({"name": info.filename, "bytes": info.file_size, "data": data})
    return {
        **dataset,
        "payload_bytes": sum(member["bytes"] for member in members),
        "members": members,
        "self_contained": any(member["data"] for member in members),
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--count", type=int, default=400)
    parser.add_argument("--probe", type=int, default=500)
    parser.add_argument("--jobs", type=int, default=16)
    return parser.parse_args()


def _probe(
    candidates: list[dict[str, Any]], jobs: int
) -> tuple[list[dict[str, Any]], dict[int, str]]:
    inspected: list[dict[str, Any]] = []
    failed: dict[int, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        running = {pool.submit(_inspect, item): item["id"] for item in candidates}
        for future in concurrent.futures.as_completed(running):
            uci_id = running[future]
            try:
                inspected.append(future.result())
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                failed[uci_id] = str(exc)
    return inspected, failed


def _select(
    inspected: list[dict[str, Any]], candidates: list[dict[str, Any]], count: int
) -> list[dict[str, Any]]:
    usable = [item for item in inspected if item["self_contained"]]
    usable.sort(key=lambda item: (item["archive_bytes"], item["id"]))
    if len(usable) < count:
        raise ValueError(
            f"only {len(usable)} of the first {len(candidates)} hosted archives contain data; "
            "increase --probe"
        )
    return usable[:count]


def main() -> int:
    args = _arguments()

    inventory = json.loads(args.inventory.read_text())
    ranked = [item for item in inventory["datasets"] if item["archive_bytes"] is not None]
    ranked.sort(key=lambda item: (item["archive_bytes"], item["id"]))
    candidates = ranked[: args.probe]
    inspected, failed = _probe(candidates, args.jobs)
    selected = _select(inspected, candidates, args.count)
    document = {
        "source": inventory["source"],
        "definition": "smallest directly hosted, self-contained UCI source archives",
        "count": args.count,
        "archive_bytes": sum(item["archive_bytes"] for item in selected),
        "datasets": selected,
        "failed": {str(key): failed[key] for key in sorted(failed)},
    }
    args.output.write_text(json.dumps(document, indent=2) + "\n")
    print(
        f"wrote {len(selected)} datasets ({document['archive_bytes']} bytes) to {args.output}; "
        f"{len(failed)} probes failed"
    )
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(main())
