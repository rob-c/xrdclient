# Authentication

The server offers mechanisms; the client tries the ones it has, in
`config.auth_order`, and stops at the first that is accepted.

```python
Config(auth_order=("gsi", "ztn", "krb5", "sss", "unix", "host"))   # the default
```

If everything is refused you get one `NoMechanismError` that names each
mechanism and why it did not apply - "no proxy at $X509_USER_PROXY", "token
expired at ...", "server did not offer sss" - rather than a bare failure.

To *make* a proxy (with VOMS attributes) rather than use one, see
[Proxies and VOMS](proxy.md) and the `xrd-proxy` command.

## Credential files must be yours alone

A proxy, a user key (`userkey.pem`), a bearer-token file and an SSS keytab
are each read only when the
file is a regular file, owned by the effective uid, and neither readable nor
writable by group or others - mode `0600` (or `0400`), what
`voms-proxy-init`, `htgettoken` and `xrdsssadmin` write and what `XrdSecgsi`
and `XrdSecsss` expect. Anything else raises `InsecureCredentialError` (a
`CredentialError` and a `PermissionError`) naming the file and the fix:

```text
the X.509 proxy /tmp/x509up_u1000 is readable by group or others (mode 0644); run: chmod 600 /tmp/x509up_u1000
```

The checks are made on the open file descriptor, so the file checked is the
file read. At the predictable locations - `/tmp/x509up_u<uid>`,
`$XDG_RUNTIME_DIR/bt_u<uid>` and `/tmp/bt_u<uid>` - a symbolic link is
refused rather than followed, since anyone can create one in `/tmp`; a path
you named (`$X509_USER_PROXY`, `$BEARER_TOKEN_FILE`, `Config(proxy=...)`,
`--token-file`) may be a link, and the file it reaches is checked. Ownership
is strict for root too: a process running as root reads only root's own
credential files, never one another user left behind - copy a file that
has to be shared rather than opening it in place.

A refused proxy or keytab is skipped with a warning (the ladder moves on,
and `NoMechanismError` repeats the reason); a refused token file stops token
discovery, because falling through to the next location would log in as
whoever left a token there.

## `gsi` - X.509 proxies

Reads `$X509_USER_PROXY`, or `/tmp/x509up_u$UID`. RFC 3820 and legacy Globus
proxies are both understood, as are the RFC 3820 proxy-certificate extensions
and the CA chain in `$X509_CERT_DIR`.

```python
Config(proxy="/tmp/x509up_u1000", ca_path="/etc/grid-security/certificates")
```

The proxy's lifetime is checked **before** the round trip, so an expired proxy
is a sentence and not a mystery timeout an hour into a batch job.

```python
from xrdclient.crypto.x509 import load_proxy
proxy = load_proxy("/tmp/x509up_u1000")
print(proxy.identity, proxy.remaining() / 3600, "hours left")
```

