"""Small, local replicas of the non-UCI large archive formats."""

from __future__ import annotations

import io
import json
import struct
import tarfile
import wave
import zipfile

import pytest

from xrd.root import _open_large as open_large_module
from xrd.root import open_root
from xrd.root._open_large import (
    ALLSKY_CLASSES,
    ALLSKY_SIDE,
    BIRDSET_CLASSES,
    MAIZE_CLASSES,
    MAIZE_TASKS,
    MAIZE_TILE,
    SOD_HEIGHT,
    SOD_MAX_OBJECTS,
    SOD_WIDTH,
    load,
)
from xrd.root.datasets import convert


def _wav(samples: bytes, *, rate: int = 16_000) -> bytes:
    held = io.BytesIO()
    with wave.open(held, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(rate)
        recording.writeframes(samples)
    return held.getvalue()


def _npy(values: list[float]) -> bytes:
    header = b"{'descr': '<f4', 'fortran_order': False, 'shape': (1536,), }"
    header += b" " * ((-(10 + len(header) + 1)) % 16) + b"\n"
    return (
        b"\x93NUMPY\x01\x00"
        + len(header).to_bytes(2, "little")
        + header
        + struct.pack("<1536f", *values)
    )


def _picture(mode: str, size: tuple[int, int], value, kind: str) -> bytes:
    image_module = pytest.importorskip("PIL.Image")
    held = io.BytesIO()
    image_module.new(mode, size, value).save(held, format=kind)
    return held.getvalue()


def test_omnifold_accepts_the_publishers_event_number_before_the_level(tmp_path):
    source = tmp_path / "omnifold.zip"
    rows = "0 truth 220 1 2 3 4 5 6 7 8 9 10 nan 11\n0 reco 221 2 3 4 5 6 7 8 9 10 11 0.2 12\n"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("test_Omni.txt", rows)
    classes, columns, entries = load("omnifold", {"archive": source}, "all")
    assert classes == ("generator", "detector") and columns["jet"] == ("f", 4)
    made = list(entries)
    assert [label for label, _ in made] == [0, 1]
    assert made[0][1]["zg"] == 0 and made[1][1]["multiplicity"] == 12


def test_omnifold_streams_every_numbered_shard_in_numeric_order(tmp_path):
    source = tmp_path / "omnifold-numbered.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            "OmniFold_Big/OmniFold_Big_10.txt", "0 reco 230 1 2 3 4 5 6 7 8 9 10 0.3 11\n"
        )
        archive.writestr(
            "OmniFold_Big/OmniFold_Big_2.txt", "0 truth 220 1 2 3 4 5 6 7 8 9 10 0.2 10\n"
        )
        archive.writestr(
            "OmniFold_Big/OmniFold_Big_1.txt", "0 truth 210 1 2 3 4 5 6 7 8 9 10 0.1 9\n"
        )

    made = list(load("omnifold", {"archive": source}, "all")[2])

    assert [row["z_pt"] for _tree, row in made] == [210, 220, 230]
    assert [row["index"] for _tree, row in made] == [0, 1, 2]


def test_wikitext_keeps_nonempty_pretokenized_lines_in_each_split(tmp_path):
    pyarrow = pytest.importorskip("pyarrow")
    parquet = pytest.importorskip("pyarrow.parquet")
    source = tmp_path / "validation.parquet"
    parquet.write_table(pyarrow.table({"text": ["", " = Heading = ", " two tokens "]}), source)
    classes, columns, entries = load("wikitext", {"validation": source}, "validation")
    assert classes == ("rows",) and columns["text"] == ("B", 32_768)
    made = list(entries)
    assert len(made) == 2 and made[1][1]["tokens"] == 2
    assert bytes(made[1][1]["text"][:11]) == b" two tokens"


