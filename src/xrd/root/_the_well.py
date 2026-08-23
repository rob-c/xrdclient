"""One hundred visual field-learning tasks spanning The Well physics collection.

The upstream collection uses one self-describing HDF5 specification for very
different spatiotemporal systems.  We pin the smallest official test shard from
each of sixteen CC BY 4.0 repositories and expose six common visual exercises
per system, plus four additional field-statistic exercises.  The physical
sources are shared in the cache even though every logical task gets an
independently usable ROOT file.
"""

from __future__ import annotations

import array
import importlib
import math
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

Rows = Iterator[tuple[int, dict[str, Any]]]
Loaded = tuple[tuple[str, ...], dict[str, Any], Rows]

SIDE = 64
PIXELS = SIDE * SIDE
TEXT_BYTES = 64
SPLITS = ("train", "validation", "test")
WELL_PAPER = "https://arxiv.org/abs/2412.00568"
WELL_CODE = "https://github.com/PolymathicAI/the_well"

TASKS: tuple[tuple[str, str], ...] = (
    ("next_state", "one-step physical-field forecasting"),
    ("temporal_change", "one-step physical-field change forecasting"),
    ("gradient_magnitude", "spatial-gradient magnitude prediction"),
    ("laplacian", "spatial-Laplacian prediction"),
    ("threshold_mask", "above-mean field segmentation"),
    ("mean_regression", "next-state spatial-mean regression"),
)
EXTRA_TASK = ("rms_regression", "next-state root-mean-square regression")
EXTRA_SOURCES = {
    "active_matter",
    "convective_envelope_rsg",
    "planetswe",
    "post_neutron_star_merger",
}


def _source(
    name: str,
    revision: str,
    member: str,
    size: int,
    domain: str,
    description: str,
    creators: Sequence[str],
    citation: str = "",
) -> dict[str, Any]:
    repository = f"https://huggingface.co/datasets/polymathic-ai/{name}"
    return {
        "name": name,
        "revision": revision,
        "member": member,
        "size": size,
        "url": f"{repository}/resolve/{revision}/{member}",
        "domain": domain,
        "description": description,
        "creators": tuple(creators),
        "citation": citation or WELL_PAPER,
        "source": repository,
        "origin": f"https://polymathic-ai.org/the_well/datasets/{name}/",
    }


