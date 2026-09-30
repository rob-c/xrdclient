"""X.509 delegation: the request a GSI server sends, and the proxy signed for it.

Also the pieces delegation added underneath: a DER writer, RSA's block-wise
"encrypt with the private key" that signed Diffie-Hellman uses, the raw
certificate structure a signer copies from, and the check that a server is
who it says it is before anything is signed for it.

Where ``openssl`` is installed, the proxies made here are also handed to
``openssl verify -allow_proxy_certs``, which knows nothing of this code.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

from _pki import (
    PROXY_CERT_INFO,
    PROXY_CERT_INFO_OID,
    bitstring,
    extension,
    integer,
    make_certificate_with,
    make_request,
    name,
    oid,
    pem,
    public_key_info,
    sequence,
    setof,
    throwaway_key,
    tlv,
    utctime,
)
from xrdclient.crypto import der, rsa, x509
from xrdclient.crypto.delegation import (
    DelegationError,
    load_proxy_request,
    proxy_path_length,
    sign_proxy_request,
)
from xrdclient.crypto.trust import (
    DEFAULT_CA_PATH,
    MAX_DEPTH,
    TrustError,
    anchors,
    names_host,
    verify_server,
)
from xrdclient.crypto.x509 import Extension, load_certificates

DC = "0.9.2342.19200300.100.1.25"
CN = "2.5.4.3"
O = "2.5.4.10"
KEY_USAGE = extension("2.5.29.15", bytes.fromhex("030205a0"), critical=True)
BASIC = extension("2.5.29.19", sequence(), critical=True)
SKI = extension("2.5.29.14", tlv(0x04, b"\x01" * 20))
AKI = extension("2.5.29.35", sequence(tlv(0x80, b"\x02" * 20)))
PCI = extension(PROXY_CERT_INFO_OID, PROXY_CERT_INFO, critical=True)

USER = ((O, "example"), (CN, "Jane Doe"))
CA = ((O, "example"), (CN, "Example CA"))


@pytest.fixture(scope="module")
def keys():
    return throwaway_key(0), throwaway_key(1), throwaway_key(2)


def _cert(der_bytes: bytes) -> x509.Certificate:
    return load_certificates(pem("CERTIFICATE", der_bytes))[0]


def signer_proxy(keys, *extensions: bytes, not_after: float | None = None) -> x509.Certificate:
    """The client's proxy: ``Jane Doe/CN=proxy``, signed by her user key."""
    proxy_key, _ca, user_key = keys
    chosen = extensions or (BASIC, KEY_USAGE, SKI, AKI, PCI)
    return _cert(
        make_certificate_with(
            name(*USER, (CN, "proxy")),
            name(*USER),
            proxy_key.public,
            user_key,
            chosen,
            serial=4,
            not_after=not_after,
        )
    )


def request_for(keys, cn: str = "12345", **kwargs) -> bytes:
    _proxy, request_key, _user = keys
    kwargs.setdefault("extensions", (PCI,))
    return make_request(name(*USER, (CN, "proxy"), (CN, cn)), request_key, **kwargs)


# -- the DER writer -----------------------------------------------------------


@pytest.mark.parametrize("value", [0, 1, 127, 128, 255, 256, -1, -129, 1 << 2048])
def test_integers_round_trip_through_the_writer(value):
    element, _ = der.parse(der.encode_integer(value))
    assert der.read_integer(element) == value
    assert der.encode_integer(value) == integer(value)


def test_oids_round_trip_and_match_the_test_encoder():
    for dotted in ("1.2.840.113549.1.1.11", "2.5.29.15", "1.3.6.1.5.5.7.1.14", "2.5.4.3"):
        assert der.encode_oid(dotted) == oid(dotted)
        assert der.oid_string(der.parse(der.encode_oid(dotted))[0]) == dotted


def test_an_oid_needs_two_arcs():
    with pytest.raises(der.DERError, match="two arcs"):
        der.encode_oid("1")


def test_long_lengths_are_minimal():
    assert der.encode(0x04, b"x" * 300)[:4] == b"\x04\x82\x01\x2c"
    assert der.sequence(b"\x05\x00") == b"\x30\x02\x05\x00"


def test_times_switch_to_generalized_in_2050():
    assert der.encode_time(1790704254) == utctime(1790704254)
    late = der.parse(der.encode_time(2556144000))[0]  # 2051-01-01
    assert (late.tag, late.value) == (der.TAG_GENERALIZED_TIME, b"20510101000000Z")


