"""Release metadata stays connected to the public runtime version."""

from pathlib import Path

import xrdclient

ROOT = Path(__file__).parents[1]


def test_version_has_one_build_source_and_release_notes() -> None:
    project = (ROOT / "pyproject.toml").read_text()
    changelog = (ROOT / "CHANGELOG.md").read_text()

    assert 'dynamic = ["version"]' in project
    assert 'path = "src/xrdclient/_version.py"' in project
    headings = [line for line in changelog.splitlines() if line.startswith("## [")]
    assert headings[0].startswith(f"## [{xrdclient.__version__}] - ")
    assert sum(line.startswith(f"## [{xrdclient.__version__}]") for line in headings) == 1
    assert f"[{xrdclient.__version__}]: https://github.com/rob-c/xrdclient/compare/" in changelog
    assert "## [0.2.0] - 2026-10-01" in headings
    assert xrdclient.__version__.count(".") == 2
