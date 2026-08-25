"""Writing ``TTree``s, and reading them back with this library's own reader.

The reader here was written against files real ROOT wrote, and knows nothing
about the writer; a tree that survives it is a tree laid out the way the
donors in ``tests/data`` are laid out, not one that merely agrees with the
code that produced it. What is asserted beyond the values: the record
versions and the class descriptions match the donor files element for
element, a tree's ``fLeaves`` points at the very leaves its branches hold -
as ROOT's own does - and every way of declaring a column wrongly is refused
by name rather than guessed at.
"""

from __future__ import annotations

import io
import pathlib
import struct

import pytest

from xrd.root import create, open_root
from xrd.root.buffer import Buffer
from xrd.root.compression import decompress
from xrd.root.objects import CLASSES
from xrd.root.winfo import INFOS
from xrd.root.writer import _keylen
from xrd.root.wtree import (
    BRANCH_VERSION,
    LEAF_VERSION,
    LEAVES,
    MIN_BASKETS,
    PLATFORM,
    SUBLEAF_VERSION,
    TREE_VERSION,
    WritableTree,
    _Column,
    _typecode,
)

DATA = pathlib.Path(__file__).parent / "data"


def written(columns, rows, *, name="events", title="", **kwargs) -> bytes:
    """A file holding one tree of ``columns`` filled with ``rows``."""
    buf = io.BytesIO()
    with create(buf, **kwargs.pop("file", {})) as out:
        tree = out.tree(name, columns, title=title, **kwargs)
        tree.extend(rows)
    return buf.getvalue()


def read_back(data: bytes):
    return open_root(io.BytesIO(data))


# -- values in, values out -------------------------------------------------


def test_a_column_of_every_type_comes_back_as_it_went_in():
    columns = {code: code for code in LEAVES}
    rows = [
        {
            "?": bool(step % 2),
            "b": -step,
            "B": step,
            "h": -1000 * step,
            "H": 1000 * step,
            "i": -100_000 * step,
            "I": 100_000 * step,
            "q": -(10**12) * step,
            "Q": 10**12 * step,
            "f": 0.5 * step,
            "d": 0.1 * step,
        }
        for step in range(7)
    ]
    with read_back(written(columns, rows)) as back:
        tree = back["events"]
        assert len(tree) == 7
        for code in LEAVES:
            _assert_column_values(tree, rows, code)


def _assert_column_values(tree, rows, code):
    got = list(tree[code].array())
    want = [row[code] for row in rows]
    assert got == pytest.approx(want) if code in {"f", "d"} else got == want


def test_the_types_a_reader_reports_are_the_types_that_were_asked_for():
    columns = {
        "flag": bool,
        "count": int,
        "energy": float,
        "small": "h",
        "pixels": ("B", 4),
        "coords": ("f", 3),
    }
    row = {
        "flag": True,
        "count": 5,
        "energy": 1.5,
        "small": -3,
        "pixels": b"\x00\x01\x02\x03",
        "coords": [1.0, 2.0, 3.0],
    }
    with read_back(written(columns, [row])) as back:
        tree = back["events"]
        # The reader names the type of one value and says how many of them
        # an entry holds separately; the writer's own view spells both.
        assert tree.typenames() == {
            "flag": "bool",
            "count": "int64",
            "energy": "float64",
            "small": "int16",
            "pixels": "uint8",
            "coords": "float32",
        }
        assert [tree[name].length for name in columns] == [1, 1, 1, 1, 4, 3]
        assert not any(tree[name].is_jagged for name in columns)
        assert list(tree["pixels"].array()) == [0, 1, 2, 3]
        assert list(tree["coords"].array()) == [1.0, 2.0, 3.0]


def test_a_column_of_arrays_reads_back_entry_by_entry():
    rows = [{"hits": [step, step + 1, step + 2, step + 3]} for step in range(5)]
    with read_back(written({"hits": ("i", 4)}, rows)) as back:
        branch = back["events"]["hits"]
        assert list(branch.array()) == [value for row in rows for value in row["hits"]]
        assert list(branch.array(1, 3)) == [1, 2, 3, 4, 2, 3, 4, 5]


