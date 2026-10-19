"""Strict DER reading and writing for the structures X.509 libraries do not model.

:mod:`xrdsec.der`, by xrdclient's name.

The implementation moved to xrdsec in 0.4.0, so that xrdclient, xgfalclient
and xrdarc share one copy of the grid's security code. This module replaces
itself in ``sys.modules`` with :mod:`xrdsec.der`, so ``xrdclient.crypto.der is
xrdsec.der``: a class is the same class whichever name it was imported under,
an ``except`` written against this name catches what xrdsec raises, and a test
that patches a name here patches what xrdsec calls.

New code imports :mod:`xrdsec.der`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap."""

import sys

from xrdsec import der as _sec
from xrdsec.der import TAG_BIT_STRING as TAG_BIT_STRING
from xrdsec.der import TAG_BOOLEAN as TAG_BOOLEAN
from xrdsec.der import TAG_GENERALIZED_TIME as TAG_GENERALIZED_TIME
from xrdsec.der import TAG_IA5_STRING as TAG_IA5_STRING
from xrdsec.der import TAG_INTEGER as TAG_INTEGER
from xrdsec.der import TAG_NULL as TAG_NULL
from xrdsec.der import TAG_OCTET_STRING as TAG_OCTET_STRING
from xrdsec.der import TAG_OID as TAG_OID
from xrdsec.der import TAG_PRINTABLE_STRING as TAG_PRINTABLE_STRING
from xrdsec.der import TAG_SEQUENCE as TAG_SEQUENCE
from xrdsec.der import TAG_SET as TAG_SET
from xrdsec.der import TAG_UTC_TIME as TAG_UTC_TIME
from xrdsec.der import TAG_UTF8_STRING as TAG_UTF8_STRING
from xrdsec.der import DERError as DERError
from xrdsec.der import Element as Element
from xrdsec.der import __all__ as __all__
from xrdsec.der import bit_string as bit_string
from xrdsec.der import boolean as boolean
from xrdsec.der import decode_time as decode_time
from xrdsec.der import encode as encode
from xrdsec.der import encode_integer as encode_integer
from xrdsec.der import encode_oid as encode_oid
from xrdsec.der import encode_time as encode_time
from xrdsec.der import explicit as explicit
from xrdsec.der import generalized_time as generalized_time
from xrdsec.der import integer as integer
from xrdsec.der import null as null
from xrdsec.der import octet_string as octet_string
from xrdsec.der import oid as oid
from xrdsec.der import oid_string as oid_string
from xrdsec.der import parse as parse
from xrdsec.der import parse_all as parse_all
from xrdsec.der import parse_one as parse_one
from xrdsec.der import printable_string as printable_string
from xrdsec.der import raw_children as raw_children
from xrdsec.der import read_integer as read_integer
from xrdsec.der import sequence as sequence
from xrdsec.der import set_of as set_of
from xrdsec.der import tlv as tlv
from xrdsec.der import utc_time as utc_time
from xrdsec.der import utf8_string as utf8_string
from xrdsec.der import validity_time as validity_time

sys.modules[__name__] = _sec
