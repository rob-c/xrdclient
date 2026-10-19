"""Crypto primitives, pinned to published vectors.

These have no second implementation to fall back on, so every one is checked
against a value from the standard that defines it. The CRCs and checksum
algorithms moved to xrdcore in 0.4.0 and are tested there
(``xrdcore/tests/test_crc.py``, ``test_checksum.py``); what is left here is
XRootD's own: the paged-I/O framing, SSS's Blowfish and ``kXR_sigver``. The
CA-directory name hashes moved to xrdsec with the rest of the X.509 code and
are pinned against OpenSSL in ``xrdsec/tests/test_x509.py``.
"""

from __future__ import annotations

import hashlib

import pytest

from xrdclient.crypto.aes import BLOCK_SIZE, cbc_encrypt
from xrdclient.crypto.blowfish import Blowfish
from xrdclient.crypto.checksum import new
from xrdclient.crypto.crc32c import (
    PAGE_SIZE,
    pack_pages,
    page_span,
    unpack_pages,
)
from xrdclient.crypto.sigver import Signer, is_signed, sigver_hash, sigver_sign, sigver_verify
from xrdclient.proto import constants as c
from xrdclient.proto import requests as r
from xrdclient.proto.frames import encode

# --------------------------------------------------------------------------
# Paged I/O framing
# --------------------------------------------------------------------------


def test_pack_pages_prefixes_each_page_with_its_crc():
    data = b"a" * (PAGE_SIZE + 10)
    packed = pack_pages(data, 0)
    assert len(packed) == len(data) + 8
    assert packed[4 : 4 + PAGE_SIZE] == data[:PAGE_SIZE]


def test_pack_unpack_round_trips_on_a_page_boundary():
    data = bytes(range(256)) * 32  # exactly two pages
    back, corrupt = unpack_pages(pack_pages(data, 0), 0)
    assert back == data
    assert corrupt == ()


@pytest.mark.parametrize("size", [0, 1, 4095, 4096, 4097, 9000])
def test_pack_unpack_round_trips_at_every_size(size):
    data = bytes(i % 251 for i in range(size))
    back, corrupt = unpack_pages(pack_pages(data, 0), 0)
    assert back == data and corrupt == ()


def test_an_unaligned_start_offset_makes_a_short_first_page():
    offset = PAGE_SIZE - 10
    data = b"x" * 100
    packed = pack_pages(data, offset)
    back, corrupt = unpack_pages(packed, offset)
    assert back == data and corrupt == ()


def test_page_span_stops_at_the_file_page_boundary():
    assert page_span(0, 10_000) == PAGE_SIZE
    assert page_span(100, 10_000) == PAGE_SIZE - 100  # a short first page
    assert page_span(PAGE_SIZE, 10) == 10  # ...and a short last one
    assert page_span(PAGE_SIZE - 1, 10_000) == 1


def test_a_flipped_bit_is_reported_by_offset():
    data = b"y" * (2 * PAGE_SIZE)
    packed = bytearray(pack_pages(data, 0))
    packed[10] ^= 0xFF
    back, corrupt = unpack_pages(bytes(packed), 0)
    assert corrupt == (0,)
    assert len(back) == len(data)


# --------------------------------------------------------------------------
# Blowfish - CFB64, the one mode SSS uses
# --------------------------------------------------------------------------


def test_blowfish_cfb64_vector():
    """Eric Young's ``bftest.c`` CFB64 vector, the one OpenSSL's tests carry."""
    cipher = Blowfish(bytes.fromhex("0123456789ABCDEFF0E1D2C3B4A59687"))
    iv = bytes.fromhex("FEDCBA9876543210")
    plain = b"7654321 Now is the time for \0"
    expected = "E73214A2822139CAF26ECF6D2EB9E76E3DA3DE04D1517200519D57A6C3"
    assert cipher.encrypt_cfb64(iv, plain).hex().upper() == expected
    assert cipher.decrypt_cfb64(iv, bytes.fromhex(expected)) == plain


@pytest.mark.parametrize(
    ("short", "repeated"), [(b"k", b"kkkk"), (b"ab", b"abab"), (b"xyz", b"xyzxyz")]
)
def test_blowfish_short_keys_are_repeated_to_the_minimum(short, repeated):
    """The key schedule cycles its key, so repeating it changes nothing; OpenSSL wants 4 bytes."""
    assert Blowfish(short).encrypt_cfb64(bytes(8), b"data") == Blowfish(repeated).encrypt_cfb64(
        bytes(8), b"data"
    )


