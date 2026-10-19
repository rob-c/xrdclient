"""Kerberos, XRootD's way: the ``krb5`` credential over xrdsec's Kerberos client.

What is tested here is xrdclient's adapter - the ``krb5\\0`` framing, the
service principal taken from the server's offer, the ``,fwd``/``fwdtgt``
forwarding handshake, ``$XRD_KRB5_BACKEND`` and the fall-back between the
system library and the portable reader, and the errors the authentication
ladder records. The Kerberos client itself (caches, ``krb5.conf``, the KDC,
the TGS exchange, the messages) is :mod:`xrdsec.krb5` and is tested in
xrdsec's suite; a real MIT KDC and a real ``xrootd`` are
``test_krb5_interop.py``'s.

The caches are written with :func:`xrdsec.testing.krb5.write_ccache`; the
KDC is stood in for by monkeypatching xrdsec's
:func:`xrdsec.krb5.client.request_ticket`.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest
from xrdsec.krb5 import asn1, tgs
from xrdsec.krb5 import client as krb5_client
from xrdsec.krb5.asn1 import Fields
from xrdsec.krb5.model import Principal, Ticket
from xrdsec.krb5.rfc3961 import get_enctype
from xrdsec.testing.krb5 import make_ticket, recording_tgs, write_ccache

from xrdclient.auth import krb5, registry
from xrdclient.auth.base import Offer
from xrdclient.auth.krb5 import KerberosCredential, default_ccache_path, service_principal
from xrdclient.config import Config
from xrdclient.crypto.der import parse
from xrdclient.errors import CredentialError

REALM = "EXAMPLE.ORG"
JANE = Principal(("jane",), REALM, 1)
TGS = Principal(("krbtgt", REALM), REALM, 2)
OFFER = Offer("krb5", "xrootd/srv.example.org@EXAMPLE.ORG")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """No test here reads the machine's krb5.conf, or another test's fetched tickets."""
    conf = tmp_path / "krb5.conf"
    conf.write_text(f"[libdefaults]\n default_realm = {REALM}\n")
    monkeypatch.setenv("KRB5_CONFIG", str(conf))
    krb5_client.clear_cache()
    yield
    krb5_client.clear_cache()


def _use(tmp_path, monkeypatch, *tickets: Ticket) -> str:
    """A FILE cache holding ``tickets`` for jane, made the default; its path."""
    path = write_ccache(tmp_path / "krb5cc_test", tickets, default=JANE)
    monkeypatch.setenv("KRB5CCNAME", f"FILE:{path}")
    return path


@pytest.fixture
def tgt_only(tmp_path, monkeypatch):
    return _use(tmp_path, monkeypatch, make_ticket(JANE, TGS))


@pytest.fixture
def kdc(monkeypatch):
    """A stand-in for the TGS exchange that records what it was asked."""
    asked: list[dict[str, Any]] = []
    monkeypatch.setattr(krb5_client, "request_ticket", recording_tgs(asked))
    return asked


def test_the_default_cache_file_follows_the_kerberos_convention(monkeypatch):
    monkeypatch.delenv("KRB5CCNAME", raising=False)
    assert default_ccache_path() == f"/tmp/krb5cc_{os.geteuid()}"
    monkeypatch.setenv("KRB5CCNAME", "FILE:/tmp/mine")
    assert default_ccache_path() == "/tmp/mine"
    monkeypatch.setenv("KRB5CCNAME", "KCM:")
    with pytest.raises(CredentialError, match=r"KRB5CCNAME=FILE:/tmp/krb5cc_\$\(id -u\) kinit"):
        default_ccache_path()


@pytest.mark.parametrize(
    "params, host, expected",
    [
        ("xrootd/srv.example.org@EXAMPLE.ORG", "other", "xrootd/srv.example.org@EXAMPLE.ORG"),
        ("xrootd/srv.example.org@EXAMPLE.ORG,fwd", "other", "xrootd/srv.example.org@EXAMPLE.ORG"),
        ("xrootd/srv.example.org", "other", "xrootd/srv.example.org"),
        ("", "srv.example.org", "xrootd/srv.example.org"),
        ("v:100,c:ssl", "srv.example.org", "xrootd/srv.example.org"),
        ("", "", "xrootd"),
    ],
)
def test_the_service_principal_follows_the_offer(params, host, expected):
    """The realm is kept: it is the server's own krb5_unparse_name of its principal."""
    assert service_principal(Offer("krb5", params), host) == expected


