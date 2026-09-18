"""RSA: key loading, and the two signature shapes GSI and RFC 8017 want."""

import pytest

from _pki import (
    integer,
    oid,
    pem,
    pkcs8_pem,
    private_key_pem,
    public_key_info,
    sequence,
    throwaway_key,
    tlv,
)
from xrdclient.crypto import (
    RSAPrivateKey,
    RSAPublicKey,
    load_private_key,
    load_public_key,
    pem_blocks,
)
from xrdclient.crypto.der import DERError, parse
from xrdclient.crypto.rsa import RSA_OID, _pkcs1_v15_pad, _probably_prime, public_key_from_bitstring


@pytest.fixture(scope="module")
def key():
    return throwaway_key(0)


def test_pem_blocks_splits_a_combined_file():
    data = pem("CERTIFICATE", b"\x30\x00") + pem("RSA PRIVATE KEY", b"\x30\x01\x02")
    assert [label for label, _ in pem_blocks(data)] == ["CERTIFICATE", "RSA PRIVATE KEY"]
    assert pem_blocks(data)[1][1] == b"\x30\x01\x02"


def test_pem_blocks_accepts_text_as_well_as_bytes():
    text = pem("CERTIFICATE", b"\x30\x00").decode()
    assert pem_blocks(text) == pem_blocks(text.encode())


def test_a_corrupt_block_does_not_lose_the_good_ones():
    """A proxy file with one unreadable member must still yield the rest."""
    broken = b"-----BEGIN CERTIFICATE-----\n!!!not base64!!!\n-----END CERTIFICATE-----\n"
    data = broken + pem("RSA PRIVATE KEY", b"\x30\x01\x02")
    assert [label for label, _ in pem_blocks(data)] == ["RSA PRIVATE KEY"]


def test_unterminated_and_mismatched_blocks_are_dropped():
    assert pem_blocks("-----BEGIN CERTIFICATE-----\nAAAA\n") == []
    assert pem_blocks("-----BEGIN CERTIFICATE-----\nAAAA\n-----END OTHER-----\n") == []


def test_pkcs1_and_pkcs8_load_to_the_same_key(key):
    assert load_private_key(private_key_pem(key)) == key
    loaded = load_private_key(pkcs8_pem(key))
    assert (loaded.n, loaded.e, loaded.d) == (key.n, key.e, key.d)


def test_raw_der_is_accepted_without_pem_armour(key):
    import base64

    der = base64.b64decode(b"".join(private_key_pem(key).splitlines()[1:-1]))
    assert load_private_key(der) == key


def test_a_label_selects_between_blocks(key):
    other = throwaway_key(1)
    data = pem("PRIVATE KEY", b"\x30\x00") + private_key_pem(key)
    assert load_private_key(data, "RSA PRIVATE KEY") == key
    assert other != key  # the fixture keys really are distinct


def test_an_encrypted_key_is_refused_by_name():
    """The fix is to decrypt it, so say that rather than 'parse error'."""
    with pytest.raises(DERError, match="encrypted"):
        load_private_key(pem("ENCRYPTED PRIVATE KEY", b"\x30\x00"))


def test_a_file_with_no_key_is_refused():
    with pytest.raises(DERError, match="no RSA private key"):
        load_private_key(pem("CERTIFICATE", b"\x30\x00"))


def test_a_non_rsa_pkcs8_key_names_its_algorithm():
    from _pki import integer, oid, sequence, tlv

    der = sequence(integer(0), sequence(oid("1.2.840.10045.2.1")), tlv(0x04, b"\x30\x00"))
    with pytest.raises(DERError, match="not RSA"):
        load_private_key(pem("PRIVATE KEY", der))


def test_a_truncated_pkcs1_body_is_refused():
    from _pki import integer, sequence

    with pytest.raises(DERError, match="at least 6"):
        load_private_key(pem("RSA PRIVATE KEY", sequence(integer(0), integer(1))))


def test_multi_prime_keys_are_refused():
    from _pki import integer, sequence

    body = sequence(*(integer(value) for value in (1, 2, 3, 4, 5, 6)))
    with pytest.raises(DERError, match="multi-prime"):
        load_private_key(pem("RSA PRIVATE KEY", body))


def test_public_keys_load_from_subject_public_key_info(key):
    loaded = load_public_key(pem("PUBLIC KEY", public_key_info(key.public)))
    assert loaded == key.public
    assert loaded.size == key.size == 256  # 2048 bits


def test_public_keys_load_from_a_bare_pkcs1_body(key):
    from _pki import integer, sequence

    body = sequence(integer(key.n), integer(key.e))
    assert load_public_key(pem("RSA PUBLIC KEY", body)) == key.public


def test_a_non_rsa_public_key_is_refused():
    from _pki import bitstring, oid, sequence, tlv

    der = sequence(sequence(oid("1.2.840.10045.2.1"), tlv(0x05, b"")), bitstring(b"\x30\x00"))
    with pytest.raises(DERError, match="not RSA"):
        load_public_key(pem("PUBLIC KEY", der))
    with pytest.raises(DERError, match="not an RSA public key"):
        load_public_key(pem("PUBLIC KEY", sequence(b"")))


