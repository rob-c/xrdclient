# Coming from pyxrootd

The official bindings hand you a `(status, result)` pair from every call and a
`.buffer` you have to remember to slice. This library raises and returns
`bytes`. The mapping is mechanical; the only real change is deleting the
status checks.

## The shape of a call

```python
# XRootD.client
from XRootD import client
fs = client.FileSystem("root://host")
status, info = fs.stat("/store/f.root")
if not status.ok:
    raise RuntimeError(status.message)
size = info.size
```

```python
# here
import xrdclient
fs = xrdclient.FileSystem("root://host")
size = fs.stat("/store/f.root").st_size
```

There is no compatibility shim, deliberately. A `(status, result)` tuple you
can forget to check is a bug that reaches production silently; every failure
here is an exception, and the one you catch is the `OSError` subclass you
already know - see [Errors](errors.md).

## Filesystem calls

| `XRootD.client.FileSystem` | here |
| --- | --- |
| `stat(path)` → `(status, StatInfo)` | `fs.stat(path)` → `os.stat_result`-alike |
| `statvfs(path)` | `fs.statvfs(path)` |
| `dirlist(path, DirListFlags.STAT)` | `fs.scandir(path)`, `fs.listdir(path)`, `fs.iterdir(path)` |
| `mkdir(path, MkDirFlags.MAKEPATH)` | `fs.makedirs(path, exist_ok=True)` |
| `rmdir(path)` | `fs.rmdir(path)` |
| `rm(path)` | `fs.remove(path)` |
| `mv(a, b)` | `fs.rename(a, b)` |
| `truncate(path, size)` | `fs.truncate(path, size)` |
| `chmod(path, mode)` | `fs.chmod(path, mode)` |
| `query(QueryCode.CHECKSUM, path)` | `fs.checksum(path)` → `ChecksumInfo` |
| `query(QueryCode.CONFIG, name)` | `fs.query_config(name)` |
| `locate(path, OpenFlags.REFRESH)` | `fs.locate(path)`, `fs.deep_locate(path)` |
| `prepare([...])` | `fs.prepare([...])` |
| `query(QueryCode.PREPARE, ...)` | `fs.query_prepare(handle, paths)` → `list[PrepareStatus]` |
| `statx(...)`, read for the offline bit | `fs.archive_info(paths)` → `list[PrepareStatus]` |
| `ping()` | `fs.ping()` |
| `protocol()` | `fs.protocol()` |
| (no equivalent) | `fs.walk`, `fs.glob`, `fs.exists`, `fs.isdir`, `fs.isfile`, `fs.getsize`, `fs.touch`, `fs.rmtree`, `fs.read_bytes`, `fs.write_bytes`, `fs.read_text`, `fs.write_text`, `fs.utime`, `fs.chown` |

`stat` returns something that quacks like `os.stat_result` - `st_size`,
`st_mtime`, `st_mode` - so code written against `os.stat` transfers unchanged.
`os.lstat` and `os.path.islink` transfer too, as `fs.lstat` and
`fs.is_symlink`, and so do `os.utime` and `os.chown`; `pyxrootd` has none of
them.

## Files

| `XRootD.client.File` | here |
| --- | --- |
| `open(url, OpenFlags.READ)` | `xrdclient.open(url, "rb")` or `xrdclient.File(url)` |
| `read(offset, size)` → `(status, buf)` | `fh.read(size)` / `file.read(size, offset)` → `bytes` |
| `readline()`, `readlines()`, iteration | the same, from `io` - it is a real file object |
| `write(data, offset)` | `fh.write(data)` / `file.write(data, offset)` |
| `vector_read(chunks)` | `file.readv([(off, len), ...])` |
| `pgread` / `pgwrite` | `file.pgread(size, offset)` / `file.pgwrite(data, offset)` |
| - (no equivalent) | `file.clone(source, ranges)` - `kXR_clone`, copied inside the server |
| `truncate(size)` | `fh.truncate(size)` |
| `sync()` | `file.sync()` |
| `stat(force)` | `file.stat()` |
| `close()` | `close()`, or just leave the `with` block |

`xrdclient.open` returns a genuine buffered file object, so `read`, `readline`,
`seek`, `tell`, iteration, `io.TextIOWrapper` and everything else in `io`
already work. The `xrdclient.File` underneath it is reachable as `fh.raw.file` when
you want `readv` or `pgread`.

## Copying

```python
# XRootD.client
process = client.CopyProcess()
process.add_job(source, target)
process.prepare()
process.run()
```

```python
# here
xrdclient.copy(source, target)                    # returns a CopyResult
xrdclient.copy_tree(source_dir, target_dir)       # recursive
xrdclient.third_party(source, target)             # server-to-server
```

Progress is a callback, not a handler class:

```python
xrdclient.copy(src, dst, progress=lambda done, total: print(f"{done}/{total}"))
```

## Configuration

Environment variables are read the same way - `XRD_REQUESTTIMEOUT`,
`XRD_CPCHUNKSIZE`, `X509_USER_PROXY`, `BEARER_TOKEN_FILE` and the rest - so an
existing site environment keeps working. What changes is that they are also
settable in Python, on an immutable object, rather than through
`client.EnvSetInt`:

```python
cfg = xrdclient.Config(request_timeout=60.0, chunk_size=8 << 20)
fs = xrdclient.FileSystem("root://host", config=cfg)
```

See [Configuration](config.md).

## Flags

`xrdclient.OpenFlags`, `xrdclient.MkDirFlags`, `xrdclient.DirListFlags`, `xrdclient.Access`,
`xrdclient.QueryCode`, `xrdclient.StatInfoFlags`, `xrdclient.LocateFlags` and `xrdclient.PrepareFlags`
exist with the same members, for the cases where you want the raw protocol.
Most code should not need them: mode
strings cover opening, `makedirs(exist_ok=True)` covers `MAKEPATH`, and
`scandir` always asks for stat information, and
`scandir(algorithm=...)` covers `kXR_dcksm`.

Where a flag is genuinely the point, it can be said in words instead of bits -
`fs.prepare(paths, evict=True)`, `fs.query("checksum", path)`,
`fh.open("new makepath")`, `fs.chmod(path, "rw-r-----")`. See
[Easy mode](easy.md).

## What you gain

Things the bindings do not offer at all:

- `pathlib`: `xrdclient.Path("root://host//store/f.root").read_bytes()`
- `os`-style traversal: `walk`, `glob`, `scandir`
- `asyncio`: the whole surface mirrored under `xrdclient.aio`
- `fsspec`: `pd.read_parquet("root://host//store/t.parquet")`
- WebDAV and HTTP behind the same three entry points
- servers to test against: `xrdclient.testing`
- no compiled dependency, so `pip install` works in any wheelhouse

## What you give up

Nothing at the protocol level, and one thing at the API level: the
`(status, result)` pair. If you have a large codebase built around it, the
translation is `status.ok` checks becoming `try`/`except` - usually a net
deletion of lines, since most call sites either checked and re-raised, or did
not check at all.
