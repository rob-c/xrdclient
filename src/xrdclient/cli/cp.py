"""``xrd-cp`` - copy files between anything and anything.

    $ xrd-cp root://eos.example.org//store/data.root /tmp/
    $ xrd-cp -r /tmp/results davs://dav.example.org/store/results
    $ xrd-cp --tpc root://a//store/f.root root://b//store/f.root

The tool is ``cp``: the last argument is the destination, several sources are
allowed when it is a directory, and a trailing ``/`` on DEST means "into this
directory". A trailing ``/`` on a SOURCE borrows rsync's convention instead:
it means "the contents of this directory", which land in DEST itself rather
than in ``DEST/<name>`` - the only spelling a repeated ``-r --sync`` can use,
since from the second run on DEST exists and ``cp`` would nest the copy into
it. Everything it does is one call into :func:`xrdclient.copy`.

Reading order: :func:`_parser` (every flag, including the ``xrdcp`` ones
under their ``xrdcp`` names in :func:`_xrdcp_flags`), then :func:`_misuse`
(flag combinations refused up front with exit code ``2``, so a contradictory
command never half-runs), then :func:`_main` -> :func:`_transfers` ->
:func:`_run` -> :func:`_copy_one`, which picks one of the four library calls:
:func:`~xrdclient.copy.append_zip`, :func:`~xrdclient.copy.third_party`,
:func:`~xrdclient.copy.copy_tree` or :func:`~xrdclient.copy.copy`.

An existing DEST is an error without ``-f``, and checksum verification
follows the library default unless ``--verify`` or ``--no-verify`` says
otherwise.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TextIO, TypedDict, cast

from .. import report as output
from ..config import Config
from ..copy import CopyResult, append_zip, copy, copy_tree, third_party
from ..errors import XRootDError
from ..types import human_bytes as _human
from ..url import XRootDURL, parse
from . import OK, USAGE, Endpoints, common_flags, config_from, fail, size_arg, version_flag

__all__ = ["main"]

PROGRAM = "xrd-cp"


def _parser() -> argparse.ArgumentParser:
    """Every ``xrd-cp`` flag, with ``--json``/``--xml`` and the shared credential flags.

    ``--verify``/``--no-verify`` and ``--progress``/``--no-progress`` default
    to ``None``, not ``False``: "not said" must stay distinguishable from
    "said no", because the library default (and, for progress, whether stderr
    is a terminal) decides the unsaid case.
    """
    parser = output.Parser(
        prog=PROGRAM,
        description="Copy files between root://, https://, and the local filesystem.",
        epilog=(
            "As with cp, SOURCE goes inside DEST when DEST is an existing directory or "
            "ends in '/'. As with rsync, a SOURCE ending in '/' means its contents, which "
            "go into DEST itself: use 'xrd-cp -r --sync size SRC/ DEST' for a sync you "
            "will run again, since without the slash the second run nests into DEST/SRC."
        ),
    )
    version_flag(parser)
    parser.add_argument(
        "source",
        nargs="+",
        help="what to copy; several when DEST is a directory; with a trailing / its contents",
    )
    parser.add_argument("dest", help="where to put it")
    parser.add_argument("-r", "--recursive", action="store_true", help="copy directories")
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="overwrite DEST if it is already there; without this, an existing DEST is an error",
    )
    # What the default used to be spelled as, kept so that a script that says
    # it out loud still runs; it asks for what it now gets anyway.
    parser.add_argument("-n", "--no-clobber", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "-c",
        "--continue",
        dest="resume",
        action="store_true",
        help="carry on from what is already at DEST instead of copying it again",
    )
    parser.add_argument(
        "--tpc",
        action="store_true",
        help="third-party copy: let the servers move the data; xrdcp's "
        "'--tpc delegate only' delegates your proxy to the destination, "
        "which dCache needs",
    )
    # Set by ``--tpc delegate`` (see :func:`_tpc_words`), never typed itself.
    parser.add_argument("--tpc-delegate", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--tpc-first", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--verify", dest="verify", action="store_true", help="require a checksum match"
    )
    parser.add_argument(
        "--no-verify", dest="verify", action="store_false", help="skip checksum verification"
    )
    parser.set_defaults(verify=None)
    parser.add_argument("-a", "--algorithm", metavar="NAME", help="checksum to verify with")
    parser.add_argument("--chunk-size", type=size_arg, metavar="N", help="transfer chunk, e.g. 8M")
    parser.add_argument(
        "--in-flight",
        type=int,
        metavar="N",
        help="chunks read ahead while the last is written (default 2, 1 for neither)",
    )
    parser.add_argument(
        "--stripes",
        type=int,
        metavar="N",
        help="connections to move one file over, a span each (default 4, 1 for one)",
    )
    parser.add_argument(
        "--streams",
        type=int,
        metavar="N",
        help="extra connections each file's data rides on (default 1, 0 for none)",
    )
    _xrdcp_flags(parser)
    parser.add_argument(
        "-p",
        "--progress",
        action="store_true",
        default=None,
        help="show progress (default on a tty)",
    )
    parser.add_argument(
        "--no-progress", dest="progress", action="store_false", help="never show progress"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report the transfers without making them"
    )
    parser.add_argument(
        "--remove-source",
        action="store_true",
        help="delete each source once its copy is verified, making this a move",
    )
    parser.add_argument(
        "--include",
        metavar="PATTERN",
        action="append",
        default=[],
        help="with -r, copy only what matches (repeatable)",
    )
    parser.add_argument(
        "--exclude",
        metavar="PATTERN",
        action="append",
        default=[],
        help="with -r, skip what matches (repeatable, and it wins over --include)",
    )
    parser.add_argument(
        "--sync",
        choices=("size", "mtime", "checksum"),
        help="with -r, skip files the target already has, compared this way",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        metavar="N",
        help="with -r, copy N files at once (default 1)",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="with -r, remove files under DEST that SOURCE does not have",
    )
    common_flags(parser)
    return parser


def _xrdcp_flags(parser: argparse.ArgumentParser) -> None:
    """The transfer controls ``xrdcp`` has, under ``xrdcp``'s names.

    Same short and long spellings and the same ranges (checked in
    :data:`_LIMITS`), so an ``xrdcp`` command line can be retargeted by
    changing the program name. ``-Z`` and ``-F`` default to ``None`` so that
    an unsaid flag leaves the library default alone rather than forcing
    ``False``. ``-f`` is not here: it is defined in :func:`_parser` and means
    overwrite, as it does for ``xrdcp`` too.
    """
    parser.add_argument(
        "-z",
        "--zip",
        metavar="MEMBER",
        help="copy MEMBER from each source ZIP archive (xrdcl.unzip)",
    )
    parser.add_argument(
        "-y",
        "--sources",
        type=int,
        metavar="N",
        help="read a root:// file from up to N of its replicas at once (1 to 32)",
    )
    parser.add_argument(
        "-X",
        "--xrate",
        type=size_arg,
        metavar="RATE",
        help="cap the transfer at RATE bytes a second, e.g. 20M (at least 10k)",
    )
    parser.add_argument(
        "--xrate-threshold",
        type=size_arg,
        metavar="RATE",
        help="fail a transfer slower than RATE bytes a second (at least 10k)",
    )
    parser.add_argument(
        "--cptimeout",
        type=float,
        metavar="SECONDS",
        help="fail a transfer still running after SECONDS",
    )
    parser.add_argument(
        "-Z",
        "--dynamic-src",
        action="store_true",
        default=None,
        help="the source may still be growing: read to its end, not to its size",
    )
    parser.add_argument(
        "--zip-append",
        action="store_true",
        help="append each source as a stored member of the destination ZIP archive",
    )
    parser.add_argument(
        "--tlsmetalink",
        action="store_true",
        help="upgrade root/xroot replica URLs in Metalinks to TLS",
    )
    parser.add_argument(
        "--zip-mtln-cksum",
        action="store_true",
        help="with --zip, verify the member against its Metalink checksum",
    )
    parser.add_argument(
        "-F",
        "--coerce",
        action="store_true",
        default=None,
        help="ignore the server's file usage rules when opening DEST (kXR_force)",
    )


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


class Bar:
    """A one-line progress display for a terminal, and nothing else.

    Deliberately not a dependency on ``tqdm``: the callback protocol
    :func:`xrdclient.copy` takes is ``(done, total)``, so anyone who wants ``tqdm``
    passes ``tqdm(...).update`` themselves.
    """

    def __init__(self, label: str, stream: TextIO | None = None) -> None:
        """A bar headed ``label``, drawn on ``stream`` (standard error by default)."""
        self.label = label
        self.stream = stream or sys.stderr
        # The last whole percentage drawn: -1 until the first, so that
        # :meth:`finish` knows whether there is a line to end.
        self._last = -1
        # Captured at construction rather than looked up per call: progress
        # callbacks can arrive on a transfer worker thread, where the
        # context variable :func:`~xrdcore.report.current` reads is not set.
        self.report = output.current()

    def __call__(self, done: int, total: int | None) -> None:
        """Show ``done`` of ``total`` bytes; ``total`` is ``None`` when unknown.

        Under a structured report every call becomes a ``progress`` record
        instead. On a terminal the line is redrawn only when the whole
        percentage changes: a callback per chunk would otherwise spend more
        time writing to the tty than a fast LAN copy spends copying.
        """
        if self.report is not None:
            self.report.record(
                "progress", bytes_transferred=done, total=total, **self.report.identity
            )
            return
        if total:
            percent = int(100 * done / total)
            if percent == self._last:
                return
            self._last = percent
            text = f"{self.label} {percent:3d}% {_human(done)}/{_human(total)}"
        else:
            text = f"{self.label}     {_human(done)}"
        print(f"\r{text}", end="", file=self.stream, flush=True)

    def finish(self) -> None:
        """End the bar's line, so the next output does not overwrite it."""
        if self.report is not None:
            return
        if self._last >= 0 or self.stream is not sys.stderr:
            print(file=self.stream)


