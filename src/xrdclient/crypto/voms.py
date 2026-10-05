"""VOMS attribute certificates, using maintained ASN.1 and crypto backends.

VOMS puts RFC 5755 attribute certificates (ACs) in extension
``1.3.6.1.4.1.8005.100.100.5`` of an RFC 3820 proxy.  The proxy is already
sent byte-for-byte by GSI and mutual TLS; this module supplies the client-side
piece historically provided by ``libvomsapi``: inspect the VO/FQAN claims and,
when trust directories are supplied, decide whether they are trustworthy.

The parsing and crypto dependencies support macOS. The checks
are modelled on BriX: holder binding, validity, embedded signer, signature,
issuer, targets, CA signatures/dates, and ``vomsdir`` LSC binding. This is not
yet a full RFC 5280 path validator: CA extension constraints and CRLs are not
validated. Applications needing that policy must not treat this as a complete
replacement for an established certificate path validator.
"""

from __future__ import annotations

import os
import shlex
import socket
import stat
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from asn1crypto import algos  # type: ignore[import-untyped]
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .._compat import SLOTS
from . import ed25519, p256
from .der import (
    TAG_BIT_STRING,
    TAG_BOOLEAN,
    TAG_GENERALIZED_TIME,
    TAG_INTEGER,
    TAG_NULL,
    TAG_OCTET_STRING,
    TAG_OID,
    TAG_SEQUENCE,
    TAG_UTF8_STRING,
    DERError,
    Element,
    encode,
    oid_string,
    parse,
    read_integer,
)
from .rsa import RSAPublicKey
from .x509 import (
    Name,
    decode_name,
    extensions_of,
    load_certificates,
    parse_certificate,
    verify_signed,
)

__all__ = [
    "VOMS_AC_OID",
    "VOMS_FQAN_OID",
    "VOMSAttribute",
    "VOMSDiagnostic",
    "VOMSEntry",
    "VOMSResult",
    "VOMSStatus",
    "default_ca_path",
    "default_voms_dir",
    "check_vomses",
    "inspect_voms",
    "validate_voms",
]

VOMS_AC_OID = "1.3.6.1.4.1.8005.100.100.5"
VOMS_FQAN_OID = "1.3.6.1.4.1.8005.100.100.4"
VOMS_CERTS_OID = "1.3.6.1.4.1.8005.100.100.10"
VOMS_ATTRIBUTES_OID = "1.3.6.1.4.1.8005.100.100.11"
_AKI_OID = "2.5.29.35"
_SKI_OID = "2.5.29.14"
_TARGETS_OID = "2.5.29.55"
_NO_REV_AVAIL_OID = "2.5.29.56"
_KNOWN_AC_EXTENSIONS = {
    VOMS_CERTS_OID,
    VOMS_ATTRIBUTES_OID,
    _AKI_OID,
    _TARGETS_OID,
    _NO_REV_AVAIL_OID,
}
_SIGNATURE_DIGESTS = {
    "1.2.840.113549.1.1.5": "sha1",
    "1.2.840.113549.1.1.11": "sha256",
    "1.2.840.113549.1.1.12": "sha384",
    "1.2.840.113549.1.1.13": "sha512",
}
_RSA_PSS_OID = "1.2.840.113549.1.1.10"
_RSA_ENCRYPTION_OID = "1.2.840.113549.1.1.1"
_ECDSA_SHA256_OID = "1.2.840.10045.4.3.2"
_EC_PUBLIC_KEY_OID = "1.2.840.10045.2.1"
_P256_OID = "1.2.840.10045.3.1.7"
_ED25519_OID = "1.3.101.112"

_MAX_CHAIN_DEPTH = 8
_MAX_AC_ENTRIES = 32


class Certificate(Protocol):
    """Read-only certificate view shared by both clients' credential models."""

    @property
    def subject(self) -> Name: ...

    @property
    def issuer(self) -> Name: ...

    @property
    def serial(self) -> int: ...

    @property
    def not_before(self) -> float: ...

    @property
    def not_after(self) -> float: ...

    @property
    def public_key(self) -> RSAPublicKey | None: ...

    @property
    def extensions(self) -> Mapping[str, tuple[bool, bytes]] | Sequence[str]: ...

    @property
    def der(self) -> bytes: ...

    @property
    def is_proxy(self) -> bool: ...


class VOMSStatus(str, Enum):
    """The first failed AC check, or ``ok`` after the implemented checks."""

    OK = "ok"
    UNCHECKED = "unchecked"
    NO_EXTENSION = "no_extension"
    DECODE = "decode"
    VERSION = "version"
    HOLDER = "holder"
    NOT_YET_VALID = "not_yet_valid"
    EXPIRED = "expired"
    NO_SIGNER = "no_signer"
    SIGNATURE_ALGORITHM = "signature_algorithm"
    SIGNATURE = "signature"
    ISSUER = "issuer"
    TARGET = "target"
    UNTRUSTED = "untrusted"
    LSC = "lsc"
    EXTENSION = "extension"
    ATTRIBUTES = "attributes"


@dataclass(frozen=True, **SLOTS)
class VOMSDiagnostic:
    """A stable error code and an actionable, non-secret user message."""

    code: str
    message: str
    path: str = ""
    errno: int | None = None


@dataclass(frozen=True, **SLOTS)
class VOMSAttribute:
    """One VOMS generic attribute (name, value, qualifier)."""

    name: str
    value: str
    qualifier: str = ""


@dataclass(frozen=True, **SLOTS)
class VOMSEntry:
    """One VOMS attribute certificate, normally representing one VO."""

    vo: str
    uri: str
    fqans: tuple[str, ...]
    attributes: tuple[VOMSAttribute, ...]
    holder: Name
    issuer: Name
    not_before: float
    not_after: float
    carrier: int
    status: VOMSStatus = VOMSStatus.UNCHECKED
    digest: str = ""
    signer: Certificate | None = field(default=None, repr=False, compare=False)
    diagnostics: tuple[VOMSDiagnostic, ...] = ()
    _version: int = field(default=0, repr=False, compare=False)
    _holder_serial: int | None = field(default=None, repr=False, compare=False)
    _tbs: bytes = field(default=b"", repr=False, compare=False)
    _inner_algorithm: bytes = field(default=b"", repr=False, compare=False)
    _outer_algorithm: bytes = field(default=b"", repr=False, compare=False)
    _signature: bytes = field(default=b"", repr=False, compare=False)
    _embedded: tuple[Certificate, ...] = field(default=(), repr=False, compare=False)
    _targets: tuple[str, ...] | None = field(default=None, repr=False, compare=False)
    _aki: bytes | None = field(default=None, repr=False, compare=False)
    _unknown_critical: bool = field(default=False, repr=False, compare=False)

    @property
    def verified(self) -> bool:
        """Whether every intrinsic and configured trust check passed."""
        return self.status is VOMSStatus.OK

    def remaining(self, now: float | None = None) -> float:
        """Seconds before the AC expires."""
        return self.not_after - (time.time() if now is None else now)

    @property
    def message(self) -> str:
        """Explain a failed check without requiring knowledge of status codes."""
        return "\n".join(issue.message for issue in self.diagnostics) or _status_message(
            self.status, self.vo
        )


