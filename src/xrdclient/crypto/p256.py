"""ECDSA P-256 signatures, for xgfalclient's SSH transport: :mod:`xrdsec.p256`, by xrdclient's name.

The implementation moved to xrdsec in 0.4.0, so that xrdclient, xgfalclient
and xrdarc share one copy of the grid's security code. This module replaces
itself in ``sys.modules`` with :mod:`xrdsec.p256`, so ``xrdclient.crypto.p256
is xrdsec.p256``: a class is the same class whichever name it was imported
under, an ``except`` written against this name catches what xrdsec raises, and
a test that patches a name here patches what xrdsec calls.

New code imports :mod:`xrdsec.p256`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap."""

import sys

from xrdsec import p256 as _sec
from xrdsec.p256 import G as G
from xrdsec.p256 import N as N
from xrdsec.p256 import P as P
from xrdsec.p256 import __all__ as __all__
from xrdsec.p256 import decode_point as decode_point
from xrdsec.p256 import encode_point as encode_point
from xrdsec.p256 import public_key as public_key
from xrdsec.p256 import sign as sign
from xrdsec.p256 import verify as verify

sys.modules[__name__] = _sec
