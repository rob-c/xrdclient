"""End-to-end reads and copies of members in local and remote ZIP archives."""

from __future__ import annotations

import io
import os
import struct
import zipfile
import zlib

import pytest

import xrdclient
from xrdclient.cli import cp
from xrdclient.client import _zip
from xrdclient.client._zip import ZipArchiveError
from xrdclient.testing import FakeServer


def _archive(
    members: dict[str, bytes], *, method: int = zipfile.ZIP_STORED, prefix: bytes = b""
) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", method) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return prefix + stream.getvalue()


def _field(data: bytes, signature: bytes, relative: int, width: int) -> int:
    return int.from_bytes(data[data.index(signature) + relative :][:width], "little")


def _replace_field(data: bytes, signature: bytes, relative: int, width: int, value: int) -> bytes:
    changed = bytearray(data)
    at = changed.index(signature) + relative
    changed[at : at + width] = value.to_bytes(width, "little")
    return bytes(changed)


def _zip64_member(name: bytes, data: bytes) -> bytes:
    """A small member whose central record uses ZIP64 for all three 32-bit values."""
    crc = zlib.crc32(data) & 0xFFFFFFFF
    local = struct.pack(
        "<4s5H3I2H",
        b"PK\x03\x04",
        45,
        0,
        0,
        0,
        0,
        crc,
        len(data),
        len(data),
        len(name),
        0,
    )
    wide = struct.pack("<HHQQQ", 1, 24, len(data), len(data), 0)
    central = (
        struct.pack(
            "<4s6H3I5H2I",
            b"PK\x01\x02",
            45,
            45,
            0,
            0,
            0,
            0,
            crc,
            0xFFFFFFFF,
            0xFFFFFFFF,
            len(name),
            len(wide),
            0,
            0,
            0,
            0,
            0xFFFFFFFF,
        )
        + name
        + wide
    )
    body = local + name + data
    end = struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, 1, 1, len(central), len(body), 0)
    return body + central + end


def _small_zip64_archive(name: bytes, data: bytes) -> bytes:
    """A complete forced-ZIP64 archive smaller than the parser's tail read."""
    ordinary = _zip64_member(name, data)
    end_at = ordinary.rfind(b"PK\x05\x06")
    body_and_directory = ordinary[:end_at]
    central_at = ordinary.index(b"PK\x01\x02")
    central_size = end_at - central_at
    wide_end = struct.pack(
        "<4sQ2H2I4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        1,
        1,
        central_size,
        central_at,
    )
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, end_at, 1)
    narrow_end = struct.pack(
        "<4s4H2IH",
        b"PK\x05\x06",
        0,
        0,
        0xFFFF,
        0xFFFF,
        0xFFFFFFFF,
        0xFFFFFFFF,
        0,
    )
    return body_and_directory + wide_end + locator + narrow_end


def _member(data: bytes, name: str = "bad") -> _zip.ZipMember:
    return _zip.member(lambda offset, length: data[offset : offset + length], len(data), name)


@pytest.fixture
def remote_archive(config):
    payload = ("αβγ one\nsecond line\n" * 4000).encode()
    archive = _archive(
        {"large-padding.bin": os.urandom(80_000), "folder/data.txt": payload},
        method=zipfile.ZIP_DEFLATED,
    )
    with FakeServer(files={"/bundle.zip": archive}) as server:
        yield server, payload, config


def test_query_reads_one_remote_member_and_keeps_other_cgi(remote_archive):
    server, payload, config = remote_archive
    url = server.url.with_path("/bundle.zip").with_query(
        authz="Bearer secret", **{"xrdcl.unzip": "folder/data.txt"}
    )
    with xrdclient.open(url, "rb", buffering=17, config=config) as member:
        assert "secret" not in member.raw.name
        assert member.read(11) == payload[:11]
        assert member.seek(-8, io.SEEK_END) == len(payload) - 8
        assert member.read() == payload[-8:]
        assert member.seek(4) == 4
        assert member.read(7) == payload[4:11]
    assert server.opened == ["/bundle.zip?authz=Bearer%20secret"]
    assert all(length < len(server.contents("/bundle.zip")) for _, _, length in server.reads)


