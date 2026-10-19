# API reference

Generated from the source. Everything below is importable from the top-level
`xrdclient` package unless the heading says otherwise.

## One-line verbs

Each of these takes a URL - as text, as an `XRootDURL`, or as an `xrdclient.Path` -
opens a connection, answers the question and closes it again. See
[Easy mode](easy.md).

::: xrdclient.ls

::: xrdclient.glob

::: xrdclient.stat

::: xrdclient.exists

::: xrdclient.size

::: xrdclient.checksum

::: xrdclient.read_bytes

::: xrdclient.read_text

::: xrdclient.write_bytes

::: xrdclient.write_text

::: xrdclient.mkdir

::: xrdclient.remove

::: xrdclient.move

::: xrdclient.stage

::: xrdclient.is_online

## Entry points

::: xrdclient.open

::: xrdclient.FileSystem
    options:
      inherited_members: true

::: xrdclient.BaseFileSystem

`FileSystem` is the `root://` implementation and the dispatcher: given an
`http(s)`/`dav(s)` or `s3` URL it hands back an `HTTPFileSystem` or an
`S3FileSystem`, siblings under `BaseFileSystem` that `isinstance(fs, FileSystem)`
still recognises.

::: xrdclient.File
    options:
      inherited_members: true

::: xrdclient.Checkpoint

## Bulk transfer

Parallel, restartable downloads and pipelined uploads over the bulk data
plane; each takes `cancel=` (an event, or a callable answering "stop now?").

::: xrdclient.client.bulk.download

::: xrdclient.client.bulk.stream

::: xrdclient.client.bulk.upload

::: xrdclient.XRootDPath

## Copying

::: xrdclient.copy

::: xrdclient.copy_tree

::: xrdclient.third_party

::: xrdclient.failed_end

::: xrdclient.CopyResult

::: xrdclient.SyncMode

## Configuration

::: xrdclient.Config

::: xrdclient.find_config_file

::: xrdclient.configure

::: xrdclient.current

::: xrdclient.override

## URLs

::: xrdclient.parse

::: xrdclient.XRootDURL

## Values

::: xrdclient.StatInfo

::: xrdclient.DirEntry

::: xrdclient.ChecksumInfo

::: xrdclient.CheckpointInfo

::: xrdclient.LocationInfo

::: xrdclient.PageResult

::: xrdclient.PrepareStatus

::: xrdclient.ProtocolInfo

::: xrdclient.ReadRange

::: xrdclient.SpaceInfo

::: xrdclient.VFSInfo

::: xrdclient.WriteChunk

::: xrdclient.human_bytes

## Flags

Every one of these accepts its own member names in a string wherever it
accepts bits - `"stage notify"`, `"checksum"`, `"rwxr-x---"` - and prints as
those names rather than as a number. The helpers below turn an ordinary
call's keyword arguments into them.

::: xrdclient.OpenFlags

::: xrdclient.Access

::: xrdclient.MkDirFlags

::: xrdclient.DirListFlags

::: xrdclient.QueryCode

::: xrdclient.StatInfoFlags

::: xrdclient.LocateFlags

::: xrdclient.PrepareFlags

::: xrdclient.flags.permissions

::: xrdclient.flags.open_flags

::: xrdclient.flags.dirlist_flags

::: xrdclient.flags.locate_flags

::: xrdclient.flags.prepare_flags

## Authentication

::: xrdclient.auth.select

::: xrdclient.auth.require

::: xrdclient.auth.supply

::: xrdclient.auth.prompt.Ask

::: xrdclient.auth.prompt.ask_on_terminal

::: xrdclient.auth.prompt.forget

## Errors

`AbortedError` (`errno` `ECANCELED`) is what an aborted or cancelled call
raises; `errno_for` and `kxr_for` translate between `kXR_*` codes and
`errno` as XrdCl does (`XProtocol::toErrno` / `mapError`).

::: xrdclient.errors

## Asynchronous

::: xrdclient.aio

## HTTP and WebDAV

The facade is this package's; the protocol pieces under it - the request
engine, connections, `Expect: 100-continue`, byte ranges, `PROPFIND`
parsing, the Tape REST and HTTP-TPC vocabulary, Metalink - are
[xrdhttp](https://github.com/rob-c/xrdhttp)'s, reachable under their old
`xrdclient.http.*` names (see [Module map](architecture.md#built-on-xrdhttp)).

::: xrdclient.http.third_party

::: xrdclient.http.macaroon

::: xrdclient.http.propfind

::: xrdclient.http.digest

::: xrdclient.http.HTTPClient

## S3

::: xrdclient.s3.S3FileSystem

::: xrdclient.s3.open_s3

`xrdclient.s3.Credentials`, `sign` and `hash_payload` (and the whole of
`xrdclient.s3.sigv4`) are [xrdhttp](https://github.com/rob-c/xrdhttp)'s
SigV4 signer under xrdclient's names, so their reference is xrdhttp's:

::: xrdhttp.s3.sigv4.Credentials

::: xrdhttp.s3.sigv4.sign

## Building blocks for other tools

Credential discovery, TLS contexts, the CLI report, safe XML, backoff and
log redaction are public modules of their own, documented with examples in
[Building a tool on xrdclient](building-tools.md).

`xrdclient.credentials` and `xrdclient.tls` are xrdsec's modules under
xrdclient's names (as are `xrdclient.crypto.*`, `xrdclient.voms_client` and
`xrdclient.testing.pki`/`voms`), so their reference is xrdsec's:

::: xrdsec.credentials

::: xrdsec.tls.client_context

`xrdclient.report`, `xrdclient.safexml` and `xrdclient.retry` are xrdcore's
modules under xrdclient's names, so their reference is xrdcore's:

::: xrdcore.report

::: xrdcore.safexml

::: xrdcore.retry

::: xrdclient.log

## The packages above this one

[`xrdroot`](https://github.com/rob-c/xrdroot), [`xrdml`](https://github.com/rob-c/xrdml) and
[`xrddatasets`](https://github.com/rob-c/xrddatasets) are installed separately and document their
own surfaces.

## Diagnosing

::: xrdclient.diagnose

::: xrdclient.Report

::: xrdclient.Check

## Testing

::: xrdclient.testing.FakeServer

::: xrdclient.testing.from_directory

::: xrdclient.testing.FakeDAVServer

::: xrdclient.testing.FakeS3Server

::: xrdclient.testing.FaultProxy
