# Module map

This page is for whoever opens the source next. The packages below were each
one large module until 0.4.0; they are now a small **facade** plus private
submodules grouped by what the code does. Nothing about the public surface
changed: every name that was importable from the old module still is, from
the same path.

## The conventions every split follows

- **The facade keeps its import path and its `__all__`.** `xrdclient.copy.engine`,
  `xrdclient.testing.server`, `xrdclient.proto.requests`
  and `xrdclient.session.router` re-export every name they used to define,
  private ones included, as `from ._x import name as name` (the redundant
  alias is what mypy's `no_implicit_reexport` and ruff's F401 read as "this
  is a deliberate re-export"). Identities hold: `requests.Dirlist is
  _requests_ns.Dirlist`.
- **Public names stay where they were defined** wherever that is practical, so
  their `__module__`, the API docs and pickles are unchanged. The exceptions
  are noted per package below: the request encoders, and a handful of
  test-server helpers.
- **A name tests monkeypatch on the facade is looked up through the facade at
  call time.** Either its callers stay in the facade (whose globals the patch
  replaces), or a submodule calls back with a function-level
  `from . import facade` and `facade.name(...)`. That is why, for example,
  `copy/_metalink.py` calls `engine._copy_one` rather than importing it.
- **Submodules import cleanly in any order.** None imports its facade at
  module level; annotation-only imports sit under `TYPE_CHECKING`, and runtime
  call-backs into the facade are function-level imports.
- **Log messages keep their logger.** A submodule that logs uses the facade's
  logger name (`get_logger("xrdclient.copy.engine")`), so a logging
  configuration that named the old module still sees them.
- **The coverage gate follows the code.** `proto/*` and `crypto/*` are gated by
  directory globs in `pyproject.toml`, which match the new files and
  subpackages, so the moved code is held to the same 100% line-and-branch mark.

Each new module opens with a docstring saying what lives there and how it fits
with its siblings; read that first.

## `xrdclient.copy` — the copy engine

`copy/engine.py` is the core: the public `copy`, `copy_tree` and `CopyResult`,
and the pipeline between them - a `_Job` (what was asked), a `_Plan` (several
replicas, a resume, the bulk plane, spans, or one stream), `_move` (the bytes),
and `_Verify` / `_checked` (the checksum). Everything the planner and verifier
need to *ask* an endpoint is called through the engine's globals.

| Module | Holds | Why it is separate |
| --- | --- | --- |
| `engine.py` | `copy`, `_copy_one`, `copy_tree`, `CopyResult`; job, plan, spans, retry, pump driver, verification, cleanup | The decisions; everything else is an adapter it calls |
| `_endpoints.py` | `_reader`, `_writer`, `_resumer`, `_probe`, `_remove`, `_server_checksum`, URL helpers | The only place that knows which schemes exist - a new protocol is taught here |
| `_local.py` | `_RecoveringLocalReader` (survives `ESTALE`), `_sync_local_target` (`fsync` barrier) | What a network or FUSE filesystem does to a plain file |
| `_fastpath.py` | `_bulk_usable`, `_bulk_to_file`, `_bulk_to_stream`, raw-descriptor helpers | The bulk data plane adapter; owns raw descriptors nothing else touches |
| `_metalink.py` | `_copy_metalink` and its replica failover | A client of the engine: calls `engine._copy_one` once per replica |
| `_tree.py` | `_walk`, `_selected`, `_prune`, `_each` / `_in_order`, `_Total` | Tree machinery around `copy_tree`, which stays in the engine because it calls `copy` |

The read-ahead pump (`pump`, `fill`, `ReadAhead`) is `xrdcore.pipeline` since
0.4.0, shared with xgfalclient; the engine imports it from xrdcore (the
pre-0.4.0 alias `copy/_pipeline.py` was removed).

`replicas.py`, `limits.py` and `zip.py` are unchanged; `tpc.py` (the `root://` rendezvous, now cancellable and delegating) imports `_compare_ends`, `_probe` and `_remove` through the engine facade.

## `xrdclient.client` — files and filesystems

`client/file.py` is the facade for `File` and `Checkpoint`; `File` is put
together from three private base classes, so `File.__mro__` is the reading
order. `client/filesystem.py` is the `root://` `FileSystem` and the scheme
dispatcher. Loggers keep the name `xrdclient.client.file`, which the compat
layer's `FileMsg` log mask selects.

| Module | Holds | Why it is separate |
| --- | --- | --- |
| `file.py` | `File` (xattrs, checkpoints, `verify`), `Checkpoint`, re-exported constants | The facade and what belongs to the file as a whole |
| `_handle.py` | `_Handle`: open, re-open after a lost server, close, `abort`, data paths, `stat`/`size` | The lifecycle every operation shares |
| `_reading.py` | `_Reading`: `read`, `readinto`, `readv`, `pgread`, readahead, plane reads | The read half |
| `_writing.py` | `_Writing`: `write`, `write_pieces`, `writev`, `clone`, `pgwrite`, `truncate`, `sync` | The write half; never re-opens |
| `_vector.py` | `kXR_readv`/`kXR_writev`/`kXR_clone` packing and reply walking | Pure functions, no handle |
| `_fsbase.py` | `BaseFileSystem`: the abstract operations and the shared helpers (`walk`, `glob`, `rmtree`, ...) | What `root://`, WebDAV and S3 filesystems have in common |
| `bulk.py` | `download`, `stream`, `upload` | Parallel, restartable transfer over the bulk plane |

## Cancellation — `xrdcore.cancel`

Since 0.4.0 the module is xrdcore's, and xrdclient's code imports it there,
so the XRootD sessions here and xrdhttp's connections look at one context
variable. (The pre-0.4.0 name `xrdclient._cancel` is gone; see "Private
names removed in 0.4.0".) One module, two ways in (its docstring has the detail): *scopes*
(`CancelScope`, carried in a context variable) are how `xrdclient.aio` abandons
the request behind a cancelled coroutine without costing the connection;
*hooks* (`cancel=` on `third_party` and the bulk transfers, through
`as_check`/`watching`) are how a synchronous caller stops a long transfer
from another thread, by aborting its handles (`File.abort`, `Router.abort`).

## `xrdclient.session.router` — redirect following

`session/router.py` keeps `Router` (when to follow a redirect, go back to the
manager, or reconnect), the connection loans that let a filesystem and its
files share a session, and the public `Hop` / `trace`. `session/_rewrite.py`
holds the pure functions that decide *what* a re-issued request says: the
path-field snapshot and token folding (`_path_fields`, `_retarget`),
negative-port URL redirects (`_redirect_url`, `_redirect_path`, `_repath`), and
the `tried=` / `triedrc=` CGI (`tried`, `with_tried`, `TRIED_REASONS`, `LOST`),
which `client/file.py` also uses when it reopens a file elsewhere.

## `xrdclient.proto.requests` — request encoders

One class per `kXR_*` opcode. Callers import them from the facade
(`from xrdclient.proto import requests as r`); the code lives in private
modules grouped by what a request addresses, so finding an opcode means
opening the one short file for its area. Every encoder subclasses
`frames.Request` and writes its 16 parameter bytes with `buffer.Writer`;
`frames.encode` builds the frame and `machine.SessionMachine` sends it. These
classes *moved*, so their `__module__` names the area module; nothing depends
on it (reprs print the class name only).

| Module | Encodes | Why it is separate |
| --- | --- | --- |
| `requests.py` | Facade and `__all__` | The only import path; nothing outside `proto` names the private modules |
| `_requests_session.py` | protocol, login, auth, ping, endsess, bind, set, sigver | Requests about the connection, sent during bring-up and signing |
| `_requests_ns.py` | stat (+vfs), statx, dirlist, locate, mkdir, rm, rmdir, mv, symlink, link, readlink, setattr, chmod, truncate | Requests that name a path in the namespace |
| `_requests_query.py` | query, prepare | Questions for the server or its storage system, with free-form payloads |
| `_requests_io.py` | open, close, read, write, sync, readv, writev, clone, pgread, pgwrite, chkpoint, fattr | Traffic addressed to an open file's handle: the hot data path |
| `_requests_base.py` | Precomputed `struct` layouts; handle, path and buffer coercion | Shared by the area modules, which depend on it and never on each other |

## `xrdclient.crypto.voms` — VOMS attribute certificates

Since 0.4.0 this is xrdsec's: `xrdclient.crypto.voms` *is* `xrdsec.voms`, and
the machinery (`_voms/model`, `ac`, `diagnostics`, `trust`, `policy`,
`vomses`) is `xrdsec._voms` (the private alias `xrdclient.crypto._voms` was
removed in 0.4.0; patch `xrdsec._voms` directly). The layering and the patch points are unchanged and
documented in xrdsec; see "Built on xrdsec" below.

## `xrdclient.testing.server` — the fake XRootD server

`testing/server.py` is the facade and the part a test touches: `FakeServer`
(in-memory files, directories, links and xattrs, the fault-injection knobs and
the listening socket), `_Connection` (handshake, framing, redirect and wait
injection, dispatch), the `_HANDLERS` opcode table and the
`python -m xrdclient.testing` entry point. The per-opcode handlers live in
`testing/_server/` and are re-exported from `server.py` under their old names.
Every handler has the signature `(conn, streamid, params, body) -> Iterator[bytes]`
and yields raw frames.

| Module | Holds |
| --- | --- |
| `_server/wire.py` | `frame`, `error`, `pgwrite_cse`, header structs, `_clean`, `_NotFound` - a leaf every other module imports |
| `_server/session.py` | protocol, login, auth rounds, bind, ping, endsess, set |
| `_server/namespace.py` | stat/statx, dirlist, mkdir/rm/rmdir/mv/chmod/truncate, symlink/setattr/link/readlink |
| `_server/files.py` | open (creation and colocation rules), close, read/write/sync, readv/writev, clone, pgread/pgwrite, checkpoints |
| `_server/queries.py` | `kXR_query` subtypes and Fcntl, locate, prepare |
| `_server/xattrs.py` | `kXR_fattr` list/get/set/delete |

The names tests patch on the facade (`_checksum`, `CHECKPOINT_CAPACITY`,
`_HANDLERS`) are defined there and read through it when a handler runs. The
frame builders `frame`, `error` and `pgwrite_cse` are defined in
`_server/wire.py` (every handler needs them) and stay importable from
`xrdclient.testing` and `xrdclient.testing.server`.

## What was looked at and left alone

`proto/machine.py` and `cli/fs.py` are large but each is one thing, so they
were not split:

- `proto/machine.py` is mostly one class, `SessionMachine`, whose methods share
  its private state; splitting it would mean mixins, a redesign rather than a
  move. Its event dataclasses are already a labelled section at the top.
- `cli/fs.py` is one function per `xrd-fs` subcommand plus the argument
  parser; finding a subcommand is already a search for its name.
- `auth/gsi.py` was the third; since 0.4.0 the XrdSecgsi protocol it held is
  `xrdsec.gsi.secgsi` (whose module docstring narrates the handshake), and
  what stays is the adapter to the `kXR_auth` framework.

## Code xgfalclient shares

xgfalclient (the gfal2 replacement) depends on this package and reuses one
implementation of each concept rather than keeping its own; the dependency is
one-way, and nothing here imports it. A module listed below is a contract
with that second client: change it and run both suites. The underscore-private
ones are private to this package's *users*, not to xgfalclient.

| Module | What both clients use | How the two differ (the policy each passes) |
| --- | --- | --- |
| `http/expect.py`, `http/tape.py`, `http/tpc.py`, `metalink.py`, `s3/sigv4.py` | The HTTP protocol pieces | xrdhttp's since 0.4.0, re-exported under these names; see "Built on xrdhttp" below |
| `crypto/crc32c.py` | XRootD's 4 KiB page framing (`pack_pages`, `unpack_pages`, `page_span`); re-exports xrdcore's `crc32c`, `crc32c_py` and `IS_ACCELERATED` | Only XRootD frames pages; the CRC is xrdcore's |
| `crypto/x509.py`, `crypto/delegation.py`, `credentials.py`, `tls.py` | xrdsec's since 0.4.0 - see "Built on xrdsec" | - |
| `report.py`, `safexml.py`, `retry.py`, `log.py` (public) | The `--json`/`--xml` report, DTD-free XML, jittered backoff and `Retry-After`, redaction | See "Built on xrdcore" below: all but `log.py` are xrdcore's modules under xrdclient's names |

The HTTP pieces both clients share - and how the two differ in the policy
each passes them - are described module by module in xrdhttp's README.

The HTTP *session* is shared, the *policies* are not: xrdclient, xgfalclient
and xrdarc all send their requests through `xrdhttp.session.Session` (one
thread-safe pool per endpoint and TLS context, the stale-connection retry,
the redirect loop, `Expect: 100-continue`, retries with `Retry-After`), and
each configures it with what callers can observe: XrdCl's rules here (URL
paths encoded exactly once, credentials only to the same origin, `kXR_*`
errors), davix's in xgfalclient (existing escapes kept, `Authorization`
forwarded across a redirect within HTTPS, gfal2 errno for every status),
A-REX's in xrdarc.

## Built on xrdcore

Since 0.4.0 the dependency-free pieces every client needed live in
[xrdcore](https://github.com/rob-c/xrdcore), and xrdclient depends on it
(`xrdcore>=0.4.0,<0.5`; `xrdclient[fast]` pulls in `xrdcore[fast]`). The
public module names keep working, normally as the *same module object* - the
file in this tree replaces itself in `sys.modules` with xrdcore's, so module
state (the active report, the jitter source) is one thing however it was
reached, and a test that patches a name patches what the code calls.
xrdclient's own code imports xrdcore directly. The pre-0.4.0 *private* names
(`_xml`, `_retry`, `_log`, `cli/_output`, `copy/_pipeline`) were removed; see
"Private names removed in 0.4.0".

| xrdclient name | Is | What stays here |
| --- | --- | --- |
| `safexml` | `xrdcore.safexml` | - |
| `retry` | `xrdcore.retry` | - |
| `_cancel` | `xrdcore.cancel` | - (kept only for xgfalclient's suite) |
| `crypto/checksum`, `crypto/crc64` | `xrdcore.checksum`, `xrdcore.crc64` | - |
| `crypto/crc32c` | a module of its own | the 4 KiB page framing; the CRC is re-exported |
| `report` | `xrdcore.report` | importing it registers `XRootDURL` (`register_text_type`), so a report prints a URL as its string; `cli.dumps` is `xrdcore.report.dumps` |
| `log` | a module of its own | the `xrdclient.` logger hierarchy (`get_logger`); `redact`, `RedactingFilter`, `mute` and the filter instance are xrdcore's |
| `errors` | a module of its own | the XRootD errors; the shared names *are* xrdcore's classes ([errors](errors.md#one-hierarchy-with-the-libraries)) |

The library suites test the moved code; this suite tests the aliases
(`tests/test_public_modules.py`), the join of the error hierarchies
(`tests/test_errors.py`) and that none of it costs `import xrdclient` anything
(`tests/test_import_cost.py`: the bare import loads no xrdcore module, and an
alias loads only the one it names).

## Built on xrdhttp

Since 0.4.0 the HTTP protocol pieces live in
[xrdhttp](https://github.com/rob-c/xrdhttp), and xrdclient depends on it
(`xrdhttp[s3]>=0.4.0,<0.5`; botocore comes with its `s3` extra, and is
still imported only when S3 is used). xrdhttp takes plain URL strings, callbacks and an
`ssl.SSLContext` its caller built, and knows nothing of either client;
what stays here is the facade - `HTTPClient` (a policy over the shared
session: credentials, where they may follow a redirect, retries), `HTTPFileSystem`,
`HTTPRawIO`, `S3FileSystem`, `XRootDURL`-typed functions, `Config` and
credential wiring, the TLS contexts (`transport.base.tls_context`, the one
`roots://` gets), and the mapping of statuses onto `kXR_*` errors. The public
module names keep working, as the same module object where nothing of
xrdclient's is needed (the file replaces itself in `sys.modules`, as the
xrdcore aliases do) and as a thin adapter where a client type is. The private
ones (`http/_engine`, `_connection`, `_wire`, `_ranges`, `_webdav`,
`s3/_codec`) were removed; xrdclient's code imports xrdhttp:

| xrdclient name | Is | What stays here |
| --- | --- | --- |
| `http/expect` | `xrdhttp.expect` | - |
| `s3/sigv4` | `xrdhttp.s3.sigv4` | - |
| `metalink` | a module of its own | every name is xrdhttp's object; its checksums are `xrdclient.ChecksumInfo`, which is `xrdcore.checksum.ChecksumInfo` |
| `http/tape` | a module of its own | the verbs over a borrowed `HTTPClient` and an `XRootDURL`, answering `PrepareStatus`; the vocabulary (`TapeEndpoint`, `listed_endpoints`, `newest_endpoint`, `api_version`, `collapse`, `entry_for`, `json_text`) is xrdhttp's |
| `http/tpc` | a module of its own | `third_party`: the macaroon minted with this client's credential, cleanup of an empty destination, the exception an outcome raises; `follow_markers`, `Marker`, `Outcome` and the header and token rules are xrdhttp's |
| `http/client` | a module of its own | `carries_credentials` and `relays_transfer_headers` take `XRootDURL`s and apply `xrdhttp.redirects`; `wire_path` is xrdhttp's; a relative `Location` is resolved by `xrdhttp.redirects.resolve_location` |

xrdclient's own code imports xrdhttp directly. xrdhttp's suite tests the
moved code at 100 % line and branch coverage; this suite tests the facades,
the aliases (`tests/test_public_modules.py`) and that none of it costs
`import xrdclient` anything (`tests/test_import_cost.py`).

## Built on xrdsec

Since 0.4.0 the grid security code - certificates and proxies, delegation,
VOMS, credential and token discovery, TLS contexts, the test PKI - lives in
[xrdsec](https://github.com/rob-c/xrdsec), which xgfalclient and xrdarc use
too, and xrdclient depends on it (`xrdsec>=0.4.0,<0.5`; xrdsec brings
`asn1crypto`). As with xrdcore, each public old module name replaces itself
in `sys.modules` with xrdsec's module, so `xrdclient.crypto.der is
xrdsec.der`: a patch, an `isinstance` and an `except` all reach what the code
uses. xrdclient's own code imports xrdsec directly; the private aliases
(`crypto/_voms`, `_proxyfiles`, `_proxyinfo`, `testing/_https`, `_replies`,
`_keys`, `_secure_files`) were removed.

| xrdclient name | Is | What stays here |
| --- | --- | --- |
| `credentials`, `tls`, `voms_client` | `xrdsec.credentials`, `.tls`, `.voms_client` | - (the VOMS REST client now says `User-Agent: xrdsec/<version>`) |
| `crypto/der`, `rsa`, `aes`, `p256`, `ed25519`, `signatures`, `proxies`, `delegation`, `userproxy`, `trust`, `voms` | the xrdsec modules of those names | - |
| `crypto/x509` | a module of its own | re-exports `xrdsec.x509`'s public objects (its `__all__`, `certificate_data`, `CertificateData`) and keeps the Config-shaped `default_proxy_path` |
| `testing/pki`, `testing/voms` | `xrdsec.testing.pki`, `.voms` | - (`testing/http.py` also re-exports xrdsec's `FakeHTTPSServer` and its helpers) |
| `testing/http.py` `FakeDAVServer`, `testing/s3.py` `FakeS3Server` | presets of `xrdhttp.testing.StorageServer` (XrdCl style; the S3 dialect for the bucket) | the names: `files`/`dirs`, `url` as an `XRootDURL`, `seen`/`targets`/`headers`/`bodies`, `require_token`, the flat tape and S3 knobs |
| `_gridpaths`, `_tokens`, `_usercred` | modules of their own | the `Config` shape: `default_proxy_path(config)`, `discover_token(config, url)` with its own `TMP_DIR` (xgfalclient patches it), `Config.key_password` and the terminal prompt |
| `transport/base.py` | a module of its own | `tls_context(config)`, over `xrdsec.tls.client_context` (re-exported with `clear_tls_cache`, `CRL_ENV`, `SYSTEM_CA_ENV`, `KeyPassword`) |
| `auth/ztn.py` | a module of its own | the `ztn` protocol; `token_claims`/`token_expiry` are `xrdsec.tokens`' with `padding=True` |
| `auth/kerberos/asn1`, `ccache`, `kcm`, `kdc`, `keyring`, `model`, `profile`, `tgs`; `crypto/rfc3961` | the `xrdsec.krb5` modules of those names | - |
| `auth/kerberos/caches` | a module of its own | re-exports `xrdsec.krb5.caches`' objects and keeps `$XRD_KRB5_BACKEND` (`backend()`, `BACKENDS`) |
| `auth/gsi.py` | a module of its own | re-exports `xrdsec.gsi.secgsi`'s public objects (the XrdSecgsi codec, DH, seal, rounds, server check, `read_proxy`); keeps `GSICredential` (a `SecgsiClient` that is also an `auth.base.Credential`): the credential's source, `Config`/`$XRD_GSI_VERIFY_SERVER`/`$XrdSecGSIDELEGPROXY`, `available`/`missing`/`using` |
| `auth/krb5.py` | a module of its own | the `krb5` protocol: `krb5\0` framing, the service principal from the offer, `,fwd`/`fwdtgt` forwarding, the backend choice; the tickets come from `xrdsec.krb5.client`, the system library's AP-REQ from `xrdsec.krb5.gss` |

XRootD's own security protocols are here as adapters or whole: `auth/gsi.py`
(over `xrdsec.gsi.secgsi`), `auth/krb5.py` (over `xrdsec.krb5`), `auth/sss.py`, and in
`crypto/` the `blowfish` and `sigver` modules. They call xrdsec for
certificates, keys and trust, and turn its `ValueError`s (`DERError`,
`TrustError`, `DelegationError`) into `AuthenticationError`/`CredentialError`
at the point they know which server and step failed
([errors](errors.md#xrdsecs-own-errors)).

The library suite tests the moved code - the client tests that only re-tested
it were deleted, and the cases xrdsec lacked were ported into its suite
(OpenSSL's CA-directory name hashes, the strict JWT claim reading, DER and
key-export contracts). This suite tests the aliases, the adapters and the
error split (`tests/test_on_xrdsec.py`) and the cost: `import xrdclient`
loads no xrdsec module, and `Config` loads only `xrdsec.credentials`
(`tests/test_import_cost.py`).

## Outside the package: `tools/`, `benchmarks/` and xrd-devtools

Only what is about xrdclient itself stays in this repository beside `src/`:
`benchmarks/` (the paired comparison against the official bindings and its
performance gate), `tools/pyxrootd_upstream.py` (upstream's pyxrootd tests
run against `xrdclient.compat`) and `maintainability.json` (this
repository's per-function policy). What serves the whole chain - the
take-over scorecard and the maintainability check, the duplicate and
configuration-conformance gates, the release pre-flight, the platform
matrix, the RPM/DEB, Nix and Homebrew packaging and the installed-package
smoke test - moved in 0.4.0 to
[xrd-devtools](https://github.com/rob-c/xrd-devtools), with its tests and
with the reusable CI workflows and candidates action every repository calls.
It is used from a checkout, never as a dependency; [Testing](testing.md)
says how the suite finds it.

## Private names removed in 0.4.0

While xgfalclient and xrdarc were being rebased onto the libraries, every
private module they had reached into stayed importable as an alias. Both now
import the libraries (xgfalclient uses the public package names,
`xrdclient.client.{bulk,file}`, `xrdclient.session.bulk`, `xrdclient.proto`,
`xrdclient.testing` and, in its tests, `xrdclient._tokens`; xrdarc imports no xrdclient at all), so the aliases nothing uses were deleted rather than
carried as dead weight that hides which module is the real one. The policy
for the next one: an underscore name is private, so it goes as soon as a grep
of the downstream repositories (`src` and `tests`) shows no user; a public
name (documented, in an `__all__`, or a non-underscore module path) stays as
a re-export with the library object's identity.

| Removed | Use instead |
| --- | --- |
| `xrdclient._log` (and `log._RedactingFilter`) | `xrdclient.log` (`RedactingFilter`) |
| `xrdclient._xml`, `xrdclient._retry` | `xrdclient.safexml`, `xrdclient.retry` (or `xrdcore.safexml`, `xrdcore.retry`) |
| `xrdclient.cli._output` | `xrdclient.report` |
| `xrdclient.copy._pipeline` | `xrdcore.pipeline` |
| `xrdclient.http._engine`, `_connection`, `_wire`, `_ranges`, `_webdav` | `xrdhttp.engine`, `.connection`, `.wire`, `.ranges`, `.webdav` |
| `xrdclient.s3._codec` | `xrdhttp.s3.codec` |
| `xrdclient.crypto._voms`, `_proxyfiles`, `_proxyinfo` | `xrdsec._voms`, `xrdsec._proxyfiles`, `xrdsec._proxyinfo` |
| `xrdclient.crypto.x509._decode_name`, `_extensions`, `_policy_language`, `_parse_certificate` (and the copied-in private namespace) | `xrdsec.x509.name_from_element`, `tbs_extensions`, `policy_language`, `parse_certificate` |
| `xrdclient.crypto.crc32c._crc32c_py` | `xrdclient.crypto.crc32c.crc32c_py` |
| `xrdclient.testing._https`, `_replies`, `_keys` | `xrdsec.testing.https`, `._replies`, `._keys` |
| `xrdclient._secure_files.read_credential_file` / `write_credential_file` | `xrdclient.credentials.read_private_file` / `write_private_file` (`predictable=not follow_symlinks`) |
| `xrdclient._gridpaths.grid_ca_path`, `grid_voms_dir`, `default_ca_path`, `DEFAULT_CA_PATH`, `grid_directory` | `xrdclient.credentials.*` (`_gridpaths` keeps only the Config-shaped `default_proxy_path`) |
| `xrdclient._usercred.encrypted`, `load_memory`, `load_pair`, `password_supplier` | `xrdsec.usercred.*` |
| `xrdclient._cancel` | `xrdcore.cancel` |
| `xrdclient._fanout` (`run_all`, `Stopped`) | `xrdcore.fanout` |

Kept on purpose: `xrdclient._tokens` (a Config-shaped adapter the HTTP client uses, whose
`TMP_DIR` xgfalclient's `conftest.py` hermetic hook patches). `tests/test_public_modules.py` and
`tests/test_on_xrdsec.py` assert that each removed name stays gone.
