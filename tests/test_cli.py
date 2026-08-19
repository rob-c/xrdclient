"""The command-line tools: ``xrd-fs`` and ``xrd-cp``."""

from __future__ import annotations

import argparse
import io
import json

import pytest

from xrd import cli
from xrd.cli import Endpoints, config_from, dumps, size_arg
from xrd.cli import cp as cp_cli
from xrd.cli import fs as fs_cli
from xrd.errors import XRootDError
from xrd.testing import FakeDAVServer, FakeS3Server, FakeServer
from xrd.types import ChecksumInfo, StatInfo
from xrd.url import parse

BODY = b"hello world"


@pytest.fixture
def url(server):
    """The ``root://`` fixture server as a URL string, with a trailing slash."""
    return str(server.url)


@pytest.fixture
def dav():
    with FakeDAVServer(files={"/d/a.root": BODY}) as running:
        yield running


def run(argv, capsys):
    """Run ``xrd-fs`` and hand back ``(exit code, stdout, stderr)``."""
    code = fs_cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def test_one_endpoint_is_opened_once_however_many_paths_name_it(url):
    with Endpoints() as endpoints:
        first, path = endpoints.at(url + "data/a.root")
        second, other = endpoints.at(url + "data")
        assert first is second
        assert (path, other) == ("/data/a.root", "/data")


def test_a_local_path_is_not_an_endpoint(tmp_path):
    with Endpoints() as endpoints, pytest.raises(ValueError, match="local path"):
        endpoints.at(str(tmp_path))


@pytest.mark.parametrize(
    ("text", "expected"), [("4096", 4096), ("8k", 8192), ("2M", 2 << 20), ("1G", 1 << 30)]
)
def test_a_size_is_written_the_way_people_write_it(text, expected):
    assert size_arg(text) == expected


@pytest.mark.parametrize("text", ["", "0", "-1", "eight", "8Q"])
def test_a_size_that_is_not_one_is_a_usage_error(text):
    with pytest.raises(argparse.ArgumentTypeError):
        size_arg(text)


def test_json_output_covers_the_types_the_library_returns():
    payload = json.loads(dumps({"stat": StatInfo(st_size=3), "cks": ChecksumInfo("md5", "ab")}))
    assert payload["stat"]["st_size"] == 3
    assert payload["stat"]["flags"] == 0
    assert payload["cks"] == {"algorithm": "md5", "value": "ab"}
    # A URL is a dataclass too; it must serialise as itself, not as its fields.
    assert json.loads(dumps({"u": parse("root://h//p")}))["u"] == "root://h:1094//p"
    with pytest.raises(TypeError, match="cannot serialise"):
        dumps({"nope": object()})


def test_the_command_line_carries_the_configuration():
    args = argparse.Namespace(
        token="t",
        user="me",
        no_verify_tls=True,
        verbose=0,
        prompt=False,
        no_prompt=True,
        config=None,
        alias=None,
    )
    config = config_from(args)
    assert (config.token, config.username, config.verify_tls) == ("t", "me", False)
    assert config.prompt is False


# ---------------------------------------------------------------------------
# xrd-fs: reading
# ---------------------------------------------------------------------------


def test_ls_lists_names(url, capsys):
    code, out, _ = run(["ls", url + "data"], capsys)
    assert code == 0
    assert out.split() == ["a.root", "empty"]


def test_ls_long_shows_mode_size_and_time(url, capsys):
    _code, out, _ = run(["ls", "-l", url + "data"], capsys)
    first = out.splitlines()[0].split()
    assert first[0].startswith("-rw")
    assert first[1] == str(len(BODY))
    assert first[-1] == "a.root"


def test_ls_recursive_labels_each_directory(url, capsys):
    _code, out, _ = run(["ls", "-R", url + "data"], capsys)
    assert "/data:" in out and "/data/empty:" in out


def test_ls_json_is_a_map_of_directory_to_entries(url, capsys):
    _code, out, _ = run(["ls", "--json", url + "data"], capsys)
    payload = json.loads(out)
    assert [e["name"] for e in payload["/data"]] == ["a.root", "empty"]
    assert payload["/data"][1]["dir"] is True


def test_stat_prints_what_the_server_knows(url, capsys):
    code, out, _ = run(["stat", url + "data/a.root"], capsys)
    assert code == 0
    assert f"Size:  {len(BODY)}" in out


def test_stat_json_gives_the_whole_record(url, capsys):
    _code, out, _ = run(["stat", "--json", url + "data/a.root", url + "data"], capsys)
    sizes = [entry["st_size"] for entry in json.loads(out)]
    assert sizes[0] == len(BODY)


def test_cat_writes_bytes_not_text(url, capsysbinary):
    code = fs_cli.main(["cat", url + "data/a.root"])
    assert (code, capsysbinary.readouterr().out) == (0, BODY)


def test_checksum_asks_the_server(url, capsys):
    code, out, _ = run(["checksum", url + "data/a.root"], capsys)
    assert code == 0
    assert out.split() == ["adler32", "1a0b045d"]


