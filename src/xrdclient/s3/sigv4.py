"""AWS Signature Version 4: :mod:`xrdhttp.s3.sigv4`, under xrdclient's name.

Headers and pre-signed URLs over botocore's canonicalisation moved to
xrdhttp in 0.4.0. :class:`~xrdclient.s3.S3FileSystem` signs with :func:`sign`
and xgfalclient's S3 plug-in with :func:`sign` and :func:`presign`, both
imported by this name. botocore is still imported only when this module is,
which is only when an ``s3://`` URL is used.

This module replaces itself in ``sys.modules`` with :mod:`xrdhttp.s3.sigv4`,
so ``xrdclient.s3.sigv4 is xrdhttp.s3.sigv4``: one set of classes for
``isinstance``, and a test that patches a name through either path patches
what every caller calls. The explicit re-exports are for type checkers, which do not
see the swap. New code should import :mod:`xrdhttp.s3.sigv4` directly.
"""

import sys

from xrdhttp.s3 import sigv4 as _lib
from xrdhttp.s3.sigv4 import ALGORITHM as ALGORITHM
from xrdhttp.s3.sigv4 import DEFAULT_REGION as DEFAULT_REGION
from xrdhttp.s3.sigv4 import EMPTY_SHA256 as EMPTY_SHA256
from xrdhttp.s3.sigv4 import UNSIGNED_PAYLOAD as UNSIGNED_PAYLOAD
from xrdhttp.s3.sigv4 import Credentials as Credentials
from xrdhttp.s3.sigv4 import __all__ as __all__
from xrdhttp.s3.sigv4 import hash_payload as hash_payload
from xrdhttp.s3.sigv4 import presign as presign
from xrdhttp.s3.sigv4 import sign as sign

sys.modules[__name__] = _lib
