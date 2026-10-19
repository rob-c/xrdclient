"""``xrd-proxy``: make, inspect and remove X.509 proxies, ``voms-proxy-*`` style.

Three subcommands, each spelled like the VOMS command it stands in for so
that muscle memory and existing scripts carry over:

``xrd-proxy init``  (``voms-proxy-init``)
    Load the user certificate and key, fetch a VOMS attribute certificate for
    each ``-voms VO[:FQAN]``, make a proxy with them and write it ``0600`` to
    ``-out`` or the default location (``$X509_USER_PROXY``, else
    ``/tmp/x509up_u<uid>``).
``xrd-proxy info``  (``voms-proxy-info``)
    Describe a proxy file; ``-exists``, ``-acexists`` and ``-timeleft`` answer
    a script through the exit code or one number.
``xrd-proxy destroy``  (``voms-proxy-destroy``)
    Remove the proxy file.

Options take one dash (``-voms``, ``-valid 24:00``) as the VOMS tools do;
the two-dash spellings work too. ``--json`` and ``--xml`` give the shared
``storage-client-report`` document (:mod:`xrdclient.report`) that ``xrd-fs`` writes.

The work is :mod:`xrdsec.proxytool`'s - the user credential search under
``voms-proxy-init``'s conventions, the VOMS requests, the proxy, where its
file goes and how it is read back - shared with ``xrdarc proxy``
(``arcproxy``); this module is the ``voms-proxy-*`` spelling of the options
and the output. As for the other tools, nothing heavy is imported before the
arguments are parsed, so ``--help`` stays instant.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from .. import report as output
from ..errors import XRootDError
from . import ERROR, OK, fail, version_flag

if TYPE_CHECKING:
    from xrdsec.proxies import ProxyInfo
    from xrdsec.proxytool import ProxyRequest, VOMSRequest
    from xrdsec.voms_client import VOMSServer

PROGRAM = "xrd-proxy"


def main(argv: Sequence[str] | None = None) -> int:
    """The ``xrd-proxy`` entry point; returns the exit code (``0``, ``1``, or ``2`` for usage)."""
    return output.run_cli(PROGRAM, argv, lambda: _main(argv))


def _main(argv: Sequence[str] | None) -> int:
    """Parse, run the subcommand, and turn a credential failure into a one-line message."""
    args = _parser().parse_args(argv)
    report = output.current()
    if report is not None:
        report.command = args.command
    output.identify(args.command)
    try:
        return int(args.handler(args))
    except (XRootDError, OSError, ValueError) as exc:
        return fail(PROGRAM, exc)


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    """The ``xrd-proxy`` parser and its three subcommands.

    The report flags are added at both levels, as for ``xrd-fs``, so
    ``xrd-proxy --json info`` and ``xrd-proxy info --json`` both work.
    """
    # No abbreviations: with one-dash long options, argparse would otherwise
    # read ``-vo`` as an abbreviation of ``-voms`` (or ``-valid``).
    parser = output.Parser(
        prog=PROGRAM, description="Make, inspect and remove X.509 proxies.", allow_abbrev=False
    )
    version_flag(parser)
    output.flags(parser)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=output.Parser)
    for name, build, help_text in (
        ("init", _init_parser, "make a proxy (voms-proxy-init)"),
        ("info", _info_parser, "describe a proxy (voms-proxy-info)"),
        ("destroy", _destroy_parser, "remove a proxy (voms-proxy-destroy)"),
    ):
        build(commands.add_parser(name, help=help_text, allow_abbrev=False))
    return parser


def _option(parser: argparse.ArgumentParser, name: str, **kwargs: Any) -> None:
    """Add ``-name`` with its ``--name`` twin: the VOMS spelling and the GNU one."""
    parser.add_argument(f"-{name}", f"--{name}", **kwargs)


def _common(parser: argparse.ArgumentParser) -> None:
    """Options every subcommand takes: report format, quiet, the proxy file."""
    output.flags(parser)
    _option(parser, "quiet", action="store_true", help="print nothing but errors")
    parser.add_argument("-q", dest="quiet", action="store_true", help=argparse.SUPPRESS)


def _init_parser(parser: argparse.ArgumentParser) -> None:
    """``init``'s options: ``voms-proxy-init``'s names for the ones it shares."""
    _common(parser)
    _option(parser, "voms", action="append", default=[], metavar="VO[:FQAN]", help="VO to ask")
    _option(parser, "vomses", action="append", default=[], metavar="PATH", help="vomses file")
    _option(parser, "valid", type=_duration, metavar="HH:MM", help="proxy lifetime (12:00)")
    _option(parser, "hours", type=float, metavar="H", help="proxy lifetime in hours")
    _option(parser, "vomslife", type=_duration, metavar="HH:MM", help="AC lifetime")
    _option(parser, "bits", type=int, default=2048, metavar="N", help="key size (2048)")
    _option(parser, "limited", action="store_true", help="make a limited proxy")
    _option(parser, "path_length", type=int, metavar="N", help="limit further delegation")
    _option(parser, "out", metavar="PATH", help="where to write the proxy")
    _option(parser, "cert", metavar="PATH", help="user certificate")
    _option(parser, "key", metavar="PATH", help="user private key")
    _option(parser, "pwstdin", action="store_true", help="read the pass phrase from stdin")
    _option(parser, "certdir", metavar="DIR", help="CA directory for the VOMS servers")
    _option(parser, "timeout", type=float, default=30.0, metavar="S", help="per-server timeout")
    styles = parser.add_mutually_exclusive_group()
    styles.add_argument("-rfc", "--rfc", dest="profile", action="store_const", const="rfc3820")
    styles.add_argument(
        "-old", "--old", dest="profile", action="store_const", const="globus-legacy"
    )
    styles.add_argument(
        "--profile", choices=("rfc3820", "arc", "xrootd", "globus-legacy"), help="proxy profile"
    )
    parser.set_defaults(handler=_init, profile="rfc3820")


def _info_parser(parser: argparse.ArgumentParser) -> None:
    """``info``'s options: ``voms-proxy-info``'s field selectors and tests."""
    _common(parser)
    _option(parser, "file", metavar="PATH", help="proxy file to describe")
    _option(parser, "all", action="store_true", help="also show the VOMS attributes")
    for name in _FIELDS:
        _option(
            parser, name, dest="fields", action="append_const", const=name, help=f"print the {name}"
        )
    _option(parser, "exists", action="store_true", help="exit 0 only if a valid proxy exists")
    _option(parser, "valid", type=_duration, metavar="HH:MM", help="with -exists: time left")
    _option(parser, "hours", type=float, metavar="H", help="with -exists: hours left")
    _option(parser, "acexists", metavar="VO", help="exit 0 only if it has an AC for VO")
    parser.set_defaults(handler=_info, fields=None)


def _destroy_parser(parser: argparse.ArgumentParser) -> None:
    """``destroy``'s options."""
    _common(parser)
    _option(parser, "file", metavar="PATH", help="proxy file to remove")
    _option(parser, "dry", action="store_true", help="say what would be removed")
    parser.set_defaults(handler=_destroy)


