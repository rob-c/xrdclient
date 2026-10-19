""":mod:`xrdsec.krb5.kcm`, by xrdclient's name.

``KCM:`` credential caches, over the KCM daemon's Unix socket.

The pure-Python Kerberos client moved to xrdsec in 0.4.0 (:mod:`xrdsec.krb5`),
so that xrdclient and xgfalclient share one implementation. This module
replaces itself in ``sys.modules`` with :mod:`xrdsec.krb5.kcm`, so
``xrdclient.auth.kerberos.kcm is xrdsec.krb5.kcm``: a class is the
same class whichever name it was imported under, an ``except`` written
against this name catches what xrdsec raises, and a test that patches a name
here patches what xrdsec calls.

New code imports :mod:`xrdsec.krb5.kcm`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap."""

import sys

from xrdsec.krb5 import kcm as _sec
from xrdsec.krb5.kcm import DEFAULT_SOCKET as DEFAULT_SOCKET
from xrdsec.krb5.kcm import KcmCache as KcmCache
from xrdsec.krb5.kcm import KcmError as KcmError
from xrdsec.krb5.kcm import __all__ as __all__

sys.modules[__name__] = _sec