@pytest.mark.parametrize(
    ("key", "message"),
    [(b"", "non-empty"), (b"k" * 57, "<= 56 bytes")],
)
def test_blowfish_refuses_a_key_the_algorithm_cannot_take(key, message):
    """448 bits is the ceiling Blowfish's key schedule is defined for."""
    with pytest.raises(ValueError, match=message):
        Blowfish(key)


def test_blowfish_cfb64_round_trips_at_any_length():
    cipher = Blowfish(b"another key")
    for length in (0, 1, 7, 8, 9, 100):
        data = bytes(range(length))
        iv = bytes(8)
        assert cipher.decrypt_cfb64(iv, cipher.encrypt_cfb64(iv, data)) == data


def test_blowfish_cfb64_is_a_stream_and_does_not_pad():
    cipher = Blowfish(b"k")
    assert len(cipher.encrypt_cfb64(bytes(8), b"abc")) == 3


def test_blowfish_repr_hides_the_key():
    assert repr(Blowfish(b"hunter2")) == "Blowfish(key=<redacted>)"
    assert "hunter2" not in repr(Blowfish(b"hunter2"))


def test_the_cfb64_iv_must_be_eight_bytes():
    with pytest.raises(ValueError, match="IV must be 8 bytes"):
        Blowfish(b"k").encrypt_cfb64(b"short", b"data")


# --------------------------------------------------------------------------
# kXR_sigver
# --------------------------------------------------------------------------


def test_sigver_hash_is_sha256_over_seqno_header_payload():
    header, payload = b"h" * 24, b"body"
    expected = hashlib.sha256((1).to_bytes(8, "big") + header + payload).digest()
    assert sigver_hash(1, header, payload) == expected


def test_sigver_hash_drops_the_payload_when_nodata():
    header = b"h" * 24
    assert sigver_hash(7, header, b"payload", nodata=True) == sigver_hash(7, header, b"")


def test_sigver_sign_is_the_hash_encrypted_under_a_zero_iv():
    key, header, payload = b"k" * 32, b"h" * 24, b"body"
    signature = sigver_sign(key, 1, header, payload)
    assert signature == cbc_encrypt(key, sigver_hash(1, header, payload))
    # PKCS#7 rounds the 32-byte hash up to three blocks.
    assert len(signature) == 48


def test_sigver_sign_prepends_a_given_iv():
    key, header, iv = b"k" * 32, b"h" * 24, bytes(range(16))
    signature = sigver_sign(key, 1, header, b"", iv=iv)
    assert signature[:BLOCK_SIZE] == iv
    assert signature[BLOCK_SIZE:] == cbc_encrypt(key, sigver_hash(1, header, b""), iv)


def test_sigver_verify_accepts_what_sigver_sign_produced():
    key, header, payload = b"k" * 32, b"h" * 24, b"body"
    signature = sigver_sign(key, 3, header, payload)
    assert sigver_verify(key, signature, 3, header, payload) is True


def test_sigver_verify_accepts_an_embedded_iv():
    key, header = b"k" * 32, b"h" * 24
    signature = sigver_sign(key, 3, header, b"x", iv=bytes(range(16)))
    assert sigver_verify(key, signature, 3, header, b"x", embedded_iv=True) is True


def test_sigver_verify_rejects_tampered_material():
    key, header = b"k" * 32, b"h" * 24
    signature = sigver_sign(key, 3, header, b"body")
    assert sigver_verify(key, signature, 3, header, b"different") is False
    assert sigver_verify(key, signature, 4, header, b"body") is False


def test_sigver_verify_rejects_what_will_not_even_decrypt():
    key = b"k" * 32
    assert sigver_verify(key, b"x" * 17, 1, b"h" * 24, b"") is False
    assert sigver_verify(key, b"short", 1, b"h" * 24, b"", embedded_iv=True) is False


@pytest.mark.parametrize("level", [c.kXR_secNone, c.kXR_secCompatible])
def test_nothing_is_signed_below_the_standard_level(level):
    assert is_signed(c.kXR_write, level) is False


def test_mutating_requests_are_signed_at_the_standard_level():
    assert is_signed(c.kXR_write, c.kXR_secStandard) is True
    assert is_signed(c.kXR_read, c.kXR_secStandard) is False
    # A clone writes to the destination without a byte of data on the wire,
    # so nothing but the signature says the request was not tampered with.
    assert is_signed(c.kXR_clone, c.kXR_secStandard) is True


def test_an_override_can_force_or_exempt_one_opcode():
    """The kXR_protocol security block overrides the level, either way."""
    assert is_signed(c.kXR_write, c.kXR_secStandard, {c.kXR_write: c.kXR_secNone}) is False
    assert is_signed(c.kXR_read, c.kXR_secStandard, {c.kXR_read: c.kXR_secStandard}) is True
    # An override for some other opcode changes nothing for this one.
    assert is_signed(c.kXR_write, c.kXR_secStandard, {c.kXR_read: c.kXR_secNone}) is True


