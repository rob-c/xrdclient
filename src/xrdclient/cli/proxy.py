"""``xrd-proxy``: make, inspect and remove X.509 proxies, ``voms-proxy-*`` style.

Three subcommands, each spelled like the VOMS command it stands in for so
that muscle memory and existing scripts carry over:

``xrd-proxy init``  (``voms-proxy-init``)
    Load the user credential - a PKCS#12 bundle (``-p12``, or
    ``$X509_USER_P12``), or a certificate and key - fetch a VOMS attribute
    certificate for each ``-voms VO[:FQAN]``, make a proxy with them and
    write it ``0600`` to ``-out`` or the default location
    (``$X509_USER_PROXY``, else ``/tmp/x509up_u<uid>``).
``xrd-proxy info``  (``voms-proxy-info``)
    Describe a proxy file; ``-exists``, ``-acexists`` and ``-timeleft`` answer
    a script through the exit code or one number.
``xrd-proxy destroy``  (``voms-proxy-destroy``)
    Remove the proxy file.
``xrd-proxy doctor``  (``check``)
    Ask every question a proxy needs - trust anchors, a credential, the VOMS
    configuration, the VO's servers and LSC, the proxy file, whether one
    could be written - in the order the answers bite, and say in plain
    English what to do about each one (and who to ask, when it is not you).

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
import glob
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, NamedTuple, NoReturn

from .. import report as output
from ..errors import XRootDError
from . import ERROR, OK, fail, version_flag

if TYPE_CHECKING:
    from xrdsec.proxies import ProxyInfo
    from xrdsec.proxytool import ProxyRequest, VOMSRequest
    from xrdsec.voms import VOMSEntry
    from xrdsec.voms_client import VOMSServer
    from xrdsec.x509 import ProxyCredential

    from ..doctor import Report

PROGRAM = "xrd-proxy"

#: The names this command is installed under. ``xrd-auth`` is the same program:
#: it has grown past proxies, to tokens and to the doctor that explains both.
NAMES = ("xrd-proxy", "xrd-auth")

#: What the command is for, in one line, for argparse.
DESCRIPTION = (
    "Make, inspect and remove X.509 proxies, and acquire, inspect and remove WLCG bearer tokens."
)


def program_name() -> str:
    """The name this was invoked as, when it is one of ours, else ``PROGRAM``.

    ``xrd-auth --help`` should say ``xrd-auth``; a test run or an odd alias should
    not put its own name in the usage line.
    """
    invoked = os.path.basename(sys.argv[0] or "")
    return invoked if invoked in NAMES else PROGRAM


#: The marks a summary counts, in the order they are reported.
_MARKS = ("ok", "warn", "bad", "skip")


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
    parser = output.Parser(prog=program_name(), description=DESCRIPTION, allow_abbrev=False)
    version_flag(parser)
    output.flags(parser)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=output.Parser)
    for name, build, help_text in (
        ("init", _init_parser, "make a proxy (voms-proxy-init)"),
        ("info", _info_parser, "describe a proxy (voms-proxy-info)"),
        ("destroy", _destroy_parser, "remove a proxy (voms-proxy-destroy)"),
        ("doctor", _doctor_parser, "check everything a proxy needs, and say what is wrong"),
        ("token", _token_parser, "get, inspect and remove a WLCG bearer token"),
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
    _option(
        parser,
        "voms",
        action="append",
        default=[],
        metavar="VO[:FQAN]",
        help="VO to ask for an AC; repeat for more VOs or FQANs (-list-vos shows them)",
    )
    _option(
        parser,
        "list-vos",
        action="store_true",
        help="list the VOs with a configured VOMS server, and exit without making a proxy",
    )
    _option(parser, "vomses", action="append", default=[], metavar="PATH", help="vomses file")
    _option(parser, "valid", type=_duration, metavar="HH:MM", help="proxy lifetime (12:00)")
    _option(parser, "hours", type=float, metavar="H", help="proxy lifetime in hours")
    _option(parser, "vomslife", type=_duration, metavar="HH:MM", help="AC lifetime")
    _option(parser, "bits", type=int, default=2048, metavar="N", help="key size (2048)")
    _option(parser, "limited", action="store_true", help="make a limited proxy")
    _option(parser, "path_length", type=int, metavar="N", help="limit further delegation")
    _option(parser, "out", metavar="PATH", help="where to write the proxy")
    _option(
        parser,
        "p12",
        metavar="PATH",
        help="PKCS#12 bundle (.p12/.pfx) instead of a certificate and key",
    )
    _option(parser, "cert", metavar="PATH", help="user certificate (PEM)")
    _option(parser, "key", metavar="PATH", help="user private key (PEM)")
    # The same two under the names a PEM pair is usually described by.
    _option(parser, "pub", dest="cert", metavar="PATH", help="the certificate: -cert's other name")
    _option(parser, "priv", dest="key", metavar="PATH", help="the private key: -key's other name")
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


def _token_parser(parser: argparse.ArgumentParser) -> None:
    """``token`` and its three commands: get one, describe one, remove one."""
    _common(parser)
    commands = parser.add_subparsers(
        dest="token_command", required=True, parser_class=output.Parser
    )
    init = commands.add_parser(
        "init",
        help="ask a WLCG issuer for a token",
        description=(
            "Get a WLCG bearer token from an issuer, by the flow that fits: the "
            "device flow (a browser and a code, no certificate), X.509 client "
            "credentials (the certificate from your PKCS#12 is the client), or "
            "an RFC 8693 exchange that narrows a token you already hold."
        ),
        allow_abbrev=False,
    )
    _common(init)
    init.add_argument(
        "--provider",
        default="",
        metavar="NAME",
        help="a named issuer: lhcb, atlas, cms, alice, wlcg, cern, egi, dune, fermilab",
    )
    init.add_argument(
        "--issuer",
        default=None,
        help="the issuer's base URL; beats --provider and $WLCG_TOKEN_ISSUER",
    )
    init.add_argument(
        "--from-resource",
        default="",
        metavar="URL",
        help="find the issuer from a storage element that publishes /.well-known/wlcg-token",
    )
    init.add_argument(
        "--flow",
        choices=("device", "x509", "exchange", "dirac"),
        default="device",
        help="how to ask (dirac delegates to DIRAC's own client, which has the token exchange)",
    )
    init.add_argument(
        "--proxy",
        metavar="PATH",
        help="the proxy to trade at a DIRAC server (--flow dirac; $X509_USER_PROXY)",
    )
    init.add_argument(
        "--group", metavar="NAME", help="the DIRAC group the token is for (--flow dirac)"
    )
    init.add_argument(
        "--dirac-server",
        metavar="URL",
        help="the DIRAC server to ask (--flow dirac; DIRAC's own configuration decides by default)",
    )
    init.add_argument(
        "--client-id",
        default=None,
        help="the client registered with the issuer ($WLCG_CLIENT_ID or $WLCG_TOKEN_CLIENT_ID)",
    )
    init.add_argument(
        "--scope",
        default=None,
        help="the scope to ask for (storage.read:/ storage.modify:/ wlcg.groups)",
    )
    init.add_argument("--audience", default=None, help="the audience to ask for (the WLCG one)")
    init.add_argument(
        "--from-token", default="", metavar="TOKEN", help="the token to narrow (--flow exchange)"
    )
    init.add_argument(
        "--out", metavar="PATH", help="where to write it ($BEARER_TOKEN_FILE, else bt_u)"
    )
    init.add_argument("-p12", "--p12", metavar="PATH", help="PKCS#12 bundle for --flow x509")
    init.add_argument("-cert", "--cert", metavar="PATH", help="user certificate for --flow x509")
    init.add_argument("-key", "--key", metavar="PATH", help="user private key for --flow x509")
    init.add_argument(
        "-pub", "--pub", dest="cert", metavar="PATH", help="the certificate: -cert's other name"
    )
    init.add_argument(
        "-priv", "--priv", dest="key", metavar="PATH", help="the private key: -key's other name"
    )
    init.add_argument("-pwstdin", "--pwstdin", action="store_true", help="pass phrase on stdin")
    init.add_argument("-certdir", "--certdir", metavar="DIR", help="CA directory to trust")
    init.set_defaults(handler=_token_init)

    info = commands.add_parser(
        "info",
        help="describe a token a client would present",
        description=(
            "Read the bearer token the tools would present, and say what it claims: "
            "its issuer, audience, scope, VO groups and how long is left. --verify "
            "checks the signature against the issuer's keys and the times."
        ),
        allow_abbrev=False,
    )
    _common(info)
    info.add_argument("path", nargs="?", default="", help="a token file (default: the search)")
    info.add_argument("--verify", action="store_true", help="check the signature and times")
    info.add_argument(
        "--provider", default="", metavar="NAME", help="a named issuer to check against"
    )
    info.add_argument("--issuer", default=None, help="the issuer's base URL")
    info.add_argument("--from-resource", default="", metavar="URL")
    info.add_argument("--audience", default=None)
    info.add_argument("-certdir", "--certdir", metavar="DIR")
    info.set_defaults(handler=_token_info)

    destroy = commands.add_parser("destroy", help="remove a token file", allow_abbrev=False)
    _common(destroy)
    destroy.add_argument("--out", metavar="PATH", help="the token file (default: the search)")
    destroy.add_argument(
        "--dry", action="store_true", help="say what would be removed, and remove nothing"
    )
    destroy.set_defaults(handler=_token_destroy)

    listing = commands.add_parser(
        "providers",
        help="the named issuers this knows, and where each one is",
        description=(
            "The token issuers a WLCG person actually meets, each probed before it "
            "was added: they answered their discovery document with a token endpoint, "
            "a device authorization endpoint and the client-credentials, device-code "
            "and token-exchange grants. --provider NAME selects one; anything in the "
            "environment (WLCG_TOKEN_ISSUER, WLCG_TOKEN_AUDIENCE, WLCG_TOKEN_SCOPE, "
            "WLCG_CLIENT_ID) overrides it."
        ),
        allow_abbrev=False,
    )
    _common(listing)
    listing.set_defaults(handler=_token_providers)


def _info_parser(parser: argparse.ArgumentParser) -> None:
    """``info``'s options: ``voms-proxy-info``'s field selectors and tests."""
    _common(parser)
    _option(parser, "file", metavar="PATH", help="proxy file to describe")
    _option(
        parser,
        "short",
        action="store_true",
        help="the compact voms-proxy-info block: no chain, no AC sections",
    )
    parser.add_argument("-brief", dest="short", action="store_true", help=argparse.SUPPRESS)
    _option(
        parser,
        "all",
        action="store_true",
        help="accepted for voms-proxy-info compatibility; everything is shown by default",
    )
    for name in _FIELDS:
        _option(
            parser, name, dest="fields", action="append_const", const=name, help=f"print the {name}"
        )
    _option(
        parser,
        "verify",
        action="store_true",
        help="check the proxy's signatures and its VOMS AC against the trust files",
    )
    _option(
        parser, "certdir", metavar="DIR", help="with -verify: the CA directory to check against"
    )
    _option(parser, "exists", action="store_true", help="exit 0 only if a valid proxy exists")
    _option(parser, "valid", type=_duration, metavar="HH:MM", help="with -exists: time left")
    _option(parser, "hours", type=float, metavar="H", help="with -exists: hours left")
    _option(parser, "acexists", metavar="VO", help="exit 0 only if it has an AC for VO")
    parser.set_defaults(handler=_info, fields=None)


def _doctor_parser(parser: argparse.ArgumentParser) -> None:
    """``doctor``'s options: the VO to look at, the proxy file, and whether to phone out."""
    _common(parser)
    _option(
        parser,
        "voms",
        action="append",
        default=[],
        metavar="VO",
        help="the VO whose servers and LSC to check (all of them are listed regardless)",
    )
    _option(parser, "file", metavar="PATH", help="the proxy file to describe")
    _option(parser, "offline", action="store_true", help="do not contact the VOMS servers")
    _option(
        parser,
        "all-vos",
        action="store_true",
        help="ask every configured VO's servers, not just -voms's (a site admin's sweep)",
    )
    _option(parser, "strict", action="store_true", help="fail on warnings too, for monitoring")
    _option(parser, "summary", action="store_true", help="end with how many of each there were")
    parser.set_defaults(handler=_doctor)


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
    from xrdsec.proxies import write_proxy

    if args.list_vos:
        return _list_vos(args)
    configured = _check_vos(args)
    signer = _signer(args)
    request = _request(args, configured)
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


