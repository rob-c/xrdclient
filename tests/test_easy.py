"""The one-line verbs, and what things look like when they are printed.

:mod:`xrdclient.easy` is the whole library reduced to "here is a URL, answer the
question". These tests are the receipts for each verb against a running
server, plus the small courtesies - a stat that prints like ``ls -l``, a size
a person can read - that make the answers legible when they arrive.
"""

from __future__ import annotations

import datetime

import pytest

import xrdclient
from xrdclient.flags import StatInfoFlags
from xrdclient.testing import FakeServer
from xrdclient.types import DirEntry, StatInfo, human_bytes

# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------


def test_ls_gives_paths_in_order(server, config):
    server.files["/data/b.root"] = b"two"
    names = [path.name for path in xrdclient.ls(server.url.with_path("/data"), config=config)]
    assert names == sorted(names)
    assert {"a.root", "b.root"} <= set(names)


def test_ls_hands_back_paths_that_still_work(server, config):
    first = xrdclient.ls(server.url.with_path("/data"), config=config)[0]
    with first:
        assert first.stat().st_size >= 0


def test_a_connection_nobody_closed_goes_back_to_the_pool(server, config):
    """Nobody at this level should have to remember to close anything."""
    import gc

    from xrdclient.session import SESSIONS

    path = xrdclient.Path(server.url.with_path("/data/a.root"), config)
    assert path.read_bytes() == b"hello world"
    del path
    gc.collect()
    assert len(SESSIONS) == 1


def test_glob_matches_across_the_listing(server, config):
    found = xrdclient.glob(server.url.with_path("/data/*.root"), config=config)
    assert [path.name for path in found] == ["a.root"]


def test_stat_exists_and_size_are_one_call_each(server, config):
    url = server.url.with_path("/data/a.root")
    assert xrdclient.exists(url, config=config)
    assert xrdclient.size(url, config=config) == len(b"hello world")
    assert xrdclient.stat(url, config=config).st_size == len(b"hello world")


def test_a_file_that_is_not_there_does_not_exist(server, config):
    assert not xrdclient.exists(server.url.with_path("/data/nowhere.root"), config=config)


def test_checksum_asks_the_server_for_the_digest(server, config):
    digest = xrdclient.checksum(server.url.with_path("/data/a.root"), "adler32", config=config)
    assert digest.algorithm == "adler32"


def test_is_online_is_true_for_a_file_on_disk(server, config):
    assert xrdclient.is_online(server.url.with_path("/data/a.root"), config=config)


def test_stage_returns_the_request_it_was_given(server, config):
    handle = xrdclient.stage(server.url.with_path("/data/a.root"), config=config)
    assert server.prepared[handle] == ["/data/a.root"]


def test_stage_takes_several_files_at_once(server, config):
    server.files["/data/c.root"] = b"three"
    urls = [server.url.with_path(f"/data/{name}") for name in ("a.root", "c.root")]
    handle = xrdclient.stage(urls, priority=2, config=config)
    assert server.prepared[handle] == ["/data/a.root", "/data/c.root"]


def test_staging_nothing_is_a_mistake_worth_saying(config):
    with pytest.raises(ValueError, match="needs a file to stage"):
        xrdclient.stage([], config=config)


# ---------------------------------------------------------------------------
# Reading and writing
# ---------------------------------------------------------------------------


def test_read_bytes_and_read_text(server, config):
    url = server.url.with_path("/data/a.root")
    assert xrdclient.read_bytes(url, config=config) == b"hello world"
    assert xrdclient.read_text(url, config=config) == "hello world"


def test_write_bytes_and_write_text(server, config):
    binary = server.url.with_path("/data/new/one.bin")
    assert xrdclient.write_bytes(binary, b"\x00\x01", config=config) == 2
    assert xrdclient.read_bytes(binary, config=config) == b"\x00\x01"

    text = server.url.with_path("/data/new/two.txt")
    # Characters written, as ``pathlib.Path.write_text`` counts them.
    assert xrdclient.write_text(text, "héllo", config=config) == 5
    assert xrdclient.read_text(text, config=config) == "héllo"


# ---------------------------------------------------------------------------
# Changing things
# ---------------------------------------------------------------------------


