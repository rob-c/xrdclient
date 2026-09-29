"""X.509 proxy delegation: sign the proxy request a GSI server sends.

When a client delegates, the *server* makes a key pair and sends the public
half as a PKCS#10 certificate request whose subject is the client proxy's
subject plus one ``CN=<serial>``. The client signs it with its proxy's private
key, producing an RFC 3820 proxy one link further down the chain, and sends
back just that certificate; the server already holds the rest of the chain
and the private key, so it ends up with a complete proxy of its own - and the
client's private key never leaves the client.

The certificate written here follows ``XrdCryptosslX509SignProxyReq``, the
function the stock client uses:

* version 3, serial number taken from the request's last ``CN``;
* subject from the request, issuer the signing proxy's subject;
* valid from now until the signing proxy expires, never beyond;
* every extension of the signing proxy copied as written (key usage, basic
  constraints, ...) except its ``ProxyCertInfo``, which is replaced by a
  critical ``ProxyCertInfo`` with the ``inheritAll`` policy;
* ``sha256WithRSAEncryption``.

Three places deliberately differ from XrdCrypto. The request's own signature
is checked, so what gets signed is a key the requester holds. A path-length
constraint is carried down the chain as :rfc:`3820` has it - one less than the
signer's - so a signer whose constraint is already ``0`` refuses instead of
issuing a proxy no verifier would accept. And the signer's subject and
authority *key identifiers* are not copied: they describe the signer's key,
and on the new certificate they send ``openssl verify`` looking for an issuer
that does not exist (a proxy XrdCl delegates fails ``openssl verify`` for
exactly that reason; one delegated from here passes).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .._compat import SLOTS
from .der import (
    TAG_BIT_STRING,
    TAG_BOOLEAN,
    TAG_INTEGER,
    TAG_NULL,
    TAG_OCTET_STRING,
    DERError,
    Element,
    encode,
    encode_integer,
    encode_oid,
    encode_time,
    oid_string,
    parse,
    raw_children,
    read_integer,
    sequence,
)
from .rsa import RSAPrivateKey, RSAPublicKey, pem_blocks, public_key_from_bitstring
from .x509 import (
    LEGACY_PROXY_OID,
    PROXY_CERT_INFO_OID,
    Certificate,
    Extension,
    Name,
    certificate_fields,
    decode_name,
    extensions_of,
    load_certificates,
    parse_extension,
    verify_signed,
)

__all__ = [
    "DelegationError",
    "ProxyRequest",
    "load_proxy_request",
    "sign_proxy_request",
    "proxy_path_length",
]

#: ``pkcs-9-at-extensionRequest``: where a PKCS#10 request carries extensions.
EXTENSION_REQUEST_OID = "1.2.840.113549.1.9.14"
#: ``id-ppl-inheritAll``: the delegated proxy may do what its issuer may.
INHERIT_ALL_OID = "1.3.6.1.5.5.7.21.1"
KEY_USAGE_OID = "2.5.29.15"
#: Key identifiers name *one* key; copied onto a certificate for another key
#: they point path building at the wrong issuer, so they are not copied.
_KEY_IDENTIFIER_OIDS = ("2.5.29.14", "2.5.29.35")
SUBJECT_ALT_NAME_OID = "2.5.29.17"
SHA256_WITH_RSA_OID = "1.2.840.113549.1.1.11"

_SERIAL_CN = re.compile(r"-?[0-9]{1,20}")


class DelegationError(ValueError):
    """The request cannot, or must not, be signed."""


@dataclass(frozen=True, **SLOTS)
class ProxyRequest:
    """A decoded PKCS#10 certificate request, as a GSI server sends it."""

    subject: Name
    subject_der: bytes
    spki: bytes
    public_key: RSAPublicKey
    extensions: tuple[Extension, ...]
    der: bytes

    @property
    def serial(self) -> int:
        """The serial the last ``CN`` names, as the unsigned 32-bit value XrdCrypto uses."""
        kind, value = self.subject.rdns[-1] if self.subject.rdns else ("", "")
        if kind != "CN" or not _SERIAL_CN.fullmatch(value):
            raise DelegationError(f"the request's subject does not end in CN=<serial>: {value!r}")
        return int(value) % (1 << 32)

    def verify(self) -> bool:
        """Whether the request is signed by the key it asks to be certified."""
        return verify_signed(self.der, self.public_key)


def load_proxy_request(data: bytes | str) -> ProxyRequest:
    """Parse a ``CERTIFICATE REQUEST`` from PEM (what XrdCrypto sends) or raw DER."""
    der = next(
        (body for label, body in pem_blocks(data) if label.endswith("CERTIFICATE REQUEST")),
        None,
    )
    if der is None:
        der = data if isinstance(data, bytes) else data.encode("latin-1")
    try:
        return _parse_request(der)
    except (DERError, ValueError, IndexError) as exc:
        raise DelegationError(f"unreadable proxy request: {exc}") from exc


def _parse_request(der: bytes) -> ProxyRequest:
    info = raw_children(der)[0]
    fields = raw_children(info)
    if len(fields) < 3 or parse(fields[0])[0].tag != TAG_INTEGER:
        raise DERError("CertificationRequestInfo needs a version, a subject and a key")
    spki = parse(fields[2])[0].children()
    if len(spki) != 2 or spki[1].tag != TAG_BIT_STRING:
        raise DERError("the request's key is not a SubjectPublicKeyInfo")
    return ProxyRequest(
        subject=decode_name(fields[1]),
        subject_der=fields[1],
        spki=fields[2],
        public_key=public_key_from_bitstring(spki[1]),
        extensions=tuple(_requested_extensions(fields[3:])),
        der=der,
    )


