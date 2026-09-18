# xrdclient

A pure-Python client for XRootD. `root://`, `roots://`, `https://`, HEP
WebDAV and `s3://`, spoken by the same objects, with no compiled extension, no
`libXrdCl`, and no third-party import in the core.

```python
import xrdclient

for path in xrdclient.ls("root://eos.example.org//store/user/me"):
    print(path.name, xrdclient.human_bytes(xrdclient.size(path)))

with xrdclient.open("root://eos.example.org//store/data.root", "rb") as fh:
    header = fh.read(1024)

xrdclient.copy("root://a.example.org//store/f.root", "davs://b.example.org/store/f.root")
```

It is a Python library first and an XRootD binding second: files are real
`io` objects, errors are `OSError` subclasses, paths are `PurePath`-shaped,
and nothing returns a `(status, result)` pair.

## Install

```console
$ pip install xrdclient                 # the whole library
$ pip install xrdclient[fsspec]         # pandas / dask / pyarrow URLs
$ pip install xrdclient[krb5]           # the Kerberos mechanism
```

Requires Python 3.9+, which is what RHEL 9 and AlmaLinux 9 ship, so the
system interpreter on a grid login node is enough. Almost nothing needs an
extra: `http://`, `https://` and WebDAV are `http.client`, S3 is that plus
`hmac`, and GSI/X.509 proxies are pure Python down to the AES and RSA.
Kerberos is the one exception — see below.

## What it does

**One-liners.** `xrdclient.ls`, `xrdclient.glob`, `xrdclient.stat`, `xrdclient.exists`, `xrdclient.size`,
`xrdclient.checksum`, `xrdclient.read_text`, `xrdclient.read_bytes`, `xrdclient.write_text`,
`xrdclient.write_bytes`, `xrdclient.mkdir`, `xrdclient.remove`, `xrdclient.move`, `xrdclient.stage` and
`xrdclient.is_online` each take a URL and answer one question, with nothing to
build and nothing to close.

```python
if not xrdclient.is_online("root://tape.example.org//store/f.root"):
    xrdclient.stage("root://tape.example.org//store/f.root")
```

**No bit algebra.** Every flag answers to its own name, and the common
choices are keyword arguments: `fh.open("r")`, `fh.open("new makepath")`,
`fs.scandir(path, stat=False)`, `fs.prepare(paths, evict=True)`,
`fs.query("checksum", path)`, `fs.chmod(path, "rw-r-----")`. A misspelling
says what you probably meant. Printing a flag prints its name, printing a
stat prints the line `ls -l` would have.

**Files.** `xrdclient.open(url, mode)` returns something from the `io` stack:
seekable, buffered, iterable, context-managed, `read`/`write`/`readinto`,
text mode when you ask for it. Vector reads (`kXR_readv`), paged I/O with
CRC32c verification, checkpointed writes, server-side range copies
(`kXR_clone`), and `sendfile`-shaped bulk copies are on the underlying object
when you want them.

```python
with xrdclient.open("root://host//store/f.root", "rb") as fh:
    for line in fh:            # buffered, like any other file
        ...
    fh.seek(-4096, 2)
    tail = fh.read()
```

**Namespaces.** `xrdclient.FileSystem` covers `stat`, `statx`, `statvfs`,
`scandir`, `walk`, `glob`, `mkdir`, `makedirs`, `rename`, `remove`,
`rmtree`, `truncate`, `chmod`, `touch`, `checksum`, `locate`, `deep_locate`,
`prepare` (with `query_prepare` for how the staging is going and
`archive_info` for where a file is now), `query_config`, extended attributes,
and - where a server has been taught the vendor opcodes - `symlink`, `link`,
`readlink`, `lstat`, `is_symlink`, `utime`, `chown` and `listxattr_tree`,
which `extensions()` asks about before sending.

```python
fs = xrdclient.FileSystem("davs://dav.example.org")
fs.makedirs("/store/user/me/2026", exist_ok=True)
print(fs.checksum("/store/user/me/f.root"))     # adler32:1a0b045d
```

