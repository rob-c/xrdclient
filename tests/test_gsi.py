"""GSI: bucket framing, Diffie-Hellman, and whole simulated handshakes.

The interesting tests play the far end — :func:`test_a_server_can_complete_the_handshake`
for the unsigned exchange, :func:`test_a_signed_exchange_ends_in_a_delegated_proxy`
for the signed one and delegation — so the session key, the proof of
possession, the chain and the delegated proxy are all checked the way
``XrdSecgsi`` would check them. The real server has the last word in
``test_gsi_delegation_interop.py``.
"""

import dataclasses
import struct
import time

import pytest

from _pki import DH_GENERATOR, DH_PRIME, dh_parameters_pem, pem, proxy_chain, throwaway_key
from xrdclient.auth import gsi, registry, select
from xrdclient.auth.base import Offer
from xrdclient.auth.gsi import (
    BUCKET_CIPHER,
    BUCKET_CIPHER_ALG,
    BUCKET_CLNT_OPTS,
    BUCKET_CRYPTOMOD,
    BUCKET_ISSUER_HASH,
    BUCKET_MAIN,
    BUCKET_MD_ALG,
    BUCKET_MESSAGE,
    BUCKET_NONE,
    BUCKET_PUK,
    BUCKET_RTAG,
    BUCKET_SIGNED_RTAG,
    BUCKET_VERSION,
    BUCKET_X509,
    BUCKET_X509_REQ,
    CLIENT_OPTS_DEFAULT,
    REFUSAL,
    STEP_CLIENT_CERT,
    STEP_CLIENT_CERTREQ,
    STEP_CLIENT_SIGPXY,
    STEP_SERVER_CERT,
    STEP_SERVER_PXYREQ,
    VERSION_SIGNED_DH,
    VERSION_UNSIGNED_DH,
    Bucket,
    Delegation,
    GSICredential,
    Session,
    answer_certificate,
    answer_proxy_request,
    build_cert_response,
    build_certreq,
    decode_message,
    encode_message,
    encode_public_blob,
    find_bucket,
    parse_dh_parameters,
    parse_peer_blob,
    seal,
    session_key,
    unseal,
)
from xrdclient.config import Config
from xrdclient.crypto import cbc_decrypt, load_certificates, load_proxy
from xrdclient.errors import CredentialError

OFFER = Offer("gsi", "v:10400,c:ssl,ca:1a2b3c4d.0")


@pytest.fixture(scope="module")
def key():
    return throwaway_key(0)


@pytest.fixture
def proxy(tmp_path_factory, key):
    path = tmp_path_factory.mktemp("gsi") / "x509up_u1000"
    path.write_bytes(proxy_chain(key))
    return load_proxy(str(path))


# -- framing ----------------------------------------------------------------


def test_a_message_round_trips_through_the_bucket_encoding():
    buckets = [Bucket(BUCKET_CRYPTOMOD, b"ssl"), Bucket(BUCKET_RTAG, b"\x01" * 8)]
    encoded = encode_message(STEP_CLIENT_CERTREQ, buckets)
    assert encoded.startswith(b"gsi\x00")
    step, decoded = decode_message(encoded)
    assert step == STEP_CLIENT_CERTREQ
    assert decoded == buckets


def test_the_encoding_is_the_wire_format_byte_for_byte():
    """Pinned against XrdSut: name, step, then type/length/value, then zero."""
    encoded = encode_message(1000, [Bucket(3000, b"ssl")])
    assert encoded == b"gsi\x00" + struct.pack(">III", 1000, 3000, 3) + b"ssl" + struct.pack(
        ">I", BUCKET_NONE
    )


def test_an_empty_message_is_a_name_a_step_and_a_terminator():
    assert decode_message(encode_message(2000, [])) == (2000, [])


def test_find_bucket_picks_out_one_type():
    encoded = encode_message(1, [Bucket(7, b"a"), Bucket(9, b"b")])
    assert find_bucket(encoded, 9) == b"b"
    assert find_bucket(encoded, 11) is None
    assert find_bucket(b"not a gsi message at all", 7) is None


