"""Small local replicas of the condensed-matter source formats."""

from __future__ import annotations

import gzip
import io
import json
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

from xrd.root import open_root
from xrd.root._condensed_matter import (
    AFM_CHANNELS,
    CONDENSED_MATTER,
    JARVIS_CLASSES,
    MATBENCH_TASKS,
    NANOWIRE_CLASSES,
    NFFA_CLASSES,
    PEROVSKITE_CLASSES,
    VISUAL_CONDENSED_MATTER,
    _afm_channels,
    _formula_amounts,
    _wse2_bounds,
    _wse2_row,
)
from xrd.root._open_large import load
from xrd.root.datasets import DATASETS, convert


def _picture(mode: str, size: tuple[int, int], value: Any, kind: str = "PNG") -> bytes:
    image_module = pytest.importorskip("PIL.Image")
    held = io.BytesIO()
    image_module.new(mode, size, value).save(held, format=kind)
    return held.getvalue()


def _tar_picture(path: Path, name: str, picture: bytes) -> None:
    with tarfile.open(path, "w") as archive:
        member = tarfile.TarInfo(name)
        member.size = len(picture)
        archive.addfile(member, io.BytesIO(picture))


def _matbench_source(path: Path, rows: list[list[Any]]) -> None:
    document = {"index": list(range(len(rows))), "columns": ["input", "target"], "data": rows}
    with gzip.open(path, "wt", encoding="utf-8") as held:
        json.dump(document, held)


def _simple_structure() -> dict[str, Any]:
    return {
        "lattice": {"matrix": [[2, 0, 0], [0, 2, 0], [0, 0, 3]]},
        "sites": [
            {"abc": [0, 0, 0], "species": [{"element": "Fe", "occu": 1}]},
            {
                "abc": [0.5, 0.5, 0.25],
                "species": [
                    {"element": "Ni", "occu": 0.75},
                    {"element": "Co", "occu": 0.25},
                ],
            },
        ],
    }


def _assert_open_spec(name: str) -> None:
    spec = DATASETS[name]
    assert spec.within_source_ceiling()
    assert spec.creators
    assert spec.origin
    assert spec.repository
    assert spec.citation
    assert spec.transformation_summary()
    assert spec.source_payload_bytes() < 2_000_000_000


def test_twenty_open_condensed_matter_problems_are_registered() -> None:
    names = {item["name"] for item in CONDENSED_MATTER}
    assert len(names) == len(CONDENSED_MATTER) == 20
    assert len(VISUAL_CONDENSED_MATTER) == 7
    assert len(MATBENCH_TASKS) == 13
    assert sum(item["source_bytes"] for item in CONDENSED_MATTER) == 7_628_822_980
    for name in names:
        _assert_open_spec(name)


def test_jarvis_pairs_positive_and_negative_bias_images(tmp_path: Path) -> None:
    images = tmp_path / "stm.zip"
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"JVASP-7": 4}))
    with zipfile.ZipFile(images, "w") as archive:
        archive.writestr("stm/JVASP-7_pos.jpg", _picture("RGB", (12, 6), (1, 2, 3), "JPEG"))
        archive.writestr("stm/JVASP-7_neg.jpg", _picture("RGB", (6, 12), (4, 5, 6), "JPEG"))

    classes, columns, entries = load(
        "condensed:jarvis_stm", {"images": images, "labels": labels}, "all"
    )
    tree, row = next(entries)
    assert classes == JARVIS_CLASSES and columns["positive_bias"] == ("B", 256 * 256 * 3)
    assert tree == row["label"] == 4
    assert row["positive_width"] == 12 and row["negative_height"] == 12
    assert bytes(row["jid"]).rstrip(b"\0") == b"JVASP-7"


def test_nffa_streams_each_selected_sem_tar_into_its_class_tree(tmp_path: Path) -> None:
    paths = {}
    for at, role in enumerate(NFFA_CLASSES):
        path = tmp_path / f"{role}.tar"
        _tar_picture(path, f"batch/{role}.png", _picture("L", (8, 4), at))
        paths[role] = path

    classes, columns, entries = load("condensed:nffa_sem", paths, "all")
    made = list(entries)
    assert classes == NFFA_CLASSES and columns["image"] == ("B", 256 * 256)
    assert [tree for tree, _row in made] == list(range(5))
    assert [row["source_width"] for _tree, row in made] == [8] * 5


def test_tem_morphology_folders_become_three_balanced_trees(tmp_path: Path) -> None:
    source = tmp_path / "tem.zip"
    folders = ("Dispersed Nanoparticles", "Separate Clusters", "Percolating Cluster")
    with zipfile.ZipFile(source, "w") as archive:
        for at, folder in enumerate(folders):
            archive.writestr(f"dataset/{folder}/{at}.jpg", _picture("L", (9, 6), at, "JPEG"))

    classes, columns, entries = load("condensed:nanowire_tem", {"archive": source}, "all")
    made = list(entries)
    assert classes == NANOWIRE_CLASSES and columns["label"] == "i"
    assert [tree for tree, _row in made] == [0, 1, 2]


