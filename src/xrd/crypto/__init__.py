"""Cryptographic primitives the protocol needs that the stdlib does not have.

Blowfish (SSS), CRC-32C (paged I/O), CRC-64 (checksums), AES (the GSI session cipher), RSA and
just enough DER to read X.509 proxies — all pure Python, so ``pip install``
never needs a compiler. Everything the stdlib already has — HMAC-SHA256,
MD5, Adler-32, CRC-32, and TLS itself — comes from ``hmac``, ``hashlib``,
``zlib`` and ``ssl``.

None of this is a data path. AES here encrypts a few hundred bytes once per
connection during the GSI handshake; bulk confidentiality is TLS's job, and
TLS is ``ssl``, which is C.
"""

from __future__ import annotations

from .aes import AES, cbc_decrypt, cbc_encrypt
from .blowfish import Blowfish
from .checksum import Checksum, algorithms, checksum_bytes, checksum_file, new
from .crc32c import IS_ACCELERATED, crc32c, pack_pages, page_span, unpack_pages
from .crc64 import crc64, crc64nvme
from .der import DERError
from .rsa import RSAPrivateKey, RSAPublicKey, load_private_key, load_public_key, pem_blocks
from .sigver import Signer, is_signed, sigver_hash, sigver_sign, sigver_verify
from .x509 import Certificate, Name, ProxyCredential, load_certificates, load_proxy

__all__ = [
    "AES",
    "Blowfish",
    "Certificate",
    "DERError",
    "Name",
    "ProxyCredential",
    "RSAPrivateKey",
    "RSAPublicKey",
    "cbc_decrypt",
    "cbc_encrypt",
    "load_certificates",
    "load_private_key",
    "load_proxy",
    "load_public_key",
    "pem_blocks",
    "Checksum",
    "Signer",
    "algorithms",
    "checksum_bytes",
    "checksum_file",
    "crc32c",
    "crc64",
    "crc64nvme",
    "is_signed",
    "new",
    "pack_pages",
    "page_span",
    "sigver_hash",
    "sigver_sign",
    "sigver_verify",
    "unpack_pages",
    "IS_ACCELERATED",
]