def test_checksum_json_keys_by_url(url, capsys):
    _code, out, _ = run(["checksum", "--json", "-a", "adler32", url + "data/a.root"], capsys)
    assert json.loads(out)[url + "data/a.root"]["value"] == "1a0b045d"


def test_df_reports_space(url, capsys):
    code, out, _ = run(["df", url], capsys)
    assert code == 0
    assert "Read/write nodes" in out
    _code, payload, _ = run(["df", "--json", url], capsys)
    assert "free_rw" in json.loads(payload)


def test_locate_names_the_servers(url, capsys):
    code, out, _ = run(["locate", url + "data/a.root"], capsys)
    assert code == 0
    assert out.strip()
    _code, payload, _ = run(["locate", "--json", url + "data/a.root"], capsys)
    assert json.loads(payload)[0]["address"]


def test_locate_can_ask_where_a_new_file_would_go(url, capsys):
    code, out, _ = run(["locate", "--create", url + "data/new.root"], capsys)
    assert code == 0
    assert out.strip()


def test_ping_times_the_round_trip(url, capsys):
    code, out, _ = run(["ping", url], capsys)
    assert code == 0
    assert "responded in" in out
    _code, payload, _ = run(["ping", "--json", url], capsys)
    assert json.loads(payload)["ms"] >= 0


def test_doctor_checks_the_machine_when_given_no_url(capsys):
    code, out, _ = run(["doctor"], capsys)
    assert code == 0
    assert out.startswith("ok  python")
    assert "auth:" in out


def test_doctor_walks_a_whole_url_and_says_the_path_is_there(url, capsys):
    code, out, _ = run(["doctor", url + "data/a.root"], capsys)
    assert code == 0
    assert "ok  connect" in out and "/data/a.root: 11 bytes" in out


def test_doctor_fails_the_exit_code_on_something_that_would_stop_a_transfer(url, capsys):
    code, out, _ = run(["doctor", url + "data/missing.root"], capsys)
    assert code == 1
    assert "!!  path" in out


def test_doctor_says_the_same_thing_as_data(url, capsys):
    _code, payload, _ = run(["doctor", "--json", url], capsys)
    report = json.loads(payload)
    assert report["ok"] is True
    assert report["url"] == url
    assert {"name", "state", "detail", "hint"} == set(report["checks"][0])


def test_doctor_can_be_asked_to_say_nothing_and_answer_with_its_exit_code(url, capsys):
    code, out, err = run(["doctor", "--quiet", url], capsys)
    assert (code, out, err) == (0, "", "")


def test_query_reads_server_configuration(server, url, capsys):
    server.config_values["version"] = "v5.6.0"
    code, out, _ = run(["query", url, "version"], capsys)
    assert code == 0
    assert out.strip() == "version v5.6.0"
    _code, payload, _ = run(["query", "--json", url, "version"], capsys)
    assert json.loads(payload) == {"version": "v5.6.0"}


# ---------------------------------------------------------------------------
# xrd-fs: writing
# ---------------------------------------------------------------------------


def test_mkdir_makes_parents_on_request(server, url, capsys):
    assert run(["mkdir", "-p", url + "data/x/y"], capsys)[0] == 0
    assert "/data/x/y" in server.dirs
    assert run(["mkdir", url + "data/x/y"], capsys)[0] == 1  # already there


def test_touch_then_rm(server, url, capsys):
    assert run(["touch", url + "data/t.txt"], capsys)[0] == 0
    assert server.contents("/data/t.txt") == b""
    assert run(["rm", url + "data/t.txt"], capsys)[0] == 0
    assert "/data/t.txt" not in server.files


def test_rm_reports_what_is_not_there_unless_forced(url, capsys):
    code, _out, err = run(["rm", url + "data/nope"], capsys)
    assert code == 1
    assert "xrd-fs:" in err
    assert run(["rm", "-f", url + "data/nope"], capsys)[0] == 0


def test_rm_recursive_takes_the_tree(server, url, capsys):
    server.add_file("/data/tree/deep/f.bin", b"x")
    assert run(["rm", "-r", url + "data/tree"], capsys)[0] == 0
    assert not [p for p in server.files if p.startswith("/data/tree")]


def test_rmdir_keeps_the_emptiness_rule(server, url, capsys):
    assert run(["rmdir", url + "data"], capsys)[0] == 1  # not empty
    assert run(["rmdir", url + "data/empty"], capsys)[0] == 0
    assert "/data/empty" not in server.dirs


def test_mv_renames_within_one_endpoint(server, url, capsys):
    assert run(["mv", url + "data/a.root", url + "data/b.root"], capsys)[0] == 0
    assert server.contents("/data/b.root") == BODY


def test_mv_between_endpoints_says_to_use_xrd_cp(url, capsys):
    with FakeServer() as other:
        code, _out, err = run(["mv", url + "data/a.root", str(other.url) + "b.root"], capsys)
    assert code == 1
    assert "xrd-cp" in err


