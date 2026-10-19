"""VOMS attribute certificates: inspect, validate, check ``vomses``.

:mod:`xrdsec.voms`, by xrdclient's name.

The implementation moved to xrdsec in 0.4.0, so that xrdclient, xgfalclient
and xrdarc share one copy of the grid's security code. This module replaces
itself in ``sys.modules`` with :mod:`xrdsec.voms`, so ``xrdclient.crypto.voms
is xrdsec.voms``: a class is the same class whichever name it was imported
under, an ``except`` written against this name catches what xrdsec raises, and
a test that patches a name here patches what xrdsec calls.

Its private machinery is :mod:`xrdsec._voms`; the pre-0.4.0 private name
``xrdclient.crypto._voms`` is gone.

New code imports :mod:`xrdsec.voms`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap."""

import sys

from xrdsec import voms as _sec
from xrdsec.voms import VOMS_AC_OID as VOMS_AC_OID
from xrdsec.voms import VOMS_FQAN_OID as VOMS_FQAN_OID
from xrdsec.voms import VOMSAttribute as VOMSAttribute
from xrdsec.voms import VOMSDiagnostic as VOMSDiagnostic
from xrdsec.voms import VOMSEntry as VOMSEntry
from xrdsec.voms import VOMSResult as VOMSResult
from xrdsec.voms import VOMSStatus as VOMSStatus
from xrdsec.voms import __all__ as __all__
from xrdsec.voms import check_vomses as check_vomses
from xrdsec.voms import default_ca_path as default_ca_path
from xrdsec.voms import default_voms_dir as default_voms_dir
from xrdsec.voms import inspect_voms as inspect_voms
from xrdsec.voms import validate_voms as validate_voms

sys.modules[__name__] = _sec
