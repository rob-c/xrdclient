"""The sans-io protocol layer: constants, buffers, frames, codecs, state machine.

Nothing here touches a socket. Everything is a pure function of bytes, which
is what lets the same implementation back both the blocking and the asyncio
front ends.
"""

from __future__ import annotations

from . import constants, requests, responses
from .buffer import Reader, Writer
from .frames import HANDSHAKE, Request, ResponseHeader, decode_header, encode

__all__ = [
    "constants",
    "requests",
    "responses",
    "Reader",
    "Writer",
    "Request",
    "ResponseHeader",
    "HANDSHAKE",
    "encode",
    "decode_header",
]
