"""Helpers for testing code that talks to XRootD.

    from xrdclient.testing import FakeServer

    with FakeServer(files={"/data/a.root": b"hello"}) as server:
        fs = xrdclient.FileSystem(server.url)
        assert fs.read_bytes("/data/a.root") == b"hello"

:class:`FakeDAVServer` is the same idea for ``http``, ``https`` and WebDAV,
:class:`FakeS3Server` for ``s3://`` buckets, and :class:`FaultProxy` is the
network those servers do not have: put it in front of any of them and the
connection can be made to drop, stall, corrupt or reorder on cue.

Nothing here is imported by the library itself, so it costs nothing at run
time; it is a supported part of the public API all the same, because a client
library that cannot be tested without a storage element is not much use.
"""

from __future__ import annotations

from .faults import FaultProxy
from .http import FakeDAVServer
from .s3 import FakeS3Server
from .server import FakeServer, error, frame, from_directory, pgwrite_cse

__all__ = [
    "FakeServer",
    "FakeDAVServer",
    "FakeS3Server",
    "FaultProxy",
    "frame",
    "error",
    "from_directory",
    "pgwrite_cse",
]
