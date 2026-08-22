"""One hundred visual materials-physics tasks from Alex-MP-20.

Alex-MP-20 combines MatterGen's MP-20 structures with Alexandria and publishes
stable train, validation and test splits.  Every logical task keeps the same
bounded, high-entry crystal representation: the raw lattice and atoms, a
normalized periodic-table image, an occupancy-only image, and three projected
crystal planes.  Targets are either published observables or transparent
geometry/composition quantities derived from the published structure.
"""

from __future__ import annotations

import array
import importlib
import math
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from ._condensed_matter import ATOMIC_NUMBER
from ._jarvis_physics import (
    CRYSTAL_SIDE,
    ELEMENT_PIXELS,
    _angle,
    _cross,
    _determinant,
    _deviation,
    _element_image,
    _fixed_bytes,
    _inverse,
    _length,
    _mean,
    _projection,
    _row_times_matrix,
)

Rows = Iterator[tuple[int, dict[str, Any]]]
Loaded = tuple[tuple[str, ...], dict[str, Any], Rows]

MAX_ATOMS = 20
MATERIAL_ID_BYTES = 32
SPACE_GROUP_BYTES = 32
CHEMICAL_SYSTEM_BYTES = 64
SPLITS = ("train", "validation", "test")
SPLIT_INDEX = {name: index for index, name in enumerate(SPLITS)}
SOURCE_COLUMNS = (
    "positions",
    "cell",
    "atomic_numbers",
    "material_id",
    "space_group",
    "chemical_system",
    "energy_above_hull",
    "dft_band_gap",
    "dft_bulk_modulus",
    "dft_mag_density",
    "hhi_score",
    "ml_bulk_modulus",
)

DIRECT_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("energy_above_hull", "energy_above_hull", "energy above the convex hull"),
    ("dft_band_gap", "dft_band_gap", "DFT band gap"),
    ("dft_bulk_modulus", "dft_bulk_modulus", "DFT bulk modulus"),
    ("dft_magnetic_density", "dft_mag_density", "DFT magnetic density"),
    ("hhi_score", "hhi_score", "Herfindahl-Hirschman supply-risk score"),
    ("ml_bulk_modulus", "ml_bulk_modulus", "MatterSim bulk modulus"),
)

DERIVED_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("atom_count", "atom_count", "unit-cell atom count"),
    ("lattice_a", "lattice_a", "first lattice-vector length"),
    ("lattice_b", "lattice_b", "second lattice-vector length"),
    ("lattice_c", "lattice_c", "third lattice-vector length"),
    ("angle_alpha", "angle_alpha", "alpha lattice angle"),
    ("angle_beta", "angle_beta", "beta lattice angle"),
    ("angle_gamma", "angle_gamma", "gamma lattice angle"),
    ("cell_volume", "cell_volume", "unit-cell volume"),
    ("volume_per_atom", "volume_per_atom", "unit-cell volume per atom"),
    ("lattice_length_mean", "lattice_length_mean", "mean lattice-vector length"),
    ("lattice_length_std", "lattice_length_std", "lattice-vector length deviation"),
    ("lattice_length_min", "lattice_length_min", "minimum lattice-vector length"),
    ("lattice_length_max", "lattice_length_max", "maximum lattice-vector length"),
    ("lattice_length_range", "lattice_length_range", "lattice-vector length range"),
    ("lattice_anisotropy", "lattice_anisotropy", "lattice length anisotropy"),
    ("lattice_angle_mean", "lattice_angle_mean", "mean lattice angle"),
    ("lattice_angle_std", "lattice_angle_std", "lattice-angle deviation"),
    ("lattice_angle_min", "lattice_angle_min", "minimum lattice angle"),
    ("lattice_angle_max", "lattice_angle_max", "maximum lattice angle"),
    ("lattice_angle_range", "lattice_angle_range", "lattice-angle range"),
    ("cell_surface_area", "cell_surface_area", "unit-cell surface area"),
    ("cell_compactness", "cell_compactness", "scale-free unit-cell compactness"),
    ("element_count", "element_count", "number of distinct elements"),
    ("atomic_number_mean", "atomic_number_mean", "mean atomic number"),
    ("atomic_number_std", "atomic_number_std", "atomic-number deviation"),
    ("atomic_number_min", "atomic_number_min", "minimum atomic number"),
    ("atomic_number_max", "atomic_number_max", "maximum atomic number"),
    ("atomic_number_range", "atomic_number_range", "atomic-number range"),
    ("atomic_number_sum", "atomic_number_sum", "sum of atomic numbers"),
    ("atomic_number_median", "atomic_number_median", "median atomic number"),
    ("composition_entropy", "composition_entropy", "elemental-composition entropy"),
    (
        "dominant_element_fraction",
        "dominant_element_fraction",
        "largest elemental stoichiometric fraction",
    ),
)

