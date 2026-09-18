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
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from .config import Config
from .path import XRootDPath
from .types import ChecksumInfo, StatInfo
from .url import XRootDURL

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


def ls(url: Location, *, config: Config | None = None) -> list[XRootDPath]:
    """What is in that directory, as paths you can go on to use.

    Sorted by name, because a listing nobody sorted reads as though the
    server shuffled it - which, in the case of a redirector, it did.
    """
    with XRootDPath(url, config) as here:
        entries = here.fs.scandir(here.url.path)
    return sorted(XRootDPath(here.url.with_path(entry.path), config) for entry in entries)


def glob(pattern: Location, *, config: Config | None = None) -> list[XRootDPath]:
    """Every path matching a URL with wildcards in it.

    ``*`` stops at a slash and ``**`` does not, as in :mod:`pathlib`::

        xrdclient.glob("root://eos.example.org//store/run7/**/*.root")
    """
    with XRootDPath(pattern, config) as here:
        found = list(here.fs.glob(here.url.path))
    return [XRootDPath(here.url.with_path(path), config) for path in found]


def stat(url: Location, *, config: Config | None = None) -> StatInfo:
    """Size, times and permissions. Prints as one line of ``ls -l``."""
    with XRootDPath(url, config) as target:
        return target.stat()


def exists(url: Location, *, config: Config | None = None) -> bool:
    """Whether there is anything there at all."""
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
    """
    with XRootDPath(url, config) as target:
        return target.fs.checksum(target.url.path, algorithm)


def read_bytes(url: Location, *, config: Config | None = None) -> bytes:
    """The whole file, as bytes. Fine for a small one; see :func:`xrdclient.open`."""
    with XRootDPath(url, config) as target:
        return target.read_bytes()


def read_text(url: Location, encoding: str = "utf-8", *, config: Config | None = None) -> str:
    """The whole file, decoded."""
    with XRootDPath(url, config) as target:
        return target.read_text(encoding)


def write_bytes(url: Location, data: bytes, *, config: Config | None = None) -> int:
    """Write bytes to the file, making the directories above it."""
    with XRootDPath(url, config) as target:
        return target.write_bytes(data)


def write_text(
    url: Location, text: str, encoding: str = "utf-8", *, config: Config | None = None
) -> int:
    """Write text to the file, making the directories above it."""
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
    with XRootDPath(url, config) as target:
        target.mkdir(mode, parents=parents, exist_ok=exist_ok)


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
    with XRootDPath(url, config) as target:
        if not target.is_dir():
            target.unlink(missing_ok=missing_ok)
        elif recursive:
            target.fs.rmtree(target.url.path)
        else:
            target.rmdir()


def move(source: Location, destination: Location, *, config: Config | None = None) -> None:
    """Move a file, wherever the two ends are.

    On one endpoint this is a rename, which costs nothing and moves no data.
    Between two it is a copy - checksummed on arrival, as every copy this
    library makes is - and the source goes only once that has succeeded.
    """
    origin, target = XRootDPath(source, config), XRootDPath(destination, config)
    with origin, target:
        if origin.url.endpoint == target.url.endpoint:
            origin.rename(target.url.path)
            return
    from .copy import copy

    copy(source, destination, config=config)
    remove(source, config=config)


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
    paths = [XRootDPath(url, config) for url in wanted]
    with paths[0] as first:
        return first.fs.prepare([path.url.path for path in paths], priority=priority)


def is_online(url: Location, *, config: Config | None = None) -> bool:
    """Whether the file is on disk now, rather than only on tape."""
    return not stat(url, config=config).is_offline()
