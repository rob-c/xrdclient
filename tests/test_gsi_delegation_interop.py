"""GSI delegation against the genuine article, and against the stock client.

A real ``xrootd`` with ``XrdSecgsi`` (``-dlgpxy``, ``-exppxy``) is the judge of
whether a delegation happened: it writes the proxy it received to disk, and
``openssl verify`` - not this package - decides whether that is a valid proxy
chain for the user. The stock client, where installed, is run against the same
server so the two can be compared: with ``XrdSecGSIDELEGPROXY`` unset nothing
is delegated by either, with it set both delegate, and the certificates they
sign have the same shape.

Skipped cleanly where ``xrootd``, its GSI plugin or ``openssl`` is missing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import _gsi
import xrdclient
from xrdclient.crypto.delegation import proxy_path_length
from xrdclient.crypto.rsa import load_private_key
from xrdclient.crypto.x509 import (
    PROXY_CERT_INFO_OID,
    Certificate,
    extensions_of,
    load_certificates,
    load_proxy,
)

pytestmark = [
    pytest.mark.interop,
    pytest.mark.skipif(
        not _gsi.available(), reason="needs xrootd, libXrdSecgsi and openssl on this machine"
    ),
]

KEY_IDENTIFIERS = {"2.5.29.14", "2.5.29.35"}


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    return _gsi.PKI(tmp_path_factory.mktemp("pki"))


@pytest.fixture(scope="module", params=[1, 2], ids=["dlgpxy1", "dlgpxy2"])
def server(request, pki, tmp_path_factory):
    export = tmp_path_factory.mktemp("export")
    (export / "hello.txt").write_text("hi\n")
    with _gsi.GSIXrootd(export, pki, dlgpxy=request.param) as srv:
        yield srv


@pytest.fixture
def fresh(server):
    server.forget()
    return server


def config(pki, **changes) -> xrdclient.Config:
    """GSI only, no pooling: every call is a login the server sees."""
    base = xrdclient.Config(
        proxy=str(pki.proxy),
        ca_path=str(pki.ca_dir),
        auth_order=("gsi",),
        pool_size=0,
        request_timeout=30.0,
        connect_timeout=30.0,
    )
    return base.evolve(**changes)


def login(server, cfg) -> None:
    assert xrdclient.stat(server.url + server.path("hello.txt"), config=cfg).size == 3


def only_delegated(server) -> Path:
    (path,) = server.delegated()
    return path


def leaf_of(path: Path, tmp_path: Path) -> tuple[Certificate, Path]:
    certificate = load_certificates(path.read_bytes())[0]
    leaf = tmp_path / "leaf.pem"
    leaf.write_bytes(certificate.pem())
    return certificate, leaf


# -- this client ---------------------------------------------------------------


def test_a_delegating_login_leaves_a_valid_proxy_on_the_server(fresh, pki, tmp_path):
    login(fresh, config(pki, gsi_delegate=True))
    path = only_delegated(fresh)
    assert "proxy chain dumped" in fresh.log()

    issued, leaf = leaf_of(path, tmp_path)
    ours = load_proxy(str(pki.proxy)).certificate
    assert issued.issuer == ours.subject
    assert issued.subject.rdns[:-1] == ours.subject.rdns
    assert issued.subject.rdns[-1][1] == str(issued.serial)
    assert issued.not_after == ours.not_after
    assert issued.is_proxy

    verdict = pki.verify(leaf)
    assert verdict.returncode == 0, verdict.stdout + verdict.stderr
    # The file is a usable proxy: its key is the one the certificate certifies,
    # and the chain behind it is the user's.
    assert load_private_key(path.read_bytes()).public == issued.public_key
    chain = load_certificates(path.read_bytes())
    assert [str(c.subject) for c in chain[1:]] == [_gsi.PROXY_DN, _gsi.USER_DN]


def test_without_the_option_nothing_is_offered_or_delegated(fresh, pki, monkeypatch):
    monkeypatch.delenv("XrdSecGSIDELEGPROXY", raising=False)
    login(fresh, config(pki))
    assert fresh.delegated() == []
    assert "options req by client: 128" in fresh.log()


def test_the_stock_environment_variable_turns_it_on(fresh, pki, monkeypatch):
    monkeypatch.setenv("XrdSecGSIDELEGPROXY", "1")
    login(fresh, config(pki).evolve(gsi_delegate=xrdclient.Config().gsi_delegate))
    assert len(fresh.delegated()) == 1


def test_a_server_that_cannot_be_verified_is_logged_into_but_not_given_a_proxy(fresh, pki, caplog):
    login(fresh, config(pki, gsi_delegate=True, ca_path=str(pki.empty_ca_dir)))
    assert fresh.delegated() == []
    assert "not delegating the X.509 proxy" in caplog.text
    assert "Not allowed to sign proxy requests" in fresh.log()


# -- the stock client, on the same server -------------------------------------------

STOCK = """
import sys
from XRootD import client
status, _ = client.FileSystem(sys.argv[1]).stat(sys.argv[2])
sys.exit(0 if status.ok else 1)
"""


def _stock_bindings() -> bool:
    probe = subprocess.run(
        [sys.executable, "-c", "import XRootD.client"], capture_output=True, check=False
    )
    return probe.returncode == 0


def stock_login(server, pki, delegate: str | None) -> None:
    """One ``stat`` through the official bindings, in a process of their own.

    The GSI plugin reads its environment once per process, so each setting
    of ``XrdSecGSIDELEGPROXY`` needs a fresh one.
    """
    env = {**os.environ, "X509_USER_PROXY": str(pki.proxy), "X509_CERT_DIR": str(pki.ca_dir)}
    env.pop("XrdSecGSIDELEGPROXY", None)
    if delegate is not None:
        env["XrdSecGSIDELEGPROXY"] = delegate
    done = subprocess.run(
        [sys.executable, "-c", STOCK, server.url, server.path("hello.txt")],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.skipif(not _stock_bindings(), reason="the official XRootD bindings are not installed")
def test_the_stock_client_delegates_exactly_when_this_one_does(fresh, pki):
    stock_login(fresh, pki, None)
    assert fresh.delegated() == []
    stock_login(fresh, pki, "0")
    assert fresh.delegated() == []
    stock_login(fresh, pki, "1")
    assert len(fresh.delegated()) == 1


def shape(certificate: Certificate) -> list[tuple[str, bool]]:
    """Its extensions and their criticality, key identifiers aside.

    XrdCrypto copies the signer's key identifiers onto the new certificate;
    this client deliberately does not (see :mod:`xrdclient.crypto.delegation`).
    """
    return [
        (e.oid, e.critical) for e in extensions_of(certificate.der) if e.oid not in KEY_IDENTIFIERS
    ]


def path_length(certificate: Certificate) -> int | None:
    pci = next(e for e in extensions_of(certificate.der) if e.oid == PROXY_CERT_INFO_OID)
    return proxy_path_length(pci)


@pytest.mark.skipif(not _stock_bindings(), reason="the official XRootD bindings are not installed")
def test_the_proxies_both_clients_sign_have_the_same_shape(fresh, pki, tmp_path):
    stock_login(fresh, pki, "1")
    theirs, _ = leaf_of(only_delegated(fresh), tmp_path)
    fresh.forget()
    login(fresh, config(pki, gsi_delegate=True))
    ours, _ = leaf_of(only_delegated(fresh), tmp_path)

    assert ours.issuer == theirs.issuer
    assert len(ours.subject.rdns) == len(theirs.subject.rdns)
    assert ours.not_after == theirs.not_after
    assert ours.public_key.e == theirs.public_key.e
    assert ours.public_key.n.bit_length() == theirs.public_key.n.bit_length()

    assert shape(ours) == shape(theirs)
    assert path_length(ours) == path_length(theirs) is None


@pytest.mark.skipif(_gsi.shutil.which("xrdcp") is None, reason="no xrdcp on PATH")
def test_xrdcp_delegates_only_for_a_delegated_tpc_whatever_the_environment(fresh, pki, tmp_path):
    """``xrdcp`` sets ``XrdSecGSIDELEGPROXY`` itself: on for ``--tpc delegate``, else off."""
    env = {
        **os.environ,
        "X509_USER_PROXY": str(pki.proxy),
        "X509_CERT_DIR": str(pki.ca_dir),
        "XrdSecGSIDELEGPROXY": "1",
    }
    done = subprocess.run(
        ["xrdcp", "-f", fresh.url + fresh.path("hello.txt"), str(tmp_path / "copy")],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert fresh.delegated() == []
