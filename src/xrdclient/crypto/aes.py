"""GSI AES-CBC and PKCS#7 padding backed by cryptography."""

from __future__ import annotations

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

__all__ = ["AES", "CTR", "cbc_encrypt", "cbc_decrypt", "pkcs7_pad", "pkcs7_unpad", "BLOCK_SIZE"]

BLOCK_SIZE = 16


class AES:
    __slots__ = ("_cipher", "key_size")

    def __init__(self, key: bytes) -> None:
        if len(key) not in (16, 24, 32):
            raise ValueError("AES key must be 16, 24 or 32 bytes")
        self._cipher = Cipher(algorithms.AES(key), modes.ECB())
        self.key_size = len(key)

    @property
    def rounds(self) -> int:
        """The AES round count, retained for SSH adapter compatibility."""
        return self.key_size // 4 + 6

    def encrypt_block(self, block: bytes) -> bytes:
        if len(block) != BLOCK_SIZE:
            raise ValueError(f"AES block must be {BLOCK_SIZE} bytes, got {len(block)}")
        return self._cipher.encryptor().update(block)

    def decrypt_block(self, block: bytes) -> bytes:
        if len(block) != BLOCK_SIZE:
            raise ValueError(f"AES block must be {BLOCK_SIZE} bytes, got {len(block)}")
        return self._cipher.decryptor().update(block)

    def __repr__(self) -> str:
        return f"AES(bits={self.key_size * 8}, key=<redacted>)"


def pkcs7_pad(data: bytes, block_size: int = BLOCK_SIZE) -> bytes:
    context = padding.PKCS7(block_size * 8).padder()
    return context.update(data) + context.finalize()


def pkcs7_unpad(data: bytes, block_size: int = BLOCK_SIZE) -> bytes:
    if not data or len(data) % block_size:
        raise ValueError("PKCS#7 input is not a whole number of blocks")
    context = padding.PKCS7(block_size * 8).unpadder()
    try:
        return context.update(data) + context.finalize()
    except ValueError as exc:
        raise ValueError("PKCS#7 padding is corrupt") from exc


def cbc_encrypt(
    key: bytes, data: bytes, iv: bytes = bytes(BLOCK_SIZE), *, pad: bool = True
) -> bytes:
    if len(iv) != BLOCK_SIZE:
        raise ValueError("AES IV must be 16 bytes")
    plain = pkcs7_pad(data) if pad else data
    if len(plain) % BLOCK_SIZE:
        raise ValueError("unpadded CBC input must be a whole number of blocks")
    context = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return context.update(plain) + context.finalize()


def cbc_decrypt(
    key: bytes, data: bytes, iv: bytes = bytes(BLOCK_SIZE), *, pad: bool = True
) -> bytes:
    if len(iv) != BLOCK_SIZE:
        raise ValueError("AES IV must be 16 bytes")
    if not data or len(data) % BLOCK_SIZE:
        raise ValueError("CBC input must be a whole number of blocks")
    context = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = context.update(data) + context.finalize()
    return pkcs7_unpad(plain) if pad else plain


class CTR:
    """Successive updates continue the keystream, including partial blocks."""

    def __init__(self, key: bytes, iv: bytes) -> None:
        if len(iv) != 16:
            raise ValueError("the CTR counter block is 16 bytes")
        self._context = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()

    def update(self, data: bytes | bytearray | memoryview) -> bytes:
        return self._context.update(data)
