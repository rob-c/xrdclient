"""The BRIX harnesses, driven against stand-in tools so they are tested everywhere.

The real ``brix-fault-proxy`` and ``brix-fault-fs`` are optional (the
fault suites that use them skip without them), so the harnesses' own
lifecycle - finding the tool, waiting for its control channel, reporting a
crash or a hang at startup, stopping a tool that ignores SIGTERM, clearing
faults and unmounting on the way out - is pinned here with small Python
scripts that speak the same command line.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import unittest
from collections.abc import Iterator
from pathlib import Path

import pytest

from xrdclient.testing import brix

#: The stand-in proxy: opens the control port and serves until killed. MODE
#: (substituted below) makes it crash, hang before listening, or ignore SIGTERM.
PROXY = """
import signal, socket, sys, time
MODE = {mode!r}
args = sys.argv[1:]
if args[0] == "ctl":
    if args[2] == "fail":
        sys.exit("refused: " + args[2])
    print("ok " + args[2])
    sys.exit(0)
if MODE == "crash":
    print("cannot bind")
    sys.exit(3)
if MODE == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if MODE != "hang":
    control = socket.create_server(("127.0.0.1", int(args[args.index("--control") + 1])))
time.sleep(60)
"""

#: The stand-in filesystem: "mounts" by copying the readiness marker across.
FS = """
import json, pathlib, shutil, signal, sys, time
MODE = {mode!r}
args = sys.argv[1:]
if args[0] == "--ctl":
    if MODE == "noclear" and args[2] == "clear":
        sys.exit("control socket gone")
    print(json.dumps({{"ok": True, "command": args[2]}}))
    sys.exit(0)
if MODE == "crash":
    print("fuse: device not found")
    sys.exit(1)
if MODE == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if MODE == "quits":
    sys.exit(0)
option = dict(zip(args[::2], args[1::2]))
if MODE != "hang":
    pathlib.Path(option["--control"]).touch()
    shutil.copy(pathlib.Path(option["--root"]) / ".ready", option["--mount"])
time.sleep(60)
"""


def tool(tmp_path: Path, template: str, mode: str = "ok") -> list[str]:
    """The argv prefix running a stand-in written to ``tmp_path``, in ``mode``.

    Run through the interpreter (``-IS``: no site packages, so it starts in
    milliseconds) rather than as an executable of its own: macOS scans every
    new executable on its first run, which can take seconds.
    """
    path = tmp_path / f"fake-{mode}.py"
    path.write_text(template.format(mode=mode))
    return [sys.executable, "-IS", str(path)]


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short startup and stop deadlines, so the failure paths take a moment, not minutes."""
    for harness in (brix.BrixProxy, brix.BrixFaultFS):
        monkeypatch.setattr(harness, "STARTUP", 3.0)
        monkeypatch.setattr(harness, "STOP", 0.5)


@pytest.fixture
def target() -> Iterator[int]:
    """A listening port for the proxy to point at."""
    with socket.create_server(("127.0.0.1", 0)) as listener:
        yield int(listener.getsockname()[1])


