from __future__ import annotations

import pytest

from xrdclient.errors import ProtocolError
from xrdclient.proto.buffer import Reader, Writer


def test_reader_reads_every_width_big_endian():
    r = Reader(bytes.fromhex("ff" "fffe" "fffffffd" "fffffffffffffffc"))
    assert r.u8() == 0xFF
    assert r.u16() == 0xFFFE
    assert r.u32() == 0xFFFFFFFD
    assert r.u64() == 0xFFFFFFFFFFFFFFFC
    assert r.remaining == 0


def test_reader_signed_widths():
    r = Reader(bytes.fromhex("fffe" "fffffffd" "fffffffffffffffc"))
    assert r.i16() == -2
    assert r.i32() == -3
    assert r.i64() == -4


def test_reader_tracks_position_and_length():
    r = Reader(b"abcdef")
    assert (len(r), r.position, r.remaining) == (6, 0, 6)
    r.skip(2)
    assert (r.position, r.remaining) == (2, 4)
    assert r.rest() == b"cdef"
    assert r.remaining == 0


def test_reader_bytes_and_view_share_no_surprises():
    r = Reader(bytearray(b"abcdef"))
    assert r.bytes(3) == b"abc"
    view = r.view(3)
    assert bytes(view) == b"def"


def test_cstring_stops_at_the_nul():
    assert Reader(b"hello\x00world").cstring() == "hello"


def test_cstring_with_a_fixed_width_consumes_the_padding():
    r = Reader(b"user\x00\x00\x00\x00rest")
    assert r.cstring(8) == "user"
    assert r.rest() == b"rest"


def test_overrun_names_the_frame_and_the_offset():
    r = Reader(b"ab", "kXR_stat")
    with pytest.raises(ProtocolError) as info:
        r.u32()
    text = str(info.value)
    assert "kXR_stat" in text
    assert "2" in text


def test_negative_reads_are_rejected():
    with pytest.raises(ProtocolError):
        Reader(b"abcd").bytes(-1)


def test_writer_is_chainable_and_big_endian():
    out = Writer().u8(1).u16(2).u32(3).i64(-1).bytes()
    assert out == bytes.fromhex("01" "0002" "00000003" "ffffffffffffffff")


def test_writer_len_tracks_the_buffer():
    w = Writer().u32(0)
    assert len(w) == 4


def test_padded_truncates_and_pads():
    assert Writer().padded("user", 8).bytes() == b"user\x00\x00\x00\x00"
    assert Writer().padded("averylongname", 8).bytes() == b"averylon"
    assert Writer().padded(b"raw", 4).bytes() == b"raw\x00"


def test_text_can_append_a_nul():
    assert Writer().text("ab").bytes() == b"ab"
    assert Writer().text("ab", nul=True).bytes() == b"ab\x00"


def test_zeros_and_raw():
    assert Writer().zeros(3).raw(b"x").bytes() == b"\x00\x00\x00x"


def test_writer_round_trips_through_reader():
    data = Writer().u32(0xDEADBEEF).text("path", nul=True).i64(-9).bytes()
    r = Reader(data)
    assert r.u32() == 0xDEADBEEF
    assert r.cstring() == "path"
    assert r.i64() == -9
