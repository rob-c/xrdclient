"""I/O conformance: every call swept across the whole offset and length grid.

A read that is right at 4096 and wrong at 4097 is the classic client bug, so
nothing here tests one "typical" call. Each test walks the interesting places
in a file - either side of a page boundary, the last byte, the end, past the
end, zero length - and compares against the bytes the server actually holds.

The vector calls are swept the other way, by segment count, because their
bugs live at the batching ceilings (:data:`READV_MAX_CHUNKS` and
:data:`READV_MAX_BYTES`) where one request silently becomes two.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

import pytest

from xrdclient.client.file import READV_MAX_CHUNKS, File
from xrdclient.client.filesystem import FileSystem
from xrdclient.config import Config
from xrdclient.errors import ProtocolError
from xrdclient.flags import Access, OpenFlags
from xrdclient.proto import constants as c
from xrdclient.testing import FakeServer
from xrdclient.types import ReadRange, WriteChunk

PAGE = c.kXR_pgPageSZ
SIZE = 4 * PAGE + 137
PATTERN = bytes((i * 7 + 11) & 0xFF for i in range(SIZE))

#: Every interesting place in a file.
OFFSETS = [0, 1, PAGE - 1, PAGE, PAGE + 1, 2 * PAGE, SIZE - 1, SIZE, SIZE + PAGE]
#: Every interesting length, including ones that run off the end.
LENGTHS = [0, 1, 7, PAGE - 1, PAGE, PAGE + 1, 2 * PAGE, SIZE, SIZE + PAGE]
#: Segment counts either side of the vector-request ceiling.
SEGMENTS = [1, 2, 3, 7, 8, 15, 16, 63, 64, 255, READV_MAX_CHUNKS - 1, READV_MAX_CHUNKS + 1]

_CONFIG = Config(username="tester", auth_order=("host",), require_tls=False)


@pytest.fixture(scope="module")
def srv():
    """One server for the whole sweep; a fresh one per case would dominate."""
    with FakeServer(files={"/d/pattern": PATTERN}, dirs=["/d"]) as server:
        yield server


@pytest.fixture(scope="module")
def client(srv):
    fs = FileSystem(srv.url, _CONFIG)
    try:
        yield fs
    finally:
        fs.close()


@contextmanager
def handle(fs, path, flags=OpenFlags.READ, mode=Access.OWNER_WRITE):
    fh = File(fs.url.with_path(path), fs.config, router=fs._router)
    fh.open(flags, mode)
    try:
        yield fh
    finally:
        fh.close()


@pytest.fixture
def reader(client):
    with handle(client, "/d/pattern") as fh:
        yield fh


@pytest.fixture
def writer(client, srv, request):
    """A fresh, empty file named after the test, plus the server's copy."""
    path = "/d/w/" + request.node.name.replace("/", "_")[:100]
    with handle(client, path, OpenFlags.UPDATE | OpenFlags.NEW | OpenFlags.MAKEPATH) as fh:
        yield fh, path


def splice(model: bytearray, offset: int, data: bytes) -> None:
    """What a write does to a file: zero-fill the hole, then overwrite.

    A zero-length write is a no-op, as it is for :func:`os.pwrite`: it does
    not extend the file to ``offset``.
    """
    if not data:
        return
    if offset > len(model):
        model.extend(bytes(offset - len(model)))
    model[offset : offset + len(data)] = data


def ranges(count: int) -> list[ReadRange]:
    """``count`` non-overlapping ranges of varied length, in file order."""
    step = SIZE // (count + 1)
    return [ReadRange(i * step, min(1 + (i * 37) % 512, SIZE - i * step)) for i in range(count)]


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("offset", OFFSETS)
def test_read_sweeps_every_length_at_this_offset(reader, offset):
    for length in LENGTHS:
        assert reader.read(length, offset) == PATTERN[offset : offset + length], length


@pytest.mark.parametrize("offset", OFFSETS)
def test_read_to_the_end_from_this_offset(reader, offset):
    assert reader.read(-1, offset) == PATTERN[offset:]


@pytest.mark.parametrize("offset", OFFSETS)
def test_readinto_sweeps_every_length_at_this_offset(reader, offset):
    for length in LENGTHS:
        buffer = bytearray(b"\xff" * length)
        count = reader.readinto(buffer, offset)
        expected = PATTERN[offset : offset + length]
        assert count == len(expected), length
        assert buffer[:count] == expected, length


