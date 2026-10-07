# Changelog

Notable user-visible changes are recorded here. This project follows
[Semantic Versioning](https://semver.org/); compatibility fixes which make the
client agree more closely with XrdCl are not considered breaking changes.

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

[0.3.2]: https://github.com/rob-c/xrdclient/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/rob-c/xrdclient/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/rob-c/xrdclient/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/rob-c/xrdclient/compare/v0.1.0...v0.2.0
