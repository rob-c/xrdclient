"""The local-copy conformance suites of :mod:`xrdclient.testing.brix`, checked without FUSE.

The real suites need ``brix-fault-fs`` and skip without it, so the checks
themselves are pinned here against a scripted stand-in: a "mount" that is a
symlink to the backing directory, and a client that does exactly what a
correct client is seen to do under each policy (copies, leaves a partial
target, fails with the right errno). The suites must pass that client, and
must fail one that reports success where a fault should have stopped it, or
reads every failure as the wrong kind - so a check that cannot fail is
caught here, not on the one machine with the tool.
"""

from __future__ import annotations

import errno
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from xrdclient.testing import brix
from xrdclient.testing.brix import (
    POLICIES,
    BrixFaultFS,
    Kind,
    LocalCopyAdapter,
    LocalCopySuite,
)


class ScriptedFS(BrixFaultFS):
    """No FUSE and no tool: the mount is the backing directory, policies are only recorded.

    :attr:`faults` and :attr:`trace` are what ``status`` reports; the
    scripted client sets them to what the real filesystem would have seen.
    """

    def __init__(self, parent: Path) -> None:
        """The real harness's directories, with an interpreter standing in for the tool."""
        super().__init__(parent, tool=[sys.executable])
        self.policy: dict[str, Any] = {}
        self.faults = 0
        self.trace: list[dict[str, str]] = []

    def __enter__(self) -> ScriptedFS:
        """Mount, in effect: the mount directory becomes a link to the backing one."""
        self.mount.rmdir()
        self.mount.symlink_to(self.backing, target_is_directory=True)
        return self

    def command(self, command: str) -> dict[str, Any]:
        """``configure`` records the policy; ``status`` reports the scripted faults and trace."""
        verb, _, argument = command.partition(" ")
        if verb == "configure":
            self.policy = json.loads(argument)
            return {"ok": True}
        assert verb == "status", command
        return {"faults": self.faults, "trace": self.trace}


class ChecksumMismatch(Exception):
    """The scripted client's integrity-check failure: an error with no errno."""


class ShortCopy(Exception):
    """The scripted client's report of a source that held fewer bytes than it said."""


def _oserror(code: int) -> Callable[[], Exception]:
    """A factory of a fresh ``OSError`` for ``code`` (one per raise, so no traceback piles up)."""
    return lambda: OSError(code, f"scripted {errno.errorcode[code]}")


@dataclass(frozen=True)
class Outcome:
    """What a correct client does under one policy, as the real filesystem would see it."""

    copies: bool = False  # the whole target is written
    partial: bool = False  # the first 64 KiB of it are
    faults: int = 0
    trace: tuple[str, ...] = ()
    error: Callable[[], Exception] | None = None


#: A correct client's outcome under each of the suites' policies.
OUTCOMES: dict[str, Outcome] = {
    "short-io": Outcome(copies=True, faults=2),
    "late-write-enospc": Outcome(partial=True, faults=1, error=_oserror(errno.ENOSPC)),
    "zero-progress": Outcome(error=_oserror(errno.EIO)),
    "torn-write": Outcome(faults=1, error=_oserror(errno.EIO)),
    "dropped-write": Outcome(copies=True, faults=1, error=ChecksumMismatch),
    "stable": Outcome(copies=True),
    "fsync-ack-only": Outcome(error=_oserror(errno.EIO)),
    "fsync-commit-then-eio": Outcome(
        copies=True, trace=("write", "fsync"), error=_oserror(errno.EIO)
    ),
    "fsync-enospc": Outcome(trace=("write", "fsync"), error=_oserror(errno.ENOSPC)),
    "stale-read-bursts": Outcome(copies=True, faults=4),
    "stale-opens": Outcome(copies=True, faults=2),
    "stale-read-once": Outcome(faults=1, error=_oserror(errno.ESTALE)),
    "lying-size": Outcome(error=ShortCopy),
}


class ScriptedClient(LocalCopyAdapter):
    """A correct client, simulated: it acts out the :data:`OUTCOMES` row of the live policy."""

    def __init__(self, filesystem: ScriptedFS) -> None:
        """Bound to the filesystem whose policy it reads and whose status it writes."""
        self.filesystem = filesystem

    def outcome(self) -> Outcome:
        """The row for the policy the filesystem was configured with."""
        names = [name for name, policy in POLICIES.items() if policy == self.filesystem.policy]
        return OUTCOMES[names[0]]

    def copy(
        self,
        source: Path,
        target: Path,
        *,
        verify: bool = False,
        retries: int | None = None,
        retry_interval: int = 0,
    ) -> None:
        """Do to the files, and report to the filesystem, what the outcome says."""
        outcome = self.outcome()
        self.filesystem.faults = outcome.faults
        self.filesystem.trace = [{"op": op} for op in outcome.trace]
        if outcome.copies:
            shutil.copyfile(source, target)
        elif outcome.partial:
            target.write_bytes(source.read_bytes()[: 64 << 10])
        if outcome.error is not None:
            raise outcome.error()

    def kind(self, error: BaseException) -> Kind:
        """Its own integrity error is :attr:`Kind.CORRUPT`; the rest as the base reads them."""
        return Kind.CORRUPT if isinstance(error, ChecksumMismatch) else super().kind(error)


