"""Credentials and redirects: what goes to a host the caller never named.

A ``Location`` is chosen by the server, so following one hands whoever it
names the next request - and, unless the client is careful, the token that
was meant for the first server. The rule is the one browsers, ``curl`` and
``requests`` follow: credentials go only to the origin they were meant for,
never over plain HTTP after HTTPS, and further than that only to domains the
caller has said to trust.
"""

from __future__ import annotations

import pytest

from xrdclient.config import Config
from xrdclient.http import HTTPClient
from xrdclient.http.client import carries_credentials
from xrdclient.testing import FakeDAVServer
from xrdclient.url import parse

TOKEN = "SECRET"


@pytest.fixture
def dav():
    with FakeDAVServer(files={"/d/a.root": b"here"}, dirs=["/d"]) as server:
        yield server


@pytest.fixture
def elsewhere():
    with FakeDAVServer(files={"/d/a.root": b"there"}, dirs=["/d"]) as server:
        yield server


def recorded(server):
    """Every request's headers, in order, as ``server`` received them."""
    seen: list[dict[str, str]] = []

    def record(method, path, headers):
        seen.append(headers)

    for verb in ("GET", "HEAD", "PUT", "COPY"):
        server.handlers[verb] = record
    return seen


def elsewhere_by_name(elsewhere, path="d/a.root"):
    """The other server, spelled as a different host rather than a port."""
    return f"http://localhost:{elsewhere.address[1]}/{path}"


def test_the_token_does_not_follow_a_redirect_to_another_host(dav, elsewhere):
    dav.redirects["/d/a.root"] = elsewhere_by_name(elsewhere)
    seen = recorded(elsewhere)
    with HTTPClient(Config(token=TOKEN)) as client:
        assert client.request("GET", dav.url / "d/a.root").body == b"there"
    assert [h.get("Authorization") for h in seen] == [None]


def test_the_token_does_not_follow_a_redirect_to_another_port(dav, elsewhere):
    dav.redirects["/d/a.root"] = str(elsewhere.url / "d/a.root")
    seen = recorded(elsewhere)
    with HTTPClient(Config(token=TOKEN)) as client:
        client.request("GET", dav.url / "d/a.root")
    assert "Authorization" not in seen[0]


def test_the_token_follows_a_redirect_on_the_same_server(dav):
    dav.add_file("/d/b.root", b"moved")
    dav.redirects["/d/a.root"] = "/d/b.root"
    dav.require_token = TOKEN
    with HTTPClient(Config(token=TOKEN)) as client:
        assert client.request("GET", dav.url / "d/a.root").body == b"moved"


def test_a_token_in_the_url_does_not_follow_either(dav, elsewhere):
    dav.redirects["/d/a.root"] = elsewhere_by_name(elsewhere)
    seen = recorded(elsewhere)
    with HTTPClient(Config()) as client:
        client.request("GET", (dav.url / "d/a.root").with_query(authz=TOKEN))
    assert "Authorization" not in seen[0]


def test_a_transfer_credential_does_not_follow_either(dav, elsewhere):
    """``TransferHeaderAuthorization`` is the far side's token in a ``COPY``."""
    dav.redirects["/d/a.root"] = elsewhere_by_name(elsewhere)
    seen = recorded(elsewhere)
    headers = {"TransferHeaderAuthorization": f"Bearer {TOKEN}", "Overwrite": "T"}
    with HTTPClient(Config()) as client:
        client.request("GET", dav.url / "d/a.root", headers=headers)
    assert "TransferHeaderAuthorization" not in seen[0]
    assert seen[0]["Overwrite"] == "T"


def test_a_trusted_domain_gets_the_token(dav, elsewhere):
    """How a site whose head node hands off to its data nodes is opted in."""
    dav.redirects["/d/a.root"] = elsewhere_by_name(elsewhere)
    elsewhere.require_token = TOKEN
    with HTTPClient(Config(token=TOKEN), trusted_redirect_domains=("localhost",)) as client:
        assert client.request("GET", dav.url / "d/a.root").body == b"there"


def test_the_token_is_not_restored_by_a_redirect_back(dav, elsewhere):
    """Once a hop leaves the origin, a server there cannot vouch for another."""
    dav.add_file("/d/b.root", b"back")
    dav.redirects["/d/a.root"] = elsewhere_by_name(elsewhere)
    elsewhere.redirects["/d/a.root"] = str(dav.url / "d/b.root")
    seen = recorded(dav)
    with HTTPClient(Config(token=TOKEN)) as client:
        assert client.request("GET", dav.url / "d/a.root").body == b"back"
    assert [h.get("Authorization") for h in seen] == [f"Bearer {TOKEN}", None]


def test_a_signed_location_still_carries_its_own_token(dav, elsewhere):
    """A token the redirecting server put in the ``Location`` is its to give."""
    elsewhere.require_token = "HANDOFF"
    dav.redirects["/d/a.root"] = elsewhere_by_name(elsewhere) + "?authz=HANDOFF"
    with HTTPClient(Config(token=TOKEN)) as client:
        assert client.request("GET", dav.url / "d/a.root").body == b"there"


# -- the rule itself --------------------------------------------------------


@pytest.mark.parametrize(
    "origin, target, trusted, expected",
    [
        ("https://a.example/x", "https://a.example/y", (), True),
        ("davs://a.example/x", "https://a.example:443/y", (), True),
        ("https://a.example/x", "https://b.example/y", (), False),
        ("https://a.example/x", "https://a.example:8443/y", (), False),
        ("https://a.example/x", "http://a.example/y", (), False),
        ("http://a.example/x", "https://a.example/y", (), False),
        ("https://head.site.org/x", "https://pool1.site.org/y", ("site.org",), True),
        ("https://head.site.org/x", "https://site.org/y", ("site.org",), True),
        ("https://head.site.org/x", "https://evilsite.org/y", ("site.org",), False),
        ("https://head.site.org/x", "http://pool1.site.org/y", ("site.org",), False),
        ("https://head.site.org/x", "https://pool1.site.org/y", (".site.org",), True),
        ("https://head.site.org/x", "https://anywhere.example/y", ("*",), True),
        ("https://head.site.org/x", "http://anywhere.example/y", ("*",), False),
        ("http://head.site.org/x", "http://pool.site.org/y", ("site.org",), True),
    ],
)
def test_who_carries_credentials(origin, target, trusted, expected):
    assert carries_credentials(parse(origin), parse(target), trusted) is expected


def test_the_trusted_domains_can_come_from_the_configuration(monkeypatch):
    """A setting in ``Config`` (or the environment) reaches every client."""
    assert HTTPClient(Config(trusted_redirect_domains=("example.org",)))._trusted() == (
        "example.org",
    )
    monkeypatch.setenv("XRD_TRUSTEDREDIRECTDOMAINS", "a.org, b.org")
    assert HTTPClient(Config())._trusted() == ("a.org", "b.org")
    explicit = HTTPClient(Config(), trusted_redirect_domains=("c.org",))
    assert explicit._trusted() == ("c.org",)
