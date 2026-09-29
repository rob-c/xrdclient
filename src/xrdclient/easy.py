"""One-line answers to the questions people actually ask a storage element.

Every function here takes a URL and does one obvious thing with it::

    >>> import xrdclient
    >>> for path in xrdclient.ls("root://eos.example.org//store/user/me"):  # doctest: +SKIP
    ...     print(path.name, xrdclient.human_bytes(path.stat().st_size))

There is nothing here that :class:`~xrdclient.FileSystem` and
:class:`~xrdclient.XRootDPath` cannot do; this is the same thing with the objects
left out, for the program that only needs one answer and for the person who
would rather not learn a class first. Each call opens a connection and closes
it again, so a loop over a thousand files should hold a
:class:`~xrdclient.XRootDPath` and use that instead - it keeps one connection for
the whole traversal.

``config=`` takes a :class:`~xrdclient.Config` for the call, for a site that needs
a longer timeout or a particular credential.

A plain path (or a ``file://`` URL) names a file on this machine, and the
verbs that answer with data - :func:`stat`, :func:`exists`, :func:`read_bytes`,
:func:`mkdir`, :func:`remove`, :func:`move` and the rest - do the same thing
there with :mod:`os`, so a script can treat both ends of a transfer alike.
:func:`ls` and :func:`glob` answer with remote paths, which a local file cannot
be, and :func:`stage` asks a tape system, which a local disk does not have;
those three refuse a local path and say so.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat as _stat
from collections.abc import Sequence
from typing import Union

from .config import Config
from .crypto import new as new_checksum
from .flags import StatInfoFlags, permissions
from .path import XRootDPath
from .types import ChecksumInfo, StatInfo
from .url import XRootDURL, parse

__all__ = [
    "ls",
    "glob",
    "stat",
    "exists",
    "size",
    "checksum",
    "read_bytes",
    "read_text",
    "write_bytes",
    "write_text",
    "mkdir",
    "remove",
    "move",
    "stage",
    "is_online",
]

#: What every verb here will take for a place: the URL as text, as a parsed
#: URL, or as a path object somebody already has.
Location = Union[str, XRootDURL, XRootDPath]


def _url(location: Location) -> XRootDURL:
    """``location`` parsed, whichever of the three spellings it came in."""
    if isinstance(location, XRootDPath):
        return location.url
    return parse(location) if isinstance(location, str) else location


def _local(location: Location) -> str | None:
    """The path on this machine ``location`` names, or ``None`` if it is remote.

    Without this a plain path reaches :class:`~xrdclient.FileSystem` as a
    server called ``""`` on port 0, and fails with an error about connecting
    there that says nothing about the actual mistake.
    """
    url = _url(location)
    return url.path if url.is_local else None


def _refuse_local(location: Location, verb: str, instead: str) -> None:
    """Raise if ``location`` is local, naming what to use for it instead."""
    if _local(location) is not None:
        raise ValueError(f"{verb}() works on remote URLs, not local path {location}: {instead}")


def _local_stat(path: str) -> StatInfo:
    """What :func:`stat` answers for a server, filled in from :func:`os.stat`."""
    info = os.stat(path)
    flags = StatInfoFlags.NONE
    if _stat.S_ISDIR(info.st_mode):
        flags |= StatInfoFlags.IS_DIR
    elif not _stat.S_ISREG(info.st_mode):
        flags |= StatInfoFlags.OTHER
    if info.st_mode & 0o111:
        flags |= StatInfoFlags.X_SET
    # The server's two access flags are this client's access, not the mode
    # bits, so they are answered the same way here.
    if os.access(path, os.R_OK):
        flags |= StatInfoFlags.IS_READABLE
    if os.access(path, os.W_OK):
        flags |= StatInfoFlags.IS_WRITABLE
    return StatInfo(
        id=str(info.st_ino),
        st_size=info.st_size,
        flags=flags,
        st_mtime=int(info.st_mtime),
        st_ctime=int(info.st_ctime),
        st_atime=int(info.st_atime),
        path=path,
        mode_str=f"{_stat.S_IMODE(info.st_mode):o}",
    )


def _local_parent(path: str) -> None:
    """Make the directories above ``path``, as a remote write does.

    ``path`` is a parsed URL's, which is always absolute, so it always has a
    parent - at worst ``/``, which :func:`os.makedirs` finds already there.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)


