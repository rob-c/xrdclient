"""xrdclient on xrdsec: the old names, the Config-shaped adapters, and the error boundary.

Since 0.4.0 the grid security code - certificates, proxies, delegation, VOMS,
credential discovery, tokens, TLS contexts, the test PKI - is xrdsec's, and
xrdsec's own suite is where its behaviour is tested. What this file pins is
the part xrdclient owns:

* every public pre-0.4.0 module name still imports, and is xrdsec's module
  object itself (so a patch, an ``isinstance`` or an ``except`` reaches what
  the code uses), except :mod:`xrdclient.crypto.x509`, a module of its own
  that keeps the Config-shaped ``default_proxy_path``; the private ones
  (``crypto._voms``, ``crypto._proxyfiles``, ``crypto._proxyinfo``,
  ``testing._https``, ``_replies``, ``_keys``, ``_secure_files``) are gone;
* the adapters that turn a :class:`~xrdclient.Config` into xrdsec's
  arguments (``_gridpaths``, ``_tokens``, ``_usercred``,
  ``transport.base.tls_context``) give the same answers as xrdsec does;
* the dependency direction: xrdsec never imports a client.
"""

from __future__ import annotations

import importlib
import os
import pathlib

import pytest
import xrdsec
import xrdsec.x509

from xrdclient import _gridpaths, _tokens, crypto, errors
from xrdclient.config import Config
from xrdclient.errors import InsecureCredentialError

#: Every xrdclient module name that is an xrdsec module, and which one.
ALIASES = [
    ("xrdclient.credentials", "xrdsec.credentials"),
    ("xrdclient.tls", "xrdsec.tls"),
    ("xrdclient.voms_client", "xrdsec.voms_client"),
    ("xrdclient.crypto.der", "xrdsec.der"),
    ("xrdclient.crypto.rsa", "xrdsec.rsa"),
    ("xrdclient.crypto.aes", "xrdsec.aes"),
    ("xrdclient.crypto.p256", "xrdsec.p256"),
    ("xrdclient.crypto.ed25519", "xrdsec.ed25519"),
    ("xrdclient.crypto.signatures", "xrdsec.signatures"),
    ("xrdclient.crypto.proxies", "xrdsec.proxies"),
    ("xrdclient.crypto.delegation", "xrdsec.delegation"),
    ("xrdclient.crypto.userproxy", "xrdsec.userproxy"),
    ("xrdclient.crypto.trust", "xrdsec.trust"),
    ("xrdclient.crypto.voms", "xrdsec.voms"),
    ("xrdclient.testing.pki", "xrdsec.testing.pki"),
    ("xrdclient.testing.voms", "xrdsec.testing.voms"),
]

#: Pre-0.4.0 private names removed in 0.4.0: nothing imports them any more.
REMOVED = [
    "xrdclient.crypto._proxyfiles",
    "xrdclient.crypto._proxyinfo",
    "xrdclient.crypto._voms",
    "xrdclient.testing._https",
    "xrdclient.testing._replies",
    "xrdclient.testing._keys",
    "xrdclient._secure_files",
]


def _private(path: pathlib.Path, data: bytes | str = b"secret", mode: int = 0o600) -> str:
    """Write ``data`` at ``path`` with ``mode``; return the path as text."""
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    path.chmod(mode)
    return str(path)


# ---------------------------------------------------------------------------
# The names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("old", "new"), ALIASES)
def test_the_old_name_is_xrdsecs_module(old, new):
    old_module, new_module = importlib.import_module(old), importlib.import_module(new)
    assert old_module is new_module
    assert set(getattr(new_module, "__all__", ())) <= set(dir(old_module))


@pytest.mark.parametrize("old", REMOVED)
def test_a_removed_private_name_is_gone(old):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(old)


#: Public ``xrdclient.crypto.x509`` names that are not in xrdsec's ``__all__``
#: (the decoder), and the xrdsec object each must be.
OLD_X509_NAMES = [
    ("certificate_data", "certificate_data"),
    ("CertificateData", "CertificateData"),
]

#: The pre-0.4.0 private names ``xrdclient.crypto.x509`` no longer carries.
REMOVED_X509_NAMES = [
    "_decode_name",
    "_extensions",
    "_policy_language",
    "_parse_certificate",
    "_canonical_value",
    "_certificate_key",
    "_legacy_certificate_data",
]


