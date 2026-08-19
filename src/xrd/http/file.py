"""File objects over HTTP, with :func:`open`'s semantics.

Reading is a ranged ``GET`` that stays open, so a seek costs a new request
and a sequential read costs none. Writing is a ``PUT``: small files go in one
request with a ``Content-Length`` (which lets the client follow a redirect,
the way a storage element in front of a pool node needs), and anything larger
switches to a chunked upload that streams without ever holding the file in
memory.
"""

from __future__ import annotations

import http.client
import io
from typing import IO, TYPE_CHECKING, Any, BinaryIO, Literal, TextIO, overload

from .._log import get_logger
from ..config import Config
from ..errors import (
    ExistsError,
    ProtocolError,
    TransientError,
    UnsupportedError,
    kXR_ItExists,
    kXR_Unsupported,
)
from ..io.raw import OpenBinaryMode, OpenTextMode
from ..url import XRootDURL, parse
from .client import HTTPClient, check_status, request_target

if TYPE_CHECKING:
    from _typeshed import ReadableBuffer, WriteableBuffer

__all__ = ["HTTPRawIO", "open_http", "DEFAULT_BUFFER_SIZE"]

_log = get_logger(__name__)

DEFAULT_BUFFER_SIZE = 1 << 20

_WRITE_OK = (200, 201, 204)


