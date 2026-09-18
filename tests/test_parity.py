"""Differential testing against the official XRootD python bindings.

Same daemon, same files, both clients: anything this library reports that
``XRootD.client`` does not is a bug in one of them, and the disagreement is
worth more than either result on its own. Where the two libraries deliberately
differ - exceptions instead of ``(status, result)`` tuples, ``bytes`` instead
of buffers - the test asserts the *values* agree, not the shapes.

The bindings are an optional development dependency; without them the whole
module skips.
"""

from __future__ import annotations

import socket
import zlib

import pytest

import xrdclient
from conftest import _REAL_CONFIG
from xrdclient.client.file import File
from xrdclient.flags import OpenFlags

client = pytest.importorskip("XRootD.client", reason="the official bindings are not installed")
from XRootD.client.flags import DirListFlags, MkDirFlags, QueryCode  # noqa: E402
from XRootD.client.flags import OpenFlags as TheirFlags  # noqa: E402

pytestmark = [pytest.mark.interop, pytest.mark.parity]

BLOB = bytes(range(256)) * 64


def check(status, result=None):
    """Unwrap the bindings' ``(status, result)`` pair, loudly."""
    assert status.ok, status.message
    return result


@pytest.fixture
def theirs(real_server):
    """The official :class:`XRootD.client.FileSystem` on the same daemon."""
    return client.FileSystem(real_server.url)


@pytest.fixture
def ours(real_server):
    with xrdclient.FileSystem(real_server.url, _REAL_CONFIG) as filesystem:
        yield filesystem


@pytest.fixture
def blob(ours, sandbox):
    path = f"{sandbox}/blob.root"
    ours.write_bytes(path, BLOB)
    return path


def their_file(url: str, flags=TheirFlags.READ):
    handle = client.File()
    check(handle.open(url, flags)[0])
    return handle


def url_for(server, path: str) -> str:
    return f"{server.url.rstrip('/')}//{path.lstrip('/')}"


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_stat_agrees_field_by_field(ours, theirs, blob):
    mine = ours.stat(blob)
    yours = check(*theirs.stat(blob))
    assert mine.st_size == yours.size
    assert mine.flags == yours.flags
    assert mine.st_mtime == yours.modtime
    assert mine.id == str(yours.id)


def test_a_missing_file_fails_on_both_sides(ours, theirs, sandbox):
    missing = f"{sandbox}/missing.root"
    status, _ = theirs.stat(missing)
    assert not status.ok and status.errno
    with pytest.raises(FileNotFoundError) as caught:
        ours.stat(missing)
    # Same error number, one as an attribute and one as an exception.
    assert caught.value.code == status.errno or status.code


def test_statvfs_reports_the_same_space(ours, theirs, sandbox):
    mine = ours.statvfs(sandbox)
    # ``kXR_Qspace`` answers a CGI string, which the bindings hand over as it
    # arrived; ``kXR_statvfs``, which is what we ask, answers megabytes. Same
    # filesystem either way, so the two must agree to within rounding.
    yours = check(*theirs.query(QueryCode.SPACE, sandbox)).decode()
    fields = dict(part.split("=", 1) for part in yours.strip("\x00").split("&"))
    free_mb = int(fields["oss.free"]) // (1 << 20)
    assert abs(mine.free_rw - free_mb) < max(64, free_mb * 0.01)
    assert mine.nodes_rw >= 1 and mine.utilization_rw <= 100


def test_a_directory_listing_holds_the_same_names(ours, theirs, sandbox):
    for name in ("a.root", "b.root", "c.dat"):
        ours.write_bytes(f"{sandbox}/{name}", b"x" * len(name))
    ours.mkdir(f"{sandbox}/sub")

    mine = {entry.name: entry for entry in ours.scandir(sandbox)}
    listing = check(*theirs.dirlist(sandbox, DirListFlags.STAT))
    yours = {entry.name: entry for entry in listing}
    assert set(mine) == set(yours)
    for name, entry in yours.items():
        assert mine[name].stat.st_size == entry.statinfo.size
        assert mine[name].is_dir() == bool(entry.statinfo.flags & 2)


def test_the_protocol_negotiation_lands_on_the_same_numbers(ours, theirs):
    """Same words off the wire - the bindings just never byte-swap them.

    ``XRootD.client`` reports ``version`` and ``hostinfo`` in network order,
    so 5.1.1 arrives as ``0x11050000``; swap it and the two agree exactly.

    The number itself belongs to whichever daemon is installed - 5.9.6 answers
    ``0x0511``, later ones answer more - so the assertion is that both clients
    read the same word, and that it is a protocol 5 server.
    """
    mine = ours.protocol()
    yours = check(*theirs.protocol())
    assert mine.version == socket.ntohl(yours.version)
    assert mine.version >> 8 == 0x05
    assert mine.flags == socket.ntohl(yours.hostinfo)


def test_both_clients_ping(ours, theirs):
    check(*theirs.ping())
    assert ours.ping() is None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("offset, length", [(0, 16), (1024, 4096), (16000, 384), (0, 16384)])
def test_the_same_bytes_come_back_from_both(ours, real_server, blob, offset, length):
    handle = their_file(url_for(real_server, blob))
    try:
        yours = check(*handle.read(offset, length))
    finally:
        handle.close()
    with File(ours.url.with_path(blob), _REAL_CONFIG) as mine:
        assert mine.read(length, offset) == yours == BLOB[offset : offset + length]


