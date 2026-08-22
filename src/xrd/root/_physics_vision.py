"""Compact, openly licensed image problems from physics and biomedicine.

The declarations below pin complete logical data payloads rather than sample
shards.  Their readers keep large arrays disk-backed, retain publisher splits,
and turn images, volumes, masks and boxes into fixed-width ROOT branches.
"""

from __future__ import annotations

import array
import importlib
import json
import shutil
import tempfile
import zipfile
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ._swefil_manifest import SWEFIL_FILES, SWEFIL_REVISION, SWEFIL_SOURCE_BYTES

Rows = Iterator[tuple[int, dict[str, Any]]]
Loaded = tuple[tuple[str, ...], dict[str, Any], Rows]

MEDMNIST_CREATORS = (
    "Jiancheng Yang",
    "Rui Shi",
    "Donglai Wei",
    "Zequan Liu",
    "Lin Zhao",
    "Bilian Ke",
    "Hanspeter Pfister",
    "Bingbing Ni",
)
MEDMNIST_ORGAN_CLASSES = (
    "bladder",
    "left_femur",
    "right_femur",
    "heart",
    "left_kidney",
    "right_kidney",
    "liver",
    "left_lung",
    "right_lung",
    "pancreas",
    "spleen",
)
MEDMNIST_ORGAN_3D_CLASSES = (
    "liver",
    "right_kidney",
    "left_kidney",
    "right_femur",
    "left_femur",
    "bladder",
    "heart",
    "right_lung",
    "left_lung",
    "spleen",
    "pancreas",
)
MEDMNIST_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "pathmnist",
        "label": "PathMNIST",
        "title": "107,180 RGB colorectal-histology patches in nine tissue classes",
        "source_bytes": 205_615_438,
        "classes": (
            "adipose",
            "background",
            "debris",
            "lymphocytes",
            "mucus",
            "smooth_muscle",
            "normal_colon_mucosa",
            "cancer_associated_stroma",
            "adenocarcinoma_epithelium",
        ),
        "channels": 3,
        "task": "multiclass colorectal histology classification",
    },
    {
        "name": "chestmnist",
        "label": "ChestMNIST",
        "title": "112,120 frontal chest radiographs with fourteen disease targets",
        "source_bytes": 82_802_576,
        "classes": (
            "atelectasis",
            "cardiomegaly",
            "effusion",
            "infiltration",
            "mass",
            "nodule",
            "pneumonia",
            "pneumothorax",
            "consolidation",
            "edema",
            "emphysema",
            "fibrosis",
            "pleural_thickening",
            "hernia",
        ),
        "channels": 1,
        "multilabel": True,
        "task": "multilabel chest-radiograph classification",
    },
    {
        "name": "octmnist",
        "label": "OCTMNIST",
        "title": "109,309 retinal OCT images in four diagnosis classes",
        "source_bytes": 54_938_180,
        "classes": (
            "choroidal_neovascularization",
            "diabetic_macular_edema",
            "drusen",
            "normal",
        ),
        "channels": 1,
        "task": "multiclass optical-coherence-tomography classification",
    },
    {
        "name": "pneumoniamnist",
        "label": "PneumoniaMNIST",
        "title": "5,856 pediatric chest radiographs labelled normal or pneumonia",
        "source_bytes": 4_170_669,
        "classes": ("normal", "pneumonia"),
        "channels": 1,
        "task": "binary pediatric pneumonia classification",
    },
    {
        "name": "retinamnist",
        "label": "RetinaMNIST",
        "title": "1,600 RGB fundus images with five retinopathy severity grades",
        "source_bytes": 3_291_041,
        "classes": tuple(f"grade_{grade}" for grade in range(5)),
        "channels": 3,
        "task": "ordinal diabetic-retinopathy grading",
    },
    {
        "name": "breastmnist",
        "label": "BreastMNIST",
        "title": "780 breast-ultrasound images for benign-or-normal versus malignant",
        "source_bytes": 559_580,
        "classes": ("malignant", "normal_or_benign"),
        "channels": 1,
        "task": "binary breast-ultrasound classification",
    },
    {
        "name": "bloodmnist",
        "label": "BloodMNIST",
        "title": "17,092 RGB blood-cell images in eight morphology classes",
        "source_bytes": 35_461_855,
        "classes": (
            "basophil",
            "eosinophil",
            "erythroblast",
            "immature_granulocyte",
            "lymphocyte",
            "monocyte",
            "neutrophil",
            "platelet",
        ),
        "channels": 3,
        "task": "multiclass blood-cell morphology classification",
    },
    {
        "name": "tissuemnist",
        "label": "TissueMNIST",
        "title": "236,386 kidney-cortex cell images in eight tissue classes",
        "source_bytes": 124_962_739,
        "classes": (
            "collecting_duct_or_connecting_tubule",
            "distal_convoluted_tubule",
            "glomerular_endothelial_cells",
            "interstitial_endothelial_cells",
            "leukocytes",
            "podocytes",
            "proximal_tubule_segments",
            "thick_ascending_limb",
        ),
        "channels": 1,
        "task": "multiclass kidney-cell morphology classification",
    },
    {
        "name": "organamnist",
        "label": "OrganAMNIST",
        "title": "58,830 axial CT organ crops in eleven anatomy classes",
        "source_bytes": 38_247_708,
        "classes": MEDMNIST_ORGAN_CLASSES,
        "channels": 1,
        "task": "multiclass axial CT organ classification",
    },
    {
        "name": "organcmnist",
        "label": "OrganCMNIST",
        "title": "23,583 coronal CT organ crops in eleven anatomy classes",
        "source_bytes": 15_526_411,
        "classes": MEDMNIST_ORGAN_CLASSES,
        "channels": 1,
        "task": "multiclass coronal CT organ classification",
    },
    {
        "name": "organsmnist",
        "label": "OrganSMNIST",
        "title": "25,211 sagittal CT organ crops in eleven anatomy classes",
        "source_bytes": 16_528_359,
        "classes": MEDMNIST_ORGAN_CLASSES,
        "channels": 1,
        "task": "multiclass sagittal CT organ classification",
    },
    {
        "name": "organmnist3d",
        "label": "OrganMNIST3D",
        "title": "1,742 CT volumes in eleven anatomy classes",
        "source_bytes": 32_657_349,
        "classes": MEDMNIST_ORGAN_3D_CLASSES,
        "channels": 1,
        "volume": True,
        "task": "multiclass 3D CT organ classification",
    },
    {
        "name": "nodulemnist3d",
        "label": "NoduleMNIST3D",
        "title": "1,633 lung-nodule CT volumes labelled benign or malignant",
        "source_bytes": 29_299_364,
        "classes": ("benign", "malignant"),
        "channels": 1,
        "volume": True,
        "task": "binary 3D lung-nodule malignancy classification",
    },
    {
        "name": "adrenalmnist3d",
        "label": "AdrenalMNIST3D",
        "title": "1,584 expert-annotated adrenal shape-mask volumes",
        "source_bytes": 276_833,
        "classes": ("normal", "hyperplasia"),
        "channels": 1,
        "volume": True,
        "task": "binary 3D adrenal-shape classification",
    },
    {
        "name": "fracturemnist3d",
        "label": "FractureMNIST3D",
        "title": "1,370 rib-fracture CT volumes in three clinical classes",
        "source_bytes": 3_278_419,
        "classes": ("buckle", "nondisplaced", "displaced"),
        "channels": 1,
        "volume": True,
        "task": "multiclass 3D rib-fracture classification",
    },
    {
        "name": "vesselmnist3d",
        "label": "VesselMNIST3D",
        "title": "1,908 voxelized brain-vessel segments with aneurysm labels",
        "source_bytes": 398_349,
        "classes": ("vessel", "aneurysm"),
        "channels": 1,
        "volume": True,
        "task": "binary 3D intracranial-aneurysm classification",
    },
    {
        "name": "synapsemnist3d",
        "label": "SynapseMNIST3D",
        "title": "1,759 electron-microscopy synapse volumes in two functional classes",
        "source_bytes": 38_034_583,
        "classes": ("inhibitory", "excitatory"),
        "channels": 1,
        "volume": True,
        "task": "binary 3D synapse classification",
    },
)
MEDMNIST_BY_NAME = {spec["name"]: spec for spec in MEDMNIST_SPECS}

