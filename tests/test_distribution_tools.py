"""Distribution definitions and shared packaging contracts stay coordinated."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from tools import distribution_package as packages
from tools import homebrew_package as brew
from tools import platforms


@pytest.fixture
def wheel(tmp_path):
    def make(name="xrdclient", version="0.2.0"):
        path = tmp_path / f"{name}-{version}-py3-none-any.whl"
        with zipfile.ZipFile(path, "w") as archive:
            prefix = f"{name}-{version}.dist-info/"
            archive.writestr(prefix + "METADATA", f"Name: {name}\nVersion: {version}\n")
            archive.writestr(prefix + "entry_points.txt", "[console_scripts]\nxrd-cp = cli:main\n")
        return path

    return make


def test_matrix_covers_every_requested_container_and_native_platform():
    config = json.loads(platforms.MATRIX.read_text())
    assert {t["name"] for t in config["containers"]} == {
        "alma8",
        "alma9",
        "alma10",
        "ubuntu24",
        "ubuntu26",
        "centos9-stream",
        "centos10-stream",
        "fedora",
        "fedora-rawhide",
    }
    assert config["native"] == ["nixos-26.05", "homebrew-macos-intel", "homebrew-macos-arm64"]
    assert len({t["name"] for t in config["containers"]}) == len(config["containers"])
    for target in config["containers"]:
        assert target["python"] == (
            "python3.12" if target["name"] in {"alma8", "alma9", "centos9-stream"} else "python3"
        )
        assert "/" in target["image"] and ":" in target["image"]


@pytest.mark.parametrize("engine", ["podman", "docker"])
def test_container_uses_readonly_sources_and_no_host_privileges(tmp_path, engine):
    argv = platforms.command(engine, tmp_path, tmp_path / "output", platforms.matrix()[0])
    assert argv[:3] == [engine, "run", "--rm"]
    assert f"{tmp_path / 'xrdclient'}:/src/xrdclient:ro" in argv
    assert f"{tmp_path / 'xgfalclient'}:/src/xgfalclient:ro" in argv
    assert f"{tmp_path}:/src:ro" not in argv
    assert "--privileged" not in argv and "--network=host" not in argv
    assert "--security-opt=no-new-privileges" in argv
    assert "--pull=always" in argv
    assert not any(a.startswith("--platform") for a in argv)


def test_ci_matrix_runs_every_container_on_both_architectures():
    include = platforms.ci_matrix()["include"]
    assert len(include) == 2 * len(platforms.matrix())
    assert {(e["name"], e["arch"]) for e in include} == {
        (t["name"], arch) for t in platforms.matrix() for arch in platforms.ARCHITECTURES
    }
    for entry in include:
        assert entry["runner"] == platforms.RUNNERS[entry["arch"]]
        assert ("arm" in entry["runner"]) is (entry["arch"] == "arm64")


@pytest.mark.parametrize("arch", platforms.ARCHITECTURES)
def test_an_explicit_architecture_selects_that_image_variant(tmp_path, arch):
    argv = platforms.command("docker", tmp_path, tmp_path / "out", platforms.matrix()[0], arch)
    assert f"--platform=linux/{arch}" in argv
    assert argv.index(f"--platform=linux/{arch}") < argv.index(platforms.matrix()[0]["image"])


def test_workspace_error_is_actionable(tmp_path):
    parser = argparse.ArgumentParser()
    with pytest.raises(SystemExit, match="2"):
        platforms.validate_workspace(parser, tmp_path)
    for name in ("xrdclient", "xgfalclient"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "pyproject.toml").touch()
    platforms.validate_workspace(parser, tmp_path)


def test_nonroot_suites_do_not_take_ownership_of_the_runner_artifact_directory():
    script = (platforms.MATRIX.parent / "linux.sh").read_text()
    assert "mkdir /artifacts/tests" in script
    assert "chown -R tester /work /artifacts/tests\n" in script
    assert "xml:/artifacts/tests/$client-coverage.xml" in script
    assert 'junitxml="/artifacts/tests/$client-tests.xml"' in script


def test_workflow_accepts_both_candidate_refs_and_does_not_retain_checkout_tokens():
    workflow = (platforms.MATRIX.parents[1] / ".github/workflows/platforms.yml").read_text()
    for name in ("xrdclient-ref", "xgfalclient-ref"):
        assert workflow.count(f"{name}:") == 2  # manual runs and reusable calls
    assert workflow.count("persist-credentials: false") == 7
    assert "permissions:\n  contents: read\n" in workflow


@pytest.mark.parametrize("code", [0, 1, 125])
def test_runner_retains_failure_logs_and_image_identity(tmp_path, monkeypatch, code):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            a[0], code, stdout='[{"Digest":"sha256:test"}]'
        ),
    )
    report = platforms.run("podman", tmp_path, tmp_path / "result", platforms.matrix()[0])
    assert report["ok"] is (code == 0)
    assert report["exit_code"] == code
    assert report["arch"] == "native"
    assert json.loads((tmp_path / "result/result.json").read_text()) == report
    assert "sha256:test" in (tmp_path / "result/image.json").read_text()
    assert (tmp_path / "result/container.log").is_file()


def test_wheel_metadata_and_manifest_are_not_guessed(wheel):
    path = wheel()
    assert packages.wheel_info(path) == {
        "name": "xrdclient",
        "version": "0.2.0",
        "scripts": ["xrd-cp"],
    }
    assert packages.manifest([path]) == [
        {"filename": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    ]


def test_deployment_bundle_rejects_ambiguous_version(wheel):
    with pytest.raises(ValueError, match=r"major\.minor\.patch"):
        packages.wheel_info(wheel(version="0.2.0rc1"))


@pytest.mark.parametrize("name", ["xrdclient", "xgfalclient"])
@pytest.mark.parametrize("revision", [0, 2])
def test_brew_bundle_has_real_hash_and_only_its_runtime(wheel, tmp_path, name, revision):
    wheel()
    wheel(name="xgfalclient")
    output = tmp_path / "output"
    output.mkdir()
    brew.bundle(tmp_path, output, name, "3.14", "https://example.test/releases", revision)
    info = (output / f"{name}.rb").read_text()
    archive = next(output.glob(f"{name}-0.2.0-cp314-*-r{revision}-wheels.tar.gz"))
    assert hashlib.sha256(archive.read_bytes()).hexdigest() in info
    assert 'ENV["PIP_NO_INDEX"] = "1"' in info
    assert "system_site_packages: false" in info
    assert f"revision {revision}\n" in info
    assert f"https://example.test/releases/{archive.name}" in info
    manifest = json.loads((output / f"{name}-manifest.json").read_text())
    assert len(manifest) == (1 if name == "xrdclient" else 2)


def test_private_launcher_preserves_redhat_and_debian_library_locations():
    text = packages.LAUNCHER.format(minor="3.12")
    assert '("lib", "lib64")' in text
    assert '"python3.12"' in text


@pytest.mark.parametrize("release", [1, 2])
def test_gfal_reuses_exact_native_runtime_in_debian_metadata(tmp_path, monkeypatch, release):
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: "amd64\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    packages.deb(
        stage,
        tmp_path,
        {"name": "xgfalclient", "version": "0.2.0", "release": release},
        Path("/usr/bin/python3.12"),
        "0.2.0",
    )
    assert f"Depends: xrdclient (= 0.2.0-{release})" in (stage / "DEBIAN/control").read_text()


@pytest.mark.parametrize("release", [1, 2])
def test_gfal_reuses_exact_native_runtime_in_rpm_metadata(tmp_path, monkeypatch, release):
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    packages.rpm(
        stage,
        tmp_path,
        {"name": "xgfalclient", "version": "0.2.0", "release": release},
        Path("/usr/bin/python3.12"),
        "0.2.0",
    )
    assert (
        f"Requires: xrdclient = 0.2.0-{release}" in (tmp_path / "rpmbuild/package.spec").read_text()
    )
