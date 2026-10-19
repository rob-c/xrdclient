"""``xrd-proxy``: init, info and destroy, in voms-proxy-* spellings and as reports."""

from __future__ import annotations

import datetime
import io
import json
import os
import stat
from typing import Any

import pytest
from xrdsec.testing.proxygrid import (
    FakeVOMS,
    Grid,
    ac_answer,
    build_grid,
    certificate,
    error_answer,
    key,
    key_pem,
    pem,
)

from xrdclient import credentials
from xrdclient.cli import proxy as cli
from xrdclient.crypto.proxies import create_proxy, describe_proxy, load_credential, write_proxy


@pytest.fixture
def grid(tmp_path, monkeypatch) -> Grid:
    made = build_grid(tmp_path / "grid")
    for name in ("X509_USER_PROXY", "X509_USER_CERT", "X509_USER_KEY", "X509_VOMSES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("X509_VOMS_DIR", str(made.voms_dir))
    monkeypatch.setenv("X509_CERT_DIR", str(made.ca_dir))
    return made


@pytest.fixture
def out(tmp_path) -> str:
    return str(tmp_path / "x509up")


def _init(grid, out, *extra) -> int:
    return cli.main(
        ["init", "-cert", str(grid.usercert), "-key", str(grid.userkey), "-out", out, *extra]
    )


def _usage(*argv: str) -> None:
    """A usage error: argparse exits ``2`` (in text mode the exit propagates, as for xrd-fs)."""
    with pytest.raises(SystemExit) as raised:
        cli.main(list(argv))
    assert raised.value.code == 2


def _report(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(capsys.readouterr().out)
    return report


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_writes_a_private_proxy_and_says_until_when(grid, out, capsys):
    assert _init(grid, out) == 0
    text = capsys.readouterr().out
    assert f"Created proxy in {out}." in text and "Your proxy is valid until" in text
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    info = describe_proxy(load_credential(out))
    assert info.kind == "RFC3820 compliant impersonation proxy"
    assert 11.9 * 3600 < info.time_left <= 12 * 3600


def test_init_options_shape_the_proxy(grid, out, capsys):
    assert _init(grid, out, "-valid", "1:30", "-bits", "1024", "-limited", "-path_length", "2") == 0
    info = describe_proxy(load_credential(out))
    assert info.limited and info.path_length == 2 and info.strength == 1024
    assert 1.4 * 3600 < info.time_left <= 1.5 * 3600
    assert _init(grid, out, "--hours", "2", "-old", "-q") == 0
    assert describe_proxy(load_credential(out)).kind == "full legacy globus proxy"
    _usage("init", "-out", out, "--profile", "arc", "-rfc")  # one style at a time
    assert _init(grid, out, "--profile", "arc") == 0
    assert capsys.readouterr().out.count("Created proxy") == 2  # -q said nothing


def test_init_reports_as_json(grid, out, capsys):
    assert _init(grid, out, "--json") == 0
    document = _report(capsys)
    assert document["command"] == "init" and document["summary"]["ok"]
    result = next(r for r in document["records"] if r["kind"] == "result")
    assert result["path"] == out and result["subject"].startswith("/DC=org/DC=example")


def test_init_finds_the_credential_in_the_usual_places(grid, out, monkeypatch, tmp_path):
    monkeypatch.setenv("X509_USER_CERT", str(grid.usercert))
    monkeypatch.setenv("X509_USER_KEY", str(grid.userkey))
    assert cli.main(["init", "-out", out, "-q"]) == 0
    monkeypatch.delenv("X509_USER_CERT")
    monkeypatch.delenv("X509_USER_KEY")
    home = tmp_path / "home"
    (home / ".globus").mkdir(parents=True)
    (home / ".globus" / "usercert.pem").write_bytes(grid.usercert.read_bytes())
    Grid.write_key(home / ".globus" / "userkey.pem", key(2))
    monkeypatch.setenv("HOME", str(home))
    assert cli.main(["init", "-out", out, "-q"]) == 0


def test_init_writes_the_default_proxy_without_following_links(grid, monkeypatch, tmp_path):
    default = tmp_path / "x509up_u1"
    monkeypatch.setattr(credentials, "default_proxy_path", lambda *a, **k: str(default))
    args = ["init", "-cert", str(grid.usercert), "-key", str(grid.userkey), "-q"]
    assert cli.main(args) == 0 and default.is_file()
    default.unlink()
    elsewhere = tmp_path / "elsewhere"
    default.symlink_to(elsewhere)
    assert cli.main(args) == 1  # a link at the predictable place is refused
    monkeypatch.setenv("X509_USER_PROXY", str(default))  # ... unless the user named it
    assert cli.main(args) == 0 and elsewhere.is_file()


def test_an_encrypted_key_asks_for_its_pass_phrase(grid, out, monkeypatch, tmp_path):
    locked = Grid.write_key(tmp_path / "locked.pem", key(2), b"s3cret")
    args = ["init", "-cert", str(grid.usercert), "-key", str(locked), "-out", out, "-q"]
    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret\n"))
    assert cli.main([*args, "-pwstdin"]) == 0
    from xrdclient import _usercred

    asked = []

    def pass_phrase(config: object, path: str) -> bytes:
        asked.append(path)
        return b"s3cret"

    monkeypatch.setattr(_usercred, "pass_phrase", pass_phrase)
    assert cli.main(args) == 0 and asked == [str(locked)]
    monkeypatch.setattr(_usercred, "pass_phrase", lambda config, path: b"wrong")
    assert cli.main(args) == 1


def test_init_warns_when_the_credential_cuts_the_proxy_short(grid, out, capsys, tmp_path):
    soon = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
    user = certificate(
        grid.user.subject, grid.ca.subject, key(2).public_key(), key(1), serial=9, not_after=soon
    )
    cert = tmp_path / "short.pem"
    cert.write_bytes(pem(user))
    assert cli.main(["init", "-cert", str(cert), "-key", str(grid.userkey), "-out", out]) == 0
    assert "shorter than asked for" in capsys.readouterr().out


def test_init_with_voms_contacts_the_server_and_embeds_the_ac(grid, out, capsys, tmp_path):
    answer = ac_answer(grid.make_ac(), "shortened to 1 hour")
    with FakeVOMS(grid, lambda query: answer) as fake:
        vomses = tmp_path / "vomses"
        vomses.write_text(f'"atlas" "127.0.0.1" "{fake.port}" "/CN=voms" "atlas"\n')
        assert (
            _init(
                grid,
                out,
                "-voms",
                "atlas:/atlas/Role=pilot",
                "-voms",
                "atlas:/atlas/de",
                "-vomses",
                str(vomses),
                "-vomslife",
                "1:00",
                "-certdir",
                str(grid.ca_dir),
            )
            == 0
        )
        assert fake.requests[0][1] == {
            "fqans": ["/atlas/Role=pilot,/atlas/de"],
            "lifetime": ["3600"],
        }
    captured = capsys.readouterr()
    assert f'Contacting 127.0.0.1:{fake.port} [/CN=voms] "atlas"...' in captured.out
    assert "Remote VOMS server contacted successfully." in captured.out
    assert "Warning: 127.0.0.1" in captured.err and "shortened to 1 hour" in captured.err
    assert describe_proxy(load_credential(out)).voms[0].vo == "atlas"


def test_init_with_voms_quietly_and_as_a_report(grid, out, capsys, tmp_path):
    with FakeVOMS(grid, lambda query: ac_answer(grid.make_ac(), "note")) as fake:
        vomses = tmp_path / "vomses"
        vomses.write_text(f'"atlas" "127.0.0.1" "{fake.port}" "/CN=voms" "atlas"\n')
        assert _init(grid, out, "-voms", "atlas", "-vomses", str(vomses), "-q", "--json") == 0
        assert fake.requests[0][1]["lifetime"] == ["43200"]  # the proxy's own lifetime
    document = _report(capsys)
    kinds = [record["kind"] for record in document["records"]]
    assert "warning" in kinds and "result" in kinds


def test_init_reports_a_refusal_and_bad_requests(grid, out, capsys, tmp_path):
    with FakeVOMS(grid, lambda query: error_answer(403, "NoSuchUser", "who?")) as fake:
        vomses = tmp_path / "vomses"
        vomses.write_text(f'"atlas" "127.0.0.1" "{fake.port}" "/CN=voms" "atlas"\n')
        assert _init(grid, out, "-voms", "atlas", "-vomses", str(vomses)) == 1
        assert "does not know your certificate" in capsys.readouterr().err
        assert _init(grid, out, "-voms", ":/atlas", "-vomses", str(vomses)) == 1
        assert "a VOMS request needs a VO name" in capsys.readouterr().err
        assert _init(grid, out, "-voms", "lhcb", "-vomses", str(vomses)) == 1
    assert not os.path.exists(out)


@pytest.mark.parametrize("value", ["1:60", "x", "-1:00", "0:00", "1:"])
def test_a_bad_duration_is_a_usage_error(grid, out, value):
    _usage("init", "-out", out, "-valid", value)


def test_durations_read_as_voms_proxy_init_reads_them():
    assert cli._duration("24:00") == 86400.0
    assert cli._duration("0:30") == 1800.0
    assert cli._duration("2") == 7200.0


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


@pytest.fixture
def voms_proxy(grid, out) -> str:
    return write_proxy(out, create_proxy(grid.credential(), voms_acs=[grid.make_ac()]))


def test_info_prints_the_voms_proxy_info_block(voms_proxy, grid, capsys):
    assert cli.main(["info", "-file", voms_proxy]) == 0
    text = capsys.readouterr().out
    assert "type      : RFC3820 compliant impersonation proxy" in text
    assert f"identity  : {grid.credential().identity}" in text
    assert "key usage : Digital Signature, Key Encipherment" in text
    assert "=== VO" not in text
    assert cli.main(["info", "-file", voms_proxy, "-all"]) == 0
    text = capsys.readouterr().out
    assert "=== VO atlas extension information ===" in text
    assert "attribute : /atlas/Role=NULL/Capability=NULL" in text


def test_info_field_selectors_print_bare_values(voms_proxy, capsys):
    assert cli.main(["info", "-file", voms_proxy, "-timeleft", "-vo", "-fqan", "-strength"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert 0 < int(lines[0]) <= 43200 and lines[1:] == [
        "atlas",
        "/atlas/Role=NULL/Capability=NULL",
        "2048",
    ]
    every = ["-subject", "-issuer", "-identity", "-type", "-path", "-actimeleft"]
    assert cli.main(["info", "-file", voms_proxy, *every]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[4] == voms_proxy and int(lines[5]) > 0


def test_info_exists_and_acexists_answer_by_exit_code(voms_proxy, capsys):
    assert cli.main(["info", "-file", voms_proxy, "-exists"]) == 0
    assert cli.main(["info", "-file", voms_proxy, "-exists", "-valid", "11:00"]) == 0
    assert cli.main(["info", "-file", voms_proxy, "-exists", "-hours", "13"]) == 1
    assert cli.main(["info", "-file", voms_proxy, "-acexists", "atlas"]) == 0
    assert cli.main(["info", "-file", voms_proxy, "-acexists", "cms"]) == 1
    assert cli.main(["info", "-file", voms_proxy + ".missing", "-exists"]) == 1
    assert capsys.readouterr().out == ""


def test_info_without_a_proxy_says_so(grid, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(credentials, "default_proxy_path", lambda *a, **k: str(tmp_path / "none"))
    assert cli.main(["info"]) == 1
    assert "there is no proxy at" in capsys.readouterr().err
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    os.chmod(empty, 0o600)
    assert cli.main(["info", "-file", str(empty)]) == 1
    leaky = write_proxy(tmp_path / "leaky", create_proxy(grid.credential()))
    os.chmod(leaky, 0o644)
    assert cli.main(["info", "-file", leaky]) == 1
    assert "chmod 600" in capsys.readouterr().err


def test_info_reports_as_json(voms_proxy, capsys):
    assert cli.main(["info", "-file", voms_proxy, "--json", "-q"]) == 0
    result = next(r for r in _report(capsys)["records"] if r["kind"] == "result")
    assert result["type"] == "RFC3820 compliant impersonation proxy"
    assert result["voms"][0]["vo"] == "atlas"
    assert cli.main(["info", "-file", voms_proxy, "--xml", "-exists"]) == 0
    document = capsys.readouterr().out
    assert 'name="exists" type="boolean">true<' in document


@pytest.mark.parametrize("flags", [["--json"], ["--output-format", "json"]])
def test_report_flags_also_go_before_the_subcommand(
    voms_proxy: str, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    """As for ``xrd-fs`` and ``xrd-cp``: ``xrd-proxy --output-format json info``."""
    assert cli.main([*flags, "info", "-file", voms_proxy, "-q"]) == 0
    report = _report(capsys)
    assert report["schema"] == "storage-client-report" and report["command"] == "info"
    assert cli.main(["--output-format", "xml", "--help"]) == 0
    assert 'schema="storage-client-report"' in capsys.readouterr().out
    _usage("--output-format", "yaml", "info")


# ---------------------------------------------------------------------------
# destroy
# ---------------------------------------------------------------------------


def test_destroy_removes_the_proxy(voms_proxy, capsys):
    assert cli.main(["destroy", "-file", voms_proxy, "-dry"]) == 0
    assert os.path.exists(voms_proxy) and "Would remove" in capsys.readouterr().out
    assert cli.main(["destroy", "-file", voms_proxy]) == 0
    assert not os.path.exists(voms_proxy) and "Removed" in capsys.readouterr().out
    assert cli.main(["destroy", "-file", voms_proxy]) == 1
    assert "there is no proxy" in capsys.readouterr().err


def test_destroy_removes_a_link_not_its_target(grid, tmp_path, monkeypatch):
    target = tmp_path / "kept"
    target.write_bytes(key_pem(key(5)))
    link = tmp_path / "x509up_u1"
    link.symlink_to(target)
    monkeypatch.setattr(credentials, "default_proxy_path", lambda *a, **k: str(link))
    assert cli.main(["destroy", "-q"]) == 0
    assert target.exists() and not link.is_symlink()


def test_destroy_leaves_another_users_file(voms_proxy, monkeypatch, capsys):
    real = os.lstat

    def lstat(path, *args, **kwargs):
        info = real(path, *args, **kwargs)
        return os.stat_result((info.st_mode, 0, 0, 0, os.geteuid() + 1, 0, 0, 0, 0, 0))

    monkeypatch.setattr(os, "lstat", lstat)
    assert cli.main(["destroy", "-file", voms_proxy]) == 1
    assert "belongs to uid" in capsys.readouterr().err and os.path.exists(voms_proxy)


def test_the_tool_has_a_version_and_needs_a_command(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert "xrd-proxy" in capsys.readouterr().out
    _usage()
    assert cli.main(["--json"]) == 2  # under a report the exit becomes a code


def test_a_certificate_without_key_usage_has_no_such_line(voms_proxy):
    import dataclasses

    info = dataclasses.replace(describe_proxy(load_credential(voms_proxy)), key_usage=())
    assert not any(line.startswith("key usage") for line in cli._summary(info, False))
    assert cli._clock(-5) == "0:00:00" and cli._clock(90061) == "25:01:01"