def test_buckets_after_the_terminator_are_ignored():
    """The zero type ends the list; XrdSut pads after it."""
    encoded = encode_message(1, [Bucket(7, b"a")]) + struct.pack(">II", 9, 1) + b"b"
    assert decode_message(encoded)[1] == [Bucket(7, b"a")]


@pytest.mark.parametrize(
    "data, message",
    [
        (b"gsi", "no protocol name"),
        (b"gsi\x00\x00\x00", "too short for a step code"),
        (b"gsi\x00" + struct.pack(">II", 1, 3000) + b"\x00\x00", "truncated length"),
        (b"gsi\x00" + struct.pack(">III", 1, 3000, 99) + b"ab", "99 bytes, 2 available"),
    ],
)
def test_malformed_messages_are_refused(data, message):
    with pytest.raises(CredentialError, match=message):
        decode_message(data)


def test_bucket_repr_does_not_dump_its_payload():
    assert repr(Bucket(3022, b"x" * 4096)) == "Bucket(type=3022, len=4096)"


# -- Diffie-Hellman ---------------------------------------------------------


def test_dh_parameters_are_read_from_pem():
    assert parse_dh_parameters(dh_parameters_pem()) == (DH_PRIME, DH_GENERATOR)


def test_unreadable_dh_parameters_are_refused():
    with pytest.raises(CredentialError, match="no PEM block"):
        parse_dh_parameters(b"nothing here")
    with pytest.raises(CredentialError, match="unreadable DH parameters"):
        parse_dh_parameters(pem("DH PARAMETERS", b"\x30\x03\x02\x01\x05"))


def test_a_public_blob_round_trips():
    public = pow(DH_GENERATOR, 12345, DH_PRIME)
    peer = parse_peer_blob(encode_public_blob(dh_parameters_pem(), public))
    assert (peer.p, peer.g, peer.public) == (DH_PRIME, DH_GENERATOR, public)
    assert peer.params_pem == dh_parameters_pem()


def test_the_closing_delimiter_is_matched_on_nine_bytes():
    """The reference encoder drops the last dash; parsing must tolerate both."""
    blob = dh_parameters_pem() + b"---BPUB---" + b"02" + b"---EPUB--"
    assert parse_peer_blob(blob).public == 2


@pytest.mark.parametrize(
    "blob, message",
    [
        (b"no delimiters", "malformed"),
        (b"---BPUB------EPUB---", "malformed"),
        (dh_parameters_pem() + b"---BPUB---zz---EPUB---", "not hexadecimal"),
    ],
)
def test_a_malformed_public_blob_is_refused(blob, message):
    with pytest.raises(CredentialError, match=message):
        parse_peer_blob(blob)


def test_both_sides_agree_on_the_session_key():
    """The whole point of the exchange; asymmetry here is silent failure."""
    ours, theirs = 0x1234567, 0x89ABCDEF
    params = dh_parameters_pem()
    mine = parse_peer_blob(encode_public_blob(params, pow(DH_GENERATOR, theirs, DH_PRIME)))
    yours = parse_peer_blob(encode_public_blob(params, pow(DH_GENERATOR, ours, DH_PRIME)))
    assert session_key(mine, ours) == session_key(yours, theirs)
    assert len(session_key(mine, ours)) == 16


def test_a_too_small_shared_secret_is_refused():
    tiny = parse_peer_blob(pem("DH PARAMETERS", _small_group()) + b"---BPUB---02---EPUB---")
    with pytest.raises(CredentialError, match="need 16"):
        session_key(tiny, 3)


def _small_group():
    from _pki import integer, sequence

    return sequence(integer(23), integer(5))


# -- the first client message ----------------------------------------------


