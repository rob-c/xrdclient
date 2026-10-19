# Errors

Every failure raises. There is no `(status, result)` tuple to check, because a
tuple you can forget to check is a bug waiting to happen, and because Python
already has a mechanism for this.

Server errors map onto the `OSError` subclass a local filesystem would have
raised, so code written against `/tmp` works against `root://` unchanged:

```python
try:
    data = xrdclient.Path("root://host//store/f.root").read_bytes()
except FileNotFoundError:
    ...
except PermissionError:
    ...
```

## The hierarchy

```
Exception
└── XRootDError                       is xrdcore.errors.XrdError           *
    ├── ProtocolError                 the peer is not speaking XRootD correctly  *
    │   └── MetalinkError             (also ValueError)                    *
    ├── ConnectionError               (also builtins.ConnectionError)      *
    │   └── TransientError            worth retrying                       *
    │       ├── TimeoutError          (also builtins.TimeoutError)         *
    │       └── WaitLimitError        the server kept saying "come back later"
    ├── AuthenticationError                                                *
    │   ├── NoMechanismError          nothing we have was accepted
    │   └── CredentialError                                                *
    │       ├── TokenExpiredError                                          *
    │       └── InsecureCredentialError (also PermissionError)             *
    ├── RedirectLimitError
    ├── ChecksumMismatchError
    ├── PageIntegrityError            a page kept arriving corrupt on a pgwrite
    ├── TooLargeError                 a whole-file read that would not fit
    ├── AbortedError                  + OSError             ECANCELED
    └── ServerError                   a kXR_error response
        ├── NotFoundError             + FileNotFoundError   ENOENT
        ├── ExistsError               + FileExistsError     EEXIST
        ├── PermissionError           + PermissionError     EACCES
        ├── TLSRequiredError          + PermissionError     EACCES
        ├── IsADirectoryError         + IsADirectoryError   EISDIR
        ├── NotADirectoryError        + NotADirectoryError  ENOTDIR
        ├── NoSpaceError              + OSError             ENOSPC
        ├── IOError                   + OSError             EIO
        ├── UnsupportedError          + OSError             ENOSYS
        ├── ReadOnlyError             + OSError             EROFS
        ├── QuotaError                + OSError             EDQUOT
        ├── AttrNotFoundError         + OSError             ENODATA
        ├── BusyError                 + OSError             EBUSY
        ├── InvalidArgumentError      + OSError             EINVAL
        └── ServerTimeoutError        + TimeoutError        ETIMEDOUT
```