@pytest.mark.parametrize(
    "params, forwards",
    [("xrootd/h@R,fwd", True), ("xrootd/h@R", False), ("fwd", False), ("", False)],
)
def test_forwarding_is_asked_for_by_a_trailing_fwd(params, forwards):
    assert krb5.wants_forwarding(Offer("krb5", params)) is forwards


def test_krb5_is_registered():
    assert registry()["krb5"] is KerberosCredential


def test_a_credential_reprs_by_its_target():
    credential = KerberosCredential("xrootd/srv.example.org")
    assert repr(credential) == "KerberosCredential(principal='xrootd/srv.example.org')"


def test_available_is_silent_when_there_is_no_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("KRB5CCNAME", f"FILE:{tmp_path / 'absent'}")
    assert KerberosCredential.available(OFFER, Config(), username="jane", host="srv") is None


def test_a_macos_api_cache_uses_the_native_library(monkeypatch):
    monkeypatch.setenv("KRB5CCNAME", "API:")
    monkeypatch.setattr(krb5, "_native_ap_req", lambda principal, cache: b"native-ap-req")
    credential = KerberosCredential.available(OFFER, Config(), username="jane", host="srv")
    assert credential is not None
    assert credential.initial() == b"krb5\0native-ap-req"


def test_the_ladder_turns_a_kerberos_failure_into_a_reason(monkeypatch, tmp_path):
    """``select`` must survive a mechanism that raises, and record why."""
    from xrdclient.auth import select

    _use(tmp_path, monkeypatch, make_ticket(JANE, TGS, lifetime=-60))
    rejected: dict[str, str] = {}
    chosen = list(
        select("&P=krb5&P=unix", Config(), username="jane", host="srv", rejected=rejected)
    )
    assert [credential.name for credential in chosen] == ["unix"]
    assert "expired" in rejected["krb5"] and "run kinit" in rejected["krb5"]


def _open(blob: bytes | None, tag: int) -> Fields:
    assert blob is not None and blob.startswith(b"krb5\x00")
    outer, _ = parse(blob[5:])
    assert outer.tag == tag
    return Fields(parse(outer.value)[0], hex(tag))


def test_without_a_ticket_there_is_no_first_blob():
    with pytest.raises(CredentialError, match="no service ticket for xrootd/srv"):
        KerberosCredential("xrootd/srv").initial()


def test_a_fwdtgt_challenge_nobody_announced_is_refused():
    credential = KerberosCredential("xrootd/srv", _session())
    with pytest.raises(CredentialError, match=r"without saying so in its offer \(',fwd'\)"):
        credential.step(b"fwdtgt\x00")


def test_any_other_challenge_is_refused():
    credential = KerberosCredential("xrootd/srv", _session())
    with pytest.raises(CredentialError, match="unexpected krb5 challenge"):
        credential.step(b"something else")


def _system_library(
    monkeypatch: pytest.MonkeyPatch, answer: bytes | Exception
) -> list[tuple[str, str]]:
    """Stand in for python-gssapi: record what it was asked, answer or refuse."""
    asked: list[tuple[str, str]] = []

    def native(principal: str, cache: str) -> bytes:
        asked.append((principal, cache))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(krb5, "_native_ap_req", native)
    return asked


@pytest.mark.parametrize("choice", ["auto", "gssapi", "", "  AUTO "])
def test_the_system_library_is_used_first(monkeypatch, tmp_path, choice):
    monkeypatch.setenv("XRD_KRB5_BACKEND", choice)
    monkeypatch.setenv("KRB5CCNAME", f"FILE:{tmp_path}/cc")
    monkeypatch.setenv("KRB5_CONFIG", str(tmp_path / "none"))
    asked = _system_library(monkeypatch, b"system-ap-req")
    credential = KerberosCredential.available(OFFER, Config(), username="jane", host="srv")
    assert credential is not None and credential.initial() == b"krb5\0system-ap-req"
    assert asked == [(krb5.service_principal(OFFER, "srv"), f"FILE:{tmp_path}/cc")]


def test_with_no_cache_named_the_message_says_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("XRD_KRB5_BACKEND", "auto")
    monkeypatch.delenv("KRB5CCNAME", raising=False)
    asked = _system_library(monkeypatch, b"x")
    KerberosCredential.available(OFFER, Config(), username="jane", host="srv")
    assert asked[0][1] == "the default cache"


def test_auto_falls_back_to_the_portable_reader(monkeypatch, tmp_path, kdc):
    """No python-gssapi, or nothing it could use: the portable path, as before."""
    monkeypatch.setenv("XRD_KRB5_BACKEND", "auto")
    _use(tmp_path, monkeypatch, make_ticket(JANE, TGS))
    asked = _system_library(monkeypatch, CredentialError("Native Kerberos ... not installed"))
    credential = KerberosCredential.available(OFFER, Config(), username="jane", host="srv")
    assert asked and credential is not None and credential._ticket is not None
    assert credential.principal == "xrootd/srv.example.org@EXAMPLE.ORG"


