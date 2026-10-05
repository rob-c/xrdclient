# Platforms and deployment packages

Both clients are validated together using the shared distribution workflow.
The package candidates retain their public APIs and exact
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

The declared Python floor is 3.10, because on 3.9 botocore pins `urllib3<1.27`
and the clients need `urllib3>=2.2`. Every row above already uses a
distribution-provided interpreter of 3.10 or newer, so the floor changes no
deployment target: AlmaLinux 8/9 and CentOS Stream 9 use their AppStream
Python 3.12 packages rather than the 3.6/3.9 system interpreters, and the
other rows use the system Python.

## Run the Linux matrix

Arrange the two candidate checkouts under one directory, then run:

```console
python3 xrdclient/tools/platforms.py --workspace . --platform alma9 --output results
python3 xrdclient/tools/platforms.py --workspace . --jobs 2 --output results
python3 xrdclient/tools/platforms.py --workspace . --arch amd64 --output results-amd64
```

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
authority for that architecture. Only the disposable containers install OS packages.
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
or system site-packages. Bytecode is compiled into the package payload and
the commands run with `-B`, so nothing is written into the runtime after
installation and package removal leaves no files or directories behind.
Neither package replaces system Python or writes credentials/trust
directories.

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
recorded in CI. Hosted ARM64 runners have no KVM, so that job claims the
`kvm` system feature and the VM boots under QEMU software emulation; the
test is the same, only slower. Binary cache availability depends on the chosen package set;
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
this candidate, so that is a pre-existing server-version incompatibility to
investigate separately rather than a regression. The hosted-runner
performance gate remains as described above.
