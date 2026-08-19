"""The authentication ladder and the mechanisms it picks between.

The SSS encoding is pinned by decrypting the blob back apart, which is the
only check that proves the header offsets and the CRC polynomial are right.
"""

from __future__ import annotations

import logging
import os
import struct
import time
import traceback
import zlib

import pytest

from xrd import auth
from xrd._log import redact
from xrd.auth.base import Offer, parse_security_trailer
from xrd.auth.simple import HostCredential, UnixCredential
from xrd.auth.sss import (
    BASE_TIME,
    SSSCredential,
    SSSKey,
    build_credential,
    default_keytab_path,
    read_keytab,
)
from xrd.auth.ztn import TokenCredential, discover_token, token_claims, token_expiry
from xrd.config import Config
from xrd.crypto.blowfish import Blowfish
from xrd.errors import CredentialError, NoMechanismError, TokenExpiredError

# --------------------------------------------------------------------------
# The security trailer
# --------------------------------------------------------------------------


def test_parse_security_trailer_keeps_the_server_order():
    offers = parse_security_trailer("&P=gsi,v:10400,ca:1&P=ztn&P=unix")
    assert [o.name for o in offers] == ["gsi", "ztn", "unix"]
    assert offers[0].params == "v:10400,ca:1"
    assert offers[1].params == ""


def test_offer_options_split_on_colons():
    assert Offer("sss", "n:mykey,c:aes").options() == {"n": "mykey", "c": "aes"}


def test_offer_options_ignore_a_bare_flag():
    assert Offer("gsi", "v:1,noauth").options() == {"v": "1"}


def test_offer_str_round_trips():
    assert str(Offer("ztn")) == "ztn"
    assert str(Offer("ztn", "ver:1")) == "ztn,ver:1"


def test_an_empty_trailer_offers_nothing():
    assert parse_security_trailer("") == []


# --------------------------------------------------------------------------
# unix and host
# --------------------------------------------------------------------------


def test_unix_credential_is_the_tag_then_the_username():
    assert UnixCredential("bob").initial() == b"unix\x00bob"


def test_unix_falls_back_to_the_configured_username():
    cred = UnixCredential.available(Offer("unix"), Config(username="cfg"), username="", host="h")
    assert cred.initial() == b"unix\x00cfg"


def test_an_explicit_username_beats_the_config():
    cred = UnixCredential.available(
        Offer("unix"), Config(username="cfg"), username="explicit", host="h"
    )
    assert cred.username == "explicit"


def test_host_credential_carries_no_material():
    assert HostCredential().initial() == b"host\x00"
    assert HostCredential.available(Offer("host"), Config(), username="", host="h") is not None


# --------------------------------------------------------------------------
# ztn
# --------------------------------------------------------------------------

JWT_HEADER = "eyJhbGciOiJub25lIn0"  # {"alg":"none"}


def jwt(exp: int | None = None) -> str:
    import base64
    import json

    claims = {"sub": "user"} | ({"exp": exp} if exp is not None else {})
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{JWT_HEADER}.{payload}.sig"


def test_token_claims_decode_without_verifying():
    assert token_claims(jwt(4102444800))["sub"] == "user"


def test_token_claims_of_something_that_is_not_a_jwt():
    assert token_claims("opaque-token") == {}
    assert token_claims("a.b") == {}


def test_token_expiry_reads_the_exp_claim():
    assert token_expiry(jwt(4102444800)) == 4102444800.0
    assert token_expiry(jwt()) is None


def test_a_live_token_produces_the_ztn_blob():
    token = jwt(int(time.time()) + 3600)
    blob = TokenCredential(token).initial()
    assert blob == (
        b"ztn\x00\x00T\x00\x00"
        + struct.pack(">H", len(token) + 1)
        + token.encode()
        + b"\x00"
    )


