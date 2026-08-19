"""The ``xrd-datasets`` tool: a datasets directory built, checked and published.

Everything here converts a registry of the test's own - two tiny tables,
one of them under a licence that forbids passing it on - with the downloads
answered from a directory via ``--base``, so no test touches the network.
"""

from __future__ import annotations

import json

import pytest

from xrd.cli import datasets as datasets_cli
from xrd.root import open_root
from xrd.root.datasets import DATASETS, Table

FLOWERS = Table(
    name="flowers",
    label="Flowers",
    title="a few flowers",
    licence="CC0",
    source="https://example.invalid/flowers",
    classes=("red", "blue"),
    url="https://example.invalid/flowers.csv",
    fields=(("width", "d"), ("count", "i"), ("kind", "label")),
    labels={"Red": 0, "Blue": 1},
)

#: The same shape under a licence :func:`redistributable` refuses.
CLOSED = Table(
    name="closed",
    label="Closed",
    title="flowers you may not pass on",
    licence="CC BY-NC 4.0",
    source="https://example.invalid/closed",
    url="https://example.invalid/closed.csv",
    classes=("red", "blue"),
    fields=(("width", "d"), ("count", "i"), ("kind", "label")),
    labels={"Red": 0, "Blue": 1},
)

ROWS = b"1.5,3,Red\n2.5,4,Blue\n3.5,5,Red\n"


@pytest.fixture
def registry(monkeypatch):
    """The test's own datasets, alongside the real ones, for one test."""
    monkeypatch.setitem(DATASETS, "flowers", FLOWERS)
    monkeypatch.setitem(DATASETS, "closed", CLOSED)


@pytest.fixture
def mirror(tmp_path):
    """A directory answering the downloads, handed to ``--base``."""
    source = tmp_path / "mirror"
    source.mkdir()
    (source / "flowers.csv").write_bytes(ROWS)
    (source / "closed.csv").write_bytes(ROWS)
    return f"{source}/"


@pytest.fixture
def out(tmp_path):
    return tmp_path / "site"


def run(argv, capsys):
    """Run ``xrd-datasets`` and hand back ``(exit code, stdout, stderr)``."""
    code = datasets_cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def built(out, mirror, capsys, *extra):
    code, _, err = run(["build", str(out), "--only", "flowers", "--base", mirror, "-q", *extra],
                       capsys)
    assert code == 0, err
    return json.loads((out / "index.json").read_text())


# --- list -------------------------------------------------------------------


def test_list_names_every_dataset_and_flags_the_withheld_ones(registry, capsys):
    code, output, _ = run(["list", "--only", "flowers", "--only", "closed"], capsys)
    assert code == 0
    assert "flowers" in output and "a few flowers" in output
    assert "closed" in output and "[not redistributable]" in output
    assert "[not redistributable]" not in output.splitlines()[-1]  # flowers is free to share


def test_list_json_carries_the_licence_verdict(registry, capsys):
    code, output, _ = run(["list", "--only", "closed", "--json"], capsys)
    assert code == 0
    assert json.loads(output) == [
        {
            "name": "closed",
            "title": "flowers you may not pass on",
            "licence": "CC BY-NC 4.0",
            "redistributable": False,
        }
    ]


def test_a_glob_that_matches_nothing_is_an_error_that_says_so(capsys):
    code, _, err = run(["list", "--only", "nosuchset"], capsys)
    assert code == 1
    assert "no dataset matches nosuchset" in err


# --- build ------------------------------------------------------------------


def test_build_converts_writes_the_index_and_the_manifest(registry, mirror, out, capsys):
    index = built(out, mirror, capsys)
    (entry,) = index["datasets"]
    assert entry["name"] == "flowers"
    assert entry["licence"] == "CC0"
    assert entry["redistributable"] is True
    assert entry["trees"] == {"red": 2, "blue": 1}
    assert entry["rows"] == 3
    assert entry["bytes"] == (out / "flowers.root").stat().st_size
    assert f"{entry['adler32']}" in (out / "MANIFEST").read_text()
    assert "flowers.root" in (out / "MANIFEST").read_text()
    with open_root(str(out / "flowers.root")) as back:
        assert sorted(back.keys()) == ["about", "blue", "red"]


