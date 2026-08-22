"""Pinned local replicas of the visual NIST JARVIS physics tasks."""

from __future__ import annotations

import concurrent.futures
import io
import json
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from xrd.ml import visualize_2d
from xrd.root import datasets as datasets_module
from xrd.root import open_root
from xrd.root._jarvis_physics import (
    DERIVED_TARGETS,
    DIRECT_TARGETS,
    JARVIS_PHYSICS,
    SPLIT_TREES,
)
from xrd.root._open_large import load
from xrd.root.datasets import DATASETS, Large, _conversion_cache_name, convert


def _source(path: Path, rows: list[dict[str, Any]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("jarvis.json", json.dumps(rows))


def _row(jid: int, target: Any = -1.25) -> dict[str, Any]:
    return {
        "jid": f"JVASP-{jid}",
        "formula": "NaCl",
        "formation_energy_peratom": target,
        "atoms": {
            "lattice_mat": [[2.0, 0.0, 0.0], [0.5, 3.0, 0.0], [0.2, 0.4, 4.0]],
            "coords": [[0.0, 0.0, 0.0], [1.35, 1.7, 2.0]],
            "elements": ["Na", "Cl"],
            "cartesian": True,
        },
    }


def test_one_hundred_visual_jarvis_physics_tasks_are_registered() -> None:
    names = {item["name"] for item in JARVIS_PHYSICS}
    three_dimensional = [name for name in names if name.startswith("jarvis_dft3d_")]
    two_dimensional = [name for name in names if name.startswith("jarvis_dft2d_")]
    actual = (
        len(DIRECT_TARGETS),
        len(DERIVED_TARGETS),
        len(names),
        len(JARVIS_PHYSICS),
        len(three_dimensional),
        len(two_dimensional),
    )
    assert actual == (32, 18, 100, 100, 50, 50)


def test_jarvis_tasks_share_two_small_pinned_sources() -> None:
    assert {item["source_bytes"] for item in JARVIS_PHYSICS} == {48_447_610, 8_394_653}
    assert {item["cache_names"]["archive"] for item in JARVIS_PHYSICS} == {
        "jarvis-dft3d-2025",
        "jarvis-dft2d-2022",
    }


def test_every_jarvis_task_keeps_its_canonical_attribution() -> None:
    names = {item["name"] for item in JARVIS_PHYSICS}
    for name in names:
        spec = DATASETS[name]
        actual = (
            isinstance(spec, Large),
            spec.licence,
            spec.within_source_ceiling(),
            spec.creators,
            spec.publisher,
            spec.repository,
            spec.origin.startswith("https://doi.org/10.6084/m9.figshare."),
            spec.mirrors,
        )
        expected = (
            True,
            "CC BY 4.0",
            True,
            ("Kamal Choudhary",),
            "National Institute of Standards and Technology",
            "Figshare",
            True,
            (("NIST JARVIS-DFT", "https://jarvis.nist.gov/jarvisdft/"),),
        )
        assert actual == expected


def test_shared_archives_have_one_cache_entry_per_pinned_source() -> None:
    first = DATASETS["jarvis_dft3d_formation_energy"]
    second = DATASETS["jarvis_dft3d_density"]
    other = DATASETS["jarvis_dft2d_density"]
    assert _conversion_cache_name(first, "all", "archive", 1) == "jarvis-dft3d-2025"
    assert _conversion_cache_name(second, "all", "archive", 1) == "jarvis-dft3d-2025"
    assert _conversion_cache_name(other, "all", "archive", 1) == "jarvis-dft2d-2022"


def test_shared_cache_serializes_concurrent_task_downloads(
    tmp_path: Path, monkeypatch: Any
) -> None:
    copied: list[int] = []
    original = datasets_module._copy_source

    def counted(source: Any, url: Any, handle: Any, config: Any) -> None:
        copied.append(1)
        time.sleep(0.05)
        original(source, url, handle, config)

    def fetch() -> tuple[Path, bool]:
        return datasets_module._fetch_file(
            b"physics", cache=tmp_path, name="shared", expected=0, config=None
        )

    monkeypatch.setattr(datasets_module, "_copy_source", counted)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch) for _ in range(4)]
        results = [future.result() for future in futures]

    assert copied == [1]
    assert {path for path, _temporary in results} == {tmp_path / "shared.source"}
    assert (tmp_path / "shared.source").read_bytes() == b"physics"


