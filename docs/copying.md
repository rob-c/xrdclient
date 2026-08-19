# Copying

```python
xrd.copy(source, target, *, chunk_size=None, verify=None, algorithm=None,
         overwrite=True, progress=None, config=None, dry_run=False,
         remove_source=False) -> CopyResult
```

Either side may be a URL, a local path, an `xrd.Path`, or an already-open
binary file object. That covers every direction without a separate function
per case:

```python
xrd.copy("root://host//store/f.root", "/scratch/f.root")     # download
xrd.copy("/scratch/f.root", "root://host//store/f.root")     # upload
xrd.copy("root://a//store/f", "davs://b/store/f")            # across, via here
with open("/scratch/f", "wb") as fh:
    xrd.copy("root://host//store/f.root", fh)                # into a stream
```

The result says what happened:

```python
r = xrd.copy(src, dst)
r.size, r.seconds, r.rate, r.checksum, r.verified
print(r)   # root://... -> /scratch/f.root (4194304 bytes, 212.4 MB/s)
```

## Verification

`verify` defaults to `config.verify_checksums`, which is on. A digest is
computed while the bytes stream past and compared against the server's own
checksum - of the target when the target is remote, otherwise of the source.
A server that cannot checksum degrades quietly; `verify=True` makes that an
error instead.

```python
xrd.copy(src, dst, verify=True, algorithm="crc32c")
```

Any name in `xrd.crypto.algorithms()` works, including the 64-bit CRCs a
gateway offers and stock XRootD has no calculator for.

A mismatch raises `ChecksumMismatchError`, which carries both digests.

!!! warning
    A checksum is an integrity check, not authentication. A server that
    serves you wrong bytes can serve you the matching digest. See
    [Security](security.md).

## Progress

```python
def bar(done, total):
    print(f"\r{done * 100 // total}%", end="")

xrd.copy(src, dst, progress=bar)
```

`total` is the size the source reported, which for a stream source may be
zero.

## Moving, and rehearsing

```python
xrd.copy(src, dst, dry_run=True)        # what it would be: size, nothing sent
xrd.copy(src, dst, remove_source=True)  # a move: the source goes after verify
```

`remove_source` deletes only once the copy has finished *and* verification has
passed, so a failed digest leaves the original where it was. `dry_run` returns
a `CopyResult` with the size the source reported and `seconds` of zero, which
is why its `str` leaves the rate off.

## Resuming

A transfer that died half way through does not have to start again:

```python
xrd.copy(src, dst, resume=True)     # keep what is at dst, carry on from there
```

Whatever is already at the target is kept and the copy begins at the end of
it. `CopyResult.resumed_at` is the offset it started from and `size` is what
this call moved, so `resumed_at + size` is the finished length either way. A
target that is not there yet - or is empty - is copied whole, which is what
makes the flag safe to set unconditionally on a retry.

Two things follow from having skipped the beginning of the file. Verification
can no longer digest the bytes in flight, because they are only the tail, so a
resumed copy compares the two files afterwards instead: one digest from each
end, which costs a read of whichever end is local. And an HTTP target cannot
be resumed at all - a `PUT` replaces the whole resource - so that raises
`UnsupportedError` rather than quietly re-uploading.

A target *longer* than its source is not a partial copy of it, and says so
with a `ValueError` instead of appending to something unrelated. So does
`resume=True` with `overwrite=False`, which asks to continue a file that is
forbidden to exist.

`copy_tree(..., resume=True)` passes the flag down to every file, which
finishes an interrupted tree rather than recopying it - `sync=` skips files
that are already complete, `resume=` finishes the one that was in flight.

## Reading ahead of the writes

A read and a write are both round trips, and a loop that does them strictly in
turn spends each one waiting out the other. Every transfer therefore reads
`config.in_flight` chunks ahead of the write that is out, on a thread of its
own, so the copy goes at the slower of its two ends rather than at their sum:

```python
xrd.copy(src, dst, config=xrd.Config(in_flight=4))   # four chunks in hand
xrd.copy(src, dst, config=xrd.Config(in_flight=1))   # strictly one at a time
```

