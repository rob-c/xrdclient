"""RSA keys as grid protocols use them: GSI's raw-padding proof of possession and key loading.

:mod:`xrdsec.rsa`, by xrdclient's name.

The implementation moved to xrdsec in 0.4.0, so that xrdclient, xgfalclient
and xrdarc share one copy of the grid's security code. This module replaces
itself in ``sys.modules`` with :mod:`xrdsec.rsa`, so ``xrdclient.crypto.rsa is
xrdsec.rsa``: a class is the same class whichever name it was imported under,
an ``except`` written against this name catches what xrdsec raises, and a test
that patches a name here patches what xrdsec calls.

New code imports :mod:`xrdsec.rsa`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap."""

import sys

from xrdsec import rsa as _sec
from xrdsec.rsa import RSA_OID as RSA_OID
from xrdsec.rsa import RSAPrivateKey as RSAPrivateKey
from xrdsec.rsa import RSAPublicKey as RSAPublicKey
from xrdsec.rsa import __all__ as __all__
from xrdsec.rsa import load_library_key as load_library_key
from xrdsec.rsa import load_private_key as load_private_key
from xrdsec.rsa import load_public_key as load_public_key
from xrdsec.rsa import pem_blocks as pem_blocks
from xrdsec.rsa import private_key_der as private_key_der
from xrdsec.rsa import private_key_pem as private_key_pem
from xrdsec.rsa import public_key_from_bitstring as public_key_from_bitstring

sys.modules[__name__] = _sec
