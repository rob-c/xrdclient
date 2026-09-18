"""A DER *writer*, so the X.509 and GSI tests mint their own material.

:mod:`xrdclient.crypto.der` deliberately only reads. The tests need bytes to read,
and fixture files checked into a repository expire, drift, and are impossible
to review. So this builds certificates in process: a few dozen lines of
encoder, and every test gets a chain that is valid *today*.

Nothing here is imported by the library.
"""

from __future__ import annotations

import base64
import calendar
import textwrap
import time

from xrdclient.crypto.rsa import RSAPrivateKey, RSAPublicKey

#: The RFC 2409 first Oakley group. A real group, small enough that a pure
#: Python ``pow`` over it is instant.
DH_PRIME = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A63A3620FFFFFFFFFFFFFFFF",
    16,
)
DH_GENERATOR = 2

SHA256_WITH_RSA_OID = "1.2.840.113549.1.1.11"
RSA_ENCRYPTION_OID = "1.2.840.113549.1.1.1"
PROXY_CERT_INFO_OID = "1.3.6.1.5.5.7.1.14"


def _length(count: int) -> bytes:
    if count < 0x80:
        return bytes([count])
    body = count.to_bytes((count.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def tlv(tag: int, value: bytes) -> bytes:
    """One tag-length-value triple."""
    return bytes([tag]) + _length(len(value)) + value


def integer(value: int) -> bytes:
    width = (value.bit_length() + 8) // 8 or 1
    return tlv(0x02, value.to_bytes(width, "big", signed=True))


def oid(dotted: str) -> bytes:
    arcs = [int(part) for part in dotted.split(".")]
    body = bytearray([40 * arcs[0] + arcs[1]])
    for arc in arcs[2:]:
        chunk = [arc & 0x7F]
        arc >>= 7
        while arc:
            chunk.append(0x80 | (arc & 0x7F))
            arc >>= 7
        body += bytes(reversed(chunk))
    return tlv(0x06, bytes(body))


def sequence(*parts: bytes) -> bytes:
    return tlv(0x30, b"".join(parts))


def setof(*parts: bytes) -> bytes:
    return tlv(0x31, b"".join(parts))


def bitstring(data: bytes) -> bytes:
    return tlv(0x03, b"\x00" + data)


def utf8(text: str) -> bytes:
    return tlv(0x0C, text.encode("utf-8"))


def utctime(when: float) -> bytes:
    return tlv(0x17, time.strftime("%y%m%d%H%M%SZ", time.gmtime(when)).encode("ascii"))


def name(*pairs: tuple[str, str]) -> bytes:
    """A distinguished name from ``(OID, value)`` pairs, in order."""
    return sequence(*(setof(sequence(oid(key), utf8(value))) for key, value in pairs))


def public_key_info(key: RSAPublicKey) -> bytes:
    inner = sequence(integer(key.n), integer(key.e))
    return sequence(sequence(oid(RSA_ENCRYPTION_OID), tlv(0x05, b"")), bitstring(inner))


def pem(label: str, der: bytes) -> bytes:
    body = "\n".join(textwrap.wrap(base64.b64encode(der).decode("ascii"), 64))
    return f"-----BEGIN {label}-----\n{body}\n-----END {label}-----\n".encode("ascii")


def make_certificate(
    subject: bytes,
    issuer: bytes,
    subject_key: RSAPublicKey,
    signer: RSAPrivateKey,
    *,
    serial: int = 1,
    not_before: float | None = None,
    not_after: float | None = None,
    extensions: tuple[tuple[str, bytes], ...] = (),
) -> bytes:
    """A signed X.509 v3 certificate, as DER."""
    now = time.time()
    algorithm = sequence(oid(SHA256_WITH_RSA_OID), tlv(0x05, b""))
    encoded = [
        tlv(0xA3, sequence(*(sequence(oid(key), tlv(0x04, body)) for key, body in extensions)))
    ]
    tbs = sequence(
        tlv(0xA0, integer(2)),
        integer(serial),
        algorithm,
        issuer,
        sequence(
            utctime(now - 3600 if not_before is None else not_before),
            utctime(now + 43200 if not_after is None else not_after),
        ),
        subject,
        public_key_info(subject_key),
        *(encoded if extensions else []),
    )
    return sequence(tbs, algorithm, bitstring(signer.sign(tbs, digest="sha256")))


#: ``ProxyCertInfo`` with an unlimited path length and the ``inheritAll``
#: policy — what ``grid-proxy-init`` writes.
PROXY_CERT_INFO = sequence(sequence(oid("1.3.6.1.5.5.7.21.1")))


def proxy_chain(
    key: RSAPrivateKey,
    *,
    identity: tuple[tuple[str, str], ...] = (
        ("0.9.2342.19200300.100.1.25", "org"),
        ("0.9.2342.19200300.100.1.25", "example"),
        ("2.5.4.3", "Jane Doe"),
    ),
    proxy_cn: str = "1234567890",
    not_after: float | None = None,
) -> bytes:
    """A three-certificate proxy file: proxy, key, user certificate, CA.

    The proxy shares ``key`` with nothing else; the user and CA certificates
    carry their own so the chain is structurally what a real one is. Returns
    the combined PEM that ``$X509_USER_PROXY`` points at.
    """
    ca_key = _cached_key(1)
    user_key = _cached_key(2)
    ca_name = name(("0.9.2342.19200300.100.1.25", "org"), ("2.5.4.3", "Example CA"))
    user_name = name(*identity)
    proxy_name = name(*identity, ("2.5.4.3", proxy_cn))

    ca = make_certificate(ca_name, ca_name, ca_key.public, ca_key, serial=1)
    user = make_certificate(user_name, ca_name, user_key.public, ca_key, serial=2)
    proxy = make_certificate(
        proxy_name,
        user_name,
        key.public,
        user_key,
        serial=3,
        not_after=not_after,
        extensions=((PROXY_CERT_INFO_OID, PROXY_CERT_INFO),),
    )
    return (
        pem("CERTIFICATE", proxy)
        + private_key_pem(key)
        + pem("CERTIFICATE", user)
        + pem("CERTIFICATE", ca)
    )


def private_key_pem(key: RSAPrivateKey) -> bytes:
    """PKCS#1 ``RSA PRIVATE KEY``, the form a proxy file carries."""
    d_p = key.d % (key.p - 1)
    d_q = key.d % (key.q - 1)
    coefficient = pow(key.q, -1, key.p)
    der = sequence(
        integer(0),
        integer(key.n),
        integer(key.e),
        integer(key.d),
        integer(key.p),
        integer(key.q),
        integer(d_p),
        integer(d_q),
        integer(coefficient),
    )
    return pem("RSA PRIVATE KEY", der)


def pkcs8_pem(key: RSAPrivateKey) -> bytes:
    """The same key wrapped as unencrypted PKCS#8 ``PRIVATE KEY``."""
    inner = private_key_pem(key)
    der = base64.b64decode(b"".join(inner.splitlines()[1:-1]))
    return pem(
        "PRIVATE KEY",
        sequence(
            integer(0),
            sequence(oid(RSA_ENCRYPTION_OID), tlv(0x05, b"")),
            tlv(0x04, der),
        ),
    )


def dh_parameters_pem() -> bytes:
    """``DH PARAMETERS`` over the Oakley group, as a server would send them."""
    return pem("DH PARAMETERS", sequence(integer(DH_PRIME), integer(DH_GENERATOR)))


_KEYS: dict[int, RSAPrivateKey] = {}


def _cached_key(slot: int) -> RSAPrivateKey:
    """One of the frozen keys from :mod:`_keys`, parsed once per process.

    They are 2048-bit because ``ssl`` refuses anything smaller, and frozen
    because a pure-Python prime search of that size costs seconds. See that
    module for why publishing them is fine.
    """
    if slot not in _KEYS:
        from _keys import KEYS
        from xrdclient.crypto.rsa import load_private_key

        _KEYS[slot] = load_private_key(KEYS[slot % len(KEYS)])
    return _KEYS[slot]


def throwaway_key(slot: int = 0) -> RSAPrivateKey:
    """A throwaway 512-bit key; the same one for the same ``slot``."""
    return _cached_key(slot)


def timestamp(text: str) -> float:
    """``"20260731120000"`` as a UNIX timestamp, in UTC."""
    return float(calendar.timegm(time.strptime(text, "%Y%m%d%H%M%S")))
