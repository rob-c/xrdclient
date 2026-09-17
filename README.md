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

It is a Python library first and an XRootD binding second: files are real
`io` objects, errors are `OSError` subclasses, paths are `PurePath`-shaped,
and nothing returns a `(status, result)` pair.

## Install

```console
$ pip install pyxrootdclient                 # the whole library
$ pip install pyxrootdclient[fsspec]         # pandas / dask / pyarrow URLs
$ pip install pyxrootdclient[krb5]           # the Kerberos mechanism
```

Requires Python 3.9+, which is what RHEL 9 and AlmaLinux 9 ship, so the
system interpreter on a grid login node is enough. Almost nothing needs an
extra: `http://`, `https://` and WebDAV are `http.client`, S3 is that plus
`hmac`, and GSI/X.509 proxies are pure Python down to the AES and RSA.
Kerberos is the one exception — see below.

## What it does

**One-liners.** `xrd.ls`, `xrd.glob`, `xrd.stat`, `xrd.exists`, `xrd.size`,
`xrd.checksum`, `xrd.read_text`, `xrd.read_bytes`, `xrd.write_text`,
`xrd.write_bytes`, `xrd.mkdir`, `xrd.remove`, `xrd.move`, `xrd.stage` and
`xrd.is_online` each take a URL and answer one question, with nothing to
build and nothing to close.

```python
if not xrd.is_online("root://tape.example.org//store/f.root"):
    xrd.stage("root://tape.example.org//store/f.root")
```

**No bit algebra.** Every flag answers to its own name, and the common
choices are keyword arguments: `fh.open("r")`, `fh.open("new makepath")`,
`fs.scandir(path, stat=False)`, `fs.prepare(paths, evict=True)`,
`fs.query("checksum", path)`, `fs.chmod(path, "rw-r-----")`. A misspelling
says what you probably meant. Printing a flag prints its name, printing a
stat prints the line `ls -l` would have.

**Files.** `xrd.open(url, mode)` returns something from the `io` stack:
seekable, buffered, iterable, context-managed, `read`/`write`/`readinto`,
text mode when you ask for it. Vector reads (`kXR_readv`), paged I/O with
CRC32c verification, checkpointed writes, server-side range copies
(`kXR_clone`), and `sendfile`-shaped bulk copies are on the underlying object
when you want them.

```python
with xrd.open("root://host//store/f.root", "rb") as fh:
    for line in fh:            # buffered, like any other file
        ...
    fh.seek(-4096, 2)
    tail = fh.read()
```

**Namespaces.** `xrd.FileSystem` covers `stat`, `statx`, `statvfs`,
`scandir`, `walk`, `glob`, `mkdir`, `makedirs`, `rename`, `remove`,
`rmtree`, `truncate`, `chmod`, `touch`, `checksum`, `locate`, `deep_locate`,
`prepare` (with `query_prepare` for how the staging is going and
`archive_info` for where a file is now), `query_config`, extended attributes,
and - where a server has been taught the vendor opcodes - `symlink`, `link`,
`readlink`, `lstat`, `is_symlink`, `utime`, `chown` and `listxattr_tree`,
which `extensions()` asks about before sending.

```python
fs = xrd.FileSystem("davs://dav.example.org")
fs.makedirs("/store/user/me/2026", exist_ok=True)
print(fs.checksum("/store/user/me/f.root"))     # adler32:1a0b045d
```

**Paths.** `xrd.Path` is a `PurePosixPath` that knows its endpoint:

```python
p = xrd.Path("root://host//store") / "user" / "me"
p.mkdir(parents=True, exist_ok=True)
(p / "note.txt").write_text("hello")
sizes = {child.name: child.stat().st_size for child in p.iterdir()}
```

**Copies.** `xrd.copy`, `xrd.copy_tree` and `xrd.third_party` move data
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

**Objects.** A bucket is one more endpoint: `s3://bucket/key` reads, writes,
lists and copies through the same `xrd.open`, `xrd.FileSystem` and `xrd.copy`,
signed with AWS SigV4 out of `hmac` and `hashlib` — no `boto3`, in the
dependency tree or the import graph. Credentials come from the environment or
`~/.aws/credentials`, or are left out entirely for a public bucket; an object
too long to hold goes up as a multipart upload, and a failed one is aborted
rather than left in the bucket. Ceph RGW, MinIO and anything else with an
endpoint are addressed path-style, AWS virtual-hosted.

