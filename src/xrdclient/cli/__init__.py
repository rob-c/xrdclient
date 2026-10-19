"""The command-line tools, and the pieces both of them share.

``xrd-cp`` and ``xrd-fs`` are thin wrappers: everything they do is a call into
the library, so anything the CLI can do is something a program can do in one
line. Exit codes are the usual three - ``0`` success, ``1`` a runtime failure,
``2`` a usage error - and every command takes ``--json`` so a shell script can
consume the output without parsing columns.

Layout:

- this module holds what both tools share: exit codes, the common flags
  (credentials, settings file, verbosity), turning those flags into a
  :class:`~xrdclient.Config`, and :class:`Endpoints`, the per-server
  connection cache;
- :mod:`xrdclient.report` (imported here as ``output``) owns machine-readable
  output (``--json``/``--xml``/``--format``) and the ``run_cli`` wrapper that
  maps exceptions to exit codes;
- :mod:`.cp` and :mod:`.fs` are the two programs, modelled on ``xrdcp`` and
  ``xrdfs`` from XrdCl.

Start reading at ``cp.main`` or ``fs.main``. What a maintainer must not
break: the exit-code contract above (batch systems and wrapper scripts branch
on it), the rule that nothing here imports the client machinery at module
level (``--help`` and ``--version`` must answer without loading TLS, the
crypto stack or a session; ``tests/test_import_cost.py`` holds
:mod:`xrdclient.report` to that), and the
redaction of URLs and tokens on every path that can reach a terminal.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import IO, Any, cast

from xrdcore import cli as _cli
from xrdcore import log as _xrdlog

from .. import report as output
from .._version import __version__
from ..config import Config
from ..url import XRootDURL, parse

__all__ = [
    "ERROR",
    "OK",
    "USAGE",
    "Endpoints",
    "common_flags",
    "confirm",
    "dumps",
    "fail",
    "interactive",
    "size_arg",
    "stdout_bytes",
    "version_flag",
]

# The exit-code contract. ``2`` is what argparse itself exits with on a bad
# flag, so a usage mistake caught later by hand (a local path where a remote
# one is required, say) uses it too and a script sees one code for "you typed
# it wrong" whoever noticed. XrdCl's ``xrdcp`` exits with its own ``5x``
# shell codes (``XRootDStatus::GetShellCode``); this client deliberately keeps
# the conventional three and puts the detail in the message and ``--json``.
OK, ERROR, USAGE = 0, 1, 2


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


#: ``--json`` output: stable key order, one document per invocation. The
#: shared report's legacy rendering (:func:`xrdcore.report.dumps`), which
#: prints an :class:`~xrdclient.url.XRootDURL` as its string because
#: :mod:`xrdclient.report` registers the type before anything is written.
dumps = output.dumps


def stdout_bytes() -> IO[bytes]:
    """Standard output as a byte stream, whatever it has been replaced with.

    File contents go out unchanged - a ``cat`` that decodes and re-encodes is
    a ``cat`` that corrupts a ROOT file - and a test capturing stdout puts an
    object there that has no ``buffer``, so this asks rather than assumes.
    """
    return cast("IO[bytes]", getattr(sys.stdout, "buffer", sys.stdout))


#: Every failure ``fail`` is handed is a runtime failure, status 1.
_FAILED: tuple[_cli.ExitRule, ...] = ((BaseException, ERROR),)


def fail(program: str, exc: BaseException) -> int:
    """Report ``exc`` the way a Unix tool does, and give back the exit code.

    It is :class:`xrdcore.cli.Fatal`, the one-line report every tool on the
    stack shares: an ``error`` record under ``--json``, and ``program:
    message`` on stderr, redacted - an error message can quote a URL, and a
    URL can carry a token or a pre-signed signature, so the terminal gets
    what a log file would. The status is always :data:`ERROR`: these tools
    keep the conventional three codes (see the module docstring), and the
    commands catch only the library's own failures, so a bug still ends in
    a traceback rather than a sentence.
    """
    return _cli.Fatal(program, _FAILED)(exc)


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

#: Binary multipliers, as ``dd`` and ``xrdcp`` read sizes: ``64k`` is 65536
#: bytes, never 64000. Used by ``--chunk-size`` and friends.
_SUFFIXES = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}


def size_arg(text: str) -> int:
    """``argparse`` type for a byte count written the way humans write it.

    Accepts ``4096``, ``64k``, ``64K``, ``64kb`` and ``1M``; anything else, and
    zero or a negative count, is an :class:`argparse.ArgumentTypeError`, which
    argparse turns into a usage message and exit code ``2``.
    """
    # A trailing "b" is noise ("64kb" means "64k"), and a bare "b" is a unit
    # with no digits, which the int() below rejects.
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
    output.flags(parser, legacy_json=True)
    parser.add_argument("-q", "--quiet", action="store_true", help="say nothing on success")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="log more (repeatable)")
    tokens = parser.add_mutually_exclusive_group()
    tokens.add_argument(
        "--token",
        metavar="TOKEN",
        help="bearer token to present; '-' reads it from standard input "
        "(a literal token is visible to other users in the process list)",
    )
    tokens.add_argument(
        "--token-file", metavar="PATH", help="read the bearer token from this file (mode 0600)"
    )
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


def version_flag(parser: argparse.ArgumentParser) -> None:
    """Add the distribution version without importing the client machinery."""
    parser.add_argument(
        "--version", action=_Version, nargs=0, help="show program's version and exit"
    )


class _Version(argparse.Action):
    """``--version`` that also answers in ``--json``/``--xml`` when those are on.

    argparse's own ``action="version"`` prints straight to stdout, which would
    put a bare string in front of the structured document a script is parsing.
    """

    def __call__(self, parser: argparse.ArgumentParser, *args: Any, **kwargs: Any) -> None:
        """Record the version in the active report, print it, and exit ``0``."""
        output.record("version", version=__version__)
        parser._print_message(f"{parser.prog} {__version__}\n", sys.stdout)
        parser.exit()


def configure_logging(verbosity: int) -> _xrdlog.Configured | None:
    """``-v`` is warnings, ``-vv`` info, ``-vvv`` the wire itself; none leaves logging alone.

    The handler is :func:`xrdcore.log.configure`'s, on the root logger (so
    xrdsec's and xrdhttp's records show too), on stderr and redacted; the
    scale is xrdcore's shifted by one, because a bare ``-v`` here asks for
    the warnings the default already prints, with the logger names added.
    Returns what was configured, for a caller that wants to undo it.
    """
    if not verbosity:
        return None
    return _xrdlog.configure(
        _xrdlog.verbosity_level(verbosity - 1), fmt="%(levelname)s %(name)s: %(message)s"
    )


#: Whether the literal ``--token`` warning has been given in this process.
_warned_literal_token = False


def _token(value: str | None) -> str | None:
    """The token ``--token`` stands for: itself, or standard input for ``-``.

    A token typed on the command line is in ``ps`` output, ``/proc/<pid>/cmdline``
    and the shell history of everyone who can read them, for as long as the
    command runs - so it still works, but says so once. ``-`` reads it from
    standard input instead, where none of those can see it.
    """
    global _warned_literal_token
    if value is None:
        return None
    if value == "-":
        token = sys.stdin.readline().strip()
        if not token:
            print("error: --token -: standard input held no token", file=sys.stderr)
            raise SystemExit(USAGE)
        return token
    if not _warned_literal_token:
        _warned_literal_token = True
        print(
            "warning: a token given with --token is visible to other users in the "
            "process list; prefer --token-file PATH, --token - (standard input) or "
            "$BEARER_TOKEN_FILE",
            file=sys.stderr,
        )
    return value


def config_from(args: argparse.Namespace) -> Config:
    """A :class:`~xrdclient.Config` carrying whatever the command line asked for.

    The settings file underneath is read first, so a flag always wins over a
    dotfile - which is the order somebody typing the flag expects.
    """
    configure_logging(args.verbose)
    base = Config.from_file(args.config, alias=args.alias)
    settings: dict[str, object] = {}
    token = _token(args.token)
    if token:
        settings["token"] = token
    if args.token_file:
        settings["token_file"] = os.path.expanduser(args.token_file)
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
        """Start with no connections; each opens on the first :meth:`at` for it."""
        self.config = config or Config()
        # Keyed by (scheme, host, port) rather than the URL string: two paths
        # on one server share a connection, but root:// and roots:// to the
        # same host do not, since one is encrypted and the other is not.
        # ``Any`` because the FileSystem type lives behind a lazy import.
        self._open: dict[tuple[str, str, int], Any] = {}

    def at(self, url: str | XRootDURL) -> tuple[Any, str]:
        """The filesystem for ``url``'s endpoint, and the path within it."""
        from ..client import FileSystem

        target = parse(url)
        report = output.current()
        if report is not None:
            # The structured report names the endpoint it talked to, with any
            # token or pre-signed query string removed first.
            report.identity["url"] = _xrdlog.redact(str(url))
        if target.is_local:
            raise ValueError(f"{url} is a local path, not a remote endpoint")
        key = (target.scheme, target.host, target.port)
        found = self._open.get(key)
        if found is None:
            found = self._open[key] = FileSystem(target.with_path("/"), self.config)
        return found, target.path or "/"

    def close(self) -> None:
        """Close every connection opened so far; the object can be reused after."""
        for filesystem in self._open.values():
            filesystem.close()
        self._open.clear()

    def __enter__(self) -> Endpoints:
        """The endpoints themselves; the ``with`` block owns their connections."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close every connection, also when the command failed part-way."""
        self.close()