class HTTPRawIO(io.RawIOBase):
    """Unbuffered binary I/O against one HTTP URL.

    Instances are what :func:`open_http` wraps; use that unless you want the
    raw layer, in which case remember that :meth:`read` may come back short.
    """

    def __init__(
        self,
        url: str | XRootDURL,
        mode: str = "rb",
        *,
        config: Config | None = None,
        client: HTTPClient | None = None,
    ) -> None:
        # Closed until the constructor finishes: a file that never opened must
        # not try to PUT anything when the interpreter collects it.
        self._done = True
        self.url = parse(url)
        self.mode = mode
        self.config = config or Config()
        self._readable = "r" in mode or "+" in mode
        self._writable = any(ch in mode for ch in "wxa+")
        self._pos = 0
        self._size: int | None = None
        self._body: http.client.HTTPResponse | None = None  # the live GET response
        self._buffer = bytearray()  # pending PUT body, while it still fits
        self._upload: http.client.HTTPConnection | None = None  # mid-chunked-PUT
        if "a" in mode:  # before a connection is taken, so none has to be given back
            raise UnsupportedError(
                kXR_Unsupported, "HTTP has no append; read, concatenate, and PUT instead"
            )
        self._owns_client = client is None
        self.client = client or HTTPClient(self.config)
        try:
            if self._writable and "x" in mode and self._exists():
                raise ExistsError(kXR_ItExists, "file exists", path=self.url.path)
        except BaseException:
            self._release_client()
            raise
        self._done = False

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def readable(self) -> bool:
        return self._readable

    def writable(self) -> bool:
        return self._writable

    def seekable(self) -> bool:
        return self._readable

    def fileno(self) -> int:
        # UnsupportedOperation, not a bare OSError: on Python 3.10
        # ``TextIOWrapper`` probes ``fileno()`` to look for a console encoding
        # and only forgives that one exception, so text mode depends on it.
        raise io.UnsupportedOperation("an HTTP file has no descriptor")

    def __repr__(self) -> str:
        return f"HTTPRawIO({str(self.url)!r}, mode={self.mode!r})"

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
        return self._size

    def _release_client(self) -> None:
        """Give back the connection a failed construction had already taken."""
        if self._owns_client:
            self.client.close()

    def _exists(self) -> bool:
        from ..errors import NotFoundError

        try:
            self.client.request("HEAD", self.url)
        except NotFoundError:
            return False
        return True

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def _stream(self) -> http.client.HTTPResponse:
        """The open response positioned at ``_pos``, started if need be."""
        if self._body is None:
            headers = {"Range": f"bytes={self._pos}-"} if self._pos else {}
            response = self.client.open("GET", self.url, headers=headers, expect=(200, 206))
            if self._pos and response.status == 200:
                # The server ignored the range; skip forward rather than
                # hand back the wrong bytes.
                _log.debug("%s ignored a Range header", self.url.host)
                response.read(self._pos)
            self._body = response
        return self._body

    def readinto(self, buffer: WriteableBuffer) -> int:
        if not self._readable:
            raise io.UnsupportedOperation("not readable")
        stream = self._stream()
        count = stream.readinto(buffer)
        if not count and memoryview(buffer).nbytes:
            # End of stream. If the response declared a length it has not
            # delivered, this is a dropped connection, not the end of the
            # file - and returning 0 here would hand the caller a silently
            # short read.
            short = stream.length
            self._drop_stream()
            if short:
                raise TransientError(
                    f"connection closed {short} bytes short of the declared "
                    f"Content-Length reading {self.url.path}",
                    committed=self._pos,
                )
        self._pos += count
        return count

    def readall(self) -> bytes:
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
        return self._pos

    def _drop_stream(self) -> None:
        body, self._body = self._body, None
        if body is not None:
            body.close()

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def write(self, data: ReadableBuffer) -> int:
        if not self._writable:
            raise io.UnsupportedOperation("not writable")
        payload = bytes(data)
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
        conn = self.client.connection(self.url)
        # ``None`` for the body: it is about to be streamed, so a signer that
        # would hash it has nothing to hash yet.
        headers = self.client.sign(
            "PUT", self.url, self.client.headers_for(self.url, self._conditional()), None
        )
        conn.putrequest("PUT", request_target(self.url))
        for name, value in headers.items():
            conn.putheader(name, value)
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        self._upload = conn
        pending, self._buffer = bytes(self._buffer), bytearray()
        if pending:
            self._send_chunk(pending)

    def _send_chunk(self, payload: bytes) -> None:
        conn = self._upload
        assert conn is not None  # only called between _begin_upload and _finish_upload
        conn.send(b"%x\r\n" % len(payload))
        conn.send(payload)
        conn.send(b"\r\n")

    def _conditional(self) -> dict[str, str]:
        """``x`` mode asks the server to refuse an existing resource, too."""
        return {"If-None-Match": "*"} if "x" in self.mode else {}

    def _finish_upload(self) -> None:
        """Send the trailer and read the verdict of a streamed ``PUT``."""
        conn, self._upload = self._upload, None
        assert conn is not None  # guarded by the caller: there is an upload to finish
        conn.send(b"0\r\n\r\n")
        response = conn.getresponse()
        try:
            response.read()
        finally:
            response.close()
        if response.status in (307, 308, 302, 301):
            raise ProtocolError(
                f"{self.url.host} redirected a streamed upload to "
                f"{response.getheader('Location')}; PUT that URL directly"
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


# ``buffering=0`` overlaps the buffered overload the same way it does for the
# builtin; see :func:`xrd.io.open_url`.
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
    :class:`xrd.s3.S3RawIO` gets S3's upload without a second copy of the
    buffering, text and mode handling around it.
    """
    from ..io import parse_mode

    _base, binary, updating = parse_mode(mode)
    if not binary and buffering == 0:
        raise ValueError("can't have unbuffered text I/O")
    if binary and encoding is not None:
        raise ValueError("binary mode doesn't take an encoding argument")
    if updating:
        raise UnsupportedError(
            kXR_Unsupported, "HTTP has no partial update; open for reading or for writing"
        )

    handle = raw(url, mode, config=config, client=client)
    if buffering == 0:
        return handle
    size = DEFAULT_BUFFER_SIZE if buffering < 0 else buffering
    stream: BinaryIO = (
        io.BufferedWriter(handle, size) if handle.writable() else io.BufferedReader(handle, size)
    )
    if binary:
        return stream
    return io.TextIOWrapper(stream, encoding=encoding, errors=errors, newline=newline)