# Elements represented often enough in broad inorganic structure collections
# to make useful stoichiometry exercises.  Noble gases and technetium are
# intentionally absent; the final 62 targets complete the 100-task shelf.
ELEMENTS = (
    "H",
    "Li",
    "Be",
    "B",
    "C",
    "N",
    "O",
    "F",
    "Na",
    "Mg",
    "Al",
    "Si",
    "P",
    "S",
    "Cl",
    "K",
    "Ca",
    "Sc",
    "Ti",
    "V",
    "Cr",
    "Mn",
    "Fe",
    "Co",
    "Ni",
    "Cu",
    "Zn",
    "Ga",
    "Ge",
    "As",
    "Se",
    "Br",
    "Rb",
    "Sr",
    "Y",
    "Zr",
    "Nb",
    "Mo",
    "Ru",
    "Rh",
    "Pd",
    "Ag",
    "Cd",
    "In",
    "Sn",
    "Sb",
    "Te",
    "I",
    "Cs",
    "Ba",
    "La",
    "Ce",
    "Pr",
    "Nd",
    "Sm",
    "Eu",
    "Gd",
    "Tb",
    "Dy",
    "Ho",
    "Er",
    "Tm",
)

TARGETS: tuple[dict[str, Any], ...] = (
    *(
        {"slug": slug, "kind": "direct", "key": key, "label": label}
        for slug, key, label in DIRECT_TARGETS
    ),
    *(
        {"slug": slug, "kind": "derived", "key": key, "label": label}
        for slug, key, label in DERIVED_TARGETS
    ),
    *(
        {
            "slug": f"fraction_{symbol.lower()}",
            "kind": "element",
            "key": ATOMIC_NUMBER[symbol],
            "label": f"{symbol} stoichiometric fraction",
        }
        for symbol in ELEMENTS
    ),
)

CREATORS = (
    "Claudio Zeni",
    "Robert Pinsler",
    "Daniel Zügner",
    "Andrew Fowler",
    "Matthew Horton",
    "Xiang Fu",
    "Zilong Wang",
    "Aliaksandra Shysheya",
    "Jonathan Crabbé",
    "Shoko Ueda",
    "Roberto Sordillo",
    "Lixin Sun",
    "Jake Smith",
    "Bichlien Nguyen",
    "Hannes Schulz",
    "Sarah Lewis",
    "Chin-Wei Huang",
    "Ziheng Lu",
    "Yichi Zhou",
    "Han Yang",
)

SOURCE = {
    "source": "https://huggingface.co/datasets/OMatG/Alex-MP-20",
    "origin": "https://doi.org/10.1038/s41586-025-08628-5",
    "source_bytes": 197_512_780,
    "sources": {
        "train": (
            "https://huggingface.co/datasets/OMatG/Alex-MP-20/resolve/"
            "refs%2Fconvert%2Fparquet/default/train/0000.parquet"
        ),
        "validation": (
            "https://huggingface.co/datasets/OMatG/Alex-MP-20/resolve/"
            "refs%2Fconvert%2Fparquet/default/val/0000.parquet"
        ),
        "test": (
            "https://huggingface.co/datasets/OMatG/Alex-MP-20/resolve/"
            "refs%2Fconvert%2Fparquet/default/test/0000.parquet"
        ),
    },
    "source_sizes": {
        "train": 155_967_383,
        "validation": 20_766_698,
        "test": 20_778_699,
    },
    "cache_names": {
        "train": "alex-mp20-train-2026.parquet",
        "validation": "alex-mp20-validation-2026.parquet",
        "test": "alex-mp20-test-2026.parquet",
    },
}


