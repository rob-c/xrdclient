"""What ``import xrdclient`` costs, held to by what it loads rather than how long.

Every command-line tool, every xgfalclient call and every test worker starts
with an import, and on a login node's network filesystem an import is paid
for per module. The package is lazy (PEP 562) so that the bare import, and
the leaf modules other tools import by themselves, load none of the heavy
stack: not the X.509 code and its ``cryptography``/``asn1crypto`` imports,
not ``botocore``, not :mod:`ssl`.

Wall-clock limits would be flaky on a shared runner, so each check runs a
fresh interpreter and asserts on ``sys.modules`` instead: a module that was
not imported cannot have cost anything.
"""

from __future__ import annotations

import json
import os
import pathlib
import pkgutil
import subprocess
import sys

import pytest

import xrdclient

SRC = str(pathlib.Path(xrdclient.__file__).parent.parent)

#: Never loaded by a cheap import. Each is either a large native extension or
#: a package that pulls in dozens of modules of its own.
HEAVY = (
    "asn1crypto",
    "asyncio",
    "botocore",
    "cryptography",
    "cryptography.x509",
    "jwt",
    "ssl",
    "urllib3",
    "xrdclient.client",
    "xrdclient.crypto",
    "xrdclient.session",
    "xrdclient.transport",
    # xrdsec's X.509, proxy, VOMS and TLS modules: the crypto stack by another name.
    "xrdsec.x509",
    "xrdsec.proxies",
    "xrdsec.voms",
    "xrdsec.tls",
)


def _loaded_by(statement: str) -> set[str]:
    """The modules a fresh interpreter has after running ``statement``."""
    probe = f"import json, sys\n{statement}\nprint(json.dumps(sorted(sys.modules)))"
    paths = (SRC, os.environ.get("PYTHONPATH"))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, paths))}
    out = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=True,
    )
    # ``-X importtime`` reports on stderr, so stdout is the module list alone;
    # the report is kept for the failure message.
    assert "import time:" in out.stderr
    return set(json.loads(out.stdout))


@pytest.mark.parametrize(
    "statement",
    [
        "import xrdclient",
        "import xrdclient.copy.limits",
        "import xrdclient.report",
        "import xrdclient.credentials",
        "import xrdclient.safexml",
        "import xrdclient.retry",
        "import xrdclient.log",
        "import xrdclient.url",
        "import xrdclient.errors",
        "from xrdclient import XRootDURL, parse, NotFoundError, StatInfoFlags, Config",
    ],
)
def test_a_cheap_import_loads_nothing_heavy(statement):
    loaded = _loaded_by(statement)
    assert not loaded & set(HEAVY), sorted(loaded & set(HEAVY))


def test_the_bare_import_loads_only_the_version():
    """Not just nothing heavy: nothing of the package beyond its version."""
    ours = {name for name in _loaded_by("import xrdclient") if name.startswith("xrdclient")}
    assert ours == {"xrdclient", "xrdclient._version"}


def test_the_bare_import_loads_nothing_of_the_libraries():
    """xrdcore and xrdsec are dependencies, not a cost: each loads when first named."""
    loaded = _loaded_by("import xrdclient")
    assert not {name for name in loaded if name.startswith(("xrdcore", "xrdsec"))}


@pytest.mark.parametrize(
    ("statement", "sec"),
    [
        ("import xrdclient.credentials", {"xrdsec.credentials"}),
        ("from xrdclient import Config", {"xrdsec.credentials"}),
    ],
)
def test_credential_discovery_loads_only_xrdsecs_credentials(statement, sec):
    """``Config``'s defaults need the CA directory, and nothing of the crypto stack."""
    loaded = {name for name in _loaded_by(statement) if name.startswith("xrdsec.")}
    assert loaded - {"xrdsec._version"} == sec


@pytest.mark.parametrize(
    ("statement", "core"),
    [
        ("import xrdclient.errors", {"xrdcore.errors"}),
        ("import xrdclient.safexml", {"xrdcore.safexml"}),
        ("import xrdclient.retry", {"xrdcore.retry"}),
        ("import xrdclient.crypto.crc32c", {"xrdcore.crc32c"}),
    ],
)
def test_a_client_module_loads_only_the_xrdcore_module_it_names(statement, core):
    """xrdcore is lazy too: an alias costs its one module, not the whole core."""
    loaded = {name for name in _loaded_by(statement) if name.startswith("xrdcore.")}
    assert loaded - {"xrdcore._version"} == core


