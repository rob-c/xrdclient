"""HTTPS end to end: the stdlib connection, the shared context, trust and revocation.

Every test here completes (or deliberately fails) a real TLS handshake with
the in-tree :class:`~xrdclient.testing.FakeDAVServer`, so what is checked is
what a storage element would see: SNI and the host name in the certificate,
the client's proxy chain, which CAs vouch for the server, and whether a CRL
in the grid CA directory is consulted.

The PKI is the shared test PKI (:mod:`xrdclient.testing.pki`), minted per
module: a CA, a server certificate for ``localhost`` only, and CRLs that do
and do not revoke it, laid out the way ``fetch-crl`` lays out a grid CA directory -
``<hash>.0`` for the CA and ``<hash>.r0`` for its CRL.
"""

from __future__ import annotations

import os
import ssl
import threading
from pathlib import Path

import pytest
from cryptography import x509
from xrdsec import tls as base  # the builder and its cache, under every scheme
from xrdsec.testing.rawpki import proxy_chain, throwaway_key

from xrdclient.config import Config
from xrdclient.errors import ConnectionError as XRDConnectionError
from xrdclient.http import HTTPClient
from xrdclient.http.file import open_http
from xrdclient.testing import FakeDAVServer
from xrdclient.testing import pki as testing_pki
from xrdclient.transport.base import CRL_ENV, SYSTEM_CA_ENV, clear_tls_cache, tls_context
from xrdclient.url import XRootDURL, parse


def _minted(label: str, root, key_slots: tuple[int, int, int]) -> testing_pki.PKI:
    """A test PKI from :mod:`xrdclient.testing.pki` whose CA is called ``<label> CA``.

    The host certificate names ``localhost`` only - not ``127.0.0.1`` - so a
    test can dial the address and watch host-name checking refuse it.
    """

    class Labelled(testing_pki.PKI):
        CA_SUBJECT = (("CN", f"{label} CA"),)

    return Labelled.create(root, hosts=("localhost",), host_cn="localhost", key_slots=key_slots)


class PKI:
    """One CA, the server certificate it signed, CRLs to taste, and a user's proxy.

    A thin layer over the shared test PKI that adds what these tests vary:
    a fresh grid CA directory per test, with or without a CRL beside the CA.
    """

    def __init__(self, label: str, root, key_slots: tuple[int, int, int] = (0, 1, 2)) -> None:
        self.minted = _minted(label, root, key_slots)
        self.hash = self.minted.ca_hash

    def crl(self, *, revoked: bool) -> bytes:
        """A CRL from the CA that does, or does not, revoke the server certificate."""
        return self.minted.crl([self.minted.host.serial_number] if revoked else [])

    def proxy_file(self) -> str:
        """The PKI's RFC 3820 proxy file: proxy, its key, the user certificate."""
        return str(self.minted.proxy_path)

    def ca_pem(self) -> bytes:
        return self.minted.ca_file.read_bytes()

    def ca_dir(self, path, *, crl: bytes | None = None) -> str:
        """A grid-style CA directory: ``<hash>.0``, and ``<hash>.r0`` if given a CRL."""
        path.mkdir(exist_ok=True)
        (path / f"{self.hash}.0").write_bytes(self.ca_pem())
        if crl is not None:
            (path / f"{self.hash}.r0").write_bytes(crl)
        return str(path)

    def server_context(self, *, client_ca: bool = False) -> ssl.SSLContext:
        """The host certificate; with ``client_ca``, a client proxy is demanded and verified."""
        return self.minted.server_context(require_client=client_ca)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    return PKI("Grid", tmp_path_factory.mktemp("pki"))


@pytest.fixture(autouse=True)
def _quiet_trust_environment(monkeypatch):
    """Neither the developer's environment nor an earlier test's contexts leak in."""
    for name in (SYSTEM_CA_ENV, CRL_ENV, "SSL_CERT_DIR"):
        monkeypatch.delenv(name, raising=False)
    clear_tls_cache()
    yield
    clear_tls_cache()


