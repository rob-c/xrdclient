# Compatibility reference

Every name `xrdclient.compat.client` exports, with its signature and a note
on anything that behaves differently from `XRootD.client`. Where a row says
nothing, the call is the bindings' call: same arguments, same defaults, same
response, and - for the calls the [parity suite](compat.md#how-it-is-checked)
covers - the same fields and numbers, checked against a live server.

Signatures are written as in the bindings. Three conventions apply to all of
them and are not repeated per row:

- **`timeout`** is whole seconds, `0` meaning no limit. Like the bindings, it
  must be an `int` from 0 to 65535: a `float` or a `bool` raises `TypeError`,
  a larger number `OverflowError`. When it runs out the call returns
  `errOperationExpired` (206); the request is not recalled. See
  [timeouts](compat-cookbook.md#timeouts).
- **`callback`** makes the call asynchronous: it returns a status at once,
  and later calls `callback(status, response, hostlist)` on a worker thread.
  A bad argument that would have raised in the caller's thread arrives as an
  `errInvalidArgs` status instead, since the caller has gone. The host list
  names the server that answered, not every hop on the way.
- **Integer arguments** - offsets, sizes, flags, modes - are checked as the
  bindings' C parser checks them: a non-`int` is `TypeError`, a value that
  does not fit the C type (64 bits for offsets, 32 for sizes, 16 for flags
  and modes) is `OverflowError`, raised before anything is sent.

## FileSystem

`client.FileSystem(url)` - one server. Connects on first use. A URL that does
not parse is accepted, and every operation on it then returns
`errNotSupported`, as in XrdCl. The native `xrdclient.FileSystem` underneath
is `.native`. There is no `close()`, as in the bindings; the connection is
handed back to the pool when the object is collected.

| Method | Returns | Notes |
| --- | --- | --- |
| `url` | `URL` | a property |
| `stat(path, timeout=0, callback=None)` | `(status, StatInfo)` | |
| `statvfs(path, timeout=0, callback=None)` | `(status, StatInfoVFS)` | |
| `dirlist(path, flags=0, timeout=0, callback=None)` | `(status, DirectoryList)` | `STAT` fills `statinfo`; `RECURSIVE` names entries by their path below `path`, a level at a time, always with stat. `LOCATE` and `MERGE` list the directory on the server the namespace sends the request to - the same answer for one server. `ZIP` returns `errNotImplemented` (15). |
| `mkdir(path, flags=0, mode=0, timeout=0, callback=None)` | `(status, None)` | `mode=0` creates `rwxr-x---`, as the bindings do |
| `rmdir(path, timeout=0, callback=None)` | `(status, None)` | |
| `rm(path, timeout=0, callback=None)` | `(status, None)` | |
| `mv(source, dest, timeout=0, callback=None)` | `(status, None)` | |
| `truncate(path, size, timeout=0, callback=None)` | `(status, None)` | |
| `chmod(path, mode, timeout=0, callback=None)` | `(status, None)` | `mode` is `AccessMode` bits |
| `locate(path, flags, timeout=0, callback=None)` | `(status, LocationInfo)` | `flags` are `OpenFlags`; `REFRESH`, `NOWAIT` and the `MAKEPATH` bit are passed on, the rest ignored |
| `deeplocate(path, flags, timeout=0, callback=None)` | `(status, LocationInfo)` | managers followed to their servers; `flags` accepted and unused |
| `ping(timeout=0, callback=None)` | `(status, None)` | |
| `protocol(timeout=0, callback=None)` | `(status, ProtocolInfo)` | numbers byte-swapped, as the bindings report them - see `ProtocolInfo` |
| `query(querycode, arg, timeout=0, callback=None)` | `(status, bytes)` | the server's raw answer; `QueryCode.CHECKSUM` gives e.g. `b"adler32 63c80793\x00"` |
| `prepare(files, flags, priority=0, timeout=0, callback=None)` | `(status, bytes)` | the answer is the request handle |
| `sendinfo(info, timeout=0, callback=None)` | `(status, bytes)` | more than 1024 characters is `errInvalidArgs`; `root://` only |
| `set_xattr(path, attrs, timeout=0, callback=None)` | `(status, [(name, status)])` | `attrs` is `[(name, value)]`; per-name statuses are `dict`s, as in the bindings |
| `get_xattr(path, attrs, timeout=0, callback=None)` | `(status, [(name, value, status)])` | a missing name has value `""` and `errno` 3027 |
| `del_xattr(path, attrs, timeout=0, callback=None)` | `(status, [(name, status)])` | |
| `list_xattr(path, timeout=0, callback=None)` | `(status, [(name, value, status)])` | |
| `copy(source, target, force=False)` | `(status, None)` | both full URLs; no checksum comparison |
| `cat(path)` | `status` | writes the file to standard output; the status is a plain `XRootDStatus` that also answers `status["ok"]` |
| `get_property(name)` | `str` or `None` | only `FollowRedirects` exists |
| `set_property(name, value)` | `bool` | `False` for a property that does not exist; stored, but redirects are always followed |

## File

`client.File()` - not open until `open` succeeds. The native `xrdclient.File`
is `.native`, `None` while the file is not open. I/O on a file that is not
open raises `ValueError("I/O operation on closed file")`, as in the bindings.

| Method | Returns | Notes |
| --- | --- | --- |
| `open(url, flags=0, mode=0, timeout=0, callback=None)` | `(status, None)` | `flags=0` means `READ`. `NEW` or `DELETE` also create missing parent directories, as xrootd does for the bindings. Opening an open file is `errInvalidOp` (3). After a failed open, every later `open` and `close` on the same object returns that same failure. |
| `openusingtemplate(src_file, url, flags=0, mode=0, timeout=0, callback=None)` | `(status, None)` | always `errNotSupported` (13) |
| `close(timeout=0, callback=None)` | `(status, None)` | closing a file that is not open succeeds with `code` 4 |
| `is_open()` | `bool` | a method, as in the bindings |
| `read(offset=0, size=0, timeout=0, callback=None)` | `(status, bytes)` | offset first; `size=0` reads to the end |
| `readline(offset=0, size=0, chunksize=0)` | `str` | `""` at the end. Keeps its own cursor, which `read` does not move; `readline(offset)` sets the cursor to `offset` and leaves it there. `size` caps the line. A line that is not UTF-8 raises `UnicodeDecodeError`. |
| `readlines(offset=0, size=0, chunksize=0)` | `list[str]` | from `offset`, or the cursor. A non-zero `offset` works here; the bindings hang on it. |
| `readchunks(offset=0, chunksize=2097152)` | iterator of `bytes` | does not move the `readline` cursor |
| `for line in f`, `next(f)`, `f.next()` | `str` | the `readline` cursor |
| `vector_read(chunks, timeout=0, callback=None)` | `(status, VectorReadInfo)` | `chunks` is `[(offset, length)]`, one request |
| `write(buffer, offset=0, size=0, timeout=0, callback=None)` | `(status, None)` | `buffer` is `bytes` or `str` (UTF-8); `size` writes only the first `size` bytes |
| `sync(timeout=0, callback=None)` | `(status, None)` | |
| `truncate(size, timeout=0, callback=None)` | `(status, None)` | |
| `stat(force=False, timeout=0, callback=None)` | `(status, StatInfo)` | `force=True` asks the server rather than the cached answer |
| `visa(timeout=0, callback=None)` | `(status, bytes)` | |
| `fcntl(arg, timeout=0, callback=None)` | `(status, None)` | always `errNotSupported` (13) |
| `clone(locs, timeout=0, callback=None)` | `(status, None)` | `locs` is a list of dicts with `src_file` (an open `File`), `src_offset`, `src_length`, `dest_offset`; `kXR_clone`, done inside the server. Needs both files on the same server. |
| `set_xattr(attrs, timeout=0, callback=None)` | `(status, [(name, status)])` | as on `FileSystem` |
| `get_xattr(attrs, timeout=0, callback=None)` | `(status, [(name, value, status)])` | |
| `del_xattr(attrs, timeout=0, callback=None)` | `(status, [(name, status)])` | |
| `list_xattr(timeout=0, callback=None)` | `(status, [(name, value, status)])` | |
| `get_property(name)` | `str` or `None` | `ReadRecovery`, `WriteRecovery`, `FollowRedirects`; and, once open, `DataServer` (`host:port`) and `LastURL` |
| `set_property(name, value)` | `bool` | the three settable properties are stored; they do not change behaviour |
| `with client.File() as f:` | | closes on exit if open |

Files opened on the same `root://` server share one connection, as XrdCl's
do; a file redirected to a data server gets a connection of its own.

## CopyProcess

`client.CopyProcess()` - queue jobs, `prepare()`, `run()`.

| Method | Returns | Notes |
| --- | --- | --- |
| `add_job(source, target, ...)` | `None` | see the keywords below. `target` is the file to write, as in XrdCl: a local `/tmp/d/` is the file `/tmp/d`, a directory is refused (`402`, `kXR_isDirectory` or `kXR_ItExists`), and a local target's parent directories are made whether or not `mkdir` is set. Naming the file after the source is `xrdcp`'s doing, not `CopyProcess`'s. |
| `parallel(n)` | `None` | run up to `n` jobs at once, on threads |
| `prepare()` | `status` | checks every URL and `thirdparty` value; `errInvalidArgs` (9) on the first bad one |
| `run(handler=None)` | `(status, [dict])` | one dict per job: `status` always, `size` once the bytes are across (even if a checksum then fails the job), `sourceCheckSum` and `targetCheckSum` (`"adler32:8eaaaa54"`, leading zeros dropped as XrdCl drops them) for each checksum taken. A local end's failure is `errLocalError` (402) with the protocol's number in `errno` - `3018` for a target that exists, `3011` for a missing local source. The overall status is the first failed job's, or success. |

### `add_job` keywords

| Keyword | Default | Here |
| --- | --- | --- |
| `sourcelimit` | `1` | accepted, no effect - one source is read |
| `force` | `False` | honoured: overwrite the target |
| `posc` | `False` | honoured for third-party copies (persist-on-successful-close) |
| `coerce` | `False` | accepted, no effect |
| `mkdir` | `False` | honoured: create the target's parent directories |
| `thirdparty` | `"none"` | honoured: `"none"`, `"first"` (try server-to-server, fall back to streaming through this process), `"only"` |
| `checksummode` | `"none"` | honoured, as XrdCl reads it: `"end2end"` and `"source"` ask the source for its checksum, `"end2end"` and `"target"` the target (a local one is digested here), and the two are compared only when both exist. Any other value - `"end"` included - takes no checksum. An end that cannot checksum fails the job with its error |
| `checksumtype` | `""` | honoured: the algorithm, e.g. `"adler32"`; empty takes the default |
| `checksumpreset` | `""` | honoured: stands in for the source's checksum in any mode, so it is compared when the target's is taken (`"end2end"`, `"target"`); a mismatch fails the job with `errCheckSumError` (305) |
| `dynamicsource` | `False` | accepted, no effect |
| `chunksize` | the bindings: 8 MiB | honoured; when not given, `Config.chunk_size` (4 MiB unless `XRD_CPCHUNKSIZE` or `EnvPutInt("CPChunkSize")` says otherwise) |
| `parallelchunks` | the bindings: 4 | honoured, as the number of chunks in flight (`Config.in_flight`) |
| `inittimeout` | `600` | accepted, no effect - `Config`'s connect and request timeouts apply |
| `tpctimeout` | `1800` (`CPTPCTimeout`) | honoured: bounds a third-party copy |
| `rmBadCksum` | `False` | honoured: delete a target whose checksum did not match |
| `cptimeout` | `0` | accepted, no effect - `Config.stall_deadline` bounds a copy |
| `xrateThreshold` | `0` | accepted, no effect |
| `xrate` | `0` | accepted, no effect - no rate limit is applied |
| `retry` | `0` (`CpRetry`) | honoured: retry a transient failure this many times |
| `cont` | `False` | honoured: resume a partial target |
| `rtrplc` | `"force"` (`CpRetryPolicy`) | honoured: a retry under `"continue"` resumes the partial target; under anything else it overwrites it, as XrdCl sets `force` for the retry |

### Progress handler

`run(handler)` calls these on any object that has them - subclassing
`client.utils.CopyProgressHandler` is conventional but not required:

| Method | When |
| --- | --- |
| `begin(jobId, total, source, target)` | before each job; `jobId` counts from 1, `source` and `target` are `URL`s |
| `update(jobId, processed, total)` | as bytes move; not called for a third-party copy, whose bytes never pass through this process |
| `should_cancel(jobId)` | before each update; returning `True` stops the job with `errOperationInterrupted` (207) |
| `end(jobId, results)` | after each job, with that job's result dict |

With `parallel(n)`, these are called from several threads at once.

## URL

`client.URL(url)` - a URL split by XrdCl's rules, which differ from the
native `xrdclient.XRootDURL` on purpose: `root://h/rel` has the relative path
`rel`; an IPv6 host keeps its brackets; a bare `/path` is
`file://localhost/path`; a bare `host:port//path` is `root://`; a string that
does not parse gives `is_valid() == False` rather than an exception.

| Attribute | Example for `root://user@host:1095//p?a=1` |
| --- | --- |
| `protocol` | `"root"` |
| `username` | `"user"` |
| `password` | `""` |
| `hostname` | `"host"` |
| `port` | `1095`; when none is given, 80 for `http` and `dav`, 443 for `https` and `davs`, 1094 otherwise - as XrdCl |
| `path` | `"/p"` |
| `path_with_params` | `"/p?a=1"` |
| `hostid` | `"user@host:1095"` |
| `is_valid()` | `True` |
| `clear()` | empties every field |
| `str(url)` | `"root://user@host:1095//p?a=1"` |

URLs compare equal when their string forms are equal, and hash by it. The
attributes can be assigned, which the bindings' read-only properties do not
allow.

## responses

Every response is a plain attribute bag: `vars(r)` is its fields, `==`
compares them, and `repr` prints `<name: value, ...>` as the bindings do.

### XRootDStatus

| Attribute | Type | Meaning |
| --- | --- | --- |
| `status` | `int` | `0` OK, `1` error, `3` fatal |
| `code` | `int` | XrdCl's error code - see [status codes](#status-codes) |
| `errno` | `int` | the server's `kXR_*` number when `code` is 400, the `kXR_*` number for the OS error when it is 402, else `0` (or the OS `errno` for a local failure) |
| `message` | `str` | XrdCl's rendering: `"[ERROR] Server responded with an error: [3011] ...\n"`, `"[SUCCESS] "` |
| `shellcode` | `int` | the exit status XrdCl's tools would use: `0` for success, else `code // 100 + 50` |
| `error` | `bool` | `status` is error or fatal |
| `fatal` | `bool` | `status` is fatal: the connection, not the request, failed |
| `ok` | `bool` | success |

`str(status)` is the message. `status["ok"]` works as well as `status.ok`,
because the bindings hand some statuses out as plain dicts.

### StatInfo

| Attribute | Type | Meaning |
| --- | --- | --- |
| `id` | `str` | the server's id for the file |
| `size` | `int` | bytes |
| `flags` | `int` | `StatInfoFlags` bits |
| `mtime`, `modtime` | `int` | modification time, epoch seconds |
| `modtimestr` | `str` | `mtime` in UTC, `"2026-09-29 12:34:57"` |
| `ctime`, `atime` | `int` | epoch seconds; `0` from a server too old to send them |
| `mode` | `str` | octal permissions, `"0644"` |
| `modeoctstr` | `str` | `"rw-r--r--"` |
| `owner`, `group` | `str` | names |
| `extended` | `bool` | whether the server sent the protocol 5 fields (`mode`, `owner`, `group`, `ctime`, `atime`); without them those are empty |
| `haschecksum` | `bool` | always `False`, as with the bindings for a plain stat |
| `checksum` | `str` | always `""` |

### StatInfoVFS

`nodes_rw`, `free_rw`, `utilization_rw`, `nodes_staging`, `free_staging`,
`utilization_staging` - all `int`, as the server reports them (free space in
MB, utilisation in percent).

### DirectoryList and ListEntry

| Class | Attribute | Meaning |
| --- | --- | --- |
| `DirectoryList` | `size` | number of entries |
| | `parent` | the directory listed, always ending in `/` |
| | `dirlist` | list of `ListEntry`; the object is also iterable |
| `ListEntry` | `name` | the name (with `RECURSIVE`, the path below `parent`) |
| | `hostaddr` | `host:port` of the server that answered |
| | `statinfo` | `StatInfo`, or `None` without `DirListFlags.STAT` |

### LocationInfo and Location

| Class | Attribute | Meaning |
| --- | --- | --- |
| `LocationInfo` | `locations` | list of `Location`; also iterable |
| `Location` | `address` | `host:port` |
| | `type` | `LocationType` |
| | `accesstype` | `AccessType` |
| | `is_manager`, `is_server` | `bool` |

### ProtocolInfo

`version` and `hostinfo`, both `int`, with their four bytes in reverse order
exactly as the bindings report them: protocol 5.2.0 (`0x520`) reads
`0x20050000`. Code that compares these numbers keeps working. The native
`FileSystem.protocol()` has them the right way round.

### VectorReadInfo and ChunkInfo

| Class | Attribute | Meaning |
| --- | --- | --- |
| `VectorReadInfo` | `size` | total bytes read |
| | `chunks` | list of `ChunkInfo`, in the order asked for; also iterable |
| `ChunkInfo` | `offset`, `length` | the range |
| | `buffer` | `bytes` |

### HostList and HostInfo

The third argument of a callback.

| Class | Attribute | Meaning |
| --- | --- | --- |
| `HostList` | `hosts` | list of `HostInfo`; also iterable. One entry - the server that answered - or none when the call failed before connecting |
| `HostInfo` | `url` | `URL` of the server |
| | `protocol` | its protocol version, not byte-swapped |
| | `flags` | its server flags |
| | `load_balancer` | always `False` |

## flags

Each namespace is a class of `int` attributes with a `reverse_mapping` dict
from value to name, as the bindings build them. The numbers are the
bindings', not the wire protocol's; where they differ from the native
`xrdclient.flags` enums, the native ones are the wire's.

| Namespace | Members |
| --- | --- |
| `OpenFlags` | `NONE` 0, `DELETE` 2, `FORCE` 4, `NEW` 8, `READ` 16, `UPDATE` 32, `REFRESH` 128, `MAKEPATH` 256, `REPLICA` 2048, `POSC` 4096, `NOWAIT` 8192, `SEQIO` 16384, `WRITE` 32768, `DUP` 65536, `SAMEFS` 131072 |
| `AccessMode` | `NONE` 0, `UR` 256, `UW` 128, `UX` 64, `GR` 32, `GW` 16, `GX` 8, `OR` 4, `OW` 2, `OX` 1 |
| `MkDirFlags` | `NONE` 0, `MAKEPATH` 1 |
| `DirListFlags` | `NONE` 0, `STAT` 1, `LOCATE` 2, `RECURSIVE` 4, `MERGE` 8, `CHUNKED` 16, `ZIP` 32 |
| `PrepareFlags` | `STAGE` 8, `WRITEMODE` 16, `COLOCATE` 32, `FRESH` 64, `EVICT` 256 |
| `QueryCode` | `STATS` 1, `PREPARE` 2, `CHECKSUM` 3, `XATTR` 4, `SPACE` 5, `CHECKSUMCANCEL` 6, `CONFIG` 7, `VISA` 8, `OPAQUE` 16, `OPAQUEFILE` 32 |
| `StatInfoFlags` | `X_BIT_SET` 1, `IS_DIR` 2, `OTHER` 4, `OFFLINE` 8, `IS_READABLE` 16, `IS_WRITABLE` 32, `POSC_PENDING` 64, `BACKUP_EXISTS` 128 |
| `LocationType` | `MANAGER_ONLINE` 0, `MANAGER_PENDING` 1, `SERVER_ONLINE` 2, `SERVER_PENDING` 3 |
| `AccessType` | `READ` 0, `READ_WRITE` 1 |
| `HostTypes` | `IS_SERVER` 1, `IS_MANAGER` 2, `ATTR_META` 256, `ATTR_PROXY` 512, `ATTR_SUPER` 1024 |

`OpenFlags.DUP` and `SAMEFS` belong to `openusingtemplate`, which is not
supported. `DirListFlags.CHUNKED` is accepted and has no effect: a listing
arrives whole. The bindings' `flags.enum` helper function is not provided.

## Environment keys

`client.EnvPutInt(key, value)` and `client.EnvPutString(key, value)` store a
setting for objects created afterwards; the keys below are translated to the
native [`Config`](config.md) field they mean. Keys are case-insensitive. As in
XrdCl, a variable already set in the process environment as `XRD_<KEY>` wins:
the put returns `False` and changes nothing - and the `Config` reads that
variable itself.

| XrdCl key | `Config` field | Conversion | This library's default |
| --- | --- | --- | --- |
| `ConnectionWindow` | `connect_timeout` | seconds | 30 |
| `ConnectionRetry` | `connect_retries` | count | 3 |
| `RequestTimeout` | `request_timeout` | seconds | 300 |
| `StreamTimeout` | `stream_timeout` | seconds | 60 |
| `RedirectLimit` | `redirect_limit` | count | 16 |
| `CPChunkSize` | `chunk_size` | bytes | 4 MiB |
| `CPParallelChunks` | `in_flight` | chunks in flight | 2 |
| `SubStreamsPerChannel` | `data_streams` | minus one: XrdCl counts the control stream, the field does not | 1 extra |

`CPParallelChunks` put through `EnvPutInt` sets `in_flight`, the number of
chunks a copy keeps in flight, which is what XrdCl's key does. The
environment variable `XRD_CPPARALLELCHUNKS` is read by `Config` itself into
`parallel_chunks`, the number of connections one large copy is spread over.

Other keys can be put and read back but change nothing, except that
`CopyProcess.add_job` reads three of them for its defaults:

| Key | XrdCl default | Used by |
| --- | --- | --- |
| `CPTPCTimeout` | 1800 | `add_job(tpctimeout=...)` default |
| `CpRetry` | 0 | `add_job(retry=...)` default |
| `CpRetryPolicy` | `"force"` | `add_job(rtrplc=...)` default |
| `StreamErrorWindow`, `TimeoutResolution`, `CPInitTimeout`, `CPTimeout`, `XRateThreshold`, `PollerPreference` | 1800, 15, 600, 0, 0, `"built-in"` | nothing |

| Function | Returns | Notes |
| --- | --- | --- |
| `EnvPutInt(key, value)` | `bool` | `False` when `XRD_<KEY>` is set in the environment |
| `EnvPutString(key, value)` | `bool` | as above |
| `EnvGetInt(key)` | `int` or `None` | the environment's value, else a put one, else **XrdCl's** default - not necessarily what is in force |
| `EnvGetString(key)` | `str` or `None` | as above |
| `EnvDelInt(key)`, `EnvDelString(key)` | `bool` | forget a put value; `False` when the environment holds it |
| `EnvGetDefault(key)` | `int`, `str` or `None` | XrdCl's built-in default |
| `SetLogLevel(level)` | `None` | sets the `xrdclient` logger: `"Error"`, `"Warning"`, `"Info"`, `"Debug"`, `"Dump"` (case-insensitive); anything else is `ValueError` |
| `SetLogMask(level, mask)` | `None` | accepted, no effect; choose a child logger such as `xrdclient.session` instead |

## utils

| Name | Notes |
| --- | --- |
| `AsyncResponseHandler()` | pass as `callback=`, then `status, response, hostlist = handler.wait()`; `wait()` blocks with no timeout |
| `CopyProgressHandler` | base class with no-op `begin`, `end`, `update` and `should_cancel` (which returns `False`) |

The bindings' `utils` module also exposes `CallbackWrapper`, `Lock`,
`XRootDStatus` and `HostList` as implementation details. They are not here:
use `responses.XRootDStatus` and `responses.HostList`, and a plain callable as
a callback.

## glob

| Function | Notes |
| --- | --- |
| `glob(pathname, raise_error=True)` | list of matches |
| `iglob(pathname, raise_error=True)` | iterator of matches |

The bindings' rules: a pattern that matches anything on the local disk is a
local pattern; otherwise each wildcard level is expanded with a `dirlist` on
the server. A trailing `?key=value` is the URL's parameters, carried onto
every result; a trailing `/` matches directories only. A directory that
cannot be listed raises `RuntimeError`, or is skipped with
`raise_error=False`.

## setXAttrAdler32

`client.setXAttrAdler32(path, checksum)` writes `checksum` (eight hex digits)
into the `XrdCks.adler32` extended attribute of a **local** file, in XrdCks's
own 96-byte record, so an `xrootd` exporting that file finds the checksum
instead of computing it. On Linux the attribute is `user.XrdCks.adler32`; on
macOS it is `XrdCks.adler32`. A checksum that is not four bytes is
`ValueError`; a filesystem without extended attributes is `OSError`.

## Status codes

Every `code` this layer can report. `status` is the level: `1` error, `3`
fatal. `shellcode` is `code // 100 + 50`, as in XrdCl.

| `code` | Name | Meaning | `status` | `shellcode` |
| --- | --- | --- | --- | --- |
| 0 | `errNone` | success | 0 | 0 |
| 4 | `suAlreadyDone` | `close()` on a file that was not open - still a success | 0 | 0 |
| 2 | `errUnknown` | a failure with no better description | 1 | 50 |
| 3 | `errInvalidOp` | `open` on a file that is already open | 1 | 50 |
| 9 | `errInvalidArgs` | a bad URL in `CopyProcess.prepare`, a bad argument to a callback call, `sendinfo` over 1024 characters | 1 | 50 |
| 12 | `errOSError` | a local file operation outside a copy failed; `errno` is the OS's | 1 | 50 |
| 13 | `errNotSupported` | `fcntl`, `openusingtemplate`, any call on a `FileSystem` whose URL did not parse | 1 | 50 |
| 14 | `errDataError` | a page read failed its CRC32C check | 1 | 50 |
| 15 | `errNotImplemented` | `dirlist` with `ZIP`; `sendinfo` to a server that is not `root://` | 1 | 50 |
| 108 | `errConnectionError` | could not connect, or the connection dropped | 3 | 51 |
| 204 | `errAuthFailed` | no mechanism was accepted; the message says why each one failed | 3 | 52 |
| 206 | `errOperationExpired` | `timeout=` or a native timeout ran out | 1 | 52 |
| 207 | `errOperationInterrupted` | a copy job cancelled by `should_cancel` | 1 | 52 |
| 303 | `errInvalidResponse` | the server's answer did not parse | 3 | 53 |
| 305 | `errCheckSumError` | a copy's checksums did not match, or `checksumpreset` was not met | 1 | 53 |
| 306 | `errRedirectLimit` | more redirects than `RedirectLimit` | 1 | 53 |
| 400 | `errErrorResponse` | the server refused the request; `errno` is its `kXR_*` number | 1 | 54 |
| 402 | `errLocalError` | a copy's local end failed; `errno` is the protocol's number for the OS error (`XProtocol::mapError`) | 1 | 54 |

XrdCl's `errInvalidAddr` (101), `errSocketTimeout` (103), `errTlsError`
(110) and `errLoginFailed` (203) are not produced. An unreachable server is
`errConnectionError` (108), a native timeout is `errOperationExpired` (206),
and a refused login is `errAuthFailed` (204), so code that tests for one of
the four exact numbers should test `status.fatal` or the neighbouring code as
well.

The `errno` values that come with `code` 400 are the server's:

| `errno` | Name | Usually |
| --- | --- | --- |
| 3000 | `kXR_ArgInvalid` | a malformed argument |
| 3001 | `kXR_ArgMissing` | a required argument missing |
| 3003 | `kXR_FileLocked` | the file is open for writing elsewhere |
| 3004 | `kXR_FileNotOpen` | the handle is not open on the server |
| 3005 | `kXR_FSError` | the storage failed |
| 3006 | `kXR_InvalidRequest` | the server does not accept this request here |
| 3007 | `kXR_IOError` | an I/O error on the server |
| 3009 | `kXR_NoSpace` | no space left |
| 3010 | `kXR_NotAuthorized` | permission denied |
| 3011 | `kXR_NotFound` | no such file or directory |
| 3012 | `kXR_ServerError` | an internal server error |
| 3013 | `kXR_Unsupported` | not supported by this server |
| 3014 | `kXR_noserver` | no server can serve the path |
| 3015 | `kXR_NotFile` | not a file |
| 3016 | `kXR_isDirectory` | is a directory |
| 3018 | `kXR_ItExists` | already exists |
| 3019 | `kXR_ChkSumErr` | checksum error on the server |
| 3021 | `kXR_overQuota` | quota exceeded |
| 3024 | `kXR_Overloaded` | the server is overloaded |
| 3025 | `kXR_fsReadOnly` | read-only filesystem |
| 3027 | `kXR_AttrNotFound` | no such extended attribute |
| 3028 | `kXR_TLSRequired` | the server requires TLS - use `roots://` |
| 3029 | `kXR_noReplicas` | no replica available |
| 3030 | `kXR_AuthFailed` | authentication failed |

## Module-level differences

Two modules of the bindings have no counterpart:

- `XRootD.client.finalize`, which shuts XrdCl down at exit - there is nothing
  here to shut down; connections are closed by an `atexit` hook.
- `XRootD.client._version` - use `importlib.metadata.version("xrdclient")`.

`install()` registers every other submodule under its `XRootD.client.` name.
