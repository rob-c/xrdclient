"""One hundred visual materials-physics tasks from NIST JARVIS-DFT.

Each logical task predicts one published or explicitly derived scalar from the
same pinned 2D or 3D crystal archive.  The ZIP member is streamed as a JSON
array, while every structure becomes an elemental image and three orthogonal
fractional-coordinate projections suitable for image-oriented teaching.
"""

from __future__ import annotations

import array
import io
import json
import math
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from .._compat import zip_strict
from ._condensed_matter import ATOMIC_NUMBER

Rows = Iterator[tuple[int, dict[str, Any]]]
Loaded = tuple[tuple[str, ...], dict[str, Any], Rows]

CRYSTAL_SIDE = 32
ELEMENT_PIXELS = 128
FORMULA_BYTES = 128
JID_BYTES = 32
MAX_ATOMS = 256
MAX_JSON_ROW_BYTES = 32 * 1024 * 1024
MAX_JSON_MEMBER_BYTES = 512 * 1024 * 1024
SPLIT_TREES = ("train", "validation", "test")
_END = object()

# Slug, source field, human target, 3D rows, 2D rows.  Counts were measured
# against the pinned files and make missing-value filtering visible on the site.
DIRECT_TARGETS: tuple[tuple[str, str, str, int, int], ...] = (
    ("formation_energy", "formation_energy_peratom", "formation energy per atom", 93_902, 1_103),
    ("optb88vdw_bandgap", "optb88vdw_bandgap", "OptB88vdW band gap", 93_902, 1_103),
    ("optb88vdw_total_energy", "optb88vdw_total_energy", "OptB88vdW total energy", 93_902, 1_103),
    ("density", "density", "mass density", 93_902, 1_103),
    ("energy_above_hull", "ehull", "energy above the convex hull", 93_902, 1_103),
    ("plane_wave_cutoff", "encut", "plane-wave cutoff", 93_557, 1_103),
    ("kpoint_length", "kpoint_length_unit", "k-point length parameter", 93_558, 1_103),
    ("atom_count", "nat", "unit-cell atom count", 93_902, 1_103),
    ("magnetic_moment_oszicar", "magmom_oszicar", "OSZICAR magnetic moment", 88_811, 1_103),
    ("magnetic_moment_outcar", "magmom_outcar", "OUTCAR magnetic moment", 92_170, 1_103),
    ("dielectric_x", "epsx", "x dielectric response", 60_354, 887),
    ("dielectric_y", "epsy", "y dielectric response", 60_354, 887),
    ("dielectric_z", "epsz", "z dielectric response", 60_354, 887),
    ("mbj_dielectric_x", "mepsx", "MBJ x dielectric response", 19_834, 249),
    ("mbj_dielectric_y", "mepsy", "MBJ y dielectric response", 19_834, 249),
    ("mbj_dielectric_z", "mepsz", "MBJ z dielectric response", 19_834, 249),
    ("mbj_bandgap", "mbj_bandgap", "MBJ band gap", 21_559, 246),
    ("electron_effective_mass", "avg_elec_mass", "average electron effective mass", 17_645, 678),
    ("hole_effective_mass", "avg_hole_mass", "average hole effective mass", 17_645, 678),
    ("band_difference_mesh", "maxdiff_mesh", "maximum mesh band difference", 5_861, 431),
    ("band_difference_bz", "maxdiff_bz", "maximum Brillouin-zone band difference", 5_861, 431),
    ("n_seebeck", "n-Seebeck", "n-type Seebeck coefficient", 23_218, 806),
    ("p_seebeck", "p-Seebeck", "p-type Seebeck coefficient", 23_218, 802),
    ("n_power_factor", "n-powerfact", "n-type thermoelectric power factor", 23_218, 802),
    ("p_power_factor", "p-powerfact", "p-type thermoelectric power factor", 23_218, 802),
    ("n_conductivity", "ncond", "n-type electrical conductivity", 23_218, 802),
    ("p_conductivity", "pcond", "p-type electrical conductivity", 23_218, 802),
    ("n_thermal_conductivity", "nkappa", "n-type electronic thermal conductivity", 23_218, 802),
    ("p_thermal_conductivity", "pkappa", "p-type electronic thermal conductivity", 23_218, 802),
    ("slme", "slme", "spectroscopic limited maximum efficiency", 10_022, 184),
    ("spin_orbit_spillage", "spillage", "spin-orbit spillage", 11_377, 603),
    ("exfoliation_energy", "exfoliation_energy", "exfoliation energy", 813, 748),
)