def test_raw_children_keep_each_child_as_written():
    body = sequence(integer(1), sequence(integer(2)))
    assert der.raw_children(body) == [integer(1), sequence(integer(2))]
    with pytest.raises(der.DERError, match="primitive"):
        der.raw_children(integer(1))


# -- RSA block operations --------------------------------------------------------


def test_private_encryption_is_undone_by_the_public_key(keys):
    key = keys[0]
    message = os.urandom(3 * (key.size - 11) + 5)  # four blocks, the last short
    sealed = key.encrypt_private(message)
    assert len(sealed) == 4 * key.size
    assert key.public.decrypt_public(sealed) == message


def test_recover_refuses_what_is_not_a_signature(keys):
    key = keys[0]
    with pytest.raises(ValueError, match="not PKCS#1"):
        keys[1].public.recover(key.encrypt_private(b"tag"))
    with pytest.raises(ValueError, match="bytes, key is"):
        key.public.recover(b"\x00" * 10)
    with pytest.raises(ValueError, match="whole number"):
        key.public.decrypt_public(b"\x00" * (key.size + 1))
    with pytest.raises(ValueError, match="whole number"):
        key.public.decrypt_public(b"")
    with pytest.raises(ValueError, match="nothing"):
        key.encrypt_private(b"")


def test_the_public_key_exports_as_openssl_writes_it(keys, tmp_path):
    public = keys[0].public
    assert public.der() == public_key_info(public)
    assert rsa.load_public_key(public.pem()) == public
    text = public.pem().decode()
    assert text.startswith("-----BEGIN PUBLIC KEY-----\n") and text.endswith(
        "-----END PUBLIC KEY-----\n"
    )
    assert all(len(line) <= 64 for line in text.splitlines())
    if shutil.which("openssl") is None:
        return
    source = tmp_path / "key.pem"
    source.write_bytes(public.pem())
    written = subprocess.run(
        ["openssl", "pkey", "-pubin", "-in", str(source), "-pubout"],
        capture_output=True,
        check=True,
    ).stdout
    assert written == public.pem()


# -- the raw certificate ----------------------------------------------------------


def test_certificate_fields_are_the_bytes_as_written(keys):
    signer = signer_proxy(keys)
    fields = x509.certificate_fields(signer.der)
    assert fields["subject"] == name(*USER, (CN, "proxy"))
    assert fields["issuer"] == name(*USER)
    assert fields["spki"] == public_key_info(keys[0].public)
    assert x509.decode_name(fields["subject"]) == signer.subject


def test_a_version_one_certificate_has_no_version_field_and_no_extensions(keys):
    algorithm = sequence(oid("1.2.840.113549.1.1.11"), tlv(0x05, b""))
    tbs = sequence(
        integer(9),
        algorithm,
        name(*CA),
        sequence(utctime(time.time() - 60), utctime(time.time() + 60)),
        name(*CA),
        public_key_info(keys[1].public),
    )
    certificate = sequence(tbs, algorithm, bitstring(keys[1].sign(tbs, digest="sha256")))
    assert "extensions" not in x509.certificate_fields(certificate)
    assert x509.extensions_of(certificate) == []
    assert x509.verify_signed(certificate, keys[1].public)


def test_a_certificate_body_that_stops_short_is_refused():
    with pytest.raises(der.DERError, match="missing"):
        x509.certificate_fields(sequence(sequence(integer(1)), sequence(), bitstring(b"")))


def test_extensions_are_read_with_their_criticality(keys):
    found = x509.extensions_of(signer_proxy(keys).der)
    assert [e.oid for e in found] == [
        "2.5.29.19",
        "2.5.29.15",
        "2.5.29.14",
        "2.5.29.35",
        PROXY_CERT_INFO_OID,
    ]
    assert [e.critical for e in found] == [True, True, False, False, True]
    assert found[1] == Extension("2.5.29.15", True, bytes.fromhex("030205a0"), KEY_USAGE)


@pytest.mark.parametrize(
    "raw",
    [
        sequence(oid("2.5.29.15")),
        sequence(integer(1), tlv(0x04, b"")),
        sequence(oid("1.2"), integer(1)),
    ],
)
def test_a_malformed_extension_is_refused(raw):
    with pytest.raises(der.DERError, match="malformed"):
        x509.parse_extension(raw)


