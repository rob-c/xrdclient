# Compatibility cookbook

Complete programs for the jobs PyXRootD code usually does, written against
`xrdclient.compat.client`. Each one runs unchanged on the official bindings
with the first import changed back to `from XRootD import client`, which is
the point: these are the bindings' idioms, and they are also this library's.

Every recipe starts with the same two names; change them to your server and a
directory you can write to:

```python
SERVER = "root://eos.example.org"          # no trailing slash
BASE = "/store/user/me/cookbook"          # absolute path on that server
```

A file's URL is then `f"{SERVER}/{BASE}/name"` - `root://host//store/...`,
with the double slash XRootD uses for an absolute path.

Runnable versions of many of these, with a runner that checks them against
the official bindings, are in
[`examples/pyxrootd/`](https://github.com/rob-c/xrdclient/tree/main/examples/pyxrootd).

## Preparing a working directory

The later recipes assume `BASE` exists and holds a small text file:

```python
from xrdclient.compat import client
from xrdclient.compat.client.flags import MkDirFlags, OpenFlags

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"

fs = client.FileSystem(SERVER)
status, _ = fs.mkdir(BASE, MkDirFlags.MAKEPATH)
if not status.ok:
    raise SystemExit(status.message)

with client.File() as f:
    status, _ = f.open(f"{SERVER}/{BASE}/lines.txt", OpenFlags.DELETE)
    if not status.ok:
        raise SystemExit(status.message)
    f.write(b"".join(b"line %d\n" % i for i in range(1000)))
```

`OpenFlags.DELETE` creates the file, or truncates it if it exists;
`OpenFlags.NEW` fails if it exists.

## Read a file in chunks

`readchunks` yields `bytes` until the end of the file, reading `chunksize` at
a time:

```python
import hashlib

from xrdclient.compat import client

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"

digest = hashlib.sha256()
with client.File() as f:
    status, _ = f.open(f"{SERVER}/{BASE}/lines.txt")
    if not status.ok:
        raise SystemExit(status.message)
    for chunk in f.readchunks(offset=0, chunksize=4 * 1024 * 1024):
        digest.update(chunk)
print(digest.hexdigest())
```

The same by hand, when you need the offsets - `read` takes the offset first,
and an empty buffer means the end:

```python
from xrdclient.compat import client

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"

CHUNK = 1024 * 1024
total = 0
with client.File() as f:
    f.open(f"{SERVER}/{BASE}/lines.txt")
    offset = 0
    while True:
        status, data = f.read(offset, CHUNK)
        if not status.ok:
            raise SystemExit(status.message)
        if not data:
            break
        total += len(data)
        offset += len(data)
print(total, "bytes")
```

Text files can be read a line at a time, by iterating the file or with
`readline`. Both use a cursor of the file's own, which `read` does not move:

```python
from xrdclient.compat import client

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"

with client.File() as f:
    f.open(f"{SERVER}/{BASE}/lines.txt")
    for number, line in enumerate(f):
        if number == 3:
            break
        print(line, end="")
```

## Vector reads

One request for many scattered ranges - what uproot does for a ROOT file's
baskets:

```python
from xrdclient.compat import client

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"

ranges = [(0, 7), (70, 7), (700, 8)]           # (offset, length)
with client.File() as f:
    f.open(f"{SERVER}/{BASE}/lines.txt")
    status, answer = f.vector_read(ranges)
    if not status.ok:
        raise SystemExit(status.message)
    print(answer.size, "bytes in", len(answer.chunks), "chunks")
    for chunk in answer:                        # ChunkInfo, in the order asked
        print(chunk.offset, chunk.length, chunk.buffer)
```

A server limits how many ranges one request may carry, and how long each may
be; `fs.query(QueryCode.CONFIG, "readv_iov_max readv_ior_max")` asks it.

## Write and append

```python
from xrdclient.compat import client
from xrdclient.compat.client.flags import AccessMode, OpenFlags

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"
url = f"{SERVER}/{BASE}/written.dat"

# Create (or replace), with explicit permissions, and write at offsets.
mode = AccessMode.UR | AccessMode.UW | AccessMode.GR
with client.File() as f:
    status, _ = f.open(url, OpenFlags.DELETE, mode)
    if not status.ok:
        raise SystemExit(status.message)
    f.write(b"header\n", offset=0)
    f.write(b"body\n", offset=7)
    status, _ = f.sync()

# Append: open for update, find the end, write there.
with client.File() as f:
    f.open(url, OpenFlags.UPDATE)
    status, info = f.stat(force=True)
    f.write(b"appended\n", offset=info.size)

with client.File() as f:
    f.open(url)
    print(f.read()[1])            # b'header\nbody\nappended\n'
```

`write` takes `bytes` or `str` (written as UTF-8). Opening with `NEW` or
`DELETE` creates missing parent directories, as it does with the bindings;
`OpenFlags.MAKEPATH` says so explicitly. `OpenFlags.POSC` makes the file
disappear unless it is closed successfully - a half-written output never
looks finished.

## List a tree recursively

`DirListFlags.RECURSIVE` lists everything below a directory in one call,
naming each entry by its path below it; with `STAT` each entry carries a
`StatInfo`:

```python
from xrdclient.compat import client
from xrdclient.compat.client.flags import DirListFlags, StatInfoFlags

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"

fs = client.FileSystem(SERVER)
status, listing = fs.dirlist(BASE, DirListFlags.STAT | DirListFlags.RECURSIVE)
if not status.ok:
    raise SystemExit(status.message)

total = 0
for entry in listing:
    is_dir = entry.statinfo.flags & StatInfoFlags.IS_DIR
    if not is_dir:
        total += entry.statinfo.size
    print("d" if is_dir else "-", f"{entry.statinfo.size:>10}", entry.name)
print(listing.size, "entries,", total, "bytes in files")
```

A recursive listing of a very large tree is built in memory in full before
the call returns. To stop early, or to act on each directory as it arrives,
walk it yourself:

```python
from xrdclient.compat import client
from xrdclient.compat.client.flags import DirListFlags, StatInfoFlags

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"

fs = client.FileSystem(SERVER)


def walk(top):
    status, listing = fs.dirlist(top, DirListFlags.STAT)
    if not status.ok:
        print("cannot list", top, status.message.strip())
        return
    for entry in listing:
        path = f"{listing.parent}{entry.name}"
        if entry.statinfo.flags & StatInfoFlags.IS_DIR:
            yield from walk(path)
        else:
            yield path, entry.statinfo.size


for path, size in walk(BASE):
    print(size, path)
```

Or, a call at a time towards the native API, `fs.native.walk(BASE)` does the
same as `os.walk`.

## Checksums

The server computes the checksum; `query` returns its raw answer, the
algorithm and the value separated by a space:

```python
from xrdclient.compat import client
from xrdclient.compat.client.flags import QueryCode

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"

fs = client.FileSystem(SERVER)
status, answer = fs.query(QueryCode.CHECKSUM, f"{BASE}/lines.txt")
if not status.ok:
    raise SystemExit(status.message)
algorithm, value = answer.decode().strip("\x00\n ").split()
print(algorithm, value)                            # adler32 c9e96f74

# A particular algorithm, if the server is configured with it:
status, answer = fs.query(QueryCode.CHECKSUM, f"{BASE}/lines.txt?cks.type=crc32")
```

To compute an adler32 locally, for comparison: `zlib.adler32(data)` and
format it with `f"{value:08x}"`.

## Staging from tape

`prepare` asks the server to bring files online; the answer is a request
handle. `StatInfoFlags.OFFLINE` says whether a file is still on tape only:

```python
import time

from xrdclient.compat import client
from xrdclient.compat.client.flags import PrepareFlags, StatInfoFlags

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"
files = [f"{BASE}/lines.txt"]

fs = client.FileSystem(SERVER)
status, handle = fs.prepare(files, PrepareFlags.STAGE)
if not status.ok:
    raise SystemExit(status.message)
print("request", handle.decode())

deadline = time.monotonic() + 3600
pending = list(files)
while pending and time.monotonic() < deadline:
    for path in list(pending):
        status, info = fs.stat(path)
        if status.ok and not info.flags & StatInfoFlags.OFFLINE:
            pending.remove(path)
    if pending:
        time.sleep(30)
print("still offline:", pending)
```

`PrepareFlags.EVICT` drops the disk copy again. On a disk-only server
`prepare` succeeds and nothing happens.

## The Tape REST API

`client.tape.TapeClient` is upstream's newer client for the WLCG Tape REST
API, the one FTS and Rucio use. Give it the storage element's `https://` or
`davs://` URL; every call returns `(status, response)` like the rest of the
bindings. (The installed 6.1 bindings do not have it yet, so this recipe
needs a newer upstream release there.)

```python
import time

from xrdclient.compat import client

ENDPOINT = "davs://tape.example.org:8443"
files = [f"{ENDPOINT}/store/user/me/raw/run7.root", "/store/user/me/raw/run8.root"]

tapes = client.TapeClient(timeout=60)
status, endpoint = tapes.discover(ENDPOINT)
print(status.ok and endpoint.uri, status.ok and endpoint.sitename)

status, request = tapes.stage(
    ENDPOINT, files, disk_lifetime=86400,        # seconds, or "P1D"
    targeted_metadata={"my-site": {"activity": "reprocessing"}},
)
status.raise_on_error()
print("request", request.request_id)

while True:
    status, progress = tapes.stage_status(ENDPOINT, request.request_id)
    status.raise_on_error()
    waiting = [f.path for f in progress.files if not f.on_disk]
    if not waiting:
        break
    print("waiting for", waiting)
    time.sleep(60)

tapes.release(ENDPOINT, request.request_id, files)   # done with the disk copies

status, infos = tapes.archive_info(files)
for info in infos:
    print(info.url, info.locality or info.error)
```

A file entry can also be a dict - `{"path": ..., "diskLifetime": "PT1H",
"targeted_metadata": {...}}` - to give one file its own settings, and
`tapes.stage([{"url": ...}, ...])` takes the endpoint from the first URL.
`stage_cancel(url, request_id, paths)` withdraws some files from a request,
`stage_delete(url, request_id)` the whole request. Given a `root://` URL, the
client makes the same `prepare` and `query` calls on that server that the
bindings' does, and the server's own tape support answers them.

## Extended attributes

Values are strings; each name gets its own status, as a `dict`:

```python
from xrdclient.compat import client

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"
path = f"{BASE}/lines.txt"

fs = client.FileSystem(SERVER)
status, results = fs.set_xattr(path, [("user.run", "7"), ("user.owner", "me")])
for name, each in results:
    print("set", name, each["ok"])

status, values = fs.get_xattr(path, ["user.run", "user.absent"])
for name, value, each in values:
    print(name, repr(value), "ok" if each["ok"] else f"errno {each['errno']}")

status, everything = fs.list_xattr(path)
print({name: value for name, value, _ in everything})

fs.del_xattr(path, ["user.run", "user.owner"])
```

A missing attribute comes back with the value `""` and `errno` 3027
(`kXR_AttrNotFound`) in its own status; the overall status is still OK. The
same four methods exist on an open `File`, without the path.

## CopyProcess with a progress bar

A handler only has to have the methods it wants called:

```python
import sys

from xrdclient.compat import client

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"


class Progress(client.utils.CopyProgressHandler):
    def begin(self, jobId, total, source, target):
        self.job, self.jobs = jobId, total
        print(f"[{jobId}/{total}] {source} -> {target}")

    def update(self, jobId, processed, total):
        if total:
            width = 40
            done = width * processed // total
            bar = "#" * done + "." * (width - done)
            sys.stdout.write(f"\r  {bar} {100 * processed // total:3d}%")
            sys.stdout.flush()

    def end(self, jobId, results):
        status = results["status"]
        print()
        print("  ok" if status.ok else f"  failed: {status.message.strip()}")
        if "sourceCheckSum" in results:
            print("  checksum", results["sourceCheckSum"])

    def should_cancel(self, jobId):
        return False                       # return True to stop this job


process = client.CopyProcess()
process.add_job(f"{SERVER}/{BASE}/lines.txt", "/tmp/cookbook/lines.txt",
                force=True, mkdir=True, checksummode="end2end")
process.add_job(f"{SERVER}/{BASE}/written.dat", "/tmp/cookbook/written.dat",
                force=True)                  # the file itself: its directory is made
process.parallel(2)

status = process.prepare()
if not status.ok:
    raise SystemExit(status.message)
status, results = process.run(Progress())
print("all ok" if status.ok else status.message)
```

With `parallel(n)` above one, the handler is called from several threads at
once and the lines above interleave; keep per-job state in a dict keyed by
`jobId` if that matters. `tqdm` works the same way: create a bar in `begin`,
set `bar.total` and `bar.n` in `update`, close it in `end`.

## Asynchronous calls, and waiting for many

Pass `callback=` and the call returns a status at once; the callback runs
later on a worker thread with `(status, response, hostlist)`.
`AsyncResponseHandler` is a callback that can be waited for:

```python
from xrdclient.compat import client

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"

fs = client.FileSystem(SERVER)
handler = client.utils.AsyncResponseHandler()
submitted = fs.stat(f"{BASE}/lines.txt", callback=handler)
assert submitted.ok                       # it was sent, not that it worked
status, info, hosts = handler.wait()
print(status.ok, info.size, [str(h.url) for h in hosts])
```

For many calls, collect one handler each and wait for all of them:

```python
from xrdclient.compat import client

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"

fs = client.FileSystem(SERVER)
paths = [f"{BASE}/lines.txt", f"{BASE}/written.dat", f"{BASE}/missing"]

handlers = {}
for path in paths:
    handlers[path] = client.utils.AsyncResponseHandler()
    fs.stat(path, callback=handlers[path])

for path, handler in handlers.items():
    status, info, _ = handler.wait()
    print(path, info.size if status.ok else status.message.strip())
```

Or with a plain function and a counter, when there is nothing to collect:

```python
import threading

from xrdclient.compat import client

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"

fs = client.FileSystem(SERVER)
paths = [f"{BASE}/lines.txt", f"{BASE}/written.dat"]
remaining = len(paths)
lock, done = threading.Lock(), threading.Event()
sizes = {}


def stat_done(status, info, hostlist, path):
    global remaining
    with lock:
        sizes[path] = info.size if status.ok else None
        remaining -= 1
        if remaining == 0:
            done.set()


for path in paths:
    fs.stat(path, callback=lambda s, r, h, path=path: stat_done(s, r, h, path))
if not done.wait(timeout=60):
    raise SystemExit("gave up waiting")
print(sizes)
```

Callbacks run on a pool of eight worker threads shared by the process. A
callback that blocks for a long time holds one of them; hand long work to
your own thread or queue.

## Timeouts

`timeout=` is whole seconds. When it runs out, the call returns
`errOperationExpired` (code 206):

```python
from xrdclient.compat import client

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"

fs = client.FileSystem(SERVER)
status, info = fs.stat(f"{BASE}/lines.txt", timeout=10)
if status.code == 206:
    print("no answer within 10 s")
elif not status.ok:
    print(status.message)
```

The request is not recalled when the caller stops waiting - XrdCl cannot do
that either - so a timed-out write may still land. A `float` is a
`TypeError`, as with the bindings: write `timeout=10`, not `timeout=10.0`.

To change the default for every call, set the environment before the program
starts, or put the setting before creating the objects:

```console
$ XRD_REQUESTTIMEOUT=60 XRD_CONNECTIONWINDOW=15 python analysis.py
```

```python
from xrdclient.compat import client

client.EnvPutInt("RequestTimeout", 60)       # objects created from now on
client.EnvPutInt("ConnectionWindow", 15)
fs = client.FileSystem("root://eos.example.org")
```

## Handling errors

Every failure the server or the network causes is a status, never an
exception. Three numbers identify it: `code` is XrdCl's category (400 means
"the server said no"), `errno` is the server's own `kXR_*` number within 400,
and `shellcode` is what a command-line tool would exit with:

```python
from xrdclient.compat import client

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"

kXR_NotAuthorized, kXR_NotFound, kXR_ItExists = 3010, 3011, 3018

fs = client.FileSystem(SERVER)
status, info = fs.stat(f"{BASE}/missing")
if status.ok:
    print(info.size)
elif status.code == 400 and status.errno == kXR_NotFound:
    print("no such file")
elif status.code == 400 and status.errno == kXR_NotAuthorized:
    print("permission denied")
elif status.fatal:
    print("connection or login failed:", status.message.strip())
else:
    print("failed:", status.message.strip())
```

A script that wants to exit as `xrdcp` would:

```python
import sys

from xrdclient.compat import client

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"

fs = client.FileSystem(SERVER)
status, _ = fs.mkdir(f"{BASE}/lines.txt")        # a file is already there
if not status.ok:
    print(status.message.strip(), file=sys.stderr)
    sys.exit(status.shellcode)                    # 54 for a server refusal
```

### Raising instead of checking

Newer upstream releases can turn a failed status into an exception, and so
can this one: `status.raise_on_error()` returns the status when it is OK
and otherwise raises an `XRootDError` subclass chosen from `code` - and,
for a server's refusal, from its `errno`:

```python
from xrdclient.compat import client

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"

fs = client.FileSystem(SERVER)
try:
    status, info = fs.stat(f"{BASE}/missing")
    status.raise_on_error()
except client.XRootDNotFoundError as exc:
    print("no such file:", exc.status.errno)          # 3011
except client.XRootDAuthorizationError:
    print("permission denied")                        # 3010, 3030, auth/login
except client.XRootDTimeoutError:
    print("timed out")                                # socket or operation expiry
except client.XRootDError as exc:                     # checksum, anything else
    print(exc.status.error_name, exc)                 # "errErrorResponse", message
```

