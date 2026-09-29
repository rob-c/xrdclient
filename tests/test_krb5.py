"""Kerberos: the credential cache, the service name, and the ``krb5`` credential.

The caches here are written by :func:`write_ccache` below, from the MIT
format's own layout; ``test_krb5_mit.py`` reads caches MIT itself wrote.
The KDC is stood in for by monkeypatching :func:`request_ticket` - what a
real one does with our requests is ``test_krb5_interop.py``'s business, and
what a fake one on a socket does is ``test_krb5_protocol.py``'s.
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import replace

import pytest

from xrdclient.auth import krb5, registry
from xrdclient.auth.base import Offer
from xrdclient.auth.kerberos import asn1, tgs
from xrdclient.auth.kerberos.asn1 import Fields
from xrdclient.auth.kerberos.ccache import ccache_name, resolve_ccache
from xrdclient.auth.krb5 import (
    CCACHE_VERSION_3,
    CCACHE_VERSION_4,
    KerberosCredential,
    Principal,
    Ticket,
    default_ccache_path,
    read_ccache,
    service_principal,
    tickets,
)
from xrdclient.config import Config
from xrdclient.crypto.der import parse
from xrdclient.crypto.rfc3961 import get_enctype
from xrdclient.errors import CredentialError

REALM = "EXAMPLE.ORG"
OFFER = Offer("krb5", "xrootd/srv.example.org@EXAMPLE.ORG")
TICKET_DER = b"\x61\x03\x30\x01\x00"  # opaque to the client; any single TLV will do
KEY = bytes(range(32))


def _blob(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def _principal(components: tuple[str, ...], realm: str = REALM, name_type: int = 1) -> bytes:
    return (
        struct.pack(">II", name_type, len(components))
        + _blob(realm.encode())
        + b"".join(_blob(part.encode()) for part in components)
    )


def _credential(
    client,
    server,
    *,
    end_time,
    enctype=18,
    version=CCACHE_VERSION_4,
    der=TICKET_DER,
    addresses=(),
    authdata=(),
    flags=0x40E00000,
    realm=REALM,
    key=KEY,
):
    out = _principal(client) + _principal(server, realm)
    out += struct.pack(">H", enctype)
    if version == CCACHE_VERSION_3:
        out += struct.pack(">H", enctype)  # version 3 wrote it twice
    out += _blob(key)  # the session key
    out += struct.pack(">IIII", int(end_time) - 3600, int(end_time) - 3600, int(end_time), 0)
    out += b"\x00"  # is_skey
    out += struct.pack(">I", flags)
    out += struct.pack(">I", len(addresses))
    out += b"".join(struct.pack(">H", kind) + _blob(value) for kind, value in addresses)
    out += struct.pack(">I", len(authdata))
    out += b"".join(struct.pack(">H", kind) + _blob(value) for kind, value in authdata)
    out += _blob(der) + _blob(b"")
    return out


def write_ccache(path, entries, *, version=CCACHE_VERSION_4, default=("jane",), flags=0x40E00000):
    """A FILE credential cache, in the format MIT and Heimdal write."""
    out = struct.pack(">H", version)
    if version == CCACHE_VERSION_4:
        out += struct.pack(">H", 0)  # no header tags
    out += _principal(default)
    for client, server, end_time in entries:
        out += _credential(client, server, end_time=end_time, version=version, flags=flags)
    path.write_bytes(out)
    return str(path)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """No test here reads the machine's krb5.conf, or another test's fetched tickets."""
    conf = tmp_path / "krb5.conf"
    conf.write_text(f"[libdefaults]\n default_realm = {REALM}\n")
    monkeypatch.setenv("KRB5_CONFIG", str(conf))
    krb5._FETCHED.clear()
    yield
    krb5._FETCHED.clear()


@pytest.fixture
def cache(tmp_path):
    ahead = time.time() + 36000
    return write_ccache(
        tmp_path / "krb5cc_1000",
        [
            (("jane",), ("krbtgt", REALM), ahead),
            (("jane",), ("xrootd", "srv.example.org"), ahead),
        ],
    )


@pytest.fixture
def tgt_only(tmp_path, monkeypatch):
    path = write_ccache(tmp_path / "tgt", [(("jane",), ("krbtgt", REALM), time.time() + 36000)])
    monkeypatch.setenv("KRB5CCNAME", f"FILE:{path}")
    return path


@pytest.fixture
def kdc(monkeypatch):
    """A stand-in for the TGS exchange that records what it was asked."""
    asked: list[dict] = []

    def request_ticket(tgt, server, profile, *, etypes, options=0, **_):
        asked.append({"tgt": tgt, "server": server, "etypes": etypes, "options": options})
        return replace(tgt, server=server, key=bytes(32), end_time=int(time.time()) + 600)

    monkeypatch.setattr(krb5, "request_ticket", request_ticket)
    return asked


# -- the cache reader -------------------------------------------------------


def test_a_version_4_cache_reads_back(cache):
    default, found = read_ccache(cache)
    assert str(default) == f"jane@{REALM}"
    assert [str(ticket.server) for ticket in found] == [
        f"krbtgt/{REALM}@{REALM}",
        f"xrootd/srv.example.org@{REALM}",
    ]
    assert all(str(ticket.client) == f"jane@{REALM}" for ticket in found)


def test_the_session_key_is_kept_and_never_printed(cache):
    ticket = read_ccache(cache)[1][0]
    assert ticket.key == KEY
    assert KEY.hex() not in repr(ticket) and "key" not in repr(ticket)


def test_a_version_3_cache_reads_back(tmp_path):
    """Version 3 repeats the enctype; getting that wrong desynchronises everything."""
    path = write_ccache(
        tmp_path / "v3",
        [(("jane",), ("krbtgt", REALM), time.time() + 60)],
        version=CCACHE_VERSION_3,
    )
    _default, found = read_ccache(path)
    assert len(found) == 1
    assert found[0].enctype == 18 and found[0].key == KEY


def test_the_ticket_fields_survive_the_round_trip(cache):
    ticket = read_ccache(cache)[1][0]
    assert ticket.enctype == 18
    assert ticket.flags == 0x40E00000 and ticket.forwardable
    assert ticket.der == TICKET_DER
    assert ticket.auth_time == ticket.start_time == ticket.end_time - 3600
    assert ticket.renew_till == 0


def test_the_ticket_granting_ticket_is_identified(cache):
    granting, service = read_ccache(cache)[1]
    assert granting.is_tgt
    assert not service.is_tgt


def test_mits_configuration_entries_are_not_tickets(tmp_path):
    """kinit stores fast_avail, pa_type and the like as pseudo-tickets in X-CACHECONF:."""
    body = struct.pack(">HH", CCACHE_VERSION_4, 0) + _principal(("jane",))
    body += _credential(
        ("jane",), ("krb5_ccache_conf_data", "fast_avail"), end_time=3600, realm="X-CACHECONF:"
    )
    body += _credential(("jane",), ("krbtgt", REALM), end_time=time.time() + 60)
    path = tmp_path / "conf"
    path.write_bytes(body)
    assert [str(t.server) for t in read_ccache(str(path))[1]] == [f"krbtgt/{REALM}@{REALM}"]


def test_expiry_is_reported_from_the_cache(tmp_path):
    path = write_ccache(
        tmp_path / "mixed",
        [
            (("jane",), ("krbtgt", REALM), time.time() - 60),
            (("jane",), ("xrootd", "srv"), time.time() + 600),
        ],
    )
    stale, live = read_ccache(path)[1]
    assert stale.expired and stale.remaining() < 0
    assert not live.expired
    assert live.remaining() == pytest.approx(600, abs=5)


def test_tickets_returns_only_the_live_ones(tmp_path):
    path = write_ccache(
        tmp_path / "mixed",
        [
            (("jane",), ("krbtgt", REALM), time.time() - 60),
            (("jane",), ("xrootd", "srv"), time.time() + 600),
        ],
    )
    assert [str(t.server) for t in tickets(path)] == [f"xrootd/srv@{REALM}"]


def test_tickets_is_quiet_about_a_cache_that_is_not_there(tmp_path, monkeypatch):
    assert tickets(str(tmp_path / "absent")) == []
    junk = tmp_path / "junk"
    junk.write_bytes(b"\x05\x04nonsense")
    assert tickets(str(junk)) == []
    monkeypatch.setenv("KRB5CCNAME", "KCM:1000")
    assert tickets() == []


def test_an_unknown_version_is_named(tmp_path):
    path = tmp_path / "future"
    path.write_bytes(struct.pack(">H", 0x0505) + b"\x00" * 32)
    with pytest.raises(ValueError, match="0x0505"):
        read_ccache(str(path))


def test_a_truncated_entry_costs_the_tail_not_the_answer(tmp_path):
    """A cache being rewritten under us must not lose the tickets already read."""
    path = write_ccache(tmp_path / "cut", [(("jane",), ("krbtgt", REALM), time.time() + 60)])
    with open(path, "rb") as handle:
        data = handle.read()
    cut = tmp_path / "cut2"
    cut.write_bytes(data + _principal(("jane",))[:10])
    default, found = read_ccache(str(cut))
    assert str(default) == f"jane@{REALM}"
    assert len(found) == 1


def test_the_cursor_refuses_to_read_past_the_end():
    from xrdclient.auth.krb5 import _Reader

    reader = _Reader(b"\x00\x01")
    assert reader.u16() == 1
    assert reader.exhausted
    with pytest.raises(ValueError, match="truncated"):
        reader.take(1)
    with pytest.raises(ValueError, match="truncated"):
        _Reader(b"").take(-1)


def test_a_ticket_carrying_addresses_and_authorization_data_still_reads_back(tmp_path):
    """Both lists are skipped, not parsed - but skipping them must be exact."""
    ahead = time.time() + 3600
    path = tmp_path / "krb5cc_addr"
    body = struct.pack(">HH", CCACHE_VERSION_4, 0) + _principal(("jane",))
    body += _credential(
        ("jane",),
        ("krbtgt", REALM),
        end_time=ahead,
        addresses=((2, b"\x7f\x00\x00\x01"), (2, b"\x0a\x00\x00\x01")),
        authdata=((1, b"restrictions"),),
    )
    path.write_bytes(body)
    _, found = read_ccache(str(path))
    assert [str(t.server) for t in found] == [f"krbtgt/{REALM}@{REALM}"]
    assert found[0].is_tgt


# -- which cache ------------------------------------------------------------


def test_the_cache_path_follows_the_kerberos_convention(monkeypatch):
    monkeypatch.delenv("KRB5CCNAME", raising=False)
    assert default_ccache_path() == f"/tmp/krb5cc_{os.geteuid()}"
    monkeypatch.setenv("KRB5CCNAME", "FILE:/tmp/mine")
    assert default_ccache_path() == "/tmp/mine"
    monkeypatch.setenv("KRB5CCNAME", "/tmp/bare")
    assert default_ccache_path() == "/tmp/bare"
    monkeypatch.setenv("KRB5CCNAME", "/tmp/odd:name")
    assert default_ccache_path() == "/tmp/odd:name"


@pytest.mark.parametrize("name", ["KCM:", "KEYRING:persistent:1000", "API:", "MEMORY:x", "WEIRD:x"])
def test_a_cache_that_is_not_a_file_is_an_error_that_says_what_to_do(monkeypatch, name):
    monkeypatch.setenv("KRB5CCNAME", name)
    with pytest.raises(CredentialError, match=r"KRB5CCNAME=FILE:/tmp/krb5cc_\$\(id -u\) kinit"):
        default_ccache_path()


def test_a_dir_collection_resolves_to_its_primary_cache(tmp_path):
    collection = tmp_path / "cc"
    collection.mkdir()
    assert resolve_ccache(f"DIR:{collection}") == str(collection / "tkt")  # none yet: MIT's default
    (collection / "primary").write_text("tktABC\n")
    assert resolve_ccache(f"DIR:{collection}") == str(collection / "tktABC")
    assert resolve_ccache(f"DIR::{collection}/tktXYZ") == f"{collection}/tktXYZ"


def test_the_default_name_comes_from_krb5_conf_when_the_environment_is_silent(monkeypatch):
    from xrdclient.auth.kerberos.profile import Profile

    monkeypatch.delenv("KRB5CCNAME", raising=False)
    monkeypatch.setenv("USER", "jane")
    profile = Profile.parse(
        "[libdefaults]\n"
        " default_ccache_name = FILE:%{TEMP}/cc_%{uid}_%{euid}_%{USERID}_%{username}%{null}\n"
    )
    uid, euid = os.getuid(), os.geteuid()
    import tempfile

    expected = f"FILE:{tempfile.gettempdir()}/cc_{uid}_{euid}_{euid}_jane"
    assert ccache_name(profile) == expected
    kcm = Profile.parse("[libdefaults]\n default_ccache_name = KCM:\n")
    with pytest.raises(CredentialError, match="lives outside the filesystem"):
        resolve_ccache(profile=kcm)


# -- principals -------------------------------------------------------------


def test_a_principal_prints_the_way_kinit_does():
    assert str(Principal(("xrootd", "srv.example.org"), REALM)) == f"xrootd/srv.example.org@{REALM}"
    assert str(Principal(("jane",), REALM)) == f"jane@{REALM}"
    assert str(Principal(("jane",), "")) == "jane"
    assert not Principal((), "")


def test_ticket_repr_says_who_and_how_long():
    ticket = Ticket(
        client=Principal(("jane",), REALM),
        server=Principal(("xrootd", "srv"), REALM),
        enctype=18,
        auth_time=0,
        start_time=0,
        end_time=int(time.time()) + 300,
        renew_till=0,
        flags=0,
    )
    assert repr(ticket).startswith("Ticket(server='xrootd/srv@EXAMPLE.ORG', expires_in=")
    assert "299" in repr(ticket) or "300" in repr(ticket)


def test_a_ticket_with_no_end_time_never_expires():
    ticket = Ticket(Principal(("j",), ""), Principal(("s",), ""), 18, 0, 0, 0, 0, 0)
    assert not ticket.expired


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


def test_the_realm_comes_from_the_offer_the_domain_map_or_the_client(tmp_path, monkeypatch):
    from xrdclient.auth.kerberos.profile import Profile

    profile = Profile.parse("[domain_realm]\n .mapped.org = MAPPED.ORG\n")
    client = Principal(("jane",), REALM)

    def target(params, host):
        return str(krb5._target(Offer("krb5", params), host, profile, client))

    assert target("xrootd/a.mapped.org@NAMED", "x") == "xrootd/a.mapped.org@NAMED"
    assert target("xrootd/a.mapped.org", "x") == "xrootd/a.mapped.org@MAPPED.ORG"
    assert target("", "b.mapped.org") == "xrootd/b.mapped.org@MAPPED.ORG"
    assert target("", "unmapped.net") == f"xrootd/unmapped.net@{REALM}"
    assert target("host", "c.mapped.org") == "host@MAPPED.ORG"  # one component: the host decides


@pytest.mark.parametrize(
    "libdefaults, expected",
    [
        ("", [18, 17, 20, 19]),
        ("default_tgs_enctypes = aes256-sha2", [20]),
        ("permitted_enctypes = aes128-cts aes256-cts", [17, 18]),
        ("default_tgs_enctypes = rc4-hmac\n permitted_enctypes = aes256-cts", [18]),
        ("default_tgs_enctypes = des-cbc-crc", [18, 17, 20, 19]),
    ],
)
def test_the_enctypes_asked_for_follow_krb5_conf(libdefaults, expected):
    from xrdclient.auth.kerberos.profile import Profile

    assert krb5._etypes(Profile.parse(f"[libdefaults]\n {libdefaults}\n")) == expected


# -- the mechanism ----------------------------------------------------------


def test_krb5_is_registered():
    assert registry()["krb5"] is KerberosCredential


def test_a_credential_reprs_by_its_target():
    credential = KerberosCredential("xrootd/srv.example.org")
    assert repr(credential) == "KerberosCredential(principal='xrootd/srv.example.org')"


def test_available_is_silent_when_there_is_no_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("KRB5CCNAME", f"FILE:{tmp_path / 'absent'}")
    assert KerberosCredential.available(OFFER, Config(), username="jane", host="srv") is None


def test_available_is_silent_about_an_empty_cache(monkeypatch, tmp_path):
    path = write_ccache(tmp_path / "empty", [])
    monkeypatch.setenv("KRB5CCNAME", f"FILE:{path}")
    assert KerberosCredential.available(OFFER, Config(), username="jane", host="srv") is None


def test_an_expired_cache_says_whose_tickets_expired_and_how_long_ago(monkeypatch, tmp_path):
    path = write_ccache(tmp_path / "old", [(("jane",), ("krbtgt", REALM), time.time() - 2460)])
    monkeypatch.setenv("KRB5CCNAME", f"FILE:{path}")
    with pytest.raises(CredentialError) as info:
        KerberosCredential.available(OFFER, Config(), username="jane", host="srv")
    assert str(info.value) == (
        f"the Kerberos tickets for jane@{REALM} in {path} expired 41 minutes ago; run kinit"
    )


@pytest.mark.parametrize("content", [None, b"\x09\x09garbage"])
def test_an_unreadable_cache_is_an_error_not_a_silence(monkeypatch, tmp_path, content):
    path = tmp_path / "bad"
    if content is None:
        path.mkdir()  # exists, but reading it fails
    else:
        path.write_bytes(content)
    monkeypatch.setenv("KRB5CCNAME", f"FILE:{path}")
    with pytest.raises(CredentialError, match="is unreadable"):
        KerberosCredential.available(OFFER, Config(), username="jane", host="srv")


def test_a_kcm_cache_is_an_error_not_a_silence(monkeypatch):
    monkeypatch.setenv("KRB5CCNAME", "KCM:")
    with pytest.raises(CredentialError, match="KCM"):
        KerberosCredential.available(OFFER, Config(), username="jane", host="srv")


def test_a_cached_service_ticket_is_used_without_asking_the_kdc(monkeypatch, cache, kdc):
    monkeypatch.setenv("KRB5CCNAME", f"FILE:{cache}")
    credential = KerberosCredential.available(OFFER, Config(), username="jane", host="srv")
    assert credential is not None and kdc == []
    assert credential.principal == "xrootd/srv.example.org@EXAMPLE.ORG"


def test_with_only_a_tgt_the_kdc_is_asked_once_per_process(tgt_only, kdc):
    first = KerberosCredential.available(OFFER, Config(), username="jane", host="srv")
    second = KerberosCredential.available(OFFER, Config(), username="jane", host="srv")
    assert first is not None and second is not None
    assert len(kdc) == 1
    assert str(kdc[0]["server"]) == "xrootd/srv.example.org@EXAMPLE.ORG"
    assert kdc[0]["etypes"] == [18, 17, 20, 19] and kdc[0]["options"] == 0
    assert kdc[0]["tgt"].key == KEY


def test_a_remembered_ticket_about_to_expire_is_fetched_again(tgt_only, kdc):
    KerberosCredential.available(OFFER, Config(), username="jane", host="srv")
    slot = next(iter(krb5._FETCHED))
    krb5._FETCHED[slot] = replace(krb5._FETCHED[slot], end_time=int(time.time()) + 30)
    KerberosCredential.available(OFFER, Config(), username="jane", host="srv")
    assert len(kdc) == 2


def test_no_tgt_and_no_service_ticket_says_kinit(monkeypatch, tmp_path, kdc):
    path = write_ccache(tmp_path / "other", [(("jane",), ("host", "elsewhere"), time.time() + 600)])
    monkeypatch.setenv("KRB5CCNAME", f"FILE:{path}")
    with pytest.raises(CredentialError, match=r"no live ticket-granting ticket for EXAMPLE\.ORG"):
        KerberosCredential.available(OFFER, Config(), username="jane", host="srv")


def test_a_fwd_offer_fetches_a_forwarded_tgt(tgt_only, kdc):
    offer = Offer("krb5", "xrootd/srv.example.org@EXAMPLE.ORG,fwd")
    credential = KerberosCredential.available(offer, Config(), username="jane", host="srv")
    assert credential is not None
    _service, forwarded = kdc
    assert forwarded["server"].components == ("krbtgt", REALM)
    assert forwarded["options"] == tgs.KDC_OPT_FORWARDED | tgs.KDC_OPT_FORWARDABLE


def test_a_fwd_offer_with_a_tgt_that_cannot_be_forwarded_says_kinit_f(monkeypatch, tmp_path, kdc):
    path = write_ccache(
        tmp_path / "nofwd", [(("jane",), ("krbtgt", REALM), time.time() + 600)], flags=0
    )
    monkeypatch.setenv("KRB5CCNAME", f"FILE:{path}")
    offer = Offer("krb5", "xrootd/srv.example.org@EXAMPLE.ORG,fwd")
    with pytest.raises(CredentialError, match=r"-exptkn .* not forwardable; run kinit -f"):
        KerberosCredential.available(offer, Config(), username="jane", host="srv")


def test_the_ladder_turns_a_kerberos_failure_into_a_reason(monkeypatch, tmp_path):
    """``select`` must survive a mechanism that raises, and record why."""
    from xrdclient.auth import select

    path = write_ccache(tmp_path / "old", [(("jane",), ("krbtgt", REALM), time.time() - 60)])
    monkeypatch.setenv("KRB5CCNAME", f"FILE:{path}")
    rejected: dict[str, str] = {}
    chosen = list(
        select("&P=krb5&P=unix", Config(), username="jane", host="srv", rejected=rejected)
    )
    assert [credential.name for credential in chosen] == ["unix"]
    assert "expired" in rejected["krb5"] and "run kinit" in rejected["krb5"]


# -- the credential blobs ----------------------------------------------------


def _session(enctype: int = 18) -> Ticket:
    return Ticket(
        client=Principal(("jane",), REALM, 1),
        server=Principal(("xrootd", "srv.example.org"), REALM, 1),
        enctype=enctype,
        auth_time=1,
        start_time=1,
        end_time=int(time.time()) + 600,
        renew_till=0,
        flags=0,
        der=TICKET_DER,
        key=get_enctype(enctype).random_key(),
    )


def _open(blob: bytes, tag: int) -> Fields:
    assert blob.startswith(b"krb5\x00")
    outer, _ = parse(blob[5:])
    assert outer.tag == tag
    return Fields(parse(outer.value)[0], hex(tag))


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
    assert authenticator.optional(3) is None  # no checksum, as krb5_mk_req_extended sends
    assert abs(authenticator.time(5) - time.time()) < 5


def test_without_a_ticket_there_is_no_first_blob():
    with pytest.raises(CredentialError, match="no service ticket for xrootd/srv"):
        KerberosCredential("xrootd/srv").initial()


def test_a_fwdtgt_challenge_is_answered_with_a_krb_cred_once():
    session = _session()
    forwarded = replace(_session(), server=Principal(("krbtgt", REALM), REALM, 2))
    credential = KerberosCredential(str(session.server), session, forward=forwarded)
    krb_cred = _open(credential.step(b"fwdtgt\x00"), asn1.APP_KRB_CRED)
    sealed = asn1.read_encrypted_data(krb_cred.get(3))
    plain = get_enctype(18).decrypt(session.key, tgs.USAGE_KRB_CRED, sealed.cipher)
    part = Fields(parse(parse(plain)[0].value)[0], "EncKrbCredPart")
    (info,) = part.get(0).children()
    assert asn1.read_key(Fields(info, "KrbCredInfo").get(0)).value == forwarded.key
    assert credential.step(b"fwdtgt\x00") is None  # the exchange is over


def test_a_fwdtgt_challenge_nobody_announced_is_refused():
    credential = KerberosCredential("xrootd/srv", _session())
    with pytest.raises(CredentialError, match=r"without saying so in its offer \(',fwd'\)"):
        credential.step(b"fwdtgt\x00")


def test_any_other_challenge_is_refused():
    credential = KerberosCredential("xrootd/srv", _session())
    with pytest.raises(CredentialError, match="unexpected krb5 challenge"):
        credential.step(b"something else")