class _CopyOptions(TypedDict, total=False):
    """What the flags may override; anything absent keeps the library default."""

    overwrite: bool
    verify: bool
    algorithm: str
    chunk_size: int
    dry_run: bool
    remove_source: bool
    resume: bool
    sources: int
    max_rate: float
    min_rate: float
    timeout: float
    dynamic_source: bool
    coerce: bool


# ---------------------------------------------------------------------------
# Destination resolution
# ---------------------------------------------------------------------------


def _is_dir(url: XRootDURL, endpoints: Endpoints) -> bool:
    """Does ``url`` name a directory that already exists?

    A trailing ``/`` is taken at its word without asking the server, as
    ``cp`` does. A remote stat that fails - DEST does not exist yet, or the
    server will not say - counts as "not a directory", so ``xrd-cp f new``
    writes ``new``; any real problem with DEST surfaces from the copy
    itself, with the copy's error message.
    """
    if url.path.endswith("/"):
        return True
    if url.is_local:
        return os.path.isdir(url.path)
    filesystem, path = endpoints.at(url)
    try:
        return bool(filesystem.isdir(path))
    except XRootDError:
        return False


def _source(text: str) -> XRootDURL:
    """``text`` parsed, keeping a trailing slash that asks for its contents.

    A bare local path is made absolute on the way in, which drops the slash;
    here it carries meaning (see :func:`_destination`), so it is put back.
    """
    url = parse(text)
    if text.endswith(("/", os.sep)) and not url.path.endswith("/"):
        return url.with_path(url.path + "/")
    return url


