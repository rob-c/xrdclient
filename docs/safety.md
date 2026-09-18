# Safety

Grid storage is unforgiving in a way a laptop is not: the file is somebody
else's, the directory is a collaboration's, and the mistake is made once and
noticed a week later. The stock clients assume you know that. This one
assumes you are finding out, and puts the guard rail where the drop is.

None of it is a mode you have to turn on, and none of it stands between you
and something you actually asked for - each guard has a way of saying "yes, I
mean it" that takes one flag or one argument.

## A read that never said how much it wanted is bounded

```python
data = fs.read_bytes("/store/dataset.root")     # 90 GB, and your laptop has 16
```

That line is the classic first afternoon. Here it raises
[`TooLargeError`](errors.md#too-much-at-once) before a byte is allocated, and
the message is the fix:

```
/store/dataset.root is 96636764160 bytes, over the 1073741824 byte ceiling on
reading a whole file into memory: read it in pieces (for block in file: ...),
copy it to disk with xrdclient.copy(), or raise config.max_read_size
```

The ceiling is `config.max_read_size`, 1 GiB by default and `$XRD_MAXREADSIZE`
from the environment. It applies to the reads that named no size - `read()`,
`read_bytes()`, `read_text()`, `XRootDPath.read_bytes()` - over every scheme,
including HTTP and S3, where the count is taken as the bytes arrive because a
chunked response does not say how long it is.

It never applies to a read that named one:

```python
with xrdclient.open(url, "rb") as fh:
    header = fh.read(1 << 20)       # a size is a decision, and is honoured
    for block in fh:                # so is streaming, at any length
        ...
xrdclient.copy(url, "/scratch/f.root")    # and so is a copy, which never buffers
```

To lift it, say so once:

```python
xrdclient.configure(max_read_size=0)                       # process-wide
fs = xrdclient.FileSystem(url, xrdclient.Config(max_read_size=0))  # or for one endpoint
```

## A copy does not overwrite what is already there

```console
$ xrd-cp root://host//store/f.root /scratch/f.root
xrd-cp: [Errno 17] File exists: '/scratch/f.root'
$ xrd-cp -f root://host//store/f.root /scratch/f.root   # yes, replace it
```

`-f`, `--force` is the difference between a copy and a copy over the top of
something. `-c`, `--continue` implies it, because carrying on from a partial
file is the one case where the destination is meant to be written into.

In the library the same switch is `overwrite=`, and it defaults to `True`
there: `xrdclient.copy(src, dst, overwrite=False)` is a program spelling out what
the command line asks of a person. The asymmetry is deliberate - a script
that says `copy(...)` has already decided, and a hand at a keyboard has not.

## A recursive remove is asked about, and refused outright at the top

```console
$ xrd-fs rm -r root://eos.example.org//store
xrd-fs: root://eos.example.org//store is the top of a namespace rather than a
tree to delete; say --yes if that really is what you mean

$ xrd-fs rm -r root://eos.example.org//store/user/me/scratch
xrd-fs: remove root://eos.example.org//store/user/me/scratch and the 412
entries under it? [y/N]
```

Two separate guards:

* Anything less than two components deep - `/`, `/store`, `/eos` - is refused
  whatever the terminal says. That path is a slipped shell quote far more
  often than it is an instruction.
* Deeper than that, and with somebody there to answer, you are asked once,
  with a count of what is about to go. The count is one listing of the top of
  the tree, so the question costs a request rather than a walk.

A batch job is never stopped by a question it cannot see: with no terminal on
both stdin and stderr, the prompt is skipped and the removal proceeds. Only
the shallow-path refusal survives into a script, and `--yes` overrules it.

## The double slash is not a trap here

`root://host//store/f.root` and `root://host/store/f.root` name the same file.
The stock tools treat the second as a different, usually missing, path, and
the resulting `[3011] no such file or directory` is the single most common
first hour of XRootD. [`xrdclient.parse`](api.md) normalises both to `/store/f.root`
before anything is sent.

## The first failure is named, rather than the last one

Almost nothing about grid storage fails where it went wrong. A stat that says
`no such file` may mean a proxy expired an hour ago and the server is
answering as nobody; a transfer that hangs may be a firewall three hops away.
The stock clients report the last symptom, and a beginner spends the afternoon
on it.

```console
$ xrd-fs doctor root://eos.example.org//store/user/me
```

`doctor` asks every question a transfer would ask, in the order that makes
each one meaningful, and prints a line for each: the interpreter, the settings
in force, every authentication mechanism with what is missing and the command
that would produce it, DNS, the port, the login, and how far down the path
actually exists. The first `!!` is the thing to fix; everything below it is a
consequence. It never prompts, changes nothing, and exits `1` if any line
failed, so it belongs in a script and in a ticket. `xrdclient.diagnose()` is the
same thing from Python, and returns the report rather than printing it.

One detail is there for beginners specifically: `unix` and `host` are marked
as saying who you are without proving it. Having them "work" is exactly what
makes a permission error look like a missing file.

## What was already true

Behaviour from elsewhere in the library that belongs on the same list:

| Guard | Where |
| --- | --- |
| Every failure raises; there is no status object to forget to check | [Errors](errors.md) |
| Copies verify a checksum by default, and `--verify` makes an unverifiable one an error | [Copying](copying.md#verification) |
| TLS is verified unless `verify_tls=False` is written out in full | [Security](security.md) |
| Tokens, keys and proxies are redacted in logs, `repr`s and tracebacks | [Security](security.md) |
| A world-readable SSS keytab is refused rather than used | [Security](security.md) |
| A listing entry cannot escape the directory it was listed from | [Security](security.md) |
| A settings file may name a `token_file` but never carry a token | [Configuration](config.md) |
| Missing credentials are asked for at a terminal instead of failing obscurely | [Authentication](auth.md#being-asked-for-what-is-missing) |
| A failed multipart upload is aborted rather than left billing | [S3](s3.md#writing) |
| An interrupted transfer can be continued rather than restarted | [Copying](copying.md#resuming) |
| `xrd-fs doctor` names the first thing that is wrong, not the last | above |

## What is deliberately not guarded

`xrd-cp -r --delete` removes files under the destination that the source does
not have, and is not asked about: it is rsync's flag, spelled the same way,
and nobody types it by accident. `remove_source=True` deletes each source
only after its copy has been written and verified. Neither has a shallow-path
guard because both are already an explicit instruction about a specific tree.