def test_the_certreq_carries_what_the_server_reads():
    message = build_certreq(cryptomod="ssl", issuer_hash="1a2b3c4d.0", rtag=b"\x07" * 8)
    step, buckets = decode_message(message)
    by_type = {bucket.type: bucket.data for bucket in buckets}
    assert step == STEP_CLIENT_CERTREQ
    assert by_type[BUCKET_CRYPTOMOD] == b"ssl"
    assert by_type[BUCKET_VERSION] == struct.pack(">I", VERSION_UNSIGNED_DH)
    assert by_type[BUCKET_ISSUER_HASH] == b"1a2b3c4d.0"
    assert by_type[BUCKET_CLNT_OPTS] == struct.pack(">I", CLIENT_OPTS_DEFAULT)
    assert find_bucket(by_type[BUCKET_MAIN], BUCKET_RTAG) == b"\x07" * 8


def test_the_advertised_version_selects_unsigned_dh():
    """At or above 10400 the server would choose signed DH, which we cannot do."""
    assert VERSION_UNSIGNED_DH < 10400


def test_an_empty_cryptomod_falls_back_to_ssl():
    assert find_bucket(build_certreq(cryptomod="", rtag=b"x"), BUCKET_CRYPTOMOD) == b"ssl"


# -- the handshake ----------------------------------------------------------


def server_challenge(private=(1 << 250) | 99, *, tag=b"\xa5" * 8, bucket=BUCKET_PUK):
    """What ``kXGS_cert`` looks like coming back from XrdSecgsi."""
    blob = encode_public_blob(dh_parameters_pem(), pow(DH_GENERATOR, private, DH_PRIME))
    inner = encode_message(STEP_SERVER_CERT, [Bucket(BUCKET_RTAG, tag)])
    return encode_message(
        STEP_SERVER_CERT,
        [Bucket(bucket, blob), Bucket(BUCKET_MAIN, inner), Bucket(BUCKET_X509, b"server chain")],
    )


def test_a_server_can_complete_the_handshake(proxy, key):
    """Play the far end: agree the key, decrypt, and verify the signature."""
    private, tag = (1 << 250) | 99, b"\xa5" * 8
    response = build_cert_response(server_challenge(private, tag=tag), proxy.pem(), key)

    step, buckets = decode_message(response)
    by_type = {bucket.type: bucket.data for bucket in buckets}
    _assert_outer_response(step, by_type)

    client_public = parse_peer_blob(by_type[BUCKET_PUK])
    assert client_public.p == DH_PRIME  # the group is the server's, echoed back
    secret = session_key(client_public, private)
    _assert_inner_response(cbc_decrypt(secret, by_type[BUCKET_MAIN]), proxy, tag)


def _assert_outer_response(step, by_type):
    assert step == STEP_CLIENT_CERT
    assert by_type[BUCKET_CIPHER_ALG] == b"aes-128-cbc"
    assert by_type[BUCKET_MD_ALG] == b"sha256"


def _assert_inner_response(plain, proxy, tag):
    inner_step, inner = decode_message(plain)
    inner_by_type = {bucket.type: bucket.data for bucket in inner}
    assert inner_step == STEP_CLIENT_CERT
    assert inner_by_type[BUCKET_X509] == proxy.pem()
    assert len(inner_by_type[BUCKET_RTAG]) == 8

    signature = inner_by_type[BUCKET_SIGNED_RTAG]
    assert proxy.certificate.public_key.verify(tag, signature)
    assert not proxy.certificate.public_key.verify(b"\x00" * 8, signature)


def test_the_response_is_deterministic_when_its_randomness_is_given(proxy, key):
    """Injectable ``private``/``rtag`` are what makes the encoding pinnable."""
    challenge = server_challenge()
    fixed = {"private": (1 << 251) | 7, "rtag": b"\x01" * 8}
    first = build_cert_response(challenge, proxy.pem(), key, **fixed)
    second = build_cert_response(challenge, proxy.pem(), key, **fixed)
    assert first == second
    other = build_cert_response(challenge, proxy.pem(), key, **{**fixed, "private": (1 << 251) | 8})
    assert other != first


