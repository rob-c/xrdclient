"""Mutual TLS: the same proxy material on ``roots://`` and ``davs://``.

Both schemes build their own :class:`ssl.SSLContext`, and the requirement is
that they behave identically — same CA sources, same client chain, and
verification that is only ever off because a human said so. So the two are
tested side by side, from one generated proxy.
"""

import ssl

import pytest

from _pki import (
    _cached_key,
    make_certificate,
    name,
    pem,
    private_key_pem,
    proxy_chain,
    sequence,
    throwaway_key,
    tlv,
)
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


@pytest.fixture(scope="module")
def ca_file(tmp_path_factory):
    """A CA of the test's own, so the store's contents do not depend on the host's.

    A system store kept as a directory (``capath``) is read lazily, during a
    handshake, and lists nothing beforehand - which is how macOS and several
    Linux distributions ship it.
    """
    key = _cached_key(1)
    subject = name(("2.5.4.3", "Test CA"))
    constraints = (("2.5.29.19", sequence(tlv(0x01, b"\xff"))),)  # basicConstraints CA:TRUE
    certificate = make_certificate(subject, subject, key.public, key, extensions=constraints)
    path = tmp_path_factory.mktemp("ca") / "ca.pem"
    path.write_bytes(pem("CERTIFICATE", certificate))
    return str(path)


@pytest.mark.parametrize("build", BUILDERS)
def test_a_proxy_is_loaded_as_the_client_chain(build, proxy, ca_file):
    """``load_cert_chain`` is what makes the connection mutually authenticated."""
    context = build(Config(proxy=proxy, ca_file=ca_file))
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname
    assert any(dict(cert["subject"][0]).get("commonName") == "Test CA"
               for cert in context.get_ca_certs())  # the configured CA is in the store


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



def test_with_nothing_named_the_default_proxy_is_presented(monkeypatch, tmp_path):
    """``/tmp/x509up_u<uid>``, which gfal2 and XrdCl present unasked.

    Without it ``https://`` to a grid storage element went out anonymously,
    and EOS, dCache and StoRM answer that with ``403`` rather than a ``401``
    that would have prompted a retry with the proxy.
    """
    path = tmp_path / "x509up_u1000"
    path.write_bytes(proxy_chain(throwaway_key(0)))
    monkeypatch.setattr("xrdclient.transport.base.default_proxy_path", lambda: str(path))
    loaded = []
    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", lambda self, p: loaded.append(p))
    for build in BUILDERS:
        build(Config(proxy=None))
    assert loaded == [str(path), str(path)]


def test_a_default_proxy_that_will_not_load_is_passed_over(monkeypatch, tmp_path, caplog):
    """A guessed file must not break a request that needs no certificate."""
    path = tmp_path / "x509up_u1000"
    path.write_text("not a proxy")
    monkeypatch.setattr("xrdclient.transport.base.default_proxy_path", lambda: str(path))
    with caplog.at_level("WARNING"):
        context = tls_context(Config(proxy=None))
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert f"not presenting the X.509 proxy {path}" in caplog.text


def test_a_named_proxy_is_never_swapped_for_the_default(monkeypatch, tmp_path):
    """``$X509_USER_PROXY`` pointing nowhere is an error, not a cue to guess."""
    default = tmp_path / "x509up_u1000"
    default.write_bytes(proxy_chain(throwaway_key(0)))
    monkeypatch.setattr("xrdclient.transport.base.default_proxy_path", lambda: str(default))
    with pytest.raises(OSError, match=r"absent\.pem"):
        tls_context(Config(proxy=str(tmp_path / "absent.pem")))


@pytest.mark.parametrize("build", BUILDERS)
def test_an_unusable_proxy_reads_as_one_sentence(build, tmp_path):
    """Not ``('cannot use the X.509 proxy ...',)``, which a one-argument SSLError prints."""
    path = tmp_path / "corrupt.pem"
    path.write_bytes(proxy_chain(throwaway_key(0))[:600])
    with pytest.raises(ssl.SSLError) as caught:
        build(Config(proxy=str(path)))
    assert str(caught.value).startswith(f"cannot use the X.509 proxy {path}: ")