# Slug, derived value, human target.  These tasks use every structure and make
# geometry/composition concepts available alongside the calculated observables.
DERIVED_TARGETS: tuple[tuple[str, str, str], ...] = (
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
    ("lattice_anisotropy", "lattice_anisotropy", "lattice length anisotropy"),
    ("lattice_angle_mean", "lattice_angle_mean", "mean lattice angle"),
    ("lattice_angle_std", "lattice_angle_std", "lattice-angle deviation"),
    ("element_count", "element_count", "number of distinct elements"),
    ("atomic_number_mean", "atomic_number_mean", "mean atomic number"),
    ("atomic_number_std", "atomic_number_std", "atomic-number deviation"),
    ("atomic_number_min", "atomic_number_min", "minimum atomic number"),
    ("atomic_number_max", "atomic_number_max", "maximum atomic number"),
)

TARGETS: tuple[dict[str, Any], ...] = (
    *(
        {
            "slug": slug,
            "source_key": source_key,
            "derived_key": "",
            "label": label,
            "counts": {"dft3d": count_3d, "dft2d": count_2d},
        }
        for slug, source_key, label, count_3d, count_2d in DIRECT_TARGETS
    ),
    *(
        {
            "slug": slug,
            "source_key": "",
            "derived_key": derived_key,
            "label": label,
            "counts": {"dft3d": 93_902, "dft2d": 1_103},
        }
        for slug, derived_key, label in DERIVED_TARGETS
    ),
)

SOURCES: tuple[dict[str, Any], ...] = (
    {
        "key": "dft3d",
        "prefix": "jarvis_dft3d",
        "label": "JARVIS-DFT 3D",
        "kind": "three-dimensional",
        "source": "https://figshare.com/articles/dataset/jdft_3d-7-7-2018_json/6815699",
        "url": "https://ndownloader.figshare.com/files/64391379",
        "source_bytes": 48_447_610,
        "origin": "https://doi.org/10.6084/m9.figshare.6815699.v11",
        "cache_name": "jarvis-dft3d-2025",
        "snapshot": "jdft_3d-9-24-2025.json.zip",
    },
    {
        "key": "dft2d",
        "prefix": "jarvis_dft2d",
        "label": "JARVIS-DFT 2D",
        "kind": "two-dimensional",
        "source": "https://figshare.com/articles/dataset/jdft_2d-7-7-2018_json/6815705",
        "url": "https://ndownloader.figshare.com/files/38521268",
        "source_bytes": 8_394_653,
        "origin": "https://doi.org/10.6084/m9.figshare.6815705.v8",
        "cache_name": "jarvis-dft2d-2022",
        "snapshot": "d2-12-12-2022.json.zip",
    },
)


