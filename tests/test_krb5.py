"""Kerberos: the credential-cache reader, and the framing around GSSAPI.

The exchange itself is :mod:`gssapi`'s, so what is testable here is what the
client does *without* the KDC — reading the cache, naming the service, and
saying something useful when the ticket or the module is missing. The caches
are written by :func:`write_ccache` below, from the MIT format's own layout.
"""

import struct
import sys
import time
import types

import pytest

from xrdclient.auth import registry
from xrdclient.auth.base import Offer
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
from xrdclient.errors import CredentialError


def _gssapi_installed() -> bool:
    try:
        import gssapi  # noqa: F401
    except ImportError:
        return False
    return True


#: Several tests assert on what happens *without* the extra, so they only
#: mean anything where it is genuinely absent.
HAS_GSSAPI = _gssapi_installed()

REALM = "EXAMPLE.ORG"
OFFER = Offer("krb5", "xrootd/srv.example.org@EXAMPLE.ORG")


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
    der=b"\x6e\x00",
    addresses=(),
    authdata=(),
):
    out = _principal(client) + _principal(server)
    out += struct.pack(">H", enctype)
    if version == CCACHE_VERSION_3:
        out += struct.pack(">H", enctype)  # version 3 wrote it twice
    out += _blob(b"\x00" * 32)  # the session key
    out += struct.pack(">IIII", int(end_time) - 3600, int(end_time) - 3600, int(end_time), 0)
    out += b"\x00"  # is_skey
    out += struct.pack(">I", 0x40E00000)  # flags
    out += struct.pack(">I", len(addresses))
    out += b"".join(struct.pack(">H", kind) + _blob(value) for kind, value in addresses)
    out += struct.pack(">I", len(authdata))
    out += b"".join(struct.pack(">H", kind) + _blob(value) for kind, value in authdata)
    out += _blob(der) + _blob(b"")
    return out


def write_ccache(path, entries, *, version=CCACHE_VERSION_4, default=("jane",)):
    """A FILE credential cache, in the format MIT and Heimdal write."""
    out = struct.pack(">H", version)
    if version == CCACHE_VERSION_4:
        out += struct.pack(">H", 0)  # no header tags
    out += _principal(default)
    for client, server, end_time in entries:
        out += _credential(client, server, end_time=end_time, version=version)
    path.write_bytes(out)
    return str(path)


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


# -- the cache reader -------------------------------------------------------


def test_a_version_4_cache_reads_back(cache):
    default, found = read_ccache(cache)
    assert str(default) == f"jane@{REALM}"
    assert [str(ticket.server) for ticket in found] == [
        f"krbtgt/{REALM}@{REALM}",
        f"xrootd/srv.example.org@{REALM}",
    ]
    assert all(str(ticket.client) == f"jane@{REALM}" for ticket in found)


def test_a_version_3_cache_reads_back(tmp_path):
    """Version 3 repeats the enctype; getting that wrong desynchronises everything."""
    path = write_ccache(
        tmp_path / "v3",
        [(("jane",), ("krbtgt", REALM), time.time() + 60)],
        version=CCACHE_VERSION_3,
    )
    _default, found = read_ccache(path)
    assert len(found) == 1
    assert found[0].enctype == 18


def test_the_ticket_fields_survive_the_round_trip(cache):
    ticket = read_ccache(cache)[1][0]
    assert ticket.enctype == 18
    assert ticket.flags == 0x40E00000
    assert ticket.der == b"\x6e\x00"
    assert ticket.auth_time == ticket.start_time == ticket.end_time - 3600
    assert ticket.renew_till == 0


def test_the_ticket_granting_ticket_is_identified(cache):
    granting, service = read_ccache(cache)[1]
    assert granting.is_tgt
    assert not service.is_tgt


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


def test_tickets_is_quiet_about_a_cache_that_is_not_there(tmp_path):
    assert tickets(str(tmp_path / "absent")) == []
    junk = tmp_path / "junk"
    junk.write_bytes(b"\x05\x04nonsense")
    assert tickets(str(junk)) == []


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
        ("xrootd/srv.example.org@EXAMPLE.ORG", "other", "xrootd/srv.example.org"),
        ("xrootd/srv.example.org", "other", "xrootd/srv.example.org"),
        ("", "srv.example.org", "xrootd/srv.example.org"),
        ("v:100,c:ssl", "srv.example.org", "xrootd/srv.example.org"),
        ("", "", "xrootd"),
        ("host/box@REALM,extra", "other", "host/box"),
    ],
)
def test_the_service_principal_follows_the_offer(params, host, expected):
    """The realm is dropped: GSSAPI derives it, and a stale one fails obscurely."""
    assert service_principal(Offer("krb5", params), host) == expected


def test_the_cache_path_follows_the_kerberos_convention(monkeypatch):
    import os

    monkeypatch.delenv("KRB5CCNAME", raising=False)
    assert default_ccache_path() == f"/tmp/krb5cc_{os.geteuid()}"
    monkeypatch.setenv("KRB5CCNAME", "FILE:/tmp/mine")
    assert default_ccache_path() == "/tmp/mine"
    monkeypatch.setenv("KRB5CCNAME", "/tmp/bare")
    assert default_ccache_path() == "/tmp/bare"
    monkeypatch.setenv("KRB5CCNAME", "KEYRING:persistent:1000")
    assert default_ccache_path() == f"/tmp/krb5cc_{os.geteuid()}"  # not a file we can read