def test_x509_re_exports_xrdsecs_public_objects():
    from xrdclient.crypto import x509

    shared = [name for name in xrdsec.x509.__all__ if name != "default_proxy_path"]
    assert [name for name in shared if getattr(x509, name) is not getattr(xrdsec.x509, name)] == []


@pytest.mark.parametrize(("old", "new"), OLD_X509_NAMES)
def test_x509_keeps_the_old_names_as_xrdsecs_objects(old, new):
    from xrdclient.crypto import x509

    assert getattr(x509, old) is getattr(xrdsec.x509, new)


def test_x509_no_longer_carries_the_private_names():
    from xrdclient.crypto import x509

    assert [name for name in REMOVED_X509_NAMES if hasattr(x509, name)] == []


def test_x509s_default_proxy_path_still_takes_a_config(monkeypatch, tmp_path):
    from xrdclient.crypto import x509

    monkeypatch.setenv("X509_USER_PROXY", "/tmp/from-env")
    assert x509.default_proxy_path(Config(proxy=str(tmp_path / "cfg.pem"))) == str(
        tmp_path / "cfg.pem"
    )
    assert x509.default_proxy_path() == xrdsec.x509.default_proxy_path() == "/tmp/from-env"


def test_the_lazy_crypto_names_are_xrdsecs_objects():
    assert crypto.validate_voms is xrdsec.voms.validate_voms
    assert crypto.inspect_voms is xrdsec.voms.inspect_voms
    assert crypto.VOMSStatus is xrdsec.voms.VOMSStatus
    assert crypto.ProxyCredential is xrdsec.x509.ProxyCredential
    assert crypto.DERError is xrdsec.der.DERError


def test_the_test_https_server_is_xrdsecs_and_the_dav_fake_builds_on_it():
    import xrdsec.testing.https

    from xrdclient.testing import FakeDAVServer, FakeHTTPSServer
    from xrdclient.testing import http as testing_http

    assert FakeHTTPSServer is testing_http.FakeHTTPSServer is xrdsec.testing.https.FakeHTTPSServer
    assert testing_http.TLSServer is xrdsec.testing.https.TLSServer
    assert FakeDAVServer.__module__ == "xrdclient.testing.http"


def test_xrdsec_never_imports_a_client():
    """The dependency is one-way; a library importing a client would make a cycle."""
    source = pathlib.Path(xrdsec.__file__).parent
    for path in source.rglob("*.py"):
        text = path.read_text()
        for client in ("xrdclient", "xgfalclient", "xrdarc", "xrdhttp"):
            assert f"import {client}" not in text and f"from {client}" not in text, path


def test_xrdclient_has_no_reverse_dependency():
    from importlib.metadata import requires

    from packaging.requirements import Requirement

    dependencies = {Requirement(value).name.lower() for value in requires("xrdclient") or ()}
    assert "xgfalclient" not in dependencies


# ---------------------------------------------------------------------------
# Errors at the boundary
# ---------------------------------------------------------------------------


def test_xrdsecs_credential_errors_are_xrdclients():
    """VOMS failures and insecure files are XRootDErrors already: nothing to translate."""
    from xrdsec.voms_client import VOMSError, VOMSRefusedError

    assert issubclass(VOMSRefusedError, VOMSError)
    assert issubclass(VOMSError, errors.CredentialError)
    assert issubclass(VOMSError, errors.XRootDError)
    assert issubclass(InsecureCredentialError, errors.CredentialError)


def test_xrdsecs_parse_errors_stay_valueerrors():
    """A malformed certificate or a refused server is a ``ValueError`` in xrdsec.

    The GSI login turns each into an :class:`~xrdclient.errors.AuthenticationError`
    or :class:`~xrdclient.errors.CredentialError` where it calls xrdsec
    (:mod:`xrdsec.gsi.secgsi`), because only the login knows which server and
    which step a failure belongs to.
    """
    from xrdsec.delegation import DelegationError
    from xrdsec.der import DERError
    from xrdsec.proxies import ProxyError
    from xrdsec.trust import TrustError

    for error in (DERError, DelegationError, ProxyError, TrustError):
        assert issubclass(error, ValueError)
        assert not issubclass(error, errors.XRootDError)


