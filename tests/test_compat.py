"""The oldest interpreter this package runs on, held to from any of them.

AlmaLinux 8 and 9 offer python3.11 and python3.12 from AppStream, and
AlmaLinux 10 and Ubuntu 24.04 ship 3.12 as ``python3``, so 3.11 is the floor.
Nothing about a newer interpreter makes code that breaks it fail there - a
``type`` statement parses, ``itertools.batched`` resolves - which is why the
floor is checked here rather than trusted to whoever next runs the suite on
a login node.

Three checks, in order of how much they cost. Every shipped module is parsed
as 3.11 would parse it; every module is read for the handful of 3.12 and 3.13
names that parse anywhere but only *resolve* on a newer interpreter; and, if
a real 3.11 is on ``PATH``, every module is imported into it.

The rest pins the 3.11 behaviour the package now leans on directly, where it
once carried a fallback of its own: slotted dataclasses, flags that iterate
into their bits, and one exception for a socket that gave up.
"""

from __future__ import annotations

import ast
import os
import pathlib
import shutil
import socket
import subprocess

import pytest

import xrdclient
from xrdclient.flags import OpenFlags, StatInfoFlags
from xrdclient.types import StatInfo

#: The floor. ``feature_version`` makes the parser refuse grammar newer than
#: this: PEP 695 generics and the ``type`` statement among it.
FLOOR = (3, 11)

SOURCES = sorted(pathlib.Path(xrdclient.__file__).parent.rglob("*.py"))


def test_there_is_something_to_check():
    """A glob that matched nothing would pass every test in this file."""
    assert len(SOURCES) > 50


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_every_module_parses_as_the_oldest_supported_python(source):
    """Syntax first: the parser refuses what the grammar gained after 3.11.

    ``class Box[T]``, ``def first[T](...)`` and ``type Alias = ...`` all fail
    here rather than at import time on somebody's login node.
    """
    ast.parse(source.read_text(), str(source), feature_version=FLOOR)


#: Standard-library names that parse on 3.11 and fail when the line is
#: reached, mapped to the release that added them.
LATER = {
    "itertools.batched": "3.12",
    "typing.override": "3.12",
    "warnings.deprecated": "3.13",
}


def _later_spellings(source: pathlib.Path) -> list[tuple[int, str]]:
    """Every use in one module of a name newer than the floor.

    Both spellings count: ``itertools.batched(...)`` and
    ``from itertools import batched``.
    """
    found = []
    for node in ast.walk(ast.parse(source.read_text())):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            found.append((node.lineno, f"{node.value.id}.{node.attr}"))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.extend((node.lineno, f"{node.module}.{alias.name}") for alias in node.names)
    return [(line, spelling) for line, spelling in found if spelling in LATER]


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_no_module_names_something_the_floor_has_not_got(source):
    """Then the names that parse everywhere and only resolve somewhere."""
    found = _later_spellings(source)
    assert not found, [f"{source.name}:{line}: {name} needs {LATER[name]}" for line, name in found]


def test_the_later_name_check_sees_both_spellings(tmp_path):
    """The check above passes trivially if it never matches anything."""
    probe = tmp_path / "probe.py"
    probe.write_text("import itertools\nfrom typing import override\nitertools.batched([], 2)\n")
    assert _later_spellings(probe) == [(2, "typing.override"), (3, "itertools.batched")]


def test_a_real_3_11_can_import_every_module():
    """And last, the interpreter itself, when the machine has one.

    The selected interpreter needs the base dependencies installed. Optional
    adapters still identify their missing extras with a pip-install hint.
    """
    python = shutil.which("python3.11")
    if python is None:
        pytest.skip("environment: no python3.11 on PATH to check the floor against")
    required = subprocess.run(
        [python, "-c", "import cryptography, botocore, asn1crypto"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if required.returncode:
        pytest.skip("environment: python3.11 lacks the base dependencies: " + required.stderr)
    src = str(pathlib.Path(xrdclient.__file__).parent.parent)
    probe = """
import importlib, pkgutil, xrdclient

bad = []
for info in pkgutil.walk_packages(xrdclient.__path__, "xrdclient."):
    try:
        importlib.import_module(info.name)
    except ImportError as exc:
        if "pip install" not in str(exc):        # an optional extra, absent
            bad.append(f"{info.name}: {exc}")
    except Exception as exc:
        bad.append(f"{info.name}: {type(exc).__name__}: {exc}")
print("\\n".join(bad))
"""
    out = subprocess.run(
        [python, "-c", probe],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": src},
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", out.stdout


def test_the_records_are_slotted():
    """``@dataclass(slots=True)`` throughout: smaller records, quicker fields."""
    info = StatInfo(flags=StatInfoFlags.IS_DIR)
    assert hasattr(StatInfo, "__slots__") and not hasattr(info, "__dict__")


def test_a_flag_comes_apart_into_the_bits_it_was_made_of():
    """3.11 flags iterate into their single named bits, in declaration order."""
    flags = StatInfoFlags.IS_DIR | StatInfoFlags.IS_READABLE
    assert list(flags) == [StatInfoFlags.IS_DIR, StatInfoFlags.IS_READABLE]
    assert list(OpenFlags.NEW) == [OpenFlags.NEW]
    assert list(OpenFlags.NONE) == []
    assert str(flags) == "is_dir|is_readable"


def test_a_bit_nobody_named_is_not_a_member():
    """A server may set a bit this client has never heard of; it is dropped."""
    spare = 1 << 30
    assert not any(flag.value == spare for flag in StatInfoFlags)
    unknown = StatInfoFlags(spare | StatInfoFlags.IS_DIR)
    assert list(unknown) == [StatInfoFlags.IS_DIR]
    assert int(unknown) & spare  # kept on the value, only left out of the parts
    assert str(StatInfoFlags(spare)) == str(spare)


def test_a_socket_giving_up_is_caught_by_one_name():
    """Since 3.10 ``socket.timeout`` is :exc:`TimeoutError`, so one ``except`` does."""
    assert socket.timeout is TimeoutError
    assert issubclass(TimeoutError, OSError)
