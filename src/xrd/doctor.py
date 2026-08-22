"""Why it will not work, said once, in the order it will bite you.

    >>> import xrd
    >>> print(xrd.diagnose("root://eos.example.org//store/user/me"))  # doctest: +SKIP
    ok  python        3.13.5 on linux
    ok  extras        zstandard; fsspec (absent: gssapi, google-crc32c)
    ok  settings      connect 30s, request 300s, TLS verified
    !!  auth:gsi      no proxy file at /tmp/x509up_u1000
                      -> voms-proxy-init -voms lhcb
    ok  auth:ztn      a bearer token from $BEARER_TOKEN_FILE
    ok  dns           eos.example.org -> 128.142.x.x
    ok  connect       1094/tcp answered in 41 ms
    ok  server        XRootD 5.6.3, a data server
    !!  path          /store/user/me: no such file or directory
                      -> /store/user exists; check the spelling of the last part

The point is that a beginner's first failure is almost never the one the
error mentions. A stat that says "no such file" may mean the proxy expired an
hour ago and the server is answering as nobody; a hang may be a firewall. Each
of those is a separate line here, so the first ``!!`` down the list is the
thing to fix, and everything after it is a consequence.

Nothing here changes anything: it opens connections, asks the questions a
read would ask, and prints. It never prompts, because a diagnosis that stops
for a password is not one you can put in a bug report.
"""

from __future__ import annotations

import platform
import socket
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from importlib.util import find_spec
from typing import TYPE_CHECKING

from . import auth
from .config import Config, current
from .errors import XRootDError
from .url import parse

if TYPE_CHECKING:
    from .client import FileSystem

__all__ = ["Check", "Report", "diagnose"]

#: Optional packages, under the name you would install them by.
EXTRAS = {
    "gssapi": "gssapi",
    "zstandard": "zstandard",
    "fsspec": "fsspec",
    "google_crc32c": "google-crc32c",
}

#: Mechanisms that prove who you are, as against the ones that merely say.
IDENTIFYING = ("gsi", "ztn", "krb5", "sss")

#: How each state is marked at the start of its line.
MARKS = {"ok": "ok", "bad": "!!", "warn": "??", "skip": "--"}


@dataclass(frozen=True)
class Check:
    """One question asked, and what came back.

    ``state`` is ``"ok"``, ``"bad"`` for something that will stop a transfer,
    ``"warn"`` for something that will not but is worth knowing, and
    ``"skip"`` for a question that did not apply.
    """

    name: str
    state: str
    detail: str
    hint: str = ""

    def __str__(self) -> str:
        line = f"{MARKS[self.state]}  {self.name:<13} {self.detail}"
        return f"{line}\n{'':<17} -> {self.hint}" if self.hint else line


@dataclass(frozen=True)
class Report(Sequence[Check]):
    """Every check, in the order they were asked.

    It is a sequence, so ``for check in report`` and ``report[0]`` work, and
    it prints as the block above. :attr:`ok` is the one-line answer.
    """

    checks: tuple[Check, ...] = ()
    #: The URL this was about, or ``""`` when it was about the machine alone.
    url: str = ""

    def __len__(self) -> int:
        return len(self.checks)

    def __getitem__(self, index: int) -> Check:  # type: ignore[override]
        return self.checks[index]

    def __str__(self) -> str:
        return "\n".join(str(check) for check in self.checks)

    @property
    def ok(self) -> bool:
        """Did everything that matters pass?"""
        return not any(check.state == "bad" for check in self.checks)

    @property
    def problems(self) -> list[Check]:
        """The checks that failed, first one first."""
        return [check for check in self.checks if check.state == "bad"]

    def to_dict(self) -> list[dict[str, str]]:
        """The same thing as data, for a script or a bug report."""
        return [
            {"name": c.name, "state": c.state, "detail": c.detail, "hint": c.hint}
            for c in self.checks
        ]


@dataclass
class _Run:
    """The state a diagnosis carries from one check to the next."""

    config: Config
    url: str = ""
    host: str = ""
    port: int = 0
    path: str = ""
    scheme: str = ""
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, state: str, detail: str, hint: str = "") -> None:
        self.checks.append(Check(name, state, detail, hint))


# ---------------------------------------------------------------------------
# The machine
# ---------------------------------------------------------------------------


def _python(run: _Run) -> None:
    version = ".".join(str(n) for n in sys.version_info[:3])
    run.add("python", "ok", f"{version} on {platform.system().lower()}")


