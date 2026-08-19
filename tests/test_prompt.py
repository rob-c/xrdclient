"""Asking a person for the credentials this machine does not have.

The rules being pinned here are the ones that decide whether a library is
usable at a terminal and safe in a cron job: ask only when nothing works,
only for material somebody could type, only once per endpoint, never on
``stdout``, and never with the answer in a log line.
"""

from __future__ import annotations

import argparse
import io
import time

import pytest

from _pki import proxy_chain, throwaway_key
from xrd import auth
from xrd.auth import Ask, GSICredential, TokenCredential, UnixCredential, prompt
from xrd.auth.base import Credential, Offer
from xrd.auth.sss import SSSCredential
from xrd.cli import common_flags, config_from
from xrd.config import Config
from xrd.errors import NoMechanismError
from xrd.http import HTTPClient
from xrd.testing import FakeDAVServer
from xrd.url import parse

GSI = Offer("gsi")
ZTN = Offer("ztn")


@pytest.fixture(autouse=True)
def _quiet_environment(monkeypatch):
    """Nothing remembered, and no credential drifting in from the machine."""
    prompt.forget()
    for name in ("BEARER_TOKEN", "BEARER_TOKEN_FILE", "XDG_RUNTIME_DIR", "XRD_PROMPT"):
        monkeypatch.delenv(name, raising=False)
    yield
    prompt.forget()


@pytest.fixture(scope="module")
def key():
    return throwaway_key(0)


@pytest.fixture
def proxy_path(tmp_path, key):
    path = tmp_path / "x509up_u1000"
    path.write_bytes(proxy_chain(key))
    return str(path)


@pytest.fixture
def nowhere(tmp_path):
    """A configuration whose every credential path is empty."""
    return Config(proxy=str(tmp_path / "no-proxy"), keytab=str(tmp_path / "no-keytab"))


class Scripted:
    """A prompter that answers from a list and keeps every question it got."""

    def __init__(self, *answers: str | None) -> None:
        self.answers = list(answers)
        self.asked: list[Ask] = []

    def __call__(self, ask: Ask) -> str | None:
        self.asked.append(ask)
        return self.answers.pop(0) if self.answers else None


class Terminal(io.StringIO):
    """A stream that claims to be one."""

    def isatty(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# The question itself
# ---------------------------------------------------------------------------


def test_a_question_says_what_is_missing_why_and_how_to_fix_it():
    ask = Ask(
        mechanism="gsi",
        what="an X.509 proxy",
        reason="there is no file at /tmp/x509up_u1000",
        hint="voms-proxy-init -voms lhcb",
        prompt="path to a proxy file",
        host="eos.example.org",
    )
    assert ask.explain().splitlines() == [
        "xrd: eos.example.org accepts gsi, but an X.509 proxy is missing",
        "     why: there is no file at /tmp/x509up_u1000",
        "     fix: voms-proxy-init -voms lhcb",
    ]
    assert ask.question().endswith("(Enter to skip): ")
    assert ask.key == ("gsi", "eos.example.org")


def test_a_question_with_no_endpoint_still_reads_as_english():
    ask = Ask("ztn", "a bearer token", "nothing anywhere", "oidc-token", "the token")
    assert ask.explain().startswith("xrd: ztn is offered, but a bearer token is missing")
    assert "not echoed" not in ask.question()


def test_a_secret_question_promises_not_to_echo():
    ask = Ask("ztn", "a bearer token", "none", "oidc-token", "the token", secret=True)
    assert ask.question().endswith("(Enter to skip, not echoed): ")


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0s"), (-45, "45s"), (90, "1m"), (4500, "1h 15m"), (100_000, "1d 3h")],
)
def test_a_duration_reads_at_a_glance(seconds, text):
    assert prompt.humanise(seconds) == text


# ---------------------------------------------------------------------------
# When it is allowed to ask at all
# ---------------------------------------------------------------------------


def test_prompting_is_off_when_nobody_is_at_the_terminal(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO())
    monkeypatch.setattr("sys.stderr", Terminal())
    assert not prompt.interactive(Config())


