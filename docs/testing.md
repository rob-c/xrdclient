# Testing against it

`xrdclient.testing` ships three servers, plus shared PKI, VOMS and HTTPS
fixtures for other projects' test suites (see *Shared fixtures* below).
They are part of the installed package, not test-suite scaffolding,
because the code that is hardest to test is yours - the retry loop, the failure handler, the thing that has to cope with a data
server disappearing.

```python
from xrdclient.testing import FakeServer, FakeDAVServer, FakeS3Server, FaultProxy
```

Nothing here needs a daemon, a port you own, or root. Everything binds an
ephemeral loopback port and cleans up.

## `FakeServer` - the XRootD protocol

```python
import xrdclient
from xrdclient.testing import FakeServer

with FakeServer(files={"/store/f.root": b"payload"}, dirs=["/store/empty"]) as srv:
    fs = xrdclient.FileSystem(str(srv.url))
    assert fs.read_bytes("/store/f.root") == b"payload"
    fs.write_bytes("/store/g.root", b"new")

    assert bytes(srv.files["/store/g.root"]) == b"new"     # the test can look
```

A real server, in the sense that matters: it speaks the binary protocol,
frames responses, handles `kXR_open`/`read`/`write`/`readv`/`pgread`/`clone`,
dirlist, stat, mkdir, rm, mv, truncate, chmod, xattrs, query, prepare,
checkpoints, the link extensions, and ping, plus the login and authentication
handshake.

It also speaks `kXR_bind`, and speaks it properly: a second connection that
names a live session is registered as that session's data path, and from then
on the bytes of a `kXR_write` are read off it and the reply to a `kXR_read`
is written to it, while the requests themselves stay on the connection that
opened the file. Each login gets its own session id, so binds land where they
were aimed.

That is what a stock daemon does. A gateway may instead key a sub-stream on the
connection a request *arrived* on and answer the whole thing there, which is
what `xrdclient` tries first — set `srv.serves_arrivals = True` for a server of that
kind. Leave it off for the standard split; a client that mixes the two would
have both ends reading the same socket.

### Making it behave badly

| Attribute | Effect |
| --- | --- |
| `srv.redirects[opcode] = (host, port, token)` | answer once with `kXR_redirect` |
| `srv.waits[opcode] = 3` | send three `kXR_wait` replies first |
| `srv.chunk_reads = 4096` | split reads into `kXR_oksofar` pieces |
| `srv.auth_rounds = 2` | demand two rounds of `kXR_authmore` |
| `srv.serves_arrivals = True` | answer a request that arrived on a data path |
| `srv.sec = "&P=gsi,v:10400&P=unix"` | what the login advertises |
| `srv.disconnect()` | drop live connections, keep listening |
| `srv.space = "oss.cgroup=..."` | what `kXR_Qspace` answers |

And for assertions:

| Attribute | Holds |
| --- | --- |
| `srv.seen` | every opcode, in order |
| `srv.opened` | the raw path of each `kXR_open`, CGI included |
| `srv.arguments` | `(opcode, argument)` for every request that named a path |
| `srv.files`, `srv.dirs`, `srv.xattrs` | the namespace, mutated live |
| `srv.properties` | the directives `kXR_set` carried, in order |
| `srv.cancelled_checksums` | paths a client asked it to stop digesting |
| `srv.cancelled_prepares` | staging handles withdrawn |
| `srv.prepared` | staging handle → the paths it named |
| `srv.nearline` | files that stat as offline, so a stage has something to wait for |
| `srv.links`, `srv.modes` | what `kXR_symlink` and `kXR_chmod` did |
| `srv.config_values` | what `kXR_query` config answers |

```python
from xrdclient.proto import constants as c

with FakeServer(files={"/f": b"x" * (1 << 20)}) as srv:
    srv.chunk_reads = 8192               # force reassembly
    srv.waits[c.kXR_open] = 1            # and one wait first
    assert xrdclient.Path(f"{srv.url}/f").read_bytes() == b"x" * (1 << 20)
    assert c.kXR_open in srv.seen
```

### Answering with something no server would send

`srv.handlers[opcode]` replaces one opcode's reply. A handler takes
`(connection, streamid, params, body)` and yields raw frames, which
`xrdclient.testing.frame` and `xrdclient.testing.error` build for you:

```python
from xrdclient.testing import FakeServer, error, frame
from xrdclient.proto import constants as c

def truncated(conn, sid, params, body):
    yield frame(sid, c.kXR_ok, b"short")      # fewer bytes than were asked for

with FakeServer(files={"/f": b"payload"}) as srv:
    srv.handlers[c.kXR_read] = truncated
    ...                                        # your retry loop, under a liar
```

A handler that yields nothing and closes `conn.sock` is a server that crashed
between doing the work and answering for it - the one case where a client must
*not* repeat a `write`, an `rm` or an `mv`.

### Sharing a directory over `root://`

The same server will hold files you already have, which is the shortest way to
put a `root://` URL in front of something on your own disk - a converted
dataset, a file you are debugging - without a daemon, a configuration file or
a port you need permission to bind:

```console
$ python -m xrdclient.testing datasets --port 21094 --pattern '*.root'
serving 2 files on root://127.0.0.1:21094/ with no login
  root://127.0.0.1:21094//home/you/datasets/mnist.root  11,561,483 bytes
  root://127.0.0.1:21094//mnist.root  11,561,483 bytes
```

Every file is offered twice, under its bare name and under the absolute path
it has here, so either URL reaches it. `from_directory` is the same thing from
Python, and takes the same arguments:

```python
from xrdclient.testing import from_directory

with from_directory("datasets", port=21094, pattern="*.root") as server:
    with xrdroot.open_root(f"{server.url}mnist.root") as f:   # pip install xrdroot
        print(f["train_7"])
```

It listens on loopback and authorises everyone, and it reads the files into
memory, which is the whole of what this server is: a demonstration and a test
fixture, not a storage element. Point it at a directory of gigabytes and it
will try to hold gigabytes.

## `FakeDAVServer` - HTTP and WebDAV

Since 0.4.0 `FakeDAVServer` and `FakeS3Server` are thin presets of
`xrdhttp.testing.StorageServer`, the fake storage element xrdhttp ships and
xgfalclient's suite runs against too. Everything documented there works on
these: `fault(...)` (a status, a drop, a delay, a truncated or stalled
answer), `tokens`/`basic`, `redirect_prefixes`, `metalinks`, CDMI `qos`,
`requests` (one `Request` record per request), and the XrdCl-style
switches listed in `xrdhttp.testing.STYLES`. The table below is the
vocabulary this package keeps on top.

```python
with FakeDAVServer(files={"/store/f.root": b"payload"}) as dav:
    fs = xrdclient.FileSystem(str(dav.url))          # already http://host:port/
    fs.stat("/store/f.root")
    assert ("PROPFIND", "/store/f.root") in dav.seen
```

| Attribute | Effect |
| --- | --- |
| `dav.require_token = "abc"` | 401 unless that bearer token arrives |
| `dav.redirects[path] = url` | 307 before the real answer |
| `dav.no_dav = True` | refuse `PROPFIND`, like a plain HTTP server |
| `dav.ignore_ranges = True` | answer a ranged `GET` with the whole body |
| `dav.digests = set()` | no `Want-Digest` support (the set holds the algorithms offered) |
| `dav.macaroon = "..."` | what a macaroon request mints |
| `dav.no_tpc = True` | refuse `COPY`, like an endpoint without third-party copy |
| `dav.tpc_failure = "..."` | answer `202` and then fail, which is the awkward one |
| `dav.tpc_markers = 4` | how many performance markers a `COPY` sends |
| `dav.copies` | the headers of every `COPY` served |
| `dav.handlers["PROPFIND"] = fn` | answer one method yourself |
| `dav.bodies`, `dav.targets` | every request body, and every raw request target |
| `dav.nearline` | files the tape API reports on tape until the test discards them |
| `dav.staged` | request id → the paths that staging request named |
| `dav.no_tape = True` | no `/api/v1`, like a site with no tape behind it |

It also speaks the WLCG Tape REST API - `POST /api/v1/stage`, `GET` and
`DELETE` on the request it returns, and `POST /api/v1/archiveinfo` - so a
staging test never needs a tape:

