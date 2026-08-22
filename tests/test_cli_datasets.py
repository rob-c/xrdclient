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
from xrd.root._alex_mp20 import ALEX_MP20
from xrd.root._hub_tables import HUB_OPEN
from xrd.root._the_well import THE_WELL
from xrd.root.datasets import DATASETS, Table

DEFAULT_WELL_LARGE = {
    item["name"]
    for item in THE_WELL
    if 100_000_000 <= item["source_bytes"] < 2_000_000_000
}

FLOWERS = Table(
    name="flowers",
    label="Flowers",
    title="a few flowers",
    licence="CC0",
    source="https://example.invalid/flowers",
    creators=("Ada Dataset",),
    publisher="Example Science Lab",
    origin="https://origin.example.invalid/flowers",
    repository="Example Archive",
    mirrors=(("Teaching mirror", "https://mirror.example.invalid/flowers"),),
    citation="https://doi.org/10.0000/example.flowers",
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
    code, _, err = run(
        ["build", str(out), "--only", "flowers", "--base", mirror, "-q", *extra], capsys
    )
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
            "licence_url": "https://creativecommons.org/licenses/by-nc/4.0/",
            "redistributable": False,
            **CLOSED.provenance(),
            "transformation": CLOSED.transformation_summary(),
            "large": False,
            "source_bytes": 0,
            "modality": "tabular",
            "task": "machine learning",
        }
    ]


def test_a_glob_that_matches_nothing_is_an_error_that_says_so(capsys):
    code, _, err = run(["list", "--only", "nosuchset"], capsys)
    assert code == 1
    assert "no dataset matches nosuchset" in err


def test_large_selects_disk_backed_sources_regardless_of_origin(capsys):
    code, output, err = run(["list", "--large", "--json"], capsys)
    assert code == 0, err
    records = json.loads(output)
    original = {
        "biodcase_2025_task3",
        "audiomnist",
        "birdset_baseal",
        "cifar10",
        "cifar100",
        "emnist",
        "circor_heart_sound",
        "susy",
        "multimodal_damage",
        "pems_sf",
        "physical_unclonable_functions",
        "reefset",
        "speech_commands_v001",
        "daily_sports_activities",
        "gas_sensor_temperature",
        "twin_gas_sensor_arrays",
        "electricity_load_diagrams",
        "opportunity_activity",
        "gas_sensor_dynamic_mixtures",
        "galaxy10_sdss",
        "p53_mutants",
        "pathmnist",
        "pamap2",
        "hhar",
        "jarvis_stm_bravais",
        "jetnet",
        "matbench_mp_e_form",
        "matbench_mp_gap",
        "matbench_mp_is_metal",
        "moke_skyrmion_segmentation",
        "nffa_sem_compact",
        "omnifold_big",
        "polymer_blend_afm",
        "tinysol",
        "uav_maize_stress",
        "wildlife_mnist",
        "wikitext_103",
        "year_prediction_msd",
        "swefil",
        "tissuemnist",
        "tem_nanoparticle_morphology",
        "wse2_stm_defects",
    }
    hub = {item["name"] for item in HUB_OPEN if item["source_bytes"] >= 100_000_000}
    alex = {item["name"] for item in ALEX_MP20}
    _assert_large_names(records, original | hub | alex)
    assert len(hub) == 37
    _assert_large_metadata(records)


def _assert_large_names(records, established):
    assert {record["name"] for record in records} == established | DEFAULT_WELL_LARGE
    assert len(DEFAULT_WELL_LARGE) == 50


def _assert_large_metadata(records):
    assert all(record["large"] and record["transformation"] for record in records)
    assert any(record["source"].startswith("https://www.nist.gov/") for record in records)
    assert any(record["source"].startswith("https://archive.ics.uci.edu/") for record in records)


def test_the_two_gigabyte_ceiling_is_strict_even_for_an_explicit_name(capsys):
    code, _, err = run(["list", "--only", "higgs"], capsys)
    assert code == 1
    assert "strict 2 GB" in err and "2,000,000,000" in err


