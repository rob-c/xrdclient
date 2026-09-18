# xrdclient — implementation roadmap

**Date:** 2026-07-31
**Design:** [`../specs/2026-07-31-pyxrootd-client-design.md`](../specs/2026-07-31-pyxrootd-client-design.md)

Eleven phases. Each ends at a state that is tested, documented, and useful on
its own — there is no phase whose only value is enabling the next one. Phases
0–4 are the critical path to a usable library; 5–10 are independently
schedulable and several can run in parallel.

**Global constraints**

- The core package imports **stdlib only**. Third-party imports are confined to
  modules behind an extra, each with a pure-Python fallback or an actionable
  `ImportError`.
- `mypy --strict` and `ruff` clean at every phase boundary; no phase may leave
  the tree red.
- Every wire layout cites the reference it was translated from
  (`libxrdc` file:line, `XRootD.jl` module, or go-hep package) in a docstring.
- TDD: failing test first, then implementation. Commit per task.
- **Never** reuse the CRC-32 table between `sss` (IEEE, zlib polynomial) and
  paged I/O / verification (CRC32c, Castagnoli). They are different algorithms.

---

## Status — as built

Phases 0–10 are **implemented**: 18 kLOC under `src/xrdclient/` against 18 kLOC of
tests, **2510 tests green in ~50 s** with no third-party import anywhere —
`root://` and `davs://` are peers, any endpoint copies to any other, both are
awaitable, both are a URL scheme in pandas and a command in a shell, and the
whole authentication ladder bar Kerberos is pure Python. Of the 2510, 47 are
the interoperability and parity suites (a real `xrootd` daemon, and the
official bindings alongside), 17 need the `[fsspec]` extra and one runs `mypy`
over the public surface and checks the revealed types; the rest run
against the stdlib alone. Coverage on `proto/`, `crypto/`, `client/`, `s3/` and `root/` is
**100%**, line and branch, and gated there — and is 100% across the whole
package besides.

