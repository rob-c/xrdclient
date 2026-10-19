"""Grid certificates and proxy files: :mod:`xrdsec.x509`, with a Config-shaped proxy path.

The implementation - one decoder (:func:`certificate_data`), the proxy rules
(XrdCl's and Globus'), :func:`load_proxy` and :class:`ProxyCredential`, the
CA-directory name hashes and the certificate building blocks - moved to
xrdsec in 0.4.0, so that xrdclient, xgfalclient and xrdarc read a grid
certificate through one copy of the code. Every public name here *is*
xrdsec's object (``x509.ProxyCredential is xrdsec.x509.ProxyCredential``), so
``isinstance`` and ``except`` agree whichever name was imported: xrdsec's
``__all__``, plus the decoder (:func:`certificate_data` and
:class:`CertificateData`), which xrdsec keeps out of its ``__all__`` but which
was public here before 0.4.0.

Unlike the other modules of :mod:`xrdclient.crypto`, this one is not a
``sys.modules`` alias of its xrdsec module, because one public name keeps its
pre-0.4.0 meaning rather than xrdsec's: :func:`default_proxy_path` here is the
Config-shaped :func:`xrdclient._gridpaths.default_proxy_path`
(``config.proxy`` first, then the environment), where
:func:`xrdsec.x509.default_proxy_path` takes an ``environ`` and a ``uid``;
called with no argument, both give the same path.

The private helpers this module used to carry (``_decode_name``,
``_extensions``, ``_policy_language``, ``_parse_certificate`` and the rest of
xrdsec's namespace copied in at import) were removed in 0.4.0, once no
downstream package used them; xrdsec publishes them as
:func:`~xrdsec.x509.name_from_element`, :func:`~xrdsec.x509.tbs_extensions`,
:func:`~xrdsec.x509.policy_language` and :func:`~xrdsec.x509.parse_certificate`.

The consequence of being a module of its own: a test that monkeypatches a
name *here* changes only this module's binding, not what xrdsec's code calls
- patch :mod:`xrdsec.x509` instead. New code imports :mod:`xrdsec.x509`.
"""

from xrdsec.x509 import ATTRIBUTE_NAMES as ATTRIBUTE_NAMES
from xrdsec.x509 import ATTRIBUTE_OIDS as ATTRIBUTE_OIDS
from xrdsec.x509 import AUTHORITY_KEY_ID_OID as AUTHORITY_KEY_ID_OID
from xrdsec.x509 import BASIC_CONSTRAINTS_OID as BASIC_CONSTRAINTS_OID
from xrdsec.x509 import INDEPENDENT_OID as INDEPENDENT_OID
from xrdsec.x509 import INHERIT_ALL_OID as INHERIT_ALL_OID
from xrdsec.x509 import KEY_USAGE_OID as KEY_USAGE_OID
from xrdsec.x509 import LEGACY_PROXY_OID as LEGACY_PROXY_OID
from xrdsec.x509 import LIMITED_PROXY_OID as LIMITED_PROXY_OID
from xrdsec.x509 import PROXY_CERT_INFO_OID as PROXY_CERT_INFO_OID
from xrdsec.x509 import SUBJECT_ALT_NAME_OID as SUBJECT_ALT_NAME_OID
from xrdsec.x509 import SUBJECT_KEY_ID_OID as SUBJECT_KEY_ID_OID
from xrdsec.x509 import Certificate as Certificate
from xrdsec.x509 import CertificateData as CertificateData
from xrdsec.x509 import Extension as Extension
from xrdsec.x509 import Name as Name
from xrdsec.x509 import ProxyCredential as ProxyCredential
from xrdsec.x509 import __all__ as __all__
from xrdsec.x509 import certificate_data as certificate_data
from xrdsec.x509 import certificate_fields as certificate_fields
from xrdsec.x509 import certificates_in as certificates_in
from xrdsec.x509 import credential_key as credential_key
from xrdsec.x509 import decode_name as decode_name
from xrdsec.x509 import encode_name as encode_name
from xrdsec.x509 import extension as extension
from xrdsec.x509 import extensions_of as extensions_of
from xrdsec.x509 import is_proxy_certificate as is_proxy_certificate
from xrdsec.x509 import issuer_hashes as issuer_hashes
from xrdsec.x509 import key_usage as key_usage
from xrdsec.x509 import legacy_proxy_kind as legacy_proxy_kind
from xrdsec.x509 import load_certificates as load_certificates
from xrdsec.x509 import load_proxy as load_proxy
from xrdsec.x509 import name_from_element as name_from_element
from xrdsec.x509 import name_hashes as name_hashes
from xrdsec.x509 import parse_certificate as parse_certificate
from xrdsec.x509 import parse_extension as parse_extension
from xrdsec.x509 import pem_encode as pem_encode
from xrdsec.x509 import policy_language as policy_language
from xrdsec.x509 import proxy_identity as proxy_identity
from xrdsec.x509 import proxy_policy as proxy_policy
from xrdsec.x509 import public_key_info as public_key_info
from xrdsec.x509 import read_credential_files as read_credential_files
from xrdsec.x509 import signature_algorithm as signature_algorithm
from xrdsec.x509 import signature_digest as signature_digest
from xrdsec.x509 import signed_by as signed_by
from xrdsec.x509 import split_der_certificates as split_der_certificates
from xrdsec.x509 import tbs_extensions as tbs_extensions
from xrdsec.x509 import verify_issued as verify_issued
from xrdsec.x509 import verify_signed as verify_signed

# Not xrdsec.x509.default_proxy_path, on purpose (see above): the pre-0.4.0
# signature.
from .._gridpaths import default_proxy_path as default_proxy_path
