"""The ``io`` layer: mode parsing, the raw object, and what ``open`` returns."""

from __future__ import annotations

import io

import pytest

from xrdclient.client.file import File
from xrdclient.flags import OpenFlags
from xrdclient.io import DEFAULT_BUFFER_SIZE, XRootDRawIO, flags_for_mode, open_url, parse_mode
from xrdclient.testing import FakeServer


@pytest.fixture
def raw(fs):
    """A raw reader on the pre-seeded file."""
    handle = File(fs.url.with_path("/data/a.root"), fs.config, router=fs._router)
    stream = XRootDRawIO(handle, "rb")
    try:
        yield stream
    finally:
        stream.close()


# ---------------------------------------------------------------------------
# parse_mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("r", ("r", False, False)),
        ("rb", ("r", True, False)),
        ("rt", ("r", False, False)),
        ("r+", ("r", False, True)),
        ("rb+", ("r", True, True)),
        ("w", ("w", False, False)),
        ("wb", ("w", True, False)),
        ("x", ("x", False, False)),
        ("a", ("a", False, False)),
        ("ab+", ("a", True, True)),
        ("br", ("r", True, False)),
        ("rU", ("r", False, False)),
    ],
)
def test_parse_mode_matches_the_builtin_vocabulary(mode, expected):
    assert parse_mode(mode) == expected


@pytest.mark.parametrize("mode", ["", "rw", "rbt", "+", "b", "rz"])
def test_a_nonsense_mode_is_refused(mode):
    with pytest.raises(ValueError):
        parse_mode(mode)


def test_text_is_the_default_just_like_the_builtin():
    """``open(path, "r")`` gives text; only a ``b`` makes it binary."""
    assert parse_mode("r")[1] is False
    assert parse_mode("rb")[1] is True


# ---------------------------------------------------------------------------
# flags_for_mode
# ---------------------------------------------------------------------------


def test_read_mode_asks_only_for_read():
    assert flags_for_mode("rb") == OpenFlags.READ


def test_update_mode_asks_for_update():
    assert flags_for_mode("rb+") == OpenFlags.UPDATE


def test_write_mode_deletes_and_makes_the_path():
    flags = flags_for_mode("wb")
    assert flags & OpenFlags.DELETE
    assert flags & OpenFlags.UPDATE
    assert flags & OpenFlags.MAKEPATH


def test_exclusive_mode_asks_for_new():
    assert flags_for_mode("xb") & OpenFlags.NEW


def test_append_mode_asks_for_append():
    assert flags_for_mode("ab") & OpenFlags.APPEND


def test_makepath_can_be_declined():
    assert not flags_for_mode("wb", makepath=False) & OpenFlags.MAKEPATH


def test_posc_is_opt_in_and_only_for_writing():
    assert flags_for_mode("wb", posc=True) & OpenFlags.POSC
    assert not flags_for_mode("wb") & OpenFlags.POSC
    assert not flags_for_mode("rb", posc=True) & OpenFlags.POSC


# ---------------------------------------------------------------------------
# XRootDRawIO
# ---------------------------------------------------------------------------


def test_the_raw_object_declares_its_capabilities(raw):
    assert isinstance(raw, io.RawIOBase)
    assert raw.readable()
    assert not raw.writable()
    assert raw.seekable()
    assert not raw.isatty()
    assert raw.mode == "rb"
    assert raw.name.endswith("/data/a.root")


def test_read_advances_the_position(raw):
    assert raw.read(5) == b"hello"
    assert raw.tell() == 5
    assert raw.read() == b" world"
    assert raw.tell() == 11
    assert raw.read() == b""


def test_readinto_reports_the_count(raw):
    buffer = bytearray(5)
    assert raw.readinto(buffer) == 5
    assert bytes(buffer) == b"hello"
    assert raw.readinto(bytearray(0)) == 0


def test_seek_accepts_all_three_whences(raw):
    assert raw.seek(6) == 6
    assert raw.read(5) == b"world"
    assert raw.seek(-5, io.SEEK_END) == 6
    assert raw.seek(-6, io.SEEK_CUR) == 0
    assert raw.read(5) == b"hello"