def test_the_ztn_blob_is_what_stock_reads():
    """The fields ``XrdSecProtocolztn::Authenticate`` actually looks at."""
    blob = TokenCredential("opaque").initial()
    assert blob[:4] == b"ztn\x00"  # id[4], doubling as the credtype
    assert blob[4] == 0  # ver
    assert blob[5] == ord("T")  # opr = IsTkn, not a token character
    assert blob[6:8] == b"\x00\x00"  # rsvd
    (length,) = struct.unpack(">H", blob[8:10])
    assert length == len("opaque") + 1  # the trailing NUL counts
    assert blob[10 : 10 + length - 1] == b"opaque"
    assert blob[10 + length - 1] == 0  # stock insists on this NUL
    assert len(blob) == 10 + length


def test_an_expired_token_fails_before_the_round_trip():
    with pytest.raises(TokenExpiredError, match="expired at"):
        TokenCredential(jwt(int(time.time()) - 10)).initial()


def test_an_opaque_token_is_sent_as_is():
    assert TokenCredential("opaque").initial().endswith(b"opaque\x00")


def test_token_repr_hides_the_token():
    text = repr(TokenCredential(jwt(4102444800)))
    assert "token=<redacted>" in text
    assert JWT_HEADER not in text


def test_discover_token_prefers_the_config(monkeypatch):
    monkeypatch.setenv("BEARER_TOKEN", "from-env")
    assert discover_token(Config(token="  from-config  ")) == "from-config"


def test_discover_token_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("BEARER_TOKEN", "from-env\n")
    monkeypatch.setattr("os.getuid", lambda: 999999)
    assert discover_token(Config()) == "from-env"


def test_discover_token_reads_the_token_file(monkeypatch, tmp_path):
    path = tmp_path / "bt"
    path.write_text("from-file\n")
    monkeypatch.delenv("BEARER_TOKEN", raising=False)
    assert discover_token(Config(token_file=str(path))) == "from-file"