@dataclass(frozen=True, **SLOTS)
class VOMSResult:
    """All ACs found in a proxy chain and the overall verdict."""

    entries: tuple[VOMSEntry, ...]
    status: VOMSStatus

    @property
    def diagnostics(self) -> tuple[VOMSDiagnostic, ...]:
        """Detailed trust-file failures, including failures in a mixed-VO proxy."""
        return tuple(issue for entry in self.entries for issue in entry.diagnostics)

    @property
    def message(self) -> str:
        """A plain-language summary suitable for a CLI error or exception."""
        failures = [entry.message for entry in self.entries if not entry.verified]
        return "\n".join(failures) or _status_message(self.status)

    @property
    def verified(self) -> tuple[VOMSEntry, ...]:
        """The independently verified entries; a bad AC never poisons a good one."""
        return tuple(entry for entry in self.entries if entry.verified)

    @property
    def fqans(self) -> tuple[str, ...]:
        """FQANs from verified entries, in wire order with duplicates removed."""
        return _unique(fqan for entry in self.verified for fqan in entry.fqans)

    @property
    def vos(self) -> tuple[str, ...]:
        """VO names from verified entries, in wire order with duplicates removed."""
        return _unique(entry.vo for entry in self.verified if entry.vo)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _status_message(status: VOMSStatus, vo: str = "") -> str:
    subject = f"VO '{vo}': " if vo else ""
    explanations = {
        VOMSStatus.OK: "VOMS validation passed.",
        VOMSStatus.UNCHECKED: "VOMS claims are decoded, not verified. Do not trust them yet.",
        VOMSStatus.NO_EXTENSION: "This proxy has no VOMS attributes. Obtain a VOMS-enabled proxy.",
        VOMSStatus.DECODE: "The VOMS attributes are damaged or malformed. Obtain a new proxy.",
        VOMSStatus.VERSION: (
            "This proxy uses an unsupported VOMS format. Obtain a new proxy or update the client."
        ),
        VOMSStatus.HOLDER: "VOMS attributes belong to a different certificate. Obtain a new proxy.",
        VOMSStatus.NOT_YET_VALID: "VOMS attributes are not valid yet. Check your computer's clock.",
        VOMSStatus.EXPIRED: "The VOMS attributes have expired. Obtain a new proxy.",
        VOMSStatus.NO_SIGNER: "The VOMS signing certificate is missing. Obtain a new proxy.",
        VOMSStatus.SIGNATURE_ALGORITHM: (
            "This proxy uses an unsupported or invalid VOMS signature. "
            "Obtain a new proxy or update the client."
        ),
        VOMSStatus.SIGNATURE: "Invalid VOMS signature. Obtain a new proxy; do not use this one.",
        VOMSStatus.ISSUER: (
            "The VO signing certificate does not match this proxy. "
            "Obtain a new proxy from your VO's service."
        ),
        VOMSStatus.TARGET: (
            "These VO permissions do not cover this server. "
            "Request a proxy for the intended service."
        ),
        VOMSStatus.UNTRUSTED: (
            "Cannot verify the VO signing certificate. "
            "Install or update the official CA certificates."
        ),
        VOMSStatus.LSC: (
            "Cannot confirm the VO signing server. Install or update your VO's official .lsc file."
        ),
        VOMSStatus.EXTENSION: (
            "This proxy needs an unsupported VOMS feature. "
            "Update the client or ask your VO administrator for a compatible proxy."
        ),
        VOMSStatus.ATTRIBUTES: (
            "This proxy has no usable VO or group membership. "
            "Request a proxy for the correct VO and group."
        ),
    }
    return subject + explanations[status]


def _problem(
    diagnostics: list[VOMSDiagnostic] | None,
    code: str,
    message: str,
    path: str = "",
    error_number: int | None = None,
) -> None:
    if diagnostics is not None:
        diagnostics.append(VOMSDiagnostic(code, message, path, error_number))


def _io_problem(
    diagnostics: list[VOMSDiagnostic] | None, role: str, path: Path, exc: OSError
) -> None:
    label = {
        "ca_directory": "CA certificates folder",
        "ca_file": "CA certificate file",
        "voms_directory": "VO trust folder",
        "lsc_file": "VOMS .lsc trust file",
        "legacy_signer_file": "VOMS signing certificate file",
        "vomses_path": "VOMS endpoint configuration",
        "vomses_directory": "VOMS endpoint folder",
        "vomses_file": "VOMS endpoint file",
    }.get(role, "configuration path")
    if isinstance(exc, PermissionError):
        reason, fix = (
            "permissions",
            "Ask the owner or administrator to allow your account to read the file "
            "and access its parent directories. Do not make private keys world-readable.",
        )
        description = "Your account does not have permission to read"
    elif isinstance(exc, FileNotFoundError):
        reason, fix = (
            "missing",
            "Install the official trust/configuration files or correct the path.",
        )
        description = "Cannot find"
    elif isinstance(exc, (NotADirectoryError, IsADirectoryError)):
        reason, fix = (
            "wrong_type",
            "Check that the configured path names the right file or directory.",
        )
        description = "The configured path has the wrong file type:"
    else:
        reason, fix = "unreadable", "Check the path and filesystem, then try again."
        description = "Cannot read"
    _problem(
        diagnostics,
        f"{role}_{reason}",
        f"{description} {label} '{path}'. {fix}",
        str(path),
        exc.errno,
    )


def _first_directory_name(element: Element) -> Name:
    """The first ``directoryName [4]`` below a GeneralNames-shaped value."""
    if element.tag == 0xA4:
        children = element.children()
        if len(children) == 1:
            return decode_name(_encoded(children[0]))
    if element.constructed:
        for child in element.children():
            found = _first_directory_name(child)
            if found:
                return found
    return Name()


def _encoded(element: Element) -> bytes:
    return encode(element.tag, element.value)


def _one(data: bytes) -> Element:
    element, end = parse(data)
    if end != len(data):
        raise DERError(f"{len(data) - end} trailing bytes after the element")
    return element