def test_seeking_before_the_start_is_an_os_error(raw):
    with pytest.raises(OSError):
        raw.seek(-1)


def test_an_unknown_whence_is_a_value_error(raw):
    with pytest.raises(ValueError):
        raw.seek(0, 99)


def test_writing_to_a_reader_is_unsupported(raw):
    with pytest.raises(io.UnsupportedOperation):
        raw.write(b"nope")
    with pytest.raises(io.UnsupportedOperation):
        raw.truncate(0)


def test_reading_from_a_writer_is_unsupported(fs):
    handle = File(fs.url.with_path("/data/wo.bin"), fs.config, router=fs._router)
    with XRootDRawIO(handle, "wb") as stream:
        with pytest.raises(io.UnsupportedOperation):
            stream.read(1)


def test_operating_on_a_closed_raw_object_is_a_value_error(raw):
    raw.close()
    with pytest.raises(ValueError, match="closed file"):
        raw.readinto(bytearray(1))


def test_close_flushes_once_and_only_once(fs, server):
    """Regression: ``RawIOBase.close`` flushes, so the handle must outlive it."""
    handle = File(fs.url.with_path("/data/close.bin"), fs.config, router=fs._router)
    stream = XRootDRawIO(handle, "wb")
    stream.write(b"payload")
    stream.close()
    assert stream.closed
    assert not handle.is_open
    assert server.contents("/data/close.bin") == b"payload"
    stream.close()  # idempotent


def test_flush_after_close_is_a_no_op(fs):
    handle = File(fs.url.with_path("/data/flush.bin"), fs.config, router=fs._router)
    stream = XRootDRawIO(handle, "wb")
    stream.close()
    stream.flush()


def test_append_mode_starts_at_the_end(fs, server):
    handle = File(fs.url.with_path("/data/a.root"), fs.config, router=fs._router)
    with XRootDRawIO(handle, "ab") as stream:
        assert stream.tell() == 11
        stream.write(b"!")
    assert server.contents("/data/a.root") == b"hello world!"


def test_append_mode_creates_a_file_that_is_not_there(fs, server):
    """Python's ``"a"`` creates; ``kXR_open_apnd`` on its own does not.

    So the open is retried with ``kXR_new``, which is the only way to get
    both halves of the behaviour out of one protocol that has neither.
    """
    handle = File(fs.url.with_path("/data/fresh.log"), fs.config, router=fs._router)
    with XRootDRawIO(handle, "ab") as stream:
        assert stream.tell() == 0
        stream.write(b"first line\n")
    assert server.contents("/data/fresh.log") == b"first line\n"


def test_append_to_something_that_cannot_be_opened_still_fails(fs):
    """The retry is for a missing file, not a licence to swallow errors."""
    handle = File(fs.url.with_path("/data"), fs.config, router=fs._router)
    with pytest.raises(OSError):
        XRootDRawIO(handle, "ab")


def test_writing_nothing_writes_nothing(fs):
    handle = File(fs.url.with_path("/data/none.bin"), fs.config, router=fs._router)
    with XRootDRawIO(handle, "wb") as stream:
        assert stream.write(b"") == 0


def test_truncate_defaults_to_the_current_position(fs, server):
    handle = File(fs.url.with_path("/data/trunc.bin"), fs.config, router=fs._router)
    with XRootDRawIO(handle, "wb") as stream:
        stream.write(b"abcdef")
        stream.seek(3)
        assert stream.truncate() == 3
    assert server.contents("/data/trunc.bin") == b"abc"


def test_the_underlying_handle_is_reachable_for_vector_reads(raw):
    assert raw.file.readv([(0, 5)]) == [b"hello"]


def test_repr_shows_the_name_and_the_position(raw):
    raw.read(5)
    assert "pos=5" in repr(raw)
    assert "a.root" in repr(raw)


def test_an_already_opened_handle_is_not_reopened(fs, server):
    from xrdclient.proto import constants as c

    handle = File(fs.url.with_path("/data/a.root"), fs.config, router=fs._router)
    handle.open(OpenFlags.READ)
    before = server.seen.count(c.kXR_open)
    with XRootDRawIO(handle, "rb", opened=True) as stream:
        assert stream.read() == b"hello world"
    assert server.seen.count(c.kXR_open) == before