def _target_provenance(target: Mapping[str, Any]) -> tuple[str, str]:
    kind = target["kind"]
    if kind == "direct":
        return (
            f"retained the published {target['key']} field as the target",
            "discarded only rows where that target is missing or non-finite",
        )
    if kind == "element":
        return (
            f"derived the {target['label']} from the published atomic numbers",
            "used every structurally valid row",
        )
    return (
        f"derived the {target['label']} from the published cell and atomic numbers",
        "used every structurally valid row",
    )


def _declaration(target: Mapping[str, Any]) -> dict[str, Any]:
    provenance, selection = _target_provenance(target)
    quantity = "up to 675,204" if target["kind"] == "direct" else "675,204"
    return {
        "name": f"alex_mp20_{target['slug']}",
        "label": f"Alex-MP-20 {target['label']}",
        "title": f"{quantity} inorganic crystal structures for {target['label']}",
        "licence": "CC BY 4.0",
        "source": SOURCE["source"],
        "source_bytes": SOURCE["source_bytes"],
        "sources": SOURCE["sources"],
        "source_sizes": SOURCE["source_sizes"],
        "cache_names": SOURCE["cache_names"],
        "converter": f"open:alex_mp20:{target['slug']}",
        "transformation": (
            "streamed OMatG's complete Hugging Face Parquet train/validation/test shards "
            "without reordering; "
            f"{selection} and {provenance}; converted Cartesian positions to wrapped "
            "fractional coordinates; retained the cell, padded atoms and source identifiers; "
            "derived a normalized 8x16 elemental image, an occupancy-only 32x32 image and "
            "three atomic-number 32x32 crystal projections"
        ),
        "classes": (),
        "splits": SPLITS,
        "requires": ("pyarrow",),
        "modality": "visualized inorganic crystal structures",
        "task": f"crystal-image {target['label']} regression",
        "basket_size": 2 * 1024 * 1024,
        "layout": "train_samples, validation_samples and test_samples TTrees",
        "creators": CREATORS,
        "publisher": "Open Materials Generation (OMatG)",
        "origin": SOURCE["origin"],
        "repository": "Hugging Face",
        "mirrors": (
            (
                "Alexandria source collection",
                "https://doi.org/10.24435/materialscloud:m7-50",
            ),
            ("MatterGen source and models", "https://github.com/microsoft/mattergen"),
        ),
        "citation": SOURCE["origin"],
    }


ALEX_MP20: tuple[dict[str, Any], ...] = tuple(_declaration(target) for target in TARGETS)
TARGET_BY_SLUG = {str(target["slug"]): target for target in TARGETS}


def _vector(values: Any, description: str) -> tuple[float, float, float]:
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        raise ValueError(f"Alex-MP-20 {description} is not a three-vector")
    try:
        result = tuple(float(value) for value in values)
    except (TypeError, ValueError):
        raise ValueError(f"Alex-MP-20 {description} contains a non-numeric value") from None
    if not all(math.isfinite(value) for value in result):
        raise ValueError(f"Alex-MP-20 {description} contains a non-finite value")
    return result  # type: ignore[return-value]


def _matrix(values: Any) -> tuple[tuple[float, float, float], ...]:
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        raise ValueError("Alex-MP-20 row has no 3x3 cell")
    matrix = tuple(_vector(row, "cell row") for row in values)
    if abs(_determinant(matrix)) < 1e-12:
        raise ValueError("Alex-MP-20 cell is singular")
    return matrix


def _numbers(values: Any) -> list[int]:
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= MAX_ATOMS:
        raise ValueError(f"Alex-MP-20 row must have 1 to {MAX_ATOMS} atoms")
    try:
        numbers = [int(value) for value in values]
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Alex-MP-20 atomic numbers are invalid") from None
    if any(number < 1 or number > 118 for number in numbers):
        raise ValueError("Alex-MP-20 row has an unknown atomic number")
    return numbers


