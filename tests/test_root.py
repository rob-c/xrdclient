"""Reading ROOT files in pure Python.

The files under ``tests/data`` are real ones written by real ROOT versions
(see the README there), so what is asserted here is what ROOT wrote. The
crafted bytes are for the corners no honest file in the corpus reaches: a
64-bit file header, a directory with no keys, and a tree written before this
reader's history begins.
"""

from __future__ import annotations

import array
import datetime
import io
import math
import os
import pathlib
import pickle
import struct
import zlib

import pytest

from xrd.root import (
    Branch,
    Directory,
    FormatError,
    Jagged,
    Key,
    ROOTFile,
    TTree,
    UnsupportedFeatureError,
    open_root,
)
from xrd.root.buffer import BYTE_COUNT_MASK, CLASS_MASK, MAP_OFFSET, NEW_CLASS_TAG, Buffer
from xrd.root.compression import _lz4, algorithm, decompress
from xrd.root.cxx import parse
from xrd.root.file import Source, _directory_record
from xrd.root.graph import Graph
from xrd.root.hist import Histogram
from xrd.root.interp import (
    MEMBER_WISE,
    Refused,
    _class_held,
    _Described,
    _objects,
    _sequence,
    build,
)
from xrd.root.objects import BranchRecord, LeafRecord, read_branch, read_tree
from xrd.root.streamers import Member
from xrd.root.tree import Basket

DATA = pathlib.Path(__file__).parent / "data"


def opened(name: str) -> ROOTFile:
    return open_root(str(DATA / f"{name}.root"))


@pytest.fixture
def simple():
    with opened("simple") as handle:
        yield handle["tree"]


@pytest.fixture
def flat():
    with opened("small-flat-tree") as handle:
        yield handle["tree"]


# -- crafted bytes --------------------------------------------------------


def tstring(text: str) -> bytes:
    raw = text.encode()
    return bytes([len(raw)]) + raw


def record(version: int, body: bytes = b"") -> bytes:
    """A byte-counted record, which is how ROOT introduces every class."""
    return struct.pack(">IH", BYTE_COUNT_MASK | (2 + len(body)), version) + body


def key_bytes(
    classname: str,
    name: str,
    title: str = "",
    *,
    seek_key: int = 0,
    payload: bytes = b"",
    big: bool = False,
    datime: int = 0,
) -> bytes:
    tail = tstring(classname) + tstring(name) + tstring(title)
    keylen = 18 + (16 if big else 8) + len(tail)
    seeks = struct.pack(">qq" if big else ">ii", seek_key, 0)
    version = 1004 if big else 4
    head = struct.pack(">iHiIhh", keylen + len(payload), version, len(payload), datime, keylen, 1)
    return head + seeks + tail + payload


def wide_file(*, seek_keys_broken: bool = False) -> bytes:
    """A whole ROOT file in the 64-bit layout, holding one unreadable object."""
    begin, nbytes_name = 100, 32
    seek_keys = begin + nbytes_name + 42
    inner = key_bytes("TH1F", "h", "a histogram", seek_key=seek_keys + 100, big=True)
    keys = key_bytes("TDirectory", "f", big=True, seek_key=seek_keys) + struct.pack(">i", 1) + inner
    directory = struct.pack(
        ">HIIiiqqq",
        1005,
        0,
        0,
        len(keys),
        nbytes_name,
        begin,
        0,
        0 if seek_keys_broken else seek_keys,
    )
    header = struct.pack(">4sii", b"root", 1000004, begin) + struct.pack(
        ">qqiiiBiqi", len(keys) + seek_keys, 0, 0, 0, nbytes_name, 4, 1, 0, 0
    )
    return header.ljust(begin + nbytes_name, b"\x00") + directory + keys


def named_bytes(name: str, title: str = "") -> bytes:
    return record(1, struct.pack(">HII", 1, 0, 0) + tstring(name) + tstring(title))


def objarray_bytes() -> bytes:
    """A ``TObjArray`` holding nothing, which is what an empty tree has."""
    return record(3, struct.pack(">HII", 1, 0, 0) + tstring("") + struct.pack(">ii", 0, 0))


def root4_tree_bytes() -> bytes:
    """A ``TTree`` in the ROOT 4 layout, where every counter is a narrow one."""
    body = named_bytes("t", "a ROOT 4 tree") + record(1) * 3
    body += struct.pack(">dddd", 7, 0, 0, 0)  # entries, then bytes written three ways
    body += struct.pack(">iii", 0, 0, 0)  # timer interval, scan field, update
    body += struct.pack(">iiii", 0, 0, 0, 0)  # the entry and size limits, and the estimate
    return record(5, body + objarray_bytes())


def root4_branch_bytes(version: int = 8, *, wide: bool = False) -> bytes:
    """A ``TBranch`` in the ROOT 4 layout, with one basket written out."""
    body = named_bytes("b", "b/I") + (record(1) if version > 7 else b"")
    body += struct.pack(">iiii", 1, 4096, 0, 1)  # compression, basket size, offsets, baskets
    body += struct.pack(">iii", 5, 0, 2)  # next entry, offset in the parent, max baskets
    if version > 6:
        body += struct.pack(">i", 0)  # split level
    body += struct.pack(">ddd", 5, 0, 0)  # entries, then bytes written both ways
    body += objarray_bytes() * 3
    tables = b"\x01" + struct.pack(">ii", 40, 0)  # how many bytes each basket is
    tables += b"\x01" + struct.pack(">ii", 0, 5)  # the entry each of them starts at
    if wide:
        tables += b"\x02" + struct.pack(">qq", 128, 0)  # 2 says the seeks took 64 bits
    else:
        tables += b"\x01" + struct.pack(">ii", 128, 0)
    return record(version, body + tables + tstring(""))


def oldest_tree_bytes() -> bytes:
    """A ``TTree`` of the first version ROOT 5 wrote, with no branches."""
    body = named_bytes("t", "an old tree") + record(1) * 3
    body += struct.pack(">qqqq", 7, 0, 0, 0)  # entries, then bytes written three ways
    body += struct.pack(">iii", 0, 0, 0)  # timer interval, scan field, update
    body += struct.pack(">qqqqq", 0, 0, 0, 0, 0)  # the entry and size limits
    return record(6, body + objarray_bytes())


def oldest_branch_bytes() -> bytes:
    """A ``TBranch`` of the first version ROOT 5 wrote."""
    body = named_bytes("b", "b/I") + record(1)
    body += struct.pack(">iiii", 1, 4096, 0, 0)  # compression, basket size, offsets, baskets
    body += struct.pack(">q", 0)  # the entry the next basket would start at
    body += struct.pack(">iii", 0, 0, 0)  # offset in the parent, max baskets, split level
    body += struct.pack(">qqq", 5, 0, 0)  # entries, then bytes written both ways
    body += objarray_bytes() * 3
    return record(10, body + b"\x01" + b"\x01" + b"\x01" + tstring(""))


def basket_bytes(payload: bytes, *, nevsize: int, nevbuf: int, last: int) -> bytes:
    """A basket whose event size is written negative, so features follow it."""
    tail = tstring("TBasket") + tstring("b") + tstring("")
    fields = (
        struct.pack(">hii", 4, 4096, -nevsize)
        + record(1, b"\x00")
        + struct.pack(">ii", nevbuf, last)
        + b"\x00"
    )
    keylen = 18 + 8 + len(tail) + len(fields)
    head = struct.pack(">iHiIhh", keylen + len(payload), 4, len(payload), 0, keylen, 1)
    return head + struct.pack(">ii", 0, 0) + tail + fields + payload


#: How long the key in front of a crafted basket is, which its offsets count from.
BASKET_KEYLEN = 18 + 8 + len(tstring("TBasket") + tstring("b") + tstring(""))