def _declaration(source: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
    count = int(target["counts"][source["key"]])
    derived = bool(target["derived_key"])
    selection = "used every structure" if derived else "discarded only non-numeric target rows"
    provenance = (
        f"derived {target['label']} from the published atoms object"
        if derived
        else f"retained the published {target['source_key']} field as the target"
    )
    return {
        "name": f"{source['prefix']}_{target['slug']}",
        "label": f"{source['label']} {target['label']}",
        "title": f"{count:,} {source['kind']} crystals for {target['label']} regression",
        "licence": "CC BY 4.0",
        "source": source["source"],
        "url": source["url"],
        "source_bytes": source["source_bytes"],
        "cache_names": {"archive": source["cache_name"]},
        "converter": f"open:jarvis:{source['key']}:{target['slug']}",
        "transformation": (
            f"streamed the pinned {source['snapshot']} JSON array without reordering; "
            f"{selection} and {provenance}; converted Cartesian coordinates to fractional "
            "coordinates; retained padded structure tensors and derived a normalized 8x16 "
            "elemental image plus three 32x32 crystal projections; assigned stable 80/10/10 "
            "train/validation/test trees from the numeric JARVIS id"
        ),
        "classes": (),
        "splits": ("all",),
        "requires": (),
        "modality": f"visualized {source['kind']} crystal structures",
        "task": f"crystal-image {target['label']} regression",
        "basket_size": 2 * 1024 * 1024,
        "layout": "train, validation and test TTrees with identical structure/image branches",
        "creators": ("Kamal Choudhary",),
        "publisher": "National Institute of Standards and Technology",
        "origin": source["origin"],
        "repository": "Figshare",
        "mirrors": (("NIST JARVIS-DFT", "https://jarvis.nist.gov/jarvisdft/"),),
        "citation": "https://doi.org/10.1038/s41524-020-00440-1",
    }


JARVIS_PHYSICS: tuple[dict[str, Any], ...] = tuple(
    _declaration(source, target) for source in SOURCES for target in TARGETS
)

TARGET_BY_SLUG = {str(target["slug"]): target for target in TARGETS}
SOURCE_BY_KEY = {str(source["key"]): source for source in SOURCES}


def _fixed_bytes(value: str, width: int, description: str) -> array.array[int]:
    encoded = value.encode("utf-8")
    if len(encoded) > width:
        raise ValueError(f"{description} is longer than {width} bytes")
    return array.array("B", encoded + bytes(width - len(encoded)))


def _json_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    members = [member for member in archive.infolist() if not member.is_dir()]
    if len(members) != 1 or not members[0].filename.lower().endswith(".json"):
        raise ValueError("JARVIS source must contain exactly one JSON member")
    member = members[0]
    if member.flag_bits & 1:
        raise ValueError("JARVIS JSON member must not be encrypted")
    if member.file_size > MAX_JSON_MEMBER_BYTES:
        raise ValueError("JARVIS JSON member exceeds the registered extraction ceiling")
    return member


def _start_array(source: io.TextIOBase) -> str:
    buffer = source.read(1024 * 1024)
    stripped = buffer.lstrip()
    if not stripped.startswith("["):
        raise ValueError("JARVIS JSON must be a top-level array")
    return stripped[1:]


def _next_value(source: io.TextIOBase, buffer: str, decoder: json.JSONDecoder) -> tuple[Any, str]:
    while True:
        buffer = buffer.lstrip(" \t\r\n,")
        if buffer.startswith("]"):
            return _END, buffer[1:]
        try:
            value, end = decoder.raw_decode(buffer)
        except json.JSONDecodeError:
            chunk = source.read(1024 * 1024)
            if not chunk:
                raise ValueError("JARVIS JSON array ends inside a row") from None
            buffer += chunk
            if len(buffer) > MAX_JSON_ROW_BYTES:
                raise ValueError("JARVIS JSON row exceeds the parser ceiling") from None
            continue
        return value, buffer[end:]


def _finish_array(source: io.TextIOBase, buffer: str) -> None:
    if (buffer + source.read()).strip():
        raise ValueError("JARVIS JSON has data after its top-level array")


def _json_rows(path: Path) -> Iterator[Mapping[str, Any]]:
    decoder = json.JSONDecoder()
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as error:
        raise ValueError("JARVIS source is not a ZIP archive") from error
    with archive:
        member = _json_member(archive)
        with archive.open(member) as raw, io.TextIOWrapper(raw, encoding="utf-8") as source:
            buffer = _start_array(source)
            while True:
                value, buffer = _next_value(source, buffer, decoder)
                if value is _END:
                    _finish_array(source, buffer)
                    return
                if not isinstance(value, dict):
                    raise ValueError("JARVIS JSON array contains a non-object row")
                yield value


def _vector(values: Any, description: str) -> tuple[float, float, float]:
    if not isinstance(values, list) or len(values) != 3:
        raise ValueError(f"JARVIS {description} is not a three-vector")
    try:
        result = tuple(float(value) for value in values)
    except (TypeError, ValueError):
        raise ValueError(f"JARVIS {description} contains a non-numeric value") from None
    if not all(math.isfinite(value) for value in result):
        raise ValueError(f"JARVIS {description} contains a non-finite value")
    return result  # type: ignore[return-value]


def _matrix(atoms: Mapping[str, Any]) -> tuple[tuple[float, float, float], ...]:
    values = atoms.get("lattice_mat")
    if not isinstance(values, list) or len(values) != 3:
        raise ValueError("JARVIS atoms object has no 3x3 lattice matrix")
    return tuple(_vector(row, "lattice row") for row in values)


def _determinant(matrix: Sequence[Sequence[float]]) -> float:
    a, b, c = matrix
    return _dot(a, _cross(b, c))


def _dot(first: Sequence[float], second: Sequence[float]) -> float:
    return sum(a * b for a, b in zip_strict(first, second))


def _cross(first: Sequence[float], second: Sequence[float]) -> tuple[float, float, float]:
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )


def _inverse(matrix: Sequence[Sequence[float]]) -> tuple[tuple[float, float, float], ...]:
    a, b, c = matrix
    determinant = _determinant(matrix)
    if abs(determinant) < 1e-12:
        raise ValueError("JARVIS lattice matrix is singular")
    columns = (_cross(b, c), _cross(c, a), _cross(a, b))
    return tuple(_inverse_row(columns, row, determinant) for row in range(3))


def _inverse_row(
    columns: Sequence[Sequence[float]], row: int, determinant: float
) -> tuple[float, float, float]:
    return (
        columns[0][row] / determinant,
        columns[1][row] / determinant,
        columns[2][row] / determinant,
    )


def _row_times_matrix(
    vector: Sequence[float], matrix: Sequence[Sequence[float]]
) -> tuple[float, float, float]:
    return tuple(sum(vector[row] * matrix[row][column] for row in range(3)) for column in range(3))  # type: ignore[return-value]


def _positions(
    atoms: Mapping[str, Any], matrix: Sequence[Sequence[float]], count: int
) -> list[tuple[float, float, float]]:
    values = atoms.get("coords")
    if not isinstance(values, list) or len(values) != count:
        raise ValueError("JARVIS coordinate and element counts differ")
    positions = [_vector(value, "atomic coordinate") for value in values]
    if atoms.get("cartesian") is True:
        inverse = _inverse(matrix)
        positions = [_row_times_matrix(position, inverse) for position in positions]
    elif atoms.get("cartesian") is not False:
        raise ValueError("JARVIS atoms object has no Cartesian-coordinate flag")
    return [tuple(value % 1.0 for value in position) for position in positions]  # type: ignore[misc]


def _atomic_numbers(atoms: Mapping[str, Any]) -> list[int]:
    elements = atoms.get("elements")
    if not isinstance(elements, list) or not elements or len(elements) > MAX_ATOMS:
        raise ValueError(f"JARVIS atoms object must have 1 to {MAX_ATOMS} elements")
    try:
        return [ATOMIC_NUMBER[str(symbol)] for symbol in elements]
    except KeyError as error:
        raise ValueError(f"JARVIS atoms object has unknown element {error.args[0]!r}") from None


def _element_image(numbers: Sequence[int]) -> array.array[float]:
    values = array.array("f", [0.0]) * ELEMENT_PIXELS
    scale = 1.0 / len(numbers)
    for number in numbers:
        values[number - 1] += scale
    return values


def _projection(numbers: Sequence[int], positions: Sequence[Sequence[float]]) -> array.array[int]:
    image = array.array("B", [0]) * (3 * CRYSTAL_SIDE * CRYSTAL_SIDE)
    for number, position in zip_strict(numbers, positions):
        bins = tuple(min(CRYSTAL_SIDE - 1, int(value * CRYSTAL_SIDE)) for value in position)
        pairs = ((bins[0], bins[1]), (bins[0], bins[2]), (bins[1], bins[2]))
        for plane, (first, second) in enumerate(pairs):
            pixel = plane * CRYSTAL_SIDE**2 + second * CRYSTAL_SIDE + first
            image[pixel] = max(image[pixel], number)
    return image


def _padded_numbers(numbers: Sequence[int]) -> array.array[int]:
    values = array.array("B", [0]) * MAX_ATOMS
    values[: len(numbers)] = array.array("B", numbers)
    return values


def _padded_positions(positions: Sequence[Sequence[float]]) -> array.array[float]:
    values = array.array("f", [0.0]) * (MAX_ATOMS * 3)
    flat = (coordinate for position in positions for coordinate in position)
    values[: len(positions) * 3] = array.array("f", flat)
    return values