Reading proxies, issuing them, VOMS and trust checks are
[xrdsec](https://github.com/rob-c/xrdsec)'s since 0.4.0 - the grid security
library xgfalclient and xrdarc use too, built on `cryptography` and
`asn1crypto` - and `xrdclient.crypto.x509`, `.proxies`, `.voms` and the rest
are its modules under xrdclient's names (`xrdclient.crypto.voms is
xrdsec.voms`). What stays in xrdclient is XRootD's own protocol: the GSI,
`sss`, Kerberos and `ztn` handshakes, and the `kXR_sigver` signer.

### A certificate and key without a proxy

A robot account, a CI job or a host certificate often has no proxy - just
`usercert.pem` and `userkey.pem`. Name them (or set `$X509_USER_CERT` and
`$X509_USER_KEY`, as for `XrdSecgsi` and gfal2) and leave `proxy` unset:

```python
Config(
    proxy=None,
    usercert="~/.globus/usercert.pem",
    userkey="~/.globus/userkey.pem",
    key_password=lambda: keyring.get_password("grid", "userkey"),  # only if encrypted
)
```

GSI then makes a 12-hour RFC 3820 proxy from them **in memory** - a fresh
2048-bit key, signed by yours, as `XrdSecgsi`'s `createpxy` does - and logs in
with that; HTTPS presents the certificate itself. Nothing is written to disk:
there is no need to splice the key into a temporary proxy file (which a crash
leaves behind). The key must pass the checks above. The order is gfal2's: a
proxy you named wins, then this pair, then `/tmp/x509up_u$UID`.

### VOMS attributes

A VOMS proxy is an ordinary RFC 3820 proxy with signed attribute
certificates in its certificate extensions. The complete proxy chain is
already sent by `gsi` and by mutual TLS on `roots://`, `xroots://` and
`davs://`; existing proxies use the same authentication flow.

The attributes can also be inspected and verified locally:

```python
from xrdclient.crypto import load_proxy, validate_voms

proxy = load_proxy("/tmp/x509up_u1000")
voms = validate_voms(proxy.chain)
if not voms.verified:
    raise RuntimeError(voms.message)

print(voms.vos)
print(voms.fqans)
```

`validate_voms` checks the AC version, holder binding through delegated
parents, validity, the embedded signer and signature, issuer, optional target
host, signer CA path, unknown critical extensions and the VO's `.lsc` file.
VOMS server signatures using RSA PKCS#1 with SHA-1 through SHA-512, RSA-PSS,
ECDSA P-256 with SHA-256, or Ed25519 are accepted; MD5-class, unsupported
curves and unknown algorithms fail closed.
Multiple ACs are independent: one bad VO does not discard another verified
one. `inspect_voms(proxy.chain)` decodes the same VO, FQAN and generic
attribute data without treating it as trusted.

The CA-chain check currently covers signatures, issuer names and dates,
not full RFC 5280 extension constraints or certificate revocation lists
(CRLs). It is not a complete replacement for an established path validator.

`voms.message` explains failures in plain language. `voms.diagnostics`
exposes stable `code`, `message`, `path` and `errno` fields for missing,
unreadable or damaged CA files, expired CA/signing certificates, and
missing, malformed or mismatched `.lsc` files. Expiry dates use UTC.
Do not repair signer mismatches by trusting certificates copied from a proxy,
or solve permissions errors by making private keys world-readable.

`xrdclient.crypto.check_vomses(path)` separately checks a vomses file or
directory: readable UTF-8, five endpoint fields and valid port numbers.
Its diagnostic tuple reports file paths and malformed line numbers without
contacting servers. It helps debug obtaining a proxy; using an existing
proxy never requires vomses. It does not establish endpoint trust or reachability.

Trust directories are found in this order:

1. `$X509_CERT_DIR` and `$X509_VOMS_DIR`;
2. `/etc/grid-security/certificates` and `/etc/grid-security/vomsdir`;
3. on macOS, the Apple Silicon Homebrew prefix under `/opt/homebrew/etc`, then
   the Intel prefix under `/usr/local/etc`.

The CA and VOMS trust material must still be installed at one of those
locations. This code consumes a proxy minted by a VOMS service; it does not
request VO assertions, because only that service can sign them.

Which Diffie-Hellman exchange is used is the server's call, as it is for the
stock client: a server at GSI version 10400 or later (every current one) gets
the *signed* exchange - each side signs its DH value with its private key, the
server's is checked against the key in its certificate, and every encrypted
buffer carries a fresh IV. Older servers get the original unsigned exchange.

Either way the server's group and public value are checked first, as
OpenSSL's `DH_check_pub_key` checks them: a prime under 512 bits is
refused, one under 2048 bits is used with a warning (current `xrootd` sends
3072 bits; older servers may send less), and a public value that is not
strictly between 1 and p-1 - which would pin the shared secret to a value
anyone can compute - is refused. The arithmetic stays on Python integers:
`cryptography` 50 deprecated finite-field DH, and GSI cannot move off it.

### The server proves who it is

On every login - not only before delegating - the server's certificate must
chain to a CA in `ca_path` (`$X509_CERT_DIR`, else the grid-security
directory) and name the host that was dialled, by `CN`, `<service>/<host>`
or `subjectAltName`, before the proxy chain is sent to it. That is what
`XrdSecgsi` clients do. A server whose certificate carries another name -
an alias, a load-balanced head node - can be allowed by pattern with the
stock variable:

```console
$ export XrdSecGSISRVNAMES='*.example.org,door-*.site.net'
```

The patterns (`*` and `?` wildcards, comma-separated) are matched against
the host name in the certificate's `CN`; the chain and dates are still
checked in full. For a test setup that cannot pass at all,
`Config(gsi_verify_server=False)` or `XRD_GSI_VERIFY_SERVER=0` turns the
login check off; a proxy is still never *delegated* to a server that fails
it.

### Delegation

A server that is to act for you - a third-party copy pulling from another
site, a gateway staging to tape - needs a proxy of its own. Delegation gives it
one without your private key leaving your machine: the server makes a key pair
and sends a certificate request, and the client signs it into a proxy one
link below yours.

```python
Config(gsi_delegate=True)          # or: export XrdSecGSIDELEGPROXY=1
```

It is off by default, as in the stock client, and the same environment
variable turns it on. When it is on:

- the client tells the server it will sign (`kOptsDlgPxy | kOptsSigReq`), so a
  server configured with `-exppxy` asks, whatever its `-dlgpxy` says;
- it signs nothing until the server's certificate chains to a CA in
  `ca_path` (`$X509_CERT_DIR`, else the Linux/Homebrew grid-security path) and
  names the host dialled - by `CN`, `<service>/<host>` or `subjectAltName`.
  With the login check on (the default) such a server is not logged into at
  all; with it off, it is logged into and told "Not allowed to sign proxy
  requests", exactly the stock client's answer;
- delegation needs the signed exchange; a server older than GSI 10400 is
  logged into without one, with a warning.

The delegated proxy follows XrdCrypto's profile: subject `<your proxy>/CN=<serial>`,
valid until your proxy expires and no longer, your proxy's extensions copied
with a critical RFC 3820 `ProxyCertInfo` (`inheritAll`), `sha256WithRSAEncryption`.
Two deliberate differences: the request's own signature is checked before
anything is signed, and your proxy's key identifiers are not copied onto a
certificate for a different key - so the proxy the server ends up with passes
`openssl verify -allow_proxy_certs`, which one delegated by the stock client
does not. A path-length constraint is carried down one step, and a proxy
already at zero refuses rather than sign something no verifier accepts.

`XrdSecGSIDELEGPROXY=2` - the stock client's "send my private key instead" -
is treated as `1`: the key is never sent.

`xrdcp` is the odd one out among the stock tools: it sets
`XrdSecGSIDELEGPROXY` itself, on only for `--tpc delegate`, so an exported
value has no effect on it. The Python bindings and this client both honour it.

## `ztn` - bearer tokens (WLCG, SciTokens, macaroons)

Discovery follows the WLCG Bearer Token Discovery specification, the same
order the C client uses - and since 0.4.0 the same order for `root://`,
`davs://` and HTTP third-party copy, which share one implementation:

1. a token in the URL (`?authz=` or `?access_token=`; HTTP only)
2. `Config(token=...)`
3. `Config(token_file=...)`, when set to something other than
   `$BEARER_TOKEN_FILE` (its default) - in code, a settings file or
   `--token-file`
4. `$BEARER_TOKEN`
5. `$BEARER_TOKEN_FILE`
6. `$XDG_RUNTIME_DIR/bt_u$UID`
7. `/tmp/bt_u$UID`

`$UID` is the effective uid (`id -u`). An unset variable, a missing file or
a blank one passes to the next step; a token file that is not private to you
stops discovery with `InsecureCredentialError` (see above). Write one with
`(umask 077; oidc-token <issuer> > /tmp/bt_u$(id -u))`, so it is `0600` from
the moment it exists.

```python
Config(token=os.environ["MY_TOKEN"])
```

A JWT's `exp` claim is read - without verifying the signature, which is the
server's job - so an expired token fails immediately with
`TokenExpiredError` and the expiry time in the message. Opaque tokens are
sent as-is: the token goes on the wire inside the `TokenResp` that
`XrdSecProtocolztn` expects, so stock reads it as a token rather than as a
response code it does not recognise.

Carry tokens over TLS. `roots://`, `xroots://` and `davs://` are TLS by
scheme; `Config(require_tls=True)` refuses a server that will not upgrade.

## `krb5` - Kerberos

`kinit` as usual, and the client does the rest - through the system's own
Kerberos library when python-gssapi is installed (`xrdclient[krb5]`), and in
pure Python otherwise. Both are xrdsec's (`xrdsec.krb5`: the pure-Python
client in `xrdsec.krb5.client`, the GSS-API layer in `xrdsec.krb5.gss`),
shared with xgfalclient since 0.4.0; what xrdclient adds is XRootD's
`krb5` framing and the choice between the two.

