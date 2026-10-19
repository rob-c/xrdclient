# Platforms and deployment packages

The whole 0.4.0 chain is validated together using the shared distribution
workflow: the libraries xrdcore, xrdsec and xrdhttp, the clients xrdclient and
xgfalclient, and xrdarc. Every package is built from its candidate checkout,
so the matrix runs before any of them is on PyPI. The matrix, its tools and
the packaging recipes (`packaging/linux.sh`, `packaging/platforms.json`,
`packaging/nix`) live in [xrd-devtools](https://github.com/rob-c/xrd-devtools),
the repository for what serves the whole chain rather than one package; this
page is their operator's guide. The candidates retain their
public APIs, the libraries' `>=0.4.0,<0.5` ranges and the exact
`xgfalclient → xrdclient` version requirement.

## Platform matrix

| Platform | Architectures | Tested interpreter | Deployment artifact |
| --- | --- | --- | --- |
| AlmaLinux 8, 9 | x86-64, ARM64 | AppStream Python 3.12 | Wheel, sdist, RPM |
| AlmaLinux 10 | x86-64, ARM64 | Distribution Python | Wheel, sdist, RPM |
| CentOS Stream 9 | x86-64, ARM64 | AppStream Python 3.12 | Wheel, sdist, RPM |
| CentOS Stream 10 | x86-64, ARM64 | Distribution Python | Wheel, sdist, RPM |
| Ubuntu 24.04, 26.04 | x86-64, ARM64 | Distribution Python | Wheel, sdist, DEB |
| Fedora 44, Rawhide | x86-64, ARM64 | Distribution Python | Wheel, sdist, RPM |
| NixOS 26.05 | x86-64, ARM64 | Nixpkgs Python 3.12 | Nix derivations and VM test |
| Homebrew macOS Intel/Apple Silicon | x86-64, ARM64 | Brew Python 3.14 | Wheel bundles and tap formulae |

These are validation targets, not a claim that every row has passed before its
CI job runs. Container testing checks distribution userspace on the host's
kernel. The NixOS VM job separately checks an actual booted NixOS installation.
Every Linux container runs natively on an x86-64 and on an ARM64 hosted
runner; the RPM, DEB and wheel bundles are architecture-specific, so each
architecture's artifacts come from its own job. Fedora Rawhide is a moving
target included to see breakage early; a Rawhide-only failure is a warning
for the next Fedora, not a release blocker on its own.
CentOS Stream 10's x86-64 image needs a v3-capable CPU; AlmaLinux 10 requires
v2. An incompatible VM CPU is an infrastructure failure, not a client skip.

Python 3.11 is the declared compatibility floor. AlmaLinux 8 and 9 (and
CentOS Stream 9) offer it, and 3.12, from AppStream; AlmaLinux 10, Ubuntu 24.04
and later ship 3.12 or newer as `python3`. Raising the floor past 3.9 (0.3.1
moved it to 3.10, 0.4.0 to 3.11) also resolved the old Python 3.9 conflict between `botocore` and `urllib3>=2.2`, so
the wheel-only install gate now runs on 3.11 and must pass; do not work around
a failure with `--no-deps` or an old urllib3. Alma 8's default Python 3.6 is
unsupported; the distribution jobs select 3.12 explicitly.

## Run the Linux matrix

Arrange the candidate checkouts as siblings under one directory - `xrdcore/`,
`xrdsec/`, `xrdhttp/`, `xrdclient/`, `xgfalclient/` and, optionally,
`xrdarc/` (its wheel, package and suite are added when it is there) - with an
xrd-devtools checkout (the tools run from a checkout, because `packaging/`
sits beside its `src/`), then run:

```console
PYTHONPATH=xrd-devtools/src python3 -m xrddevtools platforms --workspace . --platform alma9 --output results
PYTHONPATH=xrd-devtools/src python3 -m xrddevtools platforms --workspace . --jobs 2 --output results
PYTHONPATH=xrd-devtools/src python3 -m xrddevtools platforms --workspace . --arch amd64 --output results-amd64
```

(With xrd-devtools installed, `xrddevtools platforms ...` is the same.)

Rootless Podman is preferred; `--engine docker` also works. Use a fresh output
directory for each run. The default runs the host's own architecture;
`--arch amd64` or `--arch arm64` selects that image variant and, on a host of
the other architecture, runs it under the engine's emulation (Rosetta or
QEMU). Emulated runs are slower and prove the packages, not the kernel; each
`result.json` records the architecture that was run. Emulation can also stop
short of the real thing: Ubuntu 26.04's `tar`, for example, uses a system
call Rosetta does not implement, so its DEB build fails under Docker Desktop
on Apple Silicon even though the wheels install and the suites pass. A
failure of that shape is an emulator limit; the native runner in CI is the
authority for that architecture. Only the disposable containers install OS
packages.
Only the chain's source checkouts and the xrd-devtools checkout (at
`/src/xrd-devtools`, where `linux.sh` runs the package builder and smoke test
from) are mounted, each read-only and on its own; the workspace directory
itself and any other sibling are not exposed. CI checkouts don't retain GitHub tokens. Containers receive no
SSH keys, host network or privileged access. Avoid placing secrets in the
source trees.

