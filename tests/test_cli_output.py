"""The report as xrdclient's commands write it: every command, both formats.

The document format, capture and exit-code policy are :mod:`xrdcore.report`
since 0.4.0 and are tested there; this file tests what xrdclient brings - its
commands, its URL type, its protocol error codes.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
from typing import Any

import pytest

from xrdclient import report as output
from xrdclient.cli import cp, fs
from xrdclient.errors import ServerError
from xrdclient.testing import FakeServer
from xrdclient.types import PrepareStatus
from xrdclient.url import parse


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_an_xrootd_url_is_reported_as_its_string(fmt):
    """The one thing xrdclient adds to the shared report: its URL type.

    The document itself is :mod:`xrdcore.report` and is tested there; an
    :class:`~xrdclient.url.XRootDURL` is a dataclass, and without the
    registration :mod:`xrdclient.report` makes it would be written field by
    field.
    """
    report = output.Report("test", fmt)
    report.record("result", value=[parse("root://h//p")], url=parse("roots://h//q"))
    destination = io.StringIO()
    report.finish(0, destination)
    row = output.parse(destination.getvalue(), fmt)["records"][0]
    assert row["value"] == ["root://h:1094//p"]
    assert row["url"] == "roots://h:1094//q"
    assert json.loads(output.dumps({"u": parse("root://h//p")})) == {"u": "root://h:1094//p"}


@pytest.mark.parametrize("fmt", ["json", "xml"])
@pytest.mark.parametrize(
    "entry, args",
    [
        (fs.main, ["--help"]),
        (fs.main, ["--version"]),
        (fs.main, ["no-such-command"]),
        (cp.main, ["--help"]),
        (cp.main, ["--version"]),
        (cp.main, []),
    ],
)
def test_help_version_and_usage_are_reports(fmt, entry, args, capsys):
    code = entry(["--output-format", fmt, *args])
    captured = capsys.readouterr()
    report = output.parse(captured.out, fmt)
    assert captured.err == "" and report["summary"]["exit_code"] == code
    assert code == (2 if args in ([], ["no-such-command"]) else 0)
    assert report["records"]


@pytest.mark.parametrize("fmt", ["json", "xml"])
@pytest.mark.parametrize(
    "command",
    [
        "ls",
        "stat",
        "cat",
        "tail",
        "du",
        "checksum",
        "df",
        "locate",
        "ping",
        "query",
        "xattr",
        "locality",
        "prepare",
    ],
)
def test_namespace_read_reports(fmt, command, server, capsys):
    url = str(server.url) + "data/a.root"
    args = [command, "--output-format", fmt, url]
    if command == "ls":
        args[-1] = str(server.url) + "data"
    if command == "query":
        args.append("readv_iov_max")
    code = fs.main(args)
    captured = capsys.readouterr()
    report = output.parse(captured.out, fmt)
    assert captured.err == "" and report["summary"]["exit_code"] == code
    assert report["records"]
    if command in ("cat", "tail"):
        content = [row for row in report["records"] if row["kind"] == "content"]
        _assert_content(content)


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_completed_batch_records_survive_later_error(fmt, server, capsys):
    first = str(server.url) + "data/a.root"
    absent = str(server.url) + "missing"
    assert fs.main(["stat", "--output-format", fmt, first, absent]) == 1
    report = output.parse(capsys.readouterr().out, fmt)
    assert report["records"][0]["url"] == first
    assert report["records"][0]["status"] == "succeeded"
    failure = next(row for row in report["records"] if row["kind"] == "error")
    assert failure["url"] == absent and failure["code"] == 3011
    assert not report["summary"]["ok"]


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_copy_and_progress_reports(fmt, tmp_path, capsys):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"abc\0\xff")
    assert cp.main(["--output-format", fmt, str(source), str(target)]) == 0
    report = output.parse(capsys.readouterr().out, fmt)
    assert target.read_bytes() == source.read_bytes()
    result = next(row for row in report["records"] if row["kind"] == "result")
    assert result["value"]["size"] == 5 and result["status"] == "succeeded"
    assert any(row["kind"] == "progress" for row in report["records"])


def test_error_uses_numeric_protocol_code(capsys):
    def action():
        output.error(ServerError(3010, "access denied"))
        return 1

    assert output.run_cli("test", ["--json"], action) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["records"][0]["code"] == 3010


def _assert_content(content: list[dict[str, Any]]) -> None:
    assert b"".join(base64.b64decode(row["value"]["data"]) for row in content) == b"hello world"


FS_COMMANDS = sorted(
    next(
        action.choices
        for action in fs._parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
)


@pytest.mark.parametrize("fmt", ["json", "xml"])
@pytest.mark.parametrize("command", FS_COMMANDS)
def test_all_namespace_commands_have_structured_help(fmt, command, capsys):
    assert fs.main([command, "--output-format", fmt, "--help"]) == 0
    report = output.parse(capsys.readouterr().out, fmt)
    assert report["summary"]["ok"] and report["records"]


@pytest.mark.parametrize("fmt", ["json", "xml"])
@pytest.mark.parametrize(
    "command, extras, path",
    [
        ("mkdir", [], "data/new"),
        ("chmod", ["640"], "data/a.root"),
        ("truncate", ["-s", "4"], "data/a.root"),
        ("rm", [], "data/a.root"),
        ("rmdir", [], "data/empty"),
        ("touch", [], "data/new"),
        ("chown", ["1000:1000"], "data/a.root"),
        ("prepare", ["--evict"], "data/a.root"),
        ("xattr", ["--set", "user.a=<&>"], "data/a.root"),
    ],
)
def test_namespace_mutation_reports(fmt, command, extras, path, server, capsys):
    url = str(server.url) + path
    assert fs.main([command, "--output-format", fmt, *extras, url]) == 0
    report = output.parse(capsys.readouterr().out, fmt)
    row = next(row for row in report["records"] if row["kind"] == "result")
    assert row["status"] == "succeeded"


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_links_rename_attributes_and_prepare_status(fmt, server, capsys):
    first, second = str(server.url) + "data/a.root", str(server.url) + "data/link"
    for command, args in [
        ("ln", ["-s", first, second]),
        ("readlink", [second]),
        ("xattr", [first, "--set", "user.a=x"]),
        ("xattr", [first, "--remove", "user.a"]),
        ("mv", [first, str(server.url) + "data/moved"]),
    ]:
        assert fs.main([command, "--output-format", fmt, *args]) == 0
        assert output.parse(capsys.readouterr().out, fmt)["summary"]["ok"]
    _prepare_status_report(fmt, str(server.url) + "data/moved", capsys)


def _prepare_status_report(fmt: str, first: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert fs.main(["prepare", "--output-format", fmt, first]) == 0
    report = output.parse(capsys.readouterr().out, fmt)
    handle = next(row["handle"] for row in report["records"] if row["kind"] == "result")
    assert fs.main(["prepare", "--output-format", fmt, "--status", handle, first]) == 0
    report = output.parse(capsys.readouterr().out, fmt)
    row = next(row for row in report["records"] if row["kind"] == "result")
    assert row["request_id"] == handle and row["value"]["path"] == parse(first).path


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_same_directory_path_on_different_endpoints_keeps_identity(fmt, server, capsys):
    with FakeServer(files={"/data/b.root": b"b"}) as other:
        urls = [str(server.url) + "data", str(other.url) + "data"]
        assert fs.main(["ls", "--output-format", fmt, *urls]) == 0
        report = output.parse(capsys.readouterr().out, fmt)
    rows = [row for row in report["records"] if row["kind"] == "result"]
    assert [row["url"] for row in rows] == urls
    assert [row["entries"][0]["name"] for row in rows] == ["a.root", "b.root"]


def test_reused_exception_is_reported_for_each_file():
    report = output.Report("test", "json")
    error = OSError(13, "Denied")
    report.error(error, url="file:///a")
    report.error(error, url="file:///b")
    assert report.errors == 2 and report.count == 2
    destination = io.StringIO()
    report.finish(0, destination)
    assert [row["url"] for row in json.loads(destination.getvalue())["records"]] == [
        "file:///a",
        "file:///b",
    ]


def test_plain_listings_do_not_build_report_metadata(server, capsys, monkeypatch):
    def unwanted(entry):
        pytest.fail("human output must not pay for machine-report conversion")

    monkeypatch.setattr(fs, "_entry_record", unwanted)
    assert fs.main(["ls", str(server.url) + "data"]) == 0
    assert "a.root" in capsys.readouterr().out


@pytest.mark.parametrize("fmt", ["json", "xml"])
@pytest.mark.parametrize("encoding", ["ascii", "latin-1", "utf-16"])
def test_machine_documents_are_utf8_independently_of_stdout_locale(fmt, encoding, monkeypatch):
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding=encoding)
    monkeypatch.setattr(sys, "stdout", stream)

    def action():
        output.record(value="é\r<&>")
        return 0

    assert output.run_cli("test", ["--output-format", fmt], action) == 0
    result = output.parse(buffer.getvalue().decode("utf-8"), fmt)
    assert result["records"][0]["value"] == "é\r<&>"
    stream.detach()


@pytest.mark.parametrize("fmt", ["json", "xml"])
@pytest.mark.parametrize(
    "command, extras, pending",
    [("prepare", ["--status", "opaque-request"], "queued"), ("locality", [], "offline")],
)
def test_per_file_staging_errors_do_not_look_like_batch_success(
    fmt, command, extras, pending, server, capsys, monkeypatch
):
    from xrdclient import FileSystem

    statuses = [
        PrepareStatus(path="/good", online=True),
        PrepareStatus(path="/bad", error="not found"),
        PrepareStatus(path="/offline", on_tape=True),
    ]
    monkeypatch.setattr(FileSystem, "query_prepare", lambda *args: statuses)
    monkeypatch.setattr(FileSystem, "archive_info", lambda *args: statuses)
    args = [command, "--output-format", fmt, str(server.url) + "data/a.root", *extras]
    assert fs.main(args) == 0  # keep the pre-existing staging exit policy
    report = output.parse(capsys.readouterr().out, fmt)
    assert not report["summary"]["ok"] and report["summary"]["error_count"] == 1
    rows = [row for row in report["records"] if row["kind"] == "result"]
    assert [row["status"] for row in rows] == [
        "ready",
        "failed",
        pending,
    ]
    error = next(row for row in report["records"] if row["kind"] == "error")
    assert error["path"] == "/bad" and error["code"] is None


def test_legacy_json_diagnostic_failure_keeps_existing_payload(server, capsys):
    assert fs.main(["doctor", "--json", str(server.url) + "missing"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and "checks" in payload and "schema" not in payload
