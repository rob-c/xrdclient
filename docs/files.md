# Files and paths

## `xrdclient.open` returns an `io` object

```python
fh = xrdclient.open("root://host//store/f.root", "rb")
```

`fh` is an `io.BufferedReader` wrapping an `XRootDRawIO`, which is an
`io.RawIOBase`. That single decision is what makes the rest of Python work
without adapters: anything that accepts a binary file object accepts this -
`numpy.load`, `zipfile.ZipFile`, `tarfile`, `pyarrow`, `uproot`, `json.load`
through a `TextIOWrapper`, `shutil.copyfileobj`.

| Argument | Meaning |
| --- | --- |
| `mode` | `r w x a` with `b t +`, exactly as the builtin. Default `"rb"` |
| `buffering` | `0` raw (binary only), `-1` the default buffer (below), or a size |
| `encoding`, `errors`, `newline` | passed to `io.TextIOWrapper` in text mode |
| `config` | a [`Config`](config.md) for this file |
| `router` | share an existing connection |
| `posc` | persist-on-successful-close: the server discards a partial file if the connection dies |

The buffer defaults to a megabyte rather than the stdlib's 8 KiB because a
wide-area round trip costs several orders of magnitude more than a local one.
A file opened only for writing gets more: one whole window of writes,
`config.chunk_size * config.bulk_depth` (16 MiB by default), because each
flush of the buffer is sent as that many pipelined `kXR_write`s and a smaller
buffer would leave the pipeline empty. Writes bigger than the buffer skip it
and go to the wire straight from your own buffer. An explicit `buffering`
is always taken as given.

The signature is overloaded the way typeshed overloads the builtin, so a type
checker knows what came back:

```python
xrdclient.open(url, "rb").read()               # bytes
xrdclient.open(url, "r").read()                # str
xrdclient.open(url, "rb", buffering=0)         # XRootDRawIO, so .file is there
xrdclient.open(url, mode_from_config).read()   # Any - a non-literal mode is the escape hatch
```

The package ships a `py.typed` marker, so this reaches your code without a
stub package. See [Typing](typing.md).

### Modes, and what they do on the wire

| Mode | `kXR_open` options | Creates? |
| --- | --- | --- |
| `r` | `kXR_open_read` | no |
| `r+` | `kXR_open_updt` | no |
| `w`, `w+` | `kXR_open_updt \| kXR_delete` | yes (truncating) |
| `x`, `x+` | `kXR_open_updt \| kXR_new` | yes, fails if present |
| `a`, `a+` | `kXR_open_updt \| kXR_open_apnd` | yes |

!!! note "Append is the interesting one"
    XRootD's `kXR_open_apnd` opens for appending but does *not* create, and
    `kXR_new` is refused outright when the file already exists - so no single
    flag combination has Python's `"a"` semantics. The raw layer opens for
    append, and only if the server answers `kXR_NotFound` does it retry with
    `kXR_new`. This is one of the behaviours the
    [interoperability suite](interop.md) found; the fake server used to be
    more permissive than a real one and hid it.

## The handle underneath

`fh.raw.file` (or `fh.file` on a raw object) is an `xrdclient.File`, which is where
the operations that have no `io` equivalent live:

```python
handle = fh.raw.file
head, tail = handle.readv([(0, 4096), (1 << 30, 4096)])   # kXR_readv
page = handle.pgread(1 << 20, 0)                          # CRC32c per 4 KiB
print(page.corrupt_pages)                                 # [] when clean
handle.writev([(0, b"first"), (4096, b"second")])         # kXR_writev
handle.clone(other, [(0, 4096, 0)])                       # kXR_clone, server-side
handle.sync()
handle.truncate(1 << 20)
print(handle.checksum())                                  # adler32:1a0b045d
print(handle.compression)                                 # (0, '') - stored whole
```

`compression` is the page size and algorithm the server reported when the
file was opened. Modern servers store files whole and report `(0, "")`; the
fields are in every `kXR_open` reply, so this is something you can check
rather than assume.

`File` can also be used on its own, with or without `with`:

```python
from xrdclient import File

handle = File("root://host//store/f.root")
handle.open("x")                    # the builtin's letters, or the protocol's
try:                                # own names: handle.open("update new")
    handle.write(b"...", 0)
finally:
    handle.close()
```

### How the bytes move