def test_discover_token_skips_an_empty_file(monkeypatch, tmp_path):
    (tmp_path / "bt").write_text("   \n")
    monkeypatch.delenv("BEARER_TOKEN", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr("os.getuid", lambda: 999999)
    assert discover_token(Config(token_file=str(tmp_path / "bt"))) is None


def test_ztn_is_unavailable_without_a_token(monkeypatch, tmp_path):
    monkeypatch.delenv("BEARER_TOKEN", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr("os.getuid", lambda: 999999)
    assert TokenCredential.available(Offer("ztn"), Config(), username="", host="h") is None


def test_a_token_bigger_than_the_server_takes_is_refused_before_the_wire():
    """The offer's ``<expiry>:<maxtsz>:`` names a size the server will refuse."""
    config = Config(token="x" * 65)
    with pytest.raises(CredentialError, match=r"65 bytes and h accepts at most 64"):
        TokenCredential.available(Offer("ztn", "0:64:"), config, username="", host="h")


def test_a_token_within_the_stated_size_is_used():
    cred = TokenCredential.available(
        Offer("ztn", "0:4096:"), Config(token="tok"), username="", host="h"
    )
    assert cred is not None and cred.token == "tok"


def test_a_token_without_the_life_the_server_wants_is_refused():
    config = Config(token=jwt(int(time.time()) + 30))
    with pytest.raises(CredentialError, match=r"wants 300s of life"):
        TokenCredential.available(Offer("ztn", "300:4096:"), config, username="", host="h")


def test_a_lifetime_requirement_cannot_read_an_opaque_token():
    """No ``exp`` claim, nothing to hold against it - the server decides."""
    cred = TokenCredential.available(
        Offer("ztn", "300:4096:"), Config(token="opaque"), username="", host="h"
    )
    assert cred is not None


def test_legacy_ztn_parameters_are_no_requirement_at_all():
    for params in ("", "ver:1", "v:10000"):
        cred = TokenCredential.available(
            Offer("ztn", params), Config(token="x" * 5000), username="", host="h"
        )
        assert cred is not None


def test_select_reports_the_oversized_token_and_moves_on():
    rejected: dict[str, str] = {}
    names = [
        c.name
        for c in auth.select(
            "&P=ztn,0:8:&P=host", Config(token="x" * 9), username="b", rejected=rejected
        )
    ]
    assert names == ["host"]
    assert "accepts at most 8" in rejected["ztn"]


# --------------------------------------------------------------------------
# sss
# --------------------------------------------------------------------------

KEY = SSSKey(id=1, secret=bytes(range(16)), name="mykey", user="anon", group="anon")


def test_the_sss_cleartext_header_is_sixteen_bytes():
    blob = build_credential(KEY, "bob", nonce=bytes(32), gen_time=0)
    assert blob[:4] == b"sss\x00"
    assert blob[4] == 1  # version
    assert blob[6] == 0  # unnamed key
    assert blob[7] == ord("0")  # Blowfish-32
    assert struct.unpack(">q", blob[8:16])[0] == KEY.id


def test_the_sss_body_decrypts_to_the_documented_layout():
    nonce = bytes(range(32))
    blob = build_credential(KEY, "bob", nonce=nonce, gen_time=12345)
    plain = Blowfish(KEY.secret).decrypt_cfb64(bytes(8), blob[16:])

    assert plain[:32] == nonce
    assert struct.unpack(">I", plain[32:36])[0] == 12345
    assert plain[39] == 0x00  # kSecOptsDataV
    assert plain[40:43] == bytes([0x01, 0x00, 4])  # NAME, pad, len("bob\0")
    assert plain[43:47] == b"bob\x00"
    assert struct.unpack(">I", plain[-4:])[0] == zlib.crc32(plain[:-4]) & 0xFFFFFFFF


def test_the_sss_checksum_is_ieee_not_castagnoli():
    from xrd.crypto.crc32c import crc32c

    plain = Blowfish(KEY.secret).decrypt_cfb64(
        bytes(8), build_credential(KEY, "u", nonce=bytes(32), gen_time=0)[16:]
    )
    assert struct.unpack(">I", plain[-4:])[0] != crc32c(plain[:-4])


def test_an_empty_username_gets_a_placeholder():
    plain = Blowfish(KEY.secret).decrypt_cfb64(
        bytes(8), build_credential(KEY, "", nonce=bytes(32), gen_time=0)[16:]
    )
    assert plain[43:47] == b"xrd\x00"


def test_a_long_username_is_truncated_to_the_field():
    plain = Blowfish(KEY.secret).decrypt_cfb64(
        bytes(8), build_credential(KEY, "u" * 200, nonce=bytes(32), gen_time=0)[16:]
    )
    assert plain[42] == 64
    assert plain[43:107] == b"u" * 63 + b"\x00"


def test_the_nonce_must_be_the_right_width():
    with pytest.raises(ValueError, match="must be 32 bytes"):
        build_credential(KEY, "u", nonce=b"short", gen_time=0)


def test_a_generated_credential_is_never_the_same_twice():
    assert build_credential(KEY, "u") != build_credential(KEY, "u")


def test_the_generated_timestamp_counts_from_the_sss_epoch():
    plain = Blowfish(KEY.secret).decrypt_cfb64(bytes(8), build_credential(KEY, "u")[16:])
    stamp = struct.unpack(">I", plain[32:36])[0]
    assert abs(stamp - (int(time.time()) - BASE_TIME)) < 5


def test_an_expired_key_is_refused_at_use_time():
    key = SSSKey(id=2, secret=b"k" * 16, expires=1)
    with pytest.raises(CredentialError, match="expired"):
        SSSCredential(key, "bob").initial()


def test_a_key_without_an_expiry_never_expires():
    assert SSSKey(id=1, secret=b"k", expires=0).expired is False


def test_sss_key_repr_hides_the_secret():
    assert repr(KEY) == "SSSKey(id=1, name='mykey', secret=<redacted>)"
    assert "000102" not in repr(KEY)


def test_sss_credential_repr_hides_the_secret():
    assert "secret=<redacted>" in repr(SSSCredential(KEY, "bob"))


# --------------------------------------------------------------------------
# keytabs
# --------------------------------------------------------------------------

KEYTAB = """\
# a comment
0 u:anon g:anon n:mykey N:1 c:1222183880 e:0 k:{}
0 u:bob g:bob n:other N:2 c:1222183880 e:0 k:{}
""".format("aa" * 32, "bb" * 32)


def write_keytab(tmp_path, text=KEYTAB):
    path = tmp_path / "sss.keytab"
    path.write_text(text)
    path.chmod(0o600)
    return str(path)


def test_read_keytab_parses_every_field(tmp_path):
    keys = read_keytab(write_keytab(tmp_path))
    assert [k.id for k in keys] == [1, 2]
    assert keys[0].name == "mykey"
    assert keys[0].user == "anon" and keys[0].group == "anon"
    assert keys[0].secret == b"\xaa" * 32


def test_read_keytab_skips_comments_and_blank_lines(tmp_path):
    assert read_keytab(write_keytab(tmp_path, "\n# nothing\n\n")) == []


def test_read_keytab_drops_an_entry_without_a_secret(tmp_path):
    assert read_keytab(write_keytab(tmp_path, "0 u:anon N:1 e:0\n")) == []


def test_read_keytab_drops_expired_keys_unless_asked(tmp_path):
    text = f"0 u:a g:a n:old N:1 e:1 k:{'cc' * 32}\n"
    assert read_keytab(write_keytab(tmp_path, text)) == []
    assert len(read_keytab(write_keytab(tmp_path, text), include_expired=True)) == 1


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o666])
def test_a_keytab_others_can_read_is_refused(tmp_path, mode):
    path = write_keytab(tmp_path)
    os.chmod(path, mode)
    with pytest.raises(PermissionError) as caught:
        read_keytab(path)
    assert "chmod 600" in str(caught.value)
    # The secret must not travel in the message the user is about to paste
    # into a bug report.
    assert "aa" * 32 not in str(caught.value)
    assert read_keytab(path, require_private=False)[0].id == 1


