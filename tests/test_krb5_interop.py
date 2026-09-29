"""Kerberos against the genuine article: a real MIT KDC and a real ``xrootd``.

Each test makes a throwaway MIT realm (``tests/_krb5kdc.py``), gets a ticket
with MIT's own ``kinit``, and logs in to an ``xrootd`` that accepts ``krb5``
and nothing else - ``XrdSecProtocolkrb5`` calling ``krb5_rd_req`` against a
keytab MIT wrote. The server's ``login as jane`` is the verdict. Nothing in
this file trusts this client's own decoder: the KDC decides whether a
TGS-REQ was well formed, and the server whether an AP-REQ or a KRB-CRED was.

Skipped cleanly where MIT krb5 or ``xrootd`` (with its krb5 plugin) is not
installed.
"""

from __future__ import annotations

import time

import pytest

import _krb5kdc
import _xrootd
import xrdclient
from xrdclient.auth import krb5
from xrdclient.auth.base import Offer
from xrdclient.auth.kerberos.ccache import read_ccache
from xrdclient.config import Config
from xrdclient.errors import AuthenticationError

pytestmark = [
    pytest.mark.interop,
    pytest.mark.skipif(
        not _krb5kdc.available(), reason="MIT krb5 (krb5kdc, kadmin.local) not installed"
    ),
    pytest.mark.skipif(not _xrootd.available(), reason="no xrootd binary on PATH"),
]

CONFIG = Config(auth_order=("krb5",), request_timeout=15.0, connect_timeout=15.0)

SHA1 = "aes256-cts-hmac-sha1-96"
SHA384 = "aes256-cts-hmac-sha384-192"


@pytest.fixture(autouse=True)
def _fresh_memory():
    """Service tickets fetched by one test must not serve another's realm."""
    krb5._FETCHED.clear()
    yield
    krb5._FETCHED.clear()


def _realm(tmp_path, monkeypatch, *enctypes: str) -> _krb5kdc.MitRealm:
    realm = _krb5kdc.MitRealm(tmp_path / "realm", enctypes=enctypes or (SHA1,)).start()
    for name, value in realm.env.items():
        monkeypatch.setenv(name, value)
    return realm


@pytest.fixture
def realm(tmp_path, monkeypatch):
    realm = _realm(tmp_path, monkeypatch)
    yield realm
    realm.stop()


def _round_trip(server: _krb5kdc.KerberizedXrootd, name: str = "k.txt") -> bytes:
    payload = f"written by jane as {name}".encode()
    with xrdclient.FileSystem(server.url, CONFIG) as fs:
        fs.write_bytes(server.path(name), payload)
        return fs.read_bytes(server.path(name))


def _servers_tickets(realm: _krb5kdc.MitRealm) -> list[str]:
    return [str(t.server) for t in read_ccache(str(realm.ccache))[1]]


# -- (a) a TGT is enough: the TGS exchange is done here, in Python ------------


def test_a_tgt_alone_logs_in_via_a_pure_python_tgs_exchange(tmp_path, realm):
    realm.kinit()
    assert _servers_tickets(realm) == [f"krbtgt/{realm.realm}@{realm.realm}"]
    with _krb5kdc.KerberizedXrootd(tmp_path / "export", realm) as server:
        assert _round_trip(server) == b"written by jane as k.txt"
        assert server.logins() == ["jane"]
    # The KDC saw our TGS-REQ and issued the ticket...
    assert "TGS_REQ" in realm.kdc_log() and realm.service in realm.kdc_log()
    # ...which stayed in this process: the user's cache is exactly as kinit left it.
    assert _servers_tickets(realm) == [f"krbtgt/{realm.realm}@{realm.realm}"]


def test_the_kdc_is_asked_once_per_process_not_once_per_connection(tmp_path, realm):
    realm.kinit()
    offer = Offer("krb5", realm.service)
    first = krb5.KerberosCredential.available(offer, CONFIG, username="jane", host="localhost")
    again = krb5.KerberosCredential.available(offer, CONFIG, username="jane", host="localhost")
    assert first is not None and again is not None
    assert first._ticket is again._ticket
    assert realm.kdc_log().count("TGS_REQ") == 1


# -- (b) a service ticket already in the cache needs no KDC at all ------------


