"""Bounded-memory readers for large open archives outside UCI.

Every source in this module has an authoritative landing page, explicit
redistribution terms and a complete compressed payload below two gigabytes.
Readers retain the source on disk and yield one event, document or recording
at a time; none of the archives is loaded wholesale into memory.
"""

from __future__ import annotations

import array
import ast
import csv
import importlib
import io
import itertools
import json
import math
import re
import shutil
import tarfile
import tempfile
import wave
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from ._alex_mp20 import ALEX_MP20
from ._condensed_matter import CONDENSED_MATTER
from ._jarvis_physics import JARVIS_PHYSICS
from ._physics_vision import PHYSICS_VISION
from ._the_well import THE_WELL

Rows = Iterator[tuple[int, dict[str, Any]]]
Loaded = tuple[tuple[str, ...], dict[str, Any], Rows]

JETNET_CLASSES = ("gluon", "light_quark", "top", "w_boson", "z_boson")
JETNET_ROLES = ("gluon", "light_quark", "top", "w_boson", "z_boson")
TINYSOL_INSTRUMENTS = (
    "accordion",
    "alto_saxophone",
    "bass_tuba",
    "bassoon",
    "cello",
    "clarinet",
    "contrabass",
    "flute",
    "french_horn",
    "oboe",
    "trombone",
    "trumpet",
    "viola",
    "violin",
)
TINYSOL_CODES = {
    "Acc": "accordion",
    "ASax": "alto_saxophone",
    "BTb": "bass_tuba",
    "Bn": "bassoon",
    "Bsn": "bassoon",
    "Vc": "cello",
    "ClBb": "clarinet",
    "Cb": "contrabass",
    "Fl": "flute",
    "Hn": "french_horn",
    "Ob": "oboe",
    "Tbn": "trombone",
    "TpC": "trumpet",
    "Va": "viola",
    "Vn": "violin",
}
SPEECH_COMMAND_CLASSES = (
    "bed",
    "bird",
    "cat",
    "dog",
    "down",
    "eight",
    "five",
    "four",
    "go",
    "happy",
    "house",
    "left",
    "marvin",
    "nine",
    "no",
    "off",
    "on",
    "one",
    "right",
    "seven",
    "sheila",
    "six",
    "stop",
    "three",
    "tree",
    "two",
    "up",
    "wow",
    "yes",
    "zero",
    "background_noise",
)
REEFSET_CLASSES = (
    "ambient",
    "anthrop_boat_engine",
    "anthrop_bomb",
    "anthrop_mechanical",
    "bioph",
    "bioph_cascading_saw",
    "bioph_chatter",
    "bioph_chorus",
    "bioph_crackle",
    "bioph_croak",
    "bioph_damselfish",
    "bioph_dolphin",
    "bioph_double_pulse",
    "bioph_echinidae",
    "bioph_epigut",
    "bioph_grazing",
    "bioph_grouper_a",
    "bioph_grouper_groan",
    "bioph_growl",
    "bioph_holocentrus",
    "bioph_knock",
    "bioph_knock_croak_a",
    "bioph_knock_croak_b",
    "bioph_knock_croak_c",
    "bioph_low_growl",
    "bioph_megnov",
    "bioph_midshipman",
    "bioph_mycbon",
    "bioph_pomamb",
    "bioph_pulse",
    "bioph_rattle",
    "bioph_rattle_response",
    "bioph_series_a",
    "bioph_series_b",
    "bioph_stridulation",
    "bioph_whup",
    "geoph_waves",
)
BIRDSET_CLASSES = (
    "akepa1",
    "amecro",
    "amepip",
    "amered",
    "amerob",
    "amgplo",
    "apapan",
    "babwar",
    "barpet",
    "bawwar",
    "bkcchi",
    "blujay",
    "bnhcow",
    "btnwar",
    "buggna",
    "buhvir",
    "buwwar",
    "calqua",
    "cangoo",
    "carwre",
    "casfin",
    "chswar",
    "chukar",
    "clanut",
    "comwax",
    "comyel",
    "daejun",
    "dowwoo",
    "dusfly",
    "eastow",
    "eawpew",
    "elepai",
    "ercfra",
    "foxspa",
    "gcrfin",
    "haiwoo",
    "hawama",
    "hawcre",
    "hawhaw",
    "hawpet1",
    "herthr",
    "hoowar",
    "houfin",
    "iiwi",
    "jabwar",
    "kalphe",
    "kenwar",
    "louwat",
    "mallar3",
    "melthr",
    "moublu",
    "mouchi",
    "naswar",
    "norcar",
    "norfli",
    "omao",
    "orcwar",
    "ovenbi1",
    "palila",
    "reblei",
    "rebwoo",
    "reevir1",
    "robgro",
    "rocwre",
    "ruckin",
    "scatan",
    "skylar",
    "sposan",
    "swathr",
    "tuftit",
    "veery",
    "warvir",
    "warwhe1",
    "whbnut",
    "whcspa",
    "wiltur",
    "woothr",
    "yebcuc",
    "yefcan",
    "yerwar",
)
SOD_BANDS = ("near_0_to_0_5_km", "mid_0_5_to_2_km", "far_2_to_5_km")
SOD_SOURCE_BANDS = ("0km-0.5km", "0.5km-2km", "2km-5km")
SOD_WIDTH = 794
SOD_HEIGHT = 706
SOD_MAX_OBJECTS = 16
ALLSKY_CLASSES = (
    "camera_mask",
    "sky",
    "low_layer_clouds",
    "mid_layer_clouds",
    "high_layer_clouds",
)
ALLSKY_CAMERAS = (
    "Cloud_Cam_Kontas",
    "Cloud_Cam_Metas",
    "Cloud_Cam_PVot_Q71",
    "Cloud_Cam_PVotSky",
)
ALLSKY_CAMERA_METADATA = (
    (37.0952, -2.3548, 507),
    (37.0916, -2.3636, 507),
    (37.0941, -2.3548, 507),
    (37.0941, -2.3548, 507),
)
ALLSKY_SIDE = 512