def _positions(
    values: Any, matrix: Sequence[Sequence[float]], count: int
) -> list[tuple[float, float, float]]:
    if not isinstance(values, (list, tuple)) or len(values) != count:
        raise ValueError("Alex-MP-20 coordinate and atom counts differ")
    inverse = _inverse(matrix)
    cartesian = [_vector(position, "atomic position") for position in values]
    fractional = [_row_times_matrix(position, inverse) for position in cartesian]
    return [tuple(value % 1.0 for value in position) for position in fractional]  # type: ignore[misc]


def _padded_numbers(numbers: Sequence[int]) -> array.array[int]:
    values = array.array("B", [0]) * MAX_ATOMS
    values[: len(numbers)] = array.array("B", numbers)
    return values


def _padded_positions(positions: Sequence[Sequence[float]]) -> array.array[float]:
    values = array.array("f", [0.0]) * (MAX_ATOMS * 3)
    flat = (coordinate for position in positions for coordinate in position)
    values[: len(positions) * 3] = array.array("f", flat)
    return values


def _occupancy_projection(positions: Sequence[Sequence[float]]) -> array.array[int]:
    image = array.array("B", [0]) * (3 * CRYSTAL_SIDE * CRYSTAL_SIDE)
    for position in positions:
        bins = tuple(min(CRYSTAL_SIDE - 1, int(value * CRYSTAL_SIDE)) for value in position)
        for plane, (first, second) in enumerate(
            ((bins[0], bins[1]), (bins[0], bins[2]), (bins[1], bins[2]))
        ):
            pixel = plane * CRYSTAL_SIDE**2 + second * CRYSTAL_SIDE + first
            image[pixel] = min(255, image[pixel] + 1)
    return image


def _median(values: Sequence[int]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _composition_values(numbers: Sequence[int]) -> dict[str, float]:
    counts = Counter(numbers)
    fractions = [count / len(numbers) for count in counts.values()]
    return {
        "element_count": float(len(counts)),
        "atomic_number_mean": _mean(numbers),
        "atomic_number_std": _deviation(numbers),
        "atomic_number_min": float(min(numbers)),
        "atomic_number_max": float(max(numbers)),
        "atomic_number_range": float(max(numbers) - min(numbers)),
        "atomic_number_sum": float(sum(numbers)),
        "atomic_number_median": _median(numbers),
        "composition_entropy": -sum(value * math.log(value) for value in fractions),
        "dominant_element_fraction": max(fractions),
    }


def _geometry_values(
    matrix: Sequence[Sequence[float]], numbers: Sequence[int]
) -> dict[str, float]:
    lengths = tuple(_length(vector) for vector in matrix)
    angles = (
        _angle(matrix[1], matrix[2]),
        _angle(matrix[0], matrix[2]),
        _angle(matrix[0], matrix[1]),
    )
    volume = abs(_determinant(matrix))
    mean_length = _mean(lengths)
    face_pairs = ((matrix[0], matrix[1]), (matrix[0], matrix[2]), (matrix[1], matrix[2]))
    surface = 2.0 * sum(
        _length(_cross(first, second)) for first, second in face_pairs
    )
    return {
        "atom_count": float(len(numbers)),
        "lattice_a": lengths[0],
        "lattice_b": lengths[1],
        "lattice_c": lengths[2],
        "angle_alpha": angles[0],
        "angle_beta": angles[1],
        "angle_gamma": angles[2],
        "cell_volume": volume,
        "volume_per_atom": volume / len(numbers),
        "lattice_length_mean": mean_length,
        "lattice_length_std": _deviation(lengths),
        "lattice_length_min": min(lengths),
        "lattice_length_max": max(lengths),
        "lattice_length_range": max(lengths) - min(lengths),
        "lattice_anisotropy": max(lengths) / min(lengths),
        "lattice_angle_mean": _mean(angles),
        "lattice_angle_std": _deviation(angles),
        "lattice_angle_min": min(angles),
        "lattice_angle_max": max(angles),
        "lattice_angle_range": max(angles) - min(angles),
        "cell_surface_area": surface,
        "cell_compactness": volume / mean_length**3,
    }


def _derived_values(
    matrix: Sequence[Sequence[float]], numbers: Sequence[int]
) -> dict[str, float]:
    return {**_geometry_values(matrix, numbers), **_composition_values(numbers)}


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _target_value(
    row: Mapping[str, Any],
    target: Mapping[str, Any],
    derived: Mapping[str, float],
    numbers: Sequence[int],
) -> float | None:
    if target["kind"] == "direct":
        return _finite_number(row.get(str(target["key"])))
    if target["kind"] == "element":
        return numbers.count(int(target["key"])) / len(numbers)
    return derived[str(target["key"])]


def _structure(row: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, float], list[int]]:
    matrix = _matrix(row.get("cell"))
    numbers = _numbers(row.get("atomic_numbers"))
    positions = _positions(row.get("positions"), matrix, len(numbers))
    material_id = str(row.get("material_id", ""))
    if not material_id:
        raise ValueError("Alex-MP-20 row has no material id")
    data = {
        "material_id": _fixed_bytes(material_id, MATERIAL_ID_BYTES, "Alex-MP-20 material id"),
        "space_group": _fixed_bytes(
            str(row.get("space_group", "")), SPACE_GROUP_BYTES, "Alex-MP-20 space group"
        ),
        "chemical_system": _fixed_bytes(
            str(row.get("chemical_system", "")),
            CHEMICAL_SYSTEM_BYTES,
            "Alex-MP-20 chemical system",
        ),
        "element_image": _element_image(numbers),
        "occupancy_projection": _occupancy_projection(positions),
        "projection": _projection(numbers, positions),
        "lattice": array.array("d", (value for vector in matrix for value in vector)),
        "atoms": len(numbers),
        "atomic_numbers": _padded_numbers(numbers),
        "fractional_positions": _padded_positions(positions),
    }
    return data, _derived_values(matrix, numbers), numbers


