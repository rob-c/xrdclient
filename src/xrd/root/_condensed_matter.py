"""Open condensed-matter image collections and materials benchmarks.

The visual readers preserve paired biases, masks, microscopy channels and
publisher labels.  Matbench structures are streamed from gzip JSON and become
compact crystal tensors plus three 32 by 32 fractional-coordinate projections;
the largest JSON document is never materialised in memory.
"""

from __future__ import annotations

import array
import csv
import gzip
import importlib
import io
import json
import math
import re
import sys
import tarfile
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import IO, Any

Rows = Iterator[tuple[int, dict[str, Any]]]
Loaded = tuple[tuple[str, ...], dict[str, Any], Rows]

IMAGE_SIDE = 256
CRYSTAL_SIDE = 32
MAX_CRYSTAL_COMPONENTS = 512
FORMULA_BYTES = 128
JARVIS_CLASSES = (
    "hexagonal",
    "square",
    "rectangle",
    "centered_rectangle",
    "oblique",
)
NFFA_CLASSES = ("fibres", "coated_surface", "porous_sponge", "powder", "tips")
NANOWIRE_CLASSES = ("dispersed_nanoparticles", "separate_clusters", "percolating_cluster")
SKYRMION_CLASSES = ("background", "skyrmion", "defect")
WSE2_CLASSES = ("background", "trough", "peak")
PEROVSKITE_CLASSES = ("background", "abo3", "abx3", "pbi2", "defect")
AFM_CHANNELS = ("height", "amplitude", "phase", "z_sensor", "modified_phase")

ELEMENTS = (
    "H",
    "He",
    "Li",
    "Be",
    "B",
    "C",
    "N",
    "O",
    "F",
    "Ne",
    "Na",
    "Mg",
    "Al",
    "Si",
    "P",
    "S",
    "Cl",
    "Ar",
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
    "Kr",
    "Rb",
    "Sr",
    "Y",
    "Zr",
    "Nb",
    "Mo",
    "Tc",
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
    "Xe",
    "Cs",
    "Ba",
    "La",
    "Ce",
    "Pr",
    "Nd",
    "Pm",
    "Sm",
    "Eu",
    "Gd",
    "Tb",
    "Dy",
    "Ho",
    "Er",
    "Tm",
    "Yb",
    "Lu",
    "Hf",
    "Ta",
    "W",
    "Re",
    "Os",
    "Ir",
    "Pt",
    "Au",
    "Hg",
    "Tl",
    "Pb",
    "Bi",
    "Po",
    "At",
    "Rn",
    "Fr",
    "Ra",
    "Ac",
    "Th",
    "Pa",
    "U",
    "Np",
    "Pu",
    "Am",
    "Cm",
    "Bk",
    "Cf",
    "Es",
    "Fm",
    "Md",
    "No",
    "Lr",
    "Rf",
    "Db",
    "Sg",
    "Bh",
    "Hs",
    "Mt",
    "Ds",
    "Rg",
    "Cn",
    "Nh",
    "Fl",
    "Mc",
    "Lv",
    "Ts",
    "Og",
)
ATOMIC_NUMBER = {symbol: number for number, symbol in enumerate(ELEMENTS, 1)}

MATBENCH_CREATORS = (
    "Alexander Dunn",
    "Qi Wang",
    "Alex Ganose",
    "Daniel Dopp",
    "Anubhav Jain",
)
MATBENCH_COMMON = {
    "licence": "MIT",
    "source": "https://matbench.materialsproject.org/",
    "publisher": "Materials Project",
    "origin": "https://matbench.materialsproject.org/",
    "repository": "Materials Project Matbench",
    "citation": "https://doi.org/10.1038/s41524-020-00406-3",
    "creators": MATBENCH_CREATORS,
    "splits": ("all",),
    "requires": (),
    "modality": "crystal structure and composition",
    "basket_size": 2 * 1024 * 1024,
}

MATBENCH_TASKS: tuple[dict[str, Any], ...] = (
    {
        "name": "matbench_dielectric",
        "label": "Matbench dielectric",
        "title": "4,764 crystals for refractive-index regression",
        "source_bytes": 3_608_015,
        "input": "structure",
        "target": "refractive index",
        "task": "crystal-structure refractive-index regression",
    },
    {
        "name": "matbench_expt_gap",
        "label": "Matbench experimental gap",
        "title": "4,604 compositions with experimental electronic band gaps",
        "source_bytes": 37_200,
        "input": "composition",
        "target": "experimental band gap (eV)",
        "task": "composition-to-band-gap regression",
    },
    {
        "name": "matbench_expt_is_metal",
        "label": "Matbench experimental metallicity",
        "title": "4,921 compositions labelled metal or non-metal",
        "source_bytes": 34_623,
        "input": "composition",
        "target": "metallicity",
        "task": "composition-based binary metallicity classification",
        "classes": ("non_metal", "metal"),
    },
    {
        "name": "matbench_glass",
        "label": "Matbench glass formation",
        "title": "5,680 alloy compositions labelled by glass-forming ability",
        "source_bytes": 39_729,
        "input": "composition",
        "target": "glass-forming ability",
        "task": "binary metallic-glass formation classification",
        "classes": ("not_glass_forming", "glass_forming"),
    },
    {
        "name": "matbench_jdft2d",
        "label": "Matbench JARVIS 2D",
        "title": "636 two-dimensional crystals with exfoliation energies",
        "source_bytes": 267_131,
        "input": "structure",
        "target": "exfoliation energy (meV/atom)",
        "task": "2D-material exfoliation-energy regression",
    },
    {
        "name": "matbench_log_gvrh",
        "label": "Matbench shear modulus",
        "title": "10,987 crystals with log10 VRH shear moduli",
        "source_bytes": 4_166_214,
        "input": "structure",
        "target": "log10 shear modulus (GPa)",
        "task": "crystal-structure shear-modulus regression",
    },
    {
        "name": "matbench_log_kvrh",
        "label": "Matbench bulk modulus",
        "title": "10,987 crystals with log10 VRH bulk moduli",
        "source_bytes": 4_174_387,
        "input": "structure",
        "target": "log10 bulk modulus (GPa)",
        "task": "crystal-structure bulk-modulus regression",
    },
    {
        "name": "matbench_mp_e_form",
        "label": "Matbench formation energy",
        "title": "132,752 Materials Project crystals with formation energies",
        "source_bytes": 166_734_239,
        "input": "structure",
        "target": "formation energy (eV/atom)",
        "task": "crystal-structure formation-energy regression",
    },
    {
        "name": "matbench_mp_gap",
        "label": "Matbench PBE band gap",
        "title": "106,113 Materials Project crystals with PBE band gaps",
        "source_bytes": 137_068_566,
        "input": "structure",
        "target": "PBE band gap (eV)",
        "task": "crystal-structure band-gap regression",
    },
    {
        "name": "matbench_mp_is_metal",
        "label": "Matbench computed metallicity",
        "title": "106,113 Materials Project crystals labelled metal or non-metal",
        "source_bytes": 136_698_078,
        "input": "structure",
        "target": "computed metallicity",
        "task": "crystal-structure binary metallicity classification",
        "classes": ("non_metal", "metal"),
    },
    {
        "name": "matbench_perovskites",
        "label": "Matbench perovskites",
        "title": "18,928 perovskite crystals with unit-cell formation energies",
        "source_bytes": 4_193_112,
        "input": "structure",
        "target": "formation energy (eV/unit cell)",
        "task": "perovskite formation-energy regression",
    },
    {
        "name": "matbench_phonons",
        "label": "Matbench phonons",
        "title": "1,265 crystals with optical-phonon peak frequencies",
        "source_bytes": 459_672,
        "input": "structure",
        "target": "last phonon DOS peak (cm^-1)",
        "task": "crystal-structure phonon-frequency regression",
    },
    {
        "name": "matbench_steels",
        "label": "Matbench steels",
        "title": "312 steel compositions with experimental yield strengths",
        "source_bytes": 8_836,
        "input": "composition",
        "target": "yield strength (MPa)",
        "task": "steel yield-strength regression",
    },
)


