# Changelog

Notable user-visible changes are recorded here. This project follows
[Semantic Versioning](https://semver.org/); compatibility fixes which make the
client agree more closely with XrdCl are not considered breaking changes.

## [0.4.0] - Unreleased

### Security

- GSI delegation from a *limited* proxy now issues a limited proxy (it issued
  an unrestricted inheritAll one, as XrdCrypto does), and no proxy issuer
  copies a signer's `issuerAltName` or lets `subjectAltName`/`cA=TRUE` onto a
  proxy (RFC 3820 sections 3.5-3.7).
- `repr()` of a `File`, `FileSystem`, `HTTPFileSystem` or HTTP file redacts
  credentials in the URL's CGI
  (`authz=` tokens, signed-URL parameters) instead of printing them.
- Proxies, bearer-token files and SSS keytabs are read only if they are
  regular files owned by the effective uid with no group/other access
  (`0600`), checked on the open descriptor; symbolic links are refused at
  the predictable `/tmp/x509up_u<uid>`, `$XDG_RUNTIME_DIR/bt_u<uid>` and
  `/tmp/bt_u<uid>`. Violations raise the new `InsecureCredentialError`
  (a `CredentialError` and `PermissionError`) naming the file and
  `chmod 600 <path>`. The SSS keytab stat-then-open race is gone.
- GSI checks the server's certificate (CA chain and host name) on every
  login before the proxy chain is sent, not only before delegating, as
  `XrdSecgsi` clients do. `$XrdSecGSISRVNAMES` patterns allow aliases;
  `Config(gsi_verify_server=False)` / `XRD_GSI_VERIFY_SERVER=0` turns it off.
- GSI Diffie-Hellman refuses primes under 512 bits, malformed groups and
  peer values outside `(1, p-1)` (and degenerate secrets); primes under
  2048 bits are used with a warning.
- Log redaction now covers S3 pre-signed parameters (`X-Amz-Signature`,
  `X-Amz-Credential`, `X-Amz-Security-Token`, `Signature`,
  `AWSAccessKeyId`), `sig=`, EOS `cap.sym`/`cap.msg`, `access_token`,
  macaroons and whole `Authorization` header values (SigV4 included).
  `XRootDURL.__repr__` and CLI error output are redacted the same way.
- HTTP `TransferHeader*` headers no longer follow a redirect to the
  origin's parent domain when that is a public suffix (`se.ac.uk` no longer
  relays to all of `ac.uk`); EOS-style `*.cern.ch` relays are unchanged.
- Delegation's server check now requires every issuer on the path to be a CA:
  `basicConstraints` `cA:TRUE`, `keyCertSign` when `keyUsage` is present, and
  `pathLenConstraint` respected. `TrustError` carries a `code` naming the check.
- The raw (no-DigestInfo) RSA private-key operation GSI uses is now blinded.
- Redirect targets are passed through `redact()` before being logged.

### Added

- The `fsspec` extra (`pip install 'xrdclient[fsspec]'`) brings in fsspec
  for `xrdclient.fsspec_impl`; fsspec was only a development dependency, so
  nothing declared what the adapter needs.
- `xrdclient.crypto.proxies`: one X.509 proxy issuer with named profiles
  (`rfc3820`, `arc`, `xrootd`, `globus-legacy`): `issue_proxy`, `create_proxy`
  (in memory, RSA or EC, VOMS ACs embedded), `write_proxy` (atomic, `0600`,
  symbolic links refused at predictable paths), `load_credential` (private-file
  checks, encrypted keys), `describe_proxy`/`ProxyInfo` (`voms-proxy-info`'s
  fields) and `ProxyCredential.to_pem()`. `sign_proxy_request`,
  `issue_certificate` and `user_proxy` are now thin profiles of it.
- `xrdclient.voms_client`: `find_vomses`/`parse_vomses`/`servers_for` and
  `fetch_ac`, a VOMS REST (`generate-ac`) client with fail-over, typed errors
  (`VOMSRefusedError`, `VOMSUnavailableError`, `VOMSConfigError`), local AC
  validation (`VOMSWarning`) and the credential presented from memory;
  `ac_extension` builds the proxy extension.
- `xrd-proxy init|info|destroy`, in `voms-proxy-*` spellings, with
  `--json`/`--xml`/`--output-format`, before or after the subcommand as for
  `xrd-fs` (`xrd-proxy --output-format json info`). Its workflow - the user
  certificate search (`xrdsec.proxytool.VOMS_PROXY_INIT`), the VOMS
  requests, ACs before the proxy, the proxy file's path and link rule, the
  file read back - is `xrdsec.proxytool`, shared with `xrdarc proxy`;
  `cli/proxy.py` keeps the `voms-proxy-*` options and output. An expired
  user certificate is refused before any VOMS server is asked; with no user
  certificate anywhere the message says where it looked; an empty proxy
  file is reported by `info -exists` too (it was a silent exit 1).
- `credentials.write_private_file` (atomic `0600` writer, links refused at
  predictable names) and
  `tls.client_context(credential_pem=...)`, presenting a credential held only
  in memory.
- Public building blocks for tools built on xrdclient (xgfalclient, xrdarc),
  so none needs a private module (guide: `docs/building-tools.md`):
  - `xrdclient.credentials`: `find_proxy`, `default_proxy_path`,
    `find_user_credential`, `find_bearer_token` (WLCG discovery order),
    `bearer_token_files`, `grid_ca_path`, `grid_voms_dir`, `default_ca_path`
    and `read_private_file`. Each takes `environ=` (and `uid=`) instead of
    reading the process state, treats a blank variable as unset, and raises
    `InsecureCredentialError` for an unsafe file at a predictable location.
  - `xrdclient.tls.client_context(cert=, key=, ca_path=, ca_file=, verify=,
    system_ca=, check_crl=, key_password=, present_default=)` and
    `tls.clear_cache()`: the package's one cached TLS builder (stat-keyed
    reload, CRLs, system store, a separate key piped to OpenSSL). It presents
    no certificate unless asked; `tls_context(Config)` is now built on it.
  - `xrdclient.report` (was `xrdclient.cli._output`), `xrdclient.safexml`
    (was `_xml`), `xrdclient.retry` (was `_retry`) and `xrdclient.log`
    (was `_log`); the old private names were removed (see "Removed").
  - `xrdclient.retry.retry_after(headers, now=)`: an HTTP `Retry-After`
    (seconds or HTTP-date, RFC 9110 section 10.2.3) in seconds.
  - `xrdclient.log.RedactingFilter`, to redact a tool's own log records.
  - `docs/output.md` states the report's schema and versioning contract.
- One implementation of each concept shared with xgfalclient (map in
  `docs/architecture.md`; since moved to xrdhttp, see "Built on xrdhttp"):
  `DirectReader`, `send_file` and `IdlePool` (`xrdhttp.wire`), multistatus
  and `Digest` parsing (`xrdhttp.webdav`),
  `metalink.MetalinkPolicy` and `metalink.resolve`, `http.tape`
  (`TapeEndpoint`, `listed_endpoints`, `newest_endpoint`, `api_version`,
  `collapse`, `entry_for`), `http.tpc.follow_markers`/`Outcome`,
  `crypto.x509` (`certificates_in`, `read_credential_files`,
  `credential_key`, `proxy_policy`, `legacy_proxy_kind`, `signed_by`,
  `pem_encode`, the proxy-policy OIDs) and
  `crypto.delegation.parse_proxy_request`.
- `load_proxy(path, key_path=None)`: the key may be in a file of its own.
- `--token-file PATH` and `--token -` (standard input) for `xrd-fs` and
  `xrd-cp`; a literal `--token` value now warns once that it is visible in
  the process list.
- `XRD_KRB5_BACKEND=auto|gssapi|portable`: with python-gssapi installed,
  Kerberos logins go through the system library first (any cache type, DNS
  KDC discovery, cross-realm), falling back to the portable reader; ticket
  forwarding always uses the portable path.
- `fast` extra (`pip install 'xrdclient[fast]'`, i.e. `google-crc32c>=1.5`)
  for hardware-assisted CRC-32C in paged reads/writes and `crc32c` checksums;
  `xrd-fs doctor` now has a `crc32c` line saying whether it is in use.
- Encrypted private keys load: `load_private_key(..., password=...)` reads
  encrypted PKCS#8 and legacy OpenSSL `Proc-Type: 4,ENCRYPTED` keys; without a
  password the error says the key is encrypted.
- `xrdclient.crypto.signatures.verify_signature` - one signature check (RSA
  PKCS#1 v1.5 and PSS, ECDSA on any named curve with SHA-256/384/512, Ed25519,
  Ed448) for certificates, requests and VOMS ACs; `x509.verify_issued`.
- `delegation.issue_certificate`, a `CertificateBuilder`-based signer with
  xgfalclient's `build_certificate` interface; delegated proxies can now be
  signed by EC proxies (`ecdsa-with-SHA256`) and certify EC request keys.
