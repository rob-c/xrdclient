"""Obtaining VOMS ACs: ``vomses`` files, server selection and the VOMS REST client with fail-over.

:mod:`xrdsec.voms_client`, by xrdclient's name.

The implementation moved to xrdsec in 0.4.0, so that xrdclient, xgfalclient
and xrdarc share one copy of the grid's security code. This module replaces
itself in ``sys.modules`` with :mod:`xrdsec.voms_client`, so
``xrdclient.voms_client is xrdsec.voms_client``: a class is the same class
whichever name it was imported under, an ``except`` written against this name
catches what xrdsec raises, and a test that patches a name here patches what
xrdsec calls.

Being xrdsec's, the REST requests it makes say ``User-Agent:
xrdsec/<version>``.

New code imports :mod:`xrdsec.voms_client`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap.
"""

import sys

from xrdsec import voms_client as _sec
from xrdsec.voms_client import DEFAULT_TIMEOUT as DEFAULT_TIMEOUT
from xrdsec.voms_client import VOMSConfigError as VOMSConfigError
from xrdsec.voms_client import VOMSError as VOMSError
from xrdsec.voms_client import VOMSRefusedError as VOMSRefusedError
from xrdsec.voms_client import VOMSServer as VOMSServer
from xrdsec.voms_client import VOMSUnavailableError as VOMSUnavailableError
from xrdsec.voms_client import VOMSWarning as VOMSWarning
from xrdsec.voms_client import __all__ as __all__
from xrdsec.voms_client import ac_extension as ac_extension
from xrdsec.voms_client import fetch_ac as fetch_ac
from xrdsec.voms_client import find_vomses as find_vomses
from xrdsec.voms_client import parse_vomses as parse_vomses
from xrdsec.voms_client import servers_for as servers_for

sys.modules[__name__] = _sec