def _time(element: Element) -> float:
    if element.tag != TAG_GENERALIZED_TIME:
        raise DERError("VOMS validity is not GeneralizedTime")
    text = element.value.decode("ascii")
    if not text.endswith("Z"):
        raise DERError("VOMS validity is not UTC")
    try:
        return float(__import__("calendar").timegm(time.strptime(text, "%Y%m%d%H%M%SZ")))
    except ValueError as exc:
        raise DERError(f"malformed VOMS time {text!r}") from exc


def _extension_map(element: Element | None) -> dict[str, tuple[bool, bytes]]:
    found: dict[str, tuple[bool, bytes]] = {}
    if element is None:
        return found
    for extension in element.children():
        key, critical, value = _extension_fields(extension)
        found[key] = (critical, value)
    return found


def _extension_fields(extension: Element) -> tuple[str, bool, bytes]:
    if extension.tag != TAG_SEQUENCE:
        raise DERError("malformed VOMS AC extension")
    parts = extension.children()
    if len(parts) not in (2, 3) or parts[0].tag != TAG_OID:
        raise DERError("malformed VOMS AC extension")
    if len(parts) == 3 and parts[1].tag != TAG_BOOLEAN:
        raise DERError("malformed VOMS AC extension")
    if parts[-1].tag != TAG_OCTET_STRING:
        raise DERError("VOMS AC extension has no OCTET STRING value")
    critical = len(parts) == 3 and parts[1].value != b"\x00"
    return oid_string(parts[0]), critical, parts[-1].value


def _attributes(element: Element) -> tuple[str, str, tuple[str, ...]]:
    vo = uri = ""
    fqans: list[str] = []
    for attribute in element.children():
        parts = attribute.children()
        if not _is_fqan_attribute(parts):
            continue
        for syntax in parts[1].children():
            found_vo, found_uri, found_fqans = _fqan_syntax(syntax)
            if found_vo:
                vo, uri = found_vo, found_uri
            fqans.extend(found_fqans)
    if not vo and fqans:
        vo = fqans[0].split("/", 2)[1]
    return vo, uri, tuple(fqans)


def _is_fqan_attribute(parts: list[Element]) -> bool:
    return len(parts) >= 2 and parts[0].tag == TAG_OID and oid_string(parts[0]) == VOMS_FQAN_OID


def _fqan_syntax(syntax: Element) -> tuple[str, str, tuple[str, ...]]:
    fields = syntax.children()
    if not fields:
        return "", "", ()
    vo, uri = _policy_authority(fields[0])
    fqans = tuple(
        text
        for value in fields[-1].children()
        if value.tag in (TAG_OCTET_STRING, TAG_UTF8_STRING)
        if _valid_fqan(text := value.value.decode("utf-8", "replace"))
    )
    return vo, uri, fqans


def _policy_authority(element: Element) -> tuple[str, str]:
    if element.tag != 0xA0:
        return "", ""
    policy = _general_name_text(element)
    if "://" not in policy:
        return "", ""
    vo, uri = policy.split("://", 1)
    return vo, uri


def _valid_fqan(value: str) -> bool:
    return (
        len(value) > 1
        and value.startswith("/")
        and "," not in value
        and all("!" <= char <= "~" for char in value)
    )


def _general_name_text(element: Element) -> str:
    if element.tag in (0x82, 0x86):
        return element.value.decode("ascii", "replace")
    if element.constructed:
        for child in element.children():
            found = _general_name_text(child)
            if found:
                return found
    return ""


def _generic_attributes(data: bytes | None) -> tuple[VOMSAttribute, ...]:
    if data is None:
        return ()
    out: list[VOMSAttribute] = []
    outer = _one(data)
    providers = outer.children()[0].children() if outer.children() else []
    for provider in providers:
        fields = provider.children()
        if len(fields) < 2:
            continue
        for triple in fields[-1].children():
            values = triple.children()
            if len(values) == 3 and all(value.tag == TAG_OCTET_STRING for value in values):
                name, qualifier, value = (part.value.decode("utf-8", "replace") for part in values)
                out.append(VOMSAttribute(name, value, qualifier))
    return tuple(out)


def _embedded_certificates(data: bytes | None) -> tuple[Certificate, ...]:
    if data is None:
        return ()
    outer = _one(data).children()
    if len(outer) != 1:
        raise DERError("VOMS certs extension is not a one-element sequence")
    certs: list[Certificate] = []
    pos = 0
    body = outer[0].value
    while pos < len(body):
        _element, end = parse(body, pos)
        certs.append(parse_certificate(body[pos:end]))
        pos = end
    return tuple(certs)


def _targets(data: bytes | None) -> tuple[str, ...] | None:
    if data is None:
        return None
    names: list[str] = []
    outer = _one(data)
    for group in outer.children():
        for target in group.children():
            if target.tag == 0xA0:
                text = _general_name_text(target)
                if text:
                    names.append(text.lower().rstrip("."))
    return tuple(names)


def _authority_key_id(data: bytes | None) -> bytes | None:
    if data is None:
        return None
    for child in _one(data).children():
        if child.tag == 0x80:
            return child.value
    return None


def _decode_ac(element: Element, carrier: int) -> VOMSEntry:
    top, info = _ac_parts(element)
    holder, holder_serial = _ac_holder(info[1])
    extensions = _extension_map(_ac_extensions(info))
    embedded = _embedded_certificates(_extension_value(extensions, VOMS_CERTS_OID))
    generic = _generic_attributes(_extension_value(extensions, VOMS_ATTRIBUTES_OID))
    validity = _ac_validity(info[5])
    vo, uri, fqans = _attributes(info[6])
    return VOMSEntry(
        vo=vo,
        uri=uri,
        fqans=fqans,
        attributes=generic,
        holder=holder,
        issuer=_first_directory_name(info[2]),
        not_before=_time(validity[0]),
        not_after=_time(validity[1]),
        carrier=carrier,
        _version=read_integer(info[0]),
        _holder_serial=holder_serial,
        _tbs=_encoded(top[0]),
        _inner_algorithm=_encoded(info[3]),
        _outer_algorithm=_encoded(top[1]),
        _signature=_ac_signature(top[2]),
        _embedded=embedded,
        _targets=_targets(_extension_value(extensions, _TARGETS_OID)),
        _aki=_authority_key_id(_extension_value(extensions, _AKI_OID)),
        _unknown_critical=_has_unknown_critical(extensions),
    )


