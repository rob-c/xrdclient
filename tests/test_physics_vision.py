"""Local replicas of the compact physics-vision source formats."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from xrd.root import open_root
from xrd.root._open_large import load
from xrd.root._physics_vision import (
    GALAXY10_CLASSES,
    MARS_CLASSES,
    MEDMNIST_SPECS,
    PHYSICS_VISION,
    SWEFIL_MAX_OBJECTS,
    SWEFIL_ROLES,
    SWEFIL_SIDE,
)
from xrd.root._swefil_manifest import SWEFIL_FILES, SWEFIL_SOURCE_BYTES
from xrd.root.datasets import DATASETS, convert

MappingForTest = dict[str, object]


def _picture(mode: str, size: tuple[int, int], value: Any, kind: str = "JPEG") -> bytes:
    image_module = pytest.importorskip("PIL.Image")
    held = io.BytesIO()
    image_module.new(mode, size, value).save(held, format=kind)
    return held.getvalue()


def _medmnist_npz(path: Path, images: Any, labels: Any, *, prefix: str = "train") -> None:
    numpy = pytest.importorskip("numpy")
    numpy.savez(path, **{f"{prefix}_images": images, f"{prefix}_labels": labels})


def _assert_open_physics_spec(name: str) -> None:
    spec = DATASETS[name]
    assert spec.licence in {"CC BY 4.0", "CC BY-SA 4.0"}
    assert spec.source_payload_bytes() < 2_000_000_000
    assert spec.creators and spec.origin and spec.repository and spec.citation


def test_twenty_distinct_open_physics_vision_problems_are_registered() -> None:
    names = {item["name"] for item in PHYSICS_VISION}
    assert len(names) == len(PHYSICS_VISION) == 20
    assert len(MEDMNIST_SPECS) == 17 and "dermamnist" not in names
    assert {"galaxy10_sdss", "mars_surface_images", "swefil"} < names
    for name in names:
        _assert_open_physics_spec(name)
    assert DATASETS["swefil"].source_payload_bytes() == SWEFIL_SOURCE_BYTES
    assert len(DATASETS["swefil"].urls("train")) == len(SWEFIL_FILES) == 556


def test_medmnist_preserves_single_label_pixels_and_writes_class_trees(tmp_path: Path) -> None:
    numpy = pytest.importorskip("numpy")
    source = tmp_path / "pathmnist.npz"
    images = numpy.zeros((2, 28, 28, 3), dtype=numpy.uint8)
    images[0, 0, 0] = (1, 2, 3)
    labels = numpy.array([[2], [8]], dtype=numpy.uint8)
    _medmnist_npz(source, images, labels)

    classes, columns, rows = load(
        "vision:medmnist:pathmnist", {"archive": source}, "train"
    )
    assert classes[2] == "debris" and columns["image"] == ("B", 28 * 28 * 3)
    made = list(rows)
    assert [tree for tree, _row in made] == [2, 8]
    assert made[0][1]["image"][:4].tolist() == [1, 2, 3, 0]

    output = io.BytesIO()
    written = convert("pathmnist", output, split="train", parts={"archive": source})
    assert written["train_debris"] == 1
    assert written["train_adenocarcinoma_epithelium"] == 1
    with open_root(io.BytesIO(output.getvalue())) as back:
        assert back["train_debris"]["label"].array().tolist() == [2]


def test_chestmnist_keeps_the_fourteen_binary_targets_in_one_tree(tmp_path: Path) -> None:
    numpy = pytest.importorskip("numpy")
    source = tmp_path / "chestmnist.npz"
    images = numpy.full((1, 28, 28), 7, dtype=numpy.uint8)
    labels = numpy.array([[1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1]], dtype=numpy.uint8)
    _medmnist_npz(source, images, labels, prefix="val")

    classes, columns, rows = load(
        "vision:medmnist:chestmnist", {"archive": source}, "validation"
    )
    assert classes == ("samples",) and columns["targets"] == ("B", 14)
    tree, row = next(rows)
    assert tree == 0 and row["targets"].tolist() == labels[0].tolist()
    assert len(row["image"]) == 28 * 28 and set(row["image"]) == {7}


def test_medmnist_three_dimensional_volumes_are_not_flattened_away(tmp_path: Path) -> None:
    numpy = pytest.importorskip("numpy")
    source = tmp_path / "vesselmnist3d.npz"
    images = numpy.zeros((1, 28, 28, 28), dtype=numpy.uint8)
    images[0, 1, 2, 3] = 255
    labels = numpy.array([[1]], dtype=numpy.uint8)
    _medmnist_npz(source, images, labels, prefix="test")

    classes, columns, rows = load(
        "vision:medmnist:vesselmnist3d", {"archive": source}, "test"
    )
    assert classes == ("vessel", "aneurysm") and columns["image"] == ("B", 28**3)
    tree, row = next(rows)
    assert tree == 1 and len(row["image"]) == 28**3 and max(row["image"]) == 255


def test_galaxy10_streams_the_real_hdf5_geometry_one_image_at_a_time(tmp_path: Path) -> None:
    h5py = pytest.importorskip("h5py")
    source = tmp_path / "galaxy10.h5"
    with h5py.File(source, "w") as book:
        images = book.create_dataset(
            "images", shape=(21_785, 69, 69, 3), dtype="u1", chunks=(1, 69, 69, 3)
        )
        labels = book.create_dataset("ans", shape=(21_785,), dtype="u1")
        images[0, 0, 0] = (11, 12, 13)
        labels[0] = 7

    classes, columns, rows = load("vision:galaxy10", {"archive": source}, "all")
    assert classes == GALAXY10_CLASSES and columns["image"] == ("B", 69 * 69 * 3)
    tree, row = next(rows)
    assert tree == 7 and row["image"][:4].tolist() == [11, 12, 13, 0]


def test_mars_retains_publisher_split_sol_instrument_and_letterbox(tmp_path: Path) -> None:
    source = tmp_path / "mars.zip"
    filename = "0292MH0000000000000000E00_DRCL.JPG"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("train-calibrated-shuffled.txt", "")
        archive.writestr("val-calibrated-shuffled.txt", f"calibrated/{filename} 24\n")
        archive.writestr("test-calibrated-shuffled.txt", "")
        archive.writestr(f"calibrated/{filename}", _picture("L", (256, 192), 99))

    classes, columns, rows = load("vision:mars", {"archive": source}, "validation")
    assert classes == MARS_CLASSES and columns["image"] == ("B", 256 * 256 * 3)
    tree, row = next(rows)
    assert tree == 24 and row["sol"] == 292 and row["instrument"] == 2
    assert row["source_grayscale"] == 1 and row["top_padding"] == 32
    assert set(row["image"][: 32 * 256 * 3]) == {0}


def _swefil_parts(tmp_path: Path, document: MappingForTest) -> dict[str, Path]:
    dummy = tmp_path / "unused"
    dummy.write_bytes(b"")
    annotations = tmp_path / "train.json"
    annotations.write_text(json.dumps(document))
    picture = tmp_path / "sun.jpg"
    picture.write_bytes(_picture("RGB", (2048, 2048), (10, 20, 30)))
    paths = dict.fromkeys(SWEFIL_ROLES, dummy)
    for role, (name, _size) in zip(SWEFIL_ROLES, SWEFIL_FILES):
        if name == "train.json":
            paths[role] = annotations
        elif name == "images/20100604185114Th.jpg":
            paths[role] = picture
    return paths

def test_swefil_rasterizes_coco_masks_and_keeps_boxes_categories_and_views(
    tmp_path: Path,
) -> None:
    document: MappingForTest = {
        "categories": [
            {"id": 0, "name": "QRF"},
            {"id": 1, "name": "IRF"},
            {"id": 2, "name": "ARF"},
            {"id": 3, "name": "SUNSPOT"},
        ],
        "images": [
            {
                "id": 1,
                "file_name": "images/20100604185114Th.jpg",
                "width": 2048,
                "height": 2048,
            }
        ],
        "annotations": [
            {
                "id": 9,
                "image_id": 1,
                "category_id": 2,
                "area": 65_536,
                "bbox": [512, 512, 256, 256],
                "segmentation": [[512, 512, 768, 512, 768, 768, 512, 768]],
                "iscrowd": 0,
            }
        ],
    }
    classes, columns, rows = load("vision:swefil", _swefil_parts(tmp_path, document), "train")
    assert classes == ("processed", "raw")
    assert columns["boxes"] == ("f", SWEFIL_MAX_OBJECTS * 4)
    assert columns["arf_mask"] == ("B", SWEFIL_SIDE * SWEFIL_SIDE)
    tree, row = next(rows)
    assert tree == 0 and row["objects"] == 1 and row["timestamp"] == 20100604185114
    assert row["boxes"][:4] == pytest.approx([0.25, 0.25, 0.125, 0.125])
    assert row["object_labels"][:2].tolist() == [2, -1]
    assert row["annotation_ids"][:2].tolist() == [9, -1]
    assert sum(row["arf_mask"]) > 0 and sum(row["qrf_mask"]) == 0
    assert len(row["image"]) == SWEFIL_SIDE * SWEFIL_SIDE * 3
