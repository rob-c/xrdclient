"""Real XRootD transfers through the external BRIX network fault injector."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest

import xrdclient
from _brix import BrixProxy
from conftest import _REAL_CONFIG

pytestmark = pytest.mark.interop


def _url(port: int, path: Path) -> str:
    return f"root://127.0.0.1:{port}//{str(path).lstrip('/')}"


def test_repeated_brix_resets_downshift_only_recovery_reads(
    real_server: Any, sandbox: str, tmp_path: Path
) -> None:
    payload = os.urandom(8 << 20)
    source = Path(sandbox) / "brix-lossy.bin"
    source.write_bytes(payload)
    target = tmp_path / "download.bin"
    config = _REAL_CONFIG.evolve(
        data_streams=0,
        bulk_workers=2,
        bulk_chunk=4 << 20,
        bulk_recovery_chunk=64 << 10,
        bulk_recovery=30,
        retry_backoff=0.01,
    )

    with BrixProxy(real_server.port, seed=17) as proxy:
        proxy.command("chunk 4096 down")
        proxy.command("lossy 1 down")
        result = xrdclient.copy(
            _url(proxy.listen_port, source), target, config=config, verify=False
        )
        status = proxy.command("status")

    assert result.size == len(payload)
    assert target.read_bytes() == payload
    severed = re.search(r"severs=(\d+)", status)
    assert severed is not None and int(severed.group(1)) > 0


def test_upload_resumes_after_one_shot_brix_truncation(
    real_server: Any, sandbox: str, tmp_path: Path
) -> None:
    payload = os.urandom(2 << 20)
    source = tmp_path / "upload.bin"
    source.write_bytes(payload)
    target = Path(sandbox) / "brix-upload.bin"
    config = _REAL_CONFIG.evolve(
        data_streams=0,
        connect_retries=6,
        retry_backoff=0.01,
    )

    with BrixProxy(real_server.port, seed=23) as proxy:
        proxy.command("one-shot")
        proxy.command("truncate-at 131072 up")
        result = xrdclient.copy(
            source,
            _url(proxy.listen_port, target),
            config=config,
            chunk_size=256 << 10,
            verify=False,
        )
        status = proxy.command("status")

    assert result.size == len(payload)
    assert target.read_bytes() == payload
    severed = re.search(r"severs=(\d+)", status)
    assert severed is not None and int(severed.group(1)) > 0


def test_download_outlasts_repeated_truncation_until_the_link_heals(
    real_server: Any, sandbox: str, tmp_path: Path
) -> None:
    payload = os.urandom(4 << 20)
    source = Path(sandbox) / "brix-flapping.bin"
    source.write_bytes(payload)
    target = tmp_path / "flapping-download.bin"
    config = _REAL_CONFIG.evolve(
        data_streams=0,
        bulk_workers=1,
        bulk_chunk=1 << 20,
        bulk_recovery_chunk=16 << 10,
        bulk_recovery=5,
        connect_retries=50,
        retry_backoff=0.01,
    )

    with BrixProxy(real_server.port, seed=31) as proxy:
        proxy.command("chunk 4096 down")
        proxy.command("truncate-at 32768 down")
        proxy.command("heal-after 300")
        result = xrdclient.copy(
            _url(proxy.listen_port, source), target, config=config, verify=False
        )
        status = proxy.command("status")

    assert result.size == len(payload)
    assert target.read_bytes() == payload
    severed = re.search(r"severs=(\d+)", status)
    assert severed is not None and int(severed.group(1)) >= 2


def test_wire_corruption_is_never_returned_as_a_verified_file(
    real_server: Any, sandbox: str, tmp_path: Path
) -> None:
    payload = b"A" * (2 << 20)
    source = Path(sandbox) / "brix-corruption.bin"
    source.write_bytes(payload)
    target = tmp_path / "corrupt-download.bin"
    config = _REAL_CONFIG.evolve(data_streams=0, bulk_workers=1)

    with BrixProxy(real_server.port, seed=37) as proxy:
        proxy.command("replace str:AAAAAAAA str:BAAAAAAA down")
        with pytest.raises(xrdclient.ChecksumMismatchError):
            xrdclient.copy(_url(proxy.listen_port, source), target, config=config, verify=True)

    assert target.exists()
    assert target.read_bytes() != payload


def test_copy_survives_tiny_segments_jitter_and_a_silent_firewall_reap(
    real_server: Any, sandbox: str, tmp_path: Path
) -> None:
    payload = os.urandom(2 << 20)
    source = Path(sandbox) / "brix-drunk-admin.bin"
    source.write_bytes(payload)
    target = tmp_path / "drunk-admin-download.bin"
    config = _REAL_CONFIG.evolve(
        data_streams=0,
        bulk_workers=1,
        bulk_recovery=5,
        request_timeout=0.2,
        stall_deadline=5,
        connect_retries=20,
        retry_backoff=0.01,
    )

    with BrixProxy(real_server.port, seed=53) as proxy:
        proxy.command("mss 128")
        proxy.command("chunk 97 both")
        proxy.command("jitter 2 both")
        proxy.command("one-shot")
        proxy.command("random-hangup 20 40 100")
        result = xrdclient.copy(
            _url(proxy.listen_port, source), target, config=config, verify=False
        )
        status = proxy.command("status")

    assert result.size == len(payload)
    assert target.read_bytes() == payload
    hung = re.search(r"random_hangups=(\d+)", status)
    assert hung is not None and int(hung.group(1)) > 0
