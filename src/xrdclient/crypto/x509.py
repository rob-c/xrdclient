"""Library-backed X.509 inspection and RFC 3820 proxy compatibility.

GSI needs less of X.509 than it first appears: the handshake echoes the
proxy chain to the server verbatim, and the *server* validates it. What the
client genuinely needs is to look at the material it is about to offer — is
this a proxy, whose is it, and has it expired — so that a stale proxy is a
sentence rather than a 3010 from the far end an hour into a job.

So this reads certificates. It does not build paths for the login itself:
trust in the *client* is the endpoint's decision. The pieces here that do
check signatures (:func:`verify_signed`) serve X.509 delegation, where the
client is about to hand a server a credential and so has to know which server
it is talking to - see :mod:`xrdclient.crypto.trust`.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from cryptography import x509 as _x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa as _rsa

from .._compat import SLOTS
from .der import (
    TAG_BIT_STRING,
    TAG_BOOLEAN,
    TAG_INTEGER,
    TAG_OCTET_STRING,
    TAG_OID,
    TAG_SET,
    DERError,
    Element,
    encode,
    oid,
    oid_string,
    parse,
    parse_one,
    printable_string,
    raw_children,
    read_integer,
    sequence,
    set_of,
    tlv,
    utf8_string,
)
from .der import (
    decode_time as _decode_time,
)
from .rsa import RSAPublicKey, pem_blocks, public_key_from_bitstring

__all__ = [
    "Certificate",
    "Extension",
    "Name",
    "encode_name",
    "ATTRIBUTE_NAMES",
    "ATTRIBUTE_OIDS",
    "certificate_fields",
    "decode_name",
    "extensions_of",
    "parse_certificate",
    "parse_extension",
    "signature_digest",
    "verify_signed",
    "ProxyCredential",
    "default_proxy_path",
    "load_certificates",
    "load_proxy",
]

#: The short names OpenSSL prints for the attribute types that appear in
#: grid subjects. Anything else is rendered by its OID, which is honest.
ATTRIBUTE_NAMES = {
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

ATTRIBUTE_OIDS = {short: dotted for dotted, short in ATTRIBUTE_NAMES.items()}

#: ``id-ppl-*`` — the presence of the proxyCertInfo extension is what makes
#: a certificate an RFC 3820 proxy.
PROXY_CERT_INFO_OID = "1.3.6.1.5.5.7.1.14"
#: The pre-RFC Globus "legacy" proxy extension, still seen in the wild.
LEGACY_PROXY_OID = "1.3.6.1.4.1.3536.1.222"


@dataclass(frozen=True, **SLOTS)
class Name:
    """A distinguished name: ``(type, value)`` pairs plus the DER they came from."""

    rdns: tuple[tuple[str, str], ...] = ()
    der: bytes = field(default=b"", compare=False, repr=False)

    @property
    def cn(self) -> str:
        common = [value for key, value in self.rdns if key == "CN"]
        return common[-1] if common else ""

    def get(self, key: str) -> list[str]:
        return [value for name, value in self.rdns if name == key]

    def __str__(self) -> str:
        """OpenSSL's one-line form: ``/DC=org/DC=example/CN=Jane Doe``."""
        return "".join(f"/{key}={value}" for key, value in self.rdns)

    def __bool__(self) -> bool:
        return bool(self.rdns)

    def encoded(self) -> bytes:
        return self.der or encode_name(self.rdns)


def encode_name(rdns: Sequence[tuple[str, str]]) -> bytes:
    """A ``Name`` from ``(type, value)`` pairs, one attribute per RDN."""
    parts = []
    for key, value in rdns:
        kind = ATTRIBUTE_OIDS.get(key, key)
        parts.append(set_of(sequence(oid(kind), _attribute_value(kind, value))))
    return sequence(*parts)


#: Attributes RFC 4519 types as IA5String: domainComponent and emailAddress.
_IA5_ATTRIBUTES = ("0.9.2342.19200300.100.1.25", "1.2.840.113549.1.9.1")


def _attribute_value(kind: str, value: str) -> bytes:
    if kind in _IA5_ATTRIBUTES:
        return tlv(0x16, value.encode("ascii"))
    if _printable(value):
        return printable_string(value)
    return utf8_string(value)


