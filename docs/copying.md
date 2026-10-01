# Copying

```python
xrdclient.copy(source, target, *, chunk_size=None, verify=None, algorithm=None,
         overwrite=True, progress=None, config=None, dry_run=False,
         remove_source=False) -> CopyResult
```

Either side may be a URL, a local path, an `xrdclient.Path`, or an already-open
binary file object. That covers every direction without a separate function
per case:

```python
xrdclient.copy("root://host//store/f.root", "/scratch/f.root")     # download
xrdclient.copy("/scratch/f.root", "root://host//store/f.root")     # upload
xrdclient.copy("root://a//store/f", "davs://b/store/f")            # across, via here
with open("/scratch/f", "wb") as fh:
    xrdclient.copy("root://host//store/f.root", fh)                # into a stream
```

The result says what happened:

```python
r = xrdclient.copy(src, dst)
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
xrdclient.copy(src, dst, verify=True, algorithm="crc32c")
```

Any name in `xrdclient.crypto.algorithms()` works, including the 64-bit CRCs a
gateway offers and stock XRootD has no calculator for.

A mismatch raises `ChecksumMismatchError`, which carries both digests.

!!! warning
    A checksum is an integrity check, not authentication. A server that
    serves you wrong bytes can serve you the matching digest. See
    [Security](security.md).

## Metalink sources

A local or remote source ending in `.meta4` or `.metalink` is a virtual
redirector, as it is in XrdCl. Metalink 3 and 4 descriptors are parsed with
bounded memory, replicas are tried in their declared priority order, and the
first supported declared checksum is verified while the bytes move:

```python
result = xrdclient.copy("dataset.meta4", "/scratch/dataset.root")
print(result.replica)  # the concrete URL that succeeded
```

`Config(metalink_processing=False)` copies the descriptor as an ordinary
file. `tls_metalink=True` upgrades `root` and `xroot` replicas to their TLS
forms. While another replica remains, `max_metalink_wait` limits how long a
busy server may answer `kXR_wait`; the last replica retains the normal
`wait_budget`.

Metalink and ZIP selection compose: `open_url`'s `xrdcl.unzip` selector (or
`xrd-cp --zip`) is applied to every archive replica. The descriptor checksum
is ignored for the selected member by default because it normally describes
the archive; opt in with `zip_metalink_checksum=True` or
`--zip-mtln-cksum` when the catalogue declares the member checksum instead.

## ZIP members and append

Read one member without downloading the archive around it:

```python
with xrdclient.open("root://host//store/bundle.zip", "rb", member="run/data.root") as f:
    header = f.read(4096)
```

Stored members are served directly by ranged reads. Deflated members are
inflated progressively into a bounded-memory, disk-spilling seek cache; full
reads verify size and CRC32. ZIP64, archive prefixes, comments and legacy
CP437 names are supported.

`append_zip` streams one stored member into a local or `root://` archive and
rewrites only its central-directory tail:

```python
xrdclient.append_zip("result.root", "root://host//store/results.zip")
```

Existing records and comments are preserved byte-for-byte. A failed local
append restores the old tail; an existing remote archive uses an XRootD
checkpoint. The command-line forms are `xrd-cp --zip MEMBER` and
`xrd-cp --zip-append`.

## Progress

```python
def bar(done, total):
    print(f"\r{done * 100 // total}%", end="")

xrdclient.copy(src, dst, progress=bar)
```

`total` is the size the source reported, which for a stream source may be
zero.

## Moving, and rehearsing

```python
xrdclient.copy(src, dst, dry_run=True)        # what it would be: size, nothing sent
xrdclient.copy(src, dst, remove_source=True)  # a move: the source goes after verify
```

`remove_source` deletes only once the copy has finished *and* verification has
passed, so a failed digest leaves the original where it was. `dry_run` returns
a `CopyResult` with the size the source reported and `seconds` of zero, which
is why its `str` leaves the rate off.

A completed regular local target is synced once before success is reported.
This makes a late `ENOSPC` or `EIO` from a FUSE cache, network filesystem, or
failing disk visible before verification and `remove_source`. The barrier is
outside the chunk loop, so it does not reduce streaming throughput; pipes,
sockets, and devices are not synced. The synced file's stable size must also
match the bytes copied, which catches a cache that acknowledges `fsync` without
publishing its writeback journal.

