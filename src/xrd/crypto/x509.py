"""X.509 certificates and RFC 3820 proxies, parsed in pure Python.

GSI needs less of X.509 than it first appears: the handshake echoes the
proxy chain to the server verbatim, and the *server* validates it. What the
client genuinely needs is to look at the material it is about to offer — is
this a proxy, whose is it, and has it expired — so that a stale proxy is a
sentence rather than a 3010 from the far end an hour into a job.

So this reads certificates; it does not verify signatures or build paths.
Trust decisions belong to the endpoint, and pretending otherwise in a client
would be the dangerous kind of convenience.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .._compat import SLOTS
from .der import (
    TAG_BIT_STRING,
    TAG_GENERALIZED_TIME,
    TAG_INTEGER,
    TAG_UTC_TIME,
    DERError,
    Element,
    oid_string,
    parse,
    read_integer,
)
from .rsa import RSAPublicKey, pem_blocks, public_key_from_bitstring

__all__ = [
    "Certificate",
    "Name",
    "ProxyCredential",
    "default_proxy_path",
    "load_certificates",
    "load_proxy",
]

#: The short names OpenSSL prints for the attribute types that appear in
#: grid subjects. Anything else is rendered by its OID, which is honest.
_ATTRIBUTE_NAMES = {
    "2.5.4.3": "CN",
    "2.5.4.6": "C",
    "2.5.4.7": "L",
    "2.5.4.8": "ST",
    "2.5.4.10": "O",
    "2.5.4.11": "OU",
    "2.5.4.5": "serialNumber",
    "1.2.840.113549.1.9.1": "emailAddress",
    "0.9.2342.19200300.100.1.25": "DC",
    "0.9.2342.19200300.100.1.1": "UID",
}

#: ``id-ppl-*`` — the presence of the proxyCertInfo extension is what makes
#: a certificate an RFC 3820 proxy.
PROXY_CERT_INFO_OID = "1.3.6.1.5.5.7.1.14"
#: The pre-RFC Globus "legacy" proxy extension, still seen in the wild.
LEGACY_PROXY_OID = "1.3.6.1.4.1.3536.1.222"


@dataclass(frozen=True, **SLOTS)
class Name:
    """A distinguished name: ordered ``(type, value)`` pairs."""

    rdns: tuple[tuple[str, str], ...] = ()

    @property
    def cn(self) -> str:
        """The last ``CN``, which for a proxy is ``"proxy"`` or a digit."""
        common = [value for key, value in self.rdns if key == "CN"]
        return common[-1] if common else ""

    def get(self, key: str) -> list[str]:
        """Every value for one attribute type, in order."""
        return [value for name, value in self.rdns if name == key]

    def __str__(self) -> str:
        """OpenSSL's oneline form: ``/DC=org/DC=example/CN=Jane Doe``."""
        return "".join(f"/{key}={value}" for key, value in self.rdns)

    def __bool__(self) -> bool:
        return bool(self.rdns)


def _decode_name(element: Element) -> Name:
    rdns: list[tuple[str, str]] = []
    for rdn in element.children():
        for attribute in rdn.children():
            parts = attribute.children()
            if len(parts) != 2:
                continue
            key = oid_string(parts[0])
            rdns.append((_ATTRIBUTE_NAMES.get(key, key), _decode_string(parts[1])))
    return Name(tuple(rdns))


def _decode_string(element: Element) -> str:
    """DirectoryString in any of the encodings certificates actually use."""
    if element.tag == 0x1E:  # BMPString: UTF-16BE
        return element.value.decode("utf-16-be", "replace")
    return element.value.decode("utf-8", "replace")


def _decode_time(element: Element) -> float:
    """``UTCTime``/``GeneralizedTime`` as a UNIX timestamp."""
    text = element.value.decode("ascii", "replace").strip()
    if text.endswith("Z"):
        text = text[:-1]
    if element.tag == TAG_UTC_TIME:
        if len(text) < 10:
            raise DERError(f"malformed UTCTime {text!r}")
        year = int(text[:2])
        text = f"{2000 + year if year < 50 else 1900 + year}{text[2:]}"
    elif element.tag != TAG_GENERALIZED_TIME:
        raise DERError(f"tag 0x{element.tag:02x} is not a certificate time")
    text = (text + "000000")[:14]
    parsed = time.strptime(text, "%Y%m%d%H%M%S")
    return float(__import__("calendar").timegm(parsed))


@dataclass(frozen=True, **SLOTS)
class Certificate:
    """One X.509 certificate, decoded far enough to be useful."""

    subject: Name
    issuer: Name
    serial: int
    not_before: float
    not_after: float
    public_key: RSAPublicKey | None
    extensions: tuple[str, ...] = ()
    der: bytes = field(default=b"", repr=False)

    @property
    def is_proxy(self) -> bool:
        """True for an RFC 3820 proxy or a Globus legacy proxy."""
        if PROXY_CERT_INFO_OID in self.extensions or LEGACY_PROXY_OID in self.extensions:
            return True
        # A proxy always appends a CN to its issuer's subject; a CA-issued
        # end-entity certificate does not.
        return bool(self.subject.rdns) and self.subject.rdns[:-1] == self.issuer.rdns

    @property
    def expired(self) -> bool:
        return self.not_after <= time.time()

    def remaining(self) -> float:
        """Seconds of validity left; negative once expired."""
        return self.not_after - time.time()

    def pem(self) -> bytes:
        """The certificate back as a PEM block."""
        import base64
        import textwrap

        body = "\n".join(textwrap.wrap(base64.b64encode(self.der).decode("ascii"), 64))
        return f"-----BEGIN CERTIFICATE-----\n{body}\n-----END CERTIFICATE-----\n".encode("ascii")

    def __str__(self) -> str:
        return str(self.subject)


