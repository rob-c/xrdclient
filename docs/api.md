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

::: xrdclient.File

::: xrdclient.Checkpoint

::: xrdclient.XRootDPath

## Copying

::: xrdclient.copy

::: xrdclient.copy_tree

::: xrdclient.third_party

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

::: xrdclient.errors

## Asynchronous

::: xrdclient.aio

## HTTP and WebDAV

::: xrdclient.http.third_party

::: xrdclient.http.macaroon

::: xrdclient.http.propfind

::: xrdclient.http.digest

::: xrdclient.http.HTTPClient

## S3

::: xrdclient.s3.S3FileSystem

::: xrdclient.s3.open_s3

::: xrdclient.s3.Credentials

::: xrdclient.s3.sign

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