`status.exception()` returns that exception without raising it (`None` for
success), and `client.raise_on_error(status)` does the same as the method
while also accepting the plain status dicts some calls hand out. Every one
is a `RuntimeError`, so an existing `except RuntimeError` still catches them.

### Mistakes in the calling code

Mistakes in the calling code still raise, exactly as in the bindings: a
wrong type is `TypeError`, a number that does not fit is `OverflowError`, and
I/O on a `File` that is not open is `ValueError`. The
[status code table](compat-reference.md#status-codes) lists every `code` and
the common `errno` values.

A failed `open` leaves the `File` object finished with: every later `open`
and `close` on it returns the same failure - whether the failure came back
directly, to a `callback`, or as `errOperationExpired` from a `timeout`. An
open that times out is not recalled from the server, but if it succeeds after
all, its handle is closed rather than attached to the `File`. Make a new
`File` to retry.

## Using it from threads

`FileSystem` and `File` objects can be shared between threads, and calls on
them from several threads at once are safe:

```python
from concurrent.futures import ThreadPoolExecutor

from xrdclient.compat import client

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"

fs = client.FileSystem(SERVER)
names = ["lines.txt", "written.dat"]


def size_and_head(name):
    status, info = fs.stat(f"{BASE}/{name}")
    if not status.ok:
        return name, None, None
    with client.File() as f:
        f.open(f"{SERVER}/{BASE}/{name}")
        _, head = f.read(0, 16)
    return name, info.size, head


with ThreadPoolExecutor(max_workers=8) as pool:
    for name, size, head in pool.map(size_and_head, names):
        print(name, size, head)
```

Files opened on the same server share one connection, as they do in XrdCl,
so a pool of threads each opening its own file does not open a connection per
thread. `readline` and iteration keep one cursor per `File`; do not iterate
the same `File` from two threads. `read(offset, size)` names its offset, so
concurrent reads of one `File` are fine.

## uproot and fsspec

uproot's XRootD handler imports `XRootD.client`; `install()` answers that
import with this package:

```python
import xrdclient.compat

xrdclient.compat.install()                  # before uproot imports XRootD

import uproot

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"

with uproot.open(f"{SERVER}/{BASE}/ntuple.root:Events",
                 handler=uproot.XRootDSource) as tree:
    print(tree.num_entries)
    arrays = tree.arrays(["Muon_pt", "Muon_eta"], entry_stop=1000)
```

Without `handler=`, uproot uses fsspec, and fsspec's `root://` is this
library's native filesystem when this package is installed - no bindings, no
`install()`:

```python
import fsspec

SERVER = "root://eos.example.org"
BASE = "/store/user/me/cookbook"

with fsspec.open(f"{SERVER}/{BASE}/lines.txt", "rb") as fh:
    print(fh.read(16))
```

See [fsspec](fsspec.md) for the filesystem itself, and
[Replacing PyXRootD](porting.md#uproot-and-coffea) for coffea and
fsspec-xrootd.

## Bearer tokens

The `ztn` mechanism finds a token where the C++ client does:
`$BEARER_TOKEN`, then the file named by `$BEARER_TOKEN_FILE`, then
`$XDG_RUNTIME_DIR/bt_u$UID`, then `/tmp/bt_u$UID`. Set one of them before the
objects are created:

```console
$ export BEARER_TOKEN_FILE=$HOME/.tokens/eos.jwt
$ python analysis.py
```

```python
import os

from xrdclient.compat import client

os.environ["BEARER_TOKEN_FILE"] = os.path.expanduser("~/.tokens/eos.jwt")

fs = client.FileSystem("roots://eos.example.org")   # TLS: see below
status, info = fs.stat("/store/user/me/cookbook/lines.txt")
print(status.message if not status.ok else info.size)
```

A token is only offered over TLS - `roots://`, or a server that switches the
connection to TLS - because a token sent in the clear can be replayed by
anyone who sees it. An expired JWT fails at once, with the expiry time in the
message, rather than being sent.

Some services expect the token in the URL instead, as `?authz=`:

```python
from urllib.parse import quote

from xrdclient.compat import client

token = open("/run/user/1000/bt_u1000").read().strip()
url = f"roots://eos.example.org//store/user/me/cookbook/lines.txt?authz={quote('Bearer ' + token)}"

with client.File() as f:
    status, _ = f.open(url)
    print(status.message if not status.ok else f.read(0, 16)[1])
```

The parameter is passed to the server as the path's opaque data, exactly as
written. It is redacted from every log record.

## GSI proxies

GSI reads `$X509_USER_PROXY`, or `/tmp/x509up_u$UID`, and the CA
certificates in `$X509_CERT_DIR` (default `/etc/grid-security/certificates`).
Make a proxy the usual way and nothing in the code changes:

```console
$ voms-proxy-init -voms atlas
$ python analysis.py
```

To check the proxy from the program before it spends an hour failing:

```python
import os

from xrdclient.compat import client
from xrdclient.crypto.x509 import load_proxy

path = os.environ.get("X509_USER_PROXY", f"/tmp/x509up_u{os.getuid()}")
proxy = load_proxy(path)
hours = proxy.remaining() / 3600
if hours < 1:
    raise SystemExit(f"{proxy.identity}: proxy has {hours:.1f} h left; renew it")

fs = client.FileSystem("root://eos.example.org")
status, info = fs.stat("/store/user/me/cookbook/lines.txt")
print(status.message if not status.ok else info.size)
```

An expired proxy fails before the round trip, with a status whose message
says when it expired. GSI's signed Diffie-Hellman variant and X.509
delegation are not implemented; a server that insists on them is refused by
name. See [Authentication](auth.md#gsi-x509-proxies).

## Kerberos

Pure Python: `kinit`, then run the program. `FILE:`, `DIR:`, `KCM:` (RHEL
9's default) and Linux `KEYRING:` caches are all read; on macOS, whose `API:`
cache is out of reach, point `kinit` at a file:

```console
$ kinit jane@EXAMPLE.ORG
$ python analysis.py                    # KCM:, KEYRING:, FILE: - whatever kinit used
$ export KRB5CCNAME=FILE:/tmp/krb5cc_$(id -u)   # macOS only
```

```python
from xrdclient.compat import client

fs = client.FileSystem("root://eos.example.org")
status, info = fs.stat("/store/user/me/cookbook/lines.txt")
if status.code == 204:                  # errAuthFailed: every mechanism refused
    print(status.message)               # names each mechanism and why
else:
    print(info.size if status.ok else status.message)
```

If the server asks for a forwarded ticket, get a forwardable one with
`kinit -f`. The supported enctypes, KDC lookup rules and other limits are in
[Authentication](auth.md#krb5-kerberos).
