"""Harnesses for the external BRIX fault injectors: a TCP proxy and a FUSE filesystem.

:class:`~xrdclient.testing.FaultProxy` is the in-process fault proxy; BRIX
(``brix-fault-proxy``, ``brix-fault-fs``) is the external, scriptable one,
used where a fault must happen below Python: a connection reset inside a
TLS record, a ``write(2)`` that returns short, an ``fsync`` that reports
``ENOSPC`` after committing. :class:`BrixProxy` runs the proxy in front of a
port; :class:`BrixFaultFS` mounts a directory-backed FUSE filesystem whose
reads, writes, opens and syncs fail as a live JSON policy says. Both are
context managers that start the tool, wait until its control channel
answers, and stop it (unmounting first) however the block ends.

The tools are optional. Each is found through an environment variable
(``BRIX_FAULT_PROXY``, ``BRIX_FAULT_FS``) or on ``PATH``; when it is absent
the harness raises :class:`unittest.SkipTest`, which pytest and unittest both
report as a skip, so a suite needs no import of its own to stay green on a
machine without them. xrdclient's and xgfalclient's suites each carried an
identical private copy of these two classes; both use this module since 0.4.0.

Both clients also tested the same FUSE faults against a copy of one local
file to another - short and zero-progress I/O, ``ENOSPC`` after partial
progress, torn and silently dropped writes, an ``fsync`` acknowledged
without publication or committed and then failed, a late ``ENOSPC`` from
``fsync``, a source lying about its size, stale handles in bursts and a
source replaced during recovery, and stable storage on success - each in
its own words. :class:`WriteFaultSuite`, :class:`DurabilitySuite` and
:class:`SourceFaultSuite` (all three: :class:`LocalCopySuite`) are those
checks once, as classes a client's test module runs by subclassing, the way
:mod:`xrdhttp.testing.conformance` holds the HTTP ones::

    from xrdclient.testing.brix import Kind, LocalCopyAdapter, LocalCopySuite

    class Mine(LocalCopyAdapter):     # "copy local file A to local file B"
        def copy(self, source, target, *, verify, retries, retry_interval): ...

    class TestLocalCopy(LocalCopySuite):
        def make_adapter(self, filesystem):
            return Mine()

Each check takes pytest's ``tmp_path`` and mounts a fresh :class:`BrixFaultFS`
under it (:meth:`LocalCopyConformance.make_filesystem`), so this module still
imports neither pytest nor a client. A client's errors are read as a
:class:`Kind`; what only one client promises - gfal2's errno and wording,
XrdCl's error classes and results - is checked by overriding a test, calling
``super()``, and then looking at :attr:`LocalCopyConformance.error`,
:attr:`LocalCopyConformance.reply` or the backing directory, which outlive
the mount. The suites' names do not start with ``Test``, so importing one
does not run it with no client behind it.
"""

from __future__ import annotations

import contextlib
import enum
import errno
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, ClassVar, cast

__all__ = [
    "ERRNO_KINDS",
    "POLICIES",
    "BrixFaultFS",
    "BrixProxy",
    "DurabilitySuite",
    "Kind",
    "LocalCopyAdapter",
    "LocalCopyConformance",
    "LocalCopySuite",
    "SourceFaultSuite",
    "WriteFaultSuite",
    "find_tool",
    "kind_of_errno",
]


def find_tool(variable: str, command: str) -> str:
    """The BRIX tool ``variable`` names, else ``command`` on ``PATH``.

    Raises :class:`unittest.SkipTest` when neither exists: the tool is an
    optional integration dependency, so its absence is an environmental skip.
    """
    found = os.environ.get(variable) or shutil.which(command)
    if found is None:
        raise unittest.SkipTest(f"optional tool absent: no {command} on PATH and {variable} unset")
    return found


def _launcher(tool: str | Sequence[str] | None, variable: str, command: str) -> list[str]:
    """The argv prefix that runs the tool: the one given, else the one found."""
    if tool is None:
        return [find_tool(variable, command)]
    return [tool] if isinstance(tool, str) else list(tool)


