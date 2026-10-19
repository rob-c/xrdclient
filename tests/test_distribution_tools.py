"""xrdclient's CI, distribution matrix and publish job run xrd-devtools' shared ones.

The tools (scorecard, maintainability, release pre-flight, platform matrix,
RPM/DEB and Homebrew packaging, installed smoke test) and their tests moved to
the xrd-devtools repository; what is pinned here is that this repository
still runs them.
"""

from pathlib import Path

ROOT = Path(__file__).parents[1]
DEVTOOLS = "rob-c/xrd-devtools/.github"


def test_ci_runs_the_shared_gates_against_this_commit_and_the_library_candidates():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    for workflow in ("static", "tests", "chain"):
        assert f"uses: {DEVTOOLS}/workflows/{workflow}.yml@main" in ci
    assert "maintainability: true" in ci  # maintainability.json is this repository's
    assert "xrdclient-ref: ${{ github.sha }}" in ci
    # Every job of its own builds against the candidates through the one action.
    jobs = ci.count("\n    runs-on:")
    assert ci.count(f"uses: {DEVTOOLS}/actions/candidates@main") == jobs


def test_the_distribution_matrix_is_xrd_devtools_on_this_commit():
    workflow = (ROOT / ".github/workflows/platforms.yml").read_text()
    assert f"uses: {DEVTOOLS}/workflows/platforms.yml@main" in workflow
    assert "xrdclient-ref: ${{ github.sha }}" in workflow


def test_publish_checks_the_libraries_are_on_pypi_before_uploading():
    """The shared publish action runs the PyPI pre-flight before it stages the artifacts."""
    publish = (ROOT / ".github/workflows/publish.yml").read_text()
    build = publish[publish.index(f"uses: {DEVTOOLS}/actions/publish@main") :]
    assert "package: xrdclient\n" in build and "preflight: true\n" in build
    assert publish.index("preflight: true") < publish.index("pypa/gh-action-pypi-publish")