```console
$ kinit jane@EXAMPLE.ORG
```

What the server's `krb5` plugin wants is a raw Kerberos AP-REQ for the
principal it names in its offer (`xrootd/host@REALM`). The client reads the
credential cache MIT would - `$KRB5CCNAME`, else `default_ccache_name` from
`krb5.conf`, else `/tmp/krb5cc_<uid>` - and uses the service ticket there if
`kvno` or an earlier program already fetched one. If the cache holds only
your ticket-granting ticket, it asks the KDC for the service ticket itself (a
TGS exchange, over UDP with a TCP fallback, to the `kdc` listed for the realm
in `$KRB5_CONFIG` or `/etc/krb5.conf`) and keeps it in memory for the rest of
the process. A server started with `-exptkn` (its offer ends `,fwd`) also
gets a forwarded TGT, which needs a forwardable one: `kinit -f`. As with
MIT's default `kdc_timesync`, the authenticators carry the local time
corrected by the KDC clock offset `kinit` recorded in the cache, so a host
whose clock has drifted still logs in. `include` and `includedir` are
followed wherever they start a line of `krb5.conf`, as MIT follows them.

### Credential cache types

| Cache | Read how | A service ticket fetched from the KDC |
| --- | --- | --- |
| `FILE:path`, a bare path | the file | kept in memory; the file is never written |
| `DIR:dir`, `DIR::dir/tktX` | the collection's `primary` cache, or the one named | kept in memory |
| `KCM:`, `KCM:name` (RHEL 9's default, `sssd-kcm`; Heimdal's `kcm`) | the KCM protocol over the daemon's Unix socket | kept in memory **and** stored in the cache (`STORE`), as MIT's library does |
| `KEYRING:persistent:uid`, `KEYRING:session:name`, `KEYRING:user:name`, `KEYRING:process:name`, `KEYRING:thread:name`, `KEYRING:name` | the Linux `keyctl` system call, through `ctypes` | kept in memory; the keyring is never written |
| `API:` (macOS) | through python-gssapi only - held by Heimdal's credential service over XPC | the library's business |
| `MEMORY:`, `MSLSA:` | `MEMORY:` through python-gssapi; `MSLSA:` refused - Windows | - |