SOURCES: tuple[dict[str, Any], ...] = (
    _source(
        "acoustic_scattering_discontinuous",
        "1103c80a2975647674a07e08bcf5be0fe4c71ea1",
        "data/test/acoustic_scattering_discontinuous_chunk_18.hdf5",
        8_111_783_936,
        "acoustics and wave propagation",
        "acoustic waves crossing random discontinuous media",
        ("Michael McCabe", "Kyle T. Mandli", "Randall J. LeVeque"),
    ),
    _source(
        "acoustic_scattering_inclusions",
        "c17cd1d16fd351594ccea628e575650b404c4a91",
        "data/test/acoustic_scattering_inclusions_chunk_36.hdf5",
        8_111_783_936,
        "acoustics and inverse scattering",
        "acoustic waves scattered by material inclusions",
        ("Michael McCabe", "Kyle T. Mandli", "Randall J. LeVeque"),
    ),
    _source(
        "acoustic_scattering_maze",
        "8df383a3223f40f7ce66fe77b4ff4d7006dbc272",
        "data/test/acoustic_scattering_maze_chunk_18.hdf5",
        15_980_298_240,
        "acoustics and structured media",
        "acoustic waves propagating through maze-like media",
        ("Michael McCabe", "Kyle T. Mandli", "Randall J. LeVeque"),
    ),
    _source(
        "active_matter",
        "dc3e9135a75b4e5a0d979086219fc97fe668f7a3",
        "data/test/active_matter_L_10.0_zeta_1.0_alpha_-2.0.hdf5",
        276_824_064,
        "active matter and biophysical fluids",
        "continuum dynamics of active rods in a Stokes fluid",
        ("Suryanarayana Maddu", "Scott Weady", "Michael J. Shelley"),
        "https://arxiv.org/abs/2308.06675",
    ),
    _source(
        "convective_envelope_rsg",
        "6eae51fa43b0ae03bf98c64c4b8e1892278d0fab",
        "data/test/convective_envelope_rsg_trajectories_11.hdf5",
        20_149_436_416,
        "stellar astrophysics and radiation hydrodynamics",
        "three-dimensional convection in a red-supergiant envelope",
        ("Jared A. Goldberg", "Yan-Fei Jiang", "Lars Bildsten"),
        "https://doi.org/10.3847/1538-4357/ac5ab3",
    ),
    _source(
        "gray_scott_reaction_diffusion",
        "913ef5679d5c4b4ccd6fcd52c055509dfbf190f9",
        "data/test/gray_scott_reaction_diffusion_bubbles_F_0.098_k_0.057.hdf5",
        2_650_800_128,
        "nonlinear dynamics and pattern formation",
        "Gray-Scott reaction-diffusion pattern evolution",
        ("Daniel Fortunato",),
    ),
    _source(
        "helmholtz_staircase",
        "a24295051ca7ff772b08c9e1645d965b057c6298",
        "data/test/helmholtz_staircase_omega_006.hdf5",
        335_544_320,
        "wave physics and Helmholtz equations",
        "time-domain construction of frequency-domain Helmholtz solutions",
        ("Fruzsina Julia Agocs", "Alex H. Barnett"),
        "https://arxiv.org/abs/2310.12486",
    ),
    _source(
        "planetswe",
        "33eabc3e7e33454644ce5c10aee76b2e22605abd",
        "data/test/planetswe_IC36_s1.hdf5",
        1_602_224_128,
        "planetary atmospheres and geophysical fluids",
        "rotating shallow-water dynamics on a sphere",
        ("Michael McCabe", "Peter Harrington", "Shashank Subramanian", "Jed Brown"),
        "https://openreview.net/forum?id=RFfUUtKYOG",
    ),
    _source(
        "post_neutron_star_merger",
        "721253cd3220d158d3088c0cce4558dd444d1353",
        "data/test/post_neutron_star_merger_scenario_2.hdf5",
        14_109_638_656,
        "relativistic astrophysics and radiation transport",
        "accretion-torus evolution after a neutron-star merger",
        ("Jonah M. Miller", "Ben R. Ryan", "Joshua C. Dolence"),
        "https://arxiv.org/abs/1912.03378",
    ),
    _source(
        "rayleigh_benard",
        "10e143ebedae8a9b1c699b95d5b1eb8feaae09b5",
        "data/test/rayleigh_benard_Rayleigh_1e10_Prandtl_1.hdf5",
        1_082_130_432,
        "thermal convection",
        "Rayleigh-Benard convection across Rayleigh and Prandtl regimes",
        ("Keaton J. Burns", "Geoffrey M. Vasil", "Jeffrey S. Oishi"),
    ),
    _source(
        "rayleigh_taylor_instability",
        "75166ae3304f95747519ce35c2e03c0c295a2691",
        "data/test/rayleigh_taylor_instability_At_0625.hdf5",
        8_002_732_032,
        "hydrodynamic instabilities",
        "three-dimensional Rayleigh-Taylor mixing",
        ("William H. Cabot", "Andrew W. Cook"),
    ),
    _source(
        "shear_flow",
        "fc867f856f306905cf94c1f5df978cc518a2048c",
        "data/test/shear_flow_Reynolds_1e4_Schmidt_1e-1.hdf5",
        1_694_498_816,
        "fluid dynamics and passive-scalar mixing",
        "two-dimensional shear-flow turbulence",
        ("Keaton J. Burns", "Geoffrey M. Vasil", "Jeffrey S. Oishi"),
    ),
    _source(
        "supernova_explosion_64",
        "319706c855f5701908874426aef4785367160938",
        "data/test/supernova_explosion_Msun_0.1_dim64_file_00.hdf5",
        771_751_936,
        "stellar explosions and computational astrophysics",
        "three-dimensional supernova explosion evolution",
        ("Keiya Hirashima", "Kana Moriwaki", "Michiko S. Fujii", "Yutaka Hirai"),
        "https://doi.org/10.1093/mnras/stad2864",
    ),
    _source(
        "turbulence_gravity_cooling",
        "0ba58e6bc59ab2a98ad36a62b69e6649d24789fa",
        "data/test/turbulence_gravity_cooling_rho0_0.445_Z_0.1_T0_10.hdf5",
        3_179_282_432,
        "self-gravitating astrophysical turbulence",
        "turbulence with gravity and radiative cooling",
        ("Keiya Hirashima", "Kana Moriwaki", "Michiko S. Fujii", "Yutaka Hirai"),
        "https://doi.org/10.1093/mnras/stad2864",
    ),
    _source(
        "turbulent_radiative_layer_2D",
        "2ee7756575ff6f90981d0308cbcb6a2ab5995bdc",
        "data/test/turbulent_radiative_layer_tcool_0.03.hdf5",
        109_051_904,
        "multiphase plasma and radiative turbulence",
        "two-dimensional turbulent radiative mixing layers",
        ("Drummond B. Fielding", "Eve C. Ostriker", "Greg L. Bryan", "Adam S. Jermyn"),
        "https://doi.org/10.3847/2041-8213/ab8d2c",
    ),
    _source(
        "viscoelastic_instability",
        "cf278827760865b35ae89326f919bb85abc49372",
        "data/test/viscoelastic_instability_AH.hdf5",
        352_321_536,
        "soft matter and non-Newtonian fluid dynamics",
        "elasto-inertial viscoelastic channel-flow instability",
        ("Miguel Beneitez", "Jacob Page", "Yves Dubief", "Rich R. Kerswell"),
        "https://doi.org/10.1017/jfm.2024.51",
    ),
)
SOURCE_BY_NAME = {str(source["name"]): source for source in SOURCES}


