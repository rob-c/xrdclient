"""The PKCS#10 request parser, the certificate builder and XrdCrypto's ``sign_proxy_request``.

:mod:`xrdsec.delegation`, by xrdclient's name.

The implementation moved to xrdsec in 0.4.0, so that xrdclient, xgfalclient
and xrdarc share one copy of the grid's security code. This module replaces
itself in ``sys.modules`` with :mod:`xrdsec.delegation`, so
``xrdclient.crypto.delegation is xrdsec.delegation``: a class is the same
class whichever name it was imported under, an ``except`` written against this
name catches what xrdsec raises, and a test that patches a name here patches
what xrdsec calls.

New code imports :mod:`xrdsec.delegation`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap.
"""

import sys

from xrdsec import delegation as _sec
from xrdsec.delegation import DelegationError as DelegationError
from xrdsec.delegation import ProxyRequest as ProxyRequest
from xrdsec.delegation import SigningKey as SigningKey
from xrdsec.delegation import __all__ as __all__
from xrdsec.delegation import issue_certificate as issue_certificate
from xrdsec.delegation import load_proxy_request as load_proxy_request
from xrdsec.delegation import make_proxy_request as make_proxy_request
from xrdsec.delegation import parse_proxy_request as parse_proxy_request
from xrdsec.delegation import proxy_cert_info_extension as proxy_cert_info_extension
from xrdsec.delegation import proxy_path_length as proxy_path_length
from xrdsec.delegation import sign_globus_request as sign_globus_request
from xrdsec.delegation import sign_proxy_request as sign_proxy_request

sys.modules[__name__] = _sec
