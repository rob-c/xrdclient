"""Writing ROOT files in pure Python.

Nothing here compares against bytes this library wrote for itself - that
would only prove it agrees with its own mistakes. What is asserted instead:
histograms and graphs written by real ROOT survive a round trip through the
writer and come back value for value; a written file describes its classes
exactly as the donor files do; and the XXH64 this writer stores in front of
an LZ4 block matches checksums ROOT itself computed, kept in a donor file.
"""

from __future__ import annotations

import array
import hashlib
import io
import math
import pathlib
import struct

import pytest

from xrd.root import (
    Graph,
    Histogram,
    UnsupportedFeatureError,
    create,
    open_root,
)
from xrd.root.compression import (
    BLOCK,
    _lz4,
    _lz4_pack,
    _xxh64,
    _zstd_pack,
    compress,
    decompress,
)
from xrd.root.interp import OFFSET_L, OFFSET_P
from xrd.root.winfo import INFOS, WRITER_VERSION
from xrd.root.writer import WBuffer, _closure, _element, _find, _list, _record

DATA = pathlib.Path(__file__).parent / "data"

try:
    _zstd_pack(b"", 3)
    HAVE_ZSTD = True
except UnsupportedFeatureError:
    HAVE_ZSTD = False


def opened(name: str):
    return open_root(str(DATA / f"{name}.root"))


def written(**objects) -> bytes:
    """A file holding ``objects``, written to memory with the defaults."""
    buf = io.BytesIO()
    with create(buf) as out:
        for name, obj in objects.items():
            out[name] = obj
    return buf.getvalue()


def read_back(data: bytes):
    return open_root(io.BytesIO(data))


def chain(count: int) -> bytes:
    """``count`` SHA-256 digests chained: bytes with no repeats to match on."""
    out, block = bytearray(), b"seed"
    for _ in range(count):
        block = hashlib.sha256(block).digest()
        out += block
    return bytes(out)


# -- round trips through files real ROOT wrote -----------------------------


def test_every_donor_histogram_survives_a_round_trip():
    for stem, names in (
        ("gauss-h1", ("h1d", "h1f", "h1d-var", "h1f-var")),
        ("gauss-h2", ("h2f", "h2d", "h2f-var", "h2d-var")),
    ):
        with opened(stem) as donor:
            originals = {name: donor[name] for name in names}
        with read_back(written(**originals)) as back:
            for name, hist in originals.items():
                _assert_histogram_round_trip(back[name], hist)


def _assert_histogram_round_trip(again, hist):
    assert again.classname == hist.classname
    assert again.title == hist.title
    assert again.entries == hist.entries
    assert list(again.values(flow=True)) == list(hist.values(flow=True))
    assert list(again.errors(flow=True)) == list(hist.errors(flow=True))
    for axis in range(len(hist.shape)):
        assert list(again.edges(axis)) == list(hist.edges(axis))


def test_every_donor_graph_survives_a_round_trip():
    with opened("graphs") as donor:
        originals = {name: donor[name] for name in donor.keys()}
    with read_back(written(**originals)) as back:
        for name, graph in originals.items():
            again = back[name]
            assert again.classname == graph.classname
            assert again.title == graph.title
            assert (again.x, again.y) == (graph.x, graph.y)
            assert again.xerr == graph.xerr
            assert again.layers == graph.layers


def test_a_written_file_describes_its_classes_exactly_as_the_donors_do():
    with opened("gauss-h1") as donor:
        theirs = dict(donor._source.streamers())
        hist = donor["h1d"]
    with opened("graphs") as donor:
        theirs.update(donor._source.streamers())
        graph = donor["tgae"]
    with read_back(written(h=hist, g=graph)) as back:
        ours = back._source.streamers()
    assert len(ours) >= 14
    for classname, members in ours.items():
        for name, member in members.items():
            other = theirs[classname][name]
            assert (
                member.name,
                member.title,
                member.stype,
                member.typename,
                member.length,
                member.count,
            ) == (other.name, other.title, other.stype, other.typename, other.length, other.count)


