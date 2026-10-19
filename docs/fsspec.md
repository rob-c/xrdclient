# fsspec

```console
$ pip install 'xrdclient[fsspec]'
```

That is the whole setup, and pandas, dask and pyarrow already bring `fsspec`
with them. This package does not depend on it: the adapter is only loaded by
`fsspec` itself. The schemes register themselves through entry
points, so nothing has to be imported by hand:

```python
import pandas as pd

df = pd.read_parquet("root://eos.example.org//store/t.parquet")
```

| Scheme | Backend |
| --- | --- |
| `root`, `roots`, `xroot` | `XRootDFileSystem` |
| `dav`, `davs`, `webdav` | `HTTPXRootDFileSystem` |

`s3` is the one this library will not claim: `s3fs` owns it, and clobbering
that scheme would break every notebook that has it installed. Register it by
hand when you want this one instead:

```python
import fsspec
from xrdclient.fsspec_impl import S3XRootDFileSystem

fsspec.register_implementation("s3", S3XRootDFileSystem, clobber=True)
```

## `root://` and fsspec-xrootd

[fsspec-xrootd](https://github.com/CoffeaTeam/fsspec-xrootd) - the adapter
over the compiled XRootD bindings - registers `root` too, through the same
`fsspec.specs` entry-point group, and fsspec's own built-in table also names
it for `root`. How fsspec decides, as of fsspec 2026.9:

- When `fsspec` is imported it walks every installed `fsspec.specs` entry
  point and lets each one replace the built-in table's entry for its name.
  So with only this package installed, `root://` is this package, and the
  built-in mention of fsspec-xrootd never matters.
- With **both** installed, the first entry point seen for `root` wins and
  the other is skipped. "First" is whatever order `importlib.metadata`
  lists distributions in - `sys.path` order, then the order the directory
  happens to list - so in a single environment it is not something to rely
  on. `roots`, `xroot`, `dav`, `davs` and `webdav` are only ever this
  package's: fsspec-xrootd registers `root` alone.

Check which one you have, and choose explicitly where it matters:

```python
import fsspec
from xrdclient.fsspec_impl import XRootDFileSystem

fsspec.get_filesystem_class("root")       # who won, in this environment

# This package for every root:// URL from here on, whoever won at import:
fsspec.register_implementation("root", XRootDFileSystem, clobber=True)

# Or for one filesystem, without touching the registry:
fs = XRootDFileSystem("root://eos.example.org")
fsspec.open("xroot://eos.example.org//store/f.root")   # always this package
```

`register_implementation` with a class puts it in fsspec's registry proper,
which is consulted before the table entry points write to, so it holds for
the rest of the process; run it before the first `fsspec.open`. The same call
with `fsspec_xrootd.XRootDFileSystem` picks the other one.

To take every protocol this package declares in one call - what a program
that must not depend on install order does, in every process it starts (a
dask client and each of its workers, say):

```python
from xrdclient.fsspec_impl import register_fsspec

register_fsspec()   # ['dav', 'davs', 'root', 'roots', 'webdav', 'xroot']
```

It registers the classes named by this package's own `fsspec.specs` entry
points with `clobber=True`, so the list never drifts from the packaging, and
leaves `s3` alone. Calling it again changes nothing.

## Caching

`fs.open(path, "rb")` hands back a real `io.BufferedReader` over the
protocol's own read path - pipelined, zero-copy reads and sequential
readahead - and that is what `cache_type` left unset gets. Name an fsspec
cache, and reads go through it instead, built exactly as fsspec's own files
build theirs:

```python
fs.open("/store/f.root", "rb", cache_type="blockcache",
        block_size=1 << 20, cache_options={"maxblocks": 64})
```

Every key of `fsspec.caching.caches` is accepted - `"none"`, `"bytes"`,
`"readahead"`, `"blockcache"`, `"first"`, `"all"`, `"parts"`,
`"background"`, `"mmap"`. `block_size` is the cache's block size
(`fs.blocksize`, 4 MiB, if not given) and `cache_options` its keyword
arguments; the cache is on the file as `fh.cache`, as on fsspec's own files.
An unknown name is refused with a `ValueError` listing the real ones. A
cache is a way of reading, so a file opened for writing ignores
`cache_type`, as fsspec's do.

## Direct use

```python
import fsspec

with fsspec.open("root://eos.example.org//store/f.root", "rb") as fh:
    header = fh.read(1024)

fs = fsspec.filesystem("root", endpoint="root://eos.example.org")
fs.ls("/store", detail=False)
fs.info("/store/f.root")
fs.cat_file("/store/f.root", start=0, end=1024)
fs.glob("/store/**/*.root")
fs.put("/tmp/f.root", "/store/f.root")
fs.get("/store/f.root", "/tmp/f.root")
```

`cat_file` and `cat_ranges` read their bounds as a Python slice does:
`None` is the matching end of the file, a negative bound counts back from
the end (`fs.cat_file(path, start=-1024)` is the last kilobyte), and a range
past either end is clamped. `ls` of a file is a one-element listing of that
file, as fsspec expects.

## More than one server

An instance names paths on its own endpoint bare (`/store/f.root`), as
fsspec-xrootd does, and `fs.unstrip_protocol(name)` turns one back into a
full URL. A full URL to any *other* server is honoured, and the names `ls`,
`find`, `glob` and `info` return for it keep that server in them, so they
can be handed straight back to the same instance:

```python
fs = fsspec.filesystem("root", endpoint="root://eos.example.org")
fs.glob("root://other.example.org//store/*.root")
# ['root://other.example.org:1094//store/a.root', ...]
```

`mv` within one server is a rename. Between two it is a copy whose checksum
is verified before the source is deleted, as `xrdclient.move` does; a
directory needs `recursive=True`.

## Connections are shared

One instance is one endpoint, and `fsspec` caches instances by their
constructor arguments. Repeated `fsspec.open` calls against the same server
therefore share one object, and one connection. Reading a partitioned dataset
of a thousand files costs a single login.

`fs.close()` releases them. One that is never closed releases them when it is
garbage-collected - handed to the connection pool's queue, never closed from
inside the collector, where a close could wait on a lock the collecting
thread holds.

## Passing a configuration

```python
fs = fsspec.filesystem(
    "root",
    endpoint="root://eos.example.org",
    config=xrdclient.Config(token=my_token, request_timeout=60.0),
)
```

`storage_options` works the same way through pandas, dask and pyarrow:

```python
pd.read_parquet(
    "root://eos.example.org//store/t.parquet",
    storage_options={"config": xrdclient.Config(token=my_token)},
)
```

## Caveat

`fsspec` normalises paths, which means the doubled slash that XRootD URLs use
for an absolute path (`root://host//store/f.root`) is handled for you at the
`fsspec` layer but still matters when you construct URLs by hand elsewhere.
See [Namespaces](filesystem.md).
