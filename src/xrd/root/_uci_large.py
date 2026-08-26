"""Bounded-memory readers for large UCI teaching datasets.

The normal dataset adapters intentionally operate on bytes: that keeps their
small archive formats simple and very thoroughly checkable.  These sources
range from 0.1--37 GB and often contain another compressed archive,
so this module instead opens a cached path and consumes one member, signal
record, image, or event at a time.

Nothing here executes code from a dataset.  In particular PPG-DaLiA's NumPy
pickle is read by a restricted unpickler which permits only NumPy's inert
array constructors and built-in containers.
"""

from __future__ import annotations

import array
import csv
import gzip
import importlib
import io
import os
import pickle
import re
import shutil
import struct
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from math import nan
from pathlib import Path
from typing import Any, BinaryIO, ClassVar

Rows = Iterator[tuple[int, dict[str, Any]]]
Loaded = tuple[tuple[str, ...], dict[str, Any], Rows]

GASES = (
    "acetaldehyde",
    "acetone",
    "ammonia",
    "benzene",
    "butanol",
    "carbon_monoxide",
    "ethylene",
    "methane",
    "methanol",
    "toluene",
)
_GAS_NAME = re.compile(
    r"(\d{12})_board_setPoint_(\d+)V_fan_setPoint_(\d+)_"
    r"mfc_setPoint_([A-Za-z]+)_(\d+)ppm_p(\d+)$"
)
ACTIVITIES = ("unlabelled", *(f"activity_{at}" for at in range(1, 34)))
CHIP_LABELS = ("unlabelled", "no_peaks", "peaks", "peak_start", "peak_end")
CHIP_CODES = {"noPeaks": 1, "peaks": 2, "peakStart": 3, "peakEnd": 4}
DICOM_LABELS = (
    "unlabelled",
    "true_benign",
    "true_malicious",
    "false_benign",
    "false_malicious",
)
DICOM_CODES = {"TB": 1, "TM": 2, "FB": 3, "FM": 4}
DICOM_SPLITS = tuple(
    f"experiment_{experiment}_patients_{bucket}"
    for experiment in (1, 2)
    for bucket in range(4)
)
HUMANITARIAN = (
    "fires",
    "floods",
    "natural_landscape",
    "infrastructural_damage",
    "human_damage",
    "non_damage",
)
HUMANITARIAN_CODES = {
    "fires": 0,
    "flood": 1,
    "damaged_nature": 2,
    "damaged_infrastructure": 3,
    "human_damage": 4,
    "non_damage": 5,
}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DAILY_ACTIVITIES = tuple(f"activity_{at:02d}" for at in range(1, 20))
PAMAP_ACTIVITIES = tuple(
    f"activity_{at}" for at in (0, 1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12, 13, 16, 17, 18, 19, 20, 24)
)
HHAR_ACTIVITIES = ("unlabelled", "bike", "sit", "stand", "walk", "stairs_up", "stairs_down")
REALDISP_SPLITS = tuple(f"subject_{subject:02d}" for subject in range(1, 18))


