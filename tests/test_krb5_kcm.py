"""``KCM:`` caches end to end: MIT's own KCM client, a real KDC and a real ``xrootd``.

The KCM reader is :mod:`xrdsec.krb5.kcm`, tested in xrdsec's suite against
:class:`xrdsec.testing.krb5.FakeKcm` with caches MIT wrote. What is proved
here needs xrdclient and the genuine programs: MIT's ``kinit`` fills that
fake daemon, ``klist`` lists it and ``kvno`` adds to it, and logins to a
real ``xrootd`` use tickets that reached this client only over the KCM
socket - including the service ticket it stored back, which MIT then uses.
Every test is ``interop`` (it needs MIT krb5, and most need ``xrootd``).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest
from xrdsec.krb5.caches import open_ccache
from xrdsec.krb5.ccache import read_ccache
from xrdsec.krb5.kcm import DEFAULT_SOCKET
from xrdsec.testing.krb5 import FakeKcm

import xrdclient
from tests import _krb5kdc, _xrootd
from xrdclient.config import Config

REALM = "XRD.TEST"


@pytest.fixture
def short_dir():
    """A directory short enough for a Unix socket path (104 bytes on macOS)."""
    path = Path(tempfile.mkdtemp(prefix="kcm", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def daemon(short_dir):
    with FakeKcm(short_dir / "kcm.sock") as server:
        yield server


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
        assert _krb5kdc.KINIT is not None  # needs_mit
        subprocess.run([_krb5kdc.KINIT.replace("kinit", "kdestroy")], env=env, check=False)
        realm.stop()
