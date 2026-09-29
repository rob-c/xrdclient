"""The fsspec bindings, when the optional extra is installed."""

from __future__ import annotations

import pytest

fsspec = pytest.importorskip("fsspec")

from xrdclient.fsspec_impl import HTTPXRootDFileSystem, XRootDFileSystem  # noqa: E402

BODY = b"hello world"


@pytest.fixture
def xfs(server):
    """A filesystem bound to the fixture endpoint, cleared from fsspec's cache."""
    XRootDFileSystem.clear_instance_cache()
    filesystem = XRootDFileSystem(str(server.url))
    try:
        yield filesystem
    finally:
        filesystem.close()
        XRootDFileSystem.clear_instance_cache()


def test_the_protocols_are_registered_by_entry_point():
    assert fsspec.get_filesystem_class("root") is XRootDFileSystem
    assert fsspec.get_filesystem_class("davs") is HTTPXRootDFileSystem


def test_a_url_is_split_into_endpoint_and_path():
    assert XRootDFileSystem._strip_protocol("root://host:1094//store/f.root") == "/store/f.root"
    assert XRootDFileSystem._strip_protocol("store/f.root") == "/store/f.root"
    assert XRootDFileSystem._get_kwargs_from_urls("root://host:1094//store/f.root") == {
        "endpoint": "root://host:1094//"
    }
    assert XRootDFileSystem._get_kwargs_from_urls("/store/f.root") == {}


def test_ls_gives_fsspec_what_fsspec_expects(xfs):
    listing = xfs.ls("/data")
    assert [entry["name"] for entry in listing] == ["/data/a.root", "/data/empty"]
    assert listing[0]["type"] == "file"
    assert listing[1]["type"] == "directory"
    assert xfs.ls("/data", detail=False) == ["/data/a.root", "/data/empty"]


def test_the_namespace_predicates_agree_with_the_server(xfs):
    assert xfs.info("/data/a.root")["size"] == len(BODY)
    assert xfs.size("/data/a.root") == len(BODY)
    assert xfs.exists("/data/a.root") and not xfs.exists("/data/nope")
    assert xfs.isdir("/data") and not xfs.isdir("/data/a.root")
    assert xfs.isfile("/data/a.root") and not xfs.isfile("/data")
    assert xfs.modified("/data/a.root").year >= 1970
    assert xfs.created("/data/a.root") is not None


def test_the_checksum_is_the_servers_not_a_synthetic_one(xfs):
    assert xfs.checksum("/data/a.root") == "1a0b045d"


def test_reading_goes_through_the_real_io_stack(xfs):
    with xfs.open("/data/a.root", "rb") as handle:
        assert handle.seekable()
        handle.seek(6)
        assert handle.read() == b"world"
    assert xfs.cat_file("/data/a.root") == BODY
    assert xfs.cat_file("/data/a.root", 6) == b"world"
    assert xfs.cat_file("/data/a.root", 0, 5) == b"hello"


def test_text_mode_is_wrapped_the_way_fsspec_users_expect(xfs):
    with xfs.open("/data/a.root", "r") as handle:
        assert handle.read() == "hello world"


def test_ranges_across_files_use_one_vector_read_each(server, xfs):
    server.add_file("/data/b.bin", b"0123456789")
    chunks = xfs.cat_ranges(
        ["/data/a.root", "/data/b.bin", "/data/a.root"], [0, 2, 6], [5, 5, 11]
    )
    assert chunks == [b"hello", b"234", b"world"]


def test_writing_and_removing(server, xfs):
    xfs.pipe_file("/data/written.bin", b"payload")
    assert server.contents("/data/written.bin") == b"payload"
    with xfs.open("/data/streamed.bin", "wb") as handle:
        handle.write(b"stream")
    assert server.contents("/data/streamed.bin") == b"stream"
    xfs.rm("/data/written.bin")
    assert "/data/written.bin" not in server.files


def test_directories_are_made_and_unmade(server, xfs):
    xfs.mkdir("/data/one/two")
    assert "/data/one/two" in server.dirs
    xfs.makedirs("/data/one/two", exist_ok=True)
    xfs.rmdir("/data/one/two")
    assert "/data/one/two" not in server.dirs


def test_recursive_removal_takes_the_tree(server, xfs):
    server.add_file("/data/tree/deep/f.bin", b"x")
    xfs.rm("/data/tree", recursive=True)
    assert not [path for path in server.files if path.startswith("/data/tree")]


def test_touch_and_rename(server, xfs):
    xfs.touch("/data/t.bin")
    assert server.contents("/data/t.bin") == b""
    xfs.pipe_file("/data/t.bin", b"kept")
    xfs.touch("/data/t.bin", truncate=False)
    assert server.contents("/data/t.bin") == b"kept"
    xfs.mv("/data/t.bin", "/data/moved.bin")
    assert server.contents("/data/moved.bin") == b"kept"