def test_a_server_without_a_proof_request_gets_no_signature(proxy, key):
    """No ``kXRS_rtag`` means nothing to prove; sending a signature anyway is noise."""
    theirs = (1 << 255) | 0x1234567
    challenge = encode_message(
        STEP_SERVER_CERT,
        [
            Bucket(
                BUCKET_PUK,
                encode_public_blob(dh_parameters_pem(), pow(DH_GENERATOR, theirs, DH_PRIME)),
            )
        ],
    )
    response = build_cert_response(challenge, proxy.pem(), key, private=(1 << 254) | 7)
    secret = session_key(parse_peer_blob(find_bucket(response, BUCKET_PUK)), theirs)
    plain = cbc_decrypt(secret, find_bucket(response, BUCKET_MAIN))
    assert find_bucket(plain, BUCKET_SIGNED_RTAG) is None
    assert find_bucket(plain, BUCKET_X509) == proxy.pem()


def test_signed_dh_needs_the_servers_certificate(proxy, key):
    """``kXRS_cipher`` instead of ``kXRS_puk`` means the signed path."""
    challenge = server_challenge(bucket=BUCKET_CIPHER)
    with pytest.raises(CredentialError, match="sent no RSA certificate"):
        build_cert_response(challenge, proxy.pem(), key)


def test_a_challenge_with_no_public_key_is_refused(proxy, key):
    with pytest.raises(CredentialError, match="no DH public key"):
        build_cert_response(encode_message(STEP_SERVER_CERT, []), proxy.pem(), key)


# -- the mechanism ----------------------------------------------------------


def test_gsi_is_registered_with_no_extra_installed():
    """It is pure Python, so it is always there."""
    assert registry()["gsi"] is GSICredential


def test_the_credential_drives_both_rounds(proxy, key):
    credential = GSICredential(proxy, issuer_hash="1a2b3c4d.0")
    first = credential.initial()
    assert find_bucket(first, BUCKET_ISSUER_HASH) == b"1a2b3c4d.0"
    second = credential.step(server_challenge())
    assert decode_message(second)[0] == STEP_CLIENT_CERT


def test_the_identity_is_the_human_behind_the_proxy(proxy):
    credential = GSICredential(proxy)
    assert credential.identity == "/DC=org/DC=example/CN=Jane Doe"
    assert repr(credential) == "GSICredential(identity='/DC=org/DC=example/CN=Jane Doe')"


def test_a_proxy_request_before_the_key_exchange_is_a_protocol_error(proxy):
    with pytest.raises(CredentialError, match="before the key exchange"):
        GSICredential(proxy).step(encode_message(STEP_SERVER_PXYREQ, []))


def test_an_unexpected_step_is_named_not_guessed(proxy):
    with pytest.raises(CredentialError, match="unexpected GSI step 2999"):
        GSICredential(proxy).step(encode_message(2999, []))


def test_an_expired_proxy_is_reported_before_the_round_trip(tmp_path, key):
    path = tmp_path / "old.pem"
    path.write_bytes(proxy_chain(key, not_after=time.time() - 7200))
    with pytest.raises(CredentialError, match="expired"):
        GSICredential(load_proxy(str(path))).initial()


def test_available_finds_the_proxy_the_environment_points_at(monkeypatch, tmp_path, key):
    path = tmp_path / "x509up_u1000"
    path.write_bytes(proxy_chain(key))
    monkeypatch.setenv("X509_USER_PROXY", str(path))
    credential = GSICredential.available(OFFER, Config(), username="jane", host="srv")
    assert isinstance(credential, GSICredential)
    assert credential.cryptomod == "ssl"
    assert credential.issuer_hash == "1a2b3c4d.0"  # taken from the offer's ca:


def test_available_returns_none_when_there_is_nothing_to_use(monkeypatch, tmp_path, key):
    monkeypatch.setenv("X509_USER_PROXY", str(tmp_path / "absent.pem"))
    assert GSICredential.available(OFFER, Config(), username="j", host="h") is None

    junk = tmp_path / "junk.pem"
    junk.write_text("not a proxy")
    monkeypatch.setenv("X509_USER_PROXY", str(junk))
    assert GSICredential.available(OFFER, Config(), username="j", host="h") is None

    stale = tmp_path / "stale.pem"
    stale.write_bytes(proxy_chain(key, not_after=time.time() - 60))
    monkeypatch.setenv("X509_USER_PROXY", str(stale))
    assert GSICredential.available(OFFER, Config(), username="j", host="h") is None


