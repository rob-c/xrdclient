"""``XRootDPath`` - the pathlib-shaped front door."""

from __future__ import annotations

import os

import pytest

from xrdclient.config import Config
from xrdclient.path import XRootDPath
from xrdclient.url import parse

BASE = "root://eos.example.org:1094//store/user/me"


@pytest.fixture
def p():
    return XRootDPath(BASE + "/runs/run1.root")


@pytest.fixture
def remote(server, config):
    """A path rooted at the loopback server."""
    path = XRootDPath(server.url, config)
    try:
        yield path
    finally:
        path.close()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_a_path_is_built_from_a_string_a_url_or_another_path():
    from_string = XRootDPath(BASE)
    from_url = XRootDPath(parse(BASE))
    from_path = XRootDPath(from_string)
    assert from_string == from_url == from_path


def test_a_copy_keeps_the_original_config_unless_told_otherwise():
    config = Config(username="alice")
    original = XRootDPath(BASE, config)
    assert XRootDPath(original)._config is config
    other = Config(username="bob")
    assert XRootDPath(original, other)._config is other


# ---------------------------------------------------------------------------
# Pure-path surface
# ---------------------------------------------------------------------------


def test_name_stem_and_suffix(p):
    assert p.name == "run1.root"
    assert p.stem == "run1"
    assert p.suffix == ".root"


def test_suffixes_lists_every_extension():
    assert XRootDPath(BASE + "/x.tar.gz").suffixes == [".tar", ".gz"]
    assert XRootDPath(BASE + "/plain").suffixes == []


def test_parts_drops_the_empty_segments(p):
    assert p.parts == ("store", "user", "me", "runs", "run1.root")


def test_parent_walks_up_one_level(p):
    assert p.parent.name == "runs"
    assert p.parent.parent.name == "me"


def test_the_parent_of_the_root_is_the_root():
    assert XRootDPath("root://h//").parent == XRootDPath("root://h//")


def test_parents_ends_at_the_root(p):
    names = [q.url.path for q in p.parents]
    assert names[0] == "/store/user/me/runs"
    assert names[-1] == "/"


def test_anchor_is_the_endpoint_root(p):
    assert p.anchor == "root://eos.example.org:1094//"


def test_paths_are_absolute(p):
    assert p.is_absolute()


def test_with_name_stem_and_suffix(p):
    assert p.with_name("other.root").name == "other.root"
    assert p.with_suffix(".txt").name == "run1.txt"
    assert p.with_stem("run2").name == "run2.root"


def test_joinpath_and_the_slash_operator(p):
    assert (p.parent / "run2.root").name == "run2.root"
    assert p.parent.joinpath("a", "b").url.path.endswith("/runs/a/b")


def test_a_string_on_the_left_of_the_slash_rebases_the_path():
    relative = XRootDPath("root://h//data/a.root")
    assert ("/mnt" / relative).url.path == "/mnt/data/a.root"


def test_relative_to_gives_a_plain_string(p):
    assert p.relative_to(XRootDPath(BASE)) == "runs/run1.root"
    assert p.relative_to("/store/user") == "me/runs/run1.root"


def test_str_and_fspath_are_the_url(p):
    assert str(p) == BASE + "/runs/run1.root"
    assert os.fspath(p) == str(p)


def test_repr_quotes_the_url(p):
    assert repr(p) == f"XRootDPath({str(p)!r})"


def test_equality_and_hashing_are_by_endpoint_and_path(p):
    assert p == XRootDPath(str(p))
    assert p != XRootDPath(BASE)
    assert p != str(p)
    assert len({p, XRootDPath(str(p))}) == 1


def test_paths_sort_by_endpoint_then_path():
    a = XRootDPath("root://a//x")
    b = XRootDPath("root://b//a")
    assert sorted([b, a]) == [a, b]


def test_derived_paths_carry_the_config_along():
    config = Config(username="alice")
    child = XRootDPath(BASE, config) / "sub"
    assert child._config is config


# ---------------------------------------------------------------------------
# Concrete surface
# ---------------------------------------------------------------------------


def test_stat_exists_is_dir_and_is_file(remote):
    target = remote / "data/a.root"
    assert target.exists()
    assert target.stat().st_size == 11
    assert target.is_file()
    assert not target.is_dir()
    assert (remote / "data").is_dir()
    assert not (remote / "data/absent").exists()