@pytest.fixture
def server(pki, tmp_path):
    with FakeDAVServer(files={"/d/f": b"over tls"}, tls=pki.server_context()) as srv:
        yield srv


def _davs(server: FakeDAVServer, path: str = "/d/f", host: str = "localhost") -> XRootDURL:
    return parse(f"davs://{host}:{server.address[1]}{path}")


# ----------------------------------------------------------------------
# The connection itself
# ----------------------------------------------------------------------


def test_a_davs_read_verifies_the_server_against_the_grid_ca_directory(pki, server, tmp_path):
    names = []
    server.tls.sni_callback = lambda sock, name, context: names.append(name)
    config = Config(ca_path=pki.ca_dir(tmp_path / "certs"), ca_file=None)
    with HTTPClient(config) as client:
        assert client.request("GET", _davs(server)).body == b"over tls"
        assert client.request("GET", _davs(server)).body == b"over tls"
    assert len(server.peer_certificates) == 1  # one handshake, then reuse
    assert names == ["localhost"]  # SNI names the host, so a shared front end can route it


def test_the_certificate_must_name_the_host_that_was_dialled(pki, server, tmp_path):
    """Hostname checking: the certificate names ``localhost``, not its address."""
    config = Config(ca_path=pki.ca_dir(tmp_path / "certs"), ca_file=None)
    with HTTPClient(config) as client, pytest.raises(XRDConnectionError, match="match"):
        client.request("GET", _davs(server, host="127.0.0.1"))


def test_a_server_from_an_unknown_ca_is_refused(server, tmp_path):
    other = PKI("Other", tmp_path / "other", key_slots=(5, 6, 7))
    config = Config(ca_path=other.ca_dir(tmp_path / "certs"), ca_file=None)
    with HTTPClient(config) as client, pytest.raises(XRDConnectionError, match="verif"):
        client.request("GET", _davs(server))


def test_the_proxy_is_presented_as_the_client_certificate(pki, tmp_path):
    """The chain in ``$X509_USER_PROXY`` reaches the server, which verifies it."""
    proxy = pki.proxy_file()
    context = pki.server_context(client_ca=True)
    config = Config(ca_path=pki.ca_dir(tmp_path / "certs"), ca_file=None, proxy=proxy)
    with FakeDAVServer(files={"/d/f": b"x"}, tls=context) as srv:
        with HTTPClient(config) as client:
            client.request("GET", _davs(srv))
        presented = x509.load_der_x509_certificate(srv.peer_certificates[0])
    leaf = x509.load_pem_x509_certificate(Path(proxy).read_bytes())
    assert presented == leaf  # the proxy itself, not the user certificate behind it


def test_an_https_file_streams_over_the_cached_context(pki, server, tmp_path, monkeypatch):
    """Many connections, one context: the trust store and proxy are read once."""
    config = Config(ca_path=pki.ca_dir(tmp_path / "certs"), ca_file=None)
    built = []
    real = base._build

    def counting(*a):
        built.append(a)
        return real(*a)

    monkeypatch.setattr(base, "_build", counting)
    for _ in range(3):
        with open_http(_davs(server), "rb", config=config) as fh:
            assert fh.read() == b"over tls"
    assert len(built) == 1
    assert len(server.peer_certificates) == 3


# ----------------------------------------------------------------------
# Which CAs are trusted
# ----------------------------------------------------------------------


@pytest.fixture
def system_store(pki, tmp_path, monkeypatch):
    """Make the "system" trust store a file that holds only ``pki``'s CA.

    ``load_default_certs`` honours ``$SSL_CERT_FILE``, which is how a test can
    know what the system store contains.
    """
    path = tmp_path / "system.pem"
    path.write_bytes(pki.ca_pem())
    monkeypatch.setenv("SSL_CERT_FILE", str(path))
    return path