def test_xattr_gets_sets_and_removes(url, capsys):
    assert run(["xattr", url + "data/a.root", "--set", "run=42"], capsys)[0] == 0
    _code, out, _ = run(["xattr", url + "data/a.root"], capsys)
    assert out.strip() == "run=42"
    _code, payload, _ = run(["xattr", "--json", url + "data/a.root"], capsys)
    assert json.loads(payload) == {"run": "42"}
    assert run(["xattr", url + "data/a.root", "--remove", "run"], capsys)[0] == 0
    assert run(["xattr", "--json", url + "data/a.root"], capsys)[1].strip() == "{}"


def test_xattr_recursive_lists_names_under_a_directory(url, capsys):
    assert run(["xattr", url + "data/a.root", "--set", "run=42"], capsys)[0] == 0
    _code, out, _ = run(["xattr", "-r", url + "data"], capsys)
    assert out.strip() == "a.root: run"
    _code, payload, _ = run(["xattr", "--json", "-r", url + "data"], capsys)
    assert json.loads(payload) == {"a.root": ["run"]}


# ---------------------------------------------------------------------------
# xrd-fs: failure modes
# ---------------------------------------------------------------------------


def test_a_missing_path_exits_one_and_says_why(url, capsys):
    code, _out, err = run(["stat", url + "data/nope"], capsys)
    assert code == 1
    assert "no such file" in err


def test_a_local_path_is_refused_with_a_useful_message(tmp_path, capsys):
    code, _out, err = run(["ls", str(tmp_path)], capsys)
    assert (code, "local path" in err) == (1, True)


def test_no_subcommand_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exit_info:
        fs_cli.main([])
    assert exit_info.value.code == 2


def test_webdav_urls_work_in_the_same_tool(dav, capsys):
    code, out, _ = run(["ls", str(dav.url) + "d"], capsys)
    assert (code, out.strip()) == (0, "a.root")
    assert run(["checksum", str(dav.url) + "d/a.root"], capsys)[1].split()[1] == "1a0b045d"


def test_what_webdav_cannot_do_is_reported_not_crashed(dav, capsys):
    code, _out, err = run(["df", str(dav.url)], capsys)
    assert code == 1
    assert "statvfs" in err


# ---------------------------------------------------------------------------
# xrd-cp
# ---------------------------------------------------------------------------


def test_a_download_names_its_target(url, tmp_path, capsys):
    target = tmp_path / "out.root"
    code = cp_cli.main([url + "data/a.root", str(target)])
    out = capsys.readouterr().out
    assert (code, target.read_bytes()) == (0, BODY)
    assert "11 bytes" in out


def test_a_trailing_slash_means_into_this_directory(url, tmp_path, capsys):
    code = cp_cli.main([url + "data/a.root", str(tmp_path) + "/"])
    capsys.readouterr()
    assert (code, (tmp_path / "a.root").read_bytes()) == (0, BODY)


def test_an_existing_directory_means_the_same(url, tmp_path, capsys):
    assert cp_cli.main(["-q", url + "data/a.root", str(tmp_path)]) == 0
    assert (tmp_path / "a.root").read_bytes() == BODY
    assert capsys.readouterr().out == ""


def test_several_sources_need_a_directory(server, url, tmp_path, capsys):
    server.add_file("/data/b.txt", b"second")
    assert cp_cli.main([url + "data/a.root", url + "data/b.txt", str(tmp_path)]) == 0
    capsys.readouterr()
    assert (tmp_path / "b.txt").read_bytes() == b"second"
    code = cp_cli.main([url + "data/a.root", url + "data/b.txt", str(tmp_path / "one.bin")])
    assert (code, "not a directory" in capsys.readouterr().err) == (2, True)


def test_recursive_copies_the_tree(server, url, tmp_path, capsys):
    server.add_file("/data/sub/deep.bin", b"deep")
    assert cp_cli.main(["-r", "-q", url + "data", str(tmp_path / "tree")]) == 0
    assert (tmp_path / "tree/sub/deep.bin").read_bytes() == b"deep"


def test_an_upload_goes_the_other_way(server, url, tmp_path, capsys):
    source = tmp_path / "up.bin"
    source.write_bytes(b"payload")
    assert cp_cli.main(["-q", str(source), url + "data/up.bin"]) == 0
    assert server.contents("/data/up.bin") == b"payload"


def test_no_clobber_refuses_an_existing_target(url, tmp_path, capsys):
    target = tmp_path / "out.root"
    target.write_bytes(b"keep")
    code = cp_cli.main(["-n", url + "data/a.root", str(target)])
    assert (code, target.read_bytes()) == (1, b"keep")
    assert "xrd-cp:" in capsys.readouterr().err


def test_json_reports_the_transfer(url, tmp_path, capsys):
    code = cp_cli.main(["--json", url + "data/a.root", str(tmp_path / "o.root")])
    record, = json.loads(capsys.readouterr().out)
    assert code == 0
    assert record["size"] == len(BODY)
    assert record["verified"] is True
    assert record["checksum"] == "adler32:1a0b045d"


def test_verification_can_be_demanded(url, tmp_path, capsys):
    argv = ["--verify", "-a", "adler32", "--json", url + "data/a.root", str(tmp_path / "a")]
    assert cp_cli.main(argv) == 0
    record, = json.loads(capsys.readouterr().out)
    assert record["checksum"] == "adler32:1a0b045d"