def test_the_header_says_what_root_would_say():
    data = written(s="hello")
    assert data[:4] == b"root"
    assert struct.unpack_from(">ii", data, 4) == (WRITER_VERSION, 100)
    end, seek_free, nbytes_free, nfree, _nname, units, codes, seek_info, nbytes_info = (
        struct.unpack_from(">iiiiiBiii", data, 12)
    )
    assert end == len(data)
    assert (nfree, units, codes) == (1, 4, 106)
    assert 100 < seek_info < seek_free < end
    assert nbytes_free == 34 + len("<file>") + 10  # the free record's key, then its 10 bytes
    assert nbytes_info > 0
    assert struct.unpack_from(">H", data, 45)[0] == 1  # the UUID's version word


# -- what else a file can hold ---------------------------------------------


def test_strings_round_trip_including_one_too_long_for_a_short_length():
    with read_back(written(s="hello", long="x" * 300, empty="")) as back:
        assert back["s"] == "hello"
        assert back["long"] == "x" * 300
        assert back["empty"] == ""
        assert back.classnames()["s"] == "string"


def test_every_array_typecode_with_a_root_class_round_trips():
    arrays = {
        "ab": array.array("b", [-1, 2]),
        "ah": array.array("h", [-300, 300]),
        "ai": array.array("i", [-70000, 70000]),
        "al": array.array("l", [-5, 5]),
        "aq": array.array("q", [-(2**40), 2**40]),
        "af": array.array("f", [1.5, -2.5]),
        "ad": array.array("d", [math.pi, -math.e]),
    }
    with read_back(written(**arrays)) as back:
        for name, values in arrays.items():
            assert list(back[name]) == list(values)
        wide = arrays["al"].itemsize == 8
        assert back.classnames()["al"] == ("TArrayL64" if wide else "TArrayI")


def test_an_unsigned_array_is_refused_because_root_has_no_class_for_it():
    buf = io.BytesIO()
    with create(buf) as out:
        with pytest.raises(UnsupportedFeatureError, match="has no ROOT class"):
            out["u"] = array.array("I", [1, 2])


def test_a_name_written_twice_becomes_a_second_cycle_and_the_newest_wins():
    buf = io.BytesIO()
    with create(buf) as out:
        out["s"] = "first"
        out["s"] = "second"
    with read_back(buf.getvalue()) as back:
        assert back.keys() == ["s"]
        assert back["s"] == "second"
        assert back["s;1"] == "first"
        assert back["s;2"] == "second"


def test_a_histogram_hung_behind_a_graph_pointer_is_written_in_place():
    graph = Graph.new("g", [1.0, 2.0], [3.0, 4.0])
    graph.members["fHistogram"] = Histogram.new("frame", [0, 1, 2], [5, 7])
    with read_back(written(g=graph)) as back:
        frame = back["g"].members["fHistogram"]
        assert isinstance(frame, Histogram)  # described, not skipped over
        assert list(frame.values()) == [5.0, 7.0]


def test_a_plain_dict_behind_a_pointer_is_written_as_the_class_the_pointer_names():
    with opened("gauss-h1") as donor:
        members = dict(donor["h1f"].members)
        values = list(donor["h1f"].values())
    graph = Graph.new("g", [1.0, 2.0], [3.0, 4.0])
    graph.members["fHistogram"] = members  # bare members, no Histogram around them
    with read_back(written(g=graph)) as back:
        frame = back["g"].members["fHistogram"]
        assert isinstance(frame, Histogram)
        assert frame.classname == "TH1F"  # the class the pointer declares
        assert list(frame.values()) == values


# -- building new objects from plain numbers --------------------------------


def test_a_new_histogram_keeps_its_values_edges_and_title():
    hist = Histogram.new("counts", [0, 1, 2, 4], [5, 3, 1], title="what was seen")
    assert (hist.classname, hist.name, hist.title) == ("TH1D", "counts", "what was seen")
    assert list(hist.values()) == [5.0, 3.0, 1.0]
    assert list(hist.values(flow=True)) == [0.0, 5.0, 3.0, 1.0, 0.0]
    assert hist.entries == 9.0
    assert list(hist.edges()) == [0.0, 1.0, 2.0, 4.0]
    with read_back(written(counts=hist)) as back:
        again = back["counts"]
        assert list(again.values()) == [5.0, 3.0, 1.0]
        assert list(again.edges()) == [0.0, 1.0, 2.0, 4.0]
        assert again.title == "what was seen"