def test_the_bare_import_loads_nothing_of_xrdhttp():
    """The HTTP pieces are a dependency too, loaded only once HTTP is used."""
    assert not {name for name in _loaded_by("import xrdclient") if name.startswith("xrdhttp")}


@pytest.mark.parametrize(
    "statement",
    ["import xrdclient.http", "import xrdclient.metalink", "import xrdclient.http.expect"],
)
def test_http_without_s3_loads_no_botocore(statement):
    """botocore is xrdhttp's, for S3 alone: WebDAV and Metalink never pay for it."""
    loaded = _loaded_by(statement)
    assert "botocore" not in loaded
    assert not {name for name in loaded if name.startswith("xrdhttp.s3")}


def test_s3_brings_xrdhttps_signer_and_botocore():
    """The receipt for the test above: the probe does see botocore arrive."""
    loaded = _loaded_by("import xrdclient.s3")
    assert {"botocore", "xrdhttp.s3.sigv4", "xrdhttp.s3.codec"} <= loaded


def test_the_heavy_stack_still_arrives_when_it_is_used():
    """The receipt that the probe can see these modules at all."""
    loaded = _loaded_by("import xrdclient; xrdclient.FileSystem")
    assert {"ssl", "xrdclient.client", "xrdclient.session"} <= loaded


def test_every_public_name_resolves():
    """``__all__`` is the promise; each name is one attribute access away."""
    for name in xrdclient.__all__:
        assert getattr(xrdclient, name) is not None, name
    assert set(xrdclient.__all__) <= set(dir(xrdclient))


def test_the_renamed_exports_are_the_same_objects():
    from xrdclient.io import open_url
    from xrdclient.path import XRootDPath

    assert xrdclient.open is open_url
    assert xrdclient.Path is XRootDPath is xrdclient.XRootDPath


def test_submodules_are_attributes_without_an_import_of_their_own():
    """``xrdclient.errors.NotFoundError`` worked by side effect once; it still does."""
    found = {
        info.name
        for info in pkgutil.iter_modules(xrdclient.__path__)
        if not info.name.startswith("_") and info.name != "copy"
    }
    assert found == set(xrdclient._SUBMODULES)
    assert xrdclient.errors.NotFoundError is xrdclient.NotFoundError


def test_copy_stays_the_function_whatever_is_imported_first():
    """A copy submodule imported before ``xrdclient.copy`` is read must not
    rebind the name to the subpackage."""
    loaded = _loaded_by(
        "import xrdclient.copy.limits, xrdclient\n"
        "assert callable(xrdclient.copy) and xrdclient.copy.__module__ == 'xrdclient.copy.engine'"
    )
    assert "xrdclient.copy.engine" in loaded


def test_an_unknown_name_is_still_an_attribute_error():
    with pytest.raises(AttributeError, match="has no attribute 'nonesuch'"):
        _ = xrdclient.nonesuch


def test_the_copy_package_resolves_its_names_lazily():
    import xrdclient.copy as package  # the function, as it always was

    assert callable(package)
    copy_package = sys.modules["xrdclient.copy"]
    assert set(copy_package.__all__) <= set(dir(copy_package))
    for name in copy_package.__all__:
        assert getattr(copy_package, name) is not None
    with pytest.raises(AttributeError, match="has no attribute 'nonesuch'"):
        _ = copy_package.nonesuch


def test_a_module_can_still_be_set_on_the_package(monkeypatch):
    """Only a submodule landing on a public name is refused; anything else binds."""
    import types

    stand_in = types.ModuleType("stand_in")
    monkeypatch.setattr(xrdclient, "stand_in", stand_in, raising=False)
    assert xrdclient.stand_in is stand_in
    # What the import system does for the subpackage: bind it over the name.
    monkeypatch.setattr(xrdclient, "copy", stand_in)
    assert callable(xrdclient.copy)
