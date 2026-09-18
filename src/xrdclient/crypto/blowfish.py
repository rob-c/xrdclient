"""Pure-Python Blowfish - the cipher XRootD's SSS credential is sealed with.

Not in the stdlib and not worth an ``cryptography`` dependency for a 56-byte
key, so it lives here. The P-array and S-boxes are the fractional hex digits
of pi; rather than embed 1042 magic constants they are derived with Machin's
formula in exact integer arithmetic. ``test_blowfish.py`` pins both the first
derived word (0x243F6A88) and Schneier's canonical ECB vectors, which
validates the derivation and the round function together.
"""

from __future__ import annotations

import functools

__all__ = ["Blowfish"]

_MASK = 0xFFFFFFFF
_NWORDS = 18 + 4 * 256


def _pi_fraction_words(count: int) -> list[int]:
    """The first ``count`` 32-bit words of pi's fractional part."""
    bits = count * 32 + 64
    one = 1 << bits

    def atan_inv(x: int) -> int:
        # atan(1/x) = 1/x - 1/(3x^3) + 1/(5x^5) - ..., scaled by `one`.
        term = one // x
        total = term
        x2 = x * x
        k = 1
        while term:
            term //= x2
            k += 2
            total += -(term // k) if (k // 2) % 2 else term // k
        return total

    frac = 16 * atan_inv(5) - 4 * atan_inv(239) - 3 * one
    return [(frac >> (bits - 32 * (i + 1))) & _MASK for i in range(count)]


@functools.cache
def _initial_boxes() -> tuple[tuple[int, ...], tuple[tuple[int, ...], ...]]:
    """The unkeyed P-array and S-boxes. Cached; costs ~0.2 s once."""
    pi = _pi_fraction_words(_NWORDS)
    return (
        tuple(pi[:18]),
        tuple(tuple(pi[18 + 256 * i : 274 + 256 * i]) for i in range(4)),
    )


class Blowfish:
    """Blowfish with ECB and CFB64 modes.

    Key schedule cost is 521 block encryptions, so reuse an instance rather
    than re-keying per message.
    """

    __slots__ = ("_p", "_s")

    def __init__(self, key: bytes) -> None:
        _validate_key(key)
        p0, s0 = _initial_boxes()
        self._p = list(p0)
        self._s = [list(box) for box in s0]
        _xor_key(self._p, key)
        _expand_boxes(self)

    def _f(self, x: int) -> int:
        s0, s1, s2, s3 = self._s
        return (
            (((s0[x >> 24] + s1[(x >> 16) & 0xFF]) & _MASK) ^ s2[(x >> 8) & 0xFF]) + s3[x & 0xFF]
        ) & _MASK

    def encrypt_block(self, left: int, right: int) -> tuple[int, int]:
        """One 64-bit block, as a pair of 32-bit halves."""
        p = self._p
        f = self._f
        for i in range(16):
            left ^= p[i]
            right ^= f(left)
            left, right = right, left
        left, right = right, left
        return (left ^ p[17]) & _MASK, (right ^ p[16]) & _MASK

    def decrypt_block(self, left: int, right: int) -> tuple[int, int]:
        """Inverse of :meth:`encrypt_block`."""
        p = self._p
        f = self._f
        for i in range(17, 1, -1):
            left ^= p[i]
            right ^= f(left)
            left, right = right, left
        left, right = right, left
        return (left ^ p[0]) & _MASK, (right ^ p[1]) & _MASK

    # -- byte-oriented modes ---------------------------------------------

    def encrypt_ecb(self, data: bytes) -> bytes:
        if len(data) % 8:
            raise ValueError("ECB input must be a multiple of 8 bytes")
        out = bytearray()
        for i in range(0, len(data), 8):
            left, right = self.encrypt_block(
                int.from_bytes(data[i : i + 4], "big"),
                int.from_bytes(data[i + 4 : i + 8], "big"),
            )
            out += left.to_bytes(4, "big") + right.to_bytes(4, "big")
        return bytes(out)

    def decrypt_ecb(self, data: bytes) -> bytes:
        if len(data) % 8:
            raise ValueError("ECB input must be a multiple of 8 bytes")
        out = bytearray()
        for i in range(0, len(data), 8):
            left, right = self.decrypt_block(
                int.from_bytes(data[i : i + 4], "big"),
                int.from_bytes(data[i + 4 : i + 8], "big"),
            )
            out += left.to_bytes(4, "big") + right.to_bytes(4, "big")
        return bytes(out)

    def _cfb64(self, iv: bytes, data: bytes, *, decrypt: bool) -> bytes:
        if len(iv) != 8:
            raise ValueError(f"Blowfish IV must be 8 bytes, got {len(iv)}")
        out = bytearray(len(data))
        fb = bytearray(iv)
        for base in range(0, len(data), 8):
            left, right = self.encrypt_block(
                int.from_bytes(fb[0:4], "big"), int.from_bytes(fb[4:8], "big")
            )
            fb[0:4] = left.to_bytes(4, "big")
            fb[4:8] = right.to_bytes(4, "big")
            for k in range(min(8, len(data) - base)):
                src = data[base + k]
                out[base + k] = src ^ fb[k]
                # The feedback register always takes the ciphertext byte.
                fb[k] = src if decrypt else out[base + k]
        return bytes(out)

    def encrypt_cfb64(self, iv: bytes, data: bytes) -> bytes:
        """CFB64 encryption - the mode XRootD's ``bf32`` SSS blob uses."""
        return self._cfb64(iv, data, decrypt=False)

    def decrypt_cfb64(self, iv: bytes, data: bytes) -> bytes:
        """Inverse of :meth:`encrypt_cfb64`."""
        return self._cfb64(iv, data, decrypt=True)

    def __repr__(self) -> str:
        return "Blowfish(key=<redacted>)"


def _validate_key(key: bytes) -> None:
    if not key:
        raise ValueError("Blowfish key must be non-empty")
    if len(key) > 56:
        raise ValueError(f"Blowfish key must be <= 56 bytes, got {len(key)}")


def _key_words(key: bytes) -> list[int]:
    words = []
    for offset in range(0, 72, 4):
        word = bytes(key[(offset + byte) % len(key)] for byte in range(4))
        words.append(int.from_bytes(word, "big"))
    return words


def _xor_key(p_array: list[int], key: bytes) -> None:
    for index, word in enumerate(_key_words(key)):
        p_array[index] ^= word


def _expand_pair(
    cipher: Blowfish, target: list[int], index: int, pair: tuple[int, int]
) -> tuple[int, int]:
    left, right = cipher.encrypt_block(*pair)
    target[index] = left
    target[index + 1] = right
    return left, right


def _expand_boxes(cipher: Blowfish) -> None:
    pair = (0, 0)
    for index in range(0, 18, 2):
        pair = _expand_pair(cipher, cipher._p, index, pair)
    for box in cipher._s:
        for index in range(0, 256, 2):
            pair = _expand_pair(cipher, box, index, pair)