def test_the_widest_int_is_the_default_because_a_python_int_has_no_width():
    big = 2**62
    with read_back(written({"n": int}, [{"n": big}, {"n": -big}])) as back:
        assert list(back["events"]["n"].array()) == [big, -big]


def test_unsigned_columns_come_back_unsigned_rather_than_negative():
    with read_back(written({"n": "B", "m": "H"}, [{"n": 250, "m": 65000}])) as back:
        tree = back["events"]
        assert list(tree["n"].array()) == [250]
        assert list(tree["m"].array()) == [65000]
        assert tree.typenames() == {"n": "uint8", "m": "uint16"}


def test_a_one_byte_buffer_fills_a_one_byte_unsigned_array_column():
    """Width-one fixed text is still an array value, not an integer scalar."""
    with read_back(written({"code": ("B", 1)}, [{"code": b"D"}, {"code": b"\0"}])) as back:
        assert list(back["events"]["code"].array()) == [ord("D"), 0]


def test_the_tree_keeps_its_name_and_title_and_the_file_lists_it():
    data = written({"x": float}, [{"x": 1.0}], name="ntuple", title="what it holds")
    with read_back(data) as back:
        assert back.keys() == ["ntuple"]
        assert back.classnames()["ntuple"] == "TTree"
        assert back["ntuple"].title == "what it holds"


def test_a_tree_with_no_entries_is_still_a_tree():
    with read_back(written({"x": float}, [])) as back:
        tree = back["events"]
        assert len(tree) == 0
        assert tree.keys() == ["x"]
        assert list(tree["x"].array()) == []


def test_entries_can_be_added_one_at_a_time_or_in_bulk():
    buf = io.BytesIO()
    with create(buf) as out:
        tree = out.tree("events", {"x": "i"})
        tree.fill(x=1)
        tree.extend([{"x": 2}, {"x": 3}])
        assert len(tree) == 3
        assert tree.num_entries == 3
    with read_back(buf.getvalue()) as back:
        assert list(back["events"]["x"].array()) == [1, 2, 3]


def test_several_trees_in_one_file_keep_their_own_entries():
    buf = io.BytesIO()
    with create(buf) as out:
        first = out.tree("one", {"x": "i"})
        second = out.tree("two", {"y": float})
        first.extend([{"x": n} for n in range(4)])
        second.extend([{"y": n / 2} for n in range(6)])
    with read_back(buf.getvalue()) as back:
        assert sorted(back.keys()) == ["one", "two"]
        assert list(back["one"]["x"].array()) == [0, 1, 2, 3]
        assert list(back["two"]["y"].array()) == [0.0, 0.5, 1.0, 1.5, 2.0, 2.5]


def test_a_tree_and_a_histogram_can_share_a_file():
    from xrd.root import Histogram

    buf = io.BytesIO()
    with create(buf) as out:
        out["counts"] = Histogram.new("counts", [0.0, 1.0, 2.0], [3.0, 4.0])
        out.tree("events", {"x": "i"}).fill(x=7)
    with read_back(buf.getvalue()) as back:
        assert sorted(back.keys()) == ["counts", "events"]
        assert list(back["counts"].values()) == [3.0, 4.0]
        assert list(back["events"]["x"].array()) == [7]


# -- baskets -------------------------------------------------------------


def test_entries_go_out_in_baskets_as_they_gather():
    rows = [{"x": float(step)} for step in range(50)]
    data = written({"x": float}, rows, basket_size=64)  # eight entries a basket
    with read_back(data) as back:
        branch = back["events"]["x"]
        assert branch.num_baskets == 7
        assert list(branch.record.basket_entry) == [0, 8, 16, 24, 32, 40, 48, 50]
        assert list(branch.array()) == [float(step) for step in range(50)]
        assert list(branch.array(7, 9)) == [7.0, 8.0]  # across a basket boundary
        assert list(branch.array(-3, None)) == [47.0, 48.0, 49.0]


def test_a_basket_that_ends_exactly_full_is_not_followed_by_an_empty_one():
    rows = [{"x": step} for step in range(8)]
    data = written({"x": "i"}, rows, basket_size=16)  # four entries a basket
    with read_back(data) as back:
        branch = back["events"]["x"]
        assert branch.num_baskets == 2
        assert list(branch.record.basket_entry) == [0, 4, 8]
        assert list(branch.array()) == list(range(8))