def test_even_edges_are_stored_the_compact_way_and_uneven_ones_kept_whole():
    even = Histogram.new("e", [0.0, 0.5, 1.0], [1, 2])
    assert list(even.members["TH1"]["fXaxis"]["fXbins"]) == []
    uneven = Histogram.new("u", [0.0, 0.5, 2.0], [1, 2])
    assert list(uneven.members["TH1"]["fXaxis"]["fXbins"]) == [0.0, 0.5, 2.0]
    with read_back(written(e=even, u=uneven)) as back:
        assert list(back["e"].edges()) == [0.0, 0.5, 1.0]
        assert list(back["u"].edges()) == [0.0, 0.5, 2.0]


def test_a_new_histogram_takes_flow_bins_errors_and_entries_when_given():
    hist = Histogram.new("h", [0, 1, 2], [9, 5, 7, 3], errors=[1, 0.5, 0.25, 2], entries=40)
    assert hist.entries == 40.0
    with read_back(written(h=hist)) as back:
        again = back["h"]
        assert list(again.values(flow=True)) == [9.0, 5.0, 7.0, 3.0]
        assert list(again.errors(flow=True)) == [1.0, 0.5, 0.25, 2.0]
        assert again.entries == 40.0


def test_a_new_histogram_without_errors_reads_back_with_counting_ones():
    with read_back(written(h=Histogram.new("h", [0, 1, 2], [4, 9]))) as back:
        assert list(back["h"].errors()) == [2.0, 3.0]


def test_a_new_histogram_refuses_shapes_that_are_not_a_histogram():
    with pytest.raises(ValueError, match="at least two edges"):
        Histogram.new("h", [1.0], [])
    with pytest.raises(ValueError, match="edges must increase"):
        Histogram.new("h", [0, 2, 1], [1, 1])
    with pytest.raises(ValueError, match="give one per bin"):
        Histogram.new("h", [0, 1, 2], [1, 2, 3])
    with pytest.raises(ValueError, match="errors for 2 bins"):
        Histogram.new("h", [0, 1, 2], [1, 2], errors=[1])


def test_a_new_graph_picks_its_class_from_the_bars_it_was_given():
    assert Graph.new("g", [1], [2]).classname == "TGraph"
    even = Graph.new("g", [1, 2], [3, 4], yerr=[0.1, 0.2])
    assert even.classname == "TGraphErrors"
    assert list(even.members["fEX"]) == [0.0, 0.0]
    across = Graph.new("g", [1, 2], [3, 4], xerr=[0.5, 0.5])
    assert list(across.members["fEY"]) == [0.0, 0.0]
    uneven = Graph.new("g", [1, 2], [3, 4], yerr=([0.1, 0.2], [0.3, 0.4]))
    assert uneven.classname == "TGraphAsymmErrors"
    mixed = Graph.new("g", [1, 2], [3, 4], xerr=[0.5, 0.6], yerr=([0.1, 0.2], [0.3, 0.4]))
    assert mixed.classname == "TGraphAsymmErrors"
    assert list(mixed.members["fEXlow"]) == list(mixed.members["fEXhigh"]) == [0.5, 0.6]
    with read_back(written(g=mixed)) as back:
        again = back["g"]
        assert again.xerr == mixed.xerr
        assert again.layers == mixed.layers


def test_a_new_graph_of_no_points_is_still_a_graph():
    with read_back(written(g=Graph.new("g", [], []))) as back:
        assert len(back["g"]) == 0


def test_a_new_graph_refuses_bars_that_do_not_line_up_with_the_points():
    with pytest.raises(ValueError, match="are not points"):
        Graph.new("g", [1, 2], [3])
    with pytest.raises(ValueError, match=r"give one per\s+point"):
        Graph.new("g", [1, 2], [3, 4], yerr=[0.1])
    with pytest.raises(ValueError, match="low and 1 high"):
        Graph.new("g", [1, 2], [3, 4], xerr=([0.1, 0.2], [0.3]))


