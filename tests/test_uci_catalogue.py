"""The reproducible UCI collections selected for the next converter shelf."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]


def _catalogue() -> dict[str, Any]:
    return json.loads((ROOT / "catalogues/uci.json").read_text())


def test_the_requested_uci_collections_are_exact_sets_of_ids() -> None:
    document = _catalogue()
    groups = document["collections"]
    physics = set(groups["physics_100"])
    large = set(groups["large_10"])
    smallest = set(groups["smallest_400"])
    requested = set(groups["requested_union"])

    assert len(physics) == 100
    assert len(large) == 10 and large < physics
    assert len(smallest) == 400
    assert len(physics & smallest) == 45
    assert requested == physics | smallest
    assert len(requested) == 455
    assert not large & smallest


def test_every_selected_uci_record_carries_a_source_and_measured_size() -> None:
    document = _catalogue()
    records = document["datasets"]
    assert len(records) == 455
    assert len({record["uci_id"] for record in records}) == len(records)
    assert sum(record["archive_bytes"] for record in records) == 78_542_700_488
    assert document["statistics"]["physics_archive_bytes"] == 78_338_087_513
    assert document["statistics"]["large_archive_bytes"] == 73_708_449_615
    assert document["statistics"]["smallest_archive_bytes"] == 234_227_168
    for record in records:
        _assert_uci_source(record)


def _assert_uci_source(record: dict) -> None:
    uci_id = record["uci_id"]
    assert record["source"].startswith(f"https://archive.ics.uci.edu/dataset/{uci_id}/")
    assert record["archive"].startswith(f"https://archive.ics.uci.edu/static/public/{uci_id}/")
    assert record["archive"].endswith(".zip")
    assert record["archive_bytes"] > 0
    assert record["groups"]


def test_the_manifest_marks_only_the_uci_adapters_already_in_the_registry() -> None:
    document = _catalogue()
    source = "\n".join(
        (ROOT / path).read_text()
        for path in (
            "src/xrd/root/datasets.py",
            "src/xrd/root/_uci_tables.py",
            "src/xrd/root/_uci_large.py",
        )
    )
    implemented = {
        int(found) for found in re.findall(r"https://archive\.ics\.uci\.edu/dataset/(\d+)", source)
    }
    records = document["datasets"]
    marked = {record["uci_id"] for record in records if record["implemented"]}
    requested = set(document["collections"]["requested_union"])

    assert marked == implemented & requested
    assert len(marked) == 177
    assert document["statistics"]["new_adapters_required"] == 278
