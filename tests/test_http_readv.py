"""Vector reads over HTTP: one multi-range ``GET``, and what to do without one.

Checked against :class:`~xrdclient.testing.FakeDAVServer`, which answers a
request for several ranges with ``multipart/byteranges`` - or, with a knob
set, the way less capable servers do: one coalesced part, only the first
range, or the whole file.
"""

from __future__ import annotations

import functools
import io

import pytest
from xrdhttp import ranges as _ranges
from xrdhttp.testing import byteranges

from xrdclient.config import Config
from xrdclient.errors import ProtocolError, TooLargeError, TransientError
from xrdclient.fsspec_impl import HTTPXRootDFileSystem
from xrdclient.http.file import HTTPRawIO
from xrdclient.s3 import Credentials, S3FileSystem
from xrdclient.testing import FakeDAVServer, FakeS3Server
from xrdclient.testing.http import Reply
from xrdclient.types import ReadRange

DATA = bytes(range(256)) * 40  # 10 KiB that says where every byte came from

#: Overlapping, touching, repeated, out of order and empty, all at once.
RANGES = [(0, 5), (100, 50), (120, 10), (150, 7), (9000, 100), (3, 4), (5000, 0), (0, 5)]


def _expected(ranges: list[tuple[int, int]]) -> list[bytes]:
    return [DATA[offset : offset + length] for offset, length in ranges]


@pytest.fixture
def dav():
    with FakeDAVServer(files={"/f": DATA}) as server:
        yield server


@pytest.fixture
def raw(dav):
    with HTTPRawIO(dav.url / "f") as handle:
        yield handle


def _gets(dav: FakeDAVServer) -> list[str | None]:
    """The ``Range`` of every ``GET`` served so far."""
    return [
        headers.get("Range")
        for (method, _path), headers in zip(dav.seen, dav.headers, strict=True)
        if method == "GET"
    ]


def test_every_range_comes_back_from_one_request(dav, raw):
    assert raw.readv(RANGES) == _expected(RANGES)
    assert _gets(dav) == ["bytes=0-6,100-156,9000-9099"]  # merged, sorted, empty dropped


def test_read_ranges_and_tuples_are_both_accepted(raw):
    assert raw.readv([ReadRange(1, 2), (3, 1)]) == [DATA[1:3], DATA[3:4]]


def test_nothing_asked_is_nothing_fetched(dav, raw):
    assert raw.readv([]) == []
    assert raw.readv([(10, 0)]) == [b""]
    assert _gets(dav) == []


def test_the_file_position_does_not_move(raw):
    assert raw.read(4) == DATA[:4]
    raw.readv([(1000, 10), (2000, 10)])
    assert raw.tell() == 4
    assert raw.read(4) == DATA[4:8]


def test_a_vector_read_too_big_to_hold_is_refused(dav):
    with (
        HTTPRawIO(dav.url / "f", config=Config(max_read_size=100)) as handle,
        pytest.raises(TooLargeError),
    ):
        handle.readv([(0, 60), (100, 60)])


@pytest.mark.parametrize("bad", [(-1, 4), (4, -1)])
def test_a_negative_range_is_a_caller_error(raw, bad):
    with pytest.raises(ValueError, match="invalid range"):
        raw.readv([bad])


def test_a_file_open_for_writing_has_no_vector_read(dav):
    with HTTPRawIO(dav.url / "new", "wb") as handle, pytest.raises(io.UnsupportedOperation):
        handle.readv([(0, 1)])


def test_many_ranges_go_out_in_batches(dav, raw, monkeypatch):
    monkeypatch.setattr(_ranges, "MAX_SPANS", 2)
    ranges = [(offset, 3) for offset in range(0, 1000, 100)]
    assert raw.readv(ranges) == _expected(ranges)
    assert len(_gets(dav)) == 5


def test_ranges_past_the_end_are_short_or_empty(dav, raw):
    ranges = [(len(DATA) - 2, 10), (len(DATA) + 5, 3), (0, 2)]
    assert raw.readv(ranges) == [DATA[-2:], b"", DATA[:2]]


def test_ranges_all_past_the_end_are_all_empty(dav, raw):
    assert raw.readv([(len(DATA) + 1, 2), (len(DATA) + 10, 2)]) == [b"", b""]
    # 416 says nothing against multi-range requests: the next one still asks.
    raw.readv([(0, 1), (10, 1)])
    assert _gets(dav)[-1] == "bytes=0-0,10-10"


def test_a_server_that_coalesces_ranges_is_taken_at_its_word(dav, raw):
    dav.coalesce_ranges = True
    ranges = [(10, 5), (40, 5)]
    assert raw.readv(ranges) == _expected(ranges)
    assert raw.readv(ranges) == _expected(ranges)
    assert _gets(dav) == ["bytes=10-14,40-44"] * 2


def test_a_server_that_serves_one_range_gets_the_rest_one_at_a_time(dav, raw):
    dav.multipart_ranges = False
    ranges = [(10, 5), (40, 5), (70, 5)]
    assert raw.readv(ranges) == _expected(ranges)
    assert raw.readv(ranges) == _expected(ranges)
    # The first answer covered one range; the rest came singly, and the next
    # vector read did not ask for several again.
    assert _gets(dav) == [
        "bytes=10-14,40-44,70-74",
        "bytes=40-44",
        "bytes=70-74",
        "bytes=10-14",
        "bytes=40-44",
        "bytes=70-74",
    ]