def test_verification_can_be_skipped(url, tmp_path, capsys):
    assert cp_cli.main(["--no-verify", "--json", url + "data/a.root", str(tmp_path / "b")]) == 0
    record, = json.loads(capsys.readouterr().out)
    assert (record["verified"], record["checksum"]) == (False, None)


def test_the_chunk_size_is_honoured(url, tmp_path, capsys):
    assert cp_cli.main(["-q", "--chunk-size", "4", url + "data/a.root", str(tmp_path / "c")]) == 0
    assert (tmp_path / "c").read_bytes() == BODY


def test_a_scheme_nobody_speaks_is_reported(tmp_path, capsys):
    code = cp_cli.main(["ftp://example.org/f", str(tmp_path / "x")])
    assert (code, "cannot read from ftp" in capsys.readouterr().err) == (1, True)


def test_third_party_is_one_flag(url, capsys):
    with FakeServer() as destination:
        code = cp_cli.main(["--tpc", "-q", url + "data/a.root", str(destination.url) + "pulled"])
        assert code == 0
        assert any("tpc.key" in path for path in destination.opened)


def test_third_party_between_webdav_endpoints_is_the_same_flag(dav, capsys):
    """One flag, either dialect: the URLs decide which one is spoken."""
    with FakeDAVServer(dirs=["/d"]) as destination:
        code = cp_cli.main(
            ["--tpc", "-q", str(dav.url) + "d/a.root", str(destination.url) + "d/pulled"]
        )
        assert code == 0
        assert destination.contents("/d/pulled") == BODY
        assert destination.copies[-1]["Source"].endswith("/d/a.root")


def test_a_copy_between_protocols_works_both_ways(url, dav, tmp_path, capsys):
    assert cp_cli.main(["-q", str(dav.url) + "d/a.root", url + "data/from-dav"]) == 0
    assert cp_cli.main(["-q", url + "data/a.root", str(dav.url) + "d/from-root"]) == 0
    capsys.readouterr()
    assert dav.contents("/d/from-root") == BODY


def test_a_bucket_is_a_url_like_any_other_at_the_command_line(url, monkeypatch, capsys):
    """No S3 flags: the credentials come from the environment, as they do for
    every other S3 tool, and the URL is what tells the CLI where to look."""
    with FakeS3Server(objects={"d/a.root": BODY}) as s3:
        monkeypatch.setenv("AWS_ENDPOINT_URL", s3.endpoint)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", s3.access_key)
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", s3.secret_key)
        code, out, _err = run(["ls", "-l", "s3://test-bucket/d"], capsys)
        assert (code, "a.root" in out) == (0, True)
        assert cp_cli.main(["-q", url + "data/a.root", "s3://test-bucket/d/from-root"]) == 0
        capsys.readouterr()
        assert s3.contents("d/from-root") == BODY


# ---------------------------------------------------------------------------
# The progress display
# ---------------------------------------------------------------------------


def test_the_bar_redraws_only_when_the_percentage_moves():
    stream = io.StringIO()
    bar = cp_cli.Bar("f.root", stream)
    for done in (0, 1, 2, 50, 100, 100):  # the last two say the same thing
        bar(done, 100)
    bar.finish()
    frames = [f for f in stream.getvalue().split("\r") if f]
    assert len(frames) == 5  # 0%, 1%, 2%, 50%, 100% - and 100% only once
    assert "100%" in frames[-1]
    assert frames[-1].endswith("\n")


def test_the_bar_copes_with_a_source_of_unknown_size():
    stream = io.StringIO()
    bar = cp_cli.Bar("stream", stream)
    bar(1024, None)
    bar.finish()
    assert "1.0 KiB" in stream.getvalue()


@pytest.mark.parametrize(
    ("size", "text"), [(0, "0 B"), (999, "999 B"), (1024, "1.0 KiB"), (5 << 20, "5.0 MiB")]
)
def test_byte_counts_are_human_readable(size, text):
    assert cp_cli._human(size) == text


def test_progress_is_off_when_stdout_is_not_a_terminal(url, tmp_path, capsys):
    """A pipe gets clean output; a tty gets a bar. Nothing else decides it."""
    assert cp_cli.main(["-p", "-q", url + "data/a.root", str(tmp_path / "p")]) == 0
    assert "%" in capsys.readouterr().err
    assert cp_cli.main(["-q", url + "data/a.root", str(tmp_path / "q")]) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(("verbosity", "level"), [(1, "WARNING"), (2, "INFO"), (3, "DEBUG")])
def test_verbosity_flags_choose_a_logging_level(verbosity, level, monkeypatch):
    import logging

    chosen: dict[str, object] = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: chosen.update(kw))
    cli.configure_logging(verbosity)
    assert logging.getLevelName(chosen["level"]) == level


def test_no_verbosity_flag_leaves_logging_alone(monkeypatch):
    import logging

    monkeypatch.setattr(
        logging, "basicConfig", lambda **kw: pytest.fail("logging was configured anyway")
    )
    cli.configure_logging(0)