def test_tinysol_reads_pcm_and_uses_the_instrument_code_in_the_filename(tmp_path):
    wav = io.BytesIO()
    with wave.open(wav, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(44_100)
        recording.writeframes(b"\x01\x00\x02\x00")
    source = tmp_path / "tinysol.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        info = tarfile.TarInfo("TinySOL2020/Winds/Bassoon/ordinario/Bn-ord-G#4-ff-N-N.wav")
        info.size = len(wav.getvalue())
        archive.addfile(info, io.BytesIO(wav.getvalue()))
    classes, columns, entries = load("tinysol", {"archive": source}, "all")
    assert classes[3] == "bassoon" and columns["audio"] == ("f", 441_000)
    label, row = next(entries)
    assert label == 3 and row["length"] == 2 and row["sample_rate"] == 44_100
    assert row["audio"][:3] == pytest.approx([1 / 32768, 2 / 32768, 0])


def test_speech_commands_preserves_lists_and_turns_noise_into_training_windows(tmp_path):
    source = tmp_path / "speech.tar.gz"
    yes = _wav(struct.pack("<2h", 32767, -32768))
    no = _wav(struct.pack("<2h", 1, 2))
    noise = _wav(struct.pack("<32000h", *([3] * 32000)))
    with tarfile.open(source, "w:gz") as archive:
        members = {
            "speech_commands_v0.01/validation_list.txt": b"yes/alice_nohash_0.wav\n",
            "speech_commands_v0.01/testing_list.txt": b"",
            "speech_commands_v0.01/yes/alice_nohash_0.wav": yes,
            "speech_commands_v0.01/no/bob_nohash_1.wav": no,
            "speech_commands_v0.01/_background_noise_/room.wav": noise,
        }
        for name, raw in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    classes, columns, entries = load("speech_commands", {"archive": source}, "validation")
    assert classes[-1] == "background_noise" and columns["audio"] == ("f", 16_000)
    label, row = next(entries)
    assert classes[label] == "yes" and row["speaker"].rstrip(b"\0") == b"alice"
    assert row["audio"][:2] == pytest.approx([32767 / 32768, -1])
    _, _, training = load("speech_commands", {"archive": source}, "train")
    made = list(training)
    assert [classes[label] for label, _ in made] == ["no", "background_noise", "background_noise"]
    assert [row["length"] for _, row in made] == [2, 16_000, 16_000]


@pytest.mark.parametrize("tar_mode", ["w", "w:gz"])
def test_audiomnist_joins_mirror_splits_to_the_authors_speaker_metadata(
    tmp_path, tar_mode
):
    source = tmp_path / "train.tar.gz"
    with tarfile.open(source, tar_mode) as archive:
        raw = _wav(struct.pack("<2h", 16384, -16384), rate=48_000)
        info = tarfile.TarInfo("dataset/01/7_01_3.wav")
        info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))
    metadata = {
        f"{speaker:02d}": {
            "accent": "German",
            "age": "30",
            "gender": "male",
            "native speaker": "no",
            "origin": "Europe, Germany",
        }
        for speaker in range(1, 61)
    }
    meta = tmp_path / "metadata.json"
    meta.write_text(json.dumps(metadata))
    paths = {"train": source, "validation": source, "test": source, "metadata": meta}
    classes, columns, entries = load("audiomnist", paths, "train")
    assert classes == tuple(str(value) for value in range(10))
    assert columns["audio"] == ("f", 48_000)
    label, row = next(entries)
    assert label == 7 and row["speaker"] == 1 and row["repetition"] == 3
    assert row["age"] == 30 and row["origin"].rstrip(b"\0") == b"Europe, Germany"
    assert row["audio"][:3] == pytest.approx([0.5, -0.5, 0])


def test_circor_segments_long_recordings_and_retains_patient_labels(tmp_path):
    source = tmp_path / "circor.zip"
    header = "Patient ID,Age,Sex,Height,Weight,Pregnancy status,Murmur,Murmur locations,Outcome\n"
    patient = "2530,Child,Female,98.0,15.9,False,Present,AV+MV,Abnormal\n"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("circor/training_data.csv", header + patient)
        archive.writestr(
            "circor/training_data/2530_AV.wav",
            _wav(struct.pack("<25000h", *([8192] * 25000)), rate=4_000),
        )
    classes, columns, entries = load("circor", {"archive": source}, "all")
    assert classes == ("murmur_absent", "murmur_present", "murmur_unknown")
    assert columns["audio"] == ("f", 20_000)
    made = list(entries)
    assert [label for label, _ in made] == [1, 1]
    assert [row["length"] for _, row in made] == [20_000, 5_000]
    assert made[1][1]["offset"] == 20_000 and made[1][1]["audio"][4_999] == 0.25
    assert made[1][1]["audio"][5_000] == 0 and made[0][1]["outcome"] == 1