UCI_LARGE: tuple[dict[str, Any], ...] = (
    {
        "name": "gas_sensor_arrays_open_sampling",
        "label": "Gas Sensor Arrays in Open Sampling Settings",
        "title": "18,000 wind-tunnel recordings from 72 gas sensors, ten chemicals",
        "source": "https://archive.ics.uci.edu/dataset/251/gas+sensor+arrays+in+open+sampling+settings",
        "url": "https://archive.ics.uci.edu/static/public/251/gas+sensor+arrays+in+open+sampling+settings.zip",
        "source_bytes": 8368611437,
        "converter": "gas",
        "transformation": (
            "parsed every wind-tunnel recording without sensor scaling; retained controls, "
            "temperature, humidity and 72 sensor series, padding each to 26,000 samples while "
            "recording its true length; removed publisher U+200E direction marks at numeric field "
            "boundaries, ignored execution-control metadata and wrote one ROOT file and TTree per "
            "chemical"
        ),
        "classes": GASES,
        "splits": GASES,
        "split_files": True,
    },
    {
        "name": "susy",
        "label": "SUSY",
        "title": "5,000,000 simulated collisions, supersymmetric signal or background",
        "source": "https://archive.ics.uci.edu/dataset/279/susy",
        "url": "https://archive.ics.uci.edu/static/public/279/susy.zip",
        "source_bytes": 922377831,
        "converter": "susy",
        "transformation": (
            "streamed the published gzip CSV, converted its 18 feature values to float32 "
            "without scaling and preserved the publisher's final-500,000-event test split; "
            "wrote separate signal and background ROOT TTrees"
        ),
        "classes": ("background", "signal"),
        "splits": ("train", "test"),
        "modality": "particle physics",
        "task": "binary classification",
    },
    {
        "name": "higgs",
        "label": "HIGGS",
        "title": "11,000,000 simulated collisions, Higgs signal or background",
        "source": "https://archive.ics.uci.edu/dataset/280/higgs",
        "url": "https://archive.ics.uci.edu/static/public/280/higgs.zip",
        "source_bytes": 2816865137,
        "converter": "higgs",
        "transformation": (
            "streamed the published gzip CSV, converted its 28 feature values to float32 "
            "without scaling and preserved the publisher's final-500,000-event test split; "
            "wrote separate signal and background ROOT TTrees"
        ),
        "classes": ("background", "signal"),
        "splits": ("train", "test"),
    },
    {
        "name": "realdisp",
        "label": "REALDISP Activity Recognition",
        "title": "wearable motion readings from 17 people performing 33 activities",
        "source": "https://archive.ics.uci.edu/dataset/305/realdisp+activity+recognition+dataset",
        "url": "https://archive.ics.uci.edu/static/public/305/realdisp+activity+recognition+dataset.zip",
        "source_bytes": 2671716334,
        "converter": "realdisp",
        "transformation": (
            "parsed each wearable timestamp without resampling or feature scaling; grouped "
            "117 float32 sensor readings with subject, placement and activity metadata; wrote "
            "one ROOT file per subject with one TTree per activity"
        ),
        "classes": ACTIVITIES,
        "splits": REALDISP_SPLITS,
        "split_files": True,
    },
    {
        "name": "cuffless_blood_pressure",
        "label": "Cuff-Less Blood Pressure Estimation",
        "title": "12,000 aligned PPG, arterial-pressure and ECG signal records",
        "source": "https://archive.ics.uci.edu/dataset/340/cuff+less+blood+pressure+estimation",
        "url": "https://archive.ics.uci.edu/static/public/340/cuff+less+blood+pressure+estimation.zip",
        "source_bytes": 3363163675,
        "converter": "cuffless",
        "transformation": (
            "read the MATLAB/HDF5 PPG, arterial-pressure and ECG records and divided them into "
            "consecutive 1,000-sample float32 windows, padding only the final short window and "
            "retaining its true length; arterial pressure is the prediction target"
        ),
        "classes": (),
        "requires": ("h5py", "numpy"),
    },
    {
        "name": "hepmass",
        "label": "HEPMASS",
        "title": "three mass hypotheses, each with 10,500,000 simulated collisions",
        "source": "https://archive.ics.uci.edu/dataset/347/hepmass",
        "url": "https://archive.ics.uci.edu/static/public/347/hepmass.zip",
        "source_bytes": 7893269133,
        "converter": "hepmass",
        "transformation": (
            "streamed all six published gzip CSV files, converted features to float32 without "
            "scaling and preserved train/test files for the 1000, not-1000 and all-mass "
            "hypotheses; wrote one ROOT file per publisher split with signal and background "
            "TTrees"
        ),
        "classes": ("background", "signal"),
        "splits": (
            "train_1000",
            "test_1000",
            "train_all",
            "test_all",
            "train_not1000",
            "test_not1000",
        ),
        "split_files": True,
    },
    {
        "name": "chipseq",
        "label": "ChIP-seq Peak Detection",
        "title": "4,960 genomic coverage problems with structured peak labels",
        "source": "https://archive.ics.uci.edu/dataset/439/chipseq",
        "url": "https://archive.ics.uci.edu/static/public/439/chipseq.zip",
        "source_bytes": 37275964253,
        "converter": "chipseq",
        "transformation": (
            "streamed run-length encoded bedGraph coverage and split runs only where published "
            "weak-label boundaries cross them; retained genomic coordinates and counts without "
            "normalization; wrote one ROOT TTree per weak-label class"
        ),
        "classes": CHIP_LABELS,
    },
    {
        "name": "multimodal_damage",
        "label": "Multimodal Damage Identification",
        "title": "5,879 captioned disaster images in six damage classes",
        "source": "https://archive.ics.uci.edu/dataset/456/multimodal+damage+identification+for+humanitarian+computing",
        "url": "https://archive.ics.uci.edu/static/public/456/multimodal+damage+identification+for+humanitarian+computing.zip",
        "source_bytes": 1132451409,
        "converter": "humanitarian",
        "transformation": (
            "decoded each JPEG to RGB unsigned-byte pixels, proportionally letterboxed variable "
            "source geometry to 640x640 while retaining the original and resized geometry, paired "
            "it with the supplied caption and retained the damage class; repaired publisher JPEGs "
            "missing their terminal end marker and recorded that repair; wrote one ROOT TTree per "
            "class"
        ),
        "classes": HUMANITARIAN,
        "requires": ("Pillow",),
        "modality": "image and text",
        "task": "damage classification",
    },
    {
        "name": "ppg_dalia",
        "label": "PPG-DaLiA",
        "title": "daily-life wrist signals from 15 people with heart-rate targets",
        "source": "https://archive.ics.uci.edu/dataset/495/ppg+dalia",
        "url": "https://archive.ics.uci.edu/static/public/495/ppg+dalia.zip",
        "source_bytes": 2865111320,
        "converter": "ppg",
        "transformation": (
            "read the synchronized wrist signals with a restricted NumPy unpickler and formed "
            "published-style eight-second windows every two seconds; retained BVP, acceleration, "
            "EDA, temperature, activity, subject and heart-rate target without scaling"
        ),
        "classes": (),
        "requires": ("numpy",),
    },
    {
        "name": "medical_deepfakes",
        "label": "Medical Image Tamper Detection",
        "title": "22,753 CT slices from real and tampered lung scans",
        "source": "https://archive.ics.uci.edu/dataset/520/deepfakes+medical+image+tamper+detection",
        "url": "https://archive.ics.uci.edu/static/public/520/deepfakes+medical+image+tamper+detection.zip",
        "source_bytes": 6398919086,
        "converter": "dicom",
        "transformation": (
            "decoded each DICOM slice to its original 512x512 signed 16-bit pixels, retained "
            "slope/intercept calibration and attached only the published slice-level tamper "
            "class and x/y location; no image normalization or resizing was applied; partitioned "
            "the output into eight deterministic, patient-disjoint experiment shards to keep each "
            "ROOT file within the interoperable writer layout"
        ),
        "classes": DICOM_LABELS,
        "splits": DICOM_SPLITS,
        "split_files": True,
    },
    {
        "name": "pems_sf",
        "label": "PEMS-SF",
        "title": "440 days of occupancy from 963 San Francisco freeway sensors",
        "source": "https://archive.ics.uci.edu/dataset/204/pems+sf",
        "url": "https://archive.ics.uci.edu/static/public/204/pems+sf.zip",
        "source_bytes": 109480788,
        "converter": "pems",
        "transformation": (
            "parsed each published 963 by 144 daily occupancy matrix without scaling, "
            "flattened it in source order, preserved the official train/test split and wrote "
            "one ROOT TTree per weekday"
        ),
        "classes": WEEKDAYS,
        "splits": ("train", "test"),
        "modality": "traffic time series",
        "task": "weekday classification",
    },
    {
        "name": "physical_unclonable_functions",
        "label": "Physical Unclonable Functions",
        "title": "8.4 million challenge-response records from four XOR arbiter PUFs",
        "source": "https://archive.ics.uci.edu/dataset/463/physical+unclonable+functions",
        "url": "https://archive.ics.uci.edu/static/public/463/physical+unclonable+functions.zip",
        "source_bytes": 159800482,
        "converter": "puf",
        "transformation": (
            "streamed each published challenge-response CSV without scaling, retained its "
            "64- or 128-bit challenge and mapped the -1/+1 response to two ROOT TTrees while "
            "preserving the four official train/test partitions"
        ),
        "classes": ("negative", "positive"),
        "splits": ("train_5xor", "test_5xor", "train_6xor", "test_6xor"),
        "modality": "binary challenge vectors",
        "task": "binary classification",
    },
    {
        "name": "daily_sports_activities",
        "label": "Daily and Sports Activities",
        "title": "9,120 five-sensor motion segments covering 19 activities",
        "source": "https://archive.ics.uci.edu/dataset/256/daily+and+sports+activities",
        "url": "https://archive.ics.uci.edu/static/public/256/daily+and+sports+activities.zip",
        "source_bytes": 170800010,
        "converter": "daily",
        "transformation": (
            "streamed each 125 by 45 published motion segment, flattened it in temporal order "
            "without scaling and retained activity, subject and segment identifiers; wrote one "
            "ROOT TTree per activity"
        ),
        "classes": DAILY_ACTIVITIES,
        "modality": "wearable time series",
        "task": "activity classification",
    },
    {
        "name": "gas_sensor_temperature",
        "label": "Gas Sensor Array Temperature Modulation",
        "title": "4.1 million temperature-cycled gas-array readings",
        "source": "https://archive.ics.uci.edu/dataset/487/gas+sensor+array+temperature+modulation",
        "url": "https://archive.ics.uci.edu/static/public/487/gas+sensor+array+temperature+modulation.zip",
        "source_bytes": 183298753,
        "converter": "gas_temperature",
        "transformation": (
            "streamed every timestamp, retained environmental controls and all 14 raw sensor "
            "resistances without scaling, and separated clean-air and carbon-monoxide rows by "
            "the published concentration"
        ),
        "classes": ("clean_air", "carbon_monoxide"),
        "modality": "chemical sensor time series",
        "task": "gas detection",
    },
    {
        "name": "twin_gas_sensor_arrays",
        "label": "Twin Gas Sensor Arrays",
        "title": "640 ten-minute gas exposures recorded by two eight-sensor boards",
        "source": "https://archive.ics.uci.edu/dataset/361/twin+gas+sensor+arrays",
        "url": "https://archive.ics.uci.edu/static/public/361/twin+gas+sensor+arrays.zip",
        "source_bytes": 204012393,
        "converter": "twin_gas",
        "transformation": (
            "streamed each experiment, retained its time and eight raw resistance channels, "
            "padded only recordings shorter than the publisher's inclusive 60,001-sample "
            "limit and preserved board, gas, concentration and repetition metadata; wrote "
            "one ROOT TTree per gas"
        ),
        "classes": ("ethanol", "carbon_monoxide", "ethylene", "methane"),
        "modality": "chemical sensor time series",
        "task": "gas classification",
    },
    {
        "name": "electricity_load_diagrams",
        "label": "ElectricityLoadDiagrams20112014",
        "title": "140,256 quarter-hour loads from 370 electricity clients",
        "source": "https://archive.ics.uci.edu/dataset/321/electricityloaddiagrams20112014",
        "url": "https://archive.ics.uci.edu/static/public/321/electricityloaddiagrams20112014.zip",
        "source_bytes": 261335609,
        "converter": "electricity",
        "transformation": (
            "streamed the semicolon table, converted decimal commas to points and retained all "
            "370 client loads without scaling alongside the original timestamp"
        ),
        "classes": (),
        "modality": "energy time series",
        "task": "forecasting and clustering",
    },
    {
        "name": "opportunity_activity",
        "label": "OPPORTUNITY Activity Recognition",
        "title": "wearable and ambient sensor streams with locomotion annotations",
        "source": "https://archive.ics.uci.edu/dataset/226/opportunity+activity+recognition",
        "url": "https://archive.ics.uci.edu/static/public/226/opportunity+activity+recognition.zip",
        "source_bytes": 306636009,
        "converter": "opportunity",
        "transformation": (
            "streamed every published sensor row without interpolation or scaling, retained "
            "missing values as NaN and used the published locomotion annotation as the class"
        ),
        "classes": ("unlabelled", "stand", "walk", "sit", "lie"),
        "modality": "wearable time series",
        "task": "activity classification",
    },
    {
        "name": "gas_sensor_dynamic_mixtures",
        "label": "Gas Sensor Array under Dynamic Gas Mixtures",
        "title": "4.2 million readings of ethylene mixed with CO or methane",
        "source": "https://archive.ics.uci.edu/dataset/322/gas+sensor+array+under+dynamic+gas+mixtures",
        "url": "https://archive.ics.uci.edu/static/public/322/gas+sensor+array+under+dynamic+gas+mixtures.zip",
        "source_bytes": 369001314,
        "converter": "gas_dynamic",
        "transformation": (
            "streamed time, both gas concentrations and all 16 raw sensor channels without "
            "scaling; wrote separate ROOT TTrees for carbon-monoxide and methane mixtures"
        ),
        "classes": ("carbon_monoxide_mixture", "methane_mixture"),
        "modality": "chemical sensor time series",
        "task": "mixture classification and regression",
    },
    {
        "name": "p53_mutants",
        "label": "p53 Mutants",
        "title": "16,772 p53 variants described by 5,408 molecular features",
        "source": "https://archive.ics.uci.edu/dataset/188/p53+mutants",
        "url": "https://archive.ics.uci.edu/static/public/188/p53+mutants.zip",
        "source_bytes": 552632888,
        "converter": "p53",
        "transformation": (
            "streamed the complete K9 feature table from the current nested archive (accepting "
            "the legacy K8 name), removed K9's delimiter-created empty field after its class, "
            "retained 5,408 features without scaling, mapped missing cells to NaN and wrote "
            "active and inactive ROOT TTrees"
        ),
        "classes": ("inactive", "active"),
        "modality": "molecular features",
        "task": "binary classification",
    },
    {
        "name": "pamap2",
        "label": "PAMAP2 Physical Activity Monitoring",
        "title": "100 Hz heart-rate and inertial recordings from nine people",
        "source": "https://archive.ics.uci.edu/dataset/231/pamap2+physical+activity+monitoring",
        "url": "https://archive.ics.uci.edu/static/public/231/pamap2+physical+activity+monitoring.zip",
        "source_bytes": 688226940,
        "converter": "pamap2",
        "transformation": (
            "streamed every timestamp from protocol and optional recordings, retained all 52 "
            "raw measurements without interpolation or scaling and represented missing values "
            "as NaN; wrote one ROOT TTree per published activity code"
        ),
        "classes": PAMAP_ACTIVITIES,
        "modality": "wearable time series",
        "task": "activity classification",
    },
    {
        "name": "hhar",
        "label": "Heterogeneity Activity Recognition",
        "title": "43.9 million phone and watch accelerometer and gyroscope readings",
        "source": "https://archive.ics.uci.edu/dataset/344/heterogeneity+activity+recognition",
        "url": "https://archive.ics.uci.edu/static/public/344/heterogeneity+activity+recognition.zip",
        "source_bytes": 822098071,
        "converter": "hhar",
        "transformation": (
            "streamed phone and watch CSV records without resampling or scaling, retained both "
            "timestamps and x/y/z readings, encoded source device metadata deterministically "
            "and wrote one ROOT TTree per activity"
        ),
        "classes": HHAR_ACTIVITIES,
        "modality": "phone and watch time series",
        "task": "activity classification and domain adaptation",
    },
    {
        "name": "year_prediction_msd",
        "label": "Year Prediction MSD",
        "title": "515,345 songs represented by 90 timbre features and release year",
        "source": "https://archive.ics.uci.edu/dataset/203/yearpredictionmsd",
        "url": "https://archive.ics.uci.edu/static/public/203/yearpredictionmsd.zip",
        "source_bytes": 211_011_981,
        "converter": "year_prediction",
        "transformation": (
            "streamed the complete published text table, cast all 90 Echo Nest timbre "
            "statistics to float32 without scaling, retained release year as the regression "
            "target, and preserved UCI's official first-463,715 training and final-51,630 "
            "test partition"
        ),
        "classes": (),
        "splits": ("train", "test"),
        "modality": "audio features",
        "task": "release-year regression",
    },
)


