"""The generated open-Hub shelf and its bounded-memory Parquet converter."""

from __future__ import annotations

import json
import math
from datetime import datetime
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


def test_the_hub_manifest_fixes_499_currently_public_explicitly_licensed_datasets():
    document = json.loads((ROOT / "catalogues/hub-open.json").read_text())
    records = document["datasets"]
    assert len(records) == len(HUB_OPEN) == 499
    assert len({record["name"] for record in records}) == 499
    assert document["statistics"]["source_bytes"] == 12_132_322_064
    assert document["statistics"]["permissive"] == 400
    assert document["statistics"]["licences"] == {
        "Apache-2.0": 96,
        "BSD-2-Clause": 1,
        "BSD-3-Clause": 5,
        "CC BY 3.0": 2,
        "CC BY 4.0": 135,
        "CC BY-SA 3.0": 13,
        "CC BY-SA 4.0": 78,
        "CC0": 41,
        "MIT": 114,
        "MPL-2.0": 1,
        "ODbL-1.0": 7,
        "Unlicense": 6,
    }


def test_every_hub_declaration_is_complete_registered_and_redistributable():
    assert sum(len(item["splits"]) for item in HUB_OPEN) == 805
    assert sum(len(item["sources"]) for item in HUB_OPEN) == 1_235
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
    enforced = bool(item.get("enforce_source_sizes", False))
    assert spec.enforce_source_sizes is enforced
    expected = spec.source_sizes if enforced else dict.fromkeys(spec.sources, 0)
    assert all(spec.expected_source_bytes(role) == expected[role] for role in spec.sources)


def _assert_hub_metadata(item):
    spec = DATASETS[item["name"]]
    assert spec.converter == f"open:hub:{spec.name}"
    assert spec.splits == tuple(item["splits"])
    assert redistributable(spec.licence)
    assert licence_url(spec.licence) == item["licence_url"]
    assert item["features"] and 0 <= item["target"] < len(item["features"])


def test_private_hub_source_is_not_advertised_as_an_open_mirror():
    assert "hub_rl123321_tb_science_stereo_dem_icesat2" not in DATASETS


def test_oversized_hub_text_uses_bounded_power_of_two_root_files():
    item = next(item for item in HUB_OPEN if item["name"] == "hub_p_doom_crowd_code_dataset_0_1")
    spec = DATASETS[item["name"]]

    assert spec.split_files
    assert tuple(item["splits"]) == tuple(f"text_bytes_{bucket:02d}" for bucket in range(24))
    assert all(item["split_roles"][split] == ["train_000"] for split in item["splits"])


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


def test_hub_classlabel_minus_one_is_the_publisher_missing_value():
    feature = {"source": "label", "classes": ["negative", "positive"]}

    assert hub_module._class(-1, feature, "owner/test", 4) == -1


class _PartitionBook:
    def __init__(self, _path):
        self.schema_arrow = SimpleNamespace(names=["Text", "score"])

    def iter_batches(self, *, batch_size, columns):
        assert batch_size == 4096
        values = {
            "Text": ["", "a", "abc", "12345", "x" * 20],
            "score": [0, 1, 2, 3, 4],
        }
        yield _Batch({name: values[name] for name in columns})


def test_hub_text_partition_uses_split_local_widths_and_original_indices(monkeypatch, tmp_path):
    item = {
        "name": "hub_partition_test",
        "label": "owner/partition-test",
        "features": [
            {"source": "Text", "branch": "text", "dtype": "string"},
            {"source": "score", "branch": "score", "dtype": "int64"},
        ],
        "target": 1,
        "split_roles": {
            "text_bytes_02": ["train_000"],
            "text_bytes_03": ["train_000"],
        },
        "partition": {"kind": "utf8_power2", "source": "Text", "max_bucket": 3},
    }
    monkeypatch.setitem(hub_module._BY_NAME, item["name"], item)
    monkeypatch.setattr(
        hub_module.importlib,
        "import_module",
        lambda _name: SimpleNamespace(ParquetFile=_PartitionBook),
    )
    paths = {"train_000": tmp_path / "train.parquet"}

    _classes, columns, entries = load(item["name"], paths, "text_bytes_02")
    assert columns["text"] == ("B", 3)
    assert next(entries)[1] == {
        "text": b"abc",
        "text_length": 3,
        "score": 2,
        "target": 2.0,
        "index": 2,
    }

    _classes, columns, entries = load(item["name"], paths, "text_bytes_03")
    assert columns["text"] == ("B", 20)
    assert [row["index"] for _tree, row in entries] == [3, 4]


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


def _hub_item(name):
    return next(item for item in HUB_OPEN if item["name"] == name)


def _sources_except(item, skipped):
    return [feature["source"] for feature in item["features"] if feature["source"] not in skipped]