def _printable(value: str) -> bool:
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 '()+,-./:=?")
    return bool(value) and all(char in allowed for char in value)


def _decode_name(element: Element) -> Name:
    rdns: list[tuple[str, str]] = []
    for rdn in element.children():
        for attribute in rdn.children():
            parts = attribute.children()
            if len(parts) != 2:
                continue
            key = oid_string(parts[0])
            rdns.append((ATTRIBUTE_NAMES.get(key, key), _decode_string(parts[1])))
    return Name(tuple(rdns), element.encoded)


def _decode_string(element: Element) -> str:
    """DirectoryString in any of the encodings certificates actually use."""
    if element.tag == 0x1E:  # BMPString: UTF-16BE
        return element.value.decode("utf-16-be", "replace")
    return element.value.decode("utf-8", "replace")


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
    def is_anchor(self) -> bool:
        """True for a self-signed certificate: a trust anchor, not a link.

        A CA that signed itself is where verification *stops*, which is why a
        verifier keeps its own copy and never takes one from the wire.
        """
        return self.subject == self.issuer

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


def parse_certificate(der: bytes) -> Certificate:
    """Decode one DER certificate.

    VOMS attribute certificates embed their signing chain as consecutive DER
    certificates rather than PEM, so callers need the single-certificate
    decoder as well as :func:`load_certificates`.
    """
    data = certificate_data(der)
    return Certificate(
        data.subject,
        data.issuer,
        data.serial,
        data.not_before,
        data.not_after,
        data.public_key,
        data.extensions,
        data.der,
    )


# Kept for callers which used the old private test seam.
_parse_certificate = parse_certificate


@dataclass(frozen=True, **SLOTS)
class CertificateData(Certificate):
    """One decoded view for both clients; compatibility facades keep their types."""

    extension_values: dict[str, tuple[bool, bytes]] = field(default_factory=dict, repr=False)
    tbs: bytes = field(default=b"", repr=False)
    signature: bytes = field(default=b"", repr=False)
    signature_oid: str = ""
    spki: bytes = field(default=b"", repr=False)


def _library_name(name: _x509.Name) -> Name:
    return Name(
        tuple(
            (ATTRIBUTE_NAMES.get(a.oid.dotted_string, a.oid.dotted_string), str(a.value))
            for a in name
        ),
        name.public_bytes(),
    )


def certificate_data(der: bytes) -> CertificateData:
    """Decode normal certificates through cryptography, preserving original DER.

    Legacy inspection APIs also accept incomplete names/keys that a strict
    X.509 loader refuses. Their bounded compatibility decoder is shared here;
    this function does not establish trust or validate a certificate path.
    """
    try:
        cert = _x509.load_der_x509_certificate(der)
        extensions = {
            e.oid.dotted_string: (e.critical, e.value.public_bytes()) for e in cert.extensions
        }
    except ValueError:
        return _legacy_certificate_data(der)
    try:
        public = cert.public_key()
        numbers = public.public_numbers() if isinstance(public, _rsa.RSAPublicKey) else None
        key = RSAPublicKey(numbers.n, numbers.e) if numbers is not None else None
        spki = public.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    except (ValueError, UnsupportedAlgorithm):
        return _legacy_certificate_data(der)
    return CertificateData(
        _library_name(cert.subject),
        _library_name(cert.issuer),
        cert.serial_number,
        cert.not_valid_before_utc.timestamp(),
        cert.not_valid_after_utc.timestamp(),
        key,
        tuple(extensions),
        bytes(der),
        extensions,
        cert.tbs_certificate_bytes,
        cert.signature,
        cert.signature_algorithm_oid.dotted_string,
        spki,
    )


def _extensions(fields: list[Element]) -> dict[str, tuple[bool, bytes]]:
    found: dict[str, tuple[bool, bytes]] = {}
    for extra in fields:
        if extra.tag != 0xA3:
            continue
        for extension in extra.children()[0].children():
            parts = extension.children()
            if parts and parts[-1].tag == TAG_OCTET_STRING:
                critical = (
                    len(parts) == 3 and parts[1].tag == TAG_BOOLEAN and parts[1].value != b"\x00"
                )
                found[oid_string(parts[0])] = (critical, parts[-1].value)
    return found