def _float32(values: Iterable[Any]) -> array.array[float]:
    return array.array("f", (float(value) for value in values))


def _padded(values: Any, width: int, *, kind: str = "f", fill: float = nan) -> array.array[Any]:
    result = array.array(kind, values)
    if len(result) > width:
        raise ValueError(f"a signal has {len(result)} samples, and its column holds {width}")
    result.extend([fill] * (width - len(result)))
    return result


def _csv_rows(
    stream: Any, *, compressed: bool = False, header: bool = False
) -> Iterator[list[str]]:
    binary: Any = gzip.GzipFile(fileobj=stream) if compressed else stream
    text = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")
    try:
        rows = csv.reader(text)
        if header:
            next(rows, None)
        for row in rows:
            if row:
                yield [cell.strip() for cell in row]
    finally:
        text.close()


def _events(
    path: Path,
    *,
    member: str,
    width: int,
    start: int = 0,
    stop: int | None = None,
    header: bool = False,
) -> Rows:
    archive = zipfile.ZipFile(path)
    try:
        with archive.open(member) as held:
            for index, cells in enumerate(
                _csv_rows(held, compressed=member.endswith(".gz"), header=header)
            ):
                if stop is not None and index >= stop:
                    break
                if index < start:
                    continue
                if len(cells) != width + 1:
                    raise ValueError(
                        f"row {index} of {member} has {len(cells)} fields, not {width + 1}"
                    )
                label = int(float(cells[0]))
                if label not in (0, 1):
                    raise ValueError(f"row {index} of {member} has class {cells[0]!r}")
                yield (
                    label,
                    {
                        "features": _float32(cells[1:]),
                        "label": label,
                        "index": index,
                    },
                )
    finally:
        archive.close()


def _collision(path: Path, split: str, *, name: str, total: int, width: int) -> Loaded:
    test = 500_000
    start, stop = (0, total - test) if split == "train" else (total - test, total)
    columns: dict[str, Any] = {"features": ("f", width), "label": "i", "index": "i"}
    return (
        ("background", "signal"),
        columns,
        _events(path, member=f"{name}.csv.gz", width=width, start=start, stop=stop),
    )


def _hepmass(path: Path, split: str) -> Loaded:
    partition, hypothesis = split.split("_", 1)
    width = 27 if hypothesis == "1000" else 28
    columns: dict[str, Any] = {"features": ("f", width), "label": "i", "index": "i"}
    return (
        ("background", "signal"),
        columns,
        _events(path, member=f"{hypothesis}_{partition}.csv.gz", width=width, header=True),
    )


def _realdisp_entries(path: Path, split: str) -> Rows:
    archive = zipfile.ZipFile(path)
    index = 0
    pattern = re.compile(r"subject(\d+)_(ideal|self|mutual(\d+))\.log$")
    wanted = None if split == "all" else int(split.removeprefix("subject_"))
    try:
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            matched = pattern.fullmatch(info.filename)
            if matched is None or (wanted is not None and int(matched.group(1)) != wanted):
                continue
            for row in _realdisp_file(archive, info, matched, index):
                yield row
                index += 1
    finally:
        archive.close()


def _realdisp_file(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, matched: re.Match[str], first: int
) -> Rows:
    subject = int(matched.group(1))
    scenarios = {"ideal": 0, "self": 1}
    scenario = scenarios.get(matched.group(2), 2)
    displacement = int(matched.group(3) or 0)
    with archive.open(info) as held:
        text = io.TextIOWrapper(held, encoding="utf-8", newline="")
        try:
            for physical, line in enumerate(text):
                cells = line.strip().split()
                if not cells:
                    continue
                yield _realdisp_row(
                    cells, info.filename, physical, first, subject, scenario, displacement
                )
                first += 1
        finally:
            text.close()


def _realdisp_row(
    cells: list[str],
    filename: str,
    physical: int,
    index: int,
    subject: int,
    scenario: int,
    displacement: int,
) -> tuple[int, dict[str, Any]]:
    if len(cells) != 120:
        raise ValueError(f"row {physical} of {filename} has {len(cells)} fields, not 120")
    label = int(float(cells[-1]))
    if not 0 <= label < len(ACTIVITIES):
        raise ValueError(f"row {physical} of {filename} has activity {label}")
    return label, {
        "features": _float32(cells[2:-1]),
        "seconds": int(float(cells[0])),
        "microseconds": int(float(cells[1])),
        "subject": subject,
        "scenario": scenario,
        "displacement": displacement,
        "label": label,
        "index": index,
    }


def _realdisp(path: Path, split: str) -> Loaded:
    columns: dict[str, Any] = {
        "features": ("f", 117),
        "seconds": "i",
        "microseconds": "i",
        "subject": "i",
        "scenario": "i",
        "displacement": "i",
        "label": "i",
        "index": "i",
    }
    return ACTIVITIES, columns, _realdisp_entries(path, split)


def _gas_member(
    info: zipfile.ZipInfo, labels: Mapping[str, int]
) -> tuple[re.Match[str], str, str] | None:
    parts = info.filename.split("/")
    if info.is_dir():
        return None
    if len(parts) != 4:
        return None
    if _GAS_NAME.fullmatch(parts[-1]) is None:
        return None
    matched, gas = _gas_name(parts[-1], labels)
    return matched, gas, parts[2]


def _gas_entries(path: Path, split: str) -> Rows:
    labels = {name: at for at, name in enumerate(GASES)}
    index = 0
    with zipfile.ZipFile(path) as archive:
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            recording = _gas_member(info, labels)
            if recording is None:
                continue
            matched, gas, location = recording
            source_index = index
            index += 1
            if split not in ("all", gas):
                continue
            arrays = _gas_arrays(archive, info)
            which, row = _gas_entry(
                arrays, matched, location, labels[gas], source_index, info.filename
            )
            yield (which if split == "all" else 0), row


def _gas_name(filename: str, labels: Mapping[str, int]) -> tuple[re.Match[str], str]:
    """Parse the experimental controls encoded in a gas recording name."""
    matched = _GAS_NAME.fullmatch(filename)
    if matched is None:
        raise ValueError(f"the gas recording name {filename!r} has an unknown layout")
    gas = "carbon_monoxide" if matched.group(4).lower() == "co" else matched.group(4).lower()
    if gas not in labels:
        raise ValueError(f"the gas recording {filename!r} names unknown gas {gas!r}")
    return matched, gas


def _gas_arrays(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo
) -> tuple[
    array.array[int],
    array.array[float],
    array.array[float],
    array.array[float],
    array.array[float],
]:
    """Read the five aligned signal arrays in one gas recording."""
    time = array.array("i")
    controls = array.array("f")
    temperature = array.array("f")
    humidity = array.array("f")
    sensors = array.array("f")
    with archive.open(info) as held:
        text = io.TextIOWrapper(held, encoding="utf-8", newline="")
        try:
            for physical, line in enumerate(text):
                cells = line.strip().split()
                if cells:
                    _gas_line(
                        cells,
                        physical,
                        info.filename,
                        time,
                        controls,
                        temperature,
                        humidity,
                        sensors,
                    )
        finally:
            text.close()
    return time, controls, temperature, humidity, sensors


def _gas_line(
    cells: list[str],
    physical: int,
    filename: str,
    time: array.array[int],
    controls: array.array[float],
    temperature: array.array[float],
    humidity: array.array[float],
    sensors: array.array[float],
) -> None:
    """Validate and append one 92-field sensor reading."""
    if len(cells) != 92:
        raise ValueError(f"row {physical} of {filename} has {len(cells)} fields, not 92")
    values = _gas_numbers(cells, physical, filename)
    time.append(int(values[0]))
    controls.extend(values[1:9])
    temperature.append(values[9])
    humidity.append(values[10])
    for board in range(9):
        marker = 11 + board * 9
        if values[marker] != 1:
            raise ValueError(f"row {physical} of {filename} has no board {board + 1} marker")
        sensors.extend(values[marker + 1 : marker + 9])


def _gas_numbers(cells: Sequence[str], physical: int, filename: str) -> list[float]:
    """Decode one gas row after removing the publisher's boundary direction marks."""
    cleaned = [value.strip("\u200e") for value in cells]
    try:
        return [float(value) for value in cleaned]
    except ValueError as error:
        for field, value in enumerate(cleaned):
            try:
                float(value)
            except ValueError:
                raise ValueError(
                    f"row {physical} field {field} of {filename} is not numeric: "
                    f"{cells[field]!r}"
                ) from error
        raise


