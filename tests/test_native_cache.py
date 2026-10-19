"""Choosing the Kerberos implementation: ``$XRD_KRB5_BACKEND``, xrdclient's own setting."""

from __future__ import annotations

import pytest
from xrdsec.krb5 import caches

from xrdclient.auth.kerberos.caches import BACKENDS, FileCache, backend, open_ccache
from xrdclient.errors import CredentialError


@pytest.mark.parametrize(("value", "choice"), [("", "auto"), ("  GSSAPI ", "gssapi")])
def test_the_setting_is_read_forgivingly_and_defaults_to_auto(monkeypatch, value, choice):
    monkeypatch.setenv("XRD_KRB5_BACKEND", value)
    assert backend() == choice and choice in BACKENDS


def test_opening_a_cache_is_xrdsecs_whatever_the_setting(monkeypatch):
    monkeypatch.setenv("XRD_KRB5_BACKEND", "typo")
    assert open_ccache is caches.open_ccache
    cache = open_ccache("FILE:/tmp/test")
    assert isinstance(cache, FileCache) and cache.name == "/tmp/test"


def test_the_removed_native_backend_names_its_replacement(monkeypatch):
    monkeypatch.setenv("XRD_KRB5_BACKEND", "native")
    with pytest.raises(CredentialError, match=r"removed in 0\.4\.0.*'gssapi'"):
        backend()


def test_an_unknown_backend_is_refused(monkeypatch):
    monkeypatch.setenv("XRD_KRB5_BACKEND", "typo")
    with pytest.raises(CredentialError, match=r"'auto', 'gssapi' or 'portable'"):
        backend()