def inline_basket_bytes(
    payload: bytes,
    *,
    flag: int,
    nevbuf: int = 2,
    version: int = 2,
    offsets: tuple[int, ...] = (),
    displaced: bool = False,
    nevsize: int = 2,
) -> bytes:
    """A basket written into its branch, with the bytes of its entries after it."""
    tail = tstring("TBasket") + tstring("b") + tstring("")
    last = BASKET_KEYLEN + len(payload)
    head = struct.pack(">iHiIhh", BASKET_KEYLEN, 4, 0, 0, BASKET_KEYLEN, 1)
    body = struct.pack(">hii", version, 4096, nevsize)
    if nevsize < 0:  # a negative size means feature bits follow
        body += record(1, b"\x00")
    body += struct.pack(">ii", nevbuf, last) + bytes([flag])
    if offsets:
        body += struct.pack(f">i{len(offsets)}i", len(offsets), *offsets)
    if displaced:
        body += struct.pack(">ii", 1, 7)
    if version <= 1:
        body += struct.pack(">i", last)  # the oldest baskets said how long they were
    return head + struct.pack(">ii", 0, 0) + tail + body + bytes(BASKET_KEYLEN) + payload


def source_over(data: bytes, name: str = "crafted") -> Source:
    return Source(io.BytesIO(data), name, owned=True)


# -- Buffer ---------------------------------------------------------------


def test_a_buffer_reads_every_width_of_number():
    buf = Buffer(struct.pack(">BbHhIiqd", 1, -1, 2, -2, 3, -3, -4, 0.5))
    assert (buf.u8(), buf.i8(), buf.u16(), buf.i16()) == (1, -1, 2, -2)
    assert (buf.u32(), buf.i32(), buf.i64(), buf.f64()) == (3, -3, -4, 0.5)
    assert buf.remaining == 0
    assert repr(buf) == "<Buffer at 30 of 30>"


def test_a_short_record_says_so_rather_than_reading_past_the_end():
    with pytest.raises(FormatError, match="ends 4 bytes before"):
        Buffer(b"\x00\x01").i32()
    with pytest.raises(FormatError, match="asked for 9 bytes"):
        Buffer(b"\x00").take(9)
    with pytest.raises(FormatError, match="array of 16 bytes"):
        Buffer(b"\x00").i64s(2)


def test_strings_come_in_both_lengths_ROOT_writes():
    long = "x" * 300
    buf = Buffer(tstring("short") + b"\xff" + struct.pack(">i", 300) + long.encode())
    assert buf.string() == "short"
    assert buf.string() == long


def test_a_class_name_without_its_terminator_is_a_format_error():
    assert Buffer(b"TTree\x00rest").cstring() == "TTree"
    with pytest.raises(FormatError, match="ran off the end"):
        Buffer(b"TTree").cstring()


def test_a_record_written_without_a_byte_count_can_be_read_but_not_skipped():
    buf = Buffer(struct.pack(">HH", 7, 0))
    assert buf.header() == (7, None)
    assert buf.pos == 2
    assert Buffer(record(3)).header() == (3, 6)
    with pytest.raises(FormatError, match="cannot be skipped"):
        Buffer(struct.pack(">HH", 7, 0)).skip_record()


def test_resuming_after_a_record_moves_only_when_there_is_somewhere_to_move():
    buf = Buffer(b"\x00\x00\x00\x00")
    buf.resume(None)
    assert buf.pos == 0
    buf.resume(3)
    assert buf.pos == 3


def test_a_referenced_object_carries_an_extra_word():
    plain = Buffer(struct.pack(">HII", 1, 7, 0))
    assert plain.tobject() == (7, 0)
    marked = Buffer(struct.pack(">HIIH", 1, 7, 1 << 4, 0))
    assert marked.tobject() == (7, 16)
    assert marked.remaining == 0


def test_a_named_object_gives_its_name_and_title():
    body = struct.pack(">HII", 1, 0, 0) + tstring("pt") + tstring("transverse momentum")
    buf = Buffer(record(1, body))
    assert buf.named() == ("pt", "transverse momentum")


def test_a_null_or_repeated_object_is_read_from_the_map_not_the_stream():
    assert Buffer(struct.pack(">I", 0)).any({}) is None
    buf = Buffer(struct.pack(">I", 40))
    buf.refs[40] = "seen before"
    assert buf.any({}) == "seen before"


def test_a_new_class_is_read_and_remembered_for_the_next_reference():
    body = b"Thing\x00" + b"\x07"
    stream = (
        struct.pack(">I", BYTE_COUNT_MASK | (4 + len(body)))
        + struct.pack(">I", NEW_CLASS_TAG)
        + body
    )
    buf = Buffer(stream)
    assert buf.any({"Thing": lambda b: b.u8()}) == 7
    assert buf.refs[4 + MAP_OFFSET] == "Thing"


def test_a_class_reference_that_points_nowhere_is_a_format_error():
    stream = struct.pack(">II", BYTE_COUNT_MASK | 4, CLASS_MASK | 999)
    with pytest.raises(FormatError, match="points nowhere"):
        Buffer(stream).any({})


def test_an_unknown_class_is_stepped_over_and_named():
    body = b"TH1F\x00" + b"junk"
    stream = (
        struct.pack(">I", BYTE_COUNT_MASK | (4 + len(body)))
        + struct.pack(">I", NEW_CLASS_TAG)
        + body
        + b"after"
    )
    buf = Buffer(stream)
    assert buf.any({}) == "TH1F"
    assert buf.take(5) == b"after"


def test_an_unknown_class_with_no_length_cannot_be_stepped_over():
    stream = struct.pack(">I", NEW_CLASS_TAG) + b"TH1F\x00"
    with pytest.raises(FormatError, match="no length to skip"):
        Buffer(stream).any({})


def test_an_object_array_yields_what_it_holds():
    item = struct.pack(">I", NEW_CLASS_TAG) + b"Num\x00" + b"\x05"
    body = struct.pack(">HII", 1, 0, 0) + tstring("") + struct.pack(">ii", 1, 0) + item
    buf = Buffer(record(3, body))
    assert buf.objarray({"Num": lambda b: b.u8()}) == [5]
    assert buf.remaining == 0


# -- compression ----------------------------------------------------------


def test_a_compressed_block_names_its_algorithm():
    assert algorithm(b"ZL\x08") == "zlib"
    assert algorithm(b"CS\x01") == "the pre-2005 ROOT algorithm"
    assert algorithm(b"??") == "an unknown algorithm"


def block(tag: bytes, payload: bytes, unpacked: int) -> bytes:
    return (
        tag
        + b"\x08"
        + len(payload).to_bytes(3, "little")
        + unpacked.to_bytes(3, "little")
        + payload
    )


def test_zlib_blocks_are_undone_one_after_another():
    first, second = b"hello ", b"world"
    data = block(b"ZL", zlib.compress(first), len(first)) + block(
        b"ZL", zlib.compress(second), len(second)
    )
    assert decompress(data, 11) == b"hello world"


def test_an_lz4_block_is_undone_after_its_checksum(monkeypatch):
    sequence = lz4_literals(b"physics")
    data = block(b"L4", b"\x00" * 8 + sequence, 7)
    assert decompress(data, 7) == b"physics"


def test_lzma_and_zstd_blocks_are_undone_too(monkeypatch):
    import lzma

    body = b"x" * 40
    assert decompress(block(b"XZ", lzma.compress(body), 40), 40) == body
    monkeypatch.setattr("xrd.root.compression._zstd", lambda blk, size: blk * size)
    assert decompress(block(b"ZS", b"ab", 2), 4) == b"abab"


def deflated(payload: bytes) -> bytes:
    """The deflate stream alone, which is what ROOT wrote before 2005."""
    maker = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return maker.compress(payload) + maker.flush()


