"""Legacy XRootD SSS Blowfish compatibility, backed by cryptography.

Blowfish remains necessary for the existing SSS wire format, not for new
protocols. The dependency owns its key schedule, tables and cipher modes.
"""

from __future__ import annotations

import struct

from cryptography.hazmat.decrepit.ciphers.algorithms import Blowfish as _Blowfish
from cryptography.hazmat.primitives.ciphers import Cipher, modes

try:
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
except ImportError:  # cryptography 45/46, supporting Python 3.9
    from cryptography.hazmat.primitives.ciphers.modes import CFB

__all__ = ["Blowfish"]

_BLOCK = struct.Struct(">II")


class Blowfish:
    __slots__ = ("_algorithm",)

    def __init__(self, key: bytes) -> None:
        if not 1 <= len(key) <= 56:
            raise ValueError("Blowfish key must be non-empty and <= 56 bytes")
        # The original protocol permits keys below OpenSSL's four-byte
        # minimum. Repetition preserves Blowfish's cyclic key schedule.
        if len(key) < 4:
            key *= (4 + len(key) - 1) // len(key)
        self._algorithm = _Blowfish(key)

    def encrypt_block(self, left: int, right: int) -> tuple[int, int]:
        return _BLOCK.unpack(self.encrypt_ecb(_BLOCK.pack(left, right)))

    def decrypt_block(self, left: int, right: int) -> tuple[int, int]:
        return _BLOCK.unpack(self.decrypt_ecb(_BLOCK.pack(left, right)))

    def encrypt_ecb(self, data: bytes) -> bytes:
        if len(data) % 8:
            raise ValueError("ECB input must be a multiple of 8 bytes")
        context = Cipher(self._algorithm, modes.ECB()).encryptor()
        return context.update(data) + context.finalize()

    def decrypt_ecb(self, data: bytes) -> bytes:
        if len(data) % 8:
            raise ValueError("ECB input must be a multiple of 8 bytes")
        context = Cipher(self._algorithm, modes.ECB()).decryptor()
        return context.update(data) + context.finalize()

    def encrypt_cfb64(self, iv: bytes, data: bytes) -> bytes:
        if len(iv) != 8:
            raise ValueError("Blowfish IV must be 8 bytes")
        context = Cipher(self._algorithm, CFB(iv)).encryptor()
        return context.update(data) + context.finalize()

    def decrypt_cfb64(self, iv: bytes, data: bytes) -> bytes:
        if len(iv) != 8:
            raise ValueError("Blowfish IV must be 8 bytes")
        context = Cipher(self._algorithm, CFB(iv)).decryptor()
        return context.update(data) + context.finalize()

    def __repr__(self) -> str:
        return "Blowfish(key=<redacted>)"