def _list_vos(args: argparse.Namespace) -> int:
    """``init -list-vos``: every VO a VOMS server is configured for here.

    One line per VO, in columns a script can ``cut``: the VO, the aliases it
    answers to (``-voms`` takes either), its servers' ``host:port``, and
    whether the VO's LSC is installed - without an LSC an AC can be asked for
    and carried, but no site can validate it, so "yes" is the VO to pick.
    """
    from xrdsec.voms_client import find_vomses

    rows = _vo_rows(find_vomses(args.vomses or None))
    output.record("result", vos=[row.as_dict() for row in rows])
    if args.quiet:
        return OK
    if not rows:
        _say(
            "no VO is configured: install a vomses file in ~/.voms/vomses or /etc/vomses,"
            " or point -vomses/$X509_VOMSES at one\n"
        )
        return OK
    vo_width = max(len(row.vo) for row in rows)
    alias_width = max(len(", ".join(row.aliases)) for row in rows)
    for row in rows:
        endpoints = ", ".join(row.endpoints)
        _say(
            f"{row.vo.ljust(vo_width)}  {', '.join(row.aliases).ljust(alias_width)}  "
            f"{endpoints.ljust(48)}  LSC: {row.lsc_state}\n"
        )
    return OK


class _VO(NamedTuple):
    """One VO as ``init -list-vos`` shows it."""

    vo: str
    aliases: tuple[str, ...]
    endpoints: tuple[str, ...]
    #: Where the VO's LSC is, or ``""`` when ``vomsdir`` has no entry for it.
    lsc: str
    #: The ``vomsdir`` searched, or ``""`` when none is configured at all.
    voms_dir: str

    @property
    def lsc_state(self) -> str:
        """``yes``/``no``, or ``?`` when there is no ``vomsdir`` to look in."""
        if not self.voms_dir:
            return "? (no X509_VOMS_DIR)"
        return "yes" if self.lsc else "no"

    def as_dict(self) -> dict[str, Any]:
        """Plain JSON-ready values."""
        return {
            "vo": self.vo,
            "aliases": list(self.aliases),
            "endpoints": list(self.endpoints),
            "lsc": self.lsc or None,
            "voms_dir": self.voms_dir or None,
        }