def _legacy_certificate_data(der: bytes) -> CertificateData:
    top = parse_one(der).children()
    if len(top) != 3 or top[0].tag != 0x30 or top[2].tag != TAG_BIT_STRING:
        raise DERError("not an X.509 certificate")
    fields = top[0].children()
    index = 1 if fields and fields[0].tag == 0xA0 else 0
    if len(fields) < index + 6:
        raise DERError("certificate body is missing required fields")
    validity = fields[index + 3].children()
    if len(validity) != 2:
        raise DERError("certificate validity is not a pair of times")
    spki = fields[index + 5]
    key = _certificate_key(spki)
    extensions = _extensions(fields[index + 6 :])
    return CertificateData(
        _decode_name(fields[index + 4]),
        _decode_name(fields[index + 2]),
        read_integer(fields[index]) if fields[index].tag == TAG_INTEGER else 0,
        _decode_time(validity[0]),
        _decode_time(validity[1]),
        key,
        tuple(extensions),
        bytes(der),
        extensions,
        top[0].encoded,
        top[2].value[1:],
        oid_string(top[1].children()[0]),
        spki.encoded,
    )


def _certificate_key(spki: Element) -> RSAPublicKey | None:
    parts = spki.children()
    key: RSAPublicKey | None = None
    if len(parts) == 2 and parts[1].tag == TAG_BIT_STRING:
        try:
            key = public_key_from_bitstring(parts[1])
        except DERError:
            pass
    return key


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
            out.append(parse_certificate(der))
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
        """The chain as concatenated PEM, which is what GSI puts on the wire.

        The trust anchor is left out. Some tools write the CA into the proxy
        file beside the certificates it signed, and a server handed a chain
        that carries its own anchor refuses the login as inconsistent - it
        looks anchors up in its certificate directory by hash, so one arriving
        over the wire is neither needed nor believed. A file holding nothing
        but an anchor is sent as it is, so the server can say so itself.
        """
        chain = [link for link in self.chain if not link.is_anchor] or list(self.chain)
        return b"".join(certificate.pem() for certificate in chain)

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


# ---------------------------------------------------------------------------
# The raw structure, for signing and verifying
# ---------------------------------------------------------------------------

#: ``sha*WithRSAEncryption`` - the signature algorithms a grid PKI uses.
_SIGNATURE_DIGESTS = {
    "1.2.840.113549.1.1.5": "sha1",
    "1.2.840.113549.1.1.11": "sha256",
    "1.2.840.113549.1.1.12": "sha384",
    "1.2.840.113549.1.1.13": "sha512",
}


@dataclass(frozen=True, **SLOTS)
class Extension:
    """One certificate extension: what it is, and its bytes as written."""

    oid: str
    critical: bool
    value: bytes
    der: bytes = field(default=b"", repr=False)


def decode_name(der: bytes) -> Name:
    """A DER ``Name`` as a :class:`Name`."""
    return _decode_name(parse_one(der))


# The string types OpenSSL folds to lower-case UTF-8 before hashing a name
# (``ASN1_MASK_CANON``), with how each one's bytes read as text.
_CANON_STRINGS = {
    0x0C: "utf-8",  # UTF8String
    0x13: "latin-1",  # PrintableString
    0x14: "latin-1",  # T61String: OpenSSL reads it as Latin-1
    0x16: "latin-1",  # IA5String
    0x1A: "latin-1",  # VisibleString
    0x1C: "utf-32-be",  # UniversalString
    0x1E: "utf-16-be",  # BMPString
}
_ASCII_SPACE = b" \t\n\v\f\r"


def _canonical_value(tag: int, value: bytes) -> bytes:
    """One attribute value as OpenSSL's ``asn1_string_canon`` leaves it.

    Leading and trailing space goes, each run of inner space becomes one, and
    ASCII letters are lowered; bytes above 0x7F pass through untouched.
    """
    encoding = _CANON_STRINGS.get(tag)
    if encoding is None:
        return encode(tag, value)
    text = value.decode(encoding, "replace").encode("utf-8").strip(_ASCII_SPACE)
    out = bytearray()
    spaced = False
    for byte in text:
        if byte < 0x80 and byte in _ASCII_SPACE:
            if not spaced:
                out.append(0x20)
            spaced = True
            continue
        spaced = False
        out.append(byte + 0x20 if 0x41 <= byte <= 0x5A else byte)
    return encode(0x0C, bytes(out))


