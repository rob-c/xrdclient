"""Shared report contracts: typed JSON/XML, binary data and failure boundaries."""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import pytest

from xrdclient.cli import _output as output
from xrdclient.cli import cp, fs
from xrdclient.errors import ServerError
from xrdclient.testing import FakeServer
from xrdclient.types import PrepareStatus
from xrdclient.url import parse


def xml_value(node):
    kind = node.attrib["type"]
    if kind == "object":
        return {xml_name(field): xml_value(field) for field in node}
    if kind == "array":
        return [xml_value(item) for item in node]
    if kind == "null":
        return None
    text = node.text or ""
    if kind == "string":
        if "encoding" in node.attrib:
            return base64.b64decode(text).decode("utf-8", "surrogatepass")
        return text
    return json.loads(text)


def xml_name(node):
    name = node.attrib["name"]
    if "name_encoding" in node.attrib:
        return base64.b64decode(name).decode("utf-8", "surrogatepass")
    return name


def decode(text, fmt):
    if fmt == "json":
        return json.loads(text)
    root = ET.fromstring(text)
    return {
        "schema": root.attrib["schema"],
        "version": int(root.attrib["version"]),
        "tool": xml_value(root.find("tool")),
        "command": xml_value(root.find("command")),
        "records": [xml_value(node) for node in root.find("records")],
        "summary": xml_value(root.find("summary")),
    }


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_lossless_typed_report(fmt):
    @dataclass
    class Value:
        size: int

    data = {"null": None, "bool": True, "false": False, "n": 1.25, "empty": ""}
    data.update({"list": (Value(2), parse("root://h//p")), "blob": bytearray(b"\xff\0")})
    data["\udcff\0"] = "\udcff\0<&>é"
    data["cr"] = "a\rb\r\nc"
    report = output.Report("test", fmt)
    report.record("result", value=data)
    destination = io.StringIO()
    report.finish(0, destination)
    result = decode(destination.getvalue(), fmt)
    value = result["records"][0]["value"]
    assert value["list"] == [{"size": 2}, "root://h:1094//p"]
    assert value["blob"] == {"encoding": "base64", "data": "/wA="}
    assert value["\udcff\0"] == "\udcff\0<&>é"
    assert value["null"] is None and value["bool"] is True and value["false"] is False
    assert value["empty"] == "" and value["n"] == 1.25
    assert value["cr"] == "a\rb\r\nc"
    assert result["summary"] == {"exit_code": 0, "ok": True, "error_count": 0, "record_count": 1}
    assert report.spool.closed


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_binary_chunks_are_bounded_and_lossless(fmt):
    report = output.Report("cat", fmt)
    stream = output.TextOutput(report, "stdout")
    report.identity = {"url": "file:///a"}
    blob = b"\xff\0" * (1 << 20) + b"x"
    assert stream.buffer.write(memoryview(blob)) == len(blob)
    assert stream.buffer.write(b"") == 0
    stream.flush()
    stream.buffer.flush()
    assert not stream.isatty() and stream.encoding == "utf-8"
    with pytest.raises(io.UnsupportedOperation):
        stream.fileno()
    destination = io.StringIO()
    report.finish(0, destination)
    records = decode(destination.getvalue(), fmt)["records"]
    chunks = [base64.b64decode(row["value"]["data"]) for row in records]
    assert b"".join(chunks) == blob
    assert all(len(chunk) <= 1 << 20 for chunk in chunks)
    assert [row["offset"] for row in records] == [0, 1 << 20, 2 << 20]
    assert all(row["url"] == "file:///a" for row in records)


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_streams_messages_errors_and_nested_entrypoints(fmt, capsys, monkeypatch):
    def action():
        output.identify("stat", url="file:///a")
        output.record(value=3)
        output.payload({"a": b"x"})
        output.message("out")
        output.message("err", stderr=True)
        print("print")
        print("stderr", file=sys.stderr)
        output.current().command = "stat"
        assert output.run_cli("inner", ["--xml"], lambda: 0) == 0
        exc = OSError(13, "Denied")
        output.error(exc)
        output.error(exc)  # one exception crossing adapters must not be counted twice
        return 0

    monkeypatch.setattr(sys, "argv", ["test", "--output-format", fmt])
    assert output.run_cli("test", None, action) == 0
    captured = capsys.readouterr()
    result = decode(captured.out, fmt)
    assert captured.err == "" and output.current() is None
    assert result["command"] == "stat" and not result["summary"]["ok"]
    assert result["summary"]["error_count"] == 1
    error = next(row for row in result["records"] if row["kind"] == "error")
    assert error["code"] == error["errno"] == 13 and error["url"] == "file:///a"
    assert result["records"][0]["value"] == 3


@pytest.mark.parametrize(
    "exc, code",
    [
        (SystemExit(), 0),
        (SystemExit(2), 2),
        (SystemExit("bad"), 1),
        (KeyboardInterrupt(), 130),
        (ValueError("bad"), 1),
    ],
)
def test_reported_exception_policy(exc, code, capsys):
    def action():
        raise exc

    assert output.run_cli("test", ["--xml"], action) == code
    result = decode(capsys.readouterr().out, "xml")
    assert result["summary"]["exit_code"] == code
    assert result["summary"]["error_count"] == int(bool(code))