def _server_lsc(directory: str, vo: str, members: Sequence[Any]) -> str:
    """The first LSC in ``vomsdir/<vo>`` that one of ``members`` is validated against, or ``""``."""
    from xrdsec.voms_client import lsc_names

    folder = os.path.join(directory, vo) if directory else ""
    if not folder or not os.path.isdir(folder):
        return ""
    present = set(os.listdir(folder))
    for name in lsc_names(members):
        if name in present:
            return os.path.join(folder, name)
    return ""


def _vo_rows(servers: Sequence[Any]) -> list[_VO]:
    """``servers`` grouped by VO and sorted by name, which is how one picks one."""
    from xrdsec.credentials import grid_voms_dir

    groups: dict[str, list[Any]] = {}
    for server in servers:
        groups.setdefault(server.vo, []).append(server)
    directory = grid_voms_dir() or ""
    rows = [_vo_row(vo, members, directory) for vo, members in groups.items()]
    return sorted(rows, key=lambda row: row.vo)


def _vo_row(vo: str, members: Sequence[Any], directory: str) -> _VO:
    """One VO's row: its aliases, its servers' addresses, and the LSC it has, if any.

    The LSC is looked up *by server* - ``vomsdir/<vo>/<host>.lsc`` - because the
    directory existing says nothing: a ``vomses`` entry that names a server the
    VO has left finds an empty answer here, which is what makes ``LSC: no``
    mean something.
    """

    lsc = _server_lsc(directory, vo, members)
    return _VO(
        vo=vo,
        aliases=tuple(dict.fromkeys(server.alias for server in members)),
        endpoints=tuple(dict.fromkeys(f"{server.host}:{server.port}" for server in members)),
        lsc=lsc,  # already the file that exists, or ""
        voms_dir=directory,
    )