def _free_port() -> int:
    """A loopback TCP port nobody is listening on (at the moment of asking)."""
    with socket.create_server(("127.0.0.1", 0)) as listener:
        return int(listener.getsockname()[1])


def _stop(process: subprocess.Popen[str], first: float) -> None:
    """Wait ``first`` seconds for ``process`` to exit; then terminate, then kill it.

    Each escalation gets ``first`` seconds too, so a tool that ignores
    SIGTERM still cannot outlive the test.
    """
    try:
        process.wait(timeout=first)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=first)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=first)
    if process.stdout is not None:
        process.stdout.close()


def _startup_output(process: subprocess.Popen[str]) -> str:
    """What a tool that exited during startup printed (its stdout, merged with stderr)."""
    return process.stdout.read() if process.stdout is not None else ""


class BrixProxy:
    """A deterministic BRIX TCP proxy in front of ``target_port``, with live control.

    Clients connect to :attr:`listen_port`; :meth:`command` sends one control
    command (``"reset-after 4096"``, ``"clear"``, ...) and returns the tool's
    reply. ``seed`` makes the tool's random faults repeatable. ``tool``
    overrides the lookup (:func:`find_tool`): a path, or the argv prefix that
    runs the tool (``[python, "stand-in.py"]``, as this module's own tests do).
    """

    #: Seconds to wait for the control port, and for each step of a stop.
    STARTUP: ClassVar[float] = 5.0
    STOP: ClassVar[float] = 5.0

    def __init__(
        self, target_port: int, *, seed: int = 1, tool: str | Sequence[str] | None = None
    ) -> None:
        """Pick free listen and control ports; nothing starts until ``with``."""
        self.tool = _launcher(tool, "BRIX_FAULT_PROXY", "brix-fault-proxy")
        self.listen_port = _free_port()
        self.control_port = _free_port()
        while self.control_port == self.listen_port:
            self.control_port = _free_port()
        self.target_port = target_port
        self.seed = seed
        self.process: subprocess.Popen[str] | None = None

    def __enter__(self) -> BrixProxy:
        """Start the proxy and return once its control port accepts connections.

        Raises :class:`RuntimeError` with the tool's output if it exits during
        startup, or if the control port does not open within :attr:`STARTUP`.
        """
        argv = [*self.tool, "--listen", str(self.listen_port)]
        argv += ["--target", f"127.0.0.1:{self.target_port}"]
        argv += ["--control", str(self.control_port), "--seed", str(self.seed), "--quiet"]
        self.process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        deadline = time.monotonic() + self.STARTUP
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                output = _startup_output(self.process)
                self.__exit__()
                raise RuntimeError(f"brix-fault-proxy exited during startup: {output}")
            with contextlib.suppress(OSError):
                socket.create_connection(("127.0.0.1", self.control_port), timeout=0.1).close()
                return self
            time.sleep(0.02)
        self.__exit__()
        raise RuntimeError("brix-fault-proxy control port did not start")

    def __exit__(self, *exc: object) -> None:
        """Stop the proxy (SIGTERM, then SIGKILL); a second call does nothing."""
        process, self.process = self.process, None
        if process is not None:
            process.terminate()
            _stop(process, self.STOP)

    def command(self, command: str) -> str:
        """Send one control command; its reply, or :class:`RuntimeError` with the tool's error."""
        completed = subprocess.run(
            [*self.tool, "ctl", f"127.0.0.1:{self.control_port}", command],
            capture_output=True,
            text=True,
            timeout=self.STOP,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr or completed.stdout)
        return completed.stdout


def unmount(path: Path) -> bool:
    """Unmount ``path`` through the first unprivileged FUSE command that succeeds.

    ``umount`` (macOS, and Linux for the mounting user), then ``fusermount3``
    and ``fusermount``; a command that is not installed is skipped. Returns
    whether one succeeded: a filesystem that already went away is not an
    error, the tool exits on unmount either way.
    """
    candidates = (
        ["umount", str(path)],
        ["fusermount3", "-u", str(path)],
        ["fusermount", "-u", str(path)],
    )
    for command in candidates:
        if shutil.which(command[0]) is None:
            continue
        completed = subprocess.run(command, capture_output=True, text=True, timeout=10)
        if completed.returncode == 0:
            return True
    return False