class Scripted(LocalCopySuite):
    """The whole suite against the scripted filesystem and client."""

    def make_filesystem(self, parent: Path) -> BrixFaultFS:
        """The stand-in, never the tool."""
        return ScriptedFS(parent)

    def make_adapter(self, filesystem: BrixFaultFS) -> LocalCopyAdapter:
        """The scripted client, bound to the stand-in."""
        assert isinstance(filesystem, ScriptedFS)
        return ScriptedClient(filesystem)


class TestScriptedLocalCopy(Scripted):
    """Every check passes a client that does the right thing."""


class Credulous(ScriptedClient):
    """A client that copies whatever happens and never reports a fault."""

    def outcome(self) -> Outcome:
        """Always a clean, complete copy."""
        return Outcome(copies=True)


class Errnoless(ScriptedClient):
    """A client that fails where it should but loses what the failure was."""

    def kind(self, error: BaseException) -> Kind:
        """Everything is :attr:`Kind.OTHER`."""
        return Kind.OTHER


#: The checks in which the copy must fail.
REFUSALS = [
    "test_enospc_after_partial_progress_is_reported_promptly",
    "test_a_write_that_makes_no_progress_is_an_error",
    "test_a_torn_write_fails_once_without_spinning_or_replaying",
    "test_a_silently_dropped_write_is_caught_by_verification",
    "test_an_fsync_ack_without_publication_is_not_success",
    "test_an_fsync_that_commits_then_errors_is_not_replayed",
    "test_a_late_fsync_enospc_is_not_success",
    "test_stale_recovery_refuses_to_splice_a_replaced_source",
    "test_a_source_that_lies_about_its_size_is_not_a_success",
]
#: Those of them in which it must also fail for the right reason.
KINDED = [
    name for name in REFUSALS if name != "test_a_source_that_lies_about_its_size_is_not_a_success"
]


def _against(client: type[ScriptedClient]) -> Scripted:
    """A suite instance whose client is ``client``, to call one check by hand."""

    class Suite(Scripted):
        def make_adapter(self, filesystem: BrixFaultFS) -> LocalCopyAdapter:
            assert isinstance(filesystem, ScriptedFS)
            return client(filesystem)

    return Suite()


@pytest.mark.parametrize("check", REFUSALS)
def test_a_client_that_reports_success_through_a_fault_fails_the_check(
    tmp_path: Path, check: str
) -> None:
    with pytest.raises(AssertionError, match="succeeded where it must fail"):
        getattr(_against(Credulous), check)(tmp_path)


@pytest.mark.parametrize("check", KINDED)
def test_a_client_that_misreads_the_failure_fails_the_check(tmp_path: Path, check: str) -> None:
    suite = _against(Errnoless)
    with pytest.raises(AssertionError, match=r"Kind\.OTHER"):
        getattr(suite, check)(tmp_path)
    assert suite.error is not None


@pytest.mark.parametrize(
    ("check", "message"),
    [
        ("test_short_reads_and_writes_are_completed", "no short I/O was injected"),
        ("test_bursts_of_stale_reads_are_resumed_at_the_same_offset", "too few stale reads"),
    ],
)
def test_a_copy_through_a_fault_that_never_happened_proves_nothing(
    tmp_path: Path, check: str, message: str
) -> None:
    with pytest.raises(AssertionError, match=message):
        getattr(_against(Credulous), check)(tmp_path)


def test_the_default_filesystem_is_the_tool_with_the_suites_seed(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIX_FAULT_FS", "/opt/brix/fs")
    filesystem = LocalCopySuite().make_filesystem(tmp_path)
    try:
        assert (filesystem.tool, filesystem.seed) == (["/opt/brix/fs"], 29)
        assert filesystem.backing == tmp_path / "backing"
    finally:
        filesystem.__exit__()


def test_errors_read_as_kinds() -> None:
    adapter = LocalCopyAdapter()
    assert adapter.kind(OSError(errno.ENOSPC, "full")) is Kind.NO_SPACE
    assert adapter.kind(OSError(errno.EPERM, "no")) is Kind.OTHER
    assert adapter.kind(OSError("no errno")) is Kind.OTHER
    assert adapter.kind(ValueError("not an OSError")) is Kind.OTHER
    assert brix.kind_of_errno(errno.ESTALE) is Kind.STALE
    adapter.close()


def test_the_suites_do_not_need_pytest_to_import() -> None:
    probe = "import sys, xrdclient.testing.brix; print('pytest' in sys.modules)"
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert completed.stdout.strip() == "False"