def test_explicit_member_wins_over_the_query(remote_archive):
    server, payload, config = remote_archive
    url = server.url.with_path("/bundle.zip").with_query(**{"xrdcl.unzip": "missing"})
    with xrdclient.open(url, "rb", member="folder/data.txt", config=config) as member:
        assert member.read() == payload


def test_text_mode_decodes_a_deflated_member_and_seeks(remote_archive):
    server, payload, config = remote_archive
    url = server.url.with_path("/bundle.zip")
    with xrdclient.open(
        url, "r", member="folder/data.txt", encoding="utf-8", config=config
    ) as text:
        assert text.readline() == "αβγ one\n"
        text.seek(0)
        assert text.read().encode() == payload


def test_unbuffered_stored_member_has_normal_raw_io_semantics(config):
    archive = _archive({"plain.bin": b"0123456789"})
    with FakeServer(files={"/stored.zip": archive}) as server:
        raw = xrdclient.open(
            server.url.with_path("/stored.zip"),
            "rb",
            buffering=0,
            member="plain.bin",
            config=config,
        )
        assert (raw.readable(), raw.writable(), raw.seekable(), raw.mode) == (
            True,
            False,
            True,
            "rb",
        )
        assert raw.read(3) == b"012"
        assert raw.seek(2, io.SEEK_CUR) == 5
        assert raw.read() == b"56789"
        assert raw.seek(50) == 50 and raw.read() == b""
        with pytest.raises(OSError, match="negative seek"):
            raw.seek(-1)
        with pytest.raises(ValueError, match="whence"):
            raw.seek(0, 99)
        raw.close()
        assert raw.closed
        raw.close()
        with pytest.raises(ValueError, match="closed"):
            raw.read(1)


def test_local_member_and_empty_member(tmp_path):
    path = tmp_path / "local.zip"
    path.write_bytes(_archive({"empty": b"", "value": b"local"}))
    with xrdclient.open(path, "rb", member="empty") as empty:
        assert empty.read() == b""
    with xrdclient.open(path, "rb", member="value") as value:
        assert value.read() == b"local"


def test_zip64_sizes_and_offset_are_used_for_member_reads(config):
    archive = _zip64_member(b"wide.bin", b"zip64 data")
    with FakeServer(files={"/wide.zip": archive}) as server:
        with xrdclient.open(
            server.url.with_path("/wide.zip"), "rb", member="wide.bin", config=config
        ) as member:
            assert member.read() == b"zip64 data"


def test_small_forced_zip64_and_legacy_cp437_names_are_read(config):
    archive = _small_zip64_archive("café.bin".encode("cp437"), b"legacy name")
    with FakeServer(files={"/small-wide.zip": archive}) as server:
        with xrdclient.open(
            server.url.with_path("/small-wide.zip"),
            "rb",
            member="café.bin",
            config=config,
        ) as member:
            assert member.read() == b"legacy name"


def test_missing_member_and_broken_local_header_close_cleanly(config):
    archive = _archive({"one": b"1"})
    broken = archive.replace(b"PK\x03\x04", b"NOPE", 1)
    with FakeServer(files={"/a.zip": archive, "/broken.zip": broken}) as server:
        with pytest.raises(KeyError, match="absent"):
            xrdclient.open(server.url.with_path("/a.zip"), "rb", member="absent", config=config)
        with pytest.raises(ZipArchiveError, match="Central Directory corrupted"):
            xrdclient.open(server.url.with_path("/broken.zip"), "rb", member="one", config=config)


def test_short_and_out_of_range_local_headers_are_rejected(config):
    short = _archive({"one": b""})
    short = _replace_field(short, b"PK\x01\x02", 42, 4, len(short) - 10)
    outside = _archive({"one": b"1"})
    outside = _replace_field(outside, b"PK\x03\x04", 26, 2, 0xFFFF)
    with FakeServer(files={"/short.zip": short, "/outside.zip": outside}) as server:
        for path in ("/short.zip", "/outside.zip"):
            with pytest.raises(ZipArchiveError, match="Central Directory corrupted"):
                xrdclient.open(server.url.with_path(path), "rb", member="one", config=config)


