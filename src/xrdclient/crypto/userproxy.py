"""An in-memory proxy from a user certificate and key (``XrdSecgsi``'s ``createpxy``).

:mod:`xrdsec.userproxy`, by xrdclient's name.

The implementation moved to xrdsec in 0.4.0, so that xrdclient, xgfalclient
and xrdarc share one copy of the grid's security code. This module replaces
itself in ``sys.modules`` with :mod:`xrdsec.userproxy`, so
``xrdclient.crypto.userproxy is xrdsec.userproxy``: a class is the same class
whichever name it was imported under, an ``except`` written against this name
catches what xrdsec raises, and a test that patches a name here patches what
xrdsec calls.

New code imports :mod:`xrdsec.userproxy`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap.
"""

import sys

from xrdsec import userproxy as _sec
from xrdsec.userproxy import PROXY_BITS as PROXY_BITS
from xrdsec.userproxy import PROXY_HOURS as PROXY_HOURS
from xrdsec.userproxy import __all__ as __all__
from xrdsec.userproxy import user_proxy as user_proxy

sys.modules[__name__] = _sec