def _matbench_declaration(task: Mapping[str, Any]) -> dict[str, Any]:
    name = str(task["name"])
    structural = task["input"] == "structure"
    representation = (
        "retained the lattice and every site-species occupancy in padded crystal tensors; "
        "derived normalized 8x16 elemental and three 32x32 fractional-coordinate images"
        if structural
        else "parsed the published chemical formula into a normalized 8x16 elemental image"
    )
    return {
        **MATBENCH_COMMON,
        **task,
        "url": f"https://ml.materialsproject.org/projects/{name}.json.gz",
        "converter": f"open:condensed:matbench:{name}",
        "classes": tuple(task.get("classes", ())),
        "transformation": (
            f"streamed the row-oriented gzip JSON without reordering; {representation}; "
            f"retained the publisher target as {task['target']}"
        ),
        "layout": "one class TTree for classifications; one samples TTree for regressions",
    }


VISUAL_CONDENSED_MATTER: tuple[dict[str, Any], ...] = (
    {
        "name": "jarvis_stm_bravais",
        "label": "NIST JARVIS-STM",
        "title": "716 paired-bias STM simulations in five 2D Bravais classes",
        "licence": "CC BY 4.0",
        "source": "https://figshare.com/collections/Computational_scanning_tunneling_microscope_image_database/3883270",
        "source_bytes": 313_926_063,
        "sources": {
            "images": "https://ndownloader.figshare.com/files/21884952",
            "labels": "https://ndownloader.figshare.com/files/21893379",
        },
        "source_sizes": {"images": 313_906_662, "labels": 19_401},
        "converter": "open:condensed:jarvis_stm",
        "transformation": (
            "paired each material's +0.5 eV and -0.5 eV constant-height JPEGs; "
            "RGB-letterboxed both to 256x256 while retaining source geometry, "
            "JARVIS id and publisher Bravais label"
        ),
        "classes": JARVIS_CLASSES,
        "splits": ("all",),
        "requires": ("PIL",),
        "modality": "computational scanning tunnelling microscopy",
        "task": "paired-image five-class Bravais-lattice classification",
        "basket_size": 2 * 1024 * 1024,
        "layout": (
            "one TTree per Bravais lattice, with positive and negative bias images "
            "paired by material"
        ),
        "creators": (
            "Kamal Choudhary",
            "Kevin F. Garrity",
            "Charles Camp",
            "Sergei V. Kalinin",
            "Rama Vasudevan",
            "Maxim Ziatdinov",
            "Francesca Tavazza",
        ),
        "publisher": "National Institute of Standards and Technology",
        "origin": "https://doi.org/10.6084/m9.figshare.c.3883270",
        "repository": "Figshare",
        "citation": "https://doi.org/10.1038/s41597-021-00824-y",
    },
    {
        "name": "nffa_sem_compact",
        "label": "NFFA 100% SEM compact",
        "title": "Five nanomaterials SEM classes selected below the 2 GB ceiling",
        "licence": "CC BY 4.0",
        "source": "https://b2share.eudat.eu/records/f1aa0f5ad38c456eaf7b04d47a65af53",
        "source_bytes": 1_974_538_240,
        "sources": {
            name: f"https://b2share.eudat.eu/api/records/862nr-cn036/files/{filename}/content"
            for name, filename in zip(
                NFFA_CLASSES,
                (
                    "Fibres.tar",
                    "Films_Coated_Surface.tar",
                    "Porous_Sponge.tar",
                    "Powder.tar",
                    "Tips.tar",
                ),
            )
        },
        "source_sizes": dict(
            zip(NFFA_CLASSES, (81_126_912, 197_943_296, 119_734_272, 866_739_200, 708_994_560))
        ),
        "converter": "open:condensed:nffa_sem",
        "transformation": (
            "selected the five condensed-matter classes whose complete tar shards jointly "
            "remain below 2 GB; decoded each validated SEM image and "
            "grayscale-letterboxed it to 256x256 while retaining source geometry and "
            "member name"
        ),
        "classes": NFFA_CLASSES,
        "splits": ("all",),
        "requires": ("PIL",),
        "modality": "scanning electron microscopy",
        "task": "five-class nanomaterials morphology classification",
        "basket_size": 2 * 1024 * 1024,
        "layout": "one TTree per publisher SEM category",
        "creators": (
            "Rossella Aversa",
            "Mohammad Hadi Modarres",
            "Stefano Cozzini",
            "Regina Ciancio",
        ),
        "publisher": "NFFA-EUROPE Project",
        "origin": "https://doi.org/10.23728/b2share.f1aa0f5ad38c456eaf7b04d47a65af53",
        "repository": "EUDAT B2SHARE",
        "citation": "https://doi.org/10.23728/b2share.f1aa0f5ad38c456eaf7b04d47a65af53",
    },
    {
        "name": "moke_skyrmion_segmentation",
        "label": "MOKE skyrmions",
        "title": "2,960 magnetic-domain images with skyrmion and defect masks",
        "licence": "CC BY 4.0",
        "source": "https://zenodo.org/records/10997175",
        "source_bytes": 957_154_435,
        "url": "https://zenodo.org/api/records/10997175/files/public_unet_skyrmion_dataset.zip/content",
        "converter": "open:condensed:moke_skyrmions",
        "transformation": (
            "paired publisher MOKE PNGs with RGB-coded masks; resized intensity images "
            "to 256x256 and masks with nearest-neighbour sampling; mapped exact and quantized "
            "colours to the nearest blue/red/green background/skyrmion/defect palette value and "
            "retained source and partition-group ids; omitted packaged trained models and "
            "statistics because they are not examples"
        ),
        "classes": SKYRMION_CLASSES,
        "splits": ("all",),
        "requires": ("PIL", "numpy"),
        "modality": "magneto-optical Kerr-effect microscopy",
        "task": "three-class magnetic-skyrmion semantic segmentation",
        "basket_size": 2 * 1024 * 1024,
        "layout": "one samples TTree with image/mask pairs and publisher partition eligibility",
        "creators": (
            "Thomas Brian Winkler",
            "Isaac Labrie-Boulay",
            "Alena Romanova",
            "Hans Fangohr",
            "Mathias Kläui",
            "Raphael Gruber",
            "Fabian Kammerbauer",
            "Klaus Raab",
            "Jaukub Zazvorka",
            "Kilian Leutner",
        ),
        "publisher": "Johannes Gutenberg University Mainz",
        "origin": "https://doi.org/10.5281/zenodo.10997175",
        "repository": "Zenodo",
        "citation": "https://doi.org/10.1103/PhysRevApplied.21.014014",
    },
    {
        "name": "wse2_stm_defects",
        "label": "WSe2 STM defects",
        "title": "2,280 atomic-resolution STM patches with three-class defect masks",
        "licence": "CC BY 4.0",
        "source": "https://zenodo.org/records/10443995",
        "source_bytes": 1_793_065_216,
        "sources": {
            "images": "https://zenodo.org/api/records/10443995/files/WSe2-Defect-Training-Images_2023-05-01.npy/content",
            "labels": "https://zenodo.org/api/records/10443995/files/WSe2-Defect-Training-Labels_2023-05-01.npy/content",
        },
        "source_sizes": {"images": 597_688_448, "labels": 1_195_376_768},
        "converter": "open:condensed:wse2_stm",
        "transformation": (
            "memory-mapped the published float32 256x256 STM patches and float64 masks; "
            "cast masks to uint8 after validating classes 0/1/2; reproduced the authors' "
            "contiguous 1,461/719/100 train/validation/test split"
        ),
        "classes": WSE2_CLASSES,
        "splits": ("train", "validation", "test"),
        "requires": ("numpy",),
        "modality": "atomic-resolution scanning tunnelling microscopy",
        "task": "three-class atomic-defect semantic segmentation",
        "basket_size": 2 * 1024 * 1024,
        "layout": (
            "one samples TTree per author-code split with paired floating-point image and "
            "integer mask"
        ),
        "creators": ("Darian Smalley",),
        "publisher": "Hone-Barmak Group",
        "origin": "https://doi.org/10.5281/zenodo.10443995",
        "repository": "Zenodo",
        "citation": "https://doi.org/10.48550/arXiv.2312.05160",
    },
    {
        "name": "tem_nanoparticle_morphology",
        "label": "TEM nanoparticle morphology",
        "title": "300 high-resolution TEM images in three assembly classes",
        "licence": "CC BY 4.0",
        "source": "https://zenodo.org/records/7025602",
        "source_bytes": 1_571_757_800,
        "url": "https://zenodo.org/api/records/7025602/files/2022-AutoDetect-mNP-morphology.zip/content",
        "converter": "open:condensed:nanowire_tem",
        "transformation": (
            "decoded the three balanced publisher folders of 4096x4096 TEM JPEGs; "
            "grayscale-letterboxed each image to 256x256 while retaining source geometry "
            "and member name"
        ),
        "classes": NANOWIRE_CLASSES,
        "splits": ("all",),
        "requires": ("PIL",),
        "modality": "transmission electron microscopy",
        "task": "three-class nanoparticle and nanowire assembly classification",
        "basket_size": 2 * 1024 * 1024,
        "layout": "one TTree for each balanced morphology folder",
        "creators": ("Shizhao Lu", "Brian Montz", "Todd Emrick", "Arthi Jayaraman"),
        "publisher": "University of Massachusetts Amherst",
        "origin": "https://doi.org/10.5281/zenodo.7025602",
        "repository": "Zenodo",
        "citation": "https://doi.org/10.1039/D2DD00066K",
    },
    {
        "name": "polymer_blend_afm",
        "label": "Polymer-blend AFM",
        "title": "153 raw 384x384 AFM scans with five physical channels",
        "licence": "CC BY 4.0",
        "source": "https://zenodo.org/records/11179874",
        "source_bytes": 483_825_041,
        "url": "https://zenodo.org/api/records/11179874/files/original%20files.zip/content",
        "converter": "open:condensed:polymer_afm",
        "transformation": (
            "parsed each Igor Binary Wave without normalizing it; retained all five "
            "384x384 float32 channels (height, amplitude, phase, Z sensor and modified "
            "phase) and the source filename"
        ),
        "classes": (),
        "splits": ("all",),
        "requires": ("igor2", "numpy"),
        "modality": "atomic-force microscopy",
        "task": "unsupervised polymer-domain representation learning and clustering",
        "basket_size": 4 * 1024 * 1024,
        "layout": "one samples TTree with five aligned physical-image branches",
        "creators": ("Aanish Paruchuri", "Arthi Jayaraman", "Xiaodan Gu", "Yunfei Wang"),
        "publisher": "University of Delaware",
        "origin": "https://doi.org/10.5281/zenodo.11179874",
        "repository": "Zenodo",
        "citation": "https://doi.org/10.48550/arXiv.2409.11438",
    },
    {
        "name": "perovskite_sem_segmentation",
        "label": "Perovskite SEM segmentation",
        "title": "86 SEM fields with 12,071 material and defect annotations",
        "licence": "CC BY 4.0",
        "source": "https://zenodo.org/records/21263625",
        "source_bytes": 77_066_383,
        "url": "https://zenodo.org/api/records/21263625/files/seg6/content",
        "converter": "open:condensed:perovskite_sem",
        "transformation": (
            "paired each SEM image with its LabelMe JSON; resized images to 256x256 and "
            "rasterized polygon/circle annotations into a categorical nearest-neighbour "
            "mask, merging spelling variants of PbI2 while retaining the image name and "
            "both image and annotation geometry; independently rescaled coordinates where "
            "the publisher's preview PNG is smaller than its annotation canvas"
        ),
        "classes": PEROVSKITE_CLASSES,
        "splits": ("all",),
        "requires": ("PIL",),
        "modality": "scanning electron microscopy",
        "task": "five-class perovskite phase and defect semantic segmentation",
        "basket_size": 2 * 1024 * 1024,
        "layout": "one samples TTree with paired RGB image and categorical mask",
        "creators": ("Yixi Wang",),
        "publisher": "Zenodo",
        "origin": "https://doi.org/10.5281/zenodo.21263625",
        "repository": "Zenodo",
        "citation": "https://doi.org/10.5281/zenodo.21263625",
    },
)