def _check_vos(args: argparse.Namespace) -> tuple[Any, ...] | None:
    """Refuse a VO that is not configured before a key is read or a pass phrase asked.

    The same lookup :func:`xrdsec.voms_client.fetch_ac` makes later, done first
    so that ``xrd-proxy init -voms atls`` says which VOs exist even when there
    is no certificate on the machine yet - discovery would otherwise stop at
    "no user certificate found" and never mention the typo.
    """
    from xrdsec import proxytool
    from xrdsec.voms_client import find_vomses, servers_for

    if not args.voms:
        return None
    configured: list[Any] = []
    for request in proxytool.voms_requests(args.voms, proxytool.VOMS_PROXY_INIT):
        for server in find_vomses(args.vomses or None, vo=request.vo):
            if server not in configured:
                configured.append(server)
        servers_for(request.vo, configured)
    return tuple(configured)


def _signer(args: argparse.Namespace) -> ProxyCredential:
    """The credential to sign the proxy with: one PKCS#12 bundle, or a certificate and key.

    In this order, the first that is there:

    1. ``-p12`` with a bundle; else ``-cert``/``-key`` (``-pub``/``-priv``) with
       a pair, which win over ``$X509_USER_P12`` because the command line says
       which files to use;
    2. ``$X509_USER_P12``, when the file it names exists;
    3. ``$X509_USER_CERT``/``$X509_USER_KEY``, then ``~/.globus``'s
       ``usercert.pem``/``userkey.pem`` - :class:`xrdsec.proxytool.VOMS_PROXY_INIT`'s
       own search;
    4. any other single ``*.pem`` pair in ``~/.globus`` (:func:`_globus_pem_pair`),
       because a certificate downloaded under its own name is still a pair.
    """
    from xrdsec.proxies import load_credential, load_p12

    bundle, stale = _named_bundle(args)
    if bundle:
        try:
            return load_p12(bundle, password=_pass_phrase(args, bundle))
        except FileNotFoundError as missing:
            raise _no_bundle(bundle) from missing
    cert, key = _pem_pair(args, stale)
    return load_credential(cert, key, password=_pass_phrase(args, key))


def _named_bundle(args: argparse.Namespace) -> tuple[str, str]:
    """The PKCS#12 bundle to use, and the one a variable named which is not there.

    ``-p12`` always wins; a ``-cert``/``-key`` on the command line beats the
    variable. ``$X509_USER_P12`` that names a file which is not there is a
    stale setting rather than a refusal: it is reported, and the search for a
    certificate and key carries on - returned as the second element so an
    empty search can say what was skipped.
    """
    if args.p12:
        return str(args.p12), ""
    if args.cert or args.key:
        return "", ""
    named = os.environ.get("X509_USER_P12") or ""
    if not named or os.path.isfile(named):
        return named, ""
    if not args.quiet:
        _say(f"note: X509_USER_P12 names {named}, which is not there; looking on\n")
    return "", named


#: What a bundle is called in the wild; CERN hands out ``.p12``, Windows ``.pfx``.
_BUNDLE_PATTERNS = ("*.p12", "*.pfx", "*.P12", "*.PFX")


def _walk_up(folder: str, levels: int = 3) -> list[str]:
    """``folder`` and its parents, ``levels`` of them, outermost last."""
    walked = [folder]
    for _ in range(levels):
        parent = os.path.dirname(walked[-1])
        if parent == walked[-1]:
            break
        walked.append(parent)
    return walked


def _nearby_bundles(path: str) -> list[str]:
    """PKCS#12 bundles worth naming when ``path`` is not there: beside it, home, ``~/.globus``.

    A stale path has almost always drifted from a bundle one directory away, so
    the search is the one that would have found it: the directory named, then
    the home directory, then ``~/.globus``.
    """
    named = os.path.dirname(os.path.abspath(path))
    folders = [*_walk_up(named), os.path.expanduser("~"), os.path.expanduser("~/.globus")]
    found: dict[str, None] = {}
    for folder in folders:
        for pattern in _BUNDLE_PATTERNS:
            for name in sorted(glob.glob(os.path.join(folder, pattern))):
                found.setdefault(name, None)
    return list(found)[:6]


def _no_bundle(path: str) -> Exception:
    """What to say when ``-p12`` names a file that is not there, and which bundles are."""
    from xrdcore.errors import CredentialError

    found = _nearby_bundles(path)
    if not found:
        return CredentialError(f"no PKCS#12 bundle at {path}")
    names = ", ".join(found)
    return CredentialError(
        f"no PKCS#12 bundle at {path}; found {names} - name one with -p12,"
        " or point X509_USER_P12 at it"
    )


