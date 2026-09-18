# fsspec

```console
$ pip install xrdclient[fsspec]
```

That is the whole setup. The schemes register themselves through entry
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