CONDENSED_MATTER: tuple[dict[str, Any], ...] = (
    *VISUAL_CONDENSED_MATTER,
    *(_matbench_declaration(task) for task in MATBENCH_TASKS),
)


def _fixed_bytes(value: str, width: int, description: str) -> array.array[int]:
    encoded = value.encode("utf-8")
    if len(encoded) > width:
        raise ValueError(f"{description} is longer than {width} bytes")
    return array.array("B", encoded + bytes(width - len(encoded)))


def _array_bytes(code: str, raw: bytes) -> array.array[Any]:
    result: array.array[Any] = array.array(code)
    result.frombytes(raw)
    if sys.byteorder == "big" and result.itemsize > 1:
        result.byteswap()
    return result


def _letterbox(source: IO[bytes], mode: str) -> tuple[array.array[int], tuple[int, ...]]:
    image_module = importlib.import_module("PIL.Image")
    with image_module.open(source) as opened:
        width, height = opened.size
        image = opened.convert(mode)
        image.thumbnail((IMAGE_SIDE, IMAGE_SIDE), image_module.Resampling.BILINEAR)
        resized_width, resized_height = image.size
        canvas = image_module.new(mode, (IMAGE_SIDE, IMAGE_SIDE))
        left = (IMAGE_SIDE - resized_width) // 2
        top = (IMAGE_SIDE - resized_height) // 2
        canvas.paste(image, (left, top))
        pixels = array.array("B", canvas.tobytes())
    return pixels, (width, height, resized_width, resized_height, left, top)


