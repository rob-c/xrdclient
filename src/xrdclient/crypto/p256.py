"""P-256 API compatibility adapter backed by cryptography.

Curve arithmetic, nonce generation and signature verification are maintained
by cryptography, not by either client.
"""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

__all__ = ["P", "N", "G", "sign", "verify", "public_key", "encode_point", "decode_point"]

P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
G = (
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
)


def encode_point(point: tuple[int, int]) -> bytes:
    """Return the SEC1 uncompressed encoding."""
    return b"\x04" + point[0].to_bytes(32, "big") + point[1].to_bytes(32, "big")


def decode_point(data: bytes) -> tuple[int, int]:
    """Parse an uncompressed point, rejecting values outside P-256."""
    if len(data) != 65 or data[0] != 4:
        raise ValueError("expected an uncompressed P-256 point")
    try:
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), data)
    except ValueError as exc:
        raise ValueError("point is not on P-256") from exc
    numbers = key.public_numbers()
    return numbers.x, numbers.y


def _private(d: int) -> ec.EllipticCurvePrivateKey:
    if not 0 < d < N:
        raise ValueError("private scalar out of range")
    return ec.derive_private_key(d, ec.SECP256R1())


def public_key(d: int) -> tuple[int, int]:
    numbers = _private(d).public_key().public_numbers()
    return numbers.x, numbers.y


def sign(d: int, message: bytes) -> tuple[int, int]:
    """Return a deterministic RFC 6979 ECDSA-SHA256 signature."""
    signature = _private(d).sign(message, ec.ECDSA(hashes.SHA256(), deterministic_signing=True))
    return utils.decode_dss_signature(signature)


def verify(public: tuple[int, int], message: bytes, r: int, s: int) -> bool:
    if not (0 < r < N and 0 < s < N):
        return False
    try:
        key = ec.EllipticCurvePublicNumbers(*public, ec.SECP256R1()).public_key()
        key.verify(utils.encode_dss_signature(r, s), message, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        return False
    return True
