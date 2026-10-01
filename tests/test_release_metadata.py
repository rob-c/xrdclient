"""Release metadata stays connected to the public runtime version."""

from pathlib import Path

import xrdclient

ROOT = Path(__file__).parents[1]


def test_version_has_one_build_source_and_release_notes() -> None:
    project = (ROOT / "pyproject.toml").read_text()
    changelog = (ROOT / "CHANGELOG.md").read_text()

    assert 'dynamic = ["version"]' in project
    assert 'path = "src/xrdclient/_version.py"' in project
    assert f"## [{xrdclient.__version__}]" in changelog
    assert xrdclient.__version__.count(".") == 2