GALAXY10_CLASSES = (
    "face_on_disk_no_spiral",
    "smooth_completely_round",
    "smooth_in_between_round",
    "smooth_cigar_shaped",
    "edge_on_disk_rounded_bulge",
    "edge_on_disk_boxy_bulge",
    "edge_on_disk_no_bulge",
    "face_on_disk_tight_spiral",
    "face_on_disk_medium_spiral",
    "face_on_disk_loose_spiral",
)
MARS_CLASSES = (
    "apxs",
    "apxs_calibration_target",
    "chemcam_calibration_target",
    "chemin_inlet_open",
    "drill",
    "drill_holes",
    "drt_front",
    "drt_side",
    "ground",
    "horizon",
    "inlet",
    "mahli",
    "mahli_calibration_target",
    "mastcam",
    "mastcam_calibration_target",
    "observation_tray",
    "portion_box",
    "portion_tube",
    "portion_tube_opening",
    "rems_uv_sensor",
    "rover_rear_deck",
    "scoop",
    "sun",
    "turret",
    "wheel",
)
SWEFIL_CLASSES = ("processed", "raw")
SWEFIL_OBJECT_CLASSES = ("qrf", "irf", "arf", "sunspot")
SWEFIL_SIDE = 512
SWEFIL_MAX_OBJECTS = 40
SWEFIL_ROLES = tuple(f"part_{index:03d}" for index in range(len(SWEFIL_FILES)))