class BrixFaultFS:
    """A disposable, directory-backed BRIX fault filesystem with live controls.

    ``parent/backing`` holds the real files and ``parent/mount`` is where the
    FUSE view appears; a test writes through :attr:`mount` and inspects
    :attr:`backing`. :meth:`configure` replaces the live fault policy (JSON
    rules matching path, operation and action), :meth:`command` sends any
    other control command. The control socket lives under ``/tmp`` because a
    UNIX socket path must stay under about 100 bytes, which a test's
    ``tmp_path`` can exceed.
    """

    STARTUP: ClassVar[float] = 15.0
    STOP: ClassVar[float] = 5.0

    def __init__(
        self, parent: Path, *, seed: int = 29, tool: str | Sequence[str] | None = None
    ) -> None:
        """Make the backing and mount directories; nothing mounts until ``with``."""
        self.tool = _launcher(tool, "BRIX_FAULT_FS", "brix-fault-fs")
        self.backing = parent / "backing"
        self.mount = parent / "mount"
        self.backing.mkdir()
        self.mount.mkdir()
        # The tool serves the backing directory; this marker appearing in the
        # mount is how readiness is seen from outside.
        (self.backing / ".ready").touch()
        self._control_dir = Path(tempfile.mkdtemp(prefix="brix-fs-", dir="/tmp"))
        self.control = self._control_dir / "control.sock"
        self.seed = seed
        self.process: subprocess.Popen[str] | None = None

    def __enter__(self) -> BrixFaultFS:
        """Mount and return once both the control socket and the mount are up.

        Raises :class:`RuntimeError` (after cleaning up) if the tool exits
        during startup or is not ready within :attr:`STARTUP`.
        """
        argv = [*self.tool, "--root", str(self.backing), "--mount", str(self.mount)]
        argv += ["--control", str(self.control), "--seed", str(self.seed)]
        self.process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        deadline = time.monotonic() + self.STARTUP
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                output = _startup_output(self.process)
                self.__exit__()
                raise RuntimeError(f"brix-fault-fs exited during startup: {output}")
            # A FUSE mount half-way up can fail a stat with ENOTCONN, which
            # Path.exists() re-raises: not ready yet, not an error.
            with contextlib.suppress(OSError):
                if self.control.exists() and (self.mount / ".ready").exists():
                    return self
            time.sleep(0.05)
        self.__exit__()
        raise RuntimeError("brix-fault-fs mount did not become ready")

    def __exit__(self, *exc: object) -> None:
        """Clear the faults, unmount, stop the tool and remove the control directory.

        Faults are cleared first so the unmount itself cannot be failed by
        the policy under test. A second call only removes what is left.
        """
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                with contextlib.suppress(OSError, RuntimeError, subprocess.SubprocessError):
                    self.command("clear")
                unmount(self.mount)
            _stop(process, self.STOP)
        shutil.rmtree(self._control_dir, ignore_errors=True)

    def command(self, command: str) -> dict[str, Any]:
        """Run one live control command and return its JSON reply (RuntimeError on failure)."""
        completed = subprocess.run(
            [*self.tool, "--ctl", str(self.control), command],
            capture_output=True,
            text=True,
            timeout=2 * self.STOP,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr or completed.stdout)
        return cast("dict[str, Any]", json.loads(completed.stdout))

    def configure(self, policy: dict[str, Any]) -> dict[str, Any]:
        """Atomically replace the live fault policy; the tool's acknowledgement."""
        return self.command(f"configure {json.dumps(policy, separators=(',', ':'))}")


