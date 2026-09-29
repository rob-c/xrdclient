# PyXRootD examples, ported by one line

Every script here is written the way someone using the official XRootD Python
bindings writes code: `(status, response)` pairs checked with `status.ok`,
`OpenFlags` and `DirListFlags`, callbacks, `CopyProcess` with a progress
handler. Each one then has **one line** changed:

```diff
-from XRootD import client
+from xrdclient.compat import client  # was: from XRootD import client
```

Flag and utility imports port the same way, and need no comment:

```diff
-from XRootD.client.flags import OpenFlags, MkDirFlags
+from xrdclient.compat.client.flags import OpenFlags, MkDirFlags
```

Nothing else in the scripts differs from the original PyXRootD code, and
`run_all.py` proves it: it runs each script on both clients and compares what
they print.

## Running them

Every example takes the server URL and a working directory on that server,
creates what it needs there, prints what it did, and removes it again:

```console
$ python examples/pyxrootd/dirlist.py root://localhost:1094 /tmp/pyxrootd-examples
```

To run them all against a throwaway server:

```console
$ python examples/pyxrootd/run_all.py                 # both clients, and a comparison table
$ python examples/pyxrootd/run_all.py --compat-only   # without the official bindings installed
$ python examples/pyxrootd/run_all.py -k dirlist      # just the examples whose name matches
$ python examples/pyxrootd/run_all.py -v              # also print each example's output
```

`run_all.py` needs a stock `xrootd` on `PATH`. It starts one exporting a
temporary sandbox (with `xrootd.chksum` configured, so checksum queries
answer), then runs every example twice, one example after the other in the
same working directory:

* **official** - the script with the ported import turned back into
  `from XRootD import client` (and `xrdclient.compat.client.flags` back into
  `XRootD.client.flags`), running on `libXrdCl`. Skipped, with a note, if
  `XRootD` is not importable.
* **compat** - the script exactly as it is in this directory.

Before comparing, it masks what differs between two correct runs of the same
code: the server's port, temporary paths, timestamps. It then prints a table
and exits non-zero if any compat run failed, left files behind on the
server, or printed something the official run did not:

```text
example             official  compat  same output
-------------------------------------------------
async_callbacks.py  ok        ok      yes
checksum_query.py   ok        ok      yes
...
```

`tests/test_pyxrootd_examples.py` runs it as part of the test suite (marked
`interop`, and `parity` for the comparison), and checks that every example
still has exactly one ported import line of the shape above.

## What each example shows

| Example | What it shows |
|---|---|
| `stat_and_ping.py` | `ping`, `protocol`, `stat` of a file and a directory with `StatInfoFlags`, `statvfs` |
| `dirlist.py` | `dirlist` plain, with `DirListFlags.STAT`, and `DirListFlags.RECURSIVE` |
| `namespace_ops.py` | `mkdir` with and without `MkDirFlags.MAKEPATH`, `mv`, `truncate`, `chmod` with `AccessMode`, `rm`, `rmdir` |
| `open_flags.py` | What `OpenFlags.READ`, `NEW`, `DELETE`, `UPDATE` and `MAKEPATH` each do |
| `file_io.py` | `File` in a `with` block: `open`, `write` (bytes, str, at an offset), `read(offset, size)`, `stat`, `sync`, `truncate`, `close` |
| `read_lines.py` | `readline` and its cursor, `readlines`, iterating over a `File` |
| `read_chunks.py` | `readchunks` over a megabyte, checked with a digest |
| `vector_read.py` | `vector_read` of scattered ranges, and one past the end |
| `checksum_query.py` | `query(QueryCode.CHECKSUM, ...)`, choosing the algorithm with `cks.type`, and `QueryCode.CONFIG` |
| `locate.py` | `locate` and `deeplocate`, and the fields of each `Location` |
| `xattrs.py` | `set_xattr`, `get_xattr`, `list_xattr`, `del_xattr` on a path and on an open `File` |
| `copy_process.py` | `CopyProcess` with a `CopyProgressHandler` subclass: an upload and a download, `checksummode="end2end"` |
| `simple_copy.py` | `FileSystem.copy`: upload, refusal without `force`, server to server, download |
| `async_callbacks.py` | `callback=` with `AsyncResponseHandler` and with a plain function; several requests in flight |
| `timeouts.py` | Per-call `timeout=`, process-wide `EnvPutInt("RequestTimeout", ...)`, a server that never answers |
| `glob_files.py` | `client.glob` and `client.iglob` on the server, and `raise_error` |
| `env_settings.py` | `EnvGetInt` defaults, `EnvPutInt`, and that the settings take effect |
| `url_parsing.py` | `client.URL`: protocol, user, password, host, port, path, parameters, `is_valid()` |
| `error_handling.py` | A missing file: `ok`, `error`, `fatal`, `errno` 3011, `code` 400, `shellcode` 54, `str(status)` |
| `sync_changed.py` | A real workflow: discover with `glob`, checksum each file on the server, copy only what changed |
| `install_hook.py` | `xrdclient.compat.install()`, so unmodified `from XRootD import client` code runs on this library |

`install_hook.py` is the one exception to the one-line rule: it keeps
`from XRootD import client` exactly as written and adds two lines, marked
`# compat only`, in front of it. That is for code that cannot be edited at
all - a vendored library, someone else's tool.

## Something the comparison found

`async_callbacks.py` keeps each buffer it writes asynchronously in a variable
until the write has been answered. Written the obvious way -
`f.write(f"...".encode(), callback=handler)` - the official bindings do not
keep the temporary `bytes` alive, and the server intermittently received
garbage (`b'dile num2\x00r 2\n'` for `b'file number 2\n'`). The compat layer
holds on to the buffer, so either form is safe on it; the example is written
the way that is safe on both.

`sync_changed.py` waits a second before rewriting a file, for a reason that
has nothing to do with either client: `xrootd` caches a file's checksum
against its size and modification time, and the time has one-second
resolution. A same-size rewrite within the same second is answered from the
stale cache, on both clients alike.
