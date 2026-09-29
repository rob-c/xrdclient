"""The async facade is whole: every public sync name has an awaitable mirror.

``xrdclient.aio`` promises the synchronous surface with ``await`` in front.
That promise is easy to break by accident - a method added to
:class:`xrdclient.FileSystem` and never to its mirror - so these gates compare
the two surfaces name by name, and argument by argument, and fail the suite
the moment they drift. Behaviour is :mod:`test_aio`'s business; this file only
checks that there is something there to behave.
"""

from __future__ import annotations

import inspect

import pytest

import xrdclient
import xrdclient.aio
from xrdclient import easy
from xrdclient.aio import AsyncFile, AsyncFileSystem

#: Sync ``File`` names with no awaitable twin, and why there should not be one.
_FILE_EXEMPT = {
    "open": "an AsyncFile is born open, by xrdclient.aio.open or AsyncFileSystem.open",
    "handle": "the wire handle is protocol plumbing; AsyncFile.file reaches it",
    "session": "a live sync session is no use from a coroutine; AsyncFile.file reaches it",
}

#: Mirrored names whose arguments differ on purpose. ``AsyncFile`` is shaped
#: like :mod:`io` - a cursor, not an offset per call - and ``pread`` and
#: ``pwrite`` are its positional spellings.
_FILE_IO_SHAPED = {"read", "readinto", "write"}

#: The module-level functions that do I/O and so must be awaitable too.
_VERBS = [*easy.__all__, "open", "copy", "copy_tree", "third_party"]


def _public(cls: type) -> set[str]:
    return {name for name in dir(cls) if not name.startswith("_")}


def _parameters(func: object) -> list[str]:
    return list(inspect.signature(func).parameters)  # type: ignore[arg-type]


def _is_method(cls: type, name: str) -> bool:
    member = inspect.getattr_static(cls, name)
    return callable(member) and not isinstance(member, property)


def test_every_filesystem_method_has_a_mirror():
    assert _public(xrdclient.FileSystem) - _public(AsyncFileSystem) == set()


def test_every_file_method_has_a_mirror_or_a_reason_not_to():
    missing = _public(xrdclient.File) - _public(AsyncFile) - set(_FILE_EXEMPT)
    assert missing == set()
    # An exemption for something since mirrored is a stale excuse.
    assert set(_FILE_EXEMPT) & _public(AsyncFile) == set()


@pytest.mark.parametrize(
    ("sync", "mirror", "exempt"),
    [
        (xrdclient.FileSystem, AsyncFileSystem, set()),
        (xrdclient.File, AsyncFile, _FILE_IO_SHAPED),
    ],
    ids=["FileSystem", "File"],
)
def test_the_mirrors_take_the_same_arguments(sync, mirror, exempt):
    differ = {
        name: (_parameters(getattr(sync, name)), _parameters(getattr(mirror, name)))
        for name in _public(sync) & _public(mirror)
        if _is_method(sync, name) and _is_method(mirror, name) and name not in exempt
        if _parameters(getattr(sync, name)) != _parameters(getattr(mirror, name))
    }
    assert differ == {}


@pytest.mark.parametrize("name", _VERBS)
def test_every_module_level_verb_has_an_awaitable_twin(name):
    twin = getattr(xrdclient.aio, name, None)
    assert twin is not None, f"xrdclient.aio.{name} is missing"
    assert name in xrdclient.aio.__all__
    if name != "open":  # open is awaitable *and* an async context manager
        assert inspect.iscoroutinefunction(twin)
    # The copy family forwards ``**kwargs`` whole, and open leaves out the
    # internal ``router``; the easy verbs are small enough to spell out.
    if name in easy.__all__:
        assert _parameters(twin) == _parameters(getattr(xrdclient, name))
