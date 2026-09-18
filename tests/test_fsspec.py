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
