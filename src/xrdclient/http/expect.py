"""``Expect: 100-continue`` - ask before sending a body the server may not want.

A grid storage element often answers an upload before reading it. EOS's
head node replies to a ``PUT`` with ``307`` to the disk server that will hold
the file, at once, and closes; a client that has meanwhile started pushing
megabytes finds the connection gone under it (``SSL: BAD_LENGTH``, or a bare
``[SYS] unknown error``) and never reads the redirect it was sent. curl and
davix avoid that by sending the headers alone with ``Expect: 100-continue``
and waiting for the server's go-ahead, and so does this module.

:mod:`http.client` cannot: its ``getresponse`` skips a ``100 Continue``
itself and then waits for a final answer that will not come until the body
is sent. So the interim response is read here, straight off the socket and a
byte at a time - never past its blank line, so that nothing belonging to the
final response is consumed - and then:

* ``100``: the body goes out and the final response is read as usual;
* anything else: that *is* the final response, the body is never sent, and
  it is handed back so that a redirect is followed like any other;
* nothing within :data:`CONTINUE_WAIT` seconds: a server that ignores the
  expectation, which RFC 9110 allows; the body is sent anyway, as curl does.
"""

from __future__ import annotations

import http.client
import io
import socket
from typing import Any

from .._compat import TIMEOUTS

__all__ = ["CONTINUE_WAIT", "EXPECT_OVER", "InterimAnswer", "await_continue", "wants_expect"]

#: How long to wait for ``100 Continue`` before sending the body regardless:
#: curl's default (``--expect100-timeout``).
CONTINUE_WAIT = 1.0

#: Bodies at least this long are announced first. A smaller one fits in the
#: socket's buffers whether or not the server reads it, so it gains nothing
#: from the extra round trip.
EXPECT_OVER = 16 * 1024

#: An interim response longer than this is not one.
_HEAD_LIMIT = 64 * 1024


def wants_expect(method: str, length: int | None) -> bool:
    """Whether a request with a body of ``length`` bytes should ask first.

    ``None`` is a body of unknown length - a chunked upload - which is
    always worth asking about.
    """
    return method in ("PUT", "POST") and (length is None or length >= EXPECT_OVER)


class InterimAnswer(Exception):
    """The server answered before the body was sent, with ``response``."""

    def __init__(self, response: http.client.HTTPResponse) -> None:
        super().__init__(f"server answered {response.status} before the body was sent")
        self.response = response


class _Replay(io.RawIOBase):
    """The bytes already read off the socket, then the socket itself."""

    def __init__(self, head: bytes, sock: socket.socket) -> None:
        self._head = head
        self._sock = sock

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        """Closing the response closes the connection it was read from.

        The request it answers was never finished, so the socket cannot
        carry another; nothing else will close it.
        """
        if not self.closed:
            self._sock.close()
        super().close()

    def readinto(self, buffer: Any) -> int:
        view = memoryview(buffer).cast("B")
        if self._head:
            count = min(len(view), len(self._head))
            view[:count] = self._head[:count]
            self._head = self._head[count:]
            return count
        return self._sock.recv_into(view)


class _ReplaySocket:
    """Just enough of a socket for :class:`http.client.HTTPResponse` to read."""

    def __init__(self, head: bytes, sock: socket.socket) -> None:
        self._raw = _Replay(head, sock)

    def makefile(self, *_args: object, **_kwargs: object) -> io.BufferedReader:
        return io.BufferedReader(self._raw)


def _read_head(sock: socket.socket, wait: float) -> bytes | None:
    """The interim response's status line and headers; ``None`` if none came."""
    previous = sock.gettimeout()
    head = bytearray()
    try:
        sock.settimeout(wait)
        try:
            first = sock.recv(1)
        except TIMEOUTS:
            return None
        sock.settimeout(previous)
        if not first:
            raise http.client.RemoteDisconnected("closed instead of answering")
        head += first
        while not head.endswith(b"\r\n\r\n") and not head.endswith(b"\n\n"):
            if len(head) > _HEAD_LIMIT:
                raise http.client.LineTooLong("interim response")
            byte = sock.recv(1)
            if not byte:
                break
            head += byte
    finally:
        sock.settimeout(previous)
    return bytes(head)


def await_continue(
    conn: http.client.HTTPConnection, method: str, wait: float = CONTINUE_WAIT
) -> None:
    """After headers that said ``Expect: 100-continue``: may the body follow?

    Returns when it may - a ``100``, or silence for ``wait`` seconds. Raises
    :class:`InterimAnswer` carrying the server's final response when it has
    already given one; the connection it came on is spent, because the
    request it answers was never finished.
    """
    sock = conn.sock
    assert sock is not None  # headers were just sent on it
    head = _read_head(sock, wait)
    if head is None:
        return
    status_line = head.split(b"\n", 1)[0].split(None, 2)
    if len(status_line) >= 2 and status_line[1] == b"100":
        return
    response = http.client.HTTPResponse(
        _ReplaySocket(head, sock),  # type: ignore[arg-type]
        method=method,
    )
    response.begin()
    # Nothing more can follow on this connection: the request is half sent.
    response.will_close = True
    raise InterimAnswer(response)
