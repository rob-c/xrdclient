# Releasing

This is the release checklist for maintainers. A tag is a publication request:
the publish workflow rejects a tag which does not exactly match the runtime and
wheel version.

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
