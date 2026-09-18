"""``xrdclient.diagnose`` - the questions a first transfer asks, asked on purpose.

Every check has to be able to fail without the diagnosis failing with it, so
most of what is tested here is what happens when something is broken: an
unresolvable host, a port with nothing behind it, a mechanism that raises
while being asked what it wants.
"""

from __future__ import annotations

import socket
from dataclasses import replace

import pytest

from xrdclient import diagnose
from xrdclient.auth import Offer, registry
from xrdclient.config import Config
from xrdclient.doctor import Check, Report, _nearest
from xrdclient.errors import ConnectionError as XrdConnectionError
from xrdclient.testing import FakeDAVServer

BODY = b"hello world"


@pytest.fixture
def dav():
    with FakeDAVServer(files={"/d/a.root": BODY}) as running:
        yield running


def named(report, name):
    """The one check that went by this name."""
    return next(check for check in report if check.name == name)


def free_port() -> int:
    """A port nothing is listening on, which is what a closed one looks like."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# The machine alone
# ---------------------------------------------------------------------------


def test_with_no_url_it_checks_the_machine_and_stops_there():
    report = diagnose()
    assert [check.name for check in report[:3]] == ["python", "extras", "settings"]
    assert report.url == ""
    assert not any(check.name in ("url", "dns", "connect") for check in report)
    assert len(report) == len(report.to_dict()) == len(list(report))


def test_the_report_prints_one_line_a_check_and_two_when_there_is_a_hint():
    report = Report((Check("a", "ok", "fine"), Check("b", "bad", "broken", "do this")))
    assert str(report).splitlines() == [
        "ok  a             fine",
        "!!  b             broken",
        "                  -> do this",
    ]
    assert report.ok is False
    assert [check.name for check in report.problems] == ["b"]
    assert report.to_dict()[1] == {
        "name": "b",
        "state": "bad",
        "detail": "broken",
        "hint": "do this",
    }


def test_turning_off_certificate_checking_is_said_out_loud():
    report = diagnose(config=replace(Config(), verify_tls=False))
    check = named(report, "settings")
    assert (check.state, "NOT verified" in check.detail) == ("warn", True)
    assert "--no-verify-tls" in check.hint
    assert named(diagnose(config=Config()), "settings").state == "ok"


def test_a_mechanism_with_no_material_is_a_warning_and_says_what_would_fix_it(tmp_path):
    config = replace(
        Config(),
        auth_order=("gsi", "ztn"),
        proxy=str(tmp_path / "nothing"),
        token=None,
        token_file=str(tmp_path / "no-token"),
    )
    report = diagnose(config=config)
    gsi = named(report, "auth:gsi")
    assert gsi.state == "warn"
    assert "proxy-init" in gsi.hint
    assert named(report, "auth").state == "bad"
    assert not report.ok


def test_a_mechanism_that_only_says_who_you_are_is_not_taken_for_proof():
    report = diagnose(config=replace(Config(), auth_order=("unix", "host")))
    check = named(report, "auth")
    assert check.state == "warn"
    assert "without proving it" in check.detail
    assert report.ok  # a warning is not a failure: an open server will still read


def test_a_mechanism_that_cannot_say_what_it_wants_does_not_stop_the_rest(monkeypatch):
    cls = registry()["ztn"]

    def explode(*args, **kwargs):
        raise RuntimeError("the keyring is on fire")

    monkeypatch.setattr(cls, "missing", classmethod(explode))
    monkeypatch.setattr(cls, "available", classmethod(lambda *a, **k: None))
    report = diagnose(config=replace(Config(), auth_order=("ztn", "unix")))
    assert "on fire" in named(report, "auth:ztn").detail
    assert named(report, "auth:unix").state == "ok"


def test_a_mechanism_that_raises_while_being_built_is_a_warning_not_a_crash(monkeypatch):
    cls = registry()["ztn"]

    def explode(*args, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(cls, "available", classmethod(explode))
    check = named(diagnose(config=replace(Config(), auth_order=("ztn",))), "auth:ztn")
    assert (check.state, check.detail) == ("warn", "RuntimeError: no")


def test_a_mechanism_with_nothing_to_ask_for_says_only_that(monkeypatch):
    cls = registry()["krb5"]
    monkeypatch.setattr(cls, "available", classmethod(lambda *a, **k: None))
    monkeypatch.setattr(cls, "missing", classmethod(lambda *a, **k: None))
    check = named(diagnose(config=replace(Config(), auth_order=("krb5",))), "auth:krb5")
    assert (check.state, check.detail) == ("warn", "no material here")


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def test_a_url_that_is_not_one_is_the_first_and_last_thing_said():
    report = diagnose("this is not a url")
    assert named(report, "url").state == "bad"
    assert "root://HOST//path" in named(report, "url").hint
    assert not any(check.name == "dns" for check in report)


def test_a_url_this_library_cannot_parse_at_all_is_named_the_same_way():
    report = diagnose("root://host:not-a-port//store/f.root")
    assert named(report, "url").state == "bad"
    assert not report.ok


def test_a_host_that_does_not_resolve_stops_before_the_port():
    report = diagnose("root://no-such-host.invalid//store/f.root")
    assert named(report, "dns").state == "bad"
    assert "resolver" in named(report, "dns").hint
    assert not any(check.name == "connect" for check in report)


def test_a_port_with_nothing_behind_it_is_named_as_the_firewall_it_usually_is():
    report = diagnose(f"root://127.0.0.1:{free_port()}//store/f.root")
    check = named(report, "connect")
    assert check.state == "bad"
    assert "firewall" in check.hint
    assert not any(check.name == "server" for check in report)


def test_a_bucket_is_not_chased_because_the_url_does_not_say_where_it_is():
    report = diagnose("s3://my-bucket/store/f.root")
    assert named(report, "url").detail == "s3 bucket my-bucket, key /store/f.root"
    assert named(report, "endpoint").state == "skip"
    assert report.ok


def test_a_whole_healthy_path_is_ok_from_end_to_end(server):
    report = diagnose(str(server.url) + "data/a.root")
    assert [c.state for c in report if c.name in ("url", "dns", "connect", "server", "path")] == [
        "ok"
    ] * 5
    assert named(report, "path").detail.endswith("11 bytes")
    assert named(report, "server").detail.startswith("protocol ")
    assert report.ok


def test_a_directory_says_it_is_one(server):
    assert named(diagnose(str(server.url) + "data"), "path").detail.endswith("a directory")


def test_a_url_with_no_path_has_nothing_to_look_for(server):
    check = named(diagnose(str(server.url)), "path")
    assert (check.state, check.detail) == ("skip", "no path in the URL to look for")


def test_a_missing_path_is_told_how_far_down_the_tree_does_exist(server):
    report = diagnose(str(server.url) + "data/nope/deeper.root")
    check = named(report, "path")
    assert check.state == "bad"
    assert check.hint == "/data exists; check the spelling below it"
    assert not report.ok


def test_a_path_whose_top_is_wrong_says_so_rather_than_guessing(server):
    report = diagnose(str(server.url) + "wrong/a/b.root")
    assert "not even the top of the path is there" in named(report, "path").hint


def test_a_single_component_path_that_is_absent_has_no_parent_to_offer(server):
    assert "not even the top" in _nearest(_run_over(server, "/nope"), _fs(server))


def test_a_webdav_endpoint_is_diagnosed_the_same_way_without_a_protocol_reply(dav):
    report = diagnose(str(dav.url).rstrip("/") + "/d/a.root")
    assert named(report, "server").detail == "answered"
    assert named(report, "path").detail.endswith("11 bytes")
    assert report.ok


def test_a_server_that_will_not_answer_is_not_confused_with_one_that_is_absent(
    server, monkeypatch
):
    from xrdclient.client import FileSystem

    monkeypatch.setattr(
        FileSystem, "ping", lambda self: (_ for _ in ()).throw(XrdConnectionError("gone away"))
    )
    report = diagnose(str(server.url) + "data/a.root")
    check = named(report, "server")
    assert check.state == "bad"
    assert "would not answer" in check.detail
    assert not any(c.name == "path" for c in report)


def test_a_login_that_fails_outright_is_a_line_rather_than_an_exception(server, monkeypatch):
    from xrdclient.client import FileSystem

    def refuse(self, *args, **kwargs):
        raise XrdConnectionError("the door is shut")

    monkeypatch.setattr(FileSystem, "__init__", refuse)
    report = diagnose(str(server.url) + "data/a.root")
    assert named(report, "server").detail.endswith("the door is shut")
    assert not report.ok


# -- helpers for the two checks that need the pieces rather than the whole ---


def _fs(server):
    from xrdclient.client import FileSystem

    return FileSystem(str(server.url))


def _run_over(server, path):
    from xrdclient.doctor import _Run

    run = _Run(Config())
    run.path = path
    return run


def test_every_mechanism_the_config_names_gets_a_line_of_its_own():
    order = tuple(registry())
    report = diagnose(config=replace(Config(), auth_order=order))
    assert {f"auth:{name}" for name in order} <= {check.name for check in report}
    assert all(Offer(name) for name in order)


def test_the_optional_packages_are_listed_however_many_there_are(monkeypatch):
    from xrdclient import doctor

    monkeypatch.setattr(doctor, "EXTRAS", {"json": "json", "sys": "sys"})
    assert named(diagnose(), "extras").detail == "json; sys"
    monkeypatch.setattr(doctor, "EXTRAS", {"no_such_module_at_all": "unobtainium"})
    assert named(diagnose(), "extras").detail == "none (absent: unobtainium)"


def test_a_manager_offering_tls_is_described_as_what_it_is(server, monkeypatch):
    from xrdclient.client import FileSystem
    from xrdclient.types import ProtocolInfo

    info = ProtocolInfo(version=0x563, flags=0x80000002)
    monkeypatch.setattr(FileSystem, "protocol", lambda self: info)
    check = named(diagnose(str(server.url) + "data/a.root"), "server")
    assert check.detail == "protocol 5.6.3, a manager, TLS offered"
