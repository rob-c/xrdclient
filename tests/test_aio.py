"""The async facade. Driven with :func:`asyncio.run`, so no plugin is needed."""

from __future__ import annotations

import asyncio
import sys

import pytest

import xrd
from xrd.aio import AsyncFile, AsyncFileSystem
from xrd.errors import UnsupportedError
from xrd.flags import DirListFlags, QueryCode
from xrd.testing import FakeDAVServer, FakeServer

BODY = b"hello world"


def run(coroutine):
    """Drive one coroutine to completion on a fresh event loop."""
    return asyncio.run(coroutine)


@pytest.fixture
def dav():
    with FakeDAVServer(files={"/d/a.root": BODY}) as running:
        yield running


# ---------------------------------------------------------------------------
# Import discipline
# ---------------------------------------------------------------------------


def test_the_sync_package_does_not_import_asyncio():
    """``import xrd`` must not cost an event loop nobody asked for."""
    code = (
        "import sys, xrd; assert 'asyncio' not in sys.modules; "
        "xrd.aio; assert 'asyncio' in sys.modules"
    )
    import subprocess

    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env={"PYTHONPATH": "src"}
    )
    assert done.returncode == 0, done.stderr


def test_the_facade_is_reachable_both_ways():
    import xrd.aio

    assert xrd.aio is sys.modules["xrd.aio"]
    assert xrd.aio.FileSystem is AsyncFileSystem
    assert xrd.aio.File is AsyncFile


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def test_a_file_reads_the_way_it_does_synchronously(server):
    async def main():
        async with xrd.aio.open(server.url / "data/a.root") as handle:
            assert handle.readable() and not handle.writable()
            assert handle.seekable() and not handle.closed
            assert await handle.read(5) == b"hello"
            assert await handle.tell() == 5
            await handle.seek(6)
            assert await handle.read() == b"world"
        assert handle.closed

    run(main())


def test_await_open_gives_a_file_you_close_yourself(server):
    async def main():
        handle = await xrd.aio.open(server.url / "data/a.root")
        try:
            assert await handle.read() == BODY
        finally:
            await handle.close()
        assert handle.closed
        await handle.close()  # twice is allowed, as in the sync API

    run(main())


def test_text_mode_survives_the_crossing(server):
    async def main():
        async with xrd.aio.open(server.url / "data/a.root", "r") as handle:
            assert await handle.read() == "hello world"

    run(main())


def test_a_file_iterates_line_by_line(server):
    server.add_file("/data/lines.txt", b"one\ntwo\nthree\n")

    async def main():
        async with xrd.aio.open(server.url / "data/lines.txt") as handle:
            return [line async for line in handle]

    assert run(main()) == [b"one\n", b"two\n", b"three\n"]


def test_writing_and_flushing(server):
    async def main():
        async with xrd.aio.open(server.url / "data/out.bin", "wb") as handle:
            assert await handle.write(b"written") == 7
            await handle.flush()
        async with xrd.aio.open(server.url / "data/out.bin", "r+b") as handle:
            await handle.seek(0)
            assert await handle.read() == b"written"
            assert await handle.truncate(3) == 3
        assert server.contents("/data/out.bin") == b"wri"

    run(main())


def test_readinto_fills_the_buffer_given(server):
    async def main():
        buffer = bytearray(5)
        async with xrd.aio.open(server.url / "data/a.root", buffering=0) as handle:
            assert await handle.readinto(buffer) == 5
        return bytes(buffer)

    assert run(main()) == b"hello"


def test_the_protocol_level_operations_are_there(server):
    async def main():
        async with xrd.aio.open(server.url / "data/a.root") as handle:
            assert handle.file is not None
            assert await handle.readv([(0, 5), (6, 5)]) == [b"hello", b"world"]
            assert (await handle.stat()).st_size == len(BODY)
            assert (await handle.checksum()).value == "1a0b045d"
            page = await handle.pgread(11, 0)
            assert bytes(page.data) == BODY
            await handle.sync()

    run(main())


