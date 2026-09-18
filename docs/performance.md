# Performance

Pure Python framing a binary protocol sounds slow. On the transfers that
matter it is not, because the work is dominated by the network and by
`memoryview` slicing, not by interpreted bytecode.

## Measured

`benchmarks/bench.py`, 16 MiB file, real `xrootd` daemon on loopback,
best-of-3, against the official XRootD 5.9.6 Python bindings and the `xrdcp`
/ `xrdfs` command line tools. Lower is better; `1.00x` is the winner of each
row.

| Case | this client | bindings | CLI |
| --- | --- | --- | --- |
| read whole file | 1.63x | **1.00x** | 3.67x (`xrdcp`) |
| 256 x 64 KiB reads | **1.00x** | 7.91x | |
| vector read, 8 x 128 KiB | 4.43x | **1.00x** | |
| write whole file | 2.52x | **1.00x** | |
| `stat` | **1.00x** | 1.50x | 825x (`xrdfs`) |
| `listdir` | **1.00x** | 6.50x | |
| copy to local disk | **1.00x** | 1.13x | 3.64x (`xrdcp`) |

Read the table honestly: on one streaming read of a large file the C++ client
is ~1.6x faster, and on a vector read it is ~4.4x faster. On everything a real
analysis job does in a loop - many small reads, metadata, listing, copying -
this client is at least as fast, because the per-call overhead of crossing the
Python/C++ boundary costs more than parsing a header in Python does.

The `xrdfs stat` figure is process startup, not protocol. It is in the table
because 825x is what a shell loop over a thousand files actually pays.

## Running it yourself

```console
$ python benchmarks/bench.py --size 16 --repeat 3      # size in MiB
$ python benchmarks/bench.py --json results.json
$ python benchmarks/bench.py --url root://host//store/scratch   # a real endpoint
```

The harness starts its own `xrootd` on a loopback port, skips any comparison
whose counterpart is not installed, and reports both best-of-N and the median
so a single unlucky run is visible rather than averaged away. Metadata cases
are reported in operations per second, data cases in MiB/s.

## The bulk data plane

A download does not go through the event path at all. `xrdclient.copy` from a
`root://` URL to a local file, `xrdclient.open(...).readinto(buf)` for a buffer worth
pipelining, and `xrdclient.client.bulk.download` / `.stream` directly all run on a
reader that does three things differently:

**It keeps several reads in flight.** One request at a time means one round
trip per chunk and a socket that is idle for the length of it.
`config.bulk_depth` requests are outstanding at once, so the server is
answering the next chunk while this one is still being written out.

**It receives into the destination.** `recv_into` lands each reply in the
buffer it will be used from - a slice of the caller's own buffer for
`readinto`, or a rotating scratch buffer that is handed straight to the
writer. The ordinary path copies a body four times between the kernel and the
caller; this copies it none.

**It spreads across connections.** `config.bulk_workers` connections each take
one span of the file and write it at its own offset with `pwrite`. Both
`recv_into` and `pwrite` release the GIL, so the workers genuinely overlap.

Measured against a stock `xrootd` 5.9.7 in Docker, 1 GiB, warm cache, client
and server on the same bridge network:

| | this client | `xrdcp` 5.6.9 |
| --- | --- | --- |
| copy to a local file | **1506 MiB/s** | 368 MiB/s |
| stream to a pipe | **1986 MiB/s** | 128 MiB/s |

The settings are `bulk_workers` (2), `bulk_chunk` (4 MiB) and `bulk_depth`
(4), each with an `XRD_BULK*` environment variable, and `config.bulk = False`
- or `XRD_BULK=0` - puts everything back on the event path, which is the
comparison to make when a result looks wrong.

None of this changes what a transfer means. A short read is still end of
file, and a transfer that ends short of the file's length is an error rather
than a quiet truncation. A server that answers a read with a wait or a
redirect rather than bytes puts that transfer back on the general pump, which
knows how to hold the conversation.

Losing the server is survivable. A worker that loses its connection re-opens
the file and resumes from its own high-water mark, so an outage costs the time
it lasts rather than the bytes already moved. What bounds the retrying is
`config.bulk_recovery` - two minutes by default - and it is a span of time
rather than a count of attempts, because what decides whether a job survives
is whether the server comes back before the client gives up. Every chunk that
arrives refills it, so a long transfer is never killed by the sum of the
outages it already survived, and the wait between attempts doubles up to five
seconds so that a server which has come back is used promptly.

The budget deliberately does not cover first contact. Until one request has
been answered there is nothing to distinguish a server that is restarting from
a host that does not exist, so a name that does not resolve fails at once
instead of spending the whole budget on it.

