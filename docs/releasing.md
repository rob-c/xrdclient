# Releasing

This is the release checklist for maintainers. A tag is a publication request:
the publish workflow rejects a tag which does not exactly match the runtime and
wheel version.

The current candidate is **0.4.0**, the first release built on the xrdcore,
xrdsec and xrdhttp libraries and paired with xgfalclient 0.4.0; the most
recent published release is 0.3.2. Keep its
changelog entry `Unreleased` until the release is approved. Python 3.11 is the
declared floor, and the clean-install gate on it must pass, not be skipped. See [Platforms](platforms.md) for outstanding
native-platform checks and the scope of pre-version-bump validation.

## Prepare

1. Work from a clean checkout of the release commit. Review every untracked
   file; build output must not be part of the release.
2. Set `__version__` in `src/xrdclient/_version.py`. Hatch reads that same
   value, so there is no second package version to update.
3. Move the version's changelog entry from `Unreleased` to the release date.
   Check that every compatibility change and deliberate incompatibility is
   represented.
4. Run the supported-version CI matrix, the real-daemon interop/parity job,
   strict docs build and the performance job. For retry, I/O or copy changes,
   also run both BRIX suites described in [Testing](testing.md).
5. Run the [shared platform/package matrix](platforms.md) against matching
   candidate refs from both repositories. Build fresh artifacts carrying 0.4.0;
   older working-tree packages are not final release artifacts. Check the
   paired native-package dependencies, runtime versions and installed commands.
