"""``xrd-proxy``: init, info and destroy, in voms-proxy-* spellings and as reports."""

from __future__ import annotations

import datetime
import io
import json
import os
import stat
import sys
from typing import Any

import pytest
from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, pkcs12
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
    for name in (
        "X509_USER_PROXY",
        "X509_USER_CERT",
        "X509_USER_KEY",
        "X509_USER_P12",
        "X509_VOMSES",
    ):
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


def _bundle(grid, path, phrase: bytes = b"phrase"):
    """A PKCS#12 bundle of the grid's user certificate and key, ``0600``."""
    path.write_bytes(
        pkcs12.serialize_key_and_certificates(
            b"Jane Doe", key(2), grid.user, [grid.ca], BestAvailableEncryption(phrase)
        )
    )
    os.chmod(path, 0o600)
    return path


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


def test_init_reads_a_pkcs12_bundle(grid, out, monkeypatch, tmp_path):
    """One file and one pass phrase instead of a certificate and a key."""
    bundle = _bundle(grid, tmp_path / "usercred.p12")
    monkeypatch.setattr("sys.stdin", io.StringIO("phrase\n"))
    assert cli.main(["init", "-p12", str(bundle), "-out", out, "-pwstdin", "-q"]) == 0
    info = describe_proxy(load_credential(out))
    assert info.kind == "RFC3820 compliant impersonation proxy"
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    monkeypatch.setattr("sys.stdin", io.StringIO("phrase\n"))
    assert cli.main(["init", "-p12", str(bundle), "-out", out, "--pwstdin", "-q"]) == 0


def test_init_asks_for_the_bundles_pass_phrase_like_a_keys(grid, out, monkeypatch, tmp_path):
    """The prompter is the same one an encrypted PEM key uses, named the bundle."""
    bundle = _bundle(grid, tmp_path / "usercred.p12")
    from xrdclient import _usercred

    asked = []

    def pass_phrase(config: object, path: str) -> bytes:
        asked.append(path)
        return b"phrase"

    monkeypatch.setattr(_usercred, "pass_phrase", pass_phrase)
    assert cli.main(["init", "-p12", str(bundle), "-out", out, "-q"]) == 0
    assert asked == [str(bundle)]
    monkeypatch.setattr(_usercred, "pass_phrase", lambda config, path: b"wrong")
    assert cli.main(["init", "-p12", str(bundle), "-out", out, "-q"]) == 1


def test_init_finds_the_bundle_in_x509_user_p12(grid, out, monkeypatch, tmp_path):
    bundle = _bundle(grid, tmp_path / "usercred.p12")
    monkeypatch.setenv("X509_USER_P12", str(bundle))
    monkeypatch.setattr("sys.stdin", io.StringIO("phrase\n"))
    assert cli.main(["init", "-pwstdin", "-out", out, "-q"]) == 0
    assert describe_proxy(load_credential(out)).subject.startswith("/DC=org/DC=example")
    # A certificate and key named on the command line beat the variable.
    monkeypatch.setenv("X509_USER_P12", str(tmp_path / "nowhere.p12"))
    assert _init(grid, out) == 0


def test_pub_and_priv_are_cert_and_key_by_another_name(grid, out):
    """``-pub``/``-priv`` are the names a PEM pair is usually described by."""
    args = ["init", "-pub", str(grid.usercert), "-priv", str(grid.userkey), "-out", out, "-q"]
    assert cli.main(args) == 0
    assert describe_proxy(load_credential(out)).kind == "RFC3820 compliant impersonation proxy"
    # and they are one option each with -cert/-key, not a second pair
    assert cli.main(["init", "-pub", str(grid.usercert), "-out", out, "-q"]) == 1


def test_a_pem_pair_in_globus_under_other_names_is_found(grid, out, monkeypatch, tmp_path):
    """``cert.pem`` beside ``key.pem`` is a pair, not only ``usercert.pem``/``userkey.pem``."""
    home = tmp_path / "home"
    (home / ".globus").mkdir(parents=True)
    (home / ".globus" / "cert.pem").write_bytes(grid.usercert.read_bytes())
    Grid.write_key(home / ".globus" / "key.pem", key(2))
    monkeypatch.setenv("HOME", str(home))
    assert cli.main(["init", "-out", out, "-q"]) == 0
    assert describe_proxy(load_credential(out)).subject.startswith("/DC=org/DC=example")


