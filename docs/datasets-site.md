# A datasets site

One directory that holds the open datasets people learn machine learning
with, pre-converted to ROOT files, indexed, checksummed, and ready to serve —
so that on any laptop, using the built-in
`http://ai.edi.scotgrid.ac.uk` catalogue by default:

```python
import xrd.ml

data = xrd.ml.load("mnist")
for images, labels in data.train.batches(256):
    ...
```

streams minibatches straight off your server, with nothing downloaded first
and nothing installed but this library. This page is how to build that
directory, keep it honest, and put it on the web.

The protocol underneath is XRootD — the one high-energy physics built for
globally distributed analysis, and the one the OSG and the WLCG move petabytes
a day over, between hundreds of sites. That is the point of serving training
data this way: wide-area reads that were designed for exactly this shape of
access, a file read in pieces from far away, by many clients at once. The
generated page says so to every visitor, and names the two planes the data
answers on.

## Build it

```console
$ xrd-datasets build /srv/datasets --jobs 4
mnist: 70000 rows, 10.4 MiB
iris: 150 rows, 5.2 KiB
...
```

`build` fetches each dataset from its registered source, converts every split
into one ROOT file — one tree per class, with creators, publisher, canonical
origin or best available dataset record, source repository, explicit mirrors,
licence terms and transformation summary recorded in the file's own `about` key —
and writes two things beside them:

- `index.json` — what each file is, who created it, its origin and byte-serving
  repository or mirror, canonical licence URL, what conversion changed, its
  size, its `adler32`, and its row counts per
  tree. This is the catalogue that
  `xrd.ml.load("name")` resolves names against.
- `MANIFEST` — one checksum line per file, for anyone verifying with their
  own tools.

A second run keeps what is already on disk and re-indexes it; `--force`
starts over, and `--only "mnist*"` (repeatable) narrows the build. A dataset
whose download fails fails that dataset alone: the rest build, the index
records what succeeded, and the exit code says something went wrong.

Two hundred and twenty-nine registrations are handled as default large, disk-backed inputs
regardless of their
publisher: 14 UCI collections, NIST's EMNIST archive, the two University of
Toronto CIFAR archives, JetNet, OmniFold Big, TinySOL, Speech Commands,
AudioMNIST, CirCor heart sounds, WikiText-103, ReefSet and BioDCASE 2025 Task 3,
BirdSet BASEAL, the UAV Maize Stress archive, Wildlife MNIST and Year Prediction
MSD, PathMNIST, TissueMNIST, Galaxy10 SDSS and SWEFil; the new STM, SEM, TEM,
MOKE and AFM condensed-matter shelf; three large Matbench crystal tasks; 100
Alex-MP-20 visual crystal tasks; 50 field-learning tasks over the eight The Well
sources below the default ceiling; plus
37 explicitly licensed Hub Parquet repositories. Their complete
declared source payload is 93.70 GB; the public build selects 227 of them and
93.36 GB after withholding the two unlicensed CIFAR archives. The 100 Alex-MP-20
registrations share the same three cache objects, so they add only 197.51 MB to
the physical source cache rather than the 19.75 GB logical sum. None is held
wholesale in memory.
`--large` selects exactly this 100 MB–2 GB set by default. `build` retains it in a hidden
sibling of the output directory —
`/srv/.datasets-sources` for the command above — so every split and a resumed
run reuses the same download without placing source archives under the web
root. Put both `/srv/datasets` and that cache on the NFS mount, or choose it
explicitly:

```console
$ xrd-datasets build /nfs/datasets --large \
    --source-cache /nfs/dataset-sources --jobs 1
```

The 100 `jarvis_dft3d_*` and `jarvis_dft2d_*` visual materials-physics tasks
also use disk-backed streaming readers, but their individual pinned inputs are
48.45 MB and 8.39 MB, so they belong to the normal public build rather than the
100 MB `--large` selection. All 50 tasks for a dimensionality share one cache
name. A complete JARVIS task build therefore downloads just those two CC BY 4.0
archives (56.84 MB total), even with concurrent jobs, while producing 100
independent ROOT files and 100 provenance-rich detail pages.

