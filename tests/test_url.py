from __future__ import annotations

import pytest

from xrdclient.url import DEFAULT_PORT, XRootDURL, parse


@pytest.mark.parametrize(
    "text, host, port, path",
    [
        ("root://eos.example.org//store/d.root", "eos.example.org", 1094, "/store/d.root"),
        ("root://eos.example.org:1095//a", "eos.example.org", 1095, "/a"),
        ("roots://h//a/b", "h", 1094, "/a/b"),
        ("https://dav.example.org/store/x", "dav.example.org", 443, "/store/x"),
        ("http://dav.example.org/x", "dav.example.org", 80, "/x"),
        ("root://[2001:db8::1]:1234//p", "2001:db8::1", 1234, "/p"),
        ("root://host/relative", "host", DEFAULT_PORT, "/relative"),
    ],
)
def test_parse_components(text, host, port, path):
    u = parse(text)
    assert (u.host, u.port, u.path) == (host, port, path)


@pytest.mark.parametrize(
    "text",
    [
        "root://eos.example.org//store/d.root",
        "roots://h:1095//a/b?authz=tok",
        "https://dav.example.org/store/x",
        "xroot://u@h//p",
        "root://[::1]:1094//p/q",
    ],
)
def test_str_round_trips(text):
    u = parse(text)
    assert parse(str(u)) == u


def test_root_scheme_keeps_the_double_slash():
    assert str(parse("root://h//store/x")) == "root://h:1094//store/x"


def test_http_scheme_keeps_a_single_slash():
    assert str(parse("https://h/store/x")) == "https://h:443/store/x"


def test_userinfo_is_percent_decoded_and_re_encoded():
    u = parse("root://a%40b@h//p")
    assert u.username == "a@b"
    assert parse(str(u)).username == "a@b"


def test_bare_path_becomes_a_file_url(tmp_path):
    u = parse(str(tmp_path / "x"))
    assert u.is_local and u.scheme == "file"
    assert str(u) == f"file://{tmp_path / 'x'}"


def test_parse_is_idempotent_on_urls():
    u = parse("root://h//p")
    assert parse(u) is u


def test_bad_port_is_rejected():
    with pytest.raises(ValueError, match="invalid port"):
        parse("root://h:notaport//p")


def test_path_is_normalised():
    assert parse("root://h///a//b/../c").path == "/a/c"
    assert parse("root://h//a/b/").path == "/a/b/"


def test_query_round_trips_and_merges():
    u = parse("root://h//p?a=1")
    assert u.query == {"a": "1"}
    assert u.with_query(b="2").query == {"a": "1", "b": "2"}
    assert u.without_query().query == {}


def test_path_with_cgi():
    u = parse("root://h//p?authz=tok")
    assert u.path_with_cgi == "/p?authz=tok"
    assert parse("root://h//p").path_with_cgi == "/p"


def test_repr_redacts_credentials():
    text = repr(parse("root://user:hunter2@h//p?authz=BEARERTOKEN"))
    assert "hunter2" not in text
    assert "BEARERTOKEN" not in text
    assert "redacted" in text


def test_navigation_helpers():
    u = parse("root://h//a/b/c.root")
    assert u.name == "c.root"
    assert u.parent.path == "/a/b"
    assert u.join("d").path == "/a/b/c.root/d"
    assert u.with_path("/z").path == "/z"
    assert u.evolve(port=1095).port == 1095


def test_endpoint_identity_ignores_the_path():
    assert parse("root://h//a").endpoint == parse("root://h//b").endpoint


@pytest.mark.parametrize(
    "text, tls", [("roots://h//p", True), ("root://h//p", False), ("davs://h/p", True)]
)
def test_tls_schemes(text, tls):
    assert parse(text).use_tls is tls


def test_fspath_only_for_local_urls(tmp_path):
    assert parse(str(tmp_path)).__fspath__() == str(tmp_path)
    with pytest.raises(TypeError):
        parse("root://h//p").__fspath__()


def test_http_url_maps_dav_schemes():
    assert parse("davs://h:443/p").http_url == "https://h:443/p"
    assert parse("dav://h:80/p").http_url == "http://h:80/p"


def test_urls_are_hashable_and_frozen():
    u = XRootDURL(host="h", path="/p")
    assert {u, XRootDURL(host="h", path="/p")} == {u}
    with pytest.raises(AttributeError):
        u.host = "other"  # type: ignore[misc]


def test_host_and_scheme_are_lowercased():
    u = parse("ROOT://HOST//P")
    assert (u.scheme, u.host, u.path) == ("root", "host", "/P")


def test_http_url_renders_the_query_it_was_given():
    assert parse("https://h:443/p?token=abc&x=1").http_url == "https://h:443/p?token=abc&x=1"


def test_a_url_with_no_path_at_all_still_has_a_root():
    assert parse("root://host:1094").path == "/"
    assert parse("file://").path == "/"


def test_a_file_url_keeps_the_path_it_was_handed():
    assert parse("file:///store/data/a.root").path == "/store/data/a.root"
    assert parse("file:///store/data/a.root").scheme == "file"


def test_a_path_can_be_emptied_back_to_the_root():
    assert parse("root://h:1094//store/f.root").with_path("").path == "/"
    assert parse("root://h:1094//store/").with_path("/store/sub/../f").path == "/store/f"
