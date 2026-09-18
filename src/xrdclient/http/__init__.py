"""HTTP, HTTPS and WebDAV - the other way HEP storage is spoken.

    >>> import xrdclient
    >>> with xrdclient.open("davs://dav.example.org/store/f.root") as fh:
    ...     header = fh.read(1024)
    >>> xrdclient.FileSystem("https://dav.example.org").listdir("/store")

Nothing here needs a wheel: :mod:`http.client` and :mod:`xml.etree` do the
work. The public entry points are the same ones the ``root://`` side offers -
:func:`~xrdclient.open`, :class:`~xrdclient.FileSystem`, :class:`~xrdclient.XRootDPath` - which
dispatch on the URL scheme, so an application changes a URL and nothing else.
"""

from __future__ import annotations

from .client import HTTPClient, Response, bearer_token, check_status, status_code
from .dav import HTTPFileSystem, digest, macaroon, propfind
from .file import HTTPRawIO, open_http
from .tpc import Marker, third_party

__all__ = [
    "open_http",
    "digest",
    "macaroon",
    "propfind",
    "third_party",
    "HTTPFileSystem",
    "HTTPClient",
    "HTTPRawIO",
    "Marker",
    "Response",
    "bearer_token",
    "check_status",
    "status_code",
]
