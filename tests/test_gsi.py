"""GSI as an xrdclient mechanism: where the proxy comes from, configuration, the ladder.

The XrdSecgsi protocol itself - bucket framing, Diffie-Hellman, the signed
exchange, delegation, the server check - moved to :mod:`xrdsec.gsi.secgsi` in
0.4.0 and is tested in xrdsec's suite (``tests/test_gsi_secgsi.py``, these
tests' former home). What stays here is xrdclient's adapter: the names it
re-exports, :meth:`GSICredential.available` and its refusals, the
configuration it reads, and one login played end to end against
:class:`xrdsec.testing.gsi.SecgsiServer`. The real server has the last word in
``test_gsi_delegation_interop.py``.
"""

import dataclasses
import time
from pathlib import Path
from typing import TypeVar

import pytest
import xrdsec.gsi.secgsi
from xrdsec.gsi.secgsi import (
    BUCKET_ISSUER_HASH,
    STEP_CLIENT_CERT,
    Delegation,
    SecgsiClient,
    ServerCheck,
    decode_message,
    find_bucket,
)
from xrdsec.testing.gsi import SecgsiServer
from xrdsec.testing.rawpki import (
    CA_CONSTRAINTS,
    DH_GENERATOR,
    DH_PRIME,
    make_certificate_with,
    name,
    pem,
    proxy_chain,
    throwaway_key,
)

from xrdclient.auth import gsi, registry, select
from xrdclient.auth.base import Offer
from xrdclient.auth.gsi import GSICredential
from xrdclient.config import Config
from xrdclient.crypto import load_proxy
from xrdclient.errors import CredentialError

OFFER = Offer("gsi", "v:10400,c:ssl,ca:1a2b3c4d.0")

_T = TypeVar("_T")


def _present(value: _T | None) -> _T:
    """``value``, which the test expects to be there."""
    assert value is not None
    return value


def write_private(path: Path, data: bytes) -> Path:
    """A credential file as ``voms-proxy-init`` leaves one: mode 0600."""
    path.write_bytes(data)
    path.chmod(0o600)
    return path


@pytest.fixture(scope="module")
def key():
    return throwaway_key(0)


@pytest.fixture
def proxy(tmp_path_factory, key):
    path = tmp_path_factory.mktemp("gsi") / "x509up_u1000"
    write_private(path, proxy_chain(key))
    return load_proxy(str(path))


# -- the names ------------------------------------------------------------------


def test_the_protocol_names_are_xrdsecs_objects():
    """Code written against ``xrdclient.auth.gsi`` gets the library's objects."""
    for name_ in xrdsec.gsi.secgsi.__all__:
        assert getattr(gsi, name_) is getattr(xrdsec.gsi.secgsi, name_), name_
    assert set(gsi.__all__) == {*xrdsec.gsi.secgsi.__all__, "GSICredential"}
    assert issubclass(GSICredential, SecgsiClient)


def test_gsi_is_registered_with_no_extra_installed():
    """It is pure Python, so it is always there."""
    assert registry()["gsi"] is GSICredential


def test_the_identity_is_the_human_behind_the_proxy(proxy):
    credential = GSICredential(proxy)
    assert credential.identity == "/DC=org/DC=example/CN=Jane Doe"
    assert repr(credential) == "GSICredential(identity='/DC=org/DC=example/CN=Jane Doe')"


# -- available() ------------------------------------------------------------------


def test_available_finds_the_proxy_the_environment_points_at(monkeypatch, tmp_path, key):
    path = tmp_path / "x509up_u1000"
    write_private(path, proxy_chain(key))
    monkeypatch.setenv("X509_USER_PROXY", str(path))
    credential = GSICredential.available(OFFER, Config(), username="jane", host="srv")
    assert isinstance(credential, GSICredential)
    assert credential.cryptomod == "ssl"
    # Our CA's names (``openssl x509 -issuer_hash``/``-issuer_hash_old`` of the
    # user certificate), not the offer's ``ca:``, which is the server's own.
    assert credential.issuer_hash == "33027a7e.0|b5f5da31.0"


def test_available_returns_none_when_there_is_nothing_to_use(monkeypatch, tmp_path, key):
    monkeypatch.setenv("X509_USER_PROXY", str(tmp_path / "absent.pem"))
    assert GSICredential.available(OFFER, Config(), username="j", host="h") is None

    junk = tmp_path / "junk.pem"
    write_private(junk, b"not a proxy")
    monkeypatch.setenv("X509_USER_PROXY", str(junk))
    assert GSICredential.available(OFFER, Config(), username="j", host="h") is None

    stale = tmp_path / "stale.pem"
    write_private(stale, proxy_chain(key, not_after=time.time() - 60))
    monkeypatch.setenv("X509_USER_PROXY", str(stale))
    assert GSICredential.available(OFFER, Config(), username="j", host="h") is None


