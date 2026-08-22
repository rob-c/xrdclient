"""AES in pure Python, with the CBC mode GSI's session cipher uses.

The stdlib has no block cipher, and the whole point of this package is that
it installs without a compiler. GSI encrypts a few hundred bytes once per
connection, so a table-driven Python implementation is far below the noise
of the round trip it rides on — this is not, and must not become, a data
path. Bulk encryption is TLS's job, and that is ``ssl``, which is C.

FIPS-197 is the reference; the tables are generated at import from the
field arithmetic rather than pasted in, so there is nothing to mistype.
"""

from __future__ import annotations

from itertools import chain

from .._compat import zip_strict

__all__ = ["AES", "cbc_encrypt", "cbc_decrypt", "pkcs7_pad", "pkcs7_unpad", "BLOCK_SIZE"]

BLOCK_SIZE = 16


def _build_tables() -> tuple[bytes, bytes]:
    """The S-box and its inverse, from the multiplicative inverse in GF(2^8)."""
    sbox = bytearray(256)
    p = q = 1
    while True:  # walk the generator 3 over the field, which visits every unit
        p, q = _next_units(p, q)
        sbox[p] = _affine(q)
        if p == 1:
            break
    sbox[0] = 0x63
    inverse = bytearray(256)
    for index, value in enumerate(sbox):
        inverse[value] = index
    return bytes(sbox), bytes(inverse)


def _next_units(p: int, q: int) -> tuple[int, int]:
    p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
    q ^= q << 1
    q ^= q << 2
    q ^= q << 4
    q &= 0xFF
    return p, q ^ 0x09 if q & 0x80 else q


def _affine(value: int) -> int:
    transformed = value ^ ((value << 1) | (value >> 7)) ^ ((value << 2) | (value >> 6))
    transformed ^= ((value << 3) | (value >> 5)) ^ ((value << 4) | (value >> 4))
    return (transformed ^ 0x63) & 0xFF


SBOX, INV_SBOX = _build_tables()
RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36, 0x6C, 0xD8, 0xAB, 0x4D)


def _xtime(a: int) -> int:
    """Multiply by x in GF(2^8) modulo the AES polynomial."""
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    """Multiply two field elements."""
    out = 0
    while b:
        if b & 1:
            out ^= a
        a = _xtime(a)
        b >>= 1
    return out


