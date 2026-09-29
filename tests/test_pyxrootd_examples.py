"""``examples/pyxrootd``: PyXRootD scripts ported by one import line, kept working.

The examples are only worth anything while they run, and while the port
really is one line. So there are two kinds of test here. The fast ones read
every example and hold it to the shape the README promises: exactly one
``from xrdclient.compat import client  # was: from XRootD import client``, no
other mention of this library except submodule imports of the same kind, and
a mechanical rewrite back to the official import. The slow ones run
``run_all.py`` against a real ``xrootd`` - on the compat layer alone, and,
where the official bindings are installed, on both with the output compared.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE.parent / "examples" / "pyxrootd"


def _load_runner():
    spec = importlib.util.spec_from_file_location("pyxrootd_run_all", EXAMPLES / "run_all.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # A dataclass looks its module up in sys.modules while it is being made.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


run_all = _load_runner()

PORTED = "from xrdclient.compat import client  # was: from XRootD import client"
SUBMODULE = re.compile(r"^from xrdclient\.compat\.client\.(flags|utils|responses) import \w+")
SCRIPTS = [p for p in run_all.examples() if p.name != "install_hook.py"]


def test_there_is_an_example_for_each_topic():
    assert len(SCRIPTS) >= 18
    assert (EXAMPLES / "install_hook.py") in run_all.examples()
    assert (EXAMPLES / "README.md").exists()


@pytest.mark.parametrize("script", SCRIPTS, ids=[p.name for p in SCRIPTS])
def test_an_example_is_ported_by_its_one_import_line(script):
    lines = script.read_text().splitlines()
    assert lines.count(PORTED) == 1
    mentions = [line for line in lines if "xrdclient" in line and line != PORTED]
    assert all(SUBMODULE.match(line) for line in mentions), mentions
    # Nothing reaches the official package directly: it would be a second port.
    assert not [line for line in lines if re.match(r"\s*(from|import)\s+XRootD\b", line)]
    # Every example takes the server and the working directory from argv.
    assert any(line.startswith("URL = sys.argv[1]") for line in lines)
    assert any(line.startswith("WORK = sys.argv[2]") for line in lines)


@pytest.mark.parametrize("script", SCRIPTS, ids=[p.name for p in SCRIPTS])
def test_reverting_the_import_leaves_a_pyxrootd_script(script):
    original = run_all.to_official(script.read_text())
    assert "xrdclient" not in original
    assert original.splitlines().count("from XRootD import client") == 1
    compile(original, script.name, "exec")


def test_the_install_hook_example_imports_xrootd_unmodified():
    source = (EXAMPLES / "install_hook.py").read_text()
    assert "xrdclient.compat.install()  # compat only" in source
    assert "\nfrom XRootD import client  # noqa: E402\n" in source
    original = run_all.to_official(source)
    # The docstring still talks about xrdclient; the code no longer uses it.
    code = [line.strip() for line in original.splitlines()]
    assert not [line for line in code if line.startswith(("import xrdclient", "xrdclient."))]
    compile(original, "install_hook.py", "exec")


def test_normalising_masks_only_what_varies_between_runs():
    url, local = "root://127.0.0.1:40123", Path("/tmp/xrdexAB12/data")
    text = (
        f"{url}//w/f [::127.0.0.1]:40123 host:40123 {local}/w/f"
        " /private/var/folders/xy/T/tmpab/x.bin 2026-09-29 12:00:01 1790000000 port 1094"
    )
    assert run_all.normalise(text, url, local) == (
        "root://HOST//w/f HOST HOST LOCALROOT/w/f TMPPATH TIME EPOCH port 1094"
    )


def _run_all(*args: str) -> subprocess.CompletedProcess[str]:
    import _xrootd

    if not _xrootd.available():
        pytest.skip("no xrootd binary on PATH")
    return subprocess.run(
        [sys.executable, str(EXAMPLES / "run_all.py"), *args],
        capture_output=True,
        text=True,
        timeout=1500,
    )


@pytest.mark.interop
@pytest.mark.timeout(1800)
def test_every_example_runs_on_the_compat_layer():
    proc = _run_all("--compat-only")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "passed (compat only)" in proc.stdout


@pytest.mark.interop
@pytest.mark.parity
@pytest.mark.timeout(1800)
def test_every_example_prints_what_it_prints_on_the_official_bindings():
    # Probed in a child: importing the bindings here would shadow the
    # compat layer's install() for the rest of the session.
    if not run_all.have_official(sys.executable):
        pytest.skip("the official bindings are not installed")
    proc = _run_all()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "passed (official and compat)" in proc.stdout