def test_an_exposed_keytab_is_skipped_with_a_warning(tmp_path, caplog):
    config = Config(keytab=write_keytab(tmp_path))
    os.chmod(config.keytab, 0o644)
    with caplog.at_level(logging.WARNING, logger="xrd.xrd.auth.sss"):
        assert SSSCredential.available(Offer("sss"), config, username="", host="h") is None
    assert "readable by group or others" in caplog.text


def test_a_private_keytab_is_accepted(tmp_path):
    path = write_keytab(tmp_path)
    os.chmod(path, 0o400)
    assert len(read_keytab(path)) == 2


def test_default_keytab_path_prefers_the_config(monkeypatch):
    monkeypatch.setenv("XrdSecSSSKT", "/from/env")
    assert default_keytab_path(Config(keytab="/from/config")) == "/from/config"


def test_default_keytab_path_reads_both_environment_spellings(monkeypatch):
    monkeypatch.delenv("XrdSecSSSKT", raising=False)
    monkeypatch.setenv("XrdSecsssKT", "/lower")
    assert default_keytab_path(Config()) == "/lower"


def test_the_environment_is_read_again_for_a_config_that_names_no_keytab(monkeypatch):
    """A ``Config`` built before the variable was set still honours it."""
    monkeypatch.setenv("XrdSecSSSKT", "/set/afterwards")
    assert default_keytab_path(Config(keytab=None)) == "/set/afterwards"


def test_default_keytab_path_falls_back_to_the_home_directory(monkeypatch):
    monkeypatch.delenv("XrdSecSSSKT", raising=False)
    monkeypatch.delenv("XrdSecsssKT", raising=False)
    assert default_keytab_path(Config()).endswith("/.xrd/sss.keytab")