class AES:
    """One AES key, expanded once and reusable for many blocks.

    ``key`` must be 16, 24 or 32 bytes — AES-128, -192 or -256. The methods
    take and return exactly one 16-byte block; chaining is :func:`cbc_encrypt`
    and :func:`cbc_decrypt`.
    """

    __slots__ = ("_round_keys", "_rounds", "key_size")

    def __init__(self, key: bytes) -> None:
        if len(key) not in (16, 24, 32):
            raise ValueError(f"AES key must be 16, 24 or 32 bytes, got {len(key)}")
        self.key_size = len(key)
        self._rounds = {16: 10, 24: 12, 32: 14}[len(key)]
        self._round_keys = self._expand(key)

    def _expand(self, key: bytes) -> list[list[int]]:
        """FIPS-197 §5.2: the key schedule, as one 4-byte word per row."""
        words = [list(key[i : i + 4]) for i in range(0, len(key), 4)]
        nk = len(words)
        total = 4 * (self._rounds + 1)
        for index in range(nk, total):
            temp = list(words[index - 1])
            if index % nk == 0:
                temp = temp[1:] + temp[:1]
                temp = [SBOX[b] for b in temp]
                temp[0] ^= RCON[index // nk - 1]
            elif nk > 6 and index % nk == 4:
                temp = [SBOX[b] for b in temp]
            words.append([a ^ b for a, b in zip_strict(words[index - nk], temp)])
        return [
            list(chain.from_iterable(words[r * 4 : r * 4 + 4])) for r in range(self._rounds + 1)
        ]

    @staticmethod
    def _add_round_key(state: list[int], key: list[int]) -> None:
        for i in range(16):
            state[i] ^= key[i]

    @staticmethod
    def _shift_rows(state: list[int]) -> None:
        for row in range(1, 4):
            column = [state[row + 4 * c] for c in range(4)]
            column = column[row:] + column[:row]
            for c in range(4):
                state[row + 4 * c] = column[c]

    @staticmethod
    def _inv_shift_rows(state: list[int]) -> None:
        for row in range(1, 4):
            column = [state[row + 4 * c] for c in range(4)]
            column = column[-row:] + column[:-row]
            for c in range(4):
                state[row + 4 * c] = column[c]

    @staticmethod
    def _mix_columns(state: list[int]) -> None:
        for c in range(4):
            a = state[4 * c : 4 * c + 4]
            t = a[0] ^ a[1] ^ a[2] ^ a[3]
            for i in range(4):
                state[4 * c + i] = a[i] ^ t ^ _xtime(a[i] ^ a[(i + 1) % 4])

    @staticmethod
    def _inv_mix_columns(state: list[int]) -> None:
        for c in range(4):
            a = state[4 * c : 4 * c + 4]
            for i in range(4):
                state[4 * c + i] = (
                    _mul(a[i], 14)
                    ^ _mul(a[(i + 1) % 4], 11)
                    ^ _mul(a[(i + 2) % 4], 13)
                    ^ _mul(a[(i + 3) % 4], 9)
                )

    def encrypt_block(self, block: bytes) -> bytes:
        """One 16-byte block, encrypted."""
        if len(block) != BLOCK_SIZE:
            raise ValueError(f"AES block must be {BLOCK_SIZE} bytes, got {len(block)}")
        state = list(block)
        self._add_round_key(state, self._round_keys[0])
        for rnd in range(1, self._rounds):
            state = [SBOX[b] for b in state]
            self._shift_rows(state)
            self._mix_columns(state)
            self._add_round_key(state, self._round_keys[rnd])
        state = [SBOX[b] for b in state]
        self._shift_rows(state)
        self._add_round_key(state, self._round_keys[self._rounds])
        return bytes(state)

    def decrypt_block(self, block: bytes) -> bytes:
        """One 16-byte block, decrypted."""
        if len(block) != BLOCK_SIZE:
            raise ValueError(f"AES block must be {BLOCK_SIZE} bytes, got {len(block)}")
        state = list(block)
        self._add_round_key(state, self._round_keys[self._rounds])
        for rnd in range(self._rounds - 1, 0, -1):
            self._inv_shift_rows(state)
            state = [INV_SBOX[b] for b in state]
            self._add_round_key(state, self._round_keys[rnd])
            self._inv_mix_columns(state)
        self._inv_shift_rows(state)
        state = [INV_SBOX[b] for b in state]
        self._add_round_key(state, self._round_keys[0])
        return bytes(state)

    def __repr__(self) -> str:
        return f"AES(bits={self.key_size * 8}, key=<redacted>)"


def pkcs7_pad(data: bytes, block_size: int = BLOCK_SIZE) -> bytes:
    """Append 1..``block_size`` bytes so the length is a whole number of blocks."""
    pad = block_size - len(data) % block_size
    return data + bytes([pad]) * pad


def pkcs7_unpad(data: bytes, block_size: int = BLOCK_SIZE) -> bytes:
    """Strip PKCS#7 padding, refusing anything malformed."""
    if not data or len(data) % block_size:
        raise ValueError("PKCS#7 input is not a whole number of blocks")
    pad = data[-1]
    if not 1 <= pad <= block_size or data[-pad:] != bytes([pad]) * pad:
        raise ValueError("PKCS#7 padding is corrupt")
    return data[:-pad]


def cbc_encrypt(
    key: bytes, data: bytes, iv: bytes = bytes(BLOCK_SIZE), *, pad: bool = True
) -> bytes:
    """CBC-encrypt ``data``, PKCS#7-padding it first unless told not to."""
    if len(iv) != BLOCK_SIZE:
        raise ValueError(f"AES IV must be {BLOCK_SIZE} bytes, got {len(iv)}")
    plain = pkcs7_pad(data) if pad else data
    if len(plain) % BLOCK_SIZE:
        raise ValueError("unpadded CBC input must be a whole number of blocks")
    cipher = AES(key)
    previous = iv
    out = bytearray()
    for start in range(0, len(plain), BLOCK_SIZE):
        block = bytes(a ^ b for a, b in zip_strict(plain[start : start + BLOCK_SIZE], previous))
        previous = cipher.encrypt_block(block)
        out += previous
    return bytes(out)


def cbc_decrypt(
    key: bytes, data: bytes, iv: bytes = bytes(BLOCK_SIZE), *, pad: bool = True
) -> bytes:
    """CBC-decrypt ``data``, stripping PKCS#7 padding unless told not to."""
    if len(iv) != BLOCK_SIZE:
        raise ValueError(f"AES IV must be {BLOCK_SIZE} bytes, got {len(iv)}")
    if not data or len(data) % BLOCK_SIZE:
        raise ValueError("CBC input must be a whole number of blocks")
    cipher = AES(key)
    previous = iv
    out = bytearray()
    for start in range(0, len(data), BLOCK_SIZE):
        block = data[start : start + BLOCK_SIZE]
        out += bytes(a ^ b for a, b in zip_strict(cipher.decrypt_block(block), previous))
        previous = block
    return pkcs7_unpad(bytes(out)) if pad else bytes(out)
