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
"""

from __future__ import annotations

import argparse
import os
import posixpath
import sys
from collections.abc import Callable, Sequence
from typing import Any, TextIO, TypedDict, cast

from ..config import Config
from ..copy import CopyResult, append_zip, copy, copy_tree, third_party
from ..errors import XRootDError
from ..types import human_bytes as _human
from ..url import XRootDURL, parse
from . import OK, USAGE, Endpoints, common_flags, config_from, dumps, fail, size_arg, version_flag

__all__ = ["main"]

PROGRAM = "xrd-cp"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
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
        "--tpc", action="store_true", help="third-party copy: let the servers move the data"
    )
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
    """The transfer controls ``xrdcp`` has, under ``xrdcp``'s names."""
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
        self.label = label
        self.stream = stream or sys.stderr
        self._last = -1

    def __call__(self, done: int, total: int | None) -> None:
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
    """Does ``url`` name a directory that already exists?"""
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


def _tpc_local_misuse(args: argparse.Namespace) -> str | None:
    if args.tpc and (args.dry_run or args.remove_source):
        return "--tpc hands the transfer to the servers; --dry-run and --remove-source cannot"
    return None


def _resume_misuse(args: argparse.Namespace) -> str | None:
    if args.resume and (args.tpc or args.no_clobber):
        return "--continue needs a partial DEST to carry on from; --tpc and -n forbid one"
    return None


def _overwrite_misuse(args: argparse.Namespace) -> str | None:
    if args.force and args.no_clobber:
        return "-f overwrites DEST and -n refuses to; they cannot both be what you meant"
    return None


def _zip_misuse(args: argparse.Namespace) -> str | None:
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
    args = _parser().parse_args(argv)
    complaint = _misuse(args)
    if complaint is not None:
        print(f"{PROGRAM}: {complaint}", file=sys.stderr)
        return USAGE
    config = _copy_config(args)
    sources = [_source(s) for s in args.source]
    if args.zip:
        sources = [source.with_query(**{"xrdcl.unzip": args.zip}) for source in sources]
    dest = parse(args.dest)
    show = args.progress if args.progress is not None else (sys.stderr.isatty() and not args.quiet)

    try:
        results = _transfers(sources, dest, args, config, show=show)
    except (XRootDError, OSError, ValueError) as exc:
        return fail(PROGRAM, exc)
    if results is None:
        print(f"{PROGRAM}: {args.dest} is not a directory", file=sys.stderr)
        return USAGE
    _show_results(results, args)
    return OK


def _copy_config(args: argparse.Namespace) -> Config:
    """The common client configuration with copy-specific tuning applied."""
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


def _transfers(
    sources: list[XRootDURL],
    dest: XRootDURL,
    args: argparse.Namespace,
    config: Config,
    *,
    show: bool,
) -> list[CopyResult] | None:
    """Run a valid single-target invocation, or report an ambiguous target."""
    if args.zip_append:
        return _run(sources, dest, args, config, into=False, show=show)
    with Endpoints(config) as endpoints:
        into = _is_dir(dest, endpoints)
        if len(sources) > 1 and not into:
            return None
        return _run(sources, dest, args, config, into=into, show=show)


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
    if args.json:
        print(dumps([_record(r) for r in results]), file=where)
    elif not args.quiet:
        for result in results:
            print(result, file=where)


def _run(
    sources: list[XRootDURL],
    dest: XRootDURL,
    args: argparse.Namespace,
    config: Config,
    *,
    into: bool,
    show: bool,
) -> list[CopyResult]:
    """Do the transfers the parsed command line asks for."""
    options = _copy_options(args)
    results: list[CopyResult] = []
    for source in sources:
        target = (
            dest / posixpath.basename(args.zip.rstrip("/"))
            if into and args.zip
            else _destination(source, dest, into=into)
        )
        bar = Bar(posixpath.basename(source.path.rstrip("/")) or str(source)) if show else None
        try:
            results.extend(_copy_one(source, target, args, config, bar, options))
        finally:
            if bar is not None:
                bar.finish()
    return results


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
    """Keyword options shared by ordinary and recursive copies."""
    options: dict[str, Any] = {"overwrite": args.force or args.resume}
    if args.verify is not None:
        options["verify"] = args.verify  # where ``False`` is an answer too
    for attribute, keyword in _PASSED:
        value = getattr(args, attribute)
        if value:
            options[keyword] = value
    return cast("_CopyOptions", options)


def _copy_one(
    source: XRootDURL,
    target: XRootDURL,
    args: argparse.Namespace,
    config: Config,
    bar: Bar | None,
    options: _CopyOptions,
) -> list[CopyResult]:
    """Copy one resolved source using the selected transfer strategy."""
    if args.zip_append:
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
    )


def _record(result: CopyResult) -> dict[str, object]:
    """One transfer as JSON: the dataclass plus what it computes."""
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