def test_nonzero_return_is_not_silent(capsys):
    assert output.run_cli("test", ["--json"], lambda: 7) == 7
    result = json.loads(capsys.readouterr().out)
    assert result["records"][0]["code"] == 7


def test_plain_helpers_and_closed_context(capsys):
    output.record(value=object())
    output.identify("plain")
    output.error(ValueError())
    output.payload({"x": b"a"})
    output.message("out")
    output.message("err", stderr=True)
    captured = capsys.readouterr()
    assert json.loads(captured.out.removesuffix("out")) == {"x": "a"}
    assert captured.err == "err"
    assert output.run_cli("test", [], lambda: 9) == 9
    report = output.Report("test", "json")
    token = output._ACTIVE.set(report)
    try:
        output.TextOutput(report, "stdout").write("")
        report.finish(0, io.StringIO())
        output.record(value="late")
        output.error(ValueError("late"))
        with pytest.raises(SystemExit) as caught:
            output.Parser().error("late")
        assert caught.value.code == 2
        assert report.count == 0
    finally:
        output._ACTIVE.reset(token)


def test_legacy_payload_compatibility_and_binary_override(capsys):
    def action():
        output.payload({"v": b"\xff"})
        return 0

    assert output.run_cli("test", ["--json"], action, legacy_json=True) == 0
    assert json.loads(capsys.readouterr().out) == {"v": "�"}

    def binary():
        action()
        sys.stdout.buffer.write(b"\xff")
        return 0

    assert output.run_cli("test", ["--json"], binary, legacy_json=True) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == output.SCHEMA


def test_output_pipe_failure_closes_spool_and_restores_context(monkeypatch):
    class Broken(io.StringIO):
        def write(self, text):
            raise BrokenPipeError()

    reports = []
    monkeypatch.setattr(sys, "stdout", Broken())

    def action():
        reports.append(output.current())
        return 0

    assert output.run_cli("test", ["--xml"], action) == 1
    assert reports[0].spool.closed and output.current() is None


@pytest.mark.parametrize(
    "args, expected",
    [
        ([], ("text", False)),
        (["--", "--json"], ("text", False)),
        (["--output-format"], ("text", False)),
        (["--output-format=json"], ("json", False)),
        (["--xml", "--output-format=bad"], ("xml", False)),
        (["--xml", "--output-format", "bad"], ("xml", False)),
        (["--output-format", "text"], ("text", False)),
        (["--json"], ("json", True)),
    ],
)
def test_format_selection(args, expected):
    assert output._selection(args) == expected
    assert output.requested(args) == (expected[0] != "text")


@pytest.mark.parametrize("legacy", [False, True])
def test_conflicting_flags_are_structured_usage_errors(legacy, capsys):
    parser = output.Parser()
    output.flags(parser, legacy_json=legacy)
    assert (
        output.run_cli("test", ["--json", "--xml"], lambda: parser.parse_args(["--json", "--xml"]))
        == 2
    )
    result = decode(capsys.readouterr().out, "xml")
    assert result["records"][0]["error_type"] == "ValueError"


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
    report = decode(captured.out, fmt)
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
    report = decode(captured.out, fmt)
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
    report = decode(capsys.readouterr().out, fmt)
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
    report = decode(capsys.readouterr().out, fmt)
    assert target.read_bytes() == source.read_bytes()
    result = next(row for row in report["records"] if row["kind"] == "result")
    assert result["value"]["size"] == 5 and result["status"] == "succeeded"
    assert any(row["kind"] == "progress" for row in report["records"])


def test_non_finite_and_unknown_values_are_rejected():
    report = output.Report("test", "json")
    with pytest.raises(ValueError):
        report.record("result", value=float("nan"))
    with pytest.raises(TypeError):
        report.record("result", value=object())
    report.finish(1, io.StringIO())


def test_error_uses_numeric_protocol_code(capsys):
    def action():
        output.error(ServerError(3010, "access denied"))
        return 1

    assert output.run_cli("test", ["--json"], action) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["records"][0]["code"] == 3010


def _assert_content(content):
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
    report = decode(capsys.readouterr().out, fmt)
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
    report = decode(capsys.readouterr().out, fmt)
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
        assert decode(capsys.readouterr().out, fmt)["summary"]["ok"]
    _prepare_status_report(fmt, str(server.url) + "data/moved", capsys)


def _prepare_status_report(fmt, first, capsys):
    assert fs.main(["prepare", "--output-format", fmt, first]) == 0
    report = decode(capsys.readouterr().out, fmt)
    handle = next(row["handle"] for row in report["records"] if row["kind"] == "result")
    assert fs.main(["prepare", "--output-format", fmt, "--status", handle, first]) == 0
    report = decode(capsys.readouterr().out, fmt)
    row = next(row for row in report["records"] if row["kind"] == "result")
    assert row["request_id"] == handle and row["value"]["path"] == parse(first).path


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_same_directory_path_on_different_endpoints_keeps_identity(fmt, server, capsys):
    with FakeServer(files={"/data/b.root": b"b"}) as other:
        urls = [str(server.url) + "data", str(other.url) + "data"]
        assert fs.main(["ls", "--output-format", fmt, *urls]) == 0
        report = decode(capsys.readouterr().out, fmt)
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
    result = decode(buffer.getvalue().decode("utf-8"), fmt)
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
    report = decode(capsys.readouterr().out, fmt)
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