def test_raw_signatures_are_what_gsi_verifies(key):
    """``digest=None`` puts the message straight inside the padding."""
    tag = b"\x01\x02\x03\x04\x05\x06\x07\x08"
    signature = key.sign(tag)
    assert len(signature) == key.size
    assert key.public.verify(tag, signature)
    assert not key.public.verify(b"\x00" * 8, signature)


@pytest.mark.parametrize("digest", ["sha1", "sha256", "sha384", "sha512"])
def test_digest_signatures_follow_rfc_8017(key, digest):
    signature = key.sign(b"payload", digest=digest)
    assert key.public.verify(b"payload", signature, digest=digest)
    assert not key.public.verify(b"payload", signature)  # the wrapper matters


def test_the_padding_block_has_the_documented_shape():
    block = _pkcs1_v15_pad(b"\x01\x02", 64, None)
    assert block[:2] == b"\x00\x01"
    assert block[-3:] == b"\x00\x01\x02"
    assert set(block[2:-3]) == {0xFF}
    assert len(block) == 64


def test_an_oversized_message_is_refused(key):
    with pytest.raises(ValueError, match="does not fit"):
        key.sign(b"x" * 256)
    with pytest.raises(ValueError, match="unsupported digest"):
        key.sign(b"x", digest="md5")


def test_a_wrong_length_signature_is_rejected_not_raised(key):
    assert not key.public.verify(b"tag", b"\x00" * 8)


def test_the_crt_path_agrees_with_the_plain_one(key):
    """``p``/``q`` are an optimisation; dropping them must not change output."""
    slow = RSAPrivateKey(n=key.n, e=key.e, d=key.d)
    assert slow.sign(b"tag") == key.sign(b"tag")
    assert slow.public == key.public


def test_generate_produces_a_usable_key():
    fresh = RSAPrivateKey.generate(512)
    assert fresh.n.bit_length() == 512
    assert fresh.e == 65537
    assert fresh.p * fresh.q == fresh.n
    assert fresh.public.verify(b"tag", fresh.sign(b"tag"))


@pytest.mark.parametrize("bits", [511, 256])
def test_generate_refuses_unusable_sizes(bits):
    with pytest.raises(ValueError, match="at least 512"):
        RSAPrivateKey.generate(bits)


def test_the_primality_test_agrees_with_known_answers():
    assert _probably_prime(2) and _probably_prime(97) and _probably_prime(65537)
    assert not _probably_prime(1) and not _probably_prime(0) and not _probably_prime(-7)
    assert not _probably_prime(97 * 89)
    assert not _probably_prime(561)  # a Carmichael number: fools Fermat, not Miller-Rabin


def test_reprs_do_not_leak_the_private_exponent(key):
    assert repr(key) == "RSAPrivateKey(bits=2048, d=<redacted>)"
    assert str(key.d) not in repr(key)
    assert repr(key.public) == f"RSAPublicKey(bits=2048, e={key.e})"


def test_a_pkcs8_body_with_the_key_before_the_version_is_refused():
    """Field order is not decoration: an OCTET STRING first is not PKCS#8."""
    der = sequence(tlv(0x04, b"\x00"), sequence(oid(RSA_OID)), tlv(0x04, b"\x00"))
    with pytest.raises(DERError, match="version must precede"):
        load_private_key(der)


@pytest.mark.parametrize(
    ("element", "message"),
    [
        (tlv(0x02, b"\x01"), "non-empty BIT STRING"),
        (tlv(0x03, b""), "non-empty BIT STRING"),
        (tlv(0x03, b"\x03" + sequence(integer(3), integer(3))), "3 unused bits"),
        (tlv(0x03, b"\x00" + sequence(integer(3))), "has 1 fields"),
    ],
)
def test_a_subject_public_key_that_is_not_an_rsa_key_is_rejected(element, message):
    """``public_key_from_bitstring`` is reached from certificates too."""
    parsed, _ = parse(element)
    with pytest.raises(DERError, match=message):
        public_key_from_bitstring(parsed)


def test_the_algorithm_oid_is_the_published_one():
    assert RSA_OID == "1.2.840.113549.1.1.1"
    assert RSAPublicKey(n=1 << 1023, e=3).size == 128


def _next_prime(start: int) -> int:
    candidate = start | 1
    while not _probably_prime(candidate):
        candidate += 2
    return candidate


def test_generation_redraws_until_the_pair_is_usable(monkeypatch):
    """Two rejections: ``p == q`` factors ``n`` by ``isqrt``, and a short ``n``
    is not the key size that was asked for."""
    import xrdclient.crypto.rsa as rsa

    real = rsa._prime
    twin = _next_prime(1 << 255)
    small = _next_prime(twin + 2)  # just over the boundary: the product is 511 bits
    scripted = [twin, twin, twin, small]

    def draw(bits, e):
        return scripted.pop(0) if scripted else real(bits, e)

    monkeypatch.setattr(rsa, "_prime", draw)
    key = RSAPrivateKey.generate(512)
    assert not scripted  # both rejected pairs were drawn
    assert key.p != key.q
    assert key.n.bit_length() == 512