def _ac_parts(element: Element) -> tuple[list[Element], list[Element]]:
    top = element.children()
    if (
        element.tag != TAG_SEQUENCE
        or len(top) != 3
        or top[0].tag != TAG_SEQUENCE
        or top[1].tag != TAG_SEQUENCE
        or top[2].tag != TAG_BIT_STRING
        or not top[2].value
    ):
        raise DERError("malformed VOMS attribute certificate")
    info = top[0].children()
    if len(info) < 7:
        raise DERError("VOMS attribute certificate is missing required fields")
    return top, info


def _ac_holder(element: Element) -> tuple[Name, int | None]:
    holder_fields = element.children()
    base = next((item for item in holder_fields if item.tag == 0xA0), None)
    holder = Name()
    holder_serial: int | None = None
    if base is not None:
        base_fields = base.children()
        if base_fields:
            holder = _first_directory_name(base_fields[0])
        holder_serial = next(
            (read_integer(item) for item in base_fields if item.tag == TAG_INTEGER), None
        )
    return holder, holder_serial


def _ac_validity(element: Element) -> list[Element]:
    validity = element.children()
    if len(validity) != 2:
        raise DERError("VOMS validity is not a pair")
    return validity


def _ac_extensions(info: list[Element]) -> Element | None:
    if len(info) <= 7:
        return None
    return info[8] if len(info) > 8 and info[7].tag == TAG_BIT_STRING else info[7]


def _ac_signature(element: Element) -> bytes:
    return element.value[1:] if element.value[:1] == b"\x00" else b""


def _has_unknown_critical(extensions: dict[str, tuple[bool, bytes]]) -> bool:
    return any(
        critical and oid not in _KNOWN_AC_EXTENSIONS for oid, (critical, _) in extensions.items()
    )


def _extension_value(values: dict[str, tuple[bool, bytes]], oid: str) -> bytes | None:
    found = values.get(oid)
    return found[1] if found is not None else None


def _decode_extension(data: bytes, carrier: int) -> tuple[VOMSEntry, ...]:
    root = _one(data)
    outer = root.children()
    if root.tag != TAG_SEQUENCE or len(outer) != 1 or outer[0].tag != TAG_SEQUENCE:
        raise DERError("VOMS AC sequence has the wrong shape")
    certificates = outer[0].children()
    if not certificates:
        raise DERError("VOMS AC sequence is empty")
    if len(certificates) > _MAX_AC_ENTRIES:
        raise DERError("VOMS AC sequence has too many entries")
    return tuple(_decode_ac(ac, carrier) for ac in certificates)


def _voms_extension(certificate: Certificate) -> bytes | None:
    return _certificate_extension(certificate, VOMS_AC_OID)


def inspect_voms(chain: Sequence[Certificate]) -> VOMSResult:
    """Decode every VOMS AC in ``chain`` without making trust decisions.

    ``carrier`` is the certificate's index in ``chain``.  Delegated proxies
    commonly carry the AC on their parent rather than their leaf, so every
    certificate is searched.
    """
    entries: list[VOMSEntry] = []
    saw_extension = False
    failed = False
    for carrier, certificate in enumerate(chain):
        try:
            data = _voms_extension(certificate)
        except (DERError, ValueError, IndexError):
            failed = True
            continue
        if data is None:
            continue
        saw_extension = True
        try:
            entries.extend(_decode_extension(data, carrier))
        except (DERError, ValueError, IndexError):
            failed = True
    status = (
        VOMSStatus.UNCHECKED
        if entries
        else (VOMSStatus.DECODE if saw_extension or failed else VOMSStatus.NO_EXTENSION)
    )
    return VOMSResult(tuple(entries), status)


def validate_voms(
    chain: Sequence[Certificate],
    *,
    ca_path: str | None = None,
    voms_dir: str | None = None,
    host: str | None = None,
    now: float | None = None,
    skew: int = 300,
) -> VOMSResult:
    """Decode and validate all VOMS ACs in a proxy chain.

    ``ca_path`` and ``voms_dir`` default through ``X509_CERT_DIR`` and
    ``X509_VOMS_DIR`` and then conventional Linux/Homebrew locations.  A
    missing directory is a failed trust check, never a silent downgrade.
    Individual ACs are independent: one good VO still verifies when another
    AC in the same proxy is bad.
    """
    decoded = inspect_voms(chain)
    if not decoded.entries:
        return decoded
    ca = ca_path or default_ca_path()
    vdir = voms_dir or default_voms_dir()
    moment = time.time() if now is None else now
    checked = tuple(
        _validate_entry(entry, chain, ca, vdir, host, moment, skew) for entry in decoded.entries
    )
    good = any(entry.verified for entry in checked)
    status = VOMSStatus.OK if good else checked[0].status
    return VOMSResult(checked, status)


def _validate_entry(
    entry: VOMSEntry,
    chain: Sequence[Certificate],
    ca_path: str | None,
    voms_dir: str | None,
    host: str | None,
    now: float,
    skew: int,
) -> VOMSEntry:
    status = _intrinsic_status(entry, chain, host, now, skew)
    if status is not VOMSStatus.OK:
        return replace(entry, status=status)
    signer_chain = _signer_chain(entry)
    if not signer_chain:
        return replace(entry, status=VOMSStatus.ISSUER)
    signer = signer_chain[0]
    signature_status, digest = _signature_status(entry, signer)
    if signature_status is not VOMSStatus.OK:
        return replace(entry, status=signature_status, digest=digest, signer=signer)
    if not _aki_matches(entry, signer):
        return replace(entry, status=VOMSStatus.ISSUER, digest=digest, signer=signer)
    diagnostics: list[VOMSDiagnostic] = []
    trust_status = _external_trust_status(entry, signer_chain, ca_path, voms_dir, now, diagnostics)
    return replace(
        entry, status=trust_status, digest=digest, signer=signer, diagnostics=tuple(diagnostics)
    )


def _signature_status(entry: VOMSEntry, signer: Certificate) -> tuple[VOMSStatus, str]:
    algorithm = _signature_algorithm(entry)
    if algorithm is None:
        return VOMSStatus.SIGNATURE_ALGORITHM, ""
    oid, parameters = algorithm
    digest = _SIGNATURE_DIGESTS.get(oid)
    if digest is not None:
        if not _null_parameters(parameters):
            return VOMSStatus.SIGNATURE_ALGORITHM, ""
        return _rsa_signature_status(entry, signer, digest)
    if oid == _RSA_PSS_OID:
        return _pss_signature_status(entry, signer, parameters)
    if oid == _ECDSA_SHA256_OID and not parameters:
        return _ecdsa_signature_status(entry, signer)
    if oid == _ED25519_OID and not parameters:
        return _ed25519_signature_status(entry, signer)
    return VOMSStatus.SIGNATURE_ALGORITHM, ""