def ls(url: Location, *, config: Config | None = None) -> list[XRootDPath]:
    """What is in that directory, as paths you can go on to use.

    Sorted by name, because a listing nobody sorted reads as though the
    server shuffled it - which, in the case of a redirector, it did.
    A local directory is :meth:`pathlib.Path.iterdir`'s job.
    """
    _refuse_local(url, "ls", "use pathlib.Path.iterdir")
    with XRootDPath(url, config) as here:
        entries = here.fs.scandir(here.url.path)
    return sorted(XRootDPath(here.url.with_path(entry.path), config) for entry in entries)


def glob(pattern: Location, *, config: Config | None = None) -> list[XRootDPath]:
    """Every path matching a URL with wildcards in it.

    ``*`` stops at a slash and ``**`` does not, as in :mod:`pathlib`::

        xrdclient.glob("root://eos.example.org//store/run7/**/*.root")

    A local pattern is :meth:`pathlib.Path.glob`'s job.
    """
    _refuse_local(pattern, "glob", "use pathlib.Path.glob")
    with XRootDPath(pattern, config) as here:
        found = list(here.fs.glob(here.url.path))
    return [XRootDPath(here.url.with_path(path), config) for path in found]


def stat(url: Location, *, config: Config | None = None) -> StatInfo:
    """Size, times and permissions. Prints as one line of ``ls -l``."""
    local = _local(url)
    if local is not None:
        return _local_stat(local)
    with XRootDPath(url, config) as target:
        return target.stat()


def exists(url: Location, *, config: Config | None = None) -> bool:
    """Whether there is anything there at all."""
    local = _local(url)
    if local is not None:
        return os.path.exists(local)
    with XRootDPath(url, config) as target:
        return target.exists()


def size(url: Location, *, config: Config | None = None) -> int:
    """How many bytes the file is."""
    return stat(url, config=config).st_size


def checksum(
    url: Location, algorithm: str | None = None, *, config: Config | None = None
) -> ChecksumInfo:
    """The digest the server has for the file, without moving the file.

    ``algorithm`` picks between the ones a site offers - ``"adler32"``,
    ``"md5"``, ``"crc32c"`` - and the default is whichever it prefers.
    For a local file there is no server to ask, so it is read and digested
    here, with ``config.preferred_checksum`` as the default.
    """
    local = _local(url)
    if local is not None:
        return _local_checksum(local, algorithm or (config or Config()).preferred_checksum)
    with XRootDPath(url, config) as target:
        return target.fs.checksum(target.url.path, algorithm)


def _local_checksum(path: str, algorithm: str) -> ChecksumInfo:
    """``algorithm`` over the local file at ``path``, read a megabyte at a time."""
    digest = new_checksum(algorithm)
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return ChecksumInfo(algorithm, digest.hexdigest().lower())


def read_bytes(url: Location, *, config: Config | None = None) -> bytes:
    """The whole file, as bytes. Fine for a small one; see :func:`xrdclient.open`."""
    local = _local(url)
    if local is not None:
        with open(local, "rb") as handle:
            return handle.read()
    with XRootDPath(url, config) as target:
        return target.read_bytes()


def read_text(url: Location, encoding: str = "utf-8", *, config: Config | None = None) -> str:
    """The whole file, decoded."""
    local = _local(url)
    if local is not None:
        with open(local, encoding=encoding) as handle:
            return handle.read()
    with XRootDPath(url, config) as target:
        return target.read_text(encoding)


def write_bytes(url: Location, data: bytes, *, config: Config | None = None) -> int:
    """Write bytes to the file, making the directories above it."""
    local = _local(url)
    if local is not None:
        _local_parent(local)
        with open(local, "wb") as handle:
            return handle.write(data)
    with XRootDPath(url, config) as target:
        return target.write_bytes(data)


def write_text(
    url: Location, text: str, encoding: str = "utf-8", *, config: Config | None = None
) -> int:
    """Write text to the file, making the directories above it."""
    local = _local(url)
    if local is not None:
        _local_parent(local)
        with open(local, "w", encoding=encoding) as handle:
            return handle.write(text)
    with XRootDPath(url, config) as target:
        return target.write_text(text, encoding)