def test_the_ladder_prefers_gsi_when_a_proxy_exists(monkeypatch, tmp_path, key):
    path = tmp_path / "x509up_u1000"
    path.write_bytes(proxy_chain(key))
    monkeypatch.setenv("X509_USER_PROXY", str(path))
    chosen = next(select("&P=gsi,v:10400,c:ssl&P=unix", Config(), username="jane", host="srv"))
    assert isinstance(chosen, GSICredential)


def test_a_message_that_simply_stops_is_read_to_its_end():
    """XrdSut always writes the terminator; a truncated one still decodes."""
    encoded = encode_message(1, [Bucket(7, b"a")])[: -struct.calcsize(">I")]
    assert decode_message(encoded) == (1, [Bucket(7, b"a")])


def test_a_proxy_whose_key_is_not_rsa_is_refused_at_the_second_round(proxy):
    """``load_proxy`` types the key loosely, so ``step`` checks it itself."""
    borrowed = dataclasses.replace(proxy, key=object())
    with pytest.raises(CredentialError, match="not RSA"):
        GSICredential(borrowed).step(encode_message(STEP_SERVER_CERT, []))


def test_available_passes_over_a_proxy_whose_key_is_not_rsa(monkeypatch, tmp_path, key, proxy):
    path = tmp_path / "x509up_u1000"
    path.write_bytes(proxy_chain(key))
    monkeypatch.setenv("X509_USER_PROXY", str(path))
    monkeypatch.setattr(gsi, "load_proxy", lambda _: dataclasses.replace(proxy, key=object()))
    assert GSICredential.available(OFFER, Config(), username="j", host="h") is None


# -- signed Diffie-Hellman and delegation ------------------------------------


SERVER_NAME = (("2.5.4.10", "example"), ("2.5.4.3", "srv.example.org"))
CA_NAME = (("2.5.4.10", "example"), ("2.5.4.3", "Example CA"))


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    """A CA directory, and a server certificate it issued: key 2, signed by key 1."""
    from _pki import make_certificate_with, name

    ca_key, server_key = throwaway_key(1), throwaway_key(2)
    directory = tmp_path_factory.mktemp("certificates")
    ca = make_certificate_with(name(*CA_NAME), name(*CA_NAME), ca_key.public, ca_key, ())
    (directory / "0a1b2c3d.0").write_bytes(pem("CERTIFICATE", ca))
    server = make_certificate_with(
        name(*SERVER_NAME), name(*CA_NAME), server_key.public, ca_key, ()
    )
    return {"ca_path": str(directory), "server_pem": pem("CERTIFICATE", server), "key": server_key}


@pytest.fixture
def delegating_proxy(tmp_path, key):
    path = tmp_path / "x509up_dlg"
    path.write_bytes(proxy_chain(key, key_usage=True))
    return load_proxy(str(path))


def signed_challenge(pki, private=(1 << 250) | 99, *, tag=b"abcdEFGH", certificate=None):
    """``kXGS_cert`` from a server at 10400 or later: its DH blob, signed."""
    blob = encode_public_blob(dh_parameters_pem(), pow(DH_GENERATOR, private, DH_PRIME))
    inner = encode_message(STEP_SERVER_CERT, [Bucket(BUCKET_RTAG, tag)])
    return encode_message(
        STEP_SERVER_CERT,
        [
            Bucket(BUCKET_CIPHER, pki["key"].encrypt_private(blob)),
            Bucket(BUCKET_X509, pki["server_pem"] if certificate is None else certificate),
            Bucket(BUCKET_CIPHER_ALG, b"aes-128-cbc:bf-cbc"),
            Bucket(BUCKET_MD_ALG, b"sha256:sha1"),
            Bucket(BUCKET_MAIN, inner),
        ],
    )


