#!/usr/bin/env python3
"""Generate hash-verified, platform-specific wheel bundles and tap formulae."""

from __future__ import annotations

import argparse
import hashlib
import json
import sysconfig
import tarfile
from pathlib import Path

from tools.distribution_package import manifest, wheel_info


def formula(info: dict, url: str, digest: str, python: str) -> str:
    name = info["name"]
    command = {"xrdclient": "xrd-cp", "xgfalclient": "gfal-copy"}[name]
    return f'''class {name.capitalize()} < Formula
  include Language::Python::Virtualenv
  desc "Python storage client with an isolated wheel runtime"
  homepage "https://github.com/rob-c/{name}"
  url {json.dumps(url)}
  sha256 "{digest}"
  version "{info["version"]}"
  revision {info.get("revision", 0)}
  license "LGPL-3.0-or-later"
  depends_on "python@{python}"

  def install
    ENV["PIP_NO_INDEX"] = "1"
    ENV["PIP_ONLY_BINARY"] = ":all:"
    python = (Formula["python@{python}"].opt_bin/"python{python}").to_s
    venv = virtualenv_create(libexec, python, system_site_packages: false)
    wheels = Pathname.glob("wheelhouse/*.whl")
    own, deps = wheels.partition {{ |w| w.basename.to_s.start_with?("{name}-") }}
    venv.pip_install deps
    venv.pip_install_and_link own
  end

  test do
    require "json"
    report = JSON.parse(shell_output("#{{bin}}/{command} --output-format json --help"))
    assert_equal "storage-client-report", report["schema"]
    (testpath/"source").write("physics data")
    system bin/"{command}", "source", "dest"
    assert_equal "physics data", (testpath/"dest").read
  end
end
'''


def bundle(
    wheelhouse: Path, output: Path, name: str, python: str, base_url: str | None, revision: int = 0
) -> None:
    wheels = sorted(wheelhouse.glob("*.whl"))
    if name == "xrdclient":
        wheels = [w for w in wheels if not w.name.startswith("xgfalclient-")]
    info = wheel_info(next(w for w in wheels if w.name.startswith(name + "-")))
    info["revision"] = revision
    tag = f"cp{python.replace('.', '')}-{sysconfig.get_platform()}"
    archive = output / f"{name}-{info['version']}-{tag}-r{revision}-wheels.tar.gz"
    # Keep the archive flat at its root, so Homebrew doesn't strip wheelhouse/.
    manifest_file = output / f"{name}-manifest.json"
    manifest_file.write_text(json.dumps(manifest(wheels), indent=2) + "\n", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as target:
        target.add(manifest_file, arcname="wheel-manifest.json")
        for wheel in wheels:
            target.add(wheel, arcname="wheelhouse/" + wheel.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    url = base_url.rstrip("/") + "/" + archive.name if base_url else archive.resolve().as_uri()
    (output / f"{name}.rb").write_text(formula(info, url, digest, python), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python-version", default="3.14")
    parser.add_argument("--base-url", help="HTTPS location where the bundles will be published")
    parser.add_argument(
        "--revision", type=int, default=0, help="increment for dependency-only rebuilds"
    )
    args = parser.parse_args()
    if args.base_url and not args.base_url.startswith("https://"):
        parser.error("Published bundles need an https:// base URL")
    if args.revision < 0:
        parser.error("--revision must be zero or a positive integer")
    args.output.mkdir(parents=True, exist_ok=False)
    for name in ("xrdclient", "xgfalclient"):
        bundle(
            args.wheelhouse.resolve(),
            args.output,
            name,
            args.python_version,
            args.base_url,
            args.revision,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