@pytest.mark.parametrize("flag", ["--allow-oversize", "--no-size-limit"])
def test_an_explicit_flag_admits_an_oversized_dataset(flag, capsys):
    code, output, err = run(["list", "--only", "higgs", flag, "--json"], capsys)
    assert code == 0, err
    assert json.loads(output) == [
        {
            "name": "higgs",
            "title": DATASETS["higgs"].title,
            "licence": "CC BY 4.0",
            "licence_url": "https://creativecommons.org/licenses/by/4.0/",
            "redistributable": True,
            **DATASETS["higgs"].provenance(),
            "transformation": DATASETS["higgs"].transformation_summary(),
            "large": True,
            "source_bytes": 2_816_865_137,
            "modality": "tabular",
            "task": "machine learning",
        }
    ]


def test_oversize_expands_the_large_selection_to_every_registered_converter(capsys):
    code, output, err = run(["list", "--large", "--allow-oversize", "--json"], capsys)
    assert code == 0, err
    records = json.loads(output)
    oversized = {
        "chipseq",
        "cuffless_blood_pressure",
        "gas_sensor_arrays_open_sampling",
        "hepmass",
        "higgs",
        "medical_deepfakes",
        "ppg_dalia",
        "realdisp",
    }
    assert len(records) == 287
    assert oversized <= {record["name"] for record in records}
    assert sum(record["source_bytes"] for record in records) == 681_389_909_770


# --- build ------------------------------------------------------------------


def test_build_passes_the_oversize_opt_in_to_conversion(monkeypatch, out, capsys):
    calls = []

    def convert_without_fetch(name, target, **options):
        calls.append((name, options["allow_oversize"]))
        target["about"] = "oversized conversion test"
        return {}

    monkeypatch.setattr(datasets_cli, "convert", convert_without_fetch)
    code, _, err = run(
        ["build", str(out), "--only", "higgs", "--allow-oversize", "--jobs", "1", "-q"],
        capsys,
    )
    assert code == 0, err
    assert calls == [("higgs", True), ("higgs", True)]
    assert (out / "higgs.root").exists()


def test_build_converts_writes_the_index_and_the_manifest(registry, mirror, out, capsys):
    index = built(out, mirror, capsys)
    assert index["format"] == 2
    (entry,) = index["datasets"]
    _assert_built_identity(entry)
    _assert_built_payload(entry, out)
    with open_root(str(out / "flowers.root")) as back:
        assert sorted(back.keys()) == ["about", "blue", "red"]


def _assert_built_identity(entry):
    assert entry["name"] == "flowers"
    assert entry["licence"] == "CC0"
    assert entry["licence_url"] == "https://creativecommons.org/publicdomain/zero/1.0/"
    assert entry["redistributable"] is True
    assert entry["source"] == FLOWERS.source
    _assert_built_provenance(entry)
    assert entry["transformation"] == FLOWERS.transformation_summary()
    assert entry["large"] is False and entry["source_bytes"] == 0


def _assert_built_provenance(entry):
    assert entry["origin"] == FLOWERS.origin_url()
    assert entry["origin_kind"] == "canonical"
    assert entry["repository"] == FLOWERS.source_repository()
    assert entry["creators"] == ["Ada Dataset"]
    assert entry["mirrors"] == [
        {"name": "Teaching mirror", "url": "https://mirror.example.invalid/flowers"}
    ]


def _assert_built_payload(entry, out):
    assert entry["download"] == "flowers.root"
    assert entry["trees"] == {"red": 2, "blue": 1}
    assert entry["rows"] == 3
    assert entry["bytes"] == (out / "flowers.root").stat().st_size
    assert f"{entry['adler32']}" in (out / "MANIFEST").read_text()
    assert "flowers.root" in (out / "MANIFEST").read_text()


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


def test_build_all_converts_it_anyway_and_the_index_says_what_it_is(registry, mirror, out, capsys):
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
    code, output, _ = run(["site", str(out), "--base-url", "https://data.example.org/"], capsys)
    assert code == 0
    page = (out / "index.html").read_text()
    _assert_site_catalogue(page)
    _assert_site_example(page)
    _assert_site_indexing(out)
    _assert_site_nginx(out)
    _assert_site_brix(out)
    assert "ExecStart" in (out / "xrd-datasets.service").read_text()
    assert "XRD_CATALOGUE" in (out / "README.md").read_text()
    assert "index.html" in output


def _assert_site_catalogue(page):
    _assert_site_dataset_content(page)
    _assert_site_licensing(page)
    assert 'type="application/ld+json"' in page and '"DataCatalog"' in page
    assert '"creator":[{"@type":"Person","name":"Ada Dataset"}]' in page
    assert '"publisher":{"@type":"Organization","name":"Example Science Lab"}' in page
    assert '<article class="dataset-card"' in page  # indexable without JavaScript


