"""A user certificate and key kept apart: ``Config.usercert`` / ``Config.userkey``.

The pair a robot account or a CI job has instead of a proxy. HTTPS presents
it as the client certificate, GSI logs in with a proxy made from it in
memory, and neither writes anything to disk - the point being that nobody
has to splice the private key into a temporary file first.
"""

from __future__ import annotations

import datetime
import os
import ssl

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from xrdclient import _usercred
from xrdclient.auth import credential_files
from xrdclient.auth.base import Offer
from xrdclient.auth.gsi import GSICredential
from xrdclient.config import Config, crl_mode
from xrdclient.crypto.rsa import RSAPrivateKey
from xrdclient.crypto.userproxy import user_proxy
from xrdclient.crypto.x509 import load_certificates, verify_issued
from xrdclient.errors import CredentialError, InsecureCredentialError
from xrdclient.http.client import HTTPClient
from xrdclient.testing import FakeDAVServer
from xrdclient.testing.pki import PKI
from xrdclient.transport.base import clear_tls_cache, tls_context
from xrdclient.url import parse

NOW = datetime.datetime.now(datetime.UTC)


@pytest.fixture(scope="module")
def user_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _user_certificate(pki: PKI, key, *, days: float = 1) -> x509.Certificate:
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Robot Account")]))
        .issuer_name(pki.ca.subject)
        .public_key(key.public_key())
        .serial_number(77)
        .not_valid_before(NOW - datetime.timedelta(hours=1))
        .not_valid_after(NOW + datetime.timedelta(days=days))
        .add_extension(
            x509.KeyUsage(True, False, True, False, False, False, False, False, False), True
        )
        .sign(pki.ca_key, hashes.SHA256())
    )


def _write(path, data: bytes, mode: int = 0o600) -> str:
    path.write_bytes(data)
    os.chmod(path, mode)
    return str(path)


@pytest.fixture
def pair(tmp_path, pki, user_key):
    """``usercert.pem`` and a private ``userkey.pem``, as a grid user keeps them."""
    cert = _write(
        tmp_path / "usercert.pem",
        _user_certificate(pki, user_key).public_bytes(serialization.Encoding.PEM),
        0o644,
    )
    key = _write(
        tmp_path / "userkey.pem",
        user_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
    )
    return cert, key


