"""Checksum algorithms by the names storage servers use: :mod:`xrdcore.checksum`.

:func:`new`, :func:`checksum_bytes`, :func:`checksum_file` and
:func:`algorithms` - one incremental, ``hashlib``-shaped surface over
Adler-32, CRC-32, CRC-32C, CRC-64/XZ, CRC-64/NVME, MD5 and the SHAs - moved to
xrdcore in 0.4.0 so every tool on the stack computes the same function under
the same name. XRootD's spellings are the canonical ones there; the HTTP
``Digest``/``Repr-Digest`` names are mapped onto them in
:mod:`xrdhttp.webdav` and :mod:`xrdclient.http.dav`.

This module replaces itself in ``sys.modules`` with :mod:`xrdcore.checksum`,
so ``xrdclient.crypto.checksum is xrdcore.checksum``. The explicit re-exports are for
type checkers, which do not see the swap.
"""

import sys

from xrdcore import checksum as _core
from xrdcore.checksum import Checksum as Checksum
from xrdcore.checksum import ChecksumInfo as ChecksumInfo
from xrdcore.checksum import Digest as Digest
from xrdcore.checksum import __all__ as __all__
from xrdcore.checksum import algorithms as algorithms
from xrdcore.checksum import checksum_bytes as checksum_bytes
from xrdcore.checksum import checksum_file as checksum_file
from xrdcore.checksum import new as new

sys.modules[__name__] = _core
