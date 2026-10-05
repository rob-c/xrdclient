# Platforms and deployment packages

Both clients are validated together using the shared distribution workflow.
The package candidates retain their public APIs and exact
`xgfalclient → xrdclient` version requirement.

## Platform matrix

| Platform | Tested interpreter | Deployment artifact |
| --- | --- | --- |
| AlmaLinux 8, 9 | AppStream Python 3.12 | Wheel, sdist, RPM |
| AlmaLinux 10 | Distribution Python | Wheel, sdist, RPM |
| CentOS Stream 9 | AppStream Python 3.12 | Wheel, sdist, RPM |
| CentOS Stream 10 | Distribution Python | Wheel, sdist, RPM |
| Ubuntu 24.04, 26.04 | Distribution Python | Wheel, sdist, DEB |
| Fedora 44 | Distribution Python | Wheel, sdist, RPM |
| NixOS 26.05 | Nixpkgs Python 3.12 | Nix derivations and VM test |
| Homebrew macOS Intel/Apple Silicon | Brew Python 3.14 | Wheel bundles and tap formulae |

These are validation targets, not a claim that every row has passed before its
CI job runs. Container testing checks distribution userspace on the host's
kernel. The NixOS VM job separately checks an actual booted NixOS installation.
CentOS Stream 10's x86-64 image needs a v3-capable CPU; AlmaLinux 10 requires
v2. An incompatible VM CPU is an infrastructure failure, not a client skip.

Python 3.9 remains the declared compatibility floor, but currently cannot
resolve `botocore` together with `urllib3>=2.2`. The existing Python 3.9 install
gate deliberately remains red until this is resolved. Do not work around it
with `--no-deps`, an old urllib3, or a source-only syntax test. Alma 8's default
Python 3.6 is also unsupported; the distribution jobs select 3.12 explicitly.

## Run the Linux matrix

Arrange the two candidate checkouts under one directory, then run:

```console
python3 xrdclient/tools/platforms.py --workspace . --platform alma9 --output results
python3 xrdclient/tools/platforms.py --workspace . --jobs 2 --output results
```

Rootless Podman is preferred; `--engine docker` also works. Use a fresh output
directory for each run. Only the disposable containers install OS packages.
Only the two source checkouts are mounted, read-only; sibling directories are
not exposed. CI checkouts don't retain GitHub tokens. Containers receive no
SSH keys, host network or privileged access. Avoid placing secrets in the
source trees.

Each job builds both sdists and wheels, checks distribution metadata, resolves
the entire default dependency set using **only wheels**, performs an offline
clean install, runs `pip check`, and exercises every installed console command
in JSON/XML plus binary/Unicode local copies. It then builds, installs, tests
and removes the native packages. Full hermetic suites run as an ordinary user,
including VOMS/trust diagnostics, malformed input, permissions, transport
faults and CLI compatibility tests. Real-server interoperability and paired
performance comparisons remain their existing separate gates.

Results include console logs, the resolved image identity, OS/Python versions,
dependency inventories, JUnit results and coverage XML. Per-OS coverage is
reported rather than combined into a misleading cross-platform 100% claim;
the existing full-coverage and condition-coverage gates are unchanged.

## RPM and DEB deployment bundles

The shared package builder is `tools/distribution_package.py`. It consumes a
resolved wheelhouse and installs no dependencies from the network. The RPMs
and DEBs are architecture- and Python-minor-specific, not `noarch` packages.
Build separately on each target distribution; don't reuse a newer glibc
snapshot on an older system.

`xrdclient` owns `/opt/storage-clients`, including the common Python libraries.
`xgfalclient` adds its modules, compatibility packages, commands and manual
pages to that runtime and requires **exactly** the matching Xrd package
release. Commands run with isolated Python, without the user's `PYTHONPATH`
or system site-packages. Neither package replaces system Python or writes
credentials/trust directories.

These are private deployment bundles, **not** recipes ready for submission to
Fedora/Debian archives. They snapshot dependencies, including native wheels;
each package contains its wheel filenames and SHA-256 inventory. Rebuild and
redeploy when dependencies receive security updates. Increment `--release`
for dependency-only RPM/DEB rebuilds; rebuild both clients together so their
exact release dependency remains valid. Homebrew supports `--revision` for
these rebuilds and tags archive filenames with CPU/platform/Python versions.
Package-manager upgrade and removal own the runtime files; no networked
install scripts are needed.
Distribute signed packages through your normal repository tooling.

## NixOS

Using a current Nixpkgs `nixos-26.05` channel:

```console
nix-build xrdclient/packaging/nix --arg xgfalclientSrc ./xgfalclient -A xgfalclient
nix-build xrdclient/packaging/nix --arg xgfalclientSrc ./xgfalclient -A nixosTest
```

The first command builds both clients with Nixpkgs dependencies and runs their
hermetic runtime suites. Only the development-only maintainability test is
omitted because this package set lacks Complexipy; its repository-wide CI
gate still runs. The second boots a NixOS VM and tests installed CLI reports
and transfers. Running Nix inside Podman is useful for the first command but
does **not** substitute for the VM test. The resolved Nixpkgs version is
recorded in CI. Binary cache availability depends on the chosen package set;
arbitrary Nix builds are not promised to be compiler-free.

## Homebrew

Resolve wheels using the target machine's Brew Python 3.14, then generate:

```console
python3.14 -m pip download --only-binary=:all: --dest wheelhouse wheels/*.whl
cd xrdclient
python3.14 -m tools.homebrew_package --wheelhouse ../wheelhouse --output ../brew-packages
```

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

Merge the shared `xrdclient` platform workflow before enabling the small GFAL
workflow that calls it. GFAL passes its candidate commit explicitly, while
Xrd CI tests its own candidate with GFAL's `main`. Manual workflow inputs let
you select matching candidate refs in both repositories before their paired
implementation changes land on `main`. Record both source revisions
alongside released artifacts, then publish Xrd before its exactly pinned GFAL
release. No workflow in this matrix publishes packages automatically.

## Local validation: 2026-10-05

The working-tree candidates passed all eight RPM/DEB targets on x86-64:
AlmaLinux 8/9/10, Ubuntu 24.04/26.04, CentOS Stream 9/10 and Fedora 44.
Each passed wheel/sdist checks, a clean binary-only dependency install,
installed-command JSON/XML and byte-copy checks, both available hermetic
suites as a non-root user, and native package installation/removal.

Those artifacts still carried version 0.2.0 before the development version
was bumped to 0.3.0; they included the pending changes, not just the published
0.2.0 tag. Rebuild and validate the final paired 0.3.0 artifacts before release.

Both Nix packages built and passed their suites using Nixpkgs
`26.05.11216.0d9e9b832d03`. The NixOS VM recipe evaluated successfully, but
no booted VM was verified locally: the Alma guest exposes no `/dev/kvm`.
The hosted VM job remains unverified until CI runs it.

Intel Homebrew formula installation, formula tests and installed-command
checks passed. The macOS suites retained their configured 100% line/branch
coverage gates. Apple Silicon Homebrew is a CI target, not locally verified.

Optional tests requiring external fault tools, native Kerberos bindings or
independent oracle executables were skipped where unavailable; their reasons
are retained in the logs/JUnit reports. Real-server interoperability and
performance gates were not rerun here; this work changed packaging and test
fixtures, not client runtime implementations. The Python 3.9 dependency
resolution blocker above remains unresolved.
