#!/usr/bin/env python3
"""Generate fixed creator and DOI attribution for registered UCI datasets."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import pprint
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

DETAIL = "https://archive.ics.uci.edu/api/dataset?id={uci_id}"
PATTERN = re.compile(r"https://archive\.ics\.uci\.edu/dataset/(\d+)")
AGENT = "PyXRootDClient UCI attribution audit"


def _json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except OSError:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)
    raise AssertionError("the retry loop always returns or raises")


def _one(uci_id: int) -> tuple[int, dict[str, Any]]:
    record = _json(DETAIL.format(uci_id=uci_id))["data"]
    doi = record.get("dataset_doi")
    additional = record.get("additional_info") or {}
    return uci_id, {
        "creators": tuple(record.get("creators") or ()),
        "origin": (
            doi
            if str(doi).startswith(("http://", "https://"))
            else f"https://doi.org/{doi}"
            if doi
            else record["repository_url"]
        ),
        "citation": additional.get("citation") or "",
    }


def _ids(paths: list[Path]) -> list[int]:
    return sorted({int(found) for path in paths for found in PATTERN.findall(path.read_text())})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("--jobs", type=int, default=12)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    records: dict[int, dict[str, Any]] = {}
    failures: dict[int, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        running = {pool.submit(_one, uci_id): uci_id for uci_id in _ids(args.sources)}
        for future in concurrent.futures.as_completed(running):
            uci_id = running[future]
            try:
                key, record = future.result()
                records[key] = record
            except (OSError, ValueError, KeyError) as error:
                failures[uci_id] = str(error)
    rendered = pprint.pformat(dict(sorted(records.items())), width=100, sort_dicts=False)
    args.output.write_text(
        '"""Generated creator and DOI attribution for registered UCI records."""\n\n'
        "# Generated citation text follows the publisher verbatim.\n"
        "# ruff: noqa\n\n"
        "from __future__ import annotations\n\n"
        "from typing import Any\n\n"
        f"UCI_ATTRIBUTION: dict[int, dict[str, Any]] = {rendered}\n"
    )
    if args.manifest is not None:
        document = json.loads(args.manifest.read_text())
        for item in document["datasets"]:
            record = records.get(item["uci_id"])
            if record is None:
                continue
            item.update(
                creators=list(record["creators"]),
                origin=record["origin"],
                repository="UCI Machine Learning Repository",
                citation=record["citation"],
            )
        args.manifest.write_text(json.dumps(document, indent=2) + "\n")
    print(f"wrote {len(records)} records to {args.output}; {len(failures)} failed")
    for uci_id, error in sorted(failures.items()):
        print(f"UCI {uci_id}: {error}")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