def test_a_pre_2005_block_is_deflate_with_the_wrapper_left_off():
    body = b"a physics file from the last century" * 4
    data = block(b"CS", deflated(body), len(body))
    assert decompress(data, len(body)) == body


def test_a_pre_2005_block_that_will_not_inflate_says_so():
    with pytest.raises(FormatError, match="would not inflate"):
        decompress(block(b"CS", b"not deflate at all", 4), 4)


def test_a_pre_2005_block_of_the_wrong_length_is_not_handed_back():
    body = b"eight!!!"
    with pytest.raises(FormatError, match="gave 8 bytes where 4"):
        decompress(block(b"CS", deflated(body), 4), 8)


def test_a_compression_this_reader_does_not_undo_is_refused_by_name():
    with pytest.raises(UnsupportedFeatureError, match="an unknown algorithm"):
        decompress(block(b"QQ", b"xx", 2), 2)


def test_a_truncated_compressed_object_is_a_format_error():
    with pytest.raises(FormatError, match="ran out after 0 of 8"):
        decompress(b"ZL\x08\x00\x00", 8)
    with pytest.raises(FormatError, match="says it is 1 bytes and only 0"):
        decompress(block(b"ZL", b"x", 1)[:-1], 8)


def test_a_block_that_gives_more_than_promised_is_a_format_error():
    body = b"y" * 10
    with pytest.raises(FormatError, match="gave 10 bytes where 5"):
        decompress(block(b"ZL", zlib.compress(body), 10), 5)


def lz4_literals(payload: bytes) -> bytes:
    """An LZ4 sequence of literals only, which is how a block ends."""
    count = len(payload)
    if count < 15:
        return bytes([count << 4]) + payload
    token, count = bytes([0xF0]), count - 15
    while count >= 255:
        token += b"\xff"
        count -= 255
    return token + bytes([count]) + payload