def _destination(source: XRootDURL, dest: XRootDURL, *, into: bool) -> XRootDURL:
    """``cp f d`` writes ``d``; ``cp f d/`` and ``cp f dir`` write ``dir/f``.

    ``cp -r src/ dir`` writes ``dir`` itself, as rsync does: the slash asks
    for the contents of ``src``. Without it a repeated sync could never work,
    because the first run creates ``dir`` and the second then nests into
    ``dir/src``.
    """
    if not into or source.path.endswith("/"):
        return dest
    name = posixpath.basename(source.path.rstrip("/")) or "root"
    return dest / name


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _misuse(args: argparse.Namespace) -> str | None:
    """The flag combinations that cannot mean anything, in words."""
    for check in (_tree_misuse, _count_misuse, _transfer_misuse, _limit_misuse, _tpc_misuse):
        complaint = check(args)
        if complaint is not None:
            return complaint
    return None


def _tree_misuse(args: argparse.Namespace) -> str | None:
    """Options which only describe a recursive copy need ``-r``."""
    if not args.recursive and (
        args.include or args.exclude or args.sync or args.delete or args.parallel
    ):
        return "--include, --exclude, --sync, --delete and --parallel describe a tree; add -r"
    return _archive_tree_misuse(args)


def _archive_tree_misuse(args: argparse.Namespace) -> str | None:
    """ZIP operations each address one archive and never a directory tree."""
    if not args.recursive:
        return None
    if args.zip:
        return "--zip selects one file inside an archive and cannot be recursive"
    if args.zip_append:
        return "--zip-append adds files, not directory trees; omit -r"
    return None