def _medmnist_declaration(spec: Mapping[str, Any]) -> dict[str, Any]:
    name = str(spec["name"])
    shape = "28 by 28 by 28" if spec.get("volume") else "28 by 28"
    targets = "fourteen-target multilabel vector" if spec.get("multilabel") else "class label"
    layout = (
        "one samples TTree per publisher split with a fourteen-wide target vector"
        if spec.get("multilabel")
        else "one class TTree per publisher split"
    )
    return {
        **spec,
        "licence": "CC BY 4.0",
        "source": "https://zenodo.org/records/10519652",
        "url": f"https://zenodo.org/api/records/10519652/files/{name}.npz/content",
        "converter": f"open:vision:medmnist:{name}",
        "transformation": (
            f"safely extracted and memory-mapped the publisher's {shape} uint8 NPZ arrays "
            f"without pickle; retained every image or volume and its {targets} without "
            "scaling, preserved the official train/validation/test partitions, and left "
            "the MedMNIST authors' documented source crops, windows and resizing unchanged"
        ),
        "splits": ("train", "validation", "test"),
        "requires": ("numpy",),
        "modality": "biomedical imagery" if not spec.get("volume") else "3D biomedical imagery",
        "creators": MEDMNIST_CREATORS,
        "publisher": "MedMNIST project",
        "origin": "https://doi.org/10.5281/zenodo.10519652",
        "repository": "Zenodo",
        "citation": "https://doi.org/10.1038/s41597-022-01721-8",
        "layout": layout,
        "basket_size": 2 * 1024 * 1024,
    }


def _swefil_sources() -> tuple[dict[str, str], dict[str, int]]:
    base = f"https://huggingface.co/datasets/antonio-reche/SWEFil/resolve/{SWEFIL_REVISION}/"
    sources = {
        role: f"{base}{quote(name, safe='/')}?download=true"
        for role, (name, _size) in zip(SWEFIL_ROLES, SWEFIL_FILES)
    }
    sizes = {role: size for role, (_name, size) in zip(SWEFIL_ROLES, SWEFIL_FILES)}
    return sources, sizes


_SWEFIL_SOURCES, _SWEFIL_SIZES = _swefil_sources()

