"""The oldest interpreter this package runs on, held to from any of them.

RHEL 9 and AlmaLinux 9 ship Python 3.9, which is what most of the grid's
login nodes offer, so 3.9 is the floor. Nothing about a newer interpreter
makes code that breaks it fail there - the syntax parses, the ``|`` between
two classes evaluates, ``zip`` takes its ``strict=`` - which is why the floor
is checked here rather than trusted to whoever next runs the suite on a login
node.

Three checks, in order of how much they cost. Every shipped module is parsed
as 3.9 would parse it; every module is read for the handful of 3.10 and 3.11
spellings that parse anywhere but only *run* on a newer interpreter; and, if
a real 3.9 is installed, every module is imported into it.
"""

from __future__ import annotations

import ast
import os
import pathlib
import shutil
import subprocess
import sys
from dataclasses import dataclass

import pytest

import xrd
from xrd._compat import SLOTS, TIMEOUTS, flag_members, zip_strict
from xrd.flags import OpenFlags, StatInfoFlags

#: The floor. ``feature_version`` will not go below 3.7, so this is honest
#: about being a lower bound on what the parser will complain about.
FLOOR = (3, 9)

#: The one module allowed to know what version it is running on.
COMPAT = "_compat.py"

SOURCES = sorted(pathlib.Path(xrd.__file__).parent.rglob("*.py"))


def test_there_is_something_to_check():
    """A glob that matched nothing would pass every test in this file."""
    assert len(SOURCES) > 50


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_every_module_parses_as_the_oldest_supported_python(source):
    """Syntax first: the parser refuses what the grammar gained after 3.9.

    ``match``, ``except*``, the ``type`` statement and PEP 695 generics all
    fail here rather than at import time on somebody's login node.
    """
    ast.parse(source.read_text(), str(source), feature_version=FLOOR)


#: What parses on 3.9 and then raises when the line is reached. The value is
#: what to say instead, which is also what the rest of the package already
#: does.
LATER = {
    "zip(..., strict=)": "xrd._compat.zip_strict",
    "dataclass(slots=)": "**SLOTS from xrd._compat",
    "dataclass(kw_only=)": "a default of REQUIRED and a NEEDS tuple",
    "itertools.pairwise": "zip(seq, seq[1:])",
    "isinstance(x, A | B)": "isinstance(x, (A, B))",
    "sys.stdlib_module_names": "a skip, as tests/test_surface.py has",
}


def _later_spellings(source: pathlib.Path) -> list[tuple[int, str]]:
    """Every use in one module of something newer than the floor."""
    found = []
    for node in ast.walk(ast.parse(source.read_text())):
        found.extend(_later_call(node))
        found.extend(_later_attribute(node))
    return found


def _later_call(node: ast.AST) -> list[tuple[int, str]]:
    if not isinstance(node, ast.Call):
        return []
    keywords = {keyword.arg for keyword in node.keywords}
    name = node.func.id if isinstance(node.func, ast.Name) else ""
    return [
        *_later_zip(node, name, keywords),
        *_later_dataclass(node, name, keywords),
        *_later_isinstance(node, name),
    ]


def _later_zip(node: ast.Call, name: str, keywords: set[str | None]) -> list[tuple[int, str]]:
    return [(node.lineno, "zip(..., strict=)")] if name == "zip" and "strict" in keywords else []


def _later_dataclass(node: ast.Call, name: str, keywords: set[str | None]) -> list[tuple[int, str]]:
    if name != "dataclass":
        return []
    return [
        (node.lineno, f"dataclass({keyword}=)")
        for keyword in ("slots", "kw_only")
        if keyword in keywords
    ]


def _later_isinstance(node: ast.Call, name: str) -> list[tuple[int, str]]:
    is_union = len(node.args) == 2 and _is_union(node.args[1])
    return [(node.lineno, "isinstance(x, A | B)")] if name == "isinstance" and is_union else []


def _is_union(node: ast.AST) -> bool:
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)


def _later_attribute(node: ast.AST) -> list[tuple[int, str]]:
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
        return []
    spelling = f"{node.value.id}.{node.attr}"
    supported = ("itertools.pairwise", "sys.stdlib_module_names")
    return [(node.lineno, spelling)] if spelling in supported else []


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_no_module_spells_something_the_floor_cannot_run(source):
    """Then the spellings that parse everywhere and only run somewhere.

    A type annotation is not among them: every module imports
    ``annotations`` from ``__future__``, so ``str | None`` in one is a string
    until something asks. An alias assigned at the top of a module *is*
    evaluated, which is why two of them are spelled ``Union[...]``.
    """
    for line, spelling in _later_spellings(source):
        assert source.name == COMPAT, f"{source.name}:{line}: {spelling} - say {LATER[spelling]}"


