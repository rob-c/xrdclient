"""Versioned CLI reports shared by both clients, without service dependencies.

Records are spooled, not accumulated in memory. Binary streams become bounded
base64 records. Human output remains a supplemental message, never the source
of a result's fields or error code.
"""

from __future__ import annotations

import argparse
import base64
import contextvars
import dataclasses
import io
import json
import re
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, NoReturn, TextIO, cast
from xml.etree import ElementTree as ET

from ..url import XRootDURL

SCHEMA = "storage-client-report"
VERSION = 1
_ACTIVE: contextvars.ContextVar[Report | None] = contextvars.ContextVar("cli_report", default=None)
# XML parsers normalise literal CR characters, so encode those as well.
_INVALID_XML = re.compile("[\x00-\x08\x0b-\x1f\ud800-\udfff\ufffe\uffff]")


def current() -> Report | None:
    return _ACTIVE.get()


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        report = current()
        if report is not None and report.closed:
            raise SystemExit(2)
        error(ValueError(message), code=2)
        super().error(message)


def flags(parser: argparse.ArgumentParser, *, legacy_json: bool = False) -> None:
    group = parser.add_mutually_exclusive_group()
    if legacy_json:
        group.add_argument("--json", action="store_true", help="legacy JSON result format")
    else:
        group.add_argument(
            "--json",
            dest="output_format",
            action="store_const",
            const="json",
            help="versioned JSON report",
        )
    group.add_argument(
        "--xml",
        dest="output_format",
        action="store_const",
        const="xml",
        help="versioned XML report",
    )
    group.add_argument(
        "--output-format",
        choices=("text", "json", "xml"),
        help="output format; JSON/XML include results, messages and numeric errors",
    )


def _selection(argv: Sequence[str]) -> tuple[str, bool]:
    selected, legacy = "text", False
    for index, value in enumerate(argv):
        if value == "--":
            break
        if value in ("--json", "--xml"):
            selected, legacy = value[2:], value == "--json"
        elif value.startswith("--output-format="):
            candidate = value.partition("=")[2]
            if candidate in ("text", "json", "xml"):
                selected, legacy = candidate, False
        elif value == "--output-format" and index + 1 < len(argv):
            if argv[index + 1] in ("text", "json", "xml"):
                selected, legacy = argv[index + 1], False
    return selected, legacy


def requested(argv: Sequence[str]) -> bool:
    return _selection(argv)[0] in ("json", "xml")