def test_iterdir_yields_paths(remote):
    entries = sorted((remote / "data").iterdir())
    assert [e.name for e in entries] == ["a.root", "empty"]
    assert all(isinstance(e, XRootDPath) for e in entries)


def test_glob_and_rglob(remote, server):
    server.add_file("/data/empty/deep.root", b"x")
    assert [q.name for q in (remote / "data").glob("*.root")] == ["a.root"]
    assert sorted(q.name for q in remote.rglob("*.root")) == ["a.root", "deep.root"]


def test_walk_yields_paths_at_the_root(remote):
    roots = [str(root.url.path) for root, _, _ in remote.walk()]
    assert roots == ["/", "/data", "/data/empty"]


def test_walk_can_go_bottom_up(remote):
    roots = [root.url.path for root, _, _ in remote.walk(top_down=False)]
    assert roots[0] == "/data/empty"


def test_mkdir_and_rmdir(remote, server):
    (remote / "fresh").mkdir()
    assert "/fresh" in server.dirs
    (remote / "fresh").rmdir()
    assert "/fresh" not in server.dirs


def test_mkdir_with_parents(remote, server):
    (remote / "a/b/c").mkdir(parents=True)
    assert "/a/b/c" in server.dirs


def test_mkdir_exist_ok(remote):
    with pytest.raises(FileExistsError):
        (remote / "data").mkdir()
    (remote / "data").mkdir(exist_ok=True)


def test_unlink_and_its_missing_ok_flag(remote, server):
    server.add_file("/doomed", b"x")
    (remote / "doomed").unlink()
    assert "/doomed" not in server.files
    with pytest.raises(FileNotFoundError):
        (remote / "doomed").unlink()
    (remote / "doomed").unlink(missing_ok=True)


def test_rename_returns_the_new_path(remote, server):
    moved = (remote / "data/a.root").rename("/data/b.root")
    assert isinstance(moved, XRootDPath)
    assert moved.name == "b.root"
    assert server.contents("/data/b.root") == b"hello world"


def test_replace_is_rename(remote, server):
    (remote / "data/a.root").replace(remote / "data/c.root")
    assert "/data/c.root" in server.files


def test_chmod_and_touch(remote, server):
    (remote / "data/a.root").chmod(0o600)
    (remote / "made").touch()
    assert "/made" in server.files


def test_read_and_write_bytes(remote, server):
    assert (remote / "data/a.root").read_bytes() == b"hello world"
    assert (remote / "data/new.bin").write_bytes(b"\x01\x02") == 2
    assert server.contents("/data/new.bin") == b"\x01\x02"


def test_read_and_write_text(remote):
    target = remote / "data/note.txt"
    target.write_text("héllo")
    assert target.read_text() == "héllo"


def test_open_returns_a_file_object(remote):
    with (remote / "data/a.root").open("rb") as fh:
        assert fh.read(5) == b"hello"


def test_checksum_and_locate(remote, server):
    target = remote / "data/a.root"
    assert target.checksum().algorithm == "adler32"
    assert target.locate()[0].address == f"{server.address[0]}:{server.address[1]}"


def test_the_backing_filesystem_is_created_once_and_released(remote):
    first = remote.fs
    assert remote.fs is first
    remote.close()
    assert remote._fs is None
    assert remote.fs is not first


def test_the_filesystem_is_rooted_at_the_endpoint_not_the_path(remote):
    assert (remote / "data/a.root").fs.url.path == "/"


def test_derived_paths_share_one_connection(remote):
    """A traversal must not open a socket per entry."""
    child = remote / "data" / "a.root"
    assert child.fs is remote.fs
    assert remote.parent.fs is remote.fs
    assert all(entry.fs is remote.fs for entry in (remote / "data").iterdir())


def test_a_copy_shares_the_connection_but_a_reconfigured_one_does_not(remote):
    assert XRootDPath(remote).fs is remote.fs
    assert XRootDPath(remote, Config(username="other")).fs is not remote.fs


def test_a_path_is_a_context_manager(server, config):
    with XRootDPath(server.url, config) as path:
        assert (path / "data/a.root").read_bytes() == b"hello world"
    assert path._fs is None


def test_the_package_spells_it_both_ways():
    """``xrdclient.Path`` is the name people reach for; it is the same class."""
    import xrdclient

    assert xrdclient.Path is xrdclient.XRootDPath is XRootDPath
    assert "Path" in xrdclient.__all__ and "XRootDPath" in xrdclient.__all__
