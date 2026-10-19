# Security

## Reporting a vulnerability

Mail <robert.andrew.currie@gmail.com> with a description and, if you have one,
a reproducer. Please do not open a public issue for anything that lets one
party read or write another party's data. Expect an acknowledgement within a
few working days.

## What this client is trusted with

It authenticates as you and moves your data. It therefore handles bearer
tokens, X.509 proxies, Kerberos tickets and SSS shared secrets, and it acts on
whatever a storage server tells it. The threat model has three parties:

- **The network.** Assumed hostile. Mitigated by TLS, which is verified by
  default, and by the protocol's own request signing where a server asks for it.
- **The server.** Assumed to be *the server you named*, but not assumed to be
  well behaved. A compromised or buggy endpoint must not be able to make the
  client write outside the directory it was pointed at, or spend unbounded
  memory or time.
- **The local machine.** Assumed sound. Anything that can read your process
  memory or your keytab already has your credentials, and no library can fix
  that.

## What the implementation guarantees

**Nothing turns TLS verification off implicitly.** `tls_context()`, and the
public `xrdclient.tls.client_context()` it is built on (xrdsec's, shared with
xgfalclient and xrdarc), build an
`ssl.create_default_context()` - certificate chain and hostname both checked -
and only `Config(verify_tls=False)` (`verify=False`) relaxes it. The CLI spells that
`--no-verify-tls`, so it appears in shell history and in the command a
reviewer reads. `Config(require_tls=True)` refuses to speak to a server that
will not upgrade, which is the setting to use when you are carrying a bearer
token.

<a id="which-cas-are-trusted"></a>
**Which CAs are trusted depends on what the endpoint is.** On a grid node,
`/etc/grid-security/certificates` (or `$X509_CERT_DIR`) becomes `ca_path`, and
`ssl.create_default_context(capath=...)` then trusts those CAs and nothing
else. That is the grid norm, and right for `davs://` and `roots://`: a
storage element is vouched for by an IGTF CA, and trusting the hundred-odd
commercial CAs as well would let any of them vouch for it. It is wrong for an
S3 service or a public `https://` site, whose certificates come from those
commercial CAs and which would otherwise fail on every grid node. So the
default is:

| Scheme | Grid CA directory present | No grid CA directory |
| --- | --- | --- |
| `davs://`, `roots://` | grid CAs only | system store |
| `https://`, `s3://` | grid CAs **and** system store | system store |

`XRD_TLSSYSTEMCA=0` restricts `https://` and `s3://` to the grid CAs too (for
a site whose object store has an IGTF certificate and that wants nothing
else); `XRD_TLSSYSTEMCA=1` adds the system store everywhere. A pooled
connection is only reused for a request with the same trust, so a `davs://`
request never rides a connection that a commercial CA vouched for.

**Revocation is checked when asked.** `XRD_CHECKCRL=1` (or `leaf`) makes
OpenSSL check the server certificate against the CRLs in the CA directory -
the `<hash>.r0` files `fetch-crl` maintains, found by the issuer's hash -
and `XRD_CHECKCRL=chain` checks every certificate in the chain. It is off by
default because it fails closed: a CA with no CRL in the directory, or an
expired one because `fetch-crl` stopped running, fails every handshake it
signs for. Turn it on where `fetch-crl` is known to run. A rewritten CA
directory changes its modification time, which makes the next connection
build a fresh context, so new CRLs take effect without a restart.

**A server's own TLS demand is honoured, including the one about data.** A
`kXR_protocol` reply carries what the server insists on encrypting; `kXR_gotoTLS`,
`kXR_tlsLogin` and `kXR_tlsSess` all upgrade the connection before the login
goes out, and so does `kXR_tlsData`. That last one names file data rather than
the session, but this client reads and writes on the connection it logged in
on, so there is nowhere for the bytes to go that the socket does not reach -
encrypting everything is the only reading that keeps the promise. A server
that demands TLS and does not offer it is an error, not a downgrade.

