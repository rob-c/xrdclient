# Namespaces

`xrdclient.FileSystem` is one endpoint's namespace. It holds a connection, so use
it as a context manager or call `close()`.

```python
with xrdclient.FileSystem("root://eos.example.org") as fs:
    ...
```

Paths may be absolute or relative to the URL's own path.

## Reading the namespace

| Method | Notes |
| --- | --- |
| `stat(path)` | a `StatInfo` with `st_size`, `st_mtime`, `flags`, `id` |
| `statx(paths)` | many paths, one round trip |
| `statvfs(path)` | free space, utilisation, node counts |
| `exists`, `isdir`, `isfile`, `getsize` | the `os.path` spellings |
| `listdir(path)` | names only |
| `scandir(path)` | `DirEntry` objects with `stat` attached |
| `iterdir(path)` | the same, as an iterator |
| `walk(top)` | `os.walk`'s `(root, dirs, files)` triples |
| `glob(pattern, root="")` | `pathlib` semantics, absolute paths out |

```python
for entry in fs.scandir("/store"):
    print(entry.name, entry.is_dir(), entry.stat.st_size)
```

`DirEntry` is `os.PathLike`, so `open(entry)` and `os.fspath(entry)` work, and
printing one prints its path.

A listing asks the server for a stat per entry, which is what you almost
always want and is one flag on the request. Say so in words rather than bits:

```python
fs.scandir("/store", stat=False)     # names only, and cheaper for a huge directory
fs.scandir("/store", online=True)    # ask which entries are on disk rather than on tape
```

A server can also digest every file as it lists, which turns a directory's
worth of `checksum` calls into one round trip:

```python
for entry in fs.scandir("/store", algorithm="adler32"):
    print(entry.name, entry.checksum)       # ChecksumInfo, or None
```

`entry.checksum` is `None` for anything the server had no digest for - a
directory, or a file it could not read. An algorithm the server does not
compute fails the whole listing rather than some of its entries, so ask for
one `checksum()` names.

### glob

The pattern language is `pathlib.Path.glob`'s, not `fnmatch`'s: `*` and `?`
and `[...]` stop at a `/`, and `**/` is *zero or more* directories.

```python
fs.glob("/store/user/me/*.root")        # one level
fs.glob("/store/user/me/**/*.root")     # any depth, including none
fs.glob("run[0-9]*/*.root", root="/store/user/me")
```

A pattern whose magic is confined to its last component costs one `scandir`.
Anything deeper walks, but only from the deepest wildcard-free directory in
the pattern - `/store/user/me/**/*.root` never lists `/store/user`.

!!! note
    An earlier version matched recursive patterns against the path *relative*
    to the search root, which quietly returned nothing for an absolute
    pattern. It was found by running the same glob against a real daemon; see
    [interoperability](interop.md).

## Changing the namespace

```python
fs.mkdir("/store/user/me/2026")
fs.makedirs("/store/a/b/c", exist_ok=True)
fs.touch("/store/f.root", exist_ok=True)
fs.rename("/store/a.root", "/store/b.root")
fs.remove("/store/b.root")          # also spelled unlink
fs.rmdir("/store/empty")
fs.rmtree("/store/user/me/scratch")
fs.truncate("/store/f.root", 4096)
fs.chmod("/store/f.root", 0o640)                 # or "rw-r-----"
fs.utime("/store/f.root")                       # os.utime: both times, now
fs.utime("/store/f.root", (atime, mtime))       # seconds, as floats
fs.utime("/store/f.root", ns=(atime_ns, mtime_ns))
fs.chown("/store/f.root", uid, gid)             # -1 leaves an id alone
```

!!! warning "`utime` and `chown` are the same vendor extension the links are"
    They travel as `kXR_setattr` (3500), a 44-byte attribute block followed by
    the path - what XRootD.jl encodes and nginx-xrootd decodes. A stock daemon
    answers `UnsupportedError`. Neither call follows a final symbolic link:
    the server applies both the way `os.utime(..., follow_symlinks=False)` and
    `os.lchown` do. Ids are numeric, because the names belong to the server's
    passwd file rather than to yours.

### Links

```python
fs.symlink("/store/f.root", "/store/latest")    # os.symlink order: target, link
fs.readlink("/store/latest")                    # '/store/f.root'
fs.link("/store/f.root", "/store/second")       # os.link order; also fs.hardlink

fs.is_symlink("/store/latest")                  # True
fs.lstat("/store/latest")                       # the link, not its target
fs.stat("/store/latest", follow_symlinks=False) # the same thing, spelled os's way
```

