"""The generated open-Hub shelf and its bounded-memory Parquet converter."""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

from tools.hub_catalogue import OPEN_LICENCES

from xrd.root import _hub_open as hub_module
from xrd.root._hub_open import load
from xrd.root._hub_tables import HUB_OPEN
from xrd.root.datasets import DATASETS, licence_url, redistributable

ROOT = Path(__file__).parents[1]


def test_every_discovery_licence_has_canonical_terms_and_passes_the_mirror_gate():
    assert len(OPEN_LICENCES) == 29
    for _slug, (statement, canonical, _permissive) in OPEN_LICENCES.items():
        assert redistributable(statement)
        assert licence_url(statement) == canonical


def test_the_hub_manifest_fixes_exactly_500_explicitly_licensed_datasets():
    document = json.loads((ROOT / "catalogues/hub-open.json").read_text())
    records = document["datasets"]
    assert len(records) == len(HUB_OPEN) == 500
    assert len({record["name"] for record in records}) == 500
    assert document["statistics"]["source_bytes"] == 12_132_339_034
    assert document["statistics"]["permissive"] == 401
    assert document["statistics"]["licences"] == {
        "Apache-2.0": 96,
        "BSD-2-Clause": 1,
        "BSD-3-Clause": 5,
        "CC BY 3.0": 2,
        "CC BY 4.0": 135,
        "CC BY-SA 3.0": 13,
        "CC BY-SA 4.0": 78,
        "CC0": 42,
        "MIT": 114,
        "MPL-2.0": 1,
        "ODbL-1.0": 7,
        "Unlicense": 6,
    }


def test_every_hub_declaration_is_complete_registered_and_redistributable():
    assert sum(len(item["splits"]) for item in HUB_OPEN) == 783
    assert sum(len(item["sources"]) for item in HUB_OPEN) == 1_236
    for item in HUB_OPEN:
        _assert_hub_source(item)
        _assert_hub_metadata(item)


def _assert_hub_source(item):
    spec = DATASETS[item["name"]]
    assert spec.source == item["source"]
    assert spec.source.startswith("https://huggingface.co/datasets/")
    assert spec.source_payload_bytes() == item["source_bytes"]
    assert spec.source_payload_bytes() < 2_000_000_000
    assert set(spec.sources) == set(spec.source_sizes)
    assert sum(spec.source_sizes.values()) == spec.source_bytes


def _assert_hub_metadata(item):
    spec = DATASETS[item["name"]]
    assert spec.converter == f"open:hub:{spec.name}"
    assert spec.splits == tuple(item["splits"])
    assert redistributable(spec.licence)
    assert licence_url(spec.licence) == item["licence_url"]
    assert item["features"] and 0 <= item["target"] < len(item["features"])


def test_derived_text_branches_never_collide_with_source_or_training_columns():
    for item in HUB_OPEN:
        branches = hub_module._safe_branches(item["features"])
        made = set(branches)
        made.update(
            f"{branch}_length"
            for feature, branch in zip(item["features"], branches)
            if feature["dtype"] == "string"
        )
        assert len(made) == sum(
            2 if feature["dtype"] == "string" else 1 for feature in item["features"]
        )
        assert not made & {"index", "label", "target"}


class _Batch:
    def __init__(self, values):
        self.values = values
        self.num_rows = len(next(iter(values.values())))

    def to_pydict(self):
        return self.values


class _Book:
    def __init__(self, _path):
        self.schema_arrow = SimpleNamespace(names=["review", "sentiment", "score"])

    def iter_batches(self, *, batch_size, columns):
        assert batch_size == 4096
        values = {
            "review": ["ok", None],
            "sentiment": [1, "negative"],
            "score": [1.5, None],
        }
        yield _Batch({name: values[name] for name in columns})