def _parse_certificate(der: bytes) -> Certificate:
    certificate, _ = parse(der)
    tbs = certificate.children()[0]
    fields = tbs.children()
    index = 1 if fields and fields[0].tag == 0xA0 else 0  # [0] EXPLICIT version
    _require_certificate_fields(fields, index)
    serial = read_integer(fields[index]) if fields[index].tag == TAG_INTEGER else 0
    issuer = _decode_name(fields[index + 2])
    validity = fields[index + 3].children()
    _require_validity(validity)
    subject = _decode_name(fields[index + 4])
    key = _certificate_key(fields[index + 5])
    extensions = _extension_oids(fields[index + 6 :])
    return Certificate(
        subject=subject,
        issuer=issuer,
        serial=serial,
        not_before=_decode_time(validity[0]),
        not_after=_decode_time(validity[1]),
        public_key=key,
        extensions=tuple(extensions),
        der=der,
    )


def _require_certificate_fields(fields: list[Element], index: int) -> None:
    if len(fields) < index + 6:
        raise DERError("certificate body is missing required fields")


def _require_validity(validity: list[Element]) -> None:
    if len(validity) != 2:
        raise DERError("certificate validity is not a pair of times")


def _certificate_key(element: Element) -> RSAPublicKey | None:
    spki = element.children()
    if len(spki) != 2 or spki[1].tag != TAG_BIT_STRING:
        return None
    try:
        return public_key_from_bitstring(spki[1])
    except DERError:
        return None  # an EC or DSA certificate: readable, just not RSA


def _extension_oids(extras: list[Element]) -> list[str]:
    extensions: list[str] = []
    for extra in extras:
        if extra.tag == 0xA3:  # [3] EXPLICIT extensions
            extensions.extend(_extension_block(extra))
    return extensions


def _extension_block(extra: Element) -> list[str]:
    result = []
    for extension in extra.children()[0].children():
        parts = extension.children()
        if parts:
            result.append(oid_string(parts[0]))
    return result


def load_certificates(data: bytes | str) -> list[Certificate]:
    """Every ``CERTIFICATE`` block in ``data``, in file order.

    A block that will not parse is skipped rather than fatal: proxy files
    routinely carry a CA certificate this reader has no opinion about, and
    losing the whole chain over one of them would be the wrong trade.
    """
    out: list[Certificate] = []
    for label, der in pem_blocks(data):
        if label != "CERTIFICATE":
            continue
        try:
            out.append(_parse_certificate(der))
        except (DERError, ValueError):
            continue
    return out


def default_proxy_path(config: object = None) -> str:
    """``$X509_USER_PROXY``, else ``/tmp/x509up_u<uid>``."""
    import os

    configured = getattr(config, "proxy", None)
    if configured:
        return str(configured)
    env = os.environ.get("X509_USER_PROXY")
    if env:
        return env
    return f"/tmp/x509up_u{os.geteuid()}"


@dataclass(frozen=True, **SLOTS)
class ProxyCredential:
    """A loaded GSI proxy: the chain as PEM, its key, and what it says."""

    chain: tuple[Certificate, ...]
    key: object  # RSAPrivateKey; typed loosely to keep the import one-way
    path: str = ""

    @property
    def certificate(self) -> Certificate:
        """The end-entity certificate — the proxy itself, first in the file."""
        return self.chain[0]

    @property
    def subject(self) -> Name:
        return self.certificate.subject

    @property
    def identity(self) -> str:
        """The user behind the proxy: the subject with its proxy CNs stripped."""
        rdns = list(self.certificate.subject.rdns)
        while rdns and rdns[-1][0] == "CN" and (rdns[-1][1].isdigit() or rdns[-1][1] == "proxy"):
            rdns.pop()
        return str(Name(tuple(rdns)))

    @property
    def expired(self) -> bool:
        return any(certificate.expired for certificate in self.chain)

    def remaining(self) -> float:
        """Seconds until the first certificate in the chain expires."""
        return min(certificate.remaining() for certificate in self.chain)

    def pem(self) -> bytes:
        """The chain as concatenated PEM, which is what GSI puts on the wire."""
        return b"".join(certificate.pem() for certificate in self.chain)

    def __repr__(self) -> str:
        return f"ProxyCredential(subject={str(self.subject)!r}, key=<redacted>)"


def load_proxy(path: str) -> ProxyCredential:
    """Load a combined proxy file: certificate, private key, issuer chain."""
    from .rsa import load_private_key

    with open(path, "rb") as handle:
        data = handle.read()
    chain = load_certificates(data)
    if not chain:
        raise DERError(f"no certificate in {path}")
    return ProxyCredential(chain=tuple(chain), key=load_private_key(data), path=path)