# ---------------------------------------------------------------------------
# open_url
# ---------------------------------------------------------------------------


def test_binary_reading_gives_a_buffered_reader(server, config):
    with open_url(server.url / "data/a.root", "rb", config=config) as fh:
        assert isinstance(fh, io.BufferedReader)
        assert fh.read() == b"hello world"


def test_text_reading_gives_a_text_wrapper_that_iterates_lines(server, config):
    with FakeServer(files={"/lines.txt": b"one\ntwo\n"}) as srv:
        with open_url(srv.url / "lines.txt", "r", config=config) as fh:
            assert isinstance(fh, io.TextIOWrapper)
            assert list(fh) == ["one\n", "two\n"]


def test_binary_writing_gives_a_buffered_writer(server, config):
    with open_url(server.url / "data/out.bin", "wb", config=config) as fh:
        assert isinstance(fh, io.BufferedWriter)
        fh.write(b"data")
    assert server.contents("/data/out.bin") == b"data"


def test_updating_gives_a_random_access_buffer(server, config):
    with open_url(server.url / "data/a.root", "rb+", config=config) as fh:
        assert isinstance(fh, io.BufferedRandom)
        assert fh.read(5) == b"hello"
        fh.seek(0)
        fh.write(b"HELLO")
    assert server.contents("/data/a.root") == b"HELLO world"


def test_zero_buffering_gives_the_raw_object(server, config):
    with open_url(server.url / "data/a.root", "rb", buffering=0, config=config) as fh:
        assert isinstance(fh, XRootDRawIO)


def test_unbuffered_text_is_refused(server, config):
    with pytest.raises(ValueError, match="unbuffered text"):
        open_url(server.url / "data/a.root", "r", buffering=0, config=config)


def test_an_encoding_in_binary_mode_is_refused(server, config):
    with pytest.raises(ValueError, match="binary mode"):
        open_url(server.url / "data/a.root", "rb", encoding="utf-8", config=config)


def test_an_explicit_buffer_size_is_honoured(server, config):
    with open_url(server.url / "data/a.root", "rb", buffering=4, config=config) as fh:
        assert fh.read() == b"hello world"


def test_the_default_buffer_is_a_wide_area_sized_one():
    assert DEFAULT_BUFFER_SIZE == 1 << 20


def test_a_url_string_is_accepted(server, config):
    with open_url(str(server.url / "data/a.root"), "rb", config=config) as fh:
        assert fh.read() == b"hello world"


def test_text_encoding_and_newline_are_passed_through(config):
    with FakeServer(files={"/crlf.txt": "é\r\n".encode("latin-1")}) as srv:
        url = srv.url / "crlf.txt"
        with open_url(url, "r", encoding="latin-1", newline="", config=config) as fh:
            assert fh.read() == "é\r\n"


def test_reading_never_asks_to_create_anything():
    """``makepath`` and ``posc`` are write-side options; ``r`` ignores both."""
    flags = flags_for_mode("rb", makepath=True, posc=True)
    assert flags == OpenFlags.READ


def test_a_mode_that_is_none_of_the_four_opens_nothing_in_particular(monkeypatch):
    """``parse_mode`` only ever yields ``r w x a``; the chain still falls through
    without an access flag rather than guessing one."""
    from xrdclient import flags as flags_module

    monkeypatch.setattr(flags_module, "parse_mode", lambda mode: ("z", False, False))
    flags = flags_module.flags_for_mode("zb", makepath=True, posc=True)
    assert flags == OpenFlags.MAKEPATH | OpenFlags.POSC
    assert not flags & (OpenFlags.READ | OpenFlags.UPDATE)


def test_writing_to_a_closed_raw_file_says_it_is_closed(fs):
    handle = File(fs.url.with_path("/data/closed.bin"), fs.config, router=fs._router)
    raw = XRootDRawIO(handle, "wb")
    raw.close()
    with pytest.raises(ValueError, match="closed file"):
        raw.write(b"too late")