PHYSICS_VISION: tuple[dict[str, Any], ...] = (
    *(_medmnist_declaration(spec) for spec in MEDMNIST_SPECS),
    {
        "name": "galaxy10_sdss",
        "label": "Galaxy10 SDSS",
        "title": "21,785 compact SDSS galaxy images in ten morphology classes",
        "licence": "CC BY 4.0",
        "source": "https://zenodo.org/records/10844811",
        "url": "https://zenodo.org/api/records/10844811/files/Galaxy10.h5/content",
        "source_bytes": 210_234_548,
        "converter": "open:vision:galaxy10",
        "transformation": (
            "read the complete HDF5 arrays without loading them wholesale; preserved every "
            "69 by 69 channel-last RGB uint8 SDSS image and Galaxy Zoo morphology label "
            "without scaling or inventing a split, and wrote one ROOT TTree per class"
        ),
        "classes": GALAXY10_CLASSES,
        "requires": ("h5py",),
        "modality": "astronomical survey imagery",
        "task": "galaxy morphology classification",
        "creators": ("W. Henry Leung", "Jo Bovy"),
        "publisher": (
            "University of Toronto Department of Astronomy & Astrophysics; "
            "SDSS imaging with Galaxy Zoo labels"
        ),
        "origin": "https://astronn.readthedocs.io/en/latest/galaxy10.html",
        "repository": "Zenodo",
        "citation": "https://doi.org/10.1093/mnras/sty3217",
        "basket_size": 2 * 1024 * 1024,
    },
    {
        "name": "mars_surface_images",
        "label": "Mars Surface Image v1",
        "title": "6,691 compact Curiosity rover images across 24 populated categories",
        "licence": "CC BY-SA 4.0",
        "source": "https://zenodo.org/records/1049137",
        "url": "https://zenodo.org/api/records/1049137/files/msl-images.zip/content",
        "source_bytes": 60_635_475,
        "converter": "open:vision:mars",
        "transformation": (
            "decoded the 6,691 JPEG members named by the publisher's sol-separated split "
            "manifests; converted grayscale frames to RGB and letterboxed variable geometry "
            "within 256 by 256 without upscaling, retaining original dimensions, instrument, "
            "sol and source category; the 46 archive images absent from every label manifest "
            "remain source-only rather than receiving invented labels"
        ),
        "classes": MARS_CLASSES,
        "splits": ("train", "validation", "test"),
        "requires": ("PIL",),
        "modality": "planetary rover imagery",
        "task": "Mars surface and rover-component classification",
        "creators": ("Alice Stanboli", "Kiri Wagstaff"),
        "publisher": "NASA Jet Propulsion Laboratory",
        "origin": "https://doi.org/10.5281/zenodo.1049137",
        "repository": "Zenodo",
        "citation": "https://doi.org/10.1609/aaai.v32i1.11404",
        "basket_size": 2 * 1024 * 1024,
    },
    {
        "name": "swefil",
        "label": "SWEFil v1",
        "title": "554 H-alpha solar images with 4,144 filament and sunspot annotations",
        "licence": "CC BY 4.0",
        "source": "https://huggingface.co/datasets/antonio-reche/SWEFil",
        "source_bytes": SWEFIL_SOURCE_BYTES,
        "sources": _SWEFIL_SOURCES,
        "source_sizes": _SWEFIL_SIZES,
        "converter": "open:vision:swefil",
        "transformation": (
            "joined all 554 raw and processed 2048 by 2048 H-alpha JPEGs to the pinned "
            "COCO train/test annotations; resized RGB images to 512 by 512, rasterized every "
            "polygon into four overlap-preserving binary masks, retained normalized boxes, "
            "source areas, category and annotation ids with a true object count, and omitted "
            "only repository documentation and two display-only example montages"
        ),
        "classes": SWEFIL_CLASSES,
        "splits": ("train", "test"),
        "requires": ("PIL",),
        "modality": "solar H-alpha imagery",
        "task": "solar-filament detection, classification and instance segmentation",
        "creators": ("Antonio Reche", "Consuelo Cid"),
        "publisher": (
            "University of Alcalá Space Weather Research Group; GONG data from the "
            "NSO Integrated Synoptic Program (AURA/NSF/NOAA)"
        ),
        "origin": "https://huggingface.co/datasets/antonio-reche/SWEFil",
        "repository": "Hugging Face Hub",
        "citation": "https://doi.org/10.5281/zenodo.13889941",
        "basket_size": 4 * 1024 * 1024,
        "layout": "processed and raw TTrees per official split, each with four binary masks",
    },
)


def _copy_member(archive: zipfile.ZipFile, name: str, target: Path) -> None:
    try:
        source = archive.open(name)
    except KeyError:
        raise ValueError(f"MedMNIST source is missing {name!r}") from None
    with source, target.open("wb") as destination:
        shutil.copyfileobj(source, destination, length=1 << 20)


