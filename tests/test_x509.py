"""X.509 and RFC 3820 proxies. The chains are minted in process by ``_pki``."""

import time

import pytest

from _pki import (
    PROXY_CERT_INFO,
    PROXY_CERT_INFO_OID,
    bitstring,
    integer,
    make_certificate,
    name,
    oid,
    pem,
    private_key_pem,
    proxy_chain,
    public_key_info,
    sequence,
    setof,
    throwaway_key,
    tlv,
    utctime,
    utf8,
)
from xrdclient.crypto import Certificate, Name, ProxyCredential, load_certificates, load_proxy
from xrdclient.crypto.der import DERError, Element
from xrdclient.crypto.x509 import (
    LEGACY_PROXY_OID,
    _decode_time,
    _parse_certificate,
    default_proxy_path,
)

CN = "2.5.4.3"
DC = "0.9.2342.19200300.100.1.25"


@pytest.fixture(scope="module")
def key():
    return throwaway_key(0)


@pytest.fixture
def proxy_file(tmp_path, key):
    path = tmp_path / "x509up_u1000"
    path.write_bytes(proxy_chain(key))
    return path


def test_a_proxy_file_loads_as_a_chain(proxy_file, key):
    proxy = load_proxy(str(proxy_file))
    assert len(proxy.chain) == 3
    assert proxy.key == key
    assert proxy.path == str(proxy_file)
    assert proxy.certificate is proxy.chain[0]


def test_the_subject_reads_like_openssl(proxy_file):
    proxy = load_proxy(str(proxy_file))
    assert str(proxy.subject) == "/DC=org/DC=example/CN=Jane Doe/CN=1234567890"
    assert proxy.subject.cn == "1234567890"
    assert proxy.subject.get("DC") == ["org", "example"]


def test_the_identity_strips_the_proxy_common_names(proxy_file):
    """What a job's log should print: the human, not the delegation."""
    assert load_proxy(str(proxy_file)).identity == "/DC=org/DC=example/CN=Jane Doe"


@pytest.mark.parametrize("cn", ["proxy", "12345", "1"])
def test_every_proxy_naming_convention_is_stripped(tmp_path, key, cn):
    path = tmp_path / "p.pem"
    path.write_bytes(proxy_chain(key, proxy_cn=cn))
    assert load_proxy(str(path)).identity == "/DC=org/DC=example/CN=Jane Doe"


def test_the_proxy_certificate_is_recognised(proxy_file):
    chain = load_proxy(str(proxy_file)).chain
    assert chain[0].is_proxy
    assert PROXY_CERT_INFO_OID in chain[0].extensions
    assert not chain[2].is_proxy  # the self-signed CA


def test_a_legacy_globus_proxy_is_recognised(key):
    """Pre-RFC proxies carry a different OID and are still in the wild."""
    subject = name((DC, "org"), (CN, "Jane"))
    der = make_certificate(
        subject, subject, key.public, key, extensions=((LEGACY_PROXY_OID, PROXY_CERT_INFO),)
    )
    assert load_certificates(pem("CERTIFICATE", der))[0].is_proxy


def test_a_proxy_without_the_extension_is_caught_by_its_name(key):
    """Some issuers omit proxyCertInfo; the appended CN still gives it away."""
    issuer = name((DC, "org"), (CN, "Jane"))
    subject = name((DC, "org"), (CN, "Jane"), (CN, "proxy"))
    der = make_certificate(subject, issuer, key.public, key)
    certificate = load_certificates(pem("CERTIFICATE", der))[0]
    assert certificate.is_proxy and not certificate.extensions


def test_a_ca_issued_certificate_is_not_a_proxy(key):
    der = make_certificate(name((CN, "Jane")), name((CN, "Example CA")), key.public, key)
    assert not load_certificates(pem("CERTIFICATE", der))[0].is_proxy


def test_validity_is_read_and_expiry_is_reported(tmp_path, key):
    fresh = load_proxy(str(_write(tmp_path / "a.pem", proxy_chain(key))))
    assert not fresh.expired
    assert 0 < fresh.remaining() <= 43200

    stale = _write(tmp_path / "b.pem", proxy_chain(key, not_after=time.time() - 7200))
    expired = load_proxy(str(stale))
    assert expired.expired
    assert expired.remaining() < 0
    assert expired.certificate.expired


def test_the_chain_expiry_is_the_earliest_member(tmp_path, key):
    """A proxy is only as good as the shortest-lived certificate above it."""
    soon = time.time() + 600
    proxy = load_proxy(str(_write(tmp_path / "c.pem", proxy_chain(key, not_after=soon))))
    assert proxy.certificate.not_after < min(c.not_after for c in proxy.chain[1:])
    assert proxy.remaining() == pytest.approx(soon - time.time(), abs=2)
    assert not proxy.expired