**Credentials do not reach logs, reprs or tracebacks.** Every logger under
`xrdclient.` carries a filter that interpolates the record and then redacts the
result, so a secret that only becomes recognisable once the format string and
its arguments are joined is still caught. `Config`, `XRootDURL`,
`TokenCredential`, `SSSKey`, `SSSCredential`, `Blowfish`, `AES`,
`RSAPrivateKey` and `ProxyCredential` all print `<redacted>` in place of key
material, and no exception message carries a credential. The patterns cover
bearer tokens, JWTs, macaroons, `Authorization` headers (SigV4 included), S3
pre-signed parameters, `sig=`, EOS `cap.sym`/`cap.msg` and `access_token`;
CLI error output goes through the same filter. Tests enforce each of these;
see `tests/test_auth.py`.

**A prompt cannot leak the answer.** A secret is read with `getpass`, so it
is never echoed and never lands in the scrollback; questions go to `stderr`,
never `stdout`, so a redirected pipe can neither swallow one nor be polluted
by it; and the `Ask` handed to a prompter carries the mechanism, the reason
and the fix, but never a credential. Answers are held in memory for the life
of the process so that one endpoint is asked about once -
`xrdclient.auth.forget()` drops them - and nothing is written to disk. With no
terminal there is no question at all: prompting is off unless `stdin` and
`stderr` both say otherwise, or `Config(prompt=True)` insists.

**A world-readable SSS keytab is refused**, with the same reasoning as the C
implementation: the file holds shared secrets in the clear, so mode `0o077`
bits are fatal. The failure is logged at `WARNING`, not swallowed, because
silently falling through to a weaker mechanism is how that goes unnoticed.

**Kerberos keys stay in memory and are checked before use.** The session
keys read from a credential cache, and those the KDC returns, live only in
the process: they are left out of every `repr`, never logged, and a service
ticket fetched from the KDC is never written back to the cache. A TGS reply
is used only after its integrity check passes under the TGT session key and
its nonce and service principal match the request, so a reply replayed from
another exchange, or issued for another service, is refused. Only the AES
enctypes are implemented; DES, triple-DES and RC4 are refused by name. The
cryptography is pinned to every RFC 3961, RFC 3962 and RFC 8009 test vector,
and the messages to bytes captured from MIT krb5 and accepted by a real
`xrootd`.

**Directory listings cannot escape their directory.** Every consumer joins
server-supplied names onto a path - `walk()` to recurse, `copy_tree()` to
build a local destination - so `parse_dirlist()` refuses any entry containing
`/` or equal to `..`. Without that check a hostile server could answer
`../../.ssh/authorized_keys` to a recursive download.

**A settings file cannot hold a bearer token.** `Config.from_file` accepts
`token_file` and refuses `token`: a literal token in a dotfile is a secret in
every backup, every `scp -r` of a home directory and every screenshot of it,
and it outlives the reason it was written down. The refusal names
`token_file` as the way to say the same thing. `prompter` is refused too,
because it is a callable and an INI file that could name one would be an
arbitrary-import hole.

**Bounded work per response.** Frames are sized from the declared `dlen`,
redirects are capped by `config.redirect_limit`, `kXR_wait` intervals by
`config.wait_cap`, and every socket operation by `config.request_timeout`. A
server cannot hold a client forever or make it buffer an unbounded amount by
sending a header alone.

**A small, vetted dependency set and no dynamic execution.** The runtime
depends on three maintained, general-purpose libraries - `cryptography`,
`asn1crypto` and `botocore` - plus this project's own two libraries, `xrdcore`
(no dependencies at all) and `xrdsec` (the grid security code, on
`cryptography` and `asn1crypto`), and nothing else outside the standard
library; HTTP, TLS and JWT claim decoding are the standard library's. Each is
pinned to a minimum release in `pyproject.toml` (the two libraries to the same
minor series as this package), resolved from wheels on every supported
platform in CI, and recorded with its hash in the deployment bundles, so the
supply chain to audit is those projects and their own dependencies. The
optional `krb5` extra adds `python-gssapi`, and the optional
`fast` extra `google-crc32c`, only when asked for. Nothing in the package
calls `eval`, `exec`, `pickle`, or `subprocess`, and no code path constructs a
shell command.