def _count_misuse(args: argparse.Namespace) -> str | None:
    """Require each concurrency count to describe at least one unit."""
    counts = (
        (args.parallel, "--parallel is how many files to copy at once, so at least one"),
        (args.in_flight, "--in-flight is how many chunks to hold at once, so at least one"),
        (args.stripes, "--stripes is how many connections to move one file over, so at least one"),
    )
    for value, complaint in counts:
        if value is not None and value < 1:
            return complaint
    if args.streams is not None and args.streams < 0:
        # Nought is a real answer here: it asks for the control link alone.
        return "--streams is how many extra connections a file's data gets, so not negative"
    return None


def _transfer_misuse(args: argparse.Namespace) -> str | None:
    """Reject transfer strategies whose promises contradict one another."""
    for check in (_tpc_local_misuse, _resume_misuse, _overwrite_misuse, _zip_misuse):
        complaint = check(args)
        if complaint is not None:
            return complaint
    return None


def _tpc_words(given: Sequence[str] | None) -> list[str]:
    """The arguments (``given``, else ``sys.argv[1:]``) with xrdcp's ``--tpc`` words as flags.

    Stock ``xrdcp`` takes ``--tpc [delegate] [first|only]``.

    argparse cannot give ``--tpc`` optional words without also swallowing
    the source URL that follows a bare ``--tpc``, so the words are taken off
    here: ``delegate`` becomes ``--tpc-delegate``, ``only`` is what ``--tpc``
    already means, and ``first`` is kept as ``--tpc-first`` to be refused
    (:func:`_tpc_local_misuse`). Only the words directly after ``--tpc`` are
    read, ``delegate`` before the mode as xrdcp writes it, so a file named
    ``only`` elsewhere on the line is still a file.
    """
    argv = list(sys.argv[1:] if given is None else given)
    out: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        out.append(arg)
        index += 1
        if arg != "--tpc":
            continue
        if argv[index : index + 1] == ["delegate"]:
            out.append("--tpc-delegate")
            index += 1
        mode = argv[index] if index < len(argv) else ""
        if mode in ("only", "first"):
            out += [] if mode == "only" else ["--tpc-first"]
            index += 1
    return out


