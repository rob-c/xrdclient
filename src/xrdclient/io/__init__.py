"""File objects for remote paths.

:func:`open_url` has :func:`open`'s signature and returns the same stack of
objects the builtin does - a buffered binary reader, a buffered writer, a
random-access buffer, or a text wrapper - so remote files behave exactly like
local ones, including ``for line in f``.

What lives here, and where to start reading:

- :func:`open_url` is the dispatcher. It validates the mode exactly as the
  builtin would (so a mistake fails before any connection is made), then
  routes on the URL: a ``xrdcl.unzip`` selector or ``member=`` goes to
  :mod:`xrdclient.io.zip`, ``s3://`` to :func:`xrdclient.s3.open_s3`,
  ``http``/``dav`` to :func:`xrdclient.http.open_http`, and everything else
  is ``root://`` and becomes an :class:`XRootDRawIO` over a
  :class:`~xrdclient.client.file.File`.
- :class:`_Layers` and :func:`_xrootd_layers` are the half of the builtin
  ``open`` that stacks :mod:`io` objects on a raw layer. :mod:`xrdclient.io.zip`
  reuses them, so a ZIP member gets exactly the same buffering rules.
- :mod:`xrdclient.io.raw` is the raw layer itself.

What a maintainer must not break: the return *types* match the builtin's for
every mode (tests and typeshed-style overloads pin them), ``buffering=0``
hands back the raw object that carries :attr:`XRootDRawIO.file`, and the
other protocols are imported lazily so ``import xrdclient.io`` does not drag
in the HTTP stack or ``botocore`` (``tests/test_import_cost.py``).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import IO, Any, BinaryIO, Literal, TextIO, cast, overload

from ..client.file import File
from ..config import Config
from ..session.router import Router
from ..url import XRootDURL, parse
from .raw import OpenBinaryMode, OpenTextMode, XRootDRawIO, flags_for_mode, parse_mode

__all__ = ["XRootDRawIO", "flags_for_mode", "open_url", "parse_mode"]

#: The buffer size ``buffering=-1`` means here. The builtin uses
#: :data:`io.DEFAULT_BUFFER_SIZE` (8 KiB), tuned for a local disk; over a
#: wide-area link every refill is a round trip, so a megabyte amortises it.
DEFAULT_BUFFER_SIZE = 1 << 20

#: The CGI key XrdCl reads as "open this member of the ZIP archive at this
#: path" (``XrdClZipArchive``); it is consumed here and never sent on.
_UNZIP = "xrdcl.unzip"


@dataclass(frozen=True, slots=True)
class _Layers:
    """How the :mod:`io` objects above a raw layer are to be stacked.

    These are the arguments of the builtin :func:`open` that do not reach
    the raw layer at all - they only decide which buffered class wraps it
    and whether a :class:`io.TextIOWrapper` goes on top. Carrying them as one
    value keeps the HTTP, S3, ZIP and XRootD branches from each re-listing
    them, and from one of them forgetting an argument.

    ``binary`` comes from the parsed mode; ``buffering`` is the caller's
    value untouched (``-1`` default, ``0`` raw, positive explicit size),
    because :func:`_write_window` needs to know whether the caller chose a
    size or left it to us.
    """

    binary: bool
    buffering: int = -1
    encoding: str | None = None
    errors: str | None = None
    newline: str | None = None


# The overloads are the builtin's, narrowed the same way typeshed narrows
# ``open``: a literal mode string tells the caller whether ``read`` gives
# ``bytes`` or ``str``, and ``buffering=0`` hands back the raw layer - which
# is where :attr:`XRootDRawIO.file` lives. The first deliberately overlaps the
# second, because ``buffering=0`` is an ``int`` like any other and only its
# literal value says which layer the caller wants.
@overload
def open_url(  # type: ignore[overload-overlap]
    url: str | XRootDURL,
    mode: OpenBinaryMode = ...,
    *,
    buffering: Literal[0],
    encoding: None = ...,
    errors: None = ...,
    newline: None = ...,
    config: Config | None = ...,
    router: Router | None = ...,
    posc: bool = ...,
    member: None = ...,
) -> XRootDRawIO:
    """``buffering=0`` in a binary mode: the raw layer, carrying ``.file``."""


# Overlaps the buffered overload for the same reason as the one above.
@overload
def open_url(  # type: ignore[overload-overlap]
    url: str | XRootDURL,
    mode: OpenBinaryMode = ...,
    *,
    buffering: Literal[0],
    encoding: None = ...,
    errors: None = ...,
    newline: None = ...,
    config: Config | None = ...,
    router: Router | None = ...,
    posc: bool = ...,
    member: str,
) -> io.RawIOBase:
    """``buffering=0`` with a ZIP member: the member's raw layer, no ``.file``."""


