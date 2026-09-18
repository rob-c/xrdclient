"""The command-line tools, and the pieces both of them share.

``xrd-cp`` and ``xrd-fs`` are thin wrappers: everything they do is a call into
the library, so anything the CLI can do is something a program can do in one
line. Exit codes are the usual three - ``0`` success, ``1`` a runtime failure,
``2`` a usage error - and every command takes ``--json`` so a shell script can
consume the output without parsing columns.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from typing import IO, Any, cast

from ..config import Config
from ..url import XRootDURL, parse

__all__ = [
    "OK",
    "ERROR",
    "USAGE",
    "Endpoints",
    "dumps",
    "fail",
    "confirm",
    "interactive",
    "size_arg",
    "common_flags",
    "stdout_bytes",
]

OK, ERROR, USAGE = 0, 1, 2


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _plain(obj: Any) -> Any:
    """Make the library's own types serialisable without teaching them JSON."""
    if isinstance(obj, XRootDURL):
        return str(obj)  # before the dataclass branch: a URL is one, and reads badly as one
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    raise TypeError(f"cannot serialise {type(obj).__name__}")


def dumps(payload: object) -> str:
    """``--json`` output: stable key order, one document per invocation."""
    return json.dumps(payload, default=_plain, indent=2, sort_keys=False)


def stdout_bytes() -> IO[bytes]:
    """Standard output as a byte stream, whatever it has been replaced with.

    File contents go out unchanged - a ``cat`` that decodes and re-encodes is
    a ``cat`` that corrupts a ROOT file - and a test capturing stdout puts an
    object there that has no ``buffer``, so this asks rather than assumes.
    """
    return cast("IO[bytes]", getattr(sys.stdout, "buffer", sys.stdout))


def fail(program: str, exc: BaseException) -> int:
    """Report ``exc`` the way a Unix tool does, and give back the exit code."""
    print(f"{program}: {exc}", file=sys.stderr)
    return ERROR


def interactive() -> bool:
    """Is there a person at both ends of this, rather than a pipe?"""
    return bool(getattr(sys.stdin, "isatty", bool)() and getattr(sys.stderr, "isatty", bool)())


def confirm(question: str) -> bool:
    """Ask before doing something that cannot be undone.

    Only ever called when somebody is there to answer: a script in a batch job
    is never stopped by a question it cannot see. Anything but ``y`` is no,
    and so is a closed stdin.
    """
    print(f"{question} [y/N] ", end="", file=sys.stderr, flush=True)
    try:
        answer = input()
    except EOFError:
        answer = ""
    return answer.strip().lower() in {"y", "yes"}


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

_SUFFIXES = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}


def size_arg(text: str) -> int:
    """``argparse`` type for a byte count written the way humans write it."""
    raw = text.strip().lower().removesuffix("b")
    scale = _SUFFIXES.get(raw[-1:], 1)
    digits = raw[:-1] if scale > 1 else raw
    try:
        value = int(digits)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a size: {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("size must be positive")
    return value * scale


def common_flags(parser: argparse.ArgumentParser) -> None:
    """The options every command in both tools understands."""
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("-q", "--quiet", action="store_true", help="say nothing on success")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="log more (repeatable)")
    parser.add_argument("--token", metavar="TOKEN", help="bearer token to present")
    parser.add_argument("--user", metavar="NAME", help="username to authenticate as")
    parser.add_argument(
        "--config", metavar="FILE", help="settings file to read instead of the default"
    )
    parser.add_argument(
        "--alias", metavar="NAME", help="apply the [alias NAME] section of the settings file"
    )
    parser.add_argument(
        "--no-verify-tls", action="store_true", help="do not verify the server certificate"
    )
    asking = parser.add_mutually_exclusive_group()
    asking.add_argument(
        "--prompt",
        action="store_true",
        help="ask for missing credentials even when this is not a terminal",
    )
    asking.add_argument(
        "--no-prompt", action="store_true", help="never ask for credentials; fail instead"
    )


def configure_logging(verbosity: int) -> None:
    """``-v`` is warnings, ``-vv`` info, ``-vvv`` the wire itself."""
    import logging

    if not verbosity:
        return
    level = {1: logging.WARNING, 2: logging.INFO}.get(verbosity, logging.DEBUG)
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def config_from(args: argparse.Namespace) -> Config:
    """A :class:`~xrdclient.Config` carrying whatever the command line asked for.

    The settings file underneath is read first, so a flag always wins over a
    dotfile - which is the order somebody typing the flag expects.
    """
    configure_logging(args.verbose)
    base = Config.from_file(args.config, alias=args.alias)
    settings: dict[str, object] = {}
    if args.token:
        settings["token"] = args.token
    if args.user:
        settings["username"] = args.user
    if args.no_verify_tls:
        settings["verify_tls"] = False
    if args.prompt or args.no_prompt:
        settings["prompt"] = bool(args.prompt)
    return base.evolve(**settings)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


class Endpoints:
    """One :class:`~xrdclient.FileSystem` per endpoint, opened once and shared.

        >>> with Endpoints(config) as endpoints:
        ...     fs, path = endpoints.at("root://host//store/f.root")

    Commands take a list of URLs that often name the same server; without this
    a five-argument ``ls`` would open five connections.
    """

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self._open: dict[tuple[str, str, int], Any] = {}

    def at(self, url: str | XRootDURL) -> tuple[Any, str]:
        """The filesystem for ``url``'s endpoint, and the path within it."""
        from ..client import FileSystem

        target = parse(url)
        if target.is_local:
            raise ValueError(f"{url} is a local path, not a remote endpoint")
        key = (target.scheme, target.host, target.port)
        found = self._open.get(key)
        if found is None:
            found = self._open[key] = FileSystem(target.with_path("/"), self.config)
        return found, target.path or "/"

    def close(self) -> None:
        for filesystem in self._open.values():
            filesystem.close()
        self._open.clear()

    def __enter__(self) -> Endpoints:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