def _medmnist_shape(spec: Mapping[str, Any]) -> tuple[int, ...]:
    if spec.get("volume"):
        return (28, 28, 28)
    if spec["channels"] == 3:
        return (28, 28, 3)
    return (28, 28)


def _check_medmnist_arrays(images: Any, labels: Any, spec: Mapping[str, Any]) -> None:
    expected = _medmnist_shape(spec)
    targets = len(spec["classes"]) if spec.get("multilabel") else 1
    if images.dtype.name != "uint8" or tuple(images.shape[1:]) != expected:
        raise ValueError(f"{spec['label']} images are not uint8 arrays shaped (*, {expected})")
    if images.shape[0] != labels.shape[0] or tuple(labels.shape[1:]) != (targets,):
        raise ValueError(f"{spec['label']} images and labels have incompatible shapes")
    if labels.dtype.kind not in "biu":
        raise ValueError(f"{spec['label']} labels are not integers")


def _medmnist_row(
    image: Any, target: Any, spec: Mapping[str, Any], index: int
) -> tuple[int, dict[str, Any]]:
    pixels = array.array("B", image.reshape(-1))
    if spec.get("multilabel"):
        values = array.array("B", (int(value) for value in target))
        if any(value not in (0, 1) for value in values):
            raise ValueError(f"{spec['label']} row {index} has a non-binary target")
        return 0, {"image": pixels, "targets": values, "index": index}
    label = int(target[0])
    if label < 0 or label >= len(spec["classes"]):
        raise ValueError(f"{spec['label']} row {index} has unknown class {label}")
    return label, {"image": pixels, "label": label, "index": index}


def _medmnist_entries(path: Path, split: str, spec: Mapping[str, Any]) -> Rows:
    numpy = importlib.import_module("numpy")
    key = {"train": "train", "validation": "val", "test": "test"}[split]
    image_name, label_name = f"{key}_images.npy", f"{key}_labels.npy"
    with zipfile.ZipFile(path) as archive, tempfile.TemporaryDirectory(
        prefix=f"xrd-{spec['name']}-"
    ) as directory:
        image_path = Path(directory) / image_name
        label_path = Path(directory) / label_name
        _copy_member(archive, image_name, image_path)
        _copy_member(archive, label_name, label_path)
        images = numpy.load(image_path, mmap_mode="r", allow_pickle=False)
        labels = numpy.load(label_path, mmap_mode="r", allow_pickle=False)
        _check_medmnist_arrays(images, labels, spec)
        for index in range(images.shape[0]):
            yield _medmnist_row(images[index], labels[index], spec, index)


def _medmnist(path: Path, split: str, name: str) -> Loaded:
    try:
        spec = MEDMNIST_BY_NAME[name]
    except KeyError:
        raise ValueError(f"there is no MedMNIST source {name!r}") from None
    pixels = 28**3 if spec.get("volume") else 28 * 28 * int(spec["channels"])
    columns: dict[str, Any] = {"image": ("B", pixels), "index": "q"}
    if spec.get("multilabel"):
        columns["targets"] = ("B", len(spec["classes"]))
        trees = ("samples",)
    else:
        columns["label"] = "i"
        trees = tuple(spec["classes"])
    return trees, columns, _medmnist_entries(path, split, spec)


def _galaxy10_entries(path: Path) -> Rows:
    h5py = importlib.import_module("h5py")
    with h5py.File(path, "r") as source:
        if set(source) != {"ans", "images"}:
            raise ValueError("Galaxy10 SDSS does not contain exactly ans and images arrays")
        images, labels = source["images"], source["ans"]
        if images.shape != (21_785, 69, 69, 3) or labels.shape != (21_785,):
            raise ValueError("Galaxy10 SDSS has incompatible image or label geometry")
        if images.dtype.name != "uint8" or labels.dtype.name != "uint8":
            raise ValueError("Galaxy10 SDSS images and labels must be uint8")
        for index in range(images.shape[0]):
            label = int(labels[index])
            if label >= len(GALAXY10_CLASSES):
                raise ValueError(f"Galaxy10 SDSS row {index} has unknown class {label}")
            yield label, {
                "image": array.array("B", images[index].reshape(-1)),
                "label": label,
                "index": index,
            }


