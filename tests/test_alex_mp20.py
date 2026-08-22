"""Pinned local replicas of the 100 visual Alex-MP-20 physics tasks."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from xrd.ml import visualize_2d
from xrd.root import _alex_mp20 as alex_module
from xrd.root import open_root
from xrd.root._alex_mp20 import (
    ALEX_MP20,
    DERIVED_TARGETS,
    DIRECT_TARGETS,
    ELEMENTS,
    SOURCE,
    SOURCE_COLUMNS,
    TARGETS,
    load,
)
from xrd.root.datasets import DATASETS, Large, _conversion_cache_name, convert


def _row(target: Any = 1.25) -> dict[str, Any]:
    return {
        "positions": [[0.0, 0.0, 0.0], [1.0, 1.5, 2.0]],
        "cell": [[2.0, 0.0, 0.0], [0.0, 3.0, 0.0], [0.0, 0.0, 4.0]],
        "atomic_numbers": [11, 17],
        "material_id": "mp-123",
        "space_group": "Fm-3m",
        "chemical_system": "Cl-Na",
        "energy_above_hull": 0.0,
        "dft_band_gap": target,
        "dft_bulk_modulus": 32.0,
        "dft_mag_density": 0.0,
        "hhi_score": 1200.0,
        "ml_bulk_modulus": 31.5,
    }


class _Batch:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.num_rows = len(rows)

    def to_pydict(self) -> dict[str, list[Any]]:
        return {name: [row[name] for row in self._rows] for name in SOURCE_COLUMNS}


class _Book:
    rows: ClassVar[list[dict[str, Any]]] = [_row(1.25), _row(None)]

    def __init__(self, _path: Path) -> None:
        self.schema_arrow = SimpleNamespace(names=list(SOURCE_COLUMNS))

    def iter_batches(self, *, batch_size: int, columns: list[str]):
        assert batch_size == 1024 and columns == list(SOURCE_COLUMNS)
        yield _Batch(self.rows)


@pytest.fixture
def parquet(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        alex_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(ParquetFile=_Book),
    )


def _paths(tmp_path: Path) -> dict[str, Path]:
    return {split: tmp_path / f"{split}.parquet" for split in ("train", "validation", "test")}


def test_exactly_one_hundred_additional_visual_physics_tasks_are_registered() -> None:
    names = {item["name"] for item in ALEX_MP20}
    actual = (
        len(DIRECT_TARGETS),
        len(DERIVED_TARGETS),
        len(ELEMENTS),
        len(TARGETS),
        len(names),
    )
    assert actual == (6, 32, 62, 100, 100)
    assert all(name.startswith("alex_mp20_") for name in names)
    assert names <= DATASETS.keys()


def test_tasks_share_three_complete_pinned_source_shards() -> None:
    assert SOURCE["source_bytes"] == 197_512_780
    assert sum(SOURCE["source_sizes"].values()) == SOURCE["source_bytes"]
    assert {item["source_bytes"] for item in ALEX_MP20} == {197_512_780}
    first = DATASETS["alex_mp20_dft_band_gap"]
    second = DATASETS["alex_mp20_cell_volume"]
    for role, cache_name in SOURCE["cache_names"].items():
        assert _conversion_cache_name(first, "train", role, 3) == cache_name
        assert _conversion_cache_name(second, "test", role, 3) == cache_name


def test_every_task_preserves_licence_origin_authors_and_source_mirrors() -> None:
    for item in ALEX_MP20:
        spec = DATASETS[item["name"]]
        assert isinstance(spec, Large)
        assert spec.licence == "CC BY 4.0"
        assert spec.within_source_ceiling()
        assert spec.creators[0] == "Claudio Zeni"
        assert spec.publisher == "Open Materials Generation (OMatG)"
        assert spec.repository == "Hugging Face"
        assert spec.origin == "https://doi.org/10.1038/s41586-025-08628-5"
        assert {name for name, _url in spec.mirrors} == {
            "Alexandria source collection",
            "MatterGen source and models",
        }


def test_published_targets_filter_missing_values_and_preserve_visual_structure(
    parquet: None, tmp_path: Path
) -> None:
    trees, columns, entries = load("dft_band_gap", _paths(tmp_path), "train")
    made = list(entries)

    assert (
        trees,
        columns["element_image"],
        columns["projection"],
        columns["occupancy_projection"],
        len(made),
    ) == (("samples",), ("f", 128), ("B", 3 * 32 * 32), ("B", 3 * 32 * 32), 1)
    _tree, row = made[0]
    assert row["target"] == pytest.approx(1.25)
    assert row["fractional_positions"][3:6] == pytest.approx([0.5, 0.5, 0.5])
    assert sum(row["element_image"]) == pytest.approx(1.0)
    assert max(row["projection"]) == 17
    assert sum(row["occupancy_projection"]) == 6


def test_geometry_and_element_fraction_targets_are_transparent(
    parquet: None, tmp_path: Path
) -> None:
    _trees, _columns, volumes = load("cell_volume", _paths(tmp_path), "validation")
    _trees, _columns, sodium = load("fraction_na", _paths(tmp_path), "test")

    assert [row["target"] for _tree, row in volumes] == pytest.approx([24.0, 24.0])
    assert [row["target"] for _tree, row in sodium] == pytest.approx([0.5, 0.5])


def test_conversion_writes_publisher_split_and_visualization_reads_a_plane(
    parquet: None, tmp_path: Path
) -> None:
    output = io.BytesIO()
    convert(
        "alex_mp20_cell_volume",
        output,
        split="train",
        parts=_paths(tmp_path),
    )

    with open_root(io.BytesIO(output.getvalue())) as back:
        assert back["train_samples"]["target"].array().tolist() == pytest.approx([24.0, 24.0])
        assert back["train_samples"]["atoms"].array().tolist() == [2, 2]

    image = visualize_2d(
        io.BytesIO(output.getvalue()),
        tree="train_samples",
        entry=0,
        plane="xy",
    )
    assert image.shape == (32, 32)
    assert image.target == pytest.approx(24.0)
    assert image.raw_max == 17


def test_unknown_target_split_and_schema_are_rejected(parquet: None, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no Alex-MP-20 converter"):
        load("unknown", _paths(tmp_path), "train")

    with pytest.raises(ValueError, match="no 'holdout' split"):
        list(load("cell_volume", _paths(tmp_path), "holdout")[2])

    class WrongBook(_Book):
        def __init__(self, _path: Path) -> None:
            self.schema_arrow = SimpleNamespace(names=["positions"])

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        alex_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(ParquetFile=WrongBook),
    )
    try:
        with pytest.raises(ValueError, match="not the recorded"):
            list(load("cell_volume", _paths(tmp_path), "train")[2])
    finally:
        monkeypatch.undo()
