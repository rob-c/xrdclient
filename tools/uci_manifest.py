#!/usr/bin/env python3
"""Build the checked-in UCI collection manifest from inventory snapshots."""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

# The physics and measurement-science records selected in the research pass.
PHYSICS_90 = {
    32,
    41,
    66,
    85,
    88,
    93,
    106,
    108,
    118,
    135,
    138,
    139,
    148,
    155,
    156,
    157,
    173,
    175,
    179,
    181,
    188,
    194,
    196,
    204,
    206,
    209,
    213,
    214,
    220,
    221,
    224,
    226,
    231,
    245,
    246,
    250,
    253,
    254,
    256,
    265,
    270,
    271,
    273,
    278,
    283,
    286,
    290,
    295,
    302,
    308,
    309,
    310,
    313,
    316,
    319,
    321,
    322,
    325,
    328,
    330,
    333,
    341,
    344,
    360,
    361,
    362,
    387,
    394,
    400,
    401,
    408,
    447,
    448,
    463,
    482,
    483,
    487,
    494,
    508,
    509,
    510,
    511,
    517,
    583,
    692,
    750,
    846,
    994,
    995,
    1091,
}
LARGE_10 = {251, 279, 280, 305, 340, 347, 439, 456, 495, 520}
PHYSICS_100 = PHYSICS_90 | LARGE_10


def _current_uci_ids(sources: list[Path]) -> set[int]:
    pattern = r"https://archive\.ics\.uci\.edu/dataset/(\d+)"
    return {int(found) for source in sources for found in re.findall(pattern, source.read_text())}


def _record(item: dict[str, Any], groups: list[str], implemented: bool) -> dict[str, Any]:
    return {
        "uci_id": item["id"],
        "name": item["name"],
        "groups": groups,
        "implemented": implemented,
        "archive_bytes": item["archive_bytes"],
        "creators": item.get("creators", []),
        "doi": item.get("dataset_doi"),
        "origin": (
            item["dataset_doi"]
            if str(item.get("dataset_doi", "")).startswith(("http://", "https://"))
            else f"https://doi.org/{item['dataset_doi']}"
            if item.get("dataset_doi")
            else item["repository_url"]
        ),
        "repository": "UCI Machine Learning Repository",
        "source": item["repository_url"],
        "archive": item["archive_url"],
        "area": item["area"],
        "tasks": item["tasks"],
        "characteristics": item["characteristics"],
        "instances": item["instances"],
        "features": item["features"],
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("smallest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--registry",
        type=Path,
        nargs="+",
        default=[
            Path("src/xrd/root/datasets.py"),
            Path("src/xrd/root/_uci_tables.py"),
            Path("src/xrd/root/_uci_large.py"),
        ],
    )
    return parser.parse_args()


def _groups(uci_id: int, smallest: set[int]) -> list[str]:
    memberships = (
        ("physics_100", PHYSICS_100),
        ("large_10", LARGE_10),
        ("smallest_400", smallest),
    )
    return [name for name, members in memberships if uci_id in members]


def _statistics(
    by_id: dict[int, dict[str, Any]], smallest: set[int], requested: set[int], implemented: set[int]
) -> dict[str, int]:
    def archive_bytes(ids: set[int]) -> int:
        return sum(by_id[uci_id]["archive_bytes"] for uci_id in ids)

    return {
        "physics_100": len(PHYSICS_100),
        "large_10": len(LARGE_10),
        "smallest_400": len(smallest),
        "overlap": len(PHYSICS_100 & smallest),
        "requested_union": len(requested),
        "already_implemented": len(requested & implemented),
        "new_adapters_required": len(requested - implemented),
        "physics_archive_bytes": archive_bytes(PHYSICS_100),
        "large_archive_bytes": archive_bytes(LARGE_10),
        "smallest_archive_bytes": archive_bytes(smallest),
        "source_archive_bytes": archive_bytes(requested),
    }


def _document(
    inventory: dict[str, Any],
    smallest_document: dict[str, Any],
    records: list[dict[str, Any]],
    by_id: dict[int, dict[str, Any]],
    smallest: set[int],
    requested: set[int],
    implemented: set[int],
) -> dict[str, Any]:
    return {
        "format": 1,
        "snapshot": date.today().isoformat(),
        "source": inventory["source"],
        "definitions": {
            "physics_100": (
                "The selected 100 UCI physics, measurement, energy, instrumentation, "
                "materials and biophysics records."
            ),
            "large_10": "The ten largest archives in the physics selection.",
            "smallest_400": smallest_document["definition"],
        },
        "collections": {
            "physics_100": sorted(PHYSICS_100),
            "large_10": sorted(LARGE_10),
            "smallest_400": sorted(smallest),
            "requested_union": sorted(requested),
        },
        "statistics": _statistics(by_id, smallest, requested, implemented),
        "datasets": records,
    }


def main() -> int:
    args = _arguments()

    inventory = json.loads(args.inventory.read_text())
    by_id = {item["id"]: item for item in inventory["datasets"]}
    smallest_document = json.loads(args.smallest.read_text())
    smallest = {item["id"] for item in smallest_document["datasets"]}
    implemented = _current_uci_ids(args.registry)
    requested = PHYSICS_100 | smallest

    records = [
        _record(by_id[uci_id], _groups(uci_id, smallest), uci_id in implemented)
        for uci_id in sorted(requested)
    ]
    document = _document(
        inventory, smallest_document, records, by_id, smallest, requested, implemented
    )
    args.output.write_text(json.dumps(document, indent=2) + "\n")
    print(
        f"wrote {len(records)} records to {args.output}; "
        f"{document['statistics']['new_adapters_required']} need adapters"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
