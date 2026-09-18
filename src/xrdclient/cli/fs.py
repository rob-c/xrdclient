"""``xrd-fs`` - the remote namespace from a shell.

    $ xrd-fs ls -l root://eos.example.org//store/user/me
    $ xrd-fs stat --json davs://dav.example.org/store/f.root
    $ xrd-fs mkdir -p root://eos.example.org//store/user/me/new/tree
    $ xrd-fs cat root://eos.example.org//store/small.txt | head

Every subcommand takes whole URLs rather than a host plus a path, because
that is what the library takes and what a user already has in hand. Several
URLs on one endpoint share one connection.
"""

from __future__ import annotations

import argparse
import stat as _stat
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from .._compat import zip_strict
from ..doctor import diagnose
from ..errors import XRootDError
from ..types import DirEntry, StatInfo
from . import (
    ERROR,
    OK,
    Endpoints,
    common_flags,
    config_from,
    confirm,
    dumps,
    fail,
    interactive,
    size_arg,
    stdout_bytes,
)

__all__ = ["main"]

PROGRAM = "xrd-fs"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _mode(info: StatInfo) -> str:
    """``drwxr-xr-x``-shaped, from whatever flags the endpoint gave us."""
    return _stat.filemode(info.st_mode)


def _when(seconds: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds)) if seconds else "-"


def _long(entry: DirEntry) -> str:
    info = entry.stat or StatInfo()
    return f"{_mode(info)} {info.st_size:>12} {_when(info.st_mtime)} {entry.name}"


def _stat_lines(info: StatInfo) -> list[str]:
    return [
        f"  Path:  {info.path}",
        f"  Id:    {info.id}",
        f"  Size:  {info.st_size}",
        f"  Mode:  {_mode(info)} ({info.flags!r})",
        f"  MTime: {_when(info.st_mtime)}",
    ]


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------
#
# Each takes the parsed arguments and an open :class:`Endpoints`, prints what
# it has to print, and returns an exit code. They are ordinary functions so
# they can be called from a test without going through argv.


def _ls(args: argparse.Namespace, endpoints: Endpoints) -> int:
    listings = _listings(args, endpoints)
    if args.json:
        print(
            dumps(
                {
                    root: [_entry_record(entry) for entry in items]
                    for root, items in listings.items()
                }
            )
        )
    else:
        _print_listings(listings, args.long)
    return OK


def _listings(args: argparse.Namespace, endpoints: Endpoints) -> dict[str, list[DirEntry]]:
    listings: dict[str, list[DirEntry]] = {}
    for url in args.url:
        filesystem, path = endpoints.at(url)
        if args.recursive:
            for root, _dirs, _files in filesystem.walk(path):
                listings[root] = filesystem.scandir(root)
        else:
            listings[path] = filesystem.scandir(path)
    return listings


def _print_listings(listings: dict[str, list[DirEntry]], long: bool) -> None:
    multiple = len(listings) > 1
    for index, (root, items) in enumerate(listings.items()):
        if multiple:
            print(f"{'' if index == 0 else chr(10)}{root}:")
        for entry in sorted(items, key=lambda e: e.name):
            print(_long(entry) if long else entry.name)


def _entry_record(entry: DirEntry) -> dict[str, Any]:
    info = entry.stat
    return {
        "name": entry.name,
        "path": entry.path,
        "size": info.st_size if info else None,
        "dir": entry.is_dir(),
        "mtime": info.st_mtime if info else None,
    }


def _stat_cmd(args: argparse.Namespace, endpoints: Endpoints) -> int:
    found = []
    for url in args.url:
        filesystem, path = endpoints.at(url)
        found.append(filesystem.stat(path))
    if args.json:
        print(dumps(found))
        return OK
    for url, info in zip_strict(args.url, found):
        print(url)
        print("\n".join(_stat_lines(info)))
    return OK


def _cat(args: argparse.Namespace, endpoints: Endpoints) -> int:
    out = stdout_bytes()
    for url in args.url:
        filesystem, path = endpoints.at(url)
        with filesystem.open(path, "rb") as handle:
            while chunk := handle.read(1 << 20):
                out.write(chunk)
    return OK