It costs `in_flight` buffers of `chunk_size` and one thread per transfer, which
is why `1` is worth asking for between two local files, where there is no
latency to hide. The chunks are written in the order they were read, so the
digest a verified copy computes is still the file's.

## Several connections at once

A copy big enough to be worth it is moved by more than one connection: the
file is cut into `config.parallel_chunks` contiguous spans and each span is
carried by a worker of its own. It is *connections* rather than requests
because a session serialises its own calls - two spans are only ever in flight
together if there are two sessions to put them on.

```python
xrd.copy(src, dst, config=xrd.Config(parallel_chunks=8))   # eight spans
xrd.copy(src, dst, config=xrd.Config(parallel_chunks=1))   # one stream
```

From the command line that is `xrd-cp --stripes 8`. Its neighbour
`--streams` answers a different question — not how many spans of the file
move at once, but how many `kXR_bind` sub-streams each one rides; see
[`data_streams`](config.md) for what that binds and when it falls back.

It happens by itself, and only where it can pay. The target must be one that
takes a write at an offset, so a local path or `root://` but never an HTTP
`PUT`; the source must answer how long it is; and the file must be long enough
to give every worker a whole `chunk_size`, or the spans cost more in
connections than they save in round trips. Anything that fails those falls
back to the single stream, which is also what `parallel_chunks=1` asks for.

Spans arrive out of order, so - exactly as with `resume=` above - there is no
in-flight digest to verify against and the two files are compared instead.
`progress=` still counts the whole file: `done` is bytes moved across all the
workers, not a position in any one span.

## Recursive copies

```python
results = xrd.copy_tree("root://a//store/run7", "/scratch/run7")
print(sum(r.size for r in results))
```

Local destination directories are created as needed; remote ones come for
free, because a remote write asks for `kXR_mkpath`. Extra keyword arguments
are handed to `copy()` for each file.

### Several files at once

```python
xrd.copy_tree(src, dst, workers=8)          # eight transfers in flight
```

`workers` files are copied at once, defaulting to `config.parallel_files`
and, at `1`, to one after another. Raise it for a tree of small files, where
each transfer is a round trip and none is long enough to be spread over
connections of its own; a tree of large files is already busy, because each
of those is divided as above.

Results come back in the order the walk found them however many workers there
were, and the first failure is raised as it would be one at a time - whatever
has not started is cancelled rather than left to copy on behind the
exception. While more than one file is in flight, `progress` is called with
the bytes moved across the whole tree and a total of `None`, since interleaved
per-file positions would not add up to anything.

### Choosing what travels

```python
xrd.copy_tree(src, dst, exclude=("*.log", "tmp/*"))
xrd.copy_tree(src, dst, include=("*.root",), exclude=("bad/*",))
```

`fnmatch` patterns, matched against each path relative to the source root.
`include` is a whitelist - given one, nothing else travels - and `exclude`
wins over it.

### Only what has changed

```python
xrd.copy_tree(src, dst, sync="size")       # stat both sides
xrd.copy_tree(src, dst, sync="mtime")      # size, and no newer than the target
xrd.copy_tree(src, dst, sync="checksum")   # ask both endpoints for a digest
```

`sync` (a `SyncMode`) skips a file already at the target. Length is checked
first in every mode, because a different size settles it without a second
question. `checksum` is exact and costs a digest on both sides; `size` is one
stat each.

### Pruning the target

```python
xrd.copy_tree(src, dst, delete=True)
xrd.copy_tree(src, dst, delete=True, dry_run=True)   # says what it would remove
```

`delete` removes files under the target that the source does not have. What
an `include`/`exclude` hid was never a candidate, so it is never deleted
either - filtering the source does not mean emptying the target.

Server-supplied names are validated before they are joined onto your
destination - a listing entry containing `/` or equal to `..` is refused
outright, so a hostile endpoint cannot steer a recursive download out of the
directory you named.

## Third-party copy

```python
xrd.third_party("root://a//store/f.root", "root://b//store/f.root")
xrd.third_party("davs://a/store/f.root", "davs://b/store/f.root")
```

The data moves between the two servers and never through this process. One
call, two dialects: the URLs decide which one is spoken.