def test_signatures_are_checked_over_the_body_as_written(keys):
    signer = signer_proxy(keys)
    assert x509.verify_signed(signer.der, keys[2].public)
    assert not x509.verify_signed(signer.der, keys[1].public)
    assert not x509.verify_signed(b"\x30\x00", keys[2].public)
    tampered = signer.der[:-1] + bytes([signer.der[-1] ^ 1])
    assert not x509.verify_signed(tampered, keys[2].public)


def test_an_unknown_algorithm_or_a_padded_bit_string_is_not_a_valid_signature(keys):
    body = sequence(integer(1))
    md5 = sequence(oid("1.2.840.113549.1.1.4"), tlv(0x05, b""))
    good = sequence(oid("1.2.840.113549.1.1.11"), tlv(0x05, b""))
    signature = keys[0].sign(body, digest="sha256")
    assert not x509.verify_signed(sequence(body, md5, bitstring(signature)), keys[0].public)
    assert not x509.verify_signed(
        sequence(body, good, tlv(0x03, b"\x01" + signature)), keys[0].public
    )
    assert not x509.verify_signed(sequence(body, good, tlv(0x04, signature)), keys[0].public)
    with pytest.raises(der.DERError, match="unsupported"):
        x509.signature_digest("1.2.840.113549.1.1.4")
    assert x509.signature_digest("1.2.840.113549.1.1.5") == "sha1"


# -- the proxy request --------------------------------------------------------------


def test_a_request_is_read_from_pem_or_der(keys):
    raw = request_for(keys)
    for form in (raw, pem("CERTIFICATE REQUEST", raw), pem("CERTIFICATE REQUEST", raw).decode()):
        request = load_proxy_request(form)
        assert str(request.subject) == "/O=example/CN=Jane Doe/CN=proxy/CN=12345"
        assert request.serial == 12345
        assert request.public_key == keys[1].public
        assert [e.oid for e in request.extensions] == [PROXY_CERT_INFO_OID]
        assert request.verify()


def test_a_negative_cn_is_the_unsigned_serial_xrdcrypto_meant(keys):
    """The server prints an unsigned serial with ``%d``; it can come out negative."""
    assert load_proxy_request(request_for(keys, "-2")).serial == (1 << 32) - 2


@pytest.mark.parametrize("cn", ["proxy", "12a", ""])
def test_a_request_not_ending_in_a_serial_is_refused(keys, cn):
    with pytest.raises(DelegationError, match="CN=<serial>"):
        _ = load_proxy_request(request_for(keys, cn)).serial


def test_a_request_whose_last_rdn_is_not_a_cn_is_refused(keys):
    raw = make_request(name(*USER, (O, "123")), keys[1])
    with pytest.raises(DelegationError, match="CN=<serial>"):
        _ = load_proxy_request(raw).serial
    with pytest.raises(DelegationError, match="CN=<serial>"):
        _ = load_proxy_request(make_request(name(), keys[1])).serial


def test_requests_without_extensions_or_with_other_attributes_read_as_none(keys):
    assert load_proxy_request(request_for(keys, extensions=())).extensions == ()
    challenge = sequence(oid("1.2.840.113549.1.9.7"), setof(tlv(0x0C, b"pw")))
    odd = load_proxy_request(request_for(keys, attributes=challenge))
    assert odd.extensions == ()


def test_a_forged_request_does_not_verify(keys):
    assert not load_proxy_request(request_for(keys, signer=keys[0])).verify()


@pytest.mark.parametrize(
    "raw",
    [
        b"garbage",
        sequence(sequence(integer(0)), sequence(), bitstring(b"")),
        sequence(sequence(tlv(0x0C, b"v"), sequence(), sequence()), sequence(), bitstring(b"")),
        sequence(
            sequence(integer(0), sequence(), sequence(sequence(), integer(1))),
            sequence(),
            bitstring(b""),
        ),
    ],
)
def test_an_unreadable_request_is_refused(raw):
    with pytest.raises(DelegationError, match="unreadable"):
        load_proxy_request(raw)