class Kind(enum.Enum):
    """What a failed local copy means, in the words every client's errors translate into.

    POSIX's word where the filesystem gave one (a client's ``OSError`` or its
    own error class with an ``errno``), and one for what a client finds
    itself: a target that does not read back as the source.
    """

    #: The filesystem is full (``ENOSPC``), whether a write or an ``fsync`` said so.
    NO_SPACE = "no space"
    #: The filesystem failed and said only that (``EIO``).
    IO = "i/o"
    #: A handle went stale (``ESTALE``) and could not be recovered.
    STALE = "stale"
    #: The client's integrity check found a target that differs from its source.
    CORRUPT = "corrupt"
    #: Anything else.
    OTHER = "other"


#: How errno values read as a :class:`Kind`; anything else is :attr:`Kind.OTHER`.
ERRNO_KINDS: dict[int, Kind] = {
    errno.ENOSPC: Kind.NO_SPACE,
    errno.EIO: Kind.IO,
    errno.ESTALE: Kind.STALE,
}


def kind_of_errno(code: int | None) -> Kind:
    """The :class:`Kind` an ``errno`` value names; :attr:`Kind.OTHER` for any other."""
    return ERRNO_KINDS.get(code or 0, Kind.OTHER)


class LocalCopyAdapter:
    """One client's way of copying a local file to a local file, in neutral terms.

    A client's test module implements :meth:`copy` through the API a user
    calls (``xrdclient.copy``, ``Gfal2Context.filecopy``), and :meth:`kind`
    for the errors that are not an ``OSError`` with the right ``errno``. The
    meaning of each argument is the contract every client is held to.
    """

    def copy(
        self,
        source: Path,
        target: Path,
        *,
        verify: bool = False,
        retries: int | None = None,
        retry_interval: int = 0,
    ) -> None:
        """Copy ``source`` to ``target``; return on success, raise the client's error otherwise.

        ``verify`` asks for the client's integrity check of the target
        against the source afterwards. ``retries`` is how many times the
        client may retry a failed step (``None``: its default), waiting
        ``retry_interval`` seconds between tries.
        """
        raise NotImplementedError

    def kind(self, error: BaseException) -> Kind:
        """What ``error`` means: an ``OSError``'s ``errno``, else :attr:`Kind.OTHER`."""
        if isinstance(error, OSError):
            return kind_of_errno(error.errno)
        return Kind.OTHER

    def close(self) -> None:
        """Release whatever the adapter holds; it holds nothing by default."""


def _policy(*rules: dict[str, Any], **settings: Any) -> dict[str, Any]:
    """A fault policy: ``settings`` (writeback, fsync mode, tracing) and ``rules``."""
    return {**settings, "rules": list(rules)} if rules else settings


def _rule(path: str, op: str, action: str, **match: Any) -> dict[str, Any]:
    """One fault rule: ``action`` on every ``op`` of ``path`` that ``match`` selects."""
    return {"path": path, "op": op, "action": action, **match}


_SOURCE, _TARGET = "/source.bin", "/target.bin"
#: Every write-back filesystem the durability checks mount: no flush when a file closes.
_WRITEBACK: dict[str, Any] = {"writeback": True, "flush_on_close": False}

#: The fault policy of each check, by name; a client's own test may reuse one.
POLICIES: dict[str, dict[str, Any]] = {
    "short-io": _policy(
        _rule(_SOURCE, "read", "short", bytes=4096),
        _rule(_TARGET, "write", "short", bytes=4096),
        trace=True,
    ),
    "late-write-enospc": _policy(
        _rule(_TARGET, "write", "error", errno="ENOSPC", after_bytes=64 << 10), trace=True
    ),
    "zero-progress": _policy(_rule(_TARGET, "write", "short", bytes=0)),
    "torn-write": _policy(
        _rule(_TARGET, "write", "torn", bytes=4096, errno="EIO", count=1), trace=True
    ),
    "dropped-write": _policy(
        _rule(_TARGET, "write", "drop", offset_min=64 << 10, offset_max=(128 << 10) - 1),
        trace=True,
    ),
    "stable": _policy(**_WRITEBACK, fsync="commit"),
    "fsync-ack-only": _policy(**_WRITEBACK, fsync="ack"),
    "fsync-commit-then-eio": _policy(
        _rule(_TARGET, "fsync", "after_error", errno="EIO"),
        **_WRITEBACK,
        fsync="commit",
        trace=True,
    ),
    "fsync-enospc": _policy(_rule(_TARGET, "fsync", "error", errno="ENOSPC"), trace=True),
    "stale-read-bursts": _policy(
        _rule(_SOURCE, "read", "error", errno="ESTALE", every=4, burst=2), trace=True
    ),
    "stale-opens": _policy(_rule(_SOURCE, "open", "error", errno="ESTALE", count=2)),
    "stale-read-once": _policy(_rule(_SOURCE, "read", "error", errno="ESTALE", count=1)),
    "lying-size": _policy(_rule(_SOURCE, "getattr", "metadata", size_delta=64 << 10)),
}

