"""Bearer-token discovery: one order, the WLCG one, for ``root://`` and ``davs://`` alike.

Before 0.4.0 the two protocols disagreed: ``davs://`` read the token file
before ``$BEARER_TOKEN`` and never looked in ``$XDG_RUNTIME_DIR`` or ``/tmp``.
Every test here runs the same arrangement through both - ``ztn``'s
:meth:`~xrdclient.auth.ztn.TokenCredential.available` and the HTTP client's
:func:`~xrdclient.http.client.bearer_token` - so they cannot drift apart again.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import xrdclient
from xrdclient import auth
from xrdclient.auth.base import Offer
from xrdclient.auth.ztn import TokenCredential
from xrdclient.config import Config
from xrdclient.errors import InsecureCredentialError
from xrdclient.http.client import bearer_token
from xrdclient.testing import FakeDAVServer
from xrdclient.url import XRootDURL, parse

#: Every place a token can come from, most important first.
ORDER = ("url", "token", "explicit_file", "env", "env_file", "xdg", "tmp")


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o600)
    return path


@pytest.fixture
def places(tmp_path, monkeypatch):
    """A fresh runtime dir and ``/tmp`` stand-in, with nothing in either yet."""
    xdg, tmp = tmp_path / "xdg", tmp_path / "tmp"
    xdg.mkdir()
    tmp.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
    monkeypatch.setattr("xrdclient._tokens.TMP_DIR", str(tmp))
    return tmp_path


def arrange(
    root: Path, monkeypatch: pytest.MonkeyPatch, present: tuple[str, ...]
) -> tuple[Config, XRootDURL]:
    """Put a token named after its source in each of ``present``; return (config, url)."""
    name = f"bt_u{os.geteuid()}"
    url = parse("davs://dav.example.org/p" + ("?authz=url" if "url" in present else ""))
    explicit = _write(root / "explicit", "explicit_file\n") if "explicit_file" in present else None
    _place(root, monkeypatch, present, name)
    # Built last: an unset token_file is read from $BEARER_TOKEN_FILE, set above.
    config = Config()
    if "token" in present:
        config = config.evolve(token="token")
    if explicit is not None:
        config = config.evolve(token_file=str(explicit))
    return config, url


def _place(
    root: Path, monkeypatch: pytest.MonkeyPatch, present: tuple[str, ...], name: str
) -> None:
    """The environment and file sources among ``present``, each naming itself."""
    if "env" in present:
        monkeypatch.setenv("BEARER_TOKEN", "env")
    if "env_file" in present:
        monkeypatch.setenv("BEARER_TOKEN_FILE", str(_write(root / "from-env", "env_file")))
    if "xdg" in present:
        _write(root / "xdg" / name, " xdg \n")
    if "tmp" in present:
        _write(root / "tmp" / name, "tmp")


def ztn(config: Config) -> str | None:
    found = TokenCredential.available(Offer("ztn"), config, username="", host="h")
    return found.token if found is not None else None


@pytest.mark.parametrize("winner", ORDER)
def test_davs_takes_the_first_source_in_the_order(places, monkeypatch, winner):
    config, url = arrange(places, monkeypatch, ORDER[ORDER.index(winner) :])
    assert bearer_token(config, url) == winner


@pytest.mark.parametrize("winner", ORDER[1:])
def test_root_takes_the_first_source_in_the_same_order(places, monkeypatch, winner):
    """``root://`` has no URL token - an ``authz`` CGI there is the server's business."""
    config, _url = arrange(places, monkeypatch, ORDER[ORDER.index(winner) :])
    assert ztn(config) == winner


@pytest.mark.parametrize("alone", ORDER[1:])
def test_each_source_is_found_on_its_own_by_both(places, monkeypatch, alone):
    config, url = arrange(places, monkeypatch, (alone,))
    assert bearer_token(config, url) == ztn(config) == alone


def test_the_environment_outranks_its_own_file(places, monkeypatch):
    """The WLCG order: ``$BEARER_TOKEN`` before ``$BEARER_TOKEN_FILE`` - the
    order ``davs://`` used to get the wrong way round."""
    config, url = arrange(places, monkeypatch, ("env", "env_file"))
    assert config.token_file == os.environ["BEARER_TOKEN_FILE"]  # inherited, not explicit
    assert bearer_token(config, url) == ztn(config) == "env"


