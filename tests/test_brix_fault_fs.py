"""Local-copy behavior through the external BRIX FUSE fault filesystem."""

from __future__ import annotations

import errno
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import xrdclient
from _brix_fs import BrixFaultFS

pytestmark = pytest.mark.interop


@pytest.fixture
def fault_fs(tmp_path: Path) -> Iterator[BrixFaultFS]:
    with BrixFaultFS(tmp_path) as filesystem:
        yield filesystem


def _config() -> xrdclient.Config:
    return xrdclient.Config(bulk=False, in_flight=1, parallel_chunks=1)


def test_local_copy_survives_short_fuse_reads_and_writes(fault_fs: BrixFaultFS) -> None:
    payload = os.urandom(2 << 20)
    (fault_fs.backing / "source.bin").write_bytes(payload)
    fault_fs.configure(
        {
            "trace": True,
            "rules": [
                {"path": "/source.bin", "op": "read", "action": "short", "bytes": 4096},
                {"path": "/target.bin", "op": "write", "action": "short", "bytes": 4096},
            ],
        }
    )

    result = xrdclient.copy(
        fault_fs.mount / "source.bin",
        fault_fs.mount / "target.bin",
        config=_config(),
        chunk_size=64 << 10,
        verify=True,
    )

    assert result.size == len(payload)
    assert (fault_fs.backing / "target.bin").read_bytes() == payload
    status = fault_fs.command("status")
    assert status["faults"] > 0


def test_local_copy_preserves_enospc_after_partial_progress(fault_fs: BrixFaultFS) -> None:
    payload = os.urandom(1 << 20)
    source = fault_fs.backing.parent / "source.bin"
    source.write_bytes(payload)
    fault_fs.configure(
        {
            "trace": True,
            "rules": [
                {
                    "path": "/target.bin",
                    "op": "write",
                    "action": "error",
                    "errno": "ENOSPC",
                    "after_bytes": 64 << 10,
                }
            ],
        }
    )

    started = time.monotonic()
    with pytest.raises(OSError) as caught:
        xrdclient.copy(
            source,
            fault_fs.mount / "target.bin",
            config=_config(),
            chunk_size=64 << 10,
            verify=False,
        )

    assert caught.value.errno == errno.ENOSPC
    assert time.monotonic() - started < 5
    assert 0 < (fault_fs.backing / "target.bin").stat().st_size < len(payload)


def test_local_copy_rejects_zero_progress_from_fuse(fault_fs: BrixFaultFS) -> None:
    source = fault_fs.backing.parent / "source.bin"
    source.write_bytes(b"x" * (128 << 10))
    fault_fs.configure(
        {"rules": [{"path": "/target.bin", "op": "write", "action": "short", "bytes": 0}]}
    )

    with pytest.raises(OSError) as caught:
        xrdclient.copy(
            source,
            fault_fs.mount / "target.bin",
            config=_config(),
            chunk_size=64 << 10,
            verify=False,
        )

    assert caught.value.errno == errno.EIO


def test_success_means_local_bytes_reached_stable_storage(fault_fs: BrixFaultFS) -> None:
    payload = os.urandom(1 << 20)
    source = fault_fs.backing.parent / "source.bin"
    source.write_bytes(payload)
    fault_fs.configure({"writeback": True, "fsync": "commit", "flush_on_close": False})

    result = xrdclient.copy(
        source,
        fault_fs.mount / "target.bin",
        config=_config(),
        chunk_size=64 << 10,
        verify=False,
    )

    assert result.size == len(payload)
    assert (fault_fs.backing / "target.bin").read_bytes() == payload


def test_an_fsync_ack_without_publication_is_not_success(fault_fs: BrixFaultFS) -> None:
    payload = os.urandom(256 << 10)
    source = fault_fs.backing.parent / "source.bin"
    source.write_bytes(payload)
    fault_fs.configure({"writeback": True, "fsync": "ack", "flush_on_close": False})

    with pytest.raises(OSError) as caught:
        xrdclient.copy(
            source,
            fault_fs.mount / "target.bin",
            config=_config(),
            chunk_size=64 << 10,
            verify=False,
        )

    assert caught.value.errno == errno.EIO
    assert not (fault_fs.backing / "target.bin").exists()


def test_bursty_stale_source_handles_are_reopened_at_the_same_offset(
    fault_fs: BrixFaultFS,
) -> None:
    payload = os.urandom(512 << 10)
    (fault_fs.backing / "source.bin").write_bytes(payload)
    target = fault_fs.backing.parent / "target.bin"
    fault_fs.configure(
        {
            "trace": True,
            "rules": [
                {
                    "path": "/source.bin",
                    "op": "read",
                    "action": "error",
                    "errno": "ESTALE",
                    "every": 4,
                    "burst": 2,
                }
            ],
        }
    )

    result = xrdclient.copy(
        fault_fs.mount / "source.bin",
        target,
        config=_config().evolve(connect_retries=3, retry_backoff=0),
        chunk_size=64 << 10,
        verify=False,
    )

    assert result.size == len(payload)
    assert target.read_bytes() == payload
    assert fault_fs.command("status")["faults"] >= 4


def test_a_burst_of_stale_read_only_opens_is_retried(fault_fs: BrixFaultFS) -> None:
    payload = os.urandom(256 << 10)
    (fault_fs.backing / "source.bin").write_bytes(payload)
    target = fault_fs.backing.parent / "target.bin"
    fault_fs.configure(
        {
            "rules": [
                {
                    "path": "/source.bin",
                    "op": "open",
                    "action": "error",
                    "errno": "ESTALE",
                    "count": 2,
                }
            ]
        }
    )

    result = xrdclient.copy(
        fault_fs.mount / "source.bin",
        target,
        config=_config().evolve(connect_retries=3, retry_backoff=0),
        verify=False,
    )

    assert result.size == len(payload)
    assert target.read_bytes() == payload