#: Seconds within which a copy that hit a hard fault must have failed: no spinning.
_PROMPT = 5.0


def _random_file(path: Path, size: int) -> bytes:
    """Fill ``path`` with ``size`` random bytes and return them."""
    payload = os.urandom(size)
    path.write_bytes(payload)
    return payload


class LocalCopyConformance:
    """The base of every local-copy suite: a fault filesystem, the client, and the checks' tools.

    A subclass supplies :meth:`make_adapter`; :meth:`make_filesystem` may
    be overridden too (another seed, a stand-in). Sources and targets are
    either ``mount`` paths, where the faults are, or plain files beside the
    mount (``tmp_path / "source.bin"``), so a fault hits only one side.
    """

    #: The seed of the filesystem's random faults.
    seed: ClassVar[int] = 29
    #: The client error the last :meth:`refused` caught, for a client's own checks.
    error: BaseException | None = None
    #: The last ``status`` reply :meth:`status` read: the faults injected, the trace.
    reply: dict[str, Any] | None = None

    def make_filesystem(self, parent: Path) -> BrixFaultFS:
        """A fresh, unmounted fault filesystem under ``parent`` (``backing``, ``mount``)."""
        return BrixFaultFS(parent, seed=self.seed)

    def make_adapter(self, filesystem: BrixFaultFS) -> LocalCopyAdapter:
        """The client under test, made once ``filesystem`` is mounted."""
        raise NotImplementedError

    @contextlib.contextmanager
    def session(
        self, tmp_path: Path, policy: str
    ) -> Iterator[tuple[BrixFaultFS, LocalCopyAdapter]]:
        """Mount a filesystem under ``tmp_path`` with :data:`POLICIES` ``[policy]`` live.

        Yields it with the client; the client is closed and the filesystem
        unmounted afterwards, however the check ends.
        """
        with self.make_filesystem(tmp_path) as filesystem:
            client = self.make_adapter(filesystem)
            try:
                filesystem.configure(POLICIES[policy])
                yield filesystem, client
            finally:
                client.close()

    def refused(
        self,
        client: LocalCopyAdapter,
        source: Path,
        target: Path,
        *,
        verify: bool = False,
        retries: int | None = None,
        retry_interval: int = 0,
    ) -> Kind:
        """Copy as :meth:`LocalCopyAdapter.copy` does, which must fail; what the failure means.

        The error is kept as :attr:`error`. A copy that succeeds fails the
        check: every use of this is a fault that must not end in success.
        """
        try:
            client.copy(
                source, target, verify=verify, retries=retries, retry_interval=retry_interval
            )
        except Exception as error:  # every client error, whatever it derives from
            self.error = error
            return client.kind(error)
        raise AssertionError(f"copying {source} to {target} succeeded where it must fail")

    def status(self, filesystem: BrixFaultFS) -> dict[str, Any]:
        """The filesystem's ``status`` reply, kept as :attr:`reply`."""
        self.reply = filesystem.command("status")
        return self.reply


