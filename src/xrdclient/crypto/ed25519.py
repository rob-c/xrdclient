"""Ed25519 signing and verification backed by cryptography."""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

__all__ = ["sign", "verify", "public_key", "L"]

L = 2**252 + 27742317777372353535851937790883648493


def public_key(secret: bytes) -> bytes:
    """Return the raw public key for a 32-byte secret seed."""
    return Ed25519PrivateKey.from_private_bytes(secret).public_key().public_bytes_raw()


def sign(secret: bytes, message: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(secret).sign(message)


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """Check a signature, treating malformed keys/signatures as invalid."""
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True
