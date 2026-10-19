"""File objects over HTTP, with :func:`open`'s semantics.

Reading is a ranged ``GET`` that stays open, so a seek costs a new request
and a sequential read costs none. A vector read (:meth:`HTTPRawIO.readv`)
asks for all of its ranges in one multi-range ``GET``. Writing is a ``PUT``: small files go in one
request with a ``Content-Length`` (which lets the client follow a redirect,
the way a storage element in front of a pool node needs), and anything larger
switches to a chunked upload that streams without ever holding the file in
memory.

Layering, as for the builtin: :func:`open_http` -> :class:`io.TextIOWrapper`
(text modes) -> :class:`io.BufferedReader`/``BufferedWriter`` ->
:class:`HTTPRawIO`, which owns the HTTP. :class:`xrdclient.s3.S3RawIO`
subclasses :class:`HTTPRawIO` and overrides the upload hooks
(:attr:`HTTPRawIO._streaming`, :meth:`HTTPRawIO._stream_after`), so a change
to their contract is a change to S3 as well.

How it compares: davix (and so gfal2, and XrdCl's HTTP plug-in) also reads
with ranged ``GET`` requests and uploads with ``PUT``, normally of a local
file whose length it knows up front. The
chunked upload here (RFC 9112 section 7.1) is what lets ``write`` stream an
unbounded file through a constant buffer; its price is that a redirect must
come before the body, which ``Expect: 100-continue`` (:mod:`xrdhttp.expect`)
arranges. Ranges are RFC 9110 section 14; an unsatisfiable range (``416``)
is read as end of file, which is what a local file does at its end.
"""

from __future__ import annotations

import io
from collections.abc import Iterable
from typing import IO, TYPE_CHECKING, Any, BinaryIO, Literal, TextIO, overload

from xrdcore import retry as _retry
from xrdhttp import ranges as _ranges
from xrdhttp.session import Response, Upload

from ..config import Config
from ..errors import ConnectionError as XRDConnectionError
from ..errors import (
    ExistsError,
    ProtocolError,
    TransientError,
    UnsupportedError,
    WaitLimitError,
    kXR_ItExists,
    kXR_Unsupported,
)
from ..errors import TimeoutError as XRDTimeoutError
from ..io.raw import OpenBinaryMode, OpenTextMode
from ..log import get_logger, redact
from ..types import ReadRange
from ..url import XRootDURL, parse
from .client import HTTPClient, check_status

if TYPE_CHECKING:
    from _typeshed import ReadableBuffer, WriteableBuffer

__all__ = ["DEFAULT_BUFFER_SIZE", "HTTPRawIO", "open_http"]

_log = get_logger(__name__)

#: The buffered layer's size when the caller does not choose one: 1 MiB, far
#: above :data:`io.DEFAULT_BUFFER_SIZE`, because every refill of an HTTP file
#: after a seek is a round trip, not a system call.
DEFAULT_BUFFER_SIZE = 1 << 20

#: What a successful ``PUT`` answers: ``201 Created`` for a new resource,
#: ``200``/``204`` for a replaced one (RFC 9110 section 9.3.4).
_WRITE_OK = (200, 201, 204)


def _mode_access(mode: str) -> tuple[bool, bool]:
    """``(readable, writable)`` for ``mode``; append is refused outright.

    Only the access is decided here; :func:`xrdclient.io.parse_mode` has
    already validated the mode string in :func:`open_http`.
    """
    if "a" in mode:  # before a connection is taken, so none has to be given back
        raise UnsupportedError(
            kXR_Unsupported, "HTTP has no append; read, concatenate, and PUT instead"
        )
    return "r" in mode or "+" in mode, any(character in mode for character in "wxa+")


