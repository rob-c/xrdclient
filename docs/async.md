# Asynchronous use

`xrdclient.aio` mirrors the whole synchronous surface: same names, same arguments,
same exceptions, with `await` in front and `async for` over what used to be a
generator.

```python
import asyncio, xrdclient.aio

async def main():
    async with xrdclient.aio.FileSystem("root://eos.example.org") as fs:
        info = await fs.stat("/store/f.root")
        async for entry in fs.iterdir("/store"):
            print(entry.name)
        async with fs.open("/store/f.root") as fh:
            head, tail = await fh.readv([(0, 4096), (1 << 20, 4096)])

asyncio.run(main())
```

`import xrdclient` does not import `asyncio`; the facade is resolved on first use.

## How it runs

Each call is handed to a worker thread of the facade's own pool, so the event
loop is never blocked. The pool is not the loop's default executor - that one
is sized for the machine's cores and would quietly cap how many requests are
in flight - but one of `Config.aio_workers` threads (32 by default,
`$XRD_AIOWORKERS`), made on first use. The caller's context variables travel
with each call, as with `asyncio.to_thread`, so a `deadline()` or
`config.override()` around an `await` applies to the work it awaits. A few
consequences are worth knowing before you design around it.

**Calls on one session run together.** Concurrent calls on one session are
multiplexed on its connection, exactly as concurrent threads are in the
synchronous API: each request goes out at once on a stream of its own, and a
slow one - a server's `kXR_wait`, a `kXR_waitresp` parked for minutes - holds
up nobody else. One worker thread at a time reads the socket and hands each
reply to the call it answers. A bulk transfer (a large read or write) has the
connection to itself while it runs, and the other calls on it wait their
turn. To move four large files at once, give each a connection by opening
four handles:

```python
async def read_all(urls):
    async def one(url):
        async with xrdclient.aio.open(url, "rb") as fh:
            return await fh.read()
    return await asyncio.gather(*(one(u) for u in urls))
```

`asyncio.gather` over a single `FileSystem` overlaps the round trips of its
calls on the one connection - many `stat`s cost about as long as the slowest.

**One file may be shared.** `asyncio.gather(fh.pread(4096, 0),
fh.pread(4096, 1 << 20))` runs both reads at once on one handle, and is safe:
`xrdclient.File` keeps its readahead, its data-stream cursor and its recovery
after a lost server behind a lock of its own, so concurrent calls neither tear
that state nor re-open the file twice. Calls that move the cursor - `read`,
`seek`, `write` - still race each other for the *position*, as two threads
sharing a file object would; use `pread` and `pwrite` for concurrent access.

**Cancellation abandons the request.** Each call runs inside a cancel scope
of its own, and cancelling the coroutine - `task.cancel()`, an
`asyncio.timeout()` running out, a `TaskGroup` whose sibling failed - cancels
the scope. The coroutine raises `CancelledError` at once. Its worker notices
within about 50 ms wherever it is waiting on a `root://` connection, and the
request it was waiting on is *abandoned*: its stream id is kept out of use
until the server's late answer arrives, and that answer is dropped. The
connection stays in step and goes on serving the calls after it, and the
worker thread is free for the next one, so cancelling a hundred stalled calls
does not use up the pool.

```python
async with xrdclient.aio.FileSystem("root://eos.example.org") as fs:
    try:
        async with asyncio.timeout(5):
            info = await fs.stat("/store/slow.root")
    except TimeoutError:
        info = None          # the stat is abandoned, the connection still good
    await fs.ping()          # same connection, no reconnect
```

Nothing more is sent for a cancelled call: a `copy` reads and writes no
further chunk, a `readv` split into several requests sends no further batch,
and nothing is retried. A bulk transfer (a large read, the bulk download)
stops between frames and leaves the replies still on their way to be dropped
the same way. If it is caught part-way through a frame, it waits up to a
quarter of a second for the rest of that frame. If the frame does not
arrive, it closes the connection rather than leave it out of step. Calls
after it go over a fresh connection, and a file's next bulk read re-opens the
file there.

What is *not* cancelled:

- An `open()` that has already started still completes on its thread,
  because an abandoned open would leave a handle on the server that nobody
  could close. The handle it produces is closed for you.
- Over `http(s)://` and `s3://`, a request already sent runs until it is
  answered or times out (stopping it part-way would cost the connection).
  Nothing after it is sent.
- Connecting: a TCP connect or TLS handshake already started runs to its
  `connect_timeout`. The login after it is cancelled.
- A cancelled call's own clean-up round trips. Examples are a checkpoint
  rollback inside a cancelled `copy`, or removing a partial remote target.
  These would be waits on the server that was too slow. Clean-up you write
  yourself, in an `except` or `finally` around the `await`, is a call of its
  own and runs normally. That includes `async with` closing a file, and an
  `AsyncFile.checkpoint()` block rolling back.

## What is mirrored