```python
with FakeDAVServer(files={"/store/f.root": b"payload"}) as dav:
    dav.nearline.add("/store/f.root")
    fs = xrdclient.FileSystem(str(dav.url))
    handle = fs.prepare(["/store/f.root"])
    assert not fs.query_prepare(handle, ["/store/f.root"])[0]
    dav.nearline.clear()                       # the tape robot got there
    assert fs.query_prepare(handle, ["/store/f.root"])[0].online
```

`handlers` is the HTTP twin of `FakeServer.handlers`: the function is handed
`(method, path, headers)` and returns `(status, body, headers)` to answer the
request, or `None` to record it and let the real implementation reply.

```python
def hostile(method, path, headers):
    return 207, b"<not xml", {"Content-Type": 'text/xml; charset="utf-8"'}

with FakeDAVServer(files={"/d/f.root": b"payload"}) as dav:
    dav.handlers["PROPFIND"] = hostile
    with pytest.raises(xrdclient.ProtocolError):
        xrdclient.FileSystem(str(dav.url)).stat("/d/f.root")
```

`ignore_ranges` is the one worth knowing about: caches in front of real
endpoints do exactly this, and a client that trusts a `200` where it asked for
a `206` silently returns the wrong bytes. This client detects it; your code
can be tested against it.

`COPY` is served for real - the server fetches from (or pushes to) whichever
endpoint the header names, which may well be a second `FakeDAVServer` - so a
third-party copy can be tested end to end in one process:

```python
with FakeDAVServer(files={"/d/f.root": b"payload"}) as src, FakeDAVServer(dirs=["/d"]) as dst:
    xrdclient.third_party(src.url / "d/f.root", dst.url / "d/f.root")
    assert dst.contents("/d/f.root") == b"payload"
```

## `FakeS3Server` - a bucket in the test process

```python
with FakeS3Server(objects={"d/a.root": b"payload"}) as s3:
    fs = xrdclient.FileSystem(s3.url, endpoint=s3.endpoint, credentials=creds)
    assert fs.read_bytes("/d/a.root") == b"payload"
    assert ("GET", "/test-bucket/d/a.root", "") in s3.seen
```

| Attribute | Effect |
| --- | --- |
| `s3.objects` | key to contents, readable and writable by the test |
| `s3.access_key`, `s3.secret_key` | what a signature must be made with; `""` accepts anything |
| `s3.page_size` | keys per listing page, which is how a test reaches the second one |
| `s3.etags` | the tags of objects that arrived as multipart uploads |
| `s3.uploads`, `s3.aborted` | multipart uploads in flight, and the ones abandoned |
| `s3.nameless_uploads = True` | begin an upload without naming it, as nothing sane does |
| `s3.complete_fails = True` | report a failure inside the `200` that completes an upload |
| `s3.forbidden = True` | refuse everything, whatever it is signed with |
| `s3.seen` | `(method, path, query)` for every request served, in order |

Signatures are checked rather than trusted, and the check is written out again
from the AWS specification instead of calling `xrdclient.s3.sigv4` - a client that
signs the wrong string is caught here rather than agreed with.

## `FaultProxy` - breaking the network

A loopback TCP proxy that sits in front of anything with an address - a
`FakeServer`, a real daemon, a `(host, port)` pair - and can be told to
misbehave mid-connection.

```python
from xrdclient.testing import FakeServer, FaultProxy

with FakeServer(files={"/big": b"payload" * 1000}) as srv, FaultProxy(srv) as proxy:
    with xrdclient.open(proxy.url.with_path("/big"), "rb") as fh:
        head = fh.read(32)
        proxy.cut()                      # the data server goes away mid-read
        assert fh.read(32)               # re-opened underneath, no error
```

That is `recover_handles` doing its job: an open read-only handle whose
connection dies is re-opened at the same offset and the read is retried.
`File.recoveries` counts how often it happened, and
`Config(recover_handles=False)` turns it into a `TransientError` instead.

| Method | What it does |
| --- | --- |
| `drop_after(offset)` | close the connection after N bytes from the server |
| `stall_after(offset)` | stop forwarding, hold the socket open - a black hole |
| `delay(seconds, after=N)` | slow every chunk down |
| `corrupt(offset, mask)` | flip bits at a byte offset |
| `chop(size)` | forward in small pieces - reassembly, not failure |
| `rewrite(fn)` | pass each server chunk through a function |
| `refuse()` / `accept()` | stop and resume accepting connections |
| `cut()` | close every live connection now |
| `heal()` | disarm everything |