# -- compression, written --------------------------------------------------


@pytest.mark.parametrize(
    ("algorithm", "code"),
    [("zlib", 106), ("lzma", 206), ("lz4", 401), ("zstd", 503), (None, 0)],
)
def test_every_compression_setting_round_trips_and_says_so_in_the_header(algorithm, code):
    if algorithm == "zstd" and not HAVE_ZSTD:
        pytest.skip("no zstd in this interpreter")
    hist = Histogram.new("h", range(101), [float(step % 7) for step in range(100)])
    buf = io.BytesIO()
    with create(buf, compression=algorithm) as out:
        out["h"] = hist
    with read_back(buf.getvalue()) as back:
        assert back.compression == code
        assert list(back["h"].values()) == list(hist.values())


def test_the_level_asked_for_lands_in_the_header():
    buf = io.BytesIO()
    with create(buf, level=9) as out:
        out["s"] = "x" * 1000
    with read_back(buf.getvalue()) as back:
        assert back.compression == 109
        assert back["s"] == "x" * 1000


def test_an_object_that_did_not_shrink_is_stored_raw():
    with read_back(written(s="hi")) as back:
        key = back._key("s")
        assert not key.compressed
        assert back["s"] == "hi"


def test_an_unknown_compression_is_refused_by_create_and_by_compress():
    with pytest.raises(ValueError, match="compression must be one of"):
        create(io.BytesIO(), compression="brotli")
    with pytest.raises(ValueError, match="algorithm must be one of"):
        compress(b"data", "brotli")


def test_a_payload_larger_than_a_block_becomes_several_blocks():
    data = bytes(range(256)) * 66000  # a shade over the 16 MB block limit
    packed = compress(data, "zlib")
    assert int.from_bytes(packed[6:9], "little") == BLOCK
    assert decompress(packed, len(data)) == data


def test_nothing_compresses_to_one_empty_block_that_comes_back_as_nothing():
    packed = compress(b"", "zlib")
    assert packed[:2] == b"ZL" and len(packed) > 9
    assert decompress(packed, 0) == b""


def test_xxh64_matches_the_checksums_root_itself_stored():
    assert _xxh64(b"") == 0xEF46DB3751D8E999
    raw = (DATA / "dirs-6.14.00.root").read_bytes()
    verified, pos = 0, raw.find(b"L4\x01")
    while pos != -1:
        packed = int.from_bytes(raw[pos + 3 : pos + 6], "little")
        body = raw[pos + 17 : pos + 9 + packed]
        if packed > 8 and len(body) == packed - 8:
            stored = int.from_bytes(raw[pos + 9 : pos + 17], "big")
            if _xxh64(body) == stored:
                verified += 1
        pos = raw.find(b"L4\x01", pos + 1)
    assert verified >= 2


def test_lz4_packs_every_awkward_shape_the_decoder_can_prove():
    shapes = [
        b"",
        b"x",
        b"twelve bytes",
        b"abcd" * 64,  # matches, and matches that overlap what they make
        b"a" * 1000,  # a run long enough to need the length extension
        chain(40),  # nothing to match: one long run of literals
        chain(10)[:300] + b"abcd" * 8 + chain(10)[:300],  # long literals, then a match
        b"abcd" + chain(2188) + b"abcd",  # a match too far back to use
    ]
    for data in shapes:
        assert _lz4(_lz4_pack(data), len(data)) == data


# -- what the writer refuses, by name --------------------------------------


def test_an_object_the_writer_has_no_layout_for_is_refused():
    buf = io.BytesIO()
    with create(buf) as out:
        with pytest.raises(UnsupportedFeatureError, match="takes a Histogram, a Graph"):
            out["x"] = 3.14
        multi = Graph("TGraphMultiErrors", dict(Graph.new("g", [1], [2]).members))
        with pytest.raises(UnsupportedFeatureError, match="TGraphMultiErrors is not a class"):
            out["m"] = multi


def test_a_histogram_with_fits_attached_is_refused_rather_than_stripped():
    hist = Histogram.new("h", [0, 1, 2], [1, 2])
    hist.members["TH1"]["fFunctions"] = ["a fit"]
    buf = io.BytesIO()
    with create(buf) as out:
        with pytest.raises(UnsupportedFeatureError, match="empty it first"):
            out["h"] = hist