def _rsa_signature_status(
    entry: VOMSEntry, signer: Certificate, digest: str
) -> tuple[VOMSStatus, str]:
    key = _rsa_signer_key(signer)
    if key is None or not key.verify(entry._tbs, entry._signature, digest=digest):
        return VOMSStatus.SIGNATURE, digest
    return VOMSStatus.OK, digest


def _pss_signature_status(
    entry: VOMSEntry, signer: Certificate, parameters: list[Element]
) -> tuple[VOMSStatus, str]:
    config = _pss_parameters(parameters)
    if config is None:
        return VOMSStatus.SIGNATURE_ALGORITHM, ""
    key = _rsa_signer_key(signer)
    valid = key is not None and _verify_pss(
        key.n,
        key.e,
        entry._tbs,
        entry._signature,
        config.digest,
        config.mgf_digest,
        config.salt_length,
    )
    return (VOMSStatus.OK if valid else VOMSStatus.SIGNATURE), "rsassa-pss"


def _ecdsa_signature_status(entry: VOMSEntry, signer: Certificate) -> tuple[VOMSStatus, str]:
    try:
        algorithm, parameters, key = _subject_public_key(signer)
        if algorithm != _EC_PUBLIC_KEY_OID or _only_oid(parameters) != _P256_OID:
            return VOMSStatus.SIGNATURE, "sha256"
        public = p256.decode_point(key)
        signature = _one(entry._signature).children()
        if len(signature) != 2:
            return VOMSStatus.SIGNATURE, "sha256"
        valid = p256.verify(
            public, entry._tbs, read_integer(signature[0]), read_integer(signature[1])
        )
    except (DERError, IndexError, ValueError):
        valid = False
    return (VOMSStatus.OK if valid else VOMSStatus.SIGNATURE), "sha256"


def _ed25519_signature_status(entry: VOMSEntry, signer: Certificate) -> tuple[VOMSStatus, str]:
    try:
        algorithm, parameters, key = _subject_public_key(signer)
        valid = (
            algorithm == _ED25519_OID
            and not parameters
            and ed25519.verify(key, entry._tbs, entry._signature)
        )
    except (DERError, IndexError, ValueError):
        valid = False
    return (VOMSStatus.OK if valid else VOMSStatus.SIGNATURE), "ed25519"


def _aki_matches(entry: VOMSEntry, signer: Certificate) -> bool:
    if entry._aki is None:
        return True
    ski = _certificate_extension(signer, _SKI_OID)
    if ski is None:
        return True
    try:
        return entry._aki == _one(ski).value
    except DERError:
        return False


def _external_trust_status(
    entry: VOMSEntry,
    signer_chain: Sequence[Certificate],
    ca_path: str | None,
    voms_dir: str | None,
    now: float,
    diagnostics: list[VOMSDiagnostic] | None = None,
) -> VOMSStatus:
    if ca_path is None:
        _problem(
            diagnostics,
            "ca_directory_missing",
            "No CA certificate directory was found. Install your trusted CA bundle "
            "and set X509_CERT_DIR to its certificates directory.",
        )
        return VOMSStatus.UNTRUSTED
    pending: list[VOMSDiagnostic] = []
    if not _trusted_chain(signer_chain, ca_path, now, pending):
        if diagnostics is not None:
            diagnostics.extend(pending)
        return VOMSStatus.UNTRUSTED
    if voms_dir is None:
        _problem(
            diagnostics,
            "voms_directory_missing",
            f"No VOMS trust directory was found for VO '{entry.vo}'. Install the VO's official "
            ".lsc files and set X509_VOMS_DIR to the vomsdir directory.",
        )
        return VOMSStatus.LSC
    if not _lsc_matches(voms_dir, entry.vo, signer_chain, diagnostics):
        return VOMSStatus.LSC
    return VOMSStatus.OK


def _intrinsic_status(
    entry: VOMSEntry,
    chain: Sequence[Certificate],
    host: str | None,
    now: float,
    skew: int,
) -> VOMSStatus:
    status = _claims_status(entry)
    if status is not VOMSStatus.OK:
        return status
    if not _holder_matches(entry, chain):
        return VOMSStatus.HOLDER
    status = _validity_status(entry, now, skew)
    if status is not VOMSStatus.OK:
        return status
    status = _signature_shape_status(entry)
    if status is not VOMSStatus.OK:
        return status
    return _target_status(entry, host)


def _claims_status(entry: VOMSEntry) -> VOMSStatus:
    if entry._version != 1:
        return VOMSStatus.VERSION
    if entry._unknown_critical:
        return VOMSStatus.EXTENSION
    if not entry.fqans or not entry.vo:
        return VOMSStatus.ATTRIBUTES
    return VOMSStatus.OK


def _validity_status(entry: VOMSEntry, now: float, skew: int) -> VOMSStatus:
    if entry.not_before > now + skew:
        return VOMSStatus.NOT_YET_VALID
    if entry.not_after < now:
        return VOMSStatus.EXPIRED
    return VOMSStatus.OK


def _signature_shape_status(entry: VOMSEntry) -> VOMSStatus:
    if not entry._embedded:
        return VOMSStatus.NO_SIGNER
    if entry._inner_algorithm != entry._outer_algorithm:
        return VOMSStatus.SIGNATURE_ALGORITHM
    return VOMSStatus.OK


def _target_status(entry: VOMSEntry, host: str | None) -> VOMSStatus:
    targets = entry._targets
    wanted = (host or socket.gethostname()).lower().rstrip(".")
    if targets is not None and targets and wanted not in targets:
        return VOMSStatus.TARGET
    return VOMSStatus.OK


def _holder_matches(entry: VOMSEntry, chain: Sequence[Certificate]) -> bool:
    if not entry.holder or entry._holder_serial is None or entry.carrier >= len(chain):
        return False
    current = chain[entry.carrier]
    seen: set[bytes] = set()
    while current.der not in seen:
        seen.add(current.der)
        if _is_holder(entry, current):
            return True
        if not current.is_proxy:
            break
        parent = _proxy_parent(current, chain)
        if parent is None:
            break
        current = parent
    return False


def _is_holder(entry: VOMSEntry, certificate: Certificate) -> bool:
    return entry._holder_serial == certificate.serial and entry.holder in (
        certificate.subject,
        certificate.issuer,
    )


def _proxy_parent(certificate: Certificate, chain: Sequence[Certificate]) -> Certificate | None:
    return next(
        (
            candidate
            for candidate in chain
            if candidate.der != certificate.der and candidate.subject == certificate.issuer
        ),
        None,
    )


