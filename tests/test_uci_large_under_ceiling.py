"""Tiny archive replicas for every newly admitted 100 MB--2 GB UCI source."""

from __future__ import annotations

import io
import zipfile

import pytest

from xrd.root._uci_large import load


def nested_zip(files):
    held = io.BytesIO()
    with zipfile.ZipFile(held, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return held.getvalue()


def test_pems_puf_and_daily_segment_layouts(tmp_path):
    pems = tmp_path / "pems.zip"
    with zipfile.ZipFile(pems, "w") as archive:
        archive.writestr("PEMS_train", "[" + " ".join(["0"] * (963 * 144)) + "]\n")
        archive.writestr("PEMS_trainlabels", "[1]\n")
    classes, columns, rows = load("pems", pems, "train")
    assert classes[0] == "monday" and columns["occupancy"] == ("f", 963 * 144)
    assert next(rows)[0] == 0

    puf = tmp_path / "puf.zip"
    with zipfile.ZipFile(puf, "w") as archive:
        archive.writestr("x/train_5xor_128dim.csv", ",".join(["0"] * 128 + ["1"]))
    _, columns, rows = load("puf", puf, "train_5xor")
    assert columns["challenge"] == ("b", 128) and next(rows)[0] == 1

    daily = tmp_path / "daily.zip"
    segment = (",".join(["0"] * 45) + "\n") * 125
    with zipfile.ZipFile(daily, "w") as archive:
        archive.writestr("data/a01/p1/s01.txt", segment)
    _, columns, rows = load("daily", daily, "all")
    label, row = next(rows)
    assert label == 0 and columns["readings"] == ("f", 5625) and row["subject"] == 1


def test_the_three_gas_formats_stream_raw_sensor_values(tmp_path):

    temperature = tmp_path / "temperature.zip"
    with zipfile.ZipFile(temperature, "w") as archive:
        archive.writestr("20160930_203718.csv", " ".join(["0"] * 20) + "\n")
    _, _, rows = load("gas_temperature", temperature, "all")
    assert next(rows)[0] == 0

    dynamic = tmp_path / "dynamic.zip"
    with zipfile.ZipFile(dynamic, "w") as archive:
        archive.writestr("ethylene_CO.txt", " ".join(["1"] * 19) + "\n")
    _, _, rows = load("gas_dynamic", dynamic, "all")
    label, row = next(rows)
    assert label == 0 and len(row["sensors"]) == 16

    twin = tmp_path / "twin.zip"
    recording = " ".join(["1"] * 9) + "\n"
    with zipfile.ZipFile(twin, "w") as archive:
        archive.writestr("B1_GEa_F040_R2.txt", recording * 60_001)
    _, columns, rows = load("twin_gas", twin, "all")
    label, row = next(rows)
    assert label == 0 and columns["sensors"] == ("f", 480_008)
    assert row["length"] == 60_001 and row["concentration"] == 40


def test_open_sampling_gas_is_partitioned_losslessly_by_chemical(tmp_path):
    open_sampling = tmp_path / "open-sampling.zip"
    filename = "130101010101_board_setPoint_5V_fan_setPoint_50_mfc_setPoint_acetone_100ppm_p1"
    cells = ["\u200e74710", *(["1"] * 8), "20", "40", *(["1"] * 81)]
    with zipfile.ZipFile(open_sampling, "w") as archive:
        archive.writestr("dataset/run/L1/batch_execution", "publisher control metadata\n")
        archive.writestr(f"dataset/run/L1/{filename}", " ".join(cells) + "\n")
    classes, _, rows = load("gas", open_sampling, "acetone")
    label, row = next(rows)
    assert classes == ("acetone",) and label == 0
    assert row["label"] == 1 and row["length"] == 1
    assert row["time_ms"][0] == 74_710
    assert list(load("gas", open_sampling, "methane")[2]) == []


def test_open_sampling_gas_reports_the_source_of_other_non_numeric_fields(tmp_path):
    open_sampling = tmp_path / "bad-open-sampling.zip"
    filename = "130101010101_board_setPoint_5V_fan_setPoint_50_mfc_setPoint_acetone_100ppm_p1"
    cells = ["0", "not-a-number", *(["1"] * 7), "20", "40", *(["1"] * 81)]
    member = f"dataset/run/L1/{filename}"
    with zipfile.ZipFile(open_sampling, "w") as archive:
        archive.writestr(member, " ".join(cells) + "\n")

    with pytest.raises(ValueError, match=rf"row 0 field 1 of {member} is not numeric"):
        next(load("gas", open_sampling, "acetone")[2])


def test_electricity_and_opportunity_preserve_wide_rows(tmp_path):
    electricity = tmp_path / "electricity.zip"
    header = ";".join(["timestamp", *(f"MT_{at}" for at in range(370))])
    values = ";".join(["2011-01-01 00:15:00", *(["1,5"] * 370)])
    with zipfile.ZipFile(electricity, "w") as archive:
        archive.writestr("LD2011_2014.txt", header + "\n" + values + "\n")
        archive.writestr("__MACOSX/._LD2011_2014.txt", b"resource fork")
    _, _, rows = load("electricity", electricity, "all")
    _, row = next(rows)
    assert row["loads"][0] == pytest.approx(1.5) and len(row["loads"]) == 370

    opportunity = tmp_path / "opportunity.zip"
    cells = ["0"] * 250
    cells[243] = "2"
    with zipfile.ZipFile(opportunity, "w") as archive:
        archive.writestr("OpportunityUCIDataset/dataset/S1-ADL1.dat", " ".join(cells) + "\n")
    _, columns, rows = load("opportunity", opportunity, "all")
    label, row = next(rows)
    assert label == 2 and columns["features"] == ("f", 243) and len(row["features"]) == 243


def test_nested_p53_pamap_and_hhar_archives(tmp_path):
    p53 = tmp_path / "p53.zip"
    line = ",".join(["0"] * 5408 + ["active"]) + "\n"
    with zipfile.ZipFile(p53, "w") as archive:
        archive.writestr("p53_new_2012.zip", nested_zip({"K8.data": line}))
    _, columns, rows = load("p53", p53, "all")
    label, row = next(rows)
    assert label == 1 and columns["features"] == ("f", 5408) and len(row["features"]) == 5408

    current_p53 = tmp_path / "current-p53.zip"
    current_line = line.rstrip("\n") + ",\n"
    with zipfile.ZipFile(current_p53, "w") as archive:
        archive.writestr(
            "p53_new_2012.zip", nested_zip({"Data Sets/K9.data": current_line})
        )
    current_label, current_row = next(load("p53", current_p53, "all")[2])
    assert current_label == 1 and len(current_row["features"]) == 5408

    pamap = tmp_path / "pamap.zip"
    cells = ["0", "1", *(["nan"] * 52)]
    with zipfile.ZipFile(pamap, "w") as archive:
        archive.writestr(
            "PAMAP2_Dataset.zip",
            nested_zip({"PAMAP2_Dataset/Protocol/subject101.dat": " ".join(cells) + "\n"}),
        )
    _, columns, rows = load("pamap2", pamap, "all")
    label, row = next(rows)
    assert label == 1 and columns["features"] == ("f", 52) and row["subject"] == 101

    hhar = tmp_path / "hhar.zip"
    csv = "Index,Arrival_Time,Creation_Time,x,y,z,User,Model,Device,gt\n0,1,2,3,4,5,a,b,c,walk\n"
    with zipfile.ZipFile(hhar, "w") as archive:
        archive.writestr(
            "Activity recognition exp.zip",
            nested_zip({"Phones_accelerometer.csv": csv}),
        )
    _, columns, rows = load("hhar", hhar, "all")
    label, row = next(rows)
    assert label == 4 and columns["xyz"] == ("f", 3) and list(row["xyz"]) == [3, 4, 5]


def test_year_prediction_keeps_all_timbre_features_and_release_year(tmp_path):
    source = tmp_path / "year.zip"
    row = ",".join(["2001", *(str(at / 10) for at in range(90))])
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("YearPredictionMSD.txt", row + "\n")
    classes, columns, rows = load("year_prediction", source, "train")
    assert classes == ("rows",) and columns["features"] == ("f", 90)
    label, made = next(rows)
    assert label == 0 and made["target"] == 2001
    assert list(made["features"][:3]) == pytest.approx([0.0, 0.1, 0.2])