def _is_http(url: str) -> bool:
    """Whether ``url`` is reached over HTTP (``http``, ``https``, ``dav``, ``davs``)."""
    return url.split("://", 1)[0].lower() in ("http", "https", "dav", "davs")


def _tpc_local_misuse(args: argparse.Namespace) -> str | None:
    """``--tpc`` with a flag that needs the bytes to pass through this client."""
    if args.tpc and (args.dry_run or args.remove_source):
        return "--tpc hands the transfer to the servers; --dry-run and --remove-source cannot"
    if args.tpc_delegate and any(_is_http(url) for url in (*args.source, args.dest)):
        return (
            "--tpc delegate over https needs GridSite delegation, which xrd-cp does not do; "
            "plain --tpc lets each end authorise the other with a macaroon instead"
        )
    if args.tpc_first:
        return (
            "--tpc first falls back to copying through this client, which xrd-cp never "
            "does behind your back; use '--tpc only' (the same as --tpc), or no --tpc"
        )
    return None


def _resume_misuse(args: argparse.Namespace) -> str | None:
    """``--continue`` with a flag that rules out a partial DEST to continue."""
    if args.resume and (args.tpc or args.no_clobber):
        return "--continue needs a partial DEST to carry on from; --tpc and -n forbid one"
    return None


def _overwrite_misuse(args: argparse.Namespace) -> str | None:
    """``-f`` and ``-n`` together: one of them was a mistake."""
    if args.force and args.no_clobber:
        return "-f overwrites DEST and -n refuses to; they cannot both be what you meant"
    return None


def _zip_misuse(args: argparse.Namespace) -> str | None:
    """The ZIP flags against each other and against transfer modes they cannot use.

    ``--zip`` is unpacked by this client (the ``xrdcl.unzip`` CGI is XrdCl's
    client-side convention, not a server feature), so it needs the bytes;
    ``--zip-append`` writes a fresh member and has no partial or existing
    member to continue, overwrite or protect.
    """
    if args.zip and args.tpc:
        return "--zip is expanded by this client and cannot be combined with --tpc"
    if args.zip and args.zip_append:
        return "--zip reads a member and --zip-append writes one; choose one"
    if args.zip_append and (args.tpc or args.resume or args.force or args.no_clobber):
        return "--zip-append cannot be combined with --tpc, --continue, -f or -n"
    return None


#: The smallest rate ``xrdcp`` takes for ``--xrate`` and ``--xrate-threshold``.
_MIN_RATE = 10 * 1024


#: What the ``xrdcp`` flags must not be, and what to say when they are.
_LIMITS: tuple[tuple[Callable[[argparse.Namespace], bool], str], ...] = (
    (
        lambda a: a.sources is not None and not 1 <= a.sources <= 32,
        "--sources is how many servers to read from at once, from 1 to 32",
    ),
    (
        lambda a: any(r is not None and r < _MIN_RATE for r in (a.xrate, a.xrate_threshold)),
        "--xrate and --xrate-threshold take a rate of at least 10k bytes a second",
    ),
    (
        lambda a: a.cptimeout is not None and a.cptimeout <= 0,
        "--cptimeout is how long a transfer may take, so more than nothing",
    ),
    (
        lambda a: bool(a.resume) and (a.sources or 0) > 1,
        "--continue carries one partial DEST on from one source; --sources reads several",
    ),
)


def _limit_misuse(args: argparse.Namespace) -> str | None:
    """Hold the ``xrdcp`` flags to ``xrdcp``'s ranges, and to what they can combine with."""
    if args.zip and (args.sources or 0) > 1:
        return "--zip reads one archive stream and cannot be combined with --sources"
    if args.zip_append and (args.sources or 0) > 1:
        return "--zip-append reads each source once and cannot be combined with --sources"
    return next((complaint for broken, complaint in _LIMITS if broken(args)), None)