def _pem_pair(args: argparse.Namespace, stale: str) -> tuple[str, str]:
    """A certificate and key: the documented search's, else a ``*.pem`` pair in ``~/.globus``."""
    from xrdsec import proxytool
    from xrdsec.credentials import globus_pem_pair

    try:
        return proxytool.find_user_pair(proxytool.VOMS_PROXY_INIT, cert=args.cert, key=args.key)
    except proxytool.NoCredentialError as missing:
        pair, note = globus_pem_pair()
        if pair is None:
            _no_credential(missing, stale, note)
        return pair


def _no_credential(missing: Exception, stale: str, note: str) -> NoReturn:
    """What to say when nothing was found: the search's message, and what it could not use."""
    from xrdsec import proxytool

    message = str(missing)
    if note:
        message = f"{message}; {note}"
    if stale:
        message = f"{message} (X509_USER_P12 names {stale}, which is not there)"
    raise proxytool.NoCredentialError(message)


def _request(args: argparse.Namespace, configured: Sequence[Any] | None = None) -> ProxyRequest:
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
        configured=configured,
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
    info, verdict, passed = _judged(args, info, target.path)
    output.record("result", **info.as_dict())
    lines = _field_lines(info, args.fields) if args.fields else _summary(info, args.short)
    lines += verdict
    if not args.quiet:
        _say("".join(f"{line}\n" for line in lines))
    return OK if passed else ERROR


def _judged(
    args: argparse.Namespace, info: ProxyInfo, path: str
) -> tuple[ProxyInfo, list[str], bool]:
    """The description as it stands, or with ``-verify`` the validator's version of it."""
    if not args.verify:
        return info, [], True
    return _verified(args, info, path)


def _verified(
    args: argparse.Namespace, info: ProxyInfo, path: str
) -> tuple[ProxyInfo, list[str], bool]:
    """The proxy as a validator judges it: every signature, and every AC against its VO's rules.

    Describing a proxy (:func:`xrdsec.proxytool.read_proxy_info`) decodes the
    ACs and says so; this is the other half - ``validate_voms`` checks each AC's
    signature, its holder, its issuer against the CA directory and its VO's LSC
    and signing policy, and the chain is checked link by link with
    :func:`xrdsec.x509.signed_by`. The ACs it returns replace the decoded ones,
    so the ``status`` line in the report is the verdict rather than a disclaimer.
    """
    import dataclasses

    from xrdsec import credentials
    from xrdsec.proxies import load_credential
    from xrdsec.voms import validate_voms

    ca_dir = args.certdir or credentials.grid_ca_path() or ""
    chain = load_credential(path).chain
    result = validate_voms(chain, ca_path=ca_dir or None)
    lines, passed = _signature_lines(chain, ca_dir)
    for entry in result.entries:
        if entry.verified:
            lines.append(
                f"vo        : verified  {entry.vo} (LSC and signing policy in {_lsc_place()})"
            )
        else:
            # the reason is already a ``diagnostic:`` line in the entry's own section
            lines.append(f"vo        : !! {entry.vo}: {entry.status.value}")
            passed = False
    return dataclasses.replace(info, voms=result.entries), lines, passed


def _lsc_place() -> str:
    """Where the LSC that a verified AC was judged against lives, for the line that says so."""
    from xrdsec import credentials

    return credentials.grid_voms_dir() or "the default vomsdir"


def _signature_lines(chain: Sequence[Any], ca_dir: str) -> tuple[list[str], bool]:
    """Each link signed by the one above it, and the top one issued by a CA in ``ca_dir``."""
    links, signed = _pair_lines(chain)
    issuer, trusted = _issuer_line(chain[-1], ca_dir)
    return [*links, *issuer], signed and trusted


def _pair_lines(chain: Sequence[Any]) -> tuple[list[str], bool]:
    """One line per link that its issuer did not sign, or one saying they all were."""
    from xrdsec.x509 import certificate_data, signed_by

    data = [certificate_data(certificate.der) for certificate in chain]
    broken = [
        f"signature : !! {chain[index].subject} is not signed by {chain[index + 1].subject}"
        for index in range(len(data) - 1)
        if not signed_by(
            data[index].tbs, data[index].signature_oid, data[index].signature, data[index + 1].spki
        )
    ]
    if broken:
        return broken, False
    if len(data) > 1:
        return [f"signature : ok  signed by {chain[1].subject}"], True
    return ["signature : --  a single certificate, with nothing above it to check"], True


def _issuer_line(top: Any, ca_dir: str) -> tuple[list[str], bool]:
    """Whether the outermost certificate's issuer is one of the CAs in the directory."""
    from xrdsec.x509 import issuer_hashes

    if not ca_dir:
        return ["issuer    : --  no CA directory to check the chain against"], True
    if any(os.path.exists(os.path.join(ca_dir, f"{name}.0")) for name in issuer_hashes(top)):
        return [f"issuer    : ok  {top.issuer} is in {ca_dir}"], True
    return [f"issuer    : !! {top.issuer} is not in {ca_dir}"], False


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


