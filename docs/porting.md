# Replacing PyXRootD

This page is for a codebase written against the official Python bindings -
`from XRootD import client` - that should run on this library instead. The
port is an import change, and for code you cannot edit it is not even that.
What follows is how to make the change, what changes underneath it, how to
check it worked, and how to move on to the native API afterwards if you want
to.

The companion pages are the [compatibility reference](compat-reference.md),
which goes method by method, the [cookbook](compat-cookbook.md), which has
complete programs for the common jobs, and
[troubleshooting](compat-troubleshooting.md).

## The one-line change

```python
from xrdclient.compat import client        # was: from XRootD import client
```

Nothing else in the file changes. `xrdclient.compat.client` has the same
classes (`FileSystem`, `File`, `CopyProcess`, `URL`), the same method names,
the same positional and keyword arguments with the same defaults, the same
`(XRootDStatus, response)` pairs, the same attribute names on every response,
the same flag names with the same numbers, and the same `timeout=` and
`callback=` on every call that has them.

Imports of submodules port the same way:

| Before | After |
| --- | --- |
| `from XRootD import client` | `from xrdclient.compat import client` |
| `import XRootD.client as client` | `from xrdclient.compat import client` |
| `from XRootD.client.flags import OpenFlags` | `from xrdclient.compat.client.flags import OpenFlags` |
| `from XRootD.client.responses import XRootDStatus` | `from xrdclient.compat.client.responses import XRootDStatus` |
| `from XRootD.client.utils import CopyProgressHandler` | `from xrdclient.compat.client.utils import CopyProgressHandler` |
| `from XRootD.client import glob` | `from xrdclient.compat.client import glob` |

A complete program, before and after, differs in one line:

```python
from xrdclient.compat import client        # was: from XRootD import client
from xrdclient.compat.client.flags import OpenFlags

fs = client.FileSystem("root://eos.example.org")
status, info = fs.stat("/store/user/me/f.root")
if not status.ok:
    raise SystemExit(status.message)
print(info.size, info.modtimestr)

with client.File() as f:
    status, _ = f.open("root://eos.example.org//store/user/me/f.root", OpenFlags.READ)
    status, header = f.read(0, 1024)
```

## The no-edit route: `install()`

Some code imports `XRootD` where you cannot reach it: uproot, coffea,
fsspec-xrootd, a colleague's analysis framework, a script you are only allowed
to run. For those, `xrdclient.compat.install()` makes the name `XRootD` itself
resolve to this package, for the rest of the process:

```python
import xrdclient.compat
xrdclient.compat.install()      # before anything imports XRootD

from XRootD import client       # this is now xrdclient.compat.client
```

It registers `XRootD`, `XRootD.client` and the submodules `flags`,
`responses`, `utils`, `url`, `env`, `file`, `filesystem`, `copyprocess` and
`glob_funcs` in `sys.modules`. It changes nothing on disk and nothing outside
the current interpreter.

It must run **before** anything imports the real bindings. If `XRootD` is
already in `sys.modules` and is not this package, `install()` raises
`RuntimeError: the official XRootD bindings are already imported` rather than
swapping one module for another under code that already holds references to
the first. Calling it twice is harmless.

### uproot and coffea

uproot opens `root://` URLs through one of two handlers. With
`handler=uproot.XRootDSource` it imports `XRootD.client` and drives `File`,
`FileSystem` and `URL` directly; that is the path `install()` redirects:

```python
import xrdclient.compat
xrdclient.compat.install()

import uproot

url = "root://eos.example.org//store/user/me/ntuple.root"
with uproot.open(url + ":Events", handler=uproot.XRootDSource) as tree:
    pt = tree["Muon_pt"].array()
```

With the default handler uproot goes through fsspec instead, and once this
package is installed fsspec resolves `root://` to this library's own
[fsspec filesystem](fsspec.md) - native, with no bindings involved at all.
`fsspec.get_filesystem_class("root")` tells you which one is registered. If
`fsspec-xrootd` is installed too, it imports `XRootD.client` and so is also
redirected by `install()`.