def _encrypted_key(tmp_path, user_key, passphrase: bytes, *, pkcs8: bool = True) -> str:
    form = (
        serialization.PrivateFormat.PKCS8
        if pkcs8
        else serialization.PrivateFormat.TraditionalOpenSSL
    )
    return _write(
        tmp_path / "locked.pem",
        user_key.private_bytes(
            serialization.Encoding.PEM, form, serialization.BestAvailableEncryption(passphrase)
        ),
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("X509_USER_PROXY", "X509_USER_CERT", "X509_USER_KEY"):
        monkeypatch.delenv(name, raising=False)
    clear_tls_cache()
    yield
    clear_tls_cache()


def _config(cert: str, key: str, **changes) -> Config:
    return Config(proxy=None, usercert=cert, userkey=key, prompt=False, **changes)


# ---------------------------------------------------------------------------
# TLS client authentication
# ---------------------------------------------------------------------------


def test_https_presents_the_user_certificate(pki, pair, tmp_path):
    cert, key = pair
    tls = pki.server_context()  # demands, and verifies, a client certificate
    with FakeDAVServer(files={"/f": b"x"}, tls=tls) as srv:
        config = _config(cert, key, ca_path=str(pki.ca_dir), ca_file=None)
        with HTTPClient(config) as client:
            client.request("GET", parse(f"davs://localhost:{srv.address[1]}/f"))
        (presented,) = srv.peer_certificates
    assert x509.load_der_x509_certificate(presented).subject.rfc4514_string() == (
        "CN=Robot Account"
    )
    assert not list(tmp_path.glob("tmp*"))  # nothing was spliced together on disk


@pytest.mark.parametrize("pkcs8", [True, False], ids=["pkcs8", "openssl"])
def test_an_encrypted_key_is_unlocked_by_its_pass_phrase(pair, tmp_path, user_key, pkcs8):
    cert, _ = pair
    key = _encrypted_key(tmp_path, user_key, b"sesame", pkcs8=pkcs8)
    asked: list[int] = []

    def password() -> str:
        asked.append(1)
        return "sesame"

    context = tls_context(_config(cert, key, key_password=password))
    assert isinstance(context, ssl.SSLContext)
    assert asked == [1]


def test_an_encrypted_key_without_a_pass_phrase_is_refused(pair, tmp_path, user_key):
    cert, _ = pair
    key = _encrypted_key(tmp_path, user_key, b"sesame")
    with pytest.raises(CredentialError, match="is encrypted and no pass phrase"):
        tls_context(_config(cert, key))
    with pytest.raises(CredentialError, match=r"cannot use the X\.509 certificate"):
        tls_context(_config(cert, key, key_password=lambda: b"wrong"))


def test_a_person_is_asked_for_the_pass_phrase_when_there_is_one(pair, tmp_path, user_key):
    from xrdclient.auth import prompt

    cert, _ = pair
    key = _encrypted_key(tmp_path, user_key, b"sesame")
    asks = []

    def prompter(ask):
        asks.append(ask)
        return "sesame"

    try:
        config = Config(proxy=None, usercert=cert, userkey=key, prompt=True, prompter=prompter)
        tls_context(config)
    finally:
        prompt.forget()
    (ask,) = asks
    assert ask.secret and key in ask.reason


def test_a_key_others_can_read_is_refused(pair, tmp_path):
    cert, key = pair
    os.chmod(key, 0o644)
    with pytest.raises(InsecureCredentialError, match="chmod 600"):
        tls_context(_config(cert, key))


def test_a_certificate_that_is_not_one_is_reported_naming_both_files(pair, tmp_path):
    _, key = pair
    junk = _write(tmp_path / "junk.pem", b"not a certificate\n", 0o644)
    with pytest.raises(CredentialError, match=f"certificate {junk} with the key {key}"):
        tls_context(_config(junk, key))


def test_without_a_descriptor_directory_the_checked_file_is_named(pair, monkeypatch):
    monkeypatch.setattr("xrdsec.usercred._FD_DIR", "/nonexistent-fd-directory")
    cert, key = pair
    assert isinstance(tls_context(_config(cert, key)), ssl.SSLContext)


def test_a_named_proxy_wins_over_the_pair(pair, tmp_path, pki):
    cert, key = pair
    proxy = str(pki.proxy_path)
    config = Config(proxy=proxy, usercert=cert, userkey=key)
    assert not _usercred.wanted(config)
    assert isinstance(tls_context(config), ssl.SSLContext)


def test_the_pair_comes_from_the_grid_environment(pair, monkeypatch):
    cert, key = pair
    monkeypatch.setenv("X509_USER_CERT", cert)
    monkeypatch.setenv("X509_USER_KEY", key)
    config = Config()
    assert (config.usercert, config.userkey) == (cert, key)
    assert _usercred.wanted(config)
    assert set(credential_files(config)) >= {cert, key}


def test_a_renewed_certificate_is_a_new_context(pair, tmp_path, pki, user_key):
    cert, key = pair
    first = tls_context(_config(cert, key))
    assert tls_context(_config(cert, key)) is first
    renewed = _user_certificate(pki, user_key, days=2).public_bytes(serialization.Encoding.PEM)
    _write(tmp_path / "usercert.pem", renewed, 0o644)
    os.utime(cert, ns=(1, 1))
    assert tls_context(_config(cert, key)) is not first


def test_a_pass_phrase_callable_cannot_come_from_a_file(tmp_path):
    ini = tmp_path / "config.ini"
    ini.write_text("[defaults]\nkey_password = hunter2\n")
    with pytest.raises(ValueError, match="key_password cannot be set in a file"):
        Config.from_file(ini)


# ---------------------------------------------------------------------------
# GSI: a proxy made in memory
# ---------------------------------------------------------------------------


def test_gsi_logs_in_with_a_proxy_made_from_the_pair(pair):
    cert, key = pair
    credential = GSICredential.available(
        Offer("gsi", "v:10600,c:ssl,ca:abcdef01"),
        _config(cert, key),
        username="robot",
        host="localhost",
    )
    assert credential is not None
    proxy, user = credential.proxy.chain[:2]
    assert proxy.is_proxy and verify_issued(proxy, user)
    assert credential.identity == "/CN=Robot Account"  # the proxy CN stripped
    assert isinstance(credential.proxy.key, RSAPrivateKey)
    assert proxy.not_after <= user.not_after
    assert (
        GSICredential.missing(Offer("gsi", ""), _config(cert, key), username="", host="h") is None
    )


def test_gsi_with_a_locked_key_says_why(pair, tmp_path, user_key, caplog):
    cert, _ = pair
    key = _encrypted_key(tmp_path, user_key, b"sesame")
    config = _config(cert, key)
    assert GSICredential.available(Offer("gsi", ""), config, username="", host="h") is None
    assert "not using the X.509 certificate" in caplog.text
    ask = GSICredential.missing(Offer("gsi", ""), config, username="", host="h")
    assert ask is not None and "encrypted" in ask.reason
    unlocked = _config(cert, key, key_password=lambda: "sesame")
    assert GSICredential.available(Offer("gsi", ""), unlocked, username="", host="h")


def test_gsi_with_a_missing_certificate_has_nothing_to_offer(pair, tmp_path):
    _, key = pair
    config = _config(str(tmp_path / "nowhere.pem"), key)
    assert GSICredential.available(Offer("gsi", ""), config, username="", host="h") is None
    empty = _write(tmp_path / "empty.pem", b"", 0o644)
    assert (
        GSICredential.available(Offer("gsi", ""), _config(empty, key), username="", host="h")
        is None
    )


def test_a_proxy_needs_a_live_user_certificate(pair, pki, user_key):
    cert, _ = pair
    with open(cert, "rb") as handle:
        chain = load_certificates(handle.read())
    signer = RSAPrivateKey.from_cryptography(user_key)
    with pytest.raises(ValueError, match="needs the user certificate"):
        user_proxy([], signer)
    with pytest.raises(ValueError, match="has expired"):
        user_proxy(chain, signer, now=chain[0].not_after + 1)
    short = user_proxy(chain, signer, hours=0.5, bits=1024)
    assert short.chain[0].not_after - short.chain[0].not_before <= 0.5 * 3600 + 300


# ---------------------------------------------------------------------------
# The TLS settings that used to be environment-only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "mode"),
    [("", ""), ("0", ""), ("no", ""), ("1", "leaf"), ("leaf", "leaf"), ("Chain", "chain")],
)
def test_the_crl_vocabulary_is_xrd_checkcrls(raw, mode):
    assert crl_mode(raw) == mode


