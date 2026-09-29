"""``File.fcntl``, opens against a template file, and listing inside a ZIP archive.

The three things XrdCl can do to a file that are not plain I/O: hand the
storage plug-in an opaque request on a handle (``File::Fcntl``), create a
file next to - or as a clone of - one that is already open
(``OpenUsingTemplate`` with ``kXR_samefs`` / ``kXR_dup``), and list the
members of a ZIP archive from its central directory (``DirListFlags::Zip``).
All run here against :class:`~xrdclient.testing.FakeServer`; the parity
suite checks the same against a real xrootd and the official bindings.
"""

from __future__ import annotations

import io
import os
import struct
import zipfile

import pytest

from xrdclient import File, FileSystem
from xrdclient.client import _zip
from xrdclient.client._zip import ZipArchiveError
from xrdclient.errors import (
    NotFoundError,
    ServerError,
    UnsupportedError,
    kXR_ArgInvalid,
    kXR_FileNotOpen,
    kXR_Unsupported,
)
from xrdclient.flags import StatInfoFlags
from xrdclient.proto import constants as c
from xrdclient.proto import requests as r
from xrdclient.proto.buffer import Writer
from xrdclient.testing import FakeServer

# -- the request ---------------------------------------------------------------


def test_an_open_carries_the_template_after_the_options():
    """``optiont`` then six reserved bytes, then ``fhtemplt``: XProtocol's layout."""
    request = r.Open("/f", c.kXR_new, 0o644, optiont=c.kXR_dup, fhtemplt=b"\x01\x02\x03\x04")
    params = request.header_params()
    assert params == struct.pack(">HHH6x4s", 0o644, c.kXR_new, c.kXR_dup, b"\x01\x02\x03\x04")
    writer = Writer()
    request.params(writer)
    assert bytes(writer.buffer) == params
    assert r.Open("/f", 0).header_params()[4:] == bytes(12)


# -- fcntl -------------------------------------------------------------------


def test_fcntl_is_an_opaque_query_the_stock_server_refuses(server, config):
    with File(f"{server.url}/data/a.root", config) as fh:
        with pytest.raises(UnsupportedError) as caught:
            fh.fcntl(b"anything")
    assert caught.value.code == kXR_Unsupported
    assert "fctl operation not supported" in str(caught.value)


def test_fcntl_hands_the_plugin_the_bytes_and_returns_its_answer(server, config):
    asked = []
    server.fctl = lambda path, data: (asked.append((path, data)), b"re:" + data)[1]
    with File(f"{server.url}/data/a.root", config) as fh:
        assert fh.fcntl(b"\x00binary\xff") == b"re:\x00binary\xff"
        assert fh.fcntl() == b"re:"
    assert asked == [("/data/a.root", b"\x00binary\xff"), ("/data/a.root", b"")]


def test_fcntl_needs_an_open_file(server, config):
    with pytest.raises(ValueError, match="closed file"):
        File(f"{server.url}/data/a.root", config).fcntl(b"x")


def test_the_fake_refuses_fcntl_on_a_handle_it_did_not_issue(server, config):
    server.fctl = lambda path, data: data
    with FileSystem(server.url, config) as fs, pytest.raises(ServerError) as caught:
        fs._router.execute(r.Query(c.kXR_Qopaqug, b"x", fhandle=b"\xee" * 4))
    assert caught.value.code == kXR_FileNotOpen


# -- opening against a template ---------------------------------------------------


@pytest.fixture
def template(server, config):
    with File(f"{server.url}/data/a.root", config) as fh:
        yield fh


def test_samefs_places_a_new_file_beside_the_template(server, config, template):
    fh = File(f"{server.url}/data/next.root", config)
    fh.open("new update", template=template)
    with fh:
        fh.write(b"xy")
        assert fh.session is template.session
    assert server.colocated == {"/data/next.root": "/data/a.root"}
    assert server.contents("/data/next.root") == b"xy"


def test_dup_gives_the_new_file_the_templates_contents(server, config, template):
    fh = File(f"{server.url}/data/copy.root", config)
    fh.open("new update", template=template, dup=True)
    with fh:
        assert fh.read() == b"hello world"
    assert server.contents("/data/copy.root") == b"hello world"


