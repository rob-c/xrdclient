"""Rebased dependency boundaries, without KDCs or ambient credentials."""

from __future__ import annotations

import builtins
import pathlib
import runpy
import tomllib
import warnings

import pytest
from packaging.requirements import Requirement
from xrdsec.krb5 import gss

from xrdclient.auth import krb5
from xrdclient.auth.base import Offer
from xrdclient.auth.ztn import token_claims
from xrdclient.config import Config
from xrdclient.crypto import blowfish, der, ed25519, p256
from xrdclient.errors import CredentialError


@pytest.mark.parametrize("cache", ["API:user", "MEMORY:user"])
def test_a_platform_only_cache_gets_its_ap_req_from_xrdsecs_gss_layer(monkeypatch, cache):
    """``API:``/``MEMORY:`` go to the system library; the raw AP-REQ is framed as ``krb5``."""
    asked: list[str] = []

    def initiator_ap_req(principal: str, *, backend: str | None = None) -> bytes:
        asked.append(principal)
        return b"\x6e\x02\x30\x00"

    monkeypatch.setattr(gss, "load_backend", lambda name=None, **_: None)
    monkeypatch.setattr(gss, "initiator_ap_req", initiator_ap_req)
    monkeypatch.setenv("KRB5CCNAME", cache)
    offer = Offer("krb5", "xrootd/server@EXAMPLE")
    credential = krb5.KerberosCredential.available(offer, Config(), username="", host="server")
    assert credential is not None
    assert credential.initial() == b"krb5\0\x6e\x02\x30\x00"
    assert asked == ["xrootd/server@EXAMPLE"]


def test_native_cache_forwarding_is_explicitly_unsupported(monkeypatch):
    monkeypatch.setenv("KRB5CCNAME", "API:user")
    with pytest.raises(CredentialError, match=r"kinit -f.*FILE"):
        krb5.KerberosCredential.available(
            Offer("krb5", "xrootd/server@EXAMPLE,fwd"), Config(), username="", host="server"
        )


def test_cryptography_45_blowfish_import_compatibility(monkeypatch):
    original = builtins.__import__

    def older_import(name, *args, **kwargs):
        if name == "cryptography.hazmat.decrepit.ciphers.modes":
            raise ImportError("cryptography 45/46")
        return original(name, *args, **kwargs)

    with monkeypatch.context() as patcher, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        patcher.setattr(builtins, "__import__", older_import)
        loaded = runpy.run_path(blowfish.__file__)
    cipher = loaded["Blowfish"](b"k")
    assert cipher.decrypt_cfb64(bytes(8), cipher.encrypt_cfb64(bytes(8), b"data")) == b"data"


def test_dependency_rejection_boundaries():
    public = ed25519.public_key(bytes(32))
    assert not ed25519.verify(b"short", b"", ed25519.sign(bytes(32), b""))
    assert not ed25519.verify(public, b"", bytes(64))
    assert not p256.verify((0, 0), b"message", 1, 1)
    with pytest.raises(der.DERError):
        der.parse(b"\x02\x01\x01", -1)
    with pytest.raises(der.DERError):
        der.encode_oid("9.invalid")
    assert token_claims("opaque") == {}


#: The project file wheels are built from. Read rather than the installed
#: metadata, which belongs to whichever checkout was last ``pip install``-ed
#: and so can describe a different tree from the one under test.
PROJECT = tomllib.loads((pathlib.Path(__file__).parents[1] / "pyproject.toml").read_text())


def _names(requirements: list[str]) -> set[str]:
    return {Requirement(value).name.lower() for value in requirements}


def test_required_dependencies_are_portable():
    assert _names(PROJECT["project"]["dependencies"]) == {
        "cryptography",
        "xrdcore",
        # botocore comes with xrdhttp, the only package that imports it.
        "xrdhttp",
        # asn1crypto comes with xrdsec, which alone imports it now.
        "xrdsec",
    }


def test_native_kerberos_bindings_are_in_the_optional_extra():
    """python-gssapi comes with xrdsec's ``krb5`` extra, where the GSS layer is."""
    (requirement,) = map(Requirement, PROJECT["project"]["optional-dependencies"]["krb5"])
    assert (requirement.name, requirement.extras) == ("xrdsec", {"krb5"})


def test_blowfish_decryption_checks_iv_size():
    with pytest.raises(ValueError, match="IV must be 8 bytes"):
        blowfish.Blowfish(b"k").decrypt_cfb64(b"short", b"")