def test_the_ladder_prefers_gsi_when_a_proxy_exists(monkeypatch, tmp_path, key):
    path = tmp_path / "x509up_u1000"
    write_private(path, proxy_chain(key))
    monkeypatch.setenv("X509_USER_PROXY", str(path))
    chosen = next(select("&P=gsi,v:10400,c:ssl&P=unix", Config(), username="jane", host="srv"))
    assert isinstance(chosen, GSICredential)


def test_available_passes_over_a_proxy_whose_key_is_not_rsa(monkeypatch, tmp_path, key, proxy):
    path = tmp_path / "x509up_u1000"
    write_private(path, proxy_chain(key))
    monkeypatch.setenv("X509_USER_PROXY", str(path))
    monkeypatch.setattr(
        gsi, "read_proxy", lambda _path, named: dataclasses.replace(proxy, key=object())
    )
    assert GSICredential.available(OFFER, Config(), username="j", host="h") is None


def test_available_carries_the_version_and_the_delegation_choice(monkeypatch, tmp_path, key):
    path = tmp_path / "x509up_u1000"
    write_private(path, proxy_chain(key))
    monkeypatch.setenv("X509_USER_PROXY", str(path))
    config = Config(gsi_delegate=True, ca_path="/certs")
    credential = _present(
        GSICredential.available(Offer("gsi", "v:10600,c:ssl"), config, username="j", host="srv")
    )
    assert credential.server_version == 10600 and credential.signed
    assert credential.delegation == Delegation(True, "srv", "/certs")
    garbled = _present(
        GSICredential.available(Offer("gsi", "v:new"), Config(), username="j", host="h")
    )
    assert garbled.server_version == 0 and not garbled.delegation.wanted


