"""``rmtree`` and ``walk`` against symbolic links, with ``shutil``/``os`` semantics.

A listing with ``kXR_dstat`` describes what a link points at, so a link to a
directory looks exactly like a directory. Descending through one made
``rmtree`` delete whatever the link named, wherever that was, and made
``walk`` go round a cycle for ever. Links are now removed as links and not
walked into, and where the server cannot say which entries are links the
removal asks ``kXR_rmdir`` first, which never follows one.
"""

from __future__ import annotations

import os

import pytest

from xrdclient import FileSystem
from xrdclient.proto import constants as c
from xrdclient.testing import FakeServer
from xrdclient.testing.server import _HANDLERS

FILES = {
    "/outside/precious": b"keep me",
    "/tree/sub/a": b"a",
    "/tree/top": b"t",
}


def _stock(srv: FakeServer) -> None:
    """Make the fake look like stock xrootd: no vendor opcodes admitted."""
    srv.config_values["xrdfs.ext"] = ""


def _ignores_dstat(conn, sid, params, body):
    """A server that answers every listing with plain names."""
    params = params[:15] + bytes([params[15] & ~c.kXR_dstat & 0xFF]) + params[16:]
    return _HANDLERS[c.kXR_dirlist](conn, sid, params, body)


@pytest.fixture(params=["extension", "stock"])
def srv(request):
    """The tree, a precious file outside it, and a link from one to the other."""
    with FakeServer(files=dict(FILES), dirs=["/tree/empty"]) as server:
        server.links["/tree/link"] = "/outside"
        server.links["/tree/flink"] = "/outside/precious"
        if request.param == "stock":
            _stock(server)
        yield server


@pytest.fixture
def fs(srv, config):
    with FileSystem(srv.url, config) as filesystem:
        yield filesystem


def _under(srv, prefix):
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


def _error(sid):
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


def _walked(fs, top="/tree", **kwargs):
    return {root: (sorted(dirs), sorted(files)) for root, dirs, files in fs.walk(top, **kwargs)}


def test_walk_lists_a_link_but_does_not_enter_it(fs, srv):
    if srv.config_values["xrdfs.ext"] == "":
        pytest.skip("a stock server cannot say which entries are links")
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
    from conftest import _REAL_CONFIG

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