def _gas_entry(
    arrays: tuple[
        array.array[int],
        array.array[float],
        array.array[float],
        array.array[float],
        array.array[float],
    ],
    matched: re.Match[str],
    location: str,
    which: int,
    index: int,
    filename: str,
) -> tuple[int, dict[str, Any]]:
    """Pad one gas experiment and attach the controls encoded in its path."""
    time, controls, temperature, humidity, sensors = arrays
    length = len(time)
    if length > 26_000:
        raise ValueError(f"{filename} has {length} readings, not at most 26000")
    return which, {
        "time_ms": _padded(time, 26_000, kind="i", fill=0),
        "controls": _padded(controls, 8 * 26_000),
        "temperature": _padded(temperature, 26_000),
        "humidity": _padded(humidity, 26_000),
        "sensors": _padded(sensors, 72 * 26_000),
        "length": length,
        "timestamp": int(matched.group(1)),
        "heater": int(matched.group(2)),
        "fan": int(matched.group(3)),
        "concentration": int(matched.group(5)),
        "location": int(location.removeprefix("L")),
        "platform_position": int(matched.group(6)),
        "label": which,
        "index": index,
    }


def _gas(path: Path, split: str) -> Loaded:
    columns: dict[str, Any] = {
        "time_ms": ("i", 26_000),
        "controls": ("f", 8 * 26_000),
        "temperature": ("f", 26_000),
        "humidity": ("f", 26_000),
        "sensors": ("f", 72 * 26_000),
        "length": "i",
        "timestamp": "q",
        "heater": "i",
        "fan": "i",
        "concentration": "i",
        "location": "i",
        "platform_position": "i",
        "label": "i",
        "index": "i",
    }
    classes = GASES if split == "all" else (split,)
    return classes, columns, _gas_entries(path, split)


@contextmanager
def _extracted(archive: zipfile.ZipFile, info: zipfile.ZipInfo, beside: Path) -> Iterator[Path]:
    """Extract one seekable member beside the cache, falling back to /tmp."""
    try:
        held = tempfile.NamedTemporaryFile(
            prefix=f".{beside.stem}-",
            suffix=Path(info.filename).suffix,
            dir=beside.parent,
            delete=False,
        )
    except OSError:
        held = tempfile.NamedTemporaryFile(
            prefix=f"xrd-{beside.stem}-", suffix=Path(info.filename).suffix, delete=False
        )
    path = Path(held.name)
    try:
        with held, archive.open(info) as source:
            shutil.copyfileobj(source, held, length=1 << 20)
        yield path
    finally:
        path.unlink(missing_ok=True)


def _matrix_channels(value: Any, numpy: Any, name: str) -> Any:
    """A MATLAB signal record as samples by three channels."""
    measured = numpy.asarray(value)
    if measured.ndim != 2 or 3 not in measured.shape:
        raise ValueError(f"{name} has shape {measured.shape}, not three signal channels")
    return measured if measured.shape[1] == 3 else measured.T


def _cuffless_entries(path: Path) -> Rows:
    try:
        h5py = importlib.import_module("h5py")
        numpy = importlib.import_module("numpy")
    except ModuleNotFoundError:
        raise ValueError(
            "Cuff-Less Blood Pressure needs the datasets extra: "
            "pip install 'pyxrootdclient[datasets]'"
        ) from None
    archive = zipfile.ZipFile(path)
    index = 0
    try:
        members = sorted(
            (
                info
                for info in archive.infolist()
                if re.fullmatch(r"Part_[1-4]\.mat", info.filename)
            ),
            key=lambda item: item.filename,
        )
        if len(members) != 4:
            raise ValueError(f"the blood-pressure archive has {len(members)} MATLAB parts, not 4")
        for part, info in enumerate(members, 1):
            with _extracted(archive, info, path) as extracted, h5py.File(extracted, "r") as book:
                variable = Path(info.filename).stem
                if variable not in book:
                    raise ValueError(f"{info.filename} has no {variable!r} cell array")
                references = numpy.asarray(book[variable][()]).reshape(-1)
                for record, reference in enumerate(references):
                    signals = _matrix_channels(book[reference][()], numpy, info.filename)
                    for window, start in enumerate(range(0, len(signals), 1000)):
                        section = signals[start : start + 1000]
                        length = len(section)
                        yield (
                            0,
                            {
                                "ppg": _padded(section[:, 0], 1000),
                                "target": _padded(section[:, 1], 1000),
                                "ecg": _padded(section[:, 2], 1000),
                                "length": length,
                                "part": part,
                                "record": record,
                                "window": window,
                                "index": index,
                            },
                        )
                        index += 1
    finally:
        archive.close()


def _cuffless(path: Path) -> Loaded:
    columns: dict[str, Any] = {
        "ppg": ("f", 1000),
        "target": ("f", 1000),
        "ecg": ("f", 1000),
        "length": "i",
        "part": "i",
        "record": "i",
        "window": "i",
        "index": "i",
    }
    return ("rows",), columns, _cuffless_entries(path)


class _Slice(io.RawIOBase):
    """A seekable view of a stored member inside a ZIP64 file."""

    def __init__(self, path: Path, start: int, size: int) -> None:
        self._handle = path.open("rb")
        self._start = start
        self._size = size
        self._at = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._at

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            at = offset
        elif whence == os.SEEK_CUR:
            at = self._at + offset
        elif whence == os.SEEK_END:
            at = self._size + offset
        else:
            raise ValueError(f"unknown seek origin {whence}")
        if at < 0:
            raise ValueError("a nested ZIP seek goes before its first byte")
        self._at = min(at, self._size)
        return self._at

    def readinto(self, target: Any) -> int:
        count = min(len(target), self._size - self._at)
        if count <= 0:
            return 0
        self._handle.seek(self._start + self._at)
        raw = self._handle.read(count)
        target[: len(raw)] = raw
        self._at += len(raw)
        return len(raw)

    def close(self) -> None:
        self._handle.close()
        super().close()