| Endpoints | Dialect |
| --- | --- |
| two `root://` | the `XrdOucTPC` rendezvous - a key minted here, `tpc.src`/`tpc.dst` opaque, `kXR_sync` to trigger and to wait |
| two `http(s)`/`dav(s)` | WLCG `COPY`, the one FTS and Rucio use |

Both endpoints must speak the same protocol, because each dialect is one
server asking another for the file in a language it understands. A mixed
pair raises, and `copy()` is the answer - it streams through this process,
which is what a mixed pair needs anyway.

`root://` takes `token_mode` (the delegation style) and `posc`. HTTP takes
rather more, because the header set is the protocol:

```python
xrd.http.third_party(src, dst, mode="pull", overwrite=True, delegate=False,
                     verify=None, streams=None, remote_token=None,
                     transfer_headers={}, progress=None, timeout=None)
```

- **`mode`** - `"pull"` sends the `COPY` to the destination with a `Source:`
  header, which is what almost everything does. `"push"` sends it to the
  source with a `Destination:` header, for a destination that cannot make
  outbound connections.
- **the far side's token** travels in `TransferHeaderAuthorization`, and is
  taken from that URL's `authz` parameter first, so a pair of pre-signed URLs
  needs nothing else:

    ```python
    xrd.third_party(f"{src}?authz={read_token}", f"{dst}?authz={write_token}")
    ```

  The token is stripped from the URL the far side is given, since it belongs
  in the header the transfer authorises rather than in the other endpoint's
  request log.
- **`delegate=False`** sends `Credential: none`, which is what stops a server
  that supports X.509 delegation from waiting for a credential a
  token-authenticated transfer will never send.
- **`verify`** sets `RequireChecksumVerification`; left as `None` it says
  nothing and the server's own policy stands.
- **`progress`** is called with the running byte count from the performance
  markers, so a long transfer can be watched even though nothing is
  streaming through here.

The one thing to know about HTTP third-party copy is that a `202 Accepted`
means the copy *started*. The outcome is the last line of the response body,
after the performance markers, and a transfer that failed still arrived as a
`202`. This client reads to that line before returning, and turns a
`failure:` into the exception the status it quotes deserves - a source that
answers 403 raises `PermissionError`, exactly as a direct read would.

For a copy where the data must pass through you anyway, `copy()` is both
simpler and, on a fast network, not obviously slower - see
[Performance](performance.md).

## Tuning

| Setting | Effect |
| --- | --- |
| `config.chunk_size` | bytes per request, default 4 MiB (`XRD_CPCHUNKSIZE`) |
| `config.in_flight` | chunks read ahead of the write, `1` to disable (`XRD_CPINFLIGHT`) |
| `config.parallel_chunks` | connections a long copy is spread over, `1` to disable (`XRD_CPPARALLELCHUNKS`) |
| `config.parallel_files` | files of a tree copied at once (`XRD_CPPARALLELFILES`) |
| `config.verify_checksums` | default for `verify` |
| `config.preferred_checksum` | default for `algorithm` |

## From the command line

```console
$ xrd-cp /tmp/f.root root://host//store/f.root
$ xrd-cp -r /tmp/results davs://dav.example.org/store/results
$ xrd-cp --tpc root://a//store/f.root root://b//store/f.root
$ xrd-cp --tpc davs://a/store/f.root davs://b/store/f.root
$ xrd-cp --no-verify --progress root://host//store/big.root /scratch/
$ xrd-cp -r --sync size --delete /tmp/results root://host//store/results/
$ xrd-cp -r --dry-run --exclude '*.log' /tmp/results root://host//store/results/
$ xrd-cp --remove-source /tmp/f.root root://host//store/f.root
$ xrd-cp -c root://host//store/big.root /scratch/big.root   # carry on
$ xrd-cp --stripes 8 root://host//store/big.root /scratch/   # eight spans at once
$ xrd-cp --streams 2 root://host//store/big.root /scratch/   # two links per span
```

See [the command line](cli.md#xrd-cp) for the flag table, including why a
trailing slash on the destination is what makes a repeated `-r` idempotent.