def test_the_public_key_comes_back(proxy_file, key):
    assert load_proxy(str(proxy_file)).certificate.public_key == key.public


def test_a_certificate_round_trips_through_pem(proxy_file):
    certificate = load_proxy(str(proxy_file)).certificate
    again = load_certificates(certificate.pem())[0]
    assert again == certificate


def test_the_chain_pem_is_what_goes_on_the_wire(proxy_file):
    """GSI echoes the chain verbatim, so concatenation order must hold."""
    proxy = load_proxy(str(proxy_file))
    assert proxy.pem() == b"".join(c.pem() for c in proxy.chain)
    assert load_certificates(proxy.pem()) == list(proxy.chain)


def test_serials_and_issuers_are_decoded(proxy_file):
    chain = load_proxy(str(proxy_file)).chain
    assert [c.serial for c in chain] == [3, 2, 1]
    assert str(chain[0].issuer) == str(chain[1].subject)
    assert str(chain[2].issuer) == str(chain[2].subject)  # the CA is self-signed


def test_an_unparseable_member_does_not_lose_the_chain(tmp_path, key):
    """A proxy file routinely carries material this reader has no view on."""
    junk = pem("CERTIFICATE", b"\x30\x03\x02\x01\x01")
    path = _write(tmp_path / "d.pem", junk + proxy_chain(key))
    assert len(load_proxy(str(path)).chain) == 3


def test_a_file_with_no_certificate_is_refused(tmp_path, key):
    path = _write(tmp_path / "e.pem", private_key_pem(key))
    with pytest.raises(DERError, match="no certificate"):
        load_proxy(str(path))


def test_a_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        load_proxy(str(tmp_path / "absent.pem"))


def test_non_certificate_blocks_are_ignored(key):
    assert load_certificates(private_key_pem(key)) == []
    assert load_certificates("") == []


def test_the_proxy_path_follows_the_grid_convention(monkeypatch, tmp_path):
    import os

    monkeypatch.delenv("X509_USER_PROXY", raising=False)
    assert default_proxy_path() == f"/tmp/x509up_u{os.geteuid()}"
    monkeypatch.setenv("X509_USER_PROXY", str(tmp_path / "mine.pem"))
    assert default_proxy_path() == str(tmp_path / "mine.pem")


def test_an_explicit_config_beats_the_environment(monkeypatch, tmp_path):
    from xrdclient.config import Config

    monkeypatch.setenv("X509_USER_PROXY", "/tmp/from-env")
    assert default_proxy_path(Config(proxy=str(tmp_path / "cfg.pem"))) == str(tmp_path / "cfg.pem")


@pytest.mark.parametrize(
    "tag, text, expected",
    [
        (0x17, "260731120000Z", "2026-07-31 12:00:00"),
        (0x17, "4901011200Z", "2049-01-01 12:00:00"),  # the 50-year pivot, below
        (0x17, "500101120000Z", "1950-01-01 12:00:00"),  # and above
        (0x18, "20260731120000Z", "2026-07-31 12:00:00"),
        (0x18, "20260731120000", "2026-07-31 12:00:00"),  # the Z is conventional, not required
    ],
)
def test_both_time_encodings_are_understood(tag, text, expected):
    when = _decode_time(Element(tag, text.encode()))
    assert time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(when)) == expected


@pytest.mark.parametrize("tag, text", [(0x17, "26Z"), (0x02, "20260731120000Z")])
def test_malformed_times_are_refused(tag, text):
    with pytest.raises(DERError):
        _decode_time(Element(tag, text.encode()))


def test_bmp_strings_in_a_subject_are_decoded():
    """A DirectoryString may be UTF-16BE; a mojibake CN is a support ticket."""
    from _pki import oid, sequence, setof, tlv
    from xrdclient.crypto.der import parse
    from xrdclient.crypto.x509 import _decode_name

    encoded = sequence(setof(sequence(oid(CN), tlv(0x1E, "Jané".encode("utf-16-be")))))
    assert _decode_name(parse(encoded)[0]).cn == "Jané"


def test_an_unknown_attribute_type_falls_back_to_its_oid(key):
    der = make_certificate(name(("1.2.3.4", "x")), name((CN, "CA")), key.public, key)
    assert str(load_certificates(pem("CERTIFICATE", der))[0].subject) == "/1.2.3.4=x"


def test_an_empty_name_is_falsy():
    assert not Name()
    assert Name().cn == ""
    assert str(Name()) == ""
    assert Name((("CN", "a"),))