Each job builds every package's sdist and wheel, lowest layer first, checks
distribution metadata, resolves the entire default dependency set using
**only wheels** (the clients' library requirements resolve to the wheels just
built, through `--find-links`), performs an offline clean install, runs `pip
check`, and exercises every installed console command in JSON/XML, the
version commands (`xrd-fs --version`, `xrd-proxy --help`, `gfal-ls --version`,
`xrdarc --version`) and binary/Unicode local copies (xrd-devtools'
`installed_smoke.py`, run as a plain file). It then builds, installs, tests
and removes the native packages. Every package's full hermetic suite runs as an ordinary user,
including VOMS/trust diagnostics, malformed input, permissions, transport
faults and CLI compatibility tests. Real-server interoperability and paired
performance comparisons remain their existing separate gates.

Results include console logs, the resolved image identity, OS/Python versions,
dependency inventories, JUnit results and coverage XML. Per-OS coverage is
reported rather than combined into a misleading cross-platform 100% claim;
the existing full-coverage and condition-coverage gates are unchanged.

## RPM and DEB deployment bundles

The shared package builder is xrd-devtools' `distribution_package.py`
(`xrddevtools package`; `linux.sh` runs it as a plain file with the build
interpreter). It consumes a
resolved wheelhouse and installs no dependencies from the network. The RPMs
and DEBs are architecture- and Python-minor-specific, not `noarch` packages.
Build separately on each target distribution; don't reuse a newer glibc
snapshot on an older system.

The runtime is owned by the lowest layer, not by a client. One build produces:

| Package | Contents | Requires (exactly, `version-release`) |
| --- | --- | --- |
| `storage-clients-libs` | `/opt/storage-clients` itself, its launcher, the xrdcore, xrdsec and xrdhttp wheels and every third-party wheel | the system interpreter, glibc, libgcc |
| `xrdclient` | its own wheel; `xrd-cp`, `xrd-fs`, `xrd-proxy` | `storage-clients-libs` |
| `xgfalclient` | its own wheel, compatibility packages, `gfal-*` commands and manual pages | `storage-clients-libs` and `xrdclient` |
| `xrdarc` (when built) | its own wheel; `xrdarc` | `storage-clients-libs` |

Why not keep xrdclient as the owner, as before 0.4.0: xrdarc depends on the
libraries but on neither client, so its package must install without
xrdclient; and the libraries are what every client really shares. The runtime
package is versioned as xrdcore (the lowest library). A rebuild that changes
xrdsec, xrdhttp or a third-party wheel without changing xrdcore's version is a
dependency-only rebuild: increment `--release`, for every package of the build
together, since the exact requirements name it. Third-party wheels' own
console scripts (`jp.py`, `cffi-gen-src`) are left out of the runtime: their
shebangs would name the build environment.

Commands run with isolated Python, without the user's `PYTHONPATH` or system
site-packages. Bytecode is compiled into the package payload and
the commands run with `-B`, so nothing is written into the runtime after
installation and package removal leaves no files or directories behind.
No package replaces system Python or writes credentials/trust
directories.

These are private deployment bundles, **not** recipes ready for submission to
Fedora/Debian archives. They snapshot dependencies, including native wheels;
each package contains its wheel filenames and SHA-256 inventory. Rebuild and
redeploy when dependencies receive security updates. Increment `--release`
for dependency-only RPM/DEB rebuilds; rebuild every package together so their
exact release dependencies remain valid. Every client requires
`storage-clients-libs`, so it is removed last, or together with them in one
`rpm -e`/`dpkg --purge` transaction as the matrix does. Homebrew supports `--revision` for
these rebuilds and tags archive filenames with CPU/platform/Python versions.
Package-manager upgrade and removal own the runtime files; no networked
install scripts are needed.
Distribute signed packages through your normal repository tooling.

## NixOS

Using a current Nixpkgs `nixos-26.05` channel:

```console
nix-build xrd-devtools/packaging/nix -A xgfalclient
nix-build xrd-devtools/packaging/nix -A xrdarc
nix-build xrd-devtools/packaging/nix -A nixosTest
```

One expression builds the whole chain. The packages are found as siblings of
the xrd-devtools checkout (`--arg workspace ./dir` moves them all;
`--arg xrdclientSrc ./elsewhere` and likewise for `xrdcoreSrc`,
`xrdsecSrc`, `xrdhttpSrc`, `xgfalclientSrc` and `xrdarcSrc` override one);
xgfalclient and xrdarc are offered only when their checkouts exist. Each
library is its own `buildPythonPackage` on Nixpkgs' dependencies - xrdcore on
none (`google-crc32c` as its `fast` optional dependency), xrdsec on
`cryptography` and `asn1crypto`, xrdhttp on `botocore` - and the clients
depend on those derivations exactly as their `pyproject.toml` files say, so
`botocore` reaches xrdclient and xgfalclient through xrdhttp rather than
directly. Building
xgfalclient builds and tests the libraries and xrdclient beneath it, each
running its own hermetic suite. The development-only gates - the scorecard
tests and xrdclient's maintainability test, both xrd-devtools' plugin - skip
themselves: the build sandbox holds no xrd-devtools checkout and this package
set lacks Complexipy. CI runs them, required. The VM test boots NixOS with the clients installed and tests their CLI
reports, version commands and transfers. Running Nix inside Podman is useful for the first command but
does **not** substitute for the VM test. The resolved Nixpkgs version is
recorded in CI. Hosted ARM64 runners have no KVM, so that job claims the
`kvm` system feature and the VM boots under QEMU software emulation; the
test is the same, only slower. Binary cache availability depends on the
chosen package set; arbitrary Nix builds are not promised to be
compiler-free.

## Homebrew

Build every package's wheel into `wheels/`, resolve the wheelhouse using the
target machine's Brew Python 3.14, then generate:

```console
for p in xrdcore xrdsec xrdhttp xrdclient xgfalclient xrdarc; do python3.14 -m build --wheel --outdir wheels $p; done
python3.14 -m pip download --only-binary=:all: --find-links wheels --dest wheelhouse wheels/*.whl
PYTHONPATH=xrd-devtools/src python3.14 -m xrddevtools homebrew --wheelhouse wheelhouse --output brew-packages
```

One formula and archive is written per client wheel in the wheelhouse
(xrdclient, xgfalclient, xrdarc). Each archive holds exactly its client's
dependency closure, the library wheels included: xrdclient's leaves out
xgfalclient and xrdarc, xgfalclient's carries xrdclient, and xrdarc's carries
neither client. Each formula is a separate virtualenv, so nothing is shared
between them the way `/opt/storage-clients` is.

The generator emits complete archives, actual SHA-256 checksums and formulae
using private virtual environments, offline wheel installation, and CLI/copy
tests. By default URLs point to local archives for testing. For a published
tap, supply `--base-url https://...` and upload the unchanged archives there.
Generate separate artifacts for Intel and Apple Silicon; the CFFI wheel must
match the architecture and interpreter. These are custom-tap recipes, not a
Homebrew Core submission. Both macOS architectures have dedicated CI jobs.
Homebrew itself may compile Python or system dependencies when bottles are
unavailable; the wheel-only guarantee applies to the clients, not to
bootstrapping Homebrew.

The optional `krb5` extra is separate from default/compiler-free installation:
Linux native bindings may need Kerberos development headers and a compiler.

## Coordinated CI rollout

