"""Is this GSI server who it says it is? Asked before handing it a proxy.

A GSI login proves the *client* to the server. Delegation reverses the stakes:
the client is about to sign a credential for whoever is at the other end, so
it must first know who that is. This is the check the stock client makes:

* the server's certificate chains, signature by signature, to a CA in the
  certificate directory (``$X509_CERT_DIR``, else
  ``/etc/grid-security/certificates``) - the anchors are the ones on disk,
  never ones that arrived over the wire;
* every certificate on the way is inside its validity period;
* the certificate names the host the client dialled: its ``CN`` is the host
  or ``<service>/<host>``, or a ``subjectAltName`` DNS name (``*.`` matching
  one label) or IP address is.

Revocation lists are not consulted; a GSI server configured with ``-crl:0``
does not consult them either, and CRL freshness is its operator's policy.
"""

from __future__ import annotations

import ipaddress
import os
import time

from .der import parse
from .x509 import Certificate, extensions_of, load_certificates, verify_signed

__all__ = ["TrustError", "DEFAULT_CA_PATH", "verify_server", "names_host", "anchors"]

DEFAULT_CA_PATH = "/etc/grid-security/certificates"
SUBJECT_ALT_NAME_OID = "2.5.29.17"
#: Longest CA path followed before giving up: grid PKIs are two or three deep.
MAX_DEPTH = 8

_DNS_NAME = 0x82
_IP_ADDRESS = 0x87


class TrustError(ValueError):
    """The server's certificate does not establish who the server is."""


def anchors(ca_path: str | None) -> list[Certificate]:
    """Every CA certificate in the directory, from its ``<hash>.<n>`` files.

    The same files OpenSSL and XrdCrypto look CAs up by; signing policies,
    CRLs and namespaces files beside them are passed over.
    """
    directory = ca_path or os.environ.get("X509_CERT_DIR") or DEFAULT_CA_PATH
    try:
        names = sorted(os.listdir(directory))
    except OSError as exc:
        raise TrustError(f"cannot read the CA directory {directory}: {exc}") from exc
    found: list[Certificate] = []
    for name in names:
        stem, dot, index = name.partition(".")
        if not (dot and index.isdigit() and len(stem) == 8):
            continue
        try:
            with open(os.path.join(directory, name), "rb") as handle:
                found.extend(load_certificates(handle.read()))
        except OSError:
            continue
    return found


def names_host(certificate: Certificate, host: str) -> bool:
    """Whether ``certificate`` is for ``host``, the way XrdSecgsi decides."""
    wanted = host.strip("[]").lower().rstrip(".")
    common = certificate.subject.cn.lower()
    if common == wanted or common.endswith("/" + wanted):
        return True
    return any(_alt_name_matches(kind, value, wanted) for kind, value in _alt_names(certificate))


def _alt_names(certificate: Certificate) -> list[tuple[int, bytes]]:
    for extension in extensions_of(certificate.der):
        if extension.oid == SUBJECT_ALT_NAME_OID:
            return [(name.tag, name.value) for name in parse(extension.value)[0].children()]
    return []


def _alt_name_matches(kind: int, value: bytes, wanted: str) -> bool:
    if kind == _IP_ADDRESS:
        try:
            return ipaddress.ip_address(value) == ipaddress.ip_address(wanted)
        except ValueError:
            return False
    if kind != _DNS_NAME:
        return False
    pattern = value.decode("ascii", "replace").lower().rstrip(".")
    if pattern.startswith("*."):
        head, _, tail = wanted.partition(".")
        return bool(head) and tail == pattern[2:]
    return pattern == wanted


def _current(certificate: Certificate, moment: float) -> None:
    if not certificate.not_before <= moment < certificate.not_after:
        raise TrustError(f"{certificate.subject} is outside its validity period")


def _issuer_of(certificate: Certificate, trusted: list[Certificate]) -> Certificate:
    for candidate in trusted:
        key = candidate.public_key
        if candidate.subject == certificate.issuer and key and verify_signed(certificate.der, key):
            return candidate
    raise TrustError(f"no CA in the certificate directory signed {certificate.subject}")


def verify_server(
    chain_pem: bytes,
    host: str,
    ca_path: str | None = None,
    *,
    now: float | None = None,
) -> Certificate:
    """The server's certificate, once it is shown to be a CA-issued one for ``host``.

    Raises :class:`TrustError` saying which check failed.
    """
    moment = time.time() if now is None else now
    certificates = load_certificates(chain_pem)
    if not certificates:
        raise TrustError("the server sent no certificate")
    server = certificates[0]
    if not names_host(server, host):
        raise TrustError(f"the server certificate {server.subject} is not for {host}")
    trusted = anchors(ca_path)
    link = server
    for _ in range(MAX_DEPTH):
        _current(link, moment)
        issuer = _issuer_of(link, trusted)
        if issuer.is_anchor:
            _current(issuer, moment)
            return server
        link = issuer
    raise TrustError(f"the CA path from {server.subject} is longer than {MAX_DEPTH}")
