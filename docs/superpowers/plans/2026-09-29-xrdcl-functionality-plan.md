# Plan: the XrdCl functionality this client does not have yet

Date: 2026-09-29. Scope: everything `libXrdCl`, `xrdcp` and `xrdfs` do that
xrdclient does not, after the compat layer reached name-for-name parity with
`XRootD.client` (6.1.1 and upstream master). Each item says what XrdCl does,
where to read it (paths under an XRootD checkout, `src/`), how to build it
here without a compiled dependency, how to prove it against the real thing,
and roughly how big it is (S: a day, M: a few days, L: a week or more).

The rule for every item is the one the rest of the package follows:
behaviour is read off the reference source and checked side by side against
the official client on a real `xrootd`; a fake server is for fault injection,
never the only evidence.

## Phase 0 - make `main` green (in progress)

| Item | What | Size |
| --- | --- | --- |
| Docs build | `SECURITY.md` link broke under the `docs/security.md` symlink - fixed | S |
| Maintainability gate | (`copy/engine.py::copy` and `copy_tree` are split - done) `cli/fs.py::_xattr`, `SessionPool.acquire`, the fake servers' `_Connection.run` / `_h_fattr` / `_h_query`, `testing/s3.py::_Handler._signed`, and the new `benchmarks/compare.py::cases`/`main`, `examples/pyxrootd/run_all.py::main`: split into named helpers, no behaviour change | M |
| Performance gate | CI (quiet runner) wins 10 of 12 cases; still open: `write 1MiB chunks` native 0.88x (the `"wb"` io path), `write whole file` 1.37x but not significant, compat `dirlist 1000` 1.11x not significant. Pipeline the buffered writer's flushes; give the write cases more rounds or a larger payload so disk noise cannot mask a real win; make compat's listing conversion lazier | M |

## Phase 1 - behaviour a port depends on (done, 2026-09-29)

Compat behaviour parity: timeouts that expire requests (not caller-side
waits), `FollowRedirects`/`ReadRecovery`/`WriteRecovery` honoured, the full
redirect `HostList`, `dirlist` `LOCATE`/`MERGE`/`CHUNKED`, `SetLogMask`
topics, every meaningful `EnvPutInt` key; and the `CopyProcess` options
(`sourcelimit`, `dynamicsource`, `xrate`, `xrateThreshold`, `cptimeout`,
`inittimeout`, `coerce`) with matching `xrd-cp` flags.

## Phase 2 - ZIP archives (done, 2026-09-30)

XrdCl reads and writes members of ZIP archives on the server without
unpacking them: `XrdClZipArchive.cc`, `XrdClZipOperations.hh`,
`XrdClZipCache.hh`, the `xrdcl.unzip=<member>` CGI handled in
`XrdClFileStateHandler.cc`, and `xrdcp --zip <member>` / `--zip-append` /
`--zip-mtln-cksum` in `XrdApps/XrdCpConfig.cc`.

- Read: open a member by name (`root://h//a.zip?xrdcl.unzip=f.root` and a
  native `File(..., member=)`), serving reads from the member's range,
  inflating deflated members (`zlib`, stdlib) with a seekable cache as
  `XrdClZipCache` does. The central-directory parser already exists
  (`client/_zip.py`, used by `list_archive`).
- Write: `--zip-append` appends a member and rewrites the central directory
  (ZIP64 when needed), as `ZipArchive::AppendFile`.
- Verify: archives made by `zip`, Python's `zipfile` and XrdCl itself, read
  and appended through both clients and checked with `unzip -t`.

Implemented in `io/zip.py` and `copy/zip.py`, including stored and deflated
reads, bounded seek caching, CRC checks, ZIP64, transactional local and remote
append, both CLI forms, and bidirectional interoperability tests with stock
`xrdcp`.

## Phase 3 - metalink sources (done, 2026-09-30)

XrdCl accepts a Metalink file (`.meta4`, `.metalink`, local or remote) as a
copy source: the replicas it lists become alternative sources, its checksums
become the expected ones, and a failing replica fails over to the next
(`XrdClMetalinkRedirector.cc`, `XrdClRedirectorRegistry.cc`,
`--tlsmetalink`, `--zip-mtln-cksum`).

- Parse Metalink 3 and 4 with `xml.etree` (stdlib), map replicas onto the
  multi-source reader built in phase 1, and verify checksums from the file.
- Verify: `xrdcp` and `xrd-cp` on the same metalink over two real daemons,
  with one replica removed mid-run.

Implemented with bounded Metalink 3/4 parsing, priority and checksum handling,
replica failover, wait budgets, TLS upgrades, ZIP composition and the XrdCl
environment keys. A side-by-side test drives this client and stock `xrdcp`
through distinct replica host identities on a real daemon.

## Phase 4 - `xrdcp` and `xrdfs` option parity (M)

