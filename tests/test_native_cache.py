"""Native cache handling without ambient credentials or a KDC."""

from __future__ import annotations

import pickle
import struct
import sys
import time
from types import SimpleNamespace

import pytest

from xrdclient.auth import krb5 as auth
from xrdclient.auth.kerberos.caches import open_ccache
from xrdclient.auth.kerberos.ccache import marshal_principal, marshal_ticket
from xrdclient.auth.kerberos.model import Principal, Ticket
from xrdclient.auth.kerberos.native import NativeCache, NativeCacheError
from xrdclient.errors import CredentialError


class NativeError(Exception):
    err_code = -1765328189


def _principal(components=(b"alice",), realm=b"EXAMPLE"):
    return SimpleNamespace(components=components, realm=realm, type=1)


def _credential(realm=b"EXAMPLE"):
    return SimpleNamespace(
        client=_principal(),
        server=_principal((b"xrootd", b"host"), realm),
        keyblock=SimpleNamespace(enctype=18, data=b"private-test-key"),
        times=SimpleNamespace(authtime=1, starttime=2, endtime=3, renew_till=4),
        ticket_flags=2,
        ticket=b"opaque-ticket",
    )


@pytest.fixture
def binding(monkeypatch):
    calls = []
    credentials = [_credential(), _credential(b"X-CACHECONF:")]
    module = SimpleNamespace(
        init_context=lambda: object(),
        cc_default_name=lambda context: b"API:default",
        cc_resolve=lambda context, name: calls.append(name) or credentials,
        cc_get_principal=lambda context, cache: _principal(),
        us_timeofday=lambda context: (100, 500_000),
        Krb5Error=NativeError,
    )
    monkeypatch.setitem(sys.modules, "krb5", module)
    monkeypatch.setattr(time, "time", lambda: 90.0)
    return module, calls


@pytest.mark.parametrize("name", [None, "FILE:/tmp/test-cache", "API:user", "KCM:user"])
def test_native_read_keeps_principals_flags_keys_and_offset(binding, name):
    _module, calls = binding
    cache = NativeCache(name)
    principal, tickets = cache.read()
    assert principal == Principal(("alice",), "EXAMPLE", 1)
    assert len(tickets) == 1
    ticket = tickets[0]
    assert ticket.flags == 0x40000000 and ticket.forwardable
    assert ticket.kdc_offset == 10.5
    assert ticket.key == b"private-test-key"
    assert "private-test-key" not in repr(ticket)
    assert calls == [(name or "API:default").encode()]
    assert not cache.store(ticket)


def test_native_failure_keeps_code_and_explains_fix(binding):
    module, _calls = binding

    def fail():
        raise NativeError("test cache unavailable")

    module.init_context = fail
    with pytest.raises(NativeCacheError, match=r"Check KRB5CCNAME.*kinit") as error:
        NativeCache("API:user").read()
    assert error.value.native_code == NativeError.err_code
    restored = pickle.loads(pickle.dumps(error.value))
    assert restored.native_code == error.value.native_code
    assert str(restored) == str(error.value)


@pytest.mark.parametrize("module", ["krb5", "gssapi"])
def test_missing_bindings_have_optional_install_hints(monkeypatch, module):
    monkeypatch.setitem(sys.modules, module, None)
    with pytest.raises(CredentialError, match=r"pip install 'xrdclient\[krb5\]'"):
        if module == "krb5":
            NativeCache("API:user").read()
        else:
            auth._native_ap_req("xrootd/host@EXAMPLE", "API:user")


def test_backend_selection_is_explicit(monkeypatch):
    monkeypatch.setenv("XRD_KRB5_BACKEND", "native")
    monkeypatch.delenv("KRB5CCNAME", raising=False)
    assert isinstance(open_ccache(), NativeCache)
    assert open_ccache("FILE:/tmp/test").name == "FILE:/tmp/test"
    monkeypatch.setenv("KRB5CCNAME", "API:user")
    assert open_ccache().name == "API:user"
    monkeypatch.setenv("XRD_KRB5_BACKEND", "typo")
    with pytest.raises(CredentialError, match=r"native.*portable"):
        open_ccache()


def test_real_native_cache_agrees_with_portable_layout(tmp_path):
    pytest.importorskip("krb5")
    ticket = Ticket(
        Principal(("alice",), "EXAMPLE", 1),
        Principal(("xrootd", "host"), "EXAMPLE", 2),
        18,
        100,
        101,
        200,
        300,
        0x40000000,
        b"opaque-ticket",
        bytes(range(32)),
    )
    path = tmp_path / "cache"
    path.write_bytes(
        struct.pack(">HH", 0x0504, 0) + marshal_principal(ticket.client) + marshal_ticket(ticket)
    )
    principal, tickets = NativeCache("FILE:" + str(path)).read()
    assert principal.same_name(ticket.client)
    assert tickets[0] == ticket
    assert tickets[0].key == ticket.key