def _normal(value: Any) -> Any:
    if isinstance(value, XRootDURL):
        return str(value)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _normal(
            {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
        )
    elif isinstance(value, dict):
        return {str(key): _normal(item) for key, item in value.items()}
    elif isinstance(value, (list, tuple)):
        return [_normal(item) for item in value]
    elif isinstance(value, (bytes, bytearray, memoryview)):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    return value


def _xml(parent: ET.Element, tag: str, value: Any, **attrs: str) -> None:
    node = ET.SubElement(parent, tag, attrs)
    if isinstance(value, dict):
        node.set("type", "object")
        for key, item in value.items():
            attrs = {"name": key}
            if _INVALID_XML.search(key):
                attrs = {"name": _encoded(key), "name_encoding": "utf8-surrogatepass-base64"}
            _xml(node, "field", item, **attrs)
    elif isinstance(value, list):
        node.set("type", "array")
        for item in value:
            _xml(node, "item", item)
    else:
        _scalar(node, value)


def _scalar(node: ET.Element, value: Any) -> None:
    if value is None:
        node.set("type", "null")
    elif isinstance(value, str):
        node.set("type", "string")
        if _INVALID_XML.search(value):
            node.set("encoding", "utf8-surrogatepass-base64")
            value = _encoded(value)
        node.text = value
    else:
        node.set("type", "boolean" if isinstance(value, bool) else "number")
        node.text = json.dumps(value, allow_nan=False)


def _encoded(value: str) -> str:
    return base64.b64encode(value.encode("utf-8", "surrogatepass")).decode("ascii")


class Report:
    def __init__(self, tool: str, output_format: str, *, legacy: bool = False) -> None:
        self.tool, self.output_format, self.legacy = tool, output_format, legacy
        self.command = tool
        self.identity: dict[str, Any] = {}
        self.count = self.errors = 0
        self.closed = False
        self.payload: Any = None
        self.has_payload = False
        self.has_content = False
        self.last_error: BaseException | None = None
        self.last_error_identity: dict[str, Any] = {}
        self.lock = threading.RLock()
        self.spool = tempfile.SpooledTemporaryFile(
            max_size=1 << 20, mode="w+", encoding="utf-8", newline="\n"
        )

    def record(self, kind: str, **fields: Any) -> None:
        with self.lock:
            if self.closed:
                return
            value = {"sequence": self.count + 1, "kind": kind, **fields}
            self.spool.write(json.dumps(_normal(value), ensure_ascii=True, allow_nan=False) + "\n")
            self.count += 1
            self.has_content |= kind == "content"

    def error(self, exc: BaseException, **fields: Any) -> None:
        with self.lock:
            identity = {**self.identity, **fields}
            if self.closed or (self.last_error is exc and self.last_error_identity == identity):
                return
            self.last_error = exc
            self.last_error_identity = identity
            self.errors += 1
            self.record(
                "error",
                **{
                    **self.identity,
                    "code": getattr(exc, "code", getattr(exc, "errno", None)),
                    "errno": getattr(exc, "errno", None),
                    "path": getattr(exc, "path", getattr(exc, "filename", None)),
                    "error_type": type(exc).__name__,
                    "message": getattr(exc, "user_message", str(exc)),
                    **fields,
                },
            )

    def rows(self) -> Iterator[dict[str, Any]]:
        self.spool.seek(0)
        for line in self.spool:
            yield json.loads(line)

    def finish(self, code: int, stream: TextIO) -> None:
        with self.lock:
            self.closed = True
        summary = {
            "exit_code": code,
            "ok": code == 0 and self.errors == 0,
            "error_count": self.errors,
            "record_count": self.count,
        }
        try:
            if self.legacy and self.has_payload and not self.has_content:
                from . import dumps

                stream.write(dumps(self.payload) + "\n")
            elif self.output_format == "json":
                self._json(stream, summary)
            else:
                self._xml(stream, summary)
            stream.flush()
        finally:
            self.spool.close()

    def _json(self, stream: TextIO, summary: dict[str, Any]) -> None:
        header = {"schema": SCHEMA, "version": VERSION, "tool": self.tool, "command": self.command}
        stream.write(json.dumps(header)[:-1] + ', "records": [')
        for index, row in enumerate(self.rows()):
            stream.write((", " if index else "") + json.dumps(row))
        stream.write('], "summary": ' + json.dumps(summary) + "}\n")

    def _xml(self, stream: TextIO, summary: dict[str, Any]) -> None:
        header = ET.Element("report", schema=SCHEMA, version=str(VERSION))
        _xml(header, "tool", self.tool)
        _xml(header, "command", self.command)
        stream.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        stream.write(ET.tostring(header, encoding="unicode")[:-9] + "<records>")
        for row in self.rows():
            wrapper = ET.Element("records")
            _xml(wrapper, "record", row)
            stream.write(ET.tostring(wrapper[0], encoding="unicode"))
        footer = ET.Element("report")
        _xml(footer, "summary", summary)
        stream.write("</records>" + ET.tostring(footer[0], encoding="unicode") + "</report>\n")


class BinaryOutput:
    def __init__(self, report: Report, stream: str) -> None:
        self.report, self.stream = report, stream
        self.offset = 0

    def write(self, data: bytes | bytearray | memoryview) -> int:
        for start in range(0, len(data), 1 << 20):
            chunk = memoryview(data)[start : start + (1 << 20)]
            self.report.record(
                "content",
                stream=self.stream,
                offset=self.offset,
                **self.report.identity,
                value=chunk,
            )
            self.offset += len(chunk)
        return len(data)

    def flush(self) -> None:
        pass


class TextOutput:
    encoding = "utf-8"

    def __init__(self, report: Report, stream: str) -> None:
        self.report, self.stream = report, stream
        self.buffer = BinaryOutput(report, stream)

    def write(self, text: str) -> int:
        if text:
            self.report.record("message", stream=self.stream, text=text)
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise io.UnsupportedOperation("a structured report is not a raw file descriptor")


class _Destination:
    """Machine documents use UTF-8 even when stdout has a legacy locale encoding."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.buffer = getattr(stream, "buffer", None)

    def write(self, text: str) -> None:
        if self.buffer is None:
            self.stream.write(text)
        else:
            self.buffer.write(text.encode("utf-8"))

    def flush(self) -> None:
        self.stream.flush()


def record(kind: str = "result", **fields: Any) -> None:
    report = current()
    if report is not None:
        report.record(kind, **{**report.identity, **fields})


def identify(operation: str, **fields: Any) -> None:
    report = current()
    if report is not None:
        report.identity = {"operation": operation, **fields}


def error(exc: BaseException, **fields: Any) -> None:
    report = current()
    if report is not None:
        report.error(exc, **fields)


def payload(value: Any, *, where: TextIO | None = None) -> None:
    report = current()
    if report is None:
        from . import dumps

        print(dumps(value), file=where)
    else:
        report.payload, report.has_payload = value, True
        report.record("data", value=value)


def message(text: str, *, stderr: bool = False) -> None:
    report = current()
    if report is None:
        stream = sys.stderr if stderr else sys.stdout
        stream.write(text)
        stream.flush()
    else:
        report.record("message", stream="stderr" if stderr else "stdout", text=text)


def run_cli(
    tool: str, argv: Sequence[str] | None, run: Callable[[], int], *, legacy_json: bool = False
) -> int:
    selected, legacy = _selection(sys.argv[1:] if argv is None else argv)
    if current() is not None or selected not in ("json", "xml"):
        return run()
    destination = cast("TextIO", _Destination(sys.stdout))
    report = Report(tool, selected, legacy=legacy and legacy_json)
    token = _ACTIVE.set(report)
    code = 1
    try:
        with (
            redirect_stdout(cast("TextIO", TextOutput(report, "stdout"))),
            redirect_stderr(cast("TextIO", TextOutput(report, "stderr"))),
        ):
            code = _execute(run, report)
    finally:
        _ACTIVE.reset(token)
        try:
            report.finish(code, destination)
        except BrokenPipeError:
            code = 1
    return code


def _execute(run: Callable[[], int], report: Report) -> int:
    try:
        code = run()
        if code and not report.errors:
            report.error(RuntimeError("The command did not complete successfully"), code=code)
        return code
    except SystemExit as exc:
        return _exit(exc, report)
    except KeyboardInterrupt as exc:
        report.error(exc, code=130, message="The command was interrupted")
        return 130
    except Exception as exc:
        report.error(exc)
        return 1


def _exit(exc: SystemExit, report: Report) -> int:
    code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    if code and not report.errors:
        message = "Invalid command-line arguments; see the diagnostic records"
        if isinstance(exc.code, str):
            message = exc.code
        report.error(ValueError(message), code=code)
    return code