def _galaxy10(path: Path) -> Loaded:
    columns: dict[str, Any] = {
        "image": ("B", 69 * 69 * 3),
        "label": "i",
        "index": "q",
    }
    return GALAXY10_CLASSES, columns, _galaxy10_entries(path)


def _fixed_bytes(value: str, width: int, description: str) -> array.array[int]:
    encoded = value.encode("ascii")
    if len(encoded) > width:
        raise ValueError(f"{description} is longer than {width} bytes")
    return array.array("B", encoded + b"\0" * (width - len(encoded)))


def _mars_image(archive: zipfile.ZipFile, member: str) -> tuple[array.array[int], tuple[int, ...]]:
    image_module = importlib.import_module("PIL.Image")
    try:
        held = archive.open(member)
    except KeyError:
        raise ValueError(f"Mars split names missing image {member!r}") from None
    with held, image_module.open(held) as opened:
        width, height = opened.size
        grayscale = int(opened.mode == "L")
        image = opened.convert("RGB")
        image.thumbnail((256, 256), image_module.Resampling.BILINEAR)
        resized_width, resized_height = image.size
        canvas = image_module.new("RGB", (256, 256))
        left, top = (256 - resized_width) // 2, (256 - resized_height) // 2
        canvas.paste(image, (left, top))
        pixels = array.array("B", canvas.tobytes())
    return pixels, (width, height, resized_width, resized_height, left, top, grayscale)


def _mars_manifest(archive: zipfile.ZipFile, split: str) -> list[tuple[str, int]]:
    prefix = {"train": "train", "validation": "val", "test": "test"}[split]
    name = f"{prefix}-calibrated-shuffled.txt"
    try:
        text = archive.read(name).decode("ascii")
    except (KeyError, UnicodeDecodeError) as error:
        raise ValueError(f"Mars source has no valid {name}") from error
    rows: list[tuple[str, int]] = []
    for at, line in enumerate(text.splitlines(), 1):
        member, separator, raw_label = line.rpartition(" ")
        if not separator or not raw_label.isdigit() or not member.startswith("calibrated/"):
            raise ValueError(f"Mars {name} row {at} is invalid")
        label = int(raw_label)
        if label >= len(MARS_CLASSES):
            raise ValueError(f"Mars {name} row {at} has unknown class {label}")
        rows.append((member, label))
    return rows


def _mars_entries(path: Path, split: str) -> Rows:
    instruments = {"ML": 0, "MR": 1, "MH": 2}
    with zipfile.ZipFile(path) as archive:
        for index, (member, label) in enumerate(_mars_manifest(archive, split)):
            filename = member.rsplit("/", 1)[-1]
            try:
                sol = int(filename[:4])
                instrument = instruments[filename[4:6]]
            except (ValueError, KeyError):
                raise ValueError(f"Mars image {filename!r} has an unknown product name") from None
            pixels, geometry = _mars_image(archive, member)
            width, height, resized_width, resized_height, left, top, grayscale = geometry
            yield label, {
                "image": pixels,
                "label": label,
                "index": index,
                "sol": sol,
                "instrument": instrument,
                "source_width": width,
                "source_height": height,
                "resized_width": resized_width,
                "resized_height": resized_height,
                "left_padding": left,
                "top_padding": top,
                "source_grayscale": grayscale,
                "product": _fixed_bytes(filename, 48, f"Mars product {filename!r}"),
            }


def _mars(path: Path, split: str) -> Loaded:
    columns: dict[str, Any] = {
        "image": ("B", 256 * 256 * 3),
        "label": "i",
        "index": "q",
        "sol": "i",
        "instrument": "B",
        "source_width": "i",
        "source_height": "i",
        "resized_width": "i",
        "resized_height": "i",
        "left_padding": "i",
        "top_padding": "i",
        "source_grayscale": "B",
        "product": ("B", 48),
    }
    return MARS_CLASSES, columns, _mars_entries(path, split)