OPEN_LARGE: tuple[dict[str, Any], ...] = (
    {
        "name": "jetnet",
        "label": "JetNet",
        "title": "872,000 simulated particle jets in five physics classes",
        "licence": "CC BY 4.0",
        "source": "https://zenodo.org/records/6975118",
        "source_bytes": 436_490_240,
        "sources": {
            "gluon": "https://zenodo.org/api/records/6975118/files/g.hdf5/content",
            "light_quark": "https://zenodo.org/api/records/6975118/files/q.hdf5/content",
            "top": "https://zenodo.org/api/records/6975118/files/t.hdf5/content",
            "w_boson": "https://zenodo.org/api/records/6975118/files/w.hdf5/content",
            "z_boson": "https://zenodo.org/api/records/6975118/files/z.hdf5/content",
        },
        "source_sizes": {
            "gluon": 87_919_040,
            "light_quark": 84_658_832,
            "top": 88_262_768,
            "w_boson": 87_879_360,
            "z_boson": 87_770_240,
        },
        "converter": "open:jetnet",
        "transformation": (
            "read the five publisher HDF5 shards without scaling; retained each jet's four "
            "features and 30 padded constituent four-vectors with their masks; wrote one ROOT "
            "TTree per particle-jet class"
        ),
        "classes": JETNET_CLASSES,
        "requires": ("h5py", "numpy"),
        "modality": "particle physics",
        "task": "multiclass classification and generative modelling",
    },
    {
        "name": "omnifold_big",
        "label": "OmniFold Big",
        "title": "generator- and detector-level Z+jet events for unfolding",
        "licence": "CC BY 4.0",
        "source": "https://zenodo.org/records/10633065",
        "url": "https://zenodo.org/api/records/10633065/files/OmniFold_Big.zip/content",
        "source_bytes": 817_938_159,
        "converter": "open:omnifold",
        "transformation": (
            "streamed all 201 numbered publisher event shards and their truth and reconstructed "
            "event lines, retained jet "
            "kinematics, width, tau2, soft-drop mass, zg and multiplicity without scaling, "
            "mapped NaN zg to zero as the publisher notebook does, and wrote one ROOT TTree "
            "per simulation level"
        ),
        "classes": ("generator", "detector"),
        "modality": "particle physics",
        "task": "domain classification and unfolding",
    },
    {
        "name": "tinysol",
        "label": "TinySOL",
        "title": "2,913 isolated orchestral notes played by 14 instruments",
        "licence": "CC BY 4.0",
        "source": "https://zenodo.org/records/3685331",
        "url": "https://zenodo.org/api/records/3685331/files/TinySOL.tar.gz/content",
        "source_bytes": 1_026_941_458,
        "converter": "open:tinysol",
        "transformation": (
            "decoded each published PCM WAV without resampling; normalized signed 16-bit "
            "samples to float32 in [-1, 1), retained up to ten seconds of mono audio, "
            "zero-padded only the unused tail and recorded its true length and sample rate; "
            "wrote one ROOT TTree per instrument"
        ),
        "classes": TINYSOL_INSTRUMENTS,
        "modality": "audio",
        "task": "instrument classification",
        "creators": (
            "Carmine-Emanuele Cella",
            "Daniele Ghisi",
            "Vincent Lostanlen",
            "Fabien Lévy",
            "Joshua Fineberg",
            "Yan Maresz",
        ),
        "publisher": "TinySOL project",
        "origin": "https://doi.org/10.5281/zenodo.3685331",
        "repository": "Zenodo",
        "citation": "https://doi.org/10.5281/zenodo.3685331",
    },
    {
        "name": "speech_commands_v001",
        "label": "Speech Commands v0.01",
        "title": "64,727 one-second spoken commands in 30 words plus background noise",
        "licence": "CC BY 4.0",
        "source": "https://research.google/blog/launching-the-speech-commands-dataset/",
        "url": "http://download.tensorflow.org/data/speech_commands_v0.01.tar.gz",
        "source_bytes": 1_489_096_277,
        "converter": "open:speech_commands",
        "transformation": (
            "decoded the versioned 16 kHz mono WAV PCM and normalized signed 16-bit samples "
            "to float32 in [-1, 1) without resampling; zero-padded short utterances to "
            "16,000 values, retained speaker hash, utterance number and official "
            "train/validation/test lists, and divided the six long augmentation-noise tracks "
            "into non-overlapping one-second training rows"
        ),
        "classes": SPEECH_COMMAND_CLASSES,
        "splits": ("train", "validation", "test"),
        "basket_size": 4 * 1024 * 1024,
        "modality": "audio",
        "task": "keyword spotting and spoken-command classification",
        "creators": ("Pete Warden",),
        "publisher": "Google Speech Research",
        "origin": "https://research.google/blog/launching-the-speech-commands-dataset/",
        "repository": "Google TensorFlow dataset archive",
        "mirrors": (
            (
                "Hugging Face google/speech_commands",
                "https://huggingface.co/datasets/google/speech_commands",
            ),
        ),
        "citation": "https://arxiv.org/abs/1804.03209",
    },
    {
        "name": "audiomnist",
        "label": "AudioMNIST",
        "title": "30,000 spoken digits from 60 speakers with demographic metadata",
        "licence": "MIT",
        "source": "https://huggingface.co/datasets/flexthink/audiomnist",
        "source_bytes": 1_907_031_297,
        "sources": {
            "train": "https://huggingface.co/datasets/flexthink/audiomnist/resolve/00f96ff552610ab5f3c15d3a0881dda2a14a3935/dataset/train.tar.gz?download=true",
            "validation": "https://huggingface.co/datasets/flexthink/audiomnist/resolve/00f96ff552610ab5f3c15d3a0881dda2a14a3935/dataset/valid.tar.gz?download=true",
            "test": "https://huggingface.co/datasets/flexthink/audiomnist/resolve/00f96ff552610ab5f3c15d3a0881dda2a14a3935/dataset/test.tar.gz?download=true",
            "metadata": "https://huggingface.co/datasets/flexthink/audiomnist/resolve/00f96ff552610ab5f3c15d3a0881dda2a14a3935/meta/audioMNIST_meta.json?download=true",
        },
        "source_sizes": {
            "train": 1_811_968_000,
            "validation": 47_523_840,
            "test": 47_523_840,
            "metadata": 15_617,
        },
        "converter": "open:audiomnist",
        "transformation": (
            "streamed the fixed Hugging Face adaptation of the authors' WAV collection; "
            "decoded 48 kHz mono PCM to float32 in [-1, 1), zero-padded clips to 48,000 "
            "values without resampling, retained digit, speaker, repetition, age, sex, "
            "accent, native-speaker and geographic-origin metadata, and preserved the "
            "mirror-defined train/validation/test split"
        ),
        "classes": tuple(str(digit) for digit in range(10)),
        "splits": ("train", "validation", "test"),
        "basket_size": 4 * 1024 * 1024,
        "modality": "audio",
        "task": "spoken digit, speaker-demographic, and explainability classification",
        "creators": (
            "Sören Becker",
            "Johanna Vielhaben",
            "Marcel Ackermann",
            "Klaus-Robert Müller",
            "Sebastian Lapuschkin",
            "Wojciech Samek",
        ),
        "publisher": "AudioMNIST project",
        "origin": "https://github.com/soerenab/AudioMNIST",
        "repository": "Hugging Face Hub",
        "mirrors": (
            (
                "Hugging Face flexthink adaptation",
                "https://huggingface.co/datasets/flexthink/audiomnist",
            ),
        ),
        "citation": "https://doi.org/10.1016/j.jfranklin.2023.11.038",
    },
    {
        "name": "circor_heart_sound",
        "label": "CirCor DigiScope Phonocardiogram v1.0.3",
        "title": "5,272 pediatric heart-sound recordings with murmur and outcome labels",
        "licence": "ODC-By-1.0",
        "source": "https://physionet.org/content/circor-heart-sound/1.0.3/",
        "url": "https://physionet.org/content/circor-heart-sound/get-zip/1.0.3/",
        "source_bytes": 471_283_852,
        "converter": "open:circor",
        "transformation": (
            "joined each published WAV to the subject CSV; decoded 4 kHz mono PCM to "
            "float32 in [-1, 1) without filtering or resampling and divided variable-length "
            "recordings into non-overlapping five-second windows, zero-padding only the last "
            "window; retained patient, auscultation location, murmur, clinical outcome and "
            "demographic fields, while leaving the separate beat-boundary TSV annotations "
            "at the canonical origin"
        ),
        "classes": ("murmur_absent", "murmur_present", "murmur_unknown"),
        "basket_size": 4 * 1024 * 1024,
        "modality": "audio waveform",
        "task": "heart-murmur and clinical-outcome classification",
        "creators": (
            "Jorge Oliveira",
            "Francesco Renna",
            "Paulo Costa",
            "Marcelo Nogueira",
            "Ana Cristina Oliveira",
            "Andoni Elola",
            "Carlos Ferreira",
            "Alipio Jorge",
            "Ali Bahrami Rad",
            "Matthew Reyna",
            "Reza Sameni",
            "Gari Clifford",
            "Miguel Coimbra",
        ),
        "publisher": "PhysioNet",
        "origin": "https://doi.org/10.13026/tshs-mw03",
        "repository": "PhysioNet",
        "citation": "https://doi.org/10.13026/tshs-mw03",
    },
    {
        "name": "wikitext_103",
        "label": "WikiText-103",
        "title": "103 million word tokens from 28,475 verified Wikipedia articles",
        "licence": "CC BY-SA 3.0",
        "source": "https://huggingface.co/datasets/Salesforce/wikitext",
        "source_bytes": 313_093_838,
        "sources": {
            "train_0": (
                "https://huggingface.co/datasets/Salesforce/wikitext/resolve/main/"
                "wikitext-103-v1/train-00000-of-00002.parquet?download=true"
            ),
            "train_1": (
                "https://huggingface.co/datasets/Salesforce/wikitext/resolve/main/"
                "wikitext-103-v1/train-00001-of-00002.parquet?download=true"
            ),
            "validation": (
                "https://huggingface.co/datasets/Salesforce/wikitext/resolve/main/"
                "wikitext-103-v1/validation-00000-of-00001.parquet?download=true"
            ),
            "test": (
                "https://huggingface.co/datasets/Salesforce/wikitext/resolve/main/"
                "wikitext-103-v1/test-00000-of-00001.parquet?download=true"
            ),
        },
        "source_sizes": {
            "train_0": 155_788_327,
            "train_1": 155_928_670,
            "validation": 655_106,
            "test": 721_735,
        },
        "converter": "open:wikitext",
        "transformation": (
            "streamed Salesforce's maintained Parquet train, validation and test shards, "
            "preserved non-empty pre-tokenized text rows as UTF-8 and encoded each in a fixed "
            "32 KiB byte column with its true byte and token lengths"
        ),
        "classes": (),
        "splits": ("train", "validation", "test"),
        "requires": ("pyarrow",),
        "modality": "text",
        "task": "language modelling",
    },
    {
        "name": "reefset",
        "label": "ReefSet v1.0",
        "title": "57,084 labelled tropical-reef sound clips in 37 classes",
        "licence": "CC BY 4.0",
        "source": "https://zenodo.org/records/11060189",
        "url": "https://zenodo.org/api/records/11060189/files/ReefSet_v1.0.zip/content",
        "source_bytes": 1_628_719_176,
        "converter": "open:reefset",
        "transformation": (
            "decoded each published mono PCM WAV without resampling; normalized its declared "
            "8-, 16-, 24- or 32-bit integer samples to float32 in [-1, 1), retained 30,720 "
            "values, true length and sample rate, "
            "joined the publisher's JSON label, dataset, recorder and data-sharer "
            "provenance, and wrote one ROOT TTree per acoustic class"
        ),
        "classes": REEFSET_CLASSES,
        "modality": "audio",
        "task": "underwater acoustic classification and transfer learning",
        "creators": ("Ben Williams",),
        "publisher": "ReefSet project",
        "origin": "https://doi.org/10.5281/zenodo.11060189",
        "repository": "Zenodo",
        "citation": "https://doi.org/10.5281/zenodo.11060189",
    },
    {
        "name": "biodcase_2025_task3",
        "label": "BioDCASE 2025 Task 3",
        "title": "4,718 compact bioacoustic clips for Yellowhammer detection",
        "licence": "CC BY 4.0",
        "source": "https://zenodo.org/records/15228365",
        "url": "https://zenodo.org/api/records/15228365/files/Development_Set.zip/content",
        "source_bytes": 224_706_547,
        "converter": "open:biodcase",
        "transformation": (
            "decoded each published mono PCM WAV without resampling; normalized signed "
            "16-bit samples to float32 in [-1, 1), retained 32,000 values, true length "
            "and sample rate, "
            "derived the binary class and song, environment, distance and negative-kind "
            "metadata from the publisher's folders and filenames, and preserved the "
            "official training and validation split"
        ),
        "classes": ("negative", "yellowhammer"),
        "splits": ("train", "validation"),
        "modality": "audio",
        "task": "binary bioacoustic detection on constrained hardware",
        "creators": ("Ilaria Morandi", "Pavel Linhart", "Minkyung Kwak", "Tereza Petrusková"),
        "publisher": "BioDCASE",
        "origin": "https://doi.org/10.5281/zenodo.15228365",
        "repository": "Zenodo",
        "citation": "https://doi.org/10.5281/zenodo.15228365",
    },
    {
        "name": "birdset_baseal",
        "label": "BirdSet BASEAL",
        "title": "37,010 Perch-v2 bird-call embeddings with 80 multilabel targets",
        "licence": "CC BY 4.0 or CC0, per source recording",
        "source": "https://zenodo.org/records/19340660",
        "url": "https://zenodo.org/api/records/19340660/files/BirdSet_BASEAL.zip/content",
        "source_bytes": 225_207_567,
        "converter": "open:birdset",
        "transformation": (
            "safely decoded every publisher NumPy float32 embedding without pickle, scaling "
            "or truncation; joined labels and recording metadata by filename, mapped the "
            "semicolon-delimited bird codes to an 80-wide multilabel vector in lexicographic "
            "order ("
            + ", ".join(BIRDSET_CLASSES)
            + "); retained per-record source and licence provenance, and preserved the "
            "official training and validation split"
        ),
        "classes": (),
        "splits": ("train", "validation"),
        "modality": "audio embeddings",
        "task": "multilabel bird-call classification and domain adaptation",
    },
    {
        "name": "uav_maize_stress",
        "label": "UAV Maize Stress",
        "title": ("1,070 six-band maize patches, semantic masks and two source orthomosaics"),
        "licence": "CC BY 4.0",
        "source": "https://zenodo.org/records/20332029",
        "source_bytes": 1_180_895_747,
        "sources": {
            "readme": (
                "https://zenodo.org/api/records/20332029/files/"
                "00_README_CITATION_LICENSE.zip/content"
            ),
            "orthomosaics": (
                "https://zenodo.org/api/records/20332029/files/01_orthomosaics.zip/content"
            ),
            "patches": (
                "https://zenodo.org/api/records/20332029/files/02_processed_patches.zip/content"
            ),
            "metadata": ("https://zenodo.org/api/records/20332029/files/03_metadata.zip/content"),
        },
        "source_sizes": {
            "readme": 5_369,
            "orthomosaics": 815_709_770,
            "patches": 365_173_471,
            "metadata": 7_137,
        },
        "converter": "open:maize",
        "transformation": (
            "joined the publisher's four ZIP shards; decoded the two six-band float32 "
            "GeoTIFF orthomosaics through disk-backed arrays and tiled them into padded "
            "224 by 224 examples; safely decoded all 1,070 six-band float16 NumPy patches "
            "without pickle and cast them to float32; joined each patch to its 8-bit "
            "five-class semantic mask and spatial metadata; retained nodata, dimensions, "
            "coordinates and class ratios; and wrote separate orthomosaic_water, "
            "orthomosaic_rust, patch_water and patch_rust TTrees"
        ),
        "classes": (),
        "requires": ("numpy", "PIL", "tifffile", "imagecodecs"),
        "modality": "multispectral geospatial imagery",
        "task": "semantic segmentation and crop-stress mapping",
        "basket_size": 2 * 1024 * 1024,
        "layout": "four TTrees separated by source/patch stage and water/rust task",
    },
    {
        "name": "wildlife_mnist",
        "label": "Wildlife MNIST",
        "title": "120,000 textured RGB digits with disentangled visual-factor labels",
        "licence": "CC BY 4.0",
        "source": "https://zenodo.org/records/7602025",
        "source_bytes": 1_477_801_037,
        "sources": {
            "train_images": ("https://zenodo.org/api/records/7602025/files/data.npy/content"),
            "train_labels": ("https://zenodo.org/api/records/7602025/files/labels.npy/content"),
            "test_images": ("https://zenodo.org/api/records/7602025/files/data_test.npy/content"),
            "test_labels": ("https://zenodo.org/api/records/7602025/files/labels_test.npy/content"),
            "preview_train": ("https://zenodo.org/api/records/7602025/files/train.png/content"),
            "preview_digit": (
                "https://zenodo.org/api/records/7602025/files/test_digit.png/content"
            ),
            "preview_background": (
                "https://zenodo.org/api/records/7602025/files/test_background.png/content"
            ),
            "preview_texture": (
                "https://zenodo.org/api/records/7602025/files/test_texture.png/content"
            ),
        },
        "source_sizes": {
            "train_images": 737_280_128,
            "train_labels": 480_128,
            "test_images": 737_280_128,
            "test_labels": 1_440_128,
            "preview_train": 323_391,
            "preview_digit": 336_310,
            "preview_background": 323_167,
            "preview_texture": 337_657,
        },
        "converter": "open:wildlife_mnist",
        "transformation": (
            "memory-mapped the publisher's NumPy arrays without pickle and preserved every "
            "channel-first 3 by 32 by 32 float32 image at its published [-1, 1] scale; "
            "preserved the official non-mixed training and independently mixed test split; "
            "expanded the training label shared by digit, background and foreground into "
            "those three named branches and retained the test label triples as published; "
            "wrote one ROOT TTree per digit and split; omitted only the four display-only "
            "PNG montage previews"
        ),
        "classes": tuple(str(value) for value in range(10)),
        "splits": ("train", "test"),
        "requires": ("numpy",),
        "modality": "synthetic RGB imagery",
        "task": "classification, disentanglement and factor identification",
    },
    {
        "name": "sodv2",
        "label": "Satellite/Space Object Detection v2",
        "title": "600 rendered orbital scenes with 1,339 satellite bounding boxes",
        "licence": "CC BY 4.0",
        "source": "https://github.com/AEL-Lab/satellite-object-detection-dataset-v2",
        "url": (
            "https://codeload.github.com/AEL-Lab/satellite-object-detection-dataset-v2/zip/"
            "df6573b8fdaa9ba2d90c9cc81689aaedc82b26df"
        ),
        "source_bytes": 14_516_386,
        "converter": "open:sodv2",
        "transformation": (
            "decoded every published 794 by 706 JPEG into channel-last RGB uint8 values; "
            "joined its YOLO annotation, retained up to the published maximum of 16 "
            "satellites as zero-padded normalized centre-x, centre-y, width and height "
            "boxes with a true object count; preserved the official train/validation split "
            "and wrote separate TTrees for the 0-0.5 km, 0.5-2 km and 2-5 km strata"
        ),
        "classes": ("satellite",),
        "splits": ("train", "validation"),
        "requires": ("PIL",),
        "modality": "synthetic space imagery",
        "task": "satellite bounding-box detection across observation distances",
        "basket_size": 4 * 1024 * 1024,
        "layout": "three distance-stratum TTrees per publisher split",
        "creators": ("Wenxuan Zhang", "Peng Hu"),
        "publisher": (
            "AEL Lab, Department of Electrical and Computer Engineering, University of Manitoba"
        ),
        "origin": "https://github.com/AEL-Lab/satellite-object-detection-dataset-v2",
        "repository": "AEL Lab GitHub repository",
        "citation": "https://arxiv.org/abs/2505.01650",
    },
    {
        "name": "allsky_cloud_segmentation",
        "label": "All-Sky Cloud Segmentation Almeria",
        "title": "818 solar-research sky images with manually refined pixel masks",
        "licence": "CC BY 4.0",
        "source": "https://zenodo.org/records/16647156",
        "source_bytes": 16_828_503,
        "sources": {
            "train_archive": (
                "https://zenodo.org/api/records/16647156/files/kontas_2017.zip/content"
            ),
            "test_archive": ("https://zenodo.org/api/records/16647156/files/test_set.zip/content"),
            "classes": "https://zenodo.org/api/records/16647156/files/classes.yaml/content",
            "metadata": ("https://zenodo.org/api/records/16647156/files/meta_data.yaml/content"),
            "notebook": (
                "https://zenodo.org/api/records/16647156/files/"
                "image_mask_visualization.ipynb/content"
            ),
        },
        "source_sizes": {
            "train_archive": 15_626_097,
            "test_archive": 1_198_235,
            "classes": 86,
            "metadata": 502,
            "notebook": 3_583,
        },
        "converter": "open:allsky_clouds",
        "transformation": (
            "joined all 818 published 512 by 512 JPEG/PNG pairs; decoded images into "
            "channel-last RGB uint8 values and masks into unchanged uint8 class ids 0-4; "
            "retained per-mask class fractions, camera identity and published camera "
            "coordinates; preserved the 616 training, 154 validation and 48 independent "
            "test examples, including all four test imagers"
        ),
        "classes": ALLSKY_CLASSES,
        "splits": ("train", "validation", "test"),
        "requires": ("PIL",),
        "modality": "atmospheric and solar-research imagery",
        "task": "five-class semantic cloud segmentation",
        "basket_size": 2 * 1024 * 1024,
        "layout": "one samples TTree per publisher split with a five-class pixel mask",
        "creators": (
            "Yann Fabel",
            "David Magiera",
            "Bijan Nouri",
            "Niklas Blum",
            "Luis F. Zarzalejo",
        ),
        "publisher": "German Aerospace Center (DLR) Institute of Solar Research",
        "origin": "https://doi.org/10.5281/zenodo.16647156",
        "repository": "Zenodo",
        "citation": "https://doi.org/10.5194/amt-15-797-2022",
    },
    *PHYSICS_VISION,
    *CONDENSED_MATTER,
    *JARVIS_PHYSICS,
    *ALEX_MP20,
    *THE_WELL,
)


