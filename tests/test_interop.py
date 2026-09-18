"""The client against a genuine ``xrootd`` daemon.

Everywhere else the suite talks to :class:`~xrdclient.testing.FakeServer`, which was
written from the same reading of the specification as the client - so a
misreading shared by both would pass every test. This file is the control: an
unprivileged ``xrootd`` on loopback, and where it matters the stock ``xrdcp``
and ``xrdfs`` binaries reading back what we wrote.

It has already earned its keep. ``kXR_writev`` counted its data in ``dlen``,
which the fake accepted and the real server refused with
``kXR_ArgInvalid: Write vector is invalid``; the fake now checks the same
thing, and :func:`test_writev_is_framed_the_way_a_real_server_wants_it` is why.
"""

from __future__ import annotations

import os
import subprocess
import zlib
from pathlib import Path

import pytest

import xrdclient
from conftest import _REAL_CONFIG
from xrdclient.cli import cp as cli_cp
from xrdclient.cli import fs as cli_fs
from xrdclient.client.file import File
from xrdclient.errors import ExistsError, NotFoundError, UnsupportedError
from xrdclient.flags import OpenFlags, StatInfoFlags

pytestmark = pytest.mark.interop

BLOB = bytes(range(256)) * 64  # 16 KiB, and every byte value present


@pytest.fixture
def rfs(real_server):
    """A :class:`~xrdclient.FileSystem` on the real daemon."""
    with xrdclient.FileSystem(real_server.url, _REAL_CONFIG) as filesystem:
        yield filesystem


@pytest.fixture
def blob(rfs, sandbox):
    """``sandbox/blob.root``, holding :data:`BLOB`."""
    path = f"{sandbox}/blob.root"
    rfs.write_bytes(path, BLOB)
    return path


def url_for(server, path: str) -> str:
    """``root://host:port//abs/path`` - the doubled slash is not a typo.

    One slash means a path relative to the server's export, which stock
    ``xrdcp`` refuses outright ("Opening relative path ... is disallowed").
    """
    return f"{server.url.rstrip('/')}//{path.lstrip('/')}"


# ---------------------------------------------------------------------------
# The whole point of the file
# ---------------------------------------------------------------------------


def test_writev_is_framed_the_way_a_real_server_wants_it(rfs, sandbox, real_server):
    """A regression test with a server on the other end of it.

    ``dlen`` must cover the ``write_list`` alone. When it covered the data as
    well, this is the request that came back ``kXR_ArgInvalid``.
    """
    path = f"{sandbox}/v.root"
    rfs.write_bytes(path, b"\x00" * 32)
    handle = File(rfs.url.with_path(path), _REAL_CONFIG)
    handle.open(OpenFlags.UPDATE)
    with handle:
        assert handle.writev([(0, b"HELLO"), (10, b"WORLD"), (20, b"!" * 12)]) == 22
    assert rfs.read_bytes(path) == b"HELLO" + b"\x00" * 5 + b"WORLD" + b"\x00" * 5 + b"!" * 12
    assert Path(path).read_bytes() == rfs.read_bytes(path)  # the daemon exports a real file


def test_a_writev_of_many_small_pieces_lands_in_order(rfs, sandbox):
    path = f"{sandbox}/many.root"
    rfs.write_bytes(path, b"")
    chunks = [(i * 4, bytes([i]) * 4) for i in range(64)]
    handle = File(rfs.url.with_path(path), _REAL_CONFIG)
    handle.open(OpenFlags.UPDATE)
    with handle:
        assert handle.writev(chunks) == 256
    assert rfs.read_bytes(path) == b"".join(data for _, data in chunks)


# ---------------------------------------------------------------------------
# Namespace
# ---------------------------------------------------------------------------


def test_the_everyday_round_trip(rfs, sandbox):
    path = f"{sandbox}/a.root"
    assert not rfs.exists(path)
    assert rfs.write_bytes(path, b"hello world") == 11
    assert rfs.read_bytes(path) == b"hello world"
    assert rfs.read_text(path) == "hello world"
    assert rfs.getsize(path) == 11
    assert rfs.isfile(path) and not rfs.isdir(path)
    assert rfs.isdir(sandbox)
    rfs.remove(path)
    assert not rfs.exists(path)


