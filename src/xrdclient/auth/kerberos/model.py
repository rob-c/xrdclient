""":mod:`xrdsec.krb5.model`, by xrdclient's name.

Kerberos principals and credentials.

The pure-Python Kerberos client moved to xrdsec in 0.4.0 (:mod:`xrdsec.krb5`),
so that xrdclient and xgfalclient share one implementation. This module
replaces itself in ``sys.modules`` with :mod:`xrdsec.krb5.model`, so
``xrdclient.auth.kerberos.model is xrdsec.krb5.model``: a class is the
same class whichever name it was imported under, an ``except`` written
against this name catches what xrdsec raises, and a test that patches a name
here patches what xrdsec calls.

New code imports :mod:`xrdsec.krb5.model`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap."""

import sys

from xrdsec.krb5 import model as _sec
from xrdsec.krb5.model import NT_PRINCIPAL as NT_PRINCIPAL
from xrdsec.krb5.model import NT_SRV_INST as NT_SRV_INST
from xrdsec.krb5.model import TKT_FLG_FORWARDABLE as TKT_FLG_FORWARDABLE
from xrdsec.krb5.model import TKT_FLG_FORWARDED as TKT_FLG_FORWARDED
from xrdsec.krb5.model import Principal as Principal
from xrdsec.krb5.model import Ticket as Ticket
from xrdsec.krb5.model import __all__ as __all__
from xrdsec.krb5.model import parse_principal as parse_principal

sys.modules[__name__] = _sec