def test_sss_selects_the_named_key_the_server_asked_for(tmp_path):
    config = Config(keytab=write_keytab(tmp_path))
    cred = SSSCredential.available(Offer("sss", "n:other"), config, username="bob", host="h")
    assert cred.key.id == 2


def test_sss_takes_the_first_key_when_no_name_is_offered(tmp_path):
    config = Config(keytab=write_keytab(tmp_path))
    cred = SSSCredential.available(Offer("sss"), config, username="bob", host="h")
    assert cred.key.id == 1


def test_sss_is_unavailable_when_the_named_key_is_missing(tmp_path):
    config = Config(keytab=write_keytab(tmp_path))
    assert SSSCredential.available(Offer("sss", "n:absent"), config, username="", host="h") is None


def test_sss_is_unavailable_without_a_keytab(tmp_path):
    config = Config(keytab=str(tmp_path / "nope"))
    assert SSSCredential.available(Offer("sss"), config, username="", host="h") is None


def test_a_keytab_line_that_is_not_a_key_is_skipped(tmp_path):
    """The first field is the format flag; anything else is not an entry."""
    text = f"junk u:a g:a n:k N:1 e:0 k:{'dd' * 32}\n0 u:b g:b n:real N:2 e:0 k:{'ee' * 32}\n"
    assert [k.name for k in read_keytab(write_keytab(tmp_path, text))] == ["real"]


def test_a_comment_at_the_end_of_a_keytab_line_is_not_a_field(tmp_path):
    text = f"0 u:a g:a n:real N:1 e:0 k:{'ff' * 32} # minted by hand\n"
    keys = read_keytab(write_keytab(tmp_path, text))
    assert [k.name for k in keys] == ["real"] and keys[0].secret == b"\xff" * 32


def test_a_field_that_is_not_a_tagged_value_is_ignored(tmp_path):
    """Stray words in a hand-edited keytab lose the line no more than a typo."""
    text = f"0 u:a stray g:a n:real N:1 e:0 k:{'ab' * 32}\n"
    assert [k.name for k in read_keytab(write_keytab(tmp_path, text))] == ["real"]


def test_a_keytab_with_no_usable_entries_offers_no_credential(tmp_path):
    """The file is there and readable; it just holds nothing to send."""
    config = Config(keytab=write_keytab(tmp_path, "# nothing but a comment\n"))
    assert SSSCredential.available(Offer("sss"), config, username="", host="h") is None


def test_the_credential_a_live_key_hands_over_is_the_sss_blob(tmp_path):
    """``initial`` is what the ladder calls; it must produce the wire form."""
    blob = SSSCredential(KEY, "bob").initial()
    assert blob[:4] == b"sss\x00"
    plain = Blowfish(KEY.secret).decrypt_cfb64(bytes(8), blob[16:])
    assert plain[43:47] == b"bob\x00"


def test_an_unparseable_keytab_is_skipped_not_raised(tmp_path):
    config = Config(keytab=write_keytab(tmp_path, "0 N:1 k:nothex\n"))
    assert SSSCredential.available(Offer("sss"), config, username="", host="h") is None


# --------------------------------------------------------------------------
# The ladder
# --------------------------------------------------------------------------


def test_registry_holds_the_zero_dependency_mechanisms():
    assert {"unix", "host", "sss", "ztn"} <= set(auth.registry())


def test_registry_returns_a_copy():
    auth.registry()["bogus"] = object
    assert "bogus" not in auth.registry()


def test_select_follows_the_configured_preference(monkeypatch):
    monkeypatch.delenv("BEARER_TOKEN", raising=False)
    config = Config(auth_order=("host", "unix"))
    names = [c.name for c in auth.select("&P=unix&P=host", config, username="bob")]
    assert names == ["host", "unix"]


def test_select_appends_offers_the_config_never_mentioned():
    config = Config(auth_order=("host",))
    names = [c.name for c in auth.select("&P=unix&P=host", config, username="bob")]
    assert names == ["host", "unix"]