@pytest.mark.parametrize(
    ("value", "expected"), [(None, False), ("0", False), ("1", True), ("2", True), ("x", False)]
)
def test_the_stock_environment_variable_turns_delegation_on(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("XrdSecGSIDELEGPROXY", raising=False)
    else:
        monkeypatch.setenv("XrdSecGSIDELEGPROXY", value)
    assert Config().gsi_delegate is expected


def test_the_certreq_names_our_ca_not_the_servers(monkeypatch, tmp_path, key):
    """The server's ``ca:`` names *its* CA; echoing it back breaks a login to another CA's host."""
    path = tmp_path / "x509up_u1000"
    write_private(path, proxy_chain(key))
    monkeypatch.setenv("X509_USER_PROXY", str(path))
    offer = Offer("gsi", "v:10600,c:ssl,ca:82977a7e.0|5a04724c.0")  # CNAF's host CA
    credential = _present(GSICredential.available(offer, Config(), username="j", host="srv"))
    sent = find_bucket(credential.initial(), BUCKET_ISSUER_HASH)
    assert sent == b"33027a7e.0|b5f5da31.0"


def test_a_chain_with_nothing_to_hash_falls_back_to_the_offer(monkeypatch, proxy):
    anchor_only = dataclasses.replace(proxy, chain=(proxy.chain[-1],))
    monkeypatch.setattr(gsi, "read_proxy", lambda path, named: anchor_only)
    credential = _present(GSICredential.available(OFFER, Config(), username="j", host="h"))
    assert credential.issuer_hash == "1a2b3c4d.0"


@pytest.mark.parametrize(
    ("value", "expected"), [(None, True), ("1", True), ("0", False), ("no", False)]
)
def test_the_login_check_is_on_unless_turned_off(monkeypatch, tmp_path, key, value, expected):
    if value is None:
        monkeypatch.delenv("XRD_GSI_VERIFY_SERVER", raising=False)
    else:
        monkeypatch.setenv("XRD_GSI_VERIFY_SERVER", value)
    monkeypatch.setenv("XrdSecGSISRVNAMES", "*.example.org")
    path = write_private(tmp_path / "x509up", proxy_chain(key))
    config = Config(proxy=str(path), ca_path="/certs")
    assert config.gsi_verify_server is expected
    credential = _present(GSICredential.available(OFFER, config, username="j", host="srv"))
    if expected:
        check = _present(credential.server_check)
        assert dataclasses.replace(check, resolve=None) == ServerCheck(
            "srv", "/certs", ("*.example.org",), "XRD_GSI_VERIFY_SERVER=0"
        )
    else:
        assert credential.server_check is None


@pytest.mark.parametrize(("value", "trusted"), [(None, True), ("1", True), ("0", False)])
def test_dns_names_the_server_unless_trustdns_is_off(monkeypatch, tmp_path, key, value, trusted):
    """Stock XrdSecgsi's default: CNAF's round-robin alias passes on its nodes' names."""
    if value is None:
        monkeypatch.delenv("XrdSecGSITRUSTDNS", raising=False)
    else:
        monkeypatch.setenv("XrdSecGSITRUSTDNS", value)
    asked: list[str] = []

    def resolve(host: str) -> tuple[str, ...]:
        asked.append(host)
        return ("node.example.org",)

    monkeypatch.setattr(gsi, "dns_names", resolve)
    path = write_private(tmp_path / "x509up", proxy_chain(key))
    credential = _present(
        GSICredential.available(OFFER, Config(proxy=str(path)), username="j", host="alias")
    )
    check = _present(credential.server_check)
    if trusted:
        assert asked == []  # looked up only when the certificate does not match
        assert tuple(_present(check.resolve)()) == ("node.example.org",)
        assert asked == ["alias"]
    else:
        assert check.resolve is None


# -- one login, end to end ---------------------------------------------------------


@pytest.fixture(scope="module")
def server_pki(tmp_path_factory):
    """A CA directory and a ``srv.example.org`` certificate it issued, with the key."""
    ca_name = name(("2.5.4.10", "example"), ("2.5.4.3", "Example CA"))
    server_name = name(("2.5.4.10", "example"), ("2.5.4.3", "srv.example.org"))
    ca_key, server_key = throwaway_key(1), throwaway_key(2)
    directory = tmp_path_factory.mktemp("certificates")
    ca = make_certificate_with(ca_name, ca_name, ca_key.public, ca_key, (CA_CONSTRAINTS,))
    (directory / "0a1b2c3d.0").write_bytes(pem("CERTIFICATE", ca))
    server = make_certificate_with(server_name, ca_name, server_key.public, ca_key, ())
    return str(directory), pem("CERTIFICATE", server), server_key


def _login(server_pki, tmp_path, key, host: str) -> tuple[GSICredential, SecgsiServer]:
    """A credential as the ladder builds it, and the server it is about to meet."""
    ca_path, server_pem, server_key = server_pki
    path = write_private(tmp_path / "x509up", proxy_chain(key))
    config = Config(proxy=str(path), ca_path=ca_path)
    offer = Offer("gsi", "v:10600,c:ssl")
    credential = _present(GSICredential.available(offer, config, username="j", host=host))
    server = SecgsiServer(server_pem, server_key, DH_PRIME, DH_GENERATOR, (1 << 250) | 5)
    return credential, server


def test_a_credential_from_the_ladder_logs_in(monkeypatch, server_pki, tmp_path, key):
    monkeypatch.delenv("XrdSecGSISRVNAMES", raising=False)
    credential, server = _login(server_pki, tmp_path, key, "srv.example.org")
    credential.initial()
    reply = _present(credential.step(server.challenge(tag=b"abcdEFGH")))
    assert decode_message(reply)[0] == STEP_CLIENT_CERT
    login = server.read_certificate(reply, tag=b"abcdEFGH")
    assert login.chain_pem == credential.proxy.pem()


def test_a_wrong_server_is_refused_with_this_clients_bypass(monkeypatch, server_pki, tmp_path, key):
    monkeypatch.delenv("XrdSecGSISRVNAMES", raising=False)
    credential, server = _login(server_pki, tmp_path, key, "impostor.example.org")
    credential.initial()
    with pytest.raises(CredentialError) as refused:
        credential.step(server.challenge())
    assert str(refused.value).endswith(
        "on a test setup only, XRD_GSI_VERIFY_SERVER=0 skips the check"
    )


# -- the proxy file is this user's alone ---------------------------------------


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o620, 0o602, 0o660])
def test_a_proxy_others_can_read_or_write_is_skipped_loudly(tmp_path, key, mode, caplog):
    path = write_private(tmp_path / "x509up", proxy_chain(key))
    path.chmod(mode)
    config = Config(proxy=str(path))
    with caplog.at_level("WARNING", logger="xrdclient.auth.gsi"):
        assert GSICredential.available(OFFER, config, username="j", host="h") is None
    assert "ignoring the X.509 proxy" in caplog.text and f"chmod 600 {path}" in caplog.text
    ask = GSICredential.missing(OFFER, config, username="j", host="h")
    assert ask is not None and f"chmod 600 {path}" in ask.reason


def test_a_link_at_the_default_location_is_not_followed(tmp_path, key, monkeypatch):
    real = write_private(tmp_path / "real", proxy_chain(key))
    link = tmp_path / "x509up_u1000"
    link.symlink_to(real)
    monkeypatch.delenv("X509_USER_PROXY", raising=False)
    monkeypatch.setattr(gsi, "default_proxy_path", lambda config: str(link))
    assert GSICredential.available(OFFER, Config(proxy=None), username="j", host="h") is None
    ask = GSICredential.missing(OFFER, Config(proxy=None), username="j", host="h")
    assert ask is not None and "symbolic link" in ask.reason


def test_a_link_the_user_named_is_followed(tmp_path, key, monkeypatch):
    real = write_private(tmp_path / "real", proxy_chain(key))
    link = tmp_path / "x509up_link"
    link.symlink_to(real)
    monkeypatch.setenv("X509_USER_PROXY", str(link))
    found = GSICredential.available(OFFER, Config(proxy=None), username="j", host="h")
    assert isinstance(found, GSICredential)
    assert found.proxy.path == str(link)


def test_a_file_that_is_not_a_proxy_is_passed_over_quietly(tmp_path):
    path = write_private(tmp_path / "empty", b"")
    assert GSICredential.available(OFFER, Config(proxy=str(path)), username="j", host="h") is None