def test_the_json_encoder_refuses_what_it_cannot_render():
    with pytest.raises(TypeError, match="cannot serialise"):
        cli.dumps({"handle": object()})


def test_the_json_encoder_renders_flags_as_numbers_and_bytes_as_text():
    from xrd.flags import OpenFlags

    assert cli.dumps({"flags": OpenFlags.READ}) == f'{{\n  "flags": {int(OpenFlags.READ)}\n}}'
    assert '"payload": "hi"' in cli.dumps({"payload": b"hi"})


def test_a_bar_that_never_drew_anything_leaves_the_terminal_alone(capsys):
    """No percentage was ever printed, so there is no line to close."""
    cp_cli.Bar("f.root").finish()
    assert capsys.readouterr().err == ""


def test_ping_can_be_asked_to_say_nothing(url, capsys):
    code, out, err = run(["ping", "--quiet", url], capsys)
    assert (code, out, err) == (0, "", "")


def test_a_trailing_slash_is_a_directory_without_asking(url):
    """``cp f d/`` means *into* ``d`` even before anyone has looked."""
    assert cp_cli._is_dir(parse(url + "data/"), Endpoints(cli.Config())) is True


def test_a_destination_the_server_will_not_discuss_is_not_a_directory():
    """``isdir`` failing is an answer: treat the name as a file to write."""

    class Grumpy:
        def isdir(self, _path):
            raise XRootDError("no")

    class Only:
        def at(self, _url):
            return Grumpy(), "/d"

    assert cp_cli._is_dir(parse("root://h//d"), Only()) is False


# ---------------------------------------------------------------------------
# xrd-fs: the rest of the namespace
# ---------------------------------------------------------------------------


@pytest.fixture
def lines(server):
    """A file of numbered lines, and the URL of the server holding it."""
    body = b"".join(b"line %d\n" % n for n in range(1, 21))
    server.add_file("/data/log.txt", body)
    return str(server.url)


def test_tail_prints_the_last_ten_lines_by_default(lines, capsys):
    code, out, _ = run(["tail", lines + "data/log.txt"], capsys)
    assert code == 0
    assert out.splitlines() == [f"line {n}" for n in range(11, 21)]


def test_tail_takes_a_line_count(lines, capsys):
    assert run(["tail", "-n", "2", lines + "data/log.txt"], capsys)[1].split() == [
        "line", "19", "line", "20",
    ]


def test_tail_reading_only_the_end_drops_the_partial_line_it_landed_in(lines, capsys):
    """A window that starts mid-line must not print half of one."""
    _code, out, _ = run(["tail", "--bytes", "20", "-n", "5", lines + "data/log.txt"], capsys)
    assert all(line.startswith("line ") for line in out.splitlines())


def test_tail_follow_prints_what_is_appended(server, capsys):
    server.add_file("/data/grow.txt", b"first\n")
    filesystem, path = Endpoints(cli.Config()).at(str(server.url) + "data/grow.txt")
    out = io.BytesIO()
    server.add_file("/data/grow.txt", b"first\nsecond\n")
    with filesystem:
        # Long enough to come round again after printing: the print itself is a
        # whole open-and-read, and a follow that stopped there would never have
        # been asked the only question that matters - "anything more?"
        fs_cli._follow(out, filesystem, path, 6, 0.01, deadline=_soon(2.0))
    assert out.getvalue() == b"second\n"


def test_tail_follow_stops_when_the_file_goes_away(server):
    filesystem, path = Endpoints(cli.Config()).at(str(server.url) + "data/a.root")
    del server.files["/data/a.root"]
    with filesystem:
        fs_cli._follow(io.BytesIO(), filesystem, path, 0, 0.01, deadline=_soon(10.0))


def test_tail_follow_stops_when_the_file_shrinks(server):
    """A shorter file is a different file; the next byte would not follow on."""
    filesystem, path = Endpoints(cli.Config()).at(str(server.url) + "data/a.root")
    with filesystem:
        fs_cli._follow(io.BytesIO(), filesystem, path, 99, 0.01, deadline=_soon(10.0))