`is_symlink` asks the server for a `readlink` rather than comparing two
stats, because that is the question with an unambiguous answer: a stat that
followed a link is indistinguishable from a stat of a file that never was
one. `lstat` sets `kXR_statNoFollow`, another vendor bit (`0x40`, what
nginx-xrootd reads); a server without it follows the link as it always did
and says nothing about having done so - so where the distinction matters,
ask `is_symlink` first.

!!! warning "A vendor extension, not XProtocol"
    XProtocol has no link opcodes. These use `kXR_symlink` (3501),
    `kXR_readlink` (3502) and `kXR_link` (3503) - the numbers XRootD.jl and
    XrdRust settled on - framed like `kXR_mv`. A stock daemon answers
    `kXR_Unsupported`, which surfaces as `UnsupportedError`; HTTP and WebDAV
    have no verb for them and raise the same thing without a round trip. The
    argument order is `os`'s in both cases, so the calls read the way a Python
    programmer already expects.

!!! note "`touch` is not `open(..., "a")`"
    A stock server refuses to create a file for `kXR_open_updt` alone, so
    `touch` asks for `kXR_new | kXR_open_updt | kXR_mkpath` and treats
    `FileExistsError` as success when `exist_ok`. Doing it the obvious way
    fails on a real daemon while passing against a lenient fake.

## Whole-file shortcuts

```python
fs.write_bytes("/store/f.root", payload)
data = fs.read_bytes("/store/f.root")
fs.write_text("/store/note.txt", "hello")
text = fs.read_text("/store/note.txt")
fh = fs.open("/store/f.root", "rb")     # shares this connection
```

## Asking the server about itself

```python
fs.ping()                     # returns None; raises if it is not there
fs.protocol()                 # ProtocolInfo: version, flags, capabilities
fs.query_config("version", "role", "sitename")
fs.query("space", "/store")   # or QueryCode.SPACE
fs.checksum("/store/f.root")            # ChecksumInfo(algorithm, value)
fs.locate("/store/f.root")              # which servers hold it
fs.locate("/store/f.root", refresh=True)  # ignore whatever the redirector cached
fs.locate("/store/run7", flags="for_dirlist")     # about to list it
fs.locate("/store/new.root", create=True)  # where it would go, before it exists
fs.deep_locate("/store/f.root")         # follow managers to the data servers
fs.prepare(["/store/a.root", "/store/b.root"])   # stage from tape
fs.prepare(["/store/a.root"], notify=True)       # and mail me when it lands
fs.evict(["/store/a.root"])                     # prepare(evict=True), spelled out
fs.statvfs("/store")
fs.query_space("/store")      # SpaceInfo: one space token, in bytes
fs.query_stats("a")           # the server's XML statistics summary
fs.extensions()               # which vendor opcodes it admits to having
```

### Asking before sending a vendor opcode

`symlink`, `link`, `readlink`, `utime` and `chown` are not XProtocol; a stock
daemon answers `UnsupportedError`. A server that does implement them says so
in its `xrdfs.ext` configuration value, and `extensions()` is that one round
trip:

```python
if "setattr" in fs.extensions():
    fs.utime("/store/f.root")
```

The names are the server's own - `setattr`, `symlink`, `readlink`, `link` -
and a server that has never heard of the key answers by echoing it back or by
saying nothing, both of which arrive as an empty set. That is the right answer
for a stock daemon, and the reason this is worth asking once rather than
catching an exception per call. Over `davs://` it returns an empty set with no
request at all, so a program that guards on it has one code path for both
schemes.

`statvfs()` and `query_space()` answer different questions. `statvfs()`
describes the whole storage element the way `df` would, in megabytes and
percentages; `query_space()` describes the one space token - the pool an
`oss.cgroup` write lands in - in bytes, and reports its quota:

```python
space = fs.query_space("/store")
space.free, space.total, space.largest_free      # bytes
space.unlimited                                  # quota is -1, not zero
```

`prepare()` hands back a request handle. It returns as soon as the server has
taken the request down, which at a tape site is a long time before anything is
readable, so the handle is what the interesting question is asked with:

```python
handle = fs.prepare(["/store/a.root", "/store/b.root"])
for status in fs.query_prepare(handle, ["/store/a.root", "/store/b.root"]):
    print(status)                # /store/a.root: online
    status.online                # on disk, and readable now
    status.on_tape               # still only on tape
    status.requested             # this request is the one that asked for it
```

One `PrepareStatus` per path, in the order asked, and each is true when its
file is online - so `all(fs.query_prepare(handle, paths))` is "the stage has
finished". A handle the server never issued is an error rather than a list of
files it says nothing about.