def test_build_keeps_what_is_already_there_and_force_starts_over(registry, mirror, out, capsys):
    built(out, mirror, capsys)
    before = (out / "flowers.root").stat().st_mtime_ns
    code, output, _ = run(["build", str(out), "--only", "flowers", "--base", mirror], capsys)
    assert code == 0
    assert "kept" in output
    assert (out / "flowers.root").stat().st_mtime_ns == before
    built(out, mirror, capsys, "--force")
    assert (out / "flowers.root").stat().st_mtime_ns != before


def test_build_refuses_a_licence_that_withholds_redistribution(registry, mirror, out, capsys):
    code, _, err = run(["build", str(out), "--only", "closed", "--base", mirror], capsys)
    assert code == 1
    assert "does not allow redistribution" in err and "--all" in err
    assert not (out / "closed.root").exists()


def test_build_all_converts_it_anyway_and_the_index_says_what_it_is(
    registry, mirror, out, capsys
):
    code, _, err = run(
        ["build", str(out), "--only", "closed", "--base", mirror, "-q", "--all"], capsys
    )
    assert code == 0, err
    index = json.loads((out / "index.json").read_text())
    (entry,) = index["datasets"]
    assert entry["name"] == "closed"
    assert entry["redistributable"] is False
    assert (out / "closed.root").exists()


def test_a_download_that_fails_fails_that_dataset_and_no_other(registry, out, tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    code, _, err = run(["build", str(out), "--only", "flowers", "--base", f"{empty}/"], capsys)
    assert code == 1
    assert "flowers" in err
    assert not (out / "flowers.root").exists()  # no half-written file left behind
    assert json.loads((out / "index.json").read_text())["datasets"] == []


# --- verify -----------------------------------------------------------------


def test_verify_blesses_a_directory_that_matches_its_index(registry, mirror, out, capsys):
    built(out, mirror, capsys)
    code, output, _ = run(["verify", str(out)], capsys)
    assert code == 0
    assert "1 of 1 files match the index" in output


def test_verify_catches_a_missing_file(registry, mirror, out, capsys):
    built(out, mirror, capsys)
    (out / "flowers.root").unlink()
    code, _, err = run(["verify", str(out)], capsys)
    assert code == 1
    assert "missing" in err


def test_verify_catches_a_file_of_the_wrong_size(registry, mirror, out, capsys):
    built(out, mirror, capsys)
    with (out / "flowers.root").open("ab") as handle:
        handle.write(b"tail")
    code, _, err = run(["verify", str(out)], capsys)
    assert code == 1
    assert "bytes on disk" in err


def test_verify_catches_a_flipped_byte(registry, mirror, out, capsys):
    built(out, mirror, capsys)
    target = out / "flowers.root"
    raw = bytearray(target.read_bytes())
    raw[-1] ^= 0xFF
    target.write_bytes(raw)
    code, _, err = run(["verify", str(out)], capsys)
    assert code == 1
    assert "checksum" in err


def test_verify_json_reports_the_problems_by_name(registry, mirror, out, capsys):
    built(out, mirror, capsys)
    (out / "flowers.root").unlink()
    code, output, _ = run(["verify", str(out), "--json"], capsys)
    assert code == 1
    assert json.loads(output) == {"checked": 1, "problems": {"flowers": "the file is missing"}}


# --- site -------------------------------------------------------------------


def test_site_writes_the_page_and_every_serving_config(registry, mirror, out, capsys):
    built(out, mirror, capsys)
    code, output, _ = run(
        ["site", str(out), "--base-url", "https://data.example.org/"], capsys
    )
    assert code == 0
    page = (out / "index.html").read_text()
    assert "flowers" in page  # the index is embedded, the page works from file://
    assert "XRD_CATALOGUE=https://data.example.org" in page
    assert str(out.resolve()) in (out / "nginx.conf").read_text()
    brix = (out / "brix.conf").read_text()
    assert "brix_root" in brix and "brix_webdav" in brix
    assert "brix_allow_write" not in brix  # read-only on every plane
    assert "ExecStart" in (out / "xrd-datasets.service").read_text()
    assert "XRD_CATALOGUE" in (out / "README.md").read_text()
    assert "index.html" in output


def test_site_json_lists_what_it_wrote(registry, mirror, out, capsys):
    built(out, mirror, capsys)
    code, output, _ = run(["site", str(out), "--json"], capsys)
    assert code == 0
    assert "nginx.conf" in json.loads(output)["written"]


def test_site_before_build_says_what_is_missing(out, capsys):
    out.mkdir()
    code, _, err = run(["site", str(out)], capsys)
    assert code == 1
    assert "index.json" in err