def _summary(info: ProxyInfo, short: bool) -> list[str]:
    """Everything known about the credential, or with ``-short`` the VOMS block.

    The default is the whole picture: the leaf's own fields, the chain link by
    link (subject, issuer, serial, validity, what each link is), and one
    section per VOMS AC with its attributes, times, carrier and the status of
    its decoding. ``voms-proxy-info -all``'s block is ``-short``; ``-all`` is
    still accepted because it is what the VOMS tool's scripts pass, and now
    asks for what is already the default.
    """
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
    if not short:
        lines += _detail_lines(info)
    for entry in () if short else info.voms:
        lines += _voms_lines(entry)
    return lines


def _detail_lines(info: ProxyInfo) -> list[str]:
    """The extended half: proxy limits, exact validity, and the chain link by link."""
    lines: list[str] = []
    if info.is_proxy:
        lines.append(f"proxy     : {'limited' if info.limited else 'impersonation'}")
        lines.append(f"path limit: {'unlimited' if info.path_length is None else info.path_length}")
    lines += [
        f"not before: {_when(info.not_before)}",
        f"not after : {_when(info.not_after)}",
        f"chain     : {len(info.chain)} certificate{'' if len(info.chain) == 1 else 's'}",
    ]
    for index, link in enumerate(info.chain):
        label = f"[{index}]"
        lines += [
            f"  {label} {link.subject}",
            f"  {' ' * len(label)} issuer : {link.issuer}",
            f"  {' ' * len(label)} serial : {link.serial}",
            f"  {' ' * len(label)} valid  : {_when(link.not_before)} to {_when(link.not_after)}",
            f"  {' ' * len(label)} kind   : {link.kind}",
        ]
    return lines


def _voms_lines(entry: VOMSEntry) -> list[str]:
    """One VOMS AC: what it claims, from whom, until when, and whether it decoded cleanly."""
    lines = [
        f"=== VO {entry.vo} extension information ===",
        f"VO        : {entry.vo}",
        f"subject   : {entry.holder}",
        f"issuer    : {entry.issuer}",
    ]
    lines += [f"attribute : {fqan}" for fqan in entry.fqans] or ["attribute : (none)"]
    lines += [
        f"uri       : {entry.uri}",
        f"not before: {_when(entry.not_before)}",
        f"not after : {_when(entry.not_after)}",
        f"timeleft  : {_clock(entry.remaining())}",
        f"carrier   : certificate {entry.carrier}",
        f"status    : {_status_word(entry)}",
    ]
    if entry.digest:
        lines.append(f"digest    : {entry.digest}")
    if entry.diagnostics:
        lines += [f"diagnostic: {issue.message}" for issue in entry.diagnostics]
    return lines


def _status_word(entry: VOMSEntry) -> str:
    """What describing an AC says about it: decoding, not trust (see the module docstring)."""
    from xrdsec.voms import VOMSStatus

    if entry.status is VOMSStatus.UNCHECKED:
        return "decoded (describing does not check trust; xrdsec.voms.validate_voms does)"
    if entry.status is VOMSStatus.OK:
        return "ok"
    return f"{entry.status.value}: {entry.message}"


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
# token
# ---------------------------------------------------------------------------


def _token_init(args: argparse.Namespace) -> int:
    """``xrd-proxy token init``: ask an issuer for a token, then put it where a client looks."""
    from xrdsec import token_client

    try:
        # `--flow dirac` has no issuer to resolve: DIRAC's own configuration says
        # which server to ask, and its client performs the exchange.
        token = _by_dirac(args) if args.flow == "dirac" else _asked_for(args, _chosen_issuer(args))
        path = token_client.write_token(token.access_token, args.out)
    except (token_client.TokenError, proxytool_unavailable()) as refused:
        return fail(PROGRAM, refused)
    said = token.claims
    output.record(
        "result",
        path=path,
        issuer=said.get("iss"),
        audience=said.get("aud"),
        scope=said.get("scope"),
        groups=said.get("wlcg.groups"),
        time_left=int(token.time_left()),
    )
    if not args.quiet:
        _say(f"Wrote a token to {path}.\n\n")
        _say(f"issuer    : {said.get('iss')}\n")
        _say(f"audience  : {said.get('aud')}\n")
        _say(f"scope     : {said.get('scope')}\n")
        _say(f"groups    : {said.get('wlcg.groups')}\n")
        _say(f"valid for : {_clock(token.time_left())}\n")
    return OK


def proxytool_unavailable() -> type[Exception]:
    """The error a credential search raises when there is nothing to present."""
    from xrdsec import proxytool

    return proxytool.NoCredentialError


def _asked(args: argparse.Namespace) -> Any:
    """What to ask an issuer for: a command-line value, else the environment, else a profile."""
    from xrdsec import providers

    return providers.resolve(
        getattr(args, "provider", "") or "",
        issuer=getattr(args, "issuer", None) or "",
        audience=getattr(args, "audience", None) or "",
        scope=getattr(args, "scope", None) or "",
    )


def _chosen_issuer(args: argparse.Namespace) -> Any:
    """The issuer to ask: named, profiled, or discovered from a resource that lists its issuers."""
    from xrdsec import token_client

    ca_path = args.certdir or None
    if args.from_resource:
        found = token_client.discover(args.from_resource, ca_path=ca_path)
        return found[0]
    said = _asked(args)
    if not said.issuer:
        raise token_client.TokenError(
            "no issuer: pass --provider NAME or --issuer URL (or set WLCG_TOKEN_ISSUER)",
            code="no_issuer",
        )
    return token_client.discover(said.issuer, ca_path=ca_path)[0]