def test_encrypted_and_unsupported_compression_are_rejected(config):
    encrypted = _archive({"secret": b"not really encrypted"})
    for signature, relative in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        flags = _field(encrypted, signature, relative, 2)
        encrypted = _replace_field(encrypted, signature, relative, 2, flags | 1)
    bzip = _archive({"odd": b"compressed"}, method=zipfile.ZIP_BZIP2)
    with FakeServer(files={"/encrypted.zip": encrypted, "/bzip.zip": bzip}) as server:
        with pytest.raises(ZipArchiveError, match="encrypted"):
            xrdclient.open(
                server.url.with_path("/encrypted.zip"), "rb", member="secret", config=config
            )
        with pytest.raises(ZipArchiveError, match="unsupported compression method"):
            xrdclient.open(server.url.with_path("/bzip.zip"), "rb", member="odd", config=config)


@pytest.mark.parametrize("method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_full_reads_verify_the_member_crc(method, config):
    archive = _archive({"bad": b"payload" * 100}, method=method)
    crc = _field(archive, b"PK\x01\x02", 16, 4)
    archive = _replace_field(archive, b"PK\x01\x02", 16, 4, crc ^ 1)
    with FakeServer(files={"/bad.zip": archive}) as server:
        with (
            xrdclient.open(
                server.url.with_path("/bad.zip"), "rb", member="bad", config=config
            ) as member,
            pytest.raises(ZipArchiveError, match="CRC32 mismatch"),
        ):
            member.read()


def test_deflate_metadata_cannot_hide_trailing_or_expanding_data(config):
    archive = _archive({"bad": b"A" * 10_000}, method=zipfile.ZIP_DEFLATED)
    compressed = _field(archive, b"PK\x01\x02", 20, 4)
    trailing = _replace_field(archive, b"PK\x01\x02", 20, 4, compressed + 1)
    plain = _field(archive, b"PK\x01\x02", 24, 4)
    expanding = _replace_field(archive, b"PK\x01\x02", 24, 4, plain - 1)
    with FakeServer(files={"/trailing.zip": trailing, "/expanding.zip": expanding}) as server:
        with (
            xrdclient.open(
                server.url.with_path("/trailing.zip"), "rb", member="bad", config=config
            ) as member,
            pytest.raises(ZipArchiveError, match="invalid compressed size"),
        ):
            member.read()
        with (
            xrdclient.open(
                server.url.with_path("/expanding.zip"), "rb", member="bad", config=config
            ) as member,
            pytest.raises(ZipArchiveError, match="expands beyond"),
        ):
            member.read()


def test_deflate_rejects_truncation_invalid_blocks_and_a_short_plain_size(config):
    archive = _archive({"bad": os.urandom(20_000)}, method=zipfile.ZIP_DEFLATED)
    compressed = _field(archive, b"PK\x01\x02", 20, 4)
    truncated = _replace_field(archive, b"PK\x01\x02", 20, 4, compressed - 1)

    info = _member(archive)
    invalid = bytearray(archive)
    invalid[info.data_offset : info.data_offset + 4] = b"\xff\xff\xff\xff"

    plain = _field(archive, b"PK\x01\x02", 24, 4)
    short_plain = _replace_field(archive, b"PK\x01\x02", 24, 4, plain + 1)
    with FakeServer(
        files={
            "/truncated.zip": truncated,
            "/invalid.zip": bytes(invalid),
            "/short-plain.zip": short_plain,
        }
    ) as server:
        with (
            xrdclient.open(
                server.url.with_path("/truncated.zip"), "rb", member="bad", config=config
            ) as member,
            pytest.raises(ZipArchiveError, match="incomplete deflate stream"),
        ):
            member.read()
        with (
            xrdclient.open(
                server.url.with_path("/invalid.zip"), "rb", member="bad", config=config
            ) as member,
            pytest.raises(ZipArchiveError, match="is corrupt"),
        ):
            member.read()
        with (
            xrdclient.open(
                server.url.with_path("/short-plain.zip"), "rb", member="bad", config=config
            ) as member,
            pytest.raises(ZipArchiveError, match="size mismatch"),
        ):
            member.read()


def test_remote_shrinkage_is_not_mistaken_for_member_eof(config):
    stored = _archive({"bad": b"stored payload"})
    deflated = _archive({"bad": os.urandom(10_000)}, method=zipfile.ZIP_DEFLATED)
    with FakeServer(files={"/stored.zip": stored, "/deflated.zip": deflated}) as server:
        stored_member = xrdclient.open(
            server.url.with_path("/stored.zip"),
            "rb",
            buffering=0,
            member="bad",
            config=config,
        )
        server.files["/stored.zip"].clear()
        with stored_member, pytest.raises(ZipArchiveError, match="ended before its stored size"):
            stored_member.read()

        deflated_member = xrdclient.open(
            server.url.with_path("/deflated.zip"),
            "rb",
            buffering=0,
            member="bad",
            config=config,
        )
        server.files["/deflated.zip"].clear()
        with (
            deflated_member,
            pytest.raises(ZipArchiveError, match="ended before its compressed size"),
        ):
            deflated_member.read()


def test_finishing_an_inflated_member_twice_is_idempotent(config):
    archive = _archive({"bad": b"done"}, method=zipfile.ZIP_DEFLATED)
    with FakeServer(files={"/done.zip": archive}) as server:
        raw = xrdclient.open(
            server.url.with_path("/done.zip"),
            "rb",
            buffering=0,
            member="bad",
            config=config,
        )
        assert raw.read() == b"done"
        raw._finish_inflate()
        raw.close()


def test_stored_member_sizes_must_match(config):
    archive = _archive({"bad": b"payload"})
    compressed = _field(archive, b"PK\x01\x02", 20, 4)
    archive = _replace_field(archive, b"PK\x01\x02", 20, 4, compressed + 1)
    with (
        FakeServer(files={"/bad.zip": archive}) as server,
        pytest.raises(ZipArchiveError, match="different compressed and plain sizes"),
    ):
        xrdclient.open(server.url.with_path("/bad.zip"), "rb", member="bad", config=config)


@pytest.mark.parametrize("mode", ["wb", "ab", "rb+"])
def test_members_are_read_only(mode, tmp_path):
    path = tmp_path / "archive.zip"
    path.write_bytes(_archive({"a": b"a"}))
    with pytest.raises(ValueError, match="only be opened for reading"):
        xrdclient.open(path, mode, member="a")


def test_copy_and_resume_use_the_member_not_the_archive(remote_archive, tmp_path):
    server, payload, config = remote_archive
    source = server.url.with_path("/bundle.zip").with_query(**{"xrdcl.unzip": "folder/data.txt"})
    whole = tmp_path / "whole.txt"
    result = xrdclient.copy(source, whole, config=config)
    assert (whole.read_bytes(), result.size, result.source) == (payload, len(payload), str(source))

    resumed = tmp_path / "resumed.txt"
    resumed.write_bytes(payload[:123])
    result = xrdclient.copy(source, resumed, resume=True, verify=True, config=config)
    assert resumed.read_bytes() == payload
    assert (result.resumed_at, result.size, result.verified) == (123, len(payload) - 123, True)


def test_cli_zip_uses_the_member_name_for_a_directory_target(tmp_path, capsys):
    archive = tmp_path / "source.zip"
    archive.write_bytes(_archive({"dir/result.txt": b"from zip"}, method=zipfile.ZIP_DEFLATED))
    target = tmp_path / "out"
    target.mkdir()
    assert cp.main(["-z", "dir/result.txt", os.fspath(archive), os.fspath(target)]) == 0
    assert (target / "result.txt").read_bytes() == b"from zip"
    assert "result.txt" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [
        ["--zip", "a", "-r", "src", "dst"],
        ["--zip", "a", "--tpc", "src", "dst"],
        ["--zip", "a", "--sources", "2", "src", "dst"],
    ],
)
def test_cli_rejects_zip_strategies_that_cannot_select_a_member(arguments, capsys):
    assert cp.main(arguments) == 2
    assert "--zip" in capsys.readouterr().err
