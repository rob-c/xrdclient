"""Integration coverage for pure-Python local and remote ZIP appends."""

from __future__ import annotations

import io
import os
import zipfile
from datetime import datetime

import pytest

import xrdclient
from xrdclient.cli import cp
from xrdclient.client import _zip
from xrdclient.io import zip as zipio
from xrdclient.proto import constants as c
from xrdclient.testing import FakeServer


def _archive(members: dict[str, bytes], *, comment: bytes = b"", prefix: bytes = b"") -> bytes:
    stream = io.BytesIO()
    stream.write(prefix)
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.comment = comment
        for name, data in members.items():
            archive.writestr(name, data)
    return stream.getvalue()


def _directory(data: bytes) -> _zip.ZipDirectory:
    return _zip.directory(lambda offset, length: data[offset : offset + length], len(data))


def _assert_appended_archive(updated: bytes, original: bytes, source: bytes) -> None:
    before = _directory(original)
    after = _directory(updated)
    assert updated.startswith(b"self-extracting-prefix")
    assert after.records.startswith(before.records)
    assert after.comment == b"keep this"
    with zipfile.ZipFile(io.BytesIO(updated)) as archive:
        assert archive.namelist() == ["old.txt", "nested/new.bin"]
        assert archive.read("old.txt") == b"old payload"
        assert archive.read("nested/new.bin") == source
        assert archive.getinfo("nested/new.bin").compress_type == zipfile.ZIP_STORED
        assert archive.testzip() is None


def test_remote_append_preserves_existing_records_comment_and_prefix(config, tmp_path):
    original = _archive(
        {"old.txt": b"old payload"}, comment=b"keep this", prefix=b"self-extracting-prefix"
    )
    source = tmp_path / "new.bin"
    source.write_bytes(os.urandom(70_000))
    progress = []
    with FakeServer(files={"/archive.zip": original}) as server:
        result = xrdclient.append_zip(
            source,
            server.url.with_path("/archive.zip"),
            member="nested/new.bin",
            config=config,
            chunk_size=4096,
            progress=lambda done, total: progress.append((done, total)),
        )
        updated = server.contents("/archive.zip")
        assert server.seen.count(c.kXR_chkpoint) >= 2  # checkpoint begin and commit

    _assert_appended_archive(updated, original, source.read_bytes())
    assert progress[-1] == (source.stat().st_size, source.stat().st_size)
    assert (result.size, result.verified, result.checksum.algorithm) == (70_000, True, "zcrc32")


def test_remote_append_creates_a_new_archive_readable_through_member_api(config, tmp_path):
    source = tmp_path / "first.txt"
    source.write_text("first member")
    with FakeServer() as server:
        result = xrdclient.append_zip(source, server.url.with_path("/new.zip"), config=config)
        with xrdclient.open(
            server.url.with_path("/new.zip"), "r", member="first.txt", config=config
        ) as member:
            assert member.read() == "first member"
        with zipfile.ZipFile(io.BytesIO(server.contents("/new.zip"))) as archive:
            assert archive.testzip() is None
    assert result.target.endswith("//new.zip")


def test_local_append_duplicate_and_short_source_are_transactional(tmp_path):
    target = tmp_path / "archive.zip"
    target.write_bytes(_archive({"kept": b"untouched"}, comment=b"c"))
    original = target.read_bytes()
    with pytest.raises(FileExistsError, match="kept"):
        xrdclient.append_zip(io.BytesIO(b"replacement"), target, member="kept")
    assert target.read_bytes() == original

    with pytest.raises(_zip.ZipArchiveError, match="still expected"):
        zipio.append_member(xrdclient.parse(target), "short", io.BytesIO(b"x"), 10, chunk_size=2)
    assert target.read_bytes() == original


def test_remote_failed_appends_roll_back_existing_and_new_archives(config):
    original = _archive({"kept": b"safe"})
    with FakeServer(files={"/existing.zip": original, "/empty.zip": b""}) as server:
        with pytest.raises(_zip.ZipArchiveError, match="still expected"):
            zipio.append_member(
                server.url.with_path("/existing.zip"),
                "short",
                io.BytesIO(b"x"),
                10,
                config=config,
            )
        assert server.contents("/existing.zip") == original

        with pytest.raises(_zip.ZipArchiveError, match="still expected"):
            zipio.append_member(
                server.url.with_path("/empty.zip"),
                "short",
                io.BytesIO(b"x"),
                10,
                config=config,
            )
        assert server.contents("/empty.zip") == b""

        with pytest.raises(_zip.ZipArchiveError, match="still expected"):
            zipio.append_member(
                server.url.with_path("/new.zip"),
                "short",
                io.BytesIO(b"x"),
                10,
                config=config,
            )
        assert server.contents("/new.zip") == b""


def test_append_from_current_stream_position_and_remove_source(tmp_path):
    stream = io.BytesIO(b"skip-member-data")
    stream.name = "stream.bin"  # type: ignore[attr-defined]
    stream.seek(5)
    target = tmp_path / "stream.zip"
    result = xrdclient.append_zip(stream, target)
    with zipfile.ZipFile(target) as archive:
        assert archive.read("stream.bin") == b"member-data"
    assert result.size == len(b"member-data")

    source = tmp_path / "move.txt"
    source.write_bytes(b"move me")
    xrdclient.append_zip(source, target, remove_source=True)
    assert not source.exists()
    with zipfile.ZipFile(target) as archive:
        assert archive.read("move.txt") == b"move me"