Everything: `FileSystem` and all of its methods (the `move` and `unlink`
spellings included), `File`, `open`, `copy`, `copy_tree`, `third_party`, and
every one-line verb from [the easy API](easy.md) under the same name. The
scheme still selects the implementation, so `davs://` works here exactly as it
does synchronously. A test compares the two surfaces name by name, so a sync
method added without its mirror fails the suite rather than going missing here.

```python
import xrdclient.aio as aio

if await aio.exists("root://host//store/f.root"):
    blob = await aio.read_bytes("root://host//store/f.root")
paths = await aio.glob("root://host//store/run7/**/*.root")   # a list, not an iterator
await aio.mkdir("root://host//store/out")
await aio.move("root://host//store/tmp.root", "root://host//store/out/f.root")
request = await aio.stage(paths, priority=1)
```

An open file has the positional and per-handle calls too. `pread` and `pwrite`
go to the server past the file's buffer, leaving the cursor alone; pending
buffered writes are flushed first so they land in order, but bytes already
read ahead into the buffer are not refreshed, so open with `buffering=0` to
mix `pwrite` with cursor reads of the same range.

```python
async with xrdclient.aio.open(url, "r+b") as fh:
    header = await fh.pread(64, 0)       # the cursor has not moved
    await fh.pwrite(b"v2", 4)
    await fh.setxattr("run", b"7")       # on the open handle, not the path
    print(await fh.listxattr(), await fh.size())
    await fh.verify("1a0b045d")          # adler32 unless told otherwise
```

`File.size` is a property synchronously and a coroutine here, `await fh.size()`,
because it can cost a round trip. Only `open`, `handle` and `session` have no
mirror: an `AsyncFile` is born open, and the other two are protocol plumbing
reachable through `fh.file`. `fs.walk` takes `os.walk`'s arguments; its
`onerror` is called on the worker thread, not the event loop.

```python
result = await xrdclient.aio.copy("root://a//store/f.root", "/scratch/f.root")
results = await xrdclient.aio.copy_tree("root://a//store/run7", "/scratch/run7",
                                  exclude=("*.log",), sync="size", delete=True)
```

Checkpoints are an `async with`, and the link family is there too:

```python
async with xrdclient.aio.open(url, "r+b") as fh:
    async with fh.checkpoint() as cp:
        await fh.write(header)
        await fh.flush()           # the journal only sees what was sent
        print((await cp.query()).free)

await fs.symlink("/store/f.root", "/store/latest")
await fs.readlink("/store/latest")
await fs.is_symlink("/store/latest")
await fs.lstat("/store/latest")
await fs.utime("/store/f.root", ns=(atime_ns, mtime_ns))
await fs.chown("/store/f.root", uid, gid)
await fs.listxattr_tree("/store/run7")   # attribute names for a subtree
await fs.extensions()                    # which of these the server has
```

So are server-side range copies, which take either an `AsyncFile` or the
`xrdclient.File` under one:

```python
async with fs.open("/store/dst.root", "r+b") as dst, \
           fs.open("/store/src.root") as src:
    await dst.flush()                    # the server can only clone what it has
    await dst.clone(src, [(0, 4096, 0)])  # kXR_clone; see Files -> copying ranges
```

So is the second data connection, which is a coroutine here because opening
it is a round trip:

```python
async with xrdclient.aio.open(url, "rb") as fh:
    await fh.bind_data_path()      # kXR_bind; see Files -> a second connection
    print(fh.data_path)            # 1, and 0 on an http(s) endpoint
    blob = await fh.read()
```

Flushing inside the block is the one difference worth remembering: a buffered
write still sitting in the buffer reaches the server *after* the commit, and
so is not part of the transaction.

## An idiom worth stealing

Sizes for a whole directory, with the listing and the stats overlapped:

```python
async with xrdclient.aio.FileSystem("root://host") as fs:
    names = await fs.listdir("/store")
    sizes = await asyncio.gather(*(fs.getsize(f"/store/{n}") for n in names))
```

For genuinely parallel transfers, one filesystem per worker:

```python
async def fetch(name):
    async with xrdclient.aio.FileSystem("root://host") as fs:
        return await fs.read_bytes(f"/store/{name}")

blobs = await asyncio.gather(*(fetch(n) for n in names))
```

One filesystem per worker is not one login per worker: a connection a worker
has finished with goes back to the pool for the next one, so the cost of the
idiom above is the connections actually in flight rather than the number of
names. See [Pooling](config.md#pooling).

## Asking the server about itself

The sync surface is mirrored whole, including the corners:

```python
space = await fs.query_space("/store")      # SpaceInfo, in bytes
stats = await fs.query_stats("a")           # the XML summary
await fs.appid("higgs-skim")                # label this connection
await fs.set_property("monitor off")
await fs.query_prepare(handle, paths)       # how the staging is going
await fs.cancel_prepare(handle)             # withdraw a staging request
await fs.archive_info(paths)                # on disk, or still on tape
await fs.checksum_cancel("/store/big.root")
```
