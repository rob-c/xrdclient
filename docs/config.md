# Configuration

`Config` is a frozen dataclass. Build one, pass it to whatever you are opening,
and derive variants with `evolve`.

```python
cfg = xrdclient.Config(request_timeout=60.0)
patient = cfg.evolve(request_timeout=600.0)

xrdclient.open("root://host//store/f.root", config=cfg)
xrdclient.FileSystem("root://host", config=cfg)
xrdclient.copy(src, dst, config=cfg)
```

Frozen because a configuration is shared by every session it reaches, and a
setting that changes underneath a live connection is a bug you find in
production. `evolve` returns a new one.

## Timeouts and retries

| Field | Default | Environment | Meaning |
| --- | --- | --- | --- |
| `connect_timeout` | `30.0` | `XRD_CONNECTIONWINDOW` | name lookup plus TCP connect, all addresses together |
| `request_timeout` | `300.0` | `XRD_REQUESTTIMEOUT` | one request/response |
| `connect_retries` | `3` | `XRD_CONNECTIONRETRY` | reconnection attempts |
| `retry_backoff` | `0.5` | `XRD_RETRYBACKOFF` | first backoff ceiling, then doubling; jittered |
| `redirect_limit` | `16` | `XRD_REDIRECTLIMIT` | redirects before giving up |
| `wait_cap` | `600.0` | | ceiling on a server-requested wait |
| `stall_deadline` | `1800.0` | `XRD_STALLDEADLINE` | one whole operation, first byte to last |
| `wait_budget` | `1800.0` | `XRD_WAITBUDGET` | total parking one operation may be asked for |
| `tpc_timeout` | `1800.0` | `XRD_CPTPCTIMEOUT` | a `root://` third-party copy's pull, start to finish (XrdCl's `CPTPCTimeout`); `0` leaves it to the limits above |
| `keepalive_interval` | `60.0` | `XRD_TCPKEEPALIVETIME` | idle seconds before TCP keepalive probes start; `0` turns them off |
| `network_stack` | `"IPAuto"` | `XRD_NETWORKSTACK` | address families: `IPAuto`, `IPv4`, `IPv6`, `IPv4Mapped6` |

`request_timeout` bounds a single read from the socket and `stall_deadline`
bounds the operation those reads add up to: a server that dribbles a byte at a
time, or that says "still working" forever, never trips the first and is
exactly what the second is for. A `kXR_wait` restarts the deadline — a delay
the server declared is not a stall — but the delays are added up against
`wait_budget`, so a redirector cannot park a caller indefinitely one polite
minute at a time. Both take `0` to wait forever.

A third-party copy's pull is the exception to all three: the destination
answers its `kXR_sync` only when the whole file has arrived, and is silent
until then. `third_party` gives that one connection `tpc_timeout` for each of
`request_timeout`, `stall_deadline` and `wait_budget` (when it is longer), and
expires the pull itself once `tpc_timeout` has passed - XrdCl's `CPTPCTimeout`.
The copy's own `timeout=` argument, when given, takes its place.

Every retry loop in the package - reconnecting, re-opening, resuming a copy,
a bulk worker getting its span back, an HTTP read - waits the same way:
exponential with **full jitter**. The *n*-th wait is drawn uniformly between
zero and `min(wait_cap, retry_backoff * 2**(n-1))` (a bulk worker caps it at
5 s), and never runs past a [`deadline`](errors.md). The jitter is what stops
a farm of jobs that lost the same data server from coming back to it in
lockstep, retry after retry.

**Connecting.** `connect_timeout` is one budget for the whole connect: the
name lookup (which runs on a helper thread so a stuck resolver cannot exceed
it) and every address the name has. Those addresses are raced as RFC 8305
("Happy Eyeballs") describes - families interleaved, a new attempt every
250 ms or as soon as the previous one is refused, the first to connect wins -
so a host with a black-holed IPv6 address costs a quarter of a second, not the
whole window. `network_stack` restricts the families, with XrdCl's names:
`IPv4` or `IPv6` only, or `IPv4Mapped6` for IPv6 sockets throughout (IPv4
addresses reached as `::ffff:a.b.c.d`). When nothing connects, the error lists
every address and what happened to it.