def test_stale_handle_recovery_refuses_to_splice_a_replacement_file(
    fault_fs: BrixFaultFS,
) -> None:
    original = b"A" * (512 << 10)
    replacement = b"B" * len(original)
    (fault_fs.backing / "source.bin").write_bytes(original)
    (fault_fs.backing / "replacement.bin").write_bytes(replacement)
    target = fault_fs.backing.parent / "target.bin"
    fault_fs.configure(
        {
            "rules": [
                {
                    "path": "/source.bin",
                    "op": "read",
                    "action": "error",
                    "errno": "ESTALE",
                    "count": 1,
                }
            ]
        }
    )
    replace = threading.Timer(
        0.2,
        os.replace,
        args=(fault_fs.mount / "replacement.bin", fault_fs.mount / "source.bin"),
    )
    replace.start()
    try:
        with pytest.raises(OSError) as caught:
            xrdclient.copy(
                fault_fs.mount / "source.bin",
                target,
                config=_config().evolve(connect_retries=3, retry_backoff=1),
                chunk_size=64 << 10,
                verify=False,
            )
    finally:
        replace.join(timeout=5)

    assert caught.value.errno == errno.ESTALE
    assert "changed while recovering" in str(caught.value)
    assert not target.exists()


def test_a_silently_dropped_middle_write_is_caught_by_integrity_checking(
    fault_fs: BrixFaultFS,
) -> None:
    payload = os.urandom(512 << 10)
    source = fault_fs.backing.parent / "source.bin"
    source.write_bytes(payload)
    fault_fs.configure(
        {
            "trace": True,
            "rules": [
                {
                    "path": "/target.bin",
                    "op": "write",
                    "action": "drop",
                    "offset_min": 64 << 10,
                    "offset_max": (128 << 10) - 1,
                }
            ],
        }
    )

    with pytest.raises(xrdclient.ChecksumMismatchError):
        xrdclient.copy(
            source,
            fault_fs.mount / "target.bin",
            config=_config(),
            chunk_size=64 << 10,
            verify=True,
        )

    assert (fault_fs.backing / "target.bin").stat().st_size == len(payload)
    assert fault_fs.command("status")["faults"] > 0


def test_a_source_that_lies_about_its_size_is_not_a_success(fault_fs: BrixFaultFS) -> None:
    payload = os.urandom(256 << 10)
    (fault_fs.backing / "source.bin").write_bytes(payload)
    target = fault_fs.backing.parent / "target.bin"
    fault_fs.configure(
        {
            "rules": [
                {
                    "path": "/source.bin",
                    "op": "getattr",
                    "action": "metadata",
                    "size_delta": 64 << 10,
                }
            ]
        }
    )

    with pytest.raises(xrdclient.XRootDError, match="file is incomplete"):
        xrdclient.copy(
            fault_fs.mount / "source.bin",
            target,
            config=_config(),
            chunk_size=64 << 10,
            verify=False,
        )


def test_a_torn_write_fails_without_spinning_or_replaying(fault_fs: BrixFaultFS) -> None:
    source = fault_fs.backing.parent / "source.bin"
    source.write_bytes(os.urandom(256 << 10))
    fault_fs.configure(
        {
            "trace": True,
            "rules": [
                {
                    "path": "/target.bin",
                    "op": "write",
                    "action": "torn",
                    "bytes": 4096,
                    "errno": "EIO",
                    "count": 1,
                }
            ],
        }
    )

    started = time.monotonic()
    with pytest.raises(OSError) as caught:
        xrdclient.copy(
            source,
            fault_fs.mount / "target.bin",
            config=_config(),
            chunk_size=64 << 10,
            verify=False,
        )

    assert caught.value.errno == errno.EIO
    assert time.monotonic() - started < 5
    assert fault_fs.command("status")["faults"] == 1


def test_an_fsync_that_commits_then_errors_is_not_replayed(fault_fs: BrixFaultFS) -> None:
    payload = os.urandom(256 << 10)
    source = fault_fs.backing.parent / "source.bin"
    source.write_bytes(payload)
    fault_fs.configure(
        {
            "writeback": True,
            "fsync": "commit",
            "flush_on_close": False,
            "trace": True,
            "rules": [
                {
                    "path": "/target.bin",
                    "op": "fsync",
                    "action": "after_error",
                    "errno": "EIO",
                }
            ],
        }
    )

    with pytest.raises(OSError) as caught:
        xrdclient.copy(source, fault_fs.mount / "target.bin", config=_config(), verify=False)

    assert caught.value.errno == errno.EIO
    assert (fault_fs.backing / "target.bin").read_bytes() == payload
    trace = fault_fs.command("status")["trace"]
    assert [row["op"] for row in trace].count("fsync") == 1


def test_late_fsync_enospc_prevents_false_success(fault_fs: BrixFaultFS) -> None:
    source = fault_fs.backing.parent / "source.bin"
    source.write_bytes(os.urandom(256 << 10))
    fault_fs.configure(
        {
            "trace": True,
            "rules": [{"path": "/target.bin", "op": "fsync", "action": "error", "errno": "ENOSPC"}],
        }
    )

    with pytest.raises(OSError) as caught:
        xrdclient.copy(
            source,
            fault_fs.mount / "target.bin",
            config=_config(),
            chunk_size=64 << 10,
            verify=False,
        )

    assert caught.value.errno == errno.ENOSPC
    assert fault_fs.command("status")["trace"][-1]["op"] == "fsync"