# -- the mechanism ----------------------------------------------------------


def test_krb5_is_registered_even_without_gssapi():
    """Registering is how the ladder can explain itself; using it needs the extra."""
    assert registry()["krb5"] is KerberosCredential


def test_a_credential_reprs_by_its_target():
    credential = KerberosCredential("xrootd/srv.example.org")
    assert repr(credential) == "KerberosCredential(principal='xrootd/srv.example.org')"


def test_available_is_silent_when_there_is_no_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("KRB5CCNAME", f"FILE:{tmp_path / 'absent'}")
    assert KerberosCredential.available(OFFER, Config(), username="jane", host="srv") is None


def test_available_is_silent_when_every_ticket_has_expired(monkeypatch, tmp_path):
    """The common case. A GSSAPI error five layers down would be worse."""
    path = write_ccache(tmp_path / "old", [(("jane",), ("krbtgt", REALM), time.time() - 60)])
    monkeypatch.setenv("KRB5CCNAME", f"FILE:{path}")
    assert KerberosCredential.available(OFFER, Config(), username="jane", host="srv") is None


@pytest.mark.skipif(HAS_GSSAPI, reason="gssapi is installed")
def test_a_live_ticket_without_gssapi_says_so(monkeypatch, cache):
    """Falling through to unix with a valid ticket sitting there is unhelpful."""
    monkeypatch.setenv("KRB5CCNAME", f"FILE:{cache}")
    with pytest.raises(CredentialError, match="gssapi module is not installed"):
        KerberosCredential.available(OFFER, Config(), username="jane", host="srv")


@pytest.mark.skipif(HAS_GSSAPI, reason="gssapi is installed")
def test_the_ladder_turns_that_into_a_reason_not_a_failure(monkeypatch, cache):
    """``select`` must survive a mechanism that raises, and record why."""
    from xrdclient.auth import select

    monkeypatch.setenv("KRB5CCNAME", f"FILE:{cache}")
    rejected: dict[str, str] = {}
    chosen = list(
        select("&P=krb5&P=unix", Config(), username="jane", host="srv", rejected=rejected)
    )
    assert [credential.name for credential in chosen] == ["unix"]
    assert "gssapi" in rejected["krb5"]


@pytest.mark.skipif(HAS_GSSAPI, reason="gssapi is installed")
def test_the_exchange_itself_names_the_missing_extra():
    with pytest.raises(CredentialError, match=r"pip install xrdclient\[krb5\]"):
        KerberosCredential("xrootd/srv").initial()


def test_a_completed_context_stops_stepping():
    credential = KerberosCredential("xrootd/srv")
    credential._established = True
    assert credential.step(b"anything") is None


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


# -- the GSSAPI exchange, with the extra stubbed out ------------------------


@pytest.fixture
def fake_gssapi(monkeypatch):
    """A stand-in for the extra: the round-driving is ours, the crypto is not."""
    module = types.ModuleType("gssapi")

    class Name:
        def __init__(self, name, name_type=None):
            self.name, self.name_type = name, name_type

    class SecurityContext:
        answer: object = None

        def __init__(self, name=None, usage=""):
            self.name, self.usage, self.complete = name, usage, False
            self.tokens: list[bytes | None] = []

        def step(self, token=None):
            self.tokens.append(token)
            if callable(type(self).answer):
                return type(self).answer(self)
            self.complete = len(self.tokens) > 1
            return b"round-%d" % len(self.tokens)

    module.Name = Name
    module.SecurityContext = SecurityContext
    module.NameType = types.SimpleNamespace(kerberos_principal="krb5-principal")
    monkeypatch.setitem(sys.modules, "gssapi", module)
    return module


def test_the_exchange_runs_to_completion_and_then_stops(fake_gssapi):
    credential = KerberosCredential("xrootd/srv.example.org@EXAMPLE.ORG")
    assert credential.initial() == b"krb5\x00round-1"
    assert credential.step(b"challenge") == b"krb5\x00round-2"
    assert credential.step(b"anything") is None  # the context is established
    assert credential._context.name.name == "xrootd/srv.example.org@EXAMPLE.ORG"
    assert credential._context.tokens == [None, b"challenge"]


def test_a_gssapi_failure_is_reported_as_a_credential_error(fake_gssapi, monkeypatch):
    def explode(self):
        raise RuntimeError("no ticket for that service")

    monkeypatch.setattr(fake_gssapi.SecurityContext, "answer", explode)
    with pytest.raises(CredentialError, match="Kerberos exchange failed"):
        KerberosCredential("xrootd/srv").initial()


def test_a_context_that_neither_completes_nor_answers_is_refused(fake_gssapi, monkeypatch):
    monkeypatch.setattr(fake_gssapi.SecurityContext, "answer", lambda self: b"")
    with pytest.raises(CredentialError, match="produced no token and did not complete"):
        KerberosCredential("xrootd/srv").initial()


def test_a_live_ticket_and_the_extra_installed_yields_a_credential(fake_gssapi, monkeypatch, cache):
    monkeypatch.setenv("KRB5CCNAME", f"FILE:{cache}")
    credential = KerberosCredential.available(OFFER, Config(), username="jane", host="srv")
    assert credential is not None
    assert credential.principal == "xrootd/srv.example.org"  # the realm is GSSAPI's to find