def test_a_token_file_set_to_the_environments_own_is_not_promoted(places, monkeypatch):
    config, url = arrange(places, monkeypatch, ("env", "env_file"))
    config = config.evolve(token_file=os.environ["BEARER_TOKEN_FILE"])
    assert bearer_token(config, url) == ztn(config) == "env"


def test_nothing_anywhere_is_none_for_both(places, monkeypatch):
    config, url = arrange(places, monkeypatch, ())
    assert bearer_token(config, url) is None and ztn(config) is None


def test_missing_and_blank_files_are_passed_over(places, monkeypatch):
    monkeypatch.setenv("BEARER_TOKEN_FILE", str(places / "absent"))
    _write(places / "xdg" / f"bt_u{os.geteuid()}", "  \n")
    _write(places / "tmp" / f"bt_u{os.geteuid()}", "tmp")
    config = Config(token_file=str(places / "also-absent"))
    assert bearer_token(config) == ztn(config) == "tmp"


def test_a_blank_environment_variable_is_passed_over(places, monkeypatch):
    monkeypatch.setenv("BEARER_TOKEN", "   ")
    _write(places / "tmp" / f"bt_u{os.geteuid()}", "tmp")
    assert bearer_token(Config()) == ztn(Config()) == "tmp"


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o620, 0o666])
def test_a_token_file_others_can_touch_stops_discovery_for_both(places, monkeypatch, mode):
    """Not skipped: falling through to the next location would log in as whoever left it."""
    _write(places / "tmp" / f"bt_u{os.geteuid()}", "tmp")
    path = _write(places / "xdg" / f"bt_u{os.geteuid()}", "xdg")
    path.chmod(mode)
    with pytest.raises(InsecureCredentialError, match=f"chmod 600 {path}") as refused:
        bearer_token(Config())
    assert refused.value.path == str(path)
    rejected: dict[str, str] = {}
    assert list(auth.select("&P=ztn", Config(), username="u", rejected=rejected)) == []
    assert "InsecureCredentialError" in rejected["ztn"] and "chmod 600" in rejected["ztn"]


@pytest.mark.parametrize("where", ["xdg", "tmp"])
def test_a_link_at_a_predictable_location_is_refused(places, monkeypatch, where):
    target = _write(places / "elsewhere", "planted")
    (places / where / f"bt_u{os.geteuid()}").symlink_to(target)
    with pytest.raises(InsecureCredentialError, match="symbolic link"):
        bearer_token(Config())
    with pytest.raises(InsecureCredentialError, match="symbolic link"):
        TokenCredential.available(Offer("ztn"), Config(), username="", host="h")


@pytest.mark.parametrize("how", ["config", "environment"])
def test_a_link_the_user_named_is_followed(places, monkeypatch, how):
    link = places / "link"
    link.symlink_to(_write(places / "real", "linked"))
    if how == "environment":
        monkeypatch.setenv("BEARER_TOKEN_FILE", str(link))
        config = Config()
    else:
        config = Config(token_file=str(link))
    assert bearer_token(config) == ztn(config) == "linked"


def test_the_token_files_watched_are_the_ones_read_in_order(places, monkeypatch):
    monkeypatch.setenv("BEARER_TOKEN_FILE", "/env")
    name = f"bt_u{os.geteuid()}"
    files = auth.credential_files(Config(token_file="/explicit", auth_order=("ztn",)))
    assert files == ("/explicit", "/env", str(places / "xdg" / name), str(places / "tmp" / name))
    assert "BEARER_TOKEN_FILE" in auth.AMBIENT_ENV


def test_a_davs_login_finds_the_token_in_the_runtime_dir(places, monkeypatch):
    """End to end: before 0.4.0 ``davs://`` never looked there."""
    _write(places / "xdg" / f"bt_u{os.geteuid()}", "s3cr3t")
    with FakeDAVServer(files={"/f": b"data"}) as dav:
        dav.require_token = "s3cr3t"
        with xrdclient.FileSystem(dav.url, Config()) as fs:
            assert fs.stat("/f").st_size == 4
        assert dav.headers[-1]["Authorization"] == "Bearer s3cr3t"
