# fsspec

```console
$ pip install xrdclient fsspec
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