def _geometry_row(geometry: Sequence[int]) -> dict[str, int]:
    keys = (
        "source_width",
        "source_height",
        "resized_width",
        "resized_height",
        "left_padding",
        "top_padding",
    )
    return dict(zip(keys, geometry))


def _image_columns(channels: int = 1) -> dict[str, Any]:
    return {
        "image": ("B", IMAGE_SIDE * IMAGE_SIDE * channels),
        "source_width": "i",
        "source_height": "i",
        "resized_width": "i",
        "resized_height": "i",
        "left_padding": "i",
        "top_padding": "i",
        "index": "i",
    }


def _jarvis_labels(path: Path) -> dict[str, int]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("JARVIS-STM labels are not valid JSON") from error
    if not isinstance(document, dict):
        raise ValueError("JARVIS-STM labels are not a JSON object")
    labels = {str(name): int(value) for name, value in document.items()}
    if any(value not in range(len(JARVIS_CLASSES)) for value in labels.values()):
        raise ValueError("JARVIS-STM labels contain an unknown Bravais class")
    return labels


def _jarvis_pairs(archive: zipfile.ZipFile) -> list[tuple[str, str, str]]:
    grouped: dict[str, dict[str, str]] = {}
    for name in archive.namelist():
        match = re.search(r"/(JVASP-[0-9]+)_(pos|neg)\.jpg$", name)
        if match:
            grouped.setdefault(match.group(1), {})[match.group(2)] = name
    incomplete = sorted(jid for jid, pair in grouped.items() if set(pair) != {"pos", "neg"})
    if incomplete:
        raise ValueError(f"JARVIS-STM has unpaired bias images for {incomplete[0]}")
    return [(jid, grouped[jid]["pos"], grouped[jid]["neg"]) for jid in sorted(grouped)]


def _jarvis_entries(paths: Mapping[str, Path]) -> Rows:
    labels = _jarvis_labels(paths["labels"])
    with zipfile.ZipFile(paths["images"]) as archive:
        for index, (jid, positive, negative) in enumerate(_jarvis_pairs(archive)):
            if jid not in labels:
                raise ValueError(f"JARVIS-STM has no Bravais label for {jid}")
            with archive.open(positive) as held:
                pos_pixels, pos_geometry = _letterbox(held, "RGB")
            with archive.open(negative) as held:
                neg_pixels, neg_geometry = _letterbox(held, "RGB")
            label = labels[jid]
            yield (
                label,
                {
                    "positive_bias": pos_pixels,
                    "negative_bias": neg_pixels,
                    "label": label,
                    "jid": _fixed_bytes(jid, 24, f"JARVIS id {jid!r}"),
                    "positive_width": pos_geometry[0],
                    "positive_height": pos_geometry[1],
                    "negative_width": neg_geometry[0],
                    "negative_height": neg_geometry[1],
                    "index": index,
                },
            )


def _jarvis_stm(paths: Mapping[str, Path]) -> Loaded:
    columns: dict[str, Any] = {
        "positive_bias": ("B", IMAGE_SIDE * IMAGE_SIDE * 3),
        "negative_bias": ("B", IMAGE_SIDE * IMAGE_SIDE * 3),
        "label": "i",
        "jid": ("B", 24),
        "positive_width": "i",
        "positive_height": "i",
        "negative_width": "i",
        "negative_height": "i",
        "index": "i",
    }
    return JARVIS_CLASSES, columns, _jarvis_entries(paths)


def _tar_pictures(path: Path) -> Iterator[tuple[str, IO[bytes]]]:
    archive = tarfile.open(path, "r:*")
    try:
        for member in archive:
            if not member.isfile() or not member.name.lower().endswith(
                (".jpg", ".jpeg", ".png", ".tif", ".tiff")
            ):
                continue
            held = archive.extractfile(member)
            if held is not None:
                yield member.name, held
    finally:
        archive.close()


def _nffa_entries(paths: Mapping[str, Path]) -> Rows:
    index = 0
    for label, role in enumerate(NFFA_CLASSES):
        for member, held in _tar_pictures(paths[role]):
            with held:
                pixels, geometry = _letterbox(held, "L")
            yield (
                label,
                {
                    "image": pixels,
                    "label": label,
                    "member": _fixed_bytes(member, 256, f"NFFA member {member!r}"),
                    **_geometry_row(geometry),
                    "index": index,
                },
            )
            index += 1


def _nffa_sem(paths: Mapping[str, Path]) -> Loaded:
    columns = _image_columns()
    columns.update({"label": "i", "member": ("B", 256)})
    return NFFA_CLASSES, columns, _nffa_entries(paths)


def _nanowire_class(member: str) -> int | None:
    folder = member.replace("\\", "/").split("/")[-2].lower()
    lookup = {
        "dispersed nanoparticles": 0,
        "separate clusters": 1,
        "percolating cluster": 2,
    }
    return lookup.get(folder)


def _nanowire_entries(path: Path) -> Rows:
    with zipfile.ZipFile(path) as archive:
        pictures = [
            item
            for item in archive.infolist()
            if item.filename.lower().endswith((".jpg", ".jpeg", ".png"))
        ]
        for index, member in enumerate(pictures):
            label = _nanowire_class(member.filename)
            if label is None:
                raise ValueError(f"TEM image {member.filename!r} has an unknown morphology folder")
            with archive.open(member) as held:
                pixels, geometry = _letterbox(held, "L")
            yield (
                label,
                {
                    "image": pixels,
                    "label": label,
                    "member": _fixed_bytes(member.filename, 192, f"TEM member {member.filename!r}"),
                    **_geometry_row(geometry),
                    "index": index,
                },
            )