# ---------------------------------------------------------------------------
# The Config-shaped adapters
# ---------------------------------------------------------------------------


def test_the_config_shaped_proxy_path_still_prefers_the_config(monkeypatch):
    monkeypatch.setenv("X509_USER_PROXY", "/from/env")
    assert _gridpaths.default_proxy_path(Config(proxy="/from/config")) == "/from/config"
    assert _gridpaths.default_proxy_path() == "/from/env"


def test_the_config_shaped_search_is_the_same_search(tmp_path, monkeypatch):
    """``_tokens`` keeps its own ``TMP_DIR``, the attribute xgfalclient's suite patches."""
    tmp = tmp_path / "tokens-tmp"
    tmp.mkdir()
    monkeypatch.setattr(_tokens, "TMP_DIR", str(tmp))
    for name in ("BEARER_TOKEN", "BEARER_TOKEN_FILE", "XDG_RUNTIME_DIR"):
        monkeypatch.delenv(name, raising=False)
    _private(tmp / f"bt_u{os.geteuid()}", "from-tmp")
    assert _tokens.discover_token(Config(token=None, token_file=None)) == "from-tmp"
    assert _tokens.token_files(Config(token_file=None))[-1] == (
        str(tmp / f"bt_u{os.geteuid()}"),
        True,
    )


def test_a_url_token_comes_before_the_config(monkeypatch):
    from xrdclient.url import parse

    monkeypatch.setenv("BEARER_TOKEN", "from-env")
    url = parse("davs://se.example//f?authz=Bearer%20from-url")
    assert _tokens.url_token(url) == "from-url"
    assert _tokens.discover_token(Config(token="configured"), url) == "from-url"
    assert _tokens.discover_token(Config(token="configured")) == "configured"


def test_the_config_form_is_the_same_builder(tmp_path):
    """``tls_context(Config)`` and xrdsec's ``client_context`` share their contexts."""
    from xrdclient import tls
    from xrdclient.testing.pki import create_pki
    from xrdclient.transport import base

    pki = create_pki(tmp_path / "pki")
    proxy = str(pki.proxy(path=tmp_path / "proxy.pem"))
    config = Config(proxy=proxy, ca_path=str(tmp_path), ca_file=None, check_crl="")
    assert base.tls_context(config) is tls.client_context(cert=proxy, ca_path=tmp_path)
    assert base.client_context is xrdsec.tls.client_context
    assert base.clear_tls_cache is xrdsec.tls.clear_cache
    anonymous = Config(proxy=None, ca_path=str(tmp_path), ca_file=None, check_crl="")
    # The Config form presents the default proxy, so it is a different setup.
    assert base.tls_context(anonymous) is tls.client_context(ca_path=tmp_path, present_default=True)
    assert base.tls_context(anonymous) is not tls.client_context(ca_path=tmp_path)


def test_present_loads_the_configs_pair_and_asks_the_configs_password(tmp_path):
    """``_usercred.present(ctx, config)``: xrdsec's pipe, with ``Config.key_password``."""
    from cryptography.hazmat.primitives import serialization

    from xrdclient import _usercred
    from xrdclient.testing.pki import create_pki

    pki = create_pki(tmp_path / "pki")
    cert = tmp_path / "usercert.pem"
    cert.write_bytes(pki.user.public_bytes(serialization.Encoding.PEM))
    key = tmp_path / "userkey.pem"
    encryption = serialization.BestAvailableEncryption(b"s3cret")
    key.write_bytes(
        pki.user_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
        )
    )
    key.chmod(0o600)
    asked: list[int] = []

    def key_password() -> str:
        asked.append(1)
        return "s3cret"

    config = Config(
        proxy=None,
        usercert=str(cert),
        userkey=str(key),
        prompt=False,
        key_password=key_password,
    )
    _usercred.present(pki.client_context(anonymous=True), config)
    assert asked == [1]
    with pytest.raises(errors.CredentialError, match=r"Config\(key_password=\.\.\.\)"):
        _usercred.present(
            pki.client_context(anonymous=True),
            Config(proxy=None, usercert=str(cert), userkey=str(key), prompt=False),
        )
