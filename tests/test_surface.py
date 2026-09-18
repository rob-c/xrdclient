"""The public surface, every name of it, against a real server.

The other modules test what each operation *means*. This one tests that the
surface is whole: every public method of :class:`~xrdclient.FileSystem`,
:class:`~xrdclient.File` and :class:`~xrdclient.XRootDPath`, and every name in
``xrdclient.__all__``, is called here at least once and comes back.

The point is the gate at the end of each section. It compares the table of
exercises against the class's actual public attributes, so a method added
without a test - or renamed out from under one - fails the suite rather than
quietly going unexercised.
"""

from __future__ import annotations

import ast
import importlib
import os
import pathlib
import subprocess
import sys

import pytest

import xrdclient
from xrdclient.client.file import File
from xrdclient.client.filesystem import FileSystem
from xrdclient.config import Config
from xrdclient.flags import (
    Access,
    DirListFlags,
    LocateFlags,
    MkDirFlags,
    OpenFlags,
    PrepareFlags,
    QueryCode,
    StatInfoFlags,
)
from xrdclient.path import XRootDPath
from xrdclient.testing import FakeServer
from xrdclient.types import ReadRange, WriteChunk

CONFIG = Config(username="tester", auth_order=("host",), require_tls=False)

FILES = {"/store/f.root": b"payload one", "/store/sub/g.root": b"payload two"}


def public(cls: type) -> set[str]:
    """Every name a user of ``cls`` is meant to be able to reach."""
    return {name for name in vars(cls) if not name.startswith("_")}


@pytest.fixture
def srv():
    with FakeServer(files=dict(FILES), dirs=["/store/empty"]) as server:
        yield server


@pytest.fixture
def fs(srv):
    filesystem = FileSystem(srv.url.with_path("/store"), CONFIG)
    try:
        yield filesystem
    finally:
        filesystem.close()


@pytest.fixture
def fh(fs):
    """One file open for update, sharing the filesystem's connection."""
    handle = File(fs.url.with_path("/store/f.root"), fs.config, router=fs._router)
    handle.open(OpenFlags.UPDATE)
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture
def path(srv):
    p = xrdclient.Path(srv.url.with_path("/store/f.root"), config=CONFIG)
    try:
        yield p
    finally:
        p.close()


# ---------------------------------------------------------------------------
# FileSystem
# ---------------------------------------------------------------------------


def _fs_xattr(fs):
    fs.setxattr("f.root", "user.k", b"v")
    return fs.getxattr("f.root", "user.k")


def _fs_readlink(fs):
    fs.symlink("f.root", "pointer.root")
    return fs.readlink("pointer.root")


def _fs_lstat(fs):
    fs.symlink("f.root", "lstat-me.root")
    return fs.lstat("lstat-me.root")


def _fs_removexattr(fs):
    fs.setxattr("f.root", "user.k", b"v")
    return fs.removexattr("f.root", "user.k")


