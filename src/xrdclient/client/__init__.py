"""The explicit client layer: one method per protocol operation.

This is where a public call becomes a ``kXR_*`` request. Start reading at
:mod:`.filesystem` (namespace operations, plus the scheme dispatch in
``FileSystem.__new__``) and :mod:`.file` (one open handle, assembled from
:mod:`._handle`, :mod:`._reading` and :mod:`._writing`). Everything below
goes through a :class:`~xrdclient.session.router.Router`, which owns the
connection, redirects and ``kXR_wait`` handling; nothing in this package
touches a socket directly. :mod:`.bulk` is the parallel, restartable
transfer built on the session layer's fast data plane.

What must not break: :class:`BaseFileSystem` is the shared contract the
``root://``, WebDAV and S3 filesystems implement - a method added to one
belongs on the base class, or the three drift apart.
"""

from __future__ import annotations

from .bulk import BulkResult, download, stream
from .file import Checkpoint, File
from .filesystem import BaseFileSystem, FileSystem

__all__ = ["BaseFileSystem", "BulkResult", "Checkpoint", "File", "FileSystem", "download", "stream"]
