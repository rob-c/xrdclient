"""The one proxy issuer and its profiles: :mod:`xrdsec.proxies`, by xrdclient's name.

The implementation moved to xrdsec in 0.4.0, so that xrdclient, xgfalclient
and xrdarc share one copy of the grid's security code. This module replaces
itself in ``sys.modules`` with :mod:`xrdsec.proxies`, so
``xrdclient.crypto.proxies is xrdsec.proxies``: a class is the same class
whichever name it was imported under, an ``except`` written against this name
catches what xrdsec raises, and a test that patches a name here patches what
xrdsec calls.

xrdsec adds a ``globus`` profile to xrdclient's ``rfc3820``, ``arc``,
``xrootd`` and ``globus-legacy``: xgfalclient's ``globus_gsi_proxy_sign_req``
policy.

New code imports :mod:`xrdsec.proxies`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap.
"""

import sys

from xrdsec import proxies as _sec
from xrdsec.proxies import DEFAULT_LIFETIME as DEFAULT_LIFETIME
from xrdsec.proxies import NOT_BEFORE_SKEW as NOT_BEFORE_SKEW
from xrdsec.proxies import PROFILES as PROFILES
from xrdsec.proxies import ProfileName as ProfileName
from xrdsec.proxies import ProxyError as ProxyError
from xrdsec.proxies import ProxyInfo as ProxyInfo
from xrdsec.proxies import __all__ as __all__
from xrdsec.proxies import create_proxy as create_proxy
from xrdsec.proxies import describe_proxy as describe_proxy
from xrdsec.proxies import issue_proxy as issue_proxy
from xrdsec.proxies import load_credential as load_credential
from xrdsec.proxies import voms_extension as voms_extension
from xrdsec.proxies import write_proxy as write_proxy

sys.modules[__name__] = _sec