**KCM.** The socket is `kcm_socket` in `[libdefaults]`, else
`/var/run/.heim_org.h5l.kcm-socket`. `KCM:` asks the daemon for its default
cache (`GET_DEFAULT_CACHE`) and reads its principal, KDC clock offset and
credentials - all at once with MIT's `GET_CRED_LIST` where the daemon has it
(sssd does), else one at a time by UUID (Heimdal's daemon). No daemon, or
no such cache, is "no Kerberos credential", as a missing file is; a daemon
that answers with an error is a `CredentialError` naming the cache. A ticket
fetched from the KDC is stored back because that is what every MIT program
on the host does with a KCM cache, and because it is safe to: the daemon
serialises its clients, and the credential is marshalled byte-for-byte as
MIT marshals it (a test re-marshals MIT's own and compares bytes). A daemon
that refuses the store costs the next process one TGS exchange; the login
goes ahead.

**KEYRING.** The residual is MIT's `anchor:collection[:cache]`. The
collection is the keyring `_krb` in the user's persistent keyring
(`KEYCTL_GET_PERSISTENT`), or `_krb_<collection>` under the special keyring
the anchor names; its current cache is named by the `krb_ccache:primary`
key, and each cache keyring holds `__krb5_princ__`,
`__krb5_time_offsets__` and one key per credential, in the FILE format's
marshalling. A bare `KEYRING:name` is MIT's legacy form, and also finds a
cache a pre-1.12 MIT left in the session keyring under that name. This
client does not create keyrings, so a missing one is no cache; nor does it
add keys, so a fetched ticket stays in memory, as for a file. Off Linux,
`KEYRING:` is a `CredentialError` that says so and names the fix.

### Which implementation: `XRD_KRB5_BACKEND`

| Value | What logs in |
| --- | --- |
| unset, `auto` | the system library through python-gssapi when it is installed and the server does not ask for a forwarded TGT; the portable reader otherwise, or when the library finds no credential |
| `gssapi` | the system library alone; its errors are reported as they are |
| `portable` | the pure-Python reader, TGS exchange and AP-REQ builder described above |

Why the system library first: it reads every cache type the platform has
(macOS `API:` and `MEMORY:` included), discovers KDCs through DNS, follows
cross-realm referrals and supports whatever enctypes the site allows - all
of which the portable reader refuses. Its GSS-API token is unwrapped to the
raw AP-REQ XRootD's `krb5` plugin requires, and that is checked against a
real MIT KDC and `xrootd`, both with only a TGT in the cache and with the
service ticket already there (`tests/test_krb5_interop.py`). Like every
other Kerberos program it may store the service ticket it fetched in your
cache. A service principal the server names without a realm gets one from
`[domain_realm]` first, as on the portable path.

Why the portable reader stays: it needs nothing installed, and it is the
only way to forward a TGT to an `-exptkn` server - python-gssapi cannot hand
over the raw KRB-CRED XRootD reads. A forwarding login always uses it.

Why there is no `native` any more: it read the cache through pykrb5, but
python-gssapi reaches the same caches through the same library and also does
the TGS exchange, so pykrb5 added a second binding and a second set of error
paths for nothing. It was removed in 0.4.0; `XRD_KRB5_BACKEND=native` is now
a `CredentialError` that says to leave the variable unset (or set `gssapi`).
The portable reader stays: its parity with MIT is what the tests below
establish, and removing it would lose forwarding and the no-dependency
install.

macOS wheels of python-gssapi install without a compiler. Linux installs of
the optional extra need a C compiler and Kerberos development headers when
no compatible wheel is available; distributions package it as
`python3-gssapi`. Normal installs do not request native bindings.

### Portable implementation interoperability evidence

| Claim | Tested against |
| --- | --- |
| KCM wire format: framing, opcodes, status codes, marshalling | MIT krb5 1.22 (Homebrew, macOS) and 1.21 (AlmaLinux 9) `kinit`, `klist` and `kvno` running against the in-process fake daemon (`xrdsec.testing.krb5.FakeKcm`) - with and without MIT's extensions |
| KCM reading returns exactly what the FILE reader returns | caches MIT wrote (xrdsec's `tests/_krb5_mit.py`), served by the fake daemon |
| a TGT from KCM logs in to `xrootd`; the stored ticket is one MIT uses | the fake daemon + a real MIT KDC + a real `xrootd`; MIT's `klist` lists the stored ticket and MIT's `kvno` uses it with the KDC stopped |
| the same, against a **real** `sssd-kcm` | sssd 2.9.8 on AlmaLinux 9 (in a container), with MIT 1.21 and `xrootd` 5.9.7: `tests/test_krb5_kcm.py::test_a_real_kcm_daemon_...`, which runs only with `XRDCLIENT_TEST_SYSTEM_KCM=1` because it uses the machine's daemon |
| Heimdal's own `kcm` | only as the fake daemon's `heimdal=True` mode (no MIT extensions); not against Heimdal itself |
| KEYRING layout and resolution, every anchor, the error paths | a fake `keyctl` (`FakeKernel` in xrdsec's `tests/test_krb5_keyring.py`) that is called exactly as the syscall is, ctypes buffers included - runs everywhere |
| the real `keyctl` syscall; MIT's real keyring layout | Linux only, skipped elsewhere: the syscall reading the session keyring; a cache laid out with `keyutils`' `keyctl`; MIT `kinit` into `KEYRING:session:` and `KEYRING:persistent:` then a login to a real `xrootd` - all run on AlmaLinux 9 (kernel keyrings through Docker with `seccomp=unconfined`) |
| macOS `API:` | refused; nothing to test beyond the message |

Where MIT's sources were not to hand, the protocol was written from
knowledge of `cc_kcm.c`, `kcm.h` and `cc_keyring.c`, and confirmed by the
tests above rather than by reading them: the Homebrew build has headers
(`krb5.h`, for the error codes used) but no `kcm.h`. The heim-ipc reply
frame (length, transport status, then the KCM status) was confirmed by
MIT's `kinit` against the fake daemon; the keyring layout by MIT's `kinit`
writing a real kernel keyring that this client then read.

### Limits

Supported: the AES enctypes - `aes256-cts-hmac-sha1-96`,
`aes128-cts-hmac-sha1-96`, `aes256-cts-hmac-sha384-192` and
`aes128-cts-hmac-sha256-128` - in the order `default_tgs_enctypes` or
`permitted_enctypes` gives. Refused, each with an error saying so:

| Situation | What to do |
| --- | --- |
| a macOS `API:` cache, or `MEMORY:` | uses python-gssapi for a non-forwarded AP-REQ; requires native-library support. For forwarding, use `KRB5CCNAME=FILE:/tmp/krb5cc_$(id -u) kinit -f` |
| a `KEYRING:` cache anywhere but Linux | the same |
| tickets that have expired | `kinit` - the error says how long ago |
| a realm with no `kdc =` line (DNS SRV lookup is not done) | add `[realms] REALM = { kdc = host }`, or install python-gssapi |
| a service in another realm than your TGT (cross-realm) | install python-gssapi; not supported by the portable reader |
| DES, triple-DES or RC4 keys | ask for AES keys - RC4 needs MD4, which `hashlib` often lacks |

Host names are not canonicalised through DNS (as with
`dns_canonicalize_hostname = false` and `rdns = false`); the server names its
own principal in its offer, so none is needed. When it does not, the realm
comes from `[domain_realm]`, else your own.

## `sss` - Simple Shared Secret

Reads the keytab named by `$XrdSecSSSKT`, `$XrdSecsssKT`, `Config(keytab=...)`
or `~/.xrd/sss.keytab`, and picks the key the server asked for by name.

```console
$ chmod 600 ~/.xrd/sss.keytab
```

A keytab that group or others can read is **refused**, exactly as the C
implementation refuses it, with a warning in the log. The file holds shared
secrets in the clear.

SSS encrypts its credential with Blowfish, which OpenSSL 3 only provides
through its *legacy* provider. On a host in FIPS mode, or one whose OpenSSL
runs without that provider, SSS is unavailable: the mechanism says so - and
what to do about it - and the ladder moves on to the next one. If site
policy allows, point `OPENSSL_CONF` at an `openssl.cnf` that activates both
the `default` and `legacy` providers; otherwise ask the server's
administrators for `ztn`, `gsi` or `krb5`.

## `unix` and `host`

No material at all: the username, or nothing. These are what a loopback or
intra-site endpoint usually wants, and they are last in the order for that
reason.

```python
Config(auth_order=("unix", "host"))     # skip the ladder entirely
```

This is worth setting explicitly for a local daemon: the default order tries
`gsi` first and will spend time looking for a proxy that is not there.

## Choosing a username

```python
Config(username="atlasprd")
```

Defaults to the local login name. A URL may also carry one:
`root://alice@host//store/f.root`.

## TLS

```python
Config(
    require_tls=True,        # refuse a server that will not upgrade
    verify_tls=True,         # the default; never turned off implicitly
    ca_file="/etc/ssl/certs/ca-bundle.crt",
    ca_path="/etc/grid-security/certificates",
)
```

The same X.509 proxy is presented as the client certificate - or, with no
proxy named, `usercert` and `userkey` - so mutual TLS costs no extra argument.
`tls_system_ca` and `check_crl` (`$XRD_TLSSYSTEMCA`, `$XRD_CHECKCRL`) choose
the extra trust store and revocation checking; see
[Configuration](config.md#security). `verify_tls=False` exists for self-signed test
endpoints and is spelled `--no-verify-tls` on the command line so that it
shows up in shell history and in review.

A tool that makes its own HTTPS connections can have the same context:
`xrdclient.tls.client_context(cert=..., ca_path=..., check_crl=...)` -
which is `xrdsec.tls.client_context`, the builder `tls_context()` calls - built
by the same code and sharing its cache - with one difference, that it
presents no certificate unless given one (`present_default=True` restores
the default-proxy behaviour). The searches behind the defaults above
(`find_proxy`, `find_user_credential`, `find_bearer_token`, `grid_ca_path`)
are public in `xrdclient.credentials` (`xrdsec.credentials`); see
[Building a tool on xrdclient](building-tools.md).

## Being asked for what is missing

At a terminal, a login that would have failed for want of a proxy or a token
asks for one instead of dying:

```console
$ xrd-fs ls root://eoslhcb.cern.ch//eos/lhcb/user/j/jane
xrd: eoslhcb.cern.ch accepts gsi, but an X.509 proxy is missing
     why: the proxy in /tmp/x509up_u1000 expired 3h 20m ago
     fix: voms-proxy-init -voms <your VO>, or point $X509_USER_PROXY at one
     path to a proxy file (Enter to skip):
```

Answering with a path is enough; pressing Enter declines and the usual
`NoMechanismError` follows. A `ztn` question takes either a token pasted
whole or the path to a file holding one, and reads it with `getpass`, so it
is neither echoed nor left in the scrollback.

The rules are deliberately narrow, because a library that asks at the wrong
moment is a library that hangs a batch job:

* only when **every** mechanism failed - a working `unix` fallback is never
  interrupted;
* only for mechanisms the server actually offered, and only for material a
  person could type (`gsi`, `ztn`, `sss` - never `krb5`);
* only when both `stdin` and `stderr` are a terminal;
* once per `(mechanism, endpoint)` per process, with a refusal remembered
  just as firmly as an answer - and one more question if the first answer was
  a typo, saying what was wrong with it;
* on `stderr`, so `xrd-fs cat ... | wc -c` still gets bytes and only bytes.

Over HTTP there is no security trailer to work from, so a `401` stands in for
one: `WWW-Authenticate: Bearer` asks for a token, and an `https` endpoint
that says anything else is asked for the X.509 proxy. Only the first `401` of
a request asks - a second one means the credential was refused, not absent.

Turning it off, or putting the question somewhere else:

```python
Config(prompt=False)          # never ask; fail with the usual error
Config(prompt=True)           # ask even when this is not a terminal
Config(prompter=my_dialog)    # a GUI, a notebook widget, a secrets manager
```

`$XRD_PROMPT=0` does the same for a whole job, and `--no-prompt` for one
command. A prompter is any callable taking an
[`Ask`][xrdclient.auth.prompt.Ask] - the mechanism, what is missing, why, the fix,
and whether the answer is secret - and returning what was typed, or `None` to
decline. Answers live in this process only; `xrdclient.auth.forget()` drops them.

With nobody there to ask, the same explanation goes into the error instead:

```
NoMechanismError: no usable authentication mechanism (server offered: gsi,
ztn, unix) [gsi: the proxy in /tmp/x509up_u1000 expired 3h 20m ago; try:
voms-proxy-init -voms <your VO> ...]
```

## Debugging a refusal

```console
$ xrd-fs ls -vvv root://host//store
```

`-vvv` logs the wire. Credentials are redacted before any handler sees the
record, so the transcript is safe to paste into a bug report - see
[Security](security.md).