def test_each_column_fills_its_own_baskets_at_its_own_rate():
    rows = [{"wide": [step] * 8, "narrow": step} for step in range(20)]
    data = written({"wide": ("q", 8), "narrow": "b"}, rows, basket_size=128)
    with read_back(data) as back:
        tree = back["events"]
        assert tree["wide"].num_baskets == 10  # 64 bytes an entry
        assert tree["narrow"].num_baskets == 1  # one byte an entry
        assert list(tree["narrow"].array()) == list(range(20))
        assert list(tree["wide"].array(19, 20)) == [19] * 8


def test_baskets_are_compressed_and_still_read_back():
    rows = [{"x": 1.0} for _ in range(2000)]  # compresses to nothing much
    plain = written({"x": float}, rows, file={"compression": None})
    zipped = written({"x": float}, rows, file={"compression": "zlib"})
    assert len(zipped) < len(plain) // 2
    with read_back(zipped) as back:
        assert list(back["events"]["x"].array()) == [1.0] * 2000


# -- the records themselves ------------------------------------------------


def tree_record(data: bytes, name="events") -> tuple[Buffer, int, int]:
    """A written tree's own bytes, ready to walk: the buffer, version and end."""
    handle = open_root(io.BytesIO(data))
    try:
        key = handle._key(name)
        raw = handle._source.read(key.seek_key, key.nbytes)[key.keylen :]
        payload = decompress(raw, key.objlen) if key.compressed else raw
        buf = Buffer(payload, key.keylen)
        version, end = buf.header()
        return buf, version, end
    finally:
        handle.close()


def at_branches(buf: Buffer) -> Buffer:
    """Walk a tree record's own fields, leaving the buffer at ``fBranches``."""
    buf.skip_record()  # TNamed
    for _ in range(3):
        buf.skip_record()  # TAttLine, TAttFill, TAttMarker
    buf.i64s(5)  # the entry and byte counts
    buf.f64()  # fWeight
    buf.i32s(4)  # fTimerInterval, fScanField, fUpdate, fDefaultEntryOffsetLen
    ranges = buf.i32()
    buf.i64s(6)  # fMaxEntries through fEstimate
    buf.u8()
    buf.i64s(ranges)  # fClusterRangeEnd
    buf.u8()
    buf.i64s(ranges)  # fClusterSize
    return buf


def branch_fields(buf: Buffer) -> dict[str, int]:
    """One ``TBranch``'s own numbers, read where the reader does not keep them."""
    version, end = buf.header()
    buf.skip_record()  # TNamed
    buf.skip_record()  # TAttFill
    fields = {
        "version": version,
        "compress": buf.i32(),
        "basket_size": buf.i32(),
        "entry_offset_len": buf.i32(),
        "write_basket": buf.i32(),
        "entry_number": buf.i64(),
        "offset": buf.i32(),
        "max_baskets": buf.i32(),
        "split_level": buf.i32(),
        "entries": buf.i64(),
        "first_entry": buf.i64(),
        "tot_bytes": buf.i64(),
        "zip_bytes": buf.i64(),
    }
    buf.resume(end)
    return fields


def test_the_records_are_written_at_the_versions_the_donors_describe():
    """What a file says about its classes has to be what its records are."""
    assert TREE_VERSION == INFOS["TTree"][1]
    assert BRANCH_VERSION == INFOS["TBranch"][1]
    assert LEAF_VERSION == INFOS["TLeaf"][1]
    for classname, _letter, _size, _unsigned in LEAVES.values():
        assert INFOS[classname][1] == SUBLEAF_VERSION, classname
    _buf, version, _end = tree_record(written({"x": float}, [{"x": 1.0}]))
    assert version == TREE_VERSION


def test_a_written_tree_describes_its_classes_exactly_as_the_donors_do():
    with open_root(str(DATA / "small-flat-tree.root")) as donor:
        theirs = dict(donor._source.streamers())
    with open_root(str(DATA / "leaves.root")) as donor:
        # Only for the leaf classes the older donor never wrote: where both
        # describe a class the writer follows the 6.08 one, comments and all.
        for classname, members in donor._source.streamers().items():
            theirs.setdefault(classname, members)
    data = written({"x": float, "n": "i", "flag": bool}, [{"x": 1.0, "n": 2, "flag": True}])
    with read_back(data) as back:
        ours = back._source.streamers()
    for classname in ("TTree", "TBranch", "TLeaf", "TLeafD", "TLeafI", "TLeafO"):
        assert classname in ours, classname
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