def _signer_chain(entry: VOMSEntry) -> tuple[Certificate, ...]:
    for index, certificate in enumerate(entry._embedded):
        if certificate.subject == entry.issuer:
            return (certificate, *entry._embedded[:index], *entry._embedded[index + 1 :])
    return ()


def _signature_algorithm(entry: VOMSEntry) -> tuple[str, list[Element]] | None:
    try:
        return _algorithm_identifier(_one(entry._outer_algorithm))
    except (DERError, IndexError):
        return None


def _signature_digest(entry: VOMSEntry) -> str | None:
    algorithm = _signature_algorithm(entry)
    if algorithm is None:
        return None
    oid, parameters = algorithm
    digest = _SIGNATURE_DIGESTS.get(oid)
    if digest is not None:
        return digest if _null_parameters(parameters) else None
    return _non_rsa_signature_label(oid, parameters)


def _non_rsa_signature_label(oid: str, parameters: list[Element]) -> str | None:
    if oid == _RSA_PSS_OID and _pss_parameters(parameters) is not None:
        return "rsassa-pss"
    if oid == _ECDSA_SHA256_OID and not parameters:
        return "sha256"
    if oid == _ED25519_OID and not parameters:
        return "ed25519"
    return None


def _algorithm_identifier(element: Element) -> tuple[str, list[Element]]:
    if element.tag != TAG_SEQUENCE:
        raise DERError("signature algorithm has no OID")
    parts = element.children()
    if not 1 <= len(parts) <= 2 or parts[0].tag != TAG_OID:
        raise DERError("signature algorithm has no OID")
    return oid_string(parts[0]), parts[1:]


def _null_parameters(parameters: list[Element]) -> bool:
    return not parameters or (
        len(parameters) == 1 and parameters[0].tag == TAG_NULL and not parameters[0].value
    )


def _rsa_signer_key(signer: Certificate) -> RSAPublicKey | None:
    try:
        algorithm, parameters, _key = _subject_public_key(signer)
    except (DERError, IndexError):
        return None
    if algorithm != _RSA_ENCRYPTION_OID or not _null_parameters(parameters):
        return None
    return signer.public_key


@dataclass(frozen=True, **SLOTS)
class _PSSParameters:
    digest: str = "sha1"
    mgf_digest: str = "sha1"
    salt_length: int = 20


def _pss_parameters(parameters: list[Element]) -> _PSSParameters | None:
    if not parameters:
        return _PSSParameters()
    if len(parameters) != 1 or parameters[0].tag != TAG_SEQUENCE:
        return None
    try:
        # DER fields must be unique, ordered, and known. asn1crypto accepts
        # some BER schema leftovers, so enforce that input policy explicitly.
        if not _pss_fields_valid(parameters[0]):
            return None
        config = algos.RSASSAPSSParams.load(_encoded(parameters[0]), strict=True).native
        return _pss_config(config)
    except (ValueError, TypeError, KeyError):
        return None


def _pss_fields_valid(element: Element) -> bool:
    tags = [field.tag for field in element.children()]
    return tags == sorted(set(tags)) and all(tag in range(0xA0, 0xA4) for tag in tags)


def _pss_config(config: dict[str, Any]) -> _PSSParameters | None:
    digest = _pss_hash(config["hash_algorithm"])
    mgf = config["mask_gen_algorithm"]
    if set(mgf) != {"algorithm", "parameters"} or mgf["algorithm"] != "mgf1":
        return None
    mgf_digest = _pss_hash(mgf["parameters"])
    salt_length = config["salt_length"]
    if digest is None or mgf_digest is None or salt_length < 0:
        return None
    if config["trailer_field"] != "trailer_field_bc":
        return None
    return _PSSParameters(digest, mgf_digest, salt_length)


def _pss_hash(algorithm: dict[str, Any]) -> str | None:
    if set(algorithm) != {"algorithm", "parameters"}:
        return None
    digest = algorithm["algorithm"]
    if digest not in ("sha1", "sha256", "sha384", "sha512") or algorithm["parameters"] is not None:
        return None
    return str(digest)


def _subject_public_key(certificate: Certificate) -> tuple[str, list[Element], bytes]:
    top = _one(certificate.der).children()
    if not top:
        raise DERError("certificate has no body")
    fields = top[0].children()
    index = 1 if fields and fields[0].tag == 0xA0 else 0
    if len(fields) <= index + 5:
        raise DERError("certificate has no subject public key")
    spki = fields[index + 5].children()
    if len(spki) != 2 or spki[1].tag != TAG_BIT_STRING or not spki[1].value:
        raise DERError("certificate has a malformed subject public key")
    algorithm, parameters = _algorithm_identifier(spki[0])
    if spki[1].value[0] != 0:
        raise DERError("subject public key has unused bits")
    return algorithm, parameters, spki[1].value[1:]


def _only_oid(elements: list[Element]) -> str | None:
    if len(elements) != 1 or elements[0].tag != TAG_OID:
        return None
    return oid_string(elements[0])


def _verify_pss(
    modulus: int,
    exponent: int,
    message: bytes,
    signature: bytes,
    digest: str,
    mgf_digest: str,
    salt_length: int,
) -> bool:
    algorithms: dict[
        str, type[hashes.SHA1] | type[hashes.SHA256] | type[hashes.SHA384] | type[hashes.SHA512]
    ] = {
        "sha1": hashes.SHA1,
        "sha256": hashes.SHA256,
        "sha384": hashes.SHA384,
        "sha512": hashes.SHA512,
    }
    try:
        key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
        key.verify(
            signature,
            message,
            padding.PSS(mgf=padding.MGF1(algorithms[mgf_digest]()), salt_length=salt_length),
            algorithms[digest](),
        )
    except (InvalidSignature, ValueError, KeyError):
        return False
    return True


def _certificate_extension(certificate: Certificate, wanted: str) -> bytes | None:
    if isinstance(certificate.extensions, Mapping):
        found = certificate.extensions.get(wanted)
        return found[1] if found is not None else None
    for extension in extensions_of(certificate.der):
        if extension.oid == wanted:
            return extension.value
    return None