def _tail(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """The end of a file, and optionally whatever is appended to it next."""
    filesystem, path = endpoints.at(args.url)
    out = stdout_bytes()
    size = filesystem.getsize(path)
    start = max(0, size - args.bytes)
    with filesystem.open(path, "rb") as handle:
        handle.seek(start)
        tail = handle.read()
    if start:  # a partial first line is noise, not data
        tail = tail.partition(b"\n")[2]
    lines = tail.splitlines(keepends=True)
    out.write(b"".join(lines[max(0, len(lines) - args.lines) :]))
    out.flush()
    if not args.follow:
        return OK
    try:
        _follow(out, filesystem, path, size, args.interval)
    except KeyboardInterrupt:
        pass
    return OK


def _follow(
    out: Any,
    filesystem: Any,
    path: str,
    offset: int,
    interval: float,
    deadline: float | None = None,
) -> None:
    """Print what is appended to ``path`` until it stops existing.

    ``deadline`` is what a test uses to get out; a person uses ``^C``, and a
    file that is removed or replaced by a shorter one ends the follow too -
    the next byte at that offset would belong to a different file.
    """
    while deadline is None or time.monotonic() < deadline:
        time.sleep(interval)
        try:
            size = filesystem.getsize(path)
        except (XRootDError, OSError):
            return
        if size < offset:
            return
        if size > offset:
            with filesystem.open(path, "rb") as handle:
                handle.seek(offset)
                out.write(handle.read())
            out.flush()
            offset = size


def _du_tree(filesystem: Any, path: str) -> tuple[int, int]:
    """Bytes and files under ``path``, counted from the listing itself.

    A directory listing already carries the sizes, so a tree costs one request
    per directory rather than one per file. A server that lists without them
    costs one stat per entry instead, which is the price of its terseness.
    """
    size = count = 0
    for entry in filesystem.scandir(path):
        info = entry.stat or filesystem.stat(entry.path)
        if info.is_dir():
            below = _du_tree(filesystem, entry.path)
            size, count = size + below[0], count + below[1]
        else:
            size += info.st_size
            count += 1
    return size, count


def _du(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """What a tree costs, in bytes and in files."""
    totals: dict[str, tuple[int, int]] = {}
    for url in args.url:
        filesystem, path = endpoints.at(url)
        totals[url] = (
            _du_tree(filesystem, path) if filesystem.isdir(path) else (filesystem.getsize(path), 1)
        )
    if args.json:
        print(dumps({url: {"bytes": s, "files": n} for url, (s, n) in totals.items()}))
        return OK
    for url, (size, count) in totals.items():
        print(f"{size:>15} {count:>9} {url}")
    return OK


def _chmod(args: argparse.Namespace, endpoints: Endpoints) -> int:
    for url in args.url:
        filesystem, path = endpoints.at(url)
        filesystem.chmod(path, args.mode)
    return OK


def _truncate(args: argparse.Namespace, endpoints: Endpoints) -> int:
    for url in args.url:
        filesystem, path = endpoints.at(url)
        filesystem.truncate(path, args.size)
    return OK


def _grouped(urls: list[str], endpoints: Endpoints) -> list[tuple[Any, list[str]]]:
    """The paths of these URLs, gathered per endpoint so each is one request."""
    by_endpoint: dict[int, tuple[Any, list[str]]] = {}
    for url in urls:
        filesystem, path = endpoints.at(url)
        by_endpoint.setdefault(id(filesystem), (filesystem, []))[1].append(path)
    return list(by_endpoint.values())


def _prepare(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """Stage files onto disk, or let the server forget them again."""
    work = _grouped(args.url, endpoints)
    if args.status:
        return _prepare_status(args, work)
    handles = []
    for filesystem, paths in work:
        if args.evict:
            filesystem.evict(paths)
        else:
            handles.append(filesystem.prepare(paths, priority=args.priority))
    if args.json:
        print(dumps(handles))
    elif handles and not args.quiet:
        print("\n".join(handles))
    return OK


def _prepare_status(args: argparse.Namespace, work: Iterable[tuple[Any, list[str]]]) -> int:
    """Report how the staging request ``--status`` names is going."""
    reports = [
        status
        for filesystem, paths in work
        for status in filesystem.query_prepare(args.status, paths)
    ]
    if args.json:
        print(dumps(reports))
    elif not args.quiet:
        for status in reports:
            print(status)
    return OK


def _locality(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """Say where each file is now, without asking for any of it to move."""
    reports = [
        report
        for filesystem, paths in _grouped(args.url, endpoints)
        for report in filesystem.archive_info(paths)
    ]
    if args.json:
        print(dumps(reports))
    elif not args.quiet:
        for report in reports:
            print(report)
    return OK


def _ln(args: argparse.Namespace, endpoints: Endpoints) -> int:
    filesystem, target = endpoints.at(args.target)
    other, link = endpoints.at(args.link)
    if other is not filesystem:
        raise ValueError("a link and its target live on one endpoint")
    if args.symbolic:
        filesystem.symlink(target, link)
    else:
        filesystem.link(target, link)
    return OK


def _readlink(args: argparse.Namespace, endpoints: Endpoints) -> int:
    targets = {}
    for url in args.url:
        filesystem, path = endpoints.at(url)
        targets[url] = filesystem.readlink(path)
    if args.json:
        print(dumps(targets))
        return OK
    for target in targets.values():
        print(target)
    return OK


def _checksum(args: argparse.Namespace, endpoints: Endpoints) -> int:
    results = []
    for url in args.url:
        filesystem, path = endpoints.at(url)
        results.append((url, filesystem.checksum(path, args.algorithm)))
    if args.json:
        print(dumps(dict(results)))
        return OK
    for url, info in results:
        print(f"{info.value}  {url}" if len(results) > 1 else f"{info.algorithm} {info.value}")
    return OK


def _mkdir(args: argparse.Namespace, endpoints: Endpoints) -> int:
    for url in args.url:
        filesystem, path = endpoints.at(url)
        filesystem.mkdir(path, parents=args.parents, exist_ok=args.parents)
    return OK


def _too_shallow(path: str) -> bool:
    """Is this a whole namespace, or the top of somebody's, rather than a tree?

    ``/``, ``/store`` and ``/eos`` are the paths a slip of the shell produces;
    nothing under two components deep is ever what ``rm -r`` meant to say.
    """
    return len([part for part in path.split("/") if part]) < 2


def _agreed(args: argparse.Namespace, filesystem: Any, url: str, path: str) -> bool:
    """Ask before deleting a tree, when there is somebody there to ask.

    The count comes from one listing of the top of it, which is what makes the
    question worth asking: "remove this and the 400 entries under it" is the
    sentence that stops the wrong ``rm -r``.
    """
    if args.yes or not interactive():
        return True
    entries = len(filesystem.listdir(path))
    return confirm(f"{PROGRAM}: remove {url} and the {entries} entries under it?")


def _rm(args: argparse.Namespace, endpoints: Endpoints) -> int:
    code = OK
    for url in args.url:
        filesystem, path = endpoints.at(url)
        if not _remove_one(args, filesystem, url, path):
            code = ERROR
    return code


def _remove_one(args: argparse.Namespace, filesystem: Any, url: str, path: str) -> bool:
    try:
        if args.recursive:
            return _remove_tree(args, filesystem, url, path)
        filesystem.remove(path)
    except (XRootDError, OSError) as exc:
        return bool(args.force) or fail(PROGRAM, exc) == OK
    return True


def _remove_tree(args: argparse.Namespace, filesystem: Any, url: str, path: str) -> bool:
    if _too_shallow(path) and not args.yes:
        print(
            f"{PROGRAM}: {url} is the top of a namespace rather than a tree to "
            f"delete; say --yes if that really is what you mean",
            file=sys.stderr,
        )
        return False
    if _agreed(args, filesystem, url, path):
        filesystem.rmtree(path)
    return True


def _rmdir(args: argparse.Namespace, endpoints: Endpoints) -> int:
    for url in args.url:
        filesystem, path = endpoints.at(url)
        filesystem.rmdir(path)
    return OK


def _mv(args: argparse.Namespace, endpoints: Endpoints) -> int:
    filesystem, source = endpoints.at(args.source)
    other, target = endpoints.at(args.dest)
    if other is not filesystem:
        raise ValueError("mv works within one endpoint; use xrd-cp between servers")
    filesystem.rename(source, target)
    return OK


def _touch(args: argparse.Namespace, endpoints: Endpoints) -> int:
    for url in args.url:
        filesystem, path = endpoints.at(url)
        filesystem.touch(path)
        if args.time is not None:
            filesystem.utime(path, None if args.time == "now" else (args.time, args.time))
    return OK


def _owner(text: str) -> tuple[int, int]:
    """``uid``, ``uid:gid`` or ``:gid``, numeric - as ``chown`` takes them.

    Numeric only: the names belong to the server's passwd file, and this
    machine's is a different one that happens to be nearby.
    """
    uid, _, gid = text.partition(":")
    try:
        return int(uid) if uid else -1, int(gid) if gid else -1
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r}: expected uid, uid:gid or :gid") from None


def _chown(args: argparse.Namespace, endpoints: Endpoints) -> int:
    uid, gid = args.owner
    for url in args.url:
        filesystem, path = endpoints.at(url)
        filesystem.chown(path, uid, gid)
    return OK


def _df(args: argparse.Namespace, endpoints: Endpoints) -> int:
    filesystem, path = endpoints.at(args.url)
    info = filesystem.statvfs(path)
    if args.json:
        print(dumps(info))
        return OK
    print(f"  Read/write nodes: {info.nodes_rw}")
    print(f"  Read/write free:  {info.free_rw} MB ({info.utilization_rw}% used)")
    print(f"  Staging nodes:    {info.nodes_staging}")
    print(f"  Staging free:     {info.free_staging} MB ({info.utilization_staging}% used)")
    return OK


def _locate(args: argparse.Namespace, endpoints: Endpoints) -> int:
    filesystem, path = endpoints.at(args.url)
    places = (
        filesystem.deep_locate(path, create=args.create)
        if args.deep
        else filesystem.locate(path, create=args.create)
    )
    if args.json:
        print(dumps(places))
        return OK
    for place in places:
        print(f"{place.address} {place.type} {place.access}")
    return OK


def _ping(args: argparse.Namespace, endpoints: Endpoints) -> int:
    filesystem, _path = endpoints.at(args.url)
    started = time.monotonic()
    filesystem.ping()
    elapsed = (time.monotonic() - started) * 1e3
    if args.json:
        print(dumps({"endpoint": filesystem.endpoint, "ms": round(elapsed, 3)}))
    elif not args.quiet:
        print(f"{filesystem.endpoint} responded in {elapsed:.1f} ms")
    return OK


def _doctor(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """Say what is wrong before a transfer has to fail to say it.

    This one takes no endpoint from the pool: it opens its own connection so
    that a login failure is a line in the report rather than an exception out
    of here.
    """
    report = diagnose(args.url or "", config=config_from(args))
    if args.json:
        print(dumps({"url": report.url, "ok": report.ok, "checks": report.to_dict()}))
    elif not args.quiet:
        print(report)
    return OK if report.ok else ERROR


def _query(args: argparse.Namespace, endpoints: Endpoints) -> int:
    filesystem, _path = endpoints.at(args.url)
    values = filesystem.query_config(*args.name)
    if args.json:
        print(dumps(values))
        return OK
    for name in args.name:
        print(f"{name} {values.get(name, '')}")
    return OK


def _xattr(args: argparse.Namespace, endpoints: Endpoints) -> int:
    filesystem, path = endpoints.at(args.url)
    if args.set is not None:
        name, _, value = args.set.partition("=")
        filesystem.setxattr(path, name, value.encode())
        return OK
    if args.remove is not None:
        filesystem.removexattr(path, args.remove)
        return OK
    if args.recursive:
        tree = filesystem.listxattr_tree(path)
        if args.json:
            print(dumps(tree))
            return OK
        for name, names in tree.items():
            for attribute in names:
                print(f"{name}: {attribute}")
        return OK
    attributes = filesystem.xattrs(path)
    if args.json:
        print(dumps(attributes))
        return OK
    for name, value in attributes.items():
        print(f"{name}={value.decode('utf-8', 'replace')}")
    return OK


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM, description="Inspect and change a remote namespace."
    )
    subs = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name: str, handler: Callable[..., int], help_text: str) -> argparse.ArgumentParser:
        sub = subs.add_parser(name, help=help_text, description=help_text)
        sub.set_defaults(handler=handler)
        common_flags(sub)
        return sub

    ls = command("ls", _ls, "list a directory")
    ls.add_argument("url", nargs="+")
    ls.add_argument("-l", "--long", action="store_true", help="one entry per line, with detail")
    ls.add_argument("-R", "--recursive", action="store_true", help="descend into subdirectories")

    st = command("stat", _stat_cmd, "show what the server knows about a path")
    st.add_argument("url", nargs="+")

    cat = command("cat", _cat, "write a file to standard output")
    cat.add_argument("url", nargs="+")

    tail = command("tail", _tail, "the end of a file, optionally as it grows")
    tail.add_argument("url")
    tail.add_argument("-n", "--lines", type=int, default=10, help="how many lines (default 10)")
    tail.add_argument("-f", "--follow", action="store_true", help="keep printing what is appended")
    tail.add_argument(
        "--interval", type=float, default=1.0, metavar="SECONDS", help="how often to look"
    )
    tail.add_argument(
        "--bytes",
        type=size_arg,
        default=1 << 16,
        metavar="N",
        help="how much of the end to fetch looking for those lines",
    )

    du = command("du", _du, "how much space a tree uses")
    du.add_argument("url", nargs="+")

    chmod = command("chmod", _chmod, "change the mode of a path")
    chmod.add_argument("mode", type=lambda text: int(text, 8), help="octal, e.g. 750")
    chmod.add_argument("url", nargs="+")

    truncate = command("truncate", _truncate, "resize a file without opening it")
    truncate.add_argument("-s", "--size", type=int, required=True, help="new length in bytes")
    truncate.add_argument("url", nargs="+")

    prepare = command("prepare", _prepare, "stage files onto disk, or evict them")
    prepare.add_argument("url", nargs="+")
    prepare.add_argument("--evict", action="store_true", help="drop cached copies instead")
    prepare.add_argument("--priority", type=int, default=0, help="0 to 3, higher is sooner")
    prepare.add_argument(
        "--status",
        metavar="HANDLE",
        help="report on the request this handle names instead of making one",
    )

    locality = command("locality", _locality, "say whether files are on disk or on tape")
    locality.add_argument("url", nargs="+")

    ln = command("ln", _ln, "link one path to another (vendor extension)")
    ln.add_argument("target")
    ln.add_argument("link")
    ln.add_argument("-s", "--symbolic", action="store_true", help="symbolic rather than hard")

    readlink = command("readlink", _readlink, "what a symbolic link points at")
    readlink.add_argument("url", nargs="+")

    cks = command("checksum", _checksum, "ask the server for a checksum")
    cks.add_argument("url", nargs="+")
    cks.add_argument("-a", "--algorithm", metavar="NAME", help="adler32, md5, crc32c, ...")

    mkdir = command("mkdir", _mkdir, "create a directory")
    mkdir.add_argument("url", nargs="+")
    mkdir.add_argument("-p", "--parents", action="store_true", help="create missing parents")

    rm = command("rm", _rm, "remove files")
    rm.add_argument("url", nargs="+")
    rm.add_argument("-r", "--recursive", action="store_true", help="remove a directory tree")
    rm.add_argument("-f", "--force", action="store_true", help="ignore what is not there")
    rm.add_argument(
        "--yes",
        action="store_true",
        help="with -r, do not ask and do not refuse a shallow path: the answer is yes",
    )

    rmdir = command("rmdir", _rmdir, "remove an empty directory")
    rmdir.add_argument("url", nargs="+")

    mv = command("mv", _mv, "rename within one endpoint")
    mv.add_argument("source")
    mv.add_argument("dest")

    touch = command("touch", _touch, "create an empty file")
    touch.add_argument("url", nargs="+")
    touch.add_argument(
        "--time",
        type=lambda text: text if text == "now" else float(text),
        help="also set the times: 'now' or epoch seconds (a vendor extension)",
    )

    chown = command("chown", _chown, "change the owner of a path (a vendor extension)")
    chown.add_argument("owner", type=_owner, help="uid, uid:gid or :gid, numeric")
    chown.add_argument("url", nargs="+")

    df = command("df", _df, "space and utilisation")
    df.add_argument("url")

    locate = command("locate", _locate, "which servers hold a path")
    locate.add_argument("url")
    locate.add_argument("--deep", action="store_true", help="follow managers down to the data")
    locate.add_argument(
        "--create",
        action="store_true",
        help="where the file would go, rather than where it is",
    )

    ping = command("ping", _ping, "is the endpoint answering")
    ping.add_argument("url")

    doctor = command("doctor", _doctor, "check everything a transfer needs, and say what is wrong")
    doctor.add_argument(
        "url", nargs="?", help="an endpoint or a whole path; without one, this machine alone"
    )

    query = command("query", _query, "read server configuration values")
    query.add_argument("url")
    query.add_argument("name", nargs="+")

    xattr = command("xattr", _xattr, "extended attributes")
    xattr.add_argument("url")
    xattr.add_argument("--set", metavar="NAME=VALUE", help="set one attribute")
    xattr.add_argument("--remove", metavar="NAME", help="remove one attribute")
    xattr.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="names only, for every file under a directory (a vendor extension)",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = config_from(args)
    try:
        with Endpoints(config) as endpoints:
            return int(args.handler(args, endpoints))
    except (XRootDError, OSError, ValueError) as exc:
        return fail(PROGRAM, exc)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
