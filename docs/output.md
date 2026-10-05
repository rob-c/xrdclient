# Machine-readable command output

Every `xrd-fs` subcommand and `xrd-cp` accepts:

```console
$ xrd-fs stat --output-format json root://storage.example//store/a.root
$ xrd-fs prepare --xml root://tape.example//store/a.root
$ xrd-cp --output-format xml input.root root://storage.example//store/input.root
$ xrd-fs --output-format json --help
$ xrd-cp --output-format json --version
```

`--output-format text` is the default. JSON/XML modes put **all CLI output**
in one UTF-8 document on stdout: results, acknowledgements, progress, diagnostics,
help, versions and errors. Stderr is empty. Copied destination files are not
reformatted. Explicit diagnostic log files remain ordinary logs.

The formatter is also used by xgfalclient's `gfal-*` commands, compatibility
shell and version tools. It has no Rucio/FTS dependency: external coordinators
own their job identifiers, scheduling, policy and service interfaces.

## Compatibility

Existing Xrd `--json` result shapes, including diagnostic failure payloads, are
preserved for commands that already returned JSON. Use **`--output-format json`**
for the uniform report below. Under `--json`, previously silent commands,
errors without an existing result payload, help/version and
binary-output commands now produce a report too. `--xml` always means a report.
Numeric process exit codes and Python library APIs are unchanged.

## Version 1 report

```json
{
  "schema": "storage-client-report",
  "version": 1,
  "tool": "xrd-fs",
  "command": "mkdir",
  "records": [
    {
      "sequence": 1,
      "kind": "result",
      "operation": "mkdir",
      "url": "root://storage.example//store/new",
      "status": "succeeded"
    }
  ],
  "summary": {
    "exit_code": 0,
    "ok": true,
    "error_count": 0,
    "record_count": 1
  }
}
```

`sequence` gives the order of emitted records, starting at 1. A consumer should
check `schema` and `version`, and tolerate additional fields and record kinds.
Results are constructed from library values, not extracted from display text.

| `kind` | Meaning |
| --- | --- |
| `result` | Per-operation result: identities (`url`, `source`/`target`, or endpoint/paths), status and command-specific fields. |
| `data` | A typed aggregate result, corresponding to an existing Xrd JSON payload. It can repeat information in `result`; do not count it as another operation. |
| `error` | Numeric `code` (null when the backend supplies no number), separate OS `errno` (nullable), exception `error_type`, plain-language `message`, and the active operation's identity. Codes retain their original protocol/OS namespace. |
| `request` | An opaque staging `request_id` and associated URLs. Store the identifier unchanged for later polling/release. |
| `progress` | Available byte counters and timing from the transfer callbacks. Progress is not proof of final durability or successful checksum verification. |
| `event` / `wait` | Transfer stages and polling/backoff observations, when the underlying command emits them. |
| `content` | Binary stdout/stderr data encoded as base64, with `stream`, byte `offset`, identity and `value`. |
| `version` | Typed tool/library/plugin version information. |
| `message` | Supplemental display text and its original `stream`; not a machine-result API. Help and usage text are included here. |

Status values include `succeeded`, `planned` (dry run), `skipped`, `queued`,
`ready`, `offline` and `failed`. Staging handles may also be in `result.handle`.
Command-specific counters describe their actual source: GFAL `save.bytes_read`
counts consumed input, and `copy.bytes_transferred` is null when no byte monitor
ran. Do not substitute expected size for an unknown committed-byte count.

Completed operations are recorded before the next batch item starts, so earlier
acknowledgements survive a later failure. A recursive copy that raises before
returning its individual results does not necessarily provide every completed
child acknowledgement; inspect/reconcile destinations before retrying it.

`summary.ok` is true only if the command exits 0 **and** emits no error records.
GFAL staging and other compatibility commands can retain exit 0 while a file
fails: coordinators must check both `summary.ok` and individual states.
`error_count` counts error observations, not unique failed files.

## XML types and binary data

XML carries the same values as JSON, including null, booleans, arrays and
numbers; it does not flatten everything into strings. An example result is:

```xml
<report schema="storage-client-report" version="1">
  <tool type="string">xrd-fs</tool>
  <command type="string">mkdir</command>
  <records>
    <record type="object">
      <field name="sequence" type="number">1</field>
      <field name="kind" type="string">result</field>
      <field name="status" type="string">succeeded</field>
    </record>
  </records>
  <summary type="object">
    <field name="exit_code" type="number">0</field>
    <field name="ok" type="boolean">true</field>
    <field name="error_count" type="number">0</field>
    <field name="record_count" type="number">1</field>
  </summary>
</report>
```

Objects use `<field name="…">`, arrays use `<item>`, and null uses
`type="null"` without text. Attribute/value escaping is handled by Python's
built-in XML serializer, without a new parser dependency. Strings containing
XML-invalid characters or carriage returns, including undecodable filenames, have
`encoding="utf8-surrogatepass-base64"`; decode base64 then UTF-8 with
`surrogatepass`. Invalid field names are similarly encoded, with
`name_encoding="utf8-surrogatepass-base64"`.

Binary values in either format are objects containing `encoding="base64"`
and `data`. For `cat`, `tail` or copies to stdout, concatenate the decoded
`content.value.data` records in sequence. Each content chunk is at most 1 MiB
before encoding. Offsets are per stdout/stderr byte stream, not per input file.

## Process and resource boundaries

Reports are emitted **when the command finishes**, not as live NDJSON or a live
XML stream. Transfer/polling events are observations inside the final report.
`tail --follow` therefore returns its report only when following ends.
Records spill to a temporary file after 1 MiB, keeping binary capture bounded
in memory; large reports still require temporary disk space, and base64 adds
size and CPU cost. Prefer copying to a destination file and reporting metadata
for bulk data movement.

These formats describe CLI process output. Concurrent programmatic CLI calls
within one interpreter are not supported because stdout/stderr redirection is
process-wide; use separate subprocesses or the Python APIs. Ordinary exceptions,
usage errors and handled interrupts produce reports; a killed process, full
temporary disk or broken output pipe cannot guarantee a complete document.

## Example coordinator boundary

```python
import json
import subprocess

completed = subprocess.run(
    ["gfal-bringonline", "--output-format", "json", "--from-file", "urls.txt"],
    capture_output=True, text=True, check=False,
)
report = json.loads(completed.stdout)
assert (report["schema"], report["version"]) == ("storage-client-report", 1)
for row in report["records"]:
    if row["kind"] == "request":
        store_staging_handle(row["request_id"], row["urls"])
    elif row["kind"] == "error":
        record_failure(row.get("url"), row["code"], row["message"])
```

Here `store_staging_handle` and `record_failure` belong to the external
application. Neither client imports or manages that application's services.
