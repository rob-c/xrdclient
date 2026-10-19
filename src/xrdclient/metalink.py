"""Metalink 3 and 4 download descriptions: :mod:`xrdhttp.metalink`, in xrdclient's types.

XrdCl treats a local or remote ``.meta4``/``.metalink`` file as a virtual
redirector: the document describes exactly one file, its URLs are tried in
priority order, and its checksums describe the bytes every replica must
contain. The bounded, DTD-free parser that reads one - and the
:class:`MetalinkPolicy` under which xgfalclient reads the same documents the
way davix does - moved to xrdhttp in 0.4.0, and everything here *is*
xrdhttp's object (``xrdclient.metalink.parse_metalink is
xrdhttp.metalink.parse_metalink``; :class:`MetalinkError` is
:mod:`xrdcore.errors`' and so :mod:`xrdclient.errors`'). A descriptor's
checksums are :class:`xrdclient.ChecksumInfo` with no conversion, because
that is :class:`xrdcore.checksum.ChecksumInfo`, the type xrdhttp answers too -
equal to the ones a ``root://`` checksum query returns.

Where it is called from: :mod:`xrdclient.copy` (``copy/_metalink.py``)
loads a descriptor (at most :data:`MAX_DESCRIPTOR_SIZE` bytes) and calls
:func:`parse_metalink`, which is also exported as
:func:`xrdclient.parse_metalink`; xgfalclient's HTTP plug-in imports it,
:class:`MetalinkPolicy`, :func:`resolve` and the bounds by this name.
"""

from __future__ import annotations

from xrdhttp.metalink import (
    MAX_DESCRIPTOR_SIZE,
    MAX_REPLICAS,
    MAX_URL_SIZE,
    XRDCL,
    MetalinkError,
    MetalinkFile,
    MetalinkPolicy,
    is_metalink,
    parse_metalink,
    resolve,
)

__all__ = [
    "MAX_DESCRIPTOR_SIZE",
    "MAX_REPLICAS",
    "MAX_URL_SIZE",
    "XRDCL",
    "MetalinkError",
    "MetalinkFile",
    "MetalinkPolicy",
    "is_metalink",
    "parse_metalink",
    "resolve",
]