def test_jetnet_reads_all_five_hdf5_shards_without_joining_them_in_memory(tmp_path):
    h5py = pytest.importorskip("h5py")
    numpy = pytest.importorskip("numpy")
    paths = {}
    for role in ("gluon", "light_quark", "top", "w_boson", "z_boson"):
        path = tmp_path / f"{role}.hdf5"
        with h5py.File(path, "w") as book:
            book["jet_features"] = numpy.arange(4, dtype="float32").reshape(1, 4)
            book["particle_features"] = numpy.arange(120, dtype="float32").reshape(1, 30, 4)
        paths[role] = path
    classes, columns, entries = load("jetnet", paths, "all")
    assert len(classes) == 5 and columns["particle_features"] == ("f", 120)
    made = list(entries)
    assert [label for label, _ in made] == list(range(5))
    assert list(made[-1][1]["particle_features"][-2:]) == [118.0, 119.0]


def test_reefset_joins_json_provenance_to_fixed_width_pcm(tmp_path):
    source = tmp_path / "reefset.zip"
    annotations = [
        {
            "id": 7,
            "file_name": "7.somewhere.someone.ambient.wav",
            "label": "ambient",
            "data_sharer": "someone",
            "dataset": "somewhere",
            "recorder": "hydromoth",
        },
        {
            "id": 8,
            "file_name": "8.somewhere.someone.bioph.wav",
            "label": "bioph",
            "data_sharer": "someone",
            "dataset": "somewhere",
            "recorder": "soundtrap300",
        },
    ]
    with zipfile.ZipFile(source, "w") as archive:
        for item in annotations:
            archive.writestr(
                f"ReefSet_v1.0/full_dataset/{item['file_name']}", _wav(b"\x01\x00\x02\x00")
            )
        archive.writestr("ReefSet_v1.0/reefset_annotations.json", json.dumps(annotations))
    classes, columns, entries = load("reefset", {"archive": source}, "all")
    assert len(classes) == 37 and columns["audio"] == ("f", 30_720)
    made = list(entries)
    assert [label for label, _ in made] == [0, 4]
    assert made[0][1]["audio"][:3] == pytest.approx([1 / 32768, 2 / 32768, 0])
    assert made[0][1]["length"] == 2 and made[0][1]["source_id"] == 7
    assert made[0][1]["dataset"].rstrip(b"\0") == b"somewhere"