def _nanowire_tem(path: Path) -> Loaded:
    columns = _image_columns()
    columns.update({"label": "i", "member": ("B", 192)})
    return NANOWIRE_CLASSES, columns, _nanowire_entries(path)


def _moke_table(archive: zipfile.ZipFile) -> list[tuple[int, str]]:
    name = "public_unet_skyrmion_dataset/table.csv"
    try:
        text = archive.read(name).decode("utf-8-sig")
    except (KeyError, UnicodeDecodeError) as error:
        raise ValueError("MOKE archive has no valid table.csv") from error
    rows = csv.DictReader(io.StringIO(text), delimiter=";")
    result: list[tuple[int, str]] = []
    for row in rows:
        try:
            source_id = int(row["source_id"])
            stem = Path(row["img_fn"]).stem
        except (KeyError, TypeError, ValueError):
            raise ValueError("MOKE table.csv has an invalid row") from None
        result.append((source_id, stem))
    return result


def _moke_partitions(archive: zipfile.ZipFile) -> tuple[set[int], set[int]]:
    name = "public_unet_skyrmion_dataset/partition.txt"
    try:
        lines = archive.read(name).decode("ascii").splitlines()
    except (KeyError, UnicodeDecodeError) as error:
        raise ValueError("MOKE archive has no valid partition.txt") from error
    groups: dict[str, set[int]] = {"only_training": set(), "train_test_val": set()}
    for line in lines:
        parts = line.split(";")
        if parts[0] not in groups or not all(value.isdigit() for value in parts[1:]):
            raise ValueError("MOKE partition.txt has an invalid row")
        groups[parts[0]].update(map(int, parts[1:]))
    return groups["only_training"], groups["train_test_val"]


def _moke_mask(source: IO[bytes]) -> tuple[array.array[int], array.array[float]]:
    image_module = importlib.import_module("PIL.Image")
    numpy = importlib.import_module("numpy")
    with image_module.open(source) as opened:
        image = opened.convert("RGB").resize(
            (IMAGE_SIDE, IMAGE_SIDE), image_module.Resampling.NEAREST
        )
        rgb = numpy.asarray(image)
    mask = numpy.full((IMAGE_SIDE, IMAGE_SIDE), 255, dtype=numpy.uint8)
    colours = ((0, 0, 255), (255, 0, 0), (0, 255, 0))
    for label, colour in enumerate(colours):
        mask[numpy.all(rgb == colour, axis=2)] = label
    unknown = mask == 255
    if bool(numpy.any(unknown)):
        held = rgb[unknown].astype(numpy.int32)
        palette = numpy.asarray(colours, dtype=numpy.int32)
        distances = numpy.sum((held[:, None, :] - palette[None, :, :]) ** 2, axis=2)
        mask[unknown] = numpy.argmin(distances, axis=1).astype(numpy.uint8)
    pixels = mask.size
    fractions = array.array(
        "f", (int(numpy.count_nonzero(mask == label)) / pixels for label in range(3))
    )
    return _array_bytes("B", mask.tobytes()), fractions


def _moke_image(source: IO[bytes]) -> array.array[int]:
    image_module = importlib.import_module("PIL.Image")
    with image_module.open(source) as opened:
        image = opened.convert("L").resize(
            (IMAGE_SIDE, IMAGE_SIDE), image_module.Resampling.BILINEAR
        )
        return array.array("B", image.tobytes())


def _moke_partition_group(source_id: int, only_train: set[int], heldout: set[int]) -> int:
    if source_id in only_train:
        return 0
    if source_id in heldout:
        return 1
    raise ValueError(f"MOKE source id {source_id} is absent from partition.txt")


def _moke_entries(path: Path) -> Rows:
    prefix = "public_unet_skyrmion_dataset"
    with zipfile.ZipFile(path) as archive:
        only_train, heldout = _moke_partitions(archive)
        members = set(archive.namelist())
        for index, (source_id, stem) in enumerate(_moke_table(archive)):
            image_name = f"{prefix}/images/{stem}.png"
            label_name = f"{prefix}/labels/{stem}.png"
            if image_name not in members or label_name not in members:
                raise ValueError(f"MOKE pair {stem!r} is incomplete")
            with archive.open(image_name) as image_source, archive.open(label_name) as label_source:
                image = _moke_image(image_source)
                mask, fractions = _moke_mask(label_source)
            yield (
                0,
                {
                    "image": image,
                    "mask": mask,
                    "class_fractions": fractions,
                    "source_id": source_id,
                    "partition_group": _moke_partition_group(source_id, only_train, heldout),
                    "index": index,
                },
            )


def _moke_skyrmions(path: Path) -> Loaded:
    columns: dict[str, Any] = {
        "image": ("B", IMAGE_SIDE * IMAGE_SIDE),
        "mask": ("B", IMAGE_SIDE * IMAGE_SIDE),
        "class_fractions": ("f", len(SKYRMION_CLASSES)),
        "source_id": "i",
        "partition_group": "B",
        "index": "i",
    }
    return ("samples",), columns, _moke_entries(path)


def _wse2_bounds(split: str) -> tuple[int, int]:
    bounds = {"train": (0, 1461), "validation": (1461, 2180), "test": (2180, 2280)}
    try:
        return bounds[split]
    except KeyError:
        raise ValueError(f"WSe2 defects has no split {split!r}") from None


def _check_wse2(images: Any, labels: Any) -> None:
    expected = (2280, IMAGE_SIDE, IMAGE_SIDE)
    if tuple(images.shape) != expected or images.dtype.name != "float32":
        raise ValueError("WSe2 images are not 2,280 float32 arrays shaped 256x256")
    if tuple(labels.shape) != expected or labels.dtype.name != "float64":
        raise ValueError("WSe2 labels are not 2,280 float64 arrays shaped 256x256")


def _wse2_row(image: Any, mask_source: Any, index: int) -> dict[str, Any]:
    numpy = importlib.import_module("numpy")
    mask = numpy.asarray(mask_source, dtype=numpy.uint8)
    if bool(numpy.any(mask_source != mask)) or bool(numpy.any(mask > 2)):
        raise ValueError(f"WSe2 mask {index} contains a value outside 0, 1 and 2")
    pixels = mask.size
    return {
        "image": _array_bytes("f", numpy.asarray(image, dtype="<f4").tobytes()),
        "mask": _array_bytes("B", mask.tobytes()),
        "class_fractions": array.array(
            "f", (int(numpy.count_nonzero(mask == label)) / pixels for label in range(3))
        ),
        "index": index,
    }


def _wse2_entries(paths: Mapping[str, Path], split: str) -> Rows:
    numpy = importlib.import_module("numpy")
    images = numpy.load(paths["images"], mmap_mode="r", allow_pickle=False)
    labels = numpy.load(paths["labels"], mmap_mode="r", allow_pickle=False)
    _check_wse2(images, labels)
    start, end = _wse2_bounds(split)
    for index in range(start, end):
        yield 0, _wse2_row(images[index], labels[index], index)