Every arming method returns the proxy, so they chain. Counters
(`proxy.connections`, `proxy.bytes_from_server`, `proxy.bytes_from_client`)
and `proxy.armed` are there for the assertion.

```python
proxy.chop(64).delay(0.01)               # slow and fragmented, still correct
assert xrdclient.FileSystem(str(proxy.url)).stat("/big").st_size == 7000

proxy.refuse()                           # now the endpoint is simply down
with pytest.raises(xrdclient.TransientError):
    xrdclient.FileSystem(str(proxy.url)).stat("/big")
proxy.accept().heal()
```

`refuse` bites on the *next* connection; sessions already established keep
working until something cuts them, which is what `cut()` is for.

`stall_after` tests timeouts; a client that only handles a *closed* socket
hangs forever on one that is merely silent.

The external BRIX proxy adds deterministic probabilistic failures to a real
XRootD daemon. These tests repeatedly sever 4 KiB-chunked connections and
check that adaptive recovery still produces exact downloads and uploads. They
also cover fixed-boundary truncation until self-heal, same-length wire
corruption, and a compound tiny-MSS/jitter/silent-firewall-reap path:

```console
$ BRIX_FAULT_PROXY=/path/to/brix-fault-proxy \
    pytest tests/test_brix_fault_proxy.py
```

The external BRIX FUSE filesystem tests the other half of a copy: short and
zero-progress I/O, partial-write `ENOSPC`, torn and silently dropped writes,
lying metadata, repeating stale-handle bursts, live file replacement, volatile
writeback, dishonest `fsync` acknowledgement, and late or post-commit `fsync`
failure. The durability cases verify that a copy does not report success while
bytes are still only in a fallible cache:

```console
$ BRIX_FAULT_FS=/path/to/brix-fault-fs \
    pytest tests/test_brix_fault_fs.py
```

Those checks are not this suite's own: they are the local-copy conformance
suites of `xrdclient.testing.brix` (`WriteFaultSuite`, `DurabilitySuite`,
`SourceFaultSuite`, together `LocalCopySuite`), which any client runs by
subclassing one with a `LocalCopyAdapter` - "copy local file A to local file
B" through its own API, its errors read as a neutral `Kind`. xgfalclient
runs the same suite through `Gfal2Context.filecopy`; each client keeps only
its own error classes and wording, as overrides that call `super()` and then
look at `self.error`, `self.reply` or the backing directory.
`tests/test_testing_brix_suites.py` pins the suites without FUSE, against a
scripted filesystem and client, and checks that a client which reports
success through a fault, or misreads the failure, fails them.

Both tools are built by the adjacent `brix-cache/client` project. The suites
are skipped when their environment variable is unset, so the ordinary test
suite remains daemon- and FUSE-free.

## Shared fixtures: PKI, VOMS and a bare HTTPS server

Three more pieces of `xrdclient.testing` exist for the test suites of
*other* grid tools - xgfalclient and the ARC client xrdarc use them, and so
can yours - so that a CA, a proxy, a VOMS attribute certificate or a fake
REST service is written once rather than once per project. They are public
test API: **everything under `xrdclient.testing.*` follows the package's
semantic versioning** - a name or behaviour documented here changes only
with a deprecation period in a minor release, or in a major one. (Names with
a leading underscore are not covered.)

`pki` and `voms` need `cryptography` (a runtime dependency anyway) and are
imported on demand, not by `import xrdclient.testing`.