def test_an_axis_with_labels_is_refused_rather_than_silently_unlabelled():
    hist = Histogram.new("h", [0, 1, 2], [1, 2])
    hist.members["TH1"]["fXaxis"]["fLabels"] = ["one", "two"]
    buf = io.BytesIO()
    with create(buf) as out:
        with pytest.raises(UnsupportedFeatureError, match="empty it first"):
            out["h"] = hist


def test_names_a_reader_could_never_ask_for_are_refused():
    buf = io.BytesIO()
    with create(buf) as out:
        with pytest.raises(ValueError, match="could never be asked for"):
            out[""] = "x"
        with pytest.raises(ValueError, match="does not make subdirectories"):
            out["a/b"] = "x"
        with pytest.raises(ValueError, match="an old cycle"):
            out["a;1"] = "x"
        with pytest.raises(ValueError, match="the name must be a str"):
            out.write(7, "x")
        with pytest.raises(ValueError, match="too long for a key"):
            out["n" * 255] = "x"
        with pytest.raises(ValueError, match="the title must be a str"):
            out.write("a", "x", title=7)
        with pytest.raises(ValueError, match="too long for a key"):
            out.write("a", "x", title="t" * 255)


def test_a_long_title_lives_in_the_object_when_the_key_cannot_carry_it():
    hist = Histogram.new("h", [0, 1], [1], title="t" * 300)
    buf = io.BytesIO()
    with create(buf) as out:
        with pytest.raises(ValueError, match="too long for a key"):
            out["h"] = hist
        out.write("h", hist, title="")  # the key stays short; the object keeps it
    with read_back(buf.getvalue()) as back:
        assert back._key("h").title == ""
        assert back["h"].title == "t" * 300


def test_a_title_given_at_write_time_overrides_the_object_and_lands_on_the_key():
    buf = io.BytesIO()
    with create(buf) as out:
        out.write("h", Histogram.new("h", [0, 1], [1], title="its own"), title="the key's")
    with read_back(buf.getvalue()) as back:
        assert back._key("h").title == "the key's"
        assert back["h"].title == "its own"


def test_a_closed_file_refuses_more_writes_and_a_second_close_is_nothing():
    buf = io.BytesIO()
    out = create(buf)
    out["s"] = "x"
    assert not out.closed
    out.close()
    out.close()
    assert out.closed
    with pytest.raises(ValueError, match="this file is closed"):
        out["t"] = "y"
    with read_back(buf.getvalue()) as back:
        assert back.keys() == ["s"]


def test_a_with_block_that_raises_writes_nothing_at_all(tmp_path):
    buf = io.BytesIO()
    with pytest.raises(RuntimeError):
        with create(buf) as out:
            out["s"] = "x"
            raise RuntimeError("something else went wrong")
    assert buf.getvalue() == b""
    assert out.closed
    path = tmp_path / "broken.root"
    with pytest.raises(RuntimeError):
        with create(str(path)) as out:
            raise RuntimeError("likewise")
    assert path.read_bytes() == b""


# -- where a file can go ---------------------------------------------------


def test_create_writes_a_local_path_and_the_repr_counts_the_keys(tmp_path):
    path = tmp_path / "out.root"
    out = create(str(path))
    assert repr(out) == f"<WritableFile {str(path)!r}, 0 keys so far>"
    out["s"] = "hello"
    assert repr(out) == f"<WritableFile {str(path)!r}, 1 keys so far>"
    out.close()
    assert repr(out) == f"<WritableFile {str(path)!r}, closed>"
    with open_root(str(path)) as back:
        assert back["s"] == "hello"


def test_create_leaves_a_handle_it_was_given_open():
    buf = io.BytesIO()
    with create(buf) as out:
        assert out.name == "<file>"
        out["s"] = "hello"
    assert not buf.closed
    with read_back(buf.getvalue()) as back:
        assert back["s"] == "hello"


