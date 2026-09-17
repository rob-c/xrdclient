"""``xrd-cp`` - copy files between anything and anything.

    $ xrd-cp root://eos.example.org//store/data.root /tmp/
    $ xrd-cp -r /tmp/results davs://dav.example.org/store/results
    $ xrd-cp --tpc root://a//store/f.root root://b//store/f.root

The tool is ``cp``: the last argument is the destination, several sources are
allowed when it is a directory, and a trailing ``/`` means "into this
directory". Everything it does is one call into :func:`xrd.copy`.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import sys
from collections.abc import Sequence
from typing import TextIO, TypedDict

from ..config import Config
from ..copy import CopyResult, copy, copy_tree, third_party
from ..errors import XRootDError
from ..types import human_bytes as _human
from ..url import XRootDURL, parse
from . import OK, USAGE, Endpoints, common_flags, config_from, dumps, fail, size_arg

__all__ = ["main"]

PROGRAM = "xrd-cp"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Copy files between root://, https://, and the local filesystem.",
    )
    parser.add_argument("source", nargs="+", help="what to copy; several when DEST is a directory")
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


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


class Bar:
    """A one-line progress display for a terminal, and nothing else.

    Deliberately not a dependency on ``tqdm``: the callback protocol
    :func:`xrd.copy` takes is ``(done, total)``, so anyone who wants ``tqdm``
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


def _destination(source: XRootDURL, dest: XRootDURL, *, into: bool) -> XRootDURL:
    """``cp f d`` writes ``d``; ``cp f d/`` and ``cp f dir`` write ``dir/f``."""
    if not into:
        return dest
    name = posixpath.basename(source.path.rstrip("/")) or "root"
    return dest / name


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _misuse(args: argparse.Namespace) -> str | None:
    """The flag combinations that cannot mean anything, in words."""
    for check in (_tree_misuse, _count_misuse, _transfer_misuse):
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
    if args.tpc and (args.dry_run or args.remove_source):
        return "--tpc hands the transfer to the servers; --dry-run and --remove-source cannot"
    if args.resume and (args.tpc or args.no_clobber):
        return "--continue needs a partial DEST to carry on from; --tpc and -n forbid one"
    if args.force and args.no_clobber:
        return "-f overwrites DEST and -n refuses to; they cannot both be what you meant"
    return None


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    complaint = _misuse(args)
    if complaint is not None:
        print(f"{PROGRAM}: {complaint}", file=sys.stderr)
        return USAGE
    config = _copy_config(args)
    sources = [parse(s) for s in args.source]
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
        target = _destination(source, dest, into=into)
        bar = Bar(posixpath.basename(source.path.rstrip("/")) or str(source)) if show else None
        try:
            results.extend(_copy_one(source, target, args, config, bar, options))
        finally:
            if bar is not None:
                bar.finish()
    return results


def _copy_options(args: argparse.Namespace) -> _CopyOptions:
    """Keyword options shared by ordinary and recursive copies."""
    options: _CopyOptions = {"overwrite": args.force or args.resume}
    if args.verify is not None:
        options["verify"] = args.verify
    if args.algorithm:
        options["algorithm"] = args.algorithm
    if args.chunk_size:
        options["chunk_size"] = args.chunk_size
    if args.dry_run:
        options["dry_run"] = True
    if args.remove_source:
        options["remove_source"] = True
    if args.resume:
        options["resume"] = True
    return options


def _copy_one(
    source: XRootDURL,
    target: XRootDURL,
    args: argparse.Namespace,
    config: Config,
    bar: Bar | None,
    options: _CopyOptions,
) -> list[CopyResult]:
    """Copy one resolved source using the selected transfer strategy."""
    if args.tpc:
        return [third_party(source, target, config=config, overwrite=args.force)]
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