def _length(vector: Sequence[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def _mean(values: Sequence[float] | Sequence[int]) -> float:
    return sum(values) / len(values)


def _deviation(values: Sequence[float] | Sequence[int]) -> float:
    mean = _mean(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def _angle(first: Sequence[float], second: Sequence[float]) -> float:
    cosine = _dot(first, second)
    cosine /= _length(first) * _length(second)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def _derived_values(matrix: Sequence[Sequence[float]], numbers: Sequence[int]) -> dict[str, float]:
    lengths = tuple(_length(vector) for vector in matrix)
    angles = (
        _angle(matrix[1], matrix[2]),
        _angle(matrix[0], matrix[2]),
        _angle(matrix[0], matrix[1]),
    )
    volume = abs(_determinant(matrix))
    return {
        "lattice_a": lengths[0],
        "lattice_b": lengths[1],
        "lattice_c": lengths[2],
        "angle_alpha": angles[0],
        "angle_beta": angles[1],
        "angle_gamma": angles[2],
        "cell_volume": volume,
        "volume_per_atom": volume / len(numbers),
        "lattice_length_mean": _mean(lengths),
        "lattice_length_std": _deviation(lengths),
        "lattice_anisotropy": max(lengths) / min(lengths),
        "lattice_angle_mean": _mean(angles),
        "lattice_angle_std": _deviation(angles),
        "element_count": float(len(set(numbers))),
        "atomic_number_mean": _mean(numbers),
        "atomic_number_std": _deviation(numbers),
        "atomic_number_min": float(min(numbers)),
        "atomic_number_max": float(max(numbers)),
    }


def _structure(row: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, float]]:
    atoms = row.get("atoms")
    if not isinstance(atoms, dict):
        raise ValueError("JARVIS row has no atoms object")
    matrix = _matrix(atoms)
    numbers = _atomic_numbers(atoms)
    positions = _positions(atoms, matrix, len(numbers))
    jid = str(row.get("jid", ""))
    formula = str(row.get("formula", ""))
    if not jid or not formula:
        raise ValueError("JARVIS row has no material id or formula")
    data = {
        "jid": _fixed_bytes(jid, JID_BYTES, f"JARVIS id {jid!r}"),
        "formula": _fixed_bytes(formula, FORMULA_BYTES, f"JARVIS formula {formula!r}"),
        "element_image": _element_image(numbers),
        "projection": _projection(numbers, positions),
        "lattice": array.array("d", (value for vector in matrix for value in vector)),
        "atoms": len(numbers),
        "atomic_numbers": _padded_numbers(numbers),
        "fractional_positions": _padded_positions(positions),
    }
    return data, _derived_values(matrix, numbers)


def _number_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _target_value(
    row: Mapping[str, Any], target: Mapping[str, Any], derived: Mapping[str, float] | None = None
) -> float | None:
    if target["source_key"]:
        return _number_or_none(row.get(target["source_key"]))
    if derived is None:
        return None
    return derived[str(target["derived_key"])]


def _split_tree(jid: str) -> int:
    tail = jid.rpartition("-")[2]
    if not tail.isdigit():
        raise ValueError(f"JARVIS id {jid!r} has no numeric suffix")
    bucket = int(tail) % 10
    if bucket < 8:
        return 0
    return 1 if bucket == 8 else 2


def _entries(path: Path, target: Mapping[str, Any]) -> Rows:
    for index, source_row in enumerate(_json_rows(path)):
        direct = _target_value(source_row, target)
        if target["source_key"] and direct is None:
            continue
        structure, derived = _structure(source_row)
        target_value = direct if direct is not None else _target_value(source_row, target, derived)
        assert target_value is not None
        jid = bytes(structure["jid"]).rstrip(b"\0").decode("ascii")
        tree = _split_tree(jid)
        yield tree, {**structure, "target": target_value, "split": tree, "index": index}


def _columns() -> dict[str, Any]:
    return {
        "jid": ("B", JID_BYTES),
        "formula": ("B", FORMULA_BYTES),
        "element_image": ("f", ELEMENT_PIXELS),
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
    """Open one pinned JARVIS property task with bounded working memory."""
    source_key, separator, slug = converter.partition(":")
    if not separator or source_key not in SOURCE_BY_KEY or slug not in TARGET_BY_SLUG:
        raise ValueError(f"there is no JARVIS physics converter {converter!r}")
    if split != "all":
        raise ValueError("JARVIS visual physics tasks are converted together as split trees")
    return SPLIT_TREES, _columns(), _entries(paths["archive"], TARGET_BY_SLUG[slug])
