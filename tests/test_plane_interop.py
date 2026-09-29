"""The bulk data plane against a real ``xrootd``.

The fake server is written from the same reading of the protocol as the
client, so the framing that matters most - a write sent as a header and then
its payload, several writes and vector reads in flight on one connection, a
readahead that runs past the end of the file - is checked here against the
daemon itself.
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

import xrdclient
from conftest import _REAL_CONFIG
from xrdclient.client.file import File
from xrdclient.flags import OpenFlags

pytestmark = pytest.mark.interop

#: Several windows of small chunks, every byte telling where it came from.
PAYLOAD = os.urandom(3 << 20) + bytes(range(256)) * 17

SMALL = replace(_REAL_CONFIG, chunk_size=256 << 10, bulk_chunk=256 << 10, bulk_depth=3)


def _url(real_server, path: str) -> str:
    return f"{real_server.url.rstrip('/')}//{path.lstrip('/')}"


@pytest.fixture
def written(real_server, sandbox):
    """``sandbox/p.bin`` holding :data:`PAYLOAD`, written through the plane."""
    url = _url(real_server, f"{sandbox}/p.bin")
    handle = File(xrdclient.parse(url), SMALL)
    handle.open(OpenFlags.NEW | OpenFlags.UPDATE)
    with handle:
        assert handle.write(PAYLOAD, 0) == len(PAYLOAD)
    return url


def test_a_pipelined_write_reads_back_byte_exact(written):
    with File(xrdclient.parse(written), SMALL) as handle:
        assert handle.size == len(PAYLOAD)
        assert handle.read() == PAYLOAD


def test_a_buffered_writer_and_reader_agree(real_server, sandbox):
    url = _url(real_server, f"{sandbox}/s.bin")
    with xrdclient.open(url, "wb", config=SMALL) as fh:
        view = memoryview(PAYLOAD)
        for at in range(0, len(PAYLOAD), 100_000):
            fh.write(view[at : at + 100_000])
    with xrdclient.open(url, "rb", config=SMALL) as fh:
        assert fh.read() == PAYLOAD


def test_sequential_small_reads_run_ahead_to_the_end(written):
    with File(xrdclient.parse(written), SMALL) as handle:
        pieces = [handle.read(40_000, at) for at in range(0, len(PAYLOAD) + 80_000, 40_000)]
    assert b"".join(pieces) == PAYLOAD
    assert pieces[-1] == b""


def test_a_vector_read_of_many_batches_matches_the_file(written):
    ranges = [(at, 64) for at in range(0, len(PAYLOAD) - 64, len(PAYLOAD) // 2500)]
    with File(xrdclient.parse(written), SMALL) as handle:
        got = handle.readv(ranges)
    assert got == [PAYLOAD[at : at + n] for at, n in ranges]