The additional 100 `alex_mp20_*` tasks use OMatG's complete 675,204-row
[Alex-MP-20](https://huggingface.co/datasets/OMatG/Alex-MP-20) release under
CC BY 4.0. Six targets retain published energy-above-hull, DFT band-gap,
DFT/MatterSim bulk-modulus, magnetic-density and supply-risk values. Thirty-two
targets transparently derive lattice, cell and aggregate composition quantities;
62 derive elemental stoichiometric fractions. Every task keeps the publisher's
train/validation/test split, padded cell and atom arrays, a normalized 8×16
elemental image, an occupancy-only three-plane image and three atomic-number
32×32 projections. All 100 share the same 155.97 MB train, 20.77 MB validation
and 20.78 MB test cache files:

```console
$ xrd-datasets build /nfs/datasets --only 'alex_mp20_*' \
    --source-cache /nfs/dataset-sources --jobs 2
```

The occupancy image is the appropriate model input for an elemental-fraction
exercise when exact atomic numbers would leak the answer. The ROOT file retains
both views so leakage checks and representation choices stay explicit.

The 100 `well_*` tasks add breadth across sixteen distinct archives in
[The Well](https://polymathic-ai.org/the_well/): acoustic scattering in three
media, active matter, stellar convection, reaction-diffusion, Helmholtz waves,
planetary shallow-water dynamics, neutron-star-merger remnants, thermal
convection, Rayleigh-Taylor instability, shear flow, supernovae,
self-gravitating turbulence, radiative mixing layers and viscoelastic flow.
Each source contributes next-state, temporal-change, gradient, Laplacian,
segmentation and spatial-mean tasks; four also contribute RMS regression.

Every converter selects the smallest complete file in the publisher's official
test directory, discovers the first time-varying scalar from The Well's
self-describing HDF5 metadata, takes a central plane from 3D fields, and
nearest-samples it to 64×64. ROOT rows retain the raw and min-max normalized
input, target image, validity masks, field name, source limits and scalar target.
The generated page discloses the selected shard and project-created contiguous
80/10/10 temporal split.

Eight physical sources total 6.22 GB and sit below the ordinary ceiling. The
other eight require the explicit production opt-in. A small live trial is only
109.05 MB:

```console
$ xrd-datasets build /nfs/datasets \
    --only 'well_turbulent_radiative_layer_2D_*' \
    --source-cache /nfs/dataset-sources --jobs 1
```

The complete shelf downloads sixteen shared cache objects totalling 86.52 GB,
not the 555.26 GB logical sum shown when every independently buildable task is
counted. Build it only on the provisioned host:

```console
$ xrd-datasets build /nfs/datasets --only 'well_*' --allow-oversize \
    --source-cache /nfs/dataset-sources --jobs 1
```

To stage only the new explicitly licensed Hub shelf, install the dataset
readers and select its stable name prefix. This fetches 12.13 GB across all
500 repositories; the source cache makes a retry or second split reuse every
finished shard:

```console
$ python -m pip install 'pyxrootdclient[datasets]'
$ xrd-datasets build /nfs/datasets --only 'hub_*' \
    --source-cache /nfs/dataset-sources --jobs 2
```

Start the default-ceiling filesystem with at least 500 GiB free for the first full pass.
That is a conservative working allowance, not a promised final site size: it
holds the roughly 41.15 GB deduplicated source cache, partially written ROOT files, temporary nested
members and the finished output together. TinySOL's padded ten-second audio chunks,
AudioMNIST's 48 kHz rows, ReefSet's 57,084 waveforms, long sensor streams and
100 independently usable copies of the Alex-MP-20 structure representation are
the main expansion risks; measured ROOT files from the first pass are the useful
basis for tightening the allocation.

The default ceiling is strict and applies to the *whole dataset*, not each
shard: `source_payload_bytes < 2_000_000_000`. JetNet's five HDF5 files
therefore count as one 436,490,240-byte source. An explicit production opt-in
removes that upper source-selection bound for every registered converter:

```console
$ xrd-datasets list --large --allow-oversize
$ xrd-datasets build /nfs/datasets --large --allow-oversize \
    --source-cache /nfs/dataset-sources --jobs 1
```

`--no-size-limit` is an alias for `--allow-oversize`, and Python callers use
`convert(name, target, allow_oversize=True)`. The opt-in adds HIGGS (2.82 GB),
REALDISP (2.67 GB), Cuff-Less Blood Pressure (3.36 GB), PPG-DaLiA (2.87 GB),
Medical Image Tamper Detection (6.40 GB), Gas Sensor Arrays in Open Sampling
Settings (8.37 GB), HEPMASS (7.89 GB), and ChIP-seq Peak Detection (37.28 GB).
They already have purpose-built readers for their gzip CSV, ZIP/nested archive,
HDF5, NumPy, DICOM, sensor-series and bedGraph layouts, including official
train/test partitions where the publisher supplies them. It also admits 50
The Well tasks backed by eight larger shared HDF5 shards. The large selection
then totals 287 registered converters and 681.39 GB of logical source files;
the redistributable production build contains 285 and reports 681.05 GB because
the two CIFAR archives still fail the licence gate.

This removes the catalogue's source-size policy, not its integrity checks:
every downloaded archive or shard must still match its published byte count.
It also admits only converters present in this release; it does not pretend an
unimplemented external archive is buildable. Splitting a download does not
change the logical source size.

Two outputs have a second, independent constraint: the pure-Python writer's
interoperable ROOT layout uses 32-bit key offsets, so one physical file must
remain below 2 GB. The build therefore publishes HEPMASS as one ROOT file per
official mass/split pair and Gas Sensor Arrays in Open Sampling Settings as
one ROOT file per chemical. `index.json`, `MANIFEST`, `verify`, and the site
record these as one logical dataset with several checksummed downloads. Select
one without spelling its URL using the publisher split or chemical name:

```python
train = xrd.ml.load("hepmass", split="train_1000")
acetone = xrd.ml.load("gas_sensor_arrays_open_sampling", split="acetone")
```

No row is sampled or discarded by this output sharding.

The external-archive audit applies the default rule to the logical dataset
rather than the largest file visible on a deposit page. It admitted [ReefSet
v1.0](https://zenodo.org/records/11060189) (1,628,719,176 bytes), the
[BioDCASE 2025 Task 3 development
set](https://zenodo.org/records/15228365) (224,706,547 bytes), and [BirdSet
BASEAL](https://zenodo.org/records/19340660) (225,207,567 bytes). BirdSet joins
three Perch-v2 embedding bundles and retains their per-record CC BY 4.0 or CC0
source terms. It also admitted the four-shard [UAV Maize Stress
dataset](https://zenodo.org/records/20332029) (1,180,895,747 bytes), retaining
both float32 source orthomosaics and float16 derived patches as separate,
spatially indexed TTrees with their semantic masks. The same pass admitted
the four-array [Wildlife MNIST](https://zenodo.org/records/7602025)
(1,477,801,037 bytes), retaining its non-mixed training and independently mixed
test factor labels. It also admitted UCI's [Year Prediction
MSD](https://archive.ics.uci.edu/dataset/203/yearpredictionmsd) (211,011,981
bytes), preserving its official producer-safe train/test boundary. It rejected,
among others, [BirdVox-70k](https://zenodo.org/records/1226427)
(6,825,555,275 bytes across its recorder shards), the [EnergyFlow Pythia
quark/gluon collection](https://zenodo.org/records/3164691) (4,269,992,114
bytes; even one complete configuration is just over 2 GB),
[GISE-51](https://zenodo.org/records/4593514) (37,744,703,654 bytes), and the
3.8 GB [BioDCASE 2026 development
set](https://zenodo.org/records/19095788). These external archives do not yet
have registered converters, so `--allow-oversize` does not select them. A
shard, fold, or convenient sample is not catalogued as though it were the
complete dataset.

The compact physics-vision pass also admitted the CC BY 4.0
[SODv2 orbital object-detection repository](https://github.com/AEL-Lab/satellite-object-detection-dataset-v2)
(14,516,386 bytes at its pinned revision) and DLR's CC BY 4.0
[All-Sky Imager Cloud Segmentation Almeria](https://zenodo.org/records/16647156)
(16,828,503 bytes across all five deposited files). SODv2 retains every one of
the 600 RGB images, 1,339 YOLO boxes, train/validation split and three distance
strata. All-Sky retains all 818 JPEG/PNG pairs, five pixel values, published
616/154/48 split, camera identity and camera coordinates. These complete
sources are below the 100 MB `--large` selection floor, so select them by name
as shown in the [vision section](root.md#detection-and-pixel-masks) rather than
expecting `--large` to include them.

A second physics-vision pass adds 20 publicly mirrorable problems totalling
1,108,385,311 source bytes: 17 CC BY 4.0 MedMNIST subsets at 28×28 or 28³,
[Galaxy10 SDSS](https://zenodo.org/records/10844811), NASA/JPL's
[Mars Surface Image v1](https://zenodo.org/records/1049137), and
[SWEFil](https://huggingface.co/datasets/antonio-reche/SWEFil). The MedMNIST
selection excludes only DermaMNIST because its dataset-specific licence is
CC BY-NC 4.0. Galaxy10 retains 21,785 compact survey images and Galaxy Zoo
labels; Mars retains 6,691 labelled rover images and the official sol-separated
splits; SWEFil retains all 554 raw/processed GONG images, 4,144 COCO annotations,
boxes and four polygon-derived binary masks. Each generated detail page names
the dataset creators and publisher, separates the canonical origin from the
byte-serving repository, links the canonical licence and citation, and states
the exact ROOT transformation.

Use one job for the large set on its first production pass: several archive
inflations and ROOT compressions in parallel compete for disk bandwidth and
temporary space rather than making one finish sooner. The Hub shelf, JetNet,
WikiText-103, MedMNIST and the visual sets need the optional NumPy, Parquet,
HDF5 and image readers
installed with `pip install 'pyxrootdclient[datasets]'`; the remaining
admitted converters use the standard library. Do not benchmark
serving while a conversion is saturating the same NFS mount; after the build,
serving does not touch the retained source cache.

The complete publicly redistributable default large-only production pass is:

```console
$ xrd-datasets build /nfs/datasets --large \
    --source-cache /nfs/dataset-sources --jobs 1
$ xrd-datasets verify /nfs/datasets
```

Successful files are kept by the next run. A conversion is written to a hidden
temporary file and atomically published only after the ROOT writer closes; a
failed conversion removes that temporary output, and an unreadable file left by
an older interrupted build is detected and rebuilt. One converter failure is
reported without aborting the remaining queue. The retained source archive
and its adjacent `.part` are resumable: a dropped connection is reopened at
the last byte committed to disk, and an exhausted retry keeps that part so the
next build does not download tens of gigabytes again.

For an unattended or apparently idle build, diagnostics make every silent
phase observable:

```console
$ PYTHONUNBUFFERED=1 xrd-datasets build /nfs/datasets \
    --allow-oversize --large --all --jobs 1 \
    --source-cache /nfs/dataset-sources --diagnostics 30 -vvv
```

`--diagnostics` takes an optional heartbeat interval in seconds (30 by
default). It reports the active dataset and split, rows written, hidden partial
ROOT size, elapsed time, and any growing source-cache `.part` downloads, all
with UTC timestamps on standard error. It also prints the process ID and
installs an on-demand all-thread Python stack dump. Send `SIGUSR1` without
stopping the build when a heartbeat is not enough:

```console
$ kill -USR1 BUILD_PID
```

The traceback goes through the existing `tee` pipeline into the build log and
usually distinguishes a network read, Parquet/HDF5 decode, image transform,
ROOT basket compression, checksum pass, or thread wait immediately. `-vvv`
adds client and wire-level logging; `PYTHONUNBUFFERED=1` keeps every line
visible through `tee`.

For the 287-converter oversized pass, begin with at least 1 TiB of working
space as a conservative production-test allocation, not a final sizing
promise. The retained logical sources total 681.39 GB, while shared-cache
deduplication reduces the physical source fetch to about 193.10 GB. Extracted members, partially
written files and converted arrays coexist until a dataset completes. Keep
`--jobs 1`, capture peak filesystem use, and size the NFS mount from that first
measured run.

CIFAR-10 and CIFAR-100 are supported by the same disk-backed path but their
publisher states no formal dataset licence and asks for citation. The public
build therefore withholds them. `--large --all` will convert them for a private
site, but it is not permission to redistribute them.

## The licence gate

Nothing in `xrd.root.datasets` is redistributed by this library — but a site
built from it *does* redistribute, so `build` converts only datasets whose
licence allows passing the files on. Recognized families include CC0, CC BY
and BY-SA, MIT, Apache, BSD/ISC/NCSA/PostgreSQL/Zlib/Boost, AFL/ECL/Etalab,
CDLA-Permissive, ODC-By/ODbL/PDDL, MPL/EPL, Artistic, the GPL family,
Unlicense/WTFPL and public domain. The two CIFAR sets carry no formal
licence, so they are left out unless you say `--all`, which is for a
directory you serve only to yourself. The gate is
`xrd.root.datasets.redistributable`, and every index entry records the
verdict alongside the licence text, so the site itself says what its terms
are.

Attribution and share-alike obligations still apply to what the gate lets
through. Every converted file carries the canonical licence, the canonical
origin or best available dataset record, and source repository; where the
publisher exposes creator or citation metadata, those
credits are carried as well. A missing creator field means “consult the linked
canonical record”, never that the repository host should be treated as the
author.

## Check it

```console
$ xrd-datasets verify /srv/datasets
1272 of 1272 files match the index and load completely
```

`verify` reopens every file, compares size, checksum and branch schema with the
index, then decodes every entry in every branch using bounded-memory batches.
It rejects unreadable or inconsistent branches, empty physical files, and a
file whose entire ML payload is only NULL/zero/non-finite or all-bits-set
sentinel values. This deliberately reads and decompresses every ROOT basket;
use `-vv` to name each tree as the long integrity pass reaches it. Catalogues
built before the schema manifest was introduced need one ordinary `build`
rerun first; completed ROOT outputs are retained and merely re-indexed. It
refuses — exit `1`, one line per problem — to bless a directory that no longer
matches what its index claims, which is the check to run after any deploy and
in CI before one.

## Serve it

```console
$ xrd-datasets site /srv/datasets --base-url https://data.example.org
wrote index.html, nginx.conf, brix.conf, xrd-datasets.service, README.md, dataset detail pages,
sitemap.xml, robots.txt in /srv/datasets
```

`--base-url` is where the directory answers to HTTP. The native endpoint is
taken to be the same host — `root://data.example.org` — which is what a single
BriX-Cache box serving both planes gives you; `--root-url` says otherwise when
`root://` lives behind its own name or port. `--nginx-port` controls the stock
nginx virtual host and defaults to 8080; its `server_name` is derived safely
from `--base-url`.

For the ScotGrid production host, regenerate the HTML whenever `index.json`
changes and install the generated named virtual host:

```console
$ xrd-datasets site /datasets/site \
    --base-url https://ai.edi.scotgrid.ac.uk \
    --root-url root://ai.edi.scotgrid.ac.uk \
    --nginx-port 80 \
    --title "ScotGrid AI open science datasets"
$ sudo install -m 0644 /datasets/site/nginx.conf \
    /etc/nginx/conf.d/ai-datasets.conf
$ sudo nginx -t
$ sudo systemctl enable --now nginx
$ sudo firewall-cmd --permanent --add-service=http
$ sudo firewall-cmd --permanent --add-service=https
$ sudo firewall-cmd --reload
$ sudo certbot --nginx --domain ai.edi.scotgrid.ac.uk --redirect
```

On an enforcing SELinux host, label a local filesystem as public read-only
content. If `/datasets` is NFS, enable nginx's narrowly scoped NFS read access
instead:

```console
$ sudo semanage fcontext -a -t httpd_sys_content_t '/datasets/site(/.*)?'
$ sudo restorecon -Rv /datasets/site
# NFS mount only:
$ sudo setsebool -P httpd_use_nfs 1
```

The stock config permits only `GET` and `HEAD`, denies dotfiles, supplies CORS
and byte-range headers for training clients, disables recompression of ROOT
files, and emits a restrictive browser security policy. Certbot adds the HTTPS
listener and HTTP-to-HTTPS redirect after the port-80 virtual host answers.

Everything lands next to the files, so *the directory is the deploy*:

- `index.html` — a responsive, searchable, faceted and sortable server-rendered
  catalogue with an end-to-end venv/PyXRootD/PyTorch classifier at the top.
  Every card links to
  the original publisher, canonical licence, transformation summary and
  `.root` download. JSON-LD describes it as a Schema.org `DataCatalog`, so the
  content is usable before JavaScript by both people and search indexers.
- `datasets/*.html`, `sitemap.xml`, `robots.txt` — one indexable detail page
  per result, a canonical URL map and crawler policy. Open Graph, description,
  canonical and structured-data metadata are emitted without external assets.
- `nginx.conf` — hostname-aware static hosting for any stock nginx: drop it in
  `conf.d/`, and range requests (which `xrd.ml` reads by) come from nginx
  itself. The generated listener uses `--nginx-port`.
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
xrd.ml.load("mnist")  # via XRD_CATALOGUE
xrd.ml.load("root://data.example.org//mnist.root")  # the native protocol
xrd.ml.load("https://data.example.org/mnist.root")  # plain HTTP ranges
```

Nothing there is downloaded — the loop reads the baskets each batch needs. For
a set read many times over, or read from further away than you would like, one
more word keeps a local copy:

```python
xrd.ml.load("mnist", cache=True)  # pulled once, checked against the index
```

The pull is verified against the size and `adler32` this site published, so a
cached file is one the catalogue vouches for rather than merely one that
arrived. See [Keeping a local copy](ml.md#keeping-a-local-copy).

See [Machine learning](ml.md) for what happens next, and
[Training playbooks](playbooks.md) for serving a directory ad hoc, with no
daemon and no login, while you decide whether to keep it.