def test_dry_run_reports_without_creating_an_archive(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"nothing moves")
    target = tmp_path / "absent.zip"
    result = xrdclient.append_zip(source, target, dry_run=True)
    assert (result.size, result.seconds, result.checksum) == (13, 0.0, None)
    assert not target.exists()


def test_cli_appends_multiple_sources_to_one_archive(tmp_path, capsys):
    first, second = tmp_path / "one", tmp_path / "two"
    first.write_bytes(b"1")
    second.write_bytes(b"22")
    target = tmp_path / "many.zip"
    assert cp.main(["--zip-append", os.fspath(first), os.fspath(second), os.fspath(target)]) == 0
    with zipfile.ZipFile(target) as archive:
        assert archive.namelist() == ["one", "two"]
        assert (archive.read("one"), archive.read("two")) == (b"1", b"22")
    output = capsys.readouterr().out
    assert "one" in output and "two" in output


@pytest.mark.parametrize(
    "arguments",
    [
        ["--zip-append", "-r", "src", "dst"],
        ["--zip-append", "--zip", "a", "src", "dst"],
        ["--zip-append", "--tpc", "src", "dst"],
        ["--zip-append", "--continue", "src", "dst"],
        ["--zip-append", "-f", "src", "dst"],
        ["--zip-append", "-n", "src", "dst"],
        ["--zip-append", "--sources", "2", "src", "dst"],
    ],
)
def test_cli_rejects_incompatible_append_strategies(arguments, capsys):
    assert cp.main(arguments) == 2
    assert "--zip-append" in capsys.readouterr().err


def test_append_validates_target_name_size_and_chunk(tmp_path):
    source = io.BytesIO(b"x")
    with pytest.raises(ValueError, match="name"):
        zipio.append_member(xrdclient.parse(tmp_path / "a.zip"), "", source, 1)
    with pytest.raises(ValueError, match="NUL"):
        zipio.append_member(xrdclient.parse(tmp_path / "a.zip"), "bad\x00name", source, 1)
    with pytest.raises(ValueError, match="negative"):
        zipio.append_member(xrdclient.parse(tmp_path / "a.zip"), "a", source, -1)
    with pytest.raises(ValueError, match="positive"):
        zipio.append_member(xrdclient.parse(tmp_path / "a.zip"), "a", source, 1, chunk_size=0)
    with pytest.raises(ValueError, match="local paths or root"):
        zipio.append_member(xrdclient.parse("https://example.test/a.zip"), "a", source, 1)


def test_non_seekable_unnamed_stream_requires_size_information(tmp_path):
    class Stream:
        def read(self, size=-1):  # type: ignore[no-untyped-def]
            return b"x"

    with pytest.raises(ValueError, match="known size or be seekable"):
        xrdclient.append_zip(Stream(), tmp_path / "a.zip", member="a")
    with pytest.raises(ValueError, match="member="):
        xrdclient.append_zip(io.BytesIO(b"x"), tmp_path / "a.zip")
    numbered = io.BytesIO(b"x")
    numbered.name = 3  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="member="):
        xrdclient.append_zip(numbered, tmp_path / "a.zip")
    with pytest.raises(ValueError, match="path or URL"):
        xrdclient.append_zip(io.BytesIO(b"x"), io.BytesIO(), member="a")


def test_a_source_that_overreturns_is_limited_to_the_declared_size(tmp_path):
    class Overread(io.BytesIO):
        def read(self, size=-1):  # type: ignore[no-untyped-def]
            return super().read()  # deliberately violates BinaryIO.read(size)

    target = tmp_path / "limited.zip"
    zipio.append_member(xrdclient.parse(target), "three", Overread(b"abcdef"), 3)
    with zipfile.ZipFile(target) as archive:
        assert archive.read("three") == b"abc"


def test_zip64_header_builders_cover_large_member_offset_and_directory():
    stamp = zipio._dos_time(datetime(2200, 12, 31, 23, 59, 59))
    assert stamp[1] >> 9 == 127
    local = zipio._local_header(b"large", zipio._U32, 1, stamp)
    central = zipio._central_header(b"large", zipio._U32, 1, zipio._U32, stamp)
    ending = zipio._end_records(zipio._U16, zipio._U32, zipio._U32, b"comment")
    assert b"PK\x03\x04" == local[:4] and b"PK\x01\x02" == central[:4]
    assert b"PK\x06\x06" in ending and ending.endswith(b"comment")
    assert zipio._zip64_extra([(False, 1, 8)]) == b""


def test_write_all_handles_partial_and_stalled_writers():
    class Partial(io.BytesIO):
        def write(self, data):  # type: ignore[no-untyped-def]
            return super().write(data[:2])

    partial = Partial()
    zipio._write_all(partial, b"abcdef")
    assert partial.getvalue() == b"abcdef"

    class Stalled(io.BytesIO):
        def write(self, data):  # type: ignore[no-untyped-def]
            return 0

    with pytest.raises(OSError, match="no progress"):
        zipio._write_all(Stalled(), b"x")

    class Backwards(io.BytesIO):
        def write(self, data):  # type: ignore[no-untyped-def]
            return -1

    with pytest.raises(OSError, match="no progress"):
        zipio._write_all(Backwards(), b"x")

    class Broken(io.BytesIO):
        def seek(self, *args):  # type: ignore[no-untyped-def]
            raise OSError("gone")

    zipio._restore(Broken(), 0, b"tail", 4)  # rollback is deliberately best-effort