#: The flags that shape how bytes pass through this process, which a
#: third-party copy never sends through it.
_STREAM_FLAGS = (
    ("chunk_size", "--chunk-size"),
    ("in_flight", "--in-flight"),
    ("stripes", "--stripes"),
    ("streams", "--streams"),
    ("sources", "--sources"),
    ("xrate", "--xrate"),
    ("xrate_threshold", "--xrate-threshold"),
    ("cptimeout", "--cptimeout"),
    ("dynamic_src", "--dynamic-src"),
)


def _tpc_misuse(args: argparse.Namespace) -> str | None:
    """Refuse what a third-party copy cannot honour, rather than dropping it.

    The servers copy one file each time they are asked, so ``-r`` has no
    meaning here, and the tuning flags describe a stream this process never
    carries. (``--verify`` and ``--algorithm`` are honoured: both ends are
    asked for their checksum afterwards.)
    """
    if not args.tpc:
        return None
    if args.recursive:
        return "--tpc copies one file per pair of servers; -r cannot be combined with it"
    for attribute, flag in _STREAM_FLAGS:
        if getattr(args, attribute) is not None:
            return f"--tpc moves no data through this process, so {flag} has nothing to tune"
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """The ``xrd-cp`` entry point: run with ``argv`` (default ``sys.argv[1:]``).

    Returns the exit code rather than exiting, so tests and embedding
    programs can call it; the console script wrapper does the exit.
    """
    return output.run_cli(PROGRAM, argv, lambda: _main(argv), legacy_json=True)


def _main(argv: Sequence[str] | None = None) -> int:
    """Parse, refuse contradictions, resolve DEST, copy, and report.

    Exit codes: ``0`` when every transfer succeeded, ``1`` on the first
    failure (the sources after it are not attempted), ``2`` for a usage
    error, including several sources aimed at a DEST that is not a
    directory.
    """
    args = _parser().parse_args(_tpc_words(argv))
    output.identify("copy", sources=args.source, target=args.dest)
    complaint = _misuse(args)
    if complaint is not None:
        print(f"{PROGRAM}: {complaint}", file=sys.stderr)
        return USAGE
    sources = [_source(s) for s in args.source]
    if args.zip:
        # XrdCl's spelling: ``?xrdcl.unzip=MEMBER`` on the archive URL is what
        # ``xrdcp --zip`` sends through, and :func:`~xrdclient.copy` reads it.
        sources = [source.with_query(**{"xrdcl.unzip": args.zip}) for source in sources]
    dest = parse(args.dest)
    # Progress by default only for a person watching: not in a pipeline or a
    # batch log, and not when -q asked for silence.
    show = args.progress if args.progress is not None else (sys.stderr.isatty() and not args.quiet)
    job = _Job(args, _copy_config(args), show, _copy_options(args))

    try:
        results = _transfers(job, sources, dest)
    except (XRootDError, OSError, ValueError) as exc:
        return fail(PROGRAM, exc)
    if results is None:
        print(f"{PROGRAM}: {args.dest} is not a directory", file=sys.stderr)
        return USAGE
    _show_results(results, args)
    return OK


def _copy_config(args: argparse.Namespace) -> Config:
    """The common client configuration with copy-specific tuning applied.

    In the library these are :class:`~xrdclient.Config` fields, not
    :func:`~xrdclient.copy` keywords, so they are applied here, each only
    when given, and the settings file still decides the rest.
    """
    config = config_from(args)
    if args.in_flight is not None:
        config = config.evolve(in_flight=args.in_flight)
    if args.stripes is not None:
        config = config.evolve(parallel_chunks=args.stripes)
    if args.streams is not None:
        config = config.evolve(data_streams=args.streams)
    if args.tlsmetalink:
        config = config.evolve(tls_metalink=True)
    if args.zip_mtln_cksum:
        config = config.evolve(zip_metalink_checksum=True)
    return config


