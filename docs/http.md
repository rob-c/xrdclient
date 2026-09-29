# HTTP and WebDAV

HEP storage is spoken over `https://` and WebDAV as often as over `root://`.
The same three entry points cover both, dispatching on the URL scheme, so an
application changes a URL and nothing else.

```python
with xrdclient.open("davs://dav.example.org/store/f.root", "rb") as fh:
    header = fh.read(1024)

fs = xrdclient.FileSystem("https://dav.example.org")
for entry in fs.scandir("/store/user/me"):
    print(entry.name, entry.stat.st_size)

xrdclient.copy("root://a.example.org//store/f.root", "davs://b.example.org/store/f.root")
```

Schemes: `http`, `https`, `dav`, `davs`, `webdav`. Nothing here needs a wheel
- `http.client` and `xml.etree` do the work.

## What the WebDAV filesystem supports

| Operation | Method |
| --- | --- |
| `stat` | `PROPFIND` with `Depth: 0`, falling back to `HEAD` |
| `scandir`, `listdir`, `walk`, `glob` | `PROPFIND` with `Depth: 1` |
| `mkdir`, `makedirs` | `MKCOL` |
| `remove`, `rmdir`, `rmtree` | `DELETE` |
| `rename` | `MOVE` |
| `checksum` | RFC 3230 `Want-Digest` / `Digest` |
| `read_bytes`, `open` | `GET`, with `Range` for seeks |
| `write_bytes`, `open("wb")` | `PUT` |
| `third_party` | `COPY` with `Source:`/`Destination:` |
| `prepare`, `query_prepare`, `cancel_prepare` | the WLCG tape API, `/api/v1/stage` |
| `archive_info` | `POST /api/v1/archiveinfo` |

`scandir(algorithm=...)` is the one keyword with no WebDAV spelling: a
`PROPFIND` lists and a `Want-Digest` digests, and nothing asks for both at
once, so it refuses rather than quietly issuing a request per entry.

Operations with no HTTP equivalent - `locate`, `evict`,
`query_config`, `query_stats`, `query_space`, `checksum_cancel`,
`set_property`, `appid`, `statvfs` - raise `UnsupportedError` naming the
operation rather than returning something invented. They are the XRootD
protocol talking to an XRootD server about itself; WebDAV has no vocabulary
for any of it, and a plausible-looking answer built out of `PROPFIND` would be
a worse outcome than a refusal.

## Staging from tape

A tape-backed site answers the same three questions over HTTP that `root://`
answers with `kXR_prepare` and `kXR_QPrep`, through the WLCG Tape REST API
rooted at `/api/v1` - the one FTS and Rucio drive. The method names are the
same on both schemes, so a caller that knows one knows the other:

```python
fs = xrdclient.FileSystem("davs://tape.example.org")
handle = fs.prepare(["/store/a.root"])       # POST /api/v1/stage
while not all(fs.query_prepare(handle, ["/store/a.root"])):
    time.sleep(60)                           # GET /api/v1/stage/{id}
```

`prepare()` here only stages: the API has no equivalent of the other
`PrepareFlags`, and each of them is refused by name. `cancel_prepare(handle)`
is the `DELETE`. `evict()` has no counterpart at all - the API releases a
request rather than a path - so it raises `UnsupportedError`.

`archive_info(paths)` asks where files are without asking for any of them to
move, and it exists on both schemes too: over HTTP it is
`POST /api/v1/archiveinfo`, and over `root://` it is one `statx`, whose
offline flag answers the same question. Either way you get a
`PrepareStatus` per path with `online`, `on_tape` and a `state` word from the
tape API's vocabulary - `ONLINE`, `NEARLINE`, `DISK`, `TAPE`.

An endpoint with no tape behind it has no `/api/v1` either, and answers `404`,
which arrives as a `NotFoundError` naming the API path.

## Ranged reads

`GET` with a `Range` header is how a seek is served, so an `xrdclient.open` over
`https://` is still a real seekable file object. Servers that ignore `Range`
are detected (a `200` where a `206` was asked for) and reported rather than
silently returning the whole file.

## Authentication

```python
Config(token="eyJ...")                # Authorization: Bearer ...
Config(proxy="/tmp/x509up_u1000")     # mutual TLS with the X.509 proxy
```

A bearer token goes in the `Authorization` header; an X.509 proxy is
presented as the client certificate for `davs://` and `https://` alike. The
same `Config` drives both protocols.

### Credentials and redirects

A `Location` header is chosen by the server, so a client that follows it
would pass its token to whatever host that server names. The client uses
the rule that browsers, `curl` and `requests` use:

- Credentials follow a redirect only when the scheme, host and port all stay
  the same. `Authorization`, `Proxy-Authorization`, `Cookie` and every
  `TransferHeader*` header (a `COPY` passes these on to the far side) are
  dropped at the first hop that leaves that origin. They stay dropped for
  the rest of the chain, even if a later hop comes back to the original
  host.
- Credentials are never sent over plain `http` after the request started on
  `https`, even to a trusted domain.
- A token that the redirect itself carries (`Location: ...?authz=...`) is
  still presented, because the redirecting server chose to hand it over.
  That is how dCache and EOS doors usually hand off.
- An S3 signature is still computed for each hop. It is bound to the host it
  was computed for, so no other host can use it.

Some WLCG sites redirect from a head node to data nodes on other hosts and
expect the bearer token there. You can opt those sites in by domain:

```python
HTTPClient(config, trusted_redirect_domains=("desy.de", "cern.ch"))
```

An entry matches that host and every host under it, whatever the port.
`"*"` trusts every host, which restores the old behaviour, but a
downgrade from `https` to `http` still drops the token.

### Query strings

Opaque data is sent exactly as it was written. `authz=Bearer%20abc` stays
`Bearer%20abc`, and `tpc.src=h:1094` keeps its colon, because XRootD servers
compare these strings literally. Changing one parameter re-renders only that
parameter. A new value escapes only `&`, `#`, `%`, `+` and whitespace, and a
space is encoded as `%20`, never `+`.

### Names and percent signs

A URL is read the way a browser reads one: the `%XX` escapes in the path of
an `http://`, `https://`, `dav://` or `davs://` URL are decoded once, so
`https://h/store/a%20b` names the file `a b`, and a link copied out of a
browser or a server's listing page works as it is. A path passed on its own -
`fs.open("/store/a%20b")`, `fs.stat(...)`, `XRootDPath(...) / "name"` - is
already a name and is not decoded: that call opens a file literally called
`a%20b`.

On the way to the server a name is percent-encoded exactly once, `%`
included, and a name that comes back from a listing is decoded exactly once.
So whatever `listdir` returns opens the file it came from - names with a `%`,
a space, `#`, `?`, `+`, `~` or characters outside ASCII alike - and printing
a URL gives the encoded form that parses back to the same name. A file whose
name really contains `%20` is reached by URL as `%2520`.

The one escape left as it is, both ways, is `%2F`: an escaped slash is a
slash *inside* one path segment, as in a Hugging Face URL's
`.../resolve/refs%2Fconvert%2Fparquet/...`, and decoding it would split the
segment in two. The price is that a file whose name contains the three
characters `%2F` cannot be reached over HTTP; over `root://` it can.

`root://` URLs are not decoded, because XrdCl does not decode them: there the
path is the name exactly as written. `s3://` keys are likewise taken as
written, as the AWS tools take them.

## Timeouts

`Config.connect_timeout` bounds the TCP and TLS handshake and nothing more.
Every read after it waits up to `Config.request_timeout` (300 s by default,
`XRD_REQUESTTIMEOUT`), so a transfer that is slow but alive is not cut off at
the connect window. A request that times out is not retried: a server that
did not answer in the time allowed would only be given the time again.

## Macaroons

```python
from xrdclient.http import macaroon

token = macaroon("davs://dav.example.org/store/user/me",
                 caveats=["activity:DOWNLOAD"], validity="PT10M")
xrdclient.copy("davs://dav.example.org/store/user/me/f.root", "/tmp/f.root",
         config=xrdclient.Config(token=token))
```

`validity` is an ISO 8601 duration, the spelling dCache and XRootD both use.
The result is a plain string on purpose: a macaroon is a bearer token, so it
goes wherever `Config.token` goes.

## Third-party copy

```python
xrdclient.third_party("davs://a.example.org/store/f.root",
                "davs://b.example.org/store/f.root")
```

`COPY` with a `Source:` header, the dialect FTS and Rucio speak, so the two
storage elements move the file between themselves. The outcome is in the
response body rather than the status line - a failed transfer still answers
`202 Accepted` - and this client reads through the performance markers to
that last line before returning. `timeout=` is the longest to wait between
two markers, defaulting to `Config.request_timeout`; it is not a limit on the
whole transfer, since a copy that keeps reporting is alive however long it
runs. [Copying](copying.md#third-party-copy) has
the header set, the push mode, and how the far side's token travels.

## Lower-level pieces

```python
from xrdclient.http import HTTPClient, propfind, digest, open_http, status_code

client = HTTPClient(xrdclient.Config())
response = client.request("HEAD", url)
props = propfind(xrdclient.parse(url), depth=1, config=cfg)   # [(path, StatInfo)]
info = digest(url, "adler32", config=cfg)               # ChecksumInfo
status_code(403)                                        # the kXR_* code it means
```

These exist because WebDAV endpoints differ, and being able to send one
request and look at the answer beats guessing.