```python
fs = xrd.FileSystem("s3://my-bucket", endpoint="https://rgw.example.org")
fs.listdir("/store/user/me")
xrd.copy("root://eos.example.org//store/f.root", "s3://my-bucket/store/f.root")
```

**ROOT files.** `xrd.root` opens a ROOT file and reads its trees in pure
Python — no ROOT, no `uproot`, no `numpy`, no compiled extension. Nothing is
downloaded: a tree is read a basket at a time, so a hundred-gigabyte file is
walked from a laptop over the network, and `xrd.root.ml` turns those batches
into tensors for a PyTorch `DataLoader` or a `tf.data.Dataset`. Split C++
classes — member by member, or the whole object per entry as a dictionary,
and unsplit ones walked straight out of the file's own layout —
STL containers, `std::map`, both kinds of string and the packed
`Double32_t`/`Float16_t` floats are read, under every compression ROOT
writes. So are the objects beside the tree: a `TH1`, `TH2` or `TH3` comes back
as a `Histogram`, with its bins, edges and errors where you would look for
them, and a graph — layered error bars and all — as a `Graph` you can walk a
point at a time, inside another object as well as in a key. Containers written
field by field, `TClonesArray` however it was told to write itself, and
`vector<pair>` all read back as what they are. The few columns this reader will not decode are listed with the
reason rather than silently missing, because a plausible misreading of
physics data is worse than a refusal. Histograms and graphs draw themselves —
`.plot()` onto matplotlib axes when matplotlib is there, `.text()` into plain
characters when it is not — and `xrd.root.create` writes a new ROOT file:
trees filled entry by entry and flushed a basket at a time, histograms,
graphs, strings and arrays of numbers, under every compression ROOT itself
writes, still from nothing but the standard library. `xrd.root.datasets` turns
more than fourteen hundred datasets and teaching tables into streamable ROOT —
the MNIST family and EMNIST, CIFAR-10 and CIFAR-100, Semeion, the spoken
digits of FSDD, the SMS Spam Collection, MiniBooNE, the MAGIC telescope, the
HTRU2 pulsar survey, Human Activity Recognition, Iris, the Palmer penguins,
Covertype, Adult, Mushroom, Letter Recognition, Optical Digits, Wine, Wine
Quality, Spambase, Ionosphere, Glass, Abalone, Banknote Authentication, Breast
Cancer Wisconsin, Dry Bean, Seeds, Yeast, Heart Disease, Car Evaluation, Auto
MPG, Bike Sharing, Energy Efficiency, Real Estate Valuation, Student
Performance, fifty more of the UCI teaching shelf from Sonar and Soybean to
Bank Marketing, the Landsat satellite images, the pen-written digits and the
office-occupancy recordings, more than 220 default 100 MB–2 GB disk-backed sources
from UCI, NIST, Toronto, Zenodo and Salesforce including EMNIST, SUSY, JetNet,
OmniFold Big, TinySOL, Speech Commands, AudioMNIST, CirCor heart sounds,
WikiText-103, ReefSet, BioDCASE, BirdSet, UAV Maize Stress, Wildlife MNIST and
Year Prediction MSD, the compact SODv2 orbital bounding-box and DLR all-sky
cloud-mask sets, plus eight registered
multi-gigabyte UCI converters admitted explicitly with `--allow-oversize`, a hundred read
straight from the archive's own CSV, from ISOLET and Poker Hand to Connect-4,
the splice junctions, the Steel Plates faults and the CDC diabetes survey,
the 17 CC BY MedMNIST image and volume problems, Galaxy10 SDSS morphology,
Curiosity rover surface images and SWEFil solar-filament masks and boxes,
100 CC BY Alex-MP-20 crystal-image regression tasks over 675,204 inorganic
structures, each with publisher splits, geometry-only and atomic-number projections,
100 visual field-learning tasks drawn from sixteen CC BY 4.0 archives in The Well,
spanning acoustics, active matter, nonlinear waves, planetary atmospheres,
convection, hydrodynamic instabilities, soft matter, plasma and stellar astrophysics,
three hundred and thirty-two
teaching tables of the R world from Boston Housing and Old Faithful to the
Lahman baseball records, the NYC flights, the survival studies, Arbuthnot's
christenings, Snow's cholera deaths and what the archaeologists dug up, and
sixty-eight country-year tables charted by Our World in Data, from CO2
emissions and energy use to child mortality, the warming sea and the ice
sheets, and 499 explicitly licensed, public Hub repositories whose complete
scalar Parquet conversions produce 805 bounded output splits across 1,235 shards —
into ROOT files a tree per
class, or one tree of every row where what is predicted is a number rather
than a class, fetched from whoever publishes them and carrying their licence
in the file, so a training loop can read them straight off a storage element
without anybody having to leave the tools they already use. Images, audio,
text, dates, timestamps, spreadsheets and plain blocks of numbers all fit;
none of the data is redistributed here, only the converter. Five programs in
`examples/` work off a `root://` URL: MNIST twice, an autoencoder on CIFAR-10,
a small convolutional net on Fashion-MNIST, and a raw/normalized JARVIS crystal
projection viewer. None of the training examples holds more than a pool of
rows of the file, and the viewer reads only its selected entry.

