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

Each call is handed to a worker thread with `asyncio.to_thread`, so the event
loop is never blocked. Two consequences are worth knowing before you design
around it.

**Concurrency is per endpoint.** Calls on one session are serialised by that
session's lock, exactly as concurrent threads are in the synchronous API.
Concurrency across *different* endpoints is real. To read four files at once,
open four handles:

```python
async def read_all(urls):
    async def one(url):
        async with xrdclient.aio.open(url, "rb") as fh:
            return await fh.read()
    return await asyncio.gather(*(one(u) for u in urls))
```

Note that `asyncio.gather` over a single `FileSystem` still helps: the calls
queue on one connection but the round trips overlap with everything else your
loop is doing.

**Cancellation stops your coroutine, not the request.** A cancelled `await`
leaves the in-flight request running on its worker thread. Anything that must
be undone on cancellation belongs in a `finally:`.

## What is mirrored

Everything: `FileSystem` and all of its methods, `File`, `open`, `copy`,
`copy_tree`, `third_party`. The scheme still selects the implementation, so
`davs://` works here exactly as it does synchronously.

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