def test_hub_parquet_preserves_scalars_text_lengths_classes_and_missing_values(
    monkeypatch, tmp_path
):
    item = {
        "name": "hub_test",
        "label": "owner/test",
        "features": [
            {"source": "review", "branch": "review", "dtype": "string"},
            {
                "source": "sentiment",
                "branch": "sentiment",
                "dtype": "classlabel",
                "classes": ["negative", "positive"],
            },
            {"source": "score", "branch": "score", "dtype": "float32"},
        ],
        "target": 1,
        "split_roles": {"train": ["train_000"]},
    }
    monkeypatch.setitem(hub_module._BY_NAME, "hub_test", item)
    monkeypatch.setattr(
        hub_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(ParquetFile=_Book),
    )
    classes, columns, entries = load("hub_test", {"train_000": tmp_path / "train.parquet"}, "train")
    assert classes == ("rows",)
    assert columns == {
        "review": ("B", 2),
        "review_length": "i",
        "sentiment": "i",
        "score": "d",
        "label": "i",
        "index": "q",
    }
    made = list(entries)
    assert made[0] == (
        0,
        {
            "review": b"ok",
            "review_length": 2,
            "sentiment": 1,
            "score": 1.5,
            "label": 1,
            "index": 0,
        },
    )
    assert made[1][1]["review"] == b"\x00\x00"
    assert made[1][1]["review_length"] == 0
    assert made[1][1]["sentiment"] == made[1][1]["label"] == 0
    assert math.isnan(made[1][1]["score"])


class _NordSchemaBook:
    def __init__(self, _path):
        self.schema_arrow = SimpleNamespace(names=["document", "summary", "seed_dataset"])

    def iter_batches(self, *, batch_size, columns):
        assert batch_size == 4096
        values = {
            "document": ["Tre bogstaver"],
            "summary": ["Kort"],
            "seed_dataset": ["publisher"],
        }
        yield _Batch({name: values[name] for name in columns})


def test_hub_nord_schema_aliases_document_and_derives_character_lengths(monkeypatch, tmp_path):
    name = "hub_alexandrainst_nordjylland_news_summarization"
    item = {
        "name": name,
        "label": "alexandrainst/nordjylland-news-summarization",
        "features": [
            {"source": "text", "branch": "text", "dtype": "string"},
            {"source": "summary", "branch": "summary", "dtype": "string"},
            {"source": "text_len", "branch": "text_len", "dtype": "int64"},
            {"source": "summary_len", "branch": "summary_len", "dtype": "int64"},
        ],
        "target": 3,
        "split_roles": {"train": ["train_001"]},
    }
    monkeypatch.setitem(hub_module._BY_NAME, name, item)
    monkeypatch.setattr(
        hub_module.importlib,
        "import_module",
        lambda _name: SimpleNamespace(ParquetFile=_NordSchemaBook),
    )

    _classes, _columns, entries = load(name, {"train_001": tmp_path / "train.parquet"}, "train")
    _tree, row = next(entries)

    assert row["text_len"] == len("Tre bogstaver")
    assert row["summary_len"] == row["target"] == len("Kort")


class _SupersetBook:
    def __init__(self, _path):
        self.schema_arrow = SimpleNamespace(names=["extra", "score", "review"])

    def iter_batches(self, *, batch_size, columns):
        values = {"review": ["ok"], "score": [0.75], "extra": [99]}
        yield _Batch({name: values[name] for name in columns})


def test_hub_shards_may_reorder_columns_and_add_new_publisher_fields(monkeypatch, tmp_path):
    item = {
        "name": "hub_superset",
        "label": "owner/superset",
        "features": [
            {"source": "review", "branch": "review", "dtype": "string"},
            {"source": "score", "branch": "score", "dtype": "float64"},
        ],
        "target": 1,
        "split_roles": {"train": ["train_001"]},
    }
    monkeypatch.setitem(hub_module._BY_NAME, "hub_superset", item)
    monkeypatch.setattr(
        hub_module.importlib,
        "import_module",
        lambda _name: SimpleNamespace(ParquetFile=_SupersetBook),
    )

    _classes, _columns, entries = load(
        "hub_superset", {"train_001": tmp_path / "train.parquet"}, "train"
    )

    assert next(entries)[1]["target"] == 0.75


def test_hub_text_can_losslessly_hold_multi_megabyte_publisher_values():
    raw = "physics " * 500_000
    encoded = hub_module._encoded(raw, dataset="owner/long", field="trace", index=7)
    assert len(encoded) == len(raw.encode())
    assert len(encoded) > 32_768
