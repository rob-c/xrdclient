""":mod:`xrdsec.krb5.kdc`, by xrdclient's name.

Talking to a KDC over UDP and TCP.

The pure-Python Kerberos client moved to xrdsec in 0.4.0 (:mod:`xrdsec.krb5`),
so that xrdclient and xgfalclient share one implementation. This module
replaces itself in ``sys.modules`` with :mod:`xrdsec.krb5.kdc`, so
``xrdclient.auth.kerberos.kdc is xrdsec.krb5.kdc``: a class is the
same class whichever name it was imported under, an ``except`` written
against this name catches what xrdsec raises, and a test that patches a name
here patches what xrdsec calls.

New code imports :mod:`xrdsec.krb5.kdc`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap."""

import sys

from xrdsec.krb5 import kdc as _sec
from xrdsec.krb5.kdc import KdcAddress as KdcAddress
from xrdsec.krb5.kdc import __all__ as __all__
from xrdsec.krb5.kdc import exchange as exchange
from xrdsec.krb5.kdc import parse_kdc as parse_kdc

sys.modules[__name__] = _sec