def test_reprs_do_not_leak_the_key(proxy_file):
    proxy = load_proxy(str(proxy_file))
    text = repr(proxy)
    assert text == (
        "ProxyCredential(subject='/DC=org/DC=example/CN=Jane Doe/CN=1234567890', "
        "key=<redacted>)"
    )
    assert str(proxy.key.d) not in text
    assert "der" not in repr(proxy.certificate)  # 2 kB of DER is not a repr


def test_certificate_str_is_its_subject(proxy_file):
    certificate = load_proxy(str(proxy_file)).certificate
    assert str(certificate) == str(certificate.subject)


def test_the_dataclasses_are_hashable_and_frozen(proxy_file):
    proxy = load_proxy(str(proxy_file))
    assert isinstance(proxy, ProxyCredential)
    assert isinstance(proxy.certificate, Certificate)
    with pytest.raises(AttributeError):
        proxy.certificate.serial = 9  # type: ignore[misc]


def _write(path, data):
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# Certificates that are not what they should be
# ---------------------------------------------------------------------------


def _hand_built(*fields: bytes) -> bytes:
    """A certificate whose body is exactly ``fields``, signature and all."""
    algorithm = sequence(oid("1.2.840.113549.1.1.11"), tlv(0x05, b""))
    return sequence(sequence(*fields), algorithm, bitstring(b"\x00"))


def test_a_body_too_short_to_be_a_certificate_is_refused():
    with pytest.raises(DERError, match="missing required fields"):
        _parse_certificate(_hand_built(tlv(0xA0, integer(2)), integer(1)))


def test_validity_that_is_not_a_pair_of_times_is_refused(key):
    algorithm = sequence(oid("1.2.840.113549.1.1.11"), tlv(0x05, b""))
    subject = name((CN, "broken"))
    with pytest.raises(DERError, match="validity is not a pair"):
        _parse_certificate(
            _hand_built(
                tlv(0xA0, integer(2)),
                integer(1),
                algorithm,
                subject,
                sequence(utctime(time.time())),  # one time, not two
                subject,
                public_key_info(key.public),
            )
        )


def test_a_certificate_whose_key_is_not_rsa_is_still_readable(key):
    """An EC certificate is a name, a validity and a key we cannot use."""
    algorithm = sequence(oid("1.2.840.113549.1.1.11"), tlv(0x05, b""))
    subject = name((CN, "elliptic"))
    ec_key = sequence(sequence(oid("1.2.840.10045.2.1")), bitstring(b"\x04" + b"\x11" * 64))
    der = _hand_built(
        tlv(0xA0, integer(2)),
        integer(7),
        algorithm,
        subject,
        sequence(utctime(time.time() - 60), utctime(time.time() + 60)),
        subject,
        ec_key,
    )
    certificate = _parse_certificate(der)
    assert certificate.public_key is None
    assert certificate.subject.cn == "elliptic"
    assert certificate.serial == 7


def test_an_attribute_that_is_not_a_pair_is_skipped(key):
    """A malformed RDN costs that attribute, not the whole certificate."""
    lopsided = sequence(setof(sequence(oid(CN))), setof(sequence(oid(CN), utf8("real"))))
    der = make_certificate(lopsided, lopsided, key.public, key)
    assert _parse_certificate(der).subject.cn == "real"


def test_a_public_key_field_of_the_wrong_shape_leaves_the_key_out(key):
    """SubjectPublicKeyInfo must be algorithm-then-bits; anything else is not a key."""
    algorithm = sequence(oid("1.2.840.113549.1.1.11"), tlv(0x05, b""))
    subject = name((CN, "shapeless"))
    der = _hand_built(
        tlv(0xA0, integer(2)),
        integer(3),
        algorithm,
        subject,
        sequence(utctime(time.time() - 60), utctime(time.time() + 60)),
        subject,
        sequence(sequence(oid("1.2.840.10045.2.1"))),  # no BIT STRING beside it
    )
    assert _parse_certificate(der).public_key is None


def test_trailing_fields_that_are_not_extensions_are_passed_over(key):
    """``issuerUniqueID`` is ``[1]``; only ``[3]`` holds extensions."""
    algorithm = sequence(oid("1.2.840.113549.1.1.11"), tlv(0x05, b""))
    subject = name((CN, "unique"))
    der = _hand_built(
        tlv(0xA0, integer(2)),
        integer(4),
        algorithm,
        subject,
        sequence(utctime(time.time() - 60), utctime(time.time() + 60)),
        subject,
        public_key_info(key.public),
        tlv(0xA1, b"\x00\x01"),  # [1] issuerUniqueID
        tlv(0xA3, sequence(sequence())),  # [3] with one empty extension
    )
    certificate = _parse_certificate(der)
    assert certificate.extensions == ()
    assert certificate.public_key == key.public