`xrdcp` flags `xrd-cp` does not take: `--infiles` (a list of sources),
`--xattr` (copy extended attributes), `--posc`, `--retry`/`--retry-policy`,
`--rm-bad-cksum`, `--cksum type[:value|source|print]`, `--silent`/`--nopbar`,
`--notlsok`/`--tlsnodata`, `--proxy` (a SOCKS4 proxy; also `XRD_SOCKS4*`
settings), `--server`, `--version`/`--license`, and the ZIP/metalink flags
of phases 2-3.

`xrdfs` features `xrd-fs` does not have: interactive mode with `cd` and a
working directory, `cache evict|fevict`, `ls -u -R -D -Z -C`,
`locate -n -r -d -m -i -p`, `spaceinfo`, `stat -q`, `statvfs`, `query` by
code name and number, and `xattr` in `xrdfs`'s syntax.

- Add each flag to the existing CLIs, and an `xrdcp`/`xrdfs`-compatible
  entry point (`xrd-cp --xrdcp-compat`, or `xrdcp.py`/`xrdfs.py` scripts)
  whose output matches the originals, so shell scripts port too.
- Verify: run the same command lines through both tools and diff output and
  exit codes (a table-driven test like `examples/pyxrootd/run_all.py`).

## Phase 5 - SOCKS and local redirects (S-M)

- SOCKS4 proxy support in the transport (`XrdClSocket.cc`,
  `XRD_SOCKS4HOST`/`XRD_SOCKS4PORT`), since `--proxy` depends on it.
- Local redirects: a server may redirect to `file://`; XrdCl then serves the
  file locally (`XrdClLocalFileHandler.cc`). Check whether the router follows
  such a redirect today; implement it if not.

## Phase 6 - client plugins (M)

XrdCl loads protocol plugins by URL (`XrdClPlugInManager.cc`,
`/etc/xrootd/client.plugins.d`, `XRD_PLUGINCONFDIR`) - that is how
`XrdClHttp` and `XrdClS3` attach. This client has HTTP and S3 built in; what
is missing is the extension point.

- A plugin registry mapping URL patterns to `FileSystem`/`File`
  implementations, loaded from `client.plugins.d` files and Python entry
  points, so a site can plug in a protocol without forking.
- Verify: a toy plugin registered through each route.

## Phase 7 - erasure-coded storage (L)

`XrdEc` stripes a file across servers with Reed-Solomon parity
(`src/XrdEc/`, `XrdClEcHandler.cc`, enabled by `xrdcl.ec` CGI / plugin
config), using ISA-L.

- Pure-Python Reed-Solomon over GF(2^8) (the same field ISA-L uses), the
  stripe layout, the metadata (`XrdEcObjCfg`), reads that reconstruct from
  any k of n stripes, and writes.
- Verify: files written by XrdCl's EC plugin read back here and vice versa,
  on a multi-daemon setup in Docker (the Kerberos work already runs
  AlmaLinux containers); throughput is expected to be the limit, and the
  performance gate is extended to cover it before it ships.

## Phase 8 - HTTP/2 (L)

The standard library has no HTTP/2. An implementation means the framing
layer, HPACK (with its static and Huffman tables), flow control and stream
multiplexing, negotiated with ALPN (`ssl` supports it).

- Build it as a transport under `http/` beside the HTTP/1.1 client, used
  when the server offers `h2` and multiplexing pays (many small requests).
- Verify: against `nghttpd` or a Go/Rust server, and the conformance suite
  in `tests/test_conformance_http.py` run over both versions.

## Phase 9 - Kerberos edges (M)

- DNS SRV KDC discovery (`_kerberos._udp.REALM`): a minimal DNS client over
  `socket` (stdlib has no SRV resolver); honour `dns_lookup_kdc`.
- Cross-realm: follow `krbtgt/OTHER@HOME` referrals through the TGS chain.
- Legacy enctypes on request only: RC4-HMAC (MD4 in pure Python, since
  `hashlib` often lacks it) and des3-cbc-sha1-kd; off unless
  `allow_weak_crypto`.
- macOS `API:` caches stay out of scope: they live in Heimdal's XPC
  service, which Python cannot reach without a compiled bridge; the error
  keeps naming `KRB5CCNAME=FILE:...`.
- Verify: a two-realm MIT setup in Docker with SRV records served by a
  throwaway `dnsmasq`.

## Order and what gates each

Phase 0 first (a red `main` hides regressions). Then 2 and 3 - they are
what physics analyses reach for (ROOT files in ZIPs, Rucio metalinks) -
then 4 and 5, which ports of shell scripts need, then 6, 9, 7 and 8 by
demand. Every phase ends with: side-by-side tests against the official
client, 100% coverage, `mypy --strict`, the maintainability gate, the
performance gate still won, and docs (reference, cookbook, troubleshooting)
updated in the same commit.
