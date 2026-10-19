"""``KEYRING:`` caches end to end: MIT's ``kinit`` into a kernel keyring, then a real login.

The keyring reader is :mod:`xrdsec.krb5.keyring`, tested in xrdsec's suite
against a fake ``keyctl`` filled with caches MIT wrote, and against the
real syscall on Linux. What is proved here needs xrdclient and the genuine
programs: a cache MIT's ``kinit`` wrote into a session or persistent
keyring logs in to a real ``xrootd``. Linux only, and ``interop``.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from xrdsec.krb5.caches import open_ccache
from xrdsec.krb5.keyring import KEY_SPEC_SESSION_KEYRING, Keyctl, system_keyctl

import xrdclient
from tests import _krb5kdc, _xrootd
from xrdclient.config import Config

on_linux = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="platform: kernel keyrings exist only on Linux"
)


def _real_keyctl() -> Keyctl:
    keyctl = system_keyctl()
    try:
        keyctl.keyring_id(KEY_SPEC_SESSION_KEYRING)
    except OSError as exc:  # a container's seccomp profile often forbids keyctl
        pytest.skip(f"keyctl is not permitted here: {exc}")
    return keyctl


@on_linux
@pytest.mark.interop
@pytest.mark.skipif(not _krb5kdc.available(), reason="MIT krb5 not installed")
@pytest.mark.skipif(not _xrootd.available(), reason="no xrootd binary on PATH")
@pytest.mark.parametrize("anchor", ["session", "persistent"])
def test_mit_kinit_into_a_keyring_logs_in_to_a_real_xrootd(tmp_path, monkeypatch, anchor):
    _real_keyctl()
    realm = _krb5kdc.MitRealm(tmp_path / "realm").start()
    try:
        # A session collection of the test's own; in the user's persistent
        # collection, a cache of the test's own - never the user's tickets.
        tag = f"xrdtest{os.getpid()}"
        name = (
            f"KEYRING:session:{tag}"
            if anchor == "session"
            else (f"KEYRING:persistent:{os.geteuid()}:{tag}")
        )
        env = {**os.environ, **realm.env, "KRB5CCNAME": name}
        for key, value in env.items():
            if key.startswith("KRB5"):
                monkeypatch.setenv(key, value)
        kinit, klist = _krb5kdc.KINIT, _krb5kdc.KLIST
        assert kinit is not None and klist is not None  # the skipif saw MIT krb5
        subprocess.run([kinit, "-k", "-t", str(realm.user_keytab), "jane"], env=env, check=True)
        principal, (tgt,) = open_ccache(name).read()
        assert str(principal) == realm.user and tgt.is_tgt
        config = Config(auth_order=("krb5",), request_timeout=15.0, connect_timeout=15.0)
        server = _krb5kdc.KerberizedXrootd(tmp_path / "export", realm)
        with server:
            with xrdclient.FileSystem(server.url, config) as fs:
                fs.write_bytes(server.path("k.txt"), b"from the keyring")
            assert server.logins() == ["jane"]
        subprocess.run([klist.replace("klist", "kdestroy")], env=env, check=False)
    finally:
        realm.stop()