FILESYSTEM = {
    "appid": lambda fs: fs.appid("surface-test"),
    "cancel_prepare": lambda fs: fs.cancel_prepare(fs.prepare(["f.root"])),
    "checksum": lambda fs: fs.checksum("f.root"),
    "checksum_cancel": lambda fs: fs.checksum_cancel("f.root"),
    "chmod": lambda fs: fs.chmod("f.root", 0o640),
    "chown": lambda fs: fs.chown("f.root", 1000, 1000),
    "close": lambda fs: fs.close(),
    "deep_locate": lambda fs: fs.deep_locate("f.root"),
    "endpoint": lambda fs: fs.endpoint,
    "evict": lambda fs: fs.evict(["f.root"]),
    "exists": lambda fs: fs.exists("f.root"),
    "extensions": lambda fs: fs.extensions(),
    "getsize": lambda fs: fs.getsize("f.root"),
    "getxattr": _fs_xattr,
    "glob": lambda fs: list(fs.glob("/store/*.root")),
    "isdir": lambda fs: fs.isdir("sub"),
    "isfile": lambda fs: fs.isfile("f.root"),
    "iterdir": lambda fs: list(fs.iterdir(".")),
    "listdir": lambda fs: fs.listdir("."),
    "hardlink": lambda fs: fs.hardlink("f.root", "hard.root"),
    "link": lambda fs: fs.link("f.root", "linked.root"),
    "listxattr": lambda fs: fs.listxattr("f.root"),
    "listxattr_tree": lambda fs: fs.listxattr_tree("."),
    "locate": lambda fs: fs.locate("f.root"),
    "makedirs": lambda fs: fs.makedirs("deep/deeper"),
    "mkdir": lambda fs: fs.mkdir("fresh"),
    "move": lambda fs: fs.move("f.root", "moved.root"),
    "open": lambda fs: fs.open("f.root").read(),
    "ping": lambda fs: fs.ping(),
    "prepare": lambda fs: fs.prepare(["f.root"]),
    "protocol": lambda fs: fs.protocol(),
    "query": lambda fs: fs.query(QueryCode.CONFIG, "version"),
    "query_config": lambda fs: fs.query_config("version"),
    "archive_info": lambda fs: fs.archive_info(["f.root"]),
    "query_prepare": lambda fs: fs.query_prepare(fs.prepare(["f.root"]), ["f.root"]),
    "query_space": lambda fs: fs.query_space("."),
    "query_stats": lambda fs: fs.query_stats(),
    "read_bytes": lambda fs: fs.read_bytes("f.root"),
    "readlink": _fs_readlink,
    "read_text": lambda fs: fs.read_text("f.root"),
    "remove": lambda fs: fs.remove("f.root"),
    "removexattr": _fs_removexattr,
    "rename": lambda fs: fs.rename("f.root", "renamed.root"),
    "rmdir": lambda fs: fs.rmdir("empty"),
    "rmtree": lambda fs: fs.rmtree("sub"),
    "scandir": lambda fs: fs.scandir("."),
    "setxattr": lambda fs: fs.setxattr("f.root", "user.k", b"v"),
    "set_property": lambda fs: fs.set_property("appid surface-test"),
    "symlink": lambda fs: fs.symlink("f.root", "soft.root"),
    "stat": lambda fs: fs.stat("f.root"),
    "lstat": _fs_lstat,
    "is_symlink": lambda fs: fs.is_symlink("f.root"),
    "statvfs": lambda fs: fs.statvfs("."),
    "statx": lambda fs: fs.statx(["f.root", "sub"]),
    "touch": lambda fs: fs.touch("touched.root"),
    "truncate": lambda fs: fs.truncate("f.root", 4),
    "unlink": lambda fs: fs.unlink("f.root"),
    "utime": lambda fs: fs.utime("f.root", (1, 2)),
    "walk": lambda fs: list(fs.walk(".")),
    "write_bytes": lambda fs: fs.write_bytes("w.root", b"x"),
    "write_text": lambda fs: fs.write_text("w.txt", "x"),
    "xattrs": lambda fs: fs.xattrs("f.root"),
}


@pytest.mark.parametrize("name", sorted(FILESYSTEM))
def test_this_filesystem_method_works_against_a_server(fs, name):
    FILESYSTEM[name](fs)


def test_every_public_filesystem_method_is_exercised():
    assert set(FILESYSTEM) == public(FileSystem)


# ---------------------------------------------------------------------------
# File
# ---------------------------------------------------------------------------


def _file_reopen(fh):
    fh.close()
    return fh.open(OpenFlags.READ)


def _file_checkpoint(fh):
    with fh.checkpoint():
        fh.write(b"C", 0)
    return fh.read()


def _file_xattr(fh):
    fh.setxattr("user.k", b"v")
    return fh.getxattr("user.k")


def _file_removexattr(fh):
    fh.setxattr("user.k", b"v")
    return fh.removexattr("user.k")


