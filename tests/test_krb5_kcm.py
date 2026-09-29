"""``KCM:`` credential caches: the client, against a KCM daemon on a Unix socket.

There is no ``sssd-kcm`` or Heimdal ``kcm`` on a developer's Mac, so the
daemon is ``tests/_kcmd.py``: the KCM protocol, in a thread, on a socket in
a temporary directory. What keeps it honest is that MIT's own client talks
to it too - where MIT krb5 is installed, MIT's ``kinit`` fills it, MIT's
``klist`` lists it and MIT's ``kvno`` adds to it (the last section) - and
the caches it serves in the unit tests are ones MIT wrote
(``tests/_krb5_mit.py``). The end-to-end tests then log in to a real
``xrootd`` against a real MIT KDC with tickets that reached this client only
over the KCM socket.
"""

from __future__ import annotations

import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

import _kcmd
import _krb5kdc
import _xrootd
import xrdclient
from _krb5_mit import CAPTURED
from xrdclient.auth import krb5
from xrdclient.auth.base import Offer
from xrdclient.auth.kerberos import kcm
from xrdclient.auth.kerberos.caches import FileCache, open_ccache
from xrdclient.auth.kerberos.ccache import (
    marshal_principal,
    marshal_ticket,
    read_ccache,
    read_credential,
    read_credentials,
)
from xrdclient.auth.kerberos.kcm import DEFAULT_SOCKET, KcmCache, KcmError
from xrdclient.auth.kerberos.model import Principal, Ticket
from xrdclient.auth.kerberos.profile import Profile
from xrdclient.config import Config
from xrdclient.errors import CredentialError

REALM = "XRD.TEST"
ENCTYPES = sorted(CAPTURED)
JANE = Principal(("jane",), REALM, 1)


