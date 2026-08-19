# Configuration

`Config` is a frozen dataclass. Build one, pass it to whatever you are opening,
and derive variants with `evolve`.

```python
cfg = xrd.Config(request_timeout=60.0)
patient = cfg.evolve(request_timeout=600.0)

xrd.open("root://host//store/f.root", config=cfg)
xrd.FileSystem("root://host", config=cfg)
xrd.copy(src, dst, config=cfg)
```

Frozen because a configuration is shared by every session it reaches, and a
setting that changes underneath a live connection is a bug you find in
production. `evolve` returns a new one.

## Timeouts and retries

| Field | Default | Environment | Meaning |
| --- | --- | --- | --- |
| `connect_timeout` | `30.0` | `XRD_CONNECTIONWINDOW` | TCP connect and login |
| `request_timeout` | `300.0` | `XRD_REQUESTTIMEOUT` | one request/response |
| `stream_timeout` | `60.0` | `XRD_STREAMTIMEOUT` | idle socket before a keepalive |
| `connect_retries` | `3` | `XRD_CONNECTIONRETRY` | reconnection attempts |
| `retry_backoff` | `0.5` | `XRD_STREAMERRORWINDOW` | first backoff, then doubling |
| `redirect_limit` | `16` | `XRD_REDIRECTLIMIT` | redirects before giving up |
| `wait_cap` | `600.0` | | ceiling on a server-requested wait |
| `stall_deadline` | `1800.0` | `XRD_STALLDEADLINE` | one whole operation, first byte to last |
| `wait_budget` | `1800.0` | `XRD_WAITBUDGET` | total parking one operation may be asked for |
| `keepalive_interval` | `60.0` | | seconds between `kXR_ping`s |

`request_timeout` bounds a single read from the socket and `stall_deadline`
bounds the operation those reads add up to: a server that dribbles a byte at a
time, or that says "still working" forever, never trips the first and is
exactly what the second is for. A `kXR_wait` restarts the deadline — a delay
the server declared is not a stall — but the delays are added up against
`wait_budget`, so a redirector cannot park a caller indefinitely one polite
minute at a time. Both take `0` to wait forever.

## Transfers

| Field | Default | Environment |
| --- | --- | --- |
| `chunk_size` | 4 MiB | `XRD_CPCHUNKSIZE` |
| `readahead` | 1 MiB | `XRD_READAHEAD` |
| `parallel_chunks` | `4` | `XRD_CPPARALLELCHUNKS` |
| `parallel_files` | `1` | `XRD_CPPARALLELFILES` |
| `in_flight` | `2` | `XRD_CPINFLIGHT` |
| `data_streams` | `1` | `XRD_SUBSTREAMSPERCHANNEL` |
| `data_stream_timeout` | 2 s | `XRD_SUBSTREAMTIMEOUT` |
| `max_read_size` | 1 GiB | `XRD_MAXREADSIZE` |
| `cache_dir` | `~/.cache/xrd` | `XRD_CACHE` |

`parallel_chunks` is how many connections one large copy is spread over, a
span of the file each; `1` keeps the single stream. See
[Copying](copying.md#several-connections-at-once) for when it applies.
`parallel_files` is how many files of a `copy_tree` are in flight at once,
and defaults to one because each of them is already spread over
`parallel_chunks`. `in_flight` is how many chunks a transfer reads ahead of
the write it is waiting on, so that the two ends overlap; `1` is the strictly
sequential pump, and is what a copy between two local disks wants.
`data_streams` is how many extra `kXR_bind` sub-streams a file binds at open,
so a plain read or write already travels beside the control traffic instead of
behind it; it is on by default (one extra link). The official client counts the
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
`cache_dir` is where [`xrd.ml.download`](ml.md#keeping-a-local-copy) puts a
dataset it has pulled. Naming a directory does not turn caching on: nothing is
written there until a caller asks for it with `download(...)` or
`load(..., cache=True)`, because streaming the file is the ordinary case and a
copy on disk is the exception you opt into.

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
from xrd.session import SESSIONS

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
| `ca_path` | `None` | `X509_CERT_DIR` |
| `ca_file` | `None` | `SSL_CERT_FILE` |
| `auth_order` | `("gsi", "ztn", "krb5", "sss", "unix", "host")` | |
| `verify_tls` | `True` | |
| `require_tls` | `False` | |
| `ztn_cleartext` | `False` | `XRD_ZTNCLEARTEXT` |
| `prompt` | `None` (ask only at a terminal) | `XRD_PROMPT` |
| `prompter` | `None` (ask on the terminal) | |

See [Authentication](auth.md) for what each mechanism looks for.

`ztn_cleartext` opts back into offering a bearer token on a connection that
is not TLS, which the client otherwise refuses to do - a token sent in the
clear is a token anyone on the path can replay. Prefer `roots://`.

## Behaviour

| Field | Default | Meaning |
| --- | --- | --- |
| `recover_handles` | `True` | silently re-open a read-only file whose data server vanished mid-read |
| `verify_checksums` | `True` | compare checksums after a copy |
| `preferred_checksum` | `"adler32"` | algorithm asked for first |
| `s3_folder_markers` | `False` | make `mkdir` on S3 write a zero-length `dir/` marker object |
| `catalogue` | `None` (`$XRD_CATALOGUE`) | where `xrd.ml.load("name")` looks a bare name up |

`recover_handles=False` turns a lost data server into a `TransientError` at the
call that hit it, which is what you want when your job would rather fail than
re-read.

## Username

```python
Config(username="atlasprd")
```

Defaults to `$XRD_USER`, `$USER`, `$LOGNAME`, then `getpass.getuser()`, then
`"nobody"` - so it is never blank even in a container without a passwd entry.

## Secrets never print

```python
>>> xrd.Config(token="eyJhbGciOi...")
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
cfg = xrd.Config.from_file()                  # the usual places
cfg = xrd.Config.from_file(alias="eos")       # [defaults], then [alias eos]
cfg = xrd.Config.from_file("./job.ini")       # exactly this file
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

Two settings are refused in a file: `prompter`, which is a callable and cannot
be spelled in INI, and `token`, because a literal bearer token in a dotfile is
a secret in every backup of that dotfile - say `token_file` instead.

## Environment only

If you set nothing, the defaults above already read the `XRD_*` variables the
official client uses, so an existing site environment keeps working unchanged.
Environment values are read when the `Config` is constructed, not when it is
used.