def test_prompting_is_on_when_both_ends_are_a_terminal(monkeypatch):
    monkeypatch.setattr("sys.stdin", Terminal())
    monkeypatch.setattr("sys.stderr", Terminal())
    assert prompt.interactive(Config())


def test_a_stream_that_is_not_one_is_not_a_terminal(monkeypatch):
    monkeypatch.setattr("sys.stdin", object())  # a capture object, or a closed file
    assert not prompt.interactive(Config())


def test_the_configuration_overrules_the_terminal(monkeypatch):
    monkeypatch.setattr("sys.stdin", Terminal())
    monkeypatch.setattr("sys.stderr", Terminal())
    assert not prompt.interactive(Config(prompt=False))
    monkeypatch.setattr("sys.stdin", io.StringIO())
    assert prompt.interactive(Config(prompt=True))


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", False), ("no", False), ("off", False), ("false", False), ("1", True), ("yes", True)],
)
def test_the_environment_can_settle_it_for_a_whole_job(monkeypatch, value, expected):
    monkeypatch.setenv("XRD_PROMPT", value)
    assert Config().prompt is expected


def test_an_empty_environment_variable_decides_nothing(monkeypatch):
    monkeypatch.setenv("XRD_PROMPT", "  ")
    assert Config().prompt is None


# ---------------------------------------------------------------------------
# The terminal prompter
# ---------------------------------------------------------------------------


def test_the_question_goes_to_stderr_so_a_pipe_stays_clean(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("  /tmp/proxy.pem \n"))
    ask = Ask("gsi", "an X.509 proxy", "none", "voms-proxy-init", "path to a proxy file")
    assert prompt.ask_on_terminal(ask) == "/tmp/proxy.pem"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "an X.509 proxy is missing" in captured.err
    assert "path to a proxy file" in captured.err


