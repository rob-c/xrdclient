"""Client TLS contexts with the grid's trust, revocation and credential rules.

:mod:`xrdsec.tls`, by xrdclient's name.

The implementation moved to xrdsec in 0.4.0, so that xrdclient, xgfalclient
and xrdarc share one copy of the grid's security code. This module replaces
itself in ``sys.modules`` with :mod:`xrdsec.tls`, so ``xrdclient.tls is
xrdsec.tls``: a class is the same class whichever name it was imported under,
an ``except`` written against this name catches what xrdsec raises, and a test
that patches a name here patches what xrdsec calls.

Every ``roots://``, ``https://``, ``davs://`` and S3 connection is built by
its :func:`client_context` - :func:`xrdclient.transport.base.tls_context` is
that function given a :class:`~xrdclient.Config`'s settings - so one cache
serves the client and every tool built on it. Importing it imports :mod:`ssl`.

New code imports :mod:`xrdsec.tls`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap."""

import sys

from xrdsec import tls as _sec
from xrdsec.tls import CRL_ENV as CRL_ENV
from xrdsec.tls import SYSTEM_CA_ENV as SYSTEM_CA_ENV
from xrdsec.tls import KeyPassword as KeyPassword
from xrdsec.tls import __all__ as __all__
from xrdsec.tls import clear_cache as clear_cache
from xrdsec.tls import client_context as client_context
from xrdsec.tls import crl_mode as crl_mode

sys.modules[__name__] = _sec