def test_reefset_normalizes_the_publishers_32_bit_pcm_recording(tmp_path):
    source = tmp_path / "reefset.zip"
    name = "8610.indonesia_bombs.b_williams_ucl.anthrop_bomb.wav"
    held = io.BytesIO()
    with wave.open(held, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(4)
        recording.setframerate(16_000)
        recording.writeframes(struct.pack("<3i", 1, 1_073_741_824, -2_147_483_648))
    annotation = {
        "id": 8610,
        "file_name": name,
        "label": "anthrop_bomb",
        "data_sharer": "b_williams_ucl",
        "dataset": "indonesia_bombs",
        "recorder": "unknown",
    }
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(f"ReefSet_v1.0/full_dataset/{name}", held.getvalue())
        archive.writestr("ReefSet_v1.0/reefset_annotations.json", json.dumps([annotation]))

    label, row = next(load("reefset", {"archive": source}, "all")[2])

    assert label == 2 and row["length"] == 3 and row["sample_rate"] == 16_000
    assert row["audio"][:4] == pytest.approx([1 / 2_147_483_648, 0.5, -1.0, 0.0])


def test_biodcase_preserves_split_class_and_filename_metadata(tmp_path):
    source = tmp_path / "biodcase.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("Training_Set/Negatives/Background_0159.wav", _wav(b"\x01\x00\x02\x00"))
        archive.writestr(
            "Training_Set/Yellowhammer/YH_063_grassland_D.wav",
            _wav(b"\x03\x00\x04\x00"),
        )
        archive.writestr("Validation_Set/Negatives/Bird_0508.wav", _wav(b"\x05\x00\x06\x00"))
    classes, columns, entries = load("biodcase", {"archive": source}, "train")
    assert classes == ("negative", "yellowhammer")
    assert columns["audio"] == ("f", 32_000)
    made = list(entries)
    assert [label for label, _ in made] == [0, 1]
    assert made[0][1]["kind"].rstrip(b"\0") == b"background"
    assert made[1][1]["song_id"] == 63
    assert made[1][1]["location"].rstrip(b"\0") == b"grassland"
    assert made[1][1]["distance"] == b"D"

    target = tmp_path / "biodcase.root"
    convert(
        "biodcase_2025_task3",
        target,
        split="train",
        parts={"archive": source},
    )
    with open_root(target) as back:
        assert list(back["train_yellowhammer"]["distance"].array()) == [ord("D")]


def test_birdset_safely_joins_multilabel_embeddings_and_provenance(tmp_path):
    source = tmp_path / "birdset.zip"
    labels_header = "filename,label,split,validation\n"
    metadata_header = "filename,original_filepath,start_time,end_time,lat,long,license,source\n"
    rows = {
        "HSN": ("hsn.npy", "akepa1;yerwar", "train"),
        "POW": ("pow.npy", "", "validation"),
        "UHH": ("uhh.npy", "iiwi", "train"),
    }
    with zipfile.ZipFile(source, "w") as archive:
        for offset, (subset, (name, labels, split)) in enumerate(rows.items()):
            root = f"BirdSet_BASEAL/{subset}_BASEAL/"
            archive.writestr(root + "labels.csv", labels_header + f"{name},{labels},{split},0\n")
            archive.writestr(
                root + "metadata.csv",
                metadata_header + f"{name},recordings/{name},1.25,4.5,19.0,-155.0,CC BY 4.0,"
                "https://example.invalid/source\n",
            )
            archive.writestr(
                root + "embeddings/perch_v2/" + name,
                _npy([float(offset)] * 1536),
            )
    classes, columns, entries = load("birdset", {"archive": source}, "train")
    assert classes == ("samples",) and len(BIRDSET_CLASSES) == 80
    assert columns["embedding"] == ("f", 1536) and columns["label"] == ("f", 80)
    made = list(entries)
    assert len(made) == 2 and [row[1]["subset"] for row in made] == [0, 2]
    assert made[0][1]["label"][0] == 1 and made[0][1]["label"][-1] == 1
    assert sum(made[1][1]["label"]) == 1 and made[1][1]["embedding"][0] == 2
    assert b"recordings/hsn.npy" in made[0][1]["entry"]


def test_birdset_refuses_an_object_or_wrong_width_numpy_embedding(tmp_path):
    source = tmp_path / "birdset.zip"
    with zipfile.ZipFile(source, "w") as archive:
        for subset in ("HSN", "POW", "UHH"):
            root = f"BirdSet_BASEAL/{subset}_BASEAL/"
            archive.writestr(root + "labels.csv", "filename,label,split\nbad.npy,iiwi,train\n")
            archive.writestr(
                root + "metadata.csv",
                "filename,original_filepath,start_time,end_time,lat,long,license,source\n"
                "bad.npy,bad.wav,0,1,,,CC0,https://example.invalid\n",
            )
            archive.writestr(root + "embeddings/perch_v2/bad.npy", b"not a numpy file")
    _, _, entries = load("birdset", {"archive": source}, "train")
    with pytest.raises(ValueError, match="not a NumPy array"):
        next(entries)


def test_maize_merges_shards_and_separates_source_and_processed_trees(tmp_path, monkeypatch):
    numpy = pytest.importorskip("numpy")
    image_module = pytest.importorskip("PIL.Image")
    tifffile = pytest.importorskip("tifffile")
    paths = _maize_sources(tmp_path, monkeypatch, numpy, image_module, tifffile)
    _assert_maize_load(paths)
    _assert_maize_conversion(paths)


def _maize_sources(tmp_path, monkeypatch, numpy, image_module, tifffile):
    for task, (width, height) in {"water": (3, 2), "rust": (2, 3)}.items():
        monkeypatch.setitem(MAIZE_TASKS[task], "width", width)
        monkeypatch.setitem(MAIZE_TASKS[task], "height", height)
    return {
        "readme": _maize_readme(tmp_path),
        "metadata": _maize_test_metadata(tmp_path),
        "orthomosaics": _maize_test_orthomosaics(tmp_path, numpy, tifffile),
        "patches": _maize_test_patches(tmp_path, numpy, image_module),
    }


def _maize_readme(tmp_path):
    path = tmp_path / "readme.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "00_README_CITATION_LICENSE/LICENSE.txt", "Creative Commons Attribution 4.0"
        )
    return path