def test_stat_agrees_with_the_operating_system(rfs, blob):
    info = rfs.stat(blob)
    local = os.stat(blob)
    assert info.st_size == local.st_size == len(BLOB)
    assert abs(info.st_mtime - local.st_mtime) < 2
    assert not info.flags & StatInfoFlags.IS_DIR
    assert info.st_mode  # xrootd reports the permission bits it can see


def test_directories_are_made_listed_walked_and_removed(rfs, sandbox):
    rfs.mkdir(f"{sandbox}/x/y/z", parents=True)
    for name in ("a.root", "b.dat"):
        rfs.write_bytes(f"{sandbox}/{name}", b"x")
    rfs.write_bytes(f"{sandbox}/x/y/z/deep.root", b"y")

    _assert_listing(rfs, sandbox)
    _assert_walk(rfs, sandbox)
    rfs.rmtree(sandbox)
    assert not rfs.exists(sandbox)


def _assert_listing(rfs, sandbox):
    assert sorted(rfs.listdir(sandbox)) == ["a.root", "b.dat", "x"]
    entries = {e.name: e for e in rfs.scandir(sandbox)}
    assert entries["x"].is_dir() and entries["a.root"].is_file()
    assert entries["a.root"].stat.st_size == 1


def _assert_walk(rfs, sandbox):
    found = {root: sorted(files) for root, _dirs, files in rfs.walk(sandbox)}
    assert found[sandbox] == ["a.root", "b.dat"]
    assert found[f"{sandbox}/x/y/z"] == ["deep.root"]
    assert sorted(rfs.glob(f"{sandbox}/*.root")) == [f"{sandbox}/a.root"]
    assert sorted(rfs.glob(f"{sandbox}/**/*.root")) == [
        f"{sandbox}/a.root",
        f"{sandbox}/x/y/z/deep.root",
    ]


def test_rename_and_chmod_do_what_they_say(rfs, sandbox, blob):
    moved = f"{sandbox}/moved.root"
    rfs.rename(blob, moved)
    assert not rfs.exists(blob) and rfs.getsize(moved) == len(BLOB)
    rfs.chmod(moved, 0o640)
    assert os.stat(moved).st_mode & 0o777 == 0o640


def test_truncate_and_touch_reach_the_filesystem(rfs, sandbox, blob):
    rfs.truncate(blob, 100)
    assert os.stat(blob).st_size == 100
    fresh = f"{sandbox}/touched.root"
    rfs.touch(fresh)
    assert rfs.exists(fresh) and rfs.getsize(fresh) == 0


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def test_ranged_vector_and_paged_reads_all_agree(rfs, blob):
    with File(rfs.url.with_path(blob), _REAL_CONFIG) as handle:
        assert handle.size == len(BLOB)
        assert handle.read(64, 1024) == BLOB[1024:1088]
        assert handle.readv([(0, 16), (4096, 16), (8192, 16)]) == [
            BLOB[0:16],
            BLOB[4096:4112],
            BLOB[8192:8208],
        ]
        paged = handle.pgread(8192, 0)
        assert paged.data == BLOB[:8192]
        assert paged.corrupt_pages == ()  # the CRCs are the server's own
        assert handle.read() == BLOB


def test_a_write_handle_updates_pages_and_syncs(rfs, sandbox):
    path = f"{sandbox}/w.root"
    handle = File(rfs.url.with_path(path), _REAL_CONFIG)
    handle.open(OpenFlags.NEW | OpenFlags.UPDATE)
    with handle:
        assert handle.write(BLOB[:4096], 0) == 4096
        assert handle.pgwrite(b"page", 0) == 4
        handle.sync()
        assert handle.stat(refresh=True).st_size == 4096
        handle.truncate(2048)
        assert handle.stat(refresh=True).st_size == 2048
    assert rfs.read_bytes(path) == b"page" + BLOB[4:2048]