def _assert_site_dataset_content(page):
    assert "flowers" in page  # the index is embedded, the page works from file://
    assert "XRD_CATALOGUE=https://data.example.org" in page
    assert "Canonical origin" in page
    assert "Source: Example Archive" in page and "Mirror: Teaching mirror" in page
    assert "Citation" in page
    assert "Download " in page and "flowers.root" in page
    assert FLOWERS.source in page
    assert FLOWERS.transformation_summary() in page


def _assert_site_licensing(page):
    assert "canonical licence" in page
    assert "transformation into ROOT" in page
    assert "https://creativecommons.org/publicdomain/zero/1.0/" in page


def _assert_site_example(page):
    assert "python3 -m venv .venv" in page
    assert "pip install pyxrootdclient torch" in page and "torch.optim.Adam" in page
    assert "&lt; 2 GB" in page and "default per-dataset ceiling" in page


def _assert_site_indexing(out):
    detail = (out / "datasets" / "flowers.html").read_text()
    assert 'rel="canonical"' in detail and "Canonical origin" in detail
    assert "datasets/flowers.html" in (out / "sitemap.xml").read_text()
    assert "Sitemap: https://data.example.org/sitemap.xml" in (out / "robots.txt").read_text()


def _assert_site_nginx(out):
    nginx = (out / "nginx.conf").read_text()
    assert str(out.resolve()) in nginx
    assert "location ~ (^|/)\\." in nginx  # a source cache under the root stays private
    assert "application/x-root root" in nginx and "Accept-Ranges bytes" in nginx


def _assert_site_brix(out):
    brix = (out / "brix.conf").read_text()
    assert "brix_root" in brix and "brix_webdav" in brix
    assert "brix_allow_write" not in brix  # read-only on every plane


def test_a_publisher_text_citation_is_shown_as_text_and_never_made_an_href():
    made = {
        "source": "https://example.invalid/source",
        "origin": "https://example.invalid/source",
        "origin_kind": "source record",
        "repository": "Example Archive",
        "citation": "Credit the original collector, Alice Example.",
    }
    links = datasets_cli._provenance_links(made)
    detail = datasets_cli._citation_detail(made)
    assert "Credit the original collector" not in links
    assert "Credit the original collector" in detail
    assert "Dataset record" in links and "Repository: Example Archive" in links


def test_an_oversized_index_makes_the_site_disclose_the_opt_in_policy(
    registry, mirror, out, capsys
):
    index = built(out, mirror, capsys)
    index["datasets"][0]["source_bytes"] = 2_000_000_000
    (out / "index.json").write_text(json.dumps(index))
    code, _, err = run(["site", str(out)], capsys)
    assert code == 0, err
    page = (out / "index.html").read_text()
    assert "No cap" in page and "explicit oversized build" in page


def test_the_page_advertises_the_protocol_and_where_it_answers(registry, mirror, out, capsys):
    """A visitor should be able to tell what this is served over, and why."""
    built(out, mirror, capsys)
    run(["site", str(out), "--base-url", "https://data.example.org"], capsys)
    page = (out / "index.html").read_text()
    assert "XRootD" in page and "WLCG" in page and "OSG" in page
    assert "root://data.example.org//mnist.root" in page  # the native plane
    assert "https://data.example.org/mnist.root" in page  # and the HTTP one
    assert "cache=True" in page  # and the local copy, for those who want one


def test_the_root_endpoint_can_live_somewhere_else(registry, mirror, out, capsys):
    built(out, mirror, capsys)
    run(
        [
            "site",
            str(out),
            "--base-url",
            "https://data.example.org",
            "--root-url",
            "root://xrootd.example.org:1094/",
        ],
        capsys,
    )
    page = (out / "index.html").read_text()
    assert "root://xrootd.example.org:1094//mnist.root" in page
    assert "root://data.example.org" not in page


def test_the_readme_says_both_planes_and_the_cache(registry, mirror, out, capsys):
    built(out, mirror, capsys)
    run(["site", str(out), "--base-url", "https://data.example.org"], capsys)
    readme = (out / "README.md").read_text()
    assert "root://data.example.org//iris.root" in readme
    assert 'load("iris", cache=True)' in readme


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