def _maize_test_metadata(tmp_path):
    path = tmp_path / "metadata.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for task in ("water", "rust"):
            details = MAIZE_TASKS[task]
            raster = {
                "width_pixels": details["width"],
                "height_pixels": details["height"],
                "number_of_bands": 6,
                "nodata_value": -10_000.0,
                "crs": "EPSG:32636",
                "transform": {"a": 0.5, "b": 0, "c": 10, "d": 0, "e": -0.5, "f": 20},
            }
            archive.writestr(
                f"03_metadata/{details['metadata']}", json.dumps({"raster_properties": raster})
            )
    return path


def _maize_test_orthomosaics(tmp_path, numpy, tifffile):
    path = tmp_path / "orthomosaics.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for offset, task in enumerate(("water", "rust")):
            details = MAIZE_TASKS[task]
            image = numpy.full(
                (details["height"], details["width"], 6), offset + 1, dtype="float32"
            )
            held = io.BytesIO()
            tifffile.imwrite(
                held, image, photometric="minisblack", planarconfig="contig", metadata=None
            )
            archive.writestr(f"01_orthomosaics/{task}/{details['tiff']}", held.getvalue())
    return path


def _maize_test_patches(tmp_path, numpy, image_module):
    patch_name = "mix_p0000001__water__water_p0000001_y0_x0_t224"
    image_bytes = io.BytesIO()
    numpy.save(
        image_bytes,
        numpy.full((MAIZE_TILE, MAIZE_TILE, 6), 2, dtype="float16"),
        allow_pickle=False,
    )
    mask_bytes = io.BytesIO()
    image_module.fromarray(numpy.zeros((MAIZE_TILE, MAIZE_TILE), dtype="uint8")).save(
        mask_bytes, format="PNG"
    )
    fields = (
        "id,src_task,x0,y0,x1,y1,tile_h,tile_w,channels,valid_ratio,fg_ratio,is_negative,"
        "ratio_c0,ratio_c1,ratio_c2,ratio_c3,ratio_c4,geo_x0,geo_y0,geo_x1,geo_y1,crs"
    )
    values = (
        f"{patch_name},water,0,0,3,2,224,224,6,{6 / MAIZE_TILE**2},0,1,"
        "1,0,0,0,0,10,20,122,-92,EPSG:32636"
    )
    path = tmp_path / "patches.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "02_processed_patches/meta/class_map.json",
            json.dumps({"class_map": {name: at for at, name in enumerate(MAIZE_CLASSES)}}),
        )
        archive.writestr("02_processed_patches/meta/patches.csv", fields + "\n" + values + "\n")
        archive.writestr(f"02_processed_patches/images/{patch_name}.npy", image_bytes.getvalue())
        archive.writestr(f"02_processed_patches/masks/{patch_name}.png", mask_bytes.getvalue())
    return path


