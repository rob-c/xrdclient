"""``xrd-fs`` - the remote namespace from a shell.

    $ xrd-fs ls -l root://eos.example.org//store/user/me
    $ xrd-fs stat --json davs://dav.example.org/store/f.root
    $ xrd-fs mkdir -p root://eos.example.org//store/user/me/new/tree
    $ xrd-fs cat root://eos.example.org//store/small.txt | head

Every subcommand takes whole URLs rather than a host plus a path, because
that is what the library takes and what a user already has in hand. Several
URLs on one endpoint share one connection.

The subcommands are those of XrdCl's ``xrdfs`` (``ls``, ``stat``, ``cat``,
``tail``, ``mkdir``, ``rm``, ``rmdir``, ``mv``, ``chmod``, ``truncate``,
``prepare``, ``locate``, ``query``, ``xattr`` ...) plus a few the shell
user expects (``du``, ``touch``, ``readlink``, ``ln``, ``chown``) that some
servers support as vendor extensions, and ``doctor``. ``xrdfs``'s
interactive shell and its ``cd`` are not reproduced: with whole URLs there
is no current directory to change.

Each subcommand is a ``_name(args, endpoints) -> exit code`` function wired
up in :func:`_parser`; :func:`_main` opens the shared :class:`Endpoints` and
turns library errors into exit code ``1``. A subcommand prints its human
output and, alongside it, calls :mod:`xrdclient.report` so ``--json``/``--xml`` get
the same facts as records; with the legacy ``--json`` it prints one
:func:`~xrdcore.report.payload` document instead of the text.
"""

from __future__ import annotations

import argparse
import contextlib
import stat as _stat
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .. import report as output
from ..doctor import diagnose
from ..errors import XRootDError
from ..types import DirEntry, PrepareStatus, StatInfo
from . import (
    ERROR,
    OK,
    Endpoints,
    common_flags,
    config_from,
    confirm,
    fail,
    interactive,
    size_arg,
    stdout_bytes,
    version_flag,
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
    """A modification time in local time, minute resolution; ``-`` when unknown.

    Zero is what a server that does not report times sends, and printing it
    as 1970 would look like data rather than its absence.
    """
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds)) if seconds else "-"


def _long(entry: DirEntry) -> str:
    """One ``ls -l`` line: mode, size, mtime, name.

    An entry listed without stat information (a server that ignores
    ``kXR_dstat``) prints as an empty :class:`StatInfo` rather than costing
    one extra request per entry.
    """
    info = entry.stat or StatInfo()
    return f"{_mode(info)} {info.st_size:>12} {_when(info.st_mtime)} {entry.name}"


def _stat_lines(info: StatInfo) -> list[str]:
    """``stat``'s text output, close to ``xrdfs stat``'s field-per-line layout.

    The flags are printed in their ``repr`` form so the XRootD stat
    flags (``IS_DIR``, ``OFFLINE``, ...) are visible by name, not just the mode.
    """
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
    """``ls [-l] [-R] URL...``: list each directory (``kXR_dirlist`` on root://)."""
    listings = _listings(args, endpoints)
    if args.json or output.current() is not None:
        output.payload(
            {root: [_entry_record(entry) for entry in items] for root, items in listings.items()}
        )
    else:
        _print_listings(listings, args.long)
    return OK


def _listings(args: argparse.Namespace, endpoints: Endpoints) -> dict[str, list[DirEntry]]:
    """Every directory asked for (all of each tree with ``-R``), keyed by path.

    Each listing is recorded as it arrives, so a report from an ``ls -R``
    that fails deep in a tree still holds the directories already listed.
    """
    listings: dict[str, list[DirEntry]] = {}
    for url in args.url:
        filesystem, path = endpoints.at(url)
        if args.recursive:
            for root, _dirs, _files in filesystem.walk(path):
                # walk() has listed ``root`` already but yields only names;
                # the entries ``-l`` prints need their stat, so they cost a
                # second listing per directory.
                listings[root] = filesystem.scandir(root)
                _listing_record(url, root, listings[root])
        else:
            listings[path] = filesystem.scandir(path)
            _listing_record(url, path, listings[path])
    return listings