FILE = {
    "bind_data_path": lambda fh: fh.bind_data_path(),
    "checkpoint": _file_checkpoint,
    "checksum": lambda fh: fh.checksum(),
    "close": lambda fh: fh.close(),
    "compression": lambda fh: fh.compression,
    "data_path": lambda fh: fh.data_path,
    "endpoint": lambda fh: fh.endpoint,
    "session": lambda fh: fh.session,
    "flush": lambda fh: fh.flush(),
    "getxattr": _file_xattr,
    "handle": lambda fh: fh.handle,
    "is_open": lambda fh: fh.is_open,
    "listxattr": lambda fh: fh.listxattr(),
    "open": _file_reopen,
    "pgread": lambda fh: fh.pgread(4, 0),
    "pgwrite": lambda fh: fh.pgwrite(b"page", 0),
    "pread": lambda fh: fh.pread(4, 0),
    "pwrite": lambda fh: fh.pwrite(b"four", 0),
    "read": lambda fh: fh.read(),
    "readinto": lambda fh: fh.readinto(bytearray(4)),
    "readv": lambda fh: fh.readv([ReadRange(0, 4), (4, 3)]),
    "recoverable": lambda fh: fh.recoverable,
    "removexattr": _file_removexattr,
    "setxattr": lambda fh: fh.setxattr("user.k", b"v"),
    "size": lambda fh: fh.size,
    "stat": lambda fh: fh.stat(refresh=True),
    "sync": lambda fh: fh.sync(),
    "truncate": lambda fh: fh.truncate(4),
    "verify": lambda fh: fh.verify(fh.checksum().value),
    "visa": lambda fh: fh.visa(),
    "write": lambda fh: fh.write(b"W", 0),
    "writev": lambda fh: fh.writev([WriteChunk(0, b"a"), (2, b"b")]),
    "clone": lambda fh: fh.clone(fh, [(0, 2, 4)]),
}


@pytest.mark.parametrize("name", sorted(FILE))
def test_this_file_method_works_against_a_server(fh, name):
    FILE[name](fh)


def test_every_public_file_method_is_exercised():
    assert set(FILE) == public(File)


# ---------------------------------------------------------------------------
# XRootDPath
# ---------------------------------------------------------------------------


def _path_open(p):
    with p.open("rb") as fh:
        return fh.read()


PATH = {
    "anchor": lambda p: p.anchor,
    "checksum": lambda p: p.checksum(),
    "chmod": lambda p: p.chmod(0o640),
    "close": lambda p: p.close(),
    "exists": lambda p: p.exists(),
    "fs": lambda p: p.fs,
    "glob": lambda p: list(p.parent.glob("*.root")),
    "is_absolute": lambda p: p.is_absolute(),
    "is_dir": lambda p: p.parent.is_dir(),
    "is_file": lambda p: p.is_file(),
    "iterdir": lambda p: list(p.parent.iterdir()),
    "joinpath": lambda p: p.parent.joinpath("sub", "g.root").read_bytes(),
    "locate": lambda p: p.locate(),
    "mkdir": lambda p: (p.parent / "fresh").mkdir(),
    "name": lambda p: p.name,
    "open": _path_open,
    "parent": lambda p: p.parent,
    "parents": lambda p: p.parents,
    "parts": lambda p: p.parts,
    "read_bytes": lambda p: p.read_bytes(),
    "read_text": lambda p: p.read_text(),
    "relative_to": lambda p: p.relative_to("/store"),
    "rename": lambda p: p.rename("/store/renamed.root"),
    "replace": lambda p: p.replace("/store/replaced.root"),
    "rglob": lambda p: list(p.parent.rglob("*.root")),
    "rmdir": lambda p: (p.parent / "empty").rmdir(),
    "stat": lambda p: p.stat(),
    "lstat": lambda p: p.lstat(),
    "is_symlink": lambda p: p.is_symlink(),
    "stem": lambda p: p.stem,
    "suffix": lambda p: p.suffix,
    "suffixes": lambda p: p.suffixes,
    "touch": lambda p: (p.parent / "touched.root").touch(),
    "unlink": lambda p: p.unlink(),
    "url": lambda p: p.url,
    "walk": lambda p: list(p.parent.walk()),
    "with_name": lambda p: p.with_name("other.root"),
    "with_stem": lambda p: p.with_stem("other"),
    "with_suffix": lambda p: p.with_suffix(".dat"),
    "write_bytes": lambda p: p.write_bytes(b"x"),
    "write_text": lambda p: p.write_text("x"),
}


@pytest.mark.parametrize("name", sorted(PATH))
def test_this_path_method_works_against_a_server(path, name):
    PATH[name](path)


def test_every_public_path_method_is_exercised():
    assert set(PATH) == public(XRootDPath)


# ---------------------------------------------------------------------------
# The package's own namespace
# ---------------------------------------------------------------------------


ERRORS = sorted(name for name in xrdclient.__all__ if name.endswith("Error"))


@pytest.mark.parametrize("name", ERRORS)
def test_this_error_is_catchable_as_this_packages_error(name):
    """One ``except xrdclient.XRootDError`` catches everything this package raises."""
    exception = getattr(xrdclient, name)
    assert issubclass(exception, xrdclient.XRootDError)
    assert issubclass(exception, Exception)