@pytest.mark.parametrize(
    ("flags", "dup", "code", "words"),
    [
        ("new", True, kXR_ArgInvalid, "not being opened R/W"),
        ("update", False, kXR_ArgInvalid, "must be opened as a new file"),
    ],
)
def test_the_server_decides_what_a_template_open_may_be(
    server, config, template, flags, dup, code, words
):
    fh = File(f"{server.url}/data/refused.root", config)
    with pytest.raises(ServerError) as caught:
        fh.open(flags, template=template, dup=dup)
    assert (caught.value.code, words in str(caught.value)) == (code, True)
    assert not fh.is_open
    assert template.read() == b"hello world"  # the shared connection is still the template's


@pytest.mark.parametrize(
    ("dup", "words"),
    [(True, "file cloning is not supported"), (False, "colocating with a specified file")],
)
def test_storage_that_cannot_colocate_says_so(server, config, template, dup, words):
    server.colocates = False
    with pytest.raises(UnsupportedError, match=words):
        File(f"{server.url}/data/n.root", config).open("new update", template=template, dup=dup)


def test_a_template_must_be_open(server, config):
    closed = File(f"{server.url}/data/a.root", config)
    with pytest.raises(ValueError, match="closed file"):
        File(f"{server.url}/data/n.root", config).open("new", template=closed)
    assert "/data/n.root" not in server.opened


def test_the_fake_refuses_a_template_handle_it_did_not_issue(server, config):
    with FileSystem(server.url, config) as fs, pytest.raises(ServerError) as caught:
        fs._router.execute(
            r.Open("/data/n.root", c.kXR_new, optiont=c.kXR_samefs, fhtemplt=b"\xee" * 4)
        )
    assert caught.value.code == kXR_FileNotOpen


def test_a_server_too_old_for_templates_is_not_asked(config):
    with FakeServer(files={"/a": b"a"}, version=0x0000_0400) as srv:
        with File(f"{srv.url}/a", config) as template:
            fh = File(f"{srv.url}/b", config)
            with pytest.raises(UnsupportedError, match="older than the 0x520"):
                fh.open("new update", template=template)
        assert "/b" not in srv.opened


def test_the_new_file_lives_where_its_template_does(server, config, template):
    """XrdCl sends the open to the template's data server, whatever the URL said."""
    port = server.url.port
    fh = File(f"root://nowhere.invalid:{port + 1}/data/moved.root", config)
    fh.open("new update", template=template)
    try:
        assert (fh.url.host, fh.url.port, fh.url.path) == (
            template.url.host,
            port,
            "/data/moved.root",
        )
    finally:
        fh.close()


def test_a_later_plain_open_forgets_the_template(server, config, template):
    fh = File(f"{server.url}/data/twice.root", config)
    fh.open("new update", template=template)
    fh.close()
    fh.open("read")
    assert fh._template == (0, c.NULL_FHANDLE)
    fh.close()


# -- the central directory ------------------------------------------------------


def _archive(members: dict[str, bytes], *, comment: bytes = b"", deflate: bool = False) -> bytes:
    buffer = io.BytesIO()
    method = zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED
    with zipfile.ZipFile(buffer, "w", method) as archive:
        archive.comment = comment
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class _Reads:
    """A reader over ``data`` that remembers what it was asked for."""

    def __init__(self, data: bytes) -> None:
        self.data, self.asked = data, []

    def __call__(self, offset: int, length: int) -> bytes:
        self.asked.append((offset, length))
        return self.data[offset : offset + length]


def _members(data: bytes) -> list[tuple[str, int]]:
    return [(m.name, m.size) for m in _zip.members(_Reads(data), len(data))]


def test_a_small_archive_is_read_whole_in_one_go():
    data = _archive({"a.txt": b"hello", "dir/": b"", "dir/b": b"x" * 300}, deflate=True)
    reads = _Reads(data)
    assert [(m.name, m.size) for m in _zip.members(reads, len(data))] == [
        ("a.txt", 5),
        ("dir/", 0),
        ("dir/b", 300),
    ]
    assert reads.asked == [(0, len(data))]


def test_a_large_archive_costs_a_read_of_its_tail_and_one_of_its_directory():
    data = _archive({"big": os.urandom(80_000), "small": b"s"})
    reads = _Reads(data)
    assert [m.name for m in _zip.members(reads, len(data))] == ["big", "small"]
    assert len(reads.asked) == 2
    assert reads.asked[0] == (len(data) - _zip._TAIL, _zip._TAIL)


def test_an_empty_file_is_an_empty_archive():
    assert _zip.members(_Reads(b""), 0) == []


def test_an_archive_comment_is_stepped_over():
    data = _archive({"a": b"1"}, comment=b"." * 300)
    assert _members(data) == [("a", 1)]


