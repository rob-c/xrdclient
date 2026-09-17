from __future__ import annotations

import dataclasses

import pytest

from xrd import config as cfgmod
from xrd.config import Config, configure, current, override


def test_defaults_are_sane():
    c = Config()
    assert c.username
    assert c.redirect_limit > 0
    assert c.verify_tls is True
    assert c.auth_order[0] == "gsi"


def test_env_names_match_the_official_client(monkeypatch):
    monkeypatch.setenv("XRD_REQUESTTIMEOUT", "12.5")
    monkeypatch.setenv("XRD_REDIRECTLIMIT", "3")
    monkeypatch.setenv("XRD_CPCHUNKSIZE", "8192")
    c = Config()
    assert (c.request_timeout, c.redirect_limit, c.chunk_size) == (12.5, 3, 8192)


def test_unparseable_env_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("XRD_REDIRECTLIMIT", "not-a-number")
    default = Config.__dataclass_fields__["redirect_limit"].default_factory
    assert Config().redirect_limit == default()


def test_bearer_token_file_is_picked_up(monkeypatch):
    monkeypatch.setenv("BEARER_TOKEN_FILE", "/run/tok")
    assert Config().token_file == "/run/tok"


def test_evolve_is_a_copy():
    base = Config(username="a")
    other = base.evolve(username="b")
    assert (base.username, other.username) == ("a", "b")
    assert base is not other


def test_config_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        Config().username = "x"  # type: ignore[misc]


def test_repr_redacts_the_token():
    text = repr(Config(token="SECRET-JWT"))
    assert "SECRET-JWT" not in text
    assert "token='<redacted>'" in text


def test_repr_keeps_an_absent_token_readable():
    assert "token=None" in repr(Config())


def test_configure_replaces_the_current_config():
    saved = current()
    try:
        configure(username="scoped")
        assert current().username == "scoped"
    finally:
        cfgmod._current.set(saved)


def test_override_restores_on_exit():
    before = current()
    with override(username="temp") as c:
        assert c.username == "temp"
        assert current() is c
    assert current() is before


def test_override_restores_after_an_exception():
    before = current()
    with pytest.raises(RuntimeError), override(username="temp"):
        raise RuntimeError("boom")
    assert current() is before


@pytest.mark.parametrize(
    ("name", "attribute", "default"),
    [("XRD_REQUESTTIMEOUT", "request_timeout", 300.0), ("XRD_REDIRECTLIMIT", "redirect_limit", 16)],
)
def test_nonsense_in_the_environment_falls_back_to_the_default(
    monkeypatch, name, attribute, default
):
    """A typo in a shell profile must not make the client unusable."""
    monkeypatch.setenv(name, "half past two")
    assert getattr(Config(), attribute) == default


def test_the_username_comes_from_the_first_environment_variable_that_is_set(monkeypatch):
    for var in ("XRD_USER", "USER", "LOGNAME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOGNAME", "from-logname")
    assert Config().username == "from-logname"
    monkeypatch.setenv("XRD_USER", "from-xrd")
    assert Config().username == "from-xrd"