def name_hashes(name_der: bytes) -> tuple[str, str]:
    """``openssl x509 -subject_hash`` and ``-subject_hash_old`` of a DER ``Name``.

    These are the file names a CA directory knows a certificate authority by
    (``<hash>.0``), and what GSI uses to say which authority stands behind a
    certificate: the new hash is SHA-1 over the canonical form of the name,
    the old one MD5 over its DER, each as the first four bytes read
    little-endian.
    """
    import hashlib

    canonical = bytearray()
    for rdn in raw_children(name_der):
        entries = []
        for attribute in raw_children(rdn):
            oid, value = parse(attribute)[0].children()
            entries.append(
                sequence(encode(oid.tag, oid.value), _canonical_value(value.tag, value.value))
            )
        canonical += encode(TAG_SET, b"".join(sorted(entries)))

    def short(digest: bytes) -> str:
        return f"{int.from_bytes(digest[:4], 'little'):08x}"

    return short(hashlib.sha1(bytes(canonical)).digest()), short(hashlib.md5(name_der).digest())


def issuer_hashes(certificate: Certificate) -> tuple[str, str]:
    """:func:`name_hashes` of the authority that signed ``certificate``."""
    return name_hashes(certificate_fields(certificate.der)["issuer"])


def certificate_fields(der: bytes) -> dict[str, bytes]:
    """The parts of a certificate a signer copies, each exactly as written.

    Keys: ``issuer``, ``subject``, ``spki`` and - when present -
    ``extensions`` (the inner ``SEQUENCE OF Extension``).
    """
    tbs = raw_children(der)[0]
    fields = raw_children(tbs)
    index = 1 if fields and fields[0][0] == 0xA0 else 0
    if len(fields) < index + 6:
        raise DERError("certificate body is missing required fields")
    out = {
        "issuer": fields[index + 2],
        "subject": fields[index + 4],
        "spki": fields[index + 5],
    }
    for extra in fields[index + 6 :]:
        if extra[0] == 0xA3:
            out["extensions"] = raw_children(extra)[0]
    return out


def extensions_of(der: bytes) -> list[Extension]:
    """Every extension of the certificate in ``der``, in order."""
    block = certificate_fields(der).get("extensions")
    return [parse_extension(raw) for raw in raw_children(block)] if block else []


def parse_extension(raw: bytes) -> Extension:
    """One DER ``Extension``: ``SEQUENCE { OID, BOOLEAN DEFAULT FALSE, OCTET STRING }``."""
    parts = parse(raw)[0].children()
    if len(parts) not in (2, 3) or parts[0].tag != TAG_OID or parts[-1].tag != TAG_OCTET_STRING:
        raise DERError("malformed certificate extension")
    critical = len(parts) == 3 and parts[1].tag == TAG_BOOLEAN and parts[1].value != b"\x00"
    return Extension(oid_string(parts[0]), critical, parts[-1].value, raw)


def signature_digest(algorithm: str) -> str:
    """The hash behind a ``sha*WithRSAEncryption`` OID; refuses anything else."""
    digest = _SIGNATURE_DIGESTS.get(algorithm)
    if digest is None:
        raise DERError(f"unsupported signature algorithm {algorithm}")
    return digest


def verify_signed(der: bytes, key: RSAPublicKey) -> bool:
    """Whether ``key`` signed the certificate or certificate request in ``der``.

    Both are ``SEQUENCE { body, AlgorithmIdentifier, BIT STRING }`` and both
    are signed over the body exactly as written, so one function serves both.
    """
    try:
        body, algorithm, signature = raw_children(der)[:3]
        digest = signature_digest(oid_string(parse(algorithm)[0].children()[0]))
        bits = parse(signature)[0]
    except (DERError, ValueError, IndexError):
        return False
    if bits.tag != TAG_BIT_STRING or bits.value[:1] != b"\x00":
        return False
    return key.verify(body, bits.value[1:], digest=digest)