def _wse2_stm(paths: Mapping[str, Path], split: str) -> Loaded:
    columns: dict[str, Any] = {
        "image": ("f", IMAGE_SIDE * IMAGE_SIDE),
        "mask": ("B", IMAGE_SIDE * IMAGE_SIDE),
        "class_fractions": ("f", len(WSE2_CLASSES)),
        "index": "i",
    }
    return ("samples",), columns, _wse2_entries(paths, split)


def _afm_wave(source: IO[bytes], member: str) -> Mapping[str, Any]:
    binarywave = importlib.import_module("igor2.binarywave")
    try:
        document = binarywave.load(source)
        wave = document["wave"]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"AFM member {member!r} is not a valid Igor Binary Wave") from error
    if not isinstance(wave, Mapping):
        raise ValueError(f"AFM member {member!r} has no wave mapping")
    return wave


def _afm_channels(wave: Mapping[str, Any], member: str) -> dict[str, array.array[Any]]:
    numpy = importlib.import_module("numpy")
    data = wave.get("wData")
    if data is None or tuple(data.shape) != (384, 384, len(AFM_CHANNELS)):
        raise ValueError(f"AFM member {member!r} does not contain five 384x384 channels")
    if data.dtype.name != "float32":
        raise ValueError(f"AFM member {member!r} channels are not float32")
    return {
        name: _array_bytes("f", numpy.asarray(data[:, :, at], dtype="<f4").tobytes())
        for at, name in enumerate(AFM_CHANNELS)
    }


def _afm_entries(path: Path) -> Rows:
    with zipfile.ZipFile(path) as archive:
        members = sorted(
            (item for item in archive.infolist() if item.filename.lower().endswith(".ibw")),
            key=lambda item: item.filename,
        )
        for index, member in enumerate(members):
            with archive.open(member) as held:
                channels = _afm_channels(_afm_wave(held, member.filename), member.filename)
            yield (
                0,
                {
                    **channels,
                    "member": _fixed_bytes(member.filename, 64, f"AFM member {member.filename!r}"),
                    "index": index,
                },
            )


def _polymer_afm(path: Path) -> Loaded:
    columns: dict[str, Any] = dict.fromkeys(AFM_CHANNELS, ("f", 384 * 384))
    columns.update({"member": ("B", 64), "index": "i"})
    return ("samples",), columns, _afm_entries(path)


def _perovskite_label(value: str) -> int:
    folded = value.casefold().replace("₃", "3").replace("₂", "2")
    labels = {"abo3": 1, "abx3": 2, "pbi2": 3, "defect": 4}
    try:
        return labels[folded]
    except KeyError:
        raise ValueError(f"perovskite annotation has unknown label {value!r}") from None


def _scaled_points(shape: Mapping[str, Any], width: int, height: int) -> list[tuple[float, float]]:
    points = shape.get("points")
    if not isinstance(points, list) or not all(
        isinstance(point, list) and len(point) == 2 for point in points
    ):
        raise ValueError("perovskite annotation has invalid points")
    return [
        (float(point[0]) * IMAGE_SIDE / width, float(point[1]) * IMAGE_SIDE / height)
        for point in points
    ]


def _draw_perovskite_shape(drawing: Any, shape: Mapping[str, Any], width: int, height: int) -> None:
    label = _perovskite_label(str(shape.get("label", "")))
    points = _scaled_points(shape, width, height)
    kind = shape.get("shape_type")
    if kind == "polygon" and len(points) >= 3:
        drawing.polygon(points, fill=label)
        return
    if kind == "circle" and len(points) == 2:
        centre, edge = points
        radius = math.dist(centre, edge)
        box = (centre[0] - radius, centre[1] - radius, centre[0] + radius, centre[1] + radius)
        drawing.ellipse(box, fill=label)
        return
    raise ValueError(f"perovskite annotation has unsupported {kind!r} geometry")


def _perovskite_mask(
    document: Mapping[str, Any], width: int, height: int
) -> tuple[array.array[int], array.array[float], int]:
    image_module = importlib.import_module("PIL.Image")
    draw_module = importlib.import_module("PIL.ImageDraw")
    shapes = document.get("shapes")
    if not isinstance(shapes, list):
        raise ValueError("perovskite annotation has no shapes list")
    mask = image_module.new("L", (IMAGE_SIDE, IMAGE_SIDE))
    drawing = draw_module.Draw(mask)
    for shape in shapes:
        if not isinstance(shape, dict):
            raise ValueError("perovskite annotation has a non-object shape")
        _draw_perovskite_shape(drawing, shape, width, height)
    raw = mask.tobytes()
    fractions = array.array(
        "f", (raw.count(label) / len(raw) for label in range(len(PEROVSKITE_CLASSES)))
    )
    return array.array("B", raw), fractions, len(shapes)


def _perovskite_document(archive: zipfile.ZipFile, member: str) -> dict[str, Any]:
    try:
        document = json.loads(archive.read(member))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"perovskite annotation {member!r} is not valid JSON") from error
    if not isinstance(document, dict):
        raise ValueError(f"perovskite annotation {member!r} is not a JSON object")
    return document


def _perovskite_image(archive: zipfile.ZipFile, member: str) -> tuple[array.array[int], int, int]:
    image_module = importlib.import_module("PIL.Image")
    with archive.open(member) as held, image_module.open(held) as opened:
        width, height = opened.size
        image = opened.convert("RGB").resize(
            (IMAGE_SIDE, IMAGE_SIDE), image_module.Resampling.BILINEAR
        )
        return array.array("B", image.tobytes()), width, height