def test_an_empty_answer_is_a_refusal(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    ask = Ask("gsi", "an X.509 proxy", "none", "voms-proxy-init", "path")
    assert prompt.ask_on_terminal(ask) is None


def test_end_of_input_is_a_refusal_with_the_newline_the_terminal_never_got(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    ask = Ask("gsi", "an X.509 proxy", "none", "voms-proxy-init", "path")
    assert prompt.ask_on_terminal(ask) is None
    assert capsys.readouterr().err.endswith("\n")


def test_a_secret_is_read_without_echoing_it(monkeypatch, capsys):
    seen = {}

    def fake_getpass(question, stream=None):
        seen["question"], seen["stream"] = question, stream
        return "eyJhbGciOi.payload.signature\n"

    monkeypatch.setattr("getpass.getpass", fake_getpass)
    ask = Ask("ztn", "a bearer token", "none", "oidc-token", "the token", secret=True)
    assert prompt.ask_on_terminal(ask) == "eyJhbGciOi.payload.signature"
    assert "the token" in seen["question"]
    assert seen["stream"] is not None  # never the terminal's echo, and never stdout


def test_a_refused_secret_is_a_refusal_too(monkeypatch):
    def fake_getpass(question, stream=None):
        raise EOFError

    monkeypatch.setattr("getpass.getpass", fake_getpass)
    ask = Ask("ztn", "a bearer token", "none", "oidc-token", "the token", secret=True)
    assert prompt.ask_on_terminal(ask) is None


# ---------------------------------------------------------------------------
# Remembering
# ---------------------------------------------------------------------------


def test_an_endpoint_is_asked_about_once(nowhere):
    scripted = Scripted("/tmp/one", "/tmp/two")
    ask = Ask("gsi", "a proxy", "none", "voms-proxy-init", "path", host="a.example.org")
    config = nowhere.evolve(prompt=True, prompter=scripted)
    assert prompt.answer_for(ask, config) == "/tmp/one"
    assert prompt.answer_for(ask, config) == "/tmp/one"
    assert len(scripted.asked) == 1


def test_a_second_endpoint_is_its_own_question(nowhere):
    scripted = Scripted("/tmp/one", "/tmp/two")
    config = nowhere.evolve(prompt=True, prompter=scripted)
    here = Ask("gsi", "a proxy", "none", "fix", "path", host="a.example.org")
    there = Ask("gsi", "a proxy", "none", "fix", "path", host="b.example.org")
    assert prompt.answer_for(here, config) == "/tmp/one"
    assert prompt.answer_for(there, config) == "/tmp/two"


def test_a_refusal_is_remembered_as_firmly_as_an_answer(nowhere):
    scripted = Scripted(None, "/tmp/late")
    ask = Ask("gsi", "a proxy", "none", "fix", "path", host="a.example.org")
    config = nowhere.evolve(prompt=True, prompter=scripted)
    assert prompt.answer_for(ask, config) is None
    assert prompt.answer_for(ask, config) is None
    assert len(scripted.asked) == 1


def test_forgetting_one_answer_leaves_the_others(nowhere):
    scripted = Scripted("/tmp/one", "/tmp/two", "/tmp/three")
    config = nowhere.evolve(prompt=True, prompter=scripted)
    here = Ask("gsi", "a proxy", "none", "fix", "path", host="a.example.org")
    there = Ask("gsi", "a proxy", "none", "fix", "path", host="b.example.org")
    prompt.answer_for(here, config)
    prompt.answer_for(there, config)
    prompt.forget(here)
    assert prompt.answer_for(here, config) == "/tmp/three"
    assert prompt.answer_for(there, config) == "/tmp/two"


def test_forgetting_everything_clears_a_pasted_token(nowhere):
    scripted = Scripted("secret-one", "secret-two")
    ask = Ask("ztn", "a token", "none", "fix", "token", secret=True)
    config = nowhere.evolve(prompt=True, prompter=scripted)
    assert prompt.answer_for(ask, config) == "secret-one"
    prompt.forget()
    assert prompt.answer_for(ask, config) == "secret-two"


def test_an_answer_can_be_planted_without_anybody_typing_it(nowhere):
    scripted = Scripted("never asked")
    ask = Ask("gsi", "a proxy", "none", "fix", "path", host="a.example.org")
    prompt.remember(ask, "/tmp/known")
    assert prompt.answer_for(ask, nowhere.evolve(prompt=True, prompter=scripted)) == "/tmp/known"
    assert scripted.asked == []


# ---------------------------------------------------------------------------
# What each mechanism asks for
# ---------------------------------------------------------------------------


def test_gsi_says_where_it_looked(nowhere):
    ask = GSICredential.missing(GSI, nowhere, username="jane", host="eos.example.org")
    assert ask is not None
    assert ask.mechanism == "gsi" and not ask.secret
    assert ask.reason == f"there is no file at {nowhere.proxy}"
    assert "voms-proxy-init" in ask.hint


def test_gsi_says_how_long_ago_the_proxy_expired(tmp_path, key):
    path = tmp_path / "stale.pem"
    path.write_bytes(proxy_chain(key, not_after=time.time() - 4500))
    ask = GSICredential.missing(GSI, Config(proxy=str(path)), username="", host="")
    assert ask is not None
    assert ask.reason == f"the proxy in {path} expired 1h 15m ago"


def test_gsi_says_when_the_file_is_not_a_proxy_at_all(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("this is not a certificate\n")
    ask = GSICredential.missing(GSI, Config(proxy=str(path)), username="", host="")
    assert ask is not None
    assert "could not be read as a proxy" in ask.reason


def test_gsi_says_when_the_key_is_the_wrong_kind(monkeypatch, proxy_path):
    loaded = auth.gsi.load_proxy(proxy_path)
    monkeypatch.setattr(
        auth.gsi, "load_proxy", lambda path: type(loaded)(loaded.chain, key=object(), path=path)
    )
    ask = GSICredential.missing(GSI, Config(proxy=proxy_path), username="", host="")
    assert ask is not None
    assert ask.reason.endswith("is not RSA, which is all GSI does")


def test_gsi_has_nothing_to_ask_when_the_proxy_is_good(proxy_path):
    assert GSICredential.missing(GSI, Config(proxy=proxy_path), username="", host="") is None


def test_gsi_takes_a_path_with_a_tilde_in_it(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert GSICredential.using("~/proxy.pem", Config()).proxy == str(tmp_path / "proxy.pem")


def test_ztn_lists_every_place_it_looked(nowhere, monkeypatch):
    monkeypatch.setattr("os.getuid", lambda: 4242)
    ask = TokenCredential.missing(ZTN, nowhere, username="", host="dav.example.org")
    assert ask is not None
    assert ask.secret  # a token is not for the scrollback
    assert "$BEARER_TOKEN" in ask.reason and "/tmp/bt_u4242" in ask.reason
    assert "oidc-token" in ask.hint


def test_ztn_has_nothing_to_ask_when_a_token_is_configured(nowhere):
    config = nowhere.evolve(token="eyJ.a.b")
    assert TokenCredential.missing(ZTN, config, username="", host="") is None


def test_a_pasted_token_is_used_as_a_token(nowhere):
    config = TokenCredential.using("eyJhbGciOi.payload.signature", nowhere)
    assert config.token == "eyJhbGciOi.payload.signature"
    assert config.token_file == nowhere.token_file


def test_a_token_path_is_read_from_instead(tmp_path, nowhere):
    path = tmp_path / "bt_u1000"
    path.write_text("eyJ.from.a.file\n")
    config = TokenCredential.using(str(path), nowhere.evolve(token="stale"))
    assert config.token is None and config.token_file == str(path)
    assert auth.discover_token(config) == "eyJ.from.a.file"


def test_sss_says_where_the_keytab_was_not(nowhere):
    ask = SSSCredential.missing(Offer("sss"), nowhere, username="", host="")
    assert ask is not None
    assert ask.reason == f"there is no keytab at {nowhere.keytab}"
    assert "xrdsssadmin" in ask.hint


def test_sss_says_when_the_keytab_is_readable_by_the_world(tmp_path):
    path = tmp_path / "sss.keytab"
    path.write_text("0 u:anon g:anon n:mykey N:1 c:1222183880 e:0 k:{}\n".format("aa" * 32))
    path.chmod(0o644)
    ask = SSSCredential.missing(Offer("sss"), Config(keytab=str(path)), username="", host="")
    assert ask is not None
    assert "readable" in ask.reason


def test_sss_says_when_the_keytab_holds_nothing_usable(tmp_path):
    path = tmp_path / "sss.keytab"
    path.write_text("# nothing here\n")
    path.chmod(0o600)
    ask = SSSCredential.missing(Offer("sss"), Config(keytab=str(path)), username="", host="")
    assert ask is not None
    assert ask.reason.endswith("holds no unexpired key")


def test_sss_says_when_the_key_the_server_wants_is_not_there(tmp_path):
    path = tmp_path / "sss.keytab"
    path.write_text("0 u:anon g:anon n:mykey N:1 c:1222183880 e:0 k:{}\n".format("aa" * 32))
    path.chmod(0o600)
    offer = Offer("sss", "n:theirs")
    ask = SSSCredential.missing(offer, Config(keytab=str(path)), username="", host="")
    assert ask is not None
    assert "no key named 'theirs'" in ask.reason


def test_sss_is_satisfied_by_a_keytab_that_works(tmp_path):
    path = tmp_path / "sss.keytab"
    path.write_text("0 u:anon g:anon n:mykey N:1 c:1222183880 e:0 k:{}\n".format("aa" * 32))
    path.chmod(0o600)
    config = Config(keytab=str(path))
    assert SSSCredential.missing(Offer("sss"), config, username="", host="") is None
    assert SSSCredential.using(str(path), Config()).keytab == str(path)


def test_a_directory_where_a_keytab_should_be_is_reported_as_such(tmp_path, monkeypatch):
    path = tmp_path / "sss.keytab"
    path.write_text("0 u:anon g:anon n:mykey N:1 c:1222183880 e:0 k:{}\n".format("aa" * 32))
    path.chmod(0o600)

    def unreadable(*args, **kwargs):
        raise OSError("input/output error")

    monkeypatch.setattr(auth.sss, "read_keytab", unreadable)
    ask = SSSCredential.missing(Offer("sss"), Config(keytab=str(path)), username="", host="")
    assert ask is not None
    assert "could not be read as a keytab" in ask.reason


def test_a_mechanism_that_needs_nothing_asks_for_nothing():
    assert UnixCredential.missing(Offer("unix"), Config(), username="a", host="b") is None
    with pytest.raises(NotImplementedError, match="unix"):
        UnixCredential.using("anything", Config())


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def test_a_working_mechanism_is_never_interrupted(nowhere):
    scripted = Scripted("/tmp/never-read")
    config = nowhere.evolve(prompt=True, prompter=scripted)
    creds = list(auth.select("&P=gsi&P=unix", config, username="jane"))
    assert [c.name for c in creds] == ["unix"]
    assert scripted.asked == []


def test_nothing_is_asked_when_there_is_nobody_to_ask(nowhere):
    scripted = Scripted("/tmp/never-read")
    config = nowhere.evolve(prompt=False, prompter=scripted)
    assert list(auth.select("&P=gsi", config, username="jane")) == []
    assert scripted.asked == []


def test_a_proxy_supplied_by_hand_authenticates(nowhere, proxy_path):
    scripted = Scripted(proxy_path)
    config = nowhere.evolve(prompt=True, prompter=scripted)
    creds = list(auth.select("&P=gsi", config, username="jane", host="eos.example.org"))
    assert [c.name for c in creds] == ["gsi"]
    assert scripted.asked[0].host == "eos.example.org"
    assert scripted.asked[0].what == "an X.509 proxy"


def test_a_token_supplied_by_hand_authenticates_and_stays_out_of_the_log(nowhere, caplog):
    scripted = Scripted("eyJhbGciOi.payload.signature")
    config = nowhere.evolve(prompt=True, prompter=scripted)
    with caplog.at_level("DEBUG", logger="xrd"):
        (cred,) = auth.select("&P=ztn", config, host="dav.example.org")
    assert isinstance(cred, TokenCredential)
    # A ``TokenResp``, not a bare token: the id, the header, then the token.
    assert cred.initial().startswith(b"ztn\x00")
    assert cred.initial().endswith(b"eyJhbGciOi.payload.signature\x00")
    assert "payload.signature" not in caplog.text
    assert "payload.signature" not in repr(cred)


def test_only_the_mechanisms_the_server_offered_are_asked_about(nowhere, proxy_path):
    scripted = Scripted(proxy_path)
    config = nowhere.evolve(prompt=True, prompter=scripted)
    list(auth.select("&P=ztn&P=gsi", config, host="eos.example.org"))
    assert [a.mechanism for a in scripted.asked] == ["gsi"]  # auth_order puts gsi first


def test_a_mechanism_this_client_does_not_have_is_not_asked_about_either(nowhere):
    scripted = Scripted("anything")
    rejected: dict[str, str] = {}
    config = nowhere.evolve(prompt=True, prompter=scripted)
    assert list(auth.select("&P=pwd", config, rejected=rejected)) == []
    assert "not supported" in rejected["pwd"]
    assert scripted.asked == []


def test_a_refusal_leaves_the_ladder_empty_rather_than_hanging_on(nowhere):
    scripted = Scripted(None)
    config = nowhere.evolve(prompt=True, prompter=scripted)
    assert list(auth.select("&P=gsi", config, host="eos.example.org")) == []
    assert len(scripted.asked) == 1


def test_a_typo_is_worth_one_more_question(nowhere, proxy_path, tmp_path):
    typo = str(tmp_path / "x509up_u100")  # the finger slipped off the last digit
    scripted = Scripted(typo, proxy_path)
    config = nowhere.evolve(prompt=True, prompter=scripted)
    creds = list(auth.select("&P=gsi", config, host="eos.example.org"))
    assert [c.name for c in creds] == ["gsi"]
    assert len(scripted.asked) == 2
    assert scripted.asked[1].reason == f"there is no file at {typo}"


def test_two_bad_answers_end_the_questioning_for_good(nowhere, tmp_path):
    scripted = Scripted(str(tmp_path / "a"), str(tmp_path / "b"), str(tmp_path / "c"))
    config = nowhere.evolve(prompt=True, prompter=scripted)
    assert list(auth.select("&P=gsi", config, host="eos.example.org")) == []
    assert len(scripted.asked) == 2
    assert list(auth.select("&P=gsi", config, host="eos.example.org")) == []
    assert len(scripted.asked) == 2  # the refusal was remembered


def test_the_error_says_what_to_do_when_there_is_nobody_to_ask(nowhere):
    with pytest.raises(NoMechanismError) as caught:
        auth.require("&P=gsi&P=ztn", nowhere.evolve(prompt=False), host="eos.example.org")
    message = str(caught.value)
    assert f"there is no file at {nowhere.proxy}" in message
    assert "voms-proxy-init" in message
    assert "$BEARER_TOKEN" in message


# ---------------------------------------------------------------------------
# Mechanisms that misbehave
# ---------------------------------------------------------------------------


class Quiet(Credential):
    """Unusable, and with nothing a person could do about it."""

    name = "qiet"

    def initial(self) -> bytes:
        return b""

    @classmethod
    def available(cls, offer, config, *, username, host):
        return None


class Broken(Quiet):
    """Cannot even say what it wants."""

    name = "brkn"

    @classmethod
    def missing(cls, offer, config, *, username, host):
        raise RuntimeError("keyring on fire")


class Fickle(Quiet):
    """Asks for a token, then denies it ever wanted one."""

    name = "fckl"

    @classmethod
    def missing(cls, offer, config, *, username, host):
        if config.token:
            return None
        return Ask(cls.name, "a token", "none anywhere", "make one", "the token", host=host)

    @classmethod
    def using(cls, answer, config):
        return config.evolve(token=answer)


@pytest.fixture
def odd_mechanisms():
    """Register the awkward mechanisms, and take them out again afterwards."""
    for cls in (Quiet, Broken, Fickle):
        auth.register(cls)
    yield
    for cls in (Quiet, Broken, Fickle):
        auth._REGISTRY.pop(cls.name, None)


def test_a_mechanism_with_nothing_to_ask_for_is_left_alone(nowhere, odd_mechanisms):
    scripted = Scripted("anything")
    rejected: dict[str, str] = {}
    config = nowhere.evolve(prompt=True, prompter=scripted)
    assert list(auth.select("&P=qiet", config, rejected=rejected)) == []
    assert rejected["qiet"] == "no credential material found"
    assert scripted.asked == []


def test_a_mechanism_that_cannot_explain_itself_is_still_not_fatal(nowhere, odd_mechanisms):
    scripted = Scripted("anything")
    rejected: dict[str, str] = {}
    config = nowhere.evolve(prompt=True, prompter=scripted)
    assert list(auth.select("&P=brkn&P=unix", config, rejected=rejected)) != []
    assert "keyring on fire" in rejected["brkn"]
    assert scripted.asked == []


def test_a_mechanism_that_cannot_explain_itself_is_not_asked_about(nowhere, odd_mechanisms):
    scripted = Scripted("anything")
    config = nowhere.evolve(prompt=True, prompter=scripted)
    assert list(auth.select("&P=brkn", config)) == []
    assert scripted.asked == []


def test_an_answer_that_stops_the_complaint_without_working_ends_it(nowhere, odd_mechanisms):
    scripted = Scripted("a-token", "another-token")
    config = nowhere.evolve(prompt=True, prompter=scripted)
    assert list(auth.select("&P=fckl", config, host="eos.example.org")) == []
    assert len(scripted.asked) == 1  # asking again would only repeat itself


def test_supply_hands_back_a_configuration_that_carries_the_answer(nowhere):
    scripted = Scripted("eyJ.a.b")
    config = nowhere.evolve(prompt=True, prompter=scripted)
    supplied = auth.supply("ztn", config, host="dav.example.org")
    assert supplied is not None and supplied.token == "eyJ.a.b"
    assert repr(supplied).count("'<redacted>'") == 1
    assert "eyJ.a.b" not in repr(supplied)


def test_supply_has_nothing_to_offer_for_a_mechanism_that_is_not_here(nowhere):
    assert auth.supply("nope", nowhere.evolve(prompt=True)) is None


def test_supply_stays_quiet_when_prompting_is_off(nowhere):
    scripted = Scripted("eyJ.a.b")
    assert auth.supply("ztn", nowhere.evolve(prompt=False, prompter=scripted)) is None
    assert scripted.asked == []


def test_supply_asks_nothing_when_the_material_is_already_there(nowhere):
    scripted = Scripted("eyJ.a.b")
    config = nowhere.evolve(prompt=True, prompter=scripted, token="already here")
    assert auth.supply("ztn", config) is None
    assert scripted.asked == []


def test_supply_gives_up_on_a_refusal(nowhere, odd_mechanisms):
    assert auth.supply("brkn", nowhere.evolve(prompt=True, prompter=Scripted())) is None
    assert auth.supply("ztn", nowhere.evolve(prompt=True, prompter=Scripted(None))) is None


# ---------------------------------------------------------------------------
# HTTP, where a 401 stands in for the security trailer
# ---------------------------------------------------------------------------


def test_a_401_asks_for_the_token_it_wants(nowhere):
    scripted = Scripted("open-sesame")
    with FakeDAVServer(files={"/d/a.root": b"hello"}) as dav:
        dav.require_token = "open-sesame"
        with HTTPClient(nowhere.evolve(prompt=True, prompter=scripted)) as client:
            response = client.request("GET", parse(f"{dav.url}/d/a.root"), expect=(200,))
    assert response.body == b"hello"
    assert [a.mechanism for a in scripted.asked] == ["ztn"]
    assert scripted.asked[0].host == "127.0.0.1"


def test_a_401_with_a_token_in_hand_is_a_refusal_not_a_question(nowhere):
    scripted = Scripted("open-sesame")
    config = nowhere.evolve(prompt=True, prompter=scripted, token="the wrong one")
    with FakeDAVServer(files={"/d/a.root": b"hello"}) as dav:
        dav.require_token = "open-sesame"
        with HTTPClient(config) as client:
            with pytest.raises(PermissionError):
                client.request("GET", parse(f"{dav.url}/d/a.root"), expect=(200,))
    assert scripted.asked == []


def test_a_401_over_tls_asks_for_the_proxy_first(nowhere, proxy_path):
    scripted = Scripted(proxy_path)
    client = HTTPClient(nowhere.evolve(prompt=True, prompter=scripted))
    assert client._ask_for_credentials(parse("https://dav.example.org/f"), _Challenge(None))
    assert client.config.proxy == proxy_path
    assert [a.mechanism for a in scripted.asked] == ["gsi"]


def test_a_bearer_challenge_over_tls_asks_for_a_token(nowhere):
    scripted = Scripted("eyJ.a.b")
    client = HTTPClient(nowhere.evolve(prompt=True, prompter=scripted))
    challenge = _Challenge('Bearer realm="wlcg", scope="storage.read:/"')
    assert client._ask_for_credentials(parse("https://dav.example.org/f"), challenge)
    assert client.config.token == "eyJ.a.b"


def test_a_401_that_nobody_answers_is_left_as_it_is(nowhere):
    client = HTTPClient(nowhere.evolve(prompt=True, prompter=Scripted(None, None)))
    assert not client._ask_for_credentials(parse("https://dav.example.org/f"), _Challenge(None))


class _Challenge:
    """Just enough of a response to carry a ``WWW-Authenticate`` header."""

    def __init__(self, header: str | None) -> None:
        self.header = header

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.header if name == "WWW-Authenticate" else default


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def _parsed(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    common_flags(parser)
    return parser.parse_args(list(argv))


def test_the_command_line_can_refuse_to_be_asked():
    assert config_from(_parsed("--no-prompt")).prompt is False


def test_the_command_line_can_insist_on_being_asked():
    assert config_from(_parsed("--prompt")).prompt is True


def test_the_command_line_leaves_the_decision_open_by_default():
    assert config_from(_parsed()).prompt is None


def test_the_two_flags_cannot_both_be_given():
    with pytest.raises(SystemExit):
        _parsed("--prompt", "--no-prompt")
