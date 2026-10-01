# Changelog

Notable user-visible changes are recorded here. This project follows
[Semantic Versioning](https://semver.org/); compatibility fixes which make the
client agree more closely with XrdCl are not considered breaking changes.

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

[0.2.0]: https://github.com/rob-c/xrdclient/compare/v0.1.0...v0.2.0