Measured against the same 1 GiB file, with the server stopped for ten seconds
part way through the transfer and then started again:

| | finished correctly | wall clock |
| --- | --- | --- |
| this client | yes | 21-23 s |
| brix-cache `xrdcp` | yes | 20 s |
| `xrdcp` 5.6.9 | yes | 127 s |

A mistyped host name, by contrast, fails in 0.4 s.

## What a transfer no longer pays for

Two costs were being paid by every caller and used by almost none.

**Importing the library.** `import xrdclient` used to load every cryptographic
primitive the protocol can need - AES, Blowfish, RSA, X.509 and the DER reader
- because one import of the request signer pulled in the whole `xrdclient.crypto`
package, and it loaded the in-memory test transport alongside the socket one.
Both packages now bind their names on first use, so a download loads what a
download uses. On this machine that is 121 modules imported where it was 146.

**Digesting a file nobody will check.** Verification compares a digest taken
while streaming against the server's own checksum. When the source is remote
that answer exists before the transfer does, so it is asked for first: a
server that cannot checksum is found out in one query rather than after a
gigabyte has been hashed for a comparison that cannot happen. Where the server
does answer, the digest is taken and compared exactly as before.

## Where the time goes

Three things carry the load:

**No copy that can be avoided.** Response bodies are sliced out of the receive
buffer through a `memoryview`, so a multi-megabyte read is frozen to `bytes`
exactly once. The obvious spellings - `bytes(buf[:n])`, `pending + body` -
each copy twice; profiling this benchmark is how they were found.

**One socket, many requests.** Requests on a connection are multiplexed by
stream ID, so they do not wait for each other's responses, and a `FileSystem`
holds its connection open for its whole life. A thousand-file dataset read
through one `FileSystem` - or one `fsspec` instance, which is cached by its
constructor arguments - costs one login. Constructing a fresh `FileSystem` per
file costs one login *in total* as well, because a closed connection goes into
the pool and the next `FileSystem` for the same server and the same credential
takes it back out ([Pooling](config.md#pooling)). Hoisting it out of the loop
is still the clearer code, and it is the version that also avoids re-resolving
the URL; the pool is there for the code you did not write, like a helper that
takes a URL and returns bytes.

**Reads are batched when you let them.** `readv` is one round trip for many
ranges; `pgread` gets per-page CRC32C from the server for free.

## Making it faster

```python
cfg = xrdclient.Config(
    chunk_size=8 << 20,        # bigger writes, fewer round trips
    readahead=4 << 20,         # buffered reads pull more per request
    parallel_chunks=8,         # spans of a copy moved at once, one connection each
    parallel_files=4,          # files of a tree copied at once
    in_flight=4,               # chunks read ahead of the write in flight
)
```

Defaults are 4 MiB, 1 MiB, 4, 1 and 2. On a high-latency WAN link raise all three;
on loopback they make no difference. `parallel_chunks` is what a single large
transfer is spread over, so it is the one that turns a WAN copy from
round-trip-bound into bandwidth-bound - at the cost of one login per span, and
of a verification that reads both files instead of digesting the stream
([Several connections at once](copying.md#several-connections-at-once)).
`parallel_files` is the other half of that trade: a tree of small files is
round-trip-bound however wide each transfer is allowed to be, and is the case
where raising it wins the most.

`in_flight` is the cheapest of them: one thread and a few buffers per
transfer, and it is what stops a read waiting for the write before it. Set it
to `1` for a local-to-local copy, where there is no latency for the overlap to
hide.

For many ranges from one file, ask once:

```python
with xrdclient.open(url, "rb") as fh:
    blocks = fh.raw.file.readv([(off, 128 << 10) for off in offsets])
```

For one file being streamed hard, give its bytes a socket of their own:

```python
handle = fh.raw.file
handle.bind_data_path()        # kXR_bind; see Files -> a second connection
```

The reads still go out on the control link and the data comes back on the new
one, so a `stat` in another thread is not queued behind a 64 MiB read. It
costs a connection and a handshake, which is why it is a call and not a
default.

For many files, go wide rather than deep - one session per worker thread, or
`asyncio.gather` over separate handles ([Asynchronous use](async.md)). A
single session serialises its own calls.

Turn off what you are not using:

```python
xrdclient.copy(src, dst, config=cfg.evolve(verify_checksums=False))
```

Checksum verification costs a server-side digest per file. It is on by default
because silent corruption is worse than slow, but for scratch data it is
wasted work.

## What is not fast

Anything that must touch every byte in Python - computing a checksum locally,
say - runs at Python speed. Ask the server for the digest instead
(`fs.checksum(path)`), which is what `--verify` does.
