"""Opaque-data conformance: the token has to reach every single request.

CGI is not decoration in XRootD - it is where the authorisation token lives,
and where a redirector puts the state that makes a redirect stick. An
operation that drops it does not fail cleanly; it fails as "permission
denied" on the one request out of forty that lost it, usually the recursive
one, usually in production.

So: a filesystem whose URL carries a token, and an assertion that every
request the client sends carries it too - both halves of a rename, every
level of a ``makedirs``, every child of an ``rmtree``.
"""

from __future__ import annotations

import pytest

from xrd.client.filesystem import _cgi, _split_cgi
from xrd.config import Config
from xrd.proto import constants as c
from xrd.testing import FakeServer

TOKEN = "authz=TOKEN"

_CONFIG = Config(username="tester", auth_order=("host",), require_tls=False)


@pytest.fixture
def srv():
    files = {"/store/f.root": b"payload", "/store/sub/g.root": b"more"}
    with FakeServer(files=files, dirs=["/store/empty"]) as server:
        yield server


@pytest.fixture
def tokened(srv):
    """A filesystem whose own URL carries a token, as a signed URL does."""
    from xrd import FileSystem

    fs = FileSystem(f"{srv.url.with_path('/store')}?{TOKEN}", _CONFIG)
    try:
        srv.arguments.clear()
        yield fs
    finally:
        fs.close()


def arguments(srv, opcode=None):
    return [arg for op, arg in srv.arguments if opcode in (None, op)]


def carried(srv):
    """Every request that named a path carried the token.

    Not every request names one - the sub-stream probe is a ``kXR_Qconfig``
    for ``brix.substreams``, and CGI has nowhere to live on a config key.
    """
    named = [arg for arg in arguments(srv) if arg.startswith("/")]
    assert named, "no requests named a path"
    return all(TOKEN in arg for arg in named)


# ---------------------------------------------------------------------------
# The suffix itself
# ---------------------------------------------------------------------------


def test_a_path_with_no_opaque_data_stays_that_way():
    assert _cgi("", {}) == ""
    assert _split_cgi("/store/f") == ("/store/f", "")


def test_the_inherited_token_is_appended():
    assert _cgi("", {"authz": "T"}) == "?authz=T"
    assert _cgi("xrd.k=1", {"authz": "T"}) == "?xrd.k=1&authz=T"


def test_what_the_caller_spelled_out_wins():
    assert _cgi("authz=MINE", {"authz": "T"}) == "?authz=MINE"


def test_the_suffix_survives_a_split():
    assert _split_cgi("/store/f?authz=T") == ("/store/f", "?authz=T")


# ---------------------------------------------------------------------------
# Every namespace request
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda fs: fs.stat("f.root"), id="stat"),
        pytest.param(lambda fs: fs.statx(["f.root", "sub"]), id="statx"),
        pytest.param(lambda fs: fs.statvfs("."), id="statvfs"),
        pytest.param(lambda fs: fs.listdir("."), id="listdir"),
        pytest.param(lambda fs: fs.scandir("."), id="scandir"),
        pytest.param(lambda fs: fs.exists("f.root"), id="exists"),
        pytest.param(lambda fs: fs.mkdir("fresh"), id="mkdir"),
        pytest.param(lambda fs: fs.makedirs("a/b/c"), id="makedirs"),
        pytest.param(lambda fs: fs.rmdir("empty"), id="rmdir"),
        pytest.param(lambda fs: fs.remove("f.root"), id="remove"),
        pytest.param(lambda fs: fs.rename("f.root", "moved.root"), id="rename"),
        pytest.param(lambda fs: fs.chmod("f.root", 0o640), id="chmod"),
        pytest.param(lambda fs: fs.truncate("f.root", 3), id="truncate"),
        pytest.param(lambda fs: fs.touch("touched.root"), id="touch"),
        pytest.param(lambda fs: fs.checksum("f.root"), id="checksum"),
        pytest.param(lambda fs: fs.locate("f.root"), id="locate"),
        pytest.param(lambda fs: fs.deep_locate("f.root"), id="deep_locate"),
        pytest.param(lambda fs: fs.prepare(["f.root"]), id="prepare"),
        pytest.param(lambda fs: fs.setxattr("f.root", "user.k", b"v"), id="setxattr"),
        pytest.param(lambda fs: fs.listxattr("f.root"), id="listxattr"),
        pytest.param(lambda fs: fs.read_bytes("f.root"), id="read_bytes"),
        pytest.param(lambda fs: fs.write_bytes("w.root", b"x"), id="write_bytes"),
        pytest.param(lambda fs: list(fs.walk(".")), id="walk"),
        pytest.param(lambda fs: fs.rmtree("sub"), id="rmtree"),
    ],
)
def test_this_operation_carries_the_opaque_data(tokened, srv, call):
    call(tokened)
    assert carried(srv), srv.arguments


def test_both_halves_of_a_rename_keep_their_own(tokened, srv):
    tokened.rename("f.root", "moved.root")
    source, destination = arguments(srv, c.kXR_mv)[0].split()
    assert source.endswith(f"?{TOKEN}")
    assert destination.endswith(f"?{TOKEN}")