def test_mkdir_makes_the_parents_and_forgives_the_second_call(server, config):
    url = server.url.with_path("/data/deep/deeper")
    xrdclient.mkdir(url, "rwxr-x---", config=config)
    xrdclient.mkdir(url, config=config)
    assert xrdclient.exists(url, config=config)


def test_remove_takes_a_file(server, config):
    url = server.url.with_path("/data/gone.txt")
    xrdclient.write_text(url, "x", config=config)
    xrdclient.remove(url, config=config)
    assert not xrdclient.exists(url, config=config)


def test_remove_forgives_what_was_never_there(server, config):
    xrdclient.remove(server.url.with_path("/data/never.txt"), missing_ok=True, config=config)


def test_remove_takes_an_empty_directory(server, config):
    xrdclient.remove(server.url.with_path("/data/empty"), config=config)
    assert not xrdclient.exists(server.url.with_path("/data/empty"), config=config)


def test_removing_a_full_directory_has_to_be_asked_for(server, config):
    url = server.url.with_path("/data/tree")
    xrdclient.write_text(server.url.with_path("/data/tree/leaf.txt"), "x", config=config)
    with pytest.raises(xrdclient.XRootDError):
        xrdclient.remove(url, config=config)
    xrdclient.remove(url, recursive=True, config=config)
    assert not xrdclient.exists(url, config=config)


def test_move_on_one_endpoint_is_a_rename(server, config):
    source = server.url.with_path("/data/here.txt")
    target = server.url.with_path("/data/there.txt")
    xrdclient.write_text(source, "moved", config=config)
    xrdclient.move(source, target, config=config)
    assert not xrdclient.exists(source, config=config)
    assert xrdclient.read_text(target, config=config) == "moved"


def test_move_between_endpoints_copies_then_deletes(server, config):
    with FakeServer() as other:
        source = server.url.with_path("/data/a.root")
        target = other.url.with_path("/data/a.root")
        xrdclient.move(source, target, config=config)
        assert xrdclient.read_bytes(target, config=config) == b"hello world"
        assert not xrdclient.exists(source, config=config)


# ---------------------------------------------------------------------------
# Legibility
# ---------------------------------------------------------------------------


def test_human_bytes_reads_the_way_ls_h_does():
    assert human_bytes(0) == "0 B"
    assert human_bytes(512) == "512 B"
    assert human_bytes(1536) == "1.5 KiB"
    assert human_bytes(3 * 1024**3) == "3.0 GiB"


def test_human_bytes_keeps_its_units_past_the_end_of_the_table():
    """An exabyte of tape is a number, not a unit nobody has heard of."""
    assert human_bytes(5 * 1024**6).endswith(" PiB")


def test_a_stat_prints_the_line_ls_would_have():
    info = StatInfo(
        st_size=1536,
        flags=StatInfoFlags.IS_READABLE,
        st_mtime=1_700_000_000,
        path="/store/f.root",
    )
    assert str(info) == "-r--r--r--    1.5 KiB  2023-11-14 22:13  /store/f.root"


def test_a_stat_with_no_time_says_so_rather_than_1970():
    assert str(StatInfo(flags=StatInfoFlags.IS_DIR, path="/store")).endswith("-  /store")


def test_a_stat_knows_when_and_in_which_zone(server, config):
    when = xrdclient.stat(server.url.with_path("/data/a.root"), config=config).modified
    assert when.tzinfo is datetime.timezone.utc
    assert when.year >= 2020


def test_a_listing_entry_prints_where_it_is():
    assert str(DirEntry(name="f.root", parent="/store")) == "/store/f.root"


# ---------------------------------------------------------------------------
# Local paths
# ---------------------------------------------------------------------------
#
# A plain path names a file on this machine. Before these, every verb handed
# it to a FileSystem for a host called "" on port 0, so a local move either
# failed outright or - worse - copied the file and then failed to remove the
# source with an error about connecting to ":0".


def test_move_between_two_local_paths_is_a_rename(tmp_path):
    source, target = tmp_path / "a.txt", tmp_path / "b.txt"
    source.write_text("moved")
    xrdclient.move(str(source), str(target))
    assert (source.exists(), target.read_text()) == (False, "moved")