def server_reads_certificate(response, private, proxy):
    """What ``ServerDoCert`` does with a signed ``kXGC_cert``: the session key and main."""
    step, buckets = decode_message(response)
    by_type = {bucket.type: bucket.data for bucket in buckets}
    assert step == STEP_CLIENT_CERT
    assert by_type[BUCKET_PUK] == proxy.certificate.public_key.pem()
    assert by_type[BUCKET_CIPHER_ALG] == b"aes-128-cbc#16"
    client_blob = proxy.certificate.public_key.decrypt_public(by_type[BUCKET_CIPHER])
    secret = session_key(parse_peer_blob(client_blob), private, padded=True)
    return secret, unseal(secret, by_type[BUCKET_MAIN], use_iv=True)


def proxy_request_message(secret, request_der, *, tag=b"ijklMNOP", use_iv=True):
    from _pki import pem as to_pem

    inner = [Bucket(BUCKET_RTAG, tag)]
    if request_der is not None:
        inner.append(Bucket(BUCKET_X509_REQ, to_pem("CERTIFICATE REQUEST", request_der)))
    sealed = seal(secret, encode_message(STEP_SERVER_PXYREQ, inner), use_iv=use_iv)
    return encode_message(STEP_SERVER_PXYREQ, [Bucket(BUCKET_MAIN, sealed)])


def server_reads_sigpxy(response, secret, proxy, tag=b"ijklMNOP", *, use_iv=True):
    step, _buckets = decode_message(response)
    assert step == STEP_CLIENT_SIGPXY
    plain = unseal(secret, find_bucket(response, BUCKET_MAIN), use_iv=use_iv)
    assert proxy.certificate.public_key.verify(tag, find_bucket(plain, BUCKET_SIGNED_RTAG))
    return plain


def the_request(proxy, cn="4242", **kwargs):
    from _pki import make_request, name

    subject = name(*[(_OIDS[k], v) for k, v in proxy.subject.rdns], ("2.5.4.3", cn))
    return make_request(subject, throwaway_key(2), extensions=(_PCI,), **kwargs)


_OIDS = {"DC": "0.9.2342.19200300.100.1.25", "CN": "2.5.4.3"}


def _pci():
    from _pki import PROXY_CERT_INFO, PROXY_CERT_INFO_OID, extension

    return extension(PROXY_CERT_INFO_OID, PROXY_CERT_INFO, critical=True)


_PCI = _pci()


def test_a_signed_exchange_ends_in_a_delegated_proxy(pki, delegating_proxy, key):
    """Both rounds, played from the server's side, as ``XrdSecgsi`` checks them."""
    offer = Offer("gsi", "v:10600,c:ssl,ca:0a1b2c3d.0")
    credential = GSICredential(
        delegating_proxy,
        cryptomod=offer.options()["c"],
        server_version=10600,
        delegation=Delegation(True, "srv.example.org", pki["ca_path"]),
    )
    first = credential.initial()
    assert struct.unpack(">I", find_bucket(first, BUCKET_VERSION))[0] == VERSION_SIGNED_DH
    assert struct.unpack(">I", find_bucket(first, BUCKET_CLNT_OPTS))[0] == 0x85

    private = (1 << 250) | 12345
    secret, inner = server_reads_certificate(
        credential.step(signed_challenge(pki, private)), private, delegating_proxy
    )
    assert find_bucket(inner, BUCKET_X509) == delegating_proxy.pem()
    assert delegating_proxy.certificate.public_key.verify(
        b"abcdEFGH", find_bucket(inner, BUCKET_SIGNED_RTAG)
    )

    answer = credential.step(proxy_request_message(secret, the_request(delegating_proxy)))
    plain = server_reads_sigpxy(answer, secret, delegating_proxy)
    (issued,) = load_certificates(find_bucket(plain, BUCKET_X509))
    assert issued.issuer == delegating_proxy.subject
    assert issued.subject.rdns == (*delegating_proxy.subject.rdns, ("CN", "4242"))
    assert issued.public_key == throwaway_key(2).public
    assert find_bucket(plain, BUCKET_MESSAGE) is None


