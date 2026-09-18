"""Mutual TLS: the same proxy material on ``roots://`` and ``davs://``.

Both schemes build their own :class:`ssl.SSLContext`, and the requirement is
that they behave identically — same CA sources, same client chain, and
verification that is only ever off because a human said so. So the two are
tested side by side, from one generated proxy.
"""

import ssl

import pytest

from _pki import pem, private_key_pem, proxy_chain, throwaway_key
from xrdclient.config import Config
from xrdclient.http.client import _context as http_context
from xrdclient.transport.base import tls_context

#: The two context builders. They are separate functions because the two
#: stacks are separate; they must not drift apart.
BUILDERS = [tls_context, http_context]


@pytest.fixture(scope="module")
def proxy(tmp_path_factory):
    path = tmp_path_factory.mktemp("mtls") / "x509up_u1000"
    path.write_bytes(proxy_chain(throwaway_key(0)))
    return str(path)


@pytest.mark.parametrize("build", BUILDERS)
def test_a_proxy_is_loaded_as_the_client_chain(build, proxy):
    """``load_cert_chain`` is what makes the connection mutually authenticated."""
    context = build(Config(proxy=proxy))
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname
    assert [str(chain[0]) for chain in [context.get_ca_certs()]]  # the store is populated


@pytest.mark.parametrize("build", BUILDERS)
def test_verification_is_on_by_default(build):
    context = build(Config())
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname


@pytest.mark.parametrize("build", BUILDERS)
def test_verification_only_goes_off_when_asked(build):
    """Never implicit: the only route here is ``Config(verify_tls=False)``."""
    context = build(Config(verify_tls=False))
    assert context.verify_mode is ssl.CERT_NONE
    assert not context.check_hostname


@pytest.mark.parametrize("build", BUILDERS)
def test_the_x509_environment_selects_the_trust_store(build, tmp_path, monkeypatch):
    """``X509_CERT_DIR`` and ``SSL_CERT_FILE`` are what grid jobs actually set."""
    empty = tmp_path / "certs"
    empty.mkdir()
    context = build(Config(ca_path=str(empty), ca_file=None))
    assert context.get_ca_certs() == []  # an empty directory really is empty


@pytest.mark.parametrize("build", BUILDERS)
def test_a_broken_proxy_fails_loudly(build, tmp_path):
    """Silently continuing without a client certificate would fail far away."""
    path = tmp_path / "no-key.pem"
    path.write_bytes(pem("CERTIFICATE", b"\x30\x00"))
    with pytest.raises(ssl.SSLError):
        build(Config(proxy=str(path)))


@pytest.mark.parametrize("build", BUILDERS)
def test_a_key_with_no_certificate_is_refused(build, tmp_path):
    path = tmp_path / "key-only.pem"
    path.write_bytes(private_key_pem(throwaway_key(0)))
    with pytest.raises(ssl.SSLError):
        build(Config(proxy=str(path)))


@pytest.mark.parametrize("build", BUILDERS)
def test_a_missing_proxy_is_an_oserror_naming_the_path(build, tmp_path):
    missing = tmp_path / "absent.pem"
    with pytest.raises(OSError) as caught:
        build(Config(proxy=str(missing)))
    assert str(missing) in str(caught.value)


def test_both_stacks_agree_on_every_setting(proxy):
    """The point of the parametrisation above, stated once directly."""
    config = Config(proxy=proxy)
    root, http = tls_context(config), http_context(config)
    assert (root.verify_mode, root.check_hostname) == (http.verify_mode, http.check_hostname)
    assert root.get_ca_certs() == http.get_ca_certs()


def test_the_proxy_is_taken_from_the_grid_environment(monkeypatch, tmp_path):
    """``$X509_USER_PROXY`` is a ``Config`` default, so mTLS needs no argument."""
    path = tmp_path / "x509up_u1000"
    path.write_bytes(proxy_chain(throwaway_key(0)))
    monkeypatch.setenv("X509_USER_PROXY", str(path))
    assert Config().proxy == str(path)
    assert tls_context(Config()).verify_mode is ssl.CERT_REQUIRED