def test_path_lengths_are_read_from_both_layouts():
    def ext(body: bytes) -> Extension:
        return Extension(PROXY_CERT_INFO_OID, True, body)

    policy = sequence(oid("1.3.6.1.5.5.7.21.1"))
    assert proxy_path_length(ext(sequence(policy))) is None
    assert proxy_path_length(ext(sequence(integer(3), policy))) == 3
    assert proxy_path_length(ext(sequence(policy, tlv(0xA1, integer(2))))) == 2


# -- signing it -------------------------------------------------------------------


def test_the_delegated_proxy_follows_the_xrdcrypto_profile(keys):
    signer = signer_proxy(keys)
    moment = time.time()
    issued = sign_proxy_request(load_proxy_request(request_for(keys)), signer, keys[0], now=moment)

    assert issued.serial == 12345
    assert issued.subject.rdns == (*signer.subject.rdns, ("CN", "12345"))
    assert issued.issuer == signer.subject
    assert issued.public_key == keys[1].public
    assert issued.not_before == float(int(moment))
    assert issued.not_after == signer.not_after  # capped by the signer, never beyond
    assert issued.is_proxy
    assert x509.verify_signed(issued.der, keys[0].public)


def test_the_delegated_proxy_carries_the_signers_extensions_and_its_own(keys):
    issued = sign_proxy_request(load_proxy_request(request_for(keys)), signer_proxy(keys), keys[0])
    found = x509.extensions_of(issued.der)
    assert [(e.oid, e.critical) for e in found] == [
        ("2.5.29.19", True),
        ("2.5.29.15", True),
        (PROXY_CERT_INFO_OID, True),
    ]
    assert found[0].der == BASIC and found[1].der == KEY_USAGE  # copied as written
    assert proxy_path_length(found[2]) is None
    body = der.parse(found[2].value)[0].children()
    assert der.oid_string(body[0].children()[0]) == "1.3.6.1.5.5.7.21.1"  # inheritAll
    algorithm = der.parse(der.raw_children(issued.der)[1])[0].children()[0]
    assert der.oid_string(algorithm) == "1.2.840.113549.1.1.11"


def test_a_path_length_constraint_is_carried_one_step_down(keys):
    constrained = extension(
        PROXY_CERT_INFO_OID,
        sequence(integer(3), sequence(oid("1.3.6.1.5.5.7.21.1"))),
        critical=True,
    )
    signer = signer_proxy(keys, KEY_USAGE, constrained)
    issued = sign_proxy_request(load_proxy_request(request_for(keys)), signer, keys[0])
    assert proxy_path_length(x509.extensions_of(issued.der)[-1]) == 2

    legacy = extension(
        "1.3.6.1.4.1.3536.1.222",
        sequence(sequence(oid("1.3.6.1.5.5.7.21.1")), tlv(0xA1, integer(0))),
        critical=True,
    )
    with pytest.raises(DelegationError, match="path length"):
        sign_proxy_request(
            load_proxy_request(request_for(keys)), signer_proxy(keys, KEY_USAGE, legacy), keys[0]
        )


@pytest.mark.parametrize(
    ("extensions", "message"),
    [
        ((KEY_USAGE, extension("2.5.29.17", sequence(tlv(0x82, b"h")))), "subjectAltName"),
        ((BASIC, PCI), "keyUsage"),
    ],
)
def test_a_signer_xrdcrypto_would_not_use_is_refused(keys, extensions, message):
    with pytest.raises(DelegationError, match=message):
        sign_proxy_request(
            load_proxy_request(request_for(keys)), signer_proxy(keys, *extensions), keys[0]
        )


def test_requests_that_must_not_be_signed_are_refused(keys):
    signer = signer_proxy(keys)
    wrong_subject = make_request(name(*USER, (CN, "12345")), keys[1], extensions=(PCI,))
    cases = [
        (request_for(keys), signer_proxy(keys, not_after=time.time() - 1), keys[0], "expired"),
        (request_for(keys), signer, keys[1], "does not belong"),
        (wrong_subject, signer, keys[0], "not /O=example"),
        (request_for(keys, signer=keys[0]), signer, keys[0], "not signed by the key"),
        (request_for(keys, extensions=()), signer, keys[0], "no extensions"),
    ]
    for raw, who, key, message in cases:
        with pytest.raises(DelegationError, match=message):
            sign_proxy_request(load_proxy_request(raw), who, key)