def test_a_fully_qualified_path_to_another_server_is_honoured(xfs):
    """And the connection it opens is kept, not leaked once per call."""
    from xrdclient.testing import FakeServer

    with FakeServer(files={"/other/f.bin": b"elsewhere"}) as other:
        assert xfs.cat_file(str(other.url) + "other/f.bin") == b"elsewhere"
        first, _path = xfs._target(str(other.url) + "other/f.bin")
        second, _path = xfs._target(str(other.url) + "other")
        assert first is second is not xfs._fs
        xfs.close()
        assert xfs._elsewhere == {}


def test_an_instance_with_no_endpoint_says_so():
    filesystem = XRootDFileSystem()
    with pytest.raises(ValueError, match="no endpoint"):
        filesystem.info("/data/a.root")
    filesystem.close()


def test_fsspec_open_uses_the_registered_class(server):
    XRootDFileSystem.clear_instance_cache()
    with fsspec.open(str(server.url) + "data/a.root", "rb") as handle:
        assert handle.read() == BODY
    XRootDFileSystem.clear_instance_cache()


def test_closing_twice_is_allowed(server):
    filesystem = XRootDFileSystem(str(server.url))
    filesystem.close()
    filesystem.close()


def test_the_single_file_remove_hook_is_the_one_fsspec_calls(server, xfs):
    """``_rm`` is fsspec's per-file entry point; ``rm`` is the bulk one."""
    xfs.pipe_file("/data/one.bin", b"x")
    xfs._rm("/data/one.bin")
    assert "/data/one.bin" not in server.files


# ---------------------------------------------------------------------------
# Byte ranges: fsspec's slice semantics, negative offsets included
# ---------------------------------------------------------------------------

BOUNDS = [None, -20, -3, 0, 2, 6, 11, 20]


@pytest.mark.parametrize("start", BOUNDS)
@pytest.mark.parametrize("end", BOUNDS)
def test_cat_file_reads_the_slice_fsspec_documents(xfs, start, end):
    """Negative bounds count back from the end, as in a Python slice."""
    assert xfs.cat_file("/data/a.root", start, end) == BODY[start:end]


def test_cat_ranges_takes_the_same_bounds_and_scalar_ones(xfs):
    starts = [None, -3, 2, -20, 6]
    ends = [-2, None, -2, 5, 2]
    chunks = xfs.cat_ranges(["/data/a.root"] * len(starts), starts, ends)
    assert chunks == [BODY[s:e] for s, e in zip(starts, ends)]
    assert xfs.cat_ranges(["/data/a.root"] * 2, -5, None) == [b"world"] * 2


def test_cat_ranges_refuses_bounds_that_do_not_pair_with_the_paths(xfs):
    with pytest.raises(ValueError):
        xfs.cat_ranges(["/data/a.root"], [0, 1], [2, 3])


# ---------------------------------------------------------------------------
# Listing: a file lists as itself, and names find their way back
# ---------------------------------------------------------------------------


def test_ls_of_a_file_is_that_file(xfs):
    """fsspec's convention: ``ls`` of a file is a one-element listing."""
    listing = xfs.ls("/data/a.root")
    assert [entry["name"] for entry in listing] == ["/data/a.root"]
    assert listing[0]["type"] == "file" and listing[0]["size"] == len(BODY)
    assert xfs.ls("/data/a.root", detail=False) == ["/data/a.root"]
    with pytest.raises(FileNotFoundError):
        xfs.ls("/data/nope")


@pytest.fixture
def other():
    """A second server, holding what the default endpoint does not."""
    from xrdclient.testing import FakeServer

    with FakeServer(files={"/far/f.bin": b"elsewhere", "/far/sub/g.bin": b"deep"}) as srv:
        yield srv


def test_names_on_another_endpoint_carry_that_endpoint(xfs, other):
    """A bare path would send the next call to the default server instead."""
    base = str(other.url)
    names = xfs.ls(base + "far", detail=False)
    assert names == [base + "far/f.bin", base + "far/sub"]
    assert xfs.cat_file(names[0]) == b"elsewhere"
    assert xfs.info(names[0])["name"] == names[0]
    assert xfs.ls(names[0], detail=False) == [names[0]]
    assert xfs.find(base + "far") == [base + "far/f.bin", base + "far/sub/g.bin"]
    assert xfs.glob(base + "far/**/*.bin") == [base + "far/f.bin", base + "far/sub/g.bin"]
    assert xfs.isdir(xfs._parent(names[0]))


