"""S3 object storage, signed with AWS Signature Version 4.

    >>> import xrdclient
    >>> fs = xrdclient.FileSystem("s3://my-bucket")                   # doctest: +SKIP
    >>> fs.listdir("/runs/2024")                                # doctest: +SKIP
    ['a.root', 'b.root']

Everything here is stdlib: :mod:`hmac` and :mod:`hashlib` for the signature,
:mod:`xml.etree` for the listings, and the package's own HTTP client for the
connection. No ``boto3``, no ``s3fs``, no wheel of any kind - the same promise
the rest of :mod:`xrd` makes.

:class:`~xrdclient.s3.S3FileSystem` is the :class:`~xrdclient.FileSystem` surface, so code
that walks a ``root://`` tree walks a bucket unchanged; where an object store
genuinely differs from a filesystem - directories that do not exist, deletes
that cannot fail - each method says so.
"""

from __future__ import annotations

from .fs import MIN_PART_SIZE, S3FileSystem, S3RawIO, open_s3
from .sigv4 import ALGORITHM, UNSIGNED_PAYLOAD, Credentials, hash_payload, sign

__all__ = [
    "ALGORITHM",
    "MIN_PART_SIZE",
    "UNSIGNED_PAYLOAD",
    "Credentials",
    "S3FileSystem",
    "S3RawIO",
    "hash_payload",
    "open_s3",
    "sign",
]