def test_only_the_leaf_classes_a_tree_uses_are_described():
    with read_back(written({"x": float}, [{"x": 1.0}])) as back:
        described = back._source.streamers()
    assert "TLeafD" in described
    assert "TLeafI" not in described
    assert "TLeafO" not in described


def test_the_trees_leaves_are_the_very_leaves_its_branches_hold():
    """ROOT's ``fLeaves`` points at objects already written inside the
    branches, not at copies; a reader that resolves the references gets the
    same objects back, which is what proves the offsets are right."""
    data = written({"i": "q", "x": "f"}, [{"i": 1, "x": 2.0}])
    buf, _version, end = tree_record(data)
    branches = at_branches(buf).objarray(CLASSES)
    leaves = buf.objarray(CLASSES)
    assert [leaf.name for leaf in leaves] == ["i", "x"]
    assert [id(leaf) for leaf in leaves] == [id(branch.leaves[0]) for branch in branches]
    assert [leaf.classname for leaf in leaves] == ["TLeafL", "TLeafF"]
    buf.resume(end)


def test_a_branch_declares_the_geometry_a_reader_needs():
    rows = [{"x": float(step)} for step in range(10)]
    with read_back(written({"x": float}, rows, basket_size=32)) as back:
        record = back["events"]["x"].record
        assert record.entries == 10
        assert record.entry_offset_len == 0  # fixed-size entries need no table
        assert len(record.basket_seek) == 3
        assert len(record.basket_bytes) == 3
        assert len(record.basket_entry) == 4


def test_a_branch_says_what_root_would_say_about_itself():
    rows = [{"x": float(step)} for step in range(10)]
    buf, _version, end = tree_record(written({"x": float}, rows, basket_size=32))
    (fields,) = at_branches(buf).objarray({"TBranch": branch_fields})
    _assert_branch_geometry(fields)
    _assert_branch_sizes(fields)
    buf.resume(end)


def _assert_branch_geometry(fields):
    assert fields["version"] == BRANCH_VERSION
    assert fields["basket_size"] == 32
    assert fields["entry_offset_len"] == 0  # every entry the same size
    assert fields["write_basket"] == 3
    assert fields["max_baskets"] == MIN_BASKETS  # room for more, as ROOT's has
    assert fields["split_level"] == 0  # a column is a column
    assert fields["entries"] == fields["entry_number"] == 10
    assert fields["first_entry"] == 0


def _assert_branch_sizes(fields):
    assert fields["tot_bytes"] > 80  # ten doubles and three basket keys
    assert fields["zip_bytes"] > 0


def test_a_basket_key_says_how_many_entries_it_holds_and_how_big_one_is():
    rows = [{"x": float(step)} for step in range(4)]
    data = written({"x": float}, rows, file={"compression": None})
    with read_back(data) as back:
        record = back["events"]["x"].record
        seek, nbytes = record.basket_seek[0], record.basket_bytes[0]
    raw = data[seek : seek + nbytes]
    keylen = _keylen("TBasket", "x", "events", extra=19)
    version, buffer_size, nevsize, nev, last, flag = struct.unpack_from(">hiiiiB", raw, keylen - 19)
    assert (version, nevsize, nev, flag) == (3, 8, 4, 0)
    assert buffer_size == 32_000
    assert last == keylen + 32
    assert raw[keylen:] == struct.pack(">4d", 0.0, 1.0, 2.0, 3.0)


def test_the_basket_key_is_named_for_its_branch_and_titled_for_its_tree():
    data = written({"pixels": ("B", 3)}, [{"pixels": b"abc"}], name="digits")
    with read_back(data) as back:
        record = back["digits"]["pixels"].record
        seek, nbytes = record.basket_seek[0], record.basket_bytes[0]
    buf = Buffer(data[seek : seek + nbytes])
    buf.i32(), buf.u16(), buf.i32(), buf.u32(), buf.i16(), buf.i16(), buf.i32(), buf.i32()
    assert (buf.string(), buf.string(), buf.string()) == ("TBasket", "pixels", "digits")


