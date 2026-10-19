"""S3 object storage, signed with AWS Signature Version 4.

    >>> import xrdclient
    >>> fs = xrdclient.FileSystem("s3://my-bucket")                   # doctest: +SKIP
    >>> fs.listdir("/runs/2024")                                # doctest: +SKIP
    ['a.root', 'b.root']

The connection is the package's own :class:`~xrdclient.http.HTTPClient`; what
S3 adds on top is borrowed from ``botocore`` rather than re-implemented,
through xrdhttp (``xrdclient.s3.sigv4`` is :mod:`xrdhttp.s3.sigv4` under its
old name) - its SigV4 signing steps
(:mod:`xrdhttp.s3.sigv4`) and its S3 service model for the XML answers
(:mod:`xrdhttp.s3.codec`) - because those are exactly
the parts where being byte-for-byte AWS's interpretation matters. ``boto3``,
``s3fs`` and botocore's own HTTP stack are not used. ``botocore`` is heavy, so
this package is only imported when an ``s3://`` URL is actually opened
(``tests/test_import_cost.py`` holds the line).

Where to start: :mod:`xrdclient.s3.fs` for the filesystem and the multipart
upload, :mod:`xrdhttp.s3.sigv4` for what goes in ``Authorization``.

:class:`~xrdclient.s3.S3FileSystem` is the :class:`~xrdclient.FileSystem` surface, so code
that walks a ``root://`` tree walks a bucket unchanged; where an object store
genuinely differs from a filesystem - directories that do not exist, deletes
that cannot fail - each method says so.
"""

from __future__ import annotations

from xrdhttp.s3.sigv4 import ALGORITHM, UNSIGNED_PAYLOAD, Credentials, hash_payload, sign

from .fs import MIN_PART_SIZE, S3FileSystem, S3RawIO, open_s3

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
