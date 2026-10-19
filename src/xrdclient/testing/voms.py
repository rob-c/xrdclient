"""VOMS AC builders, ``vomsdir``/``vomses`` fixtures and ``FakeVOMSServer``.

:mod:`xrdsec.testing.voms`, by xrdclient's name.

The implementation moved to xrdsec in 0.4.0, so that xrdclient, xgfalclient
and xrdarc share one copy of the grid's security code. This module replaces
itself in ``sys.modules`` with :mod:`xrdsec.testing.voms`, so
``xrdclient.testing.voms is xrdsec.testing.voms``: a class is the same class
whichever name it was imported under, an ``except`` written against this name
catches what xrdsec raises, and a test that patches a name here patches what
xrdsec calls.

New code imports :mod:`xrdsec.testing.voms`; this name is kept through 0.x.
The explicit re-exports are for type checkers, which do not see the swap.
"""

import sys

from xrdsec.testing import voms as _sec
from xrdsec.testing.voms import VOMS_ATTRIBUTES_OID as VOMS_ATTRIBUTES_OID
from xrdsec.testing.voms import VOMS_CERTS_OID as VOMS_CERTS_OID
from xrdsec.testing.voms import ACSpec as ACSpec
from xrdsec.testing.voms import Attribute as Attribute
from xrdsec.testing.voms import FakeVOMSServer as FakeVOMSServer
from xrdsec.testing.voms import SigningKey as SigningKey
from xrdsec.testing.voms import __all__ as __all__
from xrdsec.testing.voms import ac_extension as ac_extension
from xrdsec.testing.voms import ac_sequence as ac_sequence
from xrdsec.testing.voms import lsc_text as lsc_text
from xrdsec.testing.voms import make_ac as make_ac
from xrdsec.testing.voms import signature_algorithm as signature_algorithm
from xrdsec.testing.voms import voms_error as voms_error
from xrdsec.testing.voms import voms_proxy as voms_proxy
from xrdsec.testing.voms import voms_reply as voms_reply
from xrdsec.testing.voms import vomses_line as vomses_line
from xrdsec.testing.voms import write_lsc as write_lsc

sys.modules[__name__] = _sec
