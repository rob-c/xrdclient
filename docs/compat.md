# Compatibility layer

`xrdclient.compat.client` is the official bindings' `XRootD.client`, running
on this library instead of `libXrdCl`. Porting a script is one line:

```python
from xrdclient.compat import client        # was: from XRootD import client
```

Everything else stays as it was: the classes, the method names, the
positional and keyword arguments and their defaults, the `(XRootDStatus,
response)` pairs, the response objects' attribute names, the flags' names and
numbers, `timeout=` and `callback=` on every call, and the progress-handler
protocol of `CopyProcess`.

For code that cannot be edited at all:

```python
import xrdclient.compat
xrdclient.compat.install()   # before anything imports XRootD

from XRootD import client    # now this package
```

`install()` refuses if the real bindings are already imported, rather than
swapping one for the other under code holding references to both.

## What is covered

| `XRootD.client` | Covered |
| --- | --- |
| `FileSystem` | every method: `stat`, `statvfs`, `dirlist` (`STAT`, `RECURSIVE`), `mkdir`, `rmdir`, `rm`, `mv`, `truncate`, `chmod`, `locate`, `deeplocate`, `ping`, `protocol`, `query`, `prepare`, `sendinfo`, `set_xattr`/`get_xattr`/`del_xattr`/`list_xattr`, `copy`, `cat`, `get_property`/`set_property`, `url` |
| `File` | every method: `open`, `close`, `is_open`, `read`, `readline`, `readlines`, `readchunks`, iteration, `vector_read`, `write`, `sync`, `truncate`, `stat`, `visa`, `clone`, the xattr methods, `get_property`/`set_property`, `with` |
| `CopyProcess` | `add_job` with all of its keywords, `parallel`, `prepare`, `run(handler)` |
| `URL` | every attribute, `is_valid`, `clear`, and XrdCl's parsing rules |
| `responses` | `XRootDStatus`, `StatInfo`, `StatInfoVFS`, `DirectoryList`, `ListEntry`, `LocationInfo`, `Location`, `ProtocolInfo`, `VectorReadInfo`, `ChunkInfo`, `HostList`, `HostInfo` |
| `flags` | every namespace, value for value, with `reverse_mapping` |
| `utils` | `CopyProgressHandler`, `AsyncResponseHandler` |
| module functions | `EnvPutInt`, `EnvGetInt`, `EnvDelInt` and the `String` trio, `EnvGetDefault`, `SetLogLevel`, `SetLogMask`, `glob`, `iglob`, `setXAttrAdler32` |

## How it is checked

`tests/test_pyxrootd_compat.py` runs every covered call twice against one
real `xrootd` - once through the official bindings, once through this package
- and requires the answers to be equal, field by field: the status's
`status`, `code`, `errno`, `shellcode`, `ok`, `error` and `fatal`, and every
attribute of every response object. Only values that differ between any two
calls anyway (file ids and timestamps) are left out of the comparison.
Without the bindings installed those tests skip, and the rest of the file
pins the same behaviour against the in-process `FakeServer`.

Some of what that turns up is the bindings' own behaviour, kept because code
depends on it:

- `readline()` keeps a cursor of its own that `read()` does not move;
  `readline(offset)` moves the cursor to `offset` and leaves it there.
- A `File` whose `open` failed answers every later `open` and `close` with
  that same failure; make a new `File` to try again.
- `close()` on a file that was never opened succeeds, with `code` 4.
- I/O on a file that is not open raises `ValueError` rather than returning a
  status, and a line that is not UTF-8 raises `UnicodeDecodeError`.
- `ProtocolInfo.version` and `.hostinfo` have their bytes reversed
  (`0x520`, protocol 5.2.0, reads `0x20050000`). The native
  `FileSystem.protocol()` has them the right way round.
- The per-attribute statuses inside an xattr result, and `cat()`'s status,
  are plain `dict`s; `XRootDStatus` answers to `status["ok"]` as well, so
  either spelling works everywhere.

## Where it differs

On purpose, and each one visible in a status rather than silently:

- **`timeout=`** bounds how long the call waits. When it runs out the call
  returns `errOperationExpired` (206), as XrdCl does; the request is not
  recalled, which XrdCl cannot do either.
- **`callback=`** runs on a worker thread and gets `(status, response,
  hostlist)`. The host list names the server that answered, not every hop.
- **`dirlist` with `LOCATE` or `MERGE`** lists the directory on the server
  the namespace sends it to; for one server that is the same answer. `ZIP`
  returns `errNotImplemented`.
- **`File.fcntl` and `openusingtemplate`** (`DUP`, `SAMEFS`) return
  `errNotSupported`: this client sends neither request.
- **`CopyProcess`** ignores `sourcelimit`, `coerce`, `dynamicsource`,
  `inittimeout`, `cptimeout`, `xrate` and `xrateThreshold`: it reads from one
  source and applies no rate limit.
- **`readlines(offset)`** with a non-zero offset returns the lines from there;
  the bindings hang.
- **`EnvPutInt`** changes the settings of objects created afterwards, mapped
  to the native [configuration](config.md) - `RequestTimeout`,
  `ConnectionWindow`, `ConnectionRetry`, `StreamTimeout`, `RedirectLimit`,
  `CPChunkSize`, `CPParallelChunks`, `SubStreamsPerChannel`. A setting nobody
  put keeps this library's default rather than XrdCl's.
- **`SetLogLevel`** sets the `xrdclient` logger's level; `SetLogMask` is
  accepted and does nothing, since topics are logger names here.

## Mixing in the native API

Every compat `FileSystem` and `File` keeps its native object as `.native`:

```python
fs = client.FileSystem("root://host")
status, info = fs.stat("/store/f.root")          # the bindings' shape
for entry in fs.native.walk("/store/run7"):      # something they never had
    ...
```

so a port can move over a call at a time. See [Coming from
pyxrootd](migrating.md) for the native equivalents, and the few names that
mean different things in the two APIs.