# -- what a tree refuses ---------------------------------------------------


def test_a_column_of_an_unknown_type_is_refused_by_name():
    with pytest.raises(ValueError, match="'x' is of type 'c'"):
        _typecode("x", "c")


def test_a_column_declared_as_an_unusable_python_type_says_which_ones_work():
    with pytest.raises(ValueError, match="declared as str, and the Python types"):
        _typecode("x", str)


def test_a_column_declared_as_neither_a_code_nor_a_type_is_refused():
    with pytest.raises(ValueError, match="declared as 3, which is neither"):
        _typecode("x", 3)


def test_a_column_pair_of_the_wrong_shape_says_what_a_pair_means():
    with pytest.raises(ValueError, match="declared as 3 things"):
        _typecode("x", ("f", 3, 4))


@pytest.mark.parametrize("length", [0, -1, "four", 2.0, True])
def test_a_column_whose_count_is_not_a_count_is_refused(length):
    with pytest.raises(ValueError, match="values per entry, which is not a count"):
        _typecode("x", ("f", length))


def test_a_pair_can_be_a_list_and_a_python_type_can_be_the_type_in_it():
    assert _typecode("x", ["f", 3]) == ("f", 3)
    assert _typecode("x", (int, 2)) == ("q", 2)


def test_platform_sized_codes_are_written_as_a_width_that_does_not_move():
    """``'l'`` is 32 bits on some platforms and 64 on others; a file is not."""
    assert PLATFORM["l"] in ("q", "i")
    assert _typecode("x", "l")[0] == PLATFORM["l"]
    assert _typecode("x", "L")[0] == PLATFORM["L"]
    with read_back(written({"n": "l"}, [{"n": 7}])) as back:
        assert list(back["events"]["n"].array()) == [7]


def test_a_tree_declared_with_something_other_than_a_mapping_is_refused():
    with create(io.BytesIO()) as out:
        with pytest.raises(TypeError, match="columns are a list"):
            out.tree("events", [("x", float)])


def test_a_tree_with_no_columns_is_refused():
    with create(io.BytesIO()) as out:
        with pytest.raises(ValueError, match="no columns holds nothing"):
            out.tree("events", {})


@pytest.mark.parametrize("size", [0, -1, "big", None])
def test_a_basket_size_that_is_not_a_size_is_refused(size):
    with create(io.BytesIO()) as out:
        with pytest.raises(ValueError, match="not a size in bytes"):
            out.tree("events", {"x": float}, basket_size=size)


@pytest.mark.parametrize("column", ["", "a.b", "a/b", "a;b", "a[0]", "a b"])
def test_a_column_name_that_means_something_else_to_a_reader_is_refused(column):
    with create(io.BytesIO()) as out:
        with pytest.raises(ValueError, match="not a name a column can have"):
            out.tree("events", {column: float})


def test_a_column_name_that_is_not_text_is_refused():
    with create(io.BytesIO()) as out:
        with pytest.raises(ValueError, match="column name must be a str"):
            out.tree("events", {7: float})


def test_a_column_name_too_long_for_a_key_is_refused():
    with create(io.BytesIO()) as out:
        with pytest.raises(ValueError, match="too long"):
            out.tree("events", {"x" * 255: float})


def test_an_entry_missing_a_column_is_refused_and_nothing_is_kept():
    with create(io.BytesIO()) as out:
        tree = out.tree("events", {"x": float, "y": float})
        with pytest.raises(ValueError, match="nothing for y"):
            tree.fill(x=1.0)
        assert len(tree) == 0


def test_an_entry_with_a_column_the_tree_does_not_have_is_refused():
    with create(io.BytesIO()) as out:
        tree = out.tree("events", {"x": float})
        with pytest.raises(ValueError, match="z, which is not a column"):
            tree.fill(x=1.0, z=2.0)


def test_an_entry_both_missing_and_inventing_a_column_says_both():
    with create(io.BytesIO()) as out:
        tree = out.tree("events", {"x": float})
        with pytest.raises(ValueError, match="nothing for x and z, which is not a column"):
            tree.fill(z=2.0)