def _extras(run: _Run) -> None:
    """Which optional packages are here - none of which is needed to read."""
    here: list[str] = []
    absent: list[str] = []
    for module, package in EXTRAS.items():
        (here if find_spec(module) is not None else absent).append(package)
    detail = "; ".join(here) if here else "none"
    if absent:
        detail += f" (absent: {', '.join(absent)})"
    run.add("extras", "ok", detail)


def _settings(run: _Run) -> None:
    config = run.config
    detail = (
        f"connect {config.connect_timeout:g}s, request {config.request_timeout:g}s, "
        f"TLS {'verified' if config.verify_tls else 'NOT verified'}"
    )
    if config.verify_tls:
        run.add("settings", "ok", detail)
        return
    run.add(
        "settings",
        "warn",
        detail,
        "certificates are not being checked, so a redirect can be followed anywhere; "
        "drop --no-verify-tls once the CA path is right",
    )


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _credentials(run: _Run) -> None:
    """Each mechanism in turn: has it material here, and if not, why not.

    Every one of these may fail without anything being wrong - one working
    credential is enough - so a mechanism with nothing behind it is a warning
    and it is the summary line that fails.
    """
    ready: list[str] = []
    for name in run.config.auth_order:
        cls = auth.registry().get(name)
        if cls is None:  # pragma: no cover - the registry is filled at import
            continue
        if _credential_ready(run, name, cls):
            ready.append(name)
    _credential_summary(run, ready)


def _credential_ready(run: _Run, name: str, cls: type[auth.Credential]) -> bool:
    offer = auth.Offer(name)
    try:
        cred = cls.available(offer, run.config, username=run.config.username, host=run.host)
    except Exception as exc:  # a broken mechanism must not stop the rest
        run.add(f"auth:{name}", "warn", f"{type(exc).__name__}: {exc}")
        return False
    if cred is not None:
        run.add(f"auth:{name}", "ok", "ready")
        return True
    _missing_credential(run, name, cls, offer)
    return False


def _missing_credential(
    run: _Run, name: str, cls: type[auth.Credential], offer: auth.Offer
) -> None:
    try:
        ask = cls.missing(offer, run.config, username=run.config.username, host=run.host)
    except Exception as exc:
        run.add(f"auth:{name}", "warn", f"cannot say what it wants: {exc}")
        return
    if ask is None:
        run.add(f"auth:{name}", "warn", "no material here")
        return
    run.add(f"auth:{name}", "warn", ask.reason, ask.hint)


def _credential_summary(run: _Run, ready: list[str]) -> None:
    proving = [name for name in ready if name in IDENTIFYING]
    if proving:
        run.add("auth", "ok", f"{', '.join(proving)} can prove who you are")
    elif ready:
        run.add(
            "auth",
            "warn",
            f"only {', '.join(ready)}, which says who you are without proving it",
            "an open server will let you read; one that asks for a real identity will not, "
            "so fix one of the warnings above before believing a permission error",
        )
    else:
        run.add(
            "auth",
            "bad",
            "nothing to authenticate with at all",
            "fix one of the warnings above; a proxy or a bearer token is what most sites want",
        )


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def _url(run: _Run) -> bool:
    """Split the URL up, and say what it turned out to be."""
    try:
        parsed = parse(run.url)
    except (ValueError, XRootDError) as exc:
        run.add("url", "bad", f"{run.url}: {exc}", "a URL looks like root://host:1094//path")
        return False
    run.host, run.port = parsed.host, parsed.port
    run.path, run.scheme = parsed.path, parsed.scheme
    if not run.host:
        run.add("url", "bad", f"{run.url}: no host in it", "root://HOST//path, with two slashes")
        return False
    if run.scheme == "s3":
        # The host of an ``s3://`` URL is the bucket, and the machine it lives
        # on comes from ``endpoint``. Resolving the bucket would be nonsense.
        run.add("url", "ok", f"s3 bucket {run.host}, key {run.path}")
        run.add(
            "endpoint",
            "skip",
            "a bucket is reached through an endpoint, which the URL does not name",
            "xrd-fs ls --endpoint https://rgw.example.org s3://bucket/... says whether it answers",
        )
        return False
    run.add("url", "ok", f"{run.scheme} to {run.host}:{run.port}, path {run.path or '/'}")
    return True