def _listing_record(url: str, path: str, entries: list[DirEntry]) -> None:
    """One directory's listing as a ``result`` record, when a report is active.

    Checked here rather than left to :func:`~xrdcore.report.record`, because
    building the entry records for a large directory is the expensive part.
    """
    if output.current() is not None:
        output.record(
            operation="ls",
            url=url,
            path=path,
            status="succeeded",
            entries=[_entry_record(entry) for entry in entries],
        )


def _print_listings(listings: dict[str, list[DirEntry]], long: bool) -> None:
    """Print listings the way ``ls`` does: sorted, headed per directory when several.

    Servers return entries in their own order, so sorting happens here, for
    the person reading; the JSON keeps the server's order.
    """
    multiple = len(listings) > 1
    for index, (root, items) in enumerate(listings.items()):
        if multiple:
            # ``ls a b`` separates its sections with a blank line, not a
            # leading one; chr(10) because an f-string expression cannot
            # hold a backslash before Python 3.12.
            print(f"{'' if index == 0 else chr(10)}{root}:")
        for entry in sorted(items, key=lambda e: e.name):
            print(_long(entry) if long else entry.name)


def _entry_record(entry: DirEntry) -> dict[str, Any]:
    """One directory entry for ``--json``; size and mtime ``None`` when unlisted."""
    info = entry.stat
    return {
        "name": entry.name,
        "path": entry.path,
        "size": info.st_size if info else None,
        "dir": entry.is_dir(),
        "mtime": info.st_mtime if info else None,
    }


