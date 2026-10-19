"""One signature check for every algorithm, with the accepted-algorithm policy.

:mod:`xrdsec.signatures`, by xrdclient's name.

The implementation moved to xrdsec in 0.4.0, so that xrdclient, xgfalclient
and xrdarc share one copy of the grid's security code. This module replaces
itself in ``sys.modules`` with :mod:`xrdsec.signatures`, so
``xrdclient.crypto.signatures is xrdsec.signatures``: a class is the same
class whichever name it was imported under, an ``except`` written against this
name catches what xrdsec raises, and a test that patches a name here patches
what xrdsec calls.

New code imports :mod:`xrdsec.signatures`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap.
"""

import sys

from xrdsec import signatures as _sec
from xrdsec.signatures import ECDSA_DIGESTS as ECDSA_DIGESTS
from xrdsec.signatures import ED448_OID as ED448_OID
from xrdsec.signatures import ED25519_OID as ED25519_OID
from xrdsec.signatures import RSA_PKCS1_DIGESTS as RSA_PKCS1_DIGESTS
from xrdsec.signatures import RSA_PSS_OID as RSA_PSS_OID
from xrdsec.signatures import PSSParameters as PSSParameters
from xrdsec.signatures import SignatureAlgorithmError as SignatureAlgorithmError
from xrdsec.signatures import __all__ as __all__
from xrdsec.signatures import null_parameters as null_parameters
from xrdsec.signatures import pss_hash as pss_hash
from xrdsec.signatures import pss_parameters as pss_parameters
from xrdsec.signatures import signature_label as signature_label
from xrdsec.signatures import verify_signature as verify_signature

sys.modules[__name__] = _sec