def _duration(text: str) -> float:
    """``HH:MM`` (``voms-proxy-init -valid``) as seconds; ``argparse`` type."""
    hours, sep, minutes = text.partition(":")
    try:
        whole = int(hours) * 3600 + (int(minutes) * 60 if sep else 0)
        valid = int(hours) >= 0 and (not sep or 0 <= int(minutes) < 60)
    except ValueError:
        valid = False
    if not valid or whole <= 0:
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (use HH:MM)")
    return float(whole)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def _init(args: argparse.Namespace) -> int:
    """``xrd-proxy init``: :func:`xrdsec.proxytool.make_proxy`, written - then say until when."""
    from xrdsec import proxytool
    from xrdsec.proxies import load_credential, write_proxy

    cert, key = proxytool.find_user_pair(proxytool.VOMS_PROXY_INIT, cert=args.cert, key=args.key)
    signer = load_credential(cert, key, password=_pass_phrase(args, key))
    request = _request(args)
    proxy = proxytool.make_proxy(signer, request, _Said(args.quiet))
    target = proxytool.proxy_target(args.out)
    written = write_proxy(target.path, proxy, follow_symlinks=target.follow_symlinks)
    expires = proxy.chain[0].not_after
    output.record("result", path=written, subject=str(proxy.subject), not_after=expires)
    if not args.quiet:
        _say(f"Created proxy in {written}.\n\nYour proxy is valid until {_when(expires)}\n")
        if expires < time.time() + request.lifetime - 60:
            _say("Warning: the proxy is shorter than asked for: the credential expires sooner\n")
    return OK