def test_the_file_object_behaves_like_a_python_file(rfs, blob, sandbox):
    with xrdclient.open(rfs.url.with_path(blob), "rb", config=_REAL_CONFIG) as fh:
        assert fh.readable() and fh.seekable() and not fh.writable()
        assert fh.read(16) == BLOB[:16]
        assert fh.seek(-16, os.SEEK_END) == len(BLOB) - 16
        assert fh.read() == BLOB[-16:]

    text = f"{sandbox}/t.txt"
    with xrdclient.open(rfs.url.with_path(text), "w", config=_REAL_CONFIG) as fh:
        fh.write("one\ntwo\n")
    with xrdclient.open(rfs.url.with_path(text), "r", config=_REAL_CONFIG) as fh:
        assert list(fh) == ["one\n", "two\n"]


def test_the_path_api_works_against_the_real_thing(real_server, sandbox):
    path = xrdclient.XRootDPath(url_for(real_server, f"{sandbox}/p.root"), config=_REAL_CONFIG)
    try:
        path.write_text("contents")
        assert path.exists() and path.is_file()
        assert path.read_text() == "contents"
        assert path.stat().st_size == 8
        assert path.name == "p.root" and path.suffix == ".root"
        assert [entry.name for entry in path.parent.iterdir()] == ["p.root"]
        path.unlink()
        assert not path.exists()
    finally:
        path.close()


# ---------------------------------------------------------------------------
# Checksums, queries, copying
# ---------------------------------------------------------------------------


def test_the_server_checksum_matches_the_one_we_compute(rfs, blob):
    info = rfs.checksum(blob)
    assert info.algorithm == "adler32"
    assert int(info.value, 16) == zlib.adler32(BLOB)
    # xrootd's "crc32" is its own implementation, not zlib's, so the only
    # honest assertion is that it is a stable digest of the contents.
    crc = rfs.checksum(blob, "crc32")
    assert crc.algorithm == "crc32" and len(crc.value) == 8
    other = f"{blob}.other"
    rfs.write_bytes(other, BLOB + b"!")
    assert rfs.checksum(other, "crc32").value != crc.value
    assert rfs.checksum(other).value != info.value


def test_the_metadata_queries_answer(rfs, sandbox, real_server):
    assert rfs.ping() is None
    protocol = rfs.protocol()
    assert protocol.version >= 0x0500
    assert rfs.query_config("version")["version"].startswith("v")
    space = rfs.statvfs(sandbox)
    assert space.nodes_rw >= 1
    assert [where.address for where in rfs.locate(sandbox)]


def test_copying_moves_bytes_in_both_directions(rfs, sandbox, blob, tmp_path, real_server):
    local = tmp_path / "down.root"
    result = xrdclient.copy(url_for(real_server, blob), str(local), config=_REAL_CONFIG)
    assert local.read_bytes() == BLOB
    assert result.size == len(BLOB)

    up = f"{sandbox}/up.root"
    xrdclient.copy(str(local), url_for(real_server, up), config=_REAL_CONFIG)
    assert rfs.read_bytes(up) == BLOB

    across = f"{sandbox}/across.root"
    xrdclient.copy(url_for(real_server, up), url_for(real_server, across), config=_REAL_CONFIG)
    assert rfs.checksum(across).value == rfs.checksum(blob).value


def test_a_copied_tree_arrives_whole(rfs, sandbox, tmp_path, real_server):
    for name in ("a.root", "sub/b.root"):
        rfs.write_bytes(f"{sandbox}/{name}", name.encode())
    out = tmp_path / "tree"
    results = xrdclient.copy_tree(url_for(real_server, sandbox), str(out), config=_REAL_CONFIG)
    assert len(results) == 2
    assert (out / "a.root").read_bytes() == b"a.root"
    assert (out / "sub" / "b.root").read_bytes() == b"sub/b.root"


# ---------------------------------------------------------------------------
# Extended attributes - if the exported filesystem has them
# ---------------------------------------------------------------------------


def test_extended_attributes_round_trip(rfs, blob):
    try:
        rfs.setxattr(blob, "user.experiment", b"cms")
    except (UnsupportedError, NotFoundError) as exc:  # tmpfs without user xattrs
        pytest.skip(f"the export does not support xattrs: {exc}")
    assert rfs.getxattr(blob, "user.experiment") == b"cms"
    # Names come back mangled - the daemon's attribute plugin strips its own
    # namespace prefix on the way out and takes four characters of the name
    # with it - so this asserts the tail, not the name. Set and get, which is
    # what a caller actually does, round trip exactly.
    assert any(name.endswith(".experiment") for name in rfs.listxattr(blob))
    rfs.removexattr(blob, "user.experiment")
    assert rfs.listxattr(blob) == []