def test_globus_files_that_are_neither_certificate_nor_key_are_left_alone(
    capsys, monkeypatch, tmp_path
):
    """A stray ``.pem`` and an unreadable one are skipped, not used and not guessed at."""
    home = tmp_path / "home"
    (home / ".globus").mkdir(parents=True)
    (home / ".globus" / "notes.pem").write_text("not a certificate or a key\n")
    unreadable = home / ".globus" / "locked.pem"
    unreadable.write_bytes(b"-----BEGIN CERTIFICATE-----\n")
    unreadable.chmod(0o000)
    monkeypatch.setenv("HOME", str(home))
    assert cli.main(["init", "-out", str(tmp_path / "x"), "-q"]) == 1
    err = capsys.readouterr().err
    assert "no user certificate found" in err and "~/.globus holds" not in err


def test_a_combined_pem_in_globus_is_used_for_both(grid, out, monkeypatch, tmp_path):
    """A file holding the certificate and the key is both halves of the pair."""
    home = tmp_path / "home"
    (home / ".globus").mkdir(parents=True)
    Grid.write_key(home / ".globus" / "me.pem", key(2))
    with (home / ".globus" / "me.pem").open("ab") as handle:
        handle.write(grid.usercert.read_bytes())
    monkeypatch.setenv("HOME", str(home))
    assert cli.main(["init", "-out", out, "-q"]) == 0


def test_two_pem_pairs_in_globus_are_not_guessed_between(grid, capsys, monkeypatch, tmp_path):
    """Two identities on disk: the user names the pair, the tool does not pick one."""
    home = tmp_path / "home"
    (home / ".globus").mkdir(parents=True)
    for prefix in ("a", "b"):
        (home / ".globus" / f"{prefix}-cert.pem").write_bytes(grid.usercert.read_bytes())
        Grid.write_key(home / ".globus" / f"{prefix}-key.pem", key(2))
    monkeypatch.setenv("HOME", str(home))
    assert cli.main(["init", "-out", str(tmp_path / "x"), "-q"]) == 1
    err = capsys.readouterr().err
    assert "2 certificate(s) and 2 key(s)" in err and "name the pair with -cert and -key" in err


def test_a_stale_bundle_variable_falls_through_to_the_pem_pair(
    grid, out, capsys, monkeypatch, tmp_path
):
    """A ``$X509_USER_P12`` that names nothing must not hide ``~/.globus``."""
    home = tmp_path / "home"
    (home / ".globus").mkdir(parents=True)
    (home / ".globus" / "usercert.pem").write_bytes(grid.usercert.read_bytes())
    Grid.write_key(home / ".globus" / "userkey.pem", key(2))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("X509_USER_P12", str(tmp_path / "gone.p12"))
    assert cli.main(["init", "-out", out]) == 0
    assert "X509_USER_P12 names" in capsys.readouterr().out  # it says what it skipped
    monkeypatch.delenv("X509_USER_P12")
    monkeypatch.setenv("HOME", str(tmp_path / "empty"))
    monkeypatch.setenv("X509_USER_P12", str(tmp_path / "gone.p12"))
    assert cli.main(["init", "-out", out, "-q"]) == 1
    assert "gone.p12, which is not there" in capsys.readouterr().err


def test_a_named_pem_pair_wins_over_the_bundle_variable(grid, out, monkeypatch, tmp_path):
    """Explicit files are used, whatever the environment says."""
    monkeypatch.setenv("X509_USER_P12", str(tmp_path / "gone.p12"))
    args = ["init", "-cert", str(grid.usercert), "-key", str(grid.userkey), "-out", out, "-q"]
    assert cli.main(args) == 0


