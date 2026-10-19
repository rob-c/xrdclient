"""AES-CBC and CTR as the GSI session cipher uses them: :mod:`xrdsec.aes`, by xrdclient's name.

The implementation moved to xrdsec in 0.4.0, so that xrdclient, xgfalclient
and xrdarc share one copy of the grid's security code. This module replaces
itself in ``sys.modules`` with :mod:`xrdsec.aes`, so ``xrdclient.crypto.aes is
xrdsec.aes``: a class is the same class whichever name it was imported under,
an ``except`` written against this name catches what xrdsec raises, and a test
that patches a name here patches what xrdsec calls.

New code imports :mod:`xrdsec.aes`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap."""

import sys

from xrdsec import aes as _sec
from xrdsec.aes import AES as AES
from xrdsec.aes import BLOCK_SIZE as BLOCK_SIZE
from xrdsec.aes import CTR as CTR
from xrdsec.aes import __all__ as __all__
from xrdsec.aes import cbc_decrypt as cbc_decrypt
from xrdsec.aes import cbc_encrypt as cbc_encrypt
from xrdsec.aes import pkcs7_pad as pkcs7_pad
from xrdsec.aes import pkcs7_unpad as pkcs7_unpad

sys.modules[__name__] = _sec