class HTTPRawIO(io.RawIOBase):
    """Unbuffered binary I/O against one HTTP URL.

    Instances are what :func:`open_http` wraps; use that unless you want the
    raw layer, in which case remember that :meth:`read` may come back short.
    """

    #: Whether to ask for several ranges in one request. A subclass for a
    #: service known not to serve them (S3 sends the whole object) says no.
    multirange = True

    def __init__(
        self,
        url: str | XRootDURL,
        mode: str = "rb",
        *,
        config: Config | None = None,
        client: HTTPClient | None = None,
    ) -> None:
        """Prepare a file on ``url``; only ``x`` mode sends a request now.

        ``client`` is borrowed (a filesystem's, so that its files share its
        pool and its :meth:`~HTTPClient.abort`); without one the file makes
        its own and closes it on :meth:`close`. Nothing is read until the
        first read, and nothing written until the buffer fills or the file
        closes.
        """
        # Closed until the constructor finishes: a file that never opened must
        # not try to PUT anything when the interpreter collects it.
        self._done = True
        self.url = parse(url)
        self.mode = mode
        self.config = config or Config()
        self._readable, self._writable = _mode_access(mode)
        self._pos = 0
        self._size: int | None = None
        self._eof_at: int | None = None  # where the resource is known to end
        self._body: Response | None = None  # the live GET response
        self._buffer = bytearray()  # pending PUT body, while it still fits
        self._upload: Upload | None = None  # mid-chunked-PUT
        #: Cleared the first time the server shows it will not serve several
        #: ranges at once, so the rest of a file's vector reads skip the attempt.
        self._multirange = self.multirange
        self._owns_client = client is None
        self.client = client or HTTPClient(self.config)
        self._check_exclusive()
        self._done = False

    def _check_exclusive(self) -> None:
        """For ``x`` mode, refuse a resource that is already there.

        A ``HEAD`` here gives the error at ``open`` time, as the builtin
        does; the ``If-None-Match: *`` the ``PUT`` carries
        (:meth:`_conditional`) closes the race with another writer.
        """
        try:
            exists = self._writable and "x" in self.mode and self._exists()
            if exists:
                raise ExistsError(kXR_ItExists, "file exists", path=self.url.path)
        except BaseException:
            self._release_client()
            raise

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def readable(self) -> bool:
        """Whether the mode allows reading (``r``)."""
        return self._readable

    def writable(self) -> bool:
        """Whether the mode allows writing (``w`` or ``x``)."""
        return self._writable

    def seekable(self) -> bool:
        """Readers seek (a new ranged ``GET``); a ``PUT`` body only goes forward."""
        return self._readable

    def fileno(self) -> int:
        """Always raises :class:`io.UnsupportedOperation`: there is no descriptor."""
        # UnsupportedOperation, not a bare OSError: ``TextIOWrapper`` has
        # probed ``fileno()`` while looking for a console encoding and
        # forgives only that exception, and it is what :mod:`io`'s own
        # descriptor-less streams raise.
        raise io.UnsupportedOperation("an HTTP file has no descriptor")

    def __repr__(self) -> str:
        """URL and mode, with credentials in the query redacted (see ``HTTPFileSystem``)."""
        return f"HTTPRawIO({redact(str(self.url))!r}, mode={self.mode!r})"

    @property
    def size(self) -> int:
        """Content length of the remote resource, fetched once."""
        if self._size is None:
            # The HEAD goes down the same pooled connection, so a response
            # still being read has to be given up first; the next read starts
            # a fresh one from where this one had got to.
            self._drop_stream()
            response = self.client.request("HEAD", self.url)
            self._size = response.content_length or 0
            if response.content_length is not None:
                # A declared length is an end a read can stop at without
                # asking; an undeclared one is not, however it is reported.
                self._eof_at = response.content_length
        return self._size

    def _release_client(self) -> None:
        """Give back the connection a failed construction had already taken."""
        if self._owns_client:
            self.client.close()

    def _exists(self) -> bool:
        """``HEAD`` the URL: ``False`` on ``404``, any other failure raised."""
        from ..errors import NotFoundError

        try:
            self.client.request("HEAD", self.url)
        except NotFoundError:
            return False
        return True

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def _stream(self) -> Response | None:
        """The open response positioned at ``_pos``, started if need be, or
        ``None`` when the server says there is nothing at or after it.

        Large reads of the body go straight to the socket
        (:class:`~xrdhttp.wire.DirectReader`, inside the session's
        response), which is what lets several streams of a copy share the
        GIL rather than queue for it.
        """
        if self._body is None:
            headers = {"Range": f"bytes={self._pos}-"} if self._pos else {}
            response = self.client.open("GET", self.url, headers=headers, expect=(200, 206, 416))
            if response.status == 416:
                # The range starts at or past the end: what a file answers
                # there is an empty read, not an error.
                self._release(response)
                return None
            if self._pos and response.status == 200:
                # The server ignored the range; skip forward rather than
                # hand back the wrong bytes.
                _log.debug("%s ignored a Range header", self.url.host)
                response.read(self._pos)
            self._body = response
        return self._body

    def readinto(self, buffer: WriteableBuffer) -> int:
        """Read up to ``len(buffer)`` bytes at the position; ``0`` only at end of file.

        May return fewer bytes than asked, as a raw stream does. A dropped
        connection is resumed transparently up to
        :attr:`Config.connect_retries` times; a timeout or a busy server is
        not, since repeating would only wait again.
        """
        if not self._readable:
            raise io.UnsupportedOperation("not readable")
        # At or past a known end - one a read reached, or the length a HEAD
        # declared - a read is local EOF. Do not turn it into
        # ``Range: bytes=<size>-``: that range is unsatisfiable and a
        # compliant server answers it with 416 rather than an empty body.
        if self._eof_at is not None and self._pos >= self._eof_at:
            return 0
        # A GET that drops mid-body is resumed with a fresh ranged GET from
        # ``_pos``, the last byte handed back - so a flaky link costs a
        # reconnect, not the read. Only bytes already committed are ever
        # counted, so a resume never double-reads or skips.
        attempts = 0
        while True:
            try:
                return self._read_live(buffer)
            except (XRDTimeoutError, WaitLimitError):
                raise  # a stall or a busy server, not a link that dropped
            except XRDConnectionError as exc:
                attempts += 1
                if attempts > self.config.connect_retries:
                    raise
                _log.debug("resuming %s at %d after %s", self.url.path, self._pos, exc)
                self._backoff(attempts)

    def _read_live(self, buffer: WriteableBuffer) -> int:
        """One read from the open stream; a drop mid-body raises TransientError."""
        stream = self._stream()
        if stream is None:
            return 0
        try:
            count = stream.readinto(buffer)
        except XRDTimeoutError:
            raise  # a stall, not a drop: the caller does not resume it
        except TransientError as exc:
            # The body ended before its declared length: a dropped
            # connection, not the end of the file - returning 0 would hand
            # the caller a silently short read. Resumed from ``_pos``.
            short = stream.owed
            self._drop_stream()
            raise TransientError(
                f"connection closed {short} bytes short of the declared "
                f"Content-Length reading {self.url.path}",
                committed=self._pos,
            ) from exc
        if not count and memoryview(buffer).nbytes:
            self._drop_stream()
            self._size = self._eof_at = self._pos
        self._pos += count
        return count

    def _backoff(self, attempts: int) -> None:
        """Sleep before resume ``attempts``, by the package's shared backoff rule."""
        _retry.pause(attempts, self.config.retry_backoff, self.config.wait_cap)

    def readv(self, ranges: Iterable[ReadRange | tuple[int, int]]) -> list[bytes]:
        """Many ``(offset, length)`` ranges, in as few requests as the server allows.

        One ``bytes`` per range, in the order asked, each short only where
        the file ends. The ranges go out together - up to
        :data:`~xrdhttp.ranges.MAX_SPANS` in a ``Range`` header -
        and come back as ``multipart/byteranges``. A server that serves only
        one range, or ignores ``Range`` altogether, is asked for the rest
        one range at a time. The file position does not move.
        """
        if not self._readable:
            raise io.UnsupportedOperation("not readable")
        wanted = [_as_pair(item) for item in ranges]
        self.config.check_whole_read(sum(length for _offset, length in wanted), self.url.path)
        merged = _ranges.spans(wanted)
        # The pooled connection is about to carry other requests.
        self._drop_stream()
        for batch in _ranges.batches(merged):
            if self._multirange and len(batch) > 1:
                self._read_multirange(batch)
            for span in batch:
                if span.data is None:
                    span.data = self._read_span(span.start, span.length)
        return _ranges.assemble(merged, wanted)

    def _read_multirange(self, batch: list[_ranges.Span]) -> None:
        """Ask for every span of ``batch`` at once; fill whatever comes back."""
        response = self.client.open(
            "GET",
            self.url,
            headers={"Range": _ranges.range_header(batch)},
            expect=(200, 206, 416),
        )
        try:
            self._multirange = self._take_ranges(response, batch)
        finally:
            # A whole file sent in place of the ranges is not read: the
            # connection goes instead, and the spans come one at a time.
            response.close()

    def _take_ranges(self, response: Response, batch: list[_ranges.Span]) -> bool:
        """Fill ``batch`` from a multi-range answer; whether to ask that way again."""
        if response.status != 206:
            # 200 is a server that ignores Range; 416, ranges past the end,
            # which the single-range path reads as the end of the file.
            return response.status == 416
        delimiter = _ranges.boundary(response)
        assembly = _ranges.Assembly(batch)
        if delimiter is not None:
            _ranges.read_multipart(response, delimiter, assembly)
            return True
        single = _ranges.content_range(response.header("Content-Range"))
        if single is None:
            raise ProtocolError(f"{self.url.host} answered a range request with no Content-Range")
        assembly.accept(*single, response.read)
        # One part for every span is a server that coalesced them, and will
        # again; one for the first alone is a server that serves only one.
        return all(span.data is not None for span in batch)

    def _read_span(self, offset: int, length: int) -> bytearray:
        """``length`` bytes at ``offset`` with a single-range ``GET``; short at the end."""
        response = self.client.open(
            "GET",
            self.url,
            headers={"Range": f"bytes={offset}-{offset + length - 1}"},
            expect=(200, 206, 416),
        )
        try:
            if response.status == 416:
                return bytearray()
            if response.status == 200:
                _skip(response, offset)  # the whole file came: skip to the range
            else:
                served = _ranges.content_range(response.header("Content-Range"))
                if served is None or served[0] != offset:
                    raise ProtocolError(
                        f"{self.url.host} answered a request for bytes {offset}- with "
                        f"{response.header('Content-Range')!r}"
                    )
            return bytearray(response.read(length))
        finally:
            response.close()

    def readall(self) -> bytes:
        """The rest of the file, bounded by :meth:`Config.check_whole_read`."""
        # Counted as it arrives rather than asked for up front: a HEAD here
        # would cost a request per whole-file read, and a chunked response
        # does not say how long it is until it ends.
        chunks = []
        total = 0
        while data := self.read(self.config.chunk_size):
            total += len(data)
            self.config.check_whole_read(total, self.url.path)
            chunks.append(data)
        return b"".join(chunks)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Move the read position; the open ``GET`` is dropped only if it moves.

        Seeking past the end is allowed, as for a local file; the next read
        there returns ``b""``.
        """
        if not self._readable:
            raise io.UnsupportedOperation("not seekable")
        # Lazily, because SEEK_END is the only one that has to ask the server
        # how big the resource is, and that costs a HEAD.
        if whence == io.SEEK_SET:
            base = 0
        elif whence == io.SEEK_CUR:
            base = self._pos
        elif whence == io.SEEK_END:
            base = self.size
        else:
            raise ValueError(f"invalid whence {whence}, expected 0, 1 or 2")
        target = base + offset
        if target < 0:
            raise OSError(f"negative seek to {target}")
        if target != self._pos:
            self._drop_stream()
            self._pos = target
        return self._pos

    def tell(self) -> int:
        """The position: bytes read (or written) so far, after any seek."""
        return self._pos

    def _drop_stream(self) -> None:
        """Give up the response being read, if there is one.

        Closing a response does not close its connection, and one abandoned
        mid-body - by a seek, a ``HEAD`` for the size, or a close - leaves the
        rest of the body on the socket. The pool would hand that connection
        to the next request, which would read the leftover bytes as its
        status line; a ``POST`` is not retried, so it would fail outright.
        """
        body, self._body = self._body, None
        if body is not None:
            self._release(body)

    def _release(self, body: Response) -> None:
        """Close a response: a short rest is read out to keep its connection, more costs it."""
        body.close()

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def write(self, data: ReadableBuffer) -> int:
        """Accept ``data`` for the ``PUT``; always takes all of it.

        Held in memory up to :meth:`_stream_after`, then streamed as chunks.
        A failure on the wire surfaces here or, for a buffered file, at
        :meth:`close`, which is when the single ``PUT`` is sent.
        """
        if not self._writable:
            raise io.UnsupportedOperation("not writable")
        payload = bytes(data)
        if not payload:
            # Nothing to do - and in a chunked upload an empty chunk is the
            # end-of-body marker, which would commit a truncated file.
            return 0
        if not self._streaming:
            self._buffer += payload
            if len(self._buffer) <= self._stream_after():
                return len(payload)
            self._begin_upload()  # too big to hold: switch to streaming
        else:
            self._send_chunk(payload)
        self._pos += len(payload)
        return len(payload)

    @property
    def _streaming(self) -> bool:
        """Has the upload started? A subclass whose upload is a series of
        requests rather than one open connection says so its own way."""
        return self._upload is not None

    def _stream_after(self) -> int:
        """How much may be held in hand before the upload has to start.

        A subclass whose protocol has a minimum piece size - S3's multipart
        upload does - raises this so that the first piece is a legal one.
        """
        return self.config.chunk_size

    def _begin_upload(self) -> None:
        """Start a chunked ``PUT`` and flush whatever is already buffered."""
        # The body is about to be streamed, so a signer that would hash it
        # has nothing to hash yet. The headers go first and alone: a server
        # that redirects the upload (EOS, to the disk server that will hold
        # the file) says so before any of it is sent to the wrong host.
        self._upload, self.url = self.client.begin_upload("PUT", self.url, self._conditional())
        pending, self._buffer = bytes(self._buffer), bytearray()
        if pending:
            self._send_chunk(pending)

    def _send_chunk(self, payload: bytes) -> None:
        """One chunk of the chunked body (the session frames it, RFC 9112 section 7.1)."""
        upload = self._upload
        assert upload is not None  # only called between _begin_upload and _finish_upload
        upload.write(payload)

    def _conditional(self) -> dict[str, str]:
        """``x`` mode asks the server to refuse an existing resource, too."""
        return {"If-None-Match": "*"} if "x" in self.mode else {}

    def _finish_upload(self) -> None:
        """Send the trailer and read the verdict of a streamed ``PUT``."""
        upload, self._upload = self._upload, None
        assert upload is not None  # guarded by the caller: there is an upload to finish
        # The last-chunk (size 0) ends the body; the answer is read whole.
        response = upload.finish()
        response.body()
        # A redirect after the body has gone cannot be followed: the body is
        # not kept. Servers that redirect uploads do it at ``100-continue``
        # time, which :meth:`HTTPClient.begin_upload` already followed.
        if response.status in (307, 308, 302, 301):
            raise ProtocolError(
                f"{self.url.host} redirected a streamed upload to "
                f"{response.header('Location')}; PUT that URL directly"
            )
        if response.status not in _WRITE_OK:
            check_status(response.status, response.reason, self.url, _WRITE_OK, None)

    def _put_buffered(self) -> None:
        """One ``PUT`` with a length, which can be redirected and retried."""
        self.client.request(
            "PUT",
            self.url,
            body=bytes(self._buffer),
            headers={"Content-Length": str(len(self._buffer)), **self._conditional()},
            expect=_WRITE_OK,
        )
        self._buffer = bytearray()

    def flush(self) -> None:
        """Nothing to do: a ``PUT`` is only complete when the file closes."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Finish the upload, if any, then release the connection. Idempotent."""
        if self._done:
            return
        self._done = True
        try:
            if self._writable:
                self._finish_upload() if self._streaming else self._put_buffered()
        finally:
            self._drop_stream()
            if self._owns_client:
                self.client.close()
            super().close()


