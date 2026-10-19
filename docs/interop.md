# Interoperability

Everything in this library was written from the XRootD protocol
specification. A specification can be misread, and a test suite written from
the same misreading will pass happily forever. So two suites exist that cannot
be fooled that way.

## Against a real daemon

```console
$ pytest -m interop
```

Starts an unprivileged `xrootd` on a loopback port, exports a temporary
directory, and runs the client against it: open, read, write, `readv`,
`pgread`, truncate, dirlist, stat, mkdir, rm, mv, chmod, xattrs, query,
locate, checksums, third-party copy. Where it matters the stock `xrdcp` and
`xrdfs` binaries read back what this client wrote, and vice versa - bytes
written here are bytes the reference implementation sees.

It has already earned its keep. `kXR_writev` counted its data in `dlen`, which
the in-process fake accepted and the real server refused with
`kXR_ArgInvalid: Write vector is invalid`. The fake now checks the same thing.

Needs `xrootd` on `PATH`; skips otherwise. No root, no configuration file you
have to install.

## Against the official bindings

```console
$ pytest -m parity
```

Same daemon, same files, both clients, field by field. Anything this library
reports that `XRootD.client` does not is a bug in one of them, and the
disagreement is worth more than either answer alone.

The native API deliberately differs in shape - exceptions here, `(status,
result)` tuples there; `bytes` here, buffers there - so the suite asserts that
the *values* agree: the same size, the same mtime, the same flags, the same
checksum, the same directory listing, the same bytes at the same offsets.

Needs the official bindings installed as well; skips otherwise.

One thing to know if both libraries are loaded into the same program. The
official bindings stop their C++ threads from an `atexit` handler, and once
they have talked to a server, a `fork()` anywhere in that process leaves the
handler waiting on threads that the fork left it unable to join: every test
passes and the interpreter then hangs instead of exiting. Nothing here forks
on its own, but a PyTorch `DataLoader` with `workers=` does, which is why this
suite runs its own fork test in a subprocess. A program that needs both can
keep the bindings in a process of their own; a program that only reads with
this library is unaffected.

## Against AWS's own worked examples

The same argument applies to the S3 signature, which is a hash of a string
this client builds: a fake endpoint that verified it with this client's own
signer would agree with any misreading. So two things are true of the test
suite instead. `xrdclient.s3.sign` is asserted against the worked examples the AWS
documentation publishes - the canonical request, the string to sign and the
final signature, byte for byte - and `FakeS3Server` re-derives the signature
from the raw request as it arrived, written out from the specification with
`hmac` and `hashlib` rather than borrowed from the code under test.

## Reading files other tools wrote

There is nothing to say here, which is the point. The wire format is the wire
format: files this client writes are ordinary files, checksums it computes
agree with `xrdfs query checksum`, and third-party copies it initiates are
the same `tpc.*` CGI handshake `xrdcp --tpc` performs. A site does not need to
know which client its users are running.

## What is deliberately not supported

| Not implemented | Why |
| --- | --- |
| GSI signed Diffie-Hellman | refused by name rather than mis-answered |
| X.509 delegation | same |

Each raises an exception that names the feature. A server insisting on one of
them says so at the handshake, not three operations later.

Two more are absent because the protocol retired them, not because this client
declined: `kXR_verifyw` and `kXR_decrypt` were dropped from XProtocol, and
their request codes have since been reissued - 3026 is `kXR_pgwrite` today. A
client still sending the old opcodes would be writing pages while believing it
was verifying them. Their replacements, `pgread` and `pgwrite`, carry a CRC32C
per 4 KiB page and are implemented in full; older clients that still name
`verifyw` are describing a protocol no current server speaks.

## Beyond the specification

`symlink`, `link`, `readlink`, `utime` and `chown` are not in XProtocol at all.
The links are sent as
`kXR_symlink` (3501), `kXR_readlink` (3502) and `kXR_link` (3503), framed like
`kXR_mv` - the same numbers and framing XRootD.jl and XrdRust chose, so the
three clients agree with each other and a server taught one of them
understands all three. A stock daemon answers `kXR_Unsupported`, which is the
honest answer for a namespace with no links, and this client turns it into
`UnsupportedError` rather than pretending. The HTTP and WebDAV backends raise
the same exception without a round trip, because there is no verb to try.