def _jetnet_entries(paths: Mapping[str, Path]) -> Rows:
    h5py = importlib.import_module("h5py")
    index = 0
    for label, role in enumerate(JETNET_ROLES):
        with h5py.File(paths[role], "r") as book:
            jets = book["jet_features"]
            particles = book["particle_features"]
            if jets.shape[0] != particles.shape[0] or jets.shape[1:] != (4,):
                raise ValueError(f"the JetNet {role} shard has incompatible feature arrays")
            if particles.shape[1:] != (30, 4):
                raise ValueError(f"the JetNet {role} constituents are not 30 by 4")
            for at in range(jets.shape[0]):
                yield (
                    label,
                    {
                        "jet_features": array.array("f", jets[at]),
                        "particle_features": array.array("f", particles[at].reshape(-1)),
                        "label": label,
                        "index": index,
                    },
                )
                index += 1


def _jetnet(paths: Mapping[str, Path]) -> Loaded:
    columns: dict[str, Any] = {
        "jet_features": ("f", 4),
        "particle_features": ("f", 120),
        "label": "i",
        "index": "q",
    }
    return JETNET_CLASSES, columns, _jetnet_entries(paths)


_OMNIFOLD_SHARD = re.compile(r"omnifold_big_(\d+)\.txt$", re.IGNORECASE)


def _numbered_omnifold_members(
    archive: zipfile.ZipFile,
) -> list[tuple[int, zipfile.ZipInfo]]:
    numbered: list[tuple[int, zipfile.ZipInfo]] = []
    for info in archive.infolist():
        matched = _OMNIFOLD_SHARD.search(info.filename)
        if matched is not None:
            numbered.append((int(matched.group(1)), info))
    return numbered


def _sorted_omnifold_members(
    numbered: Sequence[tuple[int, zipfile.ZipInfo]],
) -> list[zipfile.ZipInfo]:
    identifiers = [identifier for identifier, _info in numbered]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("OmniFold Big has a duplicate numbered event shard")
    return [info for _identifier, info in sorted(numbered)]


def _legacy_omnifold_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    for info in archive.infolist():
        if Path(info.filename).name.lower() == "test_omni.txt":
            return info
    raise ValueError("OmniFold Big has no numbered event shards")


def _omnifold_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    numbered = _numbered_omnifold_members(archive)
    return _sorted_omnifold_members(numbered) if numbered else [_legacy_omnifold_member(archive)]


def _omnifold_level(cells: Sequence[str], description: str) -> int:
    if "truth" in cells[:2]:
        return 0
    if "reco" in cells[:2]:
        return 1
    raise ValueError(f"{description} names neither truth nor reco level")


def _omnifold_row(cells: Sequence[str], index: int, description: str) -> tuple[int, dict[str, Any]]:
    if len(cells) < 15:
        raise ValueError(f"{description} has {len(cells)} fields, not at least 15")
    level = _omnifold_level(cells, description)
    zg = float(cells[13])
    return (
        level,
        {
            "z_pt": float(cells[2]),
            "jet": array.array("f", (float(value) for value in cells[3:7])),
            "width": float(cells[7]),
            "tau2": float(cells[8]),
            "soft_drop_mass": float(cells[12]),
            "zg": 0.0 if math.isnan(zg) else zg,
            "multiplicity": int(cells[14]),
            "label": level,
            "index": index,
        },
    )


def _omnifold_rows(archive: zipfile.ZipFile, info: zipfile.ZipInfo, first: int) -> Rows:
    with archive.open(info) as held:
        text = io.TextIOWrapper(held, encoding="utf-8", newline="")
        try:
            for physical, line in enumerate(text):
                cells = line.split()
                if not cells:
                    continue
                yield _omnifold_row(
                    cells,
                    first,
                    f"OmniFold row {physical} of {info.filename}",
                )
                first += 1
        finally:
            text.close()


def _omnifold_entries(path: Path) -> Rows:
    archive = zipfile.ZipFile(path)
    index = 0
    try:
        for info in _omnifold_members(archive):
            for row in _omnifold_rows(archive, info, index):
                yield row
                index += 1
    finally:
        archive.close()


def _omnifold(path: Path) -> Loaded:
    columns: dict[str, Any] = {
        "z_pt": "f",
        "jet": ("f", 4),
        "width": "f",
        "tau2": "f",
        "soft_drop_mass": "f",
        "zg": "f",
        "multiplicity": "i",
        "label": "i",
        "index": "q",
    }
    return ("generator", "detector"), columns, _omnifold_entries(path)


def _tinysol_entries(path: Path) -> Rows:
    labels = {name: at for at, name in enumerate(TINYSOL_INSTRUMENTS)}
    archive = tarfile.open(path, mode="r:gz")
    index = 0
    try:
        for member in archive:
            if not member.isfile() or not member.name.lower().endswith(".wav"):
                continue
            yield _tinysol_entry(archive, member, labels, index)
            index += 1
    finally:
        archive.close()


def _tinysol_entry(
    archive: tarfile.TarFile, member: tarfile.TarInfo, labels: Mapping[str, int], index: int
) -> tuple[int, dict[str, Any]]:
    """Decode one named TinySOL PCM member."""
    code = Path(member.name).name.split("-")[0]
    if code not in TINYSOL_CODES:
        raise ValueError(f"TinySOL recording {member.name!r} names no known instrument")
    held = archive.extractfile(member)
    if held is None:
        raise ValueError(f"TinySOL recording {member.name!r} cannot be opened")
    with held:
        raw = held.read()
    with wave.open(io.BytesIO(raw), "rb") as recording:
        samples, length, sample_rate = _tinysol_pcm(recording, member.name)
    label = labels[TINYSOL_CODES[code]]
    return label, {
        "audio": samples,
        "length": length,
        "sample_rate": sample_rate,
        "label": label,
        "index": index,
    }


def _tinysol_pcm(recording: Any, name: str) -> tuple[array.array[float], int, int]:
    """Read TinySOL's mono PCM while retaining its source-specific diagnostics."""
    if recording.getnchannels() != 1 or recording.getsampwidth() != 2:
        raise ValueError(f"TinySOL recording {name!r} is not 16-bit mono PCM")
    length = recording.getnframes()
    if length > 441_000:
        raise ValueError(f"TinySOL recording {name!r} exceeds ten seconds")
    return _pcm16(recording, 441_000, name)


def _tinysol(path: Path) -> Loaded:
    columns: dict[str, Any] = {
        "audio": ("f", 441_000),
        "length": "i",
        "sample_rate": "i",
        "label": "i",
        "index": "i",
    }
    return TINYSOL_INSTRUMENTS, columns, _tinysol_entries(path)


def _pcm16(recording: Any, maximum: int, name: str) -> tuple[array.array[float], int, int]:
    """One mono PCM recording normalized to float32 and padded to a ROOT width."""
    if recording.getnchannels() != 1 or recording.getsampwidth() != 2:
        raise ValueError(f"recording {name!r} is not 16-bit mono PCM")
    length = recording.getnframes()
    if length > maximum:
        raise ValueError(f"recording {name!r} exceeds {maximum} samples")
    pcm = array.array("h")
    pcm.frombytes(recording.readframes(length))
    if len(pcm) != length:
        raise ValueError(f"recording {name!r} ends after {len(pcm)} of {length} samples")
    samples = _float_pcm(pcm, maximum)
    return samples, length, recording.getframerate()


def _integer_pcm(recording: Any, maximum: int, name: str) -> tuple[array.array[float], int, int]:
    """One mono integer-PCM WAV normalized according to its declared sample width."""
    if recording.getnchannels() != 1 or recording.getcomptype() != "NONE":
        raise ValueError(f"recording {name!r} is not uncompressed mono PCM")
    width = recording.getsampwidth()
    if width not in (1, 2, 3, 4):
        raise ValueError(f"recording {name!r} has unsupported {width}-byte PCM samples")
    length = recording.getnframes()
    if length > maximum:
        raise ValueError(f"recording {name!r} exceeds {maximum} samples")
    raw = recording.readframes(length)
    if len(raw) != length * width:
        raise ValueError(f"recording {name!r} ends after {len(raw) // width} of {length} samples")
    samples = _integer_pcm_samples(raw, width)
    samples.extend([0.0] * (maximum - length))
    return samples, length, recording.getframerate()


def _integer_pcm_samples(raw: bytes, width: int) -> array.array[float]:
    """Little-endian integer PCM of one supported width as normalized float32."""
    if width == 1:
        return array.array("f", ((sample - 128) / 128.0 for sample in raw))
    if width == 3:
        values = (
            int.from_bytes(raw[at : at + 3], "little", signed=True)
            for at in range(0, len(raw), 3)
        )
        return array.array("f", (sample / 8_388_608.0 for sample in values))
    code, scale = ("h", 32_768.0) if width == 2 else ("i", 2_147_483_648.0)
    pcm = array.array(code)
    pcm.frombytes(raw)
    if __import__("sys").byteorder == "big":
        pcm.byteswap()
    return array.array("f", (sample / scale for sample in pcm))


def _float_pcm(pcm: array.array[int], maximum: int) -> array.array[float]:
    """Signed little-endian PCM as normalized float32 with a zero tail."""
    if pcm.itemsize == 2 and __import__("sys").byteorder == "big":
        pcm.byteswap()
    samples = array.array("f", (sample / 32768.0 for sample in pcm))
    samples.extend([0.0] * (maximum - len(pcm)))
    return samples


@lru_cache(maxsize=4)
def _speech_lists(path: Path) -> tuple[frozenset[str], frozenset[str]]:
    """Read the official partitions in one sequential archive pass."""
    found: dict[str, set[str]] = {}
    with tarfile.open(path, mode="r|gz") as archive:
        for member in archive:
            key = Path(member.name).name
            if key not in {"validation_list.txt", "testing_list.txt"}:
                continue
            held = archive.extractfile(member)
            if held is None:
                raise ValueError(f"Speech Commands list {member.name!r} cannot be opened")
            with held:
                found[key] = set(held.read().decode("utf-8").splitlines())
    if set(found) != {"validation_list.txt", "testing_list.txt"}:
        raise ValueError("Speech Commands archive lacks its official partition lists")
    return frozenset(found["validation_list.txt"]), frozenset(found["testing_list.txt"])


def _speech_relative(name: str) -> str:
    """Speech Commands member path below its optional archive root."""
    marker = "speech_commands_v0.01/"
    return name.partition(marker)[2] if marker in name else name.lstrip("./")