The platform workflow is xrd-devtools' (`.github/workflows/platforms.yml`, a
reusable workflow it also runs on its own commits); xrdclient's and
xgfalclient's `platforms.yml` only call it, each passing its own commit
(`xrdclient-ref: ${{ github.sha }}`, `xgfalclient-ref: ${{ github.sha }}`) and
the other candidates' refs, `main` by default. It checks every repository out
side by side under `clients/` with xrd-devtools' candidates action - the one
action every job of the chain gets its candidates from - so nothing runs until
`rob-c/xrd-devtools` has been pushed. Every ref (`xrdcore-ref`, `xrdsec-ref`,
`xrdhttp-ref`, `xrdclient-ref`, `xgfalclient-ref`, `xrdarc-ref`,
`devtools-ref`) is a manual and reusable-call input. Manual workflow inputs
let you select matching candidate refs in every repository before their paired
implementation changes land on `main`. Record every source revision alongside
released artifacts, and publish in the chain's order (see
[Releasing](releasing.md#releasing-the-040-chain)). No workflow in this matrix
publishes packages automatically.

## Local validation: 2026-10-18 (0.4.0 chain, partial)

On an Intel macOS host, before any library was on PyPI (the tools were then
still xrdclient's `tools/`; they have since moved to xrd-devtools unchanged):

- All six wheels built; `twine check --strict` passed; `tools/chain_preflight.py
  workspace` reported no findings.
- The chain resolved binary-only (16 wheels, `--find-links` to the built ones)
  into clean Python 3.14 and 3.11 environments; `pip check` passed and
  `installed_smoke.py --clients xrdclient xgfalclient xrdarc` passed (54
  command checks, both copies).
- The Homebrew bundles were generated; each installed offline into its own
  virtualenv and passed the smoke test for its client(s). The RPM set
  (`storage-clients-libs`, `xrdclient`, `xgfalclient`, `xrdarc`) built with
  the expected exact requirements.
- Nix (nixos-26.05, aarch64-linux in a container, no VM): the expression
  parses and `nixosTest` evaluates; xrdcore, xrdsec, xrdhttp and xrdarc built
  and passed their suites. xrdclient's suite ended with one error, and
  xgfalclient was not built. The error was `tests/test_scorecard.py` failing
  to collect: `tools/scorecard.py` imports complexipy and radon, which this
  package set lacks. The gate (now xrd-devtools' plugin) skips itself
  without them. Under the check
  phase's conditions (wheel-only installs, no `src/` on the path,
  `HOME=/homeless-shelter`, no CA variables, no network, the expression's
  pytest flags), xrdclient, xgfalclient, xrdsec and xrdhttp then passed,
  the last two without the import-boundary tests disabled. Nix itself has
  not been re-run since. The Linux containers,
  the NixOS VM test and `brew install`/`brew test` have not been run for
  0.4.0.

## Local validation: 2026-10-05 (0.3.0)

This is the record for the published 0.3.0 candidates (two packages). The
0.4.0 chain - six packages, the `storage-clients-libs` runtime - has not yet
been run through the container, NixOS-VM or Homebrew matrix and must be before
release; see *Local validation: 2026-10-18 (0.4.0 chain)* below for what has.

The 0.3.0 candidates (xrdclient and xgfalclient at version 0.3.0, matching
refs) were validated on an Apple Silicon host, so ARM64 is the natively tested
architecture in this round and x86-64 ran under emulation.

**ARM64, native.** All nine RPM/DEB containers passed: AlmaLinux 8/9/10,
Ubuntu 24.04/26.04, CentOS Stream 9/10, Fedora 44 and Fedora Rawhide
(Python 3.15.0rc2). Each passed wheel/sdist checks, a clean binary-only
dependency install, the installed-command JSON/XML and byte-copy checks, both
full hermetic suites as a non-root user, and native package installation and
removal. The AlmaLinux 9 RPMs and Ubuntu 24.04 DEBs were additionally
installed on real (non-container) AlmaLinux 9.8 and Ubuntu 24.04.4 ARM64
virtual machines, where the installed-command checks passed and removal left
no files behind. On Rawhide the development-only maintainability test is
omitted because Complexipy has no wheel for Python 3.15 yet; every runtime
test ran.

**NixOS, ARM64.** Both packages built with Nixpkgs `26.05.11216.0d9e9b832d03`
and passed their suites; the NixOS VM test booted an aarch64 NixOS and passed
its installed CLI and copy checks under QEMU software emulation (no KVM in
the container used to run Nix on macOS).

**Homebrew, Apple Silicon.** Wheel bundles were generated with Brew Python
3.14, the tap formulae installed from source, `brew test` passed for both
formulae, and every installed command passed the smoke checks. The full
hermetic suites also pass on this host with Brew Python 3.13 and 3.14.

**x86-64, emulated.** The same containers were run with `--arch amd64` under
Docker Desktop's Rosetta emulation; see the note under *Run the Linux matrix*
for what emulation cannot prove. The hosted x86-64 runners in CI remain the
authority for x86-64 artifacts.

Optional tests requiring external fault tools, native Kerberos bindings or
independent oracle executables were skipped where unavailable; their reasons
are retained in the JUnit reports. The real-daemon interop suite was run on
the host against xrootd 6.2.0: the non-delegating GSI, token, Kerberos and
parity cases pass, while GSI *delegation* against a 6.2.0 server fails with
`Secgsi: ErrSerialBuffer ... kXGC_certreq` for the published 0.2.0 as well as
that candidate, so that is a pre-existing server-version incompatibility to
investigate separately rather than a regression. The hosted-runner
performance gate remains as described above.