def test_tail_follow_from_the_command_line_stops_on_an_interrupt(server, capsys, monkeypatch):
    """Ctrl-C ends ``tail -f`` quietly - that is what a user presses to stop it."""

    def interrupted(out, filesystem, path, size, interval, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(fs_cli, "_follow", interrupted)
    argv = ["tail", "-f", "--interval", "0.01", str(server.url) + "data/a.root"]
    code, out, _ = run(argv, capsys)
    assert (code, out) == (0, BODY.decode())


def _soon(seconds: float = 0.05) -> float:
    import time

    return time.monotonic() + seconds


def test_du_totals_a_tree(url, capsys):
    code, out, _ = run(["du", url + "data"], capsys)
    fields = out.split()
    assert (code, fields[0], fields[1]) == (0, str(len(BODY)), "1")


def test_du_of_a_single_file_counts_one(url, capsys):
    _code, payload, _ = run(["du", "--json", url + "data/a.root"], capsys)
    assert list(json.loads(payload).values()) == [{"bytes": len(BODY), "files": 1}]


def test_du_adds_up_what_the_listing_does_not_carry(server, capsys):
    """A server that lists without sizes still gets counted, one stat each."""
    from xrd.proto import constants as c

    server.add_file("/data/sub/x.bin", b"12345")

    def bare(conn, sid, params, body):
        yield from _plain_listing(conn, sid, params, body)

    server.handlers[c.kXR_dirlist] = bare
    _code, out, _ = run(["du", str(server.url) + "data"], capsys)
    assert out.split()[0] == str(len(BODY) + 5)


def _plain_listing(conn, sid, params, body):
    """A ``kXR_dirlist`` reply with names only - no stat lines attached."""
    from xrd.proto import constants as c
    from xrd.testing import frame

    path = body.split(b"\x00", 1)[0].decode().partition("?")[0].rstrip("/") or "/"
    names = conn._children(path)
    yield frame(sid, c.kXR_ok, "\n".join(names).encode() + b"\x00")


def test_chmod_changes_the_mode(url, server, capsys):
    assert run(["chmod", "640", url + "data/a.root"], capsys)[0] == 0
    assert server.modes["/data/a.root"] == 0o640


def test_chown_takes_uid_gid_or_either_alone(url, server, capsys):
    assert run(["chown", "1000:1000", url + "data/a.root"], capsys)[0] == 0
    assert server.owners["/data/a.root"] == (1000, 1000)
    assert run(["chown", ":42", url + "data/a.root"], capsys)[0] == 0
    assert server.owners["/data/a.root"] == (-1, 42)
    assert run(["chown", "7", url + "data/a.root"], capsys)[0] == 0
    assert server.owners["/data/a.root"] == (7, -1)


def test_chown_rejects_a_name_rather_than_guessing_an_id(url, capsys):
    """The names live in the server's passwd file, not this machine's."""
    with pytest.raises(SystemExit):
        run(["chown", "alice", url + "data/a.root"], capsys)


def test_touch_can_set_the_times_as_well_as_create(url, server, capsys):
    assert run(["touch", "--time", "1000000000", url + "data/new.root"], capsys)[0] == 0
    assert server.times["/data/new.root"] == (10**18, 10**18)
    assert run(["touch", "--time", "now", url + "data/a.root"], capsys)[0] == 0
    assert server.times["/data/a.root"][1] > 10**18


def test_touch_without_a_time_only_creates(url, server, capsys):
    assert run(["touch", url + "data/plain.root"], capsys)[0] == 0
    assert server.contents("/data/plain.root") == b""
    assert "/data/plain.root" not in server.times


def test_truncate_resizes_without_opening(url, server, capsys):
    assert run(["truncate", "-s", "4", url + "data/a.root"], capsys)[0] == 0
    assert server.contents("/data/a.root") == b"hell"


def test_prepare_asks_for_a_stage_and_prints_the_handle(url, capsys):
    code, out, _ = run(["prepare", url + "data/a.root"], capsys)
    assert (code, bool(out.strip())) == (0, True)


def test_prepare_can_evict_instead(url, server, capsys):
    from xrd.proto import constants as c

    code, out, _ = run(["prepare", "--evict", "--priority", "2", url + "data/a.root"], capsys)
    assert (code, out) == (0, "")
    assert c.kXR_prepare in server.seen


def test_prepare_status_reports_instead_of_staging(url, capsys):
    handle = json.loads(run(["prepare", "--json", url + "data/a.root"], capsys)[1])[0]
    code, out, _ = run(["prepare", "--status", handle, url + "data/a.root"], capsys)
    assert (code, out.strip()) == (0, "/data/a.root: online")


def test_prepare_status_as_json_carries_every_flag(url, capsys):
    handle = json.loads(run(["prepare", "--json", url + "data/a.root"], capsys)[1])[0]
    code, out, _ = run(["prepare", "--json", "--status", handle, url + "data/a.root"], capsys)
    record = json.loads(out)[0]
    assert code == 0
    assert (record["path"], record["online"], record["on_tape"]) == ("/data/a.root", True, False)


def test_prepare_status_says_nothing_when_asked_to_be_quiet(url, capsys):
    handle = json.loads(run(["prepare", "--json", url + "data/a.root"], capsys)[1])[0]
    assert run(["prepare", "-q", "--status", handle, url + "data/a.root"], capsys)[1] == ""


def test_prepare_groups_paths_by_endpoint(url, capsys):
    with FakeServer(files={"/other/b.bin": b"b"}) as second:
        argv = ["prepare", "--json", url + "data/a.root", str(second.url) + "other/b.bin"]
        code, out, _ = run(argv, capsys)
    assert (code, len(json.loads(out))) == (0, 2)


def test_locality_says_where_a_file_is_without_staging_it(url, server, capsys):
    server.nearline.add("/data/a.root")
    code, out, _ = run(["locality", url + "data/a.root"], capsys)
    assert (code, out.strip()) == (0, "/data/a.root: on tape")


def test_locality_as_json_carries_the_state_word(url, capsys):
    code, out, _ = run(["locality", "--json", url + "data/a.root"], capsys)
    record = json.loads(out)[0]
    assert code == 0
    assert (record["path"], record["state"], record["online"]) == ("/data/a.root", "ONLINE", True)


def test_locality_says_nothing_when_asked_to_be_quiet(url, capsys):
    assert run(["locality", "-q", url + "data/a.root"], capsys)[1] == ""


def test_ln_makes_a_symbolic_link_and_readlink_reads_it_back(url, capsys):
    assert run(["ln", "-s", url + "data/a.root", url + "data/soft"], capsys)[0] == 0
    _code, out, _ = run(["readlink", url + "data/soft"], capsys)
    assert out.strip() == "/data/a.root"
    _code, payload, _ = run(["readlink", "--json", url + "data/soft"], capsys)
    assert list(json.loads(payload).values()) == ["/data/a.root"]


def test_ln_without_s_makes_a_hard_link(url, server, capsys):
    assert run(["ln", url + "data/a.root", url + "data/hard"], capsys)[0] == 0
    assert server.contents("/data/hard") == BODY


def test_a_link_cannot_cross_endpoints(url, capsys):
    with FakeServer() as second:
        code, _out, err = run(["ln", url + "data/a.root", str(second.url) + "elsewhere"], capsys)
    assert (code, "one endpoint" in err) == (1, True)


def test_what_webdav_has_no_link_for_is_reported(dav, capsys):
    code, _out, err = run(["readlink", str(dav.url) + "d/a.root"], capsys)
    assert (code, "readlink" in err) == (1, True)


# ---------------------------------------------------------------------------
# xrd-fs: the settings file
# ---------------------------------------------------------------------------


def test_a_named_settings_file_is_read(url, tmp_path, capsys):
    config = tmp_path / "settings.ini"
    config.write_text("[defaults]\nusername = fromfile\n")
    args = fs_cli._parser().parse_args(["ls", "--config", str(config), url])
    assert config_from(args).username == "fromfile"


def test_an_alias_selects_a_section(url, tmp_path):
    config = tmp_path / "settings.ini"
    config.write_text("[defaults]\nusername = plain\n[alias eos]\nusername = special\n")
    argv = ["ls", "--config", str(config), "--alias", "eos", url]
    assert config_from(fs_cli._parser().parse_args(argv)).username == "special"


def test_a_flag_beats_the_settings_file(url, tmp_path):
    config = tmp_path / "settings.ini"
    config.write_text("[defaults]\nusername = fromfile\n")
    argv = ["ls", "--config", str(config), "--user", "fromflag", url]
    assert config_from(fs_cli._parser().parse_args(argv)).username == "fromflag"


# ---------------------------------------------------------------------------
# xrd-cp: selection and synchronisation
# ---------------------------------------------------------------------------


@pytest.fixture
def local_tree(tmp_path):
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "keep.root").write_bytes(b"keep")
    (root / "skip.log").write_bytes(b"skip")
    (root / "sub" / "deep.root").write_bytes(b"deep")
    return root