@overload
def open_url(
    url: str | XRootDURL,
    mode: OpenBinaryMode = ...,
    *,
    buffering: int = ...,
    encoding: None = ...,
    errors: None = ...,
    newline: None = ...,
    config: Config | None = ...,
    router: Router | None = ...,
    posc: bool = ...,
    member: str | None = ...,
) -> BinaryIO:
    """A binary mode: a buffered reader, writer or random-access stream."""


@overload
def open_url(
    url: str | XRootDURL,
    mode: OpenTextMode,
    *,
    buffering: int = ...,
    encoding: str | None = ...,
    errors: str | None = ...,
    newline: str | None = ...,
    config: Config | None = ...,
    router: Router | None = ...,
    posc: bool = ...,
    member: str | None = ...,
) -> TextIO:
    """A literal text mode: a :class:`io.TextIOWrapper`."""


@overload
def open_url(
    url: str | XRootDURL,
    mode: str,
    *,
    buffering: int = ...,
    encoding: str | None = ...,
    errors: str | None = ...,
    newline: str | None = ...,
    config: Config | None = ...,
    router: Router | None = ...,
    posc: bool = ...,
    member: str | None = ...,
) -> IO[Any]:
    """A mode only known at run time: bytes or text, the type cannot say which."""


def open_url(
    url: str | XRootDURL,
    mode: str = "rb",
    *,
    buffering: int = -1,
    encoding: str | None = None,
    errors: str | None = None,
    newline: str | None = None,
    config: Config | None = None,
    router: Router | None = None,
    posc: bool = False,
    member: str | None = None,
) -> IO[Any] | io.RawIOBase:
    """Open a remote file. Mirrors :func:`open`, including its return types.

    An ``http``/``https``/``dav``/``davs`` URL is handed to
    :func:`~xrdclient.http.open_http` and an ``s3://`` one to
    :func:`~xrdclient.s3.open_s3`, both of which return the same stack of objects.

    ``buffering`` of 0 gives the raw layer (binary only), a positive value
    sets the buffer size, and ``-1`` uses
    :data:`DEFAULT_BUFFER_SIZE` - a megabyte, because a wide-area round trip
    costs far more than a local one.

    ``member`` - or an ``xrdcl.unzip=<name>`` query, XrdCl's spelling of the
    same thing - opens one member of a ZIP archive for reading through ranged
    reads (:mod:`xrdclient.io.zip`); the archive is never downloaded whole.
    ``posc`` asks the server for persist-on-successful-close on a write, so a
    writer that dies leaves no partial file; it only means something over
    ``root://``.

    The signature is the public contract - it mirrors the builtin's argument
    names, keyword-only after ``mode`` - so it is left as it is even though it
    is longer than the rest of the code base allows; inside, everything that
    only shapes the :mod:`io` stack travels as one :class:`_Layers`.
    """
    _base, binary, updating = parse_mode(mode)
    _validate_layers(binary, buffering, encoding)
    layers = _Layers(binary, buffering, encoding, errors, newline)
    target = parse(url)
    selected = member if member is not None else target.query.get(_UNZIP)
    if selected is not None:
        # XrdCl's ZIP support is read-only too: a member is located through
        # the central directory and read in place; there is no way to
        # rewrite one without rewriting everything after it.
        if _base != "r" or updating:
            raise ValueError("a ZIP member can only be opened for reading")
        return _open_member(target, selected, layers, config, router)
    remote = _other_protocol(target, mode, layers, config)
    if remote is not None:
        return remote
    handle = File(target, config, router=router)
    raw = XRootDRawIO(handle, mode, posc=posc)
    return _xrootd_layers(raw, updating, layers)