def mkdir(
    url: Location,
    mode: int | str = 0o755,
    *,
    parents: bool = True,
    exist_ok: bool = True,
    config: Config | None = None,
) -> None:
    """Make the directory, and the ones above it.

    Unlike :meth:`pathlib.Path.mkdir` this makes parents and forgives a
    directory that is already there, because that is what someone typing
    ``mkdir`` at this level means. ``mode`` reads either way: ``0o750`` or
    ``"rwxr-x---"``.
    """
    local = _local(url)
    if local is not None:
        _local_mkdir(local, permissions(mode), parents=parents, exist_ok=exist_ok)
        return
    with XRootDPath(url, config) as target:
        target.mkdir(mode, parents=parents, exist_ok=exist_ok)


def _local_mkdir(path: str, mode: int, *, parents: bool, exist_ok: bool) -> None:
    """:func:`mkdir` on this machine, with the same forgiveness it has remotely.

    The mode is set explicitly afterwards because :func:`os.mkdir` filters it
    through the umask, and a remote ``kXR_mkdir`` does not.
    """
    if exist_ok and os.path.isdir(path):
        return
    if parents:
        os.makedirs(path, mode)
    else:
        os.mkdir(path, mode)
    os.chmod(path, mode)


def remove(
    url: Location,
    *,
    recursive: bool = False,
    missing_ok: bool = False,
    config: Config | None = None,
) -> None:
    """Delete a file, or an empty directory.

    A directory with anything in it needs ``recursive=True``, which is the
    one thing in this module that cannot be undone, so it has to be asked
    for by name.
    """
    local = _local(url)
    if local is not None:
        _local_remove(local, recursive=recursive, missing_ok=missing_ok)
        return
    with XRootDPath(url, config) as target:
        if not target.is_dir():
            target.unlink(missing_ok=missing_ok)
        elif recursive:
            target.fs.rmtree(target.url.path)
        else:
            target.rmdir()


def _local_remove(path: str, *, recursive: bool, missing_ok: bool) -> None:
    """:func:`remove` on this machine: a file, an empty directory, or a tree."""
    try:
        if os.path.islink(path) or not os.path.isdir(path):
            os.remove(path)
        elif recursive:
            shutil.rmtree(path)
        else:
            os.rmdir(path)
    except FileNotFoundError:
        if not missing_ok:
            raise


def move(source: Location, destination: Location, *, config: Config | None = None) -> None:
    """Move a file, wherever the two ends are.

    On one endpoint - one server, or this machine - this is a rename, which
    costs nothing and moves no data. Between two, or across a local mount
    point a rename cannot cross, it is a copy that must pass a checksum
    comparison of both ends, and the source goes only once that has
    succeeded: a move is the one copy that destroys the original, so an end
    that cannot be checksummed makes it an error rather than an unverified
    delete, and leaves the source where it was.
    """
    origin, target = _url(source), _url(destination)
    if origin.endpoint == target.endpoint and _renamed(source, destination, config):
        return
    from .copy import copy

    # ``remove_source`` deletes only after the copy has passed verification,
    # and does it with whatever suits that end - os.remove for a local file.
    copy(source, destination, config=config, verify=True, remove_source=True)


def _renamed(source: Location, destination: Location, config: Config | None) -> bool:
    """Rename ``source`` within its endpoint, or say that it could not.

    Only a local rename across two filesystems answers ``False``: that is
    the one "cannot rename here" a copy and a delete can still satisfy.
    """
    local = _local(source)
    if local is None:
        with XRootDPath(source, config) as origin:
            origin.rename(_url(destination).path)
        return True
    try:
        os.replace(local, _url(destination).path)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        return False
    return True


def stage(
    urls: Location | Sequence[Location], *, priority: int = 0, config: Config | None = None
) -> str:
    """Ask a tape site to bring these files onto disk. Returns the request id.

    Staging takes minutes to hours, so this returns as soon as the site has
    accepted the request. :func:`is_online` says whether a file has arrived,
    and :meth:`~xrdclient.FileSystem.query_prepare` reports on the request as a
    whole.
    """
    wanted = [urls] if isinstance(urls, (str, XRootDURL, XRootDPath)) else list(urls)
    if not wanted:
        raise ValueError("stage() needs a file to stage: it was given none")
    for url in wanted:
        _refuse_local(url, "stage", "a local file is already on disk")
    paths = [XRootDPath(url, config) for url in wanted]
    with paths[0] as first:
        return first.fs.prepare([path.url.path for path in paths], priority=priority)


def is_online(url: Location, *, config: Config | None = None) -> bool:
    """Whether the file is on disk now, rather than only on tape."""
    return not stat(url, config=config).is_offline()