def test_a_machine_with_no_name_for_its_user_still_gets_a_config(monkeypatch):
    """No environment and no passwd entry: ``nobody`` beats a traceback."""
    import getpass

    for var in ("XRD_USER", "USER", "LOGNAME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(getpass, "getuser", lambda: (_ for _ in ()).throw(KeyError("no passwd")))
    assert Config().username == "nobody"


# ---------------------------------------------------------------------------
# The configuration file
# ---------------------------------------------------------------------------


def write_config(tmp_path, text):
    path = tmp_path / "config.ini"
    path.write_text(text)
    return str(path)


def test_a_file_sets_what_it_names_and_leaves_the_rest_alone(tmp_path):
    path = write_config(
        tmp_path,
        "[defaults]\nconnect_timeout = 10\nauth_order = gsi, ztn unix\nrequire_tls = yes\n",
    )
    c = Config.from_file(path)
    assert c.connect_timeout == 10.0
    assert c.auth_order == ("gsi", "ztn", "unix")
    assert c.require_tls is True
    assert c.redirect_limit == Config().redirect_limit


def test_an_alias_is_applied_on_top_of_the_defaults(tmp_path):
    path = write_config(
        tmp_path,
        "[defaults]\nusername = me\nrequire_tls = no\n[alias eos]\nrequire_tls = yes\n",
    )
    c = Config.from_file(path, alias="eos")
    assert (c.username, c.require_tls) == ("me", True)
    assert Config.from_file(path).require_tls is False


def test_an_alias_that_the_file_does_not_define_names_the_ones_it_does(tmp_path):
    path = write_config(tmp_path, "[alias eos]\nusername = me\n[alias ral]\nusername = you\n")
    with pytest.raises(KeyError, match="eos, ral"):
        Config.from_file(path, alias="cern")


def test_an_alias_with_no_file_at_all_is_an_error(monkeypatch, tmp_path):
    monkeypatch.delenv("XRD_CONFIG", raising=False)
    monkeypatch.setattr(cfgmod, "CONFIG_PATHS", (str(tmp_path / "absent.ini"),))
    with pytest.raises(FileNotFoundError, match="no alias 'eos'"):
        Config.from_file(alias="eos")
    assert Config.from_file() == Config()


def test_a_path_in_a_file_is_expanded_like_a_shell_would(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = write_config(tmp_path, "[defaults]\nproxy = ~/proxy.pem\n")
    assert Config.from_file(path).proxy == str(tmp_path / "proxy.pem")


def test_an_empty_value_for_an_optional_setting_clears_it(tmp_path):
    path = write_config(tmp_path, "[defaults]\nproxy =\nprompt =\n")
    c = Config.from_file(path)
    assert c.proxy is None and c.prompt is None


def test_settings_are_typed_the_way_the_dataclass_declares_them(tmp_path):
    path = write_config(
        tmp_path, "[defaults]\npool_size = 3\nretry_backoff = 0.25\nverify_tls = off\n"
    )
    c = Config.from_file(path)
    assert (c.pool_size, c.retry_backoff, c.verify_tls) == (3, 0.25, False)


@pytest.mark.parametrize(
    ("text", "complaint"),
    [
        ("[defaults]\npool_size = many\n", "not a number"),
        ("[defaults]\nverify_tls = perhaps\n", "not a yes/no value"),
        ("[defaults]\nusernme = me\n", "did you mean username"),
        ("[defaults]\nnonsense = 1\n", "unknown setting"),
        ("[defaults]\ntoken = secret\n", "use token_file instead"),
        ("[defaults]\nprompter = mine\n", "it is a callable"),
    ],
)
def test_a_file_that_cannot_mean_what_it_says_is_refused(tmp_path, text, complaint):
    path = write_config(tmp_path, text)
    with pytest.raises(ValueError, match=complaint):
        Config.from_file(path)


def test_a_secret_in_a_file_is_refused_without_being_quoted_back(tmp_path):
    """The complaint must not carry the token it is complaining about."""
    path = write_config(tmp_path, "[defaults]\ntoken = supersecret\n")
    with pytest.raises(ValueError) as caught:
        Config.from_file(path)
    assert "supersecret" not in str(caught.value)


def test_a_file_that_is_not_ini_says_so_with_its_name(tmp_path):
    path = write_config(tmp_path, "this is not a configuration file\n")
    with pytest.raises(ValueError, match=r"config\.ini"):
        Config.from_file(path)


def test_a_dash_reads_the_same_as_an_underscore(tmp_path):
    path = write_config(tmp_path, "[defaults]\nconnect-timeout = 7\n")
    assert Config.from_file(path).connect_timeout == 7.0


def test_the_environment_names_the_file_and_is_taken_at_its_word(monkeypatch, tmp_path):
    named = write_config(tmp_path, "[defaults]\nusername = named\n")
    monkeypatch.setenv("XRD_CONFIG", named)
    assert cfgmod.find_config_file() == named
    assert Config.from_file().username == "named"
    monkeypatch.setenv("XRD_CONFIG", str(tmp_path / "gone.ini"))
    with pytest.raises(FileNotFoundError):
        Config.from_file()


def test_the_search_order_is_the_documented_one(monkeypatch, tmp_path):
    monkeypatch.delenv("XRD_CONFIG", raising=False)
    first, second = tmp_path / "first.ini", tmp_path / "second.ini"
    monkeypatch.setattr(cfgmod, "CONFIG_PATHS", (str(first), str(second)))
    assert cfgmod.find_config_file() is None
    second.write_text("")
    assert cfgmod.find_config_file() == str(second)
    first.write_text("")
    assert cfgmod.find_config_file() == str(first)