def _swefil_path_map(paths: Mapping[str, Path]) -> dict[str, Path]:
    expected = set(SWEFIL_ROLES)
    if set(paths) != expected:
        missing = sorted(expected - set(paths))
        extra = sorted(set(paths) - expected)
        raise ValueError(f"SWEFil source roles differ; missing={missing}, extra={extra}")
    return {name: paths[role] for role, (name, _size) in zip(SWEFIL_ROLES, SWEFIL_FILES)}


def _swefil_document(path: Path, split: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"SWEFil {split} annotations are not valid JSON") from error
    if not isinstance(document, dict):
        raise ValueError(f"SWEFil {split} annotations are not a JSON object")
    categories = document.get("categories")
    names = tuple(category.get("name") for category in categories or ())
    if names != ("QRF", "IRF", "ARF", "SUNSPOT"):
        raise ValueError(f"SWEFil {split} annotations have incompatible categories")
    if not isinstance(document.get("images"), list) or not isinstance(
        document.get("annotations"), list
    ):
        raise ValueError(f"SWEFil {split} annotations have no image and annotation lists")
    return document


def _swefil_annotations(document: Mapping[str, Any]) -> Mapping[int, list[Mapping[str, Any]]]:
    grouped: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for annotation in document["annotations"]:
        image_id = annotation.get("image_id")
        if not isinstance(image_id, int):
            raise ValueError("SWEFil annotation has no integer image id")
        grouped[image_id].append(annotation)
    return grouped


def _swefil_category(annotation: Mapping[str, Any]) -> int:
    category = annotation.get("category_id")
    if not isinstance(category, int) or category not in range(len(SWEFIL_OBJECT_CLASSES)):
        raise ValueError("SWEFil annotation has an unknown category")
    return category


def _swefil_polygon(polygon: Any) -> tuple[tuple[float, float], ...]:
    if not isinstance(polygon, list) or len(polygon) < 6 or len(polygon) % 2:
        raise ValueError("SWEFil annotation has an invalid polygon")
    points = [value * SWEFIL_SIDE / 2048 for value in polygon]
    return tuple(zip(points[::2], points[1::2]))


def _draw_swefil_annotation(masks: Sequence[Any], annotation: Mapping[str, Any]) -> None:
    draw_module = importlib.import_module("PIL.ImageDraw")
    category = _swefil_category(annotation)
    polygons = annotation.get("segmentation")
    if not isinstance(polygons, list):
        raise ValueError("SWEFil annotation has no polygon segmentation")
    drawing = draw_module.Draw(masks[category])
    for polygon in polygons:
        drawing.polygon(_swefil_polygon(polygon), fill=1)


def _swefil_masks(annotations: Sequence[Mapping[str, Any]]) -> tuple[array.array[int], ...]:
    image_module = importlib.import_module("PIL.Image")
    masks = [image_module.new("L", (SWEFIL_SIDE, SWEFIL_SIDE)) for _ in SWEFIL_OBJECT_CLASSES]
    for annotation in annotations:
        _draw_swefil_annotation(masks, annotation)
    return tuple(array.array("B", mask.tobytes()) for mask in masks)


def _swefil_object(annotation: Mapping[str, Any]) -> tuple[list[Any], int, int, float]:
    bbox = annotation.get("bbox")
    identifier = annotation.get("id")
    area = annotation.get("area")
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise ValueError("SWEFil annotation has no four-value box")
    category = _swefil_category(annotation)
    if not isinstance(identifier, int) or not isinstance(area, (int, float)):
        raise ValueError("SWEFil annotation has no numeric id and area")
    return bbox, category, identifier, float(area)