- HTTP vector reads: `HTTPRawIO.readv()` (and fsspec `cat_ranges` over
  `http(s)://`/`dav(s)://`) asks for all ranges in one multi-range `GET` and
  parses `multipart/byteranges`, with bounded memory and validated parts;
  servers that serve one range or ignore `Range` fall back to single-range
  requests. `s3://` always uses single ranges.
- RFC 9530 digests: `checksum()` over WebDAV also sends `Want-Repr-Digest`
  and prefers a `Repr-Digest` answer (`sha-256`, `sha-512`, `adler`,
  `crc32c`, ...) over RFC 3230 `Digest`.
- Opt-in CRL checking with `XRD_CHECKCRL=1` (`leaf`) or `chain`, using the
  `<hash>.r0` files in the grid CA directory.
- `XRD_TLSSYSTEMCA=0|1` to override which endpoints also trust the system
  CA store.
- `xrdclient.s3.sigv4.presign()` for pre-signed URLs;
  `FakeDAVServer(tls=...)` for HTTPS tests, with multipart range and
  `Repr-Digest` knobs.
- Connections race a host's addresses as RFC 8305 describes (families
  interleaved, 250 ms apart, first to connect wins) within one
  `connect_timeout` that now also bounds the name lookup; a failure lists every
  address and its error. `Config.network_stack` (`$XRD_NETWORKSTACK`:
  `IPAuto`, `IPv4`, `IPv6`, `IPv4Mapped6`) picks the families.
- A request or re-open that goes back to a manager after a data server failed
  it carries `tried=<hosts>` and `triedrc=<reason>`, accumulated across
  attempts, as XrdCl sends them. A data server's `kXR_IOError`, `kXR_FSError`,
  `kXR_ServerError`, `kXR_NotFound`, `kXR_Overloaded` or `kXR_NoMemory` on a
  repeatable request a manager sent there is now retried at the manager
  (`kXR_refresh` for a missing file) within `redirect_limit`.
- `file://` redirects (XCache, disk-local storage) are followed for reading
  and stat, as XrdCl's `LocalFileHandler` does, with the same `File` API.
  `Config.local_redirect` (`$XRD_LOCALREDIRECT`): `local` (default - only
  from a server on this host), `always`, `never`. Writes are refused.
- `Config.aio_workers` (`$XRD_AIOWORKERS`, default 32): `xrdclient.aio` runs
  on a pool of its own instead of the event loop's small default executor.
- `xrdclient.aio` cancellation reaches the request: cancelling a coroutine
  (`Task.cancel()`, `asyncio.timeout`, a failed `TaskGroup` sibling) abandons
  the `root://` request its worker waits on - the worker is free within
  ~50 ms, the connection stays in use, and the late reply is dropped. Nothing
  more is sent for the call (no next copy chunk, `readv` batch or retry); a
  bulk transfer stops between frames, or closes the connection if it is
  stuck mid-frame. Over HTTP/S3 the request in flight finishes, and nothing
  after it is sent. An `open()` already under way still completes and is
  closed for you.
- `keepalive_interval` now does something: TCP keepalive idle time on control
  and data sockets (probes a third of it apart, three of them; `0` turns
  keepalive off), settable as `$XRD_TCPKEEPALIVETIME`.
- Property-based framing tests (`hypothesis`, in the `dev` extra): reply
  streams cut at arbitrary boundaries decode identically, and garbage raises
  only `ProtocolError`. `XRD_PROPERTY_EXAMPLES` scales them for a soak.
- `Config.usercert` / `Config.userkey` (`$X509_USER_CERT` / `$X509_USER_KEY`):
  a separate certificate and key, used when no proxy is named. HTTPS and
  `roots://` present them as the client certificate (the key read through
  the `0600` checks and handed to OpenSSL through a pipe); GSI logs in with a
  12-hour proxy made from them in memory (`crypto.userproxy.user_proxy`), as
  `XrdSecgsi`'s `createpxy` does. Nothing is written to disk. Encrypted keys
  are unlocked by `Config.key_password` (a callable) or the prompter.
- `Config.tls_system_ca` and `Config.check_crl`, defaulting from
  `$XRD_TLSSYSTEMCA` / `$XRD_CHECKCRL` exactly as before.
- `File.abort()`, `FileSystem.abort()` (and `HTTPFileSystem`, `HTTPClient`,
  `AsyncFile`, `AsyncFileSystem`): thread-safe, idempotent, non-blocking; a
  call blocked on the handle's connection wakes at once and it and every
  later call raise the new `AbortedError` (`OSError`, `errno` `ECANCELED`).
  `Router.abort()` underneath.
- `third_party(..., cancel=, progress=, delegate=, streams=)`: a pull can be
  called off as XrdCl does (`ofs.tpc cancel` to the destination, then both
  handles closed), watched (bytes at the destination), delegated with TPC
  lite (`tpc.dlgon=1`, `tpc.scgi`, `tpc tpcdlg` probe) and given parallel
  streams (`tpc.str`); `copy.tpc.failed_end(exc)` says which end refused.
- `cancel=` on `client.bulk.download` / `stream`, and the new
  `client.bulk.upload`: asked per piece, and the handles in flight are
  aborted when it fires.
- `File.write_pieces(pieces, offset, acknowledged=, waited=, check=)`: the
  bulk plane's pipelined writer as a public API, with a serial fallback.
- `errors.errno_for(kxr)` / `errors.kxr_for(errno)` and the `KXR_ERRNO`
  table (`XProtocol::toErrno` / `mapError`); the compat copy process uses them.
- `xrdclient.BaseFileSystem`, the abstract base of the `root://`, WebDAV and
  S3 filesystems; the shared helpers (walk, glob, rmtree, list_archive,
  whole-file read/write, ...) live there once.
- `FileSystem.prepare(..., cancel=True, reqid=...)` and
  `cancel_prepare(handle, paths)`: withdraw a whole staging request or some
  of its files (`kXR_cancel`, request id first; over WebDAV the tape API's
  per-file cancel). The compat `prepare(..., CANCEL)` passes the files on.
- `FileSystem.dirlist(path, stat=True)`: the raw `kXR_dirlist` answer for
  callers that parse listings themselves.
- fsspec `open()` honours `cache_type`/`cache_options` through
  `fsspec.caching` (any named cache; unset keeps the `io` fast path); an
  unknown cache type raises `ValueError`. `docs/fsspec.md` explains the
  `root://` entry-point clash with fsspec-xrootd and how to pick one.
- `xrdclient.fsspec_impl.register_fsspec()`, moved here from xrdwms's
  `staging` module: registers this package's filesystem classes for every
  protocol its `fsspec.specs` entry points declare (`clobber=True`), so
  `root://` is this package's whatever else is installed; returns the
  protocols. xrdwms's dask layer calls it on the client and every worker.
- `Config.tpc_timeout` (`$XRD_CPTPCTIMEOUT`, XrdCl's `CPTPCTimeout`, default
  1800 s) bounds a `root://` third-party copy's pull; the destination's
  connection is given at least that long for `request_timeout`,
  `stall_deadline` and `wait_budget`. `third_party(timeout=...)` overrides it.
- `third_party(..., cleanup=False)` (also `aio` and `http.third_party`): gfal2's
  `transfer_cleanup` off - a failed copy's empty destination, and a
  mismatched one, are left to the caller.
- `xrdclient.failed_end` (and `xrdclient.copy.failed_end`): which end of a
  failed `root://` third-party copy refused - reachable now that
  `xrdclient.copy` is the copy function, not the subpackage.
- `root://` third-party copy runs XrdCl's capability checks: `tpc tpcdlg` at
  the destination for every copy (not only a delegated one) and `tpc` at the
  source's data server before a classic pull; a destination open refused with
  "tpc not supported" is `UnsupportedError`, as XrdCl reports it.
- Shared, public test fixtures (semver-covered, documented in
  `docs/testing.md`): `xrdclient.testing.pki` (throwaway grid PKI - CA
  directory with subject hashes and signing policies, host/user/proxy
  certificates of every kind, CRLs, TLS contexts - and the eight frozen test
  keys as `cryptography` keys via `test_key()`/`test_key_pem()`),
  `xrdclient.testing.voms` (`make_ac`/`ACSpec` VOMS AC builder, `vomsdir`/
  `vomses` helpers, `voms_proxy`, and `FakeVOMSServer` for VOMS REST
  `/generate-ac`) and `xrdclient.testing.http.FakeHTTPSServer` (route
  table, request log, faults, client identities) with `ranged_reply`,
  `propfind_reply` and `dav_entries`. xgfalclient and xrdarc now build on
  them instead of carrying copies.
- `xrdclient.testing.brix`: `BrixProxy` and `BrixFaultFS`, the harnesses
  for the external BRIX fault proxy and FUSE fault filesystem (an absent
  tool raises `unittest.SkipTest`), tested against stand-in tools. They were
  an identical private copy in this suite and xgfalclient's.
