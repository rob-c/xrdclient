""":mod:`xrdsec.krb5.rfc3961`, by xrdclient's name.

Kerberos 5 encryption: the RFC 3961 framework and the AES enctypes.

The pure-Python Kerberos client moved to xrdsec in 0.4.0 (:mod:`xrdsec.krb5`),
so that xrdclient and xgfalclient share one implementation. This module
replaces itself in ``sys.modules`` with :mod:`xrdsec.krb5.rfc3961`, so
``xrdclient.crypto.rfc3961 is xrdsec.krb5.rfc3961``: a class is the
same class whichever name it was imported under, an ``except`` written
against this name catches what xrdsec raises, and a test that patches a name
here patches what xrdsec calls.

New code imports :mod:`xrdsec.krb5.rfc3961`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap."""

import sys

from xrdsec.krb5 import rfc3961 as _sec
from xrdsec.krb5.rfc3961 import AES128_CTS_HMAC_SHA1_96 as AES128_CTS_HMAC_SHA1_96
from xrdsec.krb5.rfc3961 import AES128_CTS_HMAC_SHA256_128 as AES128_CTS_HMAC_SHA256_128
from xrdsec.krb5.rfc3961 import AES256_CTS_HMAC_SHA1_96 as AES256_CTS_HMAC_SHA1_96
from xrdsec.krb5.rfc3961 import AES256_CTS_HMAC_SHA384_192 as AES256_CTS_HMAC_SHA384_192
from xrdsec.krb5.rfc3961 import SUPPORTED_ENCTYPES as SUPPORTED_ENCTYPES
from xrdsec.krb5.rfc3961 import Enctype as Enctype
from xrdsec.krb5.rfc3961 import IntegrityError as IntegrityError
from xrdsec.krb5.rfc3961 import UnsupportedEnctypeError as UnsupportedEnctypeError
from xrdsec.krb5.rfc3961 import __all__ as __all__
from xrdsec.krb5.rfc3961 import cts_decrypt as cts_decrypt
from xrdsec.krb5.rfc3961 import cts_encrypt as cts_encrypt
from xrdsec.krb5.rfc3961 import get_enctype as get_enctype
from xrdsec.krb5.rfc3961 import nfold as nfold

sys.modules[__name__] = _sec