def _requested_extensions(extras: list[bytes]) -> list[Extension]:
    """The extensions inside the ``[0] attributes`` of a request, if any."""
    out: list[Extension] = []
    for extra in extras:
        if extra[0] != 0xA0:
            continue
        for attribute in parse(extra)[0].children():
            parts = attribute.children()
            if len(parts) == 2 and oid_string(parts[0]) == EXTENSION_REQUEST_OID:
                out.extend(_extension_list(parts[1]))
    return out


def _extension_list(values: Element) -> list[Extension]:
    """``SET OF Extensions``: each value is itself a ``SEQUENCE OF Extension``."""
    return [
        parse_extension(raw)
        for value in values.children()
        for raw in raw_children(encode(value.tag, value.value))
    ]


def proxy_path_length(extension: Extension) -> int | None:
    """The ``pCPathLenConstraint`` of a ProxyCertInfo extension; ``None`` if unlimited.

    Reads both the RFC 3820 layout (constraint first) and the pre-RFC Globus
    one (policy first, constraint in an explicit ``[1]``).
    """
    for part in parse(extension.value)[0].children():
        if part.tag == TAG_INTEGER:
            return read_integer(part)
        if part.tag == 0xA1:
            return read_integer(part.children()[0])
    return None


def _check_request(request: ProxyRequest, signer: Certificate) -> None:
    if request.subject.rdns[:-1] != signer.subject.rdns:
        raise DelegationError(
            f"the request is for {request.subject}, which is not {signer.subject}/CN=<serial>"
        )
    if not request.verify():
        raise DelegationError("the proxy request is not signed by the key it carries")
    if not request.extensions:
        raise DelegationError("the proxy request carries no extensions")


def _inherited(signer: Certificate) -> tuple[list[bytes], int | None]:
    """The signer's extensions to copy, and its proxy path-length constraint."""
    copied: list[bytes] = []
    depth: int | None = None
    usage = False
    for extension in extensions_of(signer.der):
        if extension.oid == SUBJECT_ALT_NAME_OID:
            raise DelegationError("the signing proxy carries a subjectAltName; refusing")
        if extension.oid in (PROXY_CERT_INFO_OID, LEGACY_PROXY_OID):
            depth = proxy_path_length(extension)
            continue
        if extension.oid in _KEY_IDENTIFIER_OIDS:
            continue
        usage = usage or extension.oid == KEY_USAGE_OID
        copied.append(extension.der)
    if not usage:
        raise DelegationError("the signing proxy has no keyUsage extension")
    return copied, depth


def _proxy_cert_info(depth: int | None) -> bytes:
    """A critical ProxyCertInfo one step below a signer constrained to ``depth``."""
    if depth is not None and depth < 1:
        raise DelegationError("the signing proxy's path length forbids further delegation")
    policy = sequence(encode_oid(INHERIT_ALL_OID))
    body = sequence(encode_integer(depth - 1), policy) if depth is not None else sequence(policy)
    return sequence(
        encode_oid(PROXY_CERT_INFO_OID),
        encode(TAG_BOOLEAN, b"\xff"),
        encode(TAG_OCTET_STRING, body),
    )


def sign_proxy_request(
    request: ProxyRequest,
    signer: Certificate,
    key: RSAPrivateKey,
    *,
    now: float | None = None,
) -> Certificate:
    """Issue the proxy ``request`` asks for, signed by ``signer`` with ``key``.

    ``signer`` is the client's own proxy - the first certificate of the proxy
    file - and ``key`` its private key. Raises :class:`DelegationError` when
    the request is malformed, is not for this proxy, or the proxy cannot sign.
    """
    moment = time.time() if now is None else now
    if signer.not_after <= moment:
        raise DelegationError("the signing proxy has expired")
    if signer.public_key != key.public:
        raise DelegationError("the private key does not belong to the signing proxy")
    _check_request(request, signer)
    copied, depth = _inherited(signer)
    algorithm = sequence(encode_oid(SHA256_WITH_RSA_OID), encode(TAG_NULL, b""))
    extensions = sequence(*copied, _proxy_cert_info(depth))
    tbs = sequence(
        encode(0xA0, encode_integer(2)),
        encode_integer(request.serial),
        algorithm,
        _subject_of(signer),
        sequence(encode_time(moment), encode_time(signer.not_after)),
        request.subject_der,
        request.spki,
        encode(0xA3, extensions),
    )
    signature = key.sign(tbs, digest="sha256")
    der = sequence(tbs, algorithm, encode(TAG_BIT_STRING, b"\x00" + signature))
    return load_certificates(_pem(der))[0]


def _subject_of(certificate: Certificate) -> bytes:
    return certificate_fields(certificate.der)["subject"]


def _pem(der: bytes) -> bytes:
    import base64
    import textwrap

    body = "\n".join(textwrap.wrap(base64.b64encode(der).decode("ascii"), 64))
    return f"-----BEGIN CERTIFICATE-----\n{body}\n-----END CERTIFICATE-----\n".encode("ascii")