def _book(paths: Mapping[str, Path], split: str) -> Any:
    if split not in SPLITS:
        raise ValueError(f"Alex-MP-20 has no {split!r} split")
    parquet = importlib.import_module("pyarrow.parquet")
    book = parquet.ParquetFile(paths[split])
    names = tuple(book.schema_arrow.names)
    if names != SOURCE_COLUMNS:
        raise ValueError(
            f"Alex-MP-20 {split} columns are {', '.join(names)}, "
            f"not the recorded {', '.join(SOURCE_COLUMNS)}"
        )
    return book


def _source_rows(book: Any) -> Iterator[Mapping[str, Any]]:
    for batch in book.iter_batches(batch_size=1024, columns=list(SOURCE_COLUMNS)):
        values = batch.to_pydict()
        for offset in range(batch.num_rows):
            yield {name: values[name][offset] for name in SOURCE_COLUMNS}


def _entries(paths: Mapping[str, Path], split: str, target: Mapping[str, Any]) -> Rows:
    book = _book(paths, split)
    tree = SPLIT_INDEX[split]
    for index, source_row in enumerate(_source_rows(book)):
        structure, derived, numbers = _structure(source_row)
        value = _target_value(source_row, target, derived, numbers)
        if value is None:
            continue
        yield 0, {**structure, "target": value, "split": tree, "index": index}


def _columns() -> dict[str, Any]:
    return {
        "material_id": ("B", MATERIAL_ID_BYTES),
        "space_group": ("B", SPACE_GROUP_BYTES),
        "chemical_system": ("B", CHEMICAL_SYSTEM_BYTES),
        "element_image": ("f", ELEMENT_PIXELS),
        "occupancy_projection": ("B", 3 * CRYSTAL_SIDE * CRYSTAL_SIDE),
        "projection": ("B", 3 * CRYSTAL_SIDE * CRYSTAL_SIDE),
        "lattice": ("d", 9),
        "atoms": "i",
        "atomic_numbers": ("B", MAX_ATOMS),
        "fractional_positions": ("f", MAX_ATOMS * 3),
        "target": "d",
        "split": "i",
        "index": "q",
    }


def load(converter: str, paths: Mapping[str, Path], split: str) -> Loaded:
    """Open one Alex-MP-20 visual property task with bounded working memory."""
    target = TARGET_BY_SLUG.get(converter)
    if target is None:
        raise ValueError(f"there is no Alex-MP-20 converter {converter!r}")
    return ("samples",), _columns(), _entries(paths, split, target)