def _trusted_chain(
    embedded: Sequence[Certificate],
    ca_path: str,
    now: float,
    diagnostics: list[VOMSDiagnostic] | None = None,
) -> bool:
    origins: dict[bytes, str] = {}
    anchors = _load_anchors(ca_path, diagnostics, origins)
    if not anchors:
        return False
    candidates = [*embedded[1:], *anchors]
    current = embedded[0]
    for _ in range(_MAX_CHAIN_DEPTH):
        if not current.not_before <= now < current.not_after:
            _certificate_time_problem(diagnostics, current, now, ca_path, "signer_certificate")
            return False
        issuer = _trusted_issuer(current, candidates)
        if issuer is None:
            _problem(
                diagnostics,
                "ca_chain_incomplete",
                f"Cannot link VOMS signer '{current.subject}' to a trusted CA in '{ca_path}'. "
                "Check that the correct CA bundle and intermediate certificates are installed.",
                ca_path,
            )
            return False
        if _is_anchor(issuer, anchors):
            if issuer.not_before <= now < issuer.not_after:
                return True
            _certificate_time_problem(
                diagnostics, issuer, now, origins[issuer.der], "ca_certificate"
            )
            return False
        current = issuer
    _problem(
        diagnostics,
        "ca_chain_depth",
        "The VOMS signing certificate chain is too long or contains a loop. "
        "Check the intermediate certificates in the VO's configuration.",
        ca_path,
    )
    return False


def _certificate_time_problem(
    diagnostics: list[VOMSDiagnostic] | None,
    certificate: Certificate,
    now: float,
    path: str,
    role: str,
) -> None:
    expired = now >= certificate.not_after
    boundary = certificate.not_after if expired else certificate.not_before
    stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(boundary))
    state = "expired at" if expired else "is not valid before"
    label = "CA certificate" if role == "ca_certificate" else "VO signing certificate"
    fix = (
        "Update the trusted CA bundle."
        if role == "ca_certificate"
        else "Ask the VO administrator to check the signing certificate and obtain a new proxy."
    )
    _problem(
        diagnostics,
        f"{role}_{'expired' if expired else 'not_yet_valid'}",
        f"{label} '{certificate.subject}' {state} {stamp} (trust path: '{path}'). {fix} "
        "If the date looks wrong, check your computer's clock.",
        path,
    )


def _trusted_issuer(
    certificate: Certificate, candidates: Sequence[Certificate]
) -> Certificate | None:
    for issuer in candidates:
        if issuer.subject != certificate.issuer or issuer.public_key is None:
            continue
        if verify_signed(certificate.der, issuer.public_key):
            return issuer
    return None


def _is_anchor(certificate: Certificate, anchors: Sequence[Certificate]) -> bool:
    return any(certificate.der == anchor.der for anchor in anchors)


def _load_anchors(
    directory: str,
    diagnostics: list[VOMSDiagnostic] | None = None,
    origins: dict[bytes, str] | None = None,
) -> list[Certificate]:
    try:
        names = sorted(os.listdir(directory))
    except OSError as exc:
        _io_problem(diagnostics, "ca_directory", Path(directory), exc)
        return []
    found: list[Certificate] = []
    for name in names:
        if not _is_anchor_filename(name):
            continue
        path = Path(directory, name)
        certificates = _read_anchor_file(path, diagnostics)
        found.extend(certificates)
        if origins is not None:
            for certificate in certificates:
                origins.setdefault(certificate.der, str(path))
    if not found:
        _problem(
            diagnostics,
            "ca_store_empty",
            f"No usable CA certificates were found in '{directory}'. Install the trusted "
            "CA bundle there; this directory needs hash-named certificate files such as "
            "'12345678.0'.",
            directory,
        )
    return found


def _is_anchor_filename(name: str) -> bool:
    stem, dot, index = name.partition(".")
    return bool(dot and index.isdigit() and len(stem) == 8)


def _read_anchor_file(path: Path, diagnostics: list[VOMSDiagnostic] | None) -> list[Certificate]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        _io_problem(diagnostics, "ca_file", path, exc)
        return []
    try:
        certificates: list[Certificate] = list(load_certificates(data))
    except (DERError, IndexError, ValueError):
        certificates = []
    if not certificates:
        _problem(
            diagnostics,
            "ca_file_corrupt",
            f"CA certificate file '{path}' is empty, damaged or not a certificate. "
            "Reinstall it from your trusted CA bundle.",
            str(path),
        )
    return certificates


def _lsc_matches(
    directory: str,
    vo: str,
    chain: Sequence[Certificate],
    diagnostics: list[VOMSDiagnostic] | None = None,
) -> bool:
    if not _safe_vo(vo) or not chain:
        _problem(diagnostics, "vo_name_invalid", "The VOMS VO name or signing chain is invalid.")
        return False
    pending: list[VOMSDiagnostic] = []
    vo_path = Path(directory, vo)
    files = _trust_files(vo_path, pending)
    lsc_files = [path for path in files if path.name.endswith(".lsc")]
    if _voms_files_match(Path(directory), files, lsc_files, chain, pending):
        return True
    if not lsc_files:
        _problem(
            pending,
            "lsc_missing",
            f"No .lsc trust file was found for VO '{vo}' in '{vo_path}'. "
            "Install the VO's official .lsc file or correct X509_VOMS_DIR.",
            str(vo_path),
        )
    if diagnostics is not None:
        diagnostics.extend(pending)
    return False


def _voms_files_match(
    root: Path,
    files: Sequence[Path],
    lsc_files: Sequence[Path],
    chain: Sequence[Certificate],
    diagnostics: list[VOMSDiagnostic],
) -> bool:
    return any(_lsc_file_matches(path, chain, diagnostics) for path in lsc_files) or (
        _legacy_matches(_legacy_files(root, files), chain[0], diagnostics)
    )


def _safe_vo(vo: str) -> bool:
    return (
        bool(vo) and vo not in (".", "..") and all(char.isalnum() or char in "_.-" for char in vo)
    )


def _trust_files(
    directory: Path,
    diagnostics: list[VOMSDiagnostic] | None = None,
    role: str = "voms_directory",
) -> list[Path]:
    try:
        return sorted(
            path for path in directory.iterdir() if path.is_file() and not path.name.startswith(".")
        )
    except OSError as exc:
        _io_problem(diagnostics, role, directory, exc)
        return []


def _legacy_files(root: Path, vo_files: Sequence[Path]) -> list[Path]:
    found = [path for path in vo_files if not path.name.endswith(".lsc")]
    found.extend(path for path in _trust_files(root) if not path.name.endswith(".lsc"))
    return found


def _legacy_matches(
    paths: Sequence[Path],
    signer: Certificate,
    diagnostics: list[VOMSDiagnostic] | None = None,
) -> bool:
    for path in paths:
        try:
            certificates = load_certificates(path.read_bytes())
            if any(certificate.der == signer.der for certificate in certificates):
                return True
        except OSError as exc:
            _io_problem(diagnostics, "legacy_signer_file", path, exc)
            continue
        except (DERError, IndexError, ValueError):
            _problem(
                diagnostics,
                "legacy_signer_file_corrupt",
                f"Legacy VOMS signer file '{path}' is damaged or is not a certificate. "
                "Reinstall the VO's official trust configuration.",
                str(path),
            )
    return False