**Paths.** `xrdclient.Path` is a `PurePosixPath` that knows its endpoint:

```python
p = xrdclient.Path("root://host//store") / "user" / "me"
p.mkdir(parents=True, exist_ok=True)
(p / "note.txt").write_text("hello")
sizes = {child.name: child.stat().st_size for child in p.iterdir()}
```

**Copies.** `xrdclient.copy`, `xrdclient.copy_tree` and `xrdclient.third_party` move data
between any two endpoints, local paths included, with checksum verification
on by default and a `progress=` callback that takes `(done, total)`. A tree
can be filtered (`include=`, `exclude=`), brought up to date rather than
recopied (`sync="size" | "mtime" | "checksum"`), pruned (`delete=True`),
rehearsed (`dry_run=True`) or moved (`remove_source=True`). Every transfer keeps
`config.in_flight` chunks read ahead of the write it is waiting on, so the two
ends overlap instead of taking turns. An interrupted
transfer is continued rather than restarted with `resume=True`, or `xrd-cp -c`,
and a file long enough to be worth it is moved by `config.parallel_chunks`
connections at once, one span of the file each. A tree of small files copies
`workers=` of them in parallel, `xrd-cp -r --parallel N`.

A download runs on the bulk data plane: several reads in flight per
connection, each received straight into the buffer it will be written from,
across `config.bulk_workers` connections. On a stock `xrootd` that is
1.5 GiB/s to a local file where `xrdcp` does 0.37 GiB/s, with no C extension
involved — see [docs/performance.md](docs/performance.md). A worker that loses
its server re-opens and resumes from where it got to, and a transfer that ends
short of the file's length is an error rather than a truncated file.

A second measurement, further from the ideal case: the same download against
a GSI-authenticated `xrootd` 5.9.7 in a container rather than a bare daemon on
loopback, 1 GiB, median of seven runs each. The harness is
[`examples/gsi_copy_benchmark.py`](examples/gsi_copy_benchmark.py), which mints
its own CA and proxy, starts the server, and times whichever clients this
machine has.

| Client | Median | Range | vs `xrdcp` |
| --- | --- | --- | --- |
| **`xrdclient`, bulk data plane** | **305.8 MiB/s** | 296–320 | **1.56×** |
| `brix-xrdcp`, BriX, pure C | 292.1 MiB/s | 266–322 | 1.49× |
| `xrdclient`, one connection | 259.7 MiB/s | 218–276 | 1.32× |
| XRootD Python bindings, official | 205.0 MiB/s | 156–210 | 1.04× |
| `xrdcp`, official C++ v6.1.1 | 196.2 MiB/s | 159–204 | 1.00× |

Read that as two findings and one caveat. Pure Python is level with a pure-C
client — 5% apart, with overlapping ranges, because neither is bound by the
language on a copy: both are bound by how many reads they keep in flight.
And the official client's own ceiling is about two thirds of either, which is
a pipelining difference rather than a language one; its Python bindings sit
with it, as the same engine underneath should. The caveat is that this was
loopback through a container's NAT on one laptop, so it measures a client's
protocol efficiency and not a network — on a link with real latency the
pipelining matters more, not less, but the numbers would be that link's.

**Objects.** A bucket is one more endpoint: `s3://bucket/key` reads, writes,
lists and copies through the same `xrdclient.open`, `xrdclient.FileSystem` and `xrdclient.copy`,
signed with AWS SigV4 out of `hmac` and `hashlib` — no `boto3`, in the
dependency tree or the import graph. Credentials come from the environment or
`~/.aws/credentials`, or are left out entirely for a public bucket; an object
too long to hold goes up as a multipart upload, and a failed one is aborted
rather than left in the bucket. Ceph RGW, MinIO and anything else with an
endpoint are addressed path-style, AWS virtual-hosted.

```python
fs = xrdclient.FileSystem("s3://my-bucket", endpoint="https://rgw.example.org")
fs.listdir("/store/user/me")
xrdclient.copy("root://eos.example.org//store/f.root", "s3://my-bucket/store/f.root")
```

**Built on this.** Three packages of their own, each depending on the one
before it, so a client install stays a client install:

| | |
| --- | --- |
| [`xrdroot`](https://github.com/rob-c/xrdroot) | the ROOT file format in pure Python — trees read a basket at a time over any URL here, histograms, graphs, and a writer |
| [`xrdml`](https://github.com/rob-c/xrdml) | a URL in, minibatches of `(inputs, answers)` out; trees into a PyTorch `DataLoader` or a `tf.data.Dataset`, with nothing downloaded |
| [`xrddatasets`](https://github.com/rob-c/xrddatasets) | more than fourteen hundred open datasets converted into streamable ROOT, and the `xrd-datasets` command that publishes the catalogue serving them |

```console
$ pip install xrdml          # brings xrdroot and this client with it
```

**Async.** `xrdclient.aio` mirrors the whole surface — same names, same arguments,
`await` in front. `import xrdclient` does not import `asyncio`; the facade is
resolved on first use.

```python
import asyncio, xrdclient.aio

async def main():
    async with xrdclient.aio.FileSystem("root://eos.example.org") as fs:
        async for entry in fs.iterdir("/store"):
            print(entry.name)
        names = await fs.listdir("/store")
        sizes = await asyncio.gather(*(fs.getsize(f"/store/{n}") for n in names))
        async with fs.open("/store/f.root") as fh:
            head, tail = await fh.readv([(0, 4096), (1 << 20, 4096)])

asyncio.run(main())
```

**Authentication.** `gsi`, `ztn`, `sss`, `unix` and `host` out of the box,
tried in that order against whatever the server offers, with every rejected
mechanism and its reason named in the final error. That means X.509 proxies
from `$X509_USER_PROXY` (RFC 3820 and legacy Globus, with the lifetime
checked *before* the round trip, so an expired proxy is a sentence and not a
timeout an hour into a job) and WLCG / SciTokens / macaroons. TLS on
`roots://`, `xroots://` and `davs://` — all three present the same proxy as
the client certificate, so mutual TLS costs no argument.

`krb5` is the one mechanism that needs an extra: it reads your credential
cache with no help at all, and will tell you when your ticket expired, but the
exchange itself goes through `gssapi` because a Kerberos token can only
honestly be tested against a live KDC.

At a terminal, a login with no proxy and no token asks for one — naming what
is missing, where it looked, and the command that produces it — instead of
failing; in a batch job it stays silent and puts the same explanation in the
error. `Config(prompt=False)`, `--no-prompt` and `$XRD_PROMPT=0` settle it
either way, and `Config(prompter=...)` moves the question into a GUI or a
notebook.

Credentials are redacted from logs, reprs and tracebacks — that is enforced by
a test, not a convention.

**Guard rails.** Safer than the stock tools where a beginner meets them:
`read()` on a file bigger than `config.max_read_size` raises with the sentence
that streams it instead of filling memory; `xrd-cp` refuses to overwrite
without `-f`; `xrd-fs rm -r` asks at a terminal, with a count of what is about
to go, and refuses a path less than two components deep until `--yes`; and
`root://host/store/f` means the same file as `root://host//store/f` rather
than a confusing miss. Each has one flag that says "yes, I mean it".

When something does go wrong, `xrd-fs doctor` (or `xrdclient.diagnose()`) asks every
question a transfer would ask - settings, each authentication mechanism and
what would fix it, DNS, the port, the login, how far down the path exists -
and prints one line each, so the first `!!` is the cause rather than the last
symptom. See [Safety](https://rob-c.github.io/xrdclient/safety/).

## Command line

```console
$ xrd-fs ls -l root://eos.example.org//store/user/me
$ xrd-fs stat --json davs://dav.example.org/store/f.root
$ xrd-fs checksum -a adler32 root://host//store/f.root
$ xrd-fs tail -f root://host//store/running.log
$ xrd-fs du root://host//store/run7
$ xrd-fs doctor root://eos.example.org//store/user/me   # why will this not work?
$ xrd-cp -r /tmp/results davs://dav.example.org/store/results
$ xrd-cp -r --sync size --delete /tmp/results root://host//store/results/
$ xrd-cp --tpc root://a//store/f.root root://b//store/f.root
```

Every subcommand takes whole URLs and understands `--json`, so a shell script
never has to parse columns. Exit codes are the usual three: `0` success,
`1` a runtime failure, `2` a usage error. Settings that never change can live
in `~/.config/xrd/config.ini` and be selected with `--alias`.

## fsspec

With the `[fsspec]` extra, `root://`, `roots://`, `xroot://`, `dav://`,
`davs://` and `webdav://` are registered URL schemes:

```python
import pandas as pd
df = pd.read_parquet("root://eos.example.org//store/t.parquet")
```

## Testing against it

`xrdclient.testing` ships the servers this library's own suite runs against — no
storage element required:

```python
from xrdclient.testing import FakeServer

with FakeServer(files={"/data/a.root": b"hello"}) as server:
    fs = xrdclient.FileSystem(server.url)
    assert fs.read_bytes("/data/a.root") == b"hello"
```

`FakeDAVServer` is the same idea for HTTP and WebDAV, and `FakeS3Server` a
bucket — signatures checked against the AWS specification rather than trusted.

## Status

The wire protocol, session state machine, the whole authentication ladder,
file and namespace APIs, `pathlib` bindings, the async facade, HTTP/WebDAV,
S3, the copy engine, the bulk data plane, the CLI and the fsspec bindings are
implemented and tested —
2,815 tests, of which the great majority need no network, no KDC and no
`openssl`. The remainder are the interoperability suite, which runs against a
real `xrootd` daemon and reads back what `xrdcp` and `xrdfs` write, and the
parity suite, which runs every operation through this client and the official
XRootD bindings and compares the answers field by field. Coverage is 100% of
statements *and* branches across the package, and the wire protocol, the
cryptography and the client surface are gated there; `ruff` and
`mypy --strict` pass clean and are hard gates too. An absolute
[maintainability gate](docs/maintainability.md) reports CCN, Cognitive
Complexity, NPath, Halstead Volume and maximum nesting per function and file;
the same limits apply to all existing and new code, without baseline allowances. The
package ships `py.typed`, and `xrdclient.open` is overloaded the way the builtin is,
so a literal mode tells your type checker whether you get bytes or text.

Staging from tape works in both dialects from the same three method names:
`prepare`/`query_prepare`/`cancel_prepare` send `kXR_prepare` and `kXR_QPrep`
to a `root://` endpoint and drive the WLCG Tape REST API - the one FTS and
Rucio use - at an `http(s)`/`dav(s)` one, and `archive_info` answers "on disk
or still on tape" over either.

Third-party copy works in both dialects from one call: `xrdclient.third_party` sends
the `XrdOucTPC` rendezvous to a `root://` pair and the WLCG `COPY` dialect to
an `http(s)`/`dav(s)` one, so the bytes move server to server either way.

Connections are pooled across instances: a `FileSystem` that closes hands its
authenticated connection to the next one opened on the same server by the same
person, so a script that constructs one per file logs in once rather than a
thousand times. Reuse is keyed on the credentials as well as the endpoint, and
a connection that failed is discarded rather than passed on.

A file being streamed can put its bytes on a connection of their own:
`bind_data_path()` binds a second socket to the same session with `kXR_bind`,
and from then on reads and writes travel there while requests keep the
control link to themselves. The second connection inherits the session's
identity rather than logging in again.

Not yet: GSI's signed-DH path and X.509 delegation, both refused by name
rather than mis-answered, and HTTP/2.

Full documentation is in [`docs/`](docs/) (`mkdocs serve` to read it), with
[`SECURITY.md`](SECURITY.md) for the threat model,
[`benchmarks/bench.py`](benchmarks/bench.py) for the measurements,
[`docs/superpowers/plans/`](docs/superpowers/plans/) for the roadmap and
[`docs/superpowers/specs/`](docs/superpowers/specs/) for the design.

## Licence

LGPL-3.0-or-later: [`LICENSE`](LICENSE) is the Lesser terms, which apply on
top of the GPL text in [`COPYING`](COPYING).
