""":mod:`xrdsec.krb5.ccache`, by xrdclient's name.

The MIT/Heimdal FILE credential cache and the default cache's name.

The pure-Python Kerberos client moved to xrdsec in 0.4.0 (:mod:`xrdsec.krb5`),
so that xrdclient and xgfalclient share one implementation. This module
replaces itself in ``sys.modules`` with :mod:`xrdsec.krb5.ccache`, so
``xrdclient.auth.kerberos.ccache is xrdsec.krb5.ccache``: a class is the
same class whichever name it was imported under, an ``except`` written
against this name catches what xrdsec raises, and a test that patches a name
here patches what xrdsec calls.

New code imports :mod:`xrdsec.krb5.ccache`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap."""

import sys

from xrdsec.krb5 import ccache as _sec
from xrdsec.krb5.ccache import CCACHE_VERSION_3 as CCACHE_VERSION_3
from xrdsec.krb5.ccache import CCACHE_VERSION_4 as CCACHE_VERSION_4
from xrdsec.krb5.ccache import __all__ as __all__
from xrdsec.krb5.ccache import ccache_name as ccache_name
from xrdsec.krb5.ccache import is_config_entry as is_config_entry
from xrdsec.krb5.ccache import marshal_principal as marshal_principal
from xrdsec.krb5.ccache import marshal_ticket as marshal_ticket
from xrdsec.krb5.ccache import read_ccache as read_ccache
from xrdsec.krb5.ccache import read_credential as read_credential
from xrdsec.krb5.ccache import read_credentials as read_credentials
from xrdsec.krb5.ccache import read_principal as read_principal
from xrdsec.krb5.ccache import resolve_ccache as resolve_ccache

sys.modules[__name__] = _sec