def test_an_entry_of_the_wrong_type_is_refused_by_what_the_column_holds():
    with create(io.BytesIO()) as out:
        tree = out.tree("events", {"x": float})
        with pytest.raises(ValueError, match="'x' holds float64 values"):
            tree.fill(x="warm")


def test_an_array_entry_of_the_wrong_length_says_how_many_it_wanted():
    with create(io.BytesIO()) as out:
        tree = out.tree("events", {"hits": ("i", 4)})
        with pytest.raises(ValueError, match="takes 4 values per entry, and this one has 3"):
            tree.fill(hits=[1, 2, 3])


def test_an_array_entry_that_is_not_a_sequence_is_refused():
    with create(io.BytesIO()) as out:
        tree = out.tree("events", {"hits": ("i", 4)})
        with pytest.raises(ValueError, match="not a sequence of them"):
            tree.fill(hits=7)


def test_bytes_of_the_wrong_length_for_a_byte_column_are_refused():
    with create(io.BytesIO()) as out:
        tree = out.tree("events", {"pixels": ("B", 4)})
        with pytest.raises(ValueError, match="takes 4 bytes per entry, and this one has 2"):
            tree.fill(pixels=b"ab")


def test_an_entry_is_refused_whole_when_a_later_column_does_not_fit():
    """The first column must not keep a value the entry never completed."""
    buf = io.BytesIO()
    with create(buf) as out:
        tree = out.tree("events", {"x": float, "y": float})
        with pytest.raises(ValueError, match="'y' holds float64"):
            tree.fill(x=1.0, y="warm")
        assert len(tree) == 0
        tree.fill(x=2.0, y=3.0)
    with read_back(buf.getvalue()) as back:
        assert list(back["events"]["x"].array()) == [2.0]
        assert list(back["events"]["y"].array()) == [3.0]


def test_filling_a_tree_after_the_file_closed_says_what_it_holds():
    buf = io.BytesIO()
    with create(buf) as out:
        tree = out.tree("events", {"x": float})
        tree.fill(x=1.0)
    with pytest.raises(ValueError, match="closed; 'events' holds the 1 entries"):
        tree.fill(x=2.0)


def test_a_tree_cannot_be_named_what_a_key_cannot_be_named():
    with create(io.BytesIO()) as out:
        with pytest.raises(ValueError, match="does not make subdirectories"):
            out.tree("a/b", {"x": float})
        with pytest.raises(ValueError, match="asks for an old cycle"):
            out.tree("a;1", {"x": float})
        with pytest.raises(ValueError, match="could never be asked for"):
            out.tree("", {"x": float})


def test_a_tree_cannot_be_made_in_a_file_that_is_closed():
    out = create(io.BytesIO())
    out.close()
    with pytest.raises(ValueError, match="this file is closed"):
        out.tree("events", {"x": float})


def test_a_tree_title_too_long_for_a_key_is_refused():
    with create(io.BytesIO()) as out:
        with pytest.raises(ValueError, match=r"title.*too long"):
            out.tree("events", {"x": float}, title="t" * 300)


# -- the objects themselves ------------------------------------------------


def test_a_tree_says_what_it_is_while_it_is_being_filled():
    with create(io.BytesIO()) as out:
        tree = out.tree("events", {"x": float, "n": "i"})
        assert isinstance(tree, WritableTree)
        assert repr(tree) == "<WritableTree 'events' with 2 columns and 0 entries so far>"
        tree.fill(x=1.0, n=2)
        assert repr(tree) == "<WritableTree 'events' with 2 columns and 1 entries so far>"
        assert tree.columns == {"x": "float64", "n": "int32"}
        assert tree.classes == ("TTree", "TBranch", "TLeafD", "TLeafI")


def test_two_columns_of_one_type_name_that_class_once():
    with create(io.BytesIO()) as out:
        tree = out.tree("events", {"x": float, "y": float})
        assert tree.classes == ("TTree", "TBranch", "TLeafD")


def test_a_column_knows_how_it_will_be_spelled():
    column = _Column("pixels", "B", 784, 32_000)
    assert (column.title, column.leaf_title) == ("pixels[784]/b", "pixels[784]")
    assert (column.typename, column.size, column.unsigned) == ("uint8", 784, True)
    plain = _Column("x", "d", 1, 32_000)
    assert (plain.title, plain.leaf_title) == ("x/D", "x")