def test_published_targets_filter_missing_values_and_keep_stable_split_trees(
    tmp_path: Path,
) -> None:
    source = tmp_path / "jarvis.zip"
    _source(source, [_row(7, -1.0), _row(8, -2.0), _row(9, -3.0), _row(10, "na")])

    trees, columns, entries = load("jarvis:dft3d:formation_energy", {"archive": source}, "all")
    made = list(entries)
    assert trees == SPLIT_TREES
    assert columns["element_image"] == ("f", 128)
    assert columns["projection"] == ("B", 3 * 32 * 32)
    assert [tree for tree, _row_value in made] == [0, 1, 2]
    assert [row["target"] for _tree, row in made] == [-1.0, -2.0, -3.0]
    assert made[0][1]["fractional_positions"][3:6] == pytest.approx([0.5, 0.5, 0.5])
    assert sum(made[0][1]["element_image"]) == pytest.approx(1.0)
    assert max(made[0][1]["projection"]) == 17


def test_derived_geometry_becomes_a_trainable_root_target(tmp_path: Path) -> None:
    source = tmp_path / "jarvis.zip"
    _source(source, [_row(7), _row(8), _row(9)])
    output = io.BytesIO()

    assert convert("jarvis_dft3d_cell_volume", output, parts={"archive": source}) == {
        "train": 1,
        "validation": 1,
        "test": 1,
    }
    with open_root(io.BytesIO(output.getvalue())) as back:
        assert back["train"]["target"].array().tolist() == pytest.approx([24.0])
        assert back["validation"]["atoms"].array().tolist() == [2]
        assert back["test"]["split"].array().tolist() == [2]


def test_visualize_2d_reads_one_raw_and_normalized_crystal_entry(tmp_path: Path) -> None:
    source = tmp_path / "jarvis.zip"
    _source(source, [_row(7), _row(8), _row(9)])
    output = io.BytesIO()
    convert("jarvis_dft2d_cell_volume", output, parts={"archive": source})

    image = visualize_2d(io.BytesIO(output.getvalue()), tree="train", entry=0, plane="xy")

    assert (image.shape, image.jid, image.formula, image.target) == (
        (32, 32),
        "JVASP-7",
        "NaCl",
        pytest.approx(24.0),
    )
    assert image.raw[0][0] == 11
    assert image.raw[16][16] == 17
    assert image.raw_max == 17
    assert image.normalized[0][0] == pytest.approx(11 / 17)
    assert image.normalized[16][16] == 1.0


def test_visualize_2d_checks_plane_tree_and_entry(tmp_path: Path) -> None:
    source = tmp_path / "jarvis.zip"
    _source(source, [_row(7), _row(8), _row(9)])
    output = io.BytesIO()
    convert("jarvis_dft2d_cell_volume", output, parts={"archive": source})
    content = output.getvalue()

    with pytest.raises(ValueError, match="plane must be"):
        visualize_2d(io.BytesIO(content), plane="ab")
    with pytest.raises(KeyError, match="no 'missing' tree"):
        visualize_2d(io.BytesIO(content), tree="missing")
    with pytest.raises(IndexError, match="outside 'train'"):
        visualize_2d(io.BytesIO(content), entry=2)

    last = visualize_2d(io.BytesIO(content), entry=-1)
    assert last.entry == 0


def test_visualize_2d_plots_both_forms_when_matplotlib_is_available(tmp_path: Path) -> None:
    matplotlib = pytest.importorskip("matplotlib")
    matplotlib.use("Agg", force=True)
    from matplotlib import pyplot

    source = tmp_path / "jarvis.zip"
    _source(source, [_row(7), _row(8), _row(9)])
    output = io.BytesIO()
    convert("jarvis_dft2d_cell_volume", output, parts={"archive": source})
    image = visualize_2d(io.BytesIO(output.getvalue()))

    figure, axes = image.plot()
    assert [axis.get_title() for axis in axes] == ["Raw values", "Normalized (max)"]
    assert len(figure.axes) == 4  # two images and their two colour bars
    pyplot.close(figure)


def test_jarvis_reader_rejects_ambiguous_archives(tmp_path: Path) -> None:
    source = tmp_path / "ambiguous.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("first.json", "[]")
        archive.writestr("second.json", "[]")
    _trees, _columns, entries = load("jarvis:dft2d:density", {"archive": source}, "all")
    with pytest.raises(ValueError, match="exactly one JSON member"):
        next(entries)
