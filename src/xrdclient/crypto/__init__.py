"""Cryptographic primitives the protocol needs that the stdlib does not have.

Blowfish (SSS), CRC-32C (paged I/O), CRC-64 (checksums), AES (the GSI session cipher), RSA and
X.509 proxy adapters. cryptography and asn1crypto own the reusable primitives;
platform/protocol policy stays here. Standard hashes, checksums and TLS use
``hmac``, ``hashlib``, ``zlib`` and ``ssl``.

None of this is a data path. AES here encrypts a few hundred bytes once per
connection during the GSI handshake; bulk confidentiality is TLS's job, and
TLS is ``ssl``, which is C.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the names, for a type checker, at no cost at run time
    from .aes import AES, cbc_decrypt, cbc_encrypt
    from .blowfish import Blowfish
    from .checksum import Checksum, algorithms, checksum_bytes, checksum_file, new
    from .crc32c import IS_ACCELERATED, crc32c, pack_pages, page_span, unpack_pages
    from .crc64 import crc64, crc64nvme
    from .der import DERError
    from .rsa import (
        RSAPrivateKey,
        RSAPublicKey,
        load_private_key,
        load_public_key,
        pem_blocks,
    )
    from .sigver import Signer, is_signed, sigver_hash, sigver_sign, sigver_verify
    from .voms import (
        VOMSAttribute,
        VOMSDiagnostic,
        VOMSEntry,
        VOMSResult,
        VOMSStatus,
        check_vomses,
        inspect_voms,
        validate_voms,
    )
    from .x509 import Certificate, Name, ProxyCredential, load_certificates, load_proxy

#: Which module each public name lives in. Nothing here is imported until it is
#: asked for, because importing one of these packages used to import all of
#: them: a plain read would load AES, Blowfish, RSA and X.509 to get at the
#: request signer, and a download needs none of the four.
_MODULES = {
    "AES": "aes",
    "cbc_decrypt": "aes",
    "cbc_encrypt": "aes",
    "Blowfish": "blowfish",
    "Checksum": "checksum",
    "algorithms": "checksum",
    "checksum_bytes": "checksum",
    "checksum_file": "checksum",
    "new": "checksum",
    "IS_ACCELERATED": "crc32c",
    "crc32c": "crc32c",
    "pack_pages": "crc32c",
    "page_span": "crc32c",
    "unpack_pages": "crc32c",
    "crc64": "crc64",
    "crc64nvme": "crc64",
    "DERError": "der",
    "RSAPrivateKey": "rsa",
    "RSAPublicKey": "rsa",
    "load_private_key": "rsa",
    "load_public_key": "rsa",
    "pem_blocks": "rsa",
    "Signer": "sigver",
    "is_signed": "sigver",
    "sigver_hash": "sigver",
    "sigver_sign": "sigver",
    "sigver_verify": "sigver",
    "VOMSAttribute": "voms",
    "VOMSDiagnostic": "voms",
    "VOMSEntry": "voms",
    "VOMSResult": "voms",
    "VOMSStatus": "voms",
    "inspect_voms": "voms",
    "validate_voms": "voms",
    "check_vomses": "voms",
    "Certificate": "x509",
    "Name": "x509",
    "ProxyCredential": "x509",
    "load_certificates": "x509",
    "load_proxy": "x509",
}


def __getattr__(name: str) -> object:
    """Import the module a name lives in, the first time the name is used."""
    module = _MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value  # bound once; later lookups skip this function
    return value


def __dir__() -> list[str]:
    return sorted(_MODULES)


__all__ = [
    "AES",
    "Blowfish",
    "Certificate",
    "Checksum",
    "DERError",
    "IS_ACCELERATED",
    "Name",
    "ProxyCredential",
    "RSAPrivateKey",
    "RSAPublicKey",
    "Signer",
    "VOMSAttribute",
    "VOMSDiagnostic",
    "VOMSEntry",
    "VOMSResult",
    "VOMSStatus",
    "algorithms",
    "cbc_decrypt",
    "cbc_encrypt",
    "checksum_bytes",
    "checksum_file",
    "crc32c",
    "crc64",
    "crc64nvme",
    "is_signed",
    "inspect_voms",
    "load_certificates",
    "load_private_key",
    "load_proxy",
    "load_public_key",
    "new",
    "pack_pages",
    "page_span",
    "pem_blocks",
    "sigver_hash",
    "sigver_sign",
    "sigver_verify",
    "unpack_pages",
    "validate_voms",
    "check_vomses",
]