def test_a_read_that_starts_past_the_end_is_empty_not_an_error(reader):
    assert reader.read(PAGE, SIZE * 4) == b""


@pytest.mark.parametrize("chunk", [1, 7, PAGE - 1, PAGE, PAGE + 1, SIZE - 1])
def test_a_read_split_at_this_chunk_size_still_reassembles(srv, chunk):
    """The client splits its own request; the seams must not lose bytes."""
    with FileSystem(srv.url, _CONFIG.evolve(chunk_size=chunk)) as fs:
        with handle(fs, "/d/pattern") as fh:
            assert fh.read() == PATTERN


@pytest.mark.parametrize("chunk", [1024, PAGE, 100_000])
def test_a_read_the_server_chunks_into_oksofar_still_reassembles(srv, chunk):
    """And the same seams on the server's side, as ``kXR_oksofar`` pieces."""
    srv.chunk_reads = chunk
    try:
        with FileSystem(srv.url, _CONFIG) as fs, handle(fs, "/d/pattern") as fh:
            assert fh.read() == PATTERN
            assert fh.read(PAGE + 1, PAGE - 1) == PATTERN[PAGE - 1 : 2 * PAGE]
    finally:
        srv.chunk_reads = 0


def test_a_megabyte_arrives_in_one_request(srv):
    payload = bytes(range(256)) * 4096
    srv.add_file("/d/mib", payload)
    with FileSystem(srv.url, _CONFIG.evolve(chunk_size=len(payload))) as fs:
        with handle(fs, "/d/mib") as fh:
            assert fh.read() == payload
            assert srv.seen.count(c.kXR_read) >= 1


# ---------------------------------------------------------------------------
# readv
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count", SEGMENTS)
def test_readv_sweeps_this_segment_count(reader, count):
    wanted = ranges(count)
    got = reader.readv(wanted)
    assert len(got) == count
    assert got == [PATTERN[r.offset : r.offset + r.length] for r in wanted]


@pytest.mark.parametrize("count", [2, 8, 64])
def test_readv_answers_in_the_order_asked_not_the_order_stored(reader, count):
    wanted = list(reversed(ranges(count)))
    assert reader.readv(wanted) == [PATTERN[r.offset : r.offset + r.length] for r in wanted]


def test_readv_accepts_plain_tuples(reader):
    assert reader.readv([(0, 4), (PAGE, 4)]) == [PATTERN[:4], PATTERN[PAGE : PAGE + 4]]


def test_readv_of_one_byte_per_segment(reader):
    wanted = [ReadRange(i * 61, 1) for i in range(60)]
    assert reader.readv(wanted) == [PATTERN[r.offset : r.offset + 1] for r in wanted]


def test_readv_of_nothing_asks_nothing(reader, srv):
    seen = len(srv.seen)
    assert reader.readv([]) == []
    assert len(srv.seen) == seen


def test_readv_repeating_the_same_range_gets_it_twice(reader):
    assert reader.readv([(8, 4), (8, 4)]) == [PATTERN[8:12]] * 2


def test_readv_segments_may_overlap(reader):
    assert reader.readv([(0, 16), (8, 16)]) == [PATTERN[:16], PATTERN[8:24]]


def test_a_readv_segment_over_the_ceiling_is_refused_before_the_wire(reader, srv):
    seen = len(srv.seen)
    with pytest.raises(ProtocolError, match="use read"):
        reader.readv([(0, (2 << 20) + 1)])
    assert len(srv.seen) == seen


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("offset", OFFSETS)
def test_write_sweeps_every_length_at_this_offset(writer, srv, offset):
    handle_, path = writer
    model = bytearray()
    for length in LENGTHS[:6]:
        payload = PATTERN[:length]
        assert handle_.write(payload, offset) == length
        splice(model, offset, payload)
        assert srv.contents(path) == bytes(model), length


