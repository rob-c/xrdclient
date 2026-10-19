"""Endpoint adapters: every source and target reduced to a binary stream.

This is the seam the engine's module docstring promises: :func:`_reader` and
:func:`_writer` (with :func:`_resumer` for a write at an offset) are the only
places that know which schemes exist, so a new protocol is taught here and the
planner and pump in :mod:`xrdclient.copy.engine` never change. The small
per-endpoint questions the planner asks - how long is it (:func:`_probe`),
delete it (:func:`_remove`), what is its checksum (:func:`_server_checksum`) -
live here for the same reason, as does :func:`_make_parent_collection`, the
one thing a WebDAV target needs done before it can be written at all.

The engine re-exports every name, and calls them through its own globals, so a
test that patches ``engine._probe`` or ``engine._resumer`` still changes what a
transfer does. Code that lives *here* (``_probe``'s recursion into an archive)
calls the local definition. Nothing here imports the engine.
"""

from __future__ import annotations

import contextvars
import io
import os
import posixpath
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import IO, cast

from ..config import Config
from ..errors import UnsupportedError, XRootDError, kXR_Unsupported
from ..types import ChecksumInfo
from ..url import XRootDURL, parse
from ._local import _RecoveringLocalReader


def _is_stream(obj: object) -> bool:
    """True for something already open - a file object, a socket wrapper."""
    return hasattr(obj, "read") or hasattr(obj, "write")


def _target_url(obj: object) -> XRootDURL | None:
    """The URL ``obj`` names, or ``None`` if it is an open stream."""
    if _is_stream(obj):
        return None
    # Anything else is a URL or a path; parse says so if it is neither.
    return parse(cast("str | XRootDURL", obj))


def _zip_member(url: XRootDURL | None) -> str | None:
    """The member selected from ``url`` by XrdCl's opaque ZIP parameter."""
    return None if url is None else url.query.get("xrdcl.unzip")


def _reader(url: XRootDURL, config: Config, stack: ExitStack) -> tuple[IO[bytes], int | None]:
    """A binary reader for ``url``, plus its size when the endpoint knows it."""
    if _zip_member(url) is not None:
        from ..io import open_url

        raw = stack.enter_context(open_url(url, "rb", buffering=0, config=config))
        size = raw.seek(0, io.SEEK_END)
        raw.seek(0)
        return cast("IO[bytes]", raw), size
    if url.is_local:
        reader = _RecoveringLocalReader(url.path, config)
        stack.callback(reader.close)
        return cast("IO[bytes]", reader), os.path.getsize(url.path)
    if url.is_root:
        from ..io import open_url

        raw = stack.enter_context(open_url(url, "rb", buffering=0, config=config))
        # A RawIOBase reads bytes but is not an ``IO[bytes]`` as far as
        # typeshed is concerned; the pump only ever calls ``read``.
        return cast("IO[bytes]", raw), raw.file.size
    if url.is_http:
        from ..http import open_http

        return stack.enter_context(open_http(url, "rb", config=config)), None
    if url.is_s3:
        from ..s3 import open_s3

        return stack.enter_context(open_s3(url, "rb", config=config)), None
    raise UnsupportedError(kXR_Unsupported, f"cannot read from {url.scheme}://")


def _writer(
    url: XRootDURL, config: Config, stack: ExitStack, *, overwrite: bool, coerce: bool = False
) -> IO[bytes]:
    """A binary writer for ``url``. ``overwrite=False`` means exclusive create.

    ``coerce`` opens a ``root://`` target with ``kXR_force``, which tells the
    server to ignore its file usage rules - another client's open of the same
    file - as XrdCl's ``coerce`` does. Nothing else has such rules to ignore.
    """
    mode = "wb" if overwrite else "xb"
    if url.is_local:
        return stack.enter_context(open(url.path, mode))
    if url.is_root:
        return _root_writer(url, mode, config, stack, coerce=coerce)
    if url.is_http:
        from ..http import open_http

        return stack.enter_context(open_http(url, mode, config=config))
    if url.is_s3:
        from ..s3 import open_s3

        return stack.enter_context(open_s3(url, mode, config=config))
    raise UnsupportedError(kXR_Unsupported, f"cannot write to {url.scheme}://")


def _root_writer(
    url: XRootDURL, mode: str, config: Config, stack: ExitStack, *, coerce: bool
) -> IO[bytes]:
    """``url`` opened for writing in ``mode``, with ``kXR_force`` if ``coerce``."""
    if not coerce:
        from ..io import open_url

        raw = stack.enter_context(open_url(url, mode, buffering=0, config=config))
        return cast("IO[bytes]", raw)
    from ..client.file import File
    from ..flags import Access, OpenFlags, flags_for_mode
    from ..io.raw import XRootDRawIO

    handle = File(url, config)
    access = Access.OWNER_READ | Access.OWNER_WRITE | Access.GROUP_READ
    try:
        handle.open(flags_for_mode(mode) | OpenFlags.FORCE, access)
    except BaseException:
        handle.close()
        raise
    return cast("IO[bytes]", stack.enter_context(XRootDRawIO(handle, mode, opened=True)))