def test_an_absent_tool_is_an_environmental_skip(monkeypatch, tmp_path, target):
    monkeypatch.delenv("BRIX_FAULT_PROXY", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(unittest.SkipTest, match="optional tool absent: no brix-fault-proxy"):
        brix.BrixProxy(target)
    monkeypatch.setenv("BRIX_FAULT_FS", "/opt/brix/fs")
    assert brix.find_tool("BRIX_FAULT_FS", "brix-fault-fs") == "/opt/brix/fs"


def test_the_proxy_starts_answers_control_commands_and_stops(tmp_path, target, quick):
    proxy = brix.BrixProxy(target, seed=7, tool=tool(tmp_path, PROXY))
    assert proxy.control_port != proxy.listen_port
    with proxy:
        assert proxy.process is not None
        argv = proxy.process.args
        assert isinstance(argv, list)
        assert argv[argv.index("--seed") + 1] == "7"
        assert proxy.command("clear") == "ok clear\n"
        with pytest.raises(RuntimeError, match="refused: fail"):
            proxy.command("fail")
    assert proxy.process is None
    proxy.__exit__()  # a second stop is harmless


def test_ports_are_drawn_again_until_listen_and_control_differ(monkeypatch, tmp_path, target):
    ports = iter([5000, 5000, 5001])
    monkeypatch.setattr(brix, "_free_port", lambda: next(ports))
    proxy = brix.BrixProxy(target, tool=tool(tmp_path, PROXY))
    assert (proxy.listen_port, proxy.control_port) == (5000, 5001)


@pytest.mark.parametrize(
    ("mode", "message"),
    [("crash", "exited during startup: cannot bind"), ("hang", "control port did not start")],
)
def test_a_proxy_that_does_not_come_up_is_reported_and_cleaned_up(
    tmp_path, target, quick, mode, message
):
    proxy = brix.BrixProxy(target, tool=tool(tmp_path, PROXY, mode))
    with pytest.raises(RuntimeError, match=message), proxy:
        pass
    assert proxy.process is None


def test_a_proxy_that_ignores_sigterm_is_killed(tmp_path, target, quick):
    with brix.BrixProxy(target, tool=tool(tmp_path, PROXY, "stubborn")) as proxy:
        process = proxy.process
    assert process is not None and process.returncode is not None


def test_the_filesystem_mounts_takes_policies_and_cleans_up(tmp_path, quick, monkeypatch):
    unmounted: list[Path] = []

    def unmount(path: Path) -> bool:
        unmounted.append(path)
        return True

    monkeypatch.setattr(brix, "unmount", unmount)
    with brix.BrixFaultFS(tmp_path, seed=31, tool=tool(tmp_path, FS)) as filesystem:
        assert (filesystem.mount / ".ready").is_file()
        reply = filesystem.configure({"rules": [{"op": "read", "action": "short"}]})
        assert reply == {
            "ok": True,
            "command": 'configure {"rules":[{"op":"read","action":"short"}]}',
        }
        control = filesystem.control.parent
    assert unmounted == [tmp_path / "mount"]
    assert not control.exists()


@pytest.mark.parametrize(
    ("mode", "message"),
    [("crash", "exited during startup: fuse: device not found"), ("hang", "did not become ready")],
)
def test_a_filesystem_that_does_not_come_up_is_reported_and_cleaned_up(
    tmp_path, quick, monkeypatch, mode, message
):
    monkeypatch.setattr(brix, "unmount", lambda path: False)
    filesystem = brix.BrixFaultFS(tmp_path, tool=tool(tmp_path, FS, mode))
    with pytest.raises(RuntimeError, match=message), filesystem:
        pass
    assert filesystem.process is None and not filesystem.control.parent.exists()


def test_a_stubborn_filesystem_is_killed_even_if_clearing_fails(tmp_path, quick, monkeypatch):
    monkeypatch.setattr(brix, "unmount", lambda path: False)
    (tmp_path / "a").mkdir()
    stubborn = brix.BrixFaultFS(tmp_path / "a", tool=tool(tmp_path, FS, "stubborn"))
    with stubborn:
        process = stubborn.process
    assert process is not None and process.returncode is not None
    (tmp_path / "b").mkdir()
    failing = brix.BrixFaultFS(tmp_path / "b", tool=tool(tmp_path, FS, "noclear"))
    with failing, pytest.raises(RuntimeError, match="control socket gone"):
        failing.command("clear")


def test_a_filesystem_that_already_quit_is_not_cleared_or_unmounted(tmp_path, quick, monkeypatch):
    def unexpected(path: Path) -> bool:
        raise AssertionError("nothing to unmount")

    monkeypatch.setattr(brix, "unmount", unexpected)
    filesystem = brix.BrixFaultFS(tmp_path, tool=tool(tmp_path, FS, "quits"))
    # Started by hand, without a pipe: the stop must not need one.
    filesystem.process = subprocess.Popen([sys.executable, "-IS", "-c", "pass"], text=True)
    filesystem.process.wait()
    filesystem.__exit__()
    assert filesystem.process is None
    filesystem.__exit__()  # a second stop only tidies up


def test_a_mount_that_cannot_be_stat_ed_yet_is_waited_for(tmp_path, quick, monkeypatch):
    monkeypatch.setattr(brix, "unmount", lambda path: True)
    real = Path.exists
    failures = iter([OSError(107, "Transport endpoint is not connected")])

    def flaky(self: Path, **kwargs: bool) -> bool:
        if self.name == "control.sock":
            error = next(failures, None)
            if error is not None:
                raise error
        return real(self, **kwargs)

    monkeypatch.setattr(Path, "exists", flaky)
    with brix.BrixFaultFS(tmp_path, tool=tool(tmp_path, FS)):
        pass


def test_unmount_tries_each_fuse_command_that_is_installed(monkeypatch, tmp_path):
    installed = {"umount": None, "fusermount3": "/bin/fusermount3", "fusermount": "/bin/fm"}
    monkeypatch.setattr(shutil, "which", installed.get)
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, int(command[0] == "fusermount3"))

    monkeypatch.setattr(subprocess, "run", run)
    assert brix.unmount(tmp_path) is True
    assert [c[0] for c in calls] == ["fusermount3", "fusermount"]
    monkeypatch.setattr(
        subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 1)
    )
    assert brix.unmount(tmp_path) is False


def test_the_json_reply_is_the_tools(tmp_path, quick, monkeypatch):
    monkeypatch.setattr(brix, "unmount", lambda path: True)
    with brix.BrixFaultFS(tmp_path, tool=tool(tmp_path, FS)) as filesystem:
        assert filesystem.command("stats") == json.loads('{"ok": true, "command": "stats"}')