def test_a_server_that_ignores_ranges_is_not_read_to_the_end(dav, raw):
    dav.ignore_ranges = True
    ranges = [(10, 5), (9000, 5)]
    assert raw.readv(ranges) == _expected(ranges)
    assert raw.readv(ranges) == _expected(ranges)
    singly = ["bytes=10-14", "bytes=9000-9004"]
    assert _gets(dav) == ["bytes=10-14,9000-9004", *singly, *singly]


def test_s3_objects_are_read_one_range_at_a_time():
    """S3 sends the whole object for several ranges, so it is never asked."""
    with FakeS3Server(objects={"k": DATA}) as server:
        credentials = Credentials(server.access_key, server.secret_key)
        with (
            S3FileSystem(server.url, credentials=credentials, endpoint=server.endpoint) as fs,
            fs.open("/k", "rb", buffering=0) as handle,
        ):
            assert isinstance(handle, HTTPRawIO)
            assert handle.readv([(1, 3), (500, 4)]) == [DATA[1:4], DATA[500:504]]
    # One request per range: never one for both that S3 would answer in full.
    assert [method for method, path, _query in server.seen if path.endswith("/k")] == [
        "GET",
        "GET",
    ]


def test_fsspec_cat_ranges_is_one_vector_read(dav):
    filesystem = HTTPXRootDFileSystem(str(dav.url))
    try:
        got = filesystem.cat_ranges(["/f", "/f", "/f"], [0, 100, 5000], [4, 110, 5000])
    finally:
        filesystem.close()
    assert got == [DATA[:4], DATA[100:110], b""]
    assert _gets(dav) == ["bytes=0-3,100-109"]


# ----------------------------------------------------------------------
# Hostile and broken answers
# ----------------------------------------------------------------------


_multipart = functools.partial(
    byteranges, size=len(DATA), boundary="b0undary", preamble=b"preamble that says nothing\r\n"
)


def _answer(dav: FakeDAVServer, status: int, body: bytes, **headers: str) -> None:
    """Answer the next multi-range ``GET`` with a canned reply."""

    def handler(method: str, path: str, request: dict[str, str]) -> Reply | None:
        if "," not in request.get("Range", ""):
            return None
        return status, body, headers

    dav.handlers["GET"] = handler


MULTIPART = {"Content-Type": "multipart/byteranges; boundary=b0undary"}


def test_padding_and_parts_in_any_order_are_fine(dav, raw):
    body = _multipart((40, DATA[40:45]), (10, DATA[10:15])).replace(b"\r\n--", b"\r\n  \r\n--")
    _answer(dav, 206, body, **MULTIPART)
    assert raw.readv([(10, 5), (40, 5)]) == [DATA[10:15], DATA[40:45]]


def test_a_part_that_covers_a_span_partly_leaves_it_to_a_single_request(dav, raw):
    _answer(dav, 206, _multipart((10, DATA[10:15]), (40, DATA[40:42])), **MULTIPART)
    assert raw.readv([(10, 5), (40, 5)]) == [DATA[10:15], DATA[40:45]]
    assert _gets(dav)[-1] == "bytes=40-44"


@pytest.mark.parametrize(
    ("body", "headers", "error", "match"),
    [
        (_multipart((5000, DATA[5000:5005])), MULTIPART, ProtocolError, "not asked for"),
        (_multipart((10, DATA[10:4000])), MULTIPART, ProtocolError, "not asked for"),
        (
            b"--b0undary\r\nContent-Type: x\r\n\r\nhello\r\n--b0undary--\r\n",
            MULTIPART,
            ProtocolError,
            "no usable Content-Range",
        ),
        (
            b"--b0undary\r\nContent-Range: bytes 15-10/100\r\n\r\n\r\n--b0undary--\r\n",
            MULTIPART,
            ProtocolError,
            "no usable Content-Range",
        ),
        (
            _multipart((10, DATA[10:15])),
            {"Content-Type": "multipart/byteranges"},
            ProtocolError,
            "no boundary",
        ),
        (b"x" * (10 * 1024), MULTIPART, ProtocolError, "too long"),
        ((b"\r\n" * 200_000), MULTIPART, ProtocolError, "too long"),
        (_multipart((10, DATA[10:15]), closing=False), MULTIPART, TransientError, "closed"),
        (
            _multipart((10, DATA[10:15])).replace(b"\r\n--b0undary--", b"extra\r\n--b0undary--"),
            MULTIPART,
            ProtocolError,
            "ran past",
        ),
        (DATA[10:15], {}, ProtocolError, "no Content-Range"),
    ],
    ids=[
        "outside",
        "too-much",
        "no-content-range",
        "backwards",
        "no-boundary",
        "endless-line",
        "endless-framing",
        "truncated",
        "overrun",
        "single-without-range",
    ],
)
def test_a_malformed_multipart_answer_is_refused(dav, raw, body, headers, error, match):
    _answer(dav, 206, body, **headers)
    with pytest.raises(error, match=match):
        raw.readv([(10, 5), (40, 5)])


def test_a_part_cut_off_mid_data_is_a_dropped_connection(dav, raw):
    body = _multipart((10, DATA[10:15]), closing=False)[:-4]  # inside the part's bytes
    _answer(dav, 206, body, **MULTIPART)
    with pytest.raises(TransientError, match="inside the part"):
        raw.readv([(10, 5), (40, 5)])


def test_a_single_range_answer_must_start_where_it_was_asked(dav, raw):
    dav.handlers["GET"] = lambda method, path, request: (
        206,
        DATA[11:16],
        {"Content-Range": f"bytes 11-15/{len(DATA)}"},
    )
    with pytest.raises(ProtocolError, match="bytes 10-"):
        raw.readv([(10, 5)])