def test_create_takes_a_sink_with_nothing_but_a_write_method():
    class Sink:
        def __init__(self):
            self.data = b""

        def write(self, chunk):
            self.data += bytes(chunk)

    sink = Sink()
    with create(sink) as out:
        out["s"] = "hello"
    with read_back(sink.data) as back:
        assert back["s"] == "hello"


def test_create_writes_over_the_wire_like_anything_else():
    from xrd.testing import FakeServer

    with FakeServer(files={}) as server:
        with create(str(server.url / "out.root")) as out:
            out["s"] = "over the wire"
        with read_back(server.contents("/out.root")) as back:
            assert back["s"] == "over the wire"


# -- the machinery, directly, for the corners no honest object reaches -----


def test_a_counter_is_found_wherever_a_base_keeps_it():
    assert _find({"deep": {"unrelated": 1}, "base": {"fN": 2}}, "fN") == 2
    with pytest.raises(KeyError):
        _find({"deep": {"unrelated": 1}}, "fN")


def test_a_class_no_seed_describes_still_comes_out_of_the_closure():
    assert _closure(dict.fromkeys(["TWhatever"])) == ["TWhatever"]


def test_a_list_holding_anything_refuses_and_an_unknown_class_does_too():
    with pytest.raises(UnsupportedFeatureError, match="empty it first"):
        _list(WBuffer(), "TList", [1, 2])
    with pytest.raises(UnsupportedFeatureError, match="carries a layout for"):
        _record(WBuffer(), "TCanvas", {}, {})
    with pytest.raises(UnsupportedFeatureError, match="rather than the dict"):
        _record(WBuffer(), "TNamed", "not a dict", {})


def test_a_missing_base_falls_back_to_an_empty_object():
    buf = WBuffer()
    element = ("TStreamerBase", "TNamed", "", 0, 0, 0, 0, (0, 0, 0, 0, 0), "BASE", (1,))
    _element(buf, element, {}, {})  # no TNamed in the row: default bits, empty names
    assert bytes(buf.data[-2:]) == b"\x00\x00"  # two empty strings close it


def test_a_fixed_array_must_hold_exactly_what_was_declared():
    element = (
        "TStreamerBasicType",
        "fArr",
        "",
        OFFSET_L + 8,
        80,
        10,
        1,
        (10, 0, 0, 0, 0),
        "double",
        (),
    )
    buf = WBuffer()
    _element(buf, element, {"fArr": [1.0] * 10}, {})
    assert len(buf.data) == 80
    with pytest.raises(ValueError, match="9 values where 10 were declared"):
        _element(WBuffer(), element, {"fArr": [1.0] * 9}, {})


def test_a_counted_array_answers_to_its_counter():
    element = (
        "TStreamerBasicPointer",
        "fX",
        "",
        OFFSET_P + 8,
        8,
        0,
        0,
        (0, 0, 0, 0, 0),
        "double*",
        (4, "fNpoints", "TGraph"),
    )
    with pytest.raises(ValueError, match="counted by fNpoints, which is not here"):
        _element(WBuffer(), element, {"fX": [1.0]}, {})
    with pytest.raises(ValueError, match="fNpoints says 3"):
        _element(WBuffer(), element, {"fNpoints": 3, "fX": [1.0]}, {})
    buf = WBuffer()
    _element(buf, element, {"fNpoints": 1, "fX": [1.0, 2.0]}, {})  # the counter decides
    assert len(buf.data) == 1 + 8  # the marker byte, then one double


def test_a_streamer_type_the_writer_does_not_lay_out_is_refused():
    element = ("TStreamerSTL", "fVec", "", 500, 0, 0, 0, (0, 0, 0, 0, 0), "vector<int>", ())
    with pytest.raises(UnsupportedFeatureError, match="streamer type 500"):
        _element(WBuffer(), element, {"fVec": []}, {})


def test_the_infos_table_covers_exactly_what_the_writer_promises():
    for classname in (
        "TH1D",
        "TH1F",
        "TH2D",
        "TH2F",
        "TGraph",
        "TGraphErrors",
        "TGraphAsymmErrors",
    ):
        assert classname in INFOS
    assert "TGraphMultiErrors" not in INFOS  # refused by name, never guessed at
