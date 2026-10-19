""":mod:`xrdsec.krb5.keyring`, by xrdclient's name.

``KEYRING:`` credential caches, in Linux kernel keyrings.

The pure-Python Kerberos client moved to xrdsec in 0.4.0 (:mod:`xrdsec.krb5`),
so that xrdclient and xgfalclient share one implementation. This module
replaces itself in ``sys.modules`` with :mod:`xrdsec.krb5.keyring`, so
``xrdclient.auth.kerberos.keyring is xrdsec.krb5.keyring``: a class is the
same class whichever name it was imported under, an ``except`` written
against this name catches what xrdsec raises, and a test that patches a name
here patches what xrdsec calls.

New code imports :mod:`xrdsec.krb5.keyring`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap."""

import sys

from xrdsec.krb5 import keyring as _sec
from xrdsec.krb5.keyring import Keyctl as Keyctl
from xrdsec.krb5.keyring import KeyringCache as KeyringCache
from xrdsec.krb5.keyring import __all__ as __all__
from xrdsec.krb5.keyring import parse_residual as parse_residual
from xrdsec.krb5.keyring import system_keyctl as system_keyctl

sys.modules[__name__] = _sec
