# Command line

Two commands, `xrd-fs` and `xrd-cp`, both taking whole URLs. Both understand
`--json`, and both use the same three exit codes: `0` success, `1` a runtime
failure, `2` a usage error. A third, `xrd-datasets`, is installed by a package
above this one and shares the same flags - see below.

Common options on every subcommand:

| Option | Meaning |
| --- | --- |
| `--json` | machine-readable output, so a script never parses columns |
| `-q`, `--quiet` | say nothing on success |
| `-v`, `-vv`, `-vvv` | warnings, info, the wire itself |
| `--token TOKEN` | bearer token to present |
| `--user NAME` | username to authenticate as |
| `--no-verify-tls` | do not verify the server certificate |
| `--prompt` | ask for missing credentials even when this is not a terminal |
| `--no-prompt` | never ask for credentials; fail instead |
| `--config FILE` | settings file to read instead of the usual places |
| `--alias NAME` | the `[alias NAME]` section of that file to overlay |

See [Configuration](config.md#the-settings-file) for the file itself.

## `xrd-fs`

```console
$ xrd-fs ls -l root://eos.example.org//store/user/me
$ xrd-fs ls -R root://host//store/run7
$ xrd-fs stat --json davs://dav.example.org/store/f.root
$ xrd-fs cat root://host//store/notes.txt
$ xrd-fs checksum -a adler32 root://host//store/f.root
$ xrd-fs mkdir -p root://host//store/a/b/c
$ xrd-fs rm -r root://host//store/user/me/scratch   # asks first at a terminal
$ xrd-fs mv root://host//store/a.root root://host//store/b.root
$ xrd-fs touch root://host//store/marker
$ xrd-fs df root://host//store
$ xrd-fs locate --deep root://host//store/f.root
$ xrd-fs ping root://host
$ xrd-fs doctor root://eos.example.org//store/user/me   # why will this not work?
$ xrd-fs query root://host version role sitename
$ xrd-fs xattr --set user.experiment=atlas root://host//store/f.root
$ xrd-fs xattr -r root://host//store/run7               # names, whole subtree
$ xrd-fs tail -n 20 root://host//store/log.txt
$ xrd-fs tail -f root://host//store/running.log        # keep printing appends
$ xrd-fs du root://host//store/run7
$ xrd-fs chmod 640 root://host//store/f.root
$ xrd-fs chown 1000:1000 root://host//store/f.root     # uid, uid:gid or :gid
$ xrd-fs touch --time now root://host//store/f.root    # or epoch seconds
$ xrd-fs truncate -s 0 root://host//store/scratch.bin
$ xrd-fs prepare root://host//store/f.root             # stage it onto disk
$ xrd-fs prepare --evict root://host//store/f.root     # drop the cached copy
$ xrd-fs prepare --status prep-0001 root://host//store/f.root   # is it there yet?
$ xrd-fs locality root://host//store/f.root            # on disk, or still on tape?
$ xrd-fs ln -s root://host//store/f.root root://host//store/latest
$ xrd-fs readlink root://host//store/latest
```

`locality` asks where files are without asking for any of them to move, and
answers `path: online`, `path: on tape` or `path: nowhere (no such file)`; over
`davs://` it is the WLCG tape API's `archiveinfo`, and `--json` carries the
`state` word the site used. `tail -f` polls the size every `--interval` seconds and prints what appeared;
a file that shrinks is a different file, and it stops. `du` counts from the
listing, one request per directory, and falls back to one stat per entry when
the server lists without sizes. `ln`, `ln -s`, `readlink`, `chown` and
`touch --time` are a **vendor extension** - stock XRootD answers
`kXR_Unsupported`, and HTTP has no verb for them at all; see
[Interoperability](interop.md). So is `xattr -r`, which prints one
`path: name` line per attribute found under a directory, and prints nothing
at all on a server that does not implement it. Plain `touch` is not: it creates a file and
leaves an existing one alone, which every server understands.

`doctor` is the one to reach for when something will not work and the error
does not say why. It walks the same ground a transfer would - the interpreter,
the settings in force, every authentication mechanism in turn, DNS, the port,
the login, and the path itself - and prints a line for each, so the first `!!`
down the list is the thing to fix and everything below it is a consequence:

```console
$ xrd-fs doctor root://eos.example.org//store/user/me
ok  python        3.13.5 on linux
ok  settings      connect 30s, request 300s, TLS verified
!!  auth:gsi      the proxy at /tmp/x509up_u1000 expired 4 hours ago
                  -> voms-proxy-init -voms lhcb
ok  connect       1094/tcp answered in 41 ms
!!  path          /store/user/me: no such file or directory
                  -> /store/user exists; check the spelling below it
```

With no URL it checks the machine alone, which is the useful thing to paste
into a ticket. It never prompts for anything, exits `1` if any line failed,
and `--json` gives the same report as data. `xrd.diagnose()` is the same thing
from Python.

`rm -r` is the one subcommand that asks before it acts: at a terminal it says
what it is about to remove and how many entries are under it, and it refuses a
path less than two components deep - `/store` rather than `/store/user/me` -
until `--yes` says that really was the instruction. `-f` still means only
"ignore what is not there". Nothing prompts when neither stream is a terminal,
so a batch job runs as it always did. See [Safety](safety.md).

## `xrd-cp`

```console
$ xrd-cp /tmp/f.root root://host//store/f.root
$ xrd-cp root://host//store/f.root /scratch/
$ xrd-cp -r /tmp/results davs://dav.example.org/store/results
$ xrd-cp --tpc root://a//store/f.root root://b//store/f.root
$ xrd-cp -f /tmp/f.root root://host//store/f.root      # overwrite what is there
$ xrd-cp --verify -a crc32c /tmp/f.root root://host//store/f.root
$ xrd-cp --chunk-size 8M --progress root://host//store/big.root /scratch/
$ xrd-cp --in-flight 4 root://host//store/big.root /scratch/   # deeper read-ahead
$ xrd-cp --stripes 8 root://host//store/big.root /scratch/      # eight spans at once
$ xrd-cp --streams 2 root://host//store/big.root /scratch/      # two links per span
$ xrd-cp -r --exclude '*.log' /tmp/results root://host//store/results
$ xrd-cp -r --include '*.root' --sync size /tmp/results root://host//store/results
$ xrd-cp -r --delete /tmp/results root://host//store/results
$ xrd-cp -r --dry-run /tmp/results root://host//store/results
$ xrd-cp -r --parallel 8 /tmp/many-small root://host//store/many-small
$ xrd-cp --remove-source /tmp/f.root root://host//store/f.root   # a move
$ xrd-cp -c root://host//store/big.root /scratch/big.root   # carry on, do not restart
```

Several sources are allowed when the destination is a directory. Progress is
shown on a tty and suppressed otherwise; `--progress` and `--no-progress`
override that either way.

`-c`, `--continue` keeps whatever is already at the destination and starts
from the end of it; the JSON record carries `resumed_at` so a script can see
how much was skipped. It works on a tree too, and refuses to combine with
`--tpc` or `-n`, which each forbid the partial destination it needs. See
[Resuming](copying.md#resuming) for what it does to verification.

An existing destination is an error unless `-f`, `--force` says to replace it,
which is what `cp -i` would ask and what `xrdcp` needs `-f` for too; `-c`
implies it. See [Safety](safety.md).

For a tree:

| Option | Meaning |
| --- | --- |
| `--include PATTERN` | only paths matching one of these travel (repeatable) |
| `--exclude PATTERN` | paths matching one of these do not (wins over include) |
| `--sync {size,mtime,checksum}` | skip what is already there, judged that way |
| `--delete` | remove what is in the target but not in the source |
| `--parallel N` | copy N files at once (default 1) |
| `--dry-run` | print what would happen, transfer nothing |
| `--remove-source` | delete the source once the copy is verified - a move |

Patterns are `fnmatch` patterns matched against the path relative to the
source root, so `sub/*.root` means what it looks like. `--sync checksum` asks
both endpoints for a digest, which is exact and not free; `--sync size` is one
stat each. `--delete` never removes anything an `--exclude` hid, because it
was never a candidate in the first place.

`--parallel` is files in flight, not requests: a tree of small files is
round-trip-bound, so copying several at once is what makes it faster, while a
single large file is already spread over several connections by itself. It
needs `-r`, since without a tree there is nothing to run in parallel.

`cp` semantics decide the destination: a target that already exists as a
directory is copied *into*, so a second run of `cp -r tree /dest` writes
`/dest/tree/tree`. Give the target a trailing slash to say "into this" every
time, which is what makes `--sync` and `--delete` idempotent.

## `xrd-datasets`

Installed by [`xrddatasets`](https://github.com/rob-c/xrddatasets), not by this package, and
documented [there](https://github.com/rob-c/xrddatasets/blob/main/docs/datasets-site.md). It builds
a directory of open datasets as ROOT files and the catalogue that serves them,
using the copies and checksums here to do it.

## Scripting with `--json`

```console
$ xrd-fs stat --json root://host//store/f.root | jq .size
$ xrd-fs ls --json root://host//store | jq -r '.[] | select(.is_dir) | .name'
$ xrd-cp --json /tmp/f.root root://host//store/f.root | jq .rate
```

Errors are reported as a sentence on stderr, not a traceback - a missing file
is `no such file or directory: /store/f.root`, and the exit code is `1`.

## The URL slash

An XRootD URL needs a **doubled** slash before an absolute path:

```console
$ xrd-fs ls root://host//store/user/me     # /store/user/me
$ xrd-fs ls root://host/store/user/me      # the same file, here
```

Stock `xrdcp` refuses the single-slash form, which is the single most common
first hour of XRootD; this client normalises both to `/store/user/me` before
anything goes on the wire. See [Safety](safety.md).
