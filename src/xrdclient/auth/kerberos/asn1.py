""":mod:`xrdsec.krb5.asn1`, by xrdclient's name.

The DER encoding of the RFC 4120 messages a Kerberos login needs.

The pure-Python Kerberos client moved to xrdsec in 0.4.0 (:mod:`xrdsec.krb5`),
so that xrdclient and xgfalclient share one implementation. This module
replaces itself in ``sys.modules`` with :mod:`xrdsec.krb5.asn1`, so
``xrdclient.auth.kerberos.asn1 is xrdsec.krb5.asn1``: a class is the
same class whichever name it was imported under, an ``except`` written
against this name catches what xrdsec raises, and a test that patches a name
here patches what xrdsec calls.

New code imports :mod:`xrdsec.krb5.asn1`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap."""

import sys

from xrdsec.krb5 import asn1 as _sec
from xrdsec.krb5.asn1 import EncKdcReplyPart as EncKdcReplyPart
from xrdsec.krb5.asn1 import EncryptedData as EncryptedData
from xrdsec.krb5.asn1 import Fields as Fields
from xrdsec.krb5.asn1 import KdcReply as KdcReply
from xrdsec.krb5.asn1 import Key as Key
from xrdsec.krb5.asn1 import KrbError as KrbError
from xrdsec.krb5.asn1 import TicketInfo as TicketInfo
from xrdsec.krb5.asn1 import __all__ as __all__
from xrdsec.krb5.asn1 import decode_enc_kdc_rep_part as decode_enc_kdc_rep_part
from xrdsec.krb5.asn1 import decode_kdc_rep as decode_kdc_rep
from xrdsec.krb5.asn1 import decode_krb_error as decode_krb_error
from xrdsec.krb5.asn1 import decode_ticket as decode_ticket
from xrdsec.krb5.asn1 import encode_ap_req as encode_ap_req
from xrdsec.krb5.asn1 import encode_authenticator as encode_authenticator
from xrdsec.krb5.asn1 import encode_cred_info as encode_cred_info
from xrdsec.krb5.asn1 import encode_enc_krb_cred_part as encode_enc_krb_cred_part
from xrdsec.krb5.asn1 import encode_krb_cred as encode_krb_cred
from xrdsec.krb5.asn1 import encode_req_body as encode_req_body
from xrdsec.krb5.asn1 import encode_tgs_req as encode_tgs_req
from xrdsec.krb5.asn1 import principal_name as principal_name
from xrdsec.krb5.asn1 import read_principal_name as read_principal_name

sys.modules[__name__] = _sec