def test_a_level_from_the_future_signs_the_standard_set():
    assert is_signed(c.kXR_write, 9) is True
    assert is_signed(c.kXR_read, 9) is False


def test_signer_increments_the_sequence_number():
    signer = Signer(b"k" * 32, c.kXR_secStandard, {})
    first = signer.sign(encode(r.Write(b"H", 0, b"a"), 5))
    second = signer.sign(encode(r.Write(b"H", 0, b"b"), 6))
    assert first is not None and second is not None
    assert second[0] == first[0] + 1


def _signed(signer: Signer, frame: bytes) -> tuple[int, bytes, bool]:
    """``signer``'s signature of ``frame``, which must be one it signs."""
    signed = signer.sign(frame)
    assert signed is not None
    return signed


def test_signer_leaves_unsigned_requests_alone():
    signer = Signer(b"k" * 32, c.kXR_secStandard, {})
    assert signer.sign(encode(r.Read(b"H", 0, 1), 5)) is None


def test_signer_covers_the_header_and_the_payload():
    key = b"k" * 32
    signer = Signer(key, c.kXR_secStandard, {})
    frame = encode(r.Rm("/store/f.root"), 5)
    seqno, signature, nodata = _signed(signer, frame)
    assert nodata is False
    assert signature == sigver_sign(key, seqno, frame[:24], frame[24:])


def test_a_write_travels_outside_its_own_signature():
    """No ``kXR_secOData``: the data of a write is not hashed, and the frame
    says so, so the server hashes the same bytes the client did."""
    key = b"k" * 32
    signer = Signer(key, c.kXR_secStandard, {})
    frame = encode(r.Write(b"HDL0", 0, b"payload"), 5)
    seqno, signature, nodata = _signed(signer, frame)
    assert nodata is True
    assert signature == sigver_sign(key, seqno, frame[:24], b"", nodata=True)


def test_secodata_pulls_the_payload_back_into_the_signature():
    key = b"k" * 32
    signer = Signer(key, c.kXR_secStandard, {}, secodata=True)
    frame = encode(r.Write(b"HDL0", 0, b"payload"), 5)
    seqno, signature, nodata = _signed(signer, frame)
    assert nodata is False
    assert signature == sigver_sign(key, seqno, frame[:24], frame[24:])


def test_an_embedded_iv_signer_draws_a_fresh_iv_per_signature():
    key = b"k" * 32
    signer = Signer(key, c.kXR_secStandard, {}, embedded_iv=True)
    frame = encode(r.Rm("/store/f.root"), 5)
    seqno, signature, _ = _signed(signer, frame)
    assert len(signature) == BLOCK_SIZE + 48
    assert sigver_verify(key, signature, seqno, frame[:24], frame[24:], embedded_iv=True)


def test_the_signature_covers_only_what_dlen_declares():
    """Bytes appended after the request are the next request, not payload."""
    key = b"k" * 32
    frame = encode(r.Rm("/store/f.root"), 5)
    seqno, signature, _ = _signed(Signer(key, c.kXR_secStandard, {}), frame + b"trailing")
    assert signature == sigver_sign(key, seqno, frame[:24], frame[24:])


def test_signer_repr_hides_the_session_key():
    assert "secret" not in repr(Signer(b"secretkey" * 4, c.kXR_secStandard, {}))


def test_a_signer_without_a_key_is_inert():
    assert Signer(b"", c.kXR_secStandard, {}).sign(encode(r.Write(b"H", 0, b"a"), 1)) is None


def test_a_native_checksum_prints_its_running_value():
    h = new("adler32")
    h.update(b"123456789")
    assert repr(h) == f"Checksum('adler32', {h.hexdigest()})"
    assert h.digest() == bytes.fromhex(h.hexdigest())


def test_a_hashlib_checksum_offers_the_same_surface_as_a_native_one():
    h, expected = new("sha256"), hashlib.sha256(b"123456789")
    h.update(b"123456789")
    assert h.hexdigest() == expected.hexdigest()
    assert h.digest() == expected.digest()
    assert h.value == int.from_bytes(expected.digest(), "big")
    assert repr(h) == f"Checksum('sha256', {expected.hexdigest()})"


def test_a_fresh_signer_starts_its_sequence_at_zero():
    """The counter is readable but not settable: ``sign`` is the only way up."""
    signer = Signer(b"k" * 32, c.kXR_secStandard, {})
    assert signer.seqno == 0
    signer.sign(encode(r.Write(b"H", 0, b"a"), 5))
    assert signer.seqno == 1