- `xrdclient.testing.brix` local-copy conformance suites: `WriteFaultSuite`,
  `DurabilitySuite`, `SourceFaultSuite` (all three: `LocalCopySuite`), run
  by subclassing with a `LocalCopyAdapter` that copies one local file to
  another through a client's own API, plus the neutral `Kind`,
  `kind_of_errno`/`ERRNO_KINDS` and the `POLICIES` they mount. The thirteen
  BRIX FUSE fault scenarios (short/zero-progress/torn/dropped writes, early
  and late `ENOSPC`, dishonest and failing `fsync`, stale and replaced
  sources, a lying size, stable storage) were written out twice, here and in
  xgfalclient; both now run this one suite, keeping only their own error
  classes and wording as overrides. The module still imports no pytest; the
  suites are pinned without FUSE by `tests/test_testing_brix_suites.py`
  against a scripted filesystem and client, including clients the suites
  must fail.

### Changed

- **Built on xrdcore.** The dependency-free shared core moved to the new
  `xrdcore` package, now a dependency (`xrdcore>=0.4.0,<0.5`;
  `xrdclient[fast]` pulls in `xrdcore[fast]`). What moved where:
  `xrdclient.safexml` → `xrdcore.safexml`; `xrdclient.retry` →
  `xrdcore.retry`; `xrdclient._cancel` → `xrdcore.cancel`; the copy pump →
  `xrdcore.pipeline`;
  `xrdclient.crypto.checksum` and `.crc64` → `xrdcore.checksum` and
  `xrdcore.crc64`; the CRC-32C itself → `xrdcore.crc32c` (the 4 KiB page
  framing `pack_pages`/`unpack_pages`/`page_span` stays in
  `xrdclient.crypto.crc32c`, which re-exports `crc32c`, `crc32c_py` and
  `IS_ACCELERATED`); `xrdclient.report` →
  `xrdcore.report` (`xrdclient.report` registers `XRootDURL` so reports print
  it as a string, and `xrdclient.cli.dumps` is `xrdcore.report.dumps`);
  redaction and muting → `xrdcore.log` (`xrdclient.log` keeps the
  `xrdclient.` logger hierarchy). Every public old name still imports, and
  except for `xrdclient.log` and `xrdclient.crypto.crc32c` it is the xrdcore
  module object itself; the private ones were removed (see "Removed").
