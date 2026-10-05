#!/usr/bin/env python3
"""Check installed distributions, every console entry point, and a local copy."""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
import subprocess
import tempfile
from pathlib import Path
from xml.etree import ElementTree


def console_checks(bin_dir: Path, name: str) -> int:
    count = 0
    for entry in metadata.distribution(name).entry_points:
        if entry.group != "console_scripts":
            continue
        for output in ("json", "xml"):
            result = subprocess.run(
                [str(bin_dir / entry.name), "--output-format", output, "--help"],
                capture_output=True,
                check=True,
            )
            if output == "json":
                assert json.loads(result.stdout)["schema"] == "storage-client-report"
            else:
                assert (
                    ElementTree.fromstring(result.stdout).attrib["schema"]
                    == "storage-client-report"
                )
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", type=Path, required=True)
    parser.add_argument("--xrd-bin-dir", type=Path, help="Xrd commands when prefixes are separate")
    args = parser.parse_args()
    import gfal2
    import xgfalclient

    import xrdclient
    from xrdclient._xml import fromstring

    assert metadata.version("xrdclient") == xrdclient.__version__
    assert metadata.version("xgfalclient") == xgfalclient.__version__
    assert gfal2.get_version() == "2.23.5"
    assert fromstring(b"<doc/>").tag == "doc"
    xrd_bin = args.xrd_bin_dir or args.bin_dir
    count = console_checks(xrd_bin, "xrdclient") + console_checks(args.bin_dir, "xgfalclient")
    with tempfile.TemporaryDirectory(prefix="storage-smoke-") as folder:
        source = Path(folder) / "source-physics-μ.dat"
        source.write_bytes(bytes(range(256)) * 4096)
        for command in ("xrd-cp", "gfal-copy"):
            dest = Path(folder) / command
            binary = xrd_bin if command == "xrd-cp" else args.bin_dir
            result = subprocess.run(
                [str(binary / command), "--output-format", "json", str(source), str(dest)],
                capture_output=True,
                check=True,
            )
            assert json.loads(result.stdout)["summary"]["ok"]
            assert source.read_bytes() == dest.read_bytes()
    print(json.dumps({"ok": True, "console_checks": count, "copies": 2}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