def test_the_scattered_write_operations_are_there(server):
    async def main():
        async with xrd.aio.open(server.url / "data/v.bin", "wb") as handle:
            assert await handle.writev([(0, b"aaaa"), (4, b"bbbb")]) == 8
            assert await handle.pgwrite(b"cc", 8) == 2
        async with xrd.aio.open(server.url / "data/v.bin") as handle:
            with pytest.raises(OSError):
                handle.fileno()  # remote handles have no descriptor, and say so

    run(main())
    assert server.contents("/data/v.bin") == b"aaaabbbbcc"


def test_a_clone_copies_ranges_server_side(server):
    async def main():
        async with xrd.aio.open(server.url / "data/c.bin", "wb") as handle:
            await handle.write(b"0123456789")
            await handle.flush()  # the server can only clone what it has
            assert await handle.clone(handle, [(0, 4, 10)]) == 4  # the async wrapper
            assert await handle.clone(handle.file, [(0, 4, 14)]) == 4  # the file underneath

    run(main())
    assert server.contents("/data/c.bin") == b"0123456789" + b"0123" * 2


def test_cloning_from_something_that_is_not_a_root_file_is_refused(server):
    import io as _io

    async def main():
        async with xrd.aio.open(server.url / "data/c.bin", "wb") as handle:
            with pytest.raises(UnsupportedError, match="clone needs a root://"):
                await handle.clone(AsyncFile(_io.BytesIO(b"local")))

    run(main())


def test_lines_are_read_and_written_in_bulk(server):
    async def main():
        async with xrd.aio.open(server.url / "data/l.txt", "w") as handle:
            await handle.writelines(["one\n", "two\n"])
        async with xrd.aio.open(server.url / "data/l.txt", "r") as handle:
            assert await handle.readlines() == ["one\n", "two\n"]
            await handle.seek(0)
            assert await handle.readline() == "one\n"

    run(main())


def test_what_http_cannot_do_says_so_rather_than_failing_obscurely(dav):
    async def main():
        async with xrd.aio.open(dav.url / "d/a.root") as handle:
            assert await handle.read() == BODY
            assert handle.file is None
            with pytest.raises(UnsupportedError, match="readv needs a root://"):
                await handle.readv([(0, 5)])

    run(main())


def test_the_raw_object_is_reachable_for_anything_not_mirrored(server):
    async def main():
        async with xrd.aio.open(server.url / "data/a.root") as handle:
            assert handle.mode == "rb"
            assert handle.name.endswith("/data/a.root")
            assert "AsyncFile" in repr(handle)
            assert handle.raw.readable()

    run(main())


# ---------------------------------------------------------------------------
# Filesystems
# ---------------------------------------------------------------------------


def test_constructing_a_filesystem_touches_no_network():
    filesystem = AsyncFileSystem("root://127.0.0.1:1//")
    assert filesystem.url.host == "127.0.0.1"
    assert filesystem.endpoint == "127.0.0.1:1"
    assert "AsyncFileSystem" in repr(filesystem)
    run(filesystem.close())


def test_the_namespace_surface(server):
    async def main():
        async with AsyncFileSystem(server.url) as fs:
            await _assert_namespace_metadata(fs)
            await _assert_namespace_operations(fs)

    run(main())


async def _assert_namespace_metadata(fs):
    await fs.ping()
    assert (await fs.protocol()).version
    assert (await fs.stat("/data/a.root")).st_size == len(BODY)
    assert (await fs.statvfs("/")).nodes_rw == 1
    assert [i.is_dir() for i in await fs.statx(["/data", "/data/a.root"])] == [True, False]
    assert await fs.exists("/data/a.root") and not await fs.exists("/data/nope")
    assert await fs.isdir("/data") and await fs.isfile("/data/a.root")
    assert await fs.getsize("/data/a.root") == len(BODY)


async def _assert_namespace_operations(fs):
    assert await fs.listdir("/data") == ["a.root", "empty"]
    assert [e.name for e in await fs.scandir("/data")] == ["a.root", "empty"]
    assert (await fs.checksum("/data/a.root")).value == "1a0b045d"
    assert (await fs.query_config("version"))["version"]
    assert (await fs.locate("/data/a.root"))[0].address
    assert await fs.deep_locate("/data/a.root")
    assert await fs.prepare(["/data/a.root"])
    assert await fs.evict(["/data/a.root"])