Landed since Phase 10, from the same reading of the reference clients: the
WLCG tape API and `kXR_QPrep` staging, `archive_info`, `kXR_clone` server-side
range copies, read-ahead in the copy engine, credential prompting, and
**S3** — `s3://bucket/key` as one more scheme, with SigV4 out of `hmac`,
multipart uploads, and `xrdclient.testing.FakeS3Server` to test against
([S3 object storage](../../s3.md)) - and the guard rails a beginner meets
before they meet the protocol: a ceiling on reads that named no size, an
`xrd-cp` that will not overwrite without `-f`, and an `xrd-fs rm -r` that asks
([Safety](../../safety.md)). Newest is `xrdclient.root`: a pure-Python ROOT and
TTree reader that streams baskets off a storage element straight into PyTorch
tensors, with every column it cannot decode named and refused rather than
guessed at - since split out as [xrdroot](https://github.com/rob-c/xrdroot),
which is where that work now lives.

| Phase | State | Shipped as |
|---|---|---|
| 0 Scaffold | done | `pyproject.toml`, `_log.py` (redacting filter), `py.typed` |
| 1 `proto/`, `crypto/` | done | `constants` · `buffer` · `frames` · `requests` (752) · `responses` · `crc32c` · `blowfish` · `checksum` · `sigver` · (Phase 7 added `der` · `aes` · `rsa` · `x509`) |
| 2 `SessionMachine` | done | `proto/machine.py` (626) — zero I/O, driven by bytes |
| 3 transport / auth / session | done, **one sync core** | `transport/{sync,memory}` · `auth/{simple,sss,ztn}` · `session/{sync,router}` |
| 4 `client/` | done, **one sync core** | `client/filesystem.py` · `client/file.py` |
| 5 Pythonic surface | done | `io/raw.py` · `io/__init__.open_url` · `path.py` · `config.py` · `xrd/__init__.py` · `aio.py` (607) |
| 6 HTTP / WebDAV | done, **stdlib only** | `http/{client,file,dav}.py` (1.2k) · `testing/http.py` (386) — scheme dispatch in `FileSystem.__new__` and `open_url` |
| 7 GSI / Kerberos | done, **no `[gsi]` extra** | `crypto/{der,aes,rsa,x509}.py` (949) · `auth/gsi.py` (397) · `auth/krb5.py` (299) — X.509 proxies, unsigned-DH GSI, ccache reader, mTLS on both schemes |
| 8 Copy engine | done | `copy/engine.py` (277) · `copy/tpc.py` (161) · `http/tpc.py` (230) — `copy` · `copy_tree` · `third_party`, which dispatches on the URLs: the `XrdOucTPC` rendezvous for a `root://` pair, the WLCG `COPY` dialect for an HTTP one |
| 9 Ecosystem (fsspec, CLI) | done | `fsspec_impl.py` (290) · `cli/{__init__,fs,cp}.py` (701) — `xrd-fs` (16 subcommands) · `xrd-cp` · six registered URL schemes |
| 10 Hardening / 1.0 | done | `tests/test_parity.py` · `tests/test_interop.py` · `tests/test_faults.py` · `testing/faults.py` · `benchmarks/bench.py` · `SECURITY.md` · `docs/` (20 pages, MkDocs Material) · `.github/workflows/ci.yml` · `tests/test_typing.py` |

**Deltas from the plan, and why**

- **`XRootDPath` is not a `pathlib.PurePath` subclass.** `PurePath`'s private
  surface (`_flavour`, `_from_parsed_parts`, `with_segments`) changes between
  point releases; inheriting would trade a stable public API for a fragile
  one. The class carries the same method names instead, so calling code ports
  unchanged, and `_derive()` — not `with_segments()` — propagates the endpoint,
  the `Config`, **and the open connection**.
- **`FileSystem.query_config`, not `config`.** `fs.config` is the
  filesystem's own `Config`; the `kXR_query`/`kXR_Qconfig` lookup could not
  share the name.
- **`xrdclient.FileSystem(url)`, not `xrdclient.connect(url)`.** The class is already a
  context manager and connects lazily, so a factory function would add a name
  without adding a capability.
- **The reference server is `xrdclient.testing.FakeServer`**, written fresh rather
  than grown from `pyxrdcp/tests/_refserver.py`, and is tested in its own
  right (`tests/test_testing.py`) because it is public API.
- **The `(status, result)` compat shim is dropped.** It is the one piece of
  pyxrootd whose absence is the point: see the decision log.
- **No `httpx`, and so no `[http]` extra.** `http.client` does everything
  Phase 6 needs — keep-alive, ranged `GET`, chunked `PUT`, arbitrary verbs for
  WebDAV — so taking a dependency would buy HTTP/2 at the cost of the property
  that makes this package worth having. The extra was declared, never used, and
  is now removed: `pip install xrdclient` is all `davs://` takes.
- **`http/` is three modules, not five.** `webdav.py`, `digest.py` and
  `macaroon.py` are one file (`dav.py`): they share the XML helpers and the
  `HTTPClient`, and splitting them would have meant three modules importing
  each other to serve one class.
- **`HTTPFileSystem` subclasses `FileSystem`.** `walk`, `glob`, `rmtree` and
  the whole-file helpers are written in terms of `scandir`/`open`/`remove`, so
  they are inherited verbatim; the methods that would need an XRootD session
  are overridden to raise `UnsupportedError` naming what HTTP lacks.
- **The async facade is one module, not a parallel stack.** The plan called
  for `transport/aio.py`, `session/aio.py` and async twins throughout
  `client/`. What shipped is `xrd/aio.py`: an explicit, fully-typed mirror
  whose every method hands the synchronous call to `asyncio.to_thread`. The
  reason is that the alternative duplicates the session state machine's
  driver, and two drivers drift — one grows a fix, a timeout, a retry that the
  other does not, and the divergence surfaces as "it works synchronously".
  Delegation keeps one implementation of every operation and still gives an
  `await`-able surface that never blocks the loop; the cost is a thread per
  in-flight call, which for a metadata-heavy client is bounded by the default
  executor and cheaper than the bug class it removes. It also stays honest
  about concurrency: two endpoints genuinely overlap, and calls on *one*
  endpoint serialise on the session lock, exactly as they would have in an
  asyncio driver over a single connection. `import xrdclient` still does not import
  `asyncio` — `xrdclient.aio` is resolved by a module `__getattr__` on first use.
- **`copy_tree`, not `copytree`.** The rest of the surface is `snake_case`
  (`read_bytes`, `query_config`), and `shutil`'s spelling is the outlier.
- **`xrd-fs`, not `xrdfs`.** The hyphen matches `xrd-cp`, and the tool is not
  a drop-in for stock `xrdfs`: it takes whole URLs rather than a host followed
  by a path, because that is what the library takes and what a user has in hand.
- **fsspec gets the library's real file object**, not an
  `AbstractBufferedFile` subclass. `XRootDFile` is already an `io` object with
  its own buffering and vector reads; re-deriving that inside fsspec's cache
  layer would be slower and would have to be kept in step.
- **GSI needs no third-party crypto, so the `[gsi]` extra was deleted.** The
  plan assumed `cryptography` for AES, RSA and DER. Writing them instead cost
  ~660 lines and bought the thing the whole project is for: the complete auth
  ladder installs on a worker node with no wheel access. It is a handshake
  path, not a data path — AES here encrypts a few hundred bytes once per
  connection, and bulk confidentiality stays TLS's job, which is `ssl`, which
  is C.
- **`krb5` is the one mechanism that is not pure Python, deliberately.** Its
  ccache reader is — that is what distinguishes "authentication failed" from
  "your ticket expired 40 minutes ago" — but the AP-REQ comes from `gssapi`.
  A Kerberos exchange this client built could only be validated against a live
  KDC, and a security mechanism whose only test is its own decoder is worse
  than none.
- **Both TLS stacks share one context builder.** `http/client._context` now
  defers to `transport.base.tls_context` instead of building a second context
  that could drift from it; `roots://` and `davs://` must trust the same CAs
  and present the same proxy.
- **Writing the tests found six real defects**, which is the argument for the
  rule: `FakeServer.address` rebinding a fresh port after `stop()`; an
  `HTTPRawIO` whose constructor failed still attempting a `PUT` when collected;
  an error response left undrained, which stranded the pooled connection and
  made the next request re-send a conditional `PUT` that then failed its own
  condition; `UnsupportedError(f"...")` called with one argument in
  `copy/engine.py`, a `TypeError` waiting for anyone who named a scheme the
  library does not speak; `--json` expanding an `XRootDURL` into its fields
  because it is a dataclass and the dataclass branch came first; and
  `XRootDFileSystem._target` opening — and leaking — a connection per call for
  a fully-qualified path to another server. Phases 5 and 7 added three more:
  `AsyncFile.file` returning `None` for every buffered handle, which made the
  entire protocol-level async surface (`readv`, `pgread`, `stat`, `checksum`)
  unreachable; `_probably_prime(2)` raising `ValueError` out of `secrets`
  because 2 was missing from the small-prime table; and a missing or stale
  `$X509_USER_PROXY` producing `[Errno 2] No such file or directory` — naming
  neither the file nor the setting that chose it.
- **Phase 10 found seven more, and only a real server could have.** The
  interop suite: `kXR_writev` framed its data length in `dlen`, which the fake
  accepted and `xrootd` refused outright; `FileSystem.touch` needed
  `kXR_new|kXR_open_updt|kXR_mkpath` and to swallow `ExistsError`, because
  `kXR_new` fails when the file is there; append mode had to open plainly and
  retry with `kXR_new` only on `NotFoundError`, since `kXR_open_apnd` does not
  create; `glob` was matching like `fnmatch` rather than like `pathlib`; and a
  failed open leaked its connection. The security review: a secret split
  across a log format string and its arguments escaped redaction (and the
  filter could eat the format string itself), SSS keytabs were accepted
  regardless of file permissions, and `parse_dirlist` accepted server-supplied
  names containing `/` or `..` — which `copy_tree` joins onto a *local*
  destination. Every one landed with a test.

---

## Phase 0 — Scaffold

**Deliverable:** an installable, empty-but-correct package with CI green.

- `src/xrdclient/` layout, `pyproject.toml` (hatchling), extras declared as designed,
  `py.typed`.
- `ruff`, `mypy --strict`, `pytest` (`-n auto`, `--timeout`), `coverage`
  configured; pre-commit hooks.
- GitHub Actions: matrix 3.10–3.13 × {core-only, all-extras}. The core-only job
  asserts, by import hook, that no third-party module is imported by
  `import xrdclient`.
- `xrdclient.__version__`, `_log.py` with the secret-redacting filter, `LICENSE`
  (LGPL, matching the reference projects), `README` skeleton, attribution to
  `libxrdc` recorded up front.

**Done when:** `pip install -e .[all]` succeeds, `pytest` passes with zero
tests, both linters are clean, and CI is green on all eight matrix cells.

---

## Phase 1 — `proto/` and `crypto/`: the pure layer

**Deliverable:** every byte the protocol can put on a wire, encodable and
decodable, with no I/O anywhere. This is the largest single body of code and
the one with the most existing material to draw on.

1. **`proto/constants.py`** — port `pyxrdcp/wire/constants.py` (185 LOC) and
   complete it against `XRootD.jl/src/Wire/constants.jl` and go-hep
   `xrdproto/`: all opcodes 3000–3032 plus vendor 3500–3503, every status, all
   protocol/login/open/dirlist/stat/query/prepare/locate/fattr flags, page
   constants, header lengths. `request_name()` for diagnostics.
2. **`proto/buffer.py`** — a big-endian read/write cursor over `memoryview`
   with bounds checking that raises `ProtocolError`, not `struct.error`. All
   later codecs use it; nothing calls `struct` directly.
3. **`proto/frames.py`** — request header (24 B), response header (8 B),
   handshake, and `kXR_status` paged trailer assembly. Port + extend
   `pyxrdcp/wire/frames.py`.
4. **`proto/requests.py`** — one encoder per opcode. Port `pyxrdcp`'s 15, add
   `chmod`, `statx`, `locate`, `prepare`, `readv`, `writev`, `pgread`,
   `pgwrite`, `verifyw`, `chkpoint`, `fattr`, `endsess`, `bind`, `set`,
   `gpfile`, `clone`. go-hep `xrdproto/<op>/` is the layout reference.
5. **`proto/responses.py`** — matching decoders, including `pgread` page-CRC
   trailers, `fattr` multi-value bodies, deep `locate`, and `statx`.
6. **`crypto/`** — port `blowfish.py`, `sss.py`, `sigver.py`, `crc32c.py` from
   `pyxrdcp` verbatim where correct; add `adler32.py`, `crc64.py` (xz
   polynomial, table-driven), and a hardware-CRC32c probe with the pure
   implementation as fallback.
7. **`flags.py`, `types.py`, `errors.py`, `url.py`** — the public value types.
   `errors.py` includes the full `kXR_error` → `errno` → exception-class table.
   `url.py` handles `user@host:port`, IPv6 literals, the `//` path convention,
   and CGI (`?authz=`, `tpc.*`) round-tripping.

**Tests:** unit vectors for every encoder/decoder, sourced from all three
reference implementations; `hypothesis` round-trip properties
(`decode(encode(x)) == x`); a decoder fuzz corpus asserting no crash and no
hang on arbitrary bytes; the ported `pyxrdcp/tests/test_wire.py` passing
unmodified against the new module paths.

**Done when:** every opcode in the §9 coverage table encodes and decodes with a
test, coverage on `proto/` ≥ 95%, and the package still imports nothing outside
the stdlib.

---

## Phase 2 — `SessionMachine`: the sans-io state machine

**Deliverable:** the complete protocol brain, driven entirely by byte strings.

- `submit`/`data_to_send`/`receive_data`/`next_event`/`timers` as specified in
  design §5.
- Bring-up: handshake → `kXR_protocol` → TLS decision → `kXR_login` →
  multi-round `kXR_authmore` loop.
- **Stream multiplexing**: streamid allocation, recycling, per-stream state,
  and routing. Concurrent outstanding requests are the point of this phase.
- `kXR_oksofar` accumulation, `kXR_status` paged assembly with the page-CRC
  trailer, `kXR_attn`/`kXR_asynresp` unwrapping, `kXR_waitresp` parking,
  `kXR_wait` retry scheduling, `kXR_redirect` with a bounded budget.
- `kXR_sigver` prefix emission driven by negotiated security level, with
  sequence-number management.

**Tests:** the whole phase is tested with byte strings and a fake clock — no
sockets. Every interleaving that is hard to provoke live is easy here: a
response arriving for a recycled streamid, a redirect mid-`oksofar`, a
`waitresp` that never lands, a truncated paged frame, an `attn` between two
partial results.

**Done when:** a table-driven scenario suite covers every event type and every
documented server behavior, and the machine has no import of `socket`, `ssl`,
`asyncio`, `threading`, or `time`.

---

## Phase 3 — `transport/`, `auth/`, `session/`: connect for real

**Deliverable:** working `root://` and `roots://` sessions, sync and async,
with the auth mechanisms that need no third-party crypto.

1. **`transport/`** — `Transport` protocol; `sync.py` (socket + `ssl`),
   `aio.py` (`asyncio` streams), `tls.py` (`SSLContext` construction, hostname
   verification, CA discovery, mTLS material, ALPN). ~150 LOC each.
2. **`auth/`** — the mechanism registry and negotiation ladder; `unix`, `host`,
   `ztn` (with the full token-discovery order and expiry awareness), `sss`
   (reusing Phase 1 crypto, now multi-round). Preference order configurable.
3. **`session/sync.py`** — reader thread pumping the machine; waiters keyed by
   streamid; keepalive ping; clean shutdown; `ResourceWarning` on leak.
4. **`session/aio.py`** — the same over an `asyncio` task; cancellation
   unregisters cleanly.
5. **`session/pool.py`** — connection pool keyed by
   `(scheme, host, port, user, credential fingerprint)`, ref-counted, idle TTL,
   per-endpoint cap. Translated from `libxrdc/lib/net/cpool.c`.
6. **`session/resilience.py`** — backoff with jitter, idempotent-replay
   classification, redirect budget, timeout policy. From
   `libxrdc/lib/net/resilient.c`.

**Tests:** against the ported `_refserver`; a test asserting the sync and async
drivers produce identical event sequences for identical scripted servers; pool
lifecycle and leak tests.

**Done when:** both drivers connect, authenticate (`unix`/`ztn`/`sss`), and
round-trip a `ping` and a `stat` against both `_refserver` and
`/usr/bin/xrootd`, with concurrent streams demonstrably multiplexed on one
socket.

---

## Phase 4 — `client/`: the full operation set

**Deliverable:** `FileSystem`/`File` and their async twins, covering every
operation in design §9 for the `root://` protocol.

1. **`client/filesystem.py`** — `stat`, `statx`, `dirlist` (generator, with
   `stat=`/`cksum=`), `mkdir`, `rm`, `rmdir`, `mv`, `chmod`, `truncate`,
   `locate` (incl. deep), `query` (all codes), `checksum`, `prepare`,
   `statvfs`, `ping`, `getxattr`/`setxattr`/`listxattr`/`delxattr`, `protocol`.
2. **`client/file.py`** — `open`/`close`, `read`, `write`, `readv`, `writev`,
   `pgread` (with per-page CRC32c verification and the retry-bad-page path),
   `pgwrite`, `clone` (`kXR_clone`: the server copies byte ranges from one
   open handle into another, so the data never crosses the wire), `sync`,
   `truncate`, `chkpoint`, `visa`/`fcntl`, per-file xattr.
3. Handle recovery after reconnect (re-open at offset; refuse for `kXR_new`).
4. Async mirrors, with `async for` on every generator.
5. A test asserting the sync and async classes expose identical public method
   names and signatures — the anti-drift gate.

**Tests:** full operation matrix against `_refserver` *and* `/usr/bin/xrootd`;
read/write round-trips at page boundaries, across chunk boundaries, and at
sizes spanning `oksofar` splits; `readv` with scattered and overlapping ranges;
`pgread` with an injected bad page.

**Done when:** every §9 `root://` operation has a passing test against a real
xrootd server. **This is the first genuinely usable release: v0.1.**

---

## Phase 5 — The Pythonic surface

**Status: done**, including the async facade that Phases 3 and 4 deferred.
`xrdclient.aio` mirrors the whole surface: `xrdclient.aio.open` is both awaitable and an
async context manager, `AsyncFile` carries every method `xrdclient.open`'s handle
does (`readv`, `pgread`, `writev`, `pgwrite`, `stat`, `checksum` included) plus
`async for` over lines, and `AsyncFileSystem` carries every `FileSystem`
method, with `iterdir`/`walk`/`glob` returning async iterators that stay lazy —
a `break` does not list the rest of the tree. `AsyncFileSystem.wrap()` shares
an already-open sync connection rather than making a second one. The
predicates that need no round trip (`readable`, `closed`, `mode`, `name`)
stay synchronous, because coroutining them would cost every caller an `await`
and buy nothing. `davs://` works through the same objects. Covered by
`tests/test_aio.py` (27 tests), which includes a subprocess check that
`import xrdclient` still leaves `asyncio` out of `sys.modules`. Not done:
`io/buffered.py` and `io/vector.py` as separate modules — `io/raw.py` under
the stdlib's own `BufferedReader`/`BufferedWriter` covers what they were for,
and `File.readv` is the vector scheduler.

**Deliverable:** what the project is actually for. Nothing new goes on the
wire in this phase.

1. **`io/raw.py`** — `XRootDRawIO(io.RawIOBase)`: `readinto`, `write`, `seek`,
   `tell`, `truncate`, `readable`/`writable`/`seekable`, `close`. Verified
   against the stdlib's own `io` conformance expectations by wrapping it in
   `BufferedReader`/`BufferedWriter`/`TextIOWrapper` and exercising
   `readline`, iteration, `peek`, and encodings.
2. **`io/buffered.py`, `io/vector.py`** — readahead window, write-behind,
   adaptive chunk sizing, and a `readv`/`pgread` chunk scheduler that keeps N
   requests in flight.
3. **`xrdclient.open()`** — full `builtins.open` mode-string semantics mapped onto
   `OpenFlags`.
4. **`path.py`** — `XRootDPath`: `/`, `parent`, `name`, `suffix`, `iterdir`,
   `glob`, `rglob`, `walk`, `stat`, `exists`, `is_dir`, `is_file`, `mkdir`,
   `touch`, `chmod`, `rename`, `unlink`, `rmdir`, `read_bytes`/`read_text`,
   `write_bytes`/`write_text`, `open`, `samefile`. `with_segments()` propagates
   endpoint and credentials.
5. **`config.py`** — `Config` dataclass, `XRD_*`/`BEARER_TOKEN*`/
   `X509_USER_PROXY`/`XrdSecSSSKT` resolution, `contextvars` override,
   `xrdclient.configure()` / `with xrdclient.config(...)`.
6. **`xrd/__init__.py`** — the curated public surface; `__all__` is a
   deliberate, short list.

**Tests:** a conformance suite that runs the *same* test body against a local
file, an `XRootDPath`, and an `xrdclient.open()` handle, asserting identical
behavior — the strongest statement that the API is genuinely Pythonic.

**Done when:** the design §6 examples all run verbatim. **v0.2.**

---

## Phase 6 — HTTP, WebDAV, and XrdHttp

**Status: done** — `http/{client,file,dav}.py`, stdlib only (no `httpx`),
tested against `xrdclient.testing.FakeDAVServer` in `tests/test_http.py`. Scheme
dispatch is transparent: `xrdclient.open`, `xrdclient.FileSystem`, `xrdclient.XRootDPath` and
`xrdclient.copy` all take `http(s)`/`dav(s)`/`webdav` URLs. Not done: HTTP/2,
multi-range responses, and `COPY`-based HTTP third-party copy.

**Deliverable:** `https://`, `davs://`, and XrdHttp endpoints as first-class
peers of `root://`, behind the same `FileSystem`/`XRootDPath`/`open()` surface.

1. **`http/client.py`** — pooled HTTP/1.1 client: keep-alive, redirects,
   retries, range requests, chunked upload. Uses `httpx` when the `[http]`
   extra is present; falls back to `http.client` otherwise. Model:
   `libxrdc/lib/protocols/http/{http_req,web_ka,webfile_io}.c`.
2. **`http/webdav.py`** — `PROPFIND` (depth 0/1), `MKCOL`, `MOVE`, `DELETE`,
   `OPTIONS`; XML parsed with `xml.etree` and a hardened parser (no external
   entities). Model: `XRootD.jl/src/Storage/web.jl` and
   `libxrdc/lib/protocols/http/weblist.c` (556 LOC — the thorough version).
3. **`http/digest.py`** — `Want-Digest`/`Digest`/`Content-MD5` negotiation for
   adler32, crc32c, md5, sha-256.
4. **`http/macaroon.py`** — minting via `POST` with caveat requests,
   attenuation for delegation.
5. Wire it into the URL dispatcher so scheme selection is transparent:
   `XRootDPath("davs://...")` and `xrdclient.open("https://...")` just work.

**Tests:** against a local WebDAV server fixture and, when available, a real
XrdHttp door started from the installed `xrootd` with the HTTP protocol
enabled. Range, multi-range, resumable PUT, and PROPFIND listings compared to
`root://` results for the same paths.

**Done when:** the Phase 5 conformance suite passes with a `davs://` endpoint
substituted for `root://`. **v0.3.**

---

## Phase 7 — GSI/X.509 and Kerberos

**Status: done**, and with one extra fewer than planned. 949 lines of new
`crypto/` and 696 of new `auth/`, against 1246 lines of tests.

1. **`crypto/der.py`** (149) — a strict, read-only DER parser. Refuses
   indefinite lengths and multi-byte tags; a lenient parser of
   attacker-supplied structures is a liability.
2. **`crypto/aes.py`** (221) — AES-128/192/256 and CBC, tables generated at
   import from the field arithmetic rather than pasted in. Verified against
   FIPS-197 C.1–C.3 and NIST SP 800-38A F.2.1.
3. **`crypto/rsa.py`** (294) — PKCS#1 and PKCS#8 key loading, PKCS#1 v1.5
   signing both with and *without* a DigestInfo wrapper (GSI's proof of
   possession is the raw form), CRT signing, and `generate()` for tooling.
   Cross-checked byte-for-byte against `openssl` during development.
4. **`crypto/x509.py`** (285) — certificates, RFC 3820 and legacy Globus
   proxies, `$X509_USER_PROXY` / `/tmp/x509up_u<uid>` discovery, and the
   `identity` that strips proxy CNs back to the human. It reads; it does not
   verify signatures or build paths, because trust decisions belong to the
   endpoint.
5. **`auth/gsi.py`** (397) — XrdSut bucket framing, the unsigned-DH handshake
   (`kXGC_certreq` → `kXGS_cert` → `kXGC_cert`), DH over the server's group,
   the AES-128-CBC session key, and the signed random tag.
6. **`auth/krb5.py`** (299) — a pure-Python FILE credential-cache reader
   (versions 3 and 4) plus a `gssapi`-backed exchange. `[krb5]` → `gssapi`.
7. **Mutual TLS end to end** — `roots://` and `davs://` now share one
   `tls_context`, so the same proxy is the client certificate on both.

**Tests:** `test_der` · `test_aes` · `test_rsa` · `test_x509` · `test_gsi` ·
`test_krb5` · `test_mtls` — 128 of them, and none needs `openssl`, a KDC, or a
network. `tests/_pki.py` is a small DER *writer*, so every certificate is
minted at test time and valid today; only the 2048-bit keys are frozen, in
`tests/_keys.py`, because `ssl` refuses anything smaller and a pure-Python
prime search of that size costs seconds. `test_gsi.py` plays the far end of a
whole handshake: it agrees the DH key from the other side, decrypts the
payload, and verifies the proof-of-possession signature under the proxy's own
public key.

**Not done, and refused by name rather than mis-answered:** the signed-DH GSI
path (version ≥ 10400) and `kXGS_pxyreq` X.509 delegation.

**Remaining:** authenticating against a real GSI-configured xrootd server.
Everything here is verified against published vectors, `openssl`'s output, and
a simulated server; none of that is the same as a live SE. **v0.4.**

---

## Phase 8 — Copy engine and third-party copy

**Status: done** for the transfer matrix — `copy/engine.py` (`copy`,
`copy_tree`, `CopyResult`) moves any endpoint to any other (`root://`, HTTP,
local path, open file object) and verifies with a digest taken while
streaming; `copy/tpc.py` implements the stock `root://` rendezvous dialect,
asserted wire-exactly in `tests/test_copy.py`. `resume=True` continues an
interrupted transfer from what is already at the target, verifying by
comparing the two ends rather than the tail it streamed, and a file long
enough for it is cut into `config.parallel_chunks` spans carried by a
connection each, verified the same way. `copy_tree(..., workers=N)` runs N of
those transfers at once, in walk order and cancelling the rest on the first
failure. The pump keeps `config.in_flight` chunks read ahead of the write it
is waiting on, so a read and a write overlap instead of taking turns; the
chunk size stays fixed, because the span layout hands every worker a whole
`chunk_size` and a size that moved under it would change that plan.

**Deliverable:** `xrdclient.copy` / `xrdclient.copytree`, any backend to any backend.

1. **`copy/engine.py`** — chunked pump, in-flight window over a multiplexed
   connection, adaptive chunk size, resumption from partial state.
2. **`copy/plans.py`** — recursive walk, parallel file workers,
   `shutil.copytree`-style `ignore=`/`dirs_exist_ok=`.
3. **`copy/progress.py`** — callback protocol, `tqdm` adapter, stderr adapter.
4. Verification preferring server-side checksums on both ends, falling back to
   local computation.
5. **`copy/tpc.py`** — `root://` stock-dialect TPC (destination opened first,
   `tpc.key`/`tpc.src`/`tpc.lfn`/`tpc.stage`/`oss.asize`, `kXR_waitresp`
   deferral) per `libxrdc/lib/xfer/copy_remote.c:136`; and HTTP TPC via `COPY`
   with `TransferHeaderAuthorization` delegation.

**Tests:** byte-for-byte comparison against `/usr/bin/xrdcp` for every
direction; interrupted-and-resumed transfers via the fault proxy; TPC between
two locally launched xrootd servers.

**Done when:** every source/sink pair transfers and verifies, and TPC works
server-to-server. **v0.5.**

---

## Phase 9 — Ecosystem: fsspec, compat shim, CLI

**Deliverable:** the library becomes usable *without changing the calling
code*, which is how it actually gets adopted.

1. **`fsspec_impl.py`** — `XRootDFileSystem(AbstractFileSystem)`: `_open`,
   `ls`, `info`, `cat_file` (range), `cat_ranges` (→ `readv`), `pipe_file`,
   `put`, `get`, `mkdir`, `rm`, `mv`, `created`/`modified`, `checksum`.
   Registered for `root`, `roots`, `xroot`, `xrootd` via entry points.
   Verified with `uproot`, `pandas`, and `pyarrow` reading a real file.
2. **`compat/`** — an `XRootD.client`-shaped shim: `File`, `FileSystem`,
   `URL`, `flags`, returning `(XRootDStatus, result)` tuples with matching
   field names, so existing pyxrootd code runs by changing one import.
   Correctness is asserted by running a script through both.
3. **`cli/`** — `xrd-cp`, `xrd-fs` (subcommands + readline shell), `xrd-cksum`,
   `xrd-stat`, `xrd-ls`, `xrd-mv`, `xrd-rm`; `--json` everywhere; exit codes
   0/1/2; shell completions.

**Done when:** `uproot.open("root://...")` reads a real ROOT file through our
fsspec backend, and a pyxrootd script runs unmodified against `compat`. **v0.6.**

**Status: done**, minus the compat shim, which the decision log dropped on
purpose. As built:

- **`fsspec_impl.py`** — `XRootDFileSystem` for `root`, `roots` and `xroot`,
  plus `HTTPXRootDFileSystem` for `dav`, `davs` and `webdav`: six registered
  schemes, one class each, no extra code. `_open` hands back the library's own
  `io` object rather than an `AbstractBufferedFile` subclass; `cat_ranges`
  groups its ranges per file and issues one `kXR_readv` for each; `checksum`
  is the *server's*, not fsspec's synthetic one. A fully-qualified path to a
  different endpoint is honoured (and its connection cached and closed with the
  filesystem) rather than silently read from the wrong server.
- **`cli/`** — two entry points, not seven. `xrd-fs` carries the sixteen verbs
  (`ls`, `stat`, `cat`, `checksum`, `mkdir`, `rm`, `rmdir`, `mv`, `touch`,
  `df`, `locate`, `ping`, `query`, `xattr`) as subcommands, so `xrd-ls` and
  `xrd-stat` would only have been aliases; `xrd-cp` is `cp` — trailing-slash
  and existing-directory destinations, several sources, `-r`, `-n`, `--tpc`,
  `--verify`, `--chunk-size`, and a progress bar that is 20 lines rather than a
  `tqdm` dependency. `--json`, `-q`, `-v`, `--token`, `--user` and
  `--no-verify-tls` are common to every command; `Endpoints` opens one
  connection per endpoint however many URLs name it. The readline shell and
  shell completions are not built.
- Every subcommand handler is an ordinary `(args, endpoints) -> int` function,
  which is why `tests/test_cli.py` can drive them without a subprocess.

---

## Phase 10 — Hardening, parity, and 1.0

**Deliverable:** the confidence to call it 1.0.

**Status: done.**

1. **Differential parity harness** — done, `tests/test_parity.py`: the same
   operation through both clients against one daemon, asserting the *values*
   agree where the shapes deliberately do not. `tests/test_interop.py` is the
   other half — a real `xrootd`, with stock `xrdcp`/`xrdfs` reading back what
   we wrote. It found a genuine bug the fake could not: `kXR_writev` counted
   its data in `dlen`, which the real server refuses.
2. **Fault-injection sweep** — done, `xrdclient.testing.FaultProxy` (shipped in the
   package, not just the suite) and `tests/test_faults.py`: drop, stall,
   delay, corrupt, chop, rewrite, refuse, cut. Handle recovery, mid-transfer
   redirect and reconnect all drive through it.
3. **Benchmarks** — done, `benchmarks/bench.py` vs `xrdcp`, `xrdfs` and the
   official bindings; numbers in [Performance](../../performance.md). Faster
   on small reads, metadata, listing and copying; 1.6x slower on one large
   streaming read and 4.4x on a vector read. Profiling it found three
   redundant full-buffer copies in the receive path, all removed.
4. **Security review** — done, and not a paperwork exercise. Three real
   findings, each fixed with a test: the redaction filter could miss a secret
   split across a format string and its arguments (and could eat the format
   string itself); SSS keytabs were accepted regardless of permissions, unlike
   the C client; and `parse_dirlist` accepted server-supplied names containing
   `/` or `..`, which `copy_tree` joins onto a local destination. `SECURITY.md`
   states the threat model and what is enforced.
5. **Docs** — done: MkDocs Material, 20 pages, including the API reference,
   the "coming from pyxrootd" mapping, an auth page per mechanism, the tuning
   notes, and the testing and interoperability guides.
6. **Coverage gate** — done: 97% on `proto/`, `crypto/` and `client/`, gated
   at 90% in `pyproject.toml`; `ruff` and `mypy --strict` both pass clean over
   `src/` and are jobs in `.github/workflows/ci.yml`, alongside a
   `mkdocs build --strict` job and an interop job that installs a real
   `xrootd`. Reaching a clean `--strict` was not cosmetic: it turned
   `open_url` from a function returning `io.IOBase` — which has no `read` —
   into one overloaded like the builtin, gave `XRootDPath.fs` a real type
   instead of `Any`, replaced a `lambda` with a default argument in `readv`
   with a named builder, and found `r.Request` used as an annotation in
   `copy/tpc.py` for a name that does not exist in that module.

**Deltas from the plan**

- Parity runs against XRootD **5.9.6**, not 6.0.0 — that is what conda-forge
  ships today. Nothing in the suite is version-specific.
- The fault proxy is written here rather than borrowed from `libxrdc`'s
  `brix_fault_proxy`: it is 340 lines, it has no dependency, and shipping it
  in `xrdclient.testing` means users can break *their* code with it too.
- The "GIL-bound checksum ceiling" turned out not to be worth documenting as a
  ceiling: checksums are asked of the server (`kXR_query`), so the only
  Python-speed path is one the user opts into.

---

## Sequencing

```
0 ─ 1 ─ 2 ─ 3 ─ 4 ─ 5 ─────────────────────── 10
                 │      ╲                    ╱
                 │       6 (HTTP/WebDAV) ───┤
                 ├────── 7 (GSI/krb5) ──────┤
                 └────── 8 (copy/TPC) ──────┤
                          9 (fsspec/CLI) ───┘
```

Phases 0–5 are strictly sequential and form the critical path. 6, 7, and 8 are
independent of one another once Phase 4 lands (8 wants 6 for HTTP sources, but
degrades gracefully to `root://`-only). Phase 9 needs 5. Phase 10 needs
everything.

**Effort shape (relative, not calendar):** Phase 1 is the biggest but is
~60% port-and-extend from `pyxrdcp` and `XRootD.jl`. Phase 2 is the highest-
risk-per-line and deserves the most care. Phase 7 (GSI) was the single largest
piece of genuinely new protocol work, and it came in bigger than planned
because the crypto primitives had to be written too. Phase 5 is large in surface area but low
in risk — it is all local, all testable, and all against the stdlib's own
semantics.

---

## Decision log

| Decision | Choice | Rationale |
|---|---|---|
| Dependency policy | Stdlib core + optional extras | Installs on locked-down grid worker nodes with no wheel access. **As built the extras shrank twice:** HTTP needed none (`http.client`), and neither did GSI once AES/RSA/DER were written in pure Python. What is left is `[krb5]` (gssapi), `[fsspec]`, `[dev]`. |
| Concurrency | Sans-io core, sync + async facades | One protocol implementation, two thin drivers. Async matters for high-fan-out metadata and multi-file reads; sync must stay usable without an event loop (notebooks, scripts). |
| Error model | Exceptions (also `OSError`) + `(status, result)` compat shim | `except FileNotFoundError` must work on remote paths for the API to feel native; the shim preserves the migration path from pyxrootd. |
| Import name | `xrdclient` (dist `xrdclient`) | Verified: official bindings occupy `XRootD`. A distinct name lets both live in one venv, which the parity harness in Phase 10 requires. |
| Python floor | 3.10 | `slots=True` dataclasses, `X | Y` annotations, `match`. 3.9 is EOL. |
| Reference server | Grow `pyxrdcp/tests/_refserver.py`, ship it in `xrdclient.testing` | Offline CI everywhere, plus downstream users can test against it. **As built:** written fresh as `xrdclient.testing.FakeServer`; see the status section. |
| Error model, revised | Exceptions only; no `(status, result)` shim | The shim would reintroduce the ergonomics the project exists to replace, and every call site that wants it can write `try/except` once. |
| TPC dialect | Stock XRootD order only | `libxrdc/lib/xfer/copy_remote.c:154` documents that the legacy full-URL `tpc.src` form fails against stock servers. |
