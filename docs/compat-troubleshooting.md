# Compatibility troubleshooting

Questions that come up when code written for `XRootD.client` runs on
`xrdclient.compat.client`, with the answer to each. For what every call does,
see the [reference](compat-reference.md); for the port itself,
[Replacing PyXRootD](porting.md).

## `install()` says the bindings are already imported

```
RuntimeError: the official XRootD bindings are already imported
```

`xrdclient.compat.install()` makes `import XRootD` resolve to this package by
putting it in `sys.modules`. If the real `XRootD` is already there, some
module holds references into it, and swapping the name underneath would leave
two clients in one process with each half of the code talking to a different
one. So it refuses.

Move the call earlier - before whatever imported the real bindings. The
usual culprits:

- **An import at the top of the same file**, above the `install()` line.
  `install()` has to come before `import uproot` and anything else that may
  import `XRootD`, not just before your own `from XRootD import client`.
- **A pytest plugin**, loaded before `conftest.py`. Put `install()` at the top
  of the root `conftest.py`, and if that is still too late, disable the
  plugin with `-p no:<name>` or call `install()` from a `sitecustomize.py`.
- **A notebook kernel** that imported the bindings in an earlier cell.
  Restart the kernel and run `install()` first.
- **`python -X importtime`** prints every import in order, which shows what
  pulled `XRootD` in.

To see which one you have:

```python
import sys

module = sys.modules.get("XRootD")
print(module, getattr(module, "__xrdclient_compat__", False))
```

`True` means this package; calling `install()` again is harmless.

The alternative is not to need `install()` at all: change the import to
`from xrdclient.compat import client`, which never touches the name
`XRootD` and so can live beside the real bindings in one process.

## Kerberos: "credential cache ... cannot reach" (`API:` and the like)

The pure-Python Kerberos reads `FILE:` and `DIR:` caches, `KCM:` caches
(RHEL 9's default, through `sssd-kcm`'s socket) and Linux `KEYRING:` caches
(through the `keyctl` system call). It refuses macOS's `API:` caches, which
Heimdal's credential service holds behind XPC, and `MEMORY:` caches, which
live in another process. The error names the cache it found:

```
CredentialError: Kerberos credential cache 'API:ABCD-1234' is a macOS API: cache,
held by the system's Heimdal credential service over XPC, which this pure-Python
client cannot reach. Get a ticket into a file with: KRB5CCNAME=FILE:/tmp/krb5cc_$(id -u) kinit
```

`klist` shows which kind you have:

```console
$ klist | head -1
Ticket cache: API:ABCD-1234
```

Get a ticket into a file instead, and point the job at it:

```console
$ export KRB5CCNAME=FILE:/tmp/krb5cc_$(id -u)
$ kinit jane@EXAMPLE.ORG
```

A `KEYRING:` name on a host that is not Linux is refused the same way.

A `KCM:` cache that finds no daemon on its socket - `sssd-kcm` not
installed, or `kcm_socket` in `krb5.conf` pointing elsewhere - is treated as
no cache at all, like a missing file, so the login moves on to the next
mechanism; the authentication error then says Kerberos had nothing. Check
with `klist`, which uses the same socket. A daemon that answers with an
error is reported as "the Kerberos credential cache KCM:1000 is unreadable",
with the daemon's code (`KRB5_CC_IO`, `KRB5_FCC_NOFILE`, ...).