def test_names_are_utf8_with_bad_bytes_replaced():
    data = _archive({"café": b"1"})
    assert _members(data) == [("café", 1)]
    broken = data.replace("café".encode(), b"caf\xff\xfe")
    assert _members(broken)[0][0] == "caf��"


@pytest.mark.parametrize("data", [b"", b"abc", b"x" * 21, b"PK\x05\x06" + b"\x00" * 17])
def test_no_end_record_is_not_an_archive(data):
    with pytest.raises(ZipArchiveError, match=r"^End-of-central-directory signature not found"):
        _zip.members(_Reads(data), len(data) or 1)


def _eocd(count: int, size: int, offset: int, comment: int = 0) -> bytes:
    return struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, count, count, size, offset, comment)


@pytest.mark.parametrize("tail", [_eocd(0, 0, 0, comment=5), _eocd(1, 10, 100), _eocd(1, 100, 0)])
def test_an_end_record_pointing_outside_the_file_is_corrupt(tail):
    data = b"x" * 30 + tail
    with pytest.raises(ZipArchiveError, match="signature corrupted"):
        _zip.members(_Reads(data), len(data))


def _cdfh(
    name: bytes = b"m",
    *,
    compressed: int = 1,
    uncompressed: int = 1,
    offset: int = 0,
    disk: int = 0,
    extra: bytes = b"",
    comment: bytes = b"",
    signature: bytes = b"PK\x01\x02",
) -> bytes:
    fixed = struct.pack(
        "<4s6H3I5H2I",
        signature,
        20,
        20,
        0,
        0,
        0,
        0,
        0,
        compressed,
        uncompressed,
        len(name),
        len(extra),
        len(comment),
        disk,
        0,
        0,
        offset,
    )
    return fixed + name + extra + comment


def _with_directory(records: list[bytes], *, body: int = 64, count: int | None = None) -> bytes:
    directory = b"".join(records)
    listed = len(records) if count is None else count
    return b"\x00" * body + directory + _eocd(listed, len(directory), body)


@pytest.mark.parametrize(
    "records",
    [
        [_cdfh(signature=b"PK\x09\x09")],
        [_cdfh(compressed=200, offset=10)],
        [_cdfh(compressed=100), _cdfh(compressed=100)],
    ],
    ids=["signature", "past the end", "more than the file"],
)
def test_a_directory_that_does_not_fit_the_file_is_corrupt(records):
    data = _with_directory(records)
    with pytest.raises(ZipArchiveError, match=r"^ZIP Central Directory corrupted"):
        _zip.members(_Reads(data), len(data))


def test_a_record_longer_than_the_directory_is_corrupt():
    record = bytearray(_cdfh())
    record[28:30] = (16).to_bytes(2, "little")  # a longer name than the directory holds
    data = _with_directory([bytes(record)])
    with pytest.raises(ZipArchiveError, match=r"^ZIP Central Directory corrupted"):
        _zip.members(_Reads(data), len(data))


def test_a_directory_shorter_than_its_count_stops_where_it_ends():
    data = _with_directory([_cdfh(b"a"), _cdfh(b"b")], count=5)
    assert _members(data) == [("a", 1), ("b", 1)]


def _zip64_field(*values: int, wide: tuple[int, ...] = (8, 8, 8, 4)) -> bytes:
    body = b"".join(v.to_bytes(w, "little") for v, w in zip(values, wide))
    return struct.pack("<HH", 1, len(body)) + body


def test_zip64_sizes_come_from_the_extra_field():
    big = 5 << 32
    other = struct.pack("<HH", 0x5455, 1) + b"t"  # a field to step over
    records = [
        _cdfh(b"both", compressed=0xFFFFFFFF, uncompressed=0xFFFFFFFF, extra=_zip64_field(big, 3)),
        _cdfh(b"packed only", compressed=0xFFFFFFFF, uncompressed=7, extra=other + _zip64_field(2)),
        _cdfh(b"offset only", offset=0xFFFFFFFF, uncompressed=9, extra=_zip64_field(0)),
        _cdfh(b"disk only", disk=0xFFFF, uncompressed=4, extra=struct.pack("<HHI", 1, 4, 0)),
        _cdfh(b"no field", uncompressed=0xFFFFFFFF, compressed=0, extra=other),
        _cdfh(b"torn field", uncompressed=0xFFFFFFFF, compressed=0, extra=b"\x01"),
    ]
    found = _zip._records(b"".join(records), len(records), 1 << 40)
    assert [(m.name, m.size) for m in found] == [
        ("both", big),
        ("packed only", 7),
        ("offset only", 9),
        ("disk only", 4),
        ("no field", 0xFFFFFFFF),
        ("torn field", 0xFFFFFFFF),
    ]