Reads, writes and vector reads on a `File` run on the
[bulk data plane](performance.md#the-bulk-data-plane): each request is framed
directly on the connection and its reply received straight into the buffer it
is returned in, with several requests in flight at once.

- `write(data, offset)` sends `config.chunk_size` pieces, `config.bulk_depth`
  of them in flight, each straight from `data` without a copy. A refusal of
  any piece raises from that `write`, after every other reply in the window
  has been read, so the connection is fit for the next request and nothing is
  reported written that the server did not acknowledge.
- `readv()` sends every batch its ranges need together, so a vector read too
  big for one request still costs about one round trip.
- `read(size, offset)` on a handle opened only for reading notices when a read
  starts where the last one ended, and then fetches `config.readahead` bytes
  beyond it (1 MiB, doubling while the reads stay sequential, up to 8 MiB);
  the reads that follow are answered from those bytes without a round trip. A
  read anywhere else never reads ahead and starts the window again, so random
  access pays nothing for it. `readahead=0` turns it off. What is read ahead
  is as fresh as a buffered reader's buffer: a handle that can write never
  reads ahead.

A server that answers with a conversation rather than bytes - a `kXR_wait`, a
redirect - gets the same request again the ordinary way, and `config.bulk =
False` puts everything back on that path.

`open()` takes what `xrdclient.open` takes - `"r"`, `"w"`, `"x"`, `"a"`, `"r+"` -
or the flag names in a string, or the flags themselves; its second argument
is the mode a created file gets, and reads either as `0o640` or as
`"rw-r-----"`.

Entering a `File` that is not yet open opens it for reading; entering one you
have already opened is an error rather than a silent re-open.

### Vector reads

`readv()` returns one `bytes` per requested range, in the order you asked
for, no matter how the server batches or reorders the reply. Requests larger
than the server's advertised limits are split into as few requests as the
limits allow, and those go out together, `config.bulk_depth` at a time.

```python
ranges = [(off, 128 << 10) for off in offsets]
for wanted, got in zip(ranges, handle.readv(ranges)):
    ...
```

This is the single biggest performance lever for HEP workloads: one round
trip for a hundred scattered basket reads instead of a hundred.

### Paged I/O

`pgread` and `pgwrite` carry a CRC32c per 4 KiB page. `pgread` verifies by
default and reports the offset of any page that failed:

```python
result = handle.pgread(1 << 20, 0)
if result.corrupt_pages:
    raise IOError(f"corrupt pages at {result.corrupt_pages}")
```

### Copying ranges without moving them

`clone()` (`kXR_clone`, opcode 3032) has the server copy byte ranges out of
one open file and into another. The data never leaves the server, which makes
it the cheap way to build a file out of pieces of another one:

```python
with xrdclient.FileSystem("root://host") as fs, \
     fs.open("/store/src.root", "rb") as reader, \
     fs.open("/store/dst.root", "wb") as writer:
    src, dst = reader.raw.file, writer.raw.file
    dst.clone(src)                            # all of it, at the same offsets
    dst.clone(src, [(4096, 1024, 0)])         # one range, moved to the front
    dst.clone(src, [CloneRange(0, 8192)])     # or the dataclass, if you prefer
```

`clone` is a protocol operation, not an `io` one, so it sees the file as the
server has it: `flush()` anything still sitting in a buffer above it first.
A range is `(offset, length)` or `(offset, length, target_offset)`; leaving
the target offset out puts the bytes where they came from. `clone()` returns
the number of bytes copied, batches more than 1024 ranges into as many
requests as it takes, and skips empty ones.

Both handles must belong to the same session — a handle means nothing to a
server that did not hand it out — so open them from one `FileSystem`, as
above; two handles on separate connections are refused with `ValueError`
before anything is sent. A clone cannot be part of a checkpoint. For a copy
between two *different* servers, see
[third-party copy](copying.md#third-party-copy), which is the same idea one
level up.

!!! warning "Not every server has it"
    Opcode 3032 is one past `kXR_REQFENCE`, where `XProtocol.hh` stops: a
    stock `xrootd` (5.9 is the newest) answers "invalid request code", and
    only the servers that added the extension - the nginx-xrootd family -
    implement it. The client turns that refusal into `UnsupportedError` so
    the fallback is easy to write:

    ```python
    try:
        dst.clone(src, ranges)
    except xrdclient.UnsupportedError:
        for offset, length in ranges:
            dst.write(src.read(length, offset), offset)
    ```

### A second connection for the bytes

`bind_data_path()` opens one more connection to the same server, binds it to
the same session with `kXR_bind`, and moves this handle's bulk I/O onto it.
Requests keep going out on the control link, so a `stat` or a `close` is
never stuck behind a megabyte of file, and the reply to a read comes back on
the data path.

```python
handle = fh.raw.file
handle.bind_data_path()          # returns the path id the server assigned
print(handle.data_path)          # 1
data = handle.read(64 << 20, 0)  # the request on one socket, the data on the other
```

It applies to `read`, `readv`, `pgread`, `write` and `pgwrite`; everything
else is unaffected. Calling it twice is a no-op - a handle keeps the path it
has - and the binding is per connection, so a handle that lost its data
server and was re-opened elsewhere reports `data_path == 0` again and can be
bound again.

The second connection does not log in: it names the session it belongs to and
inherits that session's identity, which is why a server only accepts it from
the client that opened the session. It costs a handshake, not an
authentication, but it is still a connection - worth it for a file being
streamed, not for one being peeked at.

### Checkpointed writes

A checkpoint is a transaction over one handle: the server journals what you
write, and either keeps it or puts the file back the way it was.

```python
with xrdclient.File("root://host//store/f.root") as handle:
    handle.open(OpenFlags.UPDATE)
    with handle.checkpoint() as cp:
        handle.write(header, 0)
        handle.truncate(new_length)
        print(cp.query().free)      # bytes the journal still has room for
    # left cleanly -> committed; raised -> rolled back, and the error re-raised
```

Every `write`, `pgwrite` and `truncate` inside the block travels as
`kXR_ckpXeq` wrapping the real request, which is what makes it undoable.
`writev` is not one of the three a server can undo and is refused with
`UnsupportedError` rather than being let through unjournaled. Checkpoints do
not nest - the server keeps one per handle - and a server without
`kXR_chkpoint` raises on entry, before anything has been written.

`cp.query()` returns a `CheckpointInfo` with `capacity`, `used` and `free`.
A journal that fills up fails the write with `NoSpaceError`; the block still
rolls back on the way out.

### Recovery

With `Config(recover_handles=True)` - the default - a read-only handle whose
data server disappears is re-opened transparently and the read is retried.
`handle.recoverable` says whether a given handle qualifies. Writers are never
silently recovered, because a partially applied write is not something a
client can reason about on your behalf.

## `xrdclient.Path`

`xrdclient.Path` (also spelled `xrdclient.XRootDPath`) is the `pathlib` front door. It is
not a `PurePosixPath` subclass - it carries an endpoint - but it answers the
same questions:

```python
p = xrdclient.Path("root://host//store/user/me")

p.name, p.stem, p.suffix, p.parent, p.parts
p / "runs" / "run1.root"
p.with_suffix(".txt")
p.relative_to("root://host//store")

p.exists(), p.is_dir(), p.is_file(), p.is_symlink()
p.stat().st_size, p.lstat(), p.stat(follow_symlinks=False)
p.iterdir(), p.glob("**/*.root"), p.rglob("*.root"), p.walk()
p.mkdir(parents=True, exist_ok=True)
p.touch(), p.unlink(), p.rmdir(), p.rename(other), p.replace(other)
p.read_bytes(), p.write_text("hi"), p.open("rb")
p.checksum(), p.chmod(0o640), p.locate()      # chmod also takes "rw-r-----"
```

`relative_to` follows `pathlib`: it raises `ValueError` when the base is not
an ancestor or is on another endpoint, rather than answering with `..`;
`walk_up=True` asks for the `..` form, on every Python version.

A path holds a connection once it has used one; `close()` returns it, and
`with` does that for you.

## Extended attributes

Present on both `File` and `FileSystem`:

```python
fs.setxattr("/store/f.root", "user.experiment", b"atlas")
fs.getxattr("/store/f.root", "user.experiment")
fs.listxattr("/store/f.root")
fs.xattrs("/store/f.root")          # the whole mapping
fs.removexattr("/store/f.root", "user.experiment")
```

A per-attribute failure travels inside an otherwise successful reply, and the
client raises it rather than dropping it: `setxattr(..., create_only=True)`
over an existing name is a `FileExistsError`, and removing an attribute that
is not there is an `AttrNotFoundError`.

!!! warning "Servers mangle names"
    A stock `xrootd` returns `user.experiment` from a listing as
    `.experiment`. The round trip through `setxattr`/`getxattr` is exact; the
    listing is the server's idea of the name, and this client does not
    second-guess it.