def _speech_partition(relative: str, validation: frozenset[str], testing: frozenset[str]) -> str:
    if relative in validation:
        return "validation"
    if relative in testing:
        return "test"
    return "train"


def _speech_name(relative: str) -> tuple[str, str, int]:
    """Class, speaker hash, and repetition encoded in an utterance path."""
    label, separator, filename = relative.partition("/")
    stem = Path(filename).stem
    speaker, marker, repetition = stem.rpartition("_nohash_")
    if not separator or not marker or not repetition.isdigit():
        raise ValueError(f"Speech Commands recording {relative!r} has an unknown name")
    return label, speaker, int(repetition)


def _speech_recording(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    relative: str,
    labels: Mapping[str, int],
    index: int,
) -> tuple[int, dict[str, Any]]:
    label_name, speaker, utterance = _speech_name(relative)
    if label_name not in labels or label_name == "background_noise":
        raise ValueError(f"Speech Commands recording {relative!r} has unknown class {label_name!r}")
    held = archive.extractfile(member)
    if held is None:
        raise ValueError(f"Speech Commands recording {relative!r} cannot be opened")
    with held:
        raw = held.read()
    with wave.open(io.BytesIO(raw), "rb") as recording:
        audio, length, rate = _pcm16(recording, 16_000, relative)
    if rate != 16_000:
        raise ValueError(f"Speech Commands recording {relative!r} is {rate} Hz, not 16000 Hz")
    label = labels[label_name]
    return label, {
        "audio": audio,
        "length": length,
        "sample_rate": rate,
        "speaker": _fixed_text(speaker, 16, "speaker", relative),
        "utterance": utterance,
        "label": label,
        "index": index,
    }


def _speech_noise(archive: tarfile.TarFile, member: tarfile.TarInfo, start: int) -> Rows:
    """Non-overlapping one-second rows from one published augmentation track."""
    held = archive.extractfile(member)
    if held is None:
        raise ValueError(f"Speech Commands noise track {member.name!r} cannot be opened")
    with held:
        raw = held.read()
    with wave.open(io.BytesIO(raw), "rb") as recording:
        if (recording.getnchannels(), recording.getsampwidth(), recording.getframerate()) != (
            1,
            2,
            16_000,
        ):
            raise ValueError(f"Speech Commands noise track {member.name!r} is not 16 kHz PCM")
        chunk = 0
        while frames := recording.readframes(16_000):
            pcm = array.array("h")
            pcm.frombytes(frames)
            yield (
                30,
                {
                    "audio": _float_pcm(pcm, 16_000),
                    "length": len(pcm),
                    "sample_rate": 16_000,
                    "speaker": bytes(16),
                    "utterance": chunk,
                    "label": 30,
                    "index": start + chunk,
                },
            )
            chunk += 1


def _speech_member_entries(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    relative: str,
    split: str,
    validation: frozenset[str],
    testing: frozenset[str],
    labels: Mapping[str, int],
    index: int,
) -> Rows:
    if relative.startswith("_background_noise_/"):
        if split == "train":
            yield from _speech_noise(archive, member, index)
        return
    if _speech_partition(relative, validation, testing) == split:
        yield _speech_recording(archive, member, relative, labels, index)