**The cryptography is used, not invented.** TLS is `ssl`. Digests are
`hashlib` and `zlib`. Ciphers, RSA, elliptic-curve and Ed25519 signatures and
X.509 parsing come from `cryptography`; ASN.1 structures from `asn1crypto`;
AWS SigV4 signing from `botocore`. What remains local is protocol policy
rather than primitives: the XRootD `sss` and `gsi` handshake framing here, and
in xrdsec the RFC 3820 proxy rules, VOMS attribute-certificate policy, and the
legacy raw-RSA and 512-bit-key compatibility paths those handshakes still
specify on the wire. xrdsec has its own threat model and security policy
(its `SECURITY.md`), and its own suite holds that code to every line and
branch.
Those are pinned against test vectors and against the real daemon, and are not
offered as a general-purpose crypto library; `__all__` does not export them
for that use.

## Limits you should know about

- **JWT signatures are not verified by the client.** `token_claims()` decodes
  the claim set without checking anything, and is used only to fail fast on an
  expired token with a clear message instead of a server-side `3010`.
  Validating the signature is the server's job and duplicating it here would
  mean shipping a JWKS fetcher and a key cache that could disagree with the
  server's.
- **Redirects re-authenticate at the new host.** That is how XRootD load
  balancing works: a manager sends you to a data server and the client offers
  its credentials there. If your token must not leave a known set of hosts,
  set `require_tls=True` and point at those hosts directly rather than at a
  redirector.
- **HTTP redirects keep credentials within one origin.** Over `https://` and
  `davs://`, an `Authorization`, cookie or `TransferHeader*` header follows a
  redirect only when the scheme, host and port are unchanged, and never onto
  plain `http://`; once dropped it stays dropped for the rest of the chain.
  Storage that redirects from a head node to data nodes on other hosts, as
  dCache and EOS do, is named with `trusted_redirect_domains`, which trusts a
  domain and everything under it. A token the redirect's own `Location`
  carries (`?authz=`) is kept, since the redirecting server chose to send it.
  See [HTTP and WebDAV](https://rob-c.github.io/xrdclient/http/).
- **Redirect targets are redacted in the debug log.** A `Location` can
  carry a token or a signature in its query; it passes through
  `xrdclient.log.redact` before it is logged.
- **Multi-range answers are bounded.** A `multipart/byteranges` body may only
  carry the ranges that were asked for, and its framing is read within fixed
  limits, so a hostile server cannot make a vector read buffer a whole file.
- **Checksum verification is a data-integrity check, not authentication.** A
  server that serves you wrong bytes can serve you the matching checksum.
  `verify=True` catches corruption in transit and on disk; it does not catch a
  server that is lying to you.
- **`verify_tls=False` is a footgun and is meant to look like one.** It exists
  because self-signed test endpoints exist. Never set it in production.
- **Credential files must be private.** Bearer-token files are read wherever
  the WLCG discovery specification says to look, including `/tmp/bt_u$UID`,
  but - like X.509 proxies and SSS keytabs - only if they are regular files
  owned by the effective uid with mode `0600` or tighter, checked on the open
  file; a symbolic link at a predictable `/tmp` or `$XDG_RUNTIME_DIR` name is
  refused. A violation is an `InsecureCredentialError` naming the file and
  `chmod 600`. See [Authentication](https://rob-c.github.io/xrdclient/auth/).
- **A GSI server proves who it is before it sees your proxy chain.** Its
  certificate must chain to a local CA and name the host dialled, on every
  login; `XRD_GSI_VERIFY_SERVER=0` is the escape hatch for broken test setups.

## Auditing your own setup

    python -c "import xrdclient; print(xrdclient.Config())"      # secrets print as <redacted>
    xrd-fs ls -vvv root://host//store/data           # redacted wire transcript

The second command is the one to paste into a bug report: the redaction filter
runs before any handler sees the record, so the transcript is safe to share.