@pytest.mark.parametrize(
    ("scheme", "override", "env", "trusted"),
    [
        ("davs", None, None, False),  # the grid's scheme: IGTF CAs only
        ("https", None, None, True),  # the web's: the system store as well
        ("davs", True, None, True),
        ("https", False, None, False),
        ("https", None, "0", False),  # the environment wins, either way
        ("davs", None, "1", True),
        ("davs", False, "yes", True),
    ],
)
def test_the_system_store_joins_the_grid_cas_by_scheme(
    system_store, server, tmp_path, monkeypatch, scheme, override, env, trusted
):
    """With a grid CA directory that lacks the server's CA, only the system
    store can vouch for it - so whether the request succeeds says which
    stores were consulted."""
    if env is not None:
        monkeypatch.setenv(SYSTEM_CA_ENV, env)
    unrelated = PKI("Unrelated", tmp_path / "unrelated", key_slots=(5, 6, 7))
    grid = unrelated.ca_dir(tmp_path / "grid")
    url = parse(f"{scheme}://localhost:{server.address[1]}/d/f")
    with HTTPClient(Config(ca_path=grid, ca_file=None), system_ca=override) as client:
        if trusted:
            assert client.request("GET", url).body == b"over tls"
        else:
            with pytest.raises(XRDConnectionError, match="verif"):
                client.request("GET", url)


def test_without_a_grid_directory_the_system_store_is_the_store(system_store, server):
    with HTTPClient(Config(ca_path=None, ca_file=None)) as client:
        assert client.request("GET", _davs(server)).body == b"over tls"


def test_davs_and_https_to_one_host_do_not_share_a_connection(system_store, pki, tmp_path):
    """A connection verified with the system store must not carry a ``davs://`` request."""
    config = Config(ca_path=pki.ca_dir(tmp_path / "grid"), ca_file=None)
    with FakeDAVServer(files={"/f": b"x"}, tls=pki.server_context()) as srv:
        port = srv.address[1]
        with HTTPClient(config) as client:
            client.request("GET", parse(f"https://localhost:{port}/f"))
            client.request("GET", parse(f"davs://localhost:{port}/f"))
            assert client.idle_count() == 2  # one per trust store
    assert len(srv.peer_certificates) == 2


def test_an_s3_filesystem_trusts_the_system_store():
    from xrdclient.s3 import S3FileSystem

    with S3FileSystem("s3://bucket", credentials=None, endpoint="https://s3.example") as fs:
        assert fs.client.system_ca is True


def test_plain_http_never_asks_for_a_trust_store():
    with HTTPClient(Config(), system_ca=True) as client:
        assert not client.system_ca_for(parse("http://h/f"))


# ----------------------------------------------------------------------
# Revocation
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("crl", "check", "outcome"),
    [
        ("revoked", None, "ok"),  # off unless asked for
        ("revoked", "1", "revoked"),
        ("revoked", "leaf", "revoked"),
        ("revoked", "chain", "revoked"),
        ("clean", "1", "ok"),
        ("clean", "chain", "ok"),
        ("revoked", "0", "ok"),
        (None, "1", "CRL"),  # a CA without a CRL fails closed
    ],
)
def test_crls_in_the_ca_directory_are_checked_when_asked(
    pki, server, tmp_path, monkeypatch, crl, check, outcome
):
    if check is not None:
        monkeypatch.setenv(CRL_ENV, check)
    body = None if crl is None else pki.crl(revoked=crl == "revoked")
    config = Config(ca_path=pki.ca_dir(tmp_path / "certs", crl=body), ca_file=None)
    with HTTPClient(config) as client:
        if outcome == "ok":
            assert client.request("GET", _davs(server)).body == b"over tls"
        else:
            with pytest.raises(XRDConnectionError, match=outcome):
                client.request("GET", _davs(server))