```python
import torch, xrd.root, xrd.root.ml

tree = xrd.root.open_root("root://eos.example.org//store/events.root").tree()
pt = tree["Muon_pt"].array(0, 10_000)             # array.array, or Jagged rows

loader = torch.utils.data.DataLoader(
    xrd.root.ml.dataset(tree, ["Muon_pt"], step=8192), batch_size=None, num_workers=4
)
```

**Machine learning, the short way.** `xrd.ml` is the same reading with none of
the arithmetic: a URL in, minibatches of `(inputs, answers)` out. Which trees
are the training rows, which column is the picture and which the answer, how
much to hold at once, what to divide the bytes by and which tensor type each
loss function wants are all read off the file rather than written down, and
printed if you ask. Nothing is downloaded, and no framework is imported until
a batch is.

```python
import xrd.ml

data = xrd.ml.load("root://eos.example.org//store/mnist.root")
print(data)          # 70,000 rows, 10 classes; inputs image: 784 x uint8; answer label
print(data.train.preview())                     # the first digit, drawn in characters

for images, labels in data.train.batches(256):  # already float, already scaled
    loss = criterion(model(images), labels)
```

**Async.** `xrd.aio` mirrors the whole surface — same names, same arguments,
`await` in front. `import xrd` does not import `asyncio`; the facade is
resolved on first use.

```python
import asyncio, xrd.aio

async def main():
    async with xrd.aio.FileSystem("root://eos.example.org") as fs:
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

When something does go wrong, `xrd-fs doctor` (or `xrd.diagnose()`) asks every
question a transfer would ask - settings, each authentication mechanism and
what would fix it, DNS, the port, the login, how far down the path exists -
and prints one line each, so the first `!!` is the cause rather than the last
symptom. See [Safety](https://rob-c.github.io/PyXRootDClient/safety/).

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

`xrd.testing` ships the servers this library's own suite runs against — no
storage element required:

```python
from xrd.testing import FakeServer

with FakeServer(files={"/data/a.root": b"hello"}) as server:
    fs = xrd.FileSystem(server.url)
    assert fs.read_bytes("/data/a.root") == b"hello"
```

`FakeDAVServer` is the same idea for HTTP and WebDAV, and `FakeS3Server` a
bucket — signatures checked against the AWS specification rather than trusted.

## Status

The wire protocol, session state machine, the whole authentication ladder,
file and namespace APIs, `pathlib` bindings, the async facade, HTTP/WebDAV,
S3, the copy engine, the CLI, the fsspec bindings and the pure-Python ROOT
reader and writer are implemented and tested —
3014 tests, of which the great majority need no network, no KDC and no
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
package ships `py.typed`, and `xrd.open` is overloaded the way the builtin is,
so a literal mode tells your type checker whether you get bytes or text.

Staging from tape works in both dialects from the same three method names:
`prepare`/`query_prepare`/`cancel_prepare` send `kXR_prepare` and `kXR_QPrep`
to a `root://` endpoint and drive the WLCG Tape REST API - the one FTS and
Rucio use - at an `http(s)`/`dav(s)` one, and `archive_info` answers "on disk
or still on tape" over either.

Third-party copy works in both dialects from one call: `xrd.third_party` sends
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
