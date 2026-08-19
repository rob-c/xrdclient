# A datasets site

One directory that holds the open datasets people learn machine learning
with, pre-converted to ROOT files, indexed, checksummed, and ready to serve —
so that on any laptop:

```bash
export XRD_CATALOGUE=https://data.example.org
```

```python
import xrd.ml

data = xrd.ml.load("mnist")
for images, labels in data.train.batches(256):
    ...
```

streams minibatches straight off your server, with nothing downloaded first
and nothing installed but this library. This page is how to build that
directory, keep it honest, and put it on the web.

## Build it

```console
$ xrd-datasets build /srv/datasets --jobs 4
mnist: 70000 rows, 10.4 MiB
iris: 150 rows, 5.2 KiB
...
```

`build` fetches each dataset from the people who publish it, converts every
split into one ROOT file — one tree per class, the licence and source
recorded in the file's own `about` key — and writes two things beside them:

- `index.json` — what each file is, where it came from, its size, its
  `adler32`, and its row counts per tree. This is the catalogue that
  `xrd.ml.load("name")` resolves names against.
- `MANIFEST` — one checksum line per file, for anyone verifying with their
  own tools.

A second run keeps what is already on disk and re-indexes it; `--force`
starts over, and `--only "mnist*"` (repeatable) narrows the build. A dataset
whose download fails fails that dataset alone: the rest build, the index
records what succeeded, and the exit code says something went wrong.

## The licence gate

Nothing in `xrd.root.datasets` is redistributed by this library — but a site
built from it *does* redistribute, so `build` converts only datasets whose
licence allows passing the files on (`CC0`, `CC BY` and `BY-SA`, MIT, BSD,
Apache, the GPL family, public domain). The two CIFAR sets carry no formal
licence, so they are left out unless you say `--all`, which is for a
directory you serve only to yourself. The gate is
`xrd.root.datasets.redistributable`, and every index entry records the
verdict alongside the licence text, so the site itself says what its terms
are.

Attribution and share-alike obligations still apply to what the gate lets
through; they are met by the licence statement every converted file carries.

## Check it

```console
$ xrd-datasets verify /srv/datasets
586 of 586 files match the index
```

`verify` reopens every file, compares size and checksum with the index, and
reads the trees back to check the row counts. It refuses — exit `1`, one
line per problem — to bless a directory that no longer matches what its
index claims, which is the check to run after any deploy and in CI before
one.

## Serve it

```console
$ xrd-datasets site /srv/datasets --base-url https://data.example.org
wrote index.html, nginx.conf, brix.conf, xrd-datasets.service, README.md in /srv/datasets
```

Everything lands next to the files, so *the directory is the deploy*:

- `index.html` — a browsable, searchable page with the whole index embedded;
  it works served or opened from disk, and shows every visitor the two lines
  of setup at the top of this page.
- `nginx.conf` — static hosting for any stock nginx: drop it in
  `conf.d/`, and range requests (which `xrd.ml` reads by) come from nginx
  itself.
- `brix.conf` — the same directory over `root://` (1094), WebDAV (8008) and
  plain HTTP (8080) with a BriX (nginx-xrootd) build of nginx, read-only on
  every plane. This client's data-path probing speaks to BriX's
  `brix.substreams` advertisement out of the box.
- `xrd-datasets.service` — the systemd unit that runs the BriX flavour.

## Rebuild and redeploy from CI

The repository ships `.github/workflows/site.yml`: run it by hand (choosing
the datasets and the base URL) or push a `site-*` tag, and it builds,
verifies, generates the site, and uploads the whole directory as one
artifact. Deployment is downloading that artifact where nginx looks —
because a directory whose index matches its bytes is the entire state of the
site, there is nothing else to migrate.

## Point a training loop at it

Names resolve through the catalogue, whole URLs go straight to the file, and
both read the same way:

```python
xrd.ml.load("mnist")                                        # via XRD_CATALOGUE
xrd.ml.load("root://data.example.org//mnist.root")          # the native protocol
xrd.ml.load("https://data.example.org/mnist.root")          # plain HTTP ranges
```

See [Machine learning](ml.md) for what happens next, and
[Training playbooks](playbooks.md) for serving a directory ad hoc, with no
daemon and no login, while you decide whether to keep it.