def test_the_tls_settings_default_from_the_environment(monkeypatch):
    monkeypatch.setenv("XRD_TLSSYSTEMCA", "0")
    monkeypatch.setenv("XRD_CHECKCRL", "chain")
    config = Config()
    assert config.tls_system_ca is False and config.check_crl == "chain"
    monkeypatch.delenv("XRD_TLSSYSTEMCA")
    monkeypatch.delenv("XRD_CHECKCRL")
    assert Config().tls_system_ca is None and Config().check_crl == ""


def test_the_tls_settings_reach_the_context(tmp_path):
    checked = tls_context(Config(ca_path=str(tmp_path), check_crl="chain"))
    assert checked.verify_flags & ssl.VERIFY_CRL_CHECK_CHAIN
    leaf = tls_context(Config(ca_path=str(tmp_path), check_crl="yes"))
    assert leaf.verify_flags & ssl.VERIFY_CRL_CHECK_LEAF
    plain = tls_context(Config(ca_path=str(tmp_path), check_crl=""))
    assert not plain.verify_flags & (ssl.VERIFY_CRL_CHECK_LEAF | ssl.VERIFY_CRL_CHECK_CHAIN)
    without = tls_context(Config(ca_path=str(tmp_path)), system_ca=True)
    forced = tls_context(Config(ca_path=str(tmp_path), tls_system_ca=False), system_ca=True)
    assert forced is not without  # the setting overrode the caller