**Keepalive.** Control and data connections have TCP keepalive on: after
`keepalive_interval` idle seconds the kernel starts probing, a third of that
apart, and three unanswered probes drop the connection - so a peer that
vanished without closing (a rebooted node, a NAT that dropped the flow) is
noticed in about twice the interval instead of at the next request's
`request_timeout`. The per-socket timings are set where the platform has them
(`TCP_KEEPIDLE` on Linux, `TCP_KEEPALIVE` on macOS, `TCP_KEEPINTVL`,
`TCP_KEEPCNT`).

**There is no `stream_timeout`.** XrdCl's `StreamTimeout` is how long a
connection with requests outstanding may stay silent; here that has always
been `request_timeout`, which bounds every socket read, and an idle *pooled*
connection is retired by `pool_idle_ttl`. The field never had an effect and
was removed in 0.4.0, with `$XRD_STREAMTIMEOUT`. (The pyxrootd layer's
`StreamTimeout` maps to `request_timeout`.)

**A malformed variable is reported.** `XRD_REQUESTTIMEOUT=5m` is not a
number; the default is used, as before, but a warning on the
`xrdclient.config` logger now names the variable, the value and the default
- once per variable per process.

`XRD_RETRYBACKOFF` is this package's own name. The official client's
`XRD_STREAMERRORWINDOW` looks similar but is not a backoff: it is how long a
stream error is remembered (1800 s by default), so it is deliberately not read
here - a site environment that sets it must not park every reconnect for ten
minutes.

## Transfers

| Field | Default | Environment |
| --- | --- | --- |
| `bulk_workers` | `2` | `XRD_BULKWORKERS` |
| `bulk_chunk` | 4 MiB | `XRD_BULKCHUNK` |
| `bulk_recovery_chunk` | 64 KiB | `XRD_BULKRECOVERYCHUNK` |
| `bulk_depth` | `4` | `XRD_BULKDEPTH` |
| `bulk_recovery` | 120 s | `XRD_BULKRECOVERY` |
| `chunk_size` | 4 MiB | `XRD_CPCHUNKSIZE` |
| `readahead` | 1 MiB | `XRD_READAHEAD` |
| `parallel_chunks` | `4` | `XRD_CPPARALLELSPANS` |
| `parallel_files` | `1` | `XRD_CPPARALLELFILES` |
| `in_flight` | `2` | `XRD_CPINFLIGHT`, else `XRD_CPPARALLELCHUNKS` |
| `data_streams` | `1` | `XRD_SUBSTREAMSPERCHANNEL` |
| `data_stream_timeout` | 2 s | `XRD_SUBSTREAMTIMEOUT` |
| `max_read_size` | 1 GiB | `XRD_MAXREADSIZE` |