def test_a_dry_run_says_what_it_would_do_and_does_nothing(local_tree, url, server, capsys):
    code = cp_cli.main(["-r", "--dry-run", str(local_tree), url + "dry"])
    out = capsys.readouterr().out
    assert (code, out.count("->")) == (0, 3)
    assert not any(path.startswith("/dry") for path in server.files)


def test_exclude_and_include_choose_what_travels(local_tree, url, server, capsys):
    assert cp_cli.main(["-r", "-q", "--exclude", "*.log", str(local_tree), url + "sel"]) == 0
    assert sorted(p for p in server.files if p.startswith("/sel")) == [
        "/sel/keep.root",
        "/sel/sub/deep.root",
    ]


def test_sync_skips_what_is_already_there(local_tree, url, capsys):
    """The trailing slash pins the target, so the second run has nothing to do."""
    assert cp_cli.main(["-r", "-q", str(local_tree), url + "syn/"]) == 0
    code = cp_cli.main(["-r", "--sync", "size", str(local_tree), url + "syn/"])
    assert (code, capsys.readouterr().out) == (0, "")


def test_delete_prunes_the_target(local_tree, url, server, capsys):
    assert cp_cli.main(["-r", "-q", str(local_tree), url + "del/"]) == 0
    (local_tree / "skip.log").unlink()
    assert cp_cli.main(["-r", "-q", "-f", "--delete", str(local_tree), url + "del/"]) == 0
    assert "/del/tree/skip.log" not in server.files
    assert "/del/tree/keep.root" in server.files


def test_remove_source_moves_the_file(url, tmp_path, server, capsys):
    source = tmp_path / "moving.bin"
    source.write_bytes(b"payload")
    assert cp_cli.main(["-q", "--remove-source", str(source), url + "moved.bin"]) == 0
    assert server.contents("/moved.bin") == b"payload"
    assert not source.exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["--exclude", "*.log"],
        ["--include", "*.root"],
        ["--sync", "size"],
        ["--delete"],
        ["--parallel", "4"],
    ],
)
def test_the_tree_flags_need_a_tree(url, tmp_path, capsys, argv):
    code = cp_cli.main([*argv, url + "data/a.root", str(tmp_path / "x")])
    assert (code, "add -r" in capsys.readouterr().err) == (2, True)