def _open_member(
    target: XRootDURL,
    selected: str,
    layers: _Layers,
    config: Config | None,
    router: Router | None,
) -> IO[Any] | io.RawIOBase:
    """Open the ZIP member ``selected`` of the archive ``target`` names.

    The selector is stripped from the archive's URL before it is opened: it is
    an instruction to this client, not to the server, and a server that saw
    it would look for a file whose opaque data it does not understand. The
    import is deferred so the ZIP code costs nothing for a plain open.
    """
    from .zip import open_member

    query = {name: value for name, value in target.query.items() if name != _UNZIP}
    archive = target.evolve(query=query)
    return open_member(
        archive,
        selected,
        binary=layers.binary,
        buffering=layers.buffering,
        encoding=layers.encoding,
        errors=layers.errors,
        newline=layers.newline,
        config=config,
        router=router,
    )


def _validate_layers(binary: bool, buffering: int, encoding: str | None) -> None:
    """Refuse the argument combinations the builtin :func:`open` refuses.

    The messages are the builtin's, word for word, so code that catches and
    inspects them - or a person who has seen them before - is not surprised
    by a remote file. Checking here, before the URL is even parsed, means a
    bad call fails without a connection being made.
    """
    if not binary and buffering == 0:
        raise ValueError("can't have unbuffered text I/O")
    if binary and encoding is not None:
        raise ValueError("binary mode doesn't take an encoding argument")


def _other_protocol(
    target: XRootDURL, mode: str, layers: _Layers, config: Config | None
) -> IO[Any] | io.RawIOBase | None:
    """Hand an ``s3://`` or HTTP-family URL to its own opener, or ``None``.

    Those openers build their own stacks (their raw layers speak HTTP, not
    XRootD) but take the same layering arguments, so the result is
    indistinguishable to the caller. Both imports are deferred: the HTTP
    stack and especially ``botocore`` are far heavier than the rest of this
    module, and a ``root://`` open must not pay for them. ``router`` and
    ``posc`` are XRootD concepts and do not cross over.
    """
    if target.is_s3:
        from ..s3 import open_s3

        return open_s3(
            target,
            mode,
            buffering=layers.buffering,
            encoding=layers.encoding,
            errors=layers.errors,
            newline=layers.newline,
            config=config,
        )
    if target.is_http:
        from ..http import open_http

        return open_http(
            target,
            mode,
            buffering=layers.buffering,
            encoding=layers.encoding,
            errors=layers.errors,
            newline=layers.newline,
            config=config,
        )
    return None


def _write_window(raw: XRootDRawIO, size: int, buffering: int) -> int:
    """The buffer a writer gets: by default, one whole window of writes.

    Each flush of a writer's buffer is one :meth:`~xrdclient.client.file.File.write`,
    which the bulk data plane sends as ``config.bulk_depth`` pipelined
    requests of ``config.chunk_size``. A megabyte buffer would make every
    flush one request and one round trip; a buffer the size of the window
    keeps the pipeline full, at the cost of that much memory per open writer.
    An explicit ``buffering`` is taken as given.
    """
    config = raw.file.config
    if buffering >= 0 or not config.bulk:
        return size
    return max(size, config.chunk_size * config.bulk_depth)


def _xrootd_layers(raw: io.RawIOBase, updating: bool, layers: _Layers) -> IO[Any] | io.RawIOBase:
    """Stack the buffered (and, for text, decoding) layers on ``raw``.

    This is the second half of what the builtin :func:`open` does, with the
    same choice of class: :class:`io.BufferedRandom` for a ``+`` mode or a
    raw layer that is both readable and writable, :class:`io.BufferedWriter`
    for a writer, :class:`io.BufferedReader` otherwise. ``buffering=0``
    returns ``raw`` itself - the only way a caller reaches
    :attr:`XRootDRawIO.file` for vector and paged reads.

    :mod:`xrdclient.io.zip` calls this with its own read-only raw layer, which
    is why ``raw`` is any :class:`io.RawIOBase` and the write-window lookup
    (which needs an XRootD handle) happens only on the writer branch.
    """
    buffering = layers.buffering
    if buffering == 0:
        return raw
    size = DEFAULT_BUFFER_SIZE if buffering < 0 else buffering

    stream: BinaryIO
    if updating or (raw.readable() and raw.writable()):
        stream = io.BufferedRandom(raw, size)
    elif raw.writable():
        stream = io.BufferedWriter(raw, _write_window(cast("XRootDRawIO", raw), size, buffering))
    else:
        stream = io.BufferedReader(raw, size)

    if layers.binary:
        return stream
    return io.TextIOWrapper(
        stream, encoding=layers.encoding, errors=layers.errors, newline=layers.newline
    )