Since 0.4.0 `pki`, `voms` and `FakeHTTPSServer` are
[xrdsec](https://github.com/rob-c/xrdsec)'s test helpers under these names -
`xrdclient.testing.pki is xrdsec.testing.pki` - so xrdclient's, xgfalclient's
and xrdarc's suites all mint the same PKI. One visible change came with the
move: the default subjects read `/DC=org/DC=xrdsec/...` (`xrdsec Test CA`)
where they read `DC=xrdclient`; set the `*_SUBJECT` class attributes on a
`BasePKI` subclass for names of your own.

### `xrdclient.testing.pki` - a throwaway grid PKI

```python
from xrdclient.testing.pki import create_pki, test_key, test_key_pem

pki = create_pki(tmp_path)                 # CA, host (localhost/127.0.0.1/::1), user, proxy
server_ctx = pki.server_context()          # host cert; demands and verifies a client proxy
client_ctx = pki.client_context()          # presents pki.proxy_path, trusts the CA
os.environ.update(pki.environment())       # X509_USER_PROXY, X509_CERT_DIR
pki.proxy("limited", lifetime=600)         # rfc | limited | legacy; issuer= for proxy-of-proxy
pki.install_crl([pki.host.serial_number])  # <hash>.r0 beside <hash>.0
```

| | |
|---|---|
| `create_pki(dir, hosts=, host_cn=, lifetime=, key_slots=)` | mint and write a PKI (`PKI.create` for a subclass) |
| `pki.ca_dir`, `ca_file`, `ca_hash` | `X509_CERT_DIR` layout: `<hash>.0` and an IGTF `.signing_policy` |
| `pki.host_cert_path` / `host_key_path`, `user_cert_path` / `user_key_path`, `proxy_path` | grid-security file names; keys and proxies `0600` |
| `pki.ca`, `host`, `user` / `*_key` | `cryptography` certificates and keys |
| `pki.ca_signer`, `host_signer`, `user_signer` | `Signer(chain, key)`: DER chain and key, for minting more |
| `pki.credential(path=None)`, `pki.user_credential` | loaded with `xrdsec.x509.load_proxy` |
| `pki.crl(serials)`, `pki.install_crl(serials)` | a CRL as PEM; written where OpenSSL looks |
| `test_key(slot)` / `test_key_pem(slot)` | eight *fixed* 2048-bit RSA keys (0-7) as `cryptography` keys / PKCS#1 PEM |
| `ca_certificate`, `host_certificate`, `user_certificate`, `proxy_certificate` | one certificate as DER, for tests that need just one |
| `subject_hash(name_der)` | OpenSSL's `X509_NAME_hash`: a CA's file name |

The keys are frozen (and public - they protect nothing) so a suite does not
spend its time generating 2048-bit keys; certificates are minted at run time
and never expire in a checkout. Slot conventions: 0-2 CA/host/user, 3 proxy,
4 a server's delegation key, 5-7 a second independent PKI and one-off keys.
Proxies follow Globus's rules - lifetime capped by the issuer chain, a limited
proxy only begets limited ones, a GT2 issuer begets a GT2 child.

`BasePKI` is generic in how it *presents* what it minted: override
`_certificate_view`, `_key_view` and `_load_credential` (and the
`CA_SUBJECT` / `USER_SUBJECT` / `HOST_PREFIX` class attributes) to hand out
your own types. `xgfalclient.testing.pki` is exactly that: the same PKI in
xgfalclient's `Certificate`/`Credential` types, with `/DC=org/DC=xgfal/...`
names.

### `xrdclient.testing.voms` - attribute certificates and a VOMS server

```python
from xrdclient.testing.voms import ACSpec, FakeVOMSServer, make_ac, voms_proxy, write_lsc

voms = pki.host_signer                                  # any certificate + key
ac = make_ac(pki.user, voms.der, voms.key, ACSpec(fqans=("/atlas", "/atlas/higgs")))
proxy = voms_proxy(pki, [ac])                           # a proxy carrying the AC extension
write_lsc(tmp_path / "vomsdir", "atlas", "voms.example", voms.chain)
```

`make_ac(holder, voms_cert, voms_key, spec)` returns DER. The defaults of
`ACSpec` make an AC that `xrdclient.crypto.voms.validate_voms` accepts; each
field breaks or varies one thing: `fqans`, `attributes` (VOMS generic
attributes), `targets` (RFC 5755 `targetInformation`), the validity window,
`version`, `serial`, holder serial/issuer, issuer name, the embedded VOMS
chain, extra extensions, and the signature - RSA PKCS#1 v1.5 over any SHA-2
(or SHA-1), RSASSA-PSS (`pss=True`), ECDSA and Ed25519 by the key's type, or
any `sign=` function with explicit `algorithm`/`outer_algorithm`
identifiers. `ac_sequence`/`ac_extension` wrap ACs for a proxy;
`lsc_text`/`write_lsc` and `vomses_line` write the trust and client
configuration files.

`FakeVOMSServer(voms_signer, tls, vo=, members=)` answers the VOMS REST call
`GET /generate-ac?fqans=<a>,<b>&lifetime=<s>` with
`<voms><ac>base64</ac></voms>`, minting a real AC for the calling
certificate (on Python before 3.13 a server sees only the proxy, so set
`server.holder` when the AC must name the user certificate), or with
`<voms><error><code>…</code><message>…</message></error></voms>`:
`403 NoSuchUser` (no certificate, not in `members`), `403 NoSuchAttribute`,
`400 BadRequest`; a lifetime over `max_lifetime` is cut, with a `<warning>`.
`server.refuse(code, message, status)` cans the next answer; `server.issued`
holds every AC minted.

### `xrdclient.testing.http.FakeHTTPSServer` - a bare TLS test server

The plumbing every in-process HTTP fake needs, without any service on top:

```python
from xrdclient.testing.http import FakeHTTPSServer, Fault, Response, ranged_reply

with FakeHTTPSServer(tls=pki.server_context()) as srv:
    srv.route("GET", r"/data/(?P<name>.+)", lambda req: ranged_reply(blob, req.header("Range")))
    srv.add_fault(Fault("GET", r"/data/big", status=None, truncate=100))  # dies 100 bytes in
    ...                                                                   # client at srv.url
    srv.requests[-1].peer.identity    # '/DC=org/DC=xrdsec/OU=People/CN=Test User'
```

| | |
|---|---|
| `route(method, pattern, handler)` | `handler(Request) -> Response` (or `None` to hang up); `*` matches any verb, named groups land in `request.params`; 404/405 otherwise |
| `requests` | every `Request`: method, raw `target`, decoded `path`, `query`, `headers`, `body`, `peer` |
| `add_fault(Fault(...))` | per-request: `status` instead of the answer, `delay` before it, `stall` between headers and body, `truncate` the body, `drop` the connection; `times=None` for every match |
| `peer_identity(sock)` / `PeerIdentity` | the client's chain (whole chain on 3.13+, leaf before), its `subject`, and `identity` - the end-entity subject without proxy CNs |
| `ranged_reply(data, range_header, several=)` | 200 / 206 / `multipart/byteranges` / 416; `several="first"` or `"coalesce"` for servers that do that |
| `propfind_reply(entries)`, `DAVEntry`, `dav_entries(dir, href, depth)` | a WebDAV `207` multistatus, from entries or from a real directory |
| `read_body(handler)`, `TLSServer` | the chunked/`Content-Length` body reader and the TLS-per-connection server, for a fake that needs its own handler |

`FakeDAVServer` is built from the same `TLSServer`, `read_body`,
`ranged_reply` and `propfind_reply`; its knobs and behaviour are unchanged.

## Suite markers

Two pytest markers gate the tests that need more than Python:

```console
$ pytest -m "not interop and not parity"     # the default, no daemon needed
$ pytest -m interop                          # needs xrootd on PATH
$ pytest -m parity                           # needs the official bindings too
```

`interop` runs the suite against a real `xrootd` daemon started on a
loopback port; `parity` runs the same operations through this client and the
official `XRootD` bindings and compares the answers field by field. Neither is
required to develop against the library, and both run in CI.

## Coverage

The default suite covers 100% of the package's statements *and* branches -
including the fake servers in `xrdclient.testing`, which are shipped code and so are
held to the same standard:

```console
$ pytest -n 4 -m "not interop and not parity" --cov=xrdclient   # gated at 100%
```

The gate in `pyproject.toml` (`fail_under = 100`, branch coverage on) covers
the whole package: there is no `include` list and no `omit`, and nothing is
marked `pragma: no cover`. The only lines coverage leaves out are the shapes
`exclude_also` names - `if TYPE_CHECKING:`, `@overload` stubs,
`raise NotImplementedError`, `...` bodies and `if __name__ == "__main__":` -
which are the same in every repository of the chain. A branch that depends on
the platform or on a failure nobody can arrange for real is reached through
its seam instead (`tests/test_edge_paths.py`), so Linux and macOS cover the
same lines. pytest runs with `filterwarnings = ["error"]`: a warning is a
failure.

## Pre-commit quality gates

CI treats formatting, static analysis, maintainability and the built package
as separate contracts. Run the same checks locally before opening a change:

```console
$ ruff check src tests benchmarks tools examples typings
$ ruff format --check src tests benchmarks tools examples typings
$ mypy
$ complexipy
$ xrddevtools maintainability check
$ xrddevtools conformance --repo .
$ python -m build && twine check --strict dist/*
```

`xrddevtools` is the command of
[xrd-devtools](https://github.com/rob-c/xrd-devtools), the repository that
holds what serves the whole chain rather than one package. It is not on PyPI
and cannot be a dependency (PyPI refuses direct-URL requirements), so it is
used from a checkout: a sibling `../xrd-devtools` in the workspace layout,
`.dependencies/xrd-devtools` in CI, or anywhere else after `pip install -e`
(or with its `src` on `PYTHONPATH`). pytest's `pythonpath` lists the first two,
which is how `tests/test_scorecard.py` and `tests/test_maintainability.py` -
a few lines each, loading `xrddevtools.pytest_plugin` - find the gates.
Without a checkout they skip, saying so, as they do without radon and
complexipy (the `dev` extra); CI loads the plugin with `-p` and sets
`XRD_DEVTOOLS_REQUIRED=1`, so there they can never skip.

None of them has a baseline, a snapshot or an allowance list, and none of
the code has suppression comments to get past them, so new code and old are
held to the same rules (see [Maintainability](maintainability.md#no-grandfathered-code)).

Ruff includes a focused set of high-confidence security checks. Wider generic
security rules are reviewed rather than enabled blindly because protocol code
legitimately contains specified hashes, XML parsing and subprocess transports.
The CI test matrix covers Python 3.11 through 3.14.

CI is xrd-devtools' reusable workflows, the same in every repository of the
chain: `static.yml` (ruff, mypy, complexipy, the scorecard, here also the
maintainability check, and `xrddevtools conformance`), `tests.yml` (the
pytest matrix with the coverage gate) and `chain.yml` (all seven
repositories checked out side by side, this commit among them, for
`xrddevtools dupes` - no function copied between repositories - and the
conformance check of every one); so CI runs only once `rob-c/xrd-devtools`
exists. xrdclient is built on xrdcore, xrdsec and xrdhttp. Until those are on
PyPI, every job gets them through xrd-devtools' candidates action, which
checks out `rob-c/xrdcore`, `rob-c/xrdsec` and `rob-c/xrdhttp` (and
xrd-devtools) under `.dependencies/` and installs them editable (or, for the
wheel-only jobs, builds their wheels into `.dependencies/wheels`). The refs
default to `main`; the CI workflow's manual inputs `xrdcore-ref`,
`xrdsec-ref`, `xrdhttp-ref` and `devtools-ref` select other candidates.
Locally, the workspace's shared environment has all three installed editable
from their sibling checkouts. The package job also runs the chain pre-flight
without builds (`xrddevtools preflight workspace --no-build`) over this
candidate and the libraries it checked out, so a version or pin that drifts
apart fails a pull request, not a release. `publish.yml` never uses the
candidates: it gates on the libraries being on PyPI (see
[Releasing](releasing.md#releasing-the-040-chain)).

The packaging tools (`platforms`, `distribution_package`, `homebrew_package`,
`installed_smoke`, `chain_preflight`) are xrd-devtools', tested there at 100 %
with the rest of its tools. `tests/test_distribution_tools.py` here only pins
that this repository's CI, platform matrix and publish job call the shared
ones.

## Maintainability regression test

The suite also measures every handwritten Python function for cyclomatic,
cognitive and NPath complexity, Halstead Volume and maximum nesting depth. The
same absolute limits apply to existing and new code; there is no baseline or
allowance mechanism. The gate refuses a `maintainability.json` looser than
the standard limits (`xrddevtools.maintainability.STANDARD_LIMITS`: CCN 10,
Cognitive Complexity 15, NPath 200, Halstead Volume 500, maximum nesting 4)
or with any exclusion, so the suite cannot be made green by quietly relaxing
a threshold. `tests/test_maintainability.py` runs it as a test.

```console
$ xrddevtools maintainability check
$ xrddevtools maintainability hotspots
$ xrddevtools maintainability report --top 50
```

See [Maintainability metrics](maintainability.md) for the metric definitions,
machine-readable reports and the complete hotspot drill-down.
