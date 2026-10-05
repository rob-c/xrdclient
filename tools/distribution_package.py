#!/usr/bin/env python3
"""Build private, offline RPM/DEB runtimes from an already resolved wheelhouse.

The xrdclient package owns the shared runtime; xgfalclient adds only its own
wheel and has an exact package-manager dependency on that xrdclient version.
These are deployment bundles, not Fedora/Debian archive submissions.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from email.parser import Parser
from pathlib import Path

PREFIX = Path("/opt/storage-clients")
LAUNCHER = """import importlib.metadata as metadata
import pathlib
import runpy
import sys
root = pathlib.Path(__file__).parent
sys.path[:0] = [str(root / lib / "python{minor}" / "site-packages") for lib in ("lib", "lib64")]
if sys.argv[1] == "--smoke":
    script = sys.argv.pop(2)
    sys.argv.pop(1)
    runpy.run_path(script, run_name="__main__")
else:
    name = pathlib.Path(sys.argv.pop(1)).name
    for dist in ("xrdclient", "xgfalclient"):
        for entry in metadata.distribution(dist).entry_points:
            if entry.group == "console_scripts" and entry.name == name:
                sys.exit(entry.load()())
    sys.exit("Unknown storage-client command: " + name)
"""


def wheel_info(wheel: Path) -> dict:
    with zipfile.ZipFile(wheel) as archive:
        member = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
        meta = Parser().parsestr(archive.read(member).decode("utf-8"))
        entries = configparser.ConfigParser()
        entries.read_string(archive.read(member.replace("METADATA", "entry_points.txt")).decode())
    version = meta["Version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Deployment bundles require a numeric major.minor.patch release version")
    return {"name": meta["Name"], "version": version, "scripts": list(entries["console_scripts"])}


def manifest(wheels: list[Path]) -> list[dict]:
    return [
        {"filename": w.name, "sha256": hashlib.sha256(w.read_bytes()).hexdigest()} for w in wheels
    ]


def stage_runtime(stage: Path, wheels: list[Path], info: dict, python: Path, minor: str) -> None:
    prefix = stage / PREFIX.relative_to("/")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--no-compile",
            "--ignore-installed",
            "--only-binary=:all:",
            "--prefix",
            str(prefix),
            *map(str, wheels),
        ],
        check=True,
    )
    if info["name"] == "xrdclient":
        (prefix / "launch.py").write_text(LAUNCHER.format(minor=minor), encoding="utf-8")
    binary = stage / "usr/bin"
    binary.mkdir(parents=True)
    # Replace pip's build-environment shebangs with fixed, isolated launchers.
    for script in info["scripts"]:
        wrapper = prefix / "bin" / script
        wrapper.write_text(
            f'#!/bin/sh\nexec {python} -I -S {PREFIX}/launch.py "$0" "$@"\n', encoding="utf-8"
        )
        wrapper.chmod(0o755)
        (binary / script).symlink_to(PREFIX / "bin" / script)
    doc = stage / "usr/share/doc" / info["name"]
    doc.mkdir(parents=True)
    (doc / "wheel-manifest.json").write_text(
        json.dumps(manifest(wheels), indent=2) + "\n", encoding="utf-8"
    )
    manuals = prefix / "share/man/man1"
    if manuals.exists():
        target = stage / "usr/share/man/man1"
        target.mkdir(parents=True)
        for page in manuals.glob("*.1"):
            (target / page.name).symlink_to(PREFIX / "share/man/man1" / page.name)


def rpm(stage: Path, output: Path, info: dict, python: Path, native_version: str) -> None:
    top = stage.parent / "rpmbuild"
    top.mkdir()
    dependencies = f"{python}, glibc, libgcc"
    release = info.get("release", 1)
    if info["name"] == "xgfalclient":
        dependencies = f"xrdclient = {native_version}-{release}"
    spec = top / "package.spec"
    manuals = "/usr/share/man/man1/*" if info["name"] == "xgfalclient" else ""
    spec.write_text(
        f'''%global __os_install_post %{{nil}}
Name: {info["name"]}
Version: {info["version"]}
Release: {release}
Summary: Python storage client with an isolated offline runtime
License: LGPL-3.0-or-later
URL: https://github.com/rob-c/{info["name"]}
AutoReqProv: no
Requires: {dependencies}
%description
Storage client. Dependencies are private wheel snapshots; rebuild for updates.
%install
mkdir -p "%{{buildroot}}"
cp -a "{stage}/." "%{{buildroot}}/"
%files
/opt/storage-clients/*
/usr/bin/*
/usr/share/doc/{info["name"]}
{manuals}
''',
        encoding="utf-8",
    )
    subprocess.run(["rpmbuild", "-bb", "--define", f"_topdir {top}", str(spec)], check=True)
    for artifact in top.glob("RPMS/*/*.rpm"):
        shutil.copy2(artifact, output)


def deb(stage: Path, output: Path, info: dict, python: Path, native_version: str) -> None:
    arch = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    dependencies = f"{python.name}, libc6 (>= 2.28), libgcc-s1"
    release = info.get("release", 1)
    if info["name"] == "xgfalclient":
        dependencies = f"xrdclient (= {native_version}-{release})"
    control = stage / "DEBIAN"
    control.mkdir()
    (control / "control").write_text(
        f"""Package: {info["name"]}
Version: {info["version"]}-{release}
Architecture: {arch}
Maintainer: Robert Currie <robert.andrew.currie@gmail.com>
Section: science
Priority: optional
Depends: {dependencies}
Description: Python storage client with an isolated offline runtime
 Dependencies are private wheel snapshots; rebuild for security updates.
""",
        encoding="utf-8",
    )
    target = output / f"{info['name']}_{info['version']}-{release}_{arch}.deb"
    subprocess.run(
        ["dpkg-deb", "--build", "--root-owner-group", str(stage), str(target)], check=True
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", choices=("rpm", "deb"), required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True, help="system interpreter, not a venv")
    parser.add_argument(
        "--release", type=positive_release, default=1, help="increment for dependency-only rebuilds"
    )
    args = parser.parse_args()
    python = args.python.resolve()
    minor = subprocess.check_output(
        [str(python), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], text=True
    ).strip()
    wheels = sorted(args.wheelhouse.resolve().glob("*.whl"))
    native = next(w for w in wheels if w.name.startswith("xrdclient-"))
    gfal = next(w for w in wheels if w.name.startswith("xgfalclient-"))
    native_version = wheel_info(native)["version"]
    args.output.mkdir(parents=True, exist_ok=False)
    for client_wheel in (native, gfal):
        info = wheel_info(client_wheel)
        info["release"] = args.release
        owned = [gfal] if client_wheel == gfal else [w for w in wheels if w != gfal]
        with tempfile.TemporaryDirectory(prefix="storage-package-") as folder:
            stage = Path(folder) / "stage"
            stage.mkdir()
            stage_runtime(stage, owned, info, python, minor)
            {"rpm": rpm, "deb": deb}[args.family](
                stage, args.output.resolve(), info, python, native_version
            )
    return 0


def positive_release(value: str) -> int:
    release = int(value)
    if release < 1:
        raise argparse.ArgumentTypeError("--release must be a positive integer")
    return release


if __name__ == "__main__":
    raise SystemExit(main())