def test_the_friendly_keywords_reach_the_synchronous_call(server):
    """The facade is a thread away from the sync one; the words have to survive."""

    async def main():
        async with AsyncFileSystem(server.url) as fs:
            listed = await fs.scandir("/data", stat=False, online=True)
            assert [e.name for e in listed] == ["a.root", "empty"]
            assert all(e.stat is None for e in listed)
            assert (await fs.locate("/data/a.root", refresh=True))[0].address
            assert await fs.prepare(["/data/a.root"], evict=True, notify=True)
            assert (await fs.query("checksum", "/data/a.root")).startswith(b"adler32 ")
            await fs.mkdir("/data/moded", "rwxr-x---")

    run(main())
    assert server.evicted == ["/data/a.root"]


def test_a_listing_can_digest_every_entry(server):
    """The digest keyword survives the trip through the executor."""

    async def main():
        async with AsyncFileSystem(server.url) as fs:
            listed = {e.name: e.checksum for e in await fs.scandir("/data", algorithm="adler32")}
            assert listed["a.root"].algorithm == "adler32"
            assert listed["a.root"].value == "1a0b045d"
            assert listed["empty"] is None  # a directory has nothing to digest

    run(main())


def test_the_server_side_bookkeeping_is_mirrored(server):
    """The calls that ask a server to stop doing something, or to label who is
    asking, are as much part of the surface as the ones that move bytes."""

    async def main():
        async with AsyncFileSystem(server.url) as fs:
            handle = await fs.prepare(["/data/a.root"])
            assert (await fs.query_prepare(handle, ["/data/a.root"]))[0].online
            assert (await fs.archive_info(["/data/a.root"]))[0].state == "ONLINE"
            await fs.cancel_prepare(handle)
            await fs.checksum_cancel("/data/a.root")
            assert (await fs.query_stats("io")) == '<statistics sel="io"/>'
            assert (await fs.query_space("/data")).name == "public"
            await fs.set_property("monitor off")
            await fs.appid("async-analysis")

    run(main())
    assert server.cancelled_prepares == ["prep-0001"]
    assert server.cancelled_checksums == ["/data/a.root"]
    assert server.properties == ["monitor off", "appid async-analysis"]


def test_listing_is_lazy_and_asynchronous(server):
    server.add_file("/data/sub/deep.bin", b"x")

    async def main():
        async with AsyncFileSystem(server.url) as fs:
            names = [entry.name async for entry in fs.iterdir("/data")]
            roots = [root async for root, _dirs, _files in fs.walk("/data")]
            matched = [path async for path in fs.glob("/data/*.root")]
            return names, roots, matched

    names, roots, matched = run(main())
    assert names == ["a.root", "empty", "sub"]
    assert roots == ["/data", "/data/empty", "/data/sub"]
    assert matched == ["/data/a.root"]


def test_an_iteration_stopped_early_stops_the_walk_too(server):
    """``break`` must not have listed the rest of the tree first."""
    for index in range(4):
        server.add_file(f"/data/tree/{index}/f.bin", b"x")

    async def main():
        async with AsyncFileSystem(server.url) as fs:
            async for root, _dirs, _files in fs.walk("/data/tree"):
                return root

    assert run(main()) == "/data/tree"


def test_the_mutation_surface(server):
    async def main():
        async with AsyncFileSystem(server.url) as fs:
            await fs.mkdir("/data/new")
            await fs.makedirs("/data/new/deep/deeper", exist_ok=True)
            await fs.touch("/data/new/f.bin")
            assert await fs.write_bytes("/data/new/f.bin", b"payload") == 7
            assert await fs.read_bytes("/data/new/f.bin") == b"payload"
            assert await fs.write_text("/data/new/t.txt", "text") == 4
            assert await fs.read_text("/data/new/t.txt") == "text"
            await fs.truncate("/data/new/f.bin", 3)
            await fs.chmod("/data/new/f.bin", 0o640)
            await fs.rename("/data/new/f.bin", "/data/new/g.bin")
            await fs.remove("/data/new/g.bin")
            await fs.rmdir("/data/new/deep/deeper")
            await fs.rmtree("/data/new")

    run(main())
    assert "/data/new" not in server.dirs