def test_move_between_local_filesystems_copies_then_removes(tmp_path, monkeypatch):
    """``rename`` cannot cross a mount point; that becomes a copy and a delete."""
    import errno
    import os

    source, target = tmp_path / "a.txt", tmp_path / "b.txt"
    source.write_text("far")

    def cross_device(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link", src)

    monkeypatch.setattr(os, "replace", cross_device)
    xrdclient.move(str(source), str(target))
    assert (source.exists(), target.read_text()) == (False, "far")


def test_move_from_a_local_path_to_a_server_removes_the_local_file(server, config, tmp_path):
    source = tmp_path / "up.txt"
    source.write_bytes(b"upload")
    target = server.url.with_path("/data/up.txt")
    xrdclient.move(str(source), target, config=config)
    assert server.contents("/data/up.txt") == b"upload"
    assert not source.exists()


def test_move_from_a_server_to_a_local_path_removes_the_remote_file(server, config, tmp_path):
    target = tmp_path / "down.root"
    xrdclient.move(server.url.with_path("/data/a.root"), str(target), config=config)
    assert target.read_bytes() == b"hello world"
    assert "/data/a.root" not in server.files


def test_a_move_whose_copy_fails_verification_keeps_the_source(server, config, tmp_path):
    """The source is the only copy left if the new one is wrong."""
    from xrdclient.errors import ChecksumMismatchError
    from xrdclient.proto import constants as c
    from xrdclient.testing import frame

    source = tmp_path / "precious.bin"
    source.write_bytes(b"precious")
    server.handlers[c.kXR_query] = lambda conn, sid, params, body: iter(
        [frame(sid, c.kXR_ok, b"adler32 00000001\x00")]
    )
    with pytest.raises(ChecksumMismatchError):
        xrdclient.move(str(source), server.url.with_path("/data/p.bin"), config=config)
    assert source.read_bytes() == b"precious"


def test_the_asking_verbs_answer_for_a_local_path(tmp_path):
    here = tmp_path / "f.bin"
    here.write_bytes(b"12345")
    assert xrdclient.exists(str(here)) and not xrdclient.exists(str(tmp_path / "no"))
    assert xrdclient.size(str(here)) == 5
    info = xrdclient.stat(str(here))
    assert info.is_file() and info.is_readable() and not info.is_dir()
    assert info.st_mtime == int(here.stat().st_mtime)
    assert xrdclient.stat(str(tmp_path)).is_dir()
    assert xrdclient.is_online(str(here))
    assert xrdclient.checksum(str(here), "adler32").value == "02f80100"


def test_the_changing_verbs_work_on_a_local_path(tmp_path):
    deep = tmp_path / "x" / "y"
    xrdclient.mkdir(str(deep), "rwxr-x---")
    xrdclient.mkdir(str(deep))  # forgiven the second time, as remotely
    assert deep.is_dir() and (deep.stat().st_mode & 0o777) == 0o750

    leaf = deep / "sub" / "leaf.txt"
    assert xrdclient.write_text(str(leaf), "héllo") == 5
    assert xrdclient.read_text(str(leaf)) == "héllo"
    assert xrdclient.write_bytes(str(leaf), b"\x00") == 1
    assert xrdclient.read_bytes(str(leaf)) == b"\x00"

    with pytest.raises(OSError):
        xrdclient.remove(str(deep))  # not empty: has to be asked for
    xrdclient.remove(str(leaf))
    xrdclient.remove(str(leaf), missing_ok=True)
    xrdclient.remove(str(tmp_path / "x"), recursive=True)
    assert not (tmp_path / "x").exists()


def test_listing_a_local_directory_says_where_to_look_instead(tmp_path):
    """The answer is a list of remote paths; a local one could not be used."""
    with pytest.raises(ValueError, match="pathlib"):
        xrdclient.ls(str(tmp_path))
    with pytest.raises(ValueError, match="pathlib"):
        xrdclient.glob(str(tmp_path / "*"))
    with pytest.raises(ValueError, match="local"):
        xrdclient.stage(str(tmp_path / "f"))
