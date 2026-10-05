#!/usr/bin/env python3
"""Run the shared distribution matrix with rootless Podman (or Docker)."""

from __future__ import annotations

import argparse
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MATRIX = Path(__file__).resolve().parents[1] / "packaging/platforms.json"


def matrix() -> list[dict]:
    return json.loads(MATRIX.read_text(encoding="utf-8"))["containers"]


def command(engine: str, workspace: Path, output: Path, target: dict) -> list[str]:
    return [
        engine,
        "run",
        "--rm",
        "--pull=always",
        "--cpus=4",
        "--memory=2g",
        "--pids-limit=2048",
        "--security-opt=no-new-privileges",
        "--security-opt=label=disable",
        "-v",
        f"{workspace.resolve() / 'xrdclient'}:/src/xrdclient:ro",
        "-v",
        f"{workspace.resolve() / 'xgfalclient'}:/src/xgfalclient:ro",
        "-v",
        f"{output.resolve()}:/artifacts:rw",
        target["image"],
        "bash",
        "/src/xrdclient/packaging/linux.sh",
        target["family"],
        target["python"],
        *target["packages"],
    ]


def run(engine: str, workspace: Path, output: Path, target: dict) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    with (output / "container.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(
            command(engine, workspace, output, target),
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    inspected = subprocess.run(
        [engine, "image", "inspect", target["image"]], capture_output=True, text=True, check=False
    )
    (output / "image.json").write_text(inspected.stdout, encoding="utf-8")
    report = {
        "platform": target["name"],
        "image": target["image"],
        "exit_code": result.returncode,
        "ok": result.returncode == 0,
    }
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report), flush=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print the container matrix as JSON")
    parser.add_argument("--engine", choices=("podman", "docker"), default="podman")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="directory containing xrdclient/ and xgfalclient/",
    )
    parser.add_argument("--output", type=Path, default=Path("platform-results"))
    parser.add_argument("--jobs", type=int, choices=(1, 2), default=1)
    parser.add_argument("--platform", action="append", choices=[t["name"] for t in matrix()])
    args = parser.parse_args()
    if args.list:
        print(json.dumps({"include": matrix()}))
        return 0
    validate_workspace(parser, args.workspace)
    selected = [t for t in matrix() if not args.platform or t["name"] in args.platform]
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        pending = [
            pool.submit(run, args.engine, args.workspace, args.output / t["name"], t)
            for t in selected
        ]
        reports = [task.result() for task in pending]
    return int(any(not report["ok"] for report in reports))


def validate_workspace(parser: argparse.ArgumentParser, workspace: Path) -> None:
    for name in ("xrdclient", "xgfalclient"):
        if not (workspace / name / "pyproject.toml").is_file():
            parser.error(f"{workspace} must contain {name}/pyproject.toml")


if __name__ == "__main__":
    raise SystemExit(main())
