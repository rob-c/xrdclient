# Contributing to xrdclient

Changes are welcome when they preserve the three properties the client is
built around: protocol correctness, Python-native behaviour and a faster data
plane than the client it replaces.

## Development setup

Use an isolated environment; the project supports Python 3.9 through 3.14.

```console
$ python3 -m venv .venv
$ .venv/bin/python -m pip install -U pip
$ .venv/bin/python -m pip install -e '.[dev,docs]'
$ .venv/bin/pytest -q -m 'not interop and not parity'
```

The ordinary suite is hermetic. Tests marked `interop` need a real XRootD
daemon, and tests marked `parity` also need the official Python bindings.

## Definition of done

A change is ready when:

- behaviour is covered at the public boundary and, for protocol changes,
  against a wire-level test server;
- all resource ownership paths are explicit and cancellation, timeout and
  partial-I/O behaviour are tested;
- `ruff check`, `ruff format --check`, `mypy` and the maintainability gate are
  clean;
- user-visible behaviour and compatibility differences are documented;
- a data-path change passes the paired performance gate and does not trade
  correctness for a benchmark result; and
- wheels and source archives build and pass strict metadata checks.

Run the complete local gate documented in [Testing](docs/testing.md). The
maintainability limits are absolute rather than a ratchet; do not raise a
limit or add an exclusion to land a change.

## Compatibility and performance

The native API uses Python conventions. The `xrdclient.compat` API instead
matches XrdCl, including awkward return shapes which existing callers may
depend on. A deliberate difference belongs in the compatibility docs and a
test that demonstrates both behaviours.

Measure performance changes with the checked-in harness, on the same machine
and server, with paired interleaved rounds. Report the command, payload size,
round count and distribution—not only the best result.

## Documentation and releases

Public functions need type annotations and docstrings that explain contracts,
failure modes and ownership rather than restating the signature. Build the
site with `mkdocs build --strict`; warnings are failures.

Release preparation is described in [Releasing](docs/releasing.md). Add
user-visible changes to [CHANGELOG.md](CHANGELOG.md) in the same pull request.