def test_an_untrusted_server_is_answered_but_not_given_a_proxy(pki, delegating_proxy, caplog):
    """The stock client's answer: log in, and tell the server no in words."""
    credential = GSICredential(
        delegating_proxy,
        server_version=10400,
        delegation=Delegation(True, "impostor.example.org", pki["ca_path"]),
    )
    credential.initial()
    private = (1 << 250) | 7
    secret, _inner = server_reads_certificate(
        credential.step(signed_challenge(pki, private)), private, delegating_proxy
    )
    assert "not for impostor.example.org" in caplog.text
    plain = server_reads_sigpxy(
        credential.step(proxy_request_message(secret, the_request(delegating_proxy))),
        secret,
        delegating_proxy,
    )
    assert find_bucket(plain, BUCKET_X509) is None
    assert find_bucket(plain, BUCKET_MESSAGE) == REFUSAL.encode()


def test_without_the_option_nothing_is_offered_and_nothing_signed(pki, delegating_proxy):
    credential = GSICredential(delegating_proxy, server_version=10400)
    first = credential.initial()
    assert struct.unpack(">I", find_bucket(first, BUCKET_CLNT_OPTS))[0] == CLIENT_OPTS_DEFAULT
    private = (1 << 250) | 7
    secret, _ = server_reads_certificate(
        credential.step(signed_challenge(pki, private)), private, delegating_proxy
    )
    reply = credential.step(proxy_request_message(secret, the_request(delegating_proxy)))
    plain = server_reads_sigpxy(reply, secret, delegating_proxy)
    assert find_bucket(plain, BUCKET_MESSAGE) == REFUSAL.encode()


def test_an_old_server_gets_the_unsigned_exchange_and_no_offer(proxy, caplog):
    credential = GSICredential(
        proxy, server_version=10300, delegation=Delegation(True, "srv", None)
    )
    first = credential.initial()
    assert struct.unpack(">I", find_bucket(first, BUCKET_VERSION))[0] == VERSION_UNSIGNED_DH
    assert struct.unpack(">I", find_bucket(first, BUCKET_CLNT_OPTS))[0] == CLIENT_OPTS_DEFAULT
    assert "predates the signed exchange" in caplog.text
    assert decode_message(credential.step(server_challenge()))[0] == STEP_CLIENT_CERT


@pytest.mark.parametrize(
    ("offered", "version", "sent", "padded"),
    [
        ("sslnopad", 10400, b"sslnopad", False),
        ("sslnopad", 10300, b"ssl", False),
        ("ssl|gcrypt", 10400, b"ssl", True),
        ("", 10400, b"ssl", True),
        ("|x", 10400, b"ssl", True),
    ],
)
def test_the_crypto_module_says_whether_the_dh_secret_is_padded(
    proxy, offered, version, sent, padded
):
    credential = GSICredential(proxy, cryptomod=offered, server_version=version)
    assert credential.padded is padded
    assert find_bucket(credential.initial(), BUCKET_CRYPTOMOD) == sent


def test_a_padded_secret_keeps_its_leading_zeros():
    theirs = (1 << 200) | 5
    peer = parse_peer_blob(
        encode_public_blob(dh_parameters_pem(), pow(DH_GENERATOR, theirs, DH_PRIME))
    )
    mine = next(
        candidate
        for candidate in range(3, 100000)
        if pow(peer.public, candidate, DH_PRIME).bit_length() <= DH_PRIME.bit_length() - 8
    )
    padded = session_key(peer, mine, padded=True)
    assert padded[0] == 0
    assert padded != session_key(peer, mine)
    assert padded[1:] == session_key(peer, mine)[:15]


def test_a_nopad_server_gets_an_unpadded_secret(pki, proxy):
    private = (1 << 250) | 3
    message, session = answer_certificate(
        signed_challenge(pki, private), proxy.pem(), proxy.key, padded=False
    )
    client = parse_peer_blob(
        proxy.certificate.public_key.decrypt_public(find_bucket(message, BUCKET_CIPHER))
    )
    assert session.key == session_key(client, private)
    assert session.use_iv and session.server_pem == pki["server_pem"]