def test_a_bundle_that_will_not_open_is_one_error_line(grid, out, capsys, monkeypatch, tmp_path):
    """A wrong pass phrase, and a file that is not there: one line each, exit 1."""
    bundle = _bundle(grid, tmp_path / "usercred.p12")
    monkeypatch.setattr("sys.stdin", io.StringIO("wrong\n"))
    assert cli.main(["init", "-p12", str(bundle), "-out", out, "-pwstdin"]) == 1
    assert "the pass phrase is wrong" in capsys.readouterr().err
    assert cli.main(["init", "-p12", str(tmp_path / "missing.p12"), "-out", out, "-q"]) == 1


def test_a_missing_bundle_with_none_nearby_says_only_that(grid, out, capsys, tmp_path):
    """Nothing to suggest is not a reason to say nothing, nor to guess."""
    assert cli.main(["init", "-p12", str(tmp_path / "gone.p12"), "-out", out, "-q"]) == 1
    said = capsys.readouterr().err
    assert "no PKCS#12 bundle at" in said and "found" not in said


def test_the_search_up_the_tree_stops_at_the_root():
    """``_walk_up`` ends where the filesystem does, however many levels it was asked for."""
    assert cli._walk_up("/") == ["/"]


def test_a_bundle_that_is_not_there_names_the_ones_that_are(grid, out, capsys, tmp_path):
    """A stale path is the usual cause, and the bundle it meant is one directory away.

    ``-p12 ~/certs/myCert.p12`` on a machine whose bundle is ``~/CERN_cert.p12``
    used to answer "[Errno 2] No such file or directory", which says nothing
    about what to type instead.
    """
    _bundle(grid, tmp_path / "CERN_cert.p12")
    assert cli.main(["init", "-p12", str(tmp_path / "certs/myCert.p12"), "-out", out, "-q"]) == 1
    said = capsys.readouterr().err
    assert "no PKCS#12 bundle at" in said
    assert "CERN_cert.p12" in said and "-p12" in said


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


def test_info_shows_the_voms_proxy_info_block_by_default(voms_proxy, grid, capsys):
    assert cli.main(["info", "-file", voms_proxy]) == 0
    text = capsys.readouterr().out
    assert "type      : RFC3820 compliant impersonation proxy" in text
    assert f"identity  : {grid.credential().identity}" in text
    assert "key usage : Digital Signature, Key Encipherment" in text


def test_info_shows_the_limits_and_the_chain_by_default(voms_proxy, grid, capsys):
    """The extended half: policy, path length, exact validity, and every link."""
    assert cli.main(["info", "-file", voms_proxy]) == 0
    text = capsys.readouterr().out
    assert "proxy     : impersonation" in text
    assert "path limit: unlimited" in text
    assert "not before: " in text and "not after : " in text
    assert "chain     : 2 certificates" in text
    assert "  [0] " in text and "  [1] " in text
    assert f"  [1] {grid.credential().identity}" in text  # the user certificate
    assert "      issuer : " in text and "      serial : " in text
    assert "      kind   : end entity credential" in text


def test_info_shows_each_ac_by_default(voms_proxy, capsys):
    """``voms-proxy-info`` needs -all for this; here it is the default."""
    assert cli.main(["info", "-file", voms_proxy]) == 0
    text = capsys.readouterr().out
    assert "=== VO atlas extension information ===" in text
    assert "attribute : /atlas/Role=NULL/Capability=NULL" in text
    assert "carrier   : certificate 0" in text
    assert "status    : decoded (describing does not check trust" in text


def test_info_all_is_the_default_and_short_is_the_block(voms_proxy, capsys):
    """``voms-proxy-info``'s spellings still work: ``-all`` asks for the default."""
    assert cli.main(["info", "-file", voms_proxy, "-all"]) == 0
    assert "=== VO atlas extension information ===" in capsys.readouterr().out
    assert cli.main(["info", "-file", voms_proxy, "-short"]) == 0
    text = capsys.readouterr().out
    assert "type      : RFC3820 compliant impersonation proxy" in text
    assert "=== VO" not in text and "chain     :" not in text and "proxy     :" not in text
    assert cli.main(["info", "-file", voms_proxy, "-brief"]) == 0  # the same, spelled short
    assert "chain     :" not in capsys.readouterr().out


