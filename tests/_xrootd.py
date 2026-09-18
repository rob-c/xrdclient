"""A real ``xrootd`` daemon, started by the test that needs one.

Everything else in this suite runs against :class:`~xrdclient.testing.FakeServer`,
which is faithful about the wire format because it was written from the same
specification the client was. That is exactly why it cannot be the last word:
a shared misreading would pass. So where the binary is installed, the interop
and parity suites talk to the genuine article instead.

No privileges are involved. The daemon binds an ephemeral port on loopback,
exports one temporary directory, and keeps its administrative socket in a
short path under ``/tmp`` because a Unix socket path is limited to ~108 bytes
and pytest's ``tmp_path`` is nowhere near short enough.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

XROOTD = shutil.which("xrootd")

#: Enough of a configuration to be a storage element: one export, checksums
#: on (so ``kXR_Qcksum`` answers rather than saying "not supported"), and
#: extended attributes so ``kXR_fattr`` has somewhere to put them.
CONFIG = """\
xrdclient.port {port}
all.export {root}
all.adminpath {admin}
all.pidpath {admin}
xrootd.chksum max 2 adler32 crc32
ofs.persist off
xrootd.fslib default
"""


def available() -> bool:
    """Whether a real server can be started here at all."""
    return XROOTD is not None


def _free_port() -> int:
    with socket.create_server(("127.0.0.1", 0)) as sock:
        return int(sock.getsockname()[1])


class RealServer:
    """A running ``xrootd``, exporting ``root``.

        with RealServer(tmp_path) as server:
            xrdclient.FileSystem(server.url).write_bytes(server.path("a.root"), b"hi")

    Paths are absolute and real, because that is what the daemon exports; use
    :meth:`path` rather than assuming a prefix.
    """

    def __init__(self, root: str | os.PathLike[str], *, timeout: float = 20.0) -> None:
        if XROOTD is None:  # pragma: no cover - guarded by available()
            raise RuntimeError("no xrootd binary on PATH")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.port = _free_port()
        self.timeout = timeout
        # Short, and outside the export: the daemon writes its instance
        # directory into the working directory, and an exported tree with the
        # server's own droppings in it makes every directory listing a lie.
        self._admin = Path(tempfile.mkdtemp(prefix=f"xrd{os.getuid()}-", dir="/tmp"))
        self._config = self._admin / "xrootd.cfg"
        self._log = self._admin / "xrootd.log"
        self._proc: subprocess.Popen[bytes] | None = None
        self._tail = ""

    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        return f"root://127.0.0.1:{self.port}/"

    def path(self, name: str = "") -> str:
        """An exported path, absolute the way the daemon sees it."""
        return str(self.root / name) if name else str(self.root)

    def __repr__(self) -> str:
        state = "running" if self.running else "stopped"
        return f"RealServer(127.0.0.1:{self.port}, {self.root}, {state})"

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------

    def start(self) -> RealServer:
        self._config.write_text(
            CONFIG.format(port=self.port, root=self.root, admin=self._admin)
        )
        with self._log.open("wb") as handle:
            self._proc = subprocess.Popen(
                [str(XROOTD), "-c", str(self._config), "-n", "test"],
                cwd=str(self._admin),
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
        self._wait()
        return self

    def _wait(self) -> None:
        """Block until the port answers, or say what the log said instead."""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if not self.running:
                break
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.1)
        self.stop()
        tail = self.log()[-2000:]
        raise RuntimeError(f"xrootd did not come up on port {self.port}:\n{tail}")

    def stop(self) -> None:
        """Terminate the daemon and clean up after it. Idempotent."""
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - a wedged daemon
                proc.kill()
                proc.wait(timeout=5)
        self._tail = self.log()
        shutil.rmtree(self._admin, ignore_errors=True)

    def log(self) -> str:
        """The daemon's own log, for when a test fails and the client is innocent."""
        if self._log.exists():
            return self._log.read_text(errors="replace")
        return self._tail

    def __enter__(self) -> RealServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