- **Built on xrdhttp.** The HTTP protocol pieces moved to the new `xrdhttp`
  package, now a dependency (`xrdhttp[s3]>=0.4.0,<0.5`); `botocore` is no
  longer a direct dependency - it comes with xrdhttp's `s3` extra and is
  still imported only when S3 is used. What moved where: the request engine → `xrdhttp.engine`;
  connections → `xrdhttp.connection`; `http.expect` → `xrdhttp.expect`;
  socket I/O → `xrdhttp.wire`; multi-range reads → `xrdhttp.ranges`;
  `PROPFIND` parsing → `xrdhttp.webdav`; `s3.sigv4` → `xrdhttp.s3.sigv4`;
  S3's XML → `xrdhttp.s3.codec` (`http.expect` and `s3.sigv4` are the
  xrdhttp module objects; the private `http._*` and `s3._codec` names were
  removed, see "Removed"); `metalink` → `xrdhttp.metalink`
  (every name is xrdhttp's object; `xrdclient.ChecksumInfo` is now
  `xrdcore.checksum.ChecksumInfo`, the type xrdhttp answers too, so the old
  checksum-converting `parse_metalink` wrapper is gone); the Tape
  REST vocabulary and answer reading → `xrdhttp.tape` (`xrdclient.http.tape`
  keeps its `(client, url, ...)` verbs answering `PrepareStatus`); the
  HTTP-TPC header and token rules and the performance-marker reader →
  `xrdhttp.tpc` (`xrdclient.http.tpc.third_party` keeps the macaroon mint,
  the cleanup and the error mapping; `Marker`, `Outcome` and
  `follow_markers` are xrdhttp's); the redirect credential policy →
  `xrdhttp.redirects` (`xrdclient.http.client.carries_credentials`,
  `relays_transfer_headers` and `wire_path` keep their names and
  signatures). TLS contexts are still built here and handed to xrdhttp's
  connections. See docs/architecture.md, "Built on xrdhttp".
- **The HTTP client is a policy over `xrdhttp.session`.** `HTTPClient`'s
  pool, stale-connection retry, redirect loop, `Expect: 100-continue`,
  chunked upload and response bookkeeping are xrdhttp's shared `Session`
  (also xgfalclient's and xrdarc's); `http/client.py` keeps the credentials
  (token discovery, the `401` prompt, the S3 signer, the TLS context),
  where they may follow a redirect, and the `kXR_*` status table (934 ->
  646 lines, most of them documentation). `HTTPClient` is now thread-safe. `HTTPClient.open` returns an
  `xrdhttp.session.Response` (`read`, `readinto` straight off the socket,
  `readline`, `header`, `close` - which reads out a short rest to keep the
  connection, as `abandon` did); `begin_upload` returns an
  `xrdhttp.session.Upload` (`write`, `finish`) instead of a raw connection.
  `close()` drops idle connections and the client stays usable, as before;
  `idle_count()` is new. Retries: a repeatable request (`GET`, `HEAD`,
  `PUT`, `DELETE`, `OPTIONS`, `PROPFIND`, `MKCOL`, `MOVE`, `COPY`, with no
  `If-*` condition) is still sent once more, at once, after a connection
  failure (a timeout is not repeated); **new:** one answered `429` or `503`
  is retried once, after its `Retry-After` (capped at `Config.wait_cap`),
  or at once without one. A stale pooled connection now counts as that
  connection failure. A body cut short is a `TransientError` wherever it is
  read, and a ranged read resumes it.
- A relative HTTP `Location` keeps the scheme it was reached by: a redirect
  from `davs://` stays `davs://` (it became `https://`, and so trusted the
  system CAs and took another pool slot), and a network-path one
  (`//other.host/x`) no longer carries the URL's user name to the other host.
- **Errors joined with the libraries.** `XRootDError` *is*
  `xrdcore.errors.XrdError`, and `ProtocolError`, `MetalinkError`,
  `ConnectionError`, `TransientError`, `TimeoutError`, `AuthenticationError`,
  `CredentialError`, `TokenExpiredError` and `InsecureCredentialError` are the
  xrdcore classes of those names, so `except xrdclient.errors.X` catches what
  xrdcore, xrdsec and xrdhttp raise. XRootD's own errors subclass them as
  before. `XRootDError.__name__` is now `"XrdError"`;
  `InsecureCredentialError.path` now survives pickling. See docs/errors.md.
- The copy pump's debug and warning messages are logged as
  `xrdcore.pipeline`, not `xrdclient.copy._pipeline`.
- **Built on xrdsec.** The grid security code moved to the new `xrdsec`
  package, shared with xgfalclient and xrdarc and now a dependency
  (`xrdsec>=0.4.0,<0.5`; `asn1crypto` is no longer a direct dependency, it
  comes with xrdsec). What moved where: `xrdclient.credentials`, `.tls` and
  `.voms_client` → `xrdsec.credentials`, `.tls`, `.voms_client`;
  `xrdclient.crypto.der`, `.rsa`, `.aes`, `.p256`, `.ed25519`, `.signatures`,
  `.proxies`, `.delegation`, `.userproxy`, `.trust` and `.voms` (with their
  private helpers) → the xrdsec modules of those names; the TLS
  context builder in `transport/base.py` → `xrdsec.tls.client_context`
  (`tls_context(config)` stays, built on it); the strict JWT claim reading
  of `auth/ztn.py` → `xrdsec.tokens` (`token_claims`, `token_expiry`, used
  with `padding=True`); `xrdclient.testing.pki`, `.voms` and the
  `FakeHTTPSServer` behind `xrdclient.testing.http` → `xrdsec.testing.pki`,
  `.voms`, `.https`. Every public old name still imports and is xrdsec's
  module object itself, except `xrdclient.crypto.x509`, which re-exports
  xrdsec's public objects and keeps the Config-shaped `default_proxy_path`;
  the private ones were removed (see "Removed"). The Config-shaped adapters
  `_gridpaths.default_proxy_path`, `_tokens` (with its own `TMP_DIR`) and
  `_usercred` stay, over xrdsec. XRootD's own GSI, Kerberos, `sss` and `ztn` handshakes and the
  `kXR_sigver` signer stay in xrdclient. Visible differences: the test PKI's
  default subjects read `DC=xrdsec` (`xrdsec Test CA`), not `DC=xrdclient`;
  the VOMS REST client sends `User-Agent: xrdsec/<version>`; and the TLS
  builder logs as `xrdsec.tls`, not `xrdclient.transport`. xrdsec's
  `DERError`, `TrustError` and `DelegationError` remain `ValueError`s, which
  the GSI login turns into `AuthenticationError`/`CredentialError` as before;
  its credential errors are xrdcore's, so they are `XRootDError`s already
  (see docs/errors.md).
- **Kerberos is xrdsec's.** The pure-Python Kerberos client moved to
  `xrdsec.krb5`, where xgfalclient shares it with one GSS-API layer:
  `xrdclient.auth.kerberos.asn1`, `.ccache`, `.kcm`, `.kdc`, `.keyring`,
  `.model`, `.profile`, `.tgs` and `xrdclient.crypto.rfc3961` are now the
  `xrdsec.krb5` modules of those names (same module objects);
  `auth.kerberos.caches` re-exports `xrdsec.krb5.caches` and keeps
  `$XRD_KRB5_BACKEND` (`backend()`, `BACKENDS`); the cache-reading, KDC and
  forwarding logic of `auth/krb5.py` is `xrdsec.krb5.client`
  (`service_credentials`, `tickets`), and its python-gssapi AP-REQ is
  `xrdsec.krb5.gss.initiator_ap_req`. `auth/krb5.py` keeps XRootD's part -
  the `krb5\0` framing, the principal from the offer, the `,fwd`/`fwdtgt`
  handshake, the backend choice and the `CredentialError` wording - and its
  public names (`KerberosCredential`, `tickets`, `read_ccache`, `Principal`,
  `Ticket`, `default_ccache_path`, `service_principal`, `CCACHE_VERSION_3`/`4`,
  `DEFAULT_ENCTYPES`) as re-exports. `auth.kerberos.caches.open_ccache` no
  longer checks `$XRD_KRB5_BACKEND` (the login still does). The `krb5` extra
  is now `xrdsec[krb5]` (which brings python-gssapi). A refused macOS `API:`
  cache now says "Install python-gssapi (the 'krb5' extra)". The Kerberos
  library tests moved to xrdsec's suite; the MIT/`xrootd` interoperability
  tests stay here, and `tests/_krb5kdc.py` regenerates xrdsec's captures.
- **GSI is xrdsec's.** The XrdSecgsi protocol of `auth/gsi.py` - the bucket
  codec, Diffie-Hellman and its group checks, the AES session, both client
  rounds, delegation, the login-time server check and `read_proxy` - moved
  to `xrdsec.gsi.secgsi` (about 900 lines), beside the Globus GSI GSS
  mechanism xgfalclient shares (`xrdsec.gsi.gss`). `auth/gsi.py` keeps the
  adapter: `GSICredential` is now an `xrdsec.gsi.secgsi.SecgsiClient` that is
  also an `auth.base.Credential` (same constructor, attributes and rounds),
  adding where the credential comes from, the `Config` and environment
  settings (`gsi_delegate`, `gsi_verify_server`/`$XRD_GSI_VERIFY_SERVER`,
  which the refusal still names) and `available`/`missing`/`using`. Every
  protocol name it published is re-exported as xrdsec's object, plus
  `SecgsiClient`, `choose_digest` and `client_ca_hashes`. The protocol tests
  moved to xrdsec's suite, played against `xrdsec.testing.gsi.SecgsiServer`
  (the server side the tests had built by hand); the adapter's stay here.
  `crypto.delegation` and `crypto.x509` re-export xrdsec's new
  `make_proxy_request`, `sign_globus_request` and `split_der_certificates`.
  The weak-group warning now logs as `xrdsec.gsi.secgsi`.

- `ProxyCredential.identity` is the end-entity certificate's subject when
  the leaf is a proxy and the chain has one (previously: trailing
  numeric/`proxy` CNs stripped, which could strip a real numeric CN);
  otherwise the proxy CNs are stripped as before, now including
  `limited proxy` and XrdCrypto's negative serials.
- `check_vomses` also refuses an unsafe VO name in the fifth field, and its
  message names the fields in their real order
  (`"ALIAS" "HOST" "PORT" "SERVER DN" "VO"`).
- `xrdclient.aio` is precisely typed: `AsyncFile` is generic
  (`aio.open(url)` gives `AsyncFile[bytes]`, a text mode `AsyncFile[str]`),
  `iterdir`/`walk`/`glob` yield typed items, and `copy`, `copy_tree` and
  `third_party` spell out the synchronous keywords instead of `**kwargs: Any`.
  Every forwarded call is checked against the synchronous signature
  (`ParamSpec`). `aio.open` gains `member=`.
- The copy read-ahead thread runs in the caller's `contextvars` context, so a
  `deadline()`, `config.override()` or cancellation reaches its reads too.
- HTTP reads of at least 64 KiB of a known-length body go straight to the
  socket (one TLS record per call, without http.client's per-record
  wrappers), as xgfalclient's already did; a body cut short still raises
  `TransientError`.
- WebDAV `Digest` names are matched in canonical form (`sha`, `SHA-1` and
  `sha1` are one algorithm); `getlastmodified`/`creationdate` values with
  surrounding space parse.
- Tape REST discovery picks the newest endpoint up to `v1` by gfal2's
  version rule (`V1`, `1` and `v1.2` count as v1) before falling back to the
  first listed.
- An HTTP third-party copy's `aborted` outcome line fails the copy at once
  instead of waiting for the connection to close; marker lines are matched
  by prefix, as davix matches them.

- Bearer-token discovery is one implementation for `root://`, `davs://` and
  HTTP-TPC, in WLCG order: URL token, `Config.token`, an explicitly set
  `Config.token_file`, `$BEARER_TOKEN`, `$BEARER_TOKEN_FILE`,
  `$XDG_RUNTIME_DIR/bt_u<euid>`, `/tmp/bt_u<euid>`. `davs://` previously read
  the file before `$BEARER_TOKEN` and never looked in the last two places.
- SSS on a FIPS host (or OpenSSL without the legacy provider) fails with an
  explanation instead of `UnsupportedAlgorithm`, and the ladder moves on.

- Python 3.11 is now the minimum (tested on 3.11-3.14), up from 3.10 in
  0.3.1. AlmaLinux 8/9 use the AppStream `python3.11`/`python3.12` packages.
- `import xrdclient` is lazy (PEP 562): public names and subpackages load on
  first use. A bare import now loads only the version module rather than
  ~230 modules, including `ssl`, `cryptography.x509` and `asn1crypto`.
  `xrdclient.copy`, `xrdclient.url`, `xrdclient.errors` and
  `xrdclient.report` are cheap to import on their own too.
- The TLS context builder no longer imports the X.509 stack to find the proxy
  file. CA-directory and proxy discovery now live in one place; a blank
  `X509_CERT_DIR` counts as unset everywhere, as it already did for VOMS.
- JWT claims for token-expiry checks are decoded with the standard library.
  Decoding stays strict: malformed base64url, non-object JSON or a detached
  payload give no claims, as before.
- RSA key loading, digest signatures, verification, public "decrypt" and key
  generation go through `cryptography`. Malformed keys now fail with
  "unreadable private key: ..." instead of field-by-field DER messages.
- `RSAPrivateKey.generate` uses OpenSSL and requires at least 1024 bits.
- Pure-Python CRC-32C and CRC-64 fallbacks are roughly 1.5-2x faster
  (slice-by-eight over `struct.iter_unpack`).
- Kerberos CTS uses one `cryptography` CBC context instead of one ECB context
  per block.
- `ProxyRequest.public_key` is `None` for a non-RSA request key (the SPKI is
  always in `spki`).
- HTTP connections are the standard library's `http.client` connections
  (SNI, hostname checking and timeouts unchanged). The TLS context is now
  cached per setup and shared across connections and `roots://`, and rebuilt
  when the proxy, CA file or CA directory changes on disk.
- On a grid node, `https://` and `s3://` endpoints trust the system CA store
  in addition to the grid CA directory; `davs://` and `roots://` keep to the
  grid CAs. Pooled connections are not shared across the two trust levels.
- XML responses (PROPFIND, S3, metalink) are parsed in one pass, with
  DOCTYPE/entity declarations still refused.
- SigV4 signing uses only botocore's public signing steps (signatures are
  unchanged); the S3 service model is loaded from botocore's bundled data
  without creating a botocore `Session`.
- Every retry backoff (reconnect, re-open, copy resume, bulk recovery, HTTP
  reads) uses one helper: exponential, capped, deadline-aware, with full
  jitter.
- The copy read-ahead reuses its buffers by default instead of allocating one
  per chunk; a caller-supplied destination stream still gets fresh buffers.
- A `Session` no longer holds its lock for a request's lifetime: requests from
  any number of threads (and `xrdclient.aio` calls) are in flight on one
  connection together. One waiter at a time reads each link and files every
  reply with the request it answers; the rest sleep until theirs arrives. A
  `kXR_wait` is sat out, and a `kXR_waitresp` answer waited for, without
  blocking the other requests on the session, and `close()` cuts either wait
  short. `on_chunk` callbacks run on the caller's thread holding nothing.
- A link that fails now fails every request waiting on it, each with its own
  copy of the error, not only the one that happened to be reading.
- A bulk read or write takes the connection explicitly (its reader role and
  send lock); other requests on the session wait their turn within their
  own deadline, and the bulk reader's own thread gets a `ProtocolError` if it
  tries to use the session behind its back.
- Writes are no longer copied on the way out: the event path and the bulk
  plane both send the request header and a view of the caller's buffer in
  one `sendmsg` (record-sized slices on TLS, whose sends and receives are
  serialised per link). `Write` keeps a `bytearray`/`memoryview` payload as a
  view; new `encode_parts`, `SessionMachine.buffers_to_send`/
  `path_buffers_to_send`/`frame_parts_for`, and `Transport.send_buffers`/
  `wait_readable`/`wait_writable`/`interrupt` (with defaults) carry it.
