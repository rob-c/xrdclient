""":mod:`xrdsec.krb5.tgs`, by xrdclient's name.

The TGS exchange, and the AP-REQ and KRB-CRED messages.

The pure-Python Kerberos client moved to xrdsec in 0.4.0 (:mod:`xrdsec.krb5`),
so that xrdclient and xgfalclient share one implementation. This module
replaces itself in ``sys.modules`` with :mod:`xrdsec.krb5.tgs`, so
``xrdclient.auth.kerberos.tgs is xrdsec.krb5.tgs``: a class is the
same class whichever name it was imported under, an ``except`` written
against this name catches what xrdsec raises, and a test that patches a name
here patches what xrdsec calls.

New code imports :mod:`xrdsec.krb5.tgs`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap."""

import sys

from xrdsec.krb5 import tgs as _sec
from xrdsec.krb5.tgs import AP_OPTS_USE_SESSION_KEY as AP_OPTS_USE_SESSION_KEY
from xrdsec.krb5.tgs import KDC_OPT_FORWARDABLE as KDC_OPT_FORWARDABLE
from xrdsec.krb5.tgs import KDC_OPT_FORWARDED as KDC_OPT_FORWARDED
from xrdsec.krb5.tgs import TgsHooks as TgsHooks
from xrdsec.krb5.tgs import TgsRequest as TgsRequest
from xrdsec.krb5.tgs import __all__ as __all__
from xrdsec.krb5.tgs import build_ap_req as build_ap_req
from xrdsec.krb5.tgs import build_krb_cred as build_krb_cred
from xrdsec.krb5.tgs import kdc_error_message as kdc_error_message
from xrdsec.krb5.tgs import request_ticket as request_ticket
from xrdsec.krb5.tgs import tgs_exchange as tgs_exchange

sys.modules[__name__] = _sec