def _assert_maize_load(paths):
    classes, columns, entries = load("maize", paths, "all")
    assert classes == ("orthomosaic_water", "orthomosaic_rust", "patch_water", "patch_rust")
    assert columns["image"] == ("f", 224 * 224 * 6)
    assert columns["label"] == ("B", 224 * 224)
    made = list(entries)
    assert [tree for tree, _ in made] == [0, 1, 2]
    assert made[0][1]["image"][0] == 1 and made[0][1]["label"][0] == 255
    assert made[0][1]["width"] == 3 and made[0][1]["height"] == 2
    assert made[2][1]["image"][0] == 2 and made[2][1]["label"][0] == 0
    assert made[2][1]["width"] == 3 and made[2][1]["height"] == 2
    assert made[2][1]["stage"] == 1
    assert made[2][1]["class_ratios"] == pytest.approx([1, 0, 0, 0, 0])


def _assert_maize_conversion(paths):
    output = io.BytesIO()
    written = convert("uav_maize_stress", output, parts=paths)
    assert written == {
        "orthomosaic_water": 1,
        "orthomosaic_rust": 1,
        "patch_water": 1,
        "patch_rust": 0,
    }
    with open_root(io.BytesIO(output.getvalue())) as back:
        assert len(back["patch_water"]["image"].array()) == 224 * 224 * 6
        assert len(back["patch_water"]["label"].array()) == 224 * 224


def test_wildlife_mnist_preserves_nonmixed_and_mixed_factor_labels(tmp_path, monkeypatch):
    numpy = pytest.importorskip("numpy")
    monkeypatch.setattr(open_large_module, "WILDLIFE_ROWS", 2)
    paths = {}
    arrays = {
        "train_images": numpy.full((2, 3, 32, 32), -0.5, dtype="float32"),
        "train_labels": numpy.array([2, 7], dtype="int64"),
        "test_images": numpy.full((2, 3, 32, 32), 0.25, dtype="float32"),
        "test_labels": numpy.array([[2, 4, 6], [7, 8, 9]], dtype="int64"),
    }
    for role, values in arrays.items():
        path = tmp_path / f"{role}.npy"
        numpy.save(path, values, allow_pickle=False)
        paths[role] = path
    for role in ("preview_train", "preview_digit", "preview_background", "preview_texture"):
        path = tmp_path / f"{role}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\npreview")
        paths[role] = path

    classes, columns, train = load("wildlife_mnist", paths, "train")
    assert classes == tuple(str(value) for value in range(10))
    assert columns["image"] == ("f", 3072)
    _assert_wildlife_entries(list(train), -0.5, (2, 2, 2))

    _, _, test = load("wildlife_mnist", paths, "test")
    _assert_wildlife_entries(list(test), 0.25, (2, 4, 6))
    output = io.BytesIO()
    written = convert("wildlife_mnist", output, split="test", parts=paths)
    assert written["test_2"] == 1 and written["test_7"] == 1
    with open_root(io.BytesIO(output.getvalue())) as back:
        assert len(back["test_2"]["image"].array()) == 3072
        assert back["test_2"]["background"].array().tolist() == [4]


def _assert_wildlife_entries(made, pixel, labels):
    assert [tree for tree, _ in made] == [2, 7]
    assert made[0][1]["image"][0] == pytest.approx(pixel)
    row = made[0][1]
    assert (row["label"], row["background"], row["foreground"]) == labels


def test_sodv2_preserves_distance_trees_and_padded_yolo_boxes(tmp_path):
    source = tmp_path / "sodv2.zip"
    image = _picture("RGB", (SOD_WIDTH, SOD_HEIGHT), (10, 20, 30), "JPEG")
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("snapshot/README.md", "licensed under CC BY 4.0")
        archive.writestr("snapshot/train/0.5km-2km/images/image0153.jpg", image)
        archive.writestr(
            "snapshot/train/0.5km-2km/labels/image0153.txt",
            "0 0.5 0.4 0.2 0.1\n0 0.25 0.75 0.1 0.2\n",
        )
    classes, columns, entries = load("sodv2", {"archive": source}, "train")
    assert classes == ("near_0_to_0_5_km", "mid_0_5_to_2_km", "far_2_to_5_km")
    assert columns["image"] == ("B", SOD_WIDTH * SOD_HEIGHT * 3)
    assert columns["boxes"] == ("f", SOD_MAX_OBJECTS * 4)
    tree, row = next(entries)
    assert tree == 1 and row["objects"] == 2 and row["image_id"] == 153
    assert len(row["image"]) == SOD_WIDTH * SOD_HEIGHT * 3
    assert row["boxes"][:8] == pytest.approx([0.5, 0.4, 0.2, 0.1, 0.25, 0.75, 0.1, 0.2])
    assert list(row["object_labels"][:3]) == [0, 0, -1]

    output = io.BytesIO()
    assert convert("sodv2", output, split="train", parts={"archive": source}) == {
        "train_near_0_to_0_5_km": 0,
        "train_mid_0_5_to_2_km": 1,
        "train_far_2_to_5_km": 0,
    }
    with open_root(io.BytesIO(output.getvalue())) as back:
        assert back["train_mid_0_5_to_2_km"]["objects"].array().tolist() == [2]