class WriteFaultSuite(LocalCopyConformance):
    """Writes (and reads) that come back short, empty, torn, full or silently lost."""

    def test_short_reads_and_writes_are_completed(self, tmp_path: Path) -> None:
        """Short ``read(2)`` and ``write(2)`` results are a reason to go on, not to stop."""
        with self.session(tmp_path, "short-io") as (filesystem, client):
            payload = _random_file(filesystem.backing / "source.bin", 2 << 20)
            client.copy(
                filesystem.mount / "source.bin", filesystem.mount / "target.bin", verify=True
            )
            assert (filesystem.backing / "target.bin").read_bytes() == payload
            assert self.status(filesystem)["faults"] > 0, "no short I/O was injected"

    def test_enospc_after_partial_progress_is_reported_promptly(self, tmp_path: Path) -> None:
        """A full filesystem part-way through is ``ENOSPC``, at once, not a retry loop."""
        with self.session(tmp_path, "late-write-enospc") as (filesystem, client):
            _random_file(tmp_path / "source.bin", 1 << 20)
            started = time.monotonic()
            kind = self.refused(client, tmp_path / "source.bin", filesystem.mount / "target.bin")
            assert kind is Kind.NO_SPACE, kind
            assert time.monotonic() - started < _PROMPT

    def test_a_write_that_makes_no_progress_is_an_error(self, tmp_path: Path) -> None:
        """A ``write(2)`` that returns 0 again and again is ``EIO``, never a silent hang."""
        with self.session(tmp_path, "zero-progress") as (filesystem, client):
            (tmp_path / "source.bin").write_bytes(b"x" * (128 << 10))
            kind = self.refused(client, tmp_path / "source.bin", filesystem.mount / "target.bin")
            assert kind is Kind.IO, kind

    def test_a_torn_write_fails_once_without_spinning_or_replaying(self, tmp_path: Path) -> None:
        """A write that lands in part and then errors fails the copy: once, promptly."""
        with self.session(tmp_path, "torn-write") as (filesystem, client):
            _random_file(tmp_path / "source.bin", 256 << 10)
            started = time.monotonic()
            kind = self.refused(client, tmp_path / "source.bin", filesystem.mount / "target.bin")
            assert kind is Kind.IO, kind
            assert time.monotonic() - started < _PROMPT
            assert self.status(filesystem)["faults"] == 1, "the torn write was replayed"

    def test_a_silently_dropped_write_is_caught_by_verification(self, tmp_path: Path) -> None:
        """A write the filesystem acknowledges and discards is found by the integrity check."""
        with self.session(tmp_path, "dropped-write") as (filesystem, client):
            _random_file(tmp_path / "source.bin", 512 << 10)
            target = filesystem.mount / "target.bin"
            kind = self.refused(client, tmp_path / "source.bin", target, verify=True)
            assert kind is Kind.CORRUPT, kind
            assert self.status(filesystem)["faults"] > 0, "no write was dropped"


class DurabilitySuite(LocalCopyConformance):
    """Success means the bytes are on stable storage, whatever ``fsync`` claims."""

    def test_success_means_the_bytes_reached_stable_storage(self, tmp_path: Path) -> None:
        """Through a write-back cache that only ``fsync`` commits, a success is in the backing."""
        with self.session(tmp_path, "stable") as (filesystem, client):
            payload = _random_file(tmp_path / "source.bin", 1 << 20)
            client.copy(tmp_path / "source.bin", filesystem.mount / "target.bin")
            assert (filesystem.backing / "target.bin").read_bytes() == payload

    def test_an_fsync_ack_without_publication_is_not_success(self, tmp_path: Path) -> None:
        """An ``fsync`` that answers yes and commits nothing is caught: the target is not there."""
        with self.session(tmp_path, "fsync-ack-only") as (filesystem, client):
            _random_file(tmp_path / "source.bin", 256 << 10)
            kind = self.refused(client, tmp_path / "source.bin", filesystem.mount / "target.bin")
            assert kind is Kind.IO, kind
            assert not (filesystem.backing / "target.bin").exists()

    def test_an_fsync_that_commits_then_errors_is_not_replayed(self, tmp_path: Path) -> None:
        """An ``fsync`` error after the commit fails the copy, and ``fsync`` is not tried again."""
        with self.session(tmp_path, "fsync-commit-then-eio") as (filesystem, client):
            _random_file(tmp_path / "source.bin", 256 << 10)
            kind = self.refused(client, tmp_path / "source.bin", filesystem.mount / "target.bin")
            assert kind is Kind.IO, kind
            operations = [row["op"] for row in self.status(filesystem)["trace"]]
            assert operations.count("fsync") == 1, operations

    def test_a_late_fsync_enospc_is_not_success(self, tmp_path: Path) -> None:
        """Every write accepted, then ``ENOSPC`` from ``fsync``: the copy failed."""
        with self.session(tmp_path, "fsync-enospc") as (filesystem, client):
            _random_file(tmp_path / "source.bin", 256 << 10)
            kind = self.refused(client, tmp_path / "source.bin", filesystem.mount / "target.bin")
            assert kind is Kind.NO_SPACE, kind
            operations = [row["op"] for row in self.status(filesystem)["trace"]]
            assert "fsync" in operations, operations