def test_a_forced_system_library_reports_its_own_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("XRD_KRB5_BACKEND", "gssapi")
    monkeypatch.setenv("KRB5CCNAME", f"FILE:{tmp_path}/cc")
    _system_library(monkeypatch, CredentialError("Kerberos authentication could not use it"))
    with pytest.raises(CredentialError, match="could not use it"):
        KerberosCredential.available(OFFER, Config(), username="jane", host="srv")


@pytest.mark.parametrize("choice", ["auto", "gssapi"])
def test_forwarding_always_takes_the_portable_path(monkeypatch, tmp_path, choice):
    """python-gssapi cannot produce the raw KRB-CRED an -exptkn server wants."""
    monkeypatch.setenv("XRD_KRB5_BACKEND", choice)
    monkeypatch.setenv("KRB5CCNAME", f"FILE:{tmp_path}/absent")
    _system_library(monkeypatch, AssertionError("the system library was asked"))
    forwarding = Offer("krb5", "xrootd/srv@EXAMPLE.ORG,fwd")
    assert KerberosCredential.available(forwarding, Config(), username="j", host="srv") is None


def test_portable_never_asks_the_system_library(monkeypatch, tmp_path):
    monkeypatch.setenv("XRD_KRB5_BACKEND", "portable")
    monkeypatch.setenv("KRB5CCNAME", f"FILE:{tmp_path}/absent")
    _system_library(monkeypatch, AssertionError("the system library was asked"))
    assert KerberosCredential.available(OFFER, Config(), username="j", host="srv") is None


def test_a_typo_in_the_backend_is_refused_by_name(monkeypatch):
    monkeypatch.setenv("XRD_KRB5_BACKEND", "gssap")
    with pytest.raises(CredentialError, match="Unknown Kerberos backend 'gssap'"):
        KerberosCredential.available(OFFER, Config(), username="j", host="srv")


def test_the_system_library_gets_the_realm_the_portable_path_would_use(monkeypatch, tmp_path):
    conf = tmp_path / "krb5.conf"
    conf.write_text("[domain_realm]\n  .example.org = EXAMPLE.ORG\n")
    monkeypatch.setenv("KRB5_CONFIG", str(conf))
    assert krb5._native_principal(Offer("krb5", ""), "srv.example.org") == (
        "xrootd/srv.example.org@EXAMPLE.ORG"
    )
    assert krb5._native_principal(Offer("krb5", "xrootd"), "srv.example.org") == (
        "xrootd@EXAMPLE.ORG"
    )
    named = Offer("krb5", "xrootd/srv.example.org@OTHER.ORG")
    assert krb5._native_principal(named, "srv.example.org") == "xrootd/srv.example.org@OTHER.ORG"
    assert krb5._native_principal(Offer("krb5", ""), "srv.elsewhere.net") == (
        "xrootd/srv.elsewhere.net"
    )


def test_an_expired_cache_is_the_reason_the_ladder_records(monkeypatch, tmp_path):
    path = _use(tmp_path, monkeypatch, make_ticket(JANE, TGS, lifetime=-2460))
    with pytest.raises(CredentialError) as info:
        KerberosCredential.available(OFFER, Config(), username="jane", host="srv")
    assert str(info.value) == (
        f"the Kerberos tickets for jane@{REALM} in {path} expired 41 minutes ago; run kinit"
    )


def test_a_cached_service_ticket_is_used_for_the_offered_principal(monkeypatch, tmp_path, kdc):
    service = make_ticket(JANE, Principal(("xrootd", "srv.example.org"), REALM, 1))
    _use(tmp_path, monkeypatch, make_ticket(JANE, TGS), service)
    credential = KerberosCredential.available(OFFER, Config(), username="jane", host="srv")
    assert credential is not None and kdc == []
    assert credential.principal == "xrootd/srv.example.org@EXAMPLE.ORG"


def test_with_only_a_tgt_the_kdc_is_asked_once_per_process(tgt_only, kdc):
    first = KerberosCredential.available(OFFER, Config(), username="jane", host="srv")
    second = KerberosCredential.available(OFFER, Config(), username="jane", host="srv")
    assert first is not None and second is not None
    assert len(kdc) == 1 and str(kdc[0]["server"]) == "xrootd/srv.example.org@EXAMPLE.ORG"