def _tasks(source: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    extra = (EXTRA_TASK,) if source["name"] in EXTRA_SOURCES else ()
    return (*TASKS, *extra)


def _declaration(source: Mapping[str, Any], task: tuple[str, str]) -> dict[str, Any]:
    slug, label = task
    name = f"well_{source['name']}_{slug}"
    transformation = (
        "selected the smallest complete HDF5 file in the publisher's test directory as a "
        "bounded educational slice; read the first time-varying scalar field from The Well's "
        "self-describing field list; took the central plane of a 3D field; nearest-sampled each "
        "plane to 64x64; retained raw float32 values, finite-pixel masks, per-entry min/max and "
        "a min-max normalized input; formed contiguous 80/10/10 temporal train/validation/test "
        f"trees within each trajectory; derived the {label} target without changing the source"
    )
    return {
        "name": name,
        "label": f"The Well {source['name']} — {label}",
        "title": f"64x64 {source['description']} fields for {label}",
        "licence": "CC BY 4.0",
        "source": source["source"],
        "source_bytes": source["size"],
        "sources": {"sample": source["url"]},
        "source_sizes": {"sample": source["size"]},
        "cache_names": {"sample": f"the-well-{source['name']}-sample.hdf5"},
        "converter": f"open:the_well:{source['name']}:{slug}",
        "transformation": transformation,
        "classes": (),
        "splits": ("all",),
        "requires": ("h5py", "numpy"),
        "modality": f"visualized spatiotemporal field; {source['domain']}",
        "task": label,
        "basket_size": 2 * 1024 * 1024,
        "layout": "train, validation and test TTrees with paired 64x64 field images",
        "creators": source["creators"],
        "publisher": "Polymathic AI / The Well collaboration",
        "origin": source["origin"],
        "repository": "Hugging Face Hub",
        "mirrors": (("The Well source and format", WELL_CODE),),
        "citation": source["citation"],
    }


THE_WELL: tuple[dict[str, Any], ...] = tuple(
    _declaration(source, task) for source in SOURCES for task in _tasks(source)
)


def _text(value: str, label: str) -> array.array[int]:
    encoded = value.encode("utf-8")
    if len(encoded) > TEXT_BYTES:
        raise ValueError(f"The Well {label} is longer than {TEXT_BYTES} bytes")
    return array.array("B", encoded + bytes(TEXT_BYTES - len(encoded)))


def _names(group: Any) -> tuple[str, ...]:
    raw = group.attrs.get("field_names", tuple(group.keys()))
    return tuple(value.decode() if isinstance(value, bytes) else str(value) for value in raw)


def _dynamic_field(book: Any) -> tuple[str, Any]:
    for group_name in ("t0_fields", "t1_fields", "t2_fields"):
        if group_name not in book:
            continue
        group = book[group_name]
        for name in _names(group):
            field = group[name]
            if bool(field.attrs.get("time_varying", True)):
                return name, field
    raise ValueError("The Well shard has no time-varying physical field")


def _counts(field: Any) -> tuple[int, int]:
    sample_varying = bool(field.attrs.get("sample_varying", True))
    time_varying = bool(field.attrs.get("time_varying", True))
    cursor = 0
    samples = int(field.shape[cursor]) if sample_varying else 1
    cursor += int(sample_varying)
    times = int(field.shape[cursor]) if time_varying else 1
    if samples < 1 or times < 2:
        raise ValueError("The Well field needs at least one trajectory and two time steps")
    return samples, times


def _frame(field: Any, sample: int, time: int, numpy: Any) -> Any:
    index: list[int | slice] = []
    if bool(field.attrs.get("sample_varying", True)):
        index.append(sample)
    if bool(field.attrs.get("time_varying", True)):
        index.append(time)
    remaining = len(field.shape) - len(index)
    if remaining > 2:
        index.extend((slice(None), slice(None)))
        index.extend(size // 2 for size in field.shape[len(index) :])
    values = numpy.asarray(field[tuple(index)], dtype="float32").squeeze()
    if values.ndim == 1:
        values = values.reshape(1, -1)
    if values.ndim != 2 or not values.size:
        raise ValueError(f"The Well field frame has unsupported shape {values.shape}")
    rows = ((numpy.arange(SIDE) + 0.5) * values.shape[0] / SIDE).astype("int64")
    columns = ((numpy.arange(SIDE) + 0.5) * values.shape[1] / SIDE).astype("int64")
    return values[numpy.ix_(rows, columns)]


def _prepared(values: Any, numpy: Any) -> tuple[Any, Any, float, float]:
    finite = numpy.isfinite(values)
    if not bool(finite.any()):
        raise ValueError("The Well field frame contains no finite pixels")
    selected = values[finite]
    lower = float(selected.min())
    upper = float(selected.max())
    clean = numpy.where(finite, values, 0.0).astype("float32", copy=False)
    span = upper - lower
    normalized = numpy.where(finite, (values - lower) / span if span else 0.0, 0.0)
    return clean, normalized.astype("float32", copy=False), lower, upper


def _target(task: str, current: Any, future: Any, numpy: Any) -> tuple[Any, float]:
    if task == "next_state":
        return future, _mean(future, numpy)
    if task == "temporal_change":
        made = future - current
        return made, _mean(numpy.abs(made), numpy)
    if task == "gradient_magnitude":
        vertical, horizontal = numpy.gradient(future)
        made = numpy.hypot(vertical, horizontal)
        return made, _mean(made, numpy)
    if task == "laplacian":
        vertical = numpy.gradient(numpy.gradient(future, axis=0), axis=0)
        horizontal = numpy.gradient(numpy.gradient(future, axis=1), axis=1)
        made = vertical + horizontal
        return made, _mean(numpy.abs(made), numpy)
    if task == "threshold_mask":
        level = _mean(future, numpy)
        return (future > level).astype("float32"), level
    if task == "mean_regression":
        return future, _mean(future, numpy)
    if task == "rms_regression":
        squared = numpy.square(future)
        return future, math.sqrt(max(0.0, _mean(squared, numpy)))
    raise ValueError(f"there is no The Well task {task!r}")


def _mean(values: Any, numpy: Any) -> float:
    finite = values[numpy.isfinite(values)]
    if not finite.size:
        raise ValueError("The Well derived target contains no finite pixels")
    return float(finite.mean())


def _floats(values: Any) -> array.array[float]:
    return array.array("f", values.reshape(-1))


def _mask(values: Any, numpy: Any) -> array.array[int]:
    finite = numpy.isfinite(values).astype("uint8", copy=False)
    return array.array("B", finite.reshape(-1))


def _tree(time: int, pairs: int) -> int:
    bucket = min(9, 10 * time // pairs)
    return 0 if bucket < 8 else bucket - 7


def _entries(path: Path, source: Mapping[str, Any], task: str) -> Rows:
    h5py = importlib.import_module("h5py")
    numpy = importlib.import_module("numpy")
    with h5py.File(path, "r") as book:
        field_name, field = _dynamic_field(book)
        samples, times = _counts(field)
        index = 0
        for sample in range(samples):
            current = _frame(field, sample, 0, numpy)
            for time in range(times - 1):
                future = _frame(field, sample, time + 1, numpy)
                raw, normalized, lower, upper = _prepared(current, numpy)
                target_image, target = _target(task, current, future, numpy)
                target_clean = numpy.where(numpy.isfinite(target_image), target_image, 0.0)
                yield _tree(time, times - 1), {
                    "image": _floats(raw),
                    "normalized_image": _floats(normalized),
                    "image_mask": _mask(current, numpy),
                    "target_image": _floats(target_clean.astype("float32", copy=False)),
                    "target_mask": _mask(target_image, numpy),
                    "target": target,
                    "source_min": lower,
                    "source_max": upper,
                    "source_dataset": _text(str(source["name"]), "dataset name"),
                    "field": _text(field_name, "field name"),
                    "sample": sample,
                    "time": time,
                    "split": _tree(time, times - 1),
                    "index": index,
                }
                current = future
                index += 1


def _columns() -> dict[str, Any]:
    return {
        "image": ("f", PIXELS),
        "normalized_image": ("f", PIXELS),
        "image_mask": ("B", PIXELS),
        "target_image": ("f", PIXELS),
        "target_mask": ("B", PIXELS),
        "target": "d",
        "source_min": "d",
        "source_max": "d",
        "source_dataset": ("B", TEXT_BYTES),
        "field": ("B", TEXT_BYTES),
        "sample": "i",
        "time": "i",
        "split": "i",
        "index": "q",
    }


def load(converter: str, paths: Mapping[str, Path], split: str) -> Loaded:
    """Open one visual The Well task with one HDF5 plane resident at a time."""
    if split != "all":
        raise ValueError("The Well educational slices are converted together as split TTrees")
    source_name, separator, task = converter.partition(":")
    source = SOURCE_BY_NAME.get(source_name)
    available = {name for name, _label in _tasks(source)} if source is not None else set()
    if source is None or not separator or task not in available:
        raise ValueError(f"there is no The Well converter {converter!r}")
    if "sample" not in paths:
        raise ValueError(f"The Well {source_name} conversion is missing its sample shard")
    return SPLITS, _columns(), _entries(paths["sample"], source, task)