class SourceFaultSuite(LocalCopyConformance):
    """A source whose handles go stale, which is replaced, or which lies about its size."""

    def test_bursts_of_stale_reads_are_resumed_at_the_same_offset(self, tmp_path: Path) -> None:
        """Repeated ``ESTALE`` reads are recovered by reopening, and no byte moves or repeats."""
        with self.session(tmp_path, "stale-read-bursts") as (filesystem, client):
            payload = _random_file(filesystem.backing / "source.bin", 512 << 10)
            client.copy(filesystem.mount / "source.bin", tmp_path / "target.bin", retries=3)
            assert (tmp_path / "target.bin").read_bytes() == payload
            assert self.status(filesystem)["faults"] >= 4, "too few stale reads were injected"

    def test_a_burst_of_stale_opens_is_retried(self, tmp_path: Path) -> None:
        """An ``open`` that fails ``ESTALE`` twice succeeds on the third try."""
        with self.session(tmp_path, "stale-opens") as (filesystem, client):
            payload = _random_file(filesystem.backing / "source.bin", 256 << 10)
            client.copy(filesystem.mount / "source.bin", tmp_path / "target.bin", retries=3)
            assert (tmp_path / "target.bin").read_bytes() == payload

    def test_stale_recovery_refuses_to_splice_a_replaced_source(self, tmp_path: Path) -> None:
        """A source replaced while its stale handle is recovered is not spliced onto the old one.

        The replacement lands during the client's one-second wait before it
        reopens; the copy must fail ``ESTALE`` and leave no target behind.
        """
        with self.session(tmp_path, "stale-read-once") as (filesystem, client):
            (filesystem.backing / "source.bin").write_bytes(b"A" * (512 << 10))
            (filesystem.backing / "replacement.bin").write_bytes(b"B" * (512 << 10))
            source = filesystem.mount / "source.bin"
            replace = threading.Timer(
                0.2, os.replace, args=(filesystem.mount / "replacement.bin", source)
            )
            replace.start()
            try:
                kind = self.refused(
                    client, source, tmp_path / "target.bin", retries=3, retry_interval=1
                )
            finally:
                replace.join(timeout=_PROMPT)
            assert kind is Kind.STALE, kind
            assert not (tmp_path / "target.bin").exists()

    def test_a_source_that_lies_about_its_size_is_not_a_success(self, tmp_path: Path) -> None:
        """A ``stat`` promising more bytes than the file holds ends in a failure, not a copy."""
        with self.session(tmp_path, "lying-size") as (filesystem, client):
            _random_file(filesystem.backing / "source.bin", 256 << 10)
            self.refused(client, filesystem.mount / "source.bin", tmp_path / "target.bin")


class LocalCopySuite(WriteFaultSuite, DurabilitySuite, SourceFaultSuite):
    """Every local-copy check: the write, durability and source suites together."""