def _allsky_support_files(tmp_path):
    paths = {}
    classes = tmp_path / "classes.yaml"
    classes.write_text(
        "0: camera mask\n1: sky\n2: low-layer clouds\n"
        "3: mid-layer clouds\n4: high-layer clouds\n"
    )
    paths["classes"] = classes
    metadata = tmp_path / "meta_data.yaml"
    metadata.write_text(
        "Cloud_Cam_Kontas: 37.0952\nCloud_Cam_Metas: 37.0916\n"
        "Cloud_Cam_PVotQ71: 37.0941\nCloud_Cam_PVotSky: 37.0941\n"
    )
    paths["metadata"] = metadata
    notebook = tmp_path / "display.ipynb"
    notebook.write_text(json.dumps({"cells": []}))
    paths["notebook"] = notebook
    return paths


def test_allsky_clouds_preserves_pixel_masks_cameras_and_official_splits(tmp_path):
    paths = _allsky_support_files(tmp_path)
    picture = _picture("RGB", (ALLSKY_SIDE, ALLSKY_SIDE), (1, 2, 3), "JPEG")
    train_mask = _picture("L", (ALLSKY_SIDE, ALLSKY_SIDE), 2, "PNG")
    validation_mask = _picture("L", (ALLSKY_SIDE, ALLSKY_SIDE), 4, "PNG")
    train = tmp_path / "train.zip"
    with zipfile.ZipFile(train, "w") as archive:
        archive.writestr("kontas_2017/validation.csv", "fileNames\nasi_002_170425132700,\n")
        for stem, mask in (
            ("asi_001_170328164030", train_mask),
            ("asi_002_170425132700", validation_mask),
        ):
            archive.writestr(f"kontas_2017/images/{stem}.jpg", picture)
            archive.writestr(f"kontas_2017/seg_masks/{stem}.png", mask)
    test = tmp_path / "test.zip"
    stem = "20230208102802_160"
    with zipfile.ZipFile(test, "w") as archive:
        archive.writestr(f"test_set/images/Cloud_Cam_PVot_Q71/{stem}.jpg", picture)
        archive.writestr(f"test_set/seg_masks/Cloud_Cam_PVot_Q71/{stem}.png", train_mask)
    paths.update({"train_archive": train, "test_archive": test})

    classes, columns, entries = load("allsky_clouds", paths, "validation")
    assert classes == ("samples",) and len(ALLSKY_CLASSES) == 5
    assert columns["mask"] == ("B", ALLSKY_SIDE * ALLSKY_SIDE)
    _, row = next(entries)
    assert set(row["mask"]) == {4} and row["class_fractions"] == pytest.approx([0, 0, 0, 0, 1])
    assert row["camera"] == 0 and row["timestamp"] == 20170425132700

    _, _, test_entries = load("allsky_clouds", paths, "test")
    _, test_row = next(test_entries)
    assert test_row["camera"] == 2 and test_row["timestamp"] == 20230208102802
    assert test_row["latitude"] == pytest.approx(37.0941)

    output = io.BytesIO()
    assert convert(
        "allsky_cloud_segmentation", output, split="test", parts=paths
    ) == {"test_samples": 1}
    with open_root(io.BytesIO(output.getvalue())) as back:
        assert back["test_samples"]["camera"].array().tolist() == [2]