def test_a_cached_service_ticket_is_used_without_the_kdc(tmp_path, realm, monkeypatch):
    realm.kinit()
    realm.kvno(realm.service)  # MIT fetches and caches the service ticket
    realm.stop()  # and now there is no KDC to ask
    dead = tmp_path / "dead-krb5.conf"
    dead.write_text(realm.krb5_conf.read_text().replace(f":{realm.port}", ":9"))
    monkeypatch.setenv("KRB5_CONFIG", str(dead))
    with _krb5kdc.KerberizedXrootd(tmp_path / "export", realm) as server:
        assert _round_trip(server) == b"written by jane as k.txt"
        assert server.logins() == ["jane"]


# -- (c) both enctype families, end to end -----------------------------------


@pytest.mark.parametrize("enctype, number", [(SHA1, 18), (SHA384, 20)])
def test_each_aes_family_logs_in(tmp_path, monkeypatch, enctype, number):
    realm = _realm(tmp_path, monkeypatch, enctype)
    try:
        realm.kinit()
        offer = Offer("krb5", realm.service)
        credential = krb5.KerberosCredential.available(
            offer, CONFIG, username="jane", host="localhost"
        )
        assert credential is not None
        assert credential._ticket.enctype == number  # the session key the KDC chose
        assert f"etypes {{rep={number}" in realm.kdc_log() or str(number) in realm.kdc_log()
        with _krb5kdc.KerberizedXrootd(tmp_path / "export", realm) as server:
            assert _round_trip(server) == b"written by jane as k.txt"
            assert server.logins() == ["jane"]
    finally:
        realm.stop()


def test_the_kdc_can_be_reached_over_tcp_only(tmp_path, realm, monkeypatch):
    """``kdc = tcp/host:port``: the length-prefixed framing, against the real KDC."""
    realm.kinit()
    tcp = tmp_path / "tcp-krb5.conf"
    tcp.write_text(realm.krb5_conf.read_text().replace("kdc = 127", "kdc = tcp/127"))
    monkeypatch.setenv("KRB5_CONFIG", str(tcp))
    with _krb5kdc.KerberizedXrootd(tmp_path / "export", realm) as server:
        assert _round_trip(server) == b"written by jane as k.txt"


# -- (d) and (e): the failures a person meets, in words they can act on -------


def test_an_expired_ticket_says_so_and_says_kinit(tmp_path, realm):
    realm.kinit("-l", "2s")
    time.sleep(3)
    with _krb5kdc.KerberizedXrootd(tmp_path / "export", realm) as server:
        with pytest.raises(AuthenticationError) as info:
            _round_trip(server)
        assert server.logins() == []
    message = str(info.value)
    assert "expired" in message and "run kinit" in message and f"jane@{realm.realm}" in message


def test_a_service_principal_the_kdc_does_not_know_is_named(tmp_path, realm):
    realm.kinit()
    wrong = f"xrootd/nosuch@{realm.realm}"
    with _krb5kdc.KerberizedXrootd(tmp_path / "export", realm, principal=wrong) as server:
        with pytest.raises(AuthenticationError) as info:
            _round_trip(server)
    assert f"the KDC has no principal {wrong}" in str(info.value)
    assert "KDC error 7" in str(info.value)


# -- forwarding: xrootd -exptkn ----------------------------------------------


def test_a_forwarded_tgt_reaches_an_exptkn_server_and_is_written_where_it_says(tmp_path, realm):
    realm.kinit("-f")
    exported = tmp_path / "fwd_<user>"
    with _krb5kdc.KerberizedXrootd(
        tmp_path / "export", realm, export_tickets=str(exported)
    ) as server:
        assert _round_trip(server) == b"written by jane as k.txt"
        assert server.logins() == ["jane"]
    written = tmp_path / "fwd_jane"
    # MIT's own klist reads what the server's krb5_rd_cred accepted and stored...
    listing = realm.klist(written)
    assert f"krbtgt/{realm.realm}@{realm.realm}" in listing
    assert "Flags: Ff" in listing or "Ff" in listing  # forwardable, forwarded
    # ...and it is a ticket the realm's KDC will honour: MIT kvno uses it.
    default, (tgt,) = read_ccache(str(written))
    assert str(default) == realm.user and tgt.is_tgt
    assert tgt.flags & 0x20000000  # forwarded


def test_an_exptkn_server_and_a_tgt_that_is_not_forwardable_is_explained(tmp_path, realm):
    realm.kinit("-F")  # explicitly not forwardable
    exported = tmp_path / "fwd_<user>"
    with _krb5kdc.KerberizedXrootd(
        tmp_path / "export", realm, export_tickets=str(exported)
    ) as server:
        with pytest.raises(AuthenticationError, match="not forwardable; run kinit -f"):
            _round_trip(server)
