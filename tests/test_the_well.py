"""Local HDF5 replicas of the broad The Well visual physics shelf."""

from __future__ import annotations

import io
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from xrd.ml import visualize_2d
from xrd.root import open_root
from xrd.root._the_well import (
    EXTRA_SOURCES,
    PIXELS,
    SOURCES,
    TASKS,
    THE_WELL,
    load,
)
from xrd.root.datasets import DATASETS, Large, _conversion_cache_name, convert


@pytest.fixture
def well_file(tmp_path: Path) -> Path:
    h5py = pytest.importorskip("h5py")
    numpy = pytest.importorskip("numpy")
    path = tmp_path / "well.hdf5"
    grid = numpy.arange(8 * 12, dtype="float32").reshape(8, 12)
    values = numpy.empty((2, 11, 8, 12), dtype="float32")
    for sample in range(2):
        for time in range(11):
            values[sample, time] = grid + sample * 100 + time
    with h5py.File(path, "w") as book:
        fields = book.create_group("t0_fields")
        fields.attrs["field_names"] = ["density"]
        density = fields.create_dataset("density", data=values)
        density.attrs["sample_varying"] = True
        density.attrs["time_varying"] = True
        density.attrs["dim_varying"] = [True, True]
    return path


def test_exactly_one_hundred_broad_visual_tasks_are_registered() -> None:
    names = {item["name"] for item in THE_WELL}
    assert len(SOURCES) == 16
    assert len(TASKS) == 6
    assert len(EXTRA_SOURCES) == 4
    assert len(THE_WELL) == len(names) == 100
    assert names <= DATASETS.keys()


def test_sources_span_domains_and_share_one_pinned_cache_object_each() -> None:
    domains = {str(source["domain"]) for source in SOURCES}
    assert len(domains) == 16
    assert sum(source["size"] < 2_000_000_000 for source in SOURCES) == 8
    assert sum(source["size"] for source in SOURCES) == 86_520_102_912
    for source in SOURCES:
        _assert_shared_source(source)


def _assert_shared_source(source: Mapping[str, Any]) -> None:
    matching = [item for item in THE_WELL if source["name"] in item["name"]]
    expected = 7 if source["name"] in EXTRA_SOURCES else 6
    assert len(matching) == expected
    specs = [DATASETS[item["name"]] for item in matching]
    assert all(isinstance(spec, Large) for spec in specs)
    cache_names = {
        _conversion_cache_name(spec, "all", "sample", 1)  # type: ignore[arg-type]
        for spec in specs
    }
    assert cache_names == {f"the-well-{source['name']}-sample.hdf5"}


def test_every_task_has_canonical_licence_origin_authors_and_transformation() -> None:
    for item in THE_WELL:
        spec = DATASETS[item["name"]]
        assert spec.licence == "CC BY 4.0"
        assert spec.creators
        assert spec.publisher == "Polymathic AI / The Well collaboration"
        assert spec.repository == "Hugging Face Hub"
        assert spec.origin.startswith("https://polymathic-ai.org/the_well/datasets/")
        assert spec.citation.startswith(("https://arxiv.org/", "https://doi.org/", "https://openreview.net/"))
        assert "central plane" in spec.transformation_note
        assert spec.mirrors == (("The Well source and format", "https://github.com/PolymathicAI/the_well"),)


def test_loader_resamples_raw_and_normalized_images_and_temporal_splits(
    well_file: Path,
) -> None:
    trees, columns, entries = load("active_matter:next_state", {"sample": well_file}, "all")
    made = list(entries)
    assert trees == ("train", "validation", "test")
    assert columns["image"] == ("f", PIXELS)
    assert columns["target_image"] == ("f", PIXELS)
    assert Counter(tree for tree, _row in made) == {0: 16, 1: 2, 2: 2}
    _assert_visual_row(made[0][1])


def _assert_visual_row(row: Mapping[str, Any]) -> None:
    assert min(row["normalized_image"]) == pytest.approx(0.0)
    assert max(row["normalized_image"]) == pytest.approx(1.0)
    assert sum(row["image_mask"]) == PIXELS
    assert row["target"] == pytest.approx(sum(row["target_image"]) / PIXELS)
    assert bytes(row["field"]).rstrip(b"\0") == b"density"


@pytest.mark.parametrize(
    ("task", "target"),
    (
        ("temporal_change", 1.0),
        ("threshold_mask", 48.5),
        ("mean_regression", 48.5),
        ("rms_regression", 55.858079),
    ),
)
def test_documented_derived_targets_are_reproducible(
    well_file: Path, task: str, target: float
) -> None:
    _trees, _columns, entries = load(f"active_matter:{task}", {"sample": well_file}, "all")
    _tree, row = next(entries)
    assert row["target"] == pytest.approx(target, rel=1e-5)
    if task == "threshold_mask":
        assert set(row["target_image"]) == {0.0, 1.0}


def test_conversion_and_the_generic_visualizer_read_the_field(well_file: Path) -> None:
    output = io.BytesIO()
    convert(
        "well_active_matter_temporal_change",
        output,
        split="all",
        parts={"sample": well_file},
    )
    with open_root(io.BytesIO(output.getvalue())) as back:
        assert len(back["train"]) == 16
        assert len(back["validation"]) == 2
        assert len(back["test"]) == 2
    image = visualize_2d(
        io.BytesIO(output.getvalue()),
        tree="train",
        branch="image",
        planes=None,
        plane=None,
        shape=(64, 64),
        normalization="minmax",
    )
    assert image.shape == (64, 64)
    assert image.raw_min == pytest.approx(0.0)
    assert image.raw_max == pytest.approx(95.0)


def test_unknown_task_split_and_missing_shard_are_rejected(well_file: Path) -> None:
    with pytest.raises(ValueError, match="no The Well converter"):
        load("active_matter:unknown", {"sample": well_file}, "all")
    with pytest.raises(ValueError, match="converted together"):
        load("active_matter:next_state", {"sample": well_file}, "train")
    with pytest.raises(ValueError, match="missing its sample shard"):
        load("active_matter:next_state", {}, "all")