def _speech_wav_members(archive: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
    for member in archive:
        if member.isfile() and member.name.lower().endswith(".wav"):
            yield member


def _speech_commands_entries(path: Path, split: str) -> Rows:
    labels = {name: at for at, name in enumerate(SPEECH_COMMAND_CLASSES)}
    validation, testing = _speech_lists(path)
    index = 0
    with tarfile.open(path, mode="r|gz") as archive:
        for member in _speech_wav_members(archive):
            relative = _speech_relative(member.name)
            entries = _speech_member_entries(
                archive, member, relative, split, validation, testing, labels, index
            )
            for made in entries:
                yield made
                index += 1


def _speech_commands(path: Path, split: str) -> Loaded:
    columns: dict[str, Any] = {
        "audio": ("f", 16_000),
        "length": "i",
        "sample_rate": "i",
        "speaker": ("B", 16),
        "utterance": "i",
        "label": "i",
        "index": "i",
    }
    return SPEECH_COMMAND_CLASSES, columns, _speech_commands_entries(path, split)


def _audiomnist_metadata(path: Path) -> dict[str, dict[str, Any]]:
    with path.open(encoding="utf-8") as source:
        metadata = json.load(source)
    if not isinstance(metadata, dict) or len(metadata) != 60:
        raise ValueError("AudioMNIST metadata does not describe its 60 speakers")
    return metadata


def _audiomnist_name(name: str) -> tuple[int, str, int]:
    pieces = Path(name).stem.split("_")
    if len(pieces) != 3 or not all(piece.isdigit() for piece in pieces):
        raise ValueError(f"AudioMNIST recording {name!r} has an unknown name")
    digit, speaker, repetition = pieces
    if not 0 <= int(digit) <= 9:
        raise ValueError(f"AudioMNIST recording {name!r} has unknown digit {digit!r}")
    return int(digit), speaker.zfill(2), int(repetition)


def _audiomnist_entry(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    metadata: Mapping[str, dict[str, Any]],
    index: int,
) -> tuple[int, dict[str, Any]]:
    digit, speaker, repetition = _audiomnist_name(member.name)
    if speaker not in metadata:
        raise ValueError(f"AudioMNIST recording {member.name!r} has no speaker metadata")
    held = archive.extractfile(member)
    if held is None:
        raise ValueError(f"AudioMNIST recording {member.name!r} cannot be opened")
    with held, wave.open(held, "rb") as recording:
        audio, length, rate = _pcm16(recording, 48_000, member.name)
    if rate != 48_000:
        raise ValueError(f"AudioMNIST recording {member.name!r} is {rate} Hz, not 48000 Hz")
    person = metadata[speaker]
    gender = {"female": 0, "male": 1}.get(str(person.get("gender", "")).lower(), -1)
    return digit, {
        "audio": audio,
        "length": length,
        "sample_rate": rate,
        "speaker": int(speaker),
        "repetition": repetition,
        "age": int(person["age"]),
        "gender": gender,
        "native_speaker": int(str(person.get("native speaker", "")).lower() == "yes"),
        "accent": _fixed_text(person.get("accent"), 24, "accent", member.name),
        "origin": _fixed_text(person.get("origin"), 64, "origin", member.name),
        "label": digit,
        "index": index,
    }


def _audiomnist_entries(path: Path, metadata: Mapping[str, dict[str, Any]]) -> Rows:
    # The pinned Hugging Face adaptation uses ``.tar.gz`` names for both gzip
    # and plain tar shards.  Let tarfile inspect the bytes instead of trusting
    # the suffix; the contents and publisher split remain unchanged.
    archive = tarfile.open(path, mode="r:*")
    index = 0
    try:
        for member in archive:
            if not member.isfile() or not member.name.lower().endswith(".wav"):
                continue
            yield _audiomnist_entry(archive, member, metadata, index)
            index += 1
    finally:
        archive.close()


def _audiomnist(raw: Mapping[str, Path], split: str) -> Loaded:
    columns: dict[str, Any] = {
        "audio": ("f", 48_000),
        "length": "i",
        "sample_rate": "i",
        "speaker": "i",
        "repetition": "i",
        "age": "i",
        "gender": "i",
        "native_speaker": "i",
        "accent": ("B", 24),
        "origin": ("B", 64),
        "label": "i",
        "index": "i",
    }
    metadata = _audiomnist_metadata(raw["metadata"])
    return (
        tuple(str(digit) for digit in range(10)),
        columns,
        _audiomnist_entries(raw[split], metadata),
    )


def _circor_csv_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    matches = [info for info in archive.infolist() if info.filename.endswith("training_data.csv")]
    if len(matches) != 1:
        raise ValueError(f"CirCor holds {len(matches)} training_data.csv files, not one")
    return matches[0]


def _nonempty_circor_metadata(metadata: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    if not metadata:
        raise ValueError("CirCor training_data.csv contains no patients")
    return metadata


def _circor_metadata(archive: zipfile.ZipFile) -> dict[str, dict[str, str]]:
    with archive.open(_circor_csv_member(archive)) as held:
        rows = csv.DictReader(io.TextIOWrapper(held, encoding="utf-8-sig", newline=""))
        metadata = {str(row["Patient ID"]): row for row in rows}
    return _nonempty_circor_metadata(metadata)


def _circor_number(value: str) -> float:
    return math.nan if not value or value.lower() == "nan" else float(value)


def _circor_identity(name: str) -> tuple[str, str]:
    pieces = Path(name).stem.split("_")
    if len(pieces) < 2 or not pieces[0].isdigit():
        raise ValueError(f"CirCor recording {name!r} has an unknown name")
    return pieces[0], pieces[1]


def _circor_subject(
    info: zipfile.ZipInfo,
    metadata: Mapping[str, dict[str, str]],
    labels: Mapping[str, int],
) -> tuple[str, str, dict[str, str], int]:
    patient, location = _circor_identity(info.filename)
    subject = metadata.get(patient)
    murmur = subject.get("Murmur") if subject else None
    if subject is None or murmur not in labels:
        raise ValueError(f"CirCor recording {info.filename!r} has no valid patient label")
    assert murmur is not None
    return patient, location, subject, labels[murmur]


def _circor_row(
    info: zipfile.ZipInfo,
    subject: Mapping[str, str],
    patient: str,
    location: str,
    pcm: array.array[int],
    segment: int,
    label: int,
    index: int,
) -> dict[str, Any]:
    ages = ("Neonate", "Infant", "Child", "Adolescent", "Young Adult")
    age_codes = {name: at for at, name in enumerate(ages)}
    return {
        "audio": _float_pcm(pcm, 20_000),
        "length": len(pcm),
        "sample_rate": 4_000,
        "patient": int(patient),
        "location": _fixed_text(location, 4, "location", info.filename),
        "segment": segment,
        "offset": segment * 20_000,
        "age_group": age_codes.get(subject.get("Age", ""), -1),
        "sex": {"Female": 0, "Male": 1}.get(subject.get("Sex", ""), -1),
        "height": _circor_number(subject.get("Height", "")),
        "weight": _circor_number(subject.get("Weight", "")),
        "pregnant": {"False": 0, "True": 1}.get(subject.get("Pregnancy status", ""), -1),
        "murmur_here": int(location in subject.get("Murmur locations", "").split("+")),
        "outcome": {"Normal": 0, "Abnormal": 1}.get(subject.get("Outcome", ""), -1),
        "label": label,
        "index": index,
    }


def _circor_recording_rows(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    subject: Mapping[str, str],
    patient: str,
    location: str,
    label: int,
    start: int,
) -> Rows:
    with archive.open(info) as held, wave.open(held, "rb") as recording:
        if (recording.getnchannels(), recording.getsampwidth(), recording.getframerate()) != (
            1,
            2,
            4_000,
        ):
            raise ValueError(f"CirCor recording {info.filename!r} is not 4 kHz mono PCM")
        segment = 0
        while frames := recording.readframes(20_000):
            pcm = array.array("h")
            pcm.frombytes(frames)
            yield (
                label,
                _circor_row(info, subject, patient, location, pcm, segment, label, start + segment),
            )
            segment += 1


def _circor_rows(path: Path) -> Rows:
    labels = {"Absent": 0, "Present": 1, "Unknown": 2}
    with zipfile.ZipFile(path) as archive:
        metadata = _circor_metadata(archive)
        members = sorted(
            (info for info in archive.infolist() if info.filename.lower().endswith(".wav")),
            key=lambda info: info.filename,
        )
        index = 0
        for info in members:
            patient, location, subject, label = _circor_subject(info, metadata, labels)
            for made in _circor_recording_rows(
                archive, info, subject, patient, location, label, index
            ):
                yield made
                index += 1


def _circor(path: Path) -> Loaded:
    columns: dict[str, Any] = {
        "audio": ("f", 20_000),
        "length": "i",
        "sample_rate": "i",
        "patient": "i",
        "location": ("B", 4),
        "segment": "i",
        "offset": "i",
        "age_group": "i",
        "sex": "i",
        "height": "f",
        "weight": "f",
        "pregnant": "i",
        "murmur_here": "i",
        "outcome": "i",
        "label": "i",
        "index": "i",
    }
    return ("murmur_absent", "murmur_present", "murmur_unknown"), columns, _circor_rows(path)


def _fixed_text(value: Any, width: int, field: str, name: str) -> bytes:
    """A publisher metadata value as a checked, zero-padded UTF-8 branch."""
    if not isinstance(value, str):
        raise ValueError(f"recording {name!r} has a non-text {field}")
    encoded = value.encode("utf-8")
    if len(encoded) > width:
        raise ValueError(f"recording {name!r} has a {field} longer than {width} bytes")
    return encoded + bytes(width - len(encoded))


def _reefset_entries(path: Path) -> Rows:
    labels = {name: at for at, name in enumerate(REEFSET_CLASSES)}
    archive = zipfile.ZipFile(path)
    try:
        records, members = _reefset_source(archive)
        used: set[str] = set()
        for index, record in enumerate(records):
            label, row, name = _reefset_entry(archive, record, members, labels, used, index)
            used.add(name)
            yield label, row
        if used != set(members):
            raise ValueError("ReefSet contains an unannotated WAV file")
    finally:
        archive.close()


def _reefset_source(
    archive: zipfile.ZipFile,
) -> tuple[list[Any], dict[str, zipfile.ZipInfo]]:
    """Read ReefSet's one annotation document and index its WAV members."""
    annotations = [
        info for info in archive.infolist() if info.filename.endswith("/reefset_annotations.json")
    ]
    if len(annotations) != 1:
        raise ValueError(f"ReefSet holds {len(annotations)} annotation files, not one")
    with archive.open(annotations[0]) as held:
        records = json.load(held)
    if not isinstance(records, list):
        raise ValueError("ReefSet annotations are not a JSON list")
    members = {
        Path(info.filename).name: info
        for info in archive.infolist()
        if not info.is_dir() and info.filename.lower().endswith(".wav")
    }
    if len(members) != len(records):
        raise ValueError(f"ReefSet holds {len(members)} WAV files and {len(records)} annotations")
    return records, members


def _reefset_entry(
    archive: zipfile.ZipFile,
    record: Any,
    members: Mapping[str, zipfile.ZipInfo],
    labels: Mapping[str, int],
    used: set[str],
    index: int,
) -> tuple[int, dict[str, Any], str]:
    """Validate and decode one ReefSet annotation and recording."""
    if not isinstance(record, dict):
        raise ValueError(f"ReefSet annotation {index} is not an object")
    name = record.get("file_name")
    if not isinstance(name, str) or name not in members or name in used:
        raise ValueError(f"ReefSet annotation {index} names an unknown or repeated WAV")
    label_name = record.get("label")
    if not isinstance(label_name, str) or label_name not in labels:
        raise ValueError(f"ReefSet recording {name!r} has unknown label {label_name!r}")
    source_id = record.get("id")
    if not isinstance(source_id, int):
        raise ValueError(f"ReefSet recording {name!r} has no integer source id")
    with archive.open(members[name]) as held, wave.open(held, "rb") as recording:
        audio, length, sample_rate = _integer_pcm(recording, 30_720, name)
    label = labels[label_name]
    row = {
        "audio": audio,
        "length": length,
        "sample_rate": sample_rate,
        "dataset": _fixed_text(record.get("dataset"), 24, "dataset", name),
        "data_sharer": _fixed_text(record.get("data_sharer"), 24, "data_sharer", name),
        "recorder": _fixed_text(record.get("recorder"), 24, "recorder", name),
        "source_id": source_id,
        "label": label,
        "index": index,
    }
    return label, row, name


def _reefset(path: Path) -> Loaded:
    columns: dict[str, Any] = {
        "audio": ("f", 30_720),
        "length": "i",
        "sample_rate": "i",
        "dataset": ("B", 24),
        "data_sharer": ("B", 24),
        "recorder": ("B", 24),
        "source_id": "i",
        "label": "i",
        "index": "i",
    }
    return REEFSET_CLASSES, columns, _reefset_entries(path)


def _biodcase_entries(path: Path, split: str) -> Rows:
    publisher_split = {"train": "Training_Set", "validation": "Validation_Set"}[split]
    prefix = f"{publisher_split}/"
    archive = zipfile.ZipFile(path)
    index = 0
    try:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.startswith(prefix):
                continue
            yield _biodcase_entry(archive, info, prefix, index)
            index += 1
    finally:
        archive.close()


def _biodcase_entry(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, prefix: str, index: int
) -> tuple[int, dict[str, Any]]:
    """Decode one BioDCASE member after interpreting its source filename."""
    if not info.filename.lower().endswith(".wav"):
        raise ValueError(f"BioDCASE member {info.filename!r} is not a WAV recording")
    folder, separator, filename = info.filename[len(prefix) :].partition("/")
    if not separator or "/" in filename:
        raise ValueError(f"BioDCASE member {info.filename!r} has an unknown layout")
    label, song_id, location, distance, kind = _biodcase_name(folder, filename, info.filename)
    with archive.open(info) as held, wave.open(held, "rb") as recording:
        audio, length, sample_rate = _pcm16(recording, 32_000, filename)
    return label, {
        "audio": audio,
        "length": length,
        "sample_rate": sample_rate,
        "song_id": song_id,
        "location": _fixed_text(location, 10, "location", filename),
        "distance": _fixed_text(distance, 1, "distance", filename),
        "kind": _fixed_text(kind, 12, "kind", filename),
        "label": label,
        "index": index,
    }


def _biodcase_name(folder: str, filename: str, member: str) -> tuple[int, int, str, str, str]:
    """Metadata encoded in one BioDCASE directory and filename."""
    stem = Path(filename).stem
    if folder == "Yellowhammer":
        pieces = stem.split("_")
        if len(pieces) != 4 or pieces[0] != "YH" or not pieces[1].isdigit():
            raise ValueError(f"BioDCASE Yellowhammer recording {filename!r} has an unknown name")
        return 1, int(pieces[1]), pieces[2], pieces[3], "yellowhammer"
    if folder == "Negatives":
        kind, separator, _identifier = stem.partition("_")
        if not separator or kind.lower() not in ("background", "bird"):
            raise ValueError(f"BioDCASE negative recording {filename!r} has an unknown name")
        return 0, -1, "", "", kind.lower()
    raise ValueError(f"BioDCASE member {member!r} has an unknown class")


def _biodcase(path: Path, split: str) -> Loaded:
    columns: dict[str, Any] = {
        "audio": ("f", 32_000),
        "length": "i",
        "sample_rate": "i",
        "song_id": "i",
        "location": ("B", 10),
        "distance": ("B", 1),
        "kind": ("B", 12),
        "label": "i",
        "index": "i",
    }
    return ("negative", "yellowhammer"), columns, _biodcase_entries(path, split)


def _npy_float32(raw: bytes, name: str) -> array.array[float]:
    """Decode one fixed-width NumPy vector without importing NumPy or allowing pickle."""
    if not raw.startswith(b"\x93NUMPY") or len(raw) < 10:
        raise ValueError(f"BirdSet embedding {name!r} is not a NumPy array")
    header_at, header_size, encoding = _npy_header_layout(raw, name)
    header_end = header_at + header_size
    if header_size > 4096 or header_end > len(raw):
        raise ValueError(f"BirdSet embedding {name!r} has an invalid NumPy header length")
    header = _npy_header(raw[header_at:header_end], encoding, name)
    _check_npy_header(header, name)
    return _npy_payload(raw[header_end:], name)


def _npy_header_layout(raw: bytes, name: str) -> tuple[int, int, str]:
    """Header offset, length, and encoding for a supported NumPy format version."""
    major = raw[6]
    if major == 1:
        return 10, int.from_bytes(raw[8:10], "little"), "latin-1"
    if major in (2, 3):
        if len(raw) < 12:
            raise ValueError(f"BirdSet embedding {name!r} has a truncated NumPy header")
        return 12, int.from_bytes(raw[8:12], "little"), "utf-8" if major == 3 else "latin-1"
    raise ValueError(f"BirdSet embedding {name!r} uses NumPy format {major}")


def _npy_header(raw: bytes, encoding: str, name: str) -> dict[str, Any]:
    """Safely parse the literal dictionary in a NumPy header."""
    try:
        header = ast.literal_eval(raw.decode(encoding).strip())
    except (SyntaxError, ValueError, UnicodeDecodeError) as error:
        raise ValueError(f"BirdSet embedding {name!r} has an invalid NumPy header") from error
    if not isinstance(header, dict):
        raise ValueError(f"BirdSet embedding {name!r} has a non-dictionary NumPy header")
    return header


def _check_npy_header(header: Mapping[str, Any], name: str) -> None:
    """Require the exact embedding shape and representation BirdSet publishes."""
    if (
        header.get("descr") != "<f4"
        or header.get("fortran_order") is not False
        or header.get("shape") != (1536,)
    ):
        raise ValueError(
            f"BirdSet embedding {name!r} is not a C-order 1536-element little-endian float32 vector"
        )


def _npy_payload(payload: bytes, name: str) -> array.array[float]:
    """Turn a validated little-endian float32 payload into a native array."""
    if len(payload) != 1536 * 4:
        raise ValueError(f"BirdSet embedding {name!r} has {len(payload)} data bytes, not 6144")
    result = array.array("f")
    result.frombytes(payload)
    if result.itemsize != 4:
        raise ValueError("this Python build does not use four-byte floats")
    if __import__("sys").byteorder == "big":
        result.byteswap()
    return result


def _birdset_member(archive: zipfile.ZipFile, subset: str, leaf: str) -> zipfile.ZipInfo:
    suffix = f"/{subset}_BASEAL/{leaf}"
    found = [info for info in archive.infolist() if info.filename.endswith(suffix)]
    if len(found) != 1:
        raise ValueError(f"BirdSet {subset} holds {len(found)} {leaf} files, not one")
    return found[0]


def _birdset_float(value: str | None, field: str, name: str) -> float:
    if value is None or not value.strip():
        return math.nan
    try:
        return float(value)
    except ValueError as error:
        raise ValueError(f"BirdSet recording {name!r} has invalid {field} {value!r}") from error


def _birdset_entries(path: Path, split: str) -> Rows:
    class_codes = {name: at for at, name in enumerate(BIRDSET_CLASSES)}
    archive = zipfile.ZipFile(path)
    indices = itertools.count()
    try:
        for subset_code, subset in enumerate(("HSN", "POW", "UHH")):
            yield from _birdset_subset(archive, subset, subset_code, split, class_codes, indices)
    finally:
        archive.close()


def _birdset_subset(
    archive: zipfile.ZipFile,
    subset: str,
    subset_code: int,
    split: str,
    class_codes: Mapping[str, int],
    indices: Iterator[int],
) -> Rows:
    """Stream one BirdSet site's paired label and metadata tables."""
    labels_info = _birdset_member(archive, subset, "labels.csv")
    metadata_info = _birdset_member(archive, subset, "metadata.csv")
    root = labels_info.filename[: -len("labels.csv")]
    with archive.open(labels_info) as labels_held, archive.open(metadata_info) as metadata_held:
        labels_text = io.TextIOWrapper(labels_held, encoding="utf-8-sig", newline="")
        metadata_text = io.TextIOWrapper(metadata_held, encoding="utf-8-sig", newline="")
        labels_rows = csv.DictReader(labels_text)
        metadata_rows = csv.DictReader(metadata_text)
        _birdset_schema(labels_rows, metadata_rows, subset)
        seen: set[str] = set()
        for label_row, metadata_row in _birdset_pairs(labels_rows, metadata_rows, subset):
            index = next(indices)
            name = _birdset_name(label_row, metadata_row, subset, seen)
            label_names, row_split = _birdset_labels(label_row, name, class_codes)
            if row_split == split:
                yield (
                    0,
                    _birdset_entry(
                        archive,
                        root,
                        name,
                        label_names,
                        metadata_row,
                        subset,
                        subset_code,
                        class_codes,
                        index,
                    ),
                )
    if not seen:
        raise ValueError(f"BirdSet {subset} contains no labelled rows")


def _birdset_schema(
    labels: csv.DictReader[str], metadata: csv.DictReader[str], subset: str
) -> None:
    required_labels = {"filename", "label", "split"}
    required_metadata = {
        "filename",
        "original_filepath",
        "start_time",
        "end_time",
        "lat",
        "long",
        "license",
        "source",
    }
    if not required_labels.issubset(labels.fieldnames or ()):
        raise ValueError(f"BirdSet {subset} labels.csv has an unknown schema")
    if not required_metadata.issubset(metadata.fieldnames or ()):
        raise ValueError(f"BirdSet {subset} metadata.csv has an unknown schema")


def _birdset_pairs(
    labels: Iterator[dict[str, str]], metadata: Iterator[dict[str, str]], subset: str
) -> Iterator[tuple[dict[str, str], dict[str, str]]]:
    """Pair two tables exactly, rejecting a shorter companion."""
    while True:
        label_row = next(labels, None)
        metadata_row = next(metadata, None)
        if label_row is None or metadata_row is None:
            if label_row is not None or metadata_row is not None:
                raise ValueError(f"BirdSet {subset} label and metadata tables differ in length")
            return
        yield label_row, metadata_row


def _birdset_name(
    label: Mapping[str, str], metadata: Mapping[str, str], subset: str, seen: set[str]
) -> str:
    name = label.get("filename", "")
    if not name or name != metadata.get("filename") or name in seen:
        raise ValueError(
            f"BirdSet {subset} has an unknown, repeated or misaligned filename {name!r}"
        )
    seen.add(name)
    return name


def _birdset_labels(
    row: Mapping[str, str], name: str, class_codes: Mapping[str, int]
) -> tuple[tuple[str, ...], str]:
    row_split = row.get("split")
    if row_split not in ("train", "validation"):
        raise ValueError(f"BirdSet recording {name!r} has split {row_split!r}")
    label_names = tuple(
        value.strip() for value in (row.get("label") or "").split(";") if value.strip()
    )
    unknown = [value for value in label_names if value not in class_codes]
    if unknown:
        raise ValueError(f"BirdSet recording {name!r} has unknown label {unknown[0]!r}")
    return label_names, row_split


def _birdset_entry(
    archive: zipfile.ZipFile,
    root: str,
    name: str,
    label_names: Sequence[str],
    metadata: Mapping[str, str],
    subset: str,
    subset_code: int,
    class_codes: Mapping[str, int],
    index: int,
) -> dict[str, Any]:
    """Read one embedding and assemble its labels and provenance."""
    try:
        embedding_info = archive.getinfo(root + "embeddings/perch_v2/" + name)
    except KeyError as error:
        raise ValueError(f"BirdSet recording {name!r} has no embedding") from error
    if embedding_info.file_size > 16_384:
        raise ValueError(f"BirdSet embedding {name!r} is unexpectedly large")
    target = array.array("f", [0.0] * len(BIRDSET_CLASSES))
    for label_name in label_names:
        target[class_codes[label_name]] = 1.0
    provenance = "|".join(
        (
            subset,
            metadata.get("original_filepath") or "",
            metadata.get("source") or "",
            metadata.get("license") or "",
            ";".join(label_names),
        )
    )
    return {
        "embedding": _npy_float32(archive.read(embedding_info), name),
        "label": target,
        "subset": subset_code,
        "latitude": _birdset_float(metadata.get("lat"), "lat", name),
        "longitude": _birdset_float(metadata.get("long"), "long", name),
        "start_time": _birdset_float(metadata.get("start_time"), "start_time", name),
        "end_time": _birdset_float(metadata.get("end_time"), "end_time", name),
        "entry": _fixed_text(provenance, 1024, "provenance", name),
        "index": index,
    }


def _birdset(path: Path, split: str) -> Loaded:
    columns: dict[str, Any] = {
        "embedding": ("f", 1536),
        "label": ("f", len(BIRDSET_CLASSES)),
        "subset": "i",
        "latitude": "f",
        "longitude": "f",
        "start_time": "f",
        "end_time": "f",
        "entry": ("B", 1024),
        "index": "i",
    }
    return ("samples",), columns, _birdset_entries(path, split)


MAIZE_TILE = 224
MAIZE_BANDS = 6
MAIZE_CLASSES = ("soil", "low_stress", "high_stress", "healthy", "rust")
MAIZE_TASKS: dict[str, dict[str, Any]] = {
    "water": {
        "code": 0,
        "tree": 0,
        "tiff": "positive_output.tif",
        "metadata": "water_orthomosaic_metadata.json",
        "width": 7373,
        "height": 4140,
    },
    "rust": {
        "code": 1,
        "tree": 1,
        "tiff": "Processed_Orthomosaic.tif",
        "metadata": "rust_orthomosaic_metadata.json",
        "width": 7202,
        "height": 4266,
    },
}


def _maize_member(archive: zipfile.ZipFile, suffix: str, description: str) -> zipfile.ZipInfo:
    found = [
        info for info in archive.infolist() if not info.is_dir() and info.filename.endswith(suffix)
    ]
    if len(found) != 1:
        raise ValueError(f"UAV Maize holds {len(found)} {description} files, not one")
    return found[0]


def _maize_metadata(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    with zipfile.ZipFile(path) as archive:
        for task, details in MAIZE_TASKS.items():
            info = _maize_member(archive, details["metadata"], f"{task} metadata")
            with archive.open(info) as held:
                record = json.load(held)
            raster = record.get("raster_properties") if isinstance(record, dict) else None
            if not isinstance(raster, dict):
                raise ValueError(f"UAV Maize {task} metadata has no raster properties")
            expected = (details["width"], details["height"], MAIZE_BANDS)
            observed = (
                raster.get("width_pixels"),
                raster.get("height_pixels"),
                raster.get("number_of_bands"),
            )
            if observed != expected or raster.get("crs") != "EPSG:32636":
                raise ValueError(f"UAV Maize {task} metadata has incompatible raster geometry")
            transform = raster.get("transform")
            if not isinstance(transform, dict) or any(
                name not in transform for name in ("a", "b", "c", "d", "e", "f")
            ):
                raise ValueError(f"UAV Maize {task} metadata has no affine transform")
            result[task] = raster
    return result


def _maize_validate_readme(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        info = _maize_member(archive, "LICENSE.txt", "licence")
        terms = archive.read(info).decode("utf-8-sig")
    if "Creative Commons Attribution 4.0" not in terms:
        raise ValueError("UAV Maize source does not carry its declared CC BY 4.0 terms")


def _maize_float_array(values: Any) -> array.array[float]:
    """Flatten an array-like value into native ROOT float32 storage."""
    numpy = importlib.import_module("numpy")
    contiguous = numpy.ascontiguousarray(values, dtype="<f4")
    result = array.array("f")
    result.frombytes(contiguous.tobytes(order="C"))
    if result.itemsize != 4:
        raise ValueError("this Python build does not use four-byte floats")
    if __import__("sys").byteorder == "big":
        result.byteswap()
    return result


def _maize_double_array(values: Any) -> array.array[float]:
    numpy = importlib.import_module("numpy")
    contiguous = numpy.ascontiguousarray(values, dtype="<f8")
    result = array.array("d")
    result.frombytes(contiguous.tobytes(order="C"))
    if result.itemsize != 8:
        raise ValueError("this Python build does not use eight-byte doubles")
    if __import__("sys").byteorder == "big":
        result.byteswap()
    return result


def _maize_npy(raw: bytes, name: str) -> Any:
    numpy = importlib.import_module("numpy")
    try:
        image = numpy.load(io.BytesIO(raw), allow_pickle=False)
    except (OSError, ValueError) as error:
        raise ValueError(f"UAV Maize patch {name!r} is not a safe NumPy array") from error
    if (
        image.shape != (MAIZE_TILE, MAIZE_TILE, MAIZE_BANDS)
        or image.dtype != numpy.dtype("<f2")
        or not image.flags.c_contiguous
    ):
        raise ValueError(f"UAV Maize patch {name!r} is not a C-order 224 by 224 by 6 float16 array")
    return image


def _maize_mask(raw: bytes, name: str) -> Any:
    numpy = importlib.import_module("numpy")
    image_module = importlib.import_module("PIL.Image")
    try:
        with image_module.open(io.BytesIO(raw)) as image:
            mask = numpy.asarray(image, dtype="uint8")
    except (OSError, ValueError) as error:
        raise ValueError(f"UAV Maize mask {name!r} is not an 8-bit PNG") from error
    if mask.shape != (MAIZE_TILE, MAIZE_TILE):
        raise ValueError(f"UAV Maize mask {name!r} is not 224 by 224 pixels")
    if int(mask.max()) >= len(MAIZE_CLASSES):
        raise ValueError(f"UAV Maize mask {name!r} contains an unknown class id")
    return mask


def _maize_row(
    *,
    image: Any,
    mask: Any,
    task: str,
    stage: int,
    x0: int,
    y0: int,
    width: int,
    height: int,
    valid_ratio: float,
    foreground_ratio: float,
    negative: int,
    ratios: Any,
    bounds: Any,
    entry: str,
    index: int,
) -> dict[str, Any]:
    details = MAIZE_TASKS[task]
    return {
        "image": _maize_float_array(image),
        "label": array.array("B", mask.tobytes(order="C")),
        "task": details["code"],
        "stage": stage,
        "x0": x0,
        "y0": y0,
        "width": width,
        "height": height,
        "source_width": details["width"],
        "source_height": details["height"],
        "valid_ratio": valid_ratio,
        "foreground_ratio": foreground_ratio,
        "is_negative": negative,
        "class_ratios": _maize_float_array(ratios),
        "geo_bounds": _maize_double_array(bounds),
        "crs": _fixed_text("EPSG:32636", 16, "CRS", entry),
        "entry": _fixed_text(entry, 128, "identifier", entry),
        "index": index,
    }


def _maize_orthomosaic_entries(
    path: Path, metadata: Mapping[str, Mapping[str, Any]], start: int = 0
) -> Rows:
    numpy = importlib.import_module("numpy")
    tifffile = importlib.import_module("tifffile")
    archive = zipfile.ZipFile(path)
    index = start
    try:
        for task in ("water", "rust"):
            for row in _maize_orthomosaic_task(
                archive, task, metadata[task], numpy, tifffile, index
            ):
                yield row
                index += 1
    finally:
        archive.close()


def _maize_orthomosaic_task(
    archive: zipfile.ZipFile,
    task: str,
    metadata: Mapping[str, Any],
    numpy: Any,
    tifffile: Any,
    first: int,
) -> Rows:
    """Extract and stream the tiles of one source orthomosaic."""
    details = MAIZE_TASKS[task]
    info = _maize_member(archive, details["tiff"], f"{task} orthomosaic")
    with tempfile.TemporaryDirectory(prefix=f"xrd-maize-{task}-") as held_dir:
        extracted = Path(held_dir) / "source.tif"
        with archive.open(info) as source, extracted.open("wb") as target:
            shutil.copyfileobj(source, target, length=8 * 1024 * 1024)
        raster = _maize_open_raster(extracted, Path(held_dir), tifffile)
        try:
            _maize_check_raster(raster, task, details, numpy)
            yield from _maize_tiles(raster, task, details, metadata, numpy, first)
        finally:
            mapped = getattr(raster, "_mmap", None)
            if mapped is not None:
                mapped.close()


def _maize_open_raster(extracted: Path, held_dir: Path, tifffile: Any) -> Any:
    """Memory-map a TIFF where possible, otherwise decode it to temporary storage."""
    try:
        return tifffile.memmap(extracted)
    except ValueError:
        return tifffile.imread(extracted, out=held_dir / "decoded.dat")


def _maize_check_raster(raster: Any, task: str, details: Mapping[str, Any], numpy: Any) -> None:
    """Require the documented band order, dimensions, and scalar type."""
    height, width = details["height"], details["width"]
    shapes = ((height, width, MAIZE_BANDS), (MAIZE_BANDS, height, width))
    if raster.dtype != numpy.dtype("float32") or raster.shape not in shapes:
        raise ValueError(f"UAV Maize {task} orthomosaic has incompatible shape or dtype")


def _maize_tiles(
    raster: Any,
    task: str,
    details: Mapping[str, Any],
    metadata: Mapping[str, Any],
    numpy: Any,
    first: int,
) -> Rows:
    """Cut one validated raster into padded training-sized tiles."""
    index = first
    for y0 in range(0, details["height"], MAIZE_TILE):
        for x0 in range(0, details["width"], MAIZE_TILE):
            yield (
                details["tree"],
                _maize_tile(raster, task, details, metadata, numpy, x0, y0, index),
            )
            index += 1


def _maize_tile(
    raster: Any,
    task: str,
    details: Mapping[str, Any],
    metadata: Mapping[str, Any],
    numpy: Any,
    x0: int,
    y0: int,
    index: int,
) -> dict[str, Any]:
    """One padded orthomosaic tile and its geospatial bounds."""
    height, width = details["height"], details["width"]
    tile_height = min(MAIZE_TILE, height - y0)
    tile_width = min(MAIZE_TILE, width - x0)
    tile = numpy.full(
        (MAIZE_TILE, MAIZE_TILE, MAIZE_BANDS),
        float(metadata.get("nodata_value", -10_000.0)),
        dtype="float32",
    )
    if raster.shape[-1] == MAIZE_BANDS:
        tile[:tile_height, :tile_width] = raster[y0 : y0 + tile_height, x0 : x0 + tile_width, :]
    else:
        tile[:tile_height, :tile_width] = raster[
            :, y0 : y0 + tile_height, x0 : x0 + tile_width
        ].transpose(1, 2, 0)
    bounds = _maize_bounds(metadata["transform"], x0, y0, tile_width, tile_height)
    mask = numpy.full((MAIZE_TILE, MAIZE_TILE), 255, dtype="uint8")
    entry = f"{task}_orthomosaic_y{y0}_x{x0}_t{MAIZE_TILE}"
    return _maize_row(
        image=tile,
        mask=mask,
        task=task,
        stage=0,
        x0=x0,
        y0=y0,
        width=tile_width,
        height=tile_height,
        valid_ratio=(tile_width * tile_height) / MAIZE_TILE**2,
        foreground_ratio=math.nan,
        negative=-1,
        ratios=[math.nan] * len(MAIZE_CLASSES),
        bounds=bounds,
        entry=entry,
        index=index,
    )


def _maize_bounds(
    transform: Mapping[str, Any], x0: int, y0: int, width: int, height: int
) -> tuple[float, float, float, float]:
    """Geospatial corners of a pixel window under the source affine transform."""
    a, b, c = (float(transform[name]) for name in ("a", "b", "c"))
    d, e, f = (float(transform[name]) for name in ("d", "e", "f"))
    x1, y1 = x0 + width, y0 + height
    return (
        c + a * x0 + b * y0,
        f + d * x0 + e * y0,
        c + a * x1 + b * y1,
        f + d * x1 + e * y1,
    )


def _maize_int(row: Mapping[str, str], field: str, name: str) -> int:
    try:
        return int(row[field])
    except (KeyError, ValueError) as error:
        raise ValueError(f"UAV Maize patch {name!r} has invalid {field}") from error


def _maize_float(row: Mapping[str, str], field: str, name: str) -> float:
    try:
        return float(row[field])
    except (KeyError, ValueError) as error:
        raise ValueError(f"UAV Maize patch {name!r} has invalid {field}") from error


def _maize_patch_entries(path: Path, start: int) -> Rows:
    numpy = importlib.import_module("numpy")
    archive = zipfile.ZipFile(path)
    try:
        class_info = _maize_member(archive, "meta/class_map.json", "class map")
        class_record = json.loads(archive.read(class_info))
        class_map = class_record.get("class_map") if isinstance(class_record, dict) else None
        if class_map != {name: at for at, name in enumerate(MAIZE_CLASSES)}:
            raise ValueError("UAV Maize has an incompatible semantic class map")
        csv_info = _maize_member(archive, "meta/patches.csv", "patch metadata")
        images = {
            Path(info.filename).name: info
            for info in archive.infolist()
            if not info.is_dir() and "/images/" in info.filename and info.filename.endswith(".npy")
        }
        masks = {
            Path(info.filename).name: info
            for info in archive.infolist()
            if not info.is_dir() and "/masks/" in info.filename and info.filename.endswith(".png")
        }
        with archive.open(csv_info) as held:
            text = io.TextIOWrapper(held, encoding="utf-8-sig", newline="")
            rows = csv.DictReader(text)
            required = {
                "id",
                "src_task",
                "x0",
                "y0",
                "x1",
                "y1",
                "tile_h",
                "tile_w",
                "channels",
                "valid_ratio",
                "fg_ratio",
                "is_negative",
                "geo_x0",
                "geo_y0",
                "geo_x1",
                "geo_y1",
                "crs",
                *(f"ratio_c{at}" for at in range(len(MAIZE_CLASSES))),
            }
            if not required.issubset(rows.fieldnames or ()):
                raise ValueError("UAV Maize patch metadata has an unknown schema")
            seen: set[str] = set()
            for offset, row in enumerate(rows):
                name = row.get("id", "")
                task = row.get("src_task", "")
                if not name or name in seen or task not in MAIZE_TASKS:
                    raise ValueError(f"UAV Maize has an unknown or repeated patch {name!r}")
                seen.add(name)
                try:
                    image_info, mask_info = images[name + ".npy"], masks[name + ".png"]
                except KeyError as error:
                    raise ValueError(
                        f"UAV Maize patch {name!r} is missing its image or mask"
                    ) from error
                image = _maize_npy(archive.read(image_info), name)
                mask = _maize_mask(archive.read(mask_info), name)
                x0, y0 = _maize_int(row, "x0", name), _maize_int(row, "y0", name)
                x1, y1 = _maize_int(row, "x1", name), _maize_int(row, "y1", name)
                tile_width, tile_height = x1 - x0, y1 - y0
                details = MAIZE_TASKS[task]
                if (
                    not 0 < tile_width <= MAIZE_TILE
                    or not 0 < tile_height <= MAIZE_TILE
                    or x0 < 0
                    or y0 < 0
                    or x1 > details["width"]
                    or y1 > details["height"]
                    or _maize_int(row, "tile_h", name) != MAIZE_TILE
                    or _maize_int(row, "tile_w", name) != MAIZE_TILE
                    or _maize_int(row, "channels", name) != MAIZE_BANDS
                    or row.get("crs") != "EPSG:32636"
                ):
                    raise ValueError(f"UAV Maize patch {name!r} has incompatible geometry")
                ratios = [_maize_float(row, f"ratio_c{at}", name) for at in range(5)]
                observed = numpy.bincount(mask.reshape(-1), minlength=5) / mask.size
                if not numpy.allclose(observed, ratios, rtol=0.0, atol=1e-6):
                    raise ValueError(f"UAV Maize patch {name!r} mask disagrees with its metadata")
                yield (
                    MAIZE_TASKS[task]["tree"] + 2,
                    _maize_row(
                        image=image,
                        mask=mask,
                        task=task,
                        stage=1,
                        x0=x0,
                        y0=y0,
                        width=tile_width,
                        height=tile_height,
                        valid_ratio=_maize_float(row, "valid_ratio", name),
                        foreground_ratio=_maize_float(row, "fg_ratio", name),
                        negative=_maize_int(row, "is_negative", name),
                        ratios=ratios,
                        bounds=[
                            _maize_float(row, "geo_x0", name),
                            _maize_float(row, "geo_y0", name),
                            _maize_float(row, "geo_x1", name),
                            _maize_float(row, "geo_y1", name),
                        ],
                        entry=name,
                        index=start + offset,
                    ),
                )
            if {name + ".npy" for name in seen} != set(images) or {
                name + ".png" for name in seen
            } != set(masks):
                raise ValueError("UAV Maize contains an image or mask absent from patch metadata")
    finally:
        archive.close()


def _maize_entries(paths: Mapping[str, Path]) -> Rows:
    _maize_validate_readme(paths["readme"])
    metadata = _maize_metadata(paths["metadata"])
    orthomosaic_count = sum(
        math.ceil(details["width"] / MAIZE_TILE) * math.ceil(details["height"] / MAIZE_TILE)
        for details in MAIZE_TASKS.values()
    )
    yield from _maize_orthomosaic_entries(paths["orthomosaics"], metadata)
    yield from _maize_patch_entries(paths["patches"], orthomosaic_count)


def _maize(paths: Mapping[str, Path]) -> Loaded:
    columns: dict[str, Any] = {
        "image": ("f", MAIZE_TILE * MAIZE_TILE * MAIZE_BANDS),
        "label": ("B", MAIZE_TILE * MAIZE_TILE),
        "task": "i",
        "stage": "i",
        "x0": "i",
        "y0": "i",
        "width": "i",
        "height": "i",
        "source_width": "i",
        "source_height": "i",
        "valid_ratio": "f",
        "foreground_ratio": "f",
        "is_negative": "i",
        "class_ratios": ("f", len(MAIZE_CLASSES)),
        "geo_bounds": ("d", 4),
        "crs": ("B", 16),
        "entry": ("B", 128),
        "index": "q",
    }
    trees = ("orthomosaic_water", "orthomosaic_rust", "patch_water", "patch_rust")
    return trees, columns, _maize_entries(paths)


WILDLIFE_ROWS = 60_000
WILDLIFE_SIDE = 32


def _wildlife_previews(paths: Mapping[str, Path]) -> None:
    for role in ("preview_train", "preview_digit", "preview_background", "preview_texture"):
        with paths[role].open("rb") as held:
            signature = held.read(8)
        if signature != b"\x89PNG\r\n\x1a\n":
            raise ValueError(f"Wildlife MNIST {role} is not a PNG preview")


def _wildlife_entries(paths: Mapping[str, Path], split: str) -> Rows:
    numpy = importlib.import_module("numpy")
    _wildlife_previews(paths)
    images = numpy.load(paths[f"{split}_images"], mmap_mode="r", allow_pickle=False)
    labels = numpy.load(paths[f"{split}_labels"], mmap_mode="r", allow_pickle=False)
    try:
        _wildlife_check(images, labels, split, numpy)
        for index in range(WILDLIFE_ROWS):
            yield _wildlife_entry(images[index], labels[index], split, index, numpy)
    finally:
        for value in (images, labels):
            mapped = getattr(value, "_mmap", None)
            if mapped is not None:
                mapped.close()


def _wildlife_check(images: Any, labels: Any, split: str, numpy: Any) -> None:
    """Require the published Wildlife MNIST array shapes and scalar types."""
    label_shape = (WILDLIFE_ROWS,) if split == "train" else (WILDLIFE_ROWS, 3)
    if (
        images.shape != (WILDLIFE_ROWS, 3, WILDLIFE_SIDE, WILDLIFE_SIDE)
        or images.dtype != numpy.dtype("<f4")
        or labels.shape != label_shape
        or labels.dtype != numpy.dtype("<i8")
    ):
        raise ValueError(f"Wildlife MNIST {split} arrays have incompatible shapes or dtypes")


def _wildlife_entry(
    image: Any, labels: Any, split: str, index: int, numpy: Any
) -> tuple[int, dict[str, Any]]:
    """Validate and materialise one Wildlife MNIST example."""
    if split == "train":
        digit = background = foreground = int(labels)
    else:
        digit, background, foreground = (int(value) for value in labels)
    if any(value < 0 or value >= 10 for value in (digit, background, foreground)):
        raise ValueError(f"Wildlife MNIST row {index} has an unknown factor label")
    if not numpy.isfinite(image).all() or image.min() < -1 or image.max() > 1:
        raise ValueError(f"Wildlife MNIST row {index} has an invalid image value")
    return digit, {
        "image": _maize_float_array(image),
        "label": digit,
        "background": background,
        "foreground": foreground,
        "index": index,
    }


def _wildlife(paths: Mapping[str, Path], split: str) -> Loaded:
    columns: dict[str, Any] = {
        "image": ("f", 3 * WILDLIFE_SIDE * WILDLIFE_SIDE),
        "label": "i",
        "background": "i",
        "foreground": "i",
        "index": "i",
    }
    return tuple(str(value) for value in range(10)), columns, _wildlife_entries(paths, split)


def _decoded_raster(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    *,
    mode: str,
    size: tuple[int, int],
    description: str,
) -> bytes:
    """Decode one bounded raster and require the publisher's declared geometry."""
    image_module = importlib.import_module("PIL.Image")
    try:
        with image_module.open(io.BytesIO(archive.read(member))) as image:
            image.load()
            if image.mode != mode or image.size != size:
                raise ValueError(f"{description} has mode {image.mode} and size {image.size}")
            return bytes(image.tobytes())
    except (OSError, SyntaxError) as error:
        raise ValueError(f"{description} is not a readable image") from error


def _sod_band(name: str) -> int:
    """Map a publisher distance folder to the stable ROOT tree order."""
    try:
        return SOD_SOURCE_BANDS.index(name)
    except ValueError as error:
        raise ValueError(f"SODv2 has an unknown distance stratum {name!r}") from error


def _sod_box(line: str, name: str) -> tuple[float, ...]:
    """Validate one YOLO row and return its normalized centre-format box."""
    cells = line.split()
    if len(cells) != 5 or cells[0] != "0":
        raise ValueError(f"SODv2 {name!r} has an invalid YOLO row")
    try:
        values = tuple(float(value) for value in cells[1:])
    except ValueError as error:
        raise ValueError(f"SODv2 {name!r} has a non-numeric box") from error
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"SODv2 {name!r} has a non-finite box")
    _sod_coordinates(values, name)
    return values


def _sod_coordinates(values: tuple[float, ...], name: str) -> None:
    """Require one centre-format box to fit normalized YOLO coordinates."""
    x, y, width, height = values
    if min(x, y) < 0 or max(x, y) > 1:
        raise ValueError(f"SODv2 {name!r} has a centre outside normalized coordinates")
    if min(width, height) <= 0 or max(width, height) > 1:
        raise ValueError(f"SODv2 {name!r} has an invalid normalized box size")


def _sod_boxes(raw: bytes, name: str) -> tuple[array.array[float], array.array[int], int]:
    """Validate and pad one YOLO satellite annotation."""
    rows = raw.decode("ascii").splitlines()
    if not rows or len(rows) > SOD_MAX_OBJECTS:
        raise ValueError(f"SODv2 {name!r} has {len(rows)} objects")
    boxes = array.array("f", [0.0] * (SOD_MAX_OBJECTS * 4))
    labels = array.array("i", [-1] * SOD_MAX_OBJECTS)
    for index, line in enumerate(rows):
        values = _sod_box(line, name)
        boxes[index * 4 : index * 4 + 4] = array.array("f", values)
        labels[index] = 0
    return boxes, labels, len(rows)


def _sod_member(member: zipfile.ZipInfo, source_split: str) -> tuple[str, tuple[int, str]] | None:
    """Describe one relevant SODv2 archive member, or leave it ignored."""
    if member.is_dir():
        return None
    parts = Path(member.filename).parts
    if len(parts) < 5:
        return None
    if parts[-4] != source_split:
        return None
    kind = {("images", ".jpg"): "images", ("labels", ".txt"): "labels"}.get(
        (parts[-2], Path(parts[-1]).suffix.lower())
    )
    if kind is None:
        return None
    return kind, (_sod_band(parts[-3]), Path(parts[-1]).stem)


def _sod_terms(archive: zipfile.ZipFile) -> None:
    """Require the pinned snapshot to carry its registered dataset terms."""
    readmes = [member for member in archive.infolist() if member.filename.endswith("README.md")]
    if len(readmes) != 1 or b"CC BY 4.0" not in archive.read(readmes[0]):
        raise ValueError("SODv2 source does not carry its declared CC BY 4.0 terms")


def _sod_members(
    archive: zipfile.ZipFile, split: str
) -> tuple[dict[tuple[int, str], zipfile.ZipInfo], dict[tuple[int, str], zipfile.ZipInfo]]:
    """Find matching SODv2 images and labels for one official split."""
    source_split = "val" if split == "validation" else split
    images: dict[tuple[int, str], zipfile.ZipInfo] = {}
    labels: dict[tuple[int, str], zipfile.ZipInfo] = {}
    for member in archive.infolist():
        found = _sod_member(member, source_split)
        if found is None:
            continue
        kind, key = found
        destination = images if kind == "images" else labels
        if key in destination:
            raise ValueError(f"SODv2 repeats {kind} member {key[1]!r}")
        destination[key] = member
    if not images or set(images) != set(labels):
        raise ValueError("SODv2 does not contain one annotation for every selected image")
    return images, labels


def _sod_entries(path: Path, split: str) -> Rows:
    """Yield decoded orbital scenes with their fixed-width box tensors."""
    with zipfile.ZipFile(path) as archive:
        _sod_terms(archive)
        images, labels = _sod_members(archive, split)
        for index, key in enumerate(sorted(images)):
            band, stem = key
            picture = _decoded_raster(
                archive,
                images[key],
                mode="RGB",
                size=(SOD_WIDTH, SOD_HEIGHT),
                description=f"SODv2 image {stem!r}",
            )
            boxes, object_labels, objects = _sod_boxes(archive.read(labels[key]), stem)
            if not stem.startswith("image") or not stem[5:].isdigit():
                raise ValueError(f"SODv2 has an unknown image identifier {stem!r}")
            yield (
                band,
                {
                    "image": picture,
                    "boxes": boxes,
                    "object_labels": object_labels,
                    "objects": objects,
                    "distance_band": band,
                    "image_id": int(stem[5:]),
                    "index": index,
                },
            )


def _sodv2(path: Path, split: str) -> Loaded:
    columns: dict[str, Any] = {
        "image": ("B", SOD_WIDTH * SOD_HEIGHT * 3),
        "boxes": ("f", SOD_MAX_OBJECTS * 4),
        "object_labels": ("i", SOD_MAX_OBJECTS),
        "objects": "i",
        "distance_band": "i",
        "image_id": "i",
        "index": "i",
    }
    return SOD_BANDS, columns, _sod_entries(path, split)


def _allsky_support(paths: Mapping[str, Path]) -> None:
    """Validate the small class, camera and notebook companions."""
    class_rows = paths["classes"].read_text(encoding="utf-8").splitlines()
    values = tuple(
        row.split(":", 1)[1].strip().replace("-", "_").replace(" ", "_") for row in class_rows
    )
    if values != ALLSKY_CLASSES:
        raise ValueError("All-Sky Cloud Segmentation has an incompatible class map")
    metadata = paths["metadata"].read_text(encoding="utf-8")
    aliases = (*ALLSKY_CAMERAS[:2], "Cloud_Cam_PVotQ71", ALLSKY_CAMERAS[3])
    if any(alias not in metadata for alias in aliases):
        raise ValueError("All-Sky Cloud Segmentation is missing camera metadata")
    try:
        notebook = json.loads(paths["notebook"].read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("All-Sky Cloud Segmentation has an invalid companion notebook") from error
    if not isinstance(notebook.get("cells"), list):
        raise ValueError("All-Sky Cloud Segmentation has an incompatible companion notebook")


def _allsky_validation(archive: zipfile.ZipFile) -> set[str]:
    """Read the publisher's validation membership without inventing a split."""
    members = [
        member for member in archive.infolist() if member.filename.endswith("validation.csv")
    ]
    if len(members) != 1:
        raise ValueError("All-Sky Cloud Segmentation has no unique validation list")
    rows = csv.reader(io.StringIO(archive.read(members[0]).decode("utf-8-sig")))
    next(rows, None)
    return {row[0].strip() for row in rows if row and row[0].strip()}


def _allsky_member_key(parts: tuple[str, ...], test: bool) -> tuple[int, str]:
    """Return camera and filename stem for a source image or mask."""
    camera = parts[-2] if test else ALLSKY_CAMERAS[0]
    try:
        camera_id = ALLSKY_CAMERAS.index(camera)
    except ValueError as error:
        raise ValueError(f"All-Sky Cloud Segmentation has an unknown camera {camera!r}") from error
    return camera_id, Path(parts[-1]).stem


def _allsky_member(member: zipfile.ZipInfo, test: bool) -> tuple[str, tuple[int, str]] | None:
    """Describe one relevant all-sky archive member, or leave it ignored."""
    if member.is_dir():
        return None
    parts = Path(member.filename).parts
    if len(parts) < 3:
        return None
    parent = parts[-3] if test else parts[-2]
    kind = {("images", ".jpg"): "images", ("seg_masks", ".png"): "masks"}.get(
        (parent, Path(parts[-1]).suffix.lower())
    )
    if kind is None:
        return None
    return kind, _allsky_member_key(parts, test)


def _allsky_selected(key: tuple[int, str], split: str, validation: set[str]) -> bool:
    """Whether one training-archive pair belongs to the requested split."""
    if split == "test":
        return True
    return (key[1] in validation) == (split == "validation")


def _allsky_members(
    archive: zipfile.ZipFile, split: str
) -> tuple[dict[tuple[int, str], zipfile.ZipInfo], dict[tuple[int, str], zipfile.ZipInfo]]:
    """Pair every selected all-sky JPEG with its semantic PNG."""
    test = split == "test"
    validation = set() if test else _allsky_validation(archive)
    images: dict[tuple[int, str], zipfile.ZipInfo] = {}
    masks: dict[tuple[int, str], zipfile.ZipInfo] = {}
    for member in archive.infolist():
        found = _allsky_member(member, test)
        if found is None:
            continue
        kind, key = found
        if not _allsky_selected(key, split, validation):
            continue
        destination = images if kind == "images" else masks
        if key in destination:
            raise ValueError(f"All-Sky Cloud Segmentation repeats member {key[1]!r}")
        destination[key] = member
    if not images or set(images) != set(masks):
        raise ValueError("All-Sky Cloud Segmentation has an unpaired image or mask")
    return images, masks


def _allsky_timestamp(stem: str, test: bool) -> int:
    """Keep the filename acquisition time as a sortable YYYYMMDDhhmmss integer."""
    value = stem[:14] if test else "20" + stem.rsplit("_", 1)[-1]
    if len(value) != 14 or not value.isdigit():
        raise ValueError(f"All-Sky Cloud Segmentation has an unknown timestamp {stem!r}")
    return int(value)


def _allsky_mask(archive: zipfile.ZipFile, member: zipfile.ZipInfo, stem: str) -> bytes:
    """Decode one semantic mask and reject unregistered pixel classes."""
    mask = _decoded_raster(
        archive,
        member,
        mode="L",
        size=(ALLSKY_SIDE, ALLSKY_SIDE),
        description=f"All-Sky Cloud Segmentation mask {stem!r}",
    )
    if not set(mask).issubset(range(len(ALLSKY_CLASSES))):
        raise ValueError(f"All-Sky Cloud Segmentation mask {stem!r} has an unknown class")
    return mask


def _allsky_entries(paths: Mapping[str, Path], split: str) -> Rows:
    """Yield paired, decoded all-sky images and masks using bounded memory."""
    _allsky_support(paths)
    role = "test_archive" if split == "test" else "train_archive"
    with zipfile.ZipFile(paths[role]) as archive:
        images, masks = _allsky_members(archive, split)
        for index, key in enumerate(sorted(images)):
            camera, stem = key
            picture = _decoded_raster(
                archive,
                images[key],
                mode="RGB",
                size=(ALLSKY_SIDE, ALLSKY_SIDE),
                description=f"All-Sky Cloud Segmentation image {stem!r}",
            )
            mask = _allsky_mask(archive, masks[key], stem)
            pixels = len(mask)
            latitude, longitude, altitude = ALLSKY_CAMERA_METADATA[camera]
            yield (
                0,
                {
                    "image": picture,
                    "mask": mask,
                    "class_fractions": array.array(
                        "f", (mask.count(value) / pixels for value in range(len(ALLSKY_CLASSES)))
                    ),
                    "camera": camera,
                    "latitude": latitude,
                    "longitude": longitude,
                    "altitude": altitude,
                    "timezone_minutes": 60,
                    "timestamp": _allsky_timestamp(stem, split == "test"),
                    "index": index,
                },
            )


def _allsky_clouds(paths: Mapping[str, Path], split: str) -> Loaded:
    columns: dict[str, Any] = {
        "image": ("B", ALLSKY_SIDE * ALLSKY_SIDE * 3),
        "mask": ("B", ALLSKY_SIDE * ALLSKY_SIDE),
        "class_fractions": ("f", len(ALLSKY_CLASSES)),
        "camera": "i",
        "latitude": "d",
        "longitude": "d",
        "altitude": "i",
        "timezone_minutes": "i",
        "timestamp": "q",
        "index": "i",
    }
    return ("samples",), columns, _allsky_entries(paths, split)


def _wikitext_entries(paths: Mapping[str, Path], split: str) -> Rows:
    parquet = importlib.import_module("pyarrow.parquet")
    roles = ("train_0", "train_1") if split == "train" else (split,)
    index = 0
    for role in roles:
        book = parquet.ParquetFile(paths[role])
        for batch in book.iter_batches(batch_size=4096, columns=["text"]):
            for value in batch.column(0).to_pylist():
                if not value or not value.strip():
                    continue
                encoded = value.encode("utf-8")
                if len(encoded) > 32_768:
                    raise ValueError(f"WikiText-103 row {index} exceeds 32 KiB")
                yield (
                    0,
                    {
                        "text": encoded + bytes(32_768 - len(encoded)),
                        "length": len(encoded),
                        "tokens": len(value.split()),
                        "index": index,
                    },
                )
                index += 1


def _wikitext(paths: Mapping[str, Path], split: str) -> Loaded:
    columns: dict[str, Any] = {
        "text": ("B", 32_768),
        "length": "i",
        "tokens": "i",
        "index": "q",
    }
    return ("rows",), columns, _wikitext_entries(paths, split)


def _load_prefixed(prefix: str, converter: str, paths: Mapping[str, Path], split: str) -> Loaded:
    if prefix == "the_well":
        from ._the_well import load as load_the_well

        return load_the_well(converter, paths, split)
    if prefix == "alex_mp20":
        from ._alex_mp20 import load as load_alex_mp20

        return load_alex_mp20(converter, paths, split)
    if prefix == "hub":
        from ._hub_open import load as load_hub

        return load_hub(converter, paths, split)
    if prefix == "vision":
        from ._physics_vision import load as load_vision

        return load_vision(converter, paths, split)
    if prefix == "condensed":
        from ._condensed_matter import load as load_condensed

        return load_condensed(converter, paths, split)
    if prefix == "jarvis":
        from ._jarvis_physics import load as load_jarvis

        return load_jarvis(converter, paths, split)
    raise ValueError(f"there is no open converter family {prefix!r}")


PathsLoader = Callable[[Mapping[str, Path]], Loaded]
SplitPathsLoader = Callable[[Mapping[str, Path], str], Loaded]
ArchiveLoader = Callable[[Path], Loaded]
SplitArchiveLoader = Callable[[Path, str], Loaded]

PATHS_LOADERS: dict[str, PathsLoader] = {
    "jetnet": _jetnet,
    "maize": _maize,
}
SPLIT_PATHS_LOADERS: dict[str, SplitPathsLoader] = {
    "wikitext": _wikitext,
    "wildlife_mnist": _wildlife,
    "audiomnist": _audiomnist,
    "allsky_clouds": _allsky_clouds,
}
ARCHIVE_LOADERS: dict[str, ArchiveLoader] = {
    "omnifold": _omnifold,
    "tinysol": _tinysol,
    "circor": _circor,
    "reefset": _reefset,
}
SPLIT_ARCHIVE_LOADERS: dict[str, SplitArchiveLoader] = {
    "speech_commands": _speech_commands,
    "biodcase": _biodcase,
    "birdset": _birdset,
    "sodv2": _sodv2,
}


def load(converter: str, paths: Mapping[str, Path], split: str) -> Loaded:
    """Open one registered non-UCI source using bounded memory."""
    prefix, separator, nested = converter.partition(":")
    if separator:
        return _load_prefixed(prefix, nested, paths, split)
    if converter in PATHS_LOADERS:
        return PATHS_LOADERS[converter](paths)
    if converter in SPLIT_PATHS_LOADERS:
        return SPLIT_PATHS_LOADERS[converter](paths, split)
    if converter in ARCHIVE_LOADERS:
        return ARCHIVE_LOADERS[converter](paths["archive"])
    if converter in SPLIT_ARCHIVE_LOADERS:
        return SPLIT_ARCHIVE_LOADERS[converter](paths["archive"], split)
    raise ValueError(f"there is no open large-archive converter {converter!r}")