# ---------------------------------------------------------------------------
# Errors, as the real server words them
# ---------------------------------------------------------------------------


def test_a_missing_file_is_a_not_found_error(rfs, sandbox):
    with pytest.raises(NotFoundError) as caught:
        rfs.read_bytes(f"{sandbox}/nope.root")
    assert f"{sandbox}/nope.root" in str(caught.value)
    assert isinstance(caught.value, FileNotFoundError)


def test_removing_a_directory_with_something_in_it_says_so(rfs, sandbox, blob):
    with pytest.raises(ExistsError):
        rfs.rmdir(sandbox)


def test_creating_a_file_that_exists_is_refused(rfs, blob):
    handle = File(rfs.url.with_path(blob), _REAL_CONFIG)
    with pytest.raises(ExistsError):
        handle.open(OpenFlags.NEW | OpenFlags.WRITE)


def test_visa_is_a_server_limitation_not_a_client_bug(rfs, blob):
    """``kXR_fctl``/visa is in the protocol; stock ``xrootd`` does not do it.

    Asserting it keeps the mapping honest: the client must report the server's
    "unsupported" rather than dressing it up as something else.
    """
    with File(rfs.url.with_path(blob), _REAL_CONFIG) as handle:
        with pytest.raises(UnsupportedError):
            handle.visa()


# ---------------------------------------------------------------------------
# Against the stock command line tools
# ---------------------------------------------------------------------------


def _tool(name: str) -> str:
    import shutil

    found = shutil.which(name)
    if found is None:  # pragma: no cover - depends on the installation
        pytest.skip(f"no {name} on PATH")
    return found


def test_xrdcp_reads_back_what_we_wrote(real_server, rfs, sandbox, tmp_path):
    """Two independent implementations, one file: the strongest check here."""
    remote = f"{sandbox}/ours.root"
    rfs.write_bytes(remote, BLOB)
    out = tmp_path / "theirs.root"
    subprocess.run(
        [_tool("xrdcp"), "-f", "-s", url_for(real_server, remote), str(out)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    assert out.read_bytes() == BLOB


def test_we_read_back_what_xrdcp_wrote(real_server, rfs, sandbox, tmp_path):
    source = tmp_path / "theirs.root"
    source.write_bytes(BLOB)
    remote = f"{sandbox}/theirs.root"
    subprocess.run(
        [_tool("xrdcp"), "-f", "-s", str(source), url_for(real_server, remote)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    assert rfs.read_bytes(remote) == BLOB
    assert int(rfs.checksum(remote).value, 16) == zlib.adler32(BLOB)


def test_xrdfs_sees_the_directory_we_made(real_server, rfs, sandbox):
    rfs.write_bytes(f"{sandbox}/seen.root", b"hi")
    listing = subprocess.run(
        [_tool("xrdfs"), real_server.url, "ls", sandbox],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    assert f"{sandbox}/seen.root" in listing


# ---------------------------------------------------------------------------
# Our own command line
# ---------------------------------------------------------------------------


def test_the_cli_lists_stats_and_copies(real_server, rfs, sandbox, tmp_path, capsys):
    rfs.write_bytes(f"{sandbox}/cli.root", BLOB)

    assert cli_fs.main(["ls", url_for(real_server, sandbox)]) == 0
    assert "cli.root" in capsys.readouterr().out

    assert cli_fs.main(["stat", url_for(real_server, f"{sandbox}/cli.root")]) == 0
    assert str(len(BLOB)) in capsys.readouterr().out

    out = tmp_path / "cli.root"
    assert cli_cp.main(["-q", url_for(real_server, f"{sandbox}/cli.root"), str(out)]) == 0
    assert out.read_bytes() == BLOB

    assert cli_fs.main(["rm", url_for(real_server, f"{sandbox}/cli.root")]) == 0
    assert not rfs.exists(f"{sandbox}/cli.root")


def test_the_cli_reports_a_missing_file_without_a_traceback(real_server, sandbox, capsys):
    assert cli_fs.main(["stat", url_for(real_server, f"{sandbox}/no.root")]) != 0
    assert "Traceback" not in capsys.readouterr().err