coffea reads files through uproot, so the same `install()` at the top of the
steering script (or of each worker's start-up, for a distributed executor)
covers it.

### Code you cannot edit at all

When not even the entry point can take an extra line, run it under a
two-line launcher:

```python
# run_on_xrdclient.py
import runpy, sys
import xrdclient.compat

xrdclient.compat.install()
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
```

```console
$ python run_on_xrdclient.py their_script.py --their-args
```

or put the two lines in a `sitecustomize.py` on the interpreter's path, which
Python imports at start-up before any user code.

## Environments with both installed

A conda environment with `xrootd` from conda-forge, or a venv with the
`xrootd` wheel, can have this package installed beside it:

```console
$ pip install xrdclient
```

The two do not interfere. `xrdclient.compat.client` never imports `XRootD`,
so a ported module runs on this library even though the bindings are
importable, and unported modules keep using the bindings. Both can be live in
one process as long as each module imports the one it means - which is how
the parity suite compares them. `install()` is the one thing that cannot
coexist with an already-imported `XRootD`, for the reason above.

Once nothing imports `XRootD` any more, the bindings can go:

```console
$ conda remove xrootd          # or: pip uninstall xrootd
```

which also removes `libXrdCl` and the rest of the C++ client from the
environment. Keep the `xrootd` package if you use the `xrdcp` or `xrdfs`
commands, or run a server, from that environment; this library ships its own
[`xrd-cp` and `xrd-fs`](cli.md).

## What changes underneath

The calls are the same. The machinery under them is not, and a few
operational habits change with it.

**No `libXrdCl`.** Nothing is compiled and nothing is loaded from C++. The
wire protocol, TLS, GSI, Kerberos and the rest are Python. There are no
`LD_LIBRARY_PATH` problems, no ABI to match against the interpreter, and no
XrdCl worker threads or fork handlers; uproot's `XRD_RUNFORKHANDLER=1` is
harmless and ignored.

**Settings come from a `Config`.** Every compat object is built on a native
[`Config`](config.md). That `Config` reads the same `XRD_*` environment
variables the C++ client reads - `XRD_REQUESTTIMEOUT`,
`XRD_CONNECTIONWINDOW`, `XRD_CPCHUNKSIZE` and the others in the
[configuration tables](config.md) - so an existing site environment keeps
working. `client.EnvPutInt` and `client.EnvPutString` still work, and are
translated to the `Config` field each key means; the full mapping is in the
[reference](compat-reference.md#environment-keys). Two consequences:

- A setting nobody put keeps this library's default rather than XrdCl's.
  The request timeout, for example, is 300 s here and 1800 s in XrdCl.
  `EnvGetInt` still answers XrdCl's default for a key nobody set, because
  code reads it expecting that, but it is not what is in force.
- The settings are read when an object is built: a `FileSystem` when it is
  constructed, a `File` when it is opened, a `CopyProcess` job when it runs.
  An `EnvPutInt` after that affects only later objects.

XrdCl's logging variables - `XRD_LOGLEVEL`, `XRD_LOGFILE`, `XRD_LOGMASK` -
are not read; see logging below.

**Authentication is the native ladder.** The mechanisms the server offers are
tried in `Config.auth_order`: `gsi`, `ztn`, `krb5`, `sss`, `unix`, `host`.
The credentials are found where the C++ client finds them -
`X509_USER_PROXY` or `/tmp/x509up_u$UID` for GSI; `BEARER_TOKEN`,
`BEARER_TOKEN_FILE`, `$XDG_RUNTIME_DIR/bt_u$UID` and `/tmp/bt_u$UID` for
tokens; `XrdSecSSSKT` for sss - so a job that authenticated before
authenticates now. See [Authentication](auth.md) for each mechanism.

Kerberos is pure Python too, and reads the credential caches `kinit` writes
on Linux: `FILE:` and `DIR:` caches, `KCM:` (RHEL 9's default, through
`sssd-kcm`) and `KEYRING:`. Only macOS's `API:` cache is out of reach from
Python; there, point `kinit` at a file:

```console
$ export KRB5CCNAME=FILE:/tmp/krb5cc_$(id -u)
$ kinit jane@EXAMPLE.ORG
```

The other Kerberos limits - AES enctypes only, no DNS SRV lookup of the KDC,
no cross-realm - are listed in [Authentication](auth.md#krb5-kerberos).

**Logging is `logging`.** Everything is logged under the `xrdclient` logger.
`client.SetLogLevel("Debug")` sets that logger's level, taking XrdCl's words
(`"Error"`, `"Warning"`, `"Info"`, `"Debug"`, `"Dump"`); `SetLogMask` is
accepted and does nothing, since a topic here is a child logger such as
`xrdclient.session`. Nothing is printed until a handler is attached:

```python
import logging

logging.basicConfig(level=logging.INFO)
client.SetLogLevel("Debug")
```

Credentials are redacted in every log record before any handler sees it.

**Errors.** Failures still arrive as statuses with XrdCl's `code`, the
server's `errno` and the `shellcode` a script would exit with. A `TypeError`
or `ValueError` for bad arguments, and `ValueError` for I/O on a file that is
not open, still raise, as they do in the bindings.

**Timeouts are the caller's.** `timeout=` bounds how long the call waits; when
it expires the call returns `errOperationExpired` (206) and the request is
left to finish or fail on its own. It is not recalled - which XrdCl cannot do
either.

## Porting a codebase, step by step

1. **Find every import.** Every use of the bindings starts with one:

    ```console
    $ grep -rnE '^\s*(from|import)\s+XRootD\b' --include='*.py' .
    $ grep -rnE 'XRootD\.client' --include='*.py' .
    ```

    The second catches code that reaches the module through an attribute,
    such as `XRootD.client.flags.OpenFlags.READ` after a bare
    `import XRootD.client`.

2. **Read the short list of behaviours that differ.** Every name the
   bindings export is here, the newer upstream API included (typed
   exceptions, `TapeClient`); what differs is a handful of behaviours -
   caller-side timeouts, the `CopyProcess` keywords that have no effect - and
   [troubleshooting](compat-troubleshooting.md#what-behaves-differently) lists
   them.

3. **Run your tests with `install()`, before touching an import.** Add it to
   the test suite's `conftest.py`:

    ```python
    # conftest.py
    import xrdclient.compat

    xrdclient.compat.install()
    ```

    Every `import XRootD` in the code under test now gets this package, so the
    test run tells you whether the port works before you have changed a line
    of it. If `install()` raises, something imported the real bindings first -
    usually a plugin; move the call earlier, or run pytest with
    `-p no:<plugin>`.

4. **Switch the imports.** Replace each import from step one with its
   `xrdclient.compat` spelling from the table above, then take `install()`
   back out of `conftest.py` so the tests prove the imports are right on
   their own:

    ```console
    $ grep -rlE '^[[:space:]]*from XRootD import client' --include='*.py' . \
        | xargs sed -i.bak -E 's/^([[:space:]]*)from XRootD import client/\1from xrdclient.compat import client/'
    ```

    (`sed -i.bak` works on both GNU and BSD `sed`; delete the `.bak` files
    afterwards.) Submodule imports are best done by hand: there are usually
    few of them.

5. **Remove the dependency.** Replace `xrootd` with `xrdclient` in
   `requirements.txt`, `pyproject.toml`, `environment.yml` or the container
   recipe, and rebuild the environment without the bindings, so nothing can
   pick them up by accident.

6. **Check the settings you relied on.** If the code or its batch
   environment set `XRD_*` variables, check each against the
   [configuration tables](config.md); if it relied on XrdCl's default
   timeouts rather than setting them, compare the defaults. Set
   `XRD_REQUESTTIMEOUT=1800` if a job needs XrdCl's longer request timeout.

## Verifying the port

Three things in this repository exist to show that the compat layer answers
as the bindings do. Run whichever fits.

**[`examples/pyxrootd/`](https://github.com/rob-c/xrdclient/tree/main/examples/pyxrootd)**
holds short programs written the way PyXRootD users write them, each with the
one import changed. `examples/pyxrootd/run_all.py` starts a stock `xrootd`,
runs every example once on the official bindings and once on the compat
layer, and compares what the two printed:

```console
$ python examples/pyxrootd/run_all.py                 # both, and a comparison table
$ python examples/pyxrootd/run_all.py --compat-only   # without the bindings installed
$ python examples/pyxrootd/run_all.py -k dirlist      # just the examples matching a name
```

It exits non-zero if a compat run fails or prints something the official run
did not. The examples cover stat and ping, namespace operations, open flags,
file I/O, chunked and line reads, vector reads, directory listings, glob,
locate, checksums, xattrs, `CopyProcess`, callbacks, timeouts, error
handling, environment settings, URL parsing, and `install()`; the
directory's `README.md` describes each.

**[`tools/pyxrootd_upstream.py`](https://github.com/rob-c/xrdclient/blob/main/tools/pyxrootd_upstream.py)**
runs XRootD's own tests for its Python bindings against this package, so the
tests the XRootD project wrote are the judge rather than tests written here.
It copies `python/tests` from an XRootD source checkout twice, rewrites only
the import lines of one copy to name `xrdclient.compat`, runs pytest on both -
the untouched copy on the official bindings, the rewritten one on this
library - and compares every test's outcome:

```console
$ python tools/pyxrootd_upstream.py ~/src/xrootd        # path to an XRootD checkout
$ python tools/pyxrootd_upstream.py ~/src/xrootd -k test_file
```

It exits non-zero if a test that passes on the bindings does not pass here.
It needs `xrootd` and `xrdfs` on `PATH`, the official bindings importable,
and `pytest`.

**`tests/test_pyxrootd_compat.py`** is the parity suite. Its parity tests run
each covered call twice against one real `xrootd` - through `XRootD.client`
and through `xrdclient.compat.client` - and require every field of the status
and of the response to be equal, leaving out only values that differ between
any two calls anyway (file ids, timestamps and message text):

```console
$ pytest tests/test_pyxrootd_compat.py -m parity
```

Without `xrootd` on `PATH` or without the bindings installed, those tests
skip; the rest of the file pins the same behaviour against the in-process
test server and always runs. See [Interoperability and parity](interop.md).

Your own code's tests are the fourth check, and the one that matters most;
step three above is how to run them.

## Moving on to the native API

The compat layer is a complete destination: nothing forces a second step.
But the native API is shorter to write - it raises instead of returning a
status, reads return `bytes`, files are real `io` objects - and has things
the bindings never had (`walk`, `glob`, `pathlib`, `asyncio`, WebDAV, S3).
Every compat `FileSystem` and every open compat `File` carries its native
object as `.native`, so the move can be made one call at a time inside a
running port:

```python
from xrdclient.compat import client

fs = client.FileSystem("root://eos.example.org")
status, info = fs.stat("/store/run7/f.root")          # still the bindings' shape

for top, dirs, files in fs.native.walk("/store/run7"):  # native: raises on failure
    for name in files:
        print(top, name)

with client.File() as f:
    f.open("root://eos.example.org//store/run7/f.root")
    ranges = f.native.readv([(0, 100), (4096, 100)])   # native: a list of bytes
```

`File.native` is `None` until `open` succeeds and again after `close`.

The native calls raise; the compat ones never do for a server's answer. When
a port mixes them, wrap the native part in the `try` you would have written
for any `OSError`. [Coming from pyxrootd](migrating.md) has the translation
table and the few names that mean different things in the two APIs - notably
`read(offset, size)` against `read(size, offset)`, and `DirListFlags`, whose
numbers differ.
