# Building a tool on xrdclient

A grid tool needs the same plumbing before its first request whatever it
talks to: find the proxy or the token, trust the grid's CAs, refuse a
credential file somebody else could read, write `--json` output scripts can
parse, parse a server's XML without entity expansion, back off politely.
xrdclient does all of that for its own transfers, and since 0.4.0 it
publishes each piece as a stable module so that tools built on it -
[xgfalclient](https://github.com/rob-c/xgfalclient), xrdarc (a NorduGrid
ARC client), your own scripts - get the same answers without importing
anything private.

| Module | What it gives a tool |
| --- | --- |
| `xrdclient.credentials` | Proxy, certificate/key, bearer-token and CA discovery; reading a credential file safely |
| `xrdclient.tls` | A client `SSLContext` with grid trust, CRLs, an optional client credential, shared and reloaded |
| `xrdclient.report` | The `storage-client-report` `--json`/`--xml` output ([format](output.md)) |
| `xrdclient.safexml` | `fromstring` that refuses `<!DOCTYPE` and entities |
| `xrdclient.retry` | Jittered exponential backoff; `retry_after` for HTTP `Retry-After` |
| `xrdclient.log` | `redact` and a `RedactingFilter` for a tool's own logs |

`report`, `safexml` and `retry` are [xrdcore](https://github.com/rob-c/xrdcore)'s
modules under xrdclient's names (the same module objects), and `log`'s
redaction is xrdcore's too: a tool that needs only those can depend on
xrdcore alone and import `xrdcore.report`, `xrdcore.safexml`,
`xrdcore.retry` and `xrdcore.log` - along with `xrdcore.checksum`,
`xrdcore.cancel`, `xrdcore.pipeline`, `xrdcore.fanout` (thread fan-out,
first failure wins), `xrdcore.cli` (the shared exit codes, Ctrl-C and
closed-pipe handling) and `xrdcore.errors` - without
installing a client. The errors are one hierarchy either way
([errors](errors.md#one-hierarchy-with-the-libraries)).

`credentials` and `tls` are [xrdsec](https://github.com/rob-c/xrdsec)'s the
same way (`xrdclient.credentials is xrdsec.credentials`), as are the X.509,
proxy and VOMS modules under `xrdclient.crypto` and the test PKI under
`xrdclient.testing`. A tool that needs grid credentials but not an XRootD
client can depend on xrdsec alone, which also offers what xrdclient does not
publish: `xrdsec.tokens` (a URL's token in front of the WLCG search),
`xrdsec.usercred` and `xrdsec.discovery` (gfal2's and ARC's X.509 search
orders as data).

All of them are cheap to import except `xrdclient.tls`, which loads `ssl`;
`tests/test_import_cost.py` holds the others to that.

This page is about tools built *on* xrdclient. The tools that maintain the
chain itself (the take-over scorecard, the release pre-flight, the platform
and packaging tools) are not part of any package's API; they live in
[xrd-devtools](https://github.com/rob-c/xrd-devtools).

## Credentials

Every lookup takes the environment as an argument (`environ`, default
`os.environ`) and, where a file name has the uid in it, `uid` (default
`os.geteuid()`). Pass your own mapping to resolve credentials from a
configuration file of your tool's, or in a test - no monkeypatching. A
variable that is set but blank counts as unset: `X509_CERT_DIR=` means "not
configured", never "the current directory".

```python
from xrdclient import credentials

proxy = credentials.find_proxy()                 # $X509_USER_PROXY, else /tmp/x509up_u<uid>
pair = credentials.find_user_credential(dirs=("~/.arc", "~/.globus"))
token = credentials.find_bearer_token(explicit=args.token, token_file=args.token_file)
ca_dir = credentials.grid_ca_path()              # None: use the system store
```

- **Named beats guessed.** `find_proxy` returns `$X509_USER_PROXY` as
  named, present or not; it is never replaced by a proxy found elsewhere.
  Whether a missing named proxy is an error (XrdCl, ARC) or just "no
  proxy" (davix, for token-only access) is your tool's decision.
- **Insecure files raise.** A file at a predictable location
  (`/tmp/x509up_u<uid>`, `$XDG_RUNTIME_DIR/bt_u<uid>`, `/tmp/bt_u<uid>`)
  that is a symbolic link, someone else's, or readable by others raises
  `InsecureCredentialError` - not skipped, because falling through would
  use whatever somebody else left at the next location. Catch
  `xrdclient.errors.CredentialError` (its base) to report it.
- **Token order.** `find_bearer_token` is the WLCG Bearer Token Discovery
  order: `explicit`, `token_file`, `$BEARER_TOKEN`, `$BEARER_TOKEN_FILE`,
  `$XDG_RUNTIME_DIR/bt_u<uid>`, `/tmp/bt_u<uid>`. `bearer_token_files`
  lists the files it may read, for messages.
- **Reading.** `read_private_file(path, kind="X.509 proxy", predictable=False)`
  reads a credential once it is shown to be a regular file, yours, mode
  `0600`; `predictable=True` refuses a symbolic link.
- **Writing.** `write_private_file(path, data, kind="X.509 proxy", predictable=False)`
  writes one atomically, mode `0600`, through a fresh `mkstemp` file renamed
  into place; `predictable=True` refuses a symbolic link at `path`, otherwise
  a link is followed and its target replaced.

The full reference is under [API: building blocks](api.md#building-blocks-for-other-tools).

## TLS client contexts

```python
from xrdclient import credentials, tls

context = tls.client_context(
    cert=credentials.find_proxy(),           # or cert=..., key=... for a separate key
    ca_path=credentials.grid_ca_path(),
    check_crl="leaf",                        # the CA directory's <hash>.r0 files
)
```

`client_context` presents **no** certificate unless you pass one: a client
that authenticates with a bearer token, or anonymously, must not hand a
server the proxy it happens to find in `/tmp`. `present_default=True` opts
in to presenting the default proxy, as gfal2 and XrdCl do - and as
xrdclient's own `Config`-driven connections do. A separate key is read
through `read_private_file` and piped to OpenSSL, never written to disk;
`key_password` unlocks an encrypted one (text, bytes, or a callable asked
only when needed). A credential that exists only in memory - a proxy from
`xrdclient.crypto.proxies.create_proxy` - is presented with
`credential_pem=proxy.to_pem()` instead of `cert=`, piped the same way.
Contexts are shared per setup and rebuilt when a proxy
is renewed or the CA directory changes, so call it per connection without
worrying about the cost; `tls.clear_cache()` forgets them.

## Command-line reports

`xrdclient.report` is the formatter behind `xrd-fs`, `xrd-cp`, every
`gfal-*` command and `xrdarc`. A tool that uses it emits the same
[storage-client-report](output.md) document, so one parser serves them all:

```python
from xrdclient import report

def main(argv):
    parser = report.Parser(prog="mytool")
    report.flags(parser)                     # --json, --xml, --output-format
    args = parser.parse_args(argv)
    report.identify("submit", ce=args.ce)
    report.record(job=job_id, status="queued")
    return 0

raise SystemExit(report.run_cli("mytool", sys.argv[1:], lambda: main(sys.argv[1:])))
```

## XML, retries, logs

- `safexml.fromstring(data)` returns an ordinary `ElementTree` element and
  raises `safexml.UnsafeXML` for any document type or entity declaration.
- `retry.delay(attempt, base, cap)` / `retry.pause(...)` are the package's
  full-jitter backoff. `retry.retry_after(response.headers)` reads a `429`
  or `503`'s `Retry-After` in either form (seconds or an HTTP-date) and
  returns seconds, or `None` when absent or malformed; cap it before
  sleeping.
- `log.redact(text)` removes tokens, signatures and `Authorization` values;
  `handler.addFilter(log.RedactingFilter())` does it for every record a
  handler of yours writes.

## Old private names

Before 0.4.0 other tools reached into xrdclient's private modules. Those
names were removed in 0.4.0; the public modules below replace them, and are
the *same module objects* as the libraries', so module state - the active
report, the jitter source - is shared whichever library set it:

| Old (private, removed) | Use instead |
| --- | --- |
| `xrdclient.cli._output` | `xrdclient.report` |
| `xrdclient._xml` | `xrdclient.safexml` |
| `xrdclient._retry` | `xrdclient.retry` |
| `xrdclient._log` | `xrdclient.log` (`_RedactingFilter` is `RedactingFilter`) |
| `xrdclient.copy._pipeline` | `xrdcore.pipeline` |
| `xrdclient._fanout` | `xrdcore.fanout` |
| `xrdclient.http._engine`, `._connection`, `._wire`, `._ranges`, `._webdav`, `xrdclient.s3._codec` | `xrdhttp.engine`, `.connection`, `.wire`, `.ranges`, `.webdav`, `xrdhttp.s3.codec` |
| `xrdclient.crypto.crc32c._crc32c_py` | `xrdclient.crypto.crc32c.crc32c_py` (`xrdcore.crc32c.crc32c_py`) |
| `xrdclient._gridpaths.grid_ca_path` / `grid_voms_dir` / `default_ca_path` | `xrdclient.credentials.*` (same functions) |
| `xrdclient._secure_files.read_credential_file(path, kind=, follow_symlinks=)` | `xrdclient.credentials.read_private_file(path, kind=, predictable=)` |
| `xrdclient._secure_files.write_credential_file(path, data, kind=, follow_symlinks=)` | `xrdclient.credentials.write_private_file(path, data, kind=, predictable=)` |
| `xrdclient.crypto._voms`, `._proxyfiles`, `._proxyinfo` | `xrdsec._voms`, `._proxyfiles`, `._proxyinfo` |
| `xrdclient.crypto.x509._decode_name` / `._extensions` / `._policy_language` / `._parse_certificate` | `xrdsec.x509.name_from_element` / `tbs_extensions` / `policy_language` / `parse_certificate` |
| `xrdclient.testing._https`, `._replies`, `._keys` | `xrdsec.testing.https`, `._replies`, `._keys` |

These public names keep working, as the library's own module:

| xrdclient name | Is |
| --- | --- |
| `xrdclient.crypto.checksum`, `xrdclient.crypto.crc64` | `xrdcore.checksum`, `xrdcore.crc64` |
| `xrdclient.crypto.der`, `.rsa`, `.aes`, `.p256`, `.ed25519`, `.signatures`, `.proxies`, `.delegation`, `.userproxy`, `.trust`, `.voms` | `xrdsec.der`, ... `xrdsec.voms` |
| `xrdclient.testing.pki`, `.voms` | `xrdsec.testing.pki`, `.voms` |
| `xrdclient.http.expect`, `xrdclient.s3.sigv4` | `xrdhttp.expect`, `xrdhttp.s3.sigv4` |

The Config-shaped adapters - `_gridpaths.default_proxy_path(config)`,
`_tokens`, `_usercred` and `transport.base.tls_context` - stay in
xrdclient, built on xrdsec, because a `Config` is xrdclient's type. Outside
xrdclient, use the library forms, which take the environment instead:

| Config-shaped (xrdclient) | Library form |
| --- | --- |
| `xrdclient._gridpaths.default_proxy_path(config)` | `xrdclient.credentials.default_proxy_path(environ, uid)` |
| `xrdclient._tokens.discover_token(config, url)` | `xrdclient.credentials.find_bearer_token(...)` |
| `xrdclient.transport.base.tls_context(config)` | `xrdclient.tls.client_context(...)` (presents nothing unless asked) |
| `xrdclient.auth.ztn.token_claims(token)` / `token_expiry(token)` | `xrdsec.tokens.token_claims(token, padding=True)` / `token_expiry(...)` |
