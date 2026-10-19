"""The public surface as a type checker sees it.

``open`` is overloaded so that a literal mode says whether ``read`` gives
``bytes`` or ``str``, and ``buffering=0`` hands back the raw layer. That is a
promise to callers which only a type checker can break, so it is checked here
rather than left to the next release to discover.

Why this file is slow, and how it is kept from timing out: each check runs
``mypy --strict`` over a small script that imports :mod:`xrdclient`, and
mypy follows that import through the whole package, xrdcore, xrdsec,
xrdhttp, ``cryptography`` and typeshed - about 400 modules, a minute of CPU
from cold. Both checks used to run with ``--no-incremental``, so each paid
that minute, and two of them on a loaded parallel run went past the suite's
120 s per-test timeout. Now they share one incremental cache (:func:`_mypy`),
which makes the second check a few seconds; the cache lives in this pytest
run's own temporary directory, so it never outlives the sources it was built
from and never touches the repository. What is checked is unchanged: an
incremental run reports exactly what a cold one does.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import subprocess
import sys
from collections.abc import Iterator

import pytest

pytest.importorskip("mypy", reason="optional: mypy (the dev extra) is not installed")

#: Cold, mypy alone takes about a minute here, and several times that when
#: the other suites share the machine; the suite-wide 120 s default is for
#: tests that do not start a type checker.
TIMEOUT = 900

pytestmark = pytest.mark.timeout(TIMEOUT)

ROOT = pathlib.Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def _exclusive(lock: pathlib.Path) -> Iterator[None]:
    """Hold ``lock`` against the other xdist workers, where ``fcntl`` exists.

    Two mypy processes writing one cache at once can leave it inconsistent,
    so the workers take turns: the first fills the cache, the second waits
    and then finds it warm. Without ``fcntl`` (Windows) there is no lock and
    :func:`_cache_dir` gives each worker its own cache instead.
    """
    try:
        import fcntl
    except ImportError:  # Windows: see the docstring
        yield
        return
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _cache_dir(factory: pytest.TempPathFactory) -> pathlib.Path:
    """One mypy cache for this pytest run, shared by its xdist workers.

    Under xdist each worker's base directory is ``<run>/popen-gwN``, so the
    parent is this run's and no other's; without xdist the base directory
    itself is. A later run gets a fresh directory, so the cache can never be
    older than the sources it describes.
    """
    base = factory.getbasetemp()
    shared = os.environ.get("PYTEST_XDIST_WORKER") and sys.platform != "win32"
    return (base.parent if shared else base) / "mypy-cache"


def _mypy(script: pathlib.Path, factory: pytest.TempPathFactory) -> str:
    """``mypy --strict`` over ``script`` with the run's shared cache; its stdout."""
    cache = _cache_dir(factory)
    cache.mkdir(parents=True, exist_ok=True)
    with _exclusive(cache.with_name("mypy-cache.lock")):
        result = subprocess.run(
            [sys.executable, "-m", "mypy", "--strict", "--cache-dir", str(cache), str(script)],
            capture_output=True,
            text=True,
            cwd=ROOT,
            # Below the test's own timeout, so a hung mypy fails with its output.
            timeout=TIMEOUT - 60,
        )
    return result.stdout


SOURCE = """
import xrdclient

url = "root://host//store/f.root"

reveal_type(xrdclient.open(url, "rb").read())
reveal_type(xrdclient.open(url, "r").read())
reveal_type(xrdclient.open(url, "rb", buffering=0))
reveal_type(xrdclient.open(url, mode_from_config).read())

fs = xrdclient.FileSystem("root://host")
reveal_type(fs.read_bytes("/f"))
reveal_type(fs.read_text("/f"))
reveal_type(xrdclient.XRootDPath(url).stat())
"""

EXPECTED = [
    'Revealed type is "bytes"',
    'Revealed type is "str"',
    'Revealed type is "xrdclient.io.raw.XRootDRawIO"',
    'Revealed type is "Any"',
    'Revealed type is "bytes"',
    'Revealed type is "str"',
    'Revealed type is "xrdclient.types.StatInfo"',
]


WORDS = """
import xrdclient

fs = xrdclient.FileSystem("root://host")
fs.prepare(["/f"], evict=True)
fs.prepare(["/f"], flags="stage notify")
fs.scandir("/store", stat=False)
fs.locate("/f", refresh=True)
fs.chmod("/f", "rw-r-----")
fs.mkdir("/d", "rwxr-x---")
fs.query("checksum", "/f")
xrdclient.PrepareFlags("stage notify")
xrdclient.Access("rwxr-x---")
xrdclient.OpenFlags("new makepath")
xrdclient.QueryCode("checksum")
xrdclient.File("root://host//f").open("w", "rw-r--r--")
"""


def test_a_word_where_a_flag_goes_type_checks_too(
    tmp_path: pathlib.Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The words are only half a feature if using them fails ``mypy --strict``."""
    script = tmp_path / "words.py"
    script.write_text(WORDS)
    stdout = _mypy(script, tmp_path_factory)
    assert "error:" not in stdout, stdout
    assert "Success: no issues found" in stdout, stdout


def test_the_open_overloads_say_what_comes_back(
    tmp_path: pathlib.Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    script = tmp_path / "surface.py"
    # A non-literal mode is the escape hatch: it must still type-check, as
    # ``Any``, rather than being rejected.
    script.write_text("mode_from_config = str()\n" + SOURCE)
    stdout = _mypy(script, tmp_path_factory)
    revealed = [line for line in stdout.splitlines() if "Revealed type" in line]
    # mypy has changed whether it spells these "bytes" or "builtins.bytes"
    # within the range the dev extra allows, and the promise is the type,
    # not the spelling.
    found = [line.split("note: ")[-1].replace("builtins.", "") for line in revealed]
    assert found == EXPECTED, stdout