def _dns(run: _Run) -> bool:
    try:
        found = socket.getaddrinfo(run.host, run.port, type=socket.SOCK_STREAM)
    except OSError as exc:
        run.add(
            "dns",
            "bad",
            f"{run.host} does not resolve: {exc}",
            "check the spelling, and that this machine has the site's resolver",
        )
        return False
    addresses = sorted({str(info[4][0]) for info in found})
    run.add("dns", "ok", f"{run.host} -> {', '.join(addresses[:3])}")
    return True


def _reach(run: _Run) -> bool:
    """Can a socket be opened at all - which a firewall answers before login."""
    started = time.monotonic()
    try:
        with socket.create_connection((run.host, run.port), timeout=run.config.connect_timeout):
            pass
    except OSError as exc:
        run.add(
            "connect",
            "bad",
            f"{run.port}/tcp does not answer: {exc}",
            "a site firewall usually shows up here; 1094 is XRootD and 1095 is XRootD over TLS",
        )
        return False
    elapsed = (time.monotonic() - started) * 1e3
    run.add("connect", "ok", f"{run.port}/tcp answered in {elapsed:.0f} ms")
    return True


def _server(run: _Run) -> FileSystem | None:
    """Log in and ask what is there, without prompting anybody for anything."""
    from .client import FileSystem

    config = replace(run.config, prompt=False)
    try:
        filesystem = FileSystem(f"{run.scheme}://{run.host}:{run.port}", config=config)
    except (XRootDError, OSError, ValueError) as exc:
        run.add("server", "bad", f"{type(exc).__name__}: {exc}")
        return None
    try:
        filesystem.ping()
    except (XRootDError, OSError) as exc:
        filesystem.close()
        run.add(
            "server",
            "bad",
            f"logged in but would not answer: {exc}",
            "the endpoint is up; this is usually authentication or a busy manager",
        )
        return None
    try:
        info = filesystem.protocol()
        kind = "a manager" if info.is_manager else "a data server"
        detail = f"protocol {info.version_str}, {kind}"
        if info.has_tls:
            detail += ", TLS offered"
    except (XRootDError, OSError, AssertionError):  # HTTP has no such question
        detail = "answered"
    run.add("server", "ok", detail)
    return filesystem


def _path(run: _Run, filesystem: FileSystem) -> None:
    """Stat what was asked for, and if it is absent say how far down it exists."""
    if not run.path or run.path == "/":
        run.add("path", "skip", "no path in the URL to look for")
        return
    try:
        info = filesystem.stat(run.path)
    except (XRootDError, OSError) as exc:
        run.add("path", "bad", f"{run.path}: {exc}", _nearest(run, filesystem))
        return
    what = "a directory" if info.is_dir() else f"{info.size} bytes"
    run.add("path", "ok", f"{run.path}: {what}")


def _nearest(run: _Run, filesystem: FileSystem) -> str:
    """How much of the path does exist, which is the part worth being told."""
    parts = [part for part in run.path.split("/") if part]
    for depth in range(len(parts) - 1, 0, -1):
        parent = "/" + "/".join(parts[:depth])
        try:
            filesystem.stat(parent)
        except (XRootDError, OSError):
            continue
        return f"{parent} exists; check the spelling below it"
    return "not even the top of the path is there; is this the right endpoint?"


# ---------------------------------------------------------------------------


def _endpoint(run: _Run) -> None:
    """Every check that needs the network, each one giving up for the rest.

    They are in this order because each makes the next one meaningful: there
    is no point saying a path is missing when the port never answered.
    """
    if not (_url(run) and _dns(run) and _reach(run)):
        return
    filesystem = _server(run)
    if filesystem is None:
        return
    try:
        _path(run, filesystem)
    finally:
        filesystem.close()


def diagnose(url: str = "", *, config: Config | None = None) -> Report:
    """Ask everything a first transfer would ask, and report all of it.

        >>> report = xrd.diagnose("root://eos.example.org//store/f.root")  # doctest: +SKIP
        >>> if not report.ok:
        ...     print(report)

    Without a URL it checks the machine alone: the interpreter, the optional
    packages, the settings in force and what there is to authenticate with.
    With one it goes on to name resolution, the port, the login and the path,
    stopping at the first thing that makes the rest meaningless.

    It never raises for a failure - a failure is a line in the report - and it
    never prompts, so it is safe in a script and safe to paste into a ticket.
    """
    run = _Run(config or current(), url=url)
    _python(run)
    _extras(run)
    _settings(run)
    _credentials(run)
    if url:
        _endpoint(run)
    return Report(tuple(run.checks), url)