def test_the_optional_arguments_are_passed_through_not_swallowed(server):
    server.add_file("/data/tree/deep/f.bin", b"x")

    async def main():
        async with AsyncFileSystem(server.url) as fs:
            assert await fs.query(QueryCode.CONFIG, "version")  # raw, undecoded
            listed = await fs.scandir("/data", flags=DirListFlags.NONE)
            assert [e.name for e in listed] == ["a.root", "empty", "tree"]
            bottom = [root async for root, _d, _f in fs.walk("/data/tree", topdown=False)]
            assert bottom == ["/data/tree/deep", "/data/tree"]
            assert [p async for p in fs.glob("*.root", root="/data")] == ["/data/a.root"]
            await fs.mkdir("/data/x/y", parents=True)
            await fs.touch("/data/a.root")  # exists already, and exist_ok defaults true
            with pytest.raises(FileExistsError):
                await fs.touch("/data/a.root", exist_ok=False)
            await fs.rmtree("/data/nowhere", ignore_errors=True)
            await fs.rmtree("/data/x")

    run(main())
    assert server.contents("/data/a.root") == BODY


def test_extended_attributes(server):
    async def main():
        async with AsyncFileSystem(server.url) as fs:
            await fs.setxattr("/data/a.root", "run", b"42")
            assert await fs.getxattr("/data/a.root", "run") == b"42"
            assert await fs.listxattr("/data/a.root") == ["run"]
            assert await fs.xattrs("/data/a.root") == {"run": b"42"}
            assert await fs.listxattr_tree("/data") == {"a.root": ["run"]}
            assert "setattr" in await fs.extensions()
            await fs.removexattr("/data/a.root", "run")
            assert await fs.xattrs("/data/a.root") == {}

    run(main())


def test_a_filesystem_opens_files_both_ways(server):
    async def main():
        async with AsyncFileSystem(server.url) as fs:
            async with fs.open("/data/a.root") as handle:
                assert await handle.read() == BODY
            handle = await fs.open("data/a.root")  # relative, as in the sync API
            assert await handle.read(5) == b"hello"
            await handle.close()

    run(main())


def test_a_mirror_can_share_an_existing_connection(server):
    filesystem = xrd.FileSystem(server.url)
    try:
        mirror = AsyncFileSystem.wrap(filesystem)
        assert mirror.sync is filesystem
        assert mirror.config is filesystem.config
        assert run(mirror.read_bytes("/data/a.root")) == BODY
    finally:
        filesystem.close()


def test_errors_arrive_as_the_same_exceptions(server):
    async def main():
        async with AsyncFileSystem(server.url) as fs:
            with pytest.raises(FileNotFoundError):
                await fs.stat("/data/nope")

    run(main())


def test_webdav_endpoints_work_through_the_same_facade(dav):
    async def main():
        async with AsyncFileSystem(dav.url) as fs:
            assert await fs.listdir("/d") == ["a.root"]
            assert await fs.read_bytes("/d/a.root") == BODY
            await fs.write_bytes("/d/b.bin", b"put")
            with pytest.raises(UnsupportedError):
                await fs.statvfs("/")

    run(main())
    assert dav.contents("/d/b.bin") == b"put"


# ---------------------------------------------------------------------------
# Concurrency and copying
# ---------------------------------------------------------------------------


def test_two_endpoints_are_read_at_the_same_time(server):
    """The loop is never blocked, so separate connections overlap."""

    async def main():
        with FakeServer(files={"/data/b.root": b"second"}) as other:
            async with AsyncFileSystem(server.url) as first, AsyncFileSystem(other.url) as second:
                return await asyncio.gather(
                    first.read_bytes("/data/a.root"), second.read_bytes("/data/b.root")
                )

    assert run(main()) == [BODY, b"second"]


def test_one_endpoint_serialises_rather_than_corrupting(server):
    """Concurrent awaits on one session are safe; the session's lock sees to it."""
    for index in range(6):
        server.add_file(f"/data/n{index}.bin", str(index).encode() * 4)

    async def main():
        async with AsyncFileSystem(server.url) as fs:
            return await asyncio.gather(*(fs.read_bytes(f"/data/n{i}.bin") for i in range(6)))

    assert run(main()) == [str(i).encode() * 4 for i in range(6)]


