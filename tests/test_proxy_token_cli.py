"""``xrd-proxy token``: the three commands, driven at the command line.

The flows themselves are tested against a live fake issuer in
``xrdsec/tests/test_token_client.py``; here the question is the command: what
it asks for, where it writes, what it prints, and what it refuses.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import time
from typing import Any

import pytest
from xrdsec import token_client

from xrdclient.cli import proxy as cli


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwt(**claims: Any) -> str:
    """A token-shaped string: this suite never checks a signature."""
    now = int(time.time())
    body = {
        "iss": "https://wlcg.cloud.cnaf.infn.it/",
        "sub": "rcurrie",
        "aud": "https://wlcg.cern.ch/jwt/v1/any",
        "scope": "storage.read:/eos/user/r/rcurrie",
        "wlcg.groups": ["/lhcb"],
        "wlcg.ver": "1.0",
        "iat": now,
        "exp": now + 3600,
    }
    body.update(claims)
    header = _b64(b'{"alg": "RS256", "kid": "k"}')
    return f"{header}.{_b64(json.dumps(body).encode())}.{_b64(b'x')}"


@pytest.fixture
def issuer() -> token_client.Issuer:
    """A discovered issuer, as a test wants it without a server."""
    return token_client.Issuer(
        url="https://wlcg.cloud.cnaf.infn.it/",
        token_endpoint="https://wlcg.cloud.cnaf.infn.it/token",
        jwks_uri="https://wlcg.cloud.cnaf.infn.it/jwk",
        device_endpoint="https://wlcg.cloud.cnaf.infn.it/devicecode",
    )


@pytest.fixture
def token_file(tmp_path: Any) -> str:
    """A token where a client would find it, readable only by its owner."""
    path = tmp_path / "bt_u501"
    path.write_text(_jwt())
    path.chmod(0o600)
    return str(path)


def test_token_info_describes_what_the_tools_would_present(token_file: str, capsys: Any) -> None:
    assert cli.main(["token", "info", token_file]) == 0
    said = capsys.readouterr().out
    assert "wlcg.cloud.cnaf.infn.it" in said
    assert "storage.read:/eos/user/r/rcurrie" in said
    assert "timeleft" in said


def test_token_info_as_json_is_the_claims(token_file: str, capsys: Any) -> None:
    assert cli.main(["token", "info", token_file, "--json", "-q"]) == 0
    report = json.loads(capsys.readouterr().out)
    result = next(record for record in report["records"] if record["kind"] == "result")
    assert result["claims"]["sub"] == "rcurrie"
    assert result["path"] == token_file


def test_token_info_says_when_there_is_no_token(tmp_path: Any, capsys: Any) -> None:
    assert cli.main(["token", "info", str(tmp_path / "gone")]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_token_info_verify_needs_the_issuer(token_file: str, capsys: Any) -> None:
    """A signature cannot be checked against keys nobody named."""
    assert cli.main(["token", "info", token_file, "--verify"]) == 1
    assert "no key set" in capsys.readouterr().err


def test_token_info_verify_asks_the_issuer(
    token_file: str, issuer: Any, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr(cli, "_chosen_issuer", lambda args: issuer)
    seen: dict[str, Any] = {}

    def verified(token: str, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        seen["token"] = token
        return {"iss": issuer.url, "sub": "rcurrie", "exp": int(time.time()) + 60}

    monkeypatch.setattr(token_client, "verify", verified)
    assert cli.main(["token", "info", token_file, "--verify", "--issuer", issuer.url]) == 0
    assert seen["issuer"] is issuer
    assert seen["audience"] == token_client.AUDIENCE


def test_token_destroy_removes_the_file(token_file: str, capsys: Any) -> None:
    assert cli.main(["token", "destroy", "--out", token_file]) == 0
    assert "Removed" in capsys.readouterr().out
    assert not os.path.exists(token_file)


def test_token_destroy_dry_leaves_it_alone(token_file: str, capsys: Any) -> None:
    assert cli.main(["token", "destroy", "--out", token_file, "--dry"]) == 0
    assert "Would remove" in capsys.readouterr().out
    assert os.path.exists(token_file)


def test_token_destroy_says_when_there_is_nothing_to_remove(tmp_path: Any, capsys: Any) -> None:
    assert cli.main(["token", "destroy", "--out", str(tmp_path / "gone")]) == 1
    assert "there is no token" in capsys.readouterr().err


def test_token_destroy_leaves_another_users_file_alone(
    token_file: str, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr(os, "lstat", lambda path: os.stat_result((0, 0, 0, 0, 4242, 0, 0, 0, 0, 0)))
    assert cli.main(["token", "destroy", "--out", token_file]) == 1
    assert "belongs to uid 4242" in capsys.readouterr().err
    assert os.path.exists(token_file)


def test_token_init_without_an_issuer_says_what_to_do(capsys: Any, monkeypatch: Any) -> None:
    monkeypatch.delenv("WLCG_TOKEN_ISSUER", raising=False)
    assert cli.main(["token", "init", "--flow", "x509"]) == 1
    assert "no issuer" in capsys.readouterr().err


def test_token_init_writes_where_a_client_looks(
    tmp_path: Any, issuer: Any, monkeypatch: Any, capsys: Any
) -> None:
    """The whole command: ask, then write the answer where the discovery order puts it."""
    out = tmp_path / "bt_u501"
    monkeypatch.setattr(cli, "_chosen_issuer", lambda args: issuer)
    monkeypatch.setattr(
        token_client,
        "acquire",
        lambda *a, **k: token_client.Token(access_token=_jwt(), expires_in=3600),
    )
    monkeypatch.setattr(cli, "_signer", lambda args: object())
    assert (
        cli.main(["token", "init", "--flow", "x509", "--issuer", issuer.url, "--out", str(out)])
        == 0
    )
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert out.read_text().strip() == _jwt()
    said = capsys.readouterr().out
    assert "valid for" in said and "groups    : ['/lhcb']" in said


def test_token_init_exchange_narrows_the_named_token(
    tmp_path: Any, issuer: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(cli, "_chosen_issuer", lambda args: issuer)
    asked: dict[str, Any] = {}

    def exchanged(given: Any, subject: str, **kwargs: Any) -> token_client.Token:
        asked.update(kwargs, subject=subject, issuer=given)
        return token_client.Token(access_token=_jwt(), expires_in=60)

    monkeypatch.setattr(token_client, "exchange", exchanged)
    out = tmp_path / "narrow"
    assert (
        cli.main(
            [
                "token",
                "init",
                "--flow",
                "exchange",
                "--issuer",
                issuer.url,
                "--from-token",
                "the-broad-one",
                "--scope",
                "storage.read:/x",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert asked["subject"] == "the-broad-one"
    assert asked["scope"] == "storage.read:/x"
    assert out.exists()


def test_token_init_exchange_falls_back_to_the_token_in_hand(
    tmp_path: Any, issuer: Any, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr(cli, "_chosen_issuer", lambda args: issuer)
    monkeypatch.setattr(token_client, "read_token", lambda *a, **k: (_jwt(), "$BEARER_TOKEN_FILE"))
    monkeypatch.setattr(
        token_client,
        "exchange",
        lambda *a, **k: token_client.Token(access_token=_jwt(), expires_in=60),
    )
    assert (
        cli.main(
            [
                "token",
                "init",
                "--flow",
                "exchange",
                "--issuer",
                issuer.url,
                "--out",
                str(tmp_path / "o"),
            ]
        )
        == 0
    )


def test_token_init_by_device_flow_shows_a_code_and_waits(
    tmp_path: Any, issuer: Any, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr(cli, "_chosen_issuer", lambda args: issuer)
    monkeypatch.setattr(
        token_client,
        "device_authorization",
        lambda *a, **k: {
            "device_code": "dev-1",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://example.org/device",
            "verification_uri_complete": "https://example.org/device?c=ABCD-EFGH",
            "interval": 0,
            "expires_in": 30,
        },
    )
    polls: list[int] = []

    def polled(*a: Any, **k: Any) -> token_client.Token | None:
        polls.append(1)
        if len(polls) < 2:
            return None  # authorization_pending
        return token_client.Token(access_token=_jwt(), expires_in=60)

    monkeypatch.setattr(token_client, "device_poll", polled)
    out = tmp_path / "dev"
    assert (
        cli.main(["token", "init", "--issuer", issuer.url, "--client-id", "c", "--out", str(out)])
        == 0
    )
    said = capsys.readouterr().out
    assert "ABCD-EFGH" in said and "waiting for approval" in said
    assert out.exists() and len(polls) == 2


def test_token_init_by_device_flow_needs_a_client_id(
    issuer: Any, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr(cli, "_chosen_issuer", lambda args: issuer)
    monkeypatch.delenv("WLCG_CLIENT_ID", raising=False)
    assert cli.main(["token", "init", "--issuer", issuer.url]) == 1
    assert "needs --client-id" in capsys.readouterr().err


def test_token_init_by_device_flow_gives_up_when_the_code_expires(
    issuer: Any, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr(cli, "_chosen_issuer", lambda args: issuer)
    monkeypatch.setattr(
        token_client,
        "device_authorization",
        lambda *a, **k: {"device_code": "d", "user_code": "U", "interval": 0, "expires_in": 1},
    )
    monkeypatch.setattr(token_client, "device_poll", lambda *a, **k: None)
    assert cli.main(["token", "init", "--issuer", issuer.url, "--client-id", "c"]) == 1
    assert "expired" in capsys.readouterr().err


def test_an_issuer_can_be_discovered_from_a_resource(monkeypatch: Any, issuer: Any) -> None:
    monkeypatch.setattr(token_client, "discover", lambda where, **k: [issuer])
    found = cli._chosen_issuer(_args(from_resource="https://eosuser.cern.ch"))
    assert found is issuer


def _args(**values: Any) -> Any:
    """The namespace ``_chosen_issuer`` reads, without going through the parser."""
    import argparse

    base = {"from_resource": "", "issuer": "", "certdir": ""}
    base.update(values)
    return argparse.Namespace(**base)


def test_token_init_quiet_says_nothing(
    tmp_path: Any, issuer: Any, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr(cli, "_chosen_issuer", lambda args: issuer)
    monkeypatch.setattr(
        token_client,
        "acquire",
        lambda *a, **k: token_client.Token(access_token=_jwt(), expires_in=60),
    )
    monkeypatch.setattr(cli, "_signer", lambda args: object())
    out = tmp_path / "quiet"
    assert (
        cli.main(
            ["token", "init", "--flow", "x509", "--issuer", issuer.url, "--out", str(out), "-q"]
        )
        == 0
    )
    assert capsys.readouterr().out == ""


def test_token_info_understands_a_token_without_an_expiry(
    token_file: str, tmp_path: Any, capsys: Any
) -> None:
    bare = tmp_path / "bare"
    bare.write_text(_jwt(exp=None, iat=None))
    bare.chmod(0o600)
    bare.write_text(bare.read_text().replace(str(int(time.time()) + 3600), "null"))
    assert cli.main(["token", "info", str(bare)]) == 0
    assert "token     : from" in capsys.readouterr().out


def test_token_info_reads_the_token_a_client_would_present(
    tmp_path: Any, monkeypatch: Any, capsys: Any
) -> None:
    """With no file named, the search decides - the same one a transfer uses."""
    monkeypatch.setattr(token_client, "read_token", lambda *a, **k: (_jwt(), "$BEARER_TOKEN"))
    assert cli.main(["token", "info"]) == 0
    assert "$BEARER_TOKEN" in capsys.readouterr().out


def test_token_destroy_finds_the_token_by_the_search(
    tmp_path: Any, monkeypatch: Any, capsys: Any
) -> None:
    path = tmp_path / "bt_u501"
    path.write_text(_jwt())
    path.chmod(0o600)
    monkeypatch.setattr(token_client, "token_file_path", lambda *a, **k: str(path))
    assert cli.main(["token", "destroy"]) == 0
    assert not path.exists()


def test_token_destroy_says_when_nowhere_is_configured(monkeypatch: Any, capsys: Any) -> None:
    def nowhere(*a: Any, **k: Any) -> str:
        raise token_client.TokenError("there is nowhere to write a token")

    monkeypatch.setattr(token_client, "token_file_path", nowhere)
    assert cli.main(["token", "destroy"]) == 1
    assert "nowhere" in capsys.readouterr().err


def test_token_init_by_device_flow_quiet_still_waits(
    tmp_path: Any, issuer: Any, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr(cli, "_chosen_issuer", lambda args: issuer)
    monkeypatch.setattr(
        token_client,
        "device_authorization",
        lambda *a, **k: {
            "device_code": "d",
            "user_code": "U",
            "interval": "soon",
            "expires_in": "later",
        },
    )
    monkeypatch.setattr(
        token_client,
        "device_poll",
        lambda *a, **k: token_client.Token(access_token=_jwt(), expires_in=60),
    )
    assert (
        cli.main(
            [
                "token",
                "init",
                "--issuer",
                issuer.url,
                "--client-id",
                "c",
                "--out",
                str(tmp_path / "q"),
                "-q",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == ""


def test_token_info_of_a_token_with_no_expiry_says_nothing_about_time(
    tmp_path: Any, capsys: Any
) -> None:
    path = tmp_path / "noexp"
    claims = {"iss": "https://x/", "sub": "rcurrie"}
    header = _b64(b'{"alg": "RS256"}')
    body = f"{header}.{_b64(json.dumps(claims).encode())}.{_b64(b'x')}"
    path.write_text(body)
    path.chmod(0o600)
    assert cli.main(["token", "info", str(path)]) == 0
    said = capsys.readouterr().out
    assert "timeleft" not in said and "token     : from" in said


def test_token_destroy_quiet_says_nothing(token_file: str, monkeypatch: Any, capsys: Any) -> None:
    assert cli.main(["token", "destroy", "--out", token_file, "-q"]) == 0
    assert capsys.readouterr().out == ""


def test_an_issuer_named_outright_is_discovered(monkeypatch: Any, issuer: Any) -> None:
    monkeypatch.setattr(token_client, "discover", lambda where, **k: [issuer])
    found = cli._chosen_issuer(_args(issuer=issuer.url))
    assert found is issuer
