"""Cryptography and checksums for the protocols: where each piece lives and why.

The modules, by the layer that uses them:

- Authentication: :mod:`.aes` (the GSI session cipher), :mod:`.rsa` (GSI's
  raw-padding proof of possession and key loading), :mod:`.blowfish` (the
  SSS credential), :mod:`.rfc3961` (Kerberos enctypes), :mod:`.x509`
  (certificates and proxy files), :mod:`.proxies` (the one proxy issuer,
  with its ``rfc3820``/``arc``/``xrootd``/``globus-legacy`` profiles, and
  proxy files and descriptions; :mod:`.delegation` and :mod:`.userproxy` are
  thin profiles of it), :mod:`.trust` (is this GSI server who it says?).
- Authorization attributes: :mod:`.voms` (VOMS ACs; the machinery is
  xrdsec's private :mod:`xrdsec._voms` package) and :mod:`.signatures`
  (one signature check for every algorithm, with the accepted-algorithm
  policy).
- Request integrity: :mod:`.sigver` (``kXR_sigver`` signing).
- Data integrity: :mod:`.checksum`, :mod:`.crc32c` (paged I/O, per page)
  and :mod:`.crc64`.
- Encoding: :mod:`.der`, a strict DER reader/writer for the structures X.509
  libraries do not model. :mod:`.ed25519` and :mod:`.p256` serve
  xgfalclient's SSH transport.

**Where the code is.** Since 0.4.0 the grid security modules - :mod:`.der`,
:mod:`.rsa`, :mod:`.aes`, :mod:`.p256`, :mod:`.ed25519`, :mod:`.signatures`,
:mod:`.proxies`, :mod:`.delegation`, :mod:`.userproxy`, :mod:`.trust`,
:mod:`.voms` - are xrdsec's, shared with xgfalclient and xrdarc: each file
here replaces itself in ``sys.modules`` with the xrdsec module of the same
name (``xrdclient.crypto.der is xrdsec.der``), and :mod:`.x509` re-exports
:mod:`xrdsec.x509` with a Config-shaped ``default_proxy_path``. The
checksums are xrdcore's the same way (:mod:`.checksum`, :mod:`.crc64`; the
CRC of :mod:`.crc32c`), and :mod:`.rfc3961` is :mod:`xrdsec.krb5.rfc3961`,
with the rest of the Kerberos client. What is XRootD's own, and implemented
here, is :mod:`.blowfish`, :mod:`.sigver` and the paged-I/O framing in
:mod:`.crc32c`.

Since 0.4.0 block ciphers, public-key operations and certificate parsing are
``cryptography`` (OpenSSL) and ``asn1crypto`` underneath, running in
maintained, constant-time native code; what stays in these modules is
protocol shape and policy - which algorithms are accepted, how names compare,
what a trust failure tells the user. The one cryptographic operation done in
Python is the blinded raw-padding RSA signature in :mod:`.rsa`, which
``cryptography`` does not offer. Of the checksums, CRC-32C uses
``google-crc32c`` when the ``fast`` extra is installed and CRC-64 is always
pure Python; standard hashes, CRC-32/Adler-32 and TLS come from ``hashlib``,
``zlib`` and ``ssl``.

This package is lazy (PEP 562), like :mod:`xrdclient` itself: ``from
xrdclient.crypto import crc32c`` loads :mod:`.crc32c` and nothing else, so
needing one primitive never costs the import of all the others.
``tests/test_import_cost.py`` holds that line; a new public name goes in
``_MODULES`` and the ``TYPE_CHECKING`` block, never in an eager import here.
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
    from .proxies import (
        ProxyInfo,
        create_proxy,
        describe_proxy,
        issue_proxy,
        load_credential,
        write_proxy,
    )
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
    "ProxyInfo": "proxies",
    "create_proxy": "proxies",
    "describe_proxy": "proxies",
    "issue_proxy": "proxies",
    "load_credential": "proxies",
    "write_proxy": "proxies",
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
    """The lazily importable names, so ``dir()`` and completion see them."""
    return sorted(_MODULES)


__all__ = [
    "AES",
    "IS_ACCELERATED",
    "Blowfish",
    "Certificate",
    "Checksum",
    "DERError",
    "Name",
    "ProxyCredential",
    "ProxyInfo",
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
    "check_vomses",
    "checksum_bytes",
    "checksum_file",
    "crc32c",
    "crc64",
    "crc64nvme",
    "create_proxy",
    "describe_proxy",
    "inspect_voms",
    "is_signed",
    "issue_proxy",
    "load_certificates",
    "load_credential",
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
    "write_proxy",
]