def test_copying_is_awaitable(server, tmp_path):
    async def main():
        result = await xrd.aio.copy(server.url / "data/a.root", tmp_path / "out.root")
        assert result.size == len(BODY)
        results = await xrd.aio.copy_tree(server.url / "data", tmp_path / "tree")
        assert [r.size for r in results] == [len(BODY)]
        with FakeServer() as destination:
            pushed = await xrd.aio.third_party(
                server.url / "data/a.root", destination.url / "pulled.root"
            )
            assert pushed.size == len(BODY)

    run(main())
    assert (tmp_path / "out.root").read_bytes() == BODY
    assert (tmp_path / "tree/a.root").read_bytes() == BODY


def test_an_async_file_is_its_own_context_manager(server):
    """``await open(...)`` then ``async with`` on the result: still closed once."""

    async def main():
        handle = await xrd.aio.open(server.url / "data/a.root")
        async with handle as inner:
            assert inner is handle
            assert await inner.read() == BODY
        return handle

    handle = run(main())
    assert handle.closed


def test_leaving_an_unentered_open_alone_closes_nothing(server):
    """``__aexit__`` with nothing opened is the path a failed ``__aenter__`` takes."""

    async def main():
        opening = xrd.aio.open(server.url / "data/a.root")
        await opening.__aexit__(None, None, None)

    run(main())


# ---------------------------------------------------------------------------
# Links and checkpoints
# ---------------------------------------------------------------------------


def test_the_link_family_is_mirrored(server):
    async def main():
        async with AsyncFileSystem(server.url) as fs:
            await fs.symlink("/data/a.root", "/data/soft")
            assert await fs.readlink("/data/soft") == "/data/a.root"
            await fs.link("/data/a.root", "/data/hard")
            await fs.hardlink("/data/a.root", "/data/harder")

    run(main())
    assert server.links["/data/soft"] == "/data/a.root"
    assert server.contents("/data/harder") == BODY


def test_the_times_and_the_owner_are_set_from_a_coroutine(server):
    async def main():
        async with AsyncFileSystem(server.url) as fs:
            await fs.utime(
                "/data/a.root", ns=(1_000_000_000_000_000_001, 2_000_000_000_000_000_002)
            )
            await fs.chown("/data/a.root", 1000, 1000)

    run(main())
    assert server.times["/data/a.root"] == (1_000_000_000_000_000_001, 2_000_000_000_000_000_002)
    assert server.owners["/data/a.root"] == (1000, 1000)


def test_lstat_describes_the_link_and_stat_what_it_points_at(server):
    async def main():
        async with AsyncFileSystem(server.url) as fs:
            await fs.symlink("/data/a.root", "/data/soft")
            assert await fs.is_symlink("/data/soft")
            assert not await fs.is_symlink("/data/a.root")
            followed = await fs.stat("/data/soft")
            return followed, await fs.lstat("/data/soft")

    followed, itself = run(main())
    assert followed.st_size == len(BODY)
    assert itself.st_size == len("/data/a.root")


def test_a_checkpoint_commits_what_the_block_wrote(server):
    from xrd.proto import constants as c

    async def main():
        async with xrd.aio.open(server.url / "data/a.root", "r+b") as handle:
            async with handle.checkpoint() as checkpoint:
                await handle.write(b"HELLO")
                await handle.flush()
                info = await checkpoint.query()
                assert info.used == 5
                assert repr(checkpoint).startswith("AsyncCheckpoint(")

    run(main())
    assert server.contents("/data/a.root") == b"HELLO world"
    assert server.seen.count(c.kXR_chkpoint) == 4  # begin, the wrapped write, query, commit


def test_a_checkpoint_rolls_back_what_raised(server):
    async def main():
        async with xrd.aio.open(server.url / "data/a.root", "r+b") as handle:
            with pytest.raises(ZeroDivisionError):
                async with handle.checkpoint():
                    await handle.write(b"HELLO")
                    await handle.flush()
                    raise ZeroDivisionError("the block did not like what it saw")

    run(main())
    assert server.contents("/data/a.root") == BODY


def test_a_checkpoint_needs_a_root_endpoint(dav):
    async def main():
        async with xrd.aio.open(str(dav.url) + "d/a.root") as handle:
            with pytest.raises(UnsupportedError, match="checkpoint"):
                async with handle.checkpoint():
                    pass

    run(main())
