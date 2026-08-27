# Dataset selection manifests

`uci.json` fixes the UCI catalogue snapshot used to grow the ROOT conversion
shelf. It contains three collections:

- `physics_100`: the complete physics and measurement-science selection;
- `large_10`: the ten largest members of that selection, all handled by
  disk-backed streaming converters;
- `smallest_400`: the 400 smallest directly hosted UCI archives that contain
  data, excluding empty ZIPs and archives that contain only a link or README.

The collections overlap. Their union is 455 UCI records: 177 already have a
schema-aware converter in `xrd.root.datasets`, while 278 require new adapters.
An entry's `implemented` field records that distinction; being selected does
not pretend that an arbitrary source archive is already safe to build.

The 400-smallest collection is 234,227,168 compressed source bytes
(223.4 MiB). The ten large sources are 73,708,449,615 bytes (68.65 GiB), the
physics collection is 78,338,087,513 bytes (72.96 GiB), and the deduplicated
union is 78,542,700,488 bytes (73.15 GiB).

The build catalogue applies a separate publication rule: the complete unique
source payload of one dataset must be below 2,000,000,000 bytes. All 13 UCI
archives in that band within these three selections have streaming converters.
Entries above the ceiling remain in this research snapshot for provenance and
future private work, but are not selected by `xrd-datasets --large` and cannot be fetched by
the public conversion path. UCI Year Prediction MSD is an additional admitted
large regression archive outside those three historical selections, bringing
the UCI total in the 100 MB–2 GB build band to 14.

The snapshot is regenerated from UCI's API and download service, not edited by
hand:

```bash
python tools/uci_inventory.py /tmp/uci-inventory.json
python tools/uci_select.py /tmp/uci-inventory.json /tmp/uci-smallest-400.json
python tools/uci_manifest.py \
  /tmp/uci-inventory.json /tmp/uci-smallest-400.json catalogues/uci.json
python tools/uci_attribution.py \
  src/xrd/root/_uci_attribution.py \
  src/xrd/root/datasets.py src/xrd/root/_uci_tables.py \
  src/xrd/root/_uci_large.py catalogues/uci.json \
  --manifest catalogues/uci.json
```

`uci_inventory.py` records catalogue metadata and exact compressed archive
sizes. `uci_select.py` opens the smallest archives and excludes placeholders
before ranking them. `uci_manifest.py` combines that result with the physics
selection and compares it with the implemented registry.

## Provenance and mirror attribution

The runtime model deliberately keeps five roles separate: credited `creators`,
the dataset `publisher`, canonical `origin`, byte-serving `repository`, and
named `mirrors`. A hosting service is never inferred to be an author. When a
repository record does not publish creator metadata, the generated website
says to consult the linked dataset record instead of inventing a credit. A URL
is labelled “canonical origin” only when the registry explicitly supplies one;
otherwise it is labelled “dataset record” and its hosting repository is still
named separately.

UCI is the concrete audit case. `uci_attribution.py` snapshots the creators and
dataset DOI returned by UCI's API for all registered UCI converters and every
record in `uci.json`: 498 distinct UCI records in the current generated
snapshot, covering all 220 runtime UCI converters and all 455 manifest entries.
An empty creator list is retained when UCI publishes none rather than filling
it with the repository name. The DOI is the canonical origin; the UCI landing
page remains the registered source; and “UCI Machine Learning Repository” is shown
as the repository, not the author. AudioMNIST demonstrates the other direction:
the authors' GitHub project remains the origin while a fixed Hugging Face copy
is explicitly named as the mirror serving the registered archives. These
fields travel into `index.json`, HTML and JSON-LD pages, and the ROOT `about`
key alongside the licence and exact transformation.

## Explicitly licensed Hub Parquet shelf

`hub-open.json` fixes another 499 public dataset repositories discovered
through the Hugging Face Hub API. Together they produce 805 bounded output
splits from 1,235 Parquet shards and declare 12,132,322,064 source bytes
(11.30 GiB).
Every selected logical dataset is strictly below the normal two-gigabyte
ceiling; 37 are at least 100 MB and therefore join `xrd-datasets --large`.
The counted unit is a unique publisher repository ID. The inventory does not
claim semantic deduplication, so an independently republished or transformed
variant of a familiar dataset remains a separately traceable source.

This is a hard metadata gate, not an inference from popularity or public
visibility. A repository is admitted only when its dataset card carries a
recognized licence that permits redistribution, it is public and ungated,
the dataset-viewer conversion is complete and has exactly one configuration,
and every source feature is scalar. Nested image, audio, sequence and struct
features are refused until a purpose-built converter exists. So are missing,
`unknown`, `other`, non-commercial and no-derivatives terms. The generator
also requires a numeric or `ClassLabel` field that can be exposed explicitly
as a teaching target; it never silently drops a source column.

The selected snapshot contains 401 permissively licensed repositories. Its
12 represented licence families are Apache-2.0, BSD-2-Clause, BSD-3-Clause,
CC BY 3.0, CC BY 4.0, CC BY-SA 3.0, CC BY-SA 4.0, CC0, MIT, MPL-2.0,
ODbL-1.0 and the Unlicense. The discovery gate additionally recognizes the
permissive AFL-3.0, Artistic-2.0, Boost-1.0, CC BY 2.0/2.5,
CDLA-Permissive-1.0/2.0, ECL-2.0, Etalab Open Licence 2.0, ISC, NCSA,
ODC-By-1.0, PDDL-1.0, PostgreSQL, WTFPL and Zlib identifiers when a future
candidate meets the same structural checks. Canonical terms come from the
publisher named in each declaration, the licence steward, or SPDX; the Hub's
bare `public` label is deliberately not treated as a licence.

Regenerate both the importable declarations and the reviewable manifest with:

```bash
python tools/hub_catalogue.py \
  src/xrd/root/_hub_tables.py catalogues/hub-open.json
```

Runtime builds do not call the inventory APIs. They use the recorded source
URLs, discovery-time byte estimates, schemas and split map in `_hub_tables.py`.
The Hub's `refs/convert/parquet` files are moving derived exports: their URLs
and valid schemas can stay the same when the service regenerates their Parquet
encoding and changes the compressed byte count. Runtime conversion therefore
uses the recorded total for capacity planning, then validates the current
Parquet footer and every required source column instead of rejecting a valid
regeneration solely because its byte count moved. Arrow reads one record batch
at a time. Text is preserved as UTF-8 bytes plus an explicit length,
`ClassLabel` metadata becomes an integer, missing floats become NaN, and each
official split becomes its own TTree. The origin, canonical licence URL and
exact transformation summary travel in the ROOT file's `about` key and in the
generated website.

## The Well visual physics shelf

The curated declarations in `src/xrd/root/_the_well.py` pin one complete file
from the official test directory of each of sixteen CC BY 4.0 repositories in
[The Well](https://polymathic-ai.org/the_well/). They span sixteen described
physical regimes and generate exactly 100 independently buildable 64×64 field
tasks. Repository revisions, file paths and byte counts were checked against
the Hugging Face repository API; domain experts, authors, associated papers
and canonical origins come from The Well's per-dataset documentation.

The logical source total is 555,258,740,736 bytes because every task declares
its complete input. Shared cache names reduce this to sixteen physical HDF5
objects and 86,520,102,912 bytes. Eight objects are below the normal 2 GB
ceiling; the remaining eight are deliberately registered behind
`--allow-oversize`. Runtime conversion does not call discovery APIs and rejects
an HDF5 file with no time-varying physical field.