- The bulk reader turns a `kXR_error` into an exception through the same
  function as the event path, so an error with code 0 is now the same
  `ServerError` on both paths (it was a `ProtocolError` on the bulk plane).
- Internal modules reorganised for navigation (`copy/engine.py`,
  `crypto/voms.py`, `testing/server.py`, `proto/requests.py`,
  `session/router.py` split into private submodules; map in
  `docs/architecture.md`); public import paths unchanged.
- `HTTPFileSystem` is a sibling of the `root://` `FileSystem` under
  `BaseFileSystem` rather than its subclass; it is registered as a virtual
  subclass, so `isinstance(fs, FileSystem)` is unchanged, and it no longer
  inherits `root://` wire code. `S3FileSystem` still derives from it.
- `client/file.py` is split into private modules (`_handle`, `_reading`,
  `_writing`, `_vector`); `xrdclient.client.file` exports what it did.
- A `root://` third-party copy now holds the source's keyed handle until the
  pull is over and closes the destination's handle on any failure after its
  open, as XrdCl does; `tpc.src`/`tpc.dlg` are XrdCl host ids
  (`[user@]host:port`, IPv6 bracketed) and `tpc.spr`/`tpc.tpr` the real schemes.
- The compat copy process maps a local end's `errno` with the full
  `XProtocol::mapError` table (it knew ten numbers).
- `FakeDAVServer` and `FakeS3Server` are thin presets of xrdhttp's shared
  fake storage element, `xrdhttp.testing.StorageServer` (XrdCl style; the
  same server xgfalclient's suite uses): `testing/http.py` and
  `testing/s3.py` keep only the names - `files`/`dirs`, `url` as an
  `XRootDURL`, `seen`/`targets`/`headers`/`bodies`, `require_token`, the flat
  tape knobs (`nearline`, `staged`, `cancelled`, `released`, `no_tape`,
  `sitename`) and S3 knobs (`forbidden`, `complete_fails`, `failing_parts`,
  ...). Every `StorageServer` switch now works on them too (`fault(...)`,
  `tokens`, `redirect_prefixes`, `metalinks`, `qos`, ...). The reply helpers
  `testing.http` re-exports (`ranged_reply`, `propfind_reply`,
  `multistatus`, `dav_entries`, `DAVEntry`, `BOUNDARY`) are xrdhttp's now;
  `FakeHTTPSServer`, `Request`, `Response` and `Fault` stay xrdsec's. Differences a
  test can see: `digests` is the set of algorithms offered (`set()` for none,
  was `False`); `seen` and friends are views of `requests` (clear them with
  `clear()`); a `401` carries `WWW-Authenticate: Bearer`; the discovery
  document's endpoint stays `.../api/v1`; markers are tab-indented, as
  dCache sends them; the S3 fake also answers ListObjects v1, refuses a
  method it does not know with `405 MethodNotAllowed`, and creates a bucket
  on `PUT /bucket`. A suffix range (`bytes=-N`) is the last N bytes, a
  malformed `Range` is answered `200` with the whole file, and chunk trailers
  are consumed.

### Removed

- `HTTPClient`'s private machinery, now `xrdhttp.session`'s: the methods
  `connection`, `ready`, `_connect`, `_discard`, `_forget`, `_release`,
  `_once`, `_next_hop`, `_send_expecting`, `_check_aborted`, `_aborting`,
  `headers_for`, `sign`, `_carries_credentials`, and the `_pool`/`_serving`
  fields; `abandon(response)` and `reader(response)` (use
  `response.close()` and the response itself); the module's `_redirected`,
  `_key`, `_checked`, `_drain`, `_redirect`, `_wrap`, `DRAIN_LIMIT` and
  `_TOKEN_FIELDS` (`xrdclient._tokens.TOKEN_FIELDS`).
- The WebDAV and S3 fakes' private implementation (`testing.http._Handler`,
  `_Server`, `_fetch`, `_digest_header`, ...; `testing.s3._Handler`,
  `_listing_page`, ...): it is `xrdhttp.testing`. Their fake-only test cases
  moved to xrdhttp's suite (`tests/test_testing_*.py`).
- The private `xrdclient._fanout` (the thread fan-out behind the bulk
  download, the parallel copy and the multi-replica reader): it is
  `xrdcore.fanout` (`run_all`, `Stopped`), which xgfalclient's ranged HTTP
  download and xrdarc's `get` now use too. `xrdclient.errors` reduces its
  exceptions through `xrdcore.errors.rebuild` (public; was `_rebuild`).
- `XRD_KRB5_BACKEND=native` and the pykrb5 cache reader behind it
  (`xrdclient.auth.kerberos.native.NativeCache`), and pykrb5 (`krb5`) from
  the `krb5` extra: use python-gssapi, which reaches the same caches - leave
  `XRD_KRB5_BACKEND` unset (`auto`) or set it to `gssapi`. `native` is now a
  `CredentialError` that says so.
- The private helpers of `xrdclient.auth.krb5` that are now
  `xrdsec.krb5.client`'s or `xrdsec.krb5.gss`'s: `_FETCHED` (use
  `xrdsec.krb5.client.clear_cache()`), `_target` (`target_principal`),
  `_etypes` (`request_enctypes`), `_expired`, `_tgt`, `_service_ticket`,
  `_forwarded_tgt` (all inside `service_credentials`), `_unwrap_ap_req`
  (`xrdsec.krb5.gss.unwrap_ap_req`) and the `_Reader` re-export
  (`xrdsec.krb5.ccache._Reader`).
- `Config.stream_timeout` and `$XRD_STREAMTIMEOUT`, which never had an
  effect: a silent connection is bounded by `request_timeout`
  (`$XRD_REQUESTTIMEOUT`), an idle pooled one by `pool_idle_ttl`. The
  pyxrootd layer's `StreamTimeout` setting stays and maps to
  `request_timeout`.
- `crypto.Blowfish`'s ECB and single-block methods (`encrypt_ecb`,
  `decrypt_ecb`, `encrypt_block`, `decrypt_block`); SSS only ever used CFB64.
- The `PyJWT[crypto]` runtime dependency.
- Python 3.10 support (3.9 was dropped from the metadata in 0.3.1) and the
  3.9-syntax rule the code kept until now, along with the private
  `xrdclient._compat` shims (`SLOTS`, `TIMEOUTS`, `zip_strict`,
  `flag_members`).
- `AES.rounds` (unused) and the pure-Python Miller-Rabin prime search behind
  `RSAPrivateKey.generate` (private `_prime`, `_probably_prime` and helpers).