Catch broadly with `xrdclient.XRootDError`, or narrowly with the builtin you already
know. The classes marked `*` are [xrdcore's](#one-hierarchy-with-the-libraries).

## One hierarchy with the libraries

Since 0.4.0 the pieces xrdclient shares with xgfalclient and xrdarc live in
libraries - xrdcore (safe XML, checksums, the copy pump, the report),
xrdsec (credentials, proxies, VOMS, tokens) and xrdhttp (the HTTP engine,
WebDAV, S3, Metalink) - and those raise the vocabulary in `xrdcore.errors`,
never a client's own classes. The design question is how `except` written
against xrdclient's names still catches them.

The answer is that the names *are* those classes. `xrdclient.errors` binds
each of its names that means the same thing as an xrdcore error to the
xrdcore class itself:

| xrdclient name | is |
|---|---|
| `XRootDError` | `xrdcore.errors.XrdError` |
| `ProtocolError`, `MetalinkError` | `xrdcore.errors.ProtocolError`, `.MetalinkError` |
| `ConnectionError`, `TransientError`, `TimeoutError` | the xrdcore classes of those names |
| `AuthenticationError`, `CredentialError`, `TokenExpiredError`, `InsecureCredentialError` | the xrdcore classes of those names |

```python
from xrdcore import errors as core
import xrdclient

assert xrdclient.XRootDError is core.XrdError
assert xrdclient.errors.CredentialError is core.CredentialError
```

So a `CredentialError` raised deep inside xrdsec while xrdclient finds a
proxy is caught by `except xrdclient.CredentialError`, by `except
xrdclient.XRootDError`, and by `except xrdcore.errors.CredentialError` alike,
with nothing translated or re-raised on the way - and the attributes it
carries (`TransientError.attempts`/`committed`,
`InsecureCredentialError.path`) arrive intact, pickling included.

The errors only XRootD has - `ServerError` and its `OSError` family with
their `kXR_*` `code` and `errno`, `NoMechanismError`, `RedirectLimitError`,
`WaitLimitError`, `ChecksumMismatchError`, `PageIntegrityError`,
`TooLargeError`, `AbortedError` - are defined in `xrdclient.errors` and join
the shared hierarchy by inheritance, by multiple inheritance where they are a
builtin as well (`ServerTimeoutError` is a `ServerError`, an
`xrdcore.errors.TimeoutError` and a builtin `TimeoutError`). Code that only
knows the libraries catches them too: `except xrdcore.errors.XrdError`
catches every error xrdclient raises.

Why not subclass the other way, `class CredentialError(XRootDError,
xrdcore.errors.CredentialError)`? Because `except` catches a class and what
is *below* it: the library's `CredentialError` is not below a client
subclass of it, so every library call would need wrapping to re-raise in the
client's class, and one forgotten wrapper is an error that slips past
`except xrdclient.XRootDError`. Binding the names costs one visible change:
`XRootDError.__name__` is `"XrdError"`.

### xrdsec's own errors

xrdsec (credentials, proxies, VOMS, TLS - `xrdclient.credentials`,
`xrdclient.crypto.*` and `xrdclient.tls` are its modules since 0.4.0) raises
the shared classes above for everything a user can act on: an insecure or
unreadable credential file is `InsecureCredentialError` / `CredentialError`,
an expired token `TokenExpiredError`, and `VOMSError` with its kinds
(`VOMSConfigError`, `VOMSRefusedError`, `VOMSUnavailableError`) subclasses
`CredentialError`. All of those are `XRootDError`s already, so nothing is
translated at the client boundary.

The rest are deliberately *not* client errors, and stay `ValueError`s:
`DERError` (and its `SignatureAlgorithmError`) for bytes that are not the
structure expected, `DelegationError` (also `xrdsec.proxies.ProxyError`)
for a proxy request that must not be signed, and `TrustError` for a server
certificate that does not check out. Each is a fact about some bytes, not
about an operation, and only the caller knows which operation it was: the
GSI login catches them where it calls xrdsec and raises
`AuthenticationError` or `CredentialError` naming the server and the step
(`xrdsec.gsi.secgsi`, which raises `xrdcore.errors.CredentialError` - this
package's class of that name - and is tested in xrdsec's `tests/test_gsi_secgsi.py`). Code that calls `xrdclient.crypto` (xrdsec) directly sees them as they
are - which is what a tool parsing a certificate wants - and
`tests/test_on_xrdsec.py` pins both halves of this split.

`TLSRequiredError` is a server refusing to do this in the clear (`kXR_error`
3028): its message names the fix, which is to connect with `roots://` (or
`davs://`) rather than to try again.

## What a `ServerError` carries

```python
try:
    fs.stat("/store/missing")
except xrdclient.ServerError as exc:
    exc.code        # 3011, the kXR_ code
    exc.message     # the server's own text
    exc.path        # what we asked about
    exc.errno       # errno.ENOENT
    str(exc)        # plain explanation, path, server detail and numeric XRootD code
```

`str()` starts with a plain explanation and a safe next step, then includes
the path, server detail and numeric XRootD code. The original server text
remains in `message`; `code`, `errno`, exception types and pickling are unchanged.
Use these fields in programs instead of parsing display wording.

## Writing useful messages

Messages should serve physicists and first-time administrators. Explain what
failed, identify the affected file or service, and suggest a short, safe next
step. Put protocol internals in detail fields rather than making the user decode
an opcode. Never suggest disabling certificate checks or trusting a certificate
copied from an untrusted proxy. Tests should check the cause, path, numeric code
and action, not just that an exception was raised.

VOMS diagnostics distinguish a missing `.lsc` file, a damaged or expired CA
certificate, a permissions problem and missing endpoint configuration. An expiry
message includes a UTC date. Missing `vomses` is a setup issue for obtaining a
proxy, not a reason to reject an already-issued one. See [Authentication](auth.md).

Exceptions pickle correctly, which matters when they cross a
`concurrent.futures` or `multiprocessing` boundary and you want the code and
the path on the other side, not a mangled `(errno, strerror)` pair.

## Retrying

`TransientError` is the marker: connection resets, timeouts, and
server-requested waits that outlived `wait_cap`. Everything else is a
permanent answer.

```python
for attempt in range(5):
    try:
        return xrdclient.Path(url).read_bytes()
    except xrdclient.TransientError:
        time.sleep(2 ** attempt)
raise
```

The client already reconnects and retries underneath - `connect_retries`,
`retry_backoff`, and `recover_handles` for a data server that disappears
mid-read. A `TransientError` reaching you means that budget was spent.

`WaitLimitError` is the one to single out. A server answering `kXR_wait` is
busy, not broken: the connection is fine, so the client sleeps and re-issues
until `wait_cap` is spent, and then stops rather than reconnecting - a new
socket would only ask the same overloaded server the same question. Catch it
if you want to back off on a human timescale, or let it surface as the
`TransientError` it is.

Note what is *not* retried. A request that changes something on the server -
`rm`, `mv`, `mkdir`, `truncate`, `chmod`, `write`, `close` - is sent at most
once. If the connection dies after the request left and before the reply came
back, the client cannot know whether the server did the work, so it reports
the failure instead of guessing; reads, stats and listings, which cost nothing
to repeat, are reissued on a fresh connection.

## Authentication failures

```python
except xrdclient.NoMechanismError as exc:
    print(exc)
    exc.offered    # ['gsi', 'unix'] - what the server said it would take
    exc.tried      # {'gsi': 'there is no file at /tmp/x509up_u1000; try: ...'}
# Login failed. No available authentication method succeeded (server methods: gsi, unix)
# [gsi: there is no file at /tmp/x509up_u1000; try: voms-proxy-init -voms
#  <your VO>, or point $X509_USER_PROXY at one; unix: refused]
```

One exception, every mechanism, the reason each did not apply, and the
command that would fix it - rather than a bare "authentication failed" that
tells you nothing about which of the six to fix. At a terminal you are asked
for the missing proxy or token before it comes to this; see
[Authentication](auth.md#being-asked-for-what-is-missing).

## Unsupported operations

WebDAV has no `locate`, no `evict`, no `statvfs`. Those raise
`UnsupportedError` naming the operation instead of returning something
invented, so the failure happens at the call rather than three functions later
when the invented answer turns out to be wrong.

## Checksums

```python
except xrdclient.ChecksumMismatchError as exc:
    exc.algorithm, exc.expected, exc.actual
```

Raised by a verified copy. It is a data-integrity signal, not an
authentication one - see [Security](security.md).

## Too much at once

```python
except xrdclient.TooLargeError as exc:
    exc.size, exc.limit, exc.path
```

Raised *before* the allocation, by a read that never said how much it wanted
of a file larger than `config.max_read_size`. The message says how to stream
it instead. `read(n)` is a caller who knows, and never raises this; nor does
anything at all once `max_read_size` is `0`. See [Safety](safety.md).

## Errors from the packages above this one

[`xrdroot`](https://github.com/rob-c/xrdroot) raises `ROOTError` for a file it cannot read -
`FormatError` when the bytes are not the format they claim, and
`UnsupportedFeatureError` for a valid file using something that reader does
not decode. Both are `XRootDError` subclasses from here, so an `except
xrdclient.errors.XRootDError` catches them too.
