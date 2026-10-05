"""Rebased dependency boundaries, without KDCs or ambient credentials."""

from __future__ import annotations

import builtins
import runpy
import sys
import warnings
from importlib.metadata import requires
from types import SimpleNamespace

import pytest
from packaging.requirements import Requirement

from xrdclient.auth import krb5
from xrdclient.auth.base import Offer
from xrdclient.auth.ztn import token_claims
from xrdclient.config import Config
from xrdclient.crypto import blowfish, der, ed25519, p256
from xrdclient.errors import CredentialError


def _token(
    wrapper=0x60, mechanism="1.2.840.113554.1.2.2", prefix=b"\x01\x00", request=b"\x6e\x02\x30\x00"
):
    return der.encode(wrapper, der.encode_oid(mechanism) + prefix + request)


@pytest.mark.parametrize("cache", ["API:user", "MEMORY:user"])
def test_native_cache_retains_xrootd_ap_req(monkeypatch, cache):
    calls = {}

    def credentials(**kwargs):
        calls["credentials"] = kwargs
        return object()

    def context(**kwargs):
        calls["context"] = kwargs
        return SimpleNamespace(step=lambda: _token())

    module = SimpleNamespace(
        Credentials=credentials,
        SecurityContext=context,
        Name=lambda principal, **kwargs: principal,
        NameType=SimpleNamespace(kerberos_principal="principal"),
        MechType=SimpleNamespace(kerberos="kerberos"),
        exceptions=SimpleNamespace(GSSError=RuntimeError),
    )
    monkeypatch.setitem(sys.modules, "gssapi", module)
    monkeypatch.setenv("KRB5CCNAME", cache)
    offer = Offer("krb5", "xrootd/server@EXAMPLE")
    credential = krb5.KerberosCredential.available(offer, Config(), username="", host="server")
    assert credential is not None
    assert credential.initial() == b"krb5\0\x6e\x02\x30\x00"
    assert calls["credentials"] == {"usage": "initiate", "mechs": ["kerberos"]}
    assert calls["context"]["flags"] == 0  # no unsolicited delegation
    assert calls["context"]["name"] == "xrootd/server@EXAMPLE"


def test_native_cache_forwarding_is_explicitly_unsupported(monkeypatch):
    monkeypatch.setenv("KRB5CCNAME", "API:user")
    with pytest.raises(CredentialError, match=r"kinit -f.*FILE"):
        krb5.KerberosCredential.available(
            Offer("krb5", "xrootd/server@EXAMPLE,fwd"), Config(), username="", host="server"
        )


@pytest.mark.parametrize(
    "token",
    [
        b"",
        b"bad",
        _token(wrapper=0x30),
        _token(mechanism="1.2.3"),
        _token(prefix=b"\0\0"),
        _token(request=b"\x30\0"),
        _token(request=b"\x6e\0x"),
        _token() + b"x",
    ],
)
def test_native_gss_headers_are_checked(token):
    with pytest.raises(CredentialError, match="malformed AP-REQ"):
        krb5._unwrap_ap_req(token)


@pytest.mark.parametrize("error", [RuntimeError("no ticket"), NotImplementedError("no store")])
def test_native_cache_errors_are_actionable(monkeypatch, error):
    def credentials(**kwargs):
        raise error

    module = SimpleNamespace(
        Credentials=credentials,
        MechType=SimpleNamespace(kerberos="kerberos"),
        exceptions=SimpleNamespace(GSSError=RuntimeError),
    )
    monkeypatch.setitem(sys.modules, "gssapi", module)
    with pytest.raises(CredentialError, match=r"API:user.*Run kinit"):
        krb5._native_ap_req("xrootd/server", "API:user")


def test_native_empty_token_is_not_accepted(monkeypatch):
    module = SimpleNamespace(
        Credentials=lambda **kwargs: None,
        SecurityContext=lambda **kwargs: SimpleNamespace(step=lambda: None),
        Name=lambda *args, **kwargs: None,
        NameType=SimpleNamespace(kerberos_principal="principal"),
        MechType=SimpleNamespace(kerberos="kerberos"),
        exceptions=SimpleNamespace(GSSError=RuntimeError),
    )
    monkeypatch.setitem(sys.modules, "gssapi", module)
    with pytest.raises(CredentialError, match="malformed"):
        krb5._native_ap_req("xrootd/server", "API:user")


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
    assert loaded["Blowfish"](b"k").decrypt_ecb(
        loaded["Blowfish"](b"k").encrypt_ecb(bytes(8))
    ) == bytes(8)


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


def test_required_dependencies_are_portable():
    runtime = {
        Requirement(value).name.lower()
        for value in requires("xrdclient") or []
        if Requirement(value).marker is None or Requirement(value).marker.evaluate({"extra": ""})
    }
    assert runtime == {
        "asn1crypto",
        "botocore",
        "cryptography",
        "pyjwt",
        "urllib3",
    }


def test_native_kerberos_bindings_are_in_the_optional_extra():
    optional = {
        Requirement(value).name.lower()
        for value in requires("xrdclient") or []
        if Requirement(value).marker is not None
        and Requirement(value).marker.evaluate({"extra": "krb5"})
    }
    assert optional == {"gssapi", "krb5"}


def test_blowfish_decryption_checks_iv_size():
    with pytest.raises(ValueError, match="IV must be 8 bytes"):
        blowfish.Blowfish(b"k").decrypt_cfb64(b"short", b"")