def _request(args: argparse.Namespace) -> ProxyRequest:
    """``init``'s options as one :class:`xrdsec.proxytool.ProxyRequest`.

    ``-valid HH:MM`` wins over ``-hours``; neither is twelve hours, as for
    ``voms-proxy-init``. Several ``-voms`` for one VO are one request.
    """
    from xrdsec import proxytool

    lifetime = args.valid or (args.hours * 3600 if args.hours else 12 * 3600.0)
    return proxytool.ProxyRequest(
        lifetime=lifetime,
        bits=args.bits,
        limited=args.limited,
        path_length=args.path_length,
        profile=args.profile,
        voms=proxytool.voms_requests(args.voms, proxytool.VOMS_PROXY_INIT),
        voms_lifetime=args.vomslife,
        vomses=tuple(args.vomses),
        timeout=args.timeout,
        ca_path=args.certdir,
    )


def _pass_phrase(args: argparse.Namespace, key: str) -> Callable[[], bytes | str | None]:
    """What to call if the key is encrypted: stdin for ``-pwstdin``, else the prompter."""
    if args.pwstdin:
        return lambda: sys.stdin.readline().rstrip("\r\n")

    def ask() -> bytes:
        """The library's own prompter (:func:`xrdclient._usercred.pass_phrase`)."""
        from .._usercred import pass_phrase
        from ..config import Config

        return pass_phrase(Config(), key)

    return ask


