"""Legacy XRootD SSS Blowfish compatibility, backed by cryptography.

Blowfish remains necessary for the existing SSS wire format, not for new
protocols. The dependency owns its key schedule, tables and cipher modes;
CFB64 is the only mode SSS uses, so it is the only one offered here.

``cryptography`` keeps Blowfish in its ``decrepit`` module, and OpenSSL 3
only provides it through the *legacy* provider: on a host in FIPS mode, or
with an OpenSSL built or configured without that provider, creating the
cipher raises :class:`cryptography.exceptions.UnsupportedAlgorithm`. That is
not this module's to paper over - :mod:`xrdclient.auth.sss` turns it into an
authentication error that says what to do.
"""

from __future__ import annotations

from cryptography.hazmat.decrepit.ciphers.algorithms import Blowfish as _Blowfish
from cryptography.hazmat.primitives.ciphers import Cipher

try:
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
except ImportError:  # cryptography 45/46, before CFB moved to ``decrepit``
    from cryptography.hazmat.primitives.ciphers.modes import CFB

__all__ = ["Blowfish"]


class Blowfish:
    """Blowfish with one key, in the CFB64 mode SSS encrypts its credential in."""

    __slots__ = ("_algorithm",)

    def __init__(self, key: bytes) -> None:
        """Schedule ``key``, 1 to 56 bytes: the secret from an SSS keytab entry."""
        if not 1 <= len(key) <= 56:
            raise ValueError("Blowfish key must be non-empty and <= 56 bytes")
        # The original protocol permits keys below OpenSSL's four-byte
        # minimum. Repetition preserves Blowfish's cyclic key schedule.
        if len(key) < 4:
            key *= (4 + len(key) - 1) // len(key)
        self._algorithm = _Blowfish(key)

    def encrypt_cfb64(self, iv: bytes, data: bytes) -> bytes:
        """Encrypt ``data`` in 64-bit CFB under an 8-byte ``iv``; no padding is added.

        CFB is a stream mode, so the ciphertext is exactly as long as the
        plaintext - the SSS credential is not a whole number of blocks.
        """
        if len(iv) != 8:
            raise ValueError("Blowfish IV must be 8 bytes")
        context = Cipher(self._algorithm, CFB(iv)).encryptor()
        return context.update(data) + context.finalize()

    def decrypt_cfb64(self, iv: bytes, data: bytes) -> bytes:
        """Decrypt what :meth:`encrypt_cfb64` produced with the same ``iv``.

        There is no integrity check at this layer: SSS carries a CRC-32 inside
        the encrypted part, and that is what exposes a wrong key.
        """
        if len(iv) != 8:
            raise ValueError("Blowfish IV must be 8 bytes")
        context = Cipher(self._algorithm, CFB(iv)).decryptor()
        return context.update(data) + context.finalize()

    def __repr__(self) -> str:
        """Never the key: it is the shared secret of the whole SSS keytab."""
        return "Blowfish(key=<redacted>)"