def test_the_alias_assignments_that_have_to_be_evaluated_are_evaluated():
    """The receipt for that last paragraph, since importing proves it."""
    from xrd.easy import Location
    from xrd.testing.http import Handler

    assert str in Location.__args__ and Handler is not None


def test_a_real_3_9_can_import_every_module():
    """And last, the interpreter itself, when the machine has one.

    Nothing about this package needs installing to be imported - the core is
    the standard library and a ``PYTHONPATH`` - so a system Python is enough,
    and a module that needs an extra says so by name rather than failing.
    """
    python = shutil.which("python3.9")
    if python is None:
        pytest.skip("no python3.9 on PATH to check the floor against")
    src = str(pathlib.Path(xrd.__file__).parent.parent)
    probe = """
import importlib, pkgutil, xrd

bad = []
for info in pkgutil.walk_packages(xrd.__path__, "xrd."):
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


def test_a_dataclass_is_slotted_wherever_slots_exist():
    """``**SLOTS`` is ``slots=True`` on 3.10 and later, and nothing on 3.9."""

    @dataclass(frozen=True, **SLOTS)
    class Point:
        x: int
        y: int = 0

    assert bool(SLOTS) == (sys.version_info >= (3, 10))
    assert hasattr(Point, "__slots__") == bool(SLOTS)
    assert Point(1).y == 0


def test_pairs_that_match_are_paired():
    """The everyday case, whichever implementation is behind it."""
    assert list(zip_strict("abc", [1, 2, 3])) == [("a", 1), ("b", 2), ("c", 3)]
    assert list(zip_strict("ab", [1, 2], [True, False])) == [("a", 1, True), ("b", 2, False)]
    assert list(zip_strict((), ())) == []


def test_pairs_that_do_not_match_are_refused():
    """Which is the entire reason for not writing plain ``zip``.

    The wording is the implementation's own - 3.10 says which argument ran
    out first - so what is asserted is that it is refused at all, and refused
    whichever side is the short one.
    """
    with pytest.raises(ValueError):
        list(zip_strict("abc", [1, 2]))
    with pytest.raises(ValueError):
        list(zip_strict("ab", [1, 2, 3]))


def test_the_written_out_check_behaves_as_the_one_in_c_does(monkeypatch):
    """3.9 takes the other branch, so the other branch is tested here.

    Without this the fallback would be dead code on the interpreter the
    coverage gate runs on, and alive on the one that has no gate at all.
    """
    monkeypatch.setattr("xrd._compat._NATIVE_STRICT_ZIP", False)
    assert list(zip_strict("abc", [1, 2, 3])) == [("a", 1), ("b", 2), ("c", 3)]
    with pytest.raises(ValueError, match="different lengths"):
        list(zip_strict("abc", [1, 2]))
    with pytest.raises(ValueError, match="different lengths"):
        list(zip_strict([1, 2], "abc"))


def test_a_flag_comes_apart_into_the_bits_it_was_made_of():
    """3.11 taught a flag to iterate; here it is done by hand, in bit order."""
    flags = StatInfoFlags.IS_DIR | StatInfoFlags.IS_READABLE
    assert flag_members(flags) == [StatInfoFlags.IS_DIR, StatInfoFlags.IS_READABLE]
    assert flag_members(OpenFlags.NEW) == [OpenFlags.NEW]
    assert flag_members(OpenFlags.NONE) == []


def test_a_bit_nobody_named_is_not_a_member():
    """A server may set a bit this client has never heard of; it is dropped."""
    spare = 1 << 30
    assert not any(flag.value == spare for flag in StatInfoFlags)
    assert flag_members(StatInfoFlags(spare | StatInfoFlags.IS_DIR)) == [StatInfoFlags.IS_DIR]


def test_a_socket_giving_up_is_caught_by_one_name_on_every_version():
    """3.10 made the two names one; before that they were two exceptions."""
    import socket

    assert socket.timeout in TIMEOUTS and TimeoutError in TIMEOUTS
    assert issubclass(socket.timeout, OSError)
