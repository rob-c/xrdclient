"""Recursive copies: walking a tree, selecting and pruning its files, and
running several of them at once.

:func:`~xrdclient.copy.engine.copy_tree` is the public entry point and stays in
the engine, because it calls :func:`~xrdclient.copy.engine.copy` once per file
(through the engine's globals, which is what lets a test replace it) and asks
:func:`~xrdclient.copy.engine._up_to_date` whether a sync can skip one. What
lives here is the tree machinery around those calls, none of which knows how a
single file moves: :func:`_walk` and :func:`_selected` choose the files,
:func:`_each` / :func:`_in_order` run them with :class:`_Total` summing their
progress, :func:`_prune` implements ``delete=True``, and
:func:`_make_tree_dirs` makes a WebDAV target's directories once, up front.
"""

from __future__ import annotations

import functools
import os
import threading
from collections.abc import Callable, Iterator, Sequence
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

from xrdcore.fanout import run_all

from ..config import Config
from ..log import get_logger
from ..url import XRootDURL
from ._endpoints import _remove

if TYPE_CHECKING:
    from .engine import CopyResult, Progress, SyncMode

# The engine's logger, not this module's: ``deleting ...`` was always the copy
# engine's message, and a logging configuration that names it should keep it.
_log = get_logger("xrdclient.copy.engine")


def _selected(rel: str, include: Sequence[str], exclude: Sequence[str]) -> bool:
    """``rsync``'s rule: an explicit include list is a whitelist, exclude wins."""
    if include and not any(fnmatch(rel, pattern) for pattern in include):
        return False
    return not any(fnmatch(rel, pattern) for pattern in exclude)


def _prune(target: XRootDURL, config: Config, keep: set[str], *, dry_run: bool) -> list[str]:
    """Remove everything under ``target`` that ``keep`` does not name.

    Each removal is logged, because a deletion nobody asked for by name is
    the one thing in a copy worth being able to read back afterwards.
    """
    removed = []
    for rel in _walk(target, config):
        if rel in keep:
            continue
        removed.append(rel)
        _log.info("%s %s", "would delete" if dry_run else "deleting", target / rel)
        if not dry_run:
            _remove(target / rel, config)
    return removed


def _make_tree_dirs(target: XRootDURL, wanted: Sequence[str], config: Config) -> None:
    """Make every directory a WebDAV tree copy will write into, before any file.

    Each file's :func:`~xrdclient.copy.engine.copy` could make its own parent
    (:func:`~xrdclient.copy._endpoints._make_parent_collection`), but that is a
    fresh connection and an ``MKCOL`` per file; the directories are few and
    shared, so they are made once here, shallowest first, over one connection,
    and the tree then copies its files inside
    :func:`~xrdclient.copy._endpoints._parents_already_made`. Only a WebDAV
    target needs it - ``root://`` asks for ``kXR_mkpath`` and a local tree is
    made with ``os.makedirs`` as each file goes - so for those it does nothing.
    The caller skips it for a dry run, which must not touch the target.
    """
    if not target.is_http or not wanted:
        return
    base = target.path.rstrip("/")
    directories = {""}  # the target directory itself, which may not exist yet
    for rel in wanted:
        current = os.path.dirname(rel.strip("/"))
        while current:
            directories.add(current)
            current = os.path.dirname(current)
    from ..client import FileSystem

    with FileSystem(target.with_path("/"), config) as fs:
        for relative in sorted(directories, key=lambda d: d.count("/")):
            fs.mkdir(base + ("/" + relative if relative else ""), parents=True, exist_ok=True)


def _walk(url: XRootDURL, config: Config) -> Iterator[str]:
    """Every file under ``url``, as paths relative to it."""
    if url.is_local:
        for root, _, names in os.walk(url.path):
            rel = os.path.relpath(root, url.path)
            for name in names:
                yield name if rel == "." else os.path.join(rel, name)
        return
    from ..client import FileSystem

    with FileSystem(url.with_path("/"), config) as fs:
        base = url.path.rstrip("/")
        for root, _, names in fs.walk(url.path):
            rel = root[len(base) :].strip("/")
            for name in names:
                yield f"{rel}/{name}" if rel else name


class _Total:
    """Every worker's progress added up, because their files interleave.

    A per-file ``(done, total)`` pair means nothing once several files are in
    flight at once, so each worker reports its own deltas here and the caller
    hears about the tree instead: bytes moved so far, against a total nobody
    can know without walking ahead and asking every file its length.
    """

    def __init__(self, report: Progress) -> None:
        """Sum into ``report``, which is then called under the lock, one worker at a time."""
        self._report = report
        self._lock = threading.Lock()
        self._done = 0

    def worker(self) -> Progress:
        """A ``progress`` callback for one file, counted into the whole."""
        seen = 0

        def report(done: int, _total: int | None) -> None:
            """Add this file's growth since its last report; the tree total is unknown."""
            nonlocal seen
            with self._lock:
                self._done += done - seen
                seen = done
                self._report(self._done, None)

        return report


def _in_order(
    items: Sequence[str], workers: int, run: Callable[[str], CopyResult | None]
) -> list[CopyResult]:
    """``run`` over ``items``, several at a time, answering in the order asked.

    A file that raises stops the tree, as it does one at a time: the first
    failure is re-raised (any later ones noted on it) and whatever has not
    started is never started rather than left to copy on behind the
    exception - :func:`xrdcore.fanout.run_all`'s contract.
    """
    calls = [functools.partial(run, item) for item in items]
    done = run_all(calls, workers=workers, name="xrd-tree")
    return [result for result in done if result is not None]


def _sync_options(options: dict[str, Any], sync: SyncMode | None) -> dict[str, Any]:
    """The options each file of a tree is copied with.

    A sync only copies what it has already judged out of date, and replacing
    that is the whole point: an exclusive create would turn every changed file
    into a :class:`FileExistsError`, so that a sync could only ever add files
    and never bring one up to date. ``overwrite=False`` still holds for a tree
    copied without ``sync``.
    """
    return options if sync is None else {**options, "overwrite": True}


def _tree_total(progress: Progress | None, count: int) -> _Total | None:
    """Where a tree's workers add up their progress, when there are several."""
    return _Total(progress) if progress is not None and count > 1 else None


def _each(
    items: Sequence[str], workers: int, run: Callable[[str], CopyResult | None]
) -> list[CopyResult]:
    """``run`` over ``items``, ``workers`` at a time, keeping what it copied."""
    if workers > 1:
        return _in_order(items, workers, run)
    return [done for item in items if (done := run(item)) is not None]
