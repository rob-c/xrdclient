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

## `ModuleNotFoundError: No module named 'XRootD.client.finalize'`

`XRootD.client.finalize` shuts XrdCl down at interpreter exit. There is
nothing to shut down here - connections are closed by an `atexit` hook - so
the module does not exist. Delete the import; if the code calls
`finalize.finalize()`, delete that too. The same holds for
`XRootD.client._version`: use `importlib.metadata.version("xrdclient")`.

## Kerberos: "this client reads only FILE: and DIR: caches"

The pure-Python Kerberos reads credential caches that are files - `FILE:`
and `DIR:` - and refuses the ones held by a daemon or the kernel. RHEL 9 and
its rebuilds default to `KCM:`; some sites use `KEYRING:`; macOS uses `API:`.
The error names the cache it found:

```
CredentialError: Kerberos credential cache 'KCM:1000' lives outside the
filesystem, and this client reads only FILE: and DIR: caches. Get a ticket into a file with:
KRB5CCNAME=FILE:/tmp/krb5cc_$(id -u) kinit
```

`klist` shows which kind you have:

```console
$ klist | head -1
Ticket cache: KCM:1000
```

Get a ticket into a file instead, and point the job at it:

```console
$ export KRB5CCNAME=FILE:/tmp/krb5cc_$(id -u)
$ kinit jane@EXAMPLE.ORG
```

For a site that sets `default_ccache_name` in `krb5.conf`, the variable
overrides it for your session only. The other Kerberos limits - AES only, no
DNS lookup of the KDC, no cross-realm tickets - are in
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

**Not supported, and says so:**

- `File.fcntl` and `File.openusingtemplate` (`OpenFlags.DUP`, `SAMEFS`)
  return `errNotSupported` (13).
- `dirlist` with `DirListFlags.ZIP` returns `errNotImplemented` (15).
- `CopyProcess.add_job` accepts `sourcelimit`, `coerce`, `dynamicsource`,
  `inittimeout`, `cptimeout`, `xrate` and `xrateThreshold` and ignores them:
  one source is read, no rate limit is applied, and a copy is bounded by the
  `Config` timeouts.

**Different underneath:**

- **Timeouts are caller-side.** `timeout=` bounds how long the caller waits;
  when it runs out the call returns `errOperationExpired` (206) and the
  request carries on or fails by itself. It is not recalled - which XrdCl
  cannot do either - so a timed-out write may still complete.
- **Callbacks** run on a shared pool of eight threads, and their host list
  names the server that answered, not every hop.
- **`dirlist` with `LOCATE` or `MERGE`** lists the directory on the server
  the request is routed to, rather than on every server and merged. For a
  single server that is the same answer.
- **Defaults** are this library's where nobody set one: a 300 s request
  timeout rather than XrdCl's 1800 s, for instance. `EnvGetInt` still
  answers XrdCl's number for an unset key, because code reads it expecting
  that, so it may not be what is in force. Set `XRD_REQUESTTIMEOUT` or call
  `EnvPutInt` to be sure.
- **`SetLogMask`** does nothing; `XRD_LOGLEVEL`, `XRD_LOGFILE` and
  `XRD_LOGMASK` are not read. See [debugging](#debugging).

## `EnvPutInt` returns `False`

The same key is set in the process environment as `XRD_<KEY>`, and as in
XrdCl the environment wins. The native `Config` reads that variable itself,
so its value is already in force; unset it in the shell if the code should
decide.

## `readlines(offset)` behaves differently

With a non-zero `offset` the bindings hang; here it returns the lines from
that offset. Code that works on the bindings never passes one, so nothing
changes for it.

## Newer upstream API that is not here yet

The compat layer follows the `XRootD.client` API that the parity suite runs
against (the 5.x and 6.x releases). Some newer upstream releases add API that
is not yet reproduced:

- **`client.tape`** and the other tape-specific helpers. Stage with
  `FileSystem.prepare(files, PrepareFlags.STAGE)` and watch
  `StatInfoFlags.OFFLINE`, as in the
  [cookbook](compat-cookbook.md#staging-from-tape), or use the native
  `fs.native.prepare`, `query_prepare` and `archive_info`.
- **Typed exceptions** such as `XRootDNotFoundError`. The compat layer keeps
  the `(status, response)` shape and never raises for a server's answer;
  test `status.errno` instead. For exceptions, move that call to the native
  API, where a missing file is a `FileNotFoundError` subclass - see
  [Errors](errors.md).

An `AttributeError` or `ImportError` naming an upstream name not listed in
the [reference](compat-reference.md) means the same thing. Please report it,
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