def test_a_rename_lets_each_half_carry_something_different(tokened, srv):
    tokened.rename("f.root?xrd.a=1", "moved.root?xrd.b=2")
    source, destination = arguments(srv, c.kXR_mv)[0].split()
    assert "xrd.a=1" in source and "xrd.b=2" not in source
    assert "xrd.b=2" in destination and "xrd.a=1" not in destination


def test_every_level_of_a_makedirs_is_asked_for_with_the_token(tokened, srv):
    tokened.makedirs("deep/deeper/deepest")
    assert arguments(srv, c.kXR_mkdir) == [f"/store/deep/deeper/deepest?{TOKEN}"]
    assert carried(srv)


def test_every_child_of_an_rmtree_carries_the_token(tokened, srv):
    tokened.makedirs("tree/inner")
    tokened.write_bytes("tree/one.root", b"1")
    tokened.write_bytes("tree/inner/two.root", b"2")
    srv.arguments.clear()
    tokened.rmtree("tree")

    removed = arguments(srv, c.kXR_rm) + arguments(srv, c.kXR_rmdir)
    assert len(removed) == 4
    assert all(arg.endswith(f"?{TOKEN}") for arg in removed)


def test_an_open_carries_the_token_and_only_once(tokened, srv):
    tokened.read_bytes("f.root")
    assert srv.opened == [f"/store/f.root?{TOKEN}"]


def test_a_caller_can_add_to_the_inherited_token(tokened, srv):
    tokened.stat("f.root?xrd.k=1")
    assert arguments(srv, c.kXR_stat) == [f"/store/f.root?xrd.k=1&{TOKEN}"]


def test_a_caller_can_override_the_inherited_token(tokened, srv):
    tokened.stat("f.root?authz=OTHER")
    assert arguments(srv, c.kXR_stat) == ["/store/f.root?authz=OTHER"]


def test_the_token_survives_path_normalisation(tokened, srv):
    tokened.stat("sub/../f.root?xrd.k=1")
    assert arguments(srv, c.kXR_stat) == [f"/store/f.root?xrd.k=1&{TOKEN}"]


def test_walk_yields_paths_not_paths_with_a_query_on_the_end(tokened, srv):
    """What comes back is a name to use, so the token stays on the wire."""
    roots = [root for root, _, _ in tokened.walk(".")]
    assert roots == ["/store", "/store/empty", "/store/sub"]
    assert carried(srv)


def test_glob_lists_with_the_token_and_yields_without_it(tokened, srv):
    assert sorted(tokened.glob("/store/*.root")) == ["/store/f.root"]
    assert carried(srv)


def test_a_raw_query_is_left_exactly_as_it_was_written(tokened, srv):
    """``query`` is the escape hatch, so its argument is not touched.

    Half the query codes take a path and half take a name - ``kXR_Qconfig``
    wants ``"version"``, not ``"/store/version?authz=..."`` - so guessing
    would be worse than the caller saying what they mean.
    """
    from xrd.flags import QueryCode

    assert b"v5.6.0" in tokened.query(QueryCode.CONFIG, "version")
    assert arguments(srv, c.kXR_query) == ["version"]


def test_a_filesystem_without_a_token_sends_no_query_at_all(srv):
    from xrd import FileSystem

    with FileSystem(srv.url, _CONFIG) as fs:
        srv.arguments.clear()
        fs.stat("/store/f.root")
        fs.listdir("/store")
    assert arguments(srv) == ["/store/f.root", "/store"]


# ---------------------------------------------------------------------------
# The path object and the handle
# ---------------------------------------------------------------------------


def test_a_path_carries_its_query_into_the_open(srv):
    import xrd

    path = xrd.Path(f"{srv.url.with_path('/store/f.root')}?{TOKEN}", config=_CONFIG)
    try:
        assert path.read_bytes() == b"payload"
    finally:
        path.close()
    assert srv.opened == [f"/store/f.root?{TOKEN}"]


def test_a_child_path_inherits_the_query(srv):
    import xrd

    root = xrd.Path(f"{srv.url.with_path('/store')}?{TOKEN}", config=_CONFIG)
    try:
        assert (root / "f.root").read_bytes() == b"payload"
    finally:
        root.close()
    assert srv.opened == [f"/store/f.root?{TOKEN}"]


def test_xrd_open_carries_the_query(srv):
    import xrd

    with xrd.open(f"{srv.url.with_path('/store/f.root')}?{TOKEN}", "rb", config=_CONFIG) as fh:
        assert fh.read() == b"payload"
    assert srv.opened == [f"/store/f.root?{TOKEN}"]


def test_a_copy_carries_the_query_of_both_ends(srv, tmp_path):
    import xrd

    local = tmp_path / "out.root"
    xrd.copy(f"{srv.url.with_path('/store/f.root')}?{TOKEN}", local, config=_CONFIG, verify=False)
    assert local.read_bytes() == b"payload"
    assert srv.opened == [f"/store/f.root?{TOKEN}"]


def test_the_token_is_not_in_the_repr_of_a_url(srv):
    """It is a credential, and a repr ends up in a traceback."""
    import xrd

    url = xrd.parse(f"{srv.url.with_path('/store/f.root')}?{TOKEN}")
    assert "TOKEN" not in repr(url)
    assert "redacted" in repr(url)
