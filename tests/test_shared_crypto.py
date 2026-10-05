"""Contracts of the shared core; runnable without installing xgfalclient."""

from __future__ import annotations

from importlib.metadata import requires
from types import SimpleNamespace

import pytest
from packaging.requirements import Requirement

from _pki import throwaway_key
from xrdclient.crypto import aes, der, rsa, voms, x509
from xrdclient.crypto.der import DERError


@pytest.mark.parametrize("value", [0, 1, 127, 128, 255, 256, -1, -129, 2**70])
def test_der_integer_round_trips(value):
    assert der.read_integer(der.parse_one(der.integer(value))) == value


@pytest.mark.parametrize(
    "dotted", ["1.2.840.113549.1.1.11", "2.5.4.3", "1.3.6.1.4.1.3536.1.1.1.9", "2.999.3"]
)
def test_der_oid_round_trips(dotted):
    assert der.oid_string(der.parse_one(der.oid(dotted))) == dotted


def test_der_primitive_writers():
    long = der.octet_string(b"x" * 300)
    assert der.parse_one(long).value == b"x" * 300
    assert der.parse_one(der.boolean(True)).value == b"\xff"
    assert der.parse_one(der.boolean(False)).value == b"\x00"
    assert der.null() == b"\x05\x00"
    assert der.parse_one(der.printable_string("ab")).tag == der.TAG_PRINTABLE_STRING
    assert der.parse_one(der.utf8_string("é")).value == "é".encode()
    assert der.parse_one(der.bit_string(b"\x01")).value == b"\x00\x01"
    assert der.set_of(b"\x02\x01\x02", b"\x02\x01\x01") == b"\x31\x06\x02\x01\x01\x02\x01\x02"
    assert der.explicit(3, b"") == b"\xa3\x00"


def test_der_constructed_element_round_trip():
    element = der.parse_one(der.sequence(der.integer(1), der.integer(2)))
    assert element.constructed and len(element.children()) == 2
    assert der.read_integer(element[1]) == 2
    assert element.encoded == der.sequence(der.integer(1), der.integer(2))
    assert repr(element) == "Element(tag=0x30, len=6)"


def test_der_times() -> None:
    moment = 1_790_000_000.0
    assert der.decode_time(der.parse_one(der.utc_time(moment))) == moment
    assert der.decode_time(der.parse_one(der.generalized_time(moment))) == moment
    assert der.parse_one(der.validity_time(moment)).tag == der.TAG_UTC_TIME
    far = 2_600_000_000.0  # 2052
    assert der.parse_one(der.validity_time(far)).tag == der.TAG_GENERALIZED_TIME
    assert der.decode_time(der.Element(der.TAG_UTC_TIME, b"991231235959Z")) == 946684799.0
    assert der.decode_time(der.Element(der.TAG_GENERALIZED_TIME, b"20260101")) == 1767225600.0
    with pytest.raises(DERError, match="malformed UTCTime"):
        der.decode_time(der.Element(der.TAG_UTC_TIME, b"99"))
    with pytest.raises(DERError, match="not a certificate time"):
        der.decode_time(der.Element(der.TAG_INTEGER, b"1"))
    with pytest.raises(DERError, match="malformed time"):
        der.decode_time(der.Element(der.TAG_GENERALIZED_TIME, b"2026xx01000000"))


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "no tag byte"),
        (b"\x30", "no length byte"),
        (b"\x30\x80", "indefinite"),
        (b"\x30\x85\x01\x01\x01\x01\x01", "implausible"),
        (b"\x30\x82\x01", "runs past the end"),
        (b"\x30\x05\x01", "5 bytes requested"),
        (b"\x1f\x01\x00", "multi-byte tags"),
    ],
)
def test_der_rejects_malformed(data: bytes, message: str) -> None:
    with pytest.raises(DERError, match=message):
        der.parse(data)


def test_der_strictness() -> None:
    with pytest.raises(DERError, match="trailing"):
        der.parse_one(der.null() + b"\x00")
    with pytest.raises(DERError, match="primitive"):
        der.parse_one(der.integer(1)).children()
    with pytest.raises(DERError, match="expected INTEGER"):
        der.read_integer(der.parse_one(der.null()))
    with pytest.raises(DERError, match="no content"):
        der.read_integer(der.Element(der.TAG_INTEGER, b""))
    with pytest.raises(DERError, match="expected OBJECT IDENTIFIER"):
        der.oid_string(der.parse_one(der.null()))
    with pytest.raises(DERError, match="no content"):
        der.oid_string(der.Element(der.TAG_OID, b""))
    with pytest.raises(DERError, match="mid-arc"):
        der.oid_string(der.Element(der.TAG_OID, b"\x2a\x86"))
    assert der.parse_all(b"") == []


@pytest.mark.parametrize("size", [16, 24, 32])
def test_shared_aes_ctr_preserves_partial_block_state(size):
    key = bytes(range(size))
    iv = bytes(range(16))
    plain = b"a partial block followed by another partial block"
    cipher = aes.CTR(key, iv)
    encrypted = cipher.update(plain[:7]) + cipher.update(memoryview(plain[7:]))
    assert aes.CTR(key, iv).update(bytearray(encrypted)) == plain
    assert aes.AES(key).rounds == size // 4 + 6
    with pytest.raises(ValueError, match="16 bytes"):
        aes.CTR(key, b"short")


def test_shared_private_key_export():
    key = throwaway_key(0)
    assert rsa.load_private_key(rsa.private_key_der(key)).public == key.public
    assert rsa.load_private_key(rsa.private_key_pem(key)).public == key.public
    for incomplete in (rsa.RSAPrivateKey(1, 2, 3), rsa.RSAPrivateKey(1, 2, 3, p=5)):
        with pytest.raises(ValueError, match="needs its primes"):
            rsa.private_key_der(incomplete)


def test_shared_name_encoding_preserves_original_der():
    rdns = (
        ("C", "GB"),
        ("DC", "example"),
        ("emailAddress", "user@example.org"),
        ("CN", "A unicode physicist: é"),
        ("1.2.3", ""),
    )
    encoded = x509.encode_name(rdns)
    decoded = x509.decode_name(encoded)
    assert decoded.rdns == rdns
    assert decoded.encoded() == encoded
    assert x509.Name(rdns).encoded() == encoded
    assert decoded == x509.Name(rdns)
    assert hash(decoded) == hash(x509.Name(rdns))


def test_voms_reads_mapping_extensions_without_reparsing():
    certificate = SimpleNamespace(extensions={voms.VOMS_AC_OID: (False, b"attribute bytes")})
    assert voms._voms_extension(certificate) == b"attribute bytes"
    assert voms._certificate_extension(certificate, "1.2.3") is None
    assert voms._voms_extension(SimpleNamespace(extensions={})) is None


def test_xrdclient_has_no_reverse_dependency():
    dependencies = {Requirement(value).name.lower() for value in requires("xrdclient") or ()}
    assert "xgfalclient" not in dependencies