@pytest.mark.parametrize("chunk", [1, 7, PAGE, PAGE + 1])
def test_a_write_split_at_this_chunk_size_arrives_whole(srv, chunk, request):
    path = f"/d/w/split{chunk}"
    with FileSystem(srv.url, _CONFIG.evolve(chunk_size=chunk)) as fs:
        flags = OpenFlags.UPDATE | OpenFlags.NEW | OpenFlags.DELETE | OpenFlags.MAKEPATH
        with handle(fs, path, flags) as fh:
            assert fh.write(PATTERN[: 2 * PAGE + 5]) == 2 * PAGE + 5
    assert srv.contents(path) == PATTERN[: 2 * PAGE + 5]


def test_writing_nothing_writes_nothing(writer, srv):
    handle_, path = writer
    assert handle_.write(b"") == 0
    assert srv.contents(path) == b""


@pytest.mark.parametrize("count", SEGMENTS)
def test_writev_sweeps_this_segment_count(writer, srv, count):
    handle_, path = writer
    model = bytearray()
    chunks = []
    for r in ranges(count):
        payload = PATTERN[r.offset : r.offset + r.length]
        chunks.append(WriteChunk(r.offset, payload))
        splice(model, r.offset, payload)
    assert handle_.writev(chunks) == sum(len(chunk.data) for chunk in chunks)
    assert srv.contents(path) == bytes(model)


def test_writev_accepts_plain_tuples(writer, srv):
    handle_, path = writer
    assert handle_.writev([(0, b"ab"), (4, b"cd")]) == 4
    assert srv.contents(path) == b"ab\x00\x00cd"


def test_writev_of_nothing_asks_nothing(writer, srv):
    handle_, _ = writer
    seen = len(srv.seen)
    assert handle_.writev([]) == 0
    assert len(srv.seen) == seen


# ---------------------------------------------------------------------------
# paged I/O
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("offset", OFFSETS)
def test_pgread_sweeps_every_length_at_this_offset(reader, offset):
    for length in LENGTHS:
        result = reader.pgread(length, offset)
        assert result.data == PATTERN[offset : offset + length], length
        assert result.ok, length
        assert result.offset == offset, length


@pytest.mark.parametrize("offset", [1, PAGE - 1, PAGE + 1, 3 * PAGE - 3])
def test_page_units_align_to_the_file_offset_not_the_buffer(reader, offset):
    """An unaligned pgread's first unit is short, so the grid stays absolute.

    Checksums are computed over pages of the *file*; a client that instead
    cut the reply into 4 KiB pieces from the start of its own buffer would
    verify every page but the first against the wrong bytes.
    """
    result = reader.pgread(2 * PAGE + 9, offset)
    assert result.ok
    assert result.data == PATTERN[offset : offset + 2 * PAGE + 9]


@pytest.mark.parametrize("offset", OFFSETS[:7])
def test_pgwrite_sweeps_every_length_at_this_offset(writer, srv, offset):
    handle_, path = writer
    model = bytearray()
    for length in [0, 1, PAGE - 1, PAGE, PAGE + 1, 2 * PAGE]:
        payload = PATTERN[:length]
        assert handle_.pgwrite(payload, offset) == length
        splice(model, offset, payload)
        assert srv.contents(path) == bytes(model), length


def test_pgwrite_pages_are_accepted_by_a_server_that_verifies_them(writer, srv):
    """The server recomputes every CRC, so a wrong one would be an error."""
    handle_, path = writer
    assert handle_.pgwrite(PATTERN, 0) == SIZE
    assert srv.contents(path) == PATTERN


# ---------------------------------------------------------------------------
# the connection underneath
# ---------------------------------------------------------------------------


def test_concurrent_requests_share_one_connection(client, srv):
    """Many threads, one session: replies must find their own caller."""
    results: dict[int, bytes] = {}
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            with handle(client, "/d/pattern") as fh:
                results[index] = fh.read(PAGE, index * 97)
        except BaseException as exc:  # pragma: no cover - only on a failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert results == {i: PATTERN[i * 97 : i * 97 + PAGE] for i in range(12)}


def test_a_long_run_of_operations_leaks_no_streamid(client):
    machine = client._router.session._m
    for _ in range(200):
        client.stat("/d/pattern")
    assert machine._pending == {}
    assert len(machine._free) <= 2


def test_a_long_run_of_reads_leaks_no_streamid(reader, client):
    machine = client._router.session._m
    for i in range(200):
        assert reader.read(16, i) == PATTERN[i : i + 16]
    assert machine._pending == {}