6. Publish in the chain's order - the libraries, then xrdclient, then
   xgfalclient with `xrdclient==0.4.0` - as described in
   [Releasing the 0.4.0 chain](#releasing-the-040-chain). Do not change the
   separate XrdCl/PyXRootD compatibility version to 0.4.0.

## Validate the artifacts

Build into a new temporary directory so an old wheel cannot be uploaded by
accident:

```console
$ release_root=$(mktemp -d)
$ python -m build --outdir "$release_root/dist"
$ twine check --strict "$release_root"/dist/*
$ python -m venv "$release_root/smoke"
$ "$release_root/smoke/bin/python" -m pip install "$release_root"/dist/*.whl
$ "$release_root/smoke/bin/python" - <<'PY'
import importlib.metadata
import xrdclient

assert importlib.metadata.version("xrdclient") == xrdclient.__version__
print(xrdclient.__version__)
PY
```

Inspect the exact files which will be published:

```console
$ tar -tf "$release_root"/dist/*.tar.gz
$ python -m zipfile -l "$release_root"/dist/*.whl
```

The wheel must contain `xrdclient/py.typed`, and neither artifact may contain
credentials, caches, coverage data, test output or local configuration.

## Publish and verify

Create and push an annotated `v<version>` tag only after the release commit is
green. GitHub Actions builds fresh artifacts and uses PyPI trusted publishing;
do not upload a locally built artifact in parallel.

After publication, install from PyPI into a new environment, check the runtime
version and run a small transfer against a disposable endpoint. Create the
next changelog section before development resumes.

## Releasing the 0.4.0 chain

0.4.0 is six packages released together, lowest layer first, because each
publish job installs its wheel from what the previous steps put on PyPI:

| Step | Package | Repository | Requires (on PyPI) | Published to |
| --- | --- | --- | --- | --- |
| 1 | xrdcore | `rob-c/xrdcore` (new) | nothing | PyPI + GitHub |
| 2 | xrdsec, xrdhttp | `rob-c/xrdsec`, `rob-c/xrdhttp` (new) | `xrdcore>=0.4.0,<0.5` | PyPI + GitHub |
| 3 | xrdclient | `rob-c/xrdclient` | the three libraries, `>=0.4.0,<0.5` | PyPI + GitHub |
| 4 | xgfalclient | `rob-c/xgfalclient` | `xrdclient==0.4.0` and the libraries | PyPI + GitHub |
| 5 | xrdarc | `rob-c/xrdarc` | xrdcore, xrdsec and xrdhttp (never a client) | PyPI + GitHub |

PyPI never forgets a version: an upload in the wrong order cannot be undone,
only yanked and superseded by a new version of every package above it. Do not
start a step until the previous one is installable from PyPI.

### Once: create the new repositories and PyPI projects

This was done for 0.4.0 (xrdcore, xrdsec, xrdhttp, xrdwms and xrd-devtools
are on `rob-c`); it is kept as the recipe for a new repository of the chain.
For each new `<name>`, from the workspace (review `git status` before the first
commit: build output, caches and `.coverage` files are ignored, nothing else
should be untracked):

```console
$ cd <name>
$ git init -b main
$ git add -A && git status --short      # review every file
$ git commit -m "<name> 0.4.0: extracted from xrdclient 0.4.0"
$ gh repo create rob-c/<name> --public --source . --remote origin
$ git push -u origin main
```

(`git remote add origin git@github.com:rob-c/<name>.git` instead of
`gh repo create` if the GitHub repository already exists.) Wait for the first
CI run of each to pass on `main` - xrdsec's and xrdhttp's check out
`rob-c/xrdcore@main`, so push xrdcore first.

Push **xrd-devtools** the same way before any of them: every repository's CI
is its reusable workflows (`rob-c/xrd-devtools/.github/workflows/*.yml@main`)
and its candidates action, and every publish job checks it out for the
pre-flight or the artifact checks, so until `rob-c/xrd-devtools` exists no CI
run or release of the chain can start. It gets no PyPI project: it is used
from a checkout only (its `pyproject.toml` carries `Private :: Do Not
Upload`).

Each new PyPI project (xrdcore, xrdsec, xrdhttp, xrdarc) is created by its
first trusted upload. Before tagging, on
<https://pypi.org/manage/account/publishing/> add a **pending publisher** per
project: owner `rob-c`, repository `<name>`, workflow `publish.yml` (for
xrdarc, `release.yml` - see step 5), environment `pypi`. In each GitHub repository create the `pypi` environment
(Settings, Environments), restricted to `v*` tags, with a required reviewer if
you want a manual approval before upload. xrdclient and xgfalclient already
have trusted publishers; check they still name `publish.yml` and `pypi`.

### Pre-flight for the whole chain

From the workspace holding all six checkouts and xrd-devtools, with `build`,
`hatchling` and `twine` in the interpreter and xrd-devtools installed
(`pip install -e xrd-devtools`):

```console
$ xrddevtools preflight workspace --workspace .
$ xrddevtools preflight workspace --workspace . --require-dated   # just before tagging
$ xrddevtools dupes --workspace .         # no function copied between repositories
$ xrddevtools conformance --workspace .   # every pyproject.toml is the one standard
$ xrddevtools release check xrdclient     # one version source, changelog, metadata (each repo)
```

The pre-flight (`xrddevtools.chain_preflight`) checks that all six versions are the chain's
(xrdcore's, or `--version`); that every package's requirements on the others
follow the layering with the agreed specifiers (`>=0.4.0,<0.5` for each
library, `==0.4.0` for xgfalclient's xrdclient, nothing upward: xrdarc never
on a client, xrdsec and xrdhttp never on each other); that each CHANGELOG has
a `[0.4.0]` section (dated, with `--require-dated`); and that each package
builds (sdist and wheel, into a temporary directory), passes `twine check
--strict`, and that its wheel's `METADATA` carries the same version and chain
requirements as its `pyproject.toml`. `--no-build` skips the builds,
`--package NAME` checks only some. No findings, exit status 0.

Then, against the same candidate refs:

1. Each library's suite, ruff, `mypy` and scorecard (see its README), and
   each client's gates as in [Testing](testing.md).
2. The [platform matrix](platforms.md) with all six checkouts: the Linux
   containers on both architectures, NixOS and Homebrew. Run it from CI with
   every `*-ref` input set to the release commits, and keep the run's
   artifacts with the release record.

### Step 1: xrdcore

1. In xrdcore, date `## [0.4.0]` in `CHANGELOG.md`; commit; wait for CI.
2. `git tag -a v0.4.0 -m "xrdcore 0.4.0" && git push origin v0.4.0`.
3. The publish workflow rebuilds, checks the artifacts, tag, version and
   changelog, refuses a default dependency, and uploads.
4. Wait until PyPI serves it, from a clean environment:

   ```console
   $ python -m pip download --no-cache-dir --only-binary=:all: --no-deps --dest /tmp/check xrdcore==0.4.0
   ```

### Step 2: xrdsec and xrdhttp

Neither depends on the other at run time, but release **xrdsec first**:
xrdhttp's publish job installs its wheel with the `testing` extra, because
`xrdhttp.testing` (a public module, which the job imports with every other)
is built on xrdsec's test server. For each: date the changelog, commit, wait
for CI, tag `v0.4.0`, push the tag. Their publish jobs install the built
wheel with `--only-binary=:all:` from PyPI's xrdcore (and xrdhttp's from
PyPI's xrdsec), so they fail - and upload nothing - if the earlier step is
not visible yet; re-run the job once it is. Then confirm both:

```console
$ python -m pip download --no-cache-dir --only-binary=:all: --no-deps --dest /tmp/check xrdsec==0.4.0 xrdhttp==0.4.0
```

### Step 3: xrdclient

1. Date `## [0.4.0]`; commit; wait for CI (which still builds against the
   library candidates' `main`; set the `*-ref` inputs to the `v0.4.0` tags
   for the final run).
2. Tag and push `v0.4.0`. `publish.yml`'s build job is xrd-devtools' shared
   `publish` action (`rob-c/xrd-devtools/.github/actions/publish@main`, the
   same steps for every package of the chain; only the PyPI upload stays in
   this workflow, as trusted publishing requires). After the build it runs
   xrd-devtools' `chain_preflight.py pypi dist/*.whl` from the action's own
   copy (as a plain file: it needs only `packaging`), which downloads each xrdcore/xrdsec/xrdhttp requirement of the
   wheel at its lower bound from PyPI (`--only-binary=:all: --no-deps`) and
   refuses to continue if any is missing; it then installs the release wheel
   with PyPI's libraries alone and runs `pip check`, before the tag/changelog
   checks and the upload.
3. Confirm: `pip download --no-cache-dir --only-binary=:all: xrdclient==0.4.0`
   in a clean environment resolves the whole runtime from PyPI.

### Step 4: xgfalclient

Same as step 3, in xgfalclient. Its `publish.yml` gates on PyPI the same way
before uploading, through the same shared `publish` action (`preflight:
true`, which runs `chain_preflight.py pypi dist/*.whl`), so the wheel's exact `xrdclient==0.4.0` pin and the three libraries must be
downloadable as wheels. It
then installs the wheel with `--only-binary=:all:` from PyPI alone, runs `pip
check` and the tag, changelog and gfal2-version checks, and only then uploads.

### Step 5: xrdarc

xrdarc is released on GitHub and PyPI. Its CI checks out the libraries;
once they are on PyPI it can install them from there instead. Date the changelog, commit, wait for CI, then tag and push
`v0.4.0`. Its `.github/workflows/release.yml`:

1. builds the sdist and wheel and runs `twine check --strict`;
2. runs xrd-devtools' `chain_preflight.py pypi dist/*.whl` (steps 1-3 are
   the shared `publish` action every package's release builds with):
   xrdcore, xrdsec and xrdhttp 0.4.0 must be on PyPI as wheels;
3. installs the wheel with `--only-binary=:all:` from PyPI alone, runs `pip
   check`, checks the tag, version and dated changelog and that no storage
   client was installed, and runs the checkout's suite against the installed
   wheel;
4. creates the GitHub release `v0.4.0` ("xrdarc 0.4.0", notes from the
   changelog's `[0.4.0]` section) with the wheel and sdist attached;
5. uploads the same wheel and sdist to PyPI with trusted publishing (owner
   `rob-c`, repository `xrdarc`, workflow `release.yml`, environment `pypi`).

A failed tag uploads nothing; delete it, fix, and tag again, as for the
others. A user installs it with `pip install xrdarc`; pip fetches xrdcore,
xrdsec and xrdhttp from PyPI.

### After the chain

- Build the distribution packages (RPM/DEB, Homebrew bundles) from the
  published versions and record the six source revisions with them.
- In every repository, open the next `Unreleased` changelog section.
- Once the libraries are on PyPI, CI may stop checking out their candidates
  (the `packages` of xrd-devtools' candidates action in each `ci.yml`); keep
  them while library and client changes still land together. xrd-devtools
  itself stays a checkout.

### Rollback

A broken upload is **yanked**, never deleted: on PyPI, *Manage project*,
*Releases*, *Options*, *Yank*, with the reason. A yanked version stays
installable by an exact pin (`==0.4.0`) but is never chosen by a range, so
`xrdcore>=0.4.0,<0.5` moves to the fix as soon as it is published.

- A library is broken: yank it, release `0.4.1` of that library (step 1 or 2
  again). The clients' `<0.5` ranges pick it up with no client release; RPM,
  DEB and Homebrew bundles need a rebuild with an incremented release or
  revision.
- xrdclient is broken: yank it and release `0.4.1`. Because xgfalclient pins
  `xrdclient==0.4.0` exactly, xgfalclient must be re-released (`0.4.1`,
  pinning `xrdclient==0.4.1`) too, or it keeps installing the yanked one.
- The order went wrong (a client reached PyPI before a library it needs):
  publish the library at once; if the client's metadata pins something that
  can never exist, yank the client and release a corrected patch version.
- A tag pushed by mistake whose publish job failed has uploaded nothing:
  delete the tag (`git push --delete origin v0.4.0`), fix, and tag again.
  Never move a tag whose upload succeeded.
