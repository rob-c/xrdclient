"""``kXR_sigver`` request signing - stock ``XrdSecProtect``, secver 0.

When the server advertises a security level at or above ``kXR_secStandard``,
covered opcodes must be preceded by a ``kXR_sigver`` frame. What that frame
carries is not an HMAC: the reference scheme hashes and then encrypts. The
signature is ``SHA-256(seqno_be64 || request_header || payload)`` run through
the session cipher negotiated at login - AES-CBC under the DH session key.
An unsigned-DH peer uses a zero IV and sends nothing but the ciphertext; a
signed-DH peer draws a fresh IV per signature and prepends it. A data-bearing
request - ``kXR_write``, ``kXR_pgwrite`` - is hashed *without* its payload
unless the server negotiated ``kXR_secOData``, and the frame says so with
``kXR_nodata_sig``. Conformant servers answer the signature frame only when
it fails to verify.

The frame itself (:class:`~xrdclient.proto.requests.Sigver`) is an ordinary
24-byte request header on the *same stream id* as the request it covers:
``expectrid`` (the covered opcode, u16), a version byte, the flags byte
(``kXR_nodata_sig``), the 64-bit sequence number and the crypto type
(``kXR_SHA256_sig``), with the signature as its payload. What is hashed is
the covered request's 24-byte header exactly as sent - stream id included -
so a signature cannot be replayed onto another request or another stream,
and the strictly increasing sequence number stops it being replayed at all.

This module only computes and checks signatures; :class:`Signer` holds the
per-connection state, and the protocol machine (:mod:`xrdclient.proto.machine`)
decides where frames go. :func:`sigver_verify` is the server's half of the
check, for tests and tools that judge what a client signed.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct

from xrdsec.aes import BLOCK_SIZE, cbc_decrypt, cbc_encrypt

from ..proto import constants as c

__all__ = [
    "LEVEL_OPCODES",
    "SIGNED_OPCODES",
    "Signer",
    "is_signed",
    "sigver_hash",
    "sigver_sign",
    "sigver_verify",
]

#: Opcodes that mutate state, and so are signed from ``kXR_secStandard`` up.
SIGNED_OPCODES = frozenset(
    {
        c.kXR_chmod,
        c.kXR_fattr,
        c.kXR_mkdir,
        c.kXR_mv,
        c.kXR_open,
        c.kXR_pgwrite,
        c.kXR_prepare,
        c.kXR_rm,
        c.kXR_rmdir,
        c.kXR_set,
        c.kXR_truncate,
        c.kXR_write,
        c.kXR_writev,
        c.kXR_chkpoint,
        c.kXR_clone,
    }
)

#: Additional opcodes each security level brings in over the one below it.
LEVEL_OPCODES: dict[int, frozenset[int]] = {
    c.kXR_secNone: frozenset(),
    c.kXR_secCompatible: frozenset(),
    c.kXR_secStandard: SIGNED_OPCODES,
    c.kXR_secIntense: SIGNED_OPCODES | {c.kXR_close, c.kXR_dirlist, c.kXR_locate, c.kXR_stat},
    c.kXR_secPedantic: frozenset(range(c.kXR_1stRequest, c.kXR_clone + 1)),
}

#: The requests whose payload is file data rather than arguments. Their bytes
#: travel outside the signature unless ``kXR_secOData`` was negotiated.
_DATA_OPCODES = frozenset({c.kXR_write, c.kXR_pgwrite})


def is_signed(opcode: int, level: int, overrides: dict[int, int] | None = None) -> bool:
    """Whether ``opcode`` needs a signature at security ``level``.

    ``overrides`` is the per-opcode table from the ``kXR_protocol`` security
    block; a value of ``kXR_secNone`` there exempts an otherwise-covered
    opcode, and any other value forces one in.
    """
    if overrides and opcode in overrides:
        return overrides[opcode] != c.kXR_secNone
    if level <= c.kXR_secCompatible:
        return False
    return opcode in LEVEL_OPCODES.get(level, SIGNED_OPCODES)


def sigver_hash(seqno: int, header: bytes, payload: bytes, *, nodata: bool = False) -> bytes:
    """What is signed: SHA-256 over the sequence number, header and payload.

    ``nodata`` drops the payload from the hash - the data of a write travels
    unsigned unless the server negotiated ``kXR_secOData``.
    """
    msg = struct.pack(">Q", seqno) + bytes(header)
    if not nodata:
        msg += bytes(payload)
    return hashlib.sha256(msg).digest()


def sigver_sign(
    key: bytes,
    seqno: int,
    header: bytes,
    payload: bytes,
    *,
    nodata: bool = False,
    iv: bytes | None = None,
) -> bytes:
    """The signature a ``kXR_sigver`` frame carries.

    With ``iv`` left ``None`` the hash is encrypted under a zero IV and the
    ciphertext stands alone, which is the unsigned-DH session this client
    negotiates. Giving an ``iv`` prepends it to the ciphertext, which is what
    a signed-DH peer expects.
    """
    hashed = sigver_hash(seqno, header, payload, nodata=nodata)
    if iv is None:
        return cbc_encrypt(key, hashed)
    return iv + cbc_encrypt(key, hashed, iv)


def sigver_verify(
    key: bytes,
    signature: bytes,
    seqno: int,
    header: bytes,
    payload: bytes,
    *,
    nodata: bool = False,
    embedded_iv: bool = False,
) -> bool:
    """Whether ``signature`` is ``sigver_sign`` of the same material.

    ``embedded_iv`` reads the leading block as the IV, the signed-DH shape.
    A signature that will not even decrypt is simply wrong, not an error.
    """
    iv = bytes(BLOCK_SIZE)
    if embedded_iv:
        if len(signature) < BLOCK_SIZE:
            return False
        iv, signature = signature[:BLOCK_SIZE], signature[BLOCK_SIZE:]
    try:
        plain = cbc_decrypt(key, signature, iv)
    except ValueError:
        return False
    expected = sigver_hash(seqno, header, payload, nodata=nodata)
    return hmac.compare_digest(plain, expected)


class Signer:
    """Per-connection signing state: the session key and the sequence number.

    The sequence number is monotonic per connection and must never repeat, so
    :meth:`sign` is the only way to advance it. ``secodata`` is whether the
    server's ``kXR_protocol`` reply carried ``kXR_secOData``, which pulls a
    write's payload into its signature; ``embedded_iv`` is the signed-DH
    shape, a fresh IV drawn and prepended per signature.
    """

    __slots__ = ("_seqno", "embedded_iv", "key", "level", "overrides", "secodata")

    def __init__(
        self,
        key: bytes,
        level: int = c.kXR_secNone,
        overrides: dict[int, int] | None = None,
        *,
        secodata: bool = False,
        embedded_iv: bool = False,
    ) -> None:
        """Sign under ``key`` at security ``level``, with the server's per-opcode ``overrides``.

        An empty ``key`` (no session key was negotiated) makes every
        :meth:`required` answer ``False``: there is nothing to sign with.
        """
        self.key = key
        self.level = level
        self.overrides = overrides or {}
        self.secodata = secodata
        self.embedded_iv = embedded_iv
        self._seqno = 0

    @property
    def seqno(self) -> int:
        """The sequence number of the last signature made; ``0`` before the first."""
        return self._seqno

    def required(self, opcode: int) -> bool:
        """Whether a request with ``opcode`` must be preceded by a signature frame."""
        return bool(self.key) and is_signed(opcode, self.level, self.overrides)

    def sign(self, frame: bytes) -> tuple[int, bytes, bool] | None:
        """Signature for an encoded request frame, or ``None`` if unneeded.

        Returns ``(seqno, signature, nodata)``; the caller wraps them in a
        :class:`~xrdclient.proto.requests.Sigver` on the same stream, with
        ``nodata`` carried as ``kXR_nodata_sig`` so the server hashes the
        same bytes this did.
        """
        opcode = struct.unpack_from(">H", frame, 2)[0]
        if not self.required(opcode):
            return None
        self._seqno += 1
        header = frame[: c.REQUEST_HDRLEN]
        # Exactly what dlen declares, not everything after the header: the
        # frame is the request, and anything a caller appended after it is
        # the next request.
        dlen = struct.unpack_from(">i", frame, c.REQUEST_HDRLEN - 4)[0]
        payload = frame[c.REQUEST_HDRLEN : c.REQUEST_HDRLEN + dlen]
        nodata = opcode in _DATA_OPCODES and not self.secodata
        iv = os.urandom(BLOCK_SIZE) if self.embedded_iv else None
        signature = sigver_sign(self.key, self._seqno, header, payload, nodata=nodata, iv=iv)
        return self._seqno, signature, nodata

    def __repr__(self) -> str:
        """Level and position only: the session key never reaches a log."""
        return f"Signer(level={self.level}, seqno={self._seqno}, key=<redacted>)"