def _lsc_file_matches(
    path: Path,
    chain: Sequence[Certificate],
    diagnostics: list[VOMSDiagnostic] | None = None,
) -> bool:
    lines = _read_lsc_lines(path, diagnostics)
    if lines is None:
        return False
    pairs = len(lines) // 2
    if not pairs or len(lines) % 2:
        _problem(
            diagnostics,
            "lsc_corrupt",
            f"VOMS trust file '{path}' is empty or incomplete. Reinstall the VO's official "
            ".lsc file; it needs two lines per subject/issuer pair.",
            str(path),
        )
        return False
    matches = _lsc_lines_match(lines, chain)
    if not matches:
        _problem(
            diagnostics,
            "lsc_mismatch",
            f"VOMS trust file '{path}' does not match this proxy's VO signing server. "
            "Obtain the current .lsc file from the VO administrator.",
            str(path),
        )
    return matches


def _read_lsc_lines(path: Path, diagnostics: list[VOMSDiagnostic] | None) -> list[str] | None:
    try:
        return [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except OSError as exc:
        _io_problem(diagnostics, "lsc_file", path, exc)
        return None
    except UnicodeError:
        _problem(
            diagnostics,
            "lsc_corrupt",
            f"VOMS trust file '{path}' is not valid UTF-8 text. "
            "Reinstall the VO's official .lsc file.",
            str(path),
        )
        return None


def _lsc_lines_match(lines: Sequence[str], chain: Sequence[Certificate]) -> bool:
    pairs = len(lines) // 2
    return pairs <= len(chain) and all(
        _dn_matches(lines[2 * index], chain[index].subject)
        and _dn_matches(lines[2 * index + 1], chain[index].issuer)
        for index in range(pairs)
    )


def _dn_matches(value: str, name: Name) -> bool:
    return value == str(name) or value == _rfc2253(name)


def _rfc2253(name: Name) -> str:
    return ",".join(f"{key}={_escape_dn(value)}" for key, value in reversed(name.rdns))


def _escape_dn(value: str) -> str:
    escaped = "".join("\\" + char if char in ',+"\\<>;=' else char for char in value)
    if escaped.startswith((" ", "#")):
        escaped = "\\" + escaped
    if escaped.endswith(" "):
        escaped = escaped[:-1] + "\\ "
    return escaped


def _candidate_directories(kind: str) -> tuple[str, ...]:
    suffix = f"grid-security/{kind}"
    candidates = [f"/etc/{suffix}"]
    if sys.platform == "darwin":
        candidates.extend((f"/opt/homebrew/etc/{suffix}", f"/usr/local/etc/{suffix}"))
    return tuple(candidates)


def _default_directory(variable: str, kind: str) -> str | None:
    configured = os.environ.get(variable, "").strip()
    if configured:
        return configured
    return next((path for path in _candidate_directories(kind) if os.path.isdir(path)), None)


def default_ca_path() -> str | None:
    """Grid CA directory from the environment or Linux/Homebrew conventions."""
    return _default_directory("X509_CERT_DIR", "certificates")


def default_voms_dir() -> str | None:
    """VOMS LSC directory from the environment or Linux/Homebrew conventions."""
    return _default_directory("X509_VOMS_DIR", "vomsdir")


def check_vomses(path: str) -> tuple[VOMSDiagnostic, ...]:
    """Preflight endpoint configuration for obtaining a proxy, without network I/O.

    Accept a vomses file or directory of files. This is deliberately separate
    from existing-proxy validation: vomses is not CA or signer trust material,
    and is unnecessary when consuming a proxy that has already been issued.
    An empty result means the local configuration is readable and well-formed,
    not that a remote endpoint is reachable or trusted.
    """
    configured = Path(path)
    diagnostics: list[VOMSDiagnostic] = []
    try:
        mode = configured.stat().st_mode
    except OSError as exc:
        _io_problem(diagnostics, "vomses_path", configured, exc)
        if isinstance(exc, FileNotFoundError):
            issue = diagnostics[-1]
            diagnostics[-1] = replace(
                issue,
                message=issue.message + " This endpoint configuration is needed to "
                "obtain a proxy, not to use an existing one.",
            )
        return tuple(diagnostics)
    if stat.S_ISDIR(mode):
        files = _trust_files(configured, diagnostics, "vomses_directory")
    elif stat.S_ISREG(mode):
        files = [configured]
    else:
        _problem(
            diagnostics,
            "vomses_path_wrong_type",
            f"VOMS endpoint path '{path}' is not a regular file or directory. "
            "Use the VO's official vomses file or directory.",
            path,
        )
        return tuple(diagnostics)
    endpoints = sum(_check_vomses_file(file, diagnostics) for file in files)
    if not endpoints and not diagnostics:
        _problem(
            diagnostics,
            "vomses_empty",
            f"No VOMS endpoints are configured in '{path}'. Install the VO's official vomses "
            "configuration to obtain a proxy. Using an existing proxy does not require it.",
            path,
        )
    return tuple(diagnostics)


def _check_vomses_file(path: Path, diagnostics: list[VOMSDiagnostic]) -> int:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        _io_problem(diagnostics, "vomses_file", path, exc)
        return 0
    except UnicodeError:
        _problem(
            diagnostics,
            "vomses_corrupt",
            f"VOMS endpoint file '{path}' is not valid UTF-8 text. "
            "Reinstall the VO's official vomses configuration.",
            str(path),
        )
        return 0
    endpoints = 0
    for number, line in enumerate(lines, 1):
        try:
            endpoints += int(_vomses_line(line))
        except ValueError:
            _problem(
                diagnostics,
                "vomses_malformed",
                f"VOMS endpoint file '{path}', line {number}, is malformed. Expected "
                'five fields: "VO" "HOST" "PORT" "SERVER DN" "ALIAS", with a port from '
                "1 to 65535. Reinstall the VO's official vomses configuration.",
                str(path),
            )
    return endpoints


def _vomses_line(line: str) -> bool:
    fields = shlex.split(line, comments=True)
    if not fields:
        return False
    if (
        len(fields) != 5
        or not all(_endpoint_text(field) for field in fields)
        or not _safe_vo(fields[0])
    ):
        raise ValueError("invalid fields")
    if not 1 <= int(fields[2]) <= 65535:
        raise ValueError("invalid port")
    return True


def _endpoint_text(value: str) -> bool:
    return bool(value.strip()) and not any(ord(char) < 32 or ord(char) == 127 for char in value)