class _Said:
    """``voms-proxy-init``'s lines around each VOMS request: a :class:`xrdsec.proxytool.Progress`.

    The contact lines are left out under ``-q``; warnings are not, since a
    warning is about the proxy just made (an AC this machine cannot verify).
    """

    def __init__(self, quiet: bool) -> None:
        """``quiet`` is ``-q``."""
        self.quiet = quiet

    def contacting(self, request: VOMSRequest, servers: Sequence[VOMSServer]) -> None:
        """``Contacting host:port [DN] "vo"...``, naming the first server tried."""
        if not self.quiet:
            first = servers[0]
            _say(f'Contacting {first.address} [{first.dn}] "{first.vo}"...\n')

    def contacted(self, request: VOMSRequest) -> None:
        """The success line ``voms-proxy-init`` prints after each AC."""
        if not self.quiet:
            _say("Remote VOMS server contacted successfully.\n\n")

    def warned(self, message: str) -> None:
        """``Warning: ...`` on stderr, and a ``warning`` record under ``--json``."""
        output.record("warning", message=message)
        print(f"Warning: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------

#: The single-field selectors, as ``voms-proxy-info`` names them.
_FIELDS = (
    "subject",
    "issuer",
    "identity",
    "type",
    "strength",
    "timeleft",
    "path",
    "vo",
    "fqan",
    "actimeleft",
)


def _info(args: argparse.Namespace) -> int:
    """``xrd-proxy info``: describe, or answer ``-exists``/``-acexists`` by exit code."""
    from xrdsec import proxytool

    target = proxytool.proxy_target(args.file)
    testing = args.exists or bool(args.acexists)
    try:
        info = proxytool.read_proxy_info(target.path, follow_symlinks=target.follow_symlinks)
    except proxytool.NoCredentialError as exc:
        # A test answers by exit code alone; a description says why there is none.
        return ERROR if testing else fail(PROGRAM, exc)
    if testing:
        return _tests(args, info)
    output.record("result", **info.as_dict())
    lines = _field_lines(info, args.fields) if args.fields else _summary(info, args.all)
    if not args.quiet:
        _say("".join(f"{line}\n" for line in lines))
    return OK


def _tests(args: argparse.Namespace, info: ProxyInfo) -> int:
    """``-exists [-valid HH:MM]`` and ``-acexists VO``: ``0`` when every test asked passes."""
    wanted = args.valid or (args.hours * 3600 if args.hours else 0.0)
    passed = info.time_left > wanted
    if args.acexists:
        passed = passed and any(
            entry.vo == args.acexists and entry.remaining() > 0 for entry in info.voms
        )
    output.record("result", path=info.path, exists=passed, time_left=info.time_left)
    return OK if passed else ERROR


def _summary(info: ProxyInfo, every: bool) -> list[str]:
    """``voms-proxy-info``'s block, plus one section per AC with ``-all``."""
    lines = [
        f"subject   : {info.subject}",
        f"issuer    : {info.issuer}",
        f"identity  : {info.identity}",
        f"type      : {info.kind}",
        f"strength  : {info.strength}",
        f"path      : {info.path}",
        f"timeleft  : {_clock(info.time_left)}",
    ]
    if info.key_usage:
        lines.append(f"key usage : {', '.join(info.key_usage)}")
    for entry in info.voms if every else ():
        lines += [f"=== VO {entry.vo} extension information ===", f"VO        : {entry.vo}"]
        lines += [f"subject   : {entry.holder}", f"issuer    : {entry.issuer}"]
        lines += [f"attribute : {fqan}" for fqan in entry.fqans]
        lines += [f"timeleft  : {_clock(entry.remaining())}", f"uri       : {entry.uri}"]
    return lines


def _field_lines(info: ProxyInfo, fields: Sequence[str]) -> list[str]:
    """The values of the selected fields, one per line, in the order asked."""
    values: dict[str, Callable[[], list[str]]] = {
        "subject": lambda: [info.subject],
        "issuer": lambda: [info.issuer],
        "identity": lambda: [info.identity],
        "type": lambda: [info.kind],
        "strength": lambda: [str(info.strength)],
        "timeleft": lambda: [str(int(info.time_left))],
        "path": lambda: [info.path],
        "vo": lambda: [entry.vo for entry in info.voms],
        "fqan": lambda: [fqan for entry in info.voms for fqan in entry.fqans],
        "actimeleft": lambda: [str(max(0, int(entry.remaining()))) for entry in info.voms],
    }
    return [line for name in fields for line in values[name]()]


# ---------------------------------------------------------------------------
# destroy
# ---------------------------------------------------------------------------


def _destroy(args: argparse.Namespace) -> int:
    """``xrd-proxy destroy``: unlink the proxy file, if it is there and it is ours.

    A symbolic link is removed, never the file it points to; somebody else's
    file is left alone (in ``/tmp`` the sticky bit would refuse anyway, but a
    clear message beats ``EPERM``).
    """
    from xrdsec.proxytool import proxy_target

    path = proxy_target(args.file).path
    try:
        owner = os.lstat(path).st_uid
    except FileNotFoundError:
        return fail(PROGRAM, FileNotFoundError(f"there is no proxy at {path}"))
    if owner != os.geteuid():
        return fail(PROGRAM, PermissionError(f"{path} belongs to uid {owner}; not removed"))
    if not args.dry:
        os.unlink(path)
    output.record("result", path=path, removed=not args.dry)
    if not args.quiet:
        _say(f"{'Would remove' if args.dry else 'Removed'} {path}\n")
    return OK


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


def _say(text: str) -> None:
    """Human output on stdout, or a ``message`` record under ``--json``."""
    output.message(text)


def _clock(seconds: float) -> str:
    """``H:MM:SS``, as the VOMS tools print time left (hours may exceed 24)."""
    whole = max(0, int(seconds))
    return f"{whole // 3600}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


def _when(moment: float) -> str:
    """A local date as ``voms-proxy-init`` prints it: ``Sat Oct 18 23:59:59 CEST 2026``."""
    return time.strftime("%a %b %d %H:%M:%S %Z %Y", time.localtime(moment))


if __name__ == "__main__":
    raise SystemExit(main())