def _openssl_verifies_proxies() -> bool:
    if shutil.which("openssl") is None:
        return False
    # macOS ships LibreSSL as ``openssl``, whose ``verify`` has no -allow_proxy_certs.
    done = subprocess.run(
        ["openssl", "verify", "-help"], capture_output=True, text=True, check=False
    )
    return "-allow_proxy_certs" in done.stdout + done.stderr


@pytest.mark.skipif(not _openssl_verifies_proxies(), reason="no openssl -allow_proxy_certs here")
def test_openssl_accepts_the_delegated_chain(keys, tmp_path):
    """The whole chain - CA, user, proxy, delegated proxy - by OpenSSL's rules."""
    proxy_key, ca_key, user_key = keys
    ca = make_certificate_with(
        name(*CA),
        name(*CA),
        ca_key.public,
        ca_key,
        (extension("2.5.29.19", sequence(tlv(0x01, b"\xff")), critical=True),),
    )
    user = make_certificate_with(name(*USER), name(*CA), user_key.public, ca_key, (KEY_USAGE,))
    signer = signer_proxy(keys)
    issued = sign_proxy_request(load_proxy_request(request_for(keys)), signer, proxy_key)
    (tmp_path / "ca.pem").write_bytes(pem("CERTIFICATE", ca))
    (tmp_path / "chain.pem").write_bytes(signer.pem() + pem("CERTIFICATE", user))
    (tmp_path / "leaf.pem").write_bytes(issued.pem())
    done = subprocess.run(
        [
            "openssl",
            "verify",
            "-allow_proxy_certs",
            "-CAfile",
            str(tmp_path / "ca.pem"),
            "-untrusted",
            str(tmp_path / "chain.pem"),
            str(tmp_path / "leaf.pem"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


# -- is the server who it says it is ------------------------------------------------


def server_certificate(keys, subject, *extensions, issuer=CA, signer=None, **times):
    return make_certificate_with(
        name(*subject), name(*issuer), keys[0].public, signer or keys[1], extensions, **times
    )


@pytest.fixture
def ca_dir(tmp_path, keys):
    directory = tmp_path / "certificates"
    directory.mkdir()
    ca = make_certificate_with(name(*CA), name(*CA), keys[1].public, keys[1], ())
    (directory / "0a1b2c3d.0").write_bytes(pem("CERTIFICATE", ca))
    (directory / "0a1b2c3d.signing_policy").write_text("not a certificate")
    (directory / "README").write_text("nor this")
    (directory / "deadbeef.1").mkdir()  # a name that fits, but will not open
    return directory


def test_a_ca_issued_certificate_for_the_host_is_trusted(keys, ca_dir):
    chain = pem("CERTIFICATE", server_certificate(keys, ((O, "example"), (CN, "srv.example.org"))))
    assert str(verify_server(chain, "srv.example.org", str(ca_dir)).subject).endswith(
        "srv.example.org"
    )
    assert len(anchors(str(ca_dir))) == 1


def test_an_intermediate_ca_in_the_directory_is_followed(keys, ca_dir):
    middle = ((O, "example"), (CN, "Middle CA"))
    intermediate = make_certificate_with(name(*middle), name(*CA), keys[2].public, keys[1], ())
    (ca_dir / "11223344.0").write_bytes(pem("CERTIFICATE", intermediate))
    leaf = server_certificate(keys, ((CN, "host/srv"),), issuer=middle, signer=keys[2])
    verify_server(pem("CERTIFICATE", leaf), "srv", str(ca_dir))


def test_a_certificate_for_another_host_or_from_nowhere_is_not(keys, ca_dir, tmp_path):
    chain = pem("CERTIFICATE", server_certificate(keys, ((CN, "srv"),)))
    with pytest.raises(TrustError, match="not for other"):
        verify_server(chain, "other", str(ca_dir))
    with pytest.raises(TrustError, match="no certificate"):
        verify_server(b"", "srv", str(ca_dir))
    stranger = server_certificate(keys, ((CN, "srv"),), signer=keys[2])
    with pytest.raises(TrustError, match="no CA in the certificate directory"):
        verify_server(pem("CERTIFICATE", stranger), "srv", str(ca_dir))
    with pytest.raises(TrustError, match="cannot read the CA directory"):
        verify_server(chain, "srv", str(tmp_path / "absent"))


def test_expired_links_are_not_trusted(keys, ca_dir, tmp_path):
    old = server_certificate(keys, ((CN, "srv"),), not_after=time.time() - 10)
    with pytest.raises(TrustError, match="validity"):
        verify_server(pem("CERTIFICATE", old), "srv", str(ca_dir))
    stale = tmp_path / "stale"
    stale.mkdir()
    ca = make_certificate_with(
        name(*CA), name(*CA), keys[1].public, keys[1], (), not_after=time.time() - 10
    )
    (stale / "0a1b2c3d.0").write_bytes(pem("CERTIFICATE", ca))
    fresh = pem("CERTIFICATE", server_certificate(keys, ((CN, "srv"),)))
    with pytest.raises(TrustError, match="Example CA is outside"):
        verify_server(fresh, "srv", str(stale))


def test_a_path_that_never_reaches_an_anchor_gives_up(keys, tmp_path):
    first, second = ((CN, "CA one"),), ((CN, "CA two"),)
    loop = tmp_path / "loop"
    loop.mkdir()
    (loop / "00000001.0").write_bytes(
        pem(
            "CERTIFICATE",
            make_certificate_with(name(*first), name(*second), keys[1].public, keys[2], ()),
        )
    )
    (loop / "00000002.0").write_bytes(
        pem(
            "CERTIFICATE",
            make_certificate_with(name(*second), name(*first), keys[2].public, keys[1], ()),
        )
    )
    leaf = server_certificate(keys, ((CN, "srv"),), issuer=first, signer=keys[1])
    with pytest.raises(TrustError, match=f"longer than {MAX_DEPTH}"):
        verify_server(pem("CERTIFICATE", leaf), "srv", str(loop))


def test_the_ca_directory_falls_back_to_the_environment_then_the_grid_default(monkeypatch, ca_dir):
    monkeypatch.setenv("X509_CERT_DIR", str(ca_dir))
    assert len(anchors(None)) == 1
    monkeypatch.delenv("X509_CERT_DIR")
    monkeypatch.setattr(os, "listdir", _refuse)
    with pytest.raises(TrustError, match=DEFAULT_CA_PATH):
        anchors(None)


def _refuse(path):
    raise FileNotFoundError(path)


@pytest.mark.parametrize(
    ("names", "host", "expected"),
    [
        ((), "srv.example.org", False),
        ((tlv(0x82, b"srv.example.org"),), "SRV.example.org.", True),
        ((tlv(0x82, b"*.example.org"),), "srv.example.org", True),
        ((tlv(0x82, b"*.example.org"),), "a.srv.example.org", False),
        ((tlv(0x82, b"*.example.org"),), ".example.org", False),
        ((tlv(0x87, bytes([127, 0, 0, 1])),), "127.0.0.1", True),
        ((tlv(0x87, bytes(16)),), "[::]", True),
        ((tlv(0x87, bytes([127, 0, 0, 1])),), "localhost", False),
        ((tlv(0x81, b"srv@example.org"),), "srv.example.org", False),
    ],
)
def test_host_names_are_matched_the_way_xrdsecgsi_matches_them(keys, names, host, expected):
    extensions = (extension("2.5.29.17", sequence(*names)),) if names else (KEY_USAGE,)
    certificate = _cert(server_certificate(keys, ((CN, "Some Service"),), *extensions))
    assert names_host(certificate, host) is expected


def test_fields_past_the_expected_ones_are_passed_over(keys):
    """A request's stray trailing field; a certificate's ``issuerUniqueID``."""
    info = sequence(
        integer(0), name(*USER, (CN, "7")), public_key_info(keys[1].public), tlv(0x81, b"\x00")
    )
    algorithm = sequence(oid("1.2.840.113549.1.1.11"), tlv(0x05, b""))
    raw = sequence(info, algorithm, bitstring(keys[1].sign(info, digest="sha256")))
    assert load_proxy_request(raw).extensions == ()

    tbs = sequence(
        tlv(0xA0, integer(2)),
        integer(1),
        algorithm,
        name(*CA),
        sequence(utctime(time.time() - 60), utctime(time.time() + 60)),
        name(*CA),
        public_key_info(keys[1].public),
        tlv(0x81, b"\x00"),
        tlv(0xA3, sequence(KEY_USAGE)),
    )
    certificate = sequence(tbs, algorithm, bitstring(keys[1].sign(tbs, digest="sha256")))
    assert [e.oid for e in x509.extensions_of(certificate)] == ["2.5.29.15"]