A request that has not run yet can be withdrawn by the same handle:

```python
fs.cancel_prepare(handle)
```

Where a file is *now* is a separate question, and asking it stages nothing:

```python
for report in fs.archive_info(["/store/a.root", "/store/b.root"]):
    print(report.path, report.state)         # /store/a.root NEARLINE
    report.online                            # readable without a wait
```

That is one `statx` over `root://` - the protocol's offline flag is the whole
answer - and `POST /api/v1/archiveinfo` over `davs://`, which is why the
`state` word is the tape API's (`ONLINE`, `NEARLINE`, `DISK`, `TAPE`) on both.

Cancelling names the request, not the files, which is why it is a separate
method rather than a flag: passing paths here would ask the server to cancel
whatever requests have handles that look like filenames.

A checksum the server is still computing can be called off the same way, this
one by path:

```python
fs.checksum_cancel("/store/big.root")
```

## Labelling a connection

Site monitoring records who is doing what, and "python" is not an answer
anybody can act on:

```python
fs.appid("higgs-skim")               # shows up in the server's monitoring
fs.set_property("monitor off")       # kXR_set, for anything else
```

`appid()` is `set_property("appid ...")` with the string built for you. The
directive goes to the server verbatim, so `set_property()` is also how a new
one reaches you without waiting for a release.

`protocol()` answers what kind of server this is and what it can do, one
question per property rather than a flags word to mask by hand:

```python
info = fs.protocol()
info.version_str                    # '5.6.0'
info.is_server, info.is_manager     # a data server, or something that redirects
info.is_meta, info.is_supervisor, info.is_proxy, info.is_cache
info.supports_pgio                  # pgread/pgwrite, with a CRC per page
info.supports_posc                  # a failed write leaves no file behind
info.supports_gpfile, info.allows_anonymous_gpfile
info.has_tls, info.requires_tls_for_data
```

`checksum()` asks for `config.preferred_checksum` (`adler32` by default) and
returns whatever the server actually computed, which is not always what you
asked for. Anything the server names can be verified here without a compiler:
`adler32`, `crc32`, `crc32c`, `crc64` (CRC-64/XZ, what `xrdcrc64` computes,
also spelled `crc64xz`), `crc64nvme` and the `hashlib` digests -
`xrdclient.crypto.algorithms()` is the list.

!!! warning "Servers cache checksums"
    A file rewritten in place can come back with the digest of its previous
    contents. If you need certainty after a write, checksum a fresh path.

## Extended attributes

```python
fs.setxattr(path, "user.experiment", b"atlas")
fs.setxattr(path, "user.run", b"1", create_only=True)   # refuses to overwrite
fs.getxattr(path, "user.experiment")
fs.listxattr(path)
fs.xattrs(path)
fs.removexattr(path, "user.experiment")
```

`kXR_fattr` reports one `errno` per attribute *inside* a successful reply, so
a request that failed for the attribute you named still comes back as
`kXR_ok`. The client reads that code and raises: `FileExistsError` for a
`create_only=True` name that is already there, `AttrNotFoundError` (an
`OSError` with `ENODATA`) for removing one that is not.

A whole subtree's names come back in one round trip:

```python
fs.listxattr_tree("/store/run7")
# {'a.root': ['user.experiment'], 'sub/b.root': ['user.run', 'user.experiment']}
```

The paths are relative to the directory asked about, only files that have
attributes appear, and the values are not included - the reply has nowhere to
put them.

!!! warning "`listxattr_tree` is a vendor extension, and a quiet one"
    It sets `kXR_fa_recurse` (`0x20`), which only nginx-xrootd implements. A
    server that does not know the bit ignores it and lists the *directory's*
    own attributes, whose reply has a different shape and parses here as an
    empty tree - so an empty answer means "nothing, or nobody listening", and
    is not something to branch on. The server also caps its reply and drops
    the remainder of a large tree without saying so. Where either matters,
    `fs.walk()` and a `listxattr` per file is the answer you can trust.

## Sharing a connection

Every object that talks to a server accepts an existing router, and
`FileSystem.open()` hands its own to the file it opens. Opening a hundred
files through one `FileSystem` costs one connection, not a hundred.

```python
with xrdclient.FileSystem("root://host") as fs:
    handles = [fs.open(f"/store/{n}", "rb") for n in names]
```

A handle that opens its own connection closes it, including when the open
itself fails - a subtlety that cost a socket leak per caught `FileExistsError`
until the interop suite caught it.