A read-only local source is reopened at the current proven offset after
transient `EAGAIN`, `EBUSY`, `EINTR`, `ESTALE`, or `ETIMEDOUT`, bounded by
`connect_retries` and `retry_backoff`. After the first fault only that reader
downshifts to 4 KiB requests, so a repeating bad-callback burst cannot starve a
large read while the healthy path stays unchanged. Reopening is accepted only
when device, inode, size, and nanosecond modification/change times still name
the same file generation; a replacement is failed with `ESTALE`, never spliced
onto bytes already copied.

## Resuming

A transfer that died half way through does not have to start again:

```python
xrdclient.copy(src, dst, resume=True)     # keep what is at dst, carry on from there
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
xrdclient.copy(src, dst, config=xrdclient.Config(in_flight=4))   # four chunks in hand
xrdclient.copy(src, dst, config=xrdclient.Config(in_flight=1))   # strictly one at a time
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
xrdclient.copy(src, dst, config=xrdclient.Config(parallel_chunks=8))   # eight spans
xrdclient.copy(src, dst, config=xrdclient.Config(parallel_chunks=1))   # one stream
```

From the command line that is `xrd-cp --stripes 8`. Its neighbour
`--streams` answers a different question — not how many spans of the file
move at once, but how many `kXR_bind` sub-streams each one rides; see
[`data_streams`](config.md) for what that binds and when it falls back.

It happens by itself, and only where it can pay. The target must be a local
file, because each worker opens it again to write its own span: dCache and
XRootD's Ceph backend treat a remote file as written once it is closed and
refuse the second open ("File already exists"), so an upload to `root://` is
one stream, as `xrdcp` writes it - and never an HTTP `PUT`. The source must
answer how long it is; and the file must be long enough
to give every worker a whole `chunk_size`, or the spans cost more in
connections than they save in round trips. Anything that fails those falls
back to the single stream, which is also what `parallel_chunks=1` asks for.

Spans arrive out of order, so - exactly as with `resume=` above - there is no
in-flight digest to verify against and the two files are compared instead.
`progress=` still counts the whole file: `done` is bytes moved across all the
workers, not a position in any one span.

## Several sources at once

A file with replicas on several servers can be read from more than one of
them together, as `xrdcp --sources N` does:

```python
xrdclient.copy("root://redirector//store/f.root", "/scratch/f.root", sources=4)
```

The redirector is asked where every replica is - a `kXR_locate` with
`kXR_compress | kXR_prefname`, managers followed down to the servers behind
them, which is XrdCl's `DeepLocate` - and up to `sources` of those servers are
read from at once. The file is handed out in blocks (at most 128 MiB, at least
one per reader and never less than a chunk), so a fast server comes back for
more while a slow one is still busy; a server that fails part way through a
block, or turns out to hold a shorter file, gives the rest of the block back
and its reader moves on to a replica nobody has tried. Only when every replica
has failed does the copy, with `NoMoreReplicasError`.

It needs a `root://` source and a target written at offsets - a local path or
`root://`; any other pair is copied from the one source as usual. Blocks
arrive out of order, so it verifies by comparing the two ends, like a spread
copy. It cannot continue a partial target: `sources` with `resume=True` raises
`NotImplementedError`, as XrdCl answers `errNotImplemented`.

## A source still being written

`dynamic_source=True` is `xrdcp --dynamic-src`: the source's size is neither
trusted nor checked, and it is read in order, one chunk at a time, until a
read comes back short. A file that grows while it is copied arrives with what
was there by then; one that shrank is not the error it otherwise is.

```python
xrdclient.copy("root://host//store/live.log", "/scratch/live.log", dynamic_source=True)
```

## Rate limits and deadlines

```python
xrdclient.copy(src, dst, max_rate=50 << 20)    # at most 50 MiB/s
xrdclient.copy(src, dst, min_rate=1 << 20)     # fail below 1 MiB/s
xrdclient.copy(src, dst, timeout=600)          # fail after ten minutes
```

These are XrdCl's `xrate`, `xrateThreshold` and `cpTimeout`, and are measured
the way XrdCl's classic copy measures them, as each chunk arrives:

- **`max_rate`** sleeps off whatever a chunk puts the transfer ahead of that
  many bytes a second since the data started to flow.
