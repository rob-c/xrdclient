"""CRC-64/XZ and CRC-64/NVME: :mod:`xrdcore.crc64`, by xrdclient's name.

The ``crc64`` (XZ, ECMA-182 reflected) and ``crc64nvme`` checksum types moved
to xrdcore in 0.4.0 with the rest of :mod:`xrdcore.checksum`. This module
replaces itself in ``sys.modules`` with :mod:`xrdcore.crc64`, so
``xrdclient.crypto.crc64 is xrdcore.crc64``. The explicit re-exports are for type
checkers, which do not see the swap.
"""

import sys

from xrdcore import crc64 as _core
from xrdcore.crc64 import __all__ as __all__
from xrdcore.crc64 import crc64 as crc64
from xrdcore.crc64 import crc64nvme as crc64nvme

sys.modules[__name__] = _core