def _asked_for(args: argparse.Namespace, issuer: Any) -> Any:
    """The token, by whichever flow was chosen."""
    from xrdsec import token_client

    said = _asked(args)
    scope, audience = said.scope, said.audience
    ca_path = args.certdir or None
    if args.flow == "x509":
        return token_client.acquire(
            issuer,
            _signer(args),
            audience=audience,
            scope=scope,
            client_id=args.client_id or _client_id_from_environment(),
            ca_path=ca_path,
        )
    if args.flow == "exchange":
        return token_client.exchange(
            issuer,
            _subject_token(args),
            audience=audience,
            scope=scope,
            client_id=args.client_id or _client_id_from_environment(),
            ca_path=ca_path,
        )
    return _by_device_flow(args, issuer, scope, ca_path)


def _proxy_for_dirac(args: argparse.Namespace) -> str:
    """The proxy a DIRAC server trades for a token: the one named, else the session's."""
    named = args.proxy or os.environ.get("X509_USER_PROXY") or ""
    if not named:
        import tempfile

        named = os.path.join(tempfile.gettempdir(), f"x509up_u{os.geteuid()}")
    if not os.path.isfile(named):
        raise _no_dirac(f"there is no proxy at {named}: make one with 'xrd-proxy init -voms lhcb'")
    return named


def _by_dirac(args: argparse.Namespace) -> Any:
    """A DIRAC token, by the exchange only a DIRAC client can perform.

    ``dirac-login`` trades the proxy at ``Framework/ProxyManager``'s
    ``exchangeProxyForToken`` for an access token and writes it into the proxy
    file; the server, not the client, talks to DiracX (its ``legacy-exchange``
    needs the *server's* API key). Reproducing that RPC here would mean
    reimplementing DISET - DIRAC's GPL-3.0 protocol - inside an LGPL package,
    which this deliberately does not do: the installed DIRAC client performs it,
    and this only reads the result out of the file.
    """
    from xrdsec import token_client

    if not args.group:
        raise token_client.TokenError(
            "a DIRAC token is minted for a DIRAC group: pass --group (e.g. lhcb_user)",
            code="no_group",
        )
    proxy = _proxy_for_dirac(args)
    try:
        from DIRAC.Core.Security.DiracX import (  # type: ignore[import-not-found]  # optional
            addTokenToPEM,
        )
    except ImportError as exc:
        raise _no_dirac(
            "DIRAC's own client is not installed, and it is the only thing that can perform"
            " the exchange: use 'dirac-login --token', or install DIRAC"
        ) from exc
    addTokenToPEM(proxy, args.group)
    import pathlib

    return token_client.from_pem(pathlib.Path(proxy).read_text())


def _no_dirac(said: str) -> Exception:
    """The refusal a ``--flow dirac`` run gives when it cannot delegate."""
    from xrdsec import token_client

    return token_client.TokenError(said, code="no_dirac")


def _subject_token(args: argparse.Namespace) -> str:
    """The token an exchange narrows: the one named, or the one this session holds."""
    from xrdsec import token_client

    if args.from_token:
        return str(args.from_token)
    return token_client.read_token()[0]


def _by_device_flow(args: argparse.Namespace, issuer: Any, scope: str, ca_path: str | None) -> Any:
    """RFC 8628: show a code, wait for a person to approve it, and take the token."""
    from xrdsec import token_client

    client_id = args.client_id or _client_id_from_environment()
    if not client_id:
        raise token_client.TokenError(
            "the device flow needs a client id: register a public client with the issuer"
            " and pass --client-id (or set WLCG_CLIENT_ID)",
            code="no_client",
        )
    started = token_client.device_authorization(issuer, client_id, scope=scope, ca_path=ca_path)
    where = started.get("verification_uri_complete") or started.get("verification_uri")
    if not args.quiet:
        _say(f"To approve this request, open:\n\n    {where}\n\n")
        _say(f"and enter the code: {started.get('user_code')}\n\nwaiting for approval...\n")
    interval = _numbers(started.get("interval"), 5.0)
    deadline = time.time() + _numbers(started.get("expires_in"), 600.0)
    while time.time() < deadline:
        time.sleep(interval)
        token = token_client.device_poll(
            issuer, client_id, str(started.get("device_code") or ""), ca_path=ca_path
        )
        if token is not None:
            return token
    raise token_client.TokenError("the code expired before it was approved; run it again")