def _swefil_objects(annotations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if len(annotations) > SWEFIL_MAX_OBJECTS:
        raise ValueError(f"SWEFil image has {len(annotations)} annotations")
    boxes = array.array("f", [0.0] * (SWEFIL_MAX_OBJECTS * 4))
    areas = array.array("f", [0.0] * SWEFIL_MAX_OBJECTS)
    labels = array.array("b", [-1] * SWEFIL_MAX_OBJECTS)
    identifiers = array.array("i", [-1] * SWEFIL_MAX_OBJECTS)
    for at, annotation in enumerate(annotations):
        bbox, category, identifier, area = _swefil_object(annotation)
        boxes[at * 4 : at * 4 + 4] = array.array("f", (float(value) / 2048 for value in bbox))
        areas[at], labels[at], identifiers[at] = float(area) / 2048**2, category, identifier
    return {
        "objects": len(annotations),
        "boxes": boxes,
        "object_areas": areas,
        "object_labels": labels,
        "annotation_ids": identifiers,
    }


def _swefil_pixels(path: Path, filename: str) -> array.array[int]:
    image_module = importlib.import_module("PIL.Image")
    with image_module.open(path) as opened:
        if opened.size != (2048, 2048):
            raise ValueError(f"SWEFil image {filename!r} is not 2048 by 2048")
        resized = opened.convert("RGB").resize(
            (SWEFIL_SIDE, SWEFIL_SIDE), image_module.Resampling.BILINEAR
        )
        return array.array("B", resized.tobytes())


def _swefil_row(
    image: Mapping[str, Any],
    annotations: Sequence[Mapping[str, Any]],
    paths: Mapping[str, Path],
    index: int,
) -> tuple[int, dict[str, Any]]:
    filename = image.get("file_name")
    image_id = image.get("id")
    if not isinstance(filename, str) or filename not in paths or not isinstance(image_id, int):
        raise ValueError("SWEFil image record has no registered filename and integer id")
    if image.get("width") != 2048 or image.get("height") != 2048:
        raise ValueError(f"SWEFil image {filename!r} has incompatible declared geometry")
    raw = int(filename.endswith("_raw.jpg"))
    stem = Path(filename).stem.removesuffix("_raw")
    try:
        timestamp = int(stem[:14])
        observatory = "BCLMTU".index(stem[14])
    except (ValueError, IndexError):
        raise ValueError(f"SWEFil image {filename!r} has an unknown source name") from None
    masks = _swefil_masks(annotations)
    row = {
        "image": _swefil_pixels(paths[filename], filename),
        "image_id": image_id,
        "index": index,
        "view": raw,
        "timestamp": timestamp,
        "observatory": observatory,
        **_swefil_objects(annotations),
        **{f"{name}_mask": mask for name, mask in zip(SWEFIL_OBJECT_CLASSES, masks)},
    }
    return raw, row


def _swefil_entries(paths: Mapping[str, Path], split: str) -> Rows:
    mapped = _swefil_path_map(paths)
    document = _swefil_document(mapped[f"{split}.json"], split)
    annotations = _swefil_annotations(document)
    for index, image in enumerate(document["images"]):
        if not isinstance(image, Mapping):
            raise ValueError(f"SWEFil {split} image row {index} is not an object")
        image_id = image.get("id")
        if not isinstance(image_id, int):
            raise ValueError(f"SWEFil {split} image row {index} has no integer id")
        yield _swefil_row(image, annotations.get(image_id, ()), mapped, index)


def _swefil(paths: Mapping[str, Path], split: str) -> Loaded:
    columns: dict[str, Any] = {
        "image": ("B", SWEFIL_SIDE * SWEFIL_SIDE * 3),
        "image_id": "i",
        "index": "q",
        "view": "B",
        "timestamp": "q",
        "observatory": "B",
        "objects": "i",
        "boxes": ("f", SWEFIL_MAX_OBJECTS * 4),
        "object_areas": ("f", SWEFIL_MAX_OBJECTS),
        "object_labels": ("b", SWEFIL_MAX_OBJECTS),
        "annotation_ids": ("i", SWEFIL_MAX_OBJECTS),
        **{
            f"{name}_mask": ("B", SWEFIL_SIDE * SWEFIL_SIDE)
            for name in SWEFIL_OBJECT_CLASSES
        },
    }
    return SWEFIL_CLASSES, columns, _swefil_entries(paths, split)


def load(converter: str, paths: Mapping[str, Path], split: str) -> Loaded:
    """Open one registered compact physics-vision source."""
    if converter.startswith("medmnist:"):
        return _medmnist(paths["archive"], split, converter.removeprefix("medmnist:"))
    if converter == "galaxy10":
        return _galaxy10(paths["archive"])
    if converter == "mars":
        return _mars(paths["archive"], split)
    if converter == "swefil":
        return _swefil(paths, split)
    raise ValueError(f"there is no physics-vision converter {converter!r}")