def test_names_on_the_default_endpoint_stay_bare(server, xfs):
    """The fsspec-xrootd convention: this instance's own server is implied."""
    own = str(server.url)
    assert xfs.ls(own + "data", detail=False) == ["/data/a.root", "/data/empty"]
    assert xfs.glob(own + "data/*.root") == ["/data/a.root"]
    assert xfs.unstrip_protocol("/data/a.root") == own + "data/a.root"
    with fsspec.open(xfs.unstrip_protocol("/data/a.root"), "rb") as handle:
        assert handle.read() == BODY


def test_stripping_keeps_an_endpoint_only_an_instance_cannot_imply(server, xfs, other):
    """The class does not know an endpoint; an instance knows its own."""
    far = str(other.url) + "far/f.bin"
    assert XRootDFileSystem._strip_protocol(far) == "/far/f.bin"
    assert xfs._strip_protocol(far) == far
    assert xfs._strip_protocol(str(server.url) + "data/") == "/data"
    assert xfs._strip_protocol("data/") == "/data"


def test_a_list_of_paths_is_stripped_one_by_one(xfs, other):
    far = str(other.url) + "far/f.bin"
    assert XRootDFileSystem._strip_protocol(["/a/", far]) == ["/a", "/far/f.bin"]
    assert xfs._strip_protocol(["/a/", far]) == ["/a", far]


def test_the_parent_of_a_path_on_the_own_endpoint_is_bare(xfs):
    assert XRootDFileSystem._parent("root://h:1//a/b/") == "/a"
    assert xfs._parent("/data/a.root") == "/data"


def test_a_name_that_is_already_a_url_is_left_as_it_is(xfs, other):
    far = str(other.url) + "far/f.bin"
    assert xfs.unstrip_protocol(far) == far


def test_without_an_endpoint_a_name_is_unstripped_the_fsspec_way():
    bare = XRootDFileSystem(skip_instance_cache=True)
    try:
        assert bare.unstrip_protocol("/a/b") == "root:///a/b"
    finally:
        bare.close()


def test_a_directory_that_will_not_list_raises_the_listings_error(server, xfs):
    """Its stat says it is a directory, so the listing's failure is the truth."""
    from xrdclient.proto import constants as c
    from xrdclient.testing import error

    server.handlers[c.kXR_dirlist] = lambda conn, sid, params, body: iter(
        [error(sid, 3010, "no listing for you")]
    )
    with pytest.raises(OSError, match="no listing for you"):
        xfs.ls("/data")


def test_a_file_that_shrinks_mid_range_ends_the_read_early():
    """A range is read until the file runs out, not until the size it had."""
    import io

    from xrdclient.fsspec_impl import _read_span

    class Shrinking(io.BytesIO):
        def read(self, size=-1):
            data = super().read(min(size, 3))
            self.truncate(self.tell())
            return data

    assert _read_span(Shrinking(b"0123456789"), 0, 8) == b"012"


# ---------------------------------------------------------------------------
# Moving between endpoints
# ---------------------------------------------------------------------------


def test_mv_to_another_endpoint_copies_then_deletes(server, xfs, other):
    """A rename cannot cross servers, and must not land on the source's."""
    xfs.mv("/data/a.root", str(other.url) + "moved.bin")
    assert other.contents("/moved.bin") == BODY
    assert "/data/a.root" not in server.files
    assert "/moved.bin" not in server.files


def test_mv_from_another_endpoint_to_this_one(server, xfs, other):
    xfs.mv(str(other.url) + "far/f.bin", "/data/arrived.bin")
    assert server.contents("/data/arrived.bin") == b"elsewhere"
    assert "/far/f.bin" not in other.files


def test_mv_within_one_endpoint_is_a_rename_even_spelled_as_urls(server, xfs, other):
    base = str(other.url)
    xfs.mv(base + "far/f.bin", base + "far/renamed.bin")
    assert other.contents("/far/renamed.bin") == b"elsewhere"
    assert "/far/f.bin" not in other.files
    xfs.mv(str(server.url) + "data/a.root", "/data/b.root")
    assert server.contents("/data/b.root") == BODY


def test_mv_of_a_directory_to_another_endpoint_moves_the_tree(xfs, other, server):
    server.add_file("/data/tree/x.bin", b"x")
    server.add_file("/data/tree/deep/y.bin", b"y")
    with pytest.raises(IsADirectoryError):
        xfs.mv("/data/tree", str(other.url) + "tree")
    xfs.mv("/data/tree", str(other.url) + "tree", recursive=True)
    assert other.contents("/tree/x.bin") == b"x"
    assert other.contents("/tree/deep/y.bin") == b"y"
    assert not [path for path in server.files if path.startswith("/data/tree")]