def test_hub_swisscrop_schema_revision_preserves_the_publishers_trailing_space():
    swiss = _hub_item("hub_eoa_team_swisscrop25")
    swiss_names = _sources_except(swiss, {"BPA_QI"})
    swiss_plan, _derived = hub_module._schema_plan(swiss, [*swiss_names, "BPA_QI "])

    assert swiss_plan["BPA_QI"] == "BPA_QI "


def test_hub_oceantaco_schema_revision_uses_the_current_istac_field_name():
    ocean = _hub_item("hub_nilsleh_oceantaco")
    ocean_names = _sources_except(ocean, {"stac:time_start"})
    ocean_plan, _derived = hub_module._schema_plan(ocean, [*ocean_names, "istac:time_start"])

    assert ocean_plan["stac:time_start"] == "istac:time_start"


class _OceanTimestampBook:
    def __init__(self, _path):
        self.schema_arrow = SimpleNamespace(names=["istac:time_start", "internal:parent_id"])

    def iter_batches(self, *, batch_size, columns):
        assert batch_size == 4096
        values = {
            "istac:time_start": [datetime(2023, 3, 28, 23, 10, 7, 593194)],
            "internal:parent_id": [17],
        }
        yield _Batch({name: values[name] for name in columns})


def test_hub_oceantaco_serializes_arrow_timestamps_as_iso_text(monkeypatch, tmp_path):
    name = "hub_nilsleh_oceantaco"
    item = {
        "name": name,
        "label": "nilsleh/OceanTACO",
        "features": [
            {"source": "stac:time_start", "branch": "stac_time_start", "dtype": "string"},
            {"source": "internal:parent_id", "branch": "parent", "dtype": "int64"},
        ],
        "target": 1,
        "split_roles": {"train": ["train_001"]},
    }
    monkeypatch.setitem(hub_module._BY_NAME, name, item)
    monkeypatch.setattr(
        hub_module.importlib,
        "import_module",
        lambda _name: SimpleNamespace(ParquetFile=_OceanTimestampBook),
    )

    _classes, columns, entries = load(
        name, {"train_001": tmp_path / "level1.parquet"}, "train"
    )
    row = next(entries)[1]

    expected = b"2023-03-28T23:10:07.593194"
    assert columns["stac_time_start"] == ("B", len(expected))
    assert row["stac_time_start"] == expected
    assert row["stac_time_start_length"] == len(expected)


def test_hub_unstable_exports_with_publisher_parquet_are_revision_pinned():
    nbroad = _hub_item("hub_nbroad_hf_inference_providers_data")
    ocean = _hub_item("hub_nilsleh_oceantaco")

    assert nbroad["enforce_source_sizes"]
    assert f"/resolve/{nbroad['revision']}/data/" in nbroad["sources"]["train_000"]
    assert ocean["enforce_source_sizes"]
    prefix = f"/resolve/{ocean['revision']}/METADATA/"
    assert all(prefix in url for url in ocean["sources"].values())


def test_hub_forceflow_schema_revision_explicitly_defaults_omitted_delta_fields():
    force = _hub_item("hub_jokeresc_forceflow")
    delta_fields = frozenset(f"delta_force_{axis}" for axis in range(6))
    force_names = _sources_except(force, delta_fields)
    force_plan, defaulted = hub_module._schema_plan(force, force_names)

    assert all(force_plan[f"delta_force_{axis}"] == "" for axis in range(6))
    assert defaulted == delta_fields


class _MissingForceBook:
    def __init__(self, _path):
        self.schema_arrow = SimpleNamespace(names=["score"])

    def iter_batches(self, *, batch_size, columns):
        assert columns == ["score"]
        yield _Batch({"score": [1.25]})


def test_hub_force_shards_without_publisher_deltas_use_numeric_missing_values(
    monkeypatch, tmp_path
):
    name = "hub_jokeresc_forceflow"
    item = {
        "name": name,
        "label": "jokeresc/ForceFlow",
        "features": [
            {"source": "score", "branch": "score", "dtype": "float64"},
            {"source": "delta_force_0", "branch": "delta_force_0", "dtype": "float64"},
        ],
        "target": 1,
        "split_roles": {"train": ["train_003"]},
    }
    monkeypatch.setitem(hub_module._BY_NAME, name, item)
    monkeypatch.setattr(
        hub_module.importlib,
        "import_module",
        lambda _name: SimpleNamespace(ParquetFile=_MissingForceBook),
    )

    _classes, _columns, entries = load(name, {"train_003": tmp_path / "train.parquet"}, "train")
    row = next(entries)[1]

    assert math.isnan(row["delta_force_0"])
    assert math.isnan(row["target"])


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