def test_a_crl_that_appears_later_is_picked_up(pki, server, tmp_path, monkeypatch):
    """``fetch-crl`` rewriting the directory changes its mtime, and so the context."""
    monkeypatch.setenv(CRL_ENV, "1")
    certs = tmp_path / "certs"
    config = Config(ca_path=pki.ca_dir(certs, crl=pki.crl(revoked=False)), ca_file=None)
    with HTTPClient(config) as client:
        client.request("GET", _davs(server))
    revoked = certs / "new.r0"
    revoked.write_bytes(pki.crl(revoked=True))
    os.replace(revoked, certs / f"{pki.hash}.r0")
    os.utime(certs, ns=(1, 1))  # a coarse clock must not hide the change
    with HTTPClient(config) as client, pytest.raises(XRDConnectionError, match="revoked"):
        client.request("GET", _davs(server))


def test_crl_checking_needs_a_directory_and_verification():
    """A CA bundle has no CRLs to consult, and an unverified peer no chain."""
    assert not tls_context(Config(ca_path=None)).verify_flags & ssl.VERIFY_CRL_CHECK_LEAF


def test_crl_checking_is_skipped_when_verification_is_off(monkeypatch, tmp_path):
    monkeypatch.setenv(CRL_ENV, "1")
    context = tls_context(Config(ca_path=str(tmp_path), verify_tls=False))
    assert not context.verify_flags & ssl.VERIFY_CRL_CHECK_LEAF
    checked = tls_context(Config(ca_path=str(tmp_path)))
    assert checked.verify_flags & ssl.VERIFY_CRL_CHECK_LEAF


# ----------------------------------------------------------------------
# The context cache
# ----------------------------------------------------------------------


def test_one_setup_builds_one_context(tmp_path):
    config = Config(ca_path=str(tmp_path), ca_file=None)
    assert tls_context(config) is tls_context(Config(ca_path=str(tmp_path), ca_file=None))
    assert tls_context(config) is not tls_context(config, system_ca=True)
    assert tls_context(config) is not tls_context(Config(ca_path=str(tmp_path), verify_tls=False))


def test_a_renewed_proxy_gets_a_new_context(tmp_path):
    """``grid-proxy-init`` or a renewal daemon rewrites the file in place."""
    proxy = tmp_path / "x509up_u1000"
    proxy.write_bytes(proxy_chain(throwaway_key(0)))
    config = Config(proxy=str(proxy), ca_path=str(tmp_path))
    first = tls_context(config)
    assert tls_context(config) is first
    proxy.write_bytes(proxy_chain(throwaway_key(0), proxy_cn="987654321"))
    os.utime(proxy, ns=(2, 2))
    assert tls_context(config) is not first


def test_a_proxy_that_fails_to_load_is_not_remembered(tmp_path):
    """Each attempt raises again, and one that succeeds after a fix is cached."""
    proxy = tmp_path / "x509up_u1000"
    proxy.write_text("not yet")
    config = Config(proxy=str(proxy), ca_path=str(tmp_path))
    for _ in range(2):
        with pytest.raises(ssl.SSLError):
            tls_context(config)
    proxy.write_bytes(proxy_chain(throwaway_key(0)))
    os.utime(proxy, ns=(3, 3))
    assert tls_context(config) is tls_context(config)


def test_the_cache_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "_CACHE_SIZE", 2)
    contexts = []
    for index in range(3):
        directory = tmp_path / str(index)
        directory.mkdir()
        contexts.append(tls_context(Config(ca_path=str(directory))))
    assert len(base._cache) == 2
    assert tls_context(Config(ca_path=str(tmp_path / "0"))) is not contexts[0]


def test_the_cache_is_safe_to_share_between_threads(tmp_path):
    config = Config(ca_path=str(tmp_path), ca_file=None)
    found: list[ssl.SSLContext] = []

    def build():
        found.append(tls_context(config))

    threads = [threading.Thread(target=build) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(found) == 8
    assert all(context is tls_context(config) for context in found[-1:])
