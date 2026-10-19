"""Is this GSI server who it says it is, before a proxy is delegated to it.

:mod:`xrdsec.trust`, by xrdclient's name.

The implementation moved to xrdsec in 0.4.0, so that xrdclient, xgfalclient
and xrdarc share one copy of the grid's security code. This module replaces
itself in ``sys.modules`` with :mod:`xrdsec.trust`, so
``xrdclient.crypto.trust is xrdsec.trust``: a class is the same class
whichever name it was imported under, an ``except`` written against this name
catches what xrdsec raises, and a test that patches a name here patches what
xrdsec calls.

New code imports :mod:`xrdsec.trust`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap."""

import sys

from xrdsec import trust as _sec
from xrdsec.trust import DEFAULT_CA_PATH as DEFAULT_CA_PATH
from xrdsec.trust import TrustError as TrustError
from xrdsec.trust import __all__ as __all__
from xrdsec.trust import anchors as anchors
from xrdsec.trust import names_host as names_host
from xrdsec.trust import verify_server as verify_server

sys.modules[__name__] = _sec
