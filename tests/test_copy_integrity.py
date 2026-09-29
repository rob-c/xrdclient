"""A copy that did not move the whole file must never look like one that did.

These are the ways a transfer can end early or unchecked without an error of
its own: a server whose reads stop short of the size it reported, a verify
request with nothing to verify against, and a sync that finds a file changed
but was told not to replace it.
"""

from __future__ import annotations

import io
import struct
from collections.abc import Iterator

import pytest

import xrdclient
from xrdclient.errors import XRootDError
from xrdclient.proto import constants as c
from xrdclient.testing import FakeServer
from xrdclient.testing.server import _HANDLERS

PAYLOAD = bytes(range(256)) * 40  # 10 240 bytes
CUT = 4096  # where the misbehaving server's reads run dry


def _cut_short(opcode: int):  # type: ignore[no-untyped-def]
    """A read handler that treats the file as ending at :data:`CUT`.

    The stat still reports the whole length, which is what a server losing
    its backing store part way through a file looks like from here.
    """

    def handler(conn, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:  # type: ignore[no-untyped-def]
        offset, length = struct.unpack(">qi", params[4:16])
        clamped = max(0, min(length, CUT - offset))
        params = params[:4] + struct.pack(">qi", offset, clamped) + params[16:]
        yield from _HANDLERS[opcode](conn, sid, params, body)

    return handler


@pytest.fixture
def short():
    """A server holding :data:`PAYLOAD` whose reads end at :data:`CUT`."""
    with FakeServer(files={"/f.bin": PAYLOAD}) as srv:
        for opcode in (c.kXR_read, c.kXR_pgread):
            srv.handlers[opcode] = _cut_short(opcode)
        yield srv


@pytest.fixture
def pump_only():
    """No bulk plane, no spans: the single-stream pump moves everything."""
    return xrdclient.Config(bulk=False, parallel_chunks=1)


# ---------------------------------------------------------------------------
# Short transfers
# ---------------------------------------------------------------------------


def test_a_pumped_download_that_stops_short_is_an_error(short, tmp_path, pump_only):
    with pytest.raises(XRootDError, match=f"transfer ended with {CUT} of {len(PAYLOAD)} bytes"):
        xrdclient.copy(short.url / "f.bin", tmp_path / "f.bin", config=pump_only, verify=False)


def test_a_short_download_into_a_stream_names_the_stream(short, pump_only):
    sink = io.BytesIO()
    with pytest.raises(XRootDError, match=r"BytesIO.*incomplete"):
        xrdclient.copy(short.url / "f.bin", sink, config=pump_only, verify=False)
    assert sink.getvalue() == PAYLOAD[:CUT]


def test_a_short_pumped_move_keeps_the_source(short, pump_only):
    """The data-loss case: a truncated copy followed by deleting the original."""
    with FakeServer() as dst:
        with pytest.raises(XRootDError, match="incomplete"):
            xrdclient.copy(
                short.url / "f.bin",
                dst.url / "f.bin",
                config=pump_only,
                verify=False,
                remove_source=True,
            )
    assert short.contents("/f.bin") == PAYLOAD


def test_a_parallel_transfer_that_stops_short_is_an_error(short, tmp_path):
    cfg = xrdclient.Config(bulk=False)
    with pytest.raises(XRootDError, match=f"transfer ended with {CUT} of {len(PAYLOAD)} bytes"):
        xrdclient.copy(
            short.url / "f.bin",
            tmp_path / "f.bin",
            config=cfg,
            chunk_size=1024,
            verify=False,
            remove_source=True,
        )
    assert short.contents("/f.bin") == PAYLOAD


def test_a_resumed_transfer_must_reach_the_full_length(short, tmp_path, pump_only):
    """The tail is measured against the whole file, not against the tail."""
    target = tmp_path / "f.bin"
    target.write_bytes(PAYLOAD[:1024])
    with pytest.raises(XRootDError, match=f"transfer ended with {CUT} of {len(PAYLOAD)} bytes"):
        xrdclient.copy(short.url / "f.bin", target, config=pump_only, resume=True, verify=False)


def test_a_complete_resume_still_passes(server, tmp_path, pump_only):
    with FakeServer(files={"/f.bin": PAYLOAD}) as srv:
        target = tmp_path / "f.bin"
        target.write_bytes(PAYLOAD[:1024])
        result = xrdclient.copy(srv.url / "f.bin", target, config=pump_only, resume=True)
    assert target.read_bytes() == PAYLOAD
    assert result.resumed_at + result.size == len(PAYLOAD)


def test_a_source_of_unknown_length_cannot_be_short(server):
    """A stream says nothing about its size, so whatever arrives is the file."""
    result = xrdclient.copy(io.BytesIO(b"abc"), server.url / "s.bin")
    assert result.size == 3


# ---------------------------------------------------------------------------
# verify=True with nothing to verify against
# ---------------------------------------------------------------------------


def test_a_strict_local_copy_compares_the_two_files(tmp_path):
    source = tmp_path / "a.bin"
    source.write_bytes(PAYLOAD)
    result = xrdclient.copy(source, tmp_path / "b.bin", verify=True, algorithm="adler32")
    assert result.verified
    assert result.checksum.value == xrdclient.crypto.checksum_bytes("adler32", PAYLOAD)


def test_a_strict_copy_between_streams_and_local_files_is_refused(tmp_path):
    """Nobody can be asked for a checksum, so verify=True cannot be honoured."""
    target = tmp_path / "never.bin"
    with pytest.raises(ValueError, match="nothing to verify"):
        xrdclient.copy(io.BytesIO(PAYLOAD), target, verify=True)
    assert not target.exists()  # refused before a byte moved


def test_an_unverifiable_copy_by_default_still_degrades_quietly(tmp_path):
    result = xrdclient.copy(io.BytesIO(PAYLOAD), tmp_path / "ok.bin")
    assert result.checksum is None


# ---------------------------------------------------------------------------
# sync replaces what it finds out of date
# ---------------------------------------------------------------------------


def test_sync_replaces_a_changed_file_even_without_overwrite(tmp_path):
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    (source / "a.bin").write_bytes(b"first")
    xrdclient.copy_tree(source, target, sync="size", overwrite=False)
    (source / "a.bin").write_bytes(b"second version")
    results = xrdclient.copy_tree(source, target, sync="size", overwrite=False)
    assert (target / "a.bin").read_bytes() == b"second version"
    assert len(results) == 1


def test_without_sync_overwrite_false_still_refuses(tmp_path):
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    target.mkdir()
    (source / "a.bin").write_bytes(b"new")
    (target / "a.bin").write_bytes(b"old")
    with pytest.raises(FileExistsError):
        xrdclient.copy_tree(source, target, overwrite=False)