def test_parallel_copies_several_files_of_the_tree_at_once(local_tree, url, server, capsys):
    assert cp_cli.main(["-r", "-q", "--parallel", "3", str(local_tree), url + "par/"]) == 0
    assert sorted(p for p in server.files if p.startswith("/par")) == [
        "/par/tree/keep.root",
        "/par/tree/skip.log",
        "/par/tree/sub/deep.root",
    ]


def test_parallel_is_a_count_of_files_so_zero_is_a_usage_error(local_tree, url, capsys):
    code = cp_cli.main(["-r", "--parallel", "0", str(local_tree), url + "no/"])
    assert (code, "at least one" in capsys.readouterr().err) == (2, True)


def test_the_in_flight_window_reaches_the_copy(url, tmp_path, capsys, monkeypatch):
    seen = []
    real = cp_cli.copy

    def spy(*args, config, **options):
        seen.append(config.in_flight)
        return real(*args, config=config, **options)

    monkeypatch.setattr(cp_cli, "copy", spy)
    target = tmp_path / "f"
    assert cp_cli.main(["--in-flight", "5", "-q", url + "data/a.root", str(target)]) == 0
    assert (seen, target.read_bytes()) == ([5], BODY)


def test_the_window_is_a_count_of_chunks_so_zero_is_a_usage_error(url, tmp_path, capsys):
    code = cp_cli.main(["--in-flight", "0", url + "data/a.root", str(tmp_path / "f")])
    assert (code, "at least one" in capsys.readouterr().err) == (2, True)


def test_the_stripe_count_reaches_the_copy(url, tmp_path, capsys, monkeypatch):
    """The engine has always been able to move one file over several
    connections; this is the flag that asks it to."""
    seen = []
    real = cp_cli.copy

    def spy(*args, config, **options):
        seen.append(config.parallel_chunks)
        return real(*args, config=config, **options)

    monkeypatch.setattr(cp_cli, "copy", spy)
    target = tmp_path / "f"
    assert cp_cli.main(["--stripes", "3", "-q", url + "data/a.root", str(target)]) == 0
    assert (seen, target.read_bytes()) == ([3], BODY)


def test_stripes_are_a_count_of_connections_so_zero_is_a_usage_error(url, tmp_path, capsys):
    code = cp_cli.main(["--stripes", "0", url + "data/a.root", str(tmp_path / "f")])
    assert (code, "at least one" in capsys.readouterr().err) == (2, True)


def test_the_stream_count_reaches_the_copy(url, tmp_path, capsys, monkeypatch):
    """Stripes and streams are different questions: one is how many spans of
    the file move at once, the other how many connections each one rides."""
    seen = []
    real = cp_cli.copy

    def spy(*args, config, **options):
        seen.append(config.data_streams)
        return real(*args, config=config, **options)

    monkeypatch.setattr(cp_cli, "copy", spy)
    target = tmp_path / "f"
    assert cp_cli.main(["--streams", "3", "-q", url + "data/a.root", str(target)]) == 0
    assert (seen, target.read_bytes()) == ([3], BODY)


def test_no_extra_streams_is_a_request_rather_than_a_mistake(url, tmp_path, monkeypatch):
    """Unlike the counts above, nought means something here - the control link
    on its own - so it has to reach the copy instead of being refused."""
    seen = []
    real = cp_cli.copy

    def spy(*args, config, **options):
        seen.append(config.data_streams)
        return real(*args, config=config, **options)

    monkeypatch.setattr(cp_cli, "copy", spy)
    target = tmp_path / "f"
    assert cp_cli.main(["--streams", "0", "-q", url + "data/a.root", str(target)]) == 0
    assert (seen, target.read_bytes()) == ([0], BODY)


def test_a_negative_stream_count_is_a_usage_error(url, tmp_path, capsys):
    code = cp_cli.main(["--streams", "-1", url + "data/a.root", str(tmp_path / "f")])
    assert (code, "not negative" in capsys.readouterr().err) == (2, True)


def test_continue_carries_on_from_a_partial_download(url, tmp_path, capsys):
    target = tmp_path / "partial.root"
    target.write_bytes(b"hello ")
    assert cp_cli.main(["-c", "--json", url + "data/a.root", str(target)]) == 0
    assert target.read_bytes() == b"hello world"
    record = json.loads(capsys.readouterr().out)[0]
    assert (record["resumed_at"], record["size"]) == (6, 5)


@pytest.mark.parametrize("flag", ["--tpc", "-n"])
def test_continue_needs_a_target_it_is_allowed_to_extend(url, tmp_path, capsys, flag):
    code = cp_cli.main(["-c", flag, url + "data/a.root", str(tmp_path / "x")])
    assert (code, "--continue" in capsys.readouterr().err) == (2, True)


@pytest.mark.parametrize("flag", ["--dry-run", "--remove-source"])
def test_a_third_party_copy_cannot_pretend(url, tmp_path, capsys, flag):
    code = cp_cli.main(["--tpc", flag, url + "data/a.root", url + "b.root"])
    assert (code, "--tpc" in capsys.readouterr().err) == (2, True)