@dataclass(frozen=True, slots=True)
class _Job:
    """What every transfer of one invocation shares, decided once in :func:`_main`.

    Grouped so the per-source helpers take the job plus what differs per
    source, rather than the same four values threaded through each call.
    """

    #: The parsed command line; the helpers read the mode flags from it.
    args: argparse.Namespace
    #: The client configuration, with the copy tuning flags applied.
    config: Config
    #: Whether to draw a progress bar on a terminal.
    show: bool
    #: The keyword options both :func:`~xrdclient.copy` and ``copy_tree`` take.
    options: _CopyOptions


def _transfers(job: _Job, sources: list[XRootDURL], dest: XRootDURL) -> list[CopyResult] | None:
    """Run a valid single-target invocation, or report an ambiguous target.

    ``None`` means several sources were aimed at a DEST that is neither an
    existing directory nor spelled with a trailing ``/`` - ``cp``'s "target
    is not a directory", which :func:`_main` reports as a usage error.
    """
    if job.args.zip_append:
        # DEST is the archive itself, never a directory to copy into, so
        # there is nothing to look up first.
        return _run(job, sources, dest, into=False)
    with Endpoints(job.config) as endpoints:
        into = _is_dir(dest, endpoints)
        if len(sources) > 1 and not into:
            return None
        return _run(job, sources, dest, into=into)


#: Destinations that mean "the bytes come out of this command", where a
#: summary line on stdout would be spliced into the file itself.
_STDOUT_NAMES = frozenset({"-", "/dev/stdout", "/dev/fd/1", "/proc/self/fd/1"})


def _writes_to_stdout(results: Sequence[CopyResult]) -> bool:
    """Whether any transfer here put a file on standard output."""
    return any(
        r.target in _STDOUT_NAMES or r.target.removeprefix("file://") in _STDOUT_NAMES
        for r in results
    )


def _show_results(results: Sequence[CopyResult], args: argparse.Namespace) -> None:
    """Render completed transfers in the requested command-line format.

    A copy whose destination *is* standard output reports on standard error
    instead: the alternative is a summary line glued onto the end of the file,
    which is a corrupt download that looks like a successful one.
    """
    where = sys.stderr if _writes_to_stdout(results) else sys.stdout
    if args.json or output.current() is not None:
        output.payload([_record(r) for r in results], where=where)
    elif not args.quiet:
        for result in results:
            print(result, file=where)


def _run(job: _Job, sources: list[XRootDURL], dest: XRootDURL, *, into: bool) -> list[CopyResult]:
    """Copy each source in turn to where it belongs under ``dest``.

    ``into`` is whether ``dest`` is a directory the sources go inside. Each
    completed transfer becomes a ``result`` record as soon as it finishes,
    so a report from a run that fails part-way still lists what succeeded.
    """
    args = job.args
    results: list[CopyResult] = []
    for source in sources:
        # A ``--zip`` copy into a directory is named after the member, not
        # after the archive it came out of.
        target = (
            dest / posixpath.basename(args.zip.rstrip("/"))
            if into and args.zip
            else _destination(source, dest, into=into)
        )
        bar = _progress_for(source, args, job.show)
        output.identify("copy", source=str(source), target=str(target))
        try:
            completed = _copy_one(job, source, target, bar)
            for result in completed:
                output.record(
                    operation="copy",
                    status="planned" if args.dry_run else "succeeded",
                    source=result.source,
                    target=result.target,
                    value=_record(result),
                )
            results.extend(completed)
        finally:
            if bar is not None:
                bar.finish()
    return results


def _progress_for(source: XRootDURL, args: argparse.Namespace, show: bool) -> Bar | None:
    """A progress bar for ``source``, or ``None`` when nobody would see one.

    Under ``--json`` progress becomes records unless ``--no-progress`` was
    given explicitly; ``-p`` alone is not needed there, since the records do
    not disturb anyone's terminal.
    """
    reporting = output.current() is not None and args.progress is not False
    if show or reporting:
        return Bar(posixpath.basename(source.path.rstrip("/")) or str(source))
    return None


