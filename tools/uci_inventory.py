#!/usr/bin/env python3
"""Inventory UCI's catalogue and rank its directly hosted source archives.

The generated JSON is input data for the checked-in UCI selection manifest;
it is deliberately not imported by the library, so importing :mod:`xrd` never
depends on the network or on UCI's catalogue remaining unchanged.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

LIST = "https://archive.ics.uci.edu/api/datasets/list"
DETAIL = "https://archive.ics.uci.edu/api/dataset?id={id}"
AGENT = "PyXRootDClient UCI catalogue inventory"


def _json(url: str) -> Any:
    for attempt in range(3):
        request = urllib.request.Request(url, headers={"User-Agent": AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except OSError:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)
    raise AssertionError("the retry loop always returns or raises")


def _size(url: str) -> tuple[int | None, str | None]:
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            length = response.headers.get("Content-Length")
            return (int(length) if length is not None else None), response.geturl()
    except (OSError, urllib.error.HTTPError):
        return None, None


def _one(dataset: dict[str, Any]) -> dict[str, Any]:
    detail = _json(DETAIL.format(id=dataset["id"]))["data"]
    path = urllib.parse.urlsplit(detail["repository_url"]).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    archive = f"https://archive.ics.uci.edu/static/public/{dataset['id']}/{slug}.zip"
    cdn_slug = urllib.parse.quote(urllib.parse.unquote(slug), safe="")
    cdn = f"https://cdn.uci-ics-mlr-prod.aws.uci.edu/{dataset['id']}/{cdn_slug}.zip"
    size, resolved = _size(cdn)
    return {
        "id": dataset["id"],
        "name": detail["name"],
        "creators": detail.get("creators") or [],
        "dataset_doi": detail.get("dataset_doi"),
        "repository_url": detail["repository_url"],
        "data_url": detail["data_url"],
        "archive_url": archive if size is not None else None,
        "archive_bytes": size,
        "archive_resolved_url": resolved,
        "area": detail["area"],
        "tasks": detail["tasks"],
        "characteristics": detail["characteristics"],
        "instances": detail["num_instances"],
        "features": detail["num_features"],
        "target_columns": detail["target_col"],
        "variables": detail["variables"],
        "citation": (detail.get("additional_info") or {}).get("citation"),
        "intro_paper": detail.get("intro_paper"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--jobs", type=int, default=12)
    args = parser.parse_args()

    catalogue = _json(LIST)["data"]
    completed: list[dict[str, Any]] = []
    failed: dict[int, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        running = {pool.submit(_one, item): item["id"] for item in catalogue}
        for future in concurrent.futures.as_completed(running):
            uci_id = running[future]
            try:
                completed.append(future.result())
            except (OSError, ValueError, urllib.error.HTTPError) as exc:
                failed[uci_id] = str(exc)

    document = {
        "source": LIST,
        "datasets": sorted(completed, key=lambda item: item["id"]),
        "failed": {str(key): failed[key] for key in sorted(failed)},
    }
    args.output.write_text(json.dumps(document, indent=2) + "\n")
    print(f"wrote {len(completed)} datasets to {args.output}; {len(failed)} failed")
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(main())