def test_select_skips_mechanisms_this_client_cannot_do():
    rejected: dict[str, str] = {}
    names = [
        c.name
        for c in auth.select("&P=pwd&P=host", Config(), username="b", rejected=rejected)
    ]
    assert names == ["host"]
    assert "not supported" in rejected["pwd"]


def test_select_records_why_a_mechanism_had_no_material(tmp_path, monkeypatch):
    """The reason is the mechanism's own, so the error is worth reading: where
    it looked, and the command that would have put something there."""
    monkeypatch.delenv("BEARER_TOKEN", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr("os.getuid", lambda: 999999)
    rejected: dict[str, str] = {}
    list(auth.select("&P=ztn&P=host", Config(), username="b", rejected=rejected))
    assert rejected["ztn"].startswith("nothing in $BEARER_TOKEN, ")
    assert "/tmp/bt_u999999; try: oidc-token" in rejected["ztn"]


def test_a_mechanism_that_raises_does_not_mask_the_rest(monkeypatch):
    def boom(cls, offer, config, *, username, host):
        raise RuntimeError("keyring on fire")

    monkeypatch.setattr(UnixCredential, "available", classmethod(boom))
    rejected: dict[str, str] = {}
    names = [c.name for c in auth.select("&P=unix&P=host", Config(), rejected=rejected)]
    assert names == ["host"]
    assert rejected["unix"] == "RuntimeError: keyring on fire"


def test_select_accepts_offers_as_well_as_a_trailer():
    names = [c.name for c in auth.select([Offer("host")], Config())]
    assert names == ["host"]


def test_a_bearer_token_is_withheld_from_a_cleartext_connection(monkeypatch):
    """Stock behaviour: ztn over cleartext is a replayable credential, so the
    rung is skipped and the rejection says which scheme fixes it."""
    monkeypatch.setenv("BEARER_TOKEN", "t0ken")
    rejected: dict[str, str] = {}
    names = [
        c.name
        for c in auth.select("&P=ztn&P=host", Config(), username="b", rejected=rejected, tls=False)
    ]
    assert names == ["host"]
    assert "cleartext" in rejected["ztn"] and "roots://" in rejected["ztn"]


def test_a_bearer_token_travels_when_the_connection_is_encrypted(monkeypatch):
    monkeypatch.setenv("BEARER_TOKEN", "t0ken")
    names = [c.name for c in auth.select("&P=ztn", Config(), username="b", tls=True)]
    assert names == ["ztn"]


def test_ztn_cleartext_opts_back_in(monkeypatch):
    monkeypatch.setenv("BEARER_TOKEN", "t0ken")
    config = Config(ztn_cleartext=True)
    names = [c.name for c in auth.select("&P=ztn", config, username="b", tls=False)]
    assert names == ["ztn"]


def test_a_caller_who_cannot_say_withholds_nothing(monkeypatch):
    monkeypatch.setenv("BEARER_TOKEN", "t0ken")
    names = [c.name for c in auth.select("&P=ztn", Config(), username="b")]
    assert names == ["ztn"]


def test_select_is_lazy():
    """Nothing is built until the caller asks for it."""
    calls = []

    class Counting(UnixCredential):
        name = "unix"

        @classmethod
        def available(cls, offer, config, *, username, host):
            calls.append(offer.name)
            return cls(username or "u")

    auth.register(Counting)
    try:
        gen = auth.select("&P=host&P=unix", Config(auth_order=("host", "unix")))
        assert calls == []
        next(gen)
        assert calls == []
    finally:
        auth.register(UnixCredential)


def test_require_raises_when_nothing_fits(tmp_path):
    # An explicit proxy path, because this machine may well have a real one.
    config = Config(proxy=str(tmp_path / "x509up"), prompt=False)
    with pytest.raises(NoMechanismError) as exc:
        auth.require("&P=pwd&P=gsi", config)
    assert exc.value.offered == ["pwd", "gsi"]
    assert "pwd" in exc.value.tried


def test_require_returns_every_usable_mechanism():
    creds = auth.require("&P=unix&P=host", Config(), username="bob")
    assert [c.name for c in creds] == ["unix", "host"]


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, leak",
    [
        ("GET /f?authz=Bearer%20abc123", "abc123"),
        ("Authorization: Bearer eyJhbGciOiJub25lIn0.e30.sig", "e30"),
        ("token=eyJhbGciOiJub25lIn0.e30.sig", "e30"),
        ("password: hunter2", "hunter2"),
        ("secret = s3cr3t", "s3cr3t"),
        ("keytab: /etc/xrd/sss.keytab", "/etc/xrd/sss.keytab"),
    ],
)
def test_redact_removes_credential_material(text, leak):
    cleaned = redact(text)
    assert leak not in cleaned
    assert "<redacted>" in cleaned


