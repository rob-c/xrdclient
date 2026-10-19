"""Local copies through the external BRIX FUSE fault filesystem.

The faults every client's local copy must survive or report - short, empty,
torn and dropped writes, ``ENOSPC`` early and late, dishonest ``fsync``,
stale and replaced sources, a lying size - are checked once, in
:mod:`xrdclient.testing.brix`, and run here through :class:`XrdLocalAdapter`:
``xrdclient.copy`` with one stream and 64 KiB chunks. xgfalclient runs the
same suite through ``Gfal2Context.filecopy``. What only XrdCl promises - its
error classes and wording, the size it reports, the partial target it leaves
for a resumed copy - is checked by the overrides below, after the shared check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import xrdclient
from xrdclient.testing.brix import BrixFaultFS, Kind, LocalCopyAdapter, LocalCopySuite

pytestmark = pytest.mark.interop


class XrdLocalAdapter(LocalCopyAdapter):
    """``xrdclient.copy`` between two local paths, one 64 KiB chunk in flight at a time."""

    def copy(
        self,
        source: Path,
        target: Path,
        *,
        verify: bool = False,
        retries: int | None = None,
        retry_interval: int = 0,
    ) -> None:
        """Copy, and check the size XrdCl reports is the size that arrived."""
        config = xrdclient.Config(bulk=False, in_flight=1, parallel_chunks=1)
        if retries is not None:
            config = config.evolve(connect_retries=retries, retry_backoff=retry_interval)
        result = xrdclient.copy(source, target, config=config, chunk_size=64 << 10, verify=verify)
        assert result.size == target.stat().st_size

    def kind(self, error: BaseException) -> Kind:
        """A checksum mismatch is :class:`xrdclient.ChecksumMismatchError`; the rest carry errno."""
        if isinstance(error, xrdclient.ChecksumMismatchError):
            return Kind.CORRUPT
        return super().kind(error)


class TestLocalCopy(LocalCopySuite):
    """The shared suite, plus what XrdCl alone promises about each outcome."""

    def make_adapter(self, filesystem: BrixFaultFS) -> LocalCopyAdapter:
        """The same adapter for every check."""
        return XrdLocalAdapter()

    def test_enospc_after_partial_progress_is_reported_promptly(self, tmp_path: Path) -> None:
        """The partial target stays, so a resumed copy can carry on from it."""
        super().test_enospc_after_partial_progress_is_reported_promptly(tmp_path)
        size = (tmp_path / "backing" / "target.bin").stat().st_size
        assert 0 < size < (tmp_path / "source.bin").stat().st_size

    def test_a_silently_dropped_write_is_caught_by_verification(self, tmp_path: Path) -> None:
        """The mismatch is XrdCl's checksum error, and the full-length target is left."""
        super().test_a_silently_dropped_write_is_caught_by_verification(tmp_path)
        assert isinstance(self.error, xrdclient.ChecksumMismatchError)
        size = (tmp_path / "backing" / "target.bin").stat().st_size
        assert size == (tmp_path / "source.bin").stat().st_size

    def test_an_fsync_that_commits_then_errors_is_not_replayed(self, tmp_path: Path) -> None:
        """The committed bytes are left in place."""
        super().test_an_fsync_that_commits_then_errors_is_not_replayed(tmp_path)
        committed = (tmp_path / "backing" / "target.bin").read_bytes()
        assert committed == (tmp_path / "source.bin").read_bytes()

    def test_a_late_fsync_enospc_is_not_success(self, tmp_path: Path) -> None:
        """The ``fsync`` is the last thing the copy does to the target."""
        super().test_a_late_fsync_enospc_is_not_success(tmp_path)
        assert self.reply is not None
        assert self.reply["trace"][-1]["op"] == "fsync"

    def test_stale_recovery_refuses_to_splice_a_replaced_source(self, tmp_path: Path) -> None:
        """XrdCl says why it gave up."""
        super().test_stale_recovery_refuses_to_splice_a_replaced_source(tmp_path)
        assert "changed while recovering" in str(self.error)

    def test_a_source_that_lies_about_its_size_is_not_a_success(self, tmp_path: Path) -> None:
        """The shortfall is XrdCl's incomplete-transfer error."""
        super().test_a_source_that_lies_about_its_size_is_not_a_success(tmp_path)
        assert isinstance(self.error, xrdclient.XRootDError)
        assert "file is incomplete" in str(self.error)