def test_a_zip64_field_of_the_wrong_length_is_corrupt():
    record = _cdfh(uncompressed=0xFFFFFFFF, extra=_zip64_field(1, 2))
    with pytest.raises(ZipArchiveError, match="Central Directory corrupted"):
        _zip._records(record, 1, 1 << 20)


def _zip64_archive(records: list[bytes], *, body: int, gap: int = 0) -> bytes:
    """An archive whose directory is found through the ZIP64 records.

    The narrow end record says 0xFFFF entries at offset 0, as one that has
    overflowed does - and inside the file, since XrdCl checks it all the same.
    """
    directory = b"".join(records)
    eocd64 = struct.pack(
        "<4sQ2H2I4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        len(records),
        len(records),
        len(directory),
        body,
    )
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, body + len(directory), 1)
    return b"\x00" * body + directory + eocd64 + b"\x00" * gap + locator + _eocd(0xFFFF, 0, 0)


def test_a_zip64_archive_is_found_through_its_locator():
    data = _zip64_archive([_cdfh(b"a"), _cdfh(b"b")], body=70_000)
    reads = _Reads(data)
    assert [m.name for m in _zip.members(reads, len(data))] == ["a", "b"]
    assert len(reads.asked) == 2  # the record was in the tail already


def test_a_zip64_record_further_back_than_the_tail_is_read_for():
    data = _zip64_archive([_cdfh(b"a")], body=70_000, gap=70_000)
    reads = _Reads(data)
    assert [m.name for m in _zip.members(reads, len(data))] == ["a"]
    assert len(reads.asked) == 3


def test_a_locator_without_its_record_is_corrupt():
    data = bytearray(_zip64_archive([_cdfh(b"a")], body=70_000))
    at = data.rfind(b"PK\x06\x06")
    data[at : at + 4] = b"XXXX"
    with pytest.raises(ZipArchiveError, match=r"^ZIP64 End-of-central-directory signature not"):
        _zip.members(_Reads(bytes(data)), len(data))


def test_a_locator_at_the_very_start_of_the_tail_is_not_looked_at():
    """XrdCl only takes a locator that lies strictly inside what it read."""
    directory = _cdfh(b"a")
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, 0, 1)
    end = _eocd(1, len(directory), 100, comment=0xFFFF) + b"c" * 0xFFFF
    data = b"\x00" * 100 + directory + locator + end
    reads = _Reads(data)
    assert [m.name for m in _zip.members(reads, len(data))] == ["a"]
    assert reads.asked[0][0] == 100 + len(directory)  # the tail starts at the locator


# -- through the filesystem ---------------------------------------------------------


ARCHIVE = _archive({"hello.txt": b"hello world", "dir/": b"", "dir/inner": b"x" * 1000})


@pytest.fixture
def zipped(config):
    with FakeServer(files={"/z/a.zip": ARCHIVE, "/z/not.zip": b"abc", "/z/e.zip": b""}) as srv:
        with FileSystem(srv.url, config) as fs:
            yield srv, fs


def test_list_archive_gives_each_member_the_archives_stat(zipped):
    _, fs = zipped
    archive = fs.stat("/z/a.zip")
    entries = fs.list_archive("/z/a.zip")
    assert [(e.name, e.stat.st_size) for e in entries] == [
        ("hello.txt", 11),
        ("dir/", 0),
        ("dir/inner", 1000),
    ]
    assert {e.parent for e in entries} == {"/z/a.zip"}
    assert entries[0].path == "/z/a.zip/hello.txt"
    stat = entries[2].stat
    assert (stat.id, stat.st_mtime) == (archive.id, archive.st_mtime)
    assert stat.path == "/z/a.zip/dir/inner"
    assert stat.flags == archive.flags & ~StatInfoFlags.IS_WRITABLE
    assert archive.flags & StatInfoFlags.IS_WRITABLE  # there was something to take away


def test_list_archive_names_the_problem(zipped):
    _, fs = zipped
    assert fs.list_archive("/z/e.zip") == []
    with pytest.raises(ZipArchiveError, match="signature not found"):
        fs.list_archive("/z/not.zip")
    with pytest.raises(NotFoundError):
        fs.list_archive("/z/missing.zip")


def test_list_archive_keeps_the_cgi_off_the_parent(zipped):
    _, fs = zipped
    assert {e.parent for e in fs.list_archive("/z/a.zip?authz=token")} == {"/z/a.zip"}