def _as_pair(item: ReadRange | tuple[int, int]) -> tuple[int, int]:
    """One requested range as ``(offset, length)``, checked."""
    offset, length = (item.offset, item.length) if isinstance(item, ReadRange) else item
    if offset < 0 or length < 0:
        raise ValueError(
            f"invalid range ({offset}, {length}): offsets and lengths are not negative"
        )
    return offset, length


def _skip(response: _ranges.MultipartAnswer, count: int) -> None:
    """Read and discard ``count`` bytes, a chunk at a time, from any answer's body."""
    while count > 0:
        chunk = response.read(min(count, 1 << 20))
        if not chunk:
            return
        count -= len(chunk)


# ``buffering=0`` overlaps the buffered overload the same way it does for the
# builtin; see :func:`xrdclient.io.open_url`.
@overload
def open_http(  # type: ignore[overload-overlap]
    url: str | XRootDURL,
    mode: OpenBinaryMode = ...,
    *,
    buffering: Literal[0],
    encoding: None = ...,
    errors: None = ...,
    newline: None = ...,
    config: Config | None = ...,
    client: HTTPClient | None = ...,
    raw: type[HTTPRawIO] = ...,
) -> HTTPRawIO: ...


@overload
def open_http(
    url: str | XRootDURL,
    mode: OpenBinaryMode = ...,
    *,
    buffering: int = ...,
    encoding: None = ...,
    errors: None = ...,
    newline: None = ...,
    config: Config | None = ...,
    client: HTTPClient | None = ...,
    raw: type[HTTPRawIO] = ...,
) -> BinaryIO: ...