def test_signed_dh_parameters_must_verify_under_the_servers_key(pki, proxy):
    stranger = pem("CERTIFICATE", _self_signed(throwaway_key(0)))
    with pytest.raises(CredentialError, match="not signed by its certificate"):
        answer_certificate(signed_challenge(pki, certificate=stranger), proxy.pem(), proxy.key)
    with pytest.raises(CredentialError, match="sent no RSA certificate"):
        answer_certificate(signed_challenge(pki, certificate=b"junk"), proxy.pem(), proxy.key)


def _self_signed(signer):
    from _pki import make_certificate_with, name

    subject = name(("2.5.4.3", "someone"))
    return make_certificate_with(subject, subject, signer.public, signer, ())


def test_proxy_requests_that_cannot_be_signed_get_a_reason(pki, proxy):
    """A proxy with no keyUsage, a request missing or garbled: words, not a failed login."""
    session = Session(b"k" * 16, True)
    cases = [
        (the_request(proxy), b"problems signing the request: the signing proxy has no keyUsage"),
        (None, b"bucket with proxy request missing"),
    ]
    for request, reason in cases:
        plain = server_reads_sigpxy(
            answer_proxy_request(
                proxy_request_message(session.key, request), session, proxy, proxy.key,
                allowed=True,
            ),
            session.key,
            proxy,
        )
        assert find_bucket(plain, BUCKET_MESSAGE).startswith(reason)


def test_an_unsigned_session_seals_without_an_iv(delegating_proxy):
    session = Session(b"k" * 16, False)
    message = proxy_request_message(session.key, the_request(delegating_proxy), use_iv=False)
    reply = answer_proxy_request(
        message, session, delegating_proxy, delegating_proxy.key, allowed=True, iv=b"\x00" * 16
    )
    plain = server_reads_sigpxy(reply, session.key, delegating_proxy, use_iv=False)
    assert load_certificates(find_bucket(plain, BUCKET_X509))


def test_a_proxy_request_that_does_not_decrypt_or_has_no_body_is_refused(delegating_proxy):
    session = Session(b"k" * 16, True)
    with pytest.raises(CredentialError, match="no main buffer"):
        answer_proxy_request(
            encode_message(STEP_SERVER_PXYREQ, []), session, delegating_proxy,
            delegating_proxy.key, allowed=True,
        )
    garbled = encode_message(STEP_SERVER_PXYREQ, [Bucket(BUCKET_MAIN, b"\x00" * 40)])
    with pytest.raises(CredentialError, match="cannot decrypt"):
        answer_proxy_request(garbled, session, delegating_proxy, delegating_proxy.key, allowed=True)


def test_sealing_puts_the_iv_first():
    key = b"q" * 16
    sealed = seal(key, b"hello", use_iv=True, iv=b"\x07" * 16)
    assert sealed[:16] == b"\x07" * 16
    assert unseal(key, sealed, use_iv=True) == b"hello"
    assert len(seal(key, b"hello", use_iv=True)) == 32
    assert unseal(key, seal(key, b"hello", use_iv=False), use_iv=False) == b"hello"


def test_available_carries_the_version_and_the_delegation_choice(monkeypatch, tmp_path, key):
    path = tmp_path / "x509up_u1000"
    path.write_bytes(proxy_chain(key))
    monkeypatch.setenv("X509_USER_PROXY", str(path))
    config = Config(gsi_delegate=True, ca_path="/certs")
    credential = GSICredential.available(
        Offer("gsi", "v:10600,c:ssl"), config, username="j", host="srv"
    )
    assert credential.server_version == 10600 and credential.signed
    assert credential.delegation == Delegation(True, "srv", "/certs")
    garbled = GSICredential.available(Offer("gsi", "v:new"), Config(), username="j", host="h")
    assert garbled.server_version == 0 and not garbled.delegation.wanted


@pytest.mark.parametrize(
    ("value", "expected"), [(None, False), ("0", False), ("1", True), ("2", True), ("x", False)]
)
def test_the_stock_environment_variable_turns_delegation_on(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("XrdSecGSIDELEGPROXY", raising=False)
    else:
        monkeypatch.setenv("XrdSecGSIDELEGPROXY", value)
    assert Config().gsi_delegate is expected