def test_the_errors_python_already_has_names_for_are_those(fs):
    """A missing file is a ``FileNotFoundError`` before it is anything else."""
    assert issubclass(xrdclient.NotFoundError, FileNotFoundError)
    assert issubclass(xrdclient.TimeoutError, TimeoutError)
    assert issubclass(xrdclient.ConnectionError, ConnectionError)


def _use_config(srv):
    with xrdclient.override(username="other"):
        assert xrdclient.current().username == "other"
    xrdclient.configure(username=CONFIG.username)
    return xrdclient.current()


def _use_copy(srv, tmp_path):
    return xrdclient.copy(srv.url.with_path("/store/f.root"), tmp_path / "out.root", config=CONFIG)


def _use_copy_tree(srv, tmp_path):
    return xrdclient.copy_tree(srv.url.with_path("/store"), tmp_path / "tree", config=CONFIG)


def _use_third_party(srv, tmp_path):
    with FakeServer() as destination:
        return xrdclient.third_party(
            srv.url.with_path("/store/f.root"),
            destination.url.with_path("/pushed.root"),
            config=CONFIG,
        )


def _use_file(srv, tmp_path):
    with xrdclient.File(srv.url.with_path("/store/f.root"), CONFIG) as fh:
        return fh.read()


def _use_checkpoint(srv, tmp_path):
    fh = xrdclient.File(srv.url.with_path("/store/f.root"), CONFIG)
    fh.open(OpenFlags.UPDATE)
    try:
        with fh.checkpoint() as checkpoint:
            fh.write(b"C", 0)
            return checkpoint.query()
    finally:
        fh.close()


def _use_aio(srv, tmp_path):
    import asyncio

    async def go():
        async with xrdclient.aio.FileSystem(srv.url.with_path("/store"), CONFIG) as fs:
            return await fs.listdir(".")

    return asyncio.run(go())


def _one_file(srv):
    """The URL the one-line verbs below ask their questions about."""
    return srv.url.with_path("/store/f.root")


def _use_mkdir(srv, tmp):
    url = srv.url.with_path("/store/made/deeper")
    xrdclient.mkdir(url, config=CONFIG)
    return xrdclient.exists(url, config=CONFIG)


def _use_remove(srv, tmp):
    url = srv.url.with_path("/store/doomed.txt")
    xrdclient.write_text(url, "x", config=CONFIG)
    xrdclient.remove(url, config=CONFIG)
    return not xrdclient.exists(url, config=CONFIG)


def _use_move(srv, tmp):
    source, target = srv.url.with_path("/store/from.txt"), srv.url.with_path("/store/to.txt")
    xrdclient.write_text(source, "moved", config=CONFIG)
    xrdclient.move(source, target, config=CONFIG)
    return xrdclient.read_text(target, config=CONFIG)