- The `urllib3` runtime dependency (it may still arrive through botocore).
- The direct `botocore` runtime dependency (it arrives through xrdhttp), and
  private helpers that moved there with their logic:
  `http.client._public_suffix`, `_in_domain`, `_without_credentials`,
  `_url_token` and the `_PUBLIC_SUFFIXES` table (now
  `xrdhttp.redirects.public_suffix`, `in_domain`, `without_credentials`,
  `query_token`); `http.tape._flag`, `_document`, `_entries`, `_ordered`
  and `_locality` (`xrdhttp.tape.flag`, `parse_document`, `entries`,
  `locality`); `http.tpc._CopyOptions`, `_copy_headers`, `_add_*`,
  `_verdict`, `_absorb`, `_marker` (`xrdhttp.tpc.CopyOptions`,
  `copy_headers`, `add_*`, and the marker reader's own helpers).
- **Removed private compatibility paths.** While xgfalclient and xrdarc were
  rebased onto the libraries, the private modules they used to import were
  kept as aliases. Neither imports them any more, and xrdclient's own code
  now imports the libraries, so they are gone (an underscore name was never
  API; the public names listed under "Changed" all stay):
  - `xrdclient._log` → `xrdclient.log` (`log._RedactingFilter` →
    `log.RedactingFilter`);
  - `xrdclient._xml` → `xrdclient.safexml`; `xrdclient._retry` →
    `xrdclient.retry`;
  - `xrdclient.cli._output` → `xrdclient.report`;
  - `xrdclient.copy._pipeline` → `xrdcore.pipeline`;
  - `xrdclient.http._engine`, `._connection`, `._wire`, `._ranges`,
    `._webdav` → `xrdhttp.engine`, `.connection`, `.wire`, `.ranges`,
    `.webdav`; `xrdclient.s3._codec` → `xrdhttp.s3.codec`;
  - `xrdclient.crypto._voms` (and its submodules), `._proxyfiles`,
    `._proxyinfo` → `xrdsec._voms`, `._proxyfiles`, `._proxyinfo`;
  - `xrdclient.crypto.x509._decode_name`, `._extensions`,
    `._policy_language`, `._parse_certificate` and the rest of the private
    namespace copied from xrdsec (`_canonical_value`, `_certificate_key`,
    ...) → `xrdsec.x509.name_from_element`, `tbs_extensions`,
    `policy_language`, `parse_certificate`;
  - `xrdclient.crypto.crc32c._crc32c_py` → `crc32c_py`;
  - `xrdclient.testing._https`, `._replies`, `._keys` →
    `xrdsec.testing.https`, `._replies`, `._keys`;
  - `xrdclient._secure_files.read_credential_file` /
    `write_credential_file` (`follow_symlinks=`) →
    `xrdclient.credentials.read_private_file` / `write_private_file`
    (`predictable=not follow_symlinks`);
  - `xrdclient._gridpaths.grid_ca_path`, `grid_voms_dir`,
    `default_ca_path`, `DEFAULT_CA_PATH` and `grid_directory` →
    `xrdclient.credentials.*` (`_gridpaths` keeps the Config-shaped
    `default_proxy_path`);
  - `xrdclient._usercred.encrypted`, `load_memory`, `load_pair`,
    `password_supplier` → `xrdsec.usercred.*`;
  - `xrdclient._cancel` → `xrdcore.cancel`.

### Fixed

- A stat, open or directory-listing reply whose stat line has a non-numeric
  field raises `ProtocolError` instead of leaking `ValueError` (found by the
  new property-based framing tests).
- EC- and EdDSA-signed CA chains verify, in delegation trust and in VOMS
  issuer chains (previously only `sha*WithRSA` was accepted).
- A certificate with a duplicated extension is decoded (and refused as an
  issuer) instead of raising out of `load_certificates`.
- `load_proxy` of an EC proxy returns the key rather than failing, so GSI says
  "the key is not RSA".
- `s3://` and public `https://` endpoints failed certificate verification on
  machines with `/etc/grid-security/certificates`.
- A `Router` collected without `close()` no longer closes its connection from
  inside the garbage collector, where it could deadlock on the pool's lock or
  block on a `kXR_endsess`: the connection is queued and released by the next
  pool operation or a small reaper thread.
- A malformed numeric `XRD_*` variable (`XRD_REQUESTTIMEOUT=5m`) is reported
  with a warning naming it, its value and the default used, once per process.
- One file shared by threads, or by `asyncio.gather` over `pread`, no longer
  tears its readahead or data-stream state, and can no longer be re-opened
  twice (leaking a connection) after a lost server.
- A cancelled `xrdclient.aio.open()` closes the handle the open produced anyway.
- A failing span of a parallel copy, a bulk download or a multi-source copy
  now stops its siblings at their next chunk; the first error is raised with
  its own type and the others listed in a note on it. Workers run in the
  caller's `contextvars` context, so a `deadline()` reaches them.
- Leaving a read-ahead pipeline early waits a bounded time for its reader
  (closing a source blocked in a network read) instead of up to the stall
  deadline.
- Compat `XRootD.client.File`/`FileSystem` and the fsspec filesystem no
  longer close from inside the garbage collector, which could deadlock on
  the session pool's lock; dropped objects are queued and closed on an
  ordinary thread, so a dropped writer is still committed.
- `HTTPFileSystem.hardlink()` / `S3FileSystem.hardlink()` raised
  `AttributeError` instead of `UnsupportedError`.
- `root://` third-party copy dropped the URLs' CGI: `authz=` tokens, space
  tokens and `XRD.*` options now reach the placement, keyed source and
  destination opens, with the `tpc.*` fields added to them (`xrdcl.*` stays
  client-side).
- A refused `root://` third-party copy no longer removes an empty destination
  it did not create (one that already existed and refused `overwrite=False`),
  and a clean-up after a failure or checksum mismatch uses the destination's
  settings, not the source's.
- `failed_end()` no longer names the source for a failed placement open,
  where XrdCl names no end.
- HTTP third-party copy no longer removes an empty destination that was
  already there when `Overwrite: F` was refused with 412.
- `root://` third-party copy creates the destination 0644 (XrdCl's
  `UR|UW|GR|OR`) rather than 0600, and fails the copy - naming the end - when
  the source's or destination's close after the pull fails, as XrdCl does
  (the source's close used to be ignored).
- The bulk data plane reported a peer-closed connection as `TimeoutError`; it
  is now `ConnectionError`, as on the event path (same message), so a copy
  retries it as a dropped link.

### Code health

- `cli.fail` is `xrdcore.cli.Fatal` (the one-line report xrdarc and xrdwms
  end with), and `cli.configure_logging` is `xrdcore.log.configure` on the
  root logger: the same scale, format and stream, now redacting every
  record it writes (xrdsec's and xrdhttp's included) and replacing its
  handler when run again instead of doing nothing after the first time
  (`logging.basicConfig`). It returns what it configured. Under `--json`
  an error line is one `message` record instead of two. The exit codes stay
  0/1/2 and a bug still ends in a traceback: `xrdcore.cli.run`'s 130/141 are
  not adopted, by the contract in `cli/__init__`.
- No grandfathered code: ruff runs the full rule set with `SIM` and no
  ignores or per-file ignores; mypy `--strict` checks src, tests, tools,
  benchmarks and examples (tests may leave fixtures unannotated, but their
  bodies are checked); complexipy gates cognitive complexity at 15 with no
  snapshot; coverage is 100 % line and branch over the whole package, with
  no `include` list and no `omit`; pytest turns every warning into an error.
  Suppression comments are gone except the few that cannot be fixed (see
  `docs/maintainability.md`). `xrdclient.credentials`, `tls`, `report` and
  the other library aliases re-export their names explicitly instead of
  through `import *`; `tests` is a package; fsspec gets local stubs
  (`typings/fsspec`).
- Banned APIs (ruff `TID251`, listed and explained in CONTRIBUTING.md):
  what xrdcore, xrdsec and xrdhttp own is no longer done here directly, in
  `src`, `tests`, `tools` or `examples`. The tree copy and the pyxrootd
  `CopyProcess` jobs fan out through `xrdcore.fanout.run_all` (later
  failures are noted on the first; contextvars reach the jobs); the
  asyncio facade's pool and the pyxrootd callback dispatcher are
  `xrdcore.fanout.Pool`s (the dispatcher's is now made on first use and
  again in a forked child, where the old module-level pool hung); the HTTP
  client names `IncompleteRead` and `HTTPMessage` through
  `xrdhttp.connection`; the test suite's TLS servers use the `pki`
  fixture's contexts and parse XML through `xrdcore.safexml`.
- Release metadata is xrd-devtools' release gate:
  `tests/test_release_metadata.py` replaces its own two checks with `release_gate(__file__)` from
  `xrddevtools.pytest_plugin` - one version source (`_version.py`, read by
  hatch), a CHANGELOG led by exactly that version's well-formed section,
  project metadata and README that name this repository, and a release
  workflow built through the shared publish action.
  The pin of the 0.2.0 heading is gone: the gate checks that every heading
  is well-formed and that the sections run newest first.
- The publish workflow's build job is xrd-devtools' shared `publish`
  composite action (build, `twine check --strict`, the artifact or
  pre-flight checks, a clean install whose tag, runtime version and dated
  changelog section agree, the artifact upload); only the trusted-publishing
  upload to PyPI stays in this workflow's own job.
- The standard's settings in `pyproject.toml` (ruff rules, the banned-API
  table, mypy's strictness and allowed overrides, complexipy, coverage,
  pytest's warnings filter) and CONTRIBUTING's *The code standard*, *Banned
  APIs* table and *xrd-devtools* sections are managed blocks, written by
  `xrddevtools sync` from the standard kept once in xrd-devtools;
  `xrddevtools conformance` fails a hand edit between the markers. The
  repository's own keys and notes sit beside the blocks.
- Banned-API messages for importing a package of the chain built on this
  one now say so ("sits below ... dependency direction"), generated from the
  declared layering.

### Testing and packaging

- No test helper is duplicated across the chain any more (the duplicate gate,
  `xrddevtools dupes`, now scans tests too): `tests/_pki.py` and
  `tests/_proxypki.py` are xrdsec's public `xrdsec.testing.rawpki` and
  `.proxygrid`, the `kdc` fixture uses
  `xrdsec.testing.krb5.recording_tgs`, `tests/_brix.py`/`_brix_fs.py` became
  `xrdclient.testing.brix`, and the session tests read whole frames through a
  buffered reader instead of a copy of xrdsec's KCM read loop.
- The HTTP behaviour xrdclient shares with every client of a storage element
  is tested by xrdhttp's conformance suites (`xrdhttp.testing.conformance`),
  run in `tests/test_http_conformance.py` through
  `XrdAdapter`/`XrdS3Adapter` (`FileSystem`, `open_http`, `third_party`, the
  tape verbs, `S3FileSystem`) - the same suites xgfalclient runs through
  gfal2's API. The 54 tests they replace were deleted from `test_http.py`
  (21), `test_conformance_http.py` (20), `test_s3.py` (8),
  `test_http_expect.py` (3), `test_http_names.py`,
  `test_http_redirect_credentials.py` (1 each) and two trimmed to their
  xrdclient-only part (`touch`, `Overwrite: T`); what stays tests
  xrdclient's own promises - its error classes and messages, `Config`, the
  file-object and pathlib API, request strategy, pyxrootd compatibility.
- `tests/conftest.py` loads xrdsec's `xrdsec.testing.fixtures` plugin
  instead of its own `_no_ambient_bearer_token`/`_no_ambient_proxy`
  fixtures, so the suite is also hermetic for `KRB5*`, `SSL_CERT_*`,
  `HOME` and `/etc/grid-security`; its `pytest_xrdsec_hermetic` hook keeps
  xrdclient's own part (the empty `XRD_CONFIG`, the portable Kerberos
  backend).
- `tests/test_typing.py` no longer runs `mypy --no-incremental` twice: its
  two checks share one incremental cache in the pytest run's temporary
  directory (xdist workers take turns on it under a file lock), so the
  second check takes seconds instead of another cold minute, and the file
  carries its own 900 s timeout rather than the suite's 120 s. What mypy
  checks is unchanged.
- The platform matrix builds and tests the whole 0.4.0 chain: the
  libraries xrdcore, xrdsec and xrdhttp, xrdclient, xgfalclient and,
  when the workspace has it, xrdarc. `xrddevtools platforms --workspace`
  (xrd-devtools, below) mounts each sibling checkout read-only (the list is
  its `packaging/platforms.json`'s
  `sources`); every package's wheel is built, the chain resolves binary-only
  (`--find-links` to the wheels just built, so nothing needs to be on PyPI
  yet), and every package's hermetic suite runs as a non-root user.
- RPM/DEB runtime ownership moved to a new `storage-clients-libs` package:
  `/opt/storage-clients`, its launcher, the three library wheels and every
  third-party wheel, versioned as xrdcore. `xrdclient`, `xgfalclient` and
  `xrdarc` each own only their wheel and require exactly
  `storage-clients-libs = version-release` (xgfalclient also exactly
  `xrdclient`), so xrdarc installs without a client. The launcher finds any
  command among the snapshot's entry points, and `%files` is derived from
  what was staged, so a package without commands builds.
- Nix: one derivation per package (`-A xrdcore`, `xrdsec`, `xrdhttp`,
  `xrdclient`, `xgfalclient`, `xrdarc`, `nixosTest`) on Nixpkgs'
  `cryptography`, `asn1crypto`, `botocore` and `google-crc32c`; the clients
  depend on the library derivations as their `pyproject.toml` files say
  (`botocore` now reaches xrdclient through xrdhttp). Sources default to
  sibling checkouts.
- Homebrew bundles carry each client's dependency closure, libraries
  included; xrdarc gets its own bundle and formula.
- The installed-package smoke test (now xrd-devtools' `installed_smoke.py`)
  checks the libraries' and each client's
  installed versions, every command's JSON/XML report, `xrd-fs --version`,
  `xrd-proxy --help`, `gfal-ls --version` and `xrdarc --version`
  (`--clients`, `--client-bin NAME=DIR`).
- CI checks out the library candidates (xrd-devtools' candidates action,
  `xrdcore-ref`/`xrdsec-ref`/`xrdhttp-ref` inputs) until they are on PyPI;
  the platform workflow also takes `xrdarc-ref`, all defaulting to `main`. `publish.yml` refuses to upload
  unless every library requirement of the wheel is already on PyPI as a
  wheel at its lower bound, then installs the wheel from PyPI alone.
- New chain pre-flight (`xrddevtools preflight`, xrd-devtools'
  `chain_preflight.py`): checks that the six versions agree, the
  inter-package pins follow the layering, every CHANGELOG has the version's
  section, and each package builds, passes `twine check --strict` and has
  matching wheel metadata (`workspace`); and that a wheel's chain
  requirements are on PyPI (`pypi`). `docs/releasing.md` has the chain's
  release runbook.
- The Nix build's one test error is fixed: `tests/test_scorecard.py` could
  not be collected without complexipy and radon (dev-only tools Nixpkgs
  lacks) and now skips itself. The Nix expression no longer disables
  xrdsec's and xrdhttp's import-boundary tests, which now find xrdcore
  where it is installed. The smoke test passes `--output-format`
  before `--help`, as a user would, now that `xrd-proxy` takes it there.
- The tooling that serves the whole chain moved to the new
  [xrd-devtools](https://github.com/rob-c/xrd-devtools) repository (package
  `xrddevtools`, command `xrddevtools`), with its tests: `tools/scorecard.py`
  and `scorecard_baseline.json` (`xrddevtools scorecard`),
  `tools/maintainability.py` (`xrddevtools maintainability`; the gate now
  also refuses a policy looser than the standard limits or with exclusions),
  `tools/chain_preflight.py` (`preflight`), `tools/platforms.py`,
  `tools/distribution_package.py`, `tools/homebrew_package.py`,
  `tools/installed_smoke.py`, `packaging/` (`linux.sh`, `platforms.json`,
  `nix/`, now at xrd-devtools' root: `nix-build xrd-devtools/packaging/nix`),
  `tests/test_distribution_tools.py` and the tools' unit tests. xrdclient
  keeps what is its own: `tools/pyxrootd_upstream.py`, `benchmarks/` and
  `maintainability.json`. xrd-devtools is not on PyPI and cannot be a
  dependency (PyPI refuses direct-URL requirements); pytest's `pythonpath`
  finds a sibling `../xrd-devtools` or CI's `.dependencies/xrd-devtools`.
- `tests/test_scorecard.py` and `tests/test_maintainability.py` are a few
  lines each, loading xrd-devtools' pytest plugin; they skip, saying so,
  without a checkout (or without radon and complexipy), and CI makes them
  required (`-p xrddevtools.pytest_plugin`, `XRD_DEVTOOLS_REQUIRED=1`).
  `tests/test_distribution_tools.py` now only pins that CI, the platform
  matrix and `publish.yml` call the shared ones.
- CI calls xrd-devtools' reusable workflows (`static.yml` - ruff, mypy,
  complexipy, the scorecard, `xrddevtools maintainability check` and
  `xrddevtools conformance`; `tests.yml`; `chain.yml` - the duplicate and
  conformance gates over all seven repositories, this commit among them),
  and its repository-specific jobs take their candidates from xrd-devtools'
  one candidates action, which replaces `.github/actions/library-candidates`.
  `platforms.yml` only calls xrd-devtools' platform workflow with this
  commit; `publish.yml` checks out xrd-devtools to run the pre-flight. CI
  therefore needs `rob-c/xrd-devtools` to exist.
- The report tests read documents with `xrdcore.report.parse` (re-exported
  as `xrdclient.report.parse`) instead of their own XML decoder, and the
  multi-range tests build bodies with `xrdhttp.testing.byteranges`.

## [0.3.2] - 2026-10-07

### Fixed

- `walk` descends fully, and `rmtree` with it, against a server that gives
  every directory the same placeholder stat id. The link-cycle guard took that
  id (dCache answers every directory with `0`) for one node shared by all of
  them and stopped one level in; a placeholder id now identifies nothing, so a
  tree on dCache is walked and removed instead of left half-cleared. `rmtree`
  also recognises dCache's "directory not empty" as the generic server error
  it arrives as, rather than abandoning the tree on it.
- `statvfs` against a server that answers the vfs request with an ordinary
  `kXR_stat` line (dCache) reports it as unsupported, not a protocol error.
- `copy` and `copy_tree` into WebDAV make the destination's parent collections
  first. A `PUT` makes none, and on EOS a failed one leaves the path unusable,
  so the parents are created up front from a clean namespace; a tree makes all
  of its directories once over one connection.
- `glob` keeps a `?` wildcard in a pattern given as a URL. It is also the
  URL's query delimiter, so the pattern's tail was parsed away as a query,
  matching the literal prefix and finding nothing; the wildcard is now kept,
  and the listing is made through a query-clean base so a strict server
  (dCache) does not reject the stray opaque data.
- A token-authorised third-party copy mints a short-lived macaroon at the far
  end for the delegated leg instead of forwarding a bare ambient token. An
  identity-mapped token (a DiracX token) is taken for a direct read or write,
  and to mint a macaroon, but refused on the server-to-server leg; a far-end
  token written into the URL is still used as given.

### Changed

- A third-party copy no longer asks the destination to verify the source's
  checksum in band (`RequireChecksumVerification` defaults off) and compares
  the digest end to end here instead, so a pull from a source that puts no
  checksum in its `HEAD` (CNAF's StoRM) to a strict destination (dCache)
  succeeds.

## [0.3.1] - 2026-10-06

### Changed

- Raised the declared Python floor from 3.9.2 to 3.10. On Python 3.9 botocore
  pins `urllib3<1.27`, which cannot be satisfied together with this client's
  `urllib3>=2.2`, so a 3.9 install never resolved; the metadata now says so
  up front instead of failing in the resolver. Every supported platform
  package already uses a distribution-provided Python 3.10 or newer
  (AppStream Python 3.12 on AlmaLinux 8/9 and CentOS Stream 9, the system
  Python elsewhere), so no deployment target changes. The code is still kept
  to 3.9 syntax, enforced by the compatibility test.
- Paired with xgfalclient 0.3.1, which requires `xrdclient==0.3.1`.

## [0.3.0] - 2026-10-05

### Added

- Portable VOMS attribute-certificate inspection and verification via
  `inspect_voms` and `validate_voms`, exposing VOs, ordered FQANs, generic
  attributes and independent verdicts for each assertion. Validation checks
  holder binding through proxy parents, validity, signer/issuer, signatures,
  target restrictions, critical extensions, CA chains and `vomsdir` LSC bindings.
- VOMS signatures using RSA PKCS#1/PSS, P-256 ECDSA and Ed25519, with malformed
  assertions and unsupported algorithms rejected. Existing VOMS proxy chains
  remain intact during authentication and delegation.
- Automatic macOS trust discovery under Intel and Apple Silicon Homebrew
  prefixes, alongside explicit `X509_CERT_DIR`/`X509_VOMS_DIR` overrides and
  standard Linux grid-security directories.
- Plain-language VOMS diagnostics with stable codes, exact paths and filesystem
  error numbers for missing, empty, corrupt, unreadable, expired or not-yet-valid
  trust material. CA expiry, signer expiry and missing/malformed/mismatched
  `.lsc` files are distinguished, with a safe next step for the user.
- Separate `check_vomses` endpoint preflight for missing folders, permissions,
  invalid UTF-8, malformed fields and invalid ports. Using an existing proxy
  does not require vomses configuration or contact a VOMS service.
- Versioned JSON/XML reports for every `xrd-fs` subcommand and `xrd-cp`,
  including help/version, usage/runtime errors, partial batches, staging
  identifiers/states, progress and base64 binary stdout. The shared
  `storage-client-report` schema retains numeric codes and typed per-file
  results; `--output-format json|xml` selects it, while text remains the default.
  Existing successful Xrd `--json` payloads remain compatible.
- Optional pykrb5 cache reads selected by `XRD_KRB5_BACKEND=native`, retaining
  native error codes and never rewriting the cache. Explicit macOS `API:` and
  `MEMORY:` caches can obtain raw XRootD AP-REQ tokens through python-gssapi.

### Changed

- Updated the declared Python minimum from 3.9 to 3.9.2, retaining the Python
  3.9 minor-version floor. Clean-install readiness is documented below.
- Added `asn1crypto`, `botocore`, `cryptography`, `PyJWT[crypto]` and `urllib3`
  as runtime dependencies. Library adapters replace local cipher arithmetic,
  signature primitives, DER primitives, JWT claim decoding, AWS HMAC signing
  and HTTP connection/TLS setup while retaining client-specific policies.
  JWT expiry inspection is diagnostic, not signature verification.
- Made xrdclient the canonical shared implementation for xgfalclient's VOMS,
  DER/RSA/AES/signature helpers, X.509 names and inspection, XML declaration
  checks, HTTP connection/request lifecycle, streamed copy/read-ahead pipeline,
  bulk upload framing and S3 codecs. xrdclient remains independently installable
  with no import of or dependency on xgfalclient.
- Both certificate facades now use cryptography's X.509 APIs for ordinary
  certificates. One bounded fallback preserves legacy inspection behaviour;
  protocol-specific proxy policy and raw-RSA GSI compatibility remain local.
- Consolidated bounded HTTP redirects, retry/replay decisions and failed
  exchange cleanup behind adapters. Shared transfer orchestration retains
  client-specific credentials, upload modes, buffers, cancellation, checksum,
  durability, recovery and error policies.
- Extended bulk writes with acknowledged-range progress, cancellation checks,
  strict reply handling and caller-owned WAIT policy. GFAL can replay settled
  WAIT ranges without maintaining a second framing implementation.
- S3 signing, modeled response/error parsing, namespaces, timestamps, listings
  and multipart-manifest serialization now use botocore over the existing HTTP
  transport, without adopting SDK credential discovery or retry policy.
- XML loading rejects declarations through shared standard-library helpers.
  Binary records retain bounded local readers; neither XML libraries needing
  compilation nor Construct are required.
- Native `gssapi` and `krb5` bindings are confined to the optional `krb5`
  extra. Default installations retain portable Kerberos paths and use binary
  dependency wheels on the tested mainstream platforms.

### Fixed

- Partial stream writes complete before a chunk is acknowledged or progress
  advances; zero, negative and oversized write counts fail rather than losing
  or duplicating bytes. Reader workers stop cleanly on errors/cancellation.
- S3 copy and multipart completion reject embedded error replies even when
  HTTP reports success.
- Common server failures now provide clear display summaries and next steps
  without changing exception classes, numeric codes or raw server details.
  Credential diagnostics retain redaction and distinguish connection refusal
  from timeout.
- Malformed VOMS/LSC data, invalid encoding, incomplete subject/issuer pairs
  and filesystem permission failures produce typed diagnostics instead of
  parser exceptions or misleading trust failures.

### Testing and packaging

- Added extensive VOMS, trust/permission, malformed-input, clock-boundary,
  signature and policy tests, including independent OpenSSL checks when
  available. Shared engines, dependency adapters, old API surfaces, transport
  faults, durability, WAIT/acknowledgements and JSON/XML reports have regression
  coverage; existing coverage, maintainability, performance and interop gates
  remain required.
- Added a shared rootless Podman/Docker runner and coordinated CI for AlmaLinux
  8/9/10, CentOS Stream 9/10, Ubuntu 24.04/26.04, Fedora 44 and Rawhide, NixOS
  26.05 and Homebrew on Intel/Apple Silicon, on both x86-64 and ARM64. The
  runner's `--arch` option selects an image architecture locally. Tests check binary-only dependency resolution,
  built-wheel installs, all installed commands, both hermetic suites as a
  non-root user, and native package installation/removal.
- Added private RPM/DEB deployment bundles, Nix package/VM recipes and
  compiler-free Homebrew wheel-bundle formula generation. The bundles ship
  their bytecode and run with `-B`, and the runtime RPM owns its directory,
  so removal leaves nothing behind. Exact paired package
  dependencies, dependency inventories/hashes and package release/revision
  increments support coordinated upgrades and dependency security rebuilds.
- Corrected Linux/macOS test portability: deterministic replica scheduling,
  KCM listener shutdown, bounded oversized-frame fixtures, inherited Nix
  dependency paths and real filesystem/kernel capability checks.

### Release readiness and limitations

- Known limitation: Python 3.9 is the declared floor, but a clean Python 3.9
  install does not resolve, because botocore pins `urllib3<1.27` there while
  this release needs `urllib3>=2.2`. Use Python 3.10 or newer; the AlmaLinux
  8/9 and Stream 9 packages use Python 3.12. The floor will be corrected in
  a follow-up release. Native Kerberos extras may need a compiler and headers
  on Linux.
- VOMS verifies existing assertions; it does not issue them. CA-path checking
  is not full RFC 5280 constraint/CRL validation. pyhanko-certvalidator remains
  deferred to preserve Python 3.9 compatibility. Native-cache forwarding
  still requires a `FILE:` cache; real-KDC native-cache interop is not yet proven.
- The 0.3.0 candidates passed all nine RPM/DEB targets natively on ARM64,
  including Fedora Rawhide on Python 3.15, plus native-VM installs on
  AlmaLinux 9 and Ubuntu 24.04, the Nix package builds and a booted aarch64
  NixOS VM test, and Apple Silicon Homebrew installation, `brew test` and
  command checks. x86-64 artifacts come from the native hosted runners. See
  [the platform guide](docs/platforms.md) for validation scope and skipped tests.
- GSI proxy delegation against an xrootd 6.2.0 server fails in the server's
  `kXGC_certreq` handling for 0.2.0 as well as this release; non-delegating
  GSI logins work. This is tracked as a server-version incompatibility.

## [0.2.0] - 2026-10-01

### Added

- Metalink v3/v4 parsing, replica failover, checksum selection and direct
  opening of a Metalink document.
- Reading one member from a remote ZIP archive and appending stored members to
  a ZIP without downloading and rewriting the archive.
- The remaining XrdCl/PyXRootD compatibility calls, response types, flags,
  environment keys and asynchronous callback forms covered by the upstream
  surface.
- Opt-in BRIX proxy and FUSE integration suites for connection truncation,
  corruption, stalls, stale handles, short I/O, delayed writeback failures and
  dishonest filesystem metadata.
- Python 3.14 CI and PEP 561 typing markers in built distributions.

### Changed

- Read and copy recovery is adaptive: retries resume at verified byte
  boundaries, reconnect after repeated failure and do not replay operations
  whose outcome is ambiguous.
- Local destinations are flushed and checked before a successful copy is
  reported, exposing delayed `ENOSPC`, `EIO` and lost writeback.
- Quality gates now enforce formatting, strict typing, selected security
  checks, five maintainability limits, built-distribution validation and
  statistically paired performance comparisons.
- Package metadata and `xrdclient.__version__` now share one version source.

### Fixed

- Short and zero-progress reads/writes, replaced local sources, truncated
  protocol frames and corrupt same-length payloads can no longer be mistaken
  for a successful transfer.
- Retry and cleanup paths preserve the primary transfer error and avoid
  leaking failed connections back into the pool.

[0.4.0]: https://github.com/rob-c/xrdclient/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/rob-c/xrdclient/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/rob-c/xrdclient/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/rob-c/xrdclient/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/rob-c/xrdclient/compare/v0.1.0...v0.2.0
