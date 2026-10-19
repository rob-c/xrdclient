"""The throwaway test grid PKI: CA directory, host, user, proxies, CRLs and TLS contexts.

:mod:`xrdsec.testing.pki`, by xrdclient's name.

The implementation moved to xrdsec in 0.4.0, so that xrdclient, xgfalclient
and xrdarc share one copy of the grid's security code. This module replaces
itself in ``sys.modules`` with :mod:`xrdsec.testing.pki`, so
``xrdclient.testing.pki is xrdsec.testing.pki``: a class is the same class
whichever name it was imported under, an ``except`` written against this name
catches what xrdsec raises, and a test that patches a name here patches what
xrdsec calls.

Its subjects read ``DC=xrdsec`` (``DC=xrdclient`` before 0.4.0); a suite that
wants names of its own subclasses :class:`BasePKI` and sets the ``*_SUBJECT``
class attributes, as xgfalclient's does.

New code imports :mod:`xrdsec.testing.pki`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap.
"""

import sys

from xrdsec.testing import pki as _sec
from xrdsec.testing.pki import ALLOW_PROXY_CERTS as ALLOW_PROXY_CERTS
from xrdsec.testing.pki import BACKDATE as BACKDATE
from xrdsec.testing.pki import DAY as DAY
from xrdsec.testing.pki import DEFAULT_HOSTS as DEFAULT_HOSTS
from xrdsec.testing.pki import KEYS as KEYS
from xrdsec.testing.pki import PKI as PKI
from xrdsec.testing.pki import BasePKI as BasePKI
from xrdsec.testing.pki import HoldsChain as HoldsChain
from xrdsec.testing.pki import PrivateKey as PrivateKey
from xrdsec.testing.pki import Rdns as Rdns
from xrdsec.testing.pki import Signer as Signer
from xrdsec.testing.pki import __all__ as __all__
from xrdsec.testing.pki import ca_certificate as ca_certificate
from xrdsec.testing.pki import create_pki as create_pki
from xrdsec.testing.pki import host_certificate as host_certificate
from xrdsec.testing.pki import key_pem as key_pem
from xrdsec.testing.pki import proxy_certificate as proxy_certificate
from xrdsec.testing.pki import subject_hash as subject_hash
from xrdsec.testing.pki import test_key as test_key
from xrdsec.testing.pki import test_key_pem as test_key_pem
from xrdsec.testing.pki import user_certificate as user_certificate

sys.modules[__name__] = _sec