NAMES = {
    "Access": lambda srv, tmp: int(Access.OWNER_READ | Access.OWNER_WRITE) > 0,
    "Checkpoint": _use_checkpoint,
    "CheckpointInfo": lambda srv, tmp: xrdclient.CheckpointInfo(capacity=8, used=2).free,
    "ChecksumInfo": lambda srv, tmp: xrdclient.ChecksumInfo("adler32", "00010203").value,
    "Config": lambda srv, tmp: CONFIG.evolve(username="someone").username,
    "CopyResult": lambda srv, tmp: _use_copy(srv, tmp).size,
    "Check": lambda srv, tmp: str(xrdclient.Check("a", "ok", "fine")),
    "Report": lambda srv, tmp: xrdclient.Report().ok,
    "diagnose": lambda srv, tmp: xrdclient.diagnose(str(srv.url) + "store/f.root").ok,
    "DirEntry": lambda srv, tmp: xrdclient.DirEntry(name="f.root", parent="/store").path,
    "DirListFlags": lambda srv, tmp: DirListFlags.STAT | DirListFlags.NONE,
    "File": _use_file,
    "FileSystem": lambda srv, tmp: xrdclient.FileSystem(srv.url, CONFIG).close(),
    "LocationInfo": lambda srv, tmp: xrdclient.LocationInfo(address="a:1094").address,
    "LocateFlags": lambda srv, tmp: LocateFlags.FOR_DIRLIST | LocateFlags.NONE,
    "MkDirFlags": lambda srv, tmp: MkDirFlags.MAKEPATH | MkDirFlags.NONE,
    "OpenFlags": lambda srv, tmp: OpenFlags.READ | OpenFlags.UPDATE,
    "PageResult": lambda srv, tmp: xrdclient.PageResult(data=b"x", offset=0).data,
    "PrepareFlags": lambda srv, tmp: PrepareFlags.STAGE | PrepareFlags.NONE,
    "Path": lambda srv, tmp: xrdclient.Path(srv.url.with_path("/store"), config=CONFIG).close(),
    "ProtocolInfo": lambda srv, tmp: xrdclient.ProtocolInfo(version=0x310).version,
    "QueryCode": lambda srv, tmp: int(QueryCode.CHECKSUM),
    "CloneRange": lambda srv, tmp: xrdclient.CloneRange(0, 4).destination,
    "ReadRange": lambda srv, tmp: xrdclient.ReadRange(0, 4).length,
    "PrepareStatus": lambda srv, tmp: str(xrdclient.PrepareStatus(path="/a", online=True)),
    "SpaceInfo": lambda srv, tmp: xrdclient.SpaceInfo(free=1).unlimited,
    "StatInfo": lambda srv, tmp: xrdclient.StatInfo(st_size=1).st_size,
    "SyncMode": lambda srv, tmp: xrdclient.copy_tree.__doc__ and "size" in str(xrdclient.SyncMode),
    "StatInfoFlags": lambda srv, tmp: StatInfoFlags.IS_READABLE | StatInfoFlags.NONE,
    "VFSInfo": lambda srv, tmp: xrdclient.VFSInfo(nodes_rw=1).nodes_rw,
    "WriteChunk": lambda srv, tmp: xrdclient.WriteChunk(0, b"x").data,
    "XRootDPath": lambda srv, tmp: XRootDPath(srv.url.with_path("/store/f.root")).name,
    "XRootDURL": lambda srv, tmp: xrdclient.XRootDURL(host="a", port=1094).endpoint,
    "__version__": lambda srv, tmp: xrdclient.__version__.split(".")[0],
    "aio": _use_aio,
    "configure": _use_config,
    "copy": _use_copy,
    "copy_tree": _use_copy_tree,
    "current": lambda srv, tmp: xrdclient.current().username,
    "find_config_file": lambda srv, tmp: xrdclient.find_config_file(),
    "open": lambda srv, tmp: xrdclient.open(
        srv.url.with_path("/store/f.root"), config=CONFIG
    ).read(),
    "override": _use_config,
    "parse": lambda srv, tmp: xrdclient.parse("root://host:1094//store/f.root").path,
    "third_party": _use_third_party,
    # the one-line verbs
    "ls": lambda srv, tmp: xrdclient.ls(srv.url.with_path("/store"), config=CONFIG),
    "glob": lambda srv, tmp: xrdclient.glob(srv.url.with_path("/store/*.root"), config=CONFIG),
    "stat": lambda srv, tmp: xrdclient.stat(_one_file(srv), config=CONFIG),
    "exists": lambda srv, tmp: xrdclient.exists(_one_file(srv), config=CONFIG),
    "size": lambda srv, tmp: xrdclient.size(_one_file(srv), config=CONFIG),
    "checksum": lambda srv, tmp: xrdclient.checksum(_one_file(srv), config=CONFIG),
    "read_bytes": lambda srv, tmp: xrdclient.read_bytes(_one_file(srv), config=CONFIG),
    "read_text": lambda srv, tmp: xrdclient.read_text(_one_file(srv), config=CONFIG),
    "write_bytes": lambda srv, tmp: xrdclient.write_bytes(srv.url / "w.bin", b"x", config=CONFIG),
    "write_text": lambda srv, tmp: xrdclient.write_text(srv.url / "w.txt", "x", config=CONFIG),
    "mkdir": _use_mkdir,
    "remove": _use_remove,
    "move": _use_move,
    "stage": lambda srv, tmp: xrdclient.stage(_one_file(srv), config=CONFIG),
    "is_online": lambda srv, tmp: xrdclient.is_online(_one_file(srv), config=CONFIG),
    "human_bytes": lambda srv, tmp: xrdclient.human_bytes(1536),
}