def test_info_of_an_end_entity_lists_no_proxy_limits(grid, tmp_path, capsys):
    """A user certificate has no policy and no path length to report."""
    path = tmp_path / "credential"
    path.write_bytes(grid.usercert.read_bytes() + grid.userkey.read_bytes())
    os.chmod(path, 0o600)
    assert cli.main(["info", "-file", str(path)]) == 0
    text = capsys.readouterr().out
    assert "type      : end entity credential" in text
    assert "proxy     :" not in text and "path limit:" not in text
    assert "chain     : 1 certificate\n" in text


def test_info_reports_an_ac_that_did_not_decode_cleanly(voms_proxy):
    """A damaged AC says what went wrong instead of "decoded"."""
    import dataclasses

    from xrdsec.voms import VOMSStatus

    info = describe_proxy(load_credential(voms_proxy))
    entry = dataclasses.replace(info.voms[0], status=VOMSStatus.EXPIRED)
    (status,) = [line for line in cli._voms_lines(entry) if line.startswith("status")]
    assert status.startswith("status    : expired:")


def test_info_shows_an_acs_digest_and_diagnostics(voms_proxy):
    """Both are part of what an AC carries, so both are printed when present."""
    import dataclasses

    from xrdsec.voms import VOMSDiagnostic, VOMSStatus

    info = describe_proxy(load_credential(voms_proxy))
    broken = dataclasses.replace(
        info.voms[0],
        status=VOMSStatus.SIGNATURE,
        digest="sha256:1234",
        diagnostics=(VOMSDiagnostic("voms_signature", "the AC signature does not verify"),),
    )
    lines = cli._voms_lines(broken)
    assert "digest    : sha256:1234" in lines
    assert "diagnostic: the AC signature does not verify" in lines
    (status,) = [line for line in lines if line.startswith("status")]
    assert status.startswith("status    : signature:") and "does not verify" in status
    # An AC that a validator has passed says so in one word.
    assert cli._status_word(dataclasses.replace(broken, status=VOMSStatus.OK)) == "ok"


def test_list_vos_says_when_there_is_no_voms_dir(monkeypatch, capsys, tmp_path):
    """With no ``vomsdir`` there is nothing to look in, which is not the same as "no LSC"."""
    from xrdsec import credentials

    monkeypatch.setattr(credentials, "grid_voms_dir", lambda *a, **k: None)
    assert cli.main(["init", "-list-vos", "-vomses", _two_vos(tmp_path)]) == 0
    atlas, _cms = capsys.readouterr().out.splitlines()
    assert atlas.endswith("LSC: ? (no X509_VOMS_DIR)")


def _two_vos(tmp_path, atlas_server: str = "v1.example.org") -> str:
    """A vomses file for two VOs: ``atlas`` and ``cms``.

    The test grid's LSC is for ``voms.example.org``, so only a vomses entry that
    names *that* server has an LSC here: ```atlas_server`` picks which.
    """
    path = tmp_path / "vomses"
    path.write_text(
        f'"atlas" "{atlas_server}" "15000" "/CN={atlas_server}" "atlas"\n'
        '"cms" "v2.example.org" "15001" "/CN=v2" "cms"\n'
    )
    return str(path)