`parallel_chunks` is how many connections one large copy is spread over, a
span of the file each; `1` keeps the single stream. See
[Copying](copying.md#several-connections-at-once) for when it applies.
`parallel_files` is how many files of a `copy_tree` are in flight at once,
and defaults to one because each of them is already spread over
`parallel_chunks`. `in_flight` is how many chunks a transfer reads ahead of
the write it is waiting on, so that the two ends overlap; `1` is the strictly
sequential pump, and is what a copy between two local disks wants.
`$XRD_CPPARALLELCHUNKS` sets `in_flight`, because that is what the variable
means to XrdCl and `xrdcp`: chunks in flight, not connections. (Earlier
releases read it into `parallel_chunks`; that is `$XRD_CPPARALLELSPANS` now.)
`data_streams` is how many extra `kXR_bind` sub-streams a file uses, so a
plain read or write already travels beside the control traffic instead of
behind it; it is on by default (one extra link). The sub-streams belong to the
connection, as XrdCl's belong to its channel: the first file opened on a
connection binds them, and every later file on the same pooled connection
reuses them, so a warm open costs one `kXR_open` round trip and no extra
connect. The official client counts the
control link in its total, so `XRD_SUBSTREAMSPERCHANNEL=1` means "control only"
and turns the extras off — our field is the extras. It is best-effort, in two
steps. The whole request goes down the bound link first, for a server that
serves what arrives there; a server that does not gets the standard split
instead — the request on the control link, the bytes on the bound one — which
is what XProtocol describes, so the transfer stays multi-stream on a stock
daemon. Only a server that declines both drops back to the control link, where
the identical read or write re-runs at the same offset, byte-exact anywhere.
`data_stream_timeout` bounds how long that first attempt waits — kept short
because a server that serves the op answers at once. Which of the two a server
wants is a property of the server, so it is asked **once per connection**: the
first file pays the timeout, and every later file on the same connection goes
straight to the split that works.
From the command line the field is `xrd-cp --streams N`, where `0` asks for
the control link alone.
`max_read_size` is the ceiling on a read that never said how much it wanted -
`read()` with no argument, `read_bytes()`, `read_text()` - so that a file
bigger than memory raises [`TooLargeError`](errors.md#too-much-at-once)
instead of filling it. A read that names a size is never touched by it, and
`0` lifts it entirely. See [Safety](safety.md).

## Pooling

| Field | Default | Environment |
| --- | --- | --- |
| `pool_size` | `8` | `XRD_POOLSIZE` |
| `pool_idle_ttl` | `120.0` | |

One `FileSystem` (or one `fsspec` instance) owns one multiplexed connection
and reuses it for every call. When it closes, the connection is not dropped:
it goes into a process-wide pool, and the next `FileSystem` opened on the same
server *as the same person* picks it up instead of repeating the handshake,
the TLS negotiation and the login. A script that opens a `FileSystem` per file
pays for one bring-up rather than a hundred.

```python
from xrdclient.session import SESSIONS

len(SESSIONS)      # connections being held open right now
SESSIONS.clear()   # end them all, politely
```

Reuse is deliberately narrow, because sharing an authenticated connection with
the wrong caller is an authentication bug:

- The endpoint must match, TLS included.
- Every credential-bearing setting must match - `username`, `token`,
  `token_file`, `keytab`, `proxy`, `ca_path`, `ca_file`, `auth_order`,
  `verify_tls`, `require_tls`, plus any user in the URL. They are compared as
  a SHA-256 digest so that no key of the pool's ever holds a token.
- A connection that failed under a live handle is discarded, never pooled.
- Only idle connections are shared. Two `FileSystem` objects open at once are
  two connections; pooling reuses what is finished with, and does not
  multiplex what is not.

`pool_size` is how many idle connections are kept per server, `pool_idle_ttl`
how long one may sit unused before it is closed rather than handed on -
protection against a server that has forgotten a connection the client still
believes in. `pool_size = 0` turns pooling off entirely.

## Security

| Field | Default | Environment |
| --- | --- | --- |
| `token` | `None` | (`$BEARER_TOKEN` is discovered separately) |
| `token_file` | `None` | `BEARER_TOKEN_FILE` |
| `keytab` | `None` | `XrdSecSSSKT`, `XrdSecsssKT` |
| `proxy` | `None` | `X509_USER_PROXY` |
| `usercert` | `None` | `X509_USER_CERT` |
| `userkey` | `None` | `X509_USER_KEY` |
| `key_password` | `None` (ask through `prompter`, if anyone can be asked) | |
| `ca_path` | `None` | `X509_CERT_DIR` |
| `ca_file` | `None` | `SSL_CERT_FILE` |
| `tls_system_ca` | `None` (by scheme) | `XRD_TLSSYSTEMCA` |
| `check_crl` | `""` (off) | `XRD_CHECKCRL` |
| `auth_order` | `("gsi", "ztn", "krb5", "sss", "unix", "host")` | |
| `verify_tls` | `True` | |
| `require_tls` | `False` | |
| `trusted_redirect_domains` | `()` (credentials stay with their origin) | `XRD_TRUSTEDREDIRECTDOMAINS` |
| `ztn_cleartext` | `False` | `XRD_ZTNCLEARTEXT` |
| `gsi_delegate` | `False` | `XrdSecGSIDELEGPROXY` (a positive number turns it on) |
| `gsi_verify_server` | `True` | `XRD_GSI_VERIFY_SERVER` (`0` turns it off) |
| `prompt` | `None` (ask only at a terminal) | `XRD_PROMPT` |
| `prompter` | `None` (ask on the terminal) | |

See [Authentication](auth.md) for what each mechanism looks for.

`ztn_cleartext` opts back into offering a bearer token on a connection that
is not TLS, which the client otherwise refuses to do - a token sent in the
clear is a token anyone on the path can replay. Prefer `roots://`.

`usercert` and `userkey` are a user certificate and its private key kept in
two files (`usercert.pem`, `userkey.pem`) rather than combined as a proxy is.
They are used when both are set and no `proxy` is - gfal2's order: a named
proxy, then this pair, then the default proxy location. HTTPS and `roots://`
present them as the TLS client certificate; GSI logs in with a 12-hour proxy
it makes from them in memory, as `XrdSecgsi`'s `createpxy` does. Nothing is
written to disk either way: the key is read once, refused unless it is a
regular file of yours that group and others cannot read (`chmod 600`), and
handed to OpenSSL through a pipe. An encrypted key is unlocked with
`key_password` - a callable returning the pass phrase, asked only when the key
needs one - or, without it, by asking through `prompter` when `prompt` allows;
otherwise it is refused with `CredentialError`.

Two TLS settings say which CAs and revocation lists are consulted (see
[Security](security.md#which-cas-are-trusted)); both default from the
environment variables they always read:

| Field (variable) | Default | Effect |
| --- | --- | --- |
| `tls_system_ca` (`XRD_TLSSYSTEMCA`) | `None`: by scheme | `True` (`1`) adds the system CA store to `ca_path`/`ca_file` for every scheme; `False` (`0`) never does. `None`: `https://` and `s3://` add it and `davs://`/`roots://` do not. Without a grid CA directory the system store is used regardless. |
| `check_crl` (`XRD_CHECKCRL`) | `""`: off | `"leaf"` (`1`) checks the server certificate against the CRLs (`<hash>.r0`) in `ca_path`; `"chain"` checks every certificate of its chain. Needs `ca_path`; ignored with `verify_tls=False`. |

`gsi_delegate` delegates the X.509 proxy to a GSI server that asks for one -
what a third-party copy needs from its destination. The server's certificate
is checked against `ca_path` and the host name first; see
[Delegation](auth.md#delegation).

`gsi_verify_server` makes every GSI login check the server's certificate -
CA chain and host name, widened by `$XrdSecGSISRVNAMES` patterns - before
the proxy chain is sent, as `XrdSecgsi` clients do. Turn it off only for a
test setup that cannot pass; see [The server proves who it is](auth.md#the-server-proves-who-it-is).

`token_file` set to anything other than `$BEARER_TOKEN_FILE` (its default) is
read *before* `$BEARER_TOKEN`; left at the default it is read after it, as the
WLCG discovery order says. Token files, proxies and keytabs must be mode
`0600` and yours - see [Credential files](auth.md#credential-files-must-be-yours-alone).

## Behaviour

| Field | Default | Meaning |
| --- | --- | --- |
| `recover_handles` | `True` | silently re-open a read-only file whose data server vanished mid-read |
| `verify_checksums` | `True` | compare checksums after a copy |
| `preferred_checksum` | `"adler32"` | algorithm asked for first |
| `metalink_processing` | `True` | treat `.meta4` and `.metalink` sources as replica catalogues |
| `tls_metalink` | `False` | upgrade `root`/`xroot` Metalink replicas to TLS |
| `max_metalink_wait` | `60.0` | `kXR_wait` budget for a replica while another remains |
| `zip_metalink_checksum` | `False` | with a selected ZIP member, use the Metalink checksum |
| `s3_folder_markers` | `False` | make `mkdir` on S3 write a zero-length `dir/` marker object |
| `local_redirect` | `"local"` | follow a `file://` redirect: `never`, `local` (only from a server on this host), `always`; `$XRD_LOCALREDIRECT` |
| `aio_workers` | `32` | threads `xrdclient.aio` runs calls on; `$XRD_AIOWORKERS` |

`recover_handles=False` turns a lost data server into a `TransientError` at the
call that hit it, which is what you want when your job would rather fail than
re-read.

**Going back to the manager.** When a data server a manager sent a request to
fails it - the connection drops, or it answers `kXR_IOError`, `kXR_FSError`,
`kXR_ServerError`, `kXR_NotFound`, `kXR_Overloaded` or `kXR_NoMemory` - the
request goes back to the manager with `tried=<host>` (every server that has
failed it so far, comma-joined) and `triedrc=<reason>` (`enoent`, `ioerr`,
`fserr`, `srverr`) in its CGI, as XrdCl does, so the manager routes around it;
a missing file also sets `kXR_refresh` on the open. A re-opened file
(`recover_handles`) does the same. Only requests that are safe to repeat are
sent back - a write is not - and each trip back counts against
`redirect_limit`, so a file that is nowhere still ends with the server's own
error.

**Local redirects.** XCache and disk-local sites can answer an open with a
redirect to `file://localhost/path`: the file is on a disk this machine can
read. As XrdCl's `LocalFileHandler` does, the file is then read through the
operating system, with the same `File` API - open, read, vector read, stat,
close; a write, or a server-side operation such as a checksum, is refused with
`UnsupportedError`. `local_redirect` decides whether to follow one: by default
only when the server that sent it is on this host (a loopback address, or the
host's own), since a remote server that could point a client at any local file
could have a copy job upload it anywhere; `always` follows them from anywhere,
as XrdCl does, and `never` refuses them.

## Username

```python
Config(username="atlasprd")
```

Defaults to `$XRD_USER`, `$USER`, `$LOGNAME`, then `getpass.getuser()`, then
`"nobody"` - so it is never blank even in a container without a passwd entry.

## Secrets never print

```python
>>> xrdclient.Config(token="eyJhbGciOi...")
Config(username='me', ..., token='<redacted>', ...)
```

The `repr` redacts, and so does every log record - see [Security](security.md).

## The settings file

An INI file, read by `Config.from_file` and by both commands via `--config` /
`--alias`:

```ini
[defaults]
username = atlasprd
request_timeout = 600
verify_checksums = true

[alias eos]
token_file = /run/user/1000/bt_u1000
preferred_checksum = adler32
```

```python
cfg = xrdclient.Config.from_file()                  # the usual places
cfg = xrdclient.Config.from_file(alias="eos")       # [defaults], then [alias eos]
cfg = xrdclient.Config.from_file("./job.ini")       # exactly this file
```

```console
$ xrd-fs ls --alias eos root://eos.example.org//store/user/me
$ xrd-cp --config ./job.ini /tmp/f.root root://host//store/f.root
```

`[defaults]` is applied first and the alias overlays it, so an alias only says
what it changes. Field names are the `Config` field names, with `-` accepted
for `_`; values are typed by the field, and booleans take `configparser`'s
vocabulary (`true`, `yes`, `on`, `1`). Anything on the command line beats the
file, and the file beats the environment.

Looked for in this order:

| Where | Note |
| --- | --- |
| `$XRD_CONFIG` | wins outright, and must exist - a typo there is an error |
| `~/.config/xrd/config.ini` | |
| `~/.xrdrc` | |

No file at all means the defaults, which is what an absent dotfile should
mean. An `--alias` that the file does not define is an error naming the
aliases it does, because a typo there would quietly connect as somebody else.

Three settings are refused in a file: `prompter` and `key_password`, which
are callables and cannot be spelled in INI, and `token`, because a literal bearer token in a dotfile is
a secret in every backup of that dotfile - say `token_file` instead.

## Environment only

If you set nothing, the defaults above already read the `XRD_*` variables the
official client uses, so an existing site environment keeps working unchanged.
Environment values are read when the `Config` is constructed, not when it is
used.

## Settings the packages above this one add

[`xrdml`](https://github.com/rob-c/xrdml) subclasses `Config` to add `catalogue` - where
`xrdml.load("name")` looks a bare name up, `$XRD_CATALOGUE` - and `cache_dir`,
where a pulled dataset is kept, `$XRD_CACHE`. Both are set in code or through
the environment; a file read here knows this package's settings only, and
names an unknown one as an error rather than ignoring it.