@pytest.mark.parametrize("name", sorted(NAMES))
def test_this_public_name_is_usable(srv, tmp_path, name):
    call = NAMES[name]
    result = call(srv, tmp_path) if call.__code__.co_argcount == 2 else call(srv)
    assert result is not None or name in {"FileSystem", "Path"}


def test_every_public_name_is_exercised():
    """``xrdclient.__all__`` is the promise; this is the receipt."""
    assert set(NAMES) | set(ERRORS) == set(xrdclient.__all__)


def test_nothing_public_is_missing_from_dunder_all():
    """A name importable from ``xrd`` but absent from ``__all__`` is a leak."""
    import types

    exported = {
        name
        for name, value in vars(xrdclient).items()
        if not name.startswith("_")
        and not isinstance(value, types.ModuleType)  # submodules are not the API
        and name != "annotations"  # ``from __future__``, not ours
    }
    assert exported <= set(xrdclient.__all__)


def _imports(source: pathlib.Path) -> list[tuple[str, bool]]:
    """Every module ``source`` imports, and whether the import can fail safely.

    Safe means the import cannot break ``import xrdclient`` on a bare interpreter:
    either an ``ImportError`` is caught (or turned into a better one), or it
    only runs when the function around it is called.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    guarded = _guarded_nodes(tree)
    found = []
    for node in ast.walk(tree):
        found.extend((name, node in guarded) for name in _import_names(node))
    return found


def _guarded_nodes(tree: ast.AST) -> set[ast.AST]:
    guarded: set[ast.AST] = set()
    for node in ast.walk(tree):
        shelter = isinstance(node, (ast.Try, ast.FunctionDef, ast.AsyncFunctionDef))
        for child in ast.iter_child_nodes(node):
            if shelter or node in guarded:
                guarded.add(child)
    return guarded


def _import_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module.split(".")[0]]
    return []


#: Both purity checks below read the interpreter's own list of standard
#: library module names, which is 3.10 and later. The package's floor is 3.9,
#: where the list simply does not exist; every other version still runs them.
needs_stdlib_names = pytest.mark.skipif(
    not hasattr(sys, "stdlib_module_names"),
    reason="sys.stdlib_module_names arrived in 3.10",
)


@needs_stdlib_names
def test_the_package_needs_nothing_but_the_standard_library():
    """Pure Python: an interpreter and this package are the whole install list.

    Checked in the source rather than in :data:`sys.modules`, which by now
    holds whatever every other test has imported. The optional extras -
    ``fsspec``, ``gssapi``, ``google_crc32c`` - may be named, but only where a
    missing one cannot stop the package from importing.
    """
    stdlib = set(sys.stdlib_module_names) | {"xrdclient", "_typeshed"}
    offenders = {
        f"{source.name}: {name}"
        for source in pathlib.Path(xrdclient.__file__).parent.rglob("*.py")
        for name, guarded in _imports(source)
        if name not in stdlib and not guarded
    }
    assert not offenders


@needs_stdlib_names
def test_importing_the_package_pulls_in_no_third_party_module():
    """The receipt for the test above, from an interpreter of its own."""
    script = (
        "import sys; import xrdclient; "
        "print(sorted({n.split('.')[0] for n in sys.modules} "
        "- set(sys.stdlib_module_names) - {'xrdclient', '__main__', '_distutils_hack'}))"
    )
    env = {**os.environ, "PYTHONPATH": str(pathlib.Path(xrdclient.__file__).parent.parent)}
    out = subprocess.run(
        [sys.executable, "-S", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert out.stdout.strip() == "[]"


def test_the_working_directory_is_not_needed_to_use_the_package(srv, tmp_path):
    """Nothing resolves a path against ``os.getcwd``; a URL is absolute."""
    here = os.getcwd()
    os.chdir(tmp_path)
    try:
        with FileSystem(srv.url.with_path("/store"), CONFIG) as fs:
            assert fs.read_bytes("f.root") == FILES["/store/f.root"]
    finally:
        os.chdir(here)


def test_the_async_facade_is_one_attribute_away(monkeypatch):
    """``xrdclient.aio`` resolves lazily, and nothing else does."""
    monkeypatch.delattr(xrdclient, "aio", raising=False)  # as it is before anyone asks
    assert xrdclient.aio is importlib.import_module("xrdclient.aio")
    with pytest.raises(AttributeError, match="has no attribute 'nonesuch'"):
        _ = xrdclient.nonesuch