def _member_start(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> int:
    handle = archive.fp
    if handle is None:
        raise ValueError("the outer ZIP is closed")
    handle.seek(info.header_offset)
    header = handle.read(30)
    if len(header) != 30 or header[:4] != b"PK\x03\x04":
        raise ValueError(f"{info.filename} has no ZIP local header")
    name, extra = (int(value) for value in struct.unpack_from("<HH", header, 26))
    return info.header_offset + 30 + name + extra


@contextmanager
def _nested_zip(path: Path, member: str = "data.zip") -> Iterator[zipfile.ZipFile]:
    """Open a nested ZIP without copying it when its outer member is stored."""
    outer = zipfile.ZipFile(path)
    temporary: Any = None
    try:
        try:
            info = outer.getinfo(member)
        except KeyError:
            raise ValueError(f"the archive has no nested {member!r}") from None
        if info.compress_type == zipfile.ZIP_STORED:
            view = io.BufferedReader(_Slice(path, _member_start(outer, info), info.file_size))
            try:
                with zipfile.ZipFile(view) as inner:
                    yield inner
            finally:
                view.close()
        else:
            temporary = _extracted(outer, info, path)
            with temporary as extracted, zipfile.ZipFile(extracted) as inner:
                yield inner
    finally:
        outer.close()


class _ArraysOnly(pickle.Unpickler):
    """The narrow set of constructors used by a NumPy array pickle."""

    SAFE: ClassVar[set[tuple[str, str]]] = {
        ("numpy", "ndarray"),
        ("numpy", "dtype"),
        ("numpy.core.multiarray", "_reconstruct"),
        ("numpy._core.multiarray", "_reconstruct"),
        ("_codecs", "encode"),
    }

    def find_class(self, module: str, name: str) -> Any:
        if (module, name) not in self.SAFE:
            raise pickle.UnpicklingError(f"the PPG pickle asks for forbidden {module}.{name}")
        return super().find_class(module, name)


def _flat(numpy: Any, value: Any) -> Any:
    return numpy.asarray(value).reshape(-1)


def _ppg_entries(path: Path) -> Rows:
    numpy = _ppg_numpy()
    index = 0
    with _nested_zip(path) as archive:
        for info in _ppg_members(archive):
            for row in _ppg_subject(numpy, archive, info, index):
                yield row
                index += 1


def _ppg_numpy() -> Any:
    """Import NumPy with an actionable dataset-extra diagnostic."""
    try:
        return importlib.import_module("numpy")
    except ModuleNotFoundError:
        raise ValueError(
            "PPG-DaLiA needs the datasets extra: pip install 'pyxrootdclient[datasets]'"
        ) from None


def _ppg_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """The fifteen subject pickles in stable subject order."""
    members = sorted(
        (
            info
            for info in archive.infolist()
            if re.fullmatch(r"PPG_FieldStudy/S\d+/S\d+\.pkl", info.filename)
        ),
        key=lambda item: int(item.filename.split("/")[1][1:]),
    )
    if len(members) != 15:
        raise ValueError(f"PPG-DaLiA has {len(members)} subject pickles, not 15")
    return members


def _ppg_signals(numpy: Any, archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[Any, ...]:
    """Load and validate the arrays from one restricted subject pickle."""
    with archive.open(info) as held:
        document = _ArraysOnly(held, encoding="latin1").load()
    try:
        wrist = document["signal"]["wrist"]
        signals = (
            _flat(numpy, wrist["BVP"]),
            numpy.asarray(wrist["ACC"]),
            _flat(numpy, wrist["EDA"]),
            _flat(numpy, wrist["TEMP"]),
            _flat(numpy, document["label"]),
            _flat(numpy, document["activity"]),
        )
    except (KeyError, TypeError):
        raise ValueError(f"{info.filename} does not have the published signal layout") from None
    acceleration = signals[1]
    if acceleration.ndim != 2 or acceleration.shape[1] != 3:
        raise ValueError(f"{info.filename} wrist acceleration has shape {acceleration.shape}")
    return signals


def _ppg_subject(
    numpy: Any, archive: zipfile.ZipFile, info: zipfile.ZipInfo, first: int
) -> Rows:
    """Every fixed-width heart-rate window for one subject."""
    bvp, acceleration, eda, temperature, targets, activity = _ppg_signals(
        numpy, archive, info
    )
    subject = int(info.filename.split("/")[1][1:])
    for target_at, target in enumerate(targets):
        yield _ppg_row(
            bvp,
            acceleration,
            eda,
            temperature,
            activity,
            target,
            target_at,
            subject,
            first + target_at,
            info.filename,
        )


def _ppg_row(
    bvp: Any,
    acceleration: Any,
    eda: Any,
    temperature: Any,
    activity: Any,
    target: Any,
    target_at: int,
    subject: int,
    index: int,
    filename: str,
) -> tuple[int, dict[str, Any]]:
    """Validate and materialise one PPG-DaLiA training example."""
    bvp_at, acc_at, slow_at = target_at * 128, target_at * 64, target_at * 8
    if (
        bvp_at + 512 > len(bvp)
        or acc_at + 256 > len(acceleration)
        or slow_at + 32 > len(eda)
        or slow_at + 32 > len(temperature)
    ):
        raise ValueError(f"{filename} ends before heart-rate target {target_at}")
    activity_at = min(slow_at + 16, len(activity) - 1)
    return 0, {
        "bvp": _float32(bvp[bvp_at : bvp_at + 512]),
        "acceleration": _float32(acceleration[acc_at : acc_at + 256].reshape(-1)),
        "eda": _float32(eda[slow_at : slow_at + 32]),
        "temperature": _float32(temperature[slow_at : slow_at + 32]),
        "subject": subject,
        "activity": int(activity[activity_at]),
        "target": float(target),
        "index": index,
    }


def _ppg(path: Path) -> Loaded:
    columns: dict[str, Any] = {
        "bvp": ("f", 512),
        "acceleration": ("f", 768),
        "eda": ("f", 32),
        "temperature": ("f", 32),
        "subject": "i",
        "activity": "i",
        "target": "d",
        "index": "i",
    }
    return ("rows",), columns, _ppg_entries(path)


def _chromosome(value: str) -> int:
    name = value.removeprefix("chr")
    if name.isdigit() and 1 <= int(name) <= 22:
        return int(name)
    if name == "X":
        return 23
    if name == "Y":
        return 24
    raise ValueError(f"ChIP-seq names unknown chromosome {value!r}")


def _chip_labels(raw: bytes, name: str) -> dict[str, list[tuple[int, int, int]]]:
    result: dict[str, list[tuple[int, int, int]]] = {}
    for physical, line in enumerate(raw.decode("utf-8-sig").splitlines()):
        cells = line.split()
        if not cells:
            continue
        if len(cells) != 4 or cells[3] not in CHIP_CODES:
            raise ValueError(f"row {physical} of {name} is not a published weak label")
        start, stop = int(cells[1]), int(cells[2])
        if stop <= start:
            raise ValueError(f"row {physical} of {name} has an empty interval")
        result.setdefault(cells[0], []).append((start, stop, CHIP_CODES[cells[3]]))
    for regions in result.values():
        regions.sort()
    return result


def _segments(
    start: int, stop: int, regions: Sequence[tuple[int, int, int]]
) -> Iterator[tuple[int, int, int]]:
    """Split one coverage run where weak-label interval boundaries cross it."""
    at = start
    for lower, upper, label in regions:
        if upper <= at:
            continue
        if lower >= stop:
            break
        if at < lower:
            yield at, min(lower, stop), 0
            at = min(lower, stop)
        if at < stop and upper > at:
            end = min(upper, stop)
            yield at, end, label
            at = end
        if at >= stop:
            break
    if at < stop:
        yield at, stop, 0


def _chip_problem(
    coverage: BinaryIO,
    labels: bytes,
    *,
    name: str,
    problem: int,
    first: int,
) -> Iterator[tuple[int, dict[str, Any]]]:
    regions = _chip_labels(labels, name)
    coverage.seek(0)
    zipped = gzip.GzipFile(fileobj=coverage)
    text = io.TextIOWrapper(zipped, encoding="utf-8", newline="")
    index = first
    try:
        for physical, line in enumerate(text):
            cells = line.split()
            if not cells:
                continue
            if len(cells) != 4:
                raise ValueError(f"row {physical} of {name} has {len(cells)} fields, not 4")
            chromosome, start, stop, count = cells
            lower, upper = int(start), int(stop)
            if upper <= lower:
                raise ValueError(f"row {physical} of {name} has an empty coverage interval")
            for begin, end, which in _segments(lower, upper, regions.get(chromosome, ())):
                yield (
                    which,
                    {
                        "chromosome": _chromosome(chromosome),
                        "start": begin,
                        "end": end,
                        "count": int(count),
                        "problem": problem,
                        "label": which,
                        "index": index,
                    },
                )
                index += 1
    finally:
        text.close()


def _chipseq_entries(path: Path) -> Rows:
    outer = zipfile.ZipFile(path)
    pending: dict[str, dict[str, Any]] = {}
    problem = 0
    index = 0
    try:
        try:
            info = outer.getinfo("peak-detection-data.tar.xz")
        except KeyError:
            raise ValueError("the ChIP-seq archive has no peak-detection-data.tar.xz") from None
        with (
            outer.open(info) as compressed,
            tarfile.open(fileobj=compressed, mode="r|xz") as archive,
        ):
            for member in archive:
                leaf = member.name.rsplit("/", 1)[-1]
                if leaf not in ("labels.bed", "coverage.bedGraph.gz") or not member.isfile():
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    raise ValueError(f"the ChIP-seq tar cannot read {member.name}")
                key = member.name.rsplit("/", 1)[0]
                pair = pending.setdefault(key, {})
                with handle:
                    if leaf == "labels.bed":
                        pair["labels"] = handle.read()
                    else:
                        stored = tempfile.SpooledTemporaryFile(max_size=16 << 20)
                        shutil.copyfileobj(handle, stored, length=1 << 20)
                        pair["coverage"] = stored
                if "labels" not in pair or "coverage" not in pair:
                    continue
                coverage = pair["coverage"]
                try:
                    for which, row in _chip_problem(
                        coverage, pair["labels"], name=key, problem=problem, first=index
                    ):
                        yield which, row
                        index += 1
                finally:
                    coverage.close()
                del pending[key]
                problem += 1
        if pending:
            first = next(iter(pending))
            raise ValueError(f"the ChIP-seq problem {first!r} does not have both data and labels")
        if problem == 0:
            raise ValueError("the ChIP-seq archive has no complete problems")
    finally:
        for pair in pending.values():
            if "coverage" in pair:
                pair["coverage"].close()
        outer.close()


def _chipseq(path: Path) -> Loaded:
    columns: dict[str, Any] = {
        "chromosome": "i",
        "start": "q",
        "end": "q",
        "count": "i",
        "problem": "i",
        "label": "i",
        "index": "i",
    }
    return CHIP_LABELS, columns, _chipseq_entries(path)


def _fixed_text(raw: bytes, width: int, name: str) -> bytes:
    if len(raw) > width:
        raise ValueError(f"{name} has {len(raw)} text bytes, and its column holds {width}")
    return raw + bytes(width - len(raw))


def _humanitarian_image(raw: bytes, image_module: Any) -> tuple[bytes, tuple[int, ...], bool]:
    """Decode and letterbox one publisher JPEG without distorting its aspect ratio."""
    try:
        pixels, geometry = _decoded_humanitarian_image(raw, image_module)
    except OSError as exc:
        if (
            not raw.startswith(b"\xff\xd8")
            or raw.endswith(b"\xff\xd9")
            or "image file is truncated" not in str(exc)
        ):
            raise
        pixels, geometry = _decoded_humanitarian_image(raw + b"\xff\xd9", image_module)
        return pixels, geometry, True
    return pixels, geometry, False


def _decoded_humanitarian_image(
    raw: bytes, image_module: Any
) -> tuple[bytes, tuple[int, ...]]:
    """Decode one complete JPEG and return its pixels and letterbox geometry."""
    with image_module.open(io.BytesIO(raw)) as picture:
        width, height = picture.size
        image = picture.convert("RGB")
        image.thumbnail((640, 640), image_module.Resampling.BILINEAR)
        resized_width, resized_height = image.size
        left = (640 - resized_width) // 2
        top = (640 - resized_height) // 2
        canvas = image_module.new("RGB", (640, 640))
        canvas.paste(image, (left, top))
        return canvas.tobytes(), (width, height, resized_width, resized_height, left, top)


def _humanitarian_module() -> Any:
    try:
        return importlib.import_module("PIL.Image")
    except ModuleNotFoundError:
        raise ValueError(
            "Multimodal Damage needs the datasets extra: pip install 'pyxrootdclient[datasets]'"
        ) from None


def _humanitarian_images(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    return sorted(
        (
            info
            for info in archive.infolist()
            if "/images/" in info.filename and info.filename.lower().endswith(".jpg")
        ),
        key=lambda item: item.filename,
    )


def _humanitarian_entry(
    archive: zipfile.ZipFile,
    held: Mapping[str, zipfile.ZipInfo],
    info: zipfile.ZipInfo,
    image_module: Any,
    index: int,
) -> tuple[int, dict[str, Any]]:
    parts = info.filename.split("/")
    if len(parts) != 4 or parts[1] not in HUMANITARIAN_CODES:
        raise ValueError(f"the image path {info.filename!r} has an unknown class")
    which = HUMANITARIAN_CODES[parts[1]]
    with archive.open(info) as source:
        pixels, geometry, repaired = _humanitarian_image(source.read(), image_module)
    text_name = info.filename.replace("/images/", "/text/").rsplit(".", 1)[0] + ".txt"
    text_info = held.get(text_name)
    caption = archive.read(text_info).strip() if text_info is not None else b""
    return (
        which,
        {
            "image": pixels,
            "caption": _fixed_text(caption, 8192, text_name),
            "caption_length": len(caption),
            "source_width": geometry[0],
            "source_height": geometry[1],
            "resized_width": geometry[2],
            "resized_height": geometry[3],
            "left_padding": geometry[4],
            "top_padding": geometry[5],
            "jpeg_eoi_repaired": repaired,
            "label": which,
            "index": index,
        },
    )


def _humanitarian_entries(path: Path) -> Rows:
    image_module = _humanitarian_module()
    with zipfile.ZipFile(path) as archive:
        held = {info.filename: info for info in archive.infolist()}
        for index, info in enumerate(_humanitarian_images(archive)):
            yield _humanitarian_entry(archive, held, info, image_module, index)


def _humanitarian(path: Path) -> Loaded:
    columns: dict[str, Any] = {
        "image": ("B", 640 * 640 * 3),
        "caption": ("B", 8192),
        "caption_length": "i",
        "source_width": "i",
        "source_height": "i",
        "resized_width": "i",
        "resized_height": "i",
        "left_padding": "i",
        "top_padding": "i",
        "jpeg_eoi_repaired": "?",
        "label": "i",
        "index": "i",
    }
    return HUMANITARIAN, columns, _humanitarian_entries(path)


_LONG_VR = {b"OB", b"OD", b"OF", b"OL", b"OV", b"OW", b"SQ", b"UC", b"UR", b"UT", b"UN"}


def _dicom_value(raw: bytes, group: int, element: int) -> tuple[bytes, bytes]:
    marker = struct.pack("<HH", group, element)
    at = raw.find(marker, 132)
    if at < 0:
        raise ValueError(f"a DICOM slice has no ({group:04x},{element:04x}) element")
    vr = raw[at + 4 : at + 6]
    if vr in _LONG_VR:
        size = struct.unpack_from("<I", raw, at + 8)[0]
        start = at + 12
    else:
        size = struct.unpack_from("<H", raw, at + 6)[0]
        start = at + 8
    if size == 0xFFFFFFFF or start + size > len(raw):
        raise ValueError(f"DICOM element ({group:04x},{element:04x}) is not a fixed value")
    return vr, raw[start : start + size]


def _dicom_number(raw: bytes, group: int, element: int) -> int:
    vr, value = _dicom_value(raw, group, element)
    if vr != b"US" or len(value) != 2:
        raise ValueError(f"DICOM element ({group:04x},{element:04x}) is not one uint16")
    return int(struct.unpack("<H", value)[0])


def _dicom_pixels(raw: bytes) -> tuple[array.array[int], float, float]:
    if len(raw) < 132 or raw[128:132] != b"DICM":
        raise ValueError("a medical image is not a DICOM Part 10 file")
    rows = _dicom_number(raw, 0x0028, 0x0010)
    columns = _dicom_number(raw, 0x0028, 0x0011)
    bits = _dicom_number(raw, 0x0028, 0x0100)
    signed = _dicom_number(raw, 0x0028, 0x0103)
    if (rows, columns, bits, signed) != (512, 512, 16, 1):
        raise ValueError(
            f"a medical image is {rows}x{columns}, {bits}-bit, representation {signed}"
        )
    _, held = _dicom_value(raw, 0x7FE0, 0x0010)
    if len(held) != rows * columns * 2:
        raise ValueError(f"a medical image has {len(held)} pixel bytes, not {rows * columns * 2}")
    pixels = array.array("h")
    pixels.frombytes(held)
    if sys.byteorder != "little":
        pixels.byteswap()
    intercept = float(_dicom_value(raw, 0x0028, 0x1052)[1].decode().strip(" \0"))
    slope = float(_dicom_value(raw, 0x0028, 0x1053)[1].decode().strip(" \0"))
    return pixels, intercept, slope


def _dicom_truth(
    archive: zipfile.ZipFile,
) -> dict[tuple[int, int, int], tuple[int, int, int]]:
    exact: dict[tuple[int, int, int], tuple[int, int, int]] = {}
    for experiment in (1, 2):
        name = f"Tampered Scans/labels_exp{experiment}.csv"
        with archive.open(name) as held:
            text = io.TextIOWrapper(held, encoding="utf-8-sig", newline="")
            try:
                rows = csv.DictReader(text)
                for row in rows:
                    code = DICOM_CODES[row["type"]]
                    patient, section = int(row["uuid"]), int(row["slice"])
                    exact[(experiment, patient, section)] = (
                        code,
                        int(row["x"]),
                        int(row["y"]),
                    )
            finally:
                text.close()
    return exact


def _dicom_partition(split: str) -> tuple[int, int] | None:
    if split == "all":
        return None
    matched = re.fullmatch(r"experiment_([12])_patients_([0-3])", split)
    if matched is None:
        raise ValueError(f"unknown medical deepfake shard {split!r}")
    return int(matched.group(1)), int(matched.group(2))


def _dicom_identity(info: zipfile.ZipInfo) -> tuple[int, int, int]:
    parts = info.filename.split("/")
    if len(parts) != 4:
        raise ValueError(f"the DICOM path {info.filename!r} has an unknown layout")
    experiment = 1 if "Experiment 1" in parts[1] else 2 if "Experiment 2" in parts[1] else 0
    if not experiment:
        raise ValueError(f"the DICOM path {info.filename!r} names no experiment")
    return experiment, int(parts[2]), int(parts[3].removesuffix(".dcm"))


def _dicom_location(
    truth: Mapping[tuple[int, int, int], tuple[int, int, int]],
    identity: tuple[int, int, int],
) -> tuple[int, int, int]:
    known = truth.get(identity)
    return (0, -1, -1) if known is None else known


def _dicom_selected(partition: tuple[int, int] | None, identity: tuple[int, int, int]) -> bool:
    if partition is None:
        return True
    experiment, patient, _section = identity
    return (experiment, patient % 4) == partition


def _dicom_entry(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    truth: Mapping[tuple[int, int, int], tuple[int, int, int]],
    identity: tuple[int, int, int],
    index: int,
) -> tuple[int, dict[str, Any]]:
    experiment, patient, section = identity
    which, x, y = _dicom_location(truth, identity)
    with archive.open(info) as held:
        pixels, intercept, slope = _dicom_pixels(held.read())
    return (
        which,
        {
            "image": pixels,
            "experiment": experiment,
            "patient": patient,
            "slice": section,
            "x": x,
            "y": y,
            "intercept": intercept,
            "slope": slope,
            "label": which,
            "index": index,
        },
    )


def _dicom_entries(path: Path, split: str) -> Rows:
    partition = _dicom_partition(split)
    with _nested_zip(path) as archive:
        truth = _dicom_truth(archive)
        images = sorted(
            (info for info in archive.infolist() if info.filename.lower().endswith(".dcm")),
            key=lambda item: item.filename,
        )
        for index, info in enumerate(images):
            identity = _dicom_identity(info)
            if not _dicom_selected(partition, identity):
                continue
            yield _dicom_entry(archive, info, truth, identity, index)


def _dicom(path: Path, split: str) -> Loaded:
    columns: dict[str, Any] = {
        "image": ("h", 512 * 512),
        "experiment": "i",
        "patient": "i",
        "slice": "i",
        "x": "i",
        "y": "i",
        "intercept": "d",
        "slope": "d",
        "label": "i",
        "index": "i",
    }
    return DICOM_LABELS, columns, _dicom_entries(path, split)


def _member_ending(archive: zipfile.ZipFile, ending: str) -> zipfile.ZipInfo:
    # ZIPs made on macOS commonly carry ``__MACOSX/._name`` resource forks.
    # Suffix matching mistakes that metadata for the real data member because
    # ``._name`` also ends in ``name``.  Match the complete basename instead.
    matches = [
        info
        for info in archive.infolist()
        if not info.is_dir() and Path(info.filename).name == ending
    ]
    if len(matches) != 1:
        raise ValueError(f"the archive has {len(matches)} members ending in {ending!r}, not one")
    return matches[0]


@contextmanager
def _nested_archive(path: Path, fragment: str) -> Iterator[zipfile.ZipFile]:
    outer = zipfile.ZipFile(path)
    matches = [
        info
        for info in outer.infolist()
        if info.filename.lower().endswith(".zip") and fragment.lower() in info.filename.lower()
    ]
    if not matches:
        try:
            yield outer
        finally:
            outer.close()
        return
    if len(matches) != 1:
        outer.close()
        raise ValueError(f"the archive has {len(matches)} nested ZIPs matching {fragment!r}")
    try:
        with _extracted(outer, matches[0], path) as nested:
            with zipfile.ZipFile(nested) as inside:
                yield inside
    finally:
        outer.close()


def _pems_entries(path: Path, split: str) -> Rows:
    archive = zipfile.ZipFile(path)
    data = _member_ending(archive, f"PEMS_{split}")
    labels = _member_ending(archive, f"PEMS_{split}labels")
    with archive.open(labels) as held:
        label_cells = held.read().decode("ascii").strip().strip("[]").split()
    try:
        yield from _pems_rows(archive, data, label_cells)
    finally:
        archive.close()


def _pems_rows(archive: zipfile.ZipFile, data: zipfile.ZipInfo, label_cells: list[str]) -> Rows:
    with archive.open(data) as held:
        text = io.TextIOWrapper(held, encoding="ascii", newline="")
        try:
            index = 0
            for physical, line in enumerate(text):
                value = line.strip()
                if not value:
                    continue
                cells = value.strip("[]").replace(";", " ").split()
                if len(cells) != 963 * 144:
                    raise ValueError(f"PEMS row {physical} has {len(cells)} cells")
                if index >= len(label_cells):
                    raise ValueError("PEMS has more matrices than weekday labels")
                label = int(label_cells[index]) - 1
                yield label, {"occupancy": _float32(cells), "label": label, "index": index}
                index += 1
            if index != len(label_cells):
                raise ValueError(f"PEMS has {index} matrices and {len(label_cells)} labels")
        finally:
            text.close()


def _pems(path: Path, split: str) -> Loaded:
    columns: dict[str, Any] = {
        "occupancy": ("f", 963 * 144),
        "label": "i",
        "index": "i",
    }
    return WEEKDAYS, columns, _pems_entries(path, split)


def _puf_entries(path: Path, split: str, width: int) -> Rows:
    archive = zipfile.ZipFile(path)
    partition, family = split.split("_", 1)
    info = _member_ending(archive, f"{partition}_{family}_{width}dim.csv")
    try:
        with archive.open(info) as held:
            for index, cells in enumerate(_csv_rows(held)):
                if len(cells) != width + 1:
                    raise ValueError(f"PUF row {index} has {len(cells)} fields, not {width + 1}")
                response = int(float(cells[-1]))
                if response not in (-1, 1):
                    raise ValueError(f"PUF row {index} has response {response}")
                label = int(response > 0)
                yield (
                    label,
                    {
                        "challenge": array.array("b", (int(float(cell)) for cell in cells[:-1])),
                        "label": label,
                        "index": index,
                    },
                )
    finally:
        archive.close()


def _puf(path: Path, split: str) -> Loaded:
    width = 128 if split.endswith("5xor") else 64
    columns: dict[str, Any] = {"challenge": ("b", width), "label": "i", "index": "q"}
    return ("negative", "positive"), columns, _puf_entries(path, split, width)


def _daily_entries(path: Path) -> Rows:
    archive = zipfile.ZipFile(path)
    pattern = re.compile(r"(?:^|/)a(\d+)/p(\d+)/s(\d+)\.txt$")
    index = 0
    try:
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            matched = pattern.search(info.filename)
            if matched is None:
                continue
            yield _daily_file(archive, info, matched, index)
            index += 1
    finally:
        archive.close()


def _daily_file(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, matched: re.Match[str], index: int
) -> tuple[int, dict[str, Any]]:
    values = array.array("f")
    with archive.open(info) as held:
        for physical, cells in enumerate(_csv_rows(held)):
            if len(cells) != 45:
                raise ValueError(f"{info.filename} row {physical} has {len(cells)} fields")
            values.extend(float(cell) for cell in cells)
    if len(values) != 125 * 45:
        raise ValueError(f"{info.filename} has {len(values)} values, not {125 * 45}")
    label = int(matched.group(1)) - 1
    return label, {
        "readings": values,
        "subject": int(matched.group(2)),
        "segment": int(matched.group(3)),
        "label": label,
        "index": index,
    }


def _daily(path: Path) -> Loaded:
    columns: dict[str, Any] = {
        "readings": ("f", 125 * 45),
        "subject": "i",
        "segment": "i",
        "label": "i",
        "index": "i",
    }
    return DAILY_ACTIVITIES, columns, _daily_entries(path)


def _numeric_lines(
    archive: zipfile.ZipFile, widths: tuple[int, ...]
) -> Iterator[tuple[str, list[str]]]:
    for info in sorted(archive.infolist(), key=lambda item: item.filename):
        if info.is_dir() or not info.filename.lower().endswith((".txt", ".dat", ".csv")):
            continue
        yield from _numeric_file(archive, info, widths)


def _numeric_file(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, widths: tuple[int, ...]
) -> Iterator[tuple[str, list[str]]]:
    with archive.open(info) as held:
        text = io.TextIOWrapper(held, encoding="utf-8-sig", newline="")
        try:
            for line in text:
                cells = line.strip().replace(",", ".").split()
                if len(cells) not in widths:
                    continue
                try:
                    float(cells[0])
                except ValueError:
                    continue
                yield info.filename, cells
        finally:
            text.close()


def _gas_temperature_entries(path: Path) -> Rows:
    archive = zipfile.ZipFile(path)
    try:
        for index, (_, cells) in enumerate(_numeric_lines(archive, (20,))):
            concentration = float(cells[1])
            label = int(concentration > 0)
            yield (
                label,
                {
                    "time": float(cells[0]),
                    "concentration": concentration,
                    "controls": _float32(cells[2:6]),
                    "sensors": _float32(cells[6:20]),
                    "label": label,
                    "index": index,
                },
            )
    finally:
        archive.close()


def _gas_temperature(path: Path) -> Loaded:
    columns: dict[str, Any] = {
        "time": "d",
        "concentration": "f",
        "controls": ("f", 4),
        "sensors": ("f", 14),
        "label": "i",
        "index": "q",
    }
    return ("clean_air", "carbon_monoxide"), columns, _gas_temperature_entries(path)


def _gas_dynamic_entries(path: Path) -> Rows:
    archive = zipfile.ZipFile(path)
    index = 0
    try:
        for filename, cells in _numeric_lines(archive, (19,)):
            label = 0 if "co" in filename.lower() else 1
            yield (
                label,
                {
                    "time": float(cells[0]),
                    "mixture_concentration": float(cells[1]),
                    "ethylene_concentration": float(cells[2]),
                    "sensors": _float32(cells[3:]),
                    "label": label,
                    "index": index,
                },
            )
            index += 1
    finally:
        archive.close()


def _gas_dynamic(path: Path) -> Loaded:
    columns: dict[str, Any] = {
        "time": "d",
        "mixture_concentration": "f",
        "ethylene_concentration": "f",
        "sensors": ("f", 16),
        "label": "i",
        "index": "q",
    }
    return ("carbon_monoxide_mixture", "methane_mixture"), columns, _gas_dynamic_entries(path)


def _twin_gas_entries(path: Path) -> Rows:
    archive = zipfile.ZipFile(path)
    gases = {"gea": 0, "gco": 1, "gey": 2, "gme": 3}
    pattern = re.compile(r"B(\d+)_([A-Za-z]+)_F(\d+)_R(\d+)\.txt$")
    index = 0
    try:
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            matched = pattern.search(info.filename)
            if matched is None:
                continue
            code = matched.group(2).lower()
            if code not in gases:
                raise ValueError(f"{info.filename} names unknown gas {code!r}")
            label = gases[code]
            yield _twin_gas_file(archive, info, matched, label, index)
            index += 1
    finally:
        archive.close()


def _twin_gas_file(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    matched: re.Match[str],
    label: int,
    index: int,
) -> tuple[int, dict[str, Any]]:
    time = array.array("f")
    sensors = array.array("f")
    with archive.open(info) as held:
        text = io.TextIOWrapper(held, encoding="utf-8", newline="")
        try:
            for physical, line in enumerate(text):
                cells = line.split()
                if not cells:
                    continue
                if len(cells) != 9:
                    raise ValueError(f"{info.filename} row {physical} has {len(cells)} fields")
                time.append(float(cells[0]))
                sensors.extend(float(cell) for cell in cells[1:])
        finally:
            text.close()
    return label, {
        "time": _padded(time, 60_001),
        "sensors": _padded(sensors, 8 * 60_001),
        "length": len(time),
        "board": int(matched.group(1)),
        "concentration": int(matched.group(3)),
        "repetition": int(matched.group(4)),
        "label": label,
        "index": index,
    }


def _twin_gas(path: Path) -> Loaded:
    columns: dict[str, Any] = {
        "time": ("f", 60_001),
        "sensors": ("f", 8 * 60_001),
        "length": "i",
        "board": "i",
        "concentration": "i",
        "repetition": "i",
        "label": "i",
        "index": "i",
    }
    return ("ethanol", "carbon_monoxide", "ethylene", "methane"), columns, _twin_gas_entries(path)


def _electricity_entries(path: Path) -> Rows:
    archive = zipfile.ZipFile(path)
    info = _member_ending(archive, "LD2011_2014.txt")
    try:
        yield from _electricity_rows(archive, info)
    finally:
        archive.close()


def _electricity_rows(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> Rows:
    with archive.open(info) as held:
        text = io.TextIOWrapper(held, encoding="utf-8-sig", newline="")
        rows = csv.reader(text, delimiter=";")
        header = next(rows, None)
        if header is None or len(header) != 371:
            raise ValueError("the electricity table does not have 370 client columns")
        try:
            for index, cells in enumerate(rows):
                if len(cells) != 371:
                    raise ValueError(f"electricity row {index} has {len(cells)} fields")
                timestamp = cells[0].encode("ascii")
                yield (
                    0,
                    {
                        "timestamp": timestamp + bytes(19 - len(timestamp)),
                        "loads": _float32(cell.replace(",", ".") for cell in cells[1:]),
                        "index": index,
                    },
                )
        finally:
            text.close()


def _electricity(path: Path) -> Loaded:
    columns: dict[str, Any] = {"timestamp": ("B", 19), "loads": ("f", 370), "index": "q"}
    return ("rows",), columns, _electricity_entries(path)


def _opportunity_entries(path: Path) -> Rows:
    codes = {0: 0, 1: 1, 2: 2, 4: 3, 5: 4}
    with _nested_archive(path, "dataset") as archive:
        index = 0
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            if not info.filename.lower().endswith(".dat"):
                continue
            for row in _opportunity_file(archive, info, codes, index):
                yield row
                index += 1


def _opportunity_file(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, codes: dict[int, int], first: int
) -> Rows:
    with archive.open(info) as held:
        text = io.TextIOWrapper(held, encoding="ascii", newline="")
        try:
            for physical, line in enumerate(text):
                cells = line.split()
                if not cells:
                    continue
                if len(cells) < 244:
                    raise ValueError(f"{info.filename} row {physical} is too short")
                raw_label = int(float(cells[243]))
                if raw_label not in codes:
                    raise ValueError(f"unknown OPPORTUNITY locomotion code {raw_label}")
                label = codes[raw_label]
                yield label, {"features": _float32(cells[:243]), "label": label, "index": first}
                first += 1
        finally:
            text.close()


def _opportunity(path: Path) -> Loaded:
    columns: dict[str, Any] = {"features": ("f", 243), "label": "i", "index": "q"}
    return ("unlabelled", "stand", "walk", "sit", "lie"), columns, _opportunity_entries(path)


def _p53_table(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    """The complete table, whose current archive name drifted from K8 to K9."""
    members = {
        Path(info.filename).name.lower(): info
        for info in archive.infolist()
        if not info.is_dir()
    }
    for name in ("k9.data", "k8.data"):
        if name in members:
            return members[name]
    raise ValueError("the p53 archive has no complete K9 or legacy K8 table")


def _p53_entries(path: Path) -> Rows:
    with _nested_archive(path, "new_2012") as archive:
        with archive.open(_p53_table(archive)) as held:
            yield from _p53_rows(held)


def _p53_rows(held: Any) -> Rows:
    for index, cells in enumerate(_csv_rows(held)):
        if len(cells) == 5_410 and cells[-1] == "":
            cells.pop()
        if len(cells) != 5_409:
            raise ValueError(f"p53 row {index} has {len(cells)} fields, not 5409")
        word = cells[-1].strip().lower()
        if word not in ("inactive", "active"):
            raise ValueError(f"p53 row {index} has unknown class {word!r}")
        label = int(word == "active")
        yield (
            label,
            {
                "features": _float32(nan if cell == "?" else cell for cell in cells[:-1]),
                "label": label,
                "index": index,
            },
        )


def _p53(path: Path) -> Loaded:
    columns: dict[str, Any] = {"features": ("f", 5_408), "label": "i", "index": "i"}
    return ("inactive", "active"), columns, _p53_entries(path)


def _pamap_entries(path: Path) -> Rows:
    codes = {int(name.removeprefix("activity_")): at for at, name in enumerate(PAMAP_ACTIVITIES)}
    with _nested_archive(path, "dataset") as archive:
        index = 0
        pattern = re.compile(r"subject(\d+)(?:_(\w+))?\.dat$", re.IGNORECASE)
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            matched = pattern.search(info.filename)
            if matched is None:
                continue
            for row in _pamap_file(archive, info, matched, codes, index):
                yield row
                index += 1


def _pamap_file(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    matched: re.Match[str],
    codes: dict[int, int],
    first: int,
) -> Rows:
    with archive.open(info) as held:
        text = io.TextIOWrapper(held, encoding="ascii", newline="")
        try:
            for physical, line in enumerate(text):
                cells = line.split()
                if len(cells) != 54:
                    raise ValueError(f"{info.filename} row {physical} has {len(cells)} fields")
                activity = int(cells[1])
                if activity not in codes:
                    raise ValueError(f"unknown PAMAP2 activity {activity}")
                label = codes[activity]
                yield (
                    label,
                    {
                        "timestamp": float(cells[0]),
                        "features": _float32(cells[2:]),
                        "subject": int(matched.group(1)),
                        "optional": int(bool(matched.group(2))),
                        "label": label,
                        "index": first,
                    },
                )
                first += 1
        finally:
            text.close()


def _pamap(path: Path) -> Loaded:
    columns: dict[str, Any] = {
        "timestamp": "d",
        "features": ("f", 52),
        "subject": "i",
        "optional": "i",
        "label": "i",
        "index": "q",
    }
    return PAMAP_ACTIVITIES, columns, _pamap_entries(path)


def _hhar_entries(path: Path) -> Rows:
    import zlib

    labels = {name: at for at, name in enumerate(HHAR_ACTIVITIES)}
    with _nested_archive(path, "activity recognition") as archive:
        index = 0
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            lower = info.filename.lower()
            if not lower.endswith(".csv") or not any(
                word in lower for word in ("accelerometer", "gyroscope")
            ):
                continue
            sensor = int("gyroscope" in lower)
            platform = int("watch" in lower)
            with archive.open(info) as held:
                text = io.TextIOWrapper(held, encoding="utf-8-sig", newline="")
                rows = csv.DictReader(text)
                for physical, row in enumerate(rows):
                    word = (
                        (row.get("gt") or "null")
                        .strip()
                        .lower()
                        .replace("stairsup", "stairs_up")
                        .replace("stairsdown", "stairs_down")
                    )
                    word = "unlabelled" if word in ("", "null") else word
                    if word not in labels:
                        raise ValueError(f"{info.filename} row {physical} has activity {word!r}")
                    label = labels[word]

                    def encoded(value: Any) -> int:
                        return zlib.crc32((value or "").encode("utf-8"))

                    yield (
                        label,
                        {
                            "arrival_time": int(float(row["Arrival_Time"])),
                            "creation_time": int(float(row["Creation_Time"])),
                            "xyz": _float32((row["x"], row["y"], row["z"])),
                            "user": encoded(row.get("User")),
                            "model": encoded(row.get("Model")),
                            "device": encoded(row.get("Device")),
                            "sensor": sensor,
                            "platform": platform,
                            "label": label,
                            "index": index,
                        },
                    )
                    index += 1


def _hhar(path: Path) -> Loaded:
    columns: dict[str, Any] = {
        "arrival_time": "q",
        "creation_time": "q",
        "xyz": ("f", 3),
        "user": "Q",
        "model": "Q",
        "device": "Q",
        "sensor": "i",
        "platform": "i",
        "label": "i",
        "index": "q",
    }
    return HHAR_ACTIVITIES, columns, _hhar_entries(path)


def _year_prediction_entries(path: Path, split: str) -> Rows:
    start, stop = (0, 463_715) if split == "train" else (463_715, 515_345)
    archive = zipfile.ZipFile(path)
    try:
        members = [
            info
            for info in archive.infolist()
            if not info.is_dir() and Path(info.filename).name == "YearPredictionMSD.txt"
        ]
        if len(members) != 1:
            raise ValueError(f"Year Prediction MSD holds {len(members)} data tables, not one")
        with archive.open(members[0]) as held:
            for index, cells in enumerate(_csv_rows(held)):
                if index >= stop:
                    break
                if index < start:
                    continue
                if len(cells) != 91:
                    raise ValueError(
                        f"Year Prediction MSD row {index} has {len(cells)} fields, not 91"
                    )
                year = float(cells[0])
                if not year.is_integer() or not 1922 <= year <= 2011:
                    raise ValueError(
                        f"Year Prediction MSD row {index} has invalid year {cells[0]!r}"
                    )
                yield (
                    0,
                    {
                        "features": _float32(cells[1:]),
                        "target": year,
                        "index": index,
                    },
                )
    finally:
        archive.close()


def _year_prediction(path: Path, split: str) -> Loaded:
    columns: dict[str, Any] = {
        "features": ("f", 90),
        "target": "f",
        "index": "i",
    }
    return ("rows",), columns, _year_prediction_entries(path, split)


def load(converter: str, path: Path, split: str) -> Loaded:
    """Open one registered large format and return its classes, columns and rows."""
    simple = _SIMPLE_CONVERTERS.get(converter)
    if simple is not None:
        return simple(path)
    divided = _SPLIT_CONVERTERS.get(converter)
    if divided is not None:
        return divided(path, split)
    if converter == "susy":
        return _collision(path, split, name="SUSY", total=5_000_000, width=18)
    if converter == "higgs":
        return _collision(path, split, name="HIGGS", total=11_000_000, width=28)
    raise ValueError(f"there is no large UCI converter {converter!r}")


_SIMPLE_CONVERTERS: dict[str, Callable[[Path], Loaded]] = {
    "cuffless": _cuffless,
    "chipseq": _chipseq,
    "humanitarian": _humanitarian,
    "ppg": _ppg,
    "daily": _daily,
    "gas_temperature": _gas_temperature,
    "twin_gas": _twin_gas,
    "electricity": _electricity,
    "opportunity": _opportunity,
    "gas_dynamic": _gas_dynamic,
    "p53": _p53,
    "pamap2": _pamap,
    "hhar": _hhar,
}

_SPLIT_CONVERTERS: dict[str, Callable[[Path, str], Loaded]] = {
    "realdisp": _realdisp,
    "gas": _gas,
    "hepmass": _hepmass,
    "pems": _pems,
    "puf": _puf,
    "year_prediction": _year_prediction,
    "dicom": _dicom,
}
