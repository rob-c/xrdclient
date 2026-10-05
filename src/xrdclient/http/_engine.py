"""Request lifecycle shared by the HTTP facades.

Adapters own credentials, URL spelling, replay policy and numeric errors.
This module owns bounded attempts, redirect iteration and resource cleanup.
"""

from __future__ import annotations

import http.client
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

URL = TypeVar("URL")
Body = TypeVar("Body")
Exchange = TypeVar("Exchange")
Result = TypeVar("Result")
Response = TypeVar("Response", bound="HTTPResponse")

REDIRECTS = frozenset({301, 302, 303, 307, 308})


class HTTPResponse(Protocol):
    status: int


@dataclass
class Request(Generic[URL, Body]):
    method: str
    url: URL
    body: Body | None = None
    credentials: bool = True
    relay: bool = True


def redirects(
    request: Request[URL, Body],
    send: Callable[[Request[URL, Body]], Response],
    advance: Callable[[Request[URL, Body], Response], URL | None],
    limit: int,
    exhausted: Callable[[], Exception],
) -> Response:
    for _ in range(limit + 1):
        response = send(request)
        target = advance(request, response)
        if target is None:
            return response
        request.url = target
        if response.status == 303:
            request.method, request.body = "GET", None
    raise exhausted()


def attempt(
    acquire: Callable[[], Exchange],
    send: Callable[[Exchange], Result],
    abort: Callable[[Exchange], None],
    retry: Callable[[Exchange, Exception], bool],
    wrap: Callable[[Exception], Exception],
) -> Result:
    retried = False
    while True:
        exchange = acquire()
        try:
            return send(exchange)
        except (OSError, http.client.HTTPException) as exc:
            abort(exchange)
            if not retried and retry(exchange, exc):
                retried = True
                continue
            raise wrap(exc) from exc
        except BaseException:
            abort(exchange)
            raise
