"""``rmtree`` and ``walk`` against symbolic links, with ``shutil``/``os`` semantics.

A listing with ``kXR_dstat`` describes what a link points at, so a link to a
directory looks exactly like a directory. Descending through one made
``rmtree`` delete whatever the link named, wherever that was, and made
``walk`` go round a cycle for ever. Links are now removed as links and not
walked into, and where the server cannot say which entries are links the
removal asks ``kXR_rmdir`` first, which never follows one.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from typing import Any

import pytest

from xrdclient import FileSystem
from xrdclient.proto import constants as c
from xrdclient.testing import FakeServer
from xrdclient.testing.server import _HANDLERS, _Connection

FILES = {
    "/outside/precious": b"keep me",
    "/tree/sub/a": b"a",
    "/tree/top": b"t",
}


def _stock(srv: FakeServer) -> None:
    """Make the fake look like stock xrootd: no vendor opcodes admitted."""
    srv.config_values["xrdfs.ext"] = ""


def _ignores_dstat(conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
    """A server that answers every listing with plain names."""
    params = params[:15] + bytes([params[15] & ~c.kXR_dstat & 0xFF]) + params[16:]
    return _HANDLERS[c.kXR_dirlist](conn, sid, params, body)


@contextlib.contextmanager
def _tree() -> Iterator[FakeServer]:
    """The tree, a precious file outside it, and a link from one to the other."""
    with FakeServer(files=dict(FILES), dirs=["/tree/empty"]) as server:
        server.links["/tree/link"] = "/outside"
        server.links["/tree/flink"] = "/outside/precious"
        yield server


@pytest.fixture(params=["extension", "stock"])
def srv(request):
    """:func:`_tree` on a server with the vendor extensions, and on a stock one."""
    with _tree() as server:
        if request.param == "stock":
            _stock(server)
        yield server


@pytest.fixture
def fs(srv, config):
    with FileSystem(srv.url, config) as filesystem:
        yield filesystem


def _under(srv: FakeServer, prefix: str) -> list[str]:
    return sorted(p for p in [*srv.files, *srv.dirs, *srv.links] if p.startswith(prefix))


def test_rmtree_removes_a_link_and_leaves_what_it_points_at(fs, srv):
    fs.rmtree("/tree")
    assert _under(srv, "/tree") == []
    assert srv.contents("/outside/precious") == b"keep me"


def test_rmtree_ends_on_a_link_cycle(fs, srv):
    srv.links["/tree/sub/cycle"] = "/tree"
    fs.rmtree("/tree")
    assert _under(srv, "/tree") == []
    assert srv.contents("/outside/precious") == b"keep me"


def test_rmtree_refuses_a_link_as_its_root(fs, srv):
    srv.links["/alias"] = "/outside"
    with pytest.raises(OSError):
        fs.rmtree("/alias")
    fs.rmtree("/alias", ignore_errors=True)
    assert srv.contents("/outside/precious") == b"keep me"
    assert "/alias" in srv.links


def test_rmtree_of_an_empty_directory(fs, srv):
    fs.rmtree("/tree/empty")
    assert "/tree/empty" not in srv.dirs


def test_rmtree_reports_a_link_it_cannot_unlink(fs, srv):
    """Stock xrootd refuses ``kXR_rm`` on a link; that is an error, not a descent."""

    def refuse_links(conn, sid, params, body):
        if body.split(b"\x00", 1)[0].decode() in srv.links:
            return iter([_error(sid)])
        return _HANDLERS[c.kXR_rm](conn, sid, params, body)

    srv.handlers[c.kXR_rm] = refuse_links
    with pytest.raises(PermissionError):
        fs.rmtree("/tree")
    fs.rmtree("/tree", ignore_errors=True)
    assert srv.contents("/outside/precious") == b"keep me"
    assert sorted(srv.links) == ["/tree/flink", "/tree/link"]


def _error(sid: int) -> bytes:
    from xrdclient.testing.server import _error as frame

    return frame(sid, 3010, "operation not permitted")


def test_rmtree_through_a_server_that_ignores_dstat(fs, srv):
    srv.handlers[c.kXR_dirlist] = _ignores_dstat
    fs.rmtree("/tree")
    assert _under(srv, "/tree") == []
    assert srv.contents("/outside/precious") == b"keep me"


def test_rmtree_removes_what_vanished_from_under_a_names_only_listing(config):
    """An entry the stat cannot find is a leaf: removing it is the attempt."""
    with FakeServer(files={"/d/f": b"x"}) as server:
        server.handlers[c.kXR_dirlist] = _ignores_dstat
        server.links["/d/dangling"] = "/nowhere"
        with FileSystem(server.url, config) as filesystem:
            filesystem.rmtree("/d")
        assert _under(server, "/d") == []


def test_rmtree_reports_a_directory_it_cannot_list(fs, srv):
    srv.handlers[c.kXR_dirlist] = lambda conn, sid, params, body: iter([_error(sid)])
    with pytest.raises(PermissionError):
        fs.rmtree("/tree")


def test_a_stock_rmtree_reports_a_root_it_cannot_probe(fs, srv):
    srv.handlers[c.kXR_rmdir] = lambda conn, sid, params, body: iter([_error(sid)])
    with pytest.raises(PermissionError):
        fs.rmtree("/tree")
    fs.rmtree("/tree", ignore_errors=True)


def test_rmtree_does_without_a_server_that_refuses_config_queries(fs, srv):
    srv.handlers[c.kXR_query] = lambda conn, sid, params, body: iter([_error(sid)])
    fs.rmtree("/tree")
    assert _under(srv, "/tree") == []
    assert srv.contents("/outside/precious") == b"keep me"


# ---------------------------------------------------------------------------
# walk
# ---------------------------------------------------------------------------


def _walked(
    fs: FileSystem, top: str = "/tree", **kwargs: Any
) -> dict[str, tuple[list[str], list[str]]]:
    return {root: (sorted(dirs), sorted(files)) for root, dirs, files in fs.walk(top, **kwargs)}


def test_walk_lists_a_link_but_does_not_enter_it(config):
    """Only a server with the extensions can say which entries are links."""
    with _tree() as srv, FileSystem(srv.url, config) as fs:
        walked = _walked(fs)
        assert walked["/tree"] == (["empty", "link", "sub"], ["flink", "top"])
        assert "/tree/link" not in walked


def test_walk_follows_links_when_asked(fs, srv):
    walked = _walked(fs, followlinks=True)
    assert walked["/tree/link"] == ([], ["precious"])


def test_walk_ends_on_a_link_cycle(fs, srv):
    srv.links["/tree/sub/cycle"] = "/tree"
    for followlinks in (False, True):
        walked = _walked(fs, followlinks=followlinks)
        assert "/tree/sub" in walked
        assert len(walked) < 10


def test_walk_descends_through_a_server_that_ignores_dstat(fs, srv):
    srv.handlers[c.kXR_dirlist] = _ignores_dstat
    walked = _walked(fs)
    assert walked["/tree/sub"] == ([], ["a"])
    assert "flink" in walked["/tree"][1]


def test_walk_enters_a_directory_the_caller_named(fs, srv):
    """``topdown`` lets the caller edit ``dirs``; a name it adds has no stat."""
    srv.add_dir("/tree/sub/hidden")
    seen = []
    for root, dirs, _files in fs.walk("/tree"):
        seen.append(root)
        if root == "/tree":
            dirs[:] = ["sub"]
        elif root == "/tree/sub":
            dirs[:] = ["hidden", "absent"]
    assert seen == ["/tree", "/tree/sub", "/tree/sub/hidden"]


# ---------------------------------------------------------------------------
# The genuine daemon
# ---------------------------------------------------------------------------


@pytest.mark.interop
def test_a_real_rmtree_never_deletes_through_a_link(real_server, sandbox):
    from tests.conftest import _REAL_CONFIG

    os.makedirs(f"{sandbox}/outside")
    with open(f"{sandbox}/outside/precious", "w") as fh:
        fh.write("x")
    os.makedirs(f"{sandbox}/tree/sub/deeper")
    open(f"{sandbox}/tree/sub/a", "w").close()
    os.symlink(f"{sandbox}/outside", f"{sandbox}/tree/link")
    os.symlink(f"{sandbox}/tree", f"{sandbox}/tree/sub/cycle")
    with FileSystem(real_server.url, _REAL_CONFIG) as rfs:
        walked = [root for root, _dirs, _files in rfs.walk(f"{sandbox}/tree")]
        assert len(walked) < 10
        with pytest.raises(OSError):
            rfs.rmtree(f"{sandbox}/tree")  # stock xrootd will not unlink a link
        rfs.rmtree(f"{sandbox}/tree", ignore_errors=True)
    assert os.listdir(f"{sandbox}/outside") == ["precious"]
    # What is left is the links, and the directory that holds one.
    assert sorted(os.listdir(f"{sandbox}/tree")) == ["link", "sub"]
    assert os.listdir(f"{sandbox}/tree/sub") == ["cycle"]


# -- dCache has no inodes and no kXR_notempty: the backend the guards were for --


def test_walk_recurses_when_every_directory_reports_the_same_id(monkeypatch, config):
    """dCache answers ``kXR_stat`` for a directory with id ``0`` - the same for
    all of them. The walk's link-cycle guard, keyed on that id, took the first
    child it entered for every later directory and stopped one level in; a
    placeholder id identifies nothing and must not be a cycle key."""
    from xrdclient.testing import server as _server

    real = _server._Connection._stat_line

    def zeroed(self, path: str, *, follow: bool = True) -> bytes:
        line = real(self, path, follow=follow)
        if path in self.s.dirs:  # dCache's constant directory id
            return b"0 " + line.split(b" ", 1)[1]
        return line

    monkeypatch.setattr(_server._Connection, "_stat_line", zeroed)
    files = {"/t/a/b/deep": b"x", "/t/a/sibling": b"y", "/t/top": b"z", "/t/c/leaf": b"w"}
    with FakeServer(files=dict(files)) as srv, FileSystem(srv.url, config) as fs:
        found = sorted(root for root, _dirs, _files in fs.walk("/t"))
        assert found == ["/t", "/t/a", "/t/a/b", "/t/c"]
        # rmtree walks the same way, so it clears the whole tree too.
        fs.rmtree("/t")
        assert not fs.exists("/t")


def test_descent_does_not_collapse_directories_that_share_a_placeholder_id():
    from xrdclient.client._fsbase import _Descent, _Links

    class Unknowable(_Links):
        """A server that cannot say which entries are links."""

        def __init__(self) -> None:
            pass

        def __call__(self, path: str) -> bool | None:
            return None

    descent = _Descent(Unknowable(), followlinks=False)
    # A real device+inode repeats only on a genuine cycle: entered once.
    assert descent.enters("/x", "1234") is True
    assert descent.enters("/y", "1234") is False
    # A placeholder (dCache's 0, an empty S3 id) names nothing: never a repeat.
    assert descent.enters("/a", "0") is True
    assert descent.enters("/b", "0") is True
    assert descent.enters("/c", "") is True
    assert descent.enters("/d", "0:0") is True


def test_not_empty_reads_a_servers_message_when_the_errno_does_not_say():
    import errno

    from xrdclient.client._fsbase import _not_empty
    from xrdclient.errors import IOError_

    # Stock xrootd: kXR_ItExists -> EEXIST (and POSIX ENOTEMPTY).
    assert _not_empty(OSError(errno.EEXIST, "exists")) is True
    assert _not_empty(OSError(errno.ENOTEMPTY, "not empty")) is True
    # dCache: a generic server error carrying EIO, reason in the text alone.
    dcache = OSError(
        errno.EIO, "Failed to delete directory (Directory is not empty: /pnfs/x [666])."
    )
    assert _not_empty(dcache) is True
    # The typed error the client raises keeps the server's words in ``message``.
    typed = IOError_(3012, "Directory is not empty: /pnfs/x")
    assert isinstance(typed, OSError)
    assert _not_empty(typed) is True
    # A real failure that is not about emptiness stays a failure.
    assert _not_empty(OSError(errno.EIO, "The server reported a failure.")) is False
    assert _not_empty(OSError(errno.EIO)) is False


def test_rmtree_clears_a_tree_whose_server_says_not_empty_only_in_words(config):
    """dCache's ``kXR_rmdir`` on a full directory: ``kXR_ServerError`` + text."""
    from xrdclient.testing.server import _error

    def dcache_rmdir(conn, sid, params, body):
        path = body.split(b"\x00", 1)[0].decode()
        if conn._children(path):
            text = f"Failed to delete directory (Directory is not empty: {path} [666])."
            yield _error(sid, 3012, text)  # kXR_ServerError, errno EIO
            return
        yield from _HANDLERS[c.kXR_rmdir](conn, sid, params, body)

    files = {"/t/a/f": b"x", "/t/b/g": b"y"}
    with FakeServer(files=dict(files)) as srv:
        _stock(srv)
        srv.handlers[c.kXR_rmdir] = dcache_rmdir
        with FileSystem(srv.url, config) as fs:
            fs.rmtree("/t")
            assert not fs.exists("/t")