`utime` and `chown` share one opcode below them, `kXR_setattr` (3500): a
44-byte big-endian block - flags, `atime` and `mtime` as second/nanosecond
pairs, `uid`, `gid` - and then the path. The nanosecond fields carry
`utimensat`'s `UTIME_NOW` and `UTIME_OMIT`, so "now" is the *server's* now, and
an id of `-1` means "not this one", exactly as `chown(2)` has always read it.
Mode is deliberately not in the block, because `kXR_chmod` already carries it.
Neither call follows a final symbolic link: the server uses
`AT_SYMLINK_NOFOLLOW`, so on a link they change the link.

None of these is safe to send blindly, and the server says so itself: the
`xrdfs.ext` configuration key answers with the vendor opcodes it implements,
which is what `fs.extensions()` reads. The value repeats the key
(`xrdfs.ext=setattr,symlink,readlink,link`), as the server that introduced it
emits it; a stock daemon echoes an unknown config key back unchanged, so the
reply that means "none of them" is indistinguishable from a reply of no
extensions at all, and both parse to an empty set. The native FUSE client
gates its vendor opcodes on the same key.

Three option bits are vendor extensions of standard requests rather than new
requests. `kXR_statNoFollow` (`0x40` of the `kXR_stat` options byte, the value
nginx-xrootd reads) is what `lstat` and `stat(follow_symlinks=False)` set;
XProtocol's stat options stop at `kXR_vfs`, and XRootD.jl picked `0x02` for the
same idea, which no server implements. `kXR_clone` (3032) is one past
`kXR_REQFENCE` - see [Files](files.md#copying-ranges-without-moving-them).
`kXR_fa_recurse` (`0x20` of the `kXR_fattr` options byte) is the third, and the
one that fails most quietly: it turns a list of a directory's own attributes
into a walk of the files beneath it, and the reply it comes back with is not a
`kXR_fattr` reply at all but a flat run of `relpath:name\0` entries with no
count and no per-attribute status. A server that does not know the bit ignores
it and answers the ordinary list, which is why `listxattr_tree` parses an
answer with no entries in it as an empty tree rather than raising - there is no
way to tell "no attributes anywhere" from "not implemented here". The server
also caps the reply at 256 KiB and drops what does not fit, without a flag to
say it did, so a subtree big enough to matter is one to walk instead.

`kXR_evict` is the opposite case: a real protocol flag that is easy to send
wrongly. It is `0x0001` of `optionX`, the extended half-word four bytes into a
`kXR_prepare` parameter area, not `128` of the options byte - `128` there is
`kXR_usetcp`, so a client that packs `evict` as a byte flag quietly asks for a
TCP stage-in and never evicts anything. `PrepareFlags.EVICT` is spelled `1 << 8`
for that reason and `Prepare` puts it where the server looks.

## Version coverage

Tested against XRootD 5.x. The client announces protocol `0x520` (v5.2) in
`kXR_protocol`, which is what enables `kXR_pgread` and `kXR_pgwrite`. Those
two are asked for explicitly - `File.pgread` and `File.pgwrite` - and a server
that does not implement them says so rather than being guessed at; ordinary
`read`/`write` work everywhere and are what everything else uses.

## Python versions

3.11 and newer. The floor is what the grid's login nodes can actually be
given: AlmaLinux 8 and 9 offer `python3.11` and `python3.12` from AppStream
(no root-free `pip install` of an interpreter required), and AlmaLinux 10 and
Ubuntu 24.04 ship 3.12 as `python3`. CI runs the full suite on 3.11 through
3.14.

Nothing about newer syntax is banned in principle; what is banned is being
unable to notice. A `type` alias statement or `class Box[T]` reads perfectly
well on 3.13 and stops dead on 3.11, and a suite that only ever runs on 3.13
will never say so. So `tests/test_compat.py` parses every shipped module with
the 3.11 grammar, reads every module for the handful of standard-library names
that parse anywhere but only *resolve* on 3.12 or later (`itertools.batched`,
`typing.override`, `warnings.deprecated`), and - when a `python3.11` is on
`PATH` with the base dependencies installed - imports the whole package into
it.

With the floor at 3.11 the package no longer carries a compatibility layer:
dataclasses are `slots=True`, pairs are `zip(..., strict=True)`, flags iterate
into their bits natively, and `except TimeoutError` covers a socket timeout.

## Coming from `pyxrootd`

See [Coming from pyxrootd](migrating.md) for the call-by-call mapping.

## The compatibility layer

[`xrdclient.compat.client`](compat.md) is the one place the shapes are meant
to match too, and `tests/test_pyxrootd_compat.py` holds it to that: every
covered call goes through both libraries against the same daemon, and the
`(XRootDStatus, response)` pairs must be equal attribute for attribute,
statuses' codes and errnos included. It runs under the same `-m parity`.