def test_moke_converts_the_publishers_rgb_mask_palette(tmp_path: Path) -> None:
    source = tmp_path / "moke.zip"
    prefix = "public_unet_skyrmion_dataset"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(f"{prefix}/table.csv", "source_id;img_fn\n17;sample.png\n")
        archive.writestr(f"{prefix}/partition.txt", "only_training;17\ntrain_test_val;18\n")
        archive.writestr(f"{prefix}/images/sample.png", _picture("L", (3, 3), 29))
        archive.writestr(f"{prefix}/labels/sample.png", _picture("RGB", (3, 3), (255, 0, 0)))

    classes, columns, entries = load("condensed:moke_skyrmions", {"archive": source}, "all")
    _tree, row = next(entries)
    assert classes == ("samples",) and columns["mask"] == ("B", 256 * 256)
    assert set(row["mask"]) == {1} and row["class_fractions"] == pytest.approx([0, 1, 0])
    assert row["source_id"] == 17 and row["partition_group"] == 0


def test_moke_maps_quantized_mask_colours_to_the_nearest_publisher_class(tmp_path: Path) -> None:
    source = tmp_path / "moke-quantized.zip"
    prefix = "public_unet_skyrmion_dataset"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(f"{prefix}/table.csv", "source_id;img_fn\n17;sample.png\n")
        archive.writestr(f"{prefix}/partition.txt", "only_training;17\ntrain_test_val;18\n")
        archive.writestr(f"{prefix}/images/sample.png", _picture("L", (3, 3), 29))
        archive.writestr(f"{prefix}/labels/sample.png", _picture("RGB", (3, 3), (254, 2, 1)))

    _classes, _columns, entries = load("condensed:moke_skyrmions", {"archive": source}, "all")
    _tree, row = next(entries)

    assert set(row["mask"]) == {1}


def test_perovskite_labelme_polygons_and_circles_become_a_mask(tmp_path: Path) -> None:
    source = tmp_path / "perovskite.zip"
    document = {
        "imageWidth": 20,
        "imageHeight": 20,
        "shapes": [
            {"label": "ABO₃", "shape_type": "polygon", "points": [[0, 0], [5, 0], [5, 5]]},
            {"label": "PBI2", "shape_type": "circle", "points": [[7, 7], [9, 7]]},
        ],
    }
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("seg/field.png", _picture("RGB", (10, 10), (10, 20, 30)))
        archive.writestr("seg/field.json", json.dumps(document))

    classes, columns, entries = load("condensed:perovskite_sem", {"archive": source}, "all")
    _tree, row = next(entries)
    assert classes == ("samples",) and columns["image"] == ("B", 256 * 256 * 3)
    assert row["objects"] == 2 and {0, 1, 3} <= set(row["mask"])
    assert len(row["class_fractions"]) == len(PEROVSKITE_CLASSES)
    assert (row["source_width"], row["annotation_width"]) == (10, 20)


def test_matbench_compositions_are_normalized_as_element_images(tmp_path: Path) -> None:
    source = tmp_path / "glass.json.gz"
    _matbench_source(source, [["Al(NiB)2", True], ["Fe.5Co.5", False]])
    classes, columns, entries = load(
        "condensed:matbench:matbench_glass", {"archive": source}, "all"
    )
    made = list(entries)
    assert classes == ("not_glass_forming", "glass_forming") and columns["label"] == "i"
    assert [tree for tree, _row in made] == [1, 0]
    assert sum(made[0][1]["element_image"]) == pytest.approx(1)
    assert _formula_amounts("Al(NiB)2") == {"Al": 1, "Ni": 2, "B": 2}


def test_matbench_structures_retain_lattice_disorder_and_projections(tmp_path: Path) -> None:
    source = tmp_path / "phonons.json.gz"
    _matbench_source(source, [[_simple_structure(), 123.5]])
    classes, columns, entries = load(
        "condensed:matbench:matbench_phonons", {"archive": source}, "all"
    )
    _tree, row = next(entries)
    assert classes == ("samples",) and columns["projection"] == ("B", 3 * 32 * 32)
    assert row["target"] == 123.5 and row["sites"] == 2 and row["components"] == 3
    assert row["atomic_numbers"][:3].tolist() == [26, 28, 27]
    assert row["occupancies"][:3] == pytest.approx([1, 0.75, 0.25])
    assert max(row["projection"]) == 28

    output = io.BytesIO()
    assert convert("matbench_phonons", output, parts={"archive": source}) == {"samples": 1}
    with open_root(io.BytesIO(output.getvalue())) as back:
        assert back["samples"]["target"].array().tolist() == [123.5]
        assert back["samples"]["components"].array().tolist() == [3]


def test_wse2_and_afm_helpers_validate_their_dense_arrays() -> None:
    numpy = pytest.importorskip("numpy")
    image = numpy.arange(4, dtype="float32").reshape(2, 2)
    mask = numpy.array([[0, 1], [2, 2]], dtype="float64")
    row = _wse2_row(image, mask, 9)
    assert row["mask"].tolist() == [0, 1, 2, 2]
    assert row["class_fractions"] == pytest.approx([0.25, 0.25, 0.5])
    assert _wse2_bounds("validation") == (1461, 2180)

    wave = {"wData": numpy.zeros((384, 384, len(AFM_CHANNELS)), dtype="float32")}
    channels = _afm_channels(wave, "sample.ibw")
    assert tuple(channels) == AFM_CHANNELS
    assert all(len(values) == 384 * 384 for values in channels.values())
