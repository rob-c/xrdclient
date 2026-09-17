# PyXRootDClient

A pure-Python client for XRootD. `root://`, `roots://`, `https://`, HEP
WebDAV and `s3://`, spoken by the same objects, with no compiled extension, no
`libXrdCl`, and no third-party import in the core.

```python
import xrd

for path in xrd.ls("root://eos.example.org//store/user/me"):
    print(path.name, xrd.human_bytes(xrd.size(path)))

with xrd.open("root://eos.example.org//store/data.root", "rb") as fh:
    header = fh.read(1024)

xrd.copy("root://a.example.org//store/f.root", "davs://b.example.org/store/f.root")
```

It is a Python library first and an XRootD binding second.

## The design in one table

| A Python programmer expects | and gets |
| --- | --- |
| `open()` to return a file object | `xrd.open()` returns one from the `io` stack - buffered, seekable, iterable, text mode on request |
| a missing file to raise `FileNotFoundError` | it does; every error is an `OSError` or `XRootDError` subclass with the right `errno` |
| paths to behave like `pathlib` | `xrd.Path` is `PurePosixPath` shaped and knows its endpoint |
| `with` to clean up | every handle, filesystem and session is a context manager |
| no status codes to check | nothing returns `(status, result)` - see [Coming from pyxrootd](migrating.md) |
| `async` to be `await` in front | `xrd.aio` mirrors the whole surface |
| never to add up bit flags | you never do - `fh.open("r")`, `fs.prepare(paths, evict=True)`, `fs.chmod(path, "rw-r-----")` |

## Install

```console
$ pip install pyxrootdclient                 # the whole library
$ pip install pyxrootdclient[fsspec]         # pandas / dask / pyarrow URLs
$ pip install pyxrootdclient[krb5]           # the Kerberos mechanism
```

Python 3.9 or newer - the version RHEL 9 and AlmaLinux 9 ship, so a grid
login node needs nothing installed but this. Almost nothing needs an extra:
`http://`, `https://` and WebDAV are `http.client`, and GSI / X.509 proxies
are pure Python down to the AES and the RSA. Kerberos is the single
exception, because a Kerberos token can only honestly be tested against a
live KDC.

## Where to go next

- **[Easy mode](easy.md)** - fifteen one-line verbs on a URL, for when there
  is one question to ask and no reason to learn a class first.
- **[Quickstart](quickstart.md)** - the ten things you will actually do.
- **[Files and paths](files.md)**, **[Namespaces](filesystem.md)**,
  **[Copying](copying.md)** - the three halves of the API.
- **[S3 object storage](s3.md)** - the same three entry points over a bucket.
- **[Authentication](auth.md)** - proxies, tokens, keytabs, and what to do
  when the ladder refuses everything.
- **[Coming from pyxrootd](migrating.md)** - a translation table.
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
implemented and tested - 2,815 tests, the great majority of which need no network, no KDC and no
`openssl`, plus [interoperability and parity suites](interop.md) that run
against a real `xrootd` daemon and the official bindings side by side.
Coverage is 100% of statements and branches across the package, and `proto/`,
`crypto/`, `client/` and `s3/` are gated at 100%;
`ruff` and `mypy --strict` pass clean over the package, which ships
`py.typed` ([Typing](typing.md)).

Third-party copy works in both dialects from one call: `xrd.third_party` sends
the `XrdOucTPC` rendezvous to a `root://` pair and the WLCG `COPY` dialect to
an `http(s)`/`dav(s)` one, so the bytes move server to server either way.

Not yet: GSI's signed-DH path and X.509 delegation, both refused by name
rather than mis-answered, and HTTP/2.

## Licence

LGPL-3.0-or-later: `LICENSE` in the repository carries the Lesser terms, which
apply on top of the GPL text in `COPYING`.
