"""``XRootD.client.glob``: wildcards over local paths and ``root://`` URLs alike.

The bindings' rules, kept: a pattern that matches anything locally is a local
pattern; otherwise each wildcard level is expanded with a directory listing
on the server. A trailing ``?key=value`` is the URL's parameters, not a
wildcard, and is carried onto every result; a trailing ``/`` matches
directories only. ``raise_error=False`` skips a directory that cannot be
listed instead of raising :class:`RuntimeError`.
"""

from __future__ import annotations

import fnmatch
import glob as _glob
import os
from collections.abc import Iterable, Iterator
from urllib.parse import urlparse

from .filesystem import FileSystem
from .flags import DirListFlags, StatInfoFlags

__all__ = ["extract_url_params", "glob", "iglob", "split_url", "xrootd_iglob"]


def split_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/", parsed.path


def extract_url_params(pathname: str) -> tuple[str, str]:
    """``(pattern, "?params")``: the rightmost ``?`` is parameters if an ``=`` follows."""
    cut = pathname.rfind("?")
    if cut == -1 or "=" not in pathname[cut + 1 :]:
        return pathname, ""
    return pathname[:cut], pathname[cut:]


def iglob(pathname: str, raise_error: bool = True) -> Iterator[str]:
    """Each path matching ``pathname``, one at a time."""
    pattern, params = extract_url_params(pathname)
    local = _glob.iglob(pattern)
    first = next(local, None)
    if first is not None:
        yield first + params
        for path in local:
            yield path + params
        return
    for path in xrootd_iglob(pattern, params, raise_error):
        yield path + params


def xrootd_iglob(pathname: str, url_params: str, raise_error: bool) -> Iterator[str]:
    """Expand ``pathname`` a level at a time with directory listings."""
    dirs, basename = os.path.split(pathname.rstrip("/"))
    parents: Iterable[str] = (
        xrootd_iglob(dirs + "/", url_params, raise_error) if _glob.has_magic(dirs) else [dirs]
    )
    only_dirs = pathname.endswith("/")
    for dirname in parents:
        yield from _matches(dirname, basename, url_params, only_dirs, raise_error)


def _matches(
    dirname: str, basename: str, params: str, only_dirs: bool, raise_error: bool
) -> Iterator[str]:
    host, path = split_url(dirname)
    flags = DirListFlags.STAT if only_dirs else DirListFlags.NONE
    filesystem = FileSystem(host)
    try:
        status, listing = filesystem.dirlist(path + "/" + params, flags)
    finally:
        filesystem.native.close()
    if status.error:
        if raise_error:
            raise RuntimeError(f"'{status!s}' for path '{dirname}'")
        return
    for entry in listing.dirlist:
        name = entry.name
        if name in (".", "..") or not fnmatch.fnmatchcase(name, basename):
            continue
        if not only_dirs:
            yield os.path.join(dirname, name)
        elif entry.statinfo.flags & StatInfoFlags.IS_DIR:
            yield os.path.join(dirname, name) + "/"


def glob(pathname: str, raise_error: bool = True) -> list[str]:
    """Every path matching ``pathname``, as a list."""
    return list(iglob(pathname, raise_error=raise_error))
