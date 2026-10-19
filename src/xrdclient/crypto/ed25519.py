"""Ed25519 signatures, for xgfalclient's SSH transport: :mod:`xrdsec.ed25519`, by xrdclient's name.

The implementation moved to xrdsec in 0.4.0, so that xrdclient, xgfalclient
and xrdarc share one copy of the grid's security code. This module replaces
itself in ``sys.modules`` with :mod:`xrdsec.ed25519`, so
``xrdclient.crypto.ed25519 is xrdsec.ed25519``: a class is the same class
whichever name it was imported under, an ``except`` written against this name
catches what xrdsec raises, and a test that patches a name here patches what
xrdsec calls.

New code imports :mod:`xrdsec.ed25519`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap.
"""

import sys

from xrdsec import ed25519 as _sec
from xrdsec.ed25519 import L as L
from xrdsec.ed25519 import __all__ as __all__
from xrdsec.ed25519 import public_key as public_key
from xrdsec.ed25519 import sign as sign
from xrdsec.ed25519 import verify as verify

sys.modules[__name__] = _sec