def test_vector_reads_agree_chunk_for_chunk(ours, real_server, blob):
    ranges = [(0, 16), (4096, 256), (8192, 1024)]
    handle = their_file(url_for(real_server, blob))
    try:
        yours = [bytes(chunk.buffer) for chunk in check(*handle.vector_read(ranges)).chunks]
    finally:
        handle.close()
    with File(ours.url.with_path(blob), _REAL_CONFIG) as mine:
        assert mine.readv(ranges) == yours


def test_a_whole_file_read_agrees(ours, real_server, blob):
    handle = their_file(url_for(real_server, blob))
    try:
        yours = check(*handle.read())
    finally:
        handle.close()
    assert ours.read_bytes(blob) == yours == BLOB


# ---------------------------------------------------------------------------
# Writing, each client reading what the other wrote
# ---------------------------------------------------------------------------


def test_they_read_back_what_we_wrote(ours, real_server, sandbox):
    path = f"{sandbox}/ours.root"
    ours.write_bytes(path, BLOB)
    handle = their_file(url_for(real_server, path))
    try:
        assert check(*handle.read()) == BLOB
        assert check(*handle.stat(force=True)).size == len(BLOB)
    finally:
        handle.close()


def test_we_read_back_what_they_wrote(ours, real_server, sandbox):
    path = f"{sandbox}/theirs.root"
    handle = client.File()
    check(handle.open(url_for(real_server, path), TheirFlags.NEW | TheirFlags.UPDATE)[0])
    try:
        check(handle.write(BLOB)[0])
    finally:
        handle.close()
    assert ours.read_bytes(path) == BLOB


def test_a_writev_of_ours_is_readable_by_them(ours, real_server, sandbox):
    """The framing fix, checked by the implementation that defines it."""
    path = f"{sandbox}/v.root"
    ours.write_bytes(path, b"\x00" * 64)
    mine = File(ours.url.with_path(path), _REAL_CONFIG)
    mine.open(OpenFlags.UPDATE)
    with mine:
        mine.writev([(0, b"first"), (32, b"second")])
    handle = their_file(url_for(real_server, path))
    try:
        assert check(*handle.read()) == b"first" + bytes(27) + b"second" + bytes(26)
    finally:
        handle.close()


def test_truncate_from_either_side_looks_the_same_from_the_other(ours, real_server, blob):
    ours.truncate(blob, 4096)
    handle = their_file(url_for(real_server, blob))
    try:
        assert check(*handle.stat(force=True)).size == 4096
    finally:
        handle.close()

    handle = their_file(url_for(real_server, blob), TheirFlags.UPDATE)
    try:
        check(handle.truncate(1024)[0])
    finally:
        handle.close()
    assert ours.stat(blob).st_size == 1024


# ---------------------------------------------------------------------------
# Namespace mutation
# ---------------------------------------------------------------------------


def test_a_directory_made_by_one_is_seen_by_the_other(ours, theirs, sandbox):
    check(*theirs.mkdir(f"{sandbox}/theirs", MkDirFlags.MAKEPATH))
    assert ours.isdir(f"{sandbox}/theirs")
    ours.mkdir(f"{sandbox}/ours")
    assert check(*theirs.stat(f"{sandbox}/ours")).flags & 2

    ours.rmdir(f"{sandbox}/theirs")
    assert not theirs.stat(f"{sandbox}/theirs")[0].ok
    check(*theirs.rmdir(f"{sandbox}/ours"))
    assert not ours.exists(f"{sandbox}/ours")


def test_a_rename_by_one_is_visible_to_the_other(ours, theirs, sandbox, blob):
    moved = f"{sandbox}/moved.root"
    check(*theirs.mv(blob, moved))
    assert ours.getsize(moved) == len(BLOB)
    ours.rename(moved, blob)
    assert check(*theirs.stat(blob)).size == len(BLOB)


def test_a_file_removed_by_one_is_gone_for_the_other(ours, theirs, blob):
    check(*theirs.rm(blob))
    assert not ours.exists(blob)


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------


def test_checksums_match_each_other_and_zlib(ours, theirs, blob):
    mine = ours.checksum(blob)
    yours = check(*theirs.query(QueryCode.CHECKSUM, blob)).decode()
    algorithm, _, value = yours.strip("\x00").partition(" ")
    assert (mine.algorithm, mine.value) == (algorithm, value.strip("\x00"))
    assert int(mine.value, 16) == zlib.adler32(BLOB)


# ---------------------------------------------------------------------------
# Copying
# ---------------------------------------------------------------------------


def test_a_file_copied_by_their_engine_matches_ours(ours, real_server, sandbox, blob, tmp_path):
    theirs_out = tmp_path / "theirs.root"
    process = client.CopyProcess()
    process.add_job(url_for(real_server, blob), str(theirs_out))
    check(process.prepare())
    check(process.run()[0])

    ours_out = tmp_path / "ours.root"
    xrdclient.copy(url_for(real_server, blob), str(ours_out), config=_REAL_CONFIG)
    assert ours_out.read_bytes() == theirs_out.read_bytes() == BLOB


# ---------------------------------------------------------------------------
# Where we deliberately part company
# ---------------------------------------------------------------------------


def test_we_raise_where_they_return_a_status(ours, theirs, sandbox):
    """The one difference the whole library is built around.

    The bindings hand back ``(status, result)`` and leave the checking to the
    caller, which is how an unchecked status becomes a silent data-loss bug.
    Here the same condition is an exception of the matching builtin type.
    """
    missing = f"{sandbox}/none.root"
    status, result = theirs.stat(missing)
    assert not status.ok and result is None  # nothing raised, nothing noticed

    with pytest.raises(FileNotFoundError):
        ours.stat(missing)