def _numbers(value: Any, default: float) -> float:
    """A number from a JSON answer, or ``default`` when the issuer left it out."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _token_info(args: argparse.Namespace) -> int:
    """``xrd-proxy token info``: what the token says, and with ``--verify`` whether it holds."""
    from xrdsec import token_client

    try:
        token, source = _read_here(args)
        said = _token_claims(args, token)
    except (token_client.TokenError, proxytool_unavailable()) as refused:
        return fail(PROGRAM, refused)
    output.record("result", path=source, claims=said)
    if args.quiet:
        return OK
    _say(f"token     : from {source}\n")
    for name in ("iss", "sub", "aud", "scope", "wlcg.groups", "wlcg.ver", "exp", "iat"):
        if name in said:
            _say(f"{name:<10}: {said[name]}\n")
    expiry = said.get("exp")
    if expiry:
        left = float(expiry) - time.time()
        _say(f"{'timeleft':<10}: {_clock(max(0.0, left))}\n")
    return OK


def _token_claims(args: argparse.Namespace, token: str) -> dict[str, Any]:
    """The token's claims: decoded, or with ``--verify`` proved first.

    Describing a token is what a person does before using it, so the default is
    decoding - the same division ``info`` makes between describing and proving
    a proxy. ``--verify`` needs the issuer (or ``--from-resource``) because
    that is where the keys are.
    """
    from xrdsec import token_client
    from xrdsec.tokens import token_claims

    if not args.verify:
        return token_claims(token)
    return token_client.verify(
        token,
        issuer=_chosen_issuer(args) if (args.issuer or args.from_resource) else None,
        audience=args.audience if args.audience is not None else token_client.AUDIENCE,
        ca_path=args.certdir or None,
    )


def _read_here(args: argparse.Namespace) -> tuple[str, str]:
    """The token to describe and where it came from: the file named, else the search."""
    from xrdsec import token_client

    if args.path:
        import pathlib

        try:
            text = pathlib.Path(args.path).read_text()
        except OSError as exc:
            raise token_client.TokenError(f"cannot read {args.path}: {exc}") from exc
        # A DIRAC proxy carries its token in a block of its own: read that first,
        # so `xrd-proxy token info ~/.x509up` works on an LHCb DIRAC login.
        block = token_client.embedded(text)
        if block is not None:
            return str(block["access_token"]), f"{args.path} (DIRACX block)"
        return text.strip(), args.path
    return token_client.read_token()


def _token_destroy(args: argparse.Namespace) -> int:
    """``xrd-proxy token destroy``: remove the token file, refusing somebody else's."""
    from xrdsec import token_client

    try:
        path = args.out or token_client.token_file_path()
    except token_client.TokenError as refused:
        return fail(PROGRAM, refused)
    try:
        owner = os.lstat(path).st_uid
    except FileNotFoundError:
        return fail(PROGRAM, FileNotFoundError(f"there is no token at {path}"))
    if owner != os.geteuid():
        return fail(PROGRAM, PermissionError(f"{path} belongs to uid {owner}; not removed"))
    if not args.dry:
        os.unlink(path)
    output.record("result", path=path, removed=not args.dry)
    if not args.quiet:
        _say(f"{'Would remove' if args.dry else 'Removed'} {path}\n")
    return OK


def _client_id_from_environment() -> str:
    """The client id a session configured, under either name the WLCG tools use."""
    for name in ("WLCG_CLIENT_ID", "WLCG_TOKEN_CLIENT_ID"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _token_providers(args: argparse.Namespace) -> int:
    """``xrd-proxy token providers``: the named issuers, and which one is in force."""
    from xrdsec import providers

    said = providers.resolve(environ=os.environ)
    rows = [
        {
            "name": one.name,
            "issuer": one.issuer,
            "scope": one.scope,
            "storage": one.storage,
            "who": one.who,
        }
        for one in (providers.PROVIDERS[name] for name in providers.names())
    ]
    output.record("result", providers=rows, in_force=said.as_dict())
    if not args.quiet:
        for line in providers.describe():
            _say(f"{line}\n")
        _say(
            f"\nIn force now: issuer={said.issuer or '(none)'} ({said.from_issuer or 'unset'}), "
            f"scope from {said.from_scope}, audience from {said.from_audience}\n"
        )
    return OK


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _doctor(args: argparse.Namespace) -> int:
    """``xrd-proxy doctor``: every question a proxy needs, answered in plain English.

    One line per check, and under each one the thing to do about it - by the
    user, by whoever runs the machine, or by the experiment. Exit ``0`` when
    nothing would stop a proxy from being made and used, ``1`` when something
    would; ``--json``/``--xml`` report the same lines as data.
    """
    from xrdsec import doctor

    from ..doctor import Check, Report

    checks: list[Check] = []

    class _Sink:
        """Where :func:`xrdsec.doctor.check` writes: the report's own :class:`Check` lines."""

        def add(self, name: str, state: str, detail: str, hint: str = "") -> None:
            checks.append(Check(name, state, detail, hint))

    vo = args.voms[0].partition(":")[0] if args.voms else None
    doctor.check(
        _Sink(),
        vo=vo,
        proxy=args.file,
        servers=not args.offline,
        all_vos=args.all_vos,
    )
    report = Report(checks=tuple(checks))
    counts = _counted(report)
    passing = report.ok and not (args.strict and counts["warn"])
    if output.current() is not None:
        output.payload({"ok": passing, "vo": vo, "summary": counts, "checks": report.to_dict()})
    elif not args.quiet:
        _say("".join(f"{check}\n" for check in report))
        if args.summary:
            _say(f"{counts['ok']} ok, {counts['warn']} warning(s), {counts['bad']} problem(s)\n")
    return OK if passing else ERROR


def _counted(report: Report) -> dict[str, int]:
    """How many of each mark the report has: what a summary line, or a monitor, wants."""
    seen = Counter(check.state for check in report)
    return {state: seen[state] for state in _MARKS}


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