def test_lz4_copies_literals_out_however_many_there_are():
    for size in (3, 15, 270, 600):
        payload = bytes(range(size % 251)) * (size // max(size % 251, 1))
        payload = (b"abc" * size)[:size]
        assert _lz4(lz4_literals(payload) + b"", size) == payload


def test_lz4_repeats_what_it_has_already_written():
    #: four literals, then a match of four at a distance of four
    data = bytes([0x40]) + b"abcd" + b"\x04\x00" + lz4_literals(b"!")
    assert _lz4(data, 9) == b"abcdabcd!"


def test_lz4_spells_a_run_as_a_match_that_overlaps_itself():
    data = bytes([0x1B]) + b"a" + b"\x01\x00" + lz4_literals(b"z")
    assert _lz4(data, 17) == b"a" * 16 + b"z"


def test_lz4_reads_a_long_match_length_the_way_it_reads_a_long_literal():
    data = bytes([0x1F]) + b"a" + b"\x01\x00" + b"\xff\x02" + lz4_literals(b"z")
    assert _lz4(data, 1 + 19 + 255 + 2 + 1) == b"a" * (1 + 19 + 255 + 2) + b"z"


def test_an_lz4_block_may_end_on_a_match():
    assert _lz4(bytes([0x10]) + b"a" + b"\x01\x00", 5) == b"aaaaa"


def test_an_lz4_match_cannot_point_before_the_block():
    with pytest.raises(FormatError, match="bytes before the block"):
        _lz4(bytes([0x10]) + b"a" + b"\x09\x00" + b"\x00", 12)


def test_an_lz4_block_that_stops_mid_sequence_is_a_format_error():
    with pytest.raises(FormatError, match="ends in the middle"):
        _lz4(bytes([0xF0]), 4)
    with pytest.raises(FormatError, match="gave 1 bytes where 9"):
        _lz4(lz4_literals(b"a"), 9)


# -- files, keys and directories ------------------------------------------


def test_a_file_that_is_not_a_root_file_says_which_it_is(tmp_path):
    page = tmp_path / "error.html"
    page.write_bytes(b"<html>404</html>" * 20)
    with pytest.raises(FormatError, match="is not a ROOT file"):
        open_root(str(page))


def test_a_truncated_file_says_where_it_ended(tmp_path):
    short = tmp_path / "cut.root"
    short.write_bytes((DATA / "simple.root").read_bytes()[:200])
    with pytest.raises(FormatError, match="the file is truncated"):
        open_root(str(short))


def test_a_file_object_is_read_as_it_is_and_left_for_whoever_opened_it():
    handle = (DATA / "simple.root").open("rb")
    with open_root(handle) as root:
        assert root.keys() == ["tree"]
        assert root.name.endswith("simple.root")
        assert "written by ROOT 60600" in repr(root)
    assert not handle.closed
    handle.close()


def test_a_worker_process_opens_the_file_again_rather_than_sharing_one_handle(monkeypatch):
    """Two processes on one handle seek over each other; the child reopens.

    Standing in for the fork is a process id that is not the one that opened
    the file, which is the only thing the reader can tell about a worker.
    """
    with open_root(str(DATA / "simple.root")) as root:
        source = root._source
        inherited = source.handle
        monkeypatch.setattr(os, "getpid", lambda: -1)
        assert root["tree"]["one"].array().tolist() == [1.0, 2.0, 3.0, 4.0]
        assert source.handle is not inherited
        assert not inherited.closed  # the parent's descriptor, not this one's
    inherited.close()


def test_a_file_object_cannot_be_read_from_a_process_that_did_not_open_it(monkeypatch):
    """Reopening needs a URL; a handle someone else opened has none."""
    with (DATA / "simple.root").open("rb") as handle, open_root(handle) as root:
        monkeypatch.setattr(os, "getpid", lambda: -1)
        with pytest.raises(UnsupportedFeatureError, match="opened from a file object"):
            root["tree"]["one"].array()


def test_a_tree_can_be_sent_to_another_process_and_read_there():
    """Pickling a tree sends the name of the file, not the open file."""
    with open_root(str(DATA / "simple.root")) as root:
        sent = pickle.dumps(root["tree"])
    tree = pickle.loads(sent)
    try:
        assert tree["one"].array().tolist() == [1.0, 2.0, 3.0, 4.0]
    finally:
        tree._source.close()


def test_a_file_object_cannot_be_sent_to_another_process():
    with (DATA / "simple.root").open("rb") as handle, open_root(handle) as root:
        with pytest.raises(UnsupportedFeatureError, match="cannot be sent to another process"):
            pickle.dumps(root["tree"])


def test_a_file_object_with_no_name_still_opens():
    data = (DATA / "simple.root").read_bytes()
    with open_root(io.BytesIO(data)) as root:
        assert root.trees() == ["tree"]


def test_a_key_says_what_it_holds_and_when_it_was_written():
    with opened("simple") as root:
        key = root._keys[0]
        assert (key.classname, key.name, key.cycle) == ("TTree", "tree", 1)
        assert repr(key) == "<Key 'tree';1 of class TTree>"
        assert key.time.year >= 1995
        assert key.compressed is (key.objlen != key.nbytes - key.keylen)


def test_a_directory_behaves_like_the_mapping_it_is():
    with opened("dirs-6.14.00") as root:
        assert list(root) == ["dir1", "dir2", "dir3"]
        assert len(root) == 3
        assert "dir1" in root and "nope" not in root
        assert root.classnames()["dir1"] == "TDirectory"
        assert root.get("nope") is None
        assert root.get("nope", "fallback") == "fallback"
        assert repr(root["dir1"]) == "<Directory dir1 with 1 keys>"
        assert root["dir1"].path == "dir1"
        assert root["dir1/dir11"].keys() == ["h1"]
        with pytest.raises(KeyError, match="is not in /"):
            root["missing"]


def test_a_directory_holding_no_tree_says_so_when_asked_for_one():
    with opened("dirs-6.14.00") as root:
        assert root.trees() == []
        with pytest.raises(KeyError, match="holds 0 trees"):
            root.tree()
        assert root.classnames() == dict.fromkeys(("dir1", "dir2", "dir3"), "TDirectory")


def keyed(payload: bytes, classname: str, classes: dict) -> Directory:
    """A directory of one crafted key, whose class the source describes as asked."""
    pad = 64
    data = bytes(pad) + key_bytes(classname, "thing", seek_key=pad, payload=payload)
    source = source_over(data)
    source._streamers = classes
    return Directory(source, [Key(Buffer(data[pad:]))])


def test_a_key_holding_an_object_the_file_describes_reads_as_a_dictionary():
    """`WriteObjectAny` puts one object in a key, and this is that object."""
    bookkeeping = {"fUniqueID": 0, "fBits": 50331648}
    with opened("tlv-split99") as root:
        assert root["tlv"] == {
            "TObject": bookkeeping,
            "fP": {"TObject": bookkeeping, "fX": 10.0, "fY": 20.0, "fZ": 30.0},
            "fE": 40.0,
        }


def test_a_key_holding_a_string_is_that_string():
    with opened("string-example") as root:
        summary = root["FileSummaryRecord"]
    assert summary.startswith('{"LumiCounter.eventsByRun"') and summary.endswith("}")


def test_a_tdatime_is_the_moment_it_stands_for():
    """`TDatime` streams one packed word and no record, and always has."""
    stamp = datetime.datetime(2006, 1, 2, 15, 4, 5)
    with opened("tdatime") as root:
        assert root["tda"] == stamp
        assert root["foo"]["d"] == stamp
        assert root["dat"] == {"d": stamp, "pad": array.array("b", b"12345\x00")}
        tree = root["tree"]
        assert tree.typenames()["b0"] == "datetime"
        assert tree["b0"].array() == [stamp, stamp.replace(day=3)]
        assert [row["d"] for row in tree["b3"].array()] == [stamp, stamp.replace(day=3)]


def test_a_key_of_a_class_the_file_says_nothing_about_is_refused_by_name():
    directory = keyed(b"", "Mystery", {})
    with pytest.raises(UnsupportedFeatureError, match="does not describe its layout"):
        directory["thing"]


def test_a_key_whose_bytes_run_out_early_is_a_class_that_streams_itself():
    described = {"Thing": {"n": Member("n", "", 3, "int", 1)}}
    directory = keyed(record(1, b"\x00\x00"), "Thing", described)
    with pytest.raises(UnsupportedFeatureError, match="are not laid out the way"):
        directory["thing"]


def test_a_key_with_bytes_left_over_is_a_class_that_streams_itself():
    described = {"Thing": {"n": Member("n", "", 3, "int", 1)}}
    directory = keyed(record(1, struct.pack(">i", 5)) + bytes(4), "Thing", described)
    with pytest.raises(UnsupportedFeatureError, match="left 4 bytes over"):
        directory["thing"]


# -- the objects ROOT's own kit writes ------------------------------------

#: What ROOT put in the ten bins of ``h1d``, and the sums of squared weights
#: beside them, as go-hep's own reader of the same file gives them back.
GAUSS = [6.6, 72.6, 543.4, 1708.3, 3130.6, 3136.1, 1753.4, 540.1, 101.2, 7.7]
GAUSS_W2 = [7.26, 79.86, 597.74, 1879.13, 3443.66, 3449.71, 1928.74, 594.11, 111.32, 8.47]


def test_a_histogram_is_its_bins_its_edges_and_its_axes():
    with opened("gauss-h1") as root:
        hist = root["h1d"]
        _assert_histogram_identity(hist)
        _assert_histogram_bins(hist)


def _assert_histogram_identity(hist):
    assert repr(hist) == "<TH1D 'h1d' of 10 bins, 10004 entries>"
    assert (hist.name, hist.title, hist.classname) == ("h1d", "h1d", "TH1D")
    assert (hist.shape, len(hist), hist.entries) == ((10,), 10, 10004.0)
    assert [round(value, 4) for value in hist.values()] == GAUSS
    assert [round(value, 4) for value in hist.errors()] == [
        round(math.sqrt(w2), 4) for w2 in GAUSS_W2
    ]


def _assert_histogram_bins(hist):
    # The two ROOT keeps at the ends are what the bins fell out of.
    assert list(hist.values(flow=True))[:1] == [2.0]
    assert round(hist.sum(flow=True) - hist.sum(), 4) == 6.0
    assert round(hist.sum(flow=True), 4) == 11006.0
    assert len(hist.edges()) == 11
    assert hist.edges()[0] == -4.0 and hist.edges()[-1] == 4.0
    assert hist.axes[0].centers()[0] == -3.6
    assert repr(hist.axes[0]) == "<Axis 'xaxis' of 10 bins from -4 to 4>"


def test_a_histogram_binned_unevenly_keeps_every_edge_it_was_written_with():
    """An evenly binned axis writes no edges at all: the two ends say where they are."""
    with opened("gauss-h1") as root:
        assert root["h1d"].axes[0]._edges == array.array("d")
        uneven = root["h1d-var"].axes[0]
        assert len(uneven._edges) == 11
        assert (uneven.low, uneven.high, len(uneven)) == (-4.0, 4.0, 10)
        # Written exactly, where working them out from the ends would drift.
        assert list(uneven.edges())[3] == -1.6
        assert -4.0 + 0.8 * 3 != -1.6


def test_a_two_dimensional_histogram_gives_a_row_of_bins_for_each_bin_along_x():
    with opened("gauss-h2") as root:
        for name in ("h2d", "h2f"):
            hist = root[name]
            assert hist.shape == (3, 3)
            assert len(hist) == 9
            assert [list(row) for row in hist.values()] == [
                [501.0, 488.0, 314.0],
                [385.0, 379.0, 221.0],
                [228.0, 232.0, 164.0],
            ]
            assert hist.sum() == 2912.0
            assert hist.errors()[0][0] == math.sqrt(501.0)
            assert len(hist.values(flow=True)) == 5
            assert len(hist.values(flow=True)[0]) == 5
            assert list(hist.edges(1)) == [0.0, 1.0, 2.0, 3.0]


def test_a_histogram_filled_without_weights_takes_its_error_from_the_count():
    """No sum of squared weights was kept, so the error on *n* counts is its root."""
    with opened("dirs-6.14.00") as root:
        hist = root["dir1/dir11/h1"]
        assert hist.members["TH1"]["fSumw2"] == array.array("d")
        assert list(hist.values())[:4] == [3.0, 1.0, 0.0, 1.0]
        assert list(hist.errors())[:4] == [math.sqrt(3.0), 1.0, 0.0, 1.0]
        assert (hist.sum(), hist.entries) == (5.0, 5.0)


def crafted_histogram(classname: str, bins: list[float], *shape: int) -> Histogram:
    """A histogram of any shape at all, without the file that would hold one."""
    axes = {
        f"f{letter}axis": {
            "TNamed": {"fName": f"{letter.lower()}axis", "fTitle": ""},
            "fNbins": count,
            "fXmin": 0.0,
            "fXmax": float(count),
            "fXbins": array.array("d"),
        }
        for letter, count in zip("XYZ", shape)
    }
    core = {
        "TNamed": {"fName": "h", "fTitle": ""},
        "fNcells": len(bins),
        "fEntries": 0.0,
        "fSumw2": [],
        **axes,
    }
    return Histogram(classname, {"TH1": core, "TArrayD": array.array("d", bins)})


def test_a_histogram_of_three_dimensions_nests_its_rows_once_more():
    hist = crafted_histogram("TH3D", [float(cell) for cell in range(3 * 3 * 4)], 1, 1, 2)
    assert hist.shape == (1, 1, 2)
    assert [[list(row) for row in plane] for plane in hist.values()] == [[[13.0, 22.0]]]
    assert hist.sum() == 35.0
    assert hist.values(flow=True)[0][0][0] == 0.0
    assert repr(hist) == "<TH3D 'h' of 1 x 1 x 2 bins, 0 entries>"


def test_a_histogram_without_its_bins_or_its_axes_is_not_one():
    with pytest.raises(FormatError, match="without its bins or its axes"):
        Histogram("TH1D", {"TArrayD": array.array("d", [1.0])})  # no axes to lie on
    with pytest.raises(FormatError, match="without its bins or its axes"):
        # The drawing attributes are searched through and are not it either.
        Histogram("TH1D", {"TAttLine": {"fLineColor": 1}, "TH1": {"fNcells": 1}})
    with pytest.raises(FormatError, match="holds only 3 values"):
        crafted_histogram("TH1D", [1.0, 2.0, 3.0], 3)


def test_a_graph_is_its_points_and_the_bars_round_them():
    with opened("graphs") as root:
        _assert_plain_graph(root["tg"])
        _assert_graph_errors(root["tge"], root["tgae"])


def _assert_plain_graph(plain):
    assert repr(plain) == "<TGraph 'tg' of 4 points>"
    assert (plain.name, plain.title) == ("tg", "graph without errors")
    assert (len(plain), plain[0], plain[-1]) == (4, (1.0, 2.0), (4.0, 8.0))
    assert plain.points() == [(1.0, 2.0), (2.0, 4.0), (3.0, 6.0), (4.0, 8.0)]
    assert list(plain.x) == [1.0, 2.0, 3.0, 4.0]
    assert (plain.xerr, plain.yerr) == (None, None)


def _assert_graph_errors(even, uneven):
    # The same bar both sides of the point, written once.
    assert even.yerr[0] == even.yerr[1]
    assert [round(bar, 4) for bar in even.yerr[0]] == [0.2, 0.4, 0.6, 0.8]
    assert even.members["TGraph"]["fNpoints"] == 4
    below, above = uneven.yerr
    assert [round(bar, 4) for bar in below] == [0.3, 0.6, 0.9, 1.2]
    assert [round(bar, 4) for bar in above] == [0.4, 0.8, 1.2, 1.6]
    assert [round(bar, 4) for bar in uneven.xerr[1]] == [0.2, 0.4, 0.6, 0.8]


def test_a_graph_without_its_points_is_not_one():
    with pytest.raises(FormatError, match="without its points"):
        Graph("TGraph", {"fNpoints": 2, "fX": array.array("d", [1.0, 2.0])})
    with pytest.raises(FormatError, match="without its points"):
        # The drawing attributes are searched through and are not it either.
        Graph("TGraph", {"TAttLine": {"fLineColor": 1}})
    short = {"fNpoints": 4, "fX": array.array("d", [1.0, 2.0]), "fY": array.array("d", [1.0, 2.0])}
    with pytest.raises(FormatError, match="of 4 points holds only 2"):
        Graph("TGraph", short)


def test_a_graph_with_only_one_side_of_its_bars_written_has_neither():
    """Half a pair is not a bar: what the other side would be is not knowable."""
    points = {
        "TNamed": {"fName": "g", "fTitle": ""},
        "fNpoints": 1,
        "fX": array.array("d", [1.0]),
        "fY": array.array("d", [2.0]),
    }
    graph = Graph("TGraphAsymmErrors", {"TGraph": points, "fEYlow": array.array("d", [0.5])})
    assert graph.yerr is None
    assert graph.xerr is None


def clones_bytes(
    *, version: int = 4, bits: int = 0, held: str = "Thing;1", slots: tuple[int, ...] = (1, 0)
) -> bytes:
    """A ``TClonesArray`` of a class whose objects are one byte each."""
    body = struct.pack(">HII", 1, 0, bits) if version > 2 else b""
    body += tstring("arr") if version > 1 else b""
    body += tstring(held) + struct.pack(">ii", len(slots), 0)
    for filled in slots:
        body += bytes([filled]) + (b"\x2a" if filled else b"")
    return record(version, body)


def test_an_array_of_one_class_names_that_class_once_at_the_front():
    with opened("tclonesarray-no-streamerbypass") as root:
        held = root["clones"]
        assert [one["fString"] for one in held] == ["Elem-0", "elem-1", "Elem-20"]

    #: A slot that was never filled is a byte saying so and nothing else.
    one = {"Thing": lambda buf: buf.u8()}
    assert Buffer(clones_bytes()).clones(one) == [42, None]
    assert Buffer(clones_bytes(version=1)).clones(one) == [42, None]


def test_an_array_of_numbers_standing_on_its_own_is_read_as_numbers():
    """A ``TArrayD`` key is a count and that many values, with no record round it."""
    payload = struct.pack(">i", 2) + struct.pack(">dd", 1.5, 2.5)
    assert keyed(payload, "TArrayD", {})["thing"] == array.array("d", [1.5, 2.5])


def test_an_array_of_one_class_written_field_by_field_reads_the_same_either_way():
    """The twin files hold the same three objects, one written each way."""
    with opened("tclonesarray-no-streamerbypass") as plain:
        with opened("tclonesarray-with-streamerbypass") as bypass:
            assert bypass["clones"] == plain["clones"]


def bypass_bytes(*, count: int = 2, tail: bytes = b"") -> bytes:
    """A ``TClonesArray`` whose objects were written field by field."""
    body = struct.pack(">HII", 1, 0, 1 << 12) + tstring("arr")
    body += tstring("Thing;1") + struct.pack(">ii", count, 0) + tail
    return record(4, body)


def test_an_array_written_field_by_field_reads_every_first_field_then_every_second():
    steps = [("a", lambda buf, row: buf.u8()), ("b", lambda buf, row: buf.u8())]
    held = Buffer(bypass_bytes(tail=bytes([1, 2, 3, 4]))).clones({}, lambda name: steps)
    assert held == [{"a": 1, "b": 3}, {"a": 2, "b": 4}]


def test_an_array_written_field_by_field_of_an_undescribed_class_is_refused():
    with pytest.raises(UnsupportedFeatureError, match="field by field, and this file"):
        Buffer(bypass_bytes()).clones({})
    with pytest.raises(UnsupportedFeatureError, match="describe Thing well enough"):
        Buffer(bypass_bytes()).clones({}, lambda name: None)


def test_an_array_written_field_by_field_that_ends_elsewhere_is_not_trusted():
    steps = [("a", lambda buf, row: buf.u8())]
    with pytest.raises(FormatError, match="cannot be trusted"):
        Buffer(bypass_bytes(tail=bytes([1, 2, 3]))).clones({}, lambda name: steps)


def test_an_array_of_a_class_this_file_does_not_describe_is_refused_by_name():
    with pytest.raises(UnsupportedFeatureError, match="holds Mystery, which this file"):
        Buffer(clones_bytes(held="Mystery;2")).clones({})


def test_an_object_pointed_at_rather_than_held_is_read_where_it_points():
    """A member that may be null carries the class it points at, or nothing."""
    with opened("streamers") as root:
        event = root["evt"]
        assert event["P3Ptr"] == {"Px": 0, "Py": 0.0, "Pz": 1}
        assert event["ObjStrPtr"]["fString"] == "obj-ptr-000"
        assert [point["Px"] for point in event["ArrayP3s"]] == list(range(10))
        assert [held["fString"] for held in event["ArrayObjStr"]][:2] == ["obj-000", "obj-001"]
        assert event["StdStr"] == "std-000"


def test_an_array_of_objects_that_streams_itself_is_read_by_its_own_kind():
    """A ``TObjArray`` member is written the way the array itself says, not the class."""
    with opened("tconfidence-level") as root:
        source = root["dsrc"]
        assert source["fSignal"] == []
        assert list(source) == [
            "TObject",
            "fSignal",
            "fBackground",
            "fCandidates",
            "fErrorOnSignal",
            "fErrorOnBackground",
            "fIds",
            "fDummyTA",
            "fDummyIds",
        ]


def test_a_class_the_file_describes_as_having_no_members_reads_as_nothing():
    """``TLimit`` computes and keeps nothing; the file says so, and so does this."""
    with opened("tconfidence-level") as root:
        assert root["limit"] == {}


def test_a_container_of_pairs_is_a_list_of_tuples():
    """gen-tconflvl.go set bins 1 and 2 of ``fBeta_bin_params`` and left the rest."""
    with opened("tconfidence-level") as root:
        eff = root["eff"]
        params = eff["fBeta_bin_params"]
        assert len(params) == 22  # twenty bins and the flow either side
        assert params[:3] == [(1.0, 1.0), (1.0, 2.0), (2.0, 3.0)]
        assert set(params[3:]) == {(1.0, 1.0)}
        assert eff["fPassedHistogram"].name == "eff_passed"  # a histogram anywhere is one


def test_a_container_of_objects_is_read_one_object_after_another():
    """``fFunctions`` is a ``vector<TF1*>``: a count, then each object named."""
    with opened("tformula") as root:
        summed = root["fnorm"]
        assert [held["TNamed"]["fName"] for held in summed["fFunctions"]] == ["func1", "func2"]
        assert list(summed["fCoeffs"]) == [10.0, 20.0]
        assert summed["fParNames"] == ["Coeff0", "Coeff1", "p01", "p11", "p02", "p12"]


def test_a_member_of_a_class_this_reader_cannot_walk_comes_back_as_its_name():
    """Stepping over it by the length in front of it beats guessing at its shape."""
    with opened("tformula") as root:
        formula = root["func1"]
        assert formula["fFormula"] == "TFormula"
        assert formula["fComposition"] is None  # a null pointer is nothing at all
        assert list(formula["fParErrors"]) == [0.0, 0.0]


def test_a_container_written_field_by_field_is_read_a_field_at_a_time():
    """Every value gen-tgme.go wrote with C++ ROOT, including the member-wise ones."""
    with opened("tgme") as root:
        gme = root["gme"]
        assert list(gme) == [(0.0, 0.0), (1.0, 2.0), (2.0, 4.0), (3.0, 1.0), (4.0, 3.0)]
        low, high = gme.xerr
        assert list(low) == [0.3] * 5 == list(high)
        stat, syst = gme.layers
        assert [list(side) for side in stat] == [[1, 0.5, 1, 0.5, 1], [0.5, 1, 0.5, 1, 2]]
        assert [list(side) for side in syst] == [
            [0.5, 0.4, 0.8, 0.3, 1.2],
            [0.6, 0.7, 0.6, 0.4, 0.8],
        ]
        #: ``fAttLine`` is a ``vector<TAttLine>``, written all colors then all styles.
        assert [line["fLineColor"] for line in gme.members["fAttLine"]] == [632, 600]


def test_a_graph_of_layered_errors_will_not_pick_a_layer_for_you():
    with opened("tgme") as root:
        gme = root["gme"]
        with pytest.raises(UnsupportedFeatureError, match="2 layers"):
            assert gme.yerr


def test_the_layers_of_an_ordinary_graph_are_its_bars_or_nothing():
    with opened("graphs") as root:
        assert root["tg"].layers == ()
        tge = root["tge"]
        assert tge.layers == (tge.yerr,)


def test_a_key_of_a_class_holding_what_cannot_be_walked_is_refused_by_member():
    classes = {"Widget": {"w": Member("w", "", 999, "Mystery", 1, "")}}
    with pytest.raises(UnsupportedFeatureError, match="it holds 'w'"):
        keyed(record(1), "Widget", classes)["thing"]


def test_a_container_of_a_described_class_reads_field_by_field_too():
    """The class version leads, and one written without a version leaves a checksum."""
    steps = [("a", lambda buf, row: buf.u8()), ("b", lambda buf, row: buf.u8())]
    read = _objects(lambda buf: {}, steps)
    versioned = struct.pack(">hi", 2, 2) + bytes([1, 2, 3, 4])
    summed = struct.pack(">hIi", 0, 0xD00DAD, 2) + bytes([1, 2, 3, 4])
    for body in (versioned, summed):
        rows = read(Buffer(record(MEMBER_WISE | 1, body)))
        assert rows == [{"a": 1, "b": 3}, {"a": 2, "b": 4}]


def test_a_container_read_field_by_field_that_ends_elsewhere_is_not_trusted():
    steps = [("a", lambda buf, row: buf.u8())]
    body = struct.pack(">hi", 2, 2) + bytes([1, 2, 3])
    with pytest.raises(FormatError, match="cannot be trusted"):
        _objects(lambda buf: {}, steps)(Buffer(record(MEMBER_WISE | 1, body)))


def test_a_container_of_pointers_written_field_by_field_is_refused():
    with pytest.raises(UnsupportedFeatureError, match="container of pointers"):
        _objects(lambda buf: {})(Buffer(record(MEMBER_WISE | 1)))


def test_a_container_of_pairs_written_pair_by_pair_is_refused():
    read = _sequence(parse("pair<char,char>"))
    pairs = struct.pack(">hI", 1, 2) + bytes([1, 2, 3, 4])
    assert read(Buffer(record(MEMBER_WISE | 9, pairs))) == [(1, 3), (2, 4)]
    with pytest.raises(UnsupportedFeatureError, match="pair by pair"):
        read(Buffer(record(9)))
    body = struct.pack(">hI", 1, 2) + bytes([1, 2, 3, 4, 5])
    with pytest.raises(FormatError, match="cannot be trusted"):
        read(Buffer(record(MEMBER_WISE | 9, body)))


def test_a_graph_or_histogram_inside_another_object_is_still_one():
    """A ``TMultiGraph`` is not wrapped, but everything it holds comes back dressed."""
    with opened("tgme") as root:
        multi = root["mg"]
        held = multi["fGraphs"]
        assert [one.classname for one in held] == ["TGraph", "TGraphErrors", "TGraphAsymmErrors"]
        assert len(held[0]) == 5
        assert multi["fHistogram"].classname == "TH1F"  # the frame its fit drew


def test_a_class_the_file_says_nothing_about_is_stepped_over_inside_a_list():
    """A list holds whatever it holds, and one class of it being a mystery is not fatal."""
    described = _Described(source_over(b""), ())
    assert described.get("Nothing") is None
    assert described.get("Nothing") is None  # asked once, remembered after that


def test_a_list_older_than_any_that_names_itself_is_refused():
    with pytest.raises(FormatError, match="version 3 is older"):
        Buffer(record(3)).tlist({})


def test_a_container_of_a_container_of_objects_is_not_a_shape_this_reads():
    assert _class_held("vector<TArrayD>") == ("TArrayD", False)
    assert _class_held("std::vector<TF1*>") == ("TF1", True)
    assert _class_held("vector<vector<TArrayD> >") is None
    assert _class_held("vector<>") is None
    assert _class_held("map<int,TArrayD>") is None
    assert _class_held("TArrayD") is None


def test_the_only_tree_in_a_file_opens_without_being_named():
    with opened("simple") as root:
        assert isinstance(root.tree(), TTree)
        assert root.tree("tree").name == "tree"


def test_an_older_cycle_is_still_reachable_by_name():
    with opened("simple") as root:
        keys = root._keys
        newer = Key(Buffer(key_bytes("TTree", "tree")))
        newer.cycle = 2
        root._keys = [newer, keys[0]]
        assert root._key("tree").cycle == 2
        assert root._key("tree;1").cycle == 1
        assert root.keys() == ["tree"]
        with pytest.raises(KeyError):
            root._key("tree;9")


def test_a_file_written_with_64_bit_seeks_reads_the_same_way(tmp_path):
    path = tmp_path / "wide.root"
    path.write_bytes(wide_file())
    with open_root(str(path)) as root:
        assert root.version == 4
        assert root.keys() == ["h"]
        assert root.classnames() == {"h": "TH1F"}


def test_a_directory_with_no_key_list_is_refused():
    with pytest.raises(FormatError, match="no key list"):
        _directory_record(source_over(wide_file(seek_keys_broken=True)[132:]), 0)


# -- trees, branches and leaves -------------------------------------------


def test_a_flat_tree_reads_every_column_ROOT_can_write(flat):
    assert len(flat) == 100
    assert flat.typenames()["UInt64"] == "uint64"
    assert flat["Int32"].array(0, 4).tolist() == [0, 1, 2, 3]
    assert flat["Float64"].array(0, 2).tolist() == [0.0, 1.0]
    assert flat["Str"].array(0, 2) == ["evt-000", "evt-001"]
    assert flat["ArrayInt32"].length == 10
    assert flat["ArrayInt32"].array(1, 2).tolist() == [1] * 10


def test_a_variable_length_column_keeps_its_rows(flat):
    jets = flat["SliceInt32"].array(0, 4)
    assert isinstance(jets, Jagged)
    assert jets.tolist() == [[], [1], [2, 2], [3, 3, 3]]
    assert jets.lengths() == [0, 1, 2, 3]
    assert flat["SliceInt32"].is_jagged
    assert not flat["Int32"].is_jagged


def test_a_tree_describes_itself_before_anything_is_read(simple):
    _assert_tree_description(simple)
    _assert_branch_description(simple["two"])
    with pytest.raises(KeyError, match="there is one, two, three"):
        simple["four"]


def _assert_tree_description(simple):
    assert repr(simple) == "<TTree 'tree' with 3 branches and 4 entries>"
    assert simple.keys() == ["one", "two", "three"]
    assert simple.readable() == ["one", "two", "three"]
    assert list(simple) == simple.keys()
    assert "one" in simple and "four" not in simple
    assert simple.show().splitlines()[0].startswith("one")


def _assert_branch_description(branch):
    assert repr(branch) == "<Branch 'two' of float32>"
    assert branch.title == "two"
    assert len(branch) == 4
    assert branch.num_baskets == 1


def test_a_tree_reads_several_columns_over_the_same_entries(simple):
    assert simple.arrays(["one"], 1, 3)["one"].tolist() == [2, 3]
    assert simple.arrays()["three"] == ["uno", "dos", "tres", "quatro"]


def test_negative_entry_numbers_count_from_the_end(simple):
    assert simple["one"].array(-2).tolist() == [3, 4]
    assert simple["one"].array(0, -2).tolist() == [1, 2]
    assert simple["one"].array(-99, 99).tolist() == [1, 2, 3, 4]


def test_iterating_gives_batches_and_refuses_a_step_of_nothing(simple):
    batches = list(simple.iterate(["one"], step=3))
    assert [b["one"].tolist() for b in batches] == [[1, 2, 3], [4]]
    assert list(simple.iterate(["one"], step=3, entry_start=1, entry_stop=99)) == [
        {"one": array.array("i", [2, 3, 4])}
    ]
    with pytest.raises(ValueError, match="at least one entry"):
        list(simple.iterate(step=0))


def test_a_column_spanning_two_baskets_is_read_from_both():
    with opened("pod-advanced") as root:
        branch = root["orange"]["orange.Evtake_iwant"]
        assert branch.num_baskets == 2
        assert branch.record.basket_entry == [0, 77, 100]
        assert branch.array(75, 80).tolist() == [75, 76, 77, 78, 79]
        assert branch.array(0, 5).tolist() == [0, 1, 2, 3, 4]  # the second basket is not read
        assert len(branch.array()) == 100


def test_the_last_basket_read_is_kept_for_the_next_call():
    with opened("pod-advanced") as root:
        branch = root["orange"]["orange.Evtake_iwant"]
        first = branch.basket(0)
        assert branch.basket(0) is first
        assert branch.basket(1) is not first
        assert branch.basket(0) is not first


def test_a_branch_holding_several_leaves_is_named_once_per_leaf():
    with opened("padding") as root:
        tree = root["tree"]
        assert tree.keys()[:3] == ["pad.x1", "pad.x2", "pad.x3"]
        assert tree["pad.x2"].array(0, 2).tolist() == [548655054794, 72058142692982730]
        assert tree["nop.x2"].array(0, 2).tolist() == [0, 1]


def test_an_ntuple_is_a_tree_with_something_after_it():
    with opened("tntuple") as root:
        tree = root["ntup"]
        assert tree.keys() == ["x", "y"]
        assert len(tree["x"].array()) == 10


def test_a_column_this_reader_cannot_decode_is_named_with_the_reason():
    record, leaf = BranchRecord(), LeafRecord("TLeafObject")
    record.name = leaf.name = "obj"
    branch = Branch("obj", record, leaf, Refused("a shape no file has written"), None)
    assert branch.typename is None
    with pytest.raises(UnsupportedFeatureError, match="unreadable lists every column"):
        branch.array()


def test_a_vector_member_of_a_split_object_is_read_as_rows():
    with opened("embedded-std-vector") as root:
        tree = root["modules"]
        assert tree.unreadable == {}
        assert tree.typenames() == {"hits_n": "int32", "hits_time_mc": "float32"}
        assert tree["hits_time_mc"].is_jagged
        assert tree["hits_time_mc"].array(0, 2).lengths() == [10, 11]
        assert " variable" in tree.show()


def test_a_split_object_shows_every_sub_branch_and_reads_the_maps():
    with opened("std-map-split1") as root:
        tree = root["tree"]
        assert len(tree.keys()) == 6
        assert tree.unreadable == {}
        assert tree.groups() == ["evt"]  # the object the five maps were split out of
        # What go-hep reads out of entry 1 of this same file, map for map.
        assert tree["mi32"].array(1, 2) == [{0: 0}]
        assert tree["msi32"].array(1, 2) == [{"key-000": 0}]
        assert tree["mss"].array(1, 2) == [{"key-000": "val-000"}]
        assert tree["msvs"].array(1, 2) == [{"key-000": ["val-000", "val-001", "val-002"]}]
        assert tree["msvi32"].array(1, 2) == [{"key-000": array.array("i", [1, 0, 3, 0])}]


def test_a_double32_leaf_is_unpacked_by_the_recipe_in_its_title():
    with opened("leaves") as root:
        tree = root["tree"]
        assert tree.unreadable == {}
        assert tree["D16"].typename == tree["D32"].typename == "float64"
        assert list(tree["D16"].array()) == [float(n) for n in range(10)]
        assert list(tree["D32"].array()) == [float(n) for n in range(10)]
        assert tree["ArrD16"].array(3, 4).tolist() == [3.0] * 10
        assert tree["SliD32"].array(4, 5).tolist() == [[4.0] * 4]
        assert tree["U8"].typename == "uint8"
        assert tree["G64"].typename == "int64"
        assert tree["ArrU32"].array(0, 1).tolist() == [0] * 10


def test_a_leaf_class_nobody_has_heard_of_says_that_plainly():
    assert "not a kind of leaf" in LeafRecord("TLeafQuantum").reason
    assert LeafRecord("TLeafQuantum").typename is None
    assert repr(LeafRecord("TLeafI")) == "<LeafRecord '' of class TLeafI>"


def test_a_variable_leaf_sharing_a_branch_with_others_is_refused():
    record = BranchRecord()
    record.name = "shared"
    first, second = LeafRecord("TLeafI"), LeafRecord("TLeafI")
    first.count = second
    record.leaves = [first, second]
    branch = Branch("shared.a", record, first, build(record, first, None), source_over(b""))
    assert repr(record) == "<BranchRecord 'shared' with 0 baskets>"
    with pytest.raises(UnsupportedFeatureError, match="sharing a branch with 1 others"):
        branch.array()


def test_a_branch_record_walks_the_branches_beneath_it():
    with opened("std-map-split1") as root:
        tree = root["tree"]
        top = tree["evt"].record
        assert len(list(top.walk())) >= 1


def test_a_tree_and_branch_from_2007_are_read_with_the_fields_of_their_day():
    tree = read_tree(Buffer(oldest_tree_bytes()), source_over(b""), "t")
    assert (tree.name, tree.title, len(tree)) == ("t", "an old tree", 7)
    assert tree.branches == {}
    branch = read_branch(Buffer(oldest_branch_bytes()))
    assert (branch.name, branch.entries, branch.first_entry) == ("b", 5, 0)
    assert branch.basket_seek == []


def test_a_tree_written_by_ROOT_4_reads_the_values_it_was_given():
    """`g4-like.root` is Geant4-shaped output written by ROOT 4 in 2005.

    Its tree was never flushed, so every basket it has is inside the branch
    record rather than out in the file, and the seek table is all zeroes. The
    values are the ones go-hep's own test of this file expects: the entry
    number and up to that many doubles counting on from it.
    """
    with opened("g4-like") as handle:
        tree = handle["mytree"]
        assert (len(tree), tree.unreadable) == (5, {})
        assert tree.typenames() == {"i32": "int32", "f64": "float64", "slif64": "float64"}
        assert tree["i32"].array().tolist() == [1, 2, 3, 4, 5]
        assert tree["f64"].array().tolist() == [1.0, 2.0, 3.0, 4.0, 5.0]
        assert tree["slif64"].array().tolist() == [
            [],
            [1.0],
            [2.0, 3.0],
            [3.0, 4.0, 5.0],
            [4.0, 5.0, 6.0, 7.0],
        ]
        assert tree["slif64"].array(3, 4).tolist() == [[3.0, 4.0, 5.0]]


def test_a_tree_and_branch_from_ROOT_4_are_read_with_the_narrower_counters():
    tree = read_tree(Buffer(root4_tree_bytes()), source_over(b""), "t")
    assert (tree.name, tree.title, len(tree)) == ("t", "a ROOT 4 tree", 7)
    for version in (6, 7, 8, 9):
        branch = read_branch(Buffer(root4_branch_bytes(version)))
        assert (branch.name, branch.entries, branch.first_entry) == ("b", 5, 0)
        assert (branch.basket_bytes, branch.basket_entry, branch.basket_seek) == (
            [40],
            [0, 5],
            [128],
        )
    assert read_branch(Buffer(root4_branch_bytes(wide=True))).basket_seek == [128]


def test_a_tree_from_before_this_reader_is_refused_by_version():
    with pytest.raises(UnsupportedFeatureError, match="TTree version 4"):
        read_tree(Buffer(record(4)), None, "old")
    with pytest.raises(UnsupportedFeatureError, match="TTree version 3"):
        read_tree(Buffer(record(1) + record(3)), None, "old", "TNtuple")
    with pytest.raises(UnsupportedFeatureError, match="TBranch version 5"):
        read_branch(Buffer(record(5)))


def test_a_basket_that_writes_its_size_negative_carries_feature_bits():
    raw = basket_bytes(b"0123456789", nevsize=2, nevbuf=5, last=0)
    basket = Basket.keyed(source_over(raw), 0, len(raw), False)
    assert basket.nevsize == 2
    assert basket.nevbuf == 5
    assert basket.data == b"0123456789"
    assert basket.start_of(3, 0) == 6


def test_a_basket_written_into_its_branch_holds_its_entries_where_it_stands():
    """The flag in front of one says which of its parts were written down.

    A tree small enough never to have flushed is the only place these turn
    up, and then the values are in the branch record itself rather than at a
    seek point of their own.
    """
    places = (BASKET_KEYLEN, BASKET_KEYLEN + 2)
    plain = Basket.inline(Buffer(inline_basket_bytes(b"0123", flag=11, offsets=places)))
    assert (plain.data, plain.offsets) == (b"0123", list(places))
    assert (plain.start_of(1, 0), plain.end_of(1)) == (2, 4)

    high = tuple(0x7F000000 | at for at in places)  # a top byte that is a displacement
    displaced = Basket.inline(
        Buffer(inline_basket_bytes(b"0123", flag=51, offsets=high, displaced=True))
    )
    assert displaced.offsets == list(high)  # a flag past 40 keeps them as they are
    assert Basket.inline(Buffer(inline_basket_bytes(b"0123", flag=31, offsets=high))).offsets == [
        at & 0xFFFFFF for at in high
    ]

    dropped = Basket.inline(Buffer(inline_basket_bytes(b"0123", flag=91, nevsize=-2)))
    assert (dropped.offsets, dropped.nevsize, dropped.start_of(1, 0)) == ([], 2, 2)
    empty = Basket.inline(Buffer(inline_basket_bytes(b"0123", flag=11, nevbuf=0)))
    assert (empty.offsets, empty.data) == ([], b"0123")
    oldest = Basket.inline(Buffer(inline_basket_bytes(b"0123", flag=1, offsets=places, version=1)))
    assert oldest.data == b"0123"


def test_a_basket_that_kept_no_bytes_leaves_the_branch_to_read_them():
    assert Basket.inline(Buffer(inline_basket_bytes(b"", flag=2))) is None


# -- jagged ---------------------------------------------------------------


def jagged(rows: list[list[float]]) -> Jagged:
    content = array.array("d", [value for row in rows for value in row])
    offsets = array.array("q", [0])
    for row in rows:
        offsets.append(offsets[-1] + len(row))
    return Jagged(content, offsets)


def test_jagged_rows_index_like_a_sequence():
    rows = jagged([[1.0, 2.0], [], [3.0]])
    assert len(rows) == 3
    assert rows[0].tolist() == [1.0, 2.0]
    assert rows[-1].tolist() == [3.0]
    assert [row.tolist() for row in rows[0:2]] == [[1.0, 2.0], []]
    assert rows.tolist() == [[1.0, 2.0], [], [3.0]]
    assert repr(rows) == "<Jagged 3 rows of 3 d values>"
    with pytest.raises(IndexError, match="row out of range"):
        rows[7]


def test_jagged_rows_pad_to_a_rectangle():
    rows = jagged([[1.0, 2.0], [], [3.0]])
    values, width = rows.padded()
    assert width == 2
    assert values.tolist() == [1.0, 2.0, 0.0, 0.0, 3.0, 0.0]
    cut, width = rows.padded(1, fill=-1.0)
    assert (width, cut.tolist()) == (1, [1.0, -1.0, 3.0])


def test_padding_an_integer_column_keeps_it_an_integer_column():
    rows = Jagged(array.array("i", [1]), array.array("q", [0, 1, 1]))
    values, _width = rows.padded(fill=0.0)
    assert values.tolist() == [1, 0]
    assert values.typecode == "i"


# -- over the network -----------------------------------------------------


def test_a_tree_reads_over_root_without_ever_being_downloaded(config):
    from xrd.testing import FakeServer

    data = (DATA / "small-flat-tree.root").read_bytes()
    with FakeServer(files={"/data/flat.root": data}) as server:
        with open_root(server.url.with_path("/data/flat.root"), config=config) as root:
            tree = root["tree"]
            assert tree.num_entries == 100
            assert tree["Int32"].array(90, 95).tolist() == [90, 91, 92, 93, 94]
            assert tree["SliceFloat64"].array(3, 4).tolist() == [[3.0, 3.0, 3.0]]


def test_a_directory_can_be_built_over_any_source():
    root = Directory(source_over(b""), [], "sub")
    assert list(root) == []
    assert root.get("anything") is None