- **`min_rate`** fails the copy with `RateThresholdError` if the rate since
  the start has fallen below it - judged once every `config.in_flight + 1`
  chunks, XrdCl's `parallelChunks + 1`. It is a `TransientError`, as XrdCl
  retries it.
- **`timeout`** fails the copy with `CopyTimeoutError` (a `TimeoutError`) once
  it has run longer than that many seconds - checked before the data flows, as
  each chunk arrives and before verification. A single read that never
  answers is bounded by `config.request_timeout`, not by this.

Because they are per chunk, a copy with any of them is read as one stream of
`chunk_size` pieces (with `in_flight` read ahead) rather than over the bulk
plane or spread over connections; with `sources` they apply to the chunks of
every replica together.

## Ignoring file usage rules

`coerce=True` is `xrdcp --coerce`: a `root://` target is opened with
`kXR_force`, so the server ignores its usage rules - a stock `xrootd` refuses
to open a file for writing while another client has it open (`kXR_FileLocked`,
3003) unless forced. `third_party(..., coerce=True)` does the same for the
destination of a server-to-server copy.

## Recursive copies

```python
results = xrdclient.copy_tree("root://a//store/run7", "/scratch/run7")
print(sum(r.size for r in results))
```

Local destination directories are created as needed; remote ones come for
free, because a remote write asks for `kXR_mkpath`. Extra keyword arguments
are handed to `copy()` for each file.

### Several files at once

```python
xrdclient.copy_tree(src, dst, workers=8)          # eight transfers in flight
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
xrdclient.copy_tree(src, dst, exclude=("*.log", "tmp/*"))
xrdclient.copy_tree(src, dst, include=("*.root",), exclude=("bad/*",))
```

`fnmatch` patterns, matched against each path relative to the source root.
`include` is a whitelist - given one, nothing else travels - and `exclude`
wins over it.

### Only what has changed

```python
xrdclient.copy_tree(src, dst, sync="size")       # stat both sides
xrdclient.copy_tree(src, dst, sync="mtime")      # size, and no newer than the target
xrdclient.copy_tree(src, dst, sync="checksum")   # ask both endpoints for a digest
```

`sync` (a `SyncMode`) skips a file already at the target. Length is checked
first in every mode, because a different size settles it without a second
question. `checksum` is exact and costs a digest on both sides; `size` is one
stat each.

### Pruning the target

```python
xrdclient.copy_tree(src, dst, delete=True)
xrdclient.copy_tree(src, dst, delete=True, dry_run=True)   # says what it would remove
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
xrdclient.third_party("root://a//store/f.root", "root://b//store/f.root")
xrdclient.third_party("davs://a/store/f.root", "davs://b/store/f.root")
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

Over `root://` the rendezvous names the servers that actually hold each end,
as `XrdCl` does: `tpc.src` is the data server the source stat was redirected
to, and `tpc.dst` is the host the destination open landed on - not the
redirector in either URL, which the source would not recognise as the host
pulling from it.

`verify=True` (with an optional `algorithm=`) asks both servers for their
checksum once the transfer is done, since no byte passed through here to be
digested, and raises `ChecksumMismatchError` if they differ - or the server's
error if either end cannot answer.

`root://` takes `token_mode` (the delegation style), `posc`, `coerce`, and
two timeouts: `timeout` for the transfer itself and `init_timeout` for
everything before it - opening both ends and arming the pull, each step
checked as XrdCl's `initTimeout` is, raising `CopyTimeoutError`. HTTP takes
rather more, because the header set is the protocol:

```python
xrdclient.http.third_party(src, dst, mode="pull", overwrite=True, delegate=False,
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
    xrdclient.third_party(f"{src}?authz={read_token}", f"{dst}?authz={write_token}")
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
| `config.parallel_chunks` | connections a long copy is spread over, `1` to disable (`XRD_CPPARALLELSPANS`) |
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
$ xrd-cp --sources 4 root://redirector//store/f.root /scratch/   # four replicas at once
$ xrd-cp --xrate 50M --cptimeout 600 root://host//store/f.root /scratch/
```

See [the command line](cli.md#xrd-cp) for the flag table, including why a
trailing slash on the destination is what makes a repeated `-r` idempotent.