For a site that sets `default_ccache_name` in `krb5.conf`, the variable
overrides it for your session only. The other Kerberos limits - AES only, no
DNS lookup of the KDC, no cross-realm tickets - and which cache types were
tested against which real implementations are in
[Authentication](auth.md#krb5-kerberos).

## Authentication fails where it used to work

The status is `code` 204 (`errAuthFailed`) and `fatal`. Its message names
each mechanism the server offered and why each one did not apply - "no proxy
at ...", "token expired at ...", a Kerberos cache of the wrong kind. Read it before
anything else:

```python
status, _ = fs.stat(path)
if status.code == 204:
    print(status.message)
```

Then run the doctor against the same URL - see [debugging](#debugging).

## What behaves differently

Most of the bindings' quirks are kept, because code depends on them. A few
things differ; each shows up as a status or an exception, never as a wrong
answer.

**Kept on purpose:**

- `readline()` keeps a cursor of its own that `read()` does not move, and
  `readline(offset)` moves the cursor to `offset` and leaves it there - so
  `readline(6)` followed by `readline()` returns the same line twice.
- A `File` whose `open` failed answers every later `open` and `close` with
  that same failure. Make a new `File` to try again.
- `close()` on a file that was never opened succeeds, with `code` 4.
- `ProtocolInfo.version` and `.hostinfo` have their four bytes reversed:
  protocol 5.2.0 (`0x520`) reads `0x20050000`.
- I/O on a file that is not open raises `ValueError`; a line that is not
  UTF-8 raises `UnicodeDecodeError`; a `float` timeout raises `TypeError`.
- The per-name statuses in an xattr result, and `FileSystem.cat`'s status,
  are dicts; `XRootDStatus` answers `status["ok"]` too.
- `dirlist` of a path ending in `.zip`, with no callback, lists the members
  of the archive rather than failing on a file - XrdCl's synchronous
  `DirList` adds `DirListFlags.ZIP` for that suffix by itself. With a
  callback it does not, and the server says the path is not a directory.
- `fcntl` against a stock xrootd returns status 400, `errno` 3013 ("fctl
  operation not supported"): the request goes out, and there is no storage
  plug-in to take it.

**Different underneath:**

- **`CopyProcess.add_job(xrateThreshold=...)`** fails a copy that falls
  below the rate at once (`errThresholdExceeded`, 208); XrdCl first asks a
  redirector for another server, when the file was opened through one.
- **Callbacks** run on a shared pool of eight threads.
- **`xrdcl.requuid`**: XrdCl tags each open with a request id in the URL's
  CGI, which then shows in its `LastURL`, in the host list's URLs and in an
  unfollowed redirect's message. No such id is made here, so those strings
  lack it; they are otherwise the same. Likewise an unfollowed redirect's
  message leaves out any `xrd.*` or `xrdcl.*` CGI of the caller's own URL,
  which XrdCl appends.
- **`WriteRecovery`** is stored and read back, but a file open for writing is
  never re-opened here, whatever it says: what the lost server had not yet
  committed could not be put back. `ReadRecovery` is honoured.
- **A `CHUNKED` listing's last part** is known to be the last only once the
  answer is complete, so each part reaches the callback when the next one
  arrives, and a listing whose final frame is empty ends with its last
  non-empty part as the final answer rather than an empty one. `RECURSIVE`
  with `CHUNKED` arrives whole.
- **Defaults** are this library's where nobody set one: a 300 s read timeout
  and a 1800 s ceiling on a whole operation, for instance, rather than
  XrdCl's 60 s stream timeout and 1800 s request expiry. `EnvGetInt` still
  answers XrdCl's number for an unset key, because code reads it expecting
  that, so it may not be what is in force. Call `EnvPutInt` (or set
  `XRD_REQUESTTIMEOUT`) to be sure: a put `RequestTimeout` expires every call
  made without a `timeout`, as XrdCl's does.
- **Log masks** mute parts of the `xrdclient` logger hierarchy
  ([topics](compat-reference.md#log-topics)); `XRD_LOGLEVEL`, `XRD_LOGFILE`
  and `XRD_LOGMASK` are not read. See [debugging](#debugging).
- **Keys with no effect**: every `EnvPutInt` key XrdCl registers can be put
  and read back, but only those with a native equivalent change anything -
  `env.EFFECTS` says which, and why not for the rest
  ([environment keys](compat-reference.md#environment-keys)).

## `EnvPutInt` returns `False`

The same key is set in the process environment as `XRD_<KEY>`, and as in
XrdCl the environment wins - for a key XrdCl registers; any other is not
looked for there. The native `Config` reads that variable itself,
so its value is already in force; unset it in the shell if the code should
decide.

## `readlines(offset)` behaves differently

With a non-zero `offset` the bindings hang; here it returns the lines from
that offset. Code that works on the bindings never passes one, so nothing
changes for it.

## Newer upstream API

The compat layer follows the `XRootD.client` API that the parity suite runs
against (the 5.x and 6.x releases), and also the API upstream has added
since, which the installed 6.1 bindings do not have yet:

- **Typed exceptions** - `status.raise_on_error()`, `status.exception()`,
  `status.error_name`, `client.raise_on_error(status)` and the
  `XRootDError` family. Calls still return `(status, response)` and never
  raise for a server's answer; these are for code that would rather raise.
  See the [cookbook](compat-cookbook.md#raising-instead-of-checking).
- **`client.tape.TapeClient`** for the WLCG Tape REST API; see the
  [cookbook](compat-cookbook.md#the-tape-rest-api).
- **`XRootD.client.finalize`** and **`XRootD.client._version`** exist, so
  code that imports them ports unchanged; `finalize.finalize()` closes open
  files and the shared connections, and runs at exit by itself.

An `AttributeError` or `ImportError` naming an upstream name not listed in
the [reference](compat-reference.md) means that name is newer still. Please report it,
with the upstream version that has it.

## Is it slower?

For most analysis workloads it is as fast or faster:
`benchmarks/compare.py` runs every common case through the native API,
through this compat layer and through the official bindings, on the same
server, and CI runs it as a gate on every push. Many small reads, `stat`, directory listings and copies to local
disk are where the bindings' per-call cost of crossing into C++ dominates,
and this client wins them. One large streaming read and large vector reads
are where compiled code still leads. The numbers, and how to reproduce them
on your own server, are in [Performance](performance.md).

If a ported job is slower than it was:

- Check the settings it relied on. XrdCl's copy chunk is 8 MiB, this
  library's 4 MiB; `XRD_CPCHUNKSIZE=8388608` restores it.
- For whole-file downloads, the native `xrdclient.copy` and
  `xrdclient.open(...).readinto(buf)` use the [bulk data plane](performance.md#the-bulk-data-plane);
  a `CopyProcess` job already does.
- Measure against your real endpoint rather than loopback -
  `python benchmarks/bench.py --url root://host//store/scratch` - since
  round-trip time changes the picture more than anything else, and
  `benchmarks/compare.py --rtt 2` shows the effect of latency on the three
  clients side by side.

## Debugging

**Logging.** Everything is logged under the `xrdclient` logger, and nothing
is shown until a handler is attached:

```python
import logging

from xrdclient.compat import client

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(message)s")
client.SetLogLevel("Debug")        # the same as logging.getLogger("xrdclient").setLevel(DEBUG)
```

`SetLogLevel` takes XrdCl's words - `"Error"`, `"Warning"`, `"Info"`,
`"Debug"`, `"Dump"` - and raises `ValueError` for anything else. To narrow
the output to one part of the client, set the level on a child logger such as
`xrdclient.session` instead. Tokens, passwords and `authz=` values are
redacted from every record before a handler sees it, so a debug log is safe
to attach to a ticket.

**The doctor.** When something will not work and the status does not say
why, check everything a first transfer needs - the interpreter, the settings
in force, each authentication mechanism, DNS, the port, the login and the
path - one line each:

```console
$ xrd-fs doctor root://eos.example.org//store/user/me
```

The first `!!` line is the thing to fix. From Python, the same report:

```python
import xrdclient

report = xrdclient.diagnose("root://eos.example.org//store/user/me")
if not report.ok:
    print(report)
```

With no URL it checks the machine alone, which is the useful thing to paste
into a ticket. See [Command line](cli.md).

**Compare with the bindings.** Where both are installed, run the same call
through each and compare field by field; the statuses and responses are plain
attribute bags, so `vars()` shows everything:

```python
from XRootD import client as theirs
from xrdclient.compat import client as ours

url, path = "root://eos.example.org", "/store/user/me/f.root"
for name, lib in (("bindings", theirs), ("compat", ours)):
    status, info = lib.FileSystem(url).stat(path)
    print(name, vars(status), vars(info) if info else None)
```

A difference other than `id`, timestamps or message wording is a bug - please
report it with that output. `examples/pyxrootd/run_all.py` and
`pytest tests/test_pyxrootd_compat.py -m parity` do the same systematically;
see [verifying the port](porting.md#verifying-the-port).