def _stat_cmd(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """``stat URL...``: what the server reports for each path (``kXR_stat``).

    Every path is stat'ed before anything is printed, so the first failure
    leaves no partial text behind - the error is the whole output.
    """
    found = []
    for url in args.url:
        filesystem, path = endpoints.at(url)
        found.append(filesystem.stat(path))
        output.record(operation="stat", url=url, status="succeeded", value=found[-1])
    if args.json or output.current() is not None:
        output.payload(found)
        return OK
    for url, info in zip(args.url, found, strict=True):
        print(url)
        print("\n".join(_stat_lines(info)))
    return OK


def _cat(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """``cat URL...``: each file's bytes to standard output, unchanged and in order.

    Read in 1 MiB pieces so memory stays flat whatever the file's size; under
    ``--json`` each piece becomes base64 ``content`` records instead.
    """
    out = stdout_bytes()
    for url in args.url:
        filesystem, path = endpoints.at(url)
        with filesystem.open(path, "rb") as handle:
            while chunk := handle.read(1 << 20):
                out.write(chunk)
        output.record(operation="cat", url=url, status="succeeded")
    return OK


def _tail(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """The end of a file, and optionally whatever is appended to it next.

    There is no "read the last N lines" request in either protocol, so this
    reads the last ``--bytes`` (64 KiB by default) and keeps the last
    ``--lines`` of those: a file whose last ten lines are longer than that
    shows fewer. ``-f`` then polls the size every ``--interval`` seconds -
    neither protocol can push a change to a client.
    """
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
    output.record(operation="tail", url=args.url, status="succeeded", follow=args.follow)
    if not args.follow:
        return OK
    # ^C is how a person ends ``tail -f``: success, not an interruption.
    with contextlib.suppress(KeyboardInterrupt):
        _follow(out, _Followed(filesystem, path, args.interval), size)
    return OK


@dataclass(frozen=True, slots=True)
class _Followed:
    """The file ``tail -f`` watches, and how often it looks."""

    #: The endpoint's filesystem, owned by the command's :class:`Endpoints`.
    filesystem: Any
    #: The path on that endpoint.
    path: str
    #: Seconds between polls of the file's size.
    interval: float


def _follow(out: Any, followed: _Followed, offset: int, deadline: float | None = None) -> None:
    """Print what is appended to the followed file until it stops existing.

    ``offset`` is how much of it has been printed already. ``deadline`` is
    what a test uses to get out; a person uses ``^C``, and a file that is
    removed or replaced by a shorter one ends the follow too - the next byte
    at that offset would belong to a different file.
    """
    filesystem, path = followed.filesystem, followed.path
    while deadline is None or time.monotonic() < deadline:
        time.sleep(followed.interval)
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
        output.record(
            operation="du", url=url, status="succeeded", bytes=totals[url][0], files=totals[url][1]
        )
    if args.json or output.current() is not None:
        output.payload({url: {"bytes": s, "files": n} for url, (s, n) in totals.items()})
        return OK
    for url, (size, count) in totals.items():
        print(f"{size:>15} {count:>9} {url}")
    return OK


def _chmod(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """``chmod MODE URL...``: set permission bits (``kXR_chmod`` on root://)."""
    for url in args.url:
        filesystem, path = endpoints.at(url)
        filesystem.chmod(path, args.mode)
        output.record(operation="chmod", url=url, mode=args.mode, status="succeeded")
    return OK


def _truncate(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """``truncate -s N URL...``: resize by path, without opening (``kXR_truncate``)."""
    for url in args.url:
        filesystem, path = endpoints.at(url)
        filesystem.truncate(path, args.size)
        output.record(operation="truncate", url=url, size=args.size, status="succeeded")
    return OK


def _grouped(urls: list[str], endpoints: Endpoints) -> list[tuple[Any, list[str]]]:
    """The paths of these URLs, gathered per endpoint so each is one request."""
    # Keyed by the filesystem's identity: :class:`Endpoints` hands back the
    # same object for every URL on one endpoint, so id() is the endpoint.
    by_endpoint: dict[int, tuple[Any, list[str]]] = {}
    for url in urls:
        filesystem, path = endpoints.at(url)
        by_endpoint.setdefault(id(filesystem), (filesystem, []))[1].append(path)
    return list(by_endpoint.values())


def _prepare(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """Stage files onto disk, or let the server forget them again.

    One ``kXR_prepare`` per endpoint carries all of its paths, which is how
    a tape system wants them: a batch it can order by tape, not a stream of
    single requests. Each staging request yields a handle, printed one per
    line, that ``prepare --status HANDLE`` later asks about.
    """
    work = _grouped(args.url, endpoints)
    if args.status:
        return _prepare_status(args, work)
    handles = []
    for filesystem, paths in work:
        output.identify(
            "evict" if args.evict else "prepare",
            endpoint=getattr(filesystem, "endpoint", None),
            paths=paths,
        )
        if args.evict:
            filesystem.evict(paths)
        else:
            handles.append(filesystem.prepare(paths, priority=args.priority))
        _prepare_record(filesystem, paths, handles, args.evict)
    if args.json or output.current() is not None:
        output.payload(handles)
    elif handles and not args.quiet:
        print("\n".join(handles))
    return OK


def _prepare_record(filesystem: Any, paths: list[str], handles: list[str], evict: bool) -> None:
    """Record one endpoint's prepare or evict; a staging request is only ``queued``."""
    if evict:
        output.record(
            operation="evict",
            endpoint=getattr(filesystem, "endpoint", None),
            paths=paths,
            handle=None,
            status="succeeded",
        )
    else:
        output.record(
            operation="prepare",
            endpoint=getattr(filesystem, "endpoint", None),
            paths=paths,
            handle=handles[-1],
            status="queued",
        )


def _prepare_status(args: argparse.Namespace, work: Iterable[tuple[Any, list[str]]]) -> int:
    """Report how the staging request ``--status`` names is going."""
    reports = []
    for filesystem, paths in work:
        output.identify(
            "prepare_status",
            endpoint=getattr(filesystem, "endpoint", None),
            request_id=args.status,
            paths=paths,
        )
        for status in filesystem.query_prepare(args.status, paths):
            reports.append(status)
            _stage_record(status, "prepare_status")
    if args.json or output.current() is not None:
        output.payload(reports)
    elif not args.quiet:
        for status in reports:
            print(status)
    return OK


def _locality(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """Say where each file is now, without asking for any of it to move."""
    reports = []
    for filesystem, paths in _grouped(args.url, endpoints):
        output.identify("locality", endpoint=getattr(filesystem, "endpoint", None), paths=paths)
        for report in filesystem.archive_info(paths):
            reports.append(report)
            _stage_record(report, "locality")
    if args.json or output.current() is not None:
        output.payload(reports)
    elif not args.quiet:
        for report in reports:
            print(report)
    return OK


def _stage_record(status: PrepareStatus, operation: str) -> None:
    """Record one file's staging state as ``ready``/``queued``/``offline``/``failed``.

    ``prepare --status`` and ``locality`` share this: a file not yet on disk
    is ``queued`` when a staging request is known to be working on it, and
    merely ``offline`` when ``locality`` only asked where it is. A per-file
    error from the server becomes an ``error`` record too, so ``ok`` is
    ``false`` even though the command as a whole succeeded.
    """
    if output.current() is None:
        return
    state = "ready" if status.online else ("offline" if operation == "locality" else "queued")
    if status.error:
        state = "failed"
        output.error(
            RuntimeError(
                "The server could not report this file's storage state. "
                "Check its path and service. Server: " + status.error
            ),
            path=status.path,
        )
    output.record(operation=operation, path=status.path, status=state, value=status)


def _ln(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """``ln [-s] TARGET LINK``: a hard or symbolic link, as ``ln`` orders its operands.

    Not part of the XRootD protocol proper: servers that support it do so as
    a vendor extension, and a server that does not answers with an error.
    """
    filesystem, target = endpoints.at(args.target)
    other, link = endpoints.at(args.link)
    output.identify("ln", source=args.target, target=args.link, symbolic=args.symbolic)
    if other is not filesystem:
        # A link is a name in one namespace; there is no request that could
        # make one server's entry point into another's.
        raise ValueError("a link and its target live on one endpoint")
    if args.symbolic:
        filesystem.symlink(target, link)
    else:
        filesystem.link(target, link)
    output.record(
        operation="ln",
        source=args.target,
        target=args.link,
        symbolic=args.symbolic,
        status="succeeded",
    )
    return OK


def _readlink(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """``readlink URL...``: where each symbolic link points, as the server stored it."""
    targets = {}
    for url in args.url:
        filesystem, path = endpoints.at(url)
        targets[url] = filesystem.readlink(path)
        output.record(operation="readlink", url=url, status="succeeded", value=targets[url])
    if args.json or output.current() is not None:
        output.payload(targets)
        return OK
    for target in targets.values():
        print(target)
    return OK


def _checksum(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """``checksum [-a NAME] URL...``: the server's checksum of each file.

    Computed by the server (``kXR_query`` ``kXR_Qcksum`` on root://, ``Digest``
    on HTTP), so nothing is downloaded. One file prints ``ALGORITHM VALUE``
    like ``xrdfs query checksum``; several print ``VALUE  URL``, the
    ``md5sum`` layout, so the output can be compared line by line.
    """
    results = []
    for url in args.url:
        filesystem, path = endpoints.at(url)
        results.append((url, filesystem.checksum(path, args.algorithm)))
        output.record(operation="checksum", url=url, status="succeeded", value=results[-1][1])
    if args.json or output.current() is not None:
        output.payload(dict(results))
        return OK
    for url, info in results:
        print(f"{info.value}  {url}" if len(results) > 1 else f"{info.algorithm} {info.value}")
    return OK


def _mkdir(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """``mkdir [-p] URL...``: as ``mkdir -p``, ``-p`` also accepts what already exists."""
    for url in args.url:
        filesystem, path = endpoints.at(url)
        filesystem.mkdir(path, parents=args.parents, exist_ok=args.parents)
        output.record(operation="mkdir", url=url, status="succeeded")
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
    """``rm [-r] [-f] [--yes] URL...``: remove each, carrying on past failures.

    Like ``rm``, one URL that cannot be removed does not stop the others; the
    exit code is ``1`` if any failed. That is unlike the other subcommands,
    which stop at the first error, because a half-finished removal is what a
    person would otherwise have to redo by hand.
    """
    code = OK
    for url in args.url:
        filesystem, path = endpoints.at(url)
        if not _remove_one(args, filesystem, url, path):
            code = ERROR
        else:
            output.record(operation="rm", url=url, status="succeeded")
    return code


def _remove_one(args: argparse.Namespace, filesystem: Any, url: str, path: str) -> bool:
    """Remove one path; ``False`` (with the error printed) when that failed.

    ``-f`` makes a failure count as success, silently. That is broader than
    ``rm -f``, which only forgives a missing file: here any refusal from the
    server is forgiven, so ``-f`` in a script means "best effort".
    """
    try:
        if args.recursive:
            return _remove_tree(args, filesystem, url, path)
        filesystem.remove(path)
    except (XRootDError, OSError) as exc:
        return bool(args.force) or fail(PROGRAM, exc) == OK
    return True


def _remove_tree(args: argparse.Namespace, filesystem: Any, url: str, path: str) -> bool:
    """``rm -r`` of one tree, behind the shallow-path refusal and the question.

    A "no" to the question is not a failure: nothing was asked of the server,
    and the exit code says only whether what was attempted worked.
    """
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
    """``rmdir URL...``: remove empty directories (``kXR_rmdir``); non-empty is an error."""
    for url in args.url:
        filesystem, path = endpoints.at(url)
        filesystem.rmdir(path)
        output.record(operation="rmdir", url=url, status="succeeded")
    return OK


def _mv(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """``mv SOURCE DEST``: a rename on the server (``kXR_mv``), never a copy.

    Moving between endpoints would be a copy and a delete, with a window in
    which the data exists twice or not at all; that is ``xrd-cp
    --remove-source``, which verifies before it deletes.
    """
    filesystem, source = endpoints.at(args.source)
    other, target = endpoints.at(args.dest)
    output.identify("mv", source=args.source, target=args.dest)
    if other is not filesystem:
        raise ValueError("mv works within one endpoint; use xrd-cp between servers")
    filesystem.rename(source, target)
    output.record(operation="mv", source=args.source, target=args.dest, status="succeeded")
    return OK


def _touch(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """``touch [--time T] URL...``: create empty files, optionally setting their times.

    ``--time`` needs the server to support setting times, which XRootD does
    only as a vendor extension; a server without it refuses the request.
    """
    for url in args.url:
        filesystem, path = endpoints.at(url)
        filesystem.touch(path)
        if args.time is not None:
            filesystem.utime(path, None if args.time == "now" else (args.time, args.time))
        output.record(operation="touch", url=url, status="succeeded")
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
    """``chown OWNER URL...``: numeric owner change, a vendor extension; ``-1`` keeps one."""
    uid, gid = args.owner
    for url in args.url:
        filesystem, path = endpoints.at(url)
        filesystem.chown(path, uid, gid)
        output.record(operation="chown", url=url, uid=uid, gid=gid, status="succeeded")
    return OK


def _df(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """``df URL``: space and staging utilisation, as XRootD's ``statvfs`` reports it.

    The figures are per *node set* (read/write and staging servers behind a
    manager), in MB, because that is the shape of the ``kXR_stat`` ``kXR_vfs``
    answer; it is not a POSIX ``statvfs``.
    """
    filesystem, path = endpoints.at(args.url)
    info = filesystem.statvfs(path)
    if args.json or output.current() is not None:
        output.payload(info)
        return OK
    print(f"  Read/write nodes: {info.nodes_rw}")
    print(f"  Read/write free:  {info.free_rw} MB ({info.utilization_rw}% used)")
    print(f"  Staging nodes:    {info.nodes_staging}")
    print(f"  Staging free:     {info.free_staging} MB ({info.utilization_staging}% used)")
    return OK


def _locate(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """``locate [--deep] [--create] URL``: which servers hold a path (``kXR_locate``).

    ``--deep`` asks each manager in the answer again until only data servers
    are left, as ``xrdfs locate -d`` does. Prints ``address type access``.
    """
    filesystem, path = endpoints.at(args.url)
    places = (
        filesystem.deep_locate(path, create=args.create)
        if args.deep
        else filesystem.locate(path, create=args.create)
    )
    if args.json or output.current() is not None:
        output.payload(places)
        return OK
    for place in places:
        print(f"{place.address} {place.type} {place.access}")
    return OK


def _ping(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """``ping URL``: one ``kXR_ping`` round trip, timed in milliseconds.

    Connections open lazily, so on a fresh endpoint the connect and login
    happen inside the timed span: the figure is "how long until the endpoint
    answered", not the bare network round trip.
    """
    filesystem, _path = endpoints.at(args.url)
    started = time.monotonic()
    filesystem.ping()
    elapsed = (time.monotonic() - started) * 1e3
    if args.json or output.current() is not None:
        output.payload({"endpoint": filesystem.endpoint, "ms": round(elapsed, 3)})
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
    if args.json or output.current() is not None:
        output.payload({"url": report.url, "ok": report.ok, "checks": report.to_dict()})
    elif not args.quiet:
        print(report)
    return OK if report.ok else ERROR


def _query(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """``query URL NAME...``: server configuration values (``kXR_query`` ``kXR_Qconfig``).

    Prints ``NAME VALUE`` per name, in the order asked; a name the server
    does not know prints with an empty value rather than failing the rest.
    """
    filesystem, _path = endpoints.at(args.url)
    values = filesystem.query_config(*args.name)
    if args.json or output.current() is not None:
        output.payload(values)
        return OK
    for name in args.name:
        print(f"{name} {values.get(name, '')}")
    return OK


def _xattr(args: argparse.Namespace, endpoints: Endpoints) -> int:
    """``xattr URL [--set N=V | --remove N | -r]``: extended attributes (``kXR_fattr``).

    With neither ``--set`` nor ``--remove`` it lists; ``-r`` lists names only,
    for a whole tree. ``--set`` takes the value as text and stores it UTF-8.
    """
    filesystem, path = endpoints.at(args.url)
    if args.set is not None:
        # Split on the first "=" only: a value may contain "=" itself.
        name, _, value = args.set.partition("=")
        filesystem.setxattr(path, name, value.encode())
        output.record(operation="xattr_set", url=args.url, name=name, status="succeeded")
        return OK
    if args.remove is not None:
        filesystem.removexattr(path, args.remove)
        output.record(operation="xattr_remove", url=args.url, name=args.remove, status="succeeded")
        return OK
    if args.recursive:
        return _xattr_tree(args, filesystem, path)
    return _xattr_show(args, filesystem, path)


def _xattr_tree(args: argparse.Namespace, filesystem: Any, path: str) -> int:
    """``xattr -r``: ``PATH: NAME`` lines for every attribute under a directory."""
    tree = filesystem.listxattr_tree(path)
    if args.json or output.current() is not None:
        output.payload(tree)
        return OK
    for name, names in tree.items():
        for attribute in names:
            print(f"{name}: {attribute}")
    return OK


def _xattr_show(args: argparse.Namespace, filesystem: Any, path: str) -> int:
    """``NAME=VALUE`` lines for one path; values are bytes, shown as lossy UTF-8.

    The versioned ``--output-format json`` report keeps the exact bytes
    (base64); the legacy ``--json`` document decodes them as this does.
    """
    attributes = filesystem.xattrs(path)
    if args.json or output.current() is not None:
        output.payload(attributes)
        return OK
    for name, value in attributes.items():
        print(f"{name}={value.decode('utf-8', 'replace')}")
    return OK


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    """The ``xrd-fs`` parser: one subparser per subcommand, its handler attached.

    The output flags are added at both levels, so ``xrd-fs --json ls URL``
    and ``xrd-fs ls --json URL`` both work; the shared credential flags only
    on the subcommands, where argparse reports them in the subcommand's help.
    """
    parser = output.Parser(prog=PROGRAM, description="Inspect and change a remote namespace.")
    version_flag(parser)
    output.flags(parser, legacy_json=True)
    subs = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name: str, handler: Callable[..., int], help_text: str) -> argparse.ArgumentParser:
        """Add subcommand ``name``, run by ``handler``, with the common flags."""
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
    # Octal digits only: a symbolic mode (u+x) is relative to the current
    # mode, which would cost a stat per path before every change.
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
    """The ``xrd-fs`` entry point: run with ``argv`` (default ``sys.argv[1:]``).

    Returns the exit code rather than exiting, so tests and embedding
    programs can call it; the console script wrapper does the exit.
    """
    return output.run_cli(PROGRAM, argv, lambda: _main(argv), legacy_json=True)


def _main(argv: Sequence[str] | None = None) -> int:
    """Parse, run the chosen subcommand on shared endpoints, map errors to ``1``.

    Only the library's own failure types are caught and printed as a one-line
    message; anything else is a bug and propagates as a traceback (or, under
    ``--json``, an ``error`` record) rather than being dressed up as a server
    refusal.
    """
    args = _parser().parse_args(argv)
    report = output.current()
    if report is not None:
        # The document's ``command`` is the subcommand (``ls``), not the tool.
        report.command = args.command
    output.identify(args.command)
    config = config_from(args)
    try:
        with Endpoints(config) as endpoints:
            return int(args.handler(args, endpoints))
    except (XRootDError, OSError, ValueError) as exc:
        return fail(PROGRAM, exc)


if __name__ == "__main__":
    raise SystemExit(main())