def _resumer(
    url: XRootDURL, config: Config, stack: ExitStack, offset: int, *, coerce: bool = False
) -> IO[bytes]:
    """A writer for ``url`` positioned at ``offset``, keeping what is there.

    An HTTP target has no such thing: a ``PUT`` replaces the whole resource,
    so a partial upload can only be repeated, never continued.
    """
    if url.is_local:
        return _positioned(stack.enter_context(open(url.path, "r+b")), offset)
    if url.is_root:
        return _positioned(_root_writer(url, "r+b", config, stack, coerce=coerce), offset)
    raise UnsupportedError(kXR_Unsupported, f"cannot resume a copy into {url.scheme}://")


def _positioned(handle: IO[bytes], offset: int) -> IO[bytes]:
    """``handle``, moved to ``offset`` (where a resumed copy carries on)."""
    handle.seek(offset)
    return handle


def _probe(url: XRootDURL, config: Config) -> tuple[int, int] | None:
    """``(size, mtime)`` for ``url``, or ``None`` when there is nothing there."""
    if _zip_member(url) is not None:
        try:
            with ExitStack() as stack:
                reader, size = _reader(url, config, stack)
                assert size is not None
                reader.seek(0)
            archive = url.evolve(
                query={name: value for name, value in url.query.items() if name != "xrdcl.unzip"}
            )
            outer = _probe(archive, config)
            return size, outer[1] if outer is not None else 0
        except (OSError, XRootDError, KeyError):
            return None
    if url.is_local:
        try:
            info = os.stat(url.path)
        except OSError:
            return None
        return info.st_size, int(info.st_mtime)
    from ..client import FileSystem

    with FileSystem(url.with_path("/"), config) as fs:
        try:
            stat = fs.stat(url.path)
        except OSError:
            return None
    return stat.st_size, stat.st_mtime


def _remove(url: XRootDURL, config: Config) -> None:
    """Delete ``url`` - what turns a copy into a move."""
    if url.is_local:
        os.remove(url.path)
        return
    from ..client import FileSystem

    with FileSystem(url.with_path("/"), config) as fs:
        fs.remove(url.path)


def _server_checksum(url: XRootDURL, config: Config, algorithm: str) -> ChecksumInfo:
    """Ask whichever server owns ``url`` what it thinks the checksum is."""
    if url.is_http:
        from ..http import digest

        return digest(url, algorithm, config=config)
    from ..client import FileSystem

    with FileSystem(url.with_path("/"), config) as fs:
        return fs.checksum(url.path, algorithm)


#: Set while :func:`~xrdclient.copy.engine.copy_tree` copies a file whose
#: directories it has already made (:func:`~xrdclient.copy._tree._make_tree_dirs`),
#: so :func:`_make_parent_collection` does not repeat the work per file. A
#: context variable rather than a :func:`~xrdclient.copy.engine.copy` keyword
#: because ``copy``'s signature is public and mirrored exactly by
#: :func:`xrdclient.aio.copy`; it is set inside each tree worker's own thread,
#: so it never leaks into an unrelated copy.
_TREE_MADE_PARENTS: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "xrdclient_tree_made_parents", default=False
)


@contextmanager
def _parents_already_made() -> Iterator[None]:
    """Within this block, copies trust that their WebDAV parents exist."""
    token = _TREE_MADE_PARENTS.set(True)
    try:
        yield
    finally:
        _TREE_MADE_PARENTS.reset(token)


def _make_parent_collection(dst: XRootDURL | None, config: Config) -> None:
    """Make a WebDAV target's parent directory before the first byte goes out.

    A write to ``root://`` carries ``kXR_mkpath`` and a local copy calls
    ``os.makedirs``, so a copy into a directory that is not there succeeds at
    both ends; WebDAV makes no parents on ``PUT`` (it answers ``409``). Worse,
    a grid storage element (EOS) is left unable to create any of the path once
    a ``PUT`` into it has failed, so this cannot be done in reaction to the
    error - the parent is made up front, from a clean namespace. Only an
    ``http``/``dav`` destination needs it: ``root://`` and local make their own
    parents, S3 has no directories, and an open stream has no path. ``MKCOL``
    on a collection already there is forgiven (``exist_ok``), so the common
    copy into an existing directory costs one round trip and no error.
    """
    if dst is None or not dst.is_http or _TREE_MADE_PARENTS.get():
        return
    parent = posixpath.dirname(dst.path.rstrip("/"))
    if parent in ("", "/"):
        return
    from ..client import FileSystem

    with FileSystem(dst.with_path("/"), config) as fs:
        fs.mkdir(parent, parents=True, exist_ok=True)
