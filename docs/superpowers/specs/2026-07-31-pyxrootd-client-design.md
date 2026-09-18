# xrdclient — a pure-Python, Pythonic XRootD client

**Date:** 2026-07-31
**Status:** Approved and **implemented through Phase 10**. Sections marked
**As built** describe shipped behaviour and are authoritative where they
differ from the surrounding prose; everything else is design that the code
now follows. User-facing documentation lives in `docs/`; this file is the
design record.
See the [roadmap status table](../plans/2026-07-31-pyxrootd-client-roadmap.md#status-as-built).
**Distribution name:** `xrdclient` · **Import name:** `xrdclient`

---

## 1. Goal

A pure Python 3 client library for XRootD storage that is *idiomatic Python
first* and *protocol-complete second* — in that order, without sacrificing the
second. It must:

- Speak `root://` and `roots://` (the XRootD binary protocol, TLS included)
  natively, with no dependency on the C++ `XrdCl` library or its SWIG bindings.
- Speak `https://` / `davs://` — XRootD's HTTP door (XrdHttp) and generic HEP
  WebDAV endpoints (dCache, StoRM, EOS, Echo) — including the WebDAV verbs,
  `Want-Digest` checksums, and HTTP third-party copy.
- Support the **full** authentication ladder used in production HEP: `unix`,
  `host`, `ztn`/WLCG bearer tokens, `sss`, GSI/X.509 proxies, Kerberos 5, plus
  `kXR_sigver` request signing and mutual-TLS.
- Support **full read and write**: not merely `read`/`write` but `readv`,
  `writev`, `pgread`/`pgwrite` (per-page CRC32c), `clone`, `sync`, `truncate`,
  `chkpoint`, and the complete filesystem operation set including extended
  attributes.
- Feel like the standard library. `open()` returns a real `io` object;
  paths behave like `pathlib`; failures raise exceptions that are also
  `OSError`s; everything that holds a resource is a context manager; iteration
  is lazy; type hints are complete and checked.

### Non-goals

- FUSE mounts and the `LD_PRELOAD` POSIX shim (that is `libxrdc`'s territory).
- Being an XRootD *server*. (`testing/` ships a protocol-correct reference
  server, but only as a test fixture.)
- ROOT file format parsing — that is `uproot`'s job, and we integrate with it
  via `fsspec` rather than duplicating it.
- Replacing the official `XRootD` Python bindings for users who want them; we
  ship a compatibility shim and can coexist in the same virtualenv (verified:
  the official package imports as `XRootD`, we import as `xrdclient`).

---

## 2. References and attribution

Four existing codebases in this workspace are the source of truth. The
protocol understanding, resilience design, auth flows, and CLI semantics
originate in **`libxrdc`** (the pure-C client at
`../nginx-xrootd/client/`, ~70 kLOC); the Julia and Python ports below are
themselves translations of it. That attribution is carried forward: module
docstrings for translated designs cite the corresponding `libxrdc` source, and
the package documentation credits it as prior art.

| Reference | Location | Size | Role in this project |
|---|---|---|---|
| **`libxrdc`** (C) | `../nginx-xrootd/client/{lib,apps}` | ~70 kLOC | **Ground truth.** `lib/protocols/root/` (frame + ops), `lib/protocols/http/` (WebDAV/webfile), `lib/auth/{sec,cred,gsi,sss}/` (the complete auth ladder), `lib/net/{resilient,cpool,tls}.c` (resilience), `lib/xfer/copy_*.c` (copy engine + TPC dialect). |
| **`XRootD.jl`** (Julia) | `../XRootD.jl/src` | 5.2 kLOC | **Primary architectural model.** Five-layer split, threaded multiplexer, `Storage` backend abstraction, web/S3 backends. Its design spec (`docs/superpowers/specs/2026-07-01-*.md`) is the template for this one. |
| **`pyxrdcp`** (Python) | `../pyBall/pyxrdcp` | 3.1 kLOC | **Direct code reuse.** A working, tested, stdlib-only Python XRootD implementation (wire codecs, bring-up, `sss`/Blowfish, `sigver`, File/FileSystem, copy). Verified against xrootd v5.9.5. See §3. |
| **`go-hep/xrootd`** (Go) | `../go-hep/xrootd` | 20.8 kLOC | **Cross-check + API idiom.** `xrdproto/*` is one package per opcode — the most complete open wire reference (incl. `fattr`, `pgread`, `pgwrite`, `verifyw`, `chkpoint`, `signing`). `xrdio` shows the "implement the language's io interfaces" pattern we mirror with Python's `io`. `xrdproto/auth/{gsi,krb5,sss,token,unix,host}` covers every mechanism. |

Additionally available locally and used as **test oracles**, not dependencies:
`/usr/bin/xrootd` (server v6), `/usr/bin/xrdcp`, `/usr/bin/xrdfs`, and the
official `XRootD` 6.0.0 Python bindings.

---

## 3. What already exists — reuse assessment

`pyxrdcp` is not a sketch; it is a working client with an offline test suite
and verified interop against a real xrootd server. It contributes roughly
**1,800 LOC of directly portable protocol code**, which removes most of the
risk from Phase 1–2.

| `pyxrdcp` module | LOC | Disposition |
|---|---:|---|
| `wire/constants.py` | 185 | **Adopt**, extend to the full opcode/flag set (`fattr`, `chkpoint`, `gpfile`, `clone`, `bind`, `set`, `statx`, prepare/locate flags). |
| `wire/frames.py` | 58 | **Adopt**, add a zero-copy `memoryview` cursor and the `kXR_status` paged-frame trailer. |
| `wire/requests.py` | 280 | **Adopt** as the base for ~20 more opcodes. Encoder pattern (`request_id`/`body`/`payload`/`trailer`) is sound; keep it. |
| `wire/responses.py` | 140 | **Adopt**, extend (`statx`, `fattr`, `locate` deep, `prepare`, `chkpoint`, `pgread` page CRCs). |
| `session/blowfish.py` | 118 | **Adopt verbatim.** Pure-Python Blowfish; no stdlib equivalent exists. |
| `session/sss.py` | 131 | **Adopt.** Keytab parse + credential build, IEEE CRC-32 (note: *not* CRC32c). |
| `session/sigver.py` | 57 | **Adopt**, extend to `kXR_nodata_sig` and full sec-level policy. |
| `session/auth.py` | 82 | **Refactor** into a pluggable mechanism registry (§7); logic for ztn/sss/unix carries over. |
| `session/connection.py` | 218 | **Rewrite.** Single-stream blocking `roundtrip` is correct for `xrdcp` but cannot support concurrent streams, async, reconnect-with-replay, or `kXR_waitresp`. This is the sans-io refactor (§5). |
| `client/{file,filesystem,status}.py` | 258 | **Rewrite** against the new API contract (exceptions, dataclasses, `io`). Operation-to-request mapping carries over. |
| `tools/crc32c.py` | 21 | **Adopt**, add a hardware-accelerated path with this as fallback. |
| `tools/copy.py`, `storage/*` | 204 | **Supersede** by the copy engine (§10) — but keep the chunked-pump semantics. |
| `tests/_refserver.py` | 307 | **Adopt and grow substantially** — this is the offline test backbone (§12). |

**Net:** the wire and crypto layers are largely solved. The genuinely new work
is the sans-io session machine, the async facade, GSI/Kerberos, the HTTP/WebDAV
protocol peer, and the entire Pythonic surface.

---

## 4. Architecture

Seven layers, strictly one-directional dependencies. The lower four are
**sans-io**: they contain no sockets, no `asyncio`, no blocking calls, and are
therefore trivially unit-testable and shared verbatim between the sync and
async facades.

```
┌ L7  cli/ · fsspec_impl.py               entry points and ecosystem adapters
├ L6  copy/                               copy engine, TPC, recursive plans, progress
├ L5  path.py · io/                       XRootDPath, io.RawIOBase, readahead, vector I/O
├ L4  client/ · http/                     FileSystem/File ops; WebDAV/XrdHttp peer
├ L3  session/                            reader loop, streamid mux, pool, reconnect  ← I/O lives here
├ L2  transport/ · auth/                  sockets & TLS; auth mechanism registry
└ L1  proto/ · crypto/                    frame codecs, SessionMachine, ciphers, checksums
                                          ── PURE: no I/O, no clock, no globals ──
```

### 4.1 Module tree

```
src/xrdclient/
  __init__.py          public surface: open, connect, FileSystem, File, XRootDPath,
                       copy, copytree, checksum, errors, flags, aio
  url.py               XRootDURL: parse/format, CGI (?authz=, tpc.*), user@host, IPv6
  errors.py            exception hierarchy + kXR_error/errno/HTTP-status mapping
  types.py             frozen slotted dataclasses: StatInfo, DirEntry, VFSInfo,
                       LocationInfo, ProtocolInfo, ChecksumInfo, XAttr, PrepareInfo
  flags.py             IntFlag/IntEnum: OpenFlags, Access, DirListFlags, QueryCode,
                       PrepareFlags, StatInfoFlags, LocateFlags, FattrCode
  config.py            Config dataclass; XRD_*/XrdSec* env resolution; contextvar override
  _log.py              logging.getLogger("xrdclient.*") helpers, redaction of secrets

  proto/                       ── L1, sans-io ──
    constants.py               every kXR_* opcode, status, flag (full set)
    buffer.py                  big-endian read/write cursor over memoryview
    frames.py                  request header (24B), response header (8B), handshake
    requests.py                one encoder per opcode
    responses.py               one decoder per response
    machine.py                 SessionMachine — the sans-io state machine (§5)

  crypto/                      ── L1, pure ──
    crc32c.py adler32.py crc64.py    checksums (+ hw-accel probe, pure fallback)
    blowfish.py sss.py sigver.py     sss credential + request signing
    der.py                           strict DER reader (PKCS#1/#8, X.509)
    aes.py rsa.py                    the GSI session cipher and signature
    x509.py                          proxy chain load/parse   no extra: pure Python

  auth/                        ── L2 ──
    base.py        AuthMechanism protocol, Registry, negotiation ladder
    unix.py host.py ztn.py sss.py gsi.py krb5.py
    tokens.py      WLCG/SciToken discovery, claim decode, expiry-aware refresh

  transport/                   ── L2, the only place sockets exist ──
    base.py        Transport protocol (send/recv/close/upgrade_tls)
    sync.py aio.py tls.py
                   as built: sync.py (socket + ssl, TLS inline) and memory.py
                   (the in-process transport the tests drive). No aio.py — the
                   async surface is xrdclient/aio.py, one module, see below.

  session/                     ── L3 ──
    sync.py        Session: reader thread + streamid mux + keepalive
    aio.py         AsyncSession: reader task, same machine
    pool.py        ConnectionPool keyed by (scheme,host,port,user,cred-fingerprint)
    resilience.py  retry/backoff/jitter, idempotent replay, redirect budget
                   as built: sync.py and router.py (dispatch, redirect budget,
                   retry). Pooling, resilience and aio.py are not yet written.

  aio.py                       ── L5, the async facade ── (as built)
                 AsyncFile / AsyncFileSystem / open / copy / copy_tree /
                 third_party: an explicit typed mirror of the whole surface
                 that hands each call to asyncio.to_thread. One protocol
                 implementation, no drift. Reached by module __getattr__, so
                 `import xrdclient` does not import asyncio.

  client/                      ── L4 ──
    filesystem.py  FileSystem / AsyncFileSystem  (stat, dirlist, mkdir, rm, mv,
                   chmod, truncate, locate, query, prepare, statvfs, xattr, ping)
    file.py        File / AsyncFile (open, read, readv, pgread, write, writev,
                   pgwrite, clone, sync, truncate, chkpoint, fcntl/visa,
                   xattr, close)

  http/                        ── L4', the WebDAV/XrdHttp peer ── (as built)
    client.py      pooled HTTP/1.1 client on http.client: keep-alive, bearer
                   tokens, X.509 proxies, redirects, one stale-connection retry,
                   HTTP status → the same exception table root:// uses
    file.py        HTTPRawIO + open_http: ranged GET, buffered/chunked PUT
    dav.py         PROPFIND/MKCOL/MOVE/DELETE/OPTIONS + Want-Digest + macaroon
                   minting, and HTTPFileSystem(FileSystem)

  io/                          ── L5 ──
    raw.py         XRootDRawIO(io.RawIOBase) — the io-stack entry point
    buffered.py    readahead window, write-behind, adaptive chunk sizing
    vector.py      readv/pgread chunk scheduler, in-flight window

  path.py                      ── L5 — XRootDPath (pathlib semantics) ──

  copy/                        ── L6 ── (as built)
    engine.py      copy / copy_tree / CopyResult: any endpoint to any endpoint
    tpc.py         third_party: the stock root:// rendezvous dialect, and the
                   scheme dispatch to http/tpc.py for an HTTP pair

  fsspec_impl.py               ── L7 (as built) — XRootDFileSystem for root/roots/
                               xroot and HTTPXRootDFileSystem for dav/davs/webdav
                               [extra: fsspec] ──
  cli/                         ── L7 (as built) ──
    __init__.py    Endpoints (one FileSystem per endpoint), --json encoder,
                   size_arg, common flags, exit codes
    fs.py          xrd-fs: ls stat cat checksum mkdir rm rmdir mv touch df
                   locate ping query xattr
    cp.py          xrd-cp: cp semantics, -r, -n, --tpc, --verify, progress bar
  testing/                     shipped fixtures (as built): FakeServer (root://),
                               FakeDAVServer (http/WebDAV)
```

---

## 5. The sans-io core (`proto/machine.py`)

This is the central design decision and the one that makes "sync core, async
facade" cost one implementation instead of two. It follows the `h11`/`hyper`
sans-io pattern.

`SessionMachine` owns **all** protocol state and knows nothing about how bytes
move:

```python
class SessionMachine:
    def submit(self, req: Request) -> StreamId: ...      # allocate streamid, queue frame
    def data_to_send(self) -> bytes: ...                 # drain the outbound queue
    def receive_data(self, data: bytes | None) -> None: ...  # feed inbound; None = EOF
    def next_event(self) -> Event | None: ...            # pop a completed event
    @property
    def wants_tls(self) -> bool: ...                     # negotiated upgrade request
    def timers(self) -> Timers: ...                      # deadlines the driver must honor
```

Events: `ResponseReady(streamid, payload)`, `PartialData(streamid, chunk)`,
`Redirect(streamid, url, wait)`, `WaitRetry(streamid, seconds)`,
`WaitResp(streamid, timeout)`, `AuthChallenge(blob)`, `TlsUpgrade`,
`ServerAttn(code, payload)`, `Fatal(error)`.

The machine handles, entirely in pure code:

- Handshake, `kXR_protocol` capability negotiation, TLS-required decision,
  `kXR_login`, and the multi-round `kXR_authmore` challenge/response loop
  (`sss` and GSI both need more than one round — `pyxrdcp` only does one).
- **Stream multiplexing**: streamid allocation/recycling, routing each response
  to the right waiter. This is what makes concurrent reads on one socket
  possible and is the main functional gap in `pyxrdcp`.
- `kXR_oksofar` accumulation, `kXR_status` paged-result assembly including the
  page-CRC trailer, `kXR_attn`/`kXR_asynresp` unwrapping, `kXR_waitresp`
  parking, `kXR_wait` retry scheduling, `kXR_redirect` with a bounded budget.
- `kXR_sigver` prefix emission when the negotiated security level demands it,
  with sequence-number management.

Because it is pure, the entire protocol layer is testable by feeding byte
strings — including every error path, truncation, and interleaving that is
almost impossible to provoke against a live server.

**Drivers.** `session/sync.py` runs one reader thread per connection, pumping
`receive_data` and dispatching events to `threading.Event`-backed waiters.
`session/aio.py` runs one `asyncio` task doing the same with futures. Both are
~200 LOC, share zero protocol logic, and are verified against each other by
running the same test matrix twice.

*As built: there is one driver.* The second one is what the anti-drift test
above exists to police, and the cheapest way to pass that test is not to write
it: `xrd/aio.py` gives the async surface by delegating to the sync driver
through `asyncio.to_thread`. Nothing blocks the loop, `asyncio.gather` across
endpoints is genuinely concurrent, and every fix lands in one place. If a
future profile shows the executor is the bottleneck for some workload, a real
`session/aio.py` can slot underneath `xrd/aio.py` without changing a caller.

**The `on_chunk` contract.** `Session.execute(request, on_chunk=...)` hands the
body to the caller in pieces as `kXR_oksofar` frames arrive, *including the
final piece*, so `b"".join(chunks) == result.data` exactly — a caller can
stream a multi-gigabyte read straight to a file without the joined copy being
short. The machine's terminal event carries the whole body, so the driver
tracks how much it has already handed over and emits only the tail; a
`kXR_wait` that causes a resend resets that counter, because the body starts
over. The accumulated body is returned either way, so `on_chunk` is purely
additive.

**Two layers, deliberately.** `Session` owns one socket and one machine and
does not retry anything. `Router` (`session/router.py`) sits above it and owns
the policies that need a *new* socket: reconnect-and-replay of idempotent
requests up to `config.connect_retries`, redirect following with a bounded
budget, and `pin()` — a router locked to the endpoint that answered, which is
what a `File` switches to after `kXR_open` so every later operation on that
handle stays on the server that issued it.

### 5.1 Concurrency and resilience

- **One connection, many streams.** Operations acquire a streamid, submit, and
  await. The copy engine and vector reader exploit this to keep N requests in
  flight on a single socket, which is how the C client saturates a 10 GbE link.
- **Pooling.** `ConnectionPool` keys on `(scheme, host, port, username,
  credential fingerprint)`, reference-counts sessions, enforces an idle TTL and
  a per-endpoint cap, and runs keepalive `kXR_ping` on idle connections.
  Translated from `lib/net/cpool.c`.
- **Reconnect with replay.** On transport failure, `resilience.py` reconnects
  within a bounded window (exponential backoff + jitter) and replays only
  *idempotent* in-flight requests; non-idempotent ones (`write`, `pgwrite`,
  `truncate`, `mv`, `rm`) fail with a `TransientError` that records how much was
  known-committed, so the copy engine can resume rather than restart.
  Translated from `lib/net/resilient.c`.
- **File handle recovery.** After reconnect, open handles are invalid. Handles
  record their open parameters and transparently re-open at the same offset
  when the reconnect succeeds — matching `XrdCl`'s behavior. Files opened
  `kXR_new` are *not* silently re-opened (that would clobber); they surface an
  error instead.
- **Cancellation.** Every blocking call takes `timeout=`; the async API honors
  `asyncio.CancelledError` and unregisters the streamid cleanly rather than
  leaking a waiter.

---

## 6. The Pythonic surface

This is what the project is *for*. Four levels of abstraction, each complete on
its own, each a thin layer over the one below.

### 6.1 Level 1 — `xrdclient.open()` returns a real file object

`XRootDRawIO` subclasses `io.RawIOBase` and implements `readinto`, `write`,
`seek`, `tell`, `truncate`, `readable`, `writable`, `seekable`, `fileno`
(raising, correctly), and `close`. Because that contract is honored,
`io.BufferedReader`/`BufferedWriter`/`TextIOWrapper` compose on top **for
free**, and with them: `readline()`, iteration, `read()` semantics, `peek()`,
universal newlines, and encoding.

```python
import xrdclient

with xrdclient.open("root://eos.example.org//store/data/run42.root", "rb") as f:
    header = f.read(512)
    f.seek(-1024, os.SEEK_END)
    trailer = f.read()

# text mode, line iteration — nothing XRootD-specific in sight
with xrdclient.open("root://host//store/log.txt", "rt", encoding="utf-8") as f:
    for line in f:
        if "ERROR" in line:
            print(line)

# zero-copy into a caller's buffer
buf = bytearray(1 << 20)
n = f.readinto(buf)

# write, with the same mode strings the stdlib uses
with xrdclient.open("root://host//store/out.dat", "wb") as f:
    f.write(payload)
```

Modes: `r`/`w`/`x`/`a` × `b`/`t` × `+`, mapped onto `OpenFlags`
(`x` → `kXR_new`, `w` → `kXR_delete`, `a` → `kXR_open_apnd`, `+` →
`kXR_open_updt`) exactly as `builtins.open` maps them onto `O_*`.

**As built.** `xrdclient.open` is `xrdclient.io.open_url`, and it returns exactly what the
builtin returns for the same mode: `BufferedReader`, `BufferedWriter`,
`BufferedRandom`, `TextIOWrapper`, or — with `buffering=0` — the raw
`XRootDRawIO`. **Text is the default**, so `open(url, "r")` gives a
`TextIOWrapper`, and the same two errors the builtin raises are raised here:
unbuffered text, and an `encoding` in binary mode. The default buffer is 1 MiB
rather than the stdlib's 8 KiB, because the round trip is a wide-area one.

Two subtleties the tests pin, both of them the kind that only bite in
production: `RawIOBase.close()` flushes *before* it closes, so the underlying
`File` handle must outlive `super().close()` — otherwise every buffered writer
raises on exit; and `flush()` on a closed or already-finished stream is a
no-op rather than an error.

### 6.2 Level 2 — `XRootDPath`, a `pathlib`-shaped remote path

```python
from xrdclient import XRootDPath

store = XRootDPath("root://eos.example.org//store/data")

for p in (store / "2026").iterdir():          # lazy, streams the dirlist
    if p.suffix == ".root" and p.stat().st_size > 1 << 30:
        print(p.name, p.stat().st_mtime)

for p in store.glob("run*/*.root"):           # server-side dirlist, client-side match
    ...

(store / "new").mkdir(parents=True, exist_ok=True)
(store / "note.txt").write_text("hello\n")
data = (store / "note.txt").read_bytes()
(store / "note.txt").rename(store / "note.bak")
(store / "note.bak").unlink(missing_ok=True)

store.exists(); store.is_dir(); store.is_file()
```

**As built:** a standalone class, deliberately *not* a `pathlib.PurePath`
subclass — `PurePath`'s private extension points (`_flavour`,
`_from_parsed_parts`, `with_segments`) shift between point releases, and
inheriting from it would trade a stable surface for a fragile one. It carries
the same method names, so calling code ports by changing one constructor.

`_derive()` is the propagation point that `with_segments()` would have been:
every path produced by `/`, `.parent`, `.parents`, `with_name`, `iterdir`,
`glob`, `walk`, and `rename` inherits the endpoint, the `Config`, **and the
already-open connection**. That last part is load-bearing — without it,
`for entry in base.iterdir(): entry.stat()` opens one socket per entry. A
path is therefore also a context manager, and `close()` releases the
connection it and its derivatives share; using any of them afterwards simply
reconnects.

`XRootDURL` composes the same way — `srv.url / "data" / "a.root"` — so the
layer below the path object is spelled like the layer above it.

Supports `rglob`, `walk`, `touch`, `chmod`, `unlink(missing_ok=)`,
`rename`/`replace`, `checksum`, `locate`, and the `read_*`/`write_*`
conveniences. `stat()` returns a `StatInfo` that is `os.stat_result`-compatible
in field names (`st_size`, `st_mtime`, `st_mode`), so downstream code that
inspects stat results works unmodified.

### 6.3 Level 3 — `FileSystem` and `File` for protocol-level control

When you need the operations that have no `pathlib` analogue:

```python
with xrdclient.FileSystem("root://eos.example.org") as fs:      # connects lazily
    fs.ping()
    info      = fs.stat("/store/f.root")
    entries   = fs.iterdir("/store")                      # generator
    cksum     = fs.checksum("/store/f.root", "adler32")   # -> ChecksumInfo
    locations = fs.deep_locate("/store/f.root")
    space     = fs.statvfs("/store")
    fs.prepare(["/store/a.root", "/store/b.root"])         # stages by default
    settings  = fs.query_config("role", "version")        # kXR_Qconfig

    fs.setxattr("/store/f.root", "user.run", b"42")
    attrs = fs.xattrs("/store/f.root")                    # name -> value

f = xrdclient.File("root://eos.example.org//store/f.root")
f.open(OpenFlags.UPDATE)                                  # READ if you omit it
with f:                                                   # closes on exit
    # vector read: one round trip for many scattered ranges
    chunks = f.readv([(0, 4096), (1 << 30, 4096), (2 << 30, 8192)])
    # paged read with per-4KiB CRC32c verification, protocol v5
    pages = f.pgread(1 << 20, 0, verify=True)             # -> PageResult
    with f.checkpoint():                                  # commit or roll back
        f.pwrite(b"...", 0)
```

`File` takes `(size, offset)` in that order — the argument that varies is
first, and `offset` defaults to `0`, so `f.read()` means what it does on a
local file. `checksum()` returns a `ChecksumInfo` whose field is `algorithm`;
`type` is a builtin and never a field name here.

`AsyncFileSystem`/`AsyncFile` mirror this exactly under `xrdclient.aio`, with
`async with`, `await`, and `async for` on the generators.

**As built.** The names follow the sync surface rather than diverging:
`xrdclient.aio.FileSystem` (aliased `AsyncFileSystem`), not `connect`; `scandir` and
`iterdir`, not `dirlist`. `xrdclient.aio.open` is awaitable *and* an async context
manager, so both spellings below work. Every method delegates to the sync core
through `asyncio.to_thread`, so the event loop is never blocked, separate
endpoints genuinely overlap, and calls on one endpoint serialise on that
session's lock — which is what a single socket would have given anyway.

```python
async with xrdclient.aio.FileSystem("root://host") as fs:
    async for entry in fs.iterdir("/store"):
        ...
    sizes = await asyncio.gather(*(fs.getsize(p) for p in paths))

    async with fs.open("/store/f.root") as f:            # or: f = await fs.open(...)
        head, tail = await f.readv([(0, 4096), (1 << 20, 4096)])

await xrdclient.aio.copy("root://a//store/f.root", "davs://b/store/f.root")
```

`xrdclient.aio` is resolved lazily by a module-level `__getattr__`, so a program that
never mentions it never imports `asyncio`.

### 6.4 Level 4 — ecosystem integration

```python
# fsspec — unlocks uproot, pandas, dask, pyarrow, and fsspec's own caching
import fsspec
with fsspec.open("root://eos.example.org//store/f.root", "rb") as f:
    ...
import uproot
tree = uproot.open("root://eos.example.org//store/f.root")["Events"]
```

`XRootDFileSystem(AbstractFileSystem)` implements `_open`, `ls`, `info`,
`cat_file` (with `start`/`end` → range read), `cat_ranges` (mapped onto
`readv` — a genuine performance win over per-range HTTP), `pipe_file`, `put`,
`get`, `mkdir`, `rm`, `mv`, `created`/`modified`, and `checksum` (returning the
server-side checksum rather than hashing locally). Registered under the
`root`, `roots`, `xroot`, and `xrootd` protocols via entry points.

**As built** (`src/xrdclient/fsspec_impl.py`, 290 lines):

- Six schemes, two classes: `XRootDFileSystem` for `root`/`roots`/`xroot` and
  `HTTPXRootDFileSystem` — a subclass that changes nothing but `protocol` —
  for `dav`/`davs`/`webdav`. Scheme dispatch already happens inside
  `FileSystem.__new__`, so the HTTP binding is three lines.
- `_open` returns **the library's own file object**, not an
  `AbstractBufferedFile` subclass. `XRootDFile` is already an `io` object with
  buffering, `readinto`, text-mode wrapping and vector reads; re-deriving that
  inside fsspec's cache layer would be slower and would have to be kept in
  step. `block_size` becomes `buffering`.
- `cat_ranges` groups the requested ranges by file and issues one
  `kXR_readv` per file, falling back to seek/read on endpoints (HTTP) that
  have no vector read. That is the method the columnar readers actually call.
- `_strip_protocol` and `_get_kwargs_from_urls` split a URL into an endpoint
  and a path, so fsspec's instance cache keys on the endpoint and repeated
  `fsspec.open` calls against one server share one connection.
- A fully-qualified path naming a *different* endpoint is honoured rather than
  read from the wrong server; those filesystems are cached alongside the
  primary one and closed with it.
- `mkdir`/`makedirs`/`rmdir`/`rm`/`mv`/`touch`/`pipe_file` are the mutation
  surface; `rm(..., recursive=True)` is `rmtree`. `info` reports fsspec's
  `{"name", "size", "type", "mtime", "mode"}`.
- Not implemented: `put`/`get` — `AbstractFileSystem`'s generic
  implementations are correct here, and `xrdclient.copy` is the faster path when a
  caller wants one.

### 6.5 Cross-cutting Pythonic commitments

| Concern | Commitment |
|---|---|
| Errors | Exceptions, always. Hierarchy in §8. Never a sentinel, never a `(status, result)` tuple in the native API. |
| Resources | Everything holding a socket or handle is a context manager and has a finalizer that warns via `ResourceWarning` if leaked. |
| Types | Full annotations, `py.typed`, `mypy --strict` clean in CI. Public dataclasses are `frozen=True, slots=True`. |
| Enums | `enum.IntFlag`/`IntEnum` with `|` composition and readable `repr` — not bare module-level ints in the public API. |
| Iteration | `dirlist`, `glob`, `walk`, `readchunks` are generators; nothing materializes a large listing unless asked. |
| Logging | `logging` under the `xrdclient.` hierarchy, with a filter that redacts tokens, keytab material, and proxy contents. Zero `print()` outside `cli/`. |
| Config | A `Config` dataclass; env resolution honors the official client's `XRD_*` variables plus `BEARER_TOKEN*`, `X509_USER_PROXY`, `XrdSecSSSKT`. Overridable per-call and per-context via `contextvars`. |
| Buffers | Public read APIs accept and return `memoryview` where zero-copy matters; `readinto` never allocates. |
| Repr | Every public object has a `__repr__` that round-trips or is genuinely diagnostic, with secrets elided. |
| Naming | PEP 8 throughout. `kXR_*` names exist only inside `proto/`. |

---

## 7. Authentication — the full ladder

`auth/base.py` defines a mechanism protocol and a registry, so a mechanism is
a self-contained plugin rather than a branch in a `connect()` function:

```python
class AuthMechanism(Protocol):
    name: str                     # wire name: "unix", "ztn", "sss", "gsi", "krb5", "host"
    def available(self, cfg: Config) -> bool: ...
    def initiate(self, params: str, ctx: AuthContext) -> bytes: ...
    def respond(self, challenge: bytes, ctx: AuthContext) -> bytes | None: ...
    def session_key(self) -> bytes | None: ...        # feeds kXR_sigver
```

Negotiation parses the `&P=...` security trailer from the login response and
tries mechanisms by configured preference (default: `gsi` > `ztn` > `krb5` >
`sss` > `unix` > `host`), falling through on failure and reporting every
attempt in the final error. Multi-round `kXR_authmore` is driven by `respond()`
until it returns `None`.

| Mechanism | Source of truth | Notes | Extra |
|---|---|---|---|
| `unix` | `pyxrdcp/session/auth.py`, `sec_unix.c` | Username credential. Trivial, already working. | — |
| `host` | `sec_host.c`, go-hep `auth/host` | Hostname-based; no credential material. | — |
| `ztn` | `sec_token.c`, `cred_bearer.c` | WLCG/SciTokens bearer. Discovery order: explicit arg → `BEARER_TOKEN` → `BEARER_TOKEN_FILE` → `$XDG_RUNTIME_DIR/bt_u<uid>` → `/tmp/bt_u<uid>`. Also the `?authz=Bearer%20<jwt>` opaque-CGI path for gateway endpoints. Decodes `exp` and refuses/refreshes expired tokens rather than failing at the server. | — |
| `sss` | `pyxrdcp/session/{sss,blowfish}.py`, `sss_keytab.c` | Keytab parse, Blowfish, IEEE CRC-32. **Already implemented.** Extend to multi-round and keytab key selection by ID. | — |
| `gsi` | `sec_gsi.c` (451 L), `gsi/proxy.c`, `cred_x509.c`, go-hep `auth/gsi` | The largest new piece: RFC 3820 proxy chain load from `X509_USER_PROXY`/`/tmp/x509up_u<uid>`, DER cert exchange, the XrdSecgsi handshake rounds, signed challenge, delegation. **Built with no extra** — see below. | — |
| `krb5` | `sec_krb5.c`, go-hep `auth/krb5` | GSSAPI token exchange against the `xrootd/<host>` service principal, credential cache discovery via `KRB5CCNAME`. | `[krb5]` → `gssapi` |
| `pwd` | `sec_pwd.c` | Legacy; implemented last, or declined with a clear error. | — |

**As built.** Every mechanism in the table ships except `pwd`, and five of the
six are pure Python: `unix`, `host`, `ztn`, `sss` and — the change from the
plan — `gsi`. `auth/__init__.py` registers them unconditionally, with no
`try: import` around anything, because there is nothing left to fail.

`gsi` needed `cryptography` only for AES, RSA and DER, so `crypto/aes.py`
(FIPS-197), `crypto/rsa.py` (PKCS#1 v1.5) and `crypto/der.py` were written
instead — about 660 lines that make the *whole* ladder installable on a
worker node with no wheel access. Diffie-Hellman is `pow(g, x, p)` on Python
integers; the proxy chain is echoed to the server verbatim, so the client
never has to verify X.509, only read it. The **`[gsi]` extra no longer
exists.** What is implemented is the unsigned-DH path (advertised version
10300); signed DH and `kXGS_pxyreq` delegation are refused **by name** rather
than mis-answered.

`krb5` is the one exception, deliberately. Its FILE credential-cache reader
is pure Python — that is what turns "authentication failed" into "your ticket
expired 40 minutes ago", and it works with no extra — but the AP-REQ itself
comes from `gssapi`. A Kerberos exchange this client built could only be
validated against a live KDC, and a security mechanism whose only test is its
own decoder is worse than none. The mechanism still registers without the
extra: with a live ticket and no `gssapi`, `available()` raises a
`CredentialError` naming the ticket holder, which the ladder turns into a
reason in `NoMechanismError` rather than a silent fall-through to `unix`.

One asymmetry is worth stating because it is a real failure mode, not a
detail: `host` is a **one-shot** credential. It has nothing to say to a second
`kXR_authmore`, so a server that demands more than one round falls through to
the next mechanism and, if none is left, raises `NoMechanismError` naming
every attempt. `sss`, `gsi` and `krb5` are the multi-round ones.

**Request signing (`kXR_sigver`).** When the negotiated security level requires
it, mutating requests are prefixed with a SHA-256 signature over
(seqno ‖ header ‖ payload) keyed by the mechanism's session key. Base already
in `pyxrdcp/session/sigver.py`; extend to full per-level policy
(`kXR_signIgnore`/`Likely`/`Compat`) and `kXR_nodata_sig`.

**TLS.** `roots://` forces it; `kXR_gotoTLS`/`kXR_tlsLogin` trigger in-protocol
upgrade. `transport/tls.py` builds `SSLContext` with proper hostname
verification (off only behind an explicit `insecure=True`), CA discovery from
`X509_CERT_DIR`/`SSL_CERT_FILE`/certifi, mutual TLS from a proxy or
cert+key pair, ALPN, session resumption, and separate control/data TLS policy.

**HTTP-side auth** (`http/`): `Authorization: Bearer` (same token discovery),
macaroon minting via `POST` with a caveat request and automatic attenuation for
TPC delegation, mutual-TLS with the same proxy material, and `?authz=` CGI.

---

## 8. Error model

```
XRootDError(Exception)
├─ ProtocolError            malformed frame, unexpected opcode, version mismatch
├─ ConnectionError(          , builtins.ConnectionError)
│   ├─ TimeoutError(        , builtins.TimeoutError)
│   └─ TransientError        retryable; carries .attempts and .committed_bytes
├─ AuthenticationError       ├─ NoMechanismError  ├─ CredentialError  ├─ TokenExpiredError
├─ ServerError               a kXR_error the server reported; carries .code, .message
│   └─ (mapped subclasses, all also OSError):
│      FileNotFoundError · FileExistsError · PermissionError · IsADirectoryError ·
│      NotADirectoryError · OSError(ENOSPC) · InterruptedError
├─ RedirectLimitError
└─ ChecksumMismatchError     carries .expected, .actual, .algorithm
```

Every `ServerError` retains the raw `kXR_error` code and server message; the
`OSError`-compatible subclasses set `errno` so that `except FileNotFoundError`
works exactly as it does on local files. HTTP status codes map into the same
tree (404 → `FileNotFoundError`, 403 → `PermissionError`, 507 → `ENOSPC`), so
callers write one `except` block regardless of transport.

**As built.** The `kXR_*` → exception table lives in `xrd/errors.py`, not in
`proto/constants.py`: the error codes are an error-model concern and share
numeric values with unrelated opcodes (`3011` is both `kXR_NotFound` and
`kXR_ping`), so keeping them apart is what stops the two from ever being
confused. `ChecksumMismatchError` is deliberately **not** an `OSError` — no
`errno` describes "the bytes disagree", and catching it should be a conscious
act, not a side effect of `except OSError`.

The `(status, result)` compat shim in the decision log was **dropped**.
Returning a status tuple is precisely the ergonomics this library exists to
replace, and a caller who wants one can write the `try`/`except` once.

---

## 9. Protocol coverage

Full opcode set, `pyxrdcp` status → target. Wire layouts cross-checked against
all three references; go-hep's `xrdproto/*` is the tie-breaker.

| Group | Operations | Have | Add |
|---|---|---|---|
| Session | `protocol`, `login`, `auth`, `ping`, `endsess`, `bind` | ✔ (first 4) | `endsess`, `bind` (multi-stream data connections) |
| Metadata | `stat`, `statx`, `dirlist`, `locate`, `query`, `prepare` | ✔ (stat, dirlist, query) | `statx`, deep `locate`, `prepare` + flags, `query` all codes (`Qcksum`, `Qspace`, `Qconfig`, `Qxattr`, `Qvisa`, `Qopaque*`) |
| Namespace | `mkdir`, `rm`, `rmdir`, `mv`, `chmod`, `truncate` | ✔ (mkdir, rm, rmdir, mv, truncate) | `chmod`; path-level `truncate` |
| File I/O | `open`, `close`, `read`, `write`, `sync` | ✔ | — |
| Vector/paged | `readv`, `writev`, `pgread`, `pgwrite`, `verifyw`, `chkpoint` | ✗ | **all** — `readv` and `pgread` are the performance-critical ones |
| Xattr | `fattr` get/set/list/del | ✗ | all four |
| Advanced | `gpfile`, `clone`, `set`, `sigver` | ✔ (sigver, clone) | `gpfile`, `set` |
| Async | `kXR_attn`, `asynresp`, `waitresp` | partial | full handling in `SessionMachine` |
| HTTP/WebDAV | GET+Range, multi-range, HEAD, PUT (chunked), DELETE, PROPFIND 0/1/∞, MKCOL, MOVE, COPY, OPTIONS | ✗ | **all** (`XRootD.jl/src/Storage/web.jl` + `libxrdc/lib/protocols/http/` as models) |
| Checksums | crc32c, adler32, crc64/xz, md5 — local + remote | partial (crc32c) | adler32, crc64, md5; `Want-Digest` negotiation |

**As built.** Every `root://` row above is implemented and tested — session,
metadata, namespace, file I/O, vector/paged (`readv`, `writev`, `pgread` with
per-page CRC32c and the bad-page retry, `pgwrite`, `chkpoint`), xattr, and the
full `kXR_attn`/`asynresp`/`waitresp`/`oksofar`/`wait`/`redirect` handling in
`SessionMachine`. `proto/requests.py` also encodes `bind`, `endsess`, `set`, and `sigver`. The
`clone` (`kXR_clone`, opcode 3032) followed later: `File.clone` hands the
server a list of byte ranges and a second open handle and lets it do the copy
itself, which is the one operation here that moves data without moving it
through the client. 3032 is past `kXR_REQFENCE` and so is an extension rather
than a standard opcode; a server without it refuses the request, and the
client reports that as `UnsupportedError` rather than "invalid request code". The only outstanding rows are the HTTP/WebDAV one
(Phase 6) and `gpfile`, which no reference client implements and no
deployment reachable from here exercises.

---

## 9.5 HTTP, WebDAV and XrdHttp — as built

`https://`, `davs://` and XrdHttp are peers of `root://`, not a side door: the
scheme picks the implementation and nothing else changes.

```python
xrdclient.open("davs://dav.example.org/store/f.root")          # → HTTPRawIO
xrdclient.FileSystem("davs://dav.example.org").listdir("/store")  # → HTTPFileSystem
xrdclient.XRootDPath("https://dav.example.org/store/f.root").read_bytes()
xrdclient.copy("davs://a/f.root", "root://b//store/f.root")    # either direction
```

The dispatch is `FileSystem.__new__` and `io.open_url`, chosen for the same
reason `pathlib.Path.__new__` dispatches: callers name what they want, not
which class provides it. `HTTPFileSystem` **subclasses** `FileSystem`, so
`walk`, `glob`, `rmtree`, `read_bytes`/`write_bytes`/`read_text`/`write_text`
are inherited unchanged — they are written in terms of `scandir`, `open` and
`remove`, which the WebDAV side provides.

- **Transport** (`http/client.py`) is `http.client`, not `httpx`: a plain
  interpreter must be able to talk to a storage element. What it adds is what
  every grid client needs anyway — connections pooled by
  `(scheme, host, port)`, bearer tokens (a macaroon and a SciToken are both
  just bearer tokens), X.509 proxies via `SSLContext.load_cert_chain`,
  cross-host redirect following with a budget, and one retry when a pooled
  connection turns out to have gone stale.
- **One exception table, two protocols.** HTTP statuses map to the `kXR_*`
  code that means the same thing and go through the same `raise_for_status`,
  so a 404 is the same `NotFoundError` (and the same `FileNotFoundError`) a
  `root://` `kXR_NotFound` raises. WebDAV reuses statuses, so the table is
  overridable per verb: `405` from `MKCOL` means "the collection is already
  there", while everywhere else it means "the server does not implement this".
- **An error response is drained before it is raised.** Abandoning an unread
  body strands the pooled connection, and the next request would be silently
  re-sent on a fresh one — which for a conditional `PUT` means the second
  attempt fails its own condition. Conditional requests are additionally never
  repeated.
- **Reading** is a ranged `GET` held open, so sequential reads cost no extra
  requests and a `seek` costs exactly one; a server that ignores `Range`
  is detected and skipped forward rather than silently handing back the wrong
  bytes. **Writing** is a `PUT`: small files go in one request with a
  `Content-Length`, which is what lets a redirect to a pool node be followed;
  anything past `chunk_size` switches to a chunked upload that never holds the
  file in memory. HTTP has no append and no partial update, and says so with
  `UnsupportedError` instead of pretending.
- **Namespace**: `PROPFIND` is `scandir`/`stat`, `MKCOL` is `mkdir`, `MOVE` is
  `rename`, `DELETE` is `remove`. `DELETE` on a collection is recursive in
  WebDAV, so `rmdir` makes the emptiness check POSIX promises itself. A server
  with no WebDAV at all still answers `stat` — the `PROPFIND` falls back to a
  `HEAD`.
- **Staging** is the WLCG Tape REST API at `/api/v1` — the one FTS and Rucio
  drive — behind the same method names the binary protocol uses: `prepare` is
  `POST /stage`, `query_prepare` its `GET`, `cancel_prepare` its `DELETE`, and
  `archive_info` is `POST /archiveinfo`. Only staging: the API has no
  equivalent of the other `PrepareFlags`, each of which is refused by name,
  and `evict` releases a request rather than a path, so it has none at all.
  Replies are read leniently (the file list has been spelt `files`, `responses`
  and a bare array; `onDisk` has been `true`, `1` and `"1"`) and a path the
  reply says nothing about is reported as unaccounted for rather than dropped.
  `archive_info` exists on `root://` too, where it is one `statx`: the offline
  flag is the whole answer, so both schemes answer in the tape API's words.
- **XML** is `xml.etree`, which never fetches an external entity, and a
  response carrying a DTD at all is refused before parsing rather than trusted
  to be harmless.
- **Checksums** are RFC 3230 `Want-Digest`/`Digest`. XRootD and dCache send
  `adler32`/`crc32c` as hex and the rest base64; both are normalised to the hex
  `ChecksumInfo` the `root://` side returns, so a cross-protocol copy compares
  like with like.
- **Macaroons** are minted with a `POST` carrying caveats and an ISO 8601
  validity, and returned as a plain string, because a macaroon is just a bearer
  token: it goes wherever `Config.token` goes.
- **`xrdclient.testing.FakeDAVServer`** is the counterpart of `FakeServer`: ranged
  `GET`, chunked `PUT`, `PROPFIND`, `MKCOL`, `MOVE`, digests and macaroons in
  the test process, with knobs for the awkward cases (demands a token,
  redirects once, has no WebDAV, ignores ranges, offers no digest).

---

## 10. Copy engine and third-party copy

`copy/engine.py` is a scheduler over source/sink pairs, not a pile of
special cases:

- **Streaming pump** with a configurable in-flight window; chunk size adapts to
  measured throughput and the server's `kXR_pgPageSZ`. Uses multiplexed
  requests on one connection rather than N connections.
- **Backends**: `root(s)://`, `http(s)://`/`dav(s)://`, local paths, and
  file-like objects. Any pair is a valid copy, including remote→remote.
- **Resumption**: partial-transfer state (offset + running checksum) survives a
  reconnect; `--continue` resumes an interrupted upload/download.
- **Verification**: post-copy checksum comparison, preferring a server-computed
  checksum on both ends (`kXR_Qcksum` / `Want-Digest`) and falling back to local
  computation only when the server cannot supply one.
- **Recursive**: `copytree` with a lazy walk, parallel file workers, and
  `ignore=`/`dirs_exist_ok=` semantics borrowed from `shutil.copytree`.
- **Progress**: a callback protocol (`on_start`/`on_progress`/`on_done`) with a
  ready-made `tqdm` adapter and a plain-stderr adapter for the CLI.
- **Third-party copy**, both dialects:
  - `root://` TPC — the *stock* XRootD dialect exactly as documented in
    `libxrdc/lib/xfer/copy_remote.c:136`: mint a rendezvous key, open the
    **destination** first with `tpc.key/tpc.src/tpc.lfn/tpc.stage=copy/oss.asize`,
    then the **source** with `tpc.key/tpc.dst/tpc.stage=copy`, handling the
    `kXR_waitresp` deferral. That comment block explicitly warns that the legacy
    full-URL `tpc.src` form does not work against stock servers — we implement
    the stock order only.
  - HTTP TPC — `COPY` with `Source:`/`Destination:` and
    `TransferHeaderAuthorization:` carrying a delegated (macaroon or bearer)
    credential, plus `Overwrite:` and progress-marker parsing from the response
    stream.

**As built** (`copy/engine.py`, `copy/tpc.py`):

```python
xrdclient.copy(src, dst, *, chunk_size=None, verify=None, algorithm=None,
         overwrite=True, progress=None, config=None) -> CopyResult
xrdclient.copy_tree(src, dst, *, config=None, progress=None, **options) -> list[CopyResult]
xrdclient.third_party(src, dst, *, overwrite=True, posc=True, token_mode="", ...) -> CopyResult
xrdclient.http.third_party(src, dst, *, mode="pull", delegate=False, verify=None, ...) -> CopyResult
```

- An endpoint is a `str`, an `XRootDURL`, an `XRootDPath`, an `os.PathLike`, or
  an already-open binary file object — on **either** side. `root://`,
  `http(s)://`/`dav(s)://` and local paths therefore compose into any pair,
  including remote→remote and local→local, with no per-pair code.
- One pass over the data: the digest is computed **while streaming**, so
  verification costs no second read. It is compared against a server-computed
  checksum (`kXR_Qcksum` or `Want-Digest`) taken from the remote end that has
  one — the target by preference. `verify=None` follows
  `Config.verify_checksums` and degrades quietly when the server cannot
  checksum at all; `verify=True` makes that server's silence an error.
- `overwrite=False` is an exclusive create (`xb`), which is `kXR_new` on
  `root://` and `If-None-Match: *` over HTTP — not a race-prone `exists()`
  check.
- `progress(done, total)` is any callable; `total` is `None` when the source
  cannot say how big it is (a stream).
- `CopyResult` carries `source`, `target`, `size`, `elapsed`, `checksum`, and
  computes `rate` and `verified`.
- `third_party` dispatches on the URLs and implements **both** dialects: the
  stock `root://` rendezvous above, tested against the exact CGI and request
  order stock XRootD accepts, and the WLCG `COPY` dialect (`http/tpc.py`) that
  FTS and Rucio speak, tested against a `FakeDAVServer` that really does fetch
  from the other endpoint. A mixed pair raises rather than silently streaming
  through the client — `xrdclient.copy` is the call that does that, and says so.
  The `COPY` side reads the performance-marker stream to its `success:` /
  `failure:` line before returning, because the outcome is in the body: a
  transfer that failed still answers `202 Accepted`, and a client that trusts
  the status line reports a copy that did not happen.
- Resumption is `copy(..., resume=True)`: the target is probed, the reader and
  writer are positioned at its length, and verification compares both files
  because the in-flight digest would only cover the tail.
- Parallelism inside one transfer is `config.parallel_chunks` spans, one
  session each because a session serialises its own requests: the target is
  created, then written at offsets by workers reading spans of the source.
  Gated on a target that takes an offset (never an HTTP `PUT`), a source that
  will state its length, and a file long enough to give every worker a whole
  `chunk_size`. Verification compares both ends, as resumption does.
- A tree is as wide as `workers` / `config.parallel_files`: one thread per
  file in flight, results collected in submission order so the walk's order
  survives, and pending futures cancelled when one raises. Progress over a
  parallel tree is aggregated, because per-file positions interleave.
- The pump reads `config.in_flight` chunks ahead of the write in flight, on
  one thread, so a read and a write overlap instead of taking turns; chunks
  leave the queue in the order they were read, which is what keeps the
  streaming digest honest. A failure on either side stops the other: the
  reader hands its exception to the consumer, and a write that raises drains
  the queue and joins the thread rather than leaving it filling one.
  `in_flight=1` is the sequential pump, for a copy with no latency to hide.
  The chunk size stays fixed: the parallel-span layout hands every worker a
  whole `chunk_size`, so a size that moved under it would change the plan the
  spans were cut to.

---

## 11. CLI

Thin argparse wrappers over the library, installed as console scripts, with
names that do not collide with the official tools (`xrd-cp` etc., with an
opt-in `[tools]` extra that also installs unprefixed aliases):

`xrd-cp` (incl. `-r`, `--verify`, `--continue`, `--tpc`, `--parallel`),
`xrd-fs` (subcommands + an interactive shell with readline completion),
`xrd-cksum`, `xrd-stat`, `xrd-ls`, `xrd-mv`, `xrd-rm`. Exit codes: `0` ok,
`1` runtime error, `2` usage. `--json` on every command for scripting.
Behavior is compared against `/usr/bin/xrdcp` and `/usr/bin/xrdfs` in CI.

### 11.1 As built

**Two console scripts, not seven.** `xrd-cksum`, `xrd-stat`, `xrd-ls`,
`xrd-mv` and `xrd-rm` would have been aliases for `xrd-fs` subcommands, so
they are subcommands:

```console
$ xrd-fs ls -l root://eos.example.org//store/user/me
$ xrd-fs stat --json davs://dav.example.org/store/f.root
$ xrd-fs checksum -a adler32 root://host//store/f.root
$ xrd-fs xattr root://host//store/f.root --set run=42
$ xrd-cp -r /tmp/results davs://dav.example.org/store/results
$ xrd-cp --tpc root://a//store/f.root root://b//store/f.root
```

| Module | Holds |
|---|---|
| `cli/__init__.py` | `Endpoints` (one `FileSystem` per endpoint, shared across the URLs on one command line), `dumps`/`_plain` (`--json` for the library's dataclasses, enums, `bytes` and URLs), `fail` (message to stderr, exit `1`), `size_arg` (`8M`, `512K`), `common_flags`, `config_from` |
| `cli/fs.py` | `ls` `stat` `cat` `checksum` `mkdir` `rm` `rmdir` `mv` `touch` `df` `locate` `ping` `query` `xattr` |
| `cli/cp.py` | `cp` semantics — trailing-slash and existing-directory destinations, several sources into a directory, `-r`, `-n`, `--tpc`, `--verify`/`--no-verify`, `-a`, `--chunk-size`, `-p`/`--no-progress` |

Commitments worth naming:

- **Whole URLs, not host-plus-path.** `xrd-fs` is deliberately not a drop-in
  for stock `xrdfs`: a user has a URL in hand, and so does the library.
- **`--json`, `-q`, `-v`, `--token`, `--user`, `--no-verify-tls` are common to
  every command.** TLS verification is disabled only by that explicit flag,
  never implicitly.
- **The progress bar is twenty lines, not a `tqdm` dependency.** The
  `progress=` protocol `xrdclient.copy` takes is `(done, total)`; anyone who wants
  `tqdm` passes `tqdm(...).update`. It draws only when the percentage moves,
  writes to stderr, and defaults to on only when stderr is a terminal.
- **Every subcommand is an ordinary `(args, endpoints) -> int` function**, so
  the test suite drives them in-process rather than through a subprocess.
- Not built: the interactive readline shell, shell completions, and the
  `[tools]` extra of unprefixed aliases.

---

## 12. Testing strategy

Five tiers. Tiers 1–2 run everywhere with no network; 3–5 gate on available
binaries or credentials.

1. **Unit / sans-io.** Every codec and the `SessionMachine` driven by byte
   strings. Includes a corpus of captured frames from all three reference
   implementations. Property-based tests (`hypothesis`, dev-only) assert
   `decode(encode(x)) == x` for every request and response type, and that no
   decoder crashes or hangs on arbitrary bytes.
2. **Reference server — `xrdclient.testing.FakeServer`.** An in-process XRootD
   server on an ephemeral loopback port, exported from `xrdclient.testing` so
   downstream users can test against it too. It speaks the real wire protocol
   over a real socket, which is what lets `SocketTransport`, `Session`,
   `Router`, `FileSystem`, `File`, `xrdclient.io`, and `XRootDPath` all be exercised
   *unmodified* — no test seam is cut into production code anywhere.

   ```python
   from xrdclient.testing import FakeServer

   with FakeServer(files={"/data/a.root": b"hello"}, dirs=["/data/empty"]) as srv:
       with xrdclient.FileSystem(srv.url) as fs:
           assert fs.read_bytes("/data/a.root") == b"hello"
       assert srv.contents("/data/a.root") == b"hello"   # the server's own view
   ```

   Contents are plain attributes (`files`, `dirs`, `xattrs`, `config_values`),
   mutable from the test at any time. The awkward-server knobs are attributes
   too, each consumed as it fires:

   | Knob | Injects |
   |---|---|
   | `redirects[opcode] = (host, port, token)` | one `kXR_redirect`, then serves normally |
   | `waits[opcode] = n` | `n` `kXR_wait` replies before the answer |
   | `chunk_reads = n` | splits read bodies into `kXR_oksofar` chunks of `n` |
   | `auth_rounds = n` | `n` rounds of `kXR_authmore` before accepting |
   | `sec` / `version` / `flags` | what the login and `kXR_protocol` announce |
   | `seen` | every opcode received, in order — the round-trip assertion |
   | `disconnect()` | drops live connections but keeps listening, so the reconnect path is testable |

   The port is claimed lazily on first use and released by `stop()`, so a
   server built only to hold contents never opens a socket, and `start()` /
   `stop()` are both idempotent. `FakeServer` is public API, so it has its own
   tests (`tests/test_testing.py`) rather than being trusted by assumption.
3. **Fault injection.** **As built:** `xrdclient.testing.FaultProxy` (340 lines,
   written here rather than borrowed from `libxrdc`'s `brix_fault_proxy.c`, so
   that it ships in the package and users can break their own code with it).
   `drop_after` · `stall_after` · `delay` · `corrupt` · `chop` · `rewrite` ·
   `refuse` / `accept` · `cut` · `heal`, all chainable, with byte and
   connection counters for the assertion. `tests/test_faults.py` drives
   reconnect, replay, handle recovery and timeouts through it.
4. **Interop with the real server.** **As built** (`tests/test_interop.py`,
   marker `interop`): an unprivileged `xrootd` on loopback, the full operation
   matrix, and stock `xrdcp`/`xrdfs` reading back what we wrote. It found what
   the fake could not — `kXR_writev` counted its data in `dlen`, which the
   real server refuses with `kXR_ArgInvalid`.
5. **Differential parity vs. official bindings.** **As built**
   (`tests/test_parity.py`, marker `parity`): same daemon, same files, both
   clients in one process, asserting the *values* agree where the shapes
   deliberately do not. Against XRootD **5.9.6**, which is what conda-forge
   ships; nothing in the suite is version-specific.

**As built**, the quality gates: coverage **97%** on `proto/`, `crypto/` and
`client/` against a `fail_under = 90` in `pyproject.toml`; `ruff` and
`mypy --strict` clean over `src/` and enforced as CI jobs, with `py.typed`
shipped so callers get the same annotations and `tests/test_typing.py`
asserting the revealed types of the overloaded `open`; `mkdocs build --strict`, so a broken link or a
page missing from the nav fails the build; and `benchmarks/bench.py` against
`xrdcp`, `xrdfs` and the official bindings, run on demand rather than per
commit because a shared runner cannot promise stable timings.

The WebDAV tier runs against `xrdclient.testing.FakeDAVServer` rather than a
containerized dCache — it models the behaviour that actually breaks clients
(a cache that ignores `Range`, a server without `PROPFIND`, a 307 before the
answer, `Want-Digest` refused) and needs no container runtime.

---

## 13. Packaging and quality

- `src/` layout, `pyproject.toml`, hatchling. Python **3.10+** (needed for
  `slots=True` dataclasses, `X | Y` annotations, and `match`).
- Extras, **as built**: `[krb5]` gssapi · `[fsspec]` fsspec · `[dev]`. **The
  core imports nothing outside the stdlib**, and the one remaining runtime
  extra has a clear, actionable `ImportError`. Two planned extras are gone:
  `[http]`, because `http.client` carries HTTP and WebDAV, so `davs://` costs
  no dependency; and `[gsi]`, because AES, RSA and DER were written in pure
  Python (§7), so the *entire* auth ladder bar Kerberos installs with nothing.
- `ruff` (format + lint), `mypy --strict`, `pytest` (+`-n auto`, `--timeout`),
  `coverage`. Pre-commit hooks. CI matrix: 3.10–3.13 × {no extras, all extras}.
- Docs: MkDocs Material with API reference from docstrings, a "coming from
  pyxrootd" migration guide, an auth cookbook per mechanism, and runnable
  examples.
- Security, **as built**: `SECURITY.md` states the threat model; redaction is
  enforced by test in logs, reprs and tracebacks; TLS verification cannot be
  disabled implicitly. The review found three real defects, each fixed with
  a test — a secret split across a log format string and its arguments escaped
  redaction; SSS keytabs were accepted regardless of file permissions; and
  `parse_dirlist` accepted server-supplied names containing `/` or `..`, which
  `copy_tree` joins onto a local destination.

---

## 14. Principal risks

| Risk | Mitigation |
|---|---|
| **GSI/X.509 is genuinely hard** — 451 LOC of C, undocumented handshake, proxy chain semantics | Schedule it late (Phase 7) with three references in hand (`sec_gsi.c`, `gsi/proxy.c`, go-hep `auth/gsi`); test against a real server with a generated CA + proxy. **Settled:** built in Phase 7 with no third-party crypto, verified by a simulated server that agrees the DH key, decrypts the payload and checks the proof-of-possession signature; the tests mint their own chain, so nothing depends on `openssl` being installed. |
| Sans-io refactor over-engineers a working blocking client | Prove the machine against the existing `pyxrdcp` test suite in Phase 1 before building anything on it; if the sync driver is not simpler than today's `roundtrip`, the abstraction is wrong. |
| Protocol drift between server versions (v4 / v5 / v6) | Capability-gate on `kXR_protocol` `pval`; test against every server version available; never assume v5 features. |
| Async and sync surfaces diverge | Generate the async API's test matrix from the sync one; a test asserts the two classes expose identical method names and signatures. **As built: they cannot diverge — there is one implementation, and the async side delegates to it.** |
| Performance below the C/C++ clients | Multiplexed in-flight windows, `readinto`/`memoryview` zero-copy, hardware CRC32c, and a benchmark gate in CI. **Measured:** faster than the official bindings on small reads (7.9x), metadata (1.5x), listing (6.5x) and copying (1.1x); 1.6x slower on one large streaming read and 4.4x on a vector read. Writing the benchmark found three redundant full-buffer copies in the receive path, all removed. The GIL-bound checksum ceiling did not materialise as a risk: digests are asked of the server, so the Python-speed path is one the caller opts into. |
| Scope is large | Phased delivery (see the roadmap); each phase is independently useful and independently shippable. |
