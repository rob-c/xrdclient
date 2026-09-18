"""GSI: bucket framing, Diffie-Hellman, and one whole simulated handshake.

The interesting test is :func:`test_a_server_can_complete_the_handshake` —
it plays the far end, so the session key, the proof of possession and the
chain are all checked the way ``XrdSecgsi`` would check them.
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
    BUCKET_NONE,
    BUCKET_PUK,
    BUCKET_RTAG,
    BUCKET_SIGNED_RTAG,
    BUCKET_VERSION,
    BUCKET_X509,
    CLIENT_OPTS_DEFAULT,
    STEP_CLIENT_CERT,
    STEP_CLIENT_CERTREQ,
    STEP_SERVER_CERT,
    STEP_SERVER_PXYREQ,
    VERSION_UNSIGNED_DH,
    Bucket,
    GSICredential,
    build_cert_response,
    build_certreq,
    decode_message,
    encode_message,
    encode_public_blob,
    find_bucket,
    parse_dh_parameters,
    parse_peer_blob,
    session_key,
)
from xrdclient.config import Config
from xrdclient.crypto import cbc_decrypt, load_proxy
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


def test_signed_dh_is_refused_by_name(proxy, key):
    """``kXRS_cipher`` instead of ``kXRS_puk`` means the signed path."""
    challenge = server_challenge(bucket=BUCKET_CIPHER)
    with pytest.raises(CredentialError, match="signed-DH"):
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


def test_delegation_is_refused_by_name(proxy):
    with pytest.raises(CredentialError, match="delegation"):
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