def test_an_offer_without_a_realm_gets_the_clients(tgt_only, kdc):
    offer = Offer("krb5", "")
    credential = KerberosCredential.available(offer, Config(), username="jane", host="h.net")
    assert credential is not None and credential.principal == f"xrootd/h.net@{REALM}"


def test_a_fwd_offer_fetches_a_forwarded_tgt(tgt_only, kdc):
    offer = Offer("krb5", "xrootd/srv.example.org@EXAMPLE.ORG,fwd")
    credential = KerberosCredential.available(offer, Config(), username="jane", host="srv")
    assert credential is not None
    _service, forwarded = kdc
    assert forwarded["server"].components == ("krbtgt", REALM)
    assert forwarded["options"] == tgs.KDC_OPT_FORWARDED | tgs.KDC_OPT_FORWARDABLE
    krb_cred = _open(credential.step(b"fwdtgt\x00"), asn1.APP_KRB_CRED)
    assert krb_cred.get(3) is not None
    assert credential.step(b"fwdtgt\x00") is None  # the exchange is over


def test_a_fwd_offer_with_a_tgt_that_cannot_be_forwarded_says_kinit_f(monkeypatch, tmp_path, kdc):
    _use(tmp_path, monkeypatch, make_ticket(JANE, TGS, flags=0))
    offer = Offer("krb5", "xrootd/srv.example.org@EXAMPLE.ORG,fwd")
    with pytest.raises(CredentialError, match=r"not forwardable; run kinit -f"):
        KerberosCredential.available(offer, Config(), username="jane", host="srv")


# -- the credential blobs ----------------------------------------------------


def _session(enctype: int = 18) -> Ticket:
    return make_ticket(
        JANE,
        Principal(("xrootd", "srv.example.org"), REALM, 1),
        enctype=enctype,
        key=get_enctype(enctype).random_key(),
    )


@pytest.mark.parametrize("enctype", [17, 18, 19, 20])
def test_the_first_blob_is_krb5_and_a_raw_ap_req(enctype):
    session = _session(enctype)
    credential = KerberosCredential(str(session.server), session)
    ap_req = _open(credential.initial(), asn1.APP_AP_REQ)
    assert asn1.read_flags(ap_req.get(2)) == tgs.AP_OPTS_USE_SESSION_KEY
    sealed = asn1.read_encrypted_data(ap_req.get(4))
    plain = get_enctype(enctype).decrypt(session.key, tgs.USAGE_AP_REQ_AUTH, sealed.cipher)
    authenticator = Fields(parse(parse(plain)[0].value)[0], "Authenticator")
    assert authenticator.string(1) == REALM


def test_a_session_key_of_the_wrong_length_is_a_credential_error():
    ticket = make_ticket(JANE, Principal(("xrootd", "srv"), REALM), key=b"short")
    with pytest.raises(CredentialError, match="session key is 5 bytes"):
        KerberosCredential("x", ticket).initial()


# -- which implementation: the system library first, when it can --------------


@pytest.fixture
def gssapi(monkeypatch):
    """xrdsec's python-gssapi stand-in, installed as ``gssapi`` for one test."""
    from xrdsec.krb5 import gss
    from xrdsec.testing.krb5 import fake_gssapi_module

    module = fake_gssapi_module()
    monkeypatch.setitem(sys.modules, "gssapi", module)
    gss.reset()
    yield module
    gss.reset()


def test_a_missing_gssapi_binding_has_this_packages_install_hint(monkeypatch, gssapi):
    monkeypatch.setitem(sys.modules, "gssapi", None)
    with pytest.raises(CredentialError, match=r"pip install 'xrdclient\[krb5\]'"):
        krb5._native_ap_req("xrootd/host@EXAMPLE", "API:user")


def test_the_system_librarys_ap_req_comes_through_xrdsec(monkeypatch, gssapi):
    from xrdsec.krb5 import gss

    asked: list[str] = []

    def initiator_ap_req(principal: str, *, backend: str | None = None) -> bytes:
        asked.append(principal)
        return b"\x6e\x02\x30\x00"

    monkeypatch.setattr(gss, "initiator_ap_req", initiator_ap_req)
    assert krb5._native_ap_req("xrootd/server@EXAMPLE", "API:user") == b"\x6e\x02\x30\x00"
    assert asked == ["xrootd/server@EXAMPLE"]


def test_a_failure_of_the_system_library_names_the_cache(gssapi):
    """The toy mechanism's token is no AP-REQ: xrdsec refuses it, xrdclient says where."""
    with pytest.raises(CredentialError, match=r"could not use API:user: .*malformed AP-REQ"):
        krb5._native_ap_req("xrootd/server@EXAMPLE", "API:user")