@pytest.fixture
def short_dir():
    """A directory short enough for a Unix socket path (104 bytes on macOS)."""
    path = Path(tempfile.mkdtemp(prefix="kcm", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def daemon(short_dir):
    with _kcmd.FakeKcm(short_dir / "kcm.sock") as server:
        yield server


@pytest.fixture(autouse=True)
def _fresh_memory():
    krb5._FETCHED.clear()
    yield
    krb5._FETCHED.clear()


def _captured(enctype: str, name: str) -> bytes:
    return bytes.fromhex(CAPTURED[enctype][name])


def _file_view(tmp_path, image: bytes) -> tuple[Principal, list[Ticket]]:
    path = tmp_path / "file-cache"
    path.write_bytes(image)
    return read_ccache(str(path))


def _same(ours: list[Ticket], theirs: list[Ticket]) -> None:
    """Equal field by field, the session key and the clock offset included."""
    assert ours == theirs
    assert [(t.key, t.kdc_offset) for t in ours] == [(t.key, t.kdc_offset) for t in theirs]


def _ticket(server: tuple[str, ...], *, hours: float = 10, client: Principal = JANE) -> Ticket:
    now = int(time.time())
    return Ticket(
        client=client,
        server=Principal(server, REALM, 1),
        enctype=18,
        auth_time=now,
        start_time=now,
        end_time=int(now + hours * 3600),
        renew_till=0,
        flags=0x40E00000,
        der=b"\x61\x03\x30\x01\x00",
        key=bytes(range(32)),
    )


def _fill(daemon: _kcmd.FakeKcm, name: str, *tickets: Ticket, offset: int = 0) -> None:
    cache = _kcmd._Cache(principal=marshal_principal(JANE))
    cache.offset = offset
    for number, ticket in enumerate(tickets):
        cache.creds[number.to_bytes(16, "big")] = marshal_ticket(ticket)
    daemon.caches[name] = cache


# -- reading: what the FILE reader reads, over the socket ---------------------


@pytest.mark.parametrize("enctype", ENCTYPES)
@pytest.mark.parametrize("heimdal", [False, True], ids=["mit-extensions", "heimdal"])
def test_a_kcm_cache_reads_exactly_as_the_same_file_cache(tmp_path, daemon, enctype, heimdal):
    image = _captured(enctype, "service_ccache")  # MIT kinit + kvno: a TGT and a service ticket
    daemon.load("1000", image)
    daemon.heimdal = heimdal
    cache = KcmCache("", daemon.path.as_posix())
    principal, tickets = cache.read()
    expected_principal, expected = _file_view(tmp_path, image)
    assert principal == expected_principal
    _same(tickets, expected)
    assert [str(t.server) for t in tickets] == [
        f"krbtgt/{REALM}@{REALM}",
        f"xrootd/localhost@{REALM}",
    ]
    assert cache.name == "KCM:1000"  # resolved from the daemon's default
    ops = [op for op, _ in daemon.requests]
    walk = ["GET_CRED_LIST", "GET_CRED_UUID_LIST"] + ["GET_CRED_BY_UUID"] * 3
    assert ops == ["GET_DEFAULT_CACHE", "GET_PRINCIPAL", "GET_KDC_OFFSET"] + (
        walk if heimdal else ["GET_CRED_LIST"]
    )


def test_a_named_cache_is_read_without_asking_for_the_default(daemon):
    _fill(daemon, "1000:7", _ticket(("krbtgt", REALM)))
    cache = KcmCache("1000:7", str(daemon.path))
    principal, (tgt,) = cache.read()
    assert principal.same_name(JANE) and tgt.is_tgt and tgt.key == bytes(range(32))
    assert ("GET_DEFAULT_CACHE", "") not in daemon.requests


def test_the_kdc_offset_the_daemon_keeps_reaches_every_ticket(daemon):
    _fill(daemon, "1000", _ticket(("krbtgt", REALM)), offset=-300)
    (tgt,) = KcmCache("", str(daemon.path)).read()[1]
    assert tgt.kdc_offset == -300.0


@pytest.mark.parametrize(
    "setup",
    [
        lambda d: d.fail.__setitem__("GET_KDC_OFFSET", _kcmd.KRB5_FCC_INTERNAL),
        lambda d: d.raw.__setitem__("GET_KDC_OFFSET", b"\x00\x01"),
    ],
    ids=["refused", "short"],
)
def test_a_daemon_that_keeps_no_offset_means_zero(daemon, setup):
    _fill(daemon, "1000", _ticket(("krbtgt", REALM)), offset=99)
    setup(daemon)
    assert KcmCache("", str(daemon.path)).read()[1][0].kdc_offset == 0.0


def test_configuration_entries_and_unreadable_credentials_are_left_out(daemon):
    config = _ticket(("krb5_ccache_conf_data", "fast_avail"))
    config = replace(config, server=Principal(config.server.components, "X-CACHECONF:", 1))
    _fill(daemon, "1000", config, _ticket(("krbtgt", REALM)))
    daemon.caches["1000"].creds[b"\xff" * 16] = b"\x00\x00\x00\x01truncated"
    (tgt,) = KcmCache("", str(daemon.path)).read()[1]
    assert tgt.is_tgt


def test_a_credential_that_vanishes_mid_walk_is_skipped(daemon):
    _fill(daemon, "1000", _ticket(("krbtgt", REALM)))
    daemon.heimdal = True
    real = daemon._op_get_cred_uuid_list

    def with_a_ghost(args):
        status, uuids = real(args)
        return status, uuids + b"\xee" * 16  # listed, but gone by the time it is fetched

    daemon._op_get_cred_uuid_list = with_a_ghost
    assert [t.is_tgt for t in KcmCache("", str(daemon.path)).read()[1]] == [True]


# -- no cache is not an error; a broken one is ---------------------------------


def test_no_daemon_is_no_cache(short_dir):
    with pytest.raises(FileNotFoundError, match="no KCM daemon at"):
        KcmCache("", str(short_dir / "absent.sock")).read()


def test_a_cache_the_daemon_does_not_hold_is_no_cache(daemon):
    with pytest.raises(FileNotFoundError, match="KCM:1000 not found"):
        KcmCache("", str(daemon.path)).read()
    daemon.fail["GET_PRINCIPAL"] = kcm.KRB5_CC_NOTFOUND
    with pytest.raises(FileNotFoundError):
        KcmCache("elsewhere", str(daemon.path)).read()


def test_an_uninitialized_cache_is_no_cache(daemon):
    daemon.caches["1000"] = _kcmd._Cache()  # Heimdal: status 0 and no principal
    with pytest.raises(FileNotFoundError, match="not found"):
        KcmCache("", str(daemon.path)).read()


@pytest.mark.parametrize(
    "op, status",
    [("GET_PRINCIPAL", _kcmd.KRB5_CC_IO), ("GET_CRED_LIST", _kcmd.KRB5_FCC_NOFILE)],
)
def test_a_daemon_error_is_raised_with_its_name(daemon, op, status):
    _fill(daemon, "1000", _ticket(("krbtgt", REALM)))
    daemon.fail[op] = status
    with pytest.raises(KcmError, match="KRB5_") as info:
        KcmCache("", str(daemon.path)).read()
    assert info.value.code == status and isinstance(info.value, OSError)


def test_a_failed_fetch_in_the_uuid_walk_is_raised(daemon):
    _fill(daemon, "1000", _ticket(("krbtgt", REALM)))
    daemon.heimdal = True
    daemon.fail["GET_CRED_BY_UUID"] = _kcmd.KRB5_CC_IO
    with pytest.raises(KcmError):
        KcmCache("", str(daemon.path)).read()


def test_an_unknown_status_is_named_by_number():
    assert "-5" in str(KcmError(-5, 8))


@pytest.mark.parametrize(
    "op, reply, message",
    [
        ("GET_DEFAULT_CACHE", b"1000", "unterminated name"),
        ("GET_CRED_UUID_LIST", b"\x00" * 17, "torn UUID list"),
    ],
)
def test_a_malformed_reply_is_a_value_error(daemon, op, reply, message):
    _fill(daemon, "1000", _ticket(("krbtgt", REALM)))
    daemon.heimdal = True
    daemon.raw[op] = reply
    with pytest.raises(ValueError, match=message):
        KcmCache("", str(daemon.path)).read()


def _scripted(path: Path, reply: bytes) -> threading.Thread:
    """A one-shot daemon that answers the first request with ``reply``, raw."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def serve() -> None:
        conn, _ = listener.accept()
        with conn, listener:
            _kcmd._recv_frame(conn)
            conn.sendall(reply)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize(
    "reply, error, message",
    [
        (struct.pack(">Ii", 0, -1765328191), KcmError, "KRB5_CC_IO"),  # heim-ipc transport error
        (struct.pack(">II", kcm.MAX_REPLY + 1, 0), ValueError, "too big"),
        (struct.pack(">II", 2, 0) + b"\x00\x00", ValueError, "no status"),
        (struct.pack(">II", 8, 0) + b"\x00\x00", ValueError, "closed the connection"),
    ],
    ids=["transport-status", "too-big", "no-status", "cut-short"],
)
def test_the_frame_is_checked(short_dir, reply, error, message):
    path = short_dir / "scripted.sock"
    thread = _scripted(path, reply)
    with pytest.raises(error, match=message):
        KcmCache("", str(path)).read()
    thread.join(5)


def test_a_daemon_that_is_not_listening_is_an_error_not_an_absence(short_dir):
    path = short_dir / "dead.sock"
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(path))
    dead.close()  # the socket file is there, nobody is behind it
    with pytest.raises(OSError) as info:
        KcmCache("", str(path)).read()
    assert not isinstance(info.value, FileNotFoundError)


# -- storing a ticket from the KDC ----------------------------------------------


@pytest.mark.parametrize("enctype", ENCTYPES)
def test_marshal_ticket_writes_the_bytes_mit_wrote(enctype):
    """MIT ``kvno``'s service ticket, re-marshalled by this client, is byte-identical."""
    image = _captured(enctype, "service_ccache")
    daemon = _kcmd.FakeKcm(Path("unused"))
    daemon.load("x", image)
    for original in daemon.caches["x"].creds.values():
        assert marshal_ticket(read_credential(original)) == original


def test_a_stored_ticket_is_in_the_cache_for_the_next_reader(daemon):
    _fill(daemon, "1000", _ticket(("krbtgt", REALM)))
    service = _ticket(("xrootd", "srv"))
    cache = KcmCache("", str(daemon.path))
    assert cache.store(service) is True  # resolves the default first
    assert cache.name == "KCM:1000"
    _tgt, stored = KcmCache("1000", str(daemon.path)).read()[1]
    _same([stored], [service])


def test_a_refused_store_is_not_fatal(daemon, short_dir):
    _fill(daemon, "1000", _ticket(("krbtgt", REALM)))
    daemon.fail["STORE"] = _kcmd.KRB5_CC_NOSUPP
    assert KcmCache("1000", str(daemon.path)).store(_ticket(("xrootd", "srv"))) is False
    assert KcmCache("1000", str(short_dir / "gone.sock")).store(_ticket(("x",))) is False


def test_read_credentials_skips_what_it_cannot_parse():
    good = marshal_ticket(_ticket(("krbtgt", REALM)))
    assert len(read_credentials([b"", good, good[:-3]], 0.0, "KCM:x")) == 1


# -- choosing the socket ----------------------------------------------------------


def test_the_socket_comes_from_krb5_conf_else_heimdals_default(tmp_path):
    configured = Profile.parse("[libdefaults]\n kcm_socket = /run/elsewhere.sock\n")
    cache = open_ccache("KCM:", configured)
    assert isinstance(cache, KcmCache) and cache.socket_path == "/run/elsewhere.sock"
    plain = open_ccache("KCM:1000", Profile.parse("[libdefaults]\n"))
    assert isinstance(plain, KcmCache) and plain.socket_path == DEFAULT_SOCKET
    assert plain.residual == "1000"


def test_file_and_dir_names_still_open_as_files(tmp_path):
    cache = open_ccache(f"FILE:{tmp_path}/cc", Profile.parse(""))
    assert isinstance(cache, FileCache) and cache.name == f"{tmp_path}/cc"
    assert cache.store(_ticket(("x",))) is False


@pytest.mark.parametrize("name", ["API:", "API:ABCD-1234"])
def test_a_macos_api_cache_is_refused_with_the_fix(name):
    with pytest.raises(CredentialError, match=r"XPC.*KRB5CCNAME=FILE:/tmp/krb5cc_\$\(id -u\)"):
        open_ccache(name, Profile.parse(""))


# -- the krb5 credential, from a KCM cache --------------------------------------


@pytest.fixture
def kcm_env(daemon, tmp_path, monkeypatch):
    conf = tmp_path / "krb5.conf"
    conf.write_text(f"[libdefaults]\n default_realm = {REALM}\n kcm_socket = {daemon.path}\n")
    monkeypatch.setenv("KRB5_CONFIG", str(conf))
    monkeypatch.setenv("KRB5CCNAME", "KCM:")
    return daemon


@pytest.fixture
def kdc(monkeypatch):
    asked: list[Principal] = []

    def request_ticket(tgt, server, profile, *, etypes, options=0, **_):
        asked.append(server)
        return replace(tgt, server=server, key=bytes(32), end_time=int(time.time()) + 600)

    monkeypatch.setattr(krb5, "request_ticket", request_ticket)
    return asked


OFFER = Offer("krb5", f"xrootd/srv@{REALM}")


def test_a_ticket_fetched_for_a_kcm_cache_is_stored_there_as_mit_does(kcm_env, kdc):
    _fill(kcm_env, "1000", _ticket(("krbtgt", REALM)))
    credential = krb5.KerberosCredential.available(OFFER, Config(), username="jane", host="srv")
    assert credential is not None and [str(p) for p in kdc] == [f"xrootd/srv@{REALM}"]
    assert ("STORE", "1000") in kcm_env.requests
    # A new process - no memory of the fetch - finds it in the cache, and asks no KDC.
    krb5._FETCHED.clear()
    again = krb5.KerberosCredential.available(OFFER, Config(), username="jane", host="srv")
    assert again is not None and len(kdc) == 1
    assert [str(t.server) for t in krb5.tickets()] == [f"krbtgt/{REALM}@{REALM}", str(kdc[0])]


def test_no_kcm_daemon_is_no_krb5_credential(tmp_path, short_dir, monkeypatch):
    conf = tmp_path / "krb5.conf"
    conf.write_text(f"[libdefaults]\n kcm_socket = {short_dir}/none.sock\n")
    monkeypatch.setenv("KRB5_CONFIG", str(conf))
    monkeypatch.setenv("KRB5CCNAME", "KCM:")
    assert krb5.KerberosCredential.available(OFFER, Config(), username="j", host="srv") is None
    assert krb5.tickets() == []


def test_a_broken_kcm_daemon_is_named_in_the_error(kcm_env):
    _fill(kcm_env, "1000", _ticket(("krbtgt", REALM)))
    kcm_env.fail["GET_CRED_LIST"] = _kcmd.KRB5_CC_IO
    kcm_env.fail["GET_CRED_UUID_LIST"] = _kcmd.KRB5_CC_IO
    with pytest.raises(CredentialError, match="KCM:1000 is unreadable"):
        krb5.KerberosCredential.available(OFFER, Config(), username="jane", host="srv")


def test_expired_tickets_in_a_kcm_cache_say_so(kcm_env):
    _fill(kcm_env, "1000", _ticket(("krbtgt", REALM), hours=-1))
    with pytest.raises(CredentialError, match="in KCM:1000 expired"):
        krb5.KerberosCredential.available(OFFER, Config(), username="jane", host="srv")


# -- against MIT's own KCM client, a real KDC and a real xrootd -------------------

needs_mit = pytest.mark.skipif(not _krb5kdc.available(), reason="MIT krb5 not installed")
needs_xrootd = pytest.mark.skipif(not _xrootd.available(), reason="no xrootd binary on PATH")
CONFIG = Config(auth_order=("krb5",), request_timeout=15.0, connect_timeout=15.0)


class _KcmRealm:
    """A throwaway MIT realm whose programs - and this client - use a KCM cache."""

    def __init__(self, base: Path, sock: Path, monkeypatch) -> None:
        self.realm = _krb5kdc.MitRealm(base).start()
        conf = self.realm.krb5_conf
        # kcm_mach_service: on macOS, MIT tries Mach RPC first; a name nobody
        # serves makes it fall back to the socket, as it does on Linux.
        conf.write_text(
            conf.read_text().replace(
                "[libdefaults]\n",
                f"[libdefaults]\n kcm_socket = {sock}\n kcm_mach_service = org.xrdclient.none\n",
            )
        )
        self.env = {**os.environ, **self.realm.env, "KRB5CCNAME": "KCM:"}
        for name, value in self.env.items():
            if name.startswith("KRB5"):
                monkeypatch.setenv(name, value)

    def run(self, program: str | None, *argv: str) -> str:
        assert program
        done = subprocess.run(
            [program, *argv], env=self.env, capture_output=True, text=True, timeout=60, check=False
        )
        assert done.returncode == 0, done.stdout + done.stderr
        return done.stdout

    def kinit(self, *options: str) -> None:
        self.run(_krb5kdc.KINIT, *options, "-k", "-t", str(self.realm.user_keytab), "jane")


@pytest.fixture
def kcm_realm(tmp_path, daemon, monkeypatch):
    realm = _KcmRealm(tmp_path / "realm", daemon.path, monkeypatch)
    yield realm
    realm.realm.stop()


@needs_mit
@pytest.mark.interop
@pytest.mark.parametrize("heimdal", [False, True], ids=["mit-extensions", "heimdal"])
def test_mit_kinit_fills_the_fake_daemon_and_this_client_reads_what_klist_lists(
    kcm_realm, daemon, heimdal
):
    daemon.heimdal = heimdal  # MIT falls back to INITIALIZE + STORE without REPLACE
    kcm_realm.kinit("-f")
    listing = kcm_realm.run(_krb5kdc.KLIST, "-e", "-f")
    assert "Ticket cache: KCM:1000" in listing and "Default principal: jane@XRD.TEST" in listing
    principal, (tgt,) = open_ccache().read()
    assert str(principal) == "jane@XRD.TEST" and tgt.is_tgt and tgt.forwardable
    assert f"krbtgt/{REALM}@{REALM}" in listing and len(tgt.key) == 32


@needs_mit
@needs_xrootd
@pytest.mark.interop
def test_a_tgt_in_kcm_logs_in_to_a_real_xrootd_and_mit_can_use_the_ticket_stored_back(
    tmp_path, kcm_realm, daemon
):
    kcm_realm.kinit()
    realm = kcm_realm.realm
    with _krb5kdc.KerberizedXrootd(tmp_path / "export", realm) as server:
        with xrdclient.FileSystem(server.url, CONFIG) as fs:
            fs.write_bytes(server.path("k.txt"), b"over KCM")
            assert fs.read_bytes(server.path("k.txt")) == b"over KCM"
        assert server.logins() == ["jane"]
    assert ("STORE", "1000") in daemon.requests
    # MIT's klist lists the ticket this client stored; with the KDC gone, MIT's
    # kvno finds it in the cache and is satisfied - MIT reads our marshalling.
    assert realm.service in kcm_realm.run(_krb5kdc.KLIST, "-e")
    realm.stop()
    assert "kvno = 1" in kcm_realm.run(_krb5kdc.KVNO, realm.service)


@needs_mit
@needs_xrootd
@pytest.mark.interop
def test_a_service_ticket_mit_put_in_kcm_logs_in_with_no_kdc(tmp_path, kcm_realm, monkeypatch):
    kcm_realm.kinit()
    realm = kcm_realm.realm
    kcm_realm.run(_krb5kdc.KVNO, realm.service)  # MIT fetches it and STOREs it in the daemon
    realm.stop()
    dead = tmp_path / "dead-krb5.conf"
    dead.write_text(realm.krb5_conf.read_text().replace(f":{realm.port}", ":9"))
    monkeypatch.setenv("KRB5_CONFIG", str(dead))
    with _krb5kdc.KerberizedXrootd(tmp_path / "export", realm) as server:
        with xrdclient.FileSystem(server.url, CONFIG) as fs:
            fs.write_bytes(server.path("n.txt"), b"no kdc")
        assert server.logins() == ["jane"]


@needs_mit
@needs_xrootd
@pytest.mark.interop
def test_a_forwarded_tgt_from_kcm_reaches_an_exptkn_server(tmp_path, kcm_realm):
    kcm_realm.kinit("-f")
    realm = kcm_realm.realm
    exported = tmp_path / "fwd_<user>"
    with _krb5kdc.KerberizedXrootd(
        tmp_path / "export", realm, export_tickets=str(exported)
    ) as server:
        with xrdclient.FileSystem(server.url, CONFIG) as fs:
            fs.write_bytes(server.path("f.txt"), b"forwarded")
        assert server.logins() == ["jane"]
    default, (tgt,) = read_ccache(str(tmp_path / "fwd_jane"))
    assert str(default) == realm.user and tgt.is_tgt and tgt.flags & 0x20000000


# -- against a real KCM daemon: opt in, it is the user's own ------------------------


def _system_kcm_answers() -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(2)
            probe.connect(DEFAULT_SOCKET)
    except OSError:
        return False
    return True


@needs_mit
@needs_xrootd
@pytest.mark.interop
@pytest.mark.skipif(
    os.environ.get("XRDCLIENT_TEST_SYSTEM_KCM") != "1",
    reason="set XRDCLIENT_TEST_SYSTEM_KCM=1 to use this machine's KCM daemon (sssd-kcm)",
)
def test_a_real_kcm_daemon_serves_mit_kinits_tickets_to_a_real_xrootd_login(tmp_path, monkeypatch):
    """sssd-kcm (or Heimdal's kcm) on its default socket, in a cache of the test's own."""
    if not _system_kcm_answers():
        pytest.skip(f"no KCM daemon answers on {DEFAULT_SOCKET}")
    realm = _krb5kdc.MitRealm(tmp_path / "realm").start()
    name = f"KCM:{os.geteuid()}:xrdtest{os.getpid()}"
    env = {**os.environ, **realm.env, "KRB5CCNAME": name}
    for key, value in env.items():
        if key.startswith("KRB5"):
            monkeypatch.setenv(key, value)

    def run(program: str | None, *argv: str) -> str:
        assert program
        done = subprocess.run([program, *argv], env=env, capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stdout + done.stderr
        return done.stdout

    try:
        run(_krb5kdc.KINIT, "-f", "-k", "-t", str(realm.user_keytab), "jane")
        principal, (tgt,) = open_ccache(name).read()
        assert str(principal) == realm.user and tgt.is_tgt and tgt.forwardable
        with _krb5kdc.KerberizedXrootd(tmp_path / "export", realm) as server:
            with xrdclient.FileSystem(server.url, CONFIG) as fs:
                fs.write_bytes(server.path("k.txt"), b"via sssd-kcm")
            assert server.logins() == ["jane"]
        # The service ticket this client stored, as the real daemon lists it to MIT.
        assert realm.service in run(_krb5kdc.KLIST, "-e")
        realm.stop()
        assert "kvno = 1" in run(_krb5kdc.KVNO, realm.service)
    finally:
        subprocess.run([_krb5kdc.KINIT.replace("kinit", "kdestroy")], env=env, check=False)
        realm.stop()