def _annotation_geometry(document: Mapping[str, Any], member: str) -> tuple[int, int]:
    try:
        width = int(document["imageWidth"])
        height = int(document["imageHeight"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"perovskite annotation {member!r} has no geometry") from None
    if width <= 0 or height <= 0:
        raise ValueError(f"perovskite annotation {member!r} has invalid geometry")
    return width, height


def _perovskite_images(archive: zipfile.ZipFile) -> dict[str, str]:
    extensions = (".jpg", ".jpeg", ".png", ".tif", ".tiff")
    return {
        Path(name).stem: name for name in archive.namelist() if name.lower().endswith(extensions)
    }


def _perovskite_entries(path: Path) -> Rows:
    with zipfile.ZipFile(path) as archive:
        images = _perovskite_images(archive)
        annotations = sorted(name for name in archive.namelist() if name.lower().endswith(".json"))
        for index, annotation in enumerate(annotations):
            document = _perovskite_document(archive, annotation)
            stem = Path(annotation).stem
            if stem not in images:
                raise ValueError(f"perovskite annotation {annotation!r} has no image")
            pixels, width, height = _perovskite_image(archive, images[stem])
            annotation_width, annotation_height = _annotation_geometry(document, annotation)
            mask, fractions, objects = _perovskite_mask(
                document, annotation_width, annotation_height
            )
            yield (
                0,
                {
                    "image": pixels,
                    "mask": mask,
                    "class_fractions": fractions,
                    "objects": objects,
                    "source_width": width,
                    "source_height": height,
                    "annotation_width": annotation_width,
                    "annotation_height": annotation_height,
                    "member": _fixed_bytes(images[stem], 96, f"perovskite member {images[stem]!r}"),
                    "index": index,
                },
            )


def _perovskite_sem(path: Path) -> Loaded:
    columns: dict[str, Any] = {
        "image": ("B", IMAGE_SIDE * IMAGE_SIDE * 3),
        "mask": ("B", IMAGE_SIDE * IMAGE_SIDE),
        "class_fractions": ("f", len(PEROVSKITE_CLASSES)),
        "objects": "i",
        "source_width": "i",
        "source_height": "i",
        "annotation_width": "i",
        "annotation_height": "i",
        "member": ("B", 96),
        "index": "i",
    }
    return ("samples",), columns, _perovskite_entries(path)


FORMULA_TOKEN = re.compile(r"[A-Z][a-z]?|\(|\)|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")


def _formula_tokens(formula: str) -> list[str]:
    tokens = FORMULA_TOKEN.findall(formula)
    if "".join(tokens) != formula or not tokens:
        raise ValueError(f"cannot parse chemical formula {formula!r}")
    return tokens


def _number_after(tokens: Sequence[str], index: int) -> tuple[float, int]:
    if index < len(tokens) and (tokens[index][0].isdigit() or tokens[index][0] == "."):
        return float(tokens[index]), index + 1
    return 1.0, index


def _add_amount(amounts: dict[str, float], symbol: str, amount: float) -> None:
    if symbol not in ATOMIC_NUMBER:
        raise ValueError(f"chemical formula contains unknown element {symbol!r}")
    amounts[symbol] = amounts.get(symbol, 0.0) + amount


def _close_formula_group(stack: list[dict[str, float]], multiplier: float) -> None:
    if len(stack) == 1:
        raise ValueError("chemical formula has an unmatched closing parenthesis")
    group = stack.pop()
    for symbol, amount in group.items():
        _add_amount(stack[-1], symbol, amount * multiplier)


def _formula_amounts(formula: str) -> dict[str, float]:
    tokens = _formula_tokens(formula)
    stack: list[dict[str, float]] = [{}]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token == "(":
            stack.append({})
        elif token == ")":
            multiplier, index = _number_after(tokens, index)
            _close_formula_group(stack, multiplier)
        elif token[0].isalpha():
            multiplier, index = _number_after(tokens, index)
            _add_amount(stack[-1], token, multiplier)
        else:
            raise ValueError(f"chemical formula {formula!r} has a misplaced number")
    if len(stack) != 1:
        raise ValueError(f"chemical formula {formula!r} has an unmatched opening parenthesis")
    return stack[0]


def _element_image(amounts: Mapping[str, float]) -> array.array[float]:
    total = sum(amounts.values())
    if total <= 0:
        raise ValueError("chemical composition has no positive occupancy")
    values = [0.0] * 128
    for symbol, amount in amounts.items():
        values[ATOMIC_NUMBER[symbol] - 1] = float(amount) / total
    return array.array("f", values)


def _stream_to_data(source: Any) -> str:
    held = ""
    marker = '"data": ['
    while marker not in held:
        chunk = source.read(1024 * 1024)
        if not chunk or len(held) > 16 * 1024 * 1024:
            raise ValueError("Matbench JSON has no top-level data array")
        held += chunk
    return held.split(marker, 1)[1]


def _next_json_row(source: Any, buffer: str, decoder: json.JSONDecoder) -> tuple[Any, str]:
    while True:
        buffer = buffer.lstrip(" \t\r\n,")
        if buffer.startswith("]"):
            return None, buffer
        try:
            value, end = decoder.raw_decode(buffer)
        except json.JSONDecodeError:
            chunk = source.read(1024 * 1024)
            if not chunk:
                raise ValueError("Matbench data array ends inside a JSON row") from None
            buffer += chunk
            continue
        return value, buffer[end:]


def _matbench_rows(path: Path) -> Iterator[list[Any]]:
    decoder = json.JSONDecoder()
    try:
        source = gzip.open(path, "rt", encoding="utf-8")
    except OSError as error:
        raise ValueError("Matbench source is not gzip JSON") from error
    with source:
        buffer = _stream_to_data(source)
        while True:
            value, buffer = _next_json_row(source, buffer, decoder)
            if value is None:
                return
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError("Matbench data array contains a row other than two values")
            yield value


def _site_position(site: Mapping[str, Any]) -> tuple[float, float, float]:
    abc = site.get("abc")
    if not isinstance(abc, list) or len(abc) != 3:
        raise ValueError("Matbench structure site has no three fractional coordinates")
    try:
        return float(abc[0]), float(abc[1]), float(abc[2])
    except (TypeError, ValueError):
        raise ValueError("Matbench structure site has non-numeric coordinates") from None


def _site_species(site: Mapping[str, Any]) -> list[tuple[int, float]]:
    species = site.get("species")
    if not isinstance(species, list) or not species:
        raise ValueError("Matbench structure site has no species")
    result: list[tuple[int, float]] = []
    for component in species:
        try:
            symbol = str(component["element"])
            occupancy = float(component["occu"])
            number = ATOMIC_NUMBER[symbol]
        except (KeyError, TypeError, ValueError):
            raise ValueError("Matbench structure site has an invalid species") from None
        result.append((number, occupancy))
    return result


def _lattice_matrix(structure: Mapping[str, Any]) -> list[list[Any]]:
    lattice = structure.get("lattice")
    if not isinstance(lattice, dict):
        raise ValueError("Matbench structure has no lattice")
    matrix = lattice.get("matrix")
    if not isinstance(matrix, list) or len(matrix) != 3:
        raise ValueError("Matbench structure has no 3x3 lattice matrix")
    for row in matrix:
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("Matbench structure has an invalid lattice matrix")
    return matrix


def _lattice_values(structure: Mapping[str, Any]) -> array.array[float]:
    matrix = _lattice_matrix(structure)
    try:
        return array.array("d", (float(value) for row in matrix for value in row))
    except (TypeError, ValueError):
        raise ValueError("Matbench lattice matrix contains a non-numeric value") from None


def _structure_components(
    structure: Mapping[str, Any],
) -> tuple[int, list[tuple[int, int, float, tuple[float, float, float]]]]:
    sites = structure.get("sites")
    if not isinstance(sites, list) or not sites:
        raise ValueError("Matbench structure has no sites")
    components: list[tuple[int, int, float, tuple[float, float, float]]] = []
    for site_index, site in enumerate(sites):
        if not isinstance(site, dict):
            raise ValueError("Matbench structure has a non-object site")
        position = _site_position(site)
        components.extend(
            (site_index, number, occupancy, position) for number, occupancy in _site_species(site)
        )
    return len(sites), components


def _component_amounts(
    components: Sequence[tuple[int, int, float, tuple[float, float, float]]],
) -> dict[str, float]:
    amounts: dict[str, float] = {}
    for _site, number, occupancy, _position in components:
        symbol = ELEMENTS[number - 1]
        amounts[symbol] = amounts.get(symbol, 0.0) + occupancy
    return amounts


def _derived_formula(amounts: Mapping[str, float]) -> str:
    ordered = sorted(amounts, key=ATOMIC_NUMBER.__getitem__)
    return "".join(f"{symbol}{amounts[symbol]:g}" for symbol in ordered)


def _projection(
    components: Sequence[tuple[int, int, float, tuple[float, float, float]]],
) -> array.array[int]:
    values = array.array("B", [0]) * (3 * CRYSTAL_SIDE * CRYSTAL_SIDE)
    for _site, number, _occupancy, position in components:
        bins = tuple(min(CRYSTAL_SIDE - 1, int((value % 1.0) * CRYSTAL_SIDE)) for value in position)
        pairs = ((bins[0], bins[1]), (bins[0], bins[2]), (bins[1], bins[2]))
        for plane, (first, second) in enumerate(pairs):
            pixel = plane * CRYSTAL_SIDE**2 + second * CRYSTAL_SIDE + first
            values[pixel] = max(values[pixel], number)
    return values


def _component_arrays(
    components: Sequence[tuple[int, int, float, tuple[float, float, float]]],
) -> dict[str, Any]:
    if len(components) > MAX_CRYSTAL_COMPONENTS:
        raise ValueError(
            f"Matbench structure has more than {MAX_CRYSTAL_COMPONENTS} site-species components"
        )
    site_indices = array.array("H", [0]) * MAX_CRYSTAL_COMPONENTS
    numbers = array.array("B", [0]) * MAX_CRYSTAL_COMPONENTS
    occupancies = array.array("f", [0.0]) * MAX_CRYSTAL_COMPONENTS
    positions = array.array("f", [0.0]) * (MAX_CRYSTAL_COMPONENTS * 3)
    for at, (site, number, occupancy, position) in enumerate(components):
        site_indices[at], numbers[at], occupancies[at] = site, number, occupancy
        positions[at * 3 : at * 3 + 3] = array.array("f", position)
    return {
        "site_indices": site_indices,
        "atomic_numbers": numbers,
        "occupancies": occupancies,
        "fractional_positions": positions,
    }


def _structure_row(structure: Mapping[str, Any]) -> dict[str, Any]:
    sites, components = _structure_components(structure)
    amounts = _component_amounts(components)
    formula = _derived_formula(amounts)
    return {
        "composition": _fixed_bytes(formula, FORMULA_BYTES, f"derived formula {formula!r}"),
        "element_image": _element_image(amounts),
        "lattice": _lattice_values(structure),
        "projection": _projection(components),
        "sites": sites,
        "components": len(components),
        **_component_arrays(components),
    }


def _matbench_target(target: Any, classification: bool, index: int) -> tuple[int, dict[str, Any]]:
    if classification:
        label = int(target)
        if label not in (0, 1) or target not in (False, True, 0, 1):
            raise ValueError(f"Matbench row {index} has a non-binary target")
        return label, {"label": label}
    if not isinstance(target, (int, float)):
        raise ValueError(f"Matbench row {index} has a non-numeric regression target")
    return 0, {"target": float(target)}


def _matbench_entries(path: Path, task: Mapping[str, Any]) -> Rows:
    classification = bool(task.get("classes"))
    for index, (source, target) in enumerate(_matbench_rows(path)):
        tree, target_row = _matbench_target(target, classification, index)
        if task["input"] == "structure":
            if not isinstance(source, dict):
                raise ValueError(f"Matbench row {index} does not contain a structure")
            input_row = _structure_row(source)
        else:
            if not isinstance(source, str):
                raise ValueError(f"Matbench row {index} does not contain a composition")
            input_row = {
                "composition": _fixed_bytes(source, FORMULA_BYTES, f"formula {source!r}"),
                "element_image": _element_image(_formula_amounts(source)),
            }
        yield tree, {**input_row, **target_row, "index": index}


def _matbench_columns(structural: bool, classification: bool) -> dict[str, Any]:
    columns: dict[str, Any] = {
        "composition": ("B", FORMULA_BYTES),
        "element_image": ("f", 128),
        "index": "q",
    }
    columns["label" if classification else "target"] = "i" if classification else "d"
    if structural:
        columns.update(
            {
                "lattice": ("d", 9),
                "projection": ("B", 3 * CRYSTAL_SIDE * CRYSTAL_SIDE),
                "sites": "i",
                "components": "i",
                "site_indices": ("H", MAX_CRYSTAL_COMPONENTS),
                "atomic_numbers": ("B", MAX_CRYSTAL_COMPONENTS),
                "occupancies": ("f", MAX_CRYSTAL_COMPONENTS),
                "fractional_positions": ("f", MAX_CRYSTAL_COMPONENTS * 3),
            }
        )
    return columns


MATBENCH_BY_NAME = {str(task["name"]): task for task in MATBENCH_TASKS}


def _matbench(path: Path, name: str) -> Loaded:
    try:
        task = MATBENCH_BY_NAME[name]
    except KeyError:
        raise ValueError(f"there is no Matbench task {name!r}") from None
    classes = tuple(task.get("classes", ()))
    trees = classes or ("samples",)
    columns = _matbench_columns(task["input"] == "structure", bool(classes))
    return trees, columns, _matbench_entries(path, task)


CondensedLoader = Callable[[Mapping[str, Path], str], Loaded]
CONDENSED_LOADERS: dict[str, CondensedLoader] = {
    "jarvis_stm": lambda paths, _split: _jarvis_stm(paths),
    "nffa_sem": lambda paths, _split: _nffa_sem(paths),
    "wse2_stm": _wse2_stm,
    "moke_skyrmions": lambda paths, _split: _moke_skyrmions(paths["archive"]),
    "nanowire_tem": lambda paths, _split: _nanowire_tem(paths["archive"]),
    "polymer_afm": lambda paths, _split: _polymer_afm(paths["archive"]),
    "perovskite_sem": lambda paths, _split: _perovskite_sem(paths["archive"]),
}


def load(converter: str, paths: Mapping[str, Path], split: str) -> Loaded:
    """Open one condensed-matter source with bounded working memory."""
    if converter.startswith("matbench:"):
        return _matbench(paths["archive"], converter.removeprefix("matbench:"))
    try:
        loader = CONDENSED_LOADERS[converter]
    except KeyError:
        raise ValueError(f"there is no condensed-matter converter {converter!r}") from None
    return loader(paths, split)