@overload
def open_http(
    url: str | XRootDURL,
    mode: OpenTextMode,
    *,
    buffering: int = ...,
    encoding: str | None = ...,
    errors: str | None = ...,
    newline: str | None = ...,
    config: Config | None = ...,
    client: HTTPClient | None = ...,
    raw: type[HTTPRawIO] = ...,
) -> TextIO: ...


@overload
def open_http(
    url: str | XRootDURL,
    mode: str,
    *,
    buffering: int = ...,
    encoding: str | None = ...,
    errors: str | None = ...,
    newline: str | None = ...,
    config: Config | None = ...,
    client: HTTPClient | None = ...,
    raw: type[HTTPRawIO] = ...,
) -> IO[Any]: ...


def open_http(
    url: str | XRootDURL,
    mode: str = "rb",
    *,
    buffering: int = -1,
    encoding: str | None = None,
    errors: str | None = None,
    newline: str | None = None,
    config: Config | None = None,
    client: HTTPClient | None = None,
    raw: type[HTTPRawIO] = HTTPRawIO,
) -> IO[Any] | io.RawIOBase:
    """Open an ``http``/``https``/``dav``/``davs`` URL like :func:`open`.

        >>> with open_http("https://dav.example.org/store/f.txt") as fh:
        ...     for line in fh:
        ...         ...

    The return type follows the builtin's: raw at ``buffering=0``, a buffered
    binary stream otherwise, wrapped in a text layer unless the mode says
    ``b``. ``x`` refuses an existing resource; ``a`` cannot be honoured over
    HTTP and raises.

    ``raw`` names the class that does the talking, which is how
    :class:`xrdclient.s3.S3RawIO` gets S3's upload without a second copy of the
    buffering, text and mode handling around it.
    """
    from ..io import parse_mode

    _base, binary, updating = parse_mode(mode)
    _validate_open_layer(binary, updating, buffering, encoding)
    handle = raw(url, mode, config=config, client=client)
    if buffering == 0:
        return handle  # binary only: unbuffered text was refused above
    stream = _buffered(handle, buffering)
    if binary:
        return stream
    return io.TextIOWrapper(stream, encoding=encoding, errors=errors, newline=newline)