#: Flags passed on to :func:`~xrdclient.copy` when given: attribute, keyword.
_PASSED = (
    ("algorithm", "algorithm"),
    ("chunk_size", "chunk_size"),
    ("dry_run", "dry_run"),
    ("remove_source", "remove_source"),
    ("resume", "resume"),
    ("sources", "sources"),
    ("xrate", "max_rate"),
    ("xrate_threshold", "min_rate"),
    ("cptimeout", "timeout"),
    ("dynamic_src", "dynamic_source"),
    ("coerce", "coerce"),
)


def _copy_options(args: argparse.Namespace) -> _CopyOptions:
    """Keyword options shared by ordinary and recursive copies.

    Only flags actually given are passed, so the library's defaults stay the
    single source of truth for everything else.
    """
    # ``--continue`` writes into an existing DEST by definition, so it implies
    # the permission ``-f`` would otherwise have to give.
    options: dict[str, Any] = {"overwrite": args.force or args.resume}
    if args.verify is not None:
        options["verify"] = args.verify  # where ``False`` is an answer too
    for attribute, keyword in _PASSED:
        value = getattr(args, attribute)
        if value:
            options[keyword] = value
    return cast("_CopyOptions", options)


def _copy_one(job: _Job, source: XRootDURL, target: XRootDURL, bar: Bar | None) -> list[CopyResult]:
    """Copy one resolved source using the selected transfer strategy.

    The strategies are exclusive (:func:`_misuse` has already refused the
    combinations), checked in the order that makes the dispatch unambiguous.
    A recursive copy returns one result per file; the others return one.
    """
    args, config, options = job.args, job.config, job.options
    if args.zip_append:
        # The copy options do not apply: :func:`~xrdclient.copy.append_zip`
        # takes only the chunk size, and refuses a member already present
        # rather than overwriting or resuming one.
        return [
            append_zip(
                source,
                target,
                chunk_size=args.chunk_size,
                progress=bar,
                config=config,
                dry_run=args.dry_run,
                remove_source=args.remove_source,
            )
        ]
    if args.tpc:
        return [_third_party(source, target, args, config)]
    if args.recursive:
        return copy_tree(
            source,
            target,
            config=config,
            progress=bar,
            include=args.include,
            exclude=args.exclude,
            sync=args.sync,
            delete=args.delete,
            workers=args.parallel,
            **options,
        )
    return [copy(source, target, config=config, progress=bar, **options)]


def _third_party(
    source: XRootDURL, target: XRootDURL, args: argparse.Namespace, config: Config
) -> CopyResult:
    """One ``--tpc`` transfer, verified when asked.

    Naming a checksum with ``-a`` counts as asking, since there is no digest
    taken on the way past for it to choose; ``--no-verify`` still wins.
    """
    verify = bool(args.verify) or (args.verify is None and bool(args.algorithm))
    return third_party(
        source,
        target,
        config=config,
        overwrite=args.force,
        verify=verify,
        algorithm=args.algorithm,
        coerce=bool(args.coerce),
        delegate=bool(args.tpc_delegate),
    )


def _record(result: CopyResult) -> dict[str, object]:
    """One transfer as JSON: the dataclass plus what it computes.

    Scripts parse these keys from ``--json``, so renaming one breaks them.
    ``rate`` is ``None`` for a copy that took no measurable time rather than
    a division by zero.
    """
    return {
        "source": result.source,
        "target": result.target,
        "size": result.size,
        "seconds": round(result.seconds, 6),
        "rate": round(result.rate, 3) if result.seconds > 0 else None,
        "resumed_at": result.resumed_at,
        "verified": result.verified,
        "checksum": None if result.checksum is None else str(result.checksum),
    }


if __name__ == "__main__":
    raise SystemExit(main())
