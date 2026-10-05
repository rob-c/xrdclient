"""urllib3 connections with the clients' streaming response contract.

Keep stdlib response objects for GSI/Expect: 100-continue and direct socket
reads. Connection establishment, socket options and TLS are owned by urllib3.
"""

from __future__ import annotations

import errno
import http.client
from collections.abc import Callable
from typing import Any, cast

from urllib3.connection import HTTPConnection as _HTTPConnection
from urllib3.connection import HTTPSConnection as _HTTPSConnection
from urllib3.exceptions import ConnectTimeoutError, HTTPError


def _connect(connect: Callable[[], None]) -> None:
    try:
        connect()
    except HTTPError as exc:
        if isinstance(exc.__cause__, OSError):
            raise exc.__cause__ from exc
        if isinstance(exc, ConnectTimeoutError):
            raise TimeoutError(str(exc)) from exc
        raise OSError(errno.EIO, str(exc)) from exc


class _ResponseCompatibility:
    def request(
        self,
        method: str,
        url: str,
        body: Any = None,
        headers: Any = None,
        *,
        encode_chunked: bool = False,
    ) -> None:
        http.client.HTTPConnection.request(
            cast("http.client.HTTPConnection", self),
            method,
            url,
            body,
            headers or {},
            encode_chunked=encode_chunked,
        )

    def getresponse(self) -> http.client.HTTPResponse:
        return http.client.HTTPConnection.getresponse(cast("http.client.HTTPConnection", self))


class HTTPConnection(_ResponseCompatibility, _HTTPConnection):  # type: ignore[misc]
    def connect(self) -> None:
        _connect(super().connect)


class HTTPSConnection(_ResponseCompatibility, _HTTPSConnection):  # type: ignore[misc]
    def connect(self) -> None:
        _connect(super().connect)