def _validate_open_layer(
    binary: bool, updating: bool, buffering: int, encoding: str | None
) -> None:
    """The builtin :func:`open`'s argument checks, with its own messages.

    Run before the raw file is made, so a bad combination costs no request.
    ``+`` modes are refused: HTTP can replace a resource or read it, but has
    no way to change bytes in the middle of one.
    """
    if not binary and buffering == 0:
        raise ValueError("can't have unbuffered text I/O")
    if binary and encoding is not None:
        raise ValueError("binary mode doesn't take an encoding argument")
    if updating:
        raise UnsupportedError(
            kXR_Unsupported, "HTTP has no partial update; open for reading or for writing"
        )


def _buffered(handle: HTTPRawIO, buffering: int) -> BinaryIO:
    """``handle`` behind the buffered layer its direction calls for.

    A negative ``buffering`` is "the default", :data:`DEFAULT_BUFFER_SIZE`.
    Never a :class:`io.BufferedRandom`: no mode reaches here that both reads
    and writes (``+`` is refused by :func:`_validate_open_layer`).
    """
    size = DEFAULT_BUFFER_SIZE if buffering < 0 else buffering
    stream: BinaryIO = (
        io.BufferedWriter(handle, size) if handle.writable() else io.BufferedReader(handle, size)
    )
    return stream