def test_redact_leaves_ordinary_text_alone():
    assert redact("opened root://host:1094//store/data.root") == (
        "opened root://host:1094//store/data.root"
    )


def test_a_logged_credential_never_reaches_a_handler(caplog):
    """The filter is on the logger, so caplog sees the redacted record."""
    log = auth._log
    with caplog.at_level("DEBUG", logger="xrd.auth"):
        log.debug("sending %s", "token=eyJhbGciOiJub25lIn0.e30.sig")
    assert "e30" not in caplog.text
    assert "<redacted>" in caplog.text


def test_no_credential_reaches_a_traceback():
    """A stack trace is the one thing everybody pastes into a bug report."""
    secret = "s3cr3t-material"
    token = jwt(int(time.time()) - 10).replace("sig", secret)
    cases = [
        lambda: TokenCredential(token).initial(),
        lambda: SSSCredential(SSSKey(id=1, secret=secret.encode(), expires=1), "bob").initial(),
    ]
    for call in cases:
        try:
            call()
        except Exception as exc:
            # the three-argument form: 3.10 added the shorter one
            text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            assert secret not in text, text
        else:
            raise AssertionError("expected the credential to be refused")


def test_a_secret_split_across_the_format_string_is_still_caught(caplog):
    """Neither half looks like a credential; the joined message does."""
    with caplog.at_level("DEBUG", logger="xrd.auth"):
        auth._log.debug("%s=%s", "password", "hunter2")
    assert "hunter2" not in caplog.text
    assert "<redacted>" in caplog.text


def test_redaction_does_not_eat_a_placeholder(caplog):
    """``"keytab: %s"`` is the shape of a secret assignment, and is not one."""
    with caplog.at_level("DEBUG", logger="xrd.auth"):
        auth._log.debug("keytab: %s", "/etc/xrd/sss.keytab")
    assert "<redacted>" in caplog.text  # the path still goes, as it should
    assert "not all arguments converted" not in caplog.text


def test_a_broken_format_string_is_still_passed_along():
    """Redaction cannot read the message, and a logging bug is not ours to eat."""
    from xrd._log import _filter

    record = logging.LogRecord("xrd.auth", logging.DEBUG, __file__, 1, "counted %d", ("x",), None)
    assert _filter.filter(record) is True
    assert record.msg == "counted %d" and record.args == ("x",)


def test_the_unix_credential_prints_the_name_it_will_send():
    from xrd.auth.simple import UnixCredential

    assert repr(UnixCredential("tester")) == "UnixCredential(username='tester')"


def test_a_credential_with_nothing_to_show_prints_its_mechanism():
    """``host`` carries no material, so it falls back to the base repr."""
    assert repr(HostCredential()) == "HostCredential(name='host')"


def test_a_clean_message_with_no_arguments_is_left_exactly_as_it_was():
    """The filter only rewrites a record it had a reason to touch."""
    from xrd._log import _filter

    record = logging.LogRecord("xrd.auth", logging.DEBUG, __file__, 1, "connected", None, None)
    assert _filter.filter(record) is True
    assert record.msg == "connected" and record.args is None