def test_init_list_vos_names_the_vos_and_their_servers(grid, capsys, tmp_path):
    assert cli.main(["init", "-list-vos", "-vomses", _two_vos(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "atlas" in text and "v1.example.org:15000" in text
    assert "cms" in text and "v2.example.org:15001" in text


def test_init_list_vos_says_which_vos_have_an_lsc(grid, capsys, tmp_path):
    """An AC for a VO without its LSC can be fetched and carried, but no site can validate it."""
    assert cli.main(["init", "-list-vos", "-vomses", _two_vos(tmp_path)]) == 0
    atlas, cms = capsys.readouterr().out.splitlines()
    # `atlas` is configured at v1.example.org, and the LSC here is for
    # voms.example.org: the directory exists, the LSC for *this* server does not.
    assert atlas.startswith("atlas") and atlas.endswith("LSC: no")
    assert cms.startswith("cms") and cms.endswith("LSC: no")


def test_init_list_vos_finds_the_lsc_for_the_server_it_was_given(grid, capsys, tmp_path):
    """``LSC: yes`` must mean "for the server in this entry", not "the directory exists"."""
    vomses = _two_vos(tmp_path, atlas_server="voms.example.org")
    assert cli.main(["init", "-list-vos", "-vomses", vomses]) == 0
    atlas, _cms = capsys.readouterr().out.splitlines()
    assert atlas.startswith("atlas") and atlas.endswith("LSC: yes")


def test_init_list_vos_reports_as_json(grid, capsys, tmp_path):
    assert cli.main(["init", "--list-vos", "-vomses", _two_vos(tmp_path), "--json", "-q"]) == 0
    result = next(r for r in _report(capsys)["records"] if r["kind"] == "result")
    assert [vo["vo"] for vo in result["vos"]] == ["atlas", "cms"]
    assert result["vos"][0]["lsc"] is None  # v1.example.org has no LSC in the test grid


def test_init_list_vos_json_names_the_lsc_file_it_matched(grid, capsys, tmp_path):
    vomses = _two_vos(tmp_path, atlas_server="voms.example.org")
    assert cli.main(["init", "--list-vos", "-vomses", vomses, "--json", "-q"]) == 0
    result = next(r for r in _report(capsys)["records"] if r["kind"] == "result")
    assert result["vos"][0]["lsc"].endswith("vomsdir/atlas/voms.example.org.lsc")
    assert result["vos"][1]["lsc"] is None


def test_init_collects_each_server_once_across_the_vos_asked_for(grid, capsys, tmp_path):
    """A VO and one of its aliases resolve to the same servers: each is collected once."""
    path = tmp_path / "vomses"
    path.write_text('"atlas-alias" "v1.example.org" "15000" "/CN=v1" "atlas"\n')
    args = ["init", "-voms", "atlas", "-voms", "atlas-alias", "-vomses", str(path), "-out"]
    assert cli.main([*args, str(tmp_path / "x"), "-q"]) == 1
    # both names were accepted (no VO complaint); what failed is the missing credential
    err = capsys.readouterr().err
    assert "no user certificate" in err and "no VOMS server" not in err


def test_init_names_a_mistyped_vo_before_looking_for_a_credential(grid, capsys, tmp_path):
    """Choosing a VO is the first thing checked: no certificate is needed to hear the typo."""
    args = ["init", "-voms", "atls", "-vomses", _two_vos(tmp_path), "-out", str(tmp_path / "x")]
    assert cli.main(args) == 1
    err = capsys.readouterr().err
    assert "no VOMS server is configured for 'atls'" in err
    assert "did you mean 'atlas'?" in err and "configured: atlas, cms" in err
    assert not (tmp_path / "x").exists()


def test_init_list_vos_is_silent_under_quiet(grid, capsys, tmp_path):
    assert cli.main(["init", "-list-vos", "-vomses", _two_vos(tmp_path), "-q"]) == 0
    assert capsys.readouterr().out == ""


def test_init_list_vos_says_when_no_vo_is_configured(capsys, tmp_path, monkeypatch):
    empty = tmp_path / "vomses"
    empty.write_text("# nothing here\n")
    monkeypatch.setenv("X509_VOMSES", str(empty))
    for name in ("VOMS_USERCONF", "X509_VOMS_FILE"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["init", "-list-vos"]) == 0
    assert "no VO is configured" in capsys.readouterr().out


def test_info_verify_checks_the_signatures_and_the_vo(voms_proxy, capsys):
    """The AC's signature, holder, issuer and LSC - what describing alone does not look at."""
    assert cli.main(["info", "-file", voms_proxy, "-verify"]) == 0
    text = capsys.readouterr().out
    assert "signature : ok  signed by " in text
    assert "issuer    : ok  " in text and " is in " in text
    assert "vo        : verified  atlas (LSC and signing policy in" in text
    # the verdict replaces the "decoded, trust not checked" disclaimer
    assert "status    : ok" in text and "does not check trust" not in text


def test_info_verify_fails_when_the_acs_trust_files_do_not_vouch(voms_proxy, monkeypatch, capsys):
    """A missing LSC is the VO's configuration, and the exit code says the proxy is not proven."""
    monkeypatch.setenv(
        "X509_VOMS_DIR", str(tmp_path_missing := __import__("pathlib").Path("/nope"))
    )
    assert cli.main(["info", "-file", voms_proxy, "-verify"]) == 1
    text = capsys.readouterr().out
    assert "vo        : !! atlas: lsc" in text
    assert str(tmp_path_missing) in text  # the diagnostic names the path it looked in


def test_info_verify_without_a_ca_directory_says_so(voms_proxy, monkeypatch, capsys):
    """No CA directory to judge the chain by is reported, not guessed at."""
    monkeypatch.setattr(credentials, "grid_ca_path", lambda *a, **k: None)
    assert cli.main(["info", "-file", voms_proxy, "-verify"]) == 0
    assert "issuer    : --  no CA directory to check the chain against" in capsys.readouterr().out


def test_info_verify_names_a_link_the_one_above_it_did_not_sign(voms_proxy, grid, tmp_path):
    """The check exists for the case where a chain was put together wrong."""
    from cryptography.hazmat.primitives.serialization import Encoding
    from xrdsec.proxies import load_credential
    from xrdsec.x509 import parse_certificate

    leaf = load_credential(voms_proxy).chain[0]  # the proxy: signed by the user, not by the CA
    anchor = parse_certificate(grid.ca.public_bytes(Encoding.DER))
    lines, passed = cli._signature_lines([leaf, anchor], str(grid.ca_dir))
    assert not passed
    assert any("is not signed by" in line for line in lines)
    # ... and a top link whose CA is not in the directory is named too
    lines, passed = cli._signature_lines([leaf, anchor], str(tmp_path))
    assert not passed
    assert any("is not in" in line for line in lines)
    # a single certificate has nothing above it to check
    lines, passed = cli._signature_lines([leaf], "")
    assert passed and lines[0].startswith("signature : --  a single certificate")


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
    assert [link["kind"] for link in result["chain"]] == [
        "RFC3820 compliant impersonation proxy",
        "end entity credential",
    ]
    assert result["chain"][0]["is_ca"] is False and result["chain"][0]["serial"] > 0
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
# doctor
# ---------------------------------------------------------------------------


def test_doctor_prints_each_line_and_its_advice(capsys):
    """The plumbing only: the checks themselves are ``xrdsec.doctor``'s, tested there."""
    from xrdsec import doctor

    def checked(sink, *, vo=None, proxy=None, servers=True, all_vos=False):
        sink.add("certs", "ok", "138 CAs, 134 CRLs")
        sink.add("credential", "bad", "no user certificate", doctor.do("add your .p12"))
        sink.add("vo", "warn", f"{vo}: not configured", doctor.ask_experiment("join the VO", vo))

    monkeypatch_doctor = pytest.MonkeyPatch()
    monkeypatch_doctor.setattr(doctor, "check", checked)
    try:
        assert cli.main(["doctor", "-voms", "lhcb:/lhcb/Role=pilot"]) == 1
    finally:
        monkeypatch_doctor.undo()
    text = capsys.readouterr().out
    assert "ok  certs         138 CAs, 134 CRLs" in text
    assert "!!  credential    no user certificate" in text
    assert "-> do: add your .p12" in text
    assert "-> ask your experiment (lhcb): join the VO" in text
    assert "vo            lhcb: not configured" in text


def test_doctor_reports_as_json(capsys):
    from xrdsec import doctor

    written: dict[str, object] = {}

    def checked(sink, *, vo=None, proxy=None, servers=True, all_vos=False):
        written.update(vo=vo, proxy=proxy, servers=servers)
        sink.add("certs", "ok", "1 CAs")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(doctor, "check", checked)
        assert cli.main(["doctor", "--json", "-voms", "atlas:/atlas", "-file", "/tmp/p"]) == 0
    [record] = [r for r in _report(capsys)["records"] if r["kind"] == "data"]
    document = record["value"]
    assert document["ok"] is True and document["vo"] == "atlas"
    assert document["checks"] == [{"name": "certs", "state": "ok", "detail": "1 CAs", "hint": ""}]
    assert written == {"vo": "atlas", "proxy": "/tmp/p", "servers": True}


def test_doctor_offline_does_not_contact_the_servers(capsys):
    from xrdsec import doctor

    seen: dict[str, object] = {}

    def checked(sink, *, vo=None, proxy=None, servers=True, all_vos=False):
        seen["servers"] = servers

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(doctor, "check", checked)
        assert cli.main(["doctor", "-offline", "-q"]) == 0
    assert seen == {"servers": False}
    assert capsys.readouterr().out == ""  # -q says nothing


def test_doctor_strict_and_summary_are_for_monitoring(capsys):
    """``--strict`` turns a warning into a failure, and ``--summary`` counts the marks."""
    from xrdsec import doctor

    def checked(sink, *, vo=None, proxy=None, servers=True, all_vos=False):
        sink.add("certs", "ok", "1 CAs")
        sink.add("proxy", "warn", "no proxy yet")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(doctor, "check", checked)
        assert cli.main(["doctor", "-q"]) == 0  # a warning alone is not a failure
        assert cli.main(["doctor", "-q", "--strict"]) == 1  # ... unless monitoring says so
        assert cli.main(["doctor", "--summary"]) == 0
    assert "1 ok, 1 warning(s), 0 problem(s)" in capsys.readouterr().out


def test_doctor_all_vos_asks_for_the_sweep(capsys):
    from xrdsec import doctor

    seen: dict[str, object] = {}

    def checked(sink, *, vo=None, proxy=None, servers=True, all_vos=False):
        seen.update(all_vos=all_vos)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(doctor, "check", checked)
        assert cli.main(["doctor", "-q", "--all-vos"]) == 0
    assert seen == {"all_vos": True}


def test_doctor_exits_one_for_something_that_stops_a_proxy(capsys, monkeypatch, tmp_path):
    """Against this machine's real environment: no credential anywhere is the failing case."""
    for name in ("X509_USER_CERT", "X509_USER_KEY", "X509_USER_P12", "X509_USER_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    assert cli.main(["doctor", "-offline", "-q"]) == 1


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


@pytest.mark.parametrize("invoked", ["xrd-auth", "xrd-proxy"])
def test_the_command_answers_to_each_name_it_is_installed_under(
    monkeypatch: pytest.MonkeyPatch, invoked: str
) -> None:
    """``xrd-auth`` and ``xrd-proxy`` are one program, and say which they were."""
    monkeypatch.setattr(sys, "argv", [f"/opt/homebrew/bin/{invoked}"])
    assert cli.program_name() == invoked


def test_another_name_leaves_the_usage_line_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test runner or an unrelated alias must not rename the command's usage."""
    monkeypatch.setattr(sys, "argv", ["/usr/bin/pytest"])
    assert cli.program_name() == cli.PROGRAM


def test_the_one_line_says_what_the_command_now_does() -> None:
    """It is no longer only proxies: tokens and the doctor are part of it."""
    assert "proxies" in cli.DESCRIPTION
    assert "tokens" in cli.DESCRIPTION


def test_an_error_is_prefixed_with_the_name_that_was_used(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`xrd-auth`'s complaints are `xrd-auth:`'s, not `xrd-proxy:`'s."""
    monkeypatch.setattr(sys, "argv", ["/opt/homebrew/bin/xrd-auth"])
    monkeypatch.setenv("WLCG_CLIENT_ID", "")
    assert cli.main(["token", "init", "--provider", "lhcb", "--flow", "device"]) != 0
    assert "xrd-auth:" in capsys.readouterr().err
