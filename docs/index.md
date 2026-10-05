# xrdclient

A Python 3 client for XRootD. `root://`, `roots://`, `https://`, HEP
WebDAV and `s3://`, spoken by the same objects. General-purpose libraries own parsing and
cryptographic primitives.

```python
import xrdclient

for path in xrdclient.ls("root://eos.example.org//store/user/me"):
    print(path.name, xrdclient.human_bytes(xrdclient.size(path)))

with xrdclient.open("root://eos.example.org//store/data.root", "rb") as fh:
    header = fh.read(1024)

xrdclient.copy("root://a.example.org//store/f.root", "davs://b.example.org/store/f.root")
```

It is a Python library first and an XRootD binding second.

## The design in one table

| A Python programmer expects | and gets |
| --- | --- |
| `open()` to return a file object | `xrdclient.open()` returns one from the `io` stack - buffered, seekable, iterable, text mode on request |
| a missing file to raise `FileNotFoundError` | it does; every error is an `OSError` or `XRootDError` subclass with the right `errno` |
| paths to behave like `pathlib` | `xrdclient.Path` is `PurePosixPath` shaped and knows its endpoint |
| `with` to clean up | every handle, filesystem and session is a context manager |
| no status codes to check | nothing returns `(status, result)` - see [Coming from pyxrootd](migrating.md) |
| `async` to be `await` in front | `xrdclient.aio` mirrors the whole surface |
| never to add up bit flags | you never do - `fh.open("r")`, `fs.prepare(paths, evict=True)`, `fs.chmod(path, "rw-r-----")` |

## Install

```console
$ pip install xrdclient                 # portable base install
```

Requires Python 3.9.2+. Runtime dependencies are `botocore`, `PyJWT[crypto]`,
`urllib3`, `asn1crypto` and `cryptography`.
XML and binary record parsing use local standard-library helpers.
Native Kerberos bindings are optional:
`pip install 'xrdclient[krb5]'` installs python-gssapi and pykrb5. macOS has
wheels; Linux source installs need Kerberos development headers and a compiler.
CI checks base dependency wheels for Intel/ARM64 macOS, glibc and musl Linux.

The maintained libraries own AWS signing, JWT claim decoding, DER primitives,
cipher/curve operations and connection setup/TLS. Protocol-specific GSI,
RFC 3820/VOMS policy, redirects and upload handshakes remain thin client adapters.

## Where to go next

- **[JSON/XML command output](output.md)** - typed reports for every command,
  including errors, staging states, progress and binary stdout.
- **[Easy mode](easy.md)** - fifteen one-line verbs on a URL, for when there
  is one question to ask and no reason to learn a class first.
- **[Quickstart](quickstart.md)** - the ten things you will actually do.
- **[Files and paths](files.md)**, **[Namespaces](filesystem.md)**,
  **[Copying](copying.md)** - the three halves of the API.
- **[S3 object storage](s3.md)** - the same three entry points over a bucket.
- **[Authentication](auth.md)** - proxies, tokens, keytabs, and what to do
  when the ladder refuses everything.
- **[Replacing PyXRootD](porting.md)** - run code written for `XRootD.client`
  on this library by changing one import, or none; with a
  [reference](compat-reference.md), a [cookbook](compat-cookbook.md) and
  [troubleshooting](compat-troubleshooting.md).
- **[Coming from pyxrootd](migrating.md)** - a translation table to the
  native API.
- **[Performance](performance.md)** - measured against `xrdcp` and the
  official bindings, with the numbers and the harness.
- **[Safety](safety.md)** - the guard rails the stock clients do not have.
- **[Security](security.md)** - the threat model and what is enforced.

## Built on this

Three packages of their own, installed separately, each depending on the one
before it:

- **[`xrdroot`](https://github.com/rob-c/xrdroot)** - the ROOT file format in pure Python: trees,
  C++ objects split and unsplit, STL containers, histograms and graphs that
  draw themselves, and a writer.
- **[`xrdml`](https://github.com/rob-c/xrdml)** - a URL in, minibatches of `(inputs, answers)` out,
  and nothing downloaded in between.
- **[`xrddatasets`](https://github.com/rob-c/xrddatasets)** - open data converted into streamable
  ROOT files, and the site that serves the catalogue.

## Status

The wire protocol, the session state machine, the whole authentication ladder,
file and namespace APIs, `pathlib` bindings, the async facade, HTTP/WebDAV,
S3, the copy engine, the bulk data plane, the CLI and the fsspec bindings are
implemented and tested. The great majority of tests need no network, no KDC
and no `openssl`, plus [interoperability and parity suites](interop.md) that run
against a real `xrootd` daemon and the official bindings side by side.
Coverage is 100% of statements and branches across the package, and `proto/`,
`crypto/`, `client/` and `s3/` are gated at 100%;
`ruff` and `mypy --strict` pass clean over the package, which ships
`py.typed` ([Typing](typing.md)).

Third-party copy works in both dialects from one call: `xrdclient.third_party` sends
the `XrdOucTPC` rendezvous to a `root://` pair and the WLCG `COPY` dialect to
an `http(s)`/`dav(s)` one, so the bytes move server to server either way.

Not yet: HTTP/2.

## Licence

LGPL-3.0-or-later: `LICENSE` in the repository carries the Lesser terms, which
apply on top of the GPL text in `COPYING`.
