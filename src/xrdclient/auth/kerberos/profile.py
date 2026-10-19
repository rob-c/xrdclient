""":mod:`xrdsec.krb5.profile`, by xrdclient's name.

``krb5.conf``: realms, KDCs and the domain mapping.

The pure-Python Kerberos client moved to xrdsec in 0.4.0 (:mod:`xrdsec.krb5`),
so that xrdclient and xgfalclient share one implementation. This module
replaces itself in ``sys.modules`` with :mod:`xrdsec.krb5.profile`, so
``xrdclient.auth.kerberos.profile is xrdsec.krb5.profile``: a class is the
same class whichever name it was imported under, an ``except`` written
against this name catches what xrdsec raises, and a test that patches a name
here patches what xrdsec calls.

New code imports :mod:`xrdsec.krb5.profile`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap."""

import sys

from xrdsec.krb5 import profile as _sec
from xrdsec.krb5.profile import DEFAULT_CONFIG as DEFAULT_CONFIG
from xrdsec.krb5.profile import Profile as Profile
from xrdsec.krb5.profile import __all__ as __all__
from xrdsec.krb5.profile import enctype_list as enctype_list

sys.modules[__name__] = _sec
