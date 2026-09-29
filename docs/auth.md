# Authentication

The server offers mechanisms; the client tries the ones it has, in
`config.auth_order`, and stops at the first that is accepted.

```python
Config(auth_order=("gsi", "ztn", "krb5", "sss", "unix", "host"))   # the default
```

If everything is refused you get one `NoMechanismError` that names each
mechanism and why it did not apply - "no proxy at $X509_USER_PROXY", "token
expired at ...", "server did not offer sss" - rather than a bare failure.

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

The whole path is pure Python - DER, X.509, RSA, AES - so there is no
`openssl` to have the wrong version of.

Which Diffie-Hellman exchange is used is the server's call, as it is for the
stock client: a server at GSI version 10400 or later (every current one) gets
the *signed* exchange - each side signs its DH value with its private key, the
server's is checked against the key in its certificate, and every encrypted
buffer carries a fresh IV. Older servers get the original unsigned exchange.

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
  `ca_path` (`$X509_CERT_DIR`, else `/etc/grid-security/certificates`) and
  names the host dialled - by `CN`, `<service>/<host>` or `subjectAltName`.
  A server that fails either is still logged into, and told "Not allowed to
  sign proxy requests", exactly the stock client's answer;
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
order the C client uses:

1. `Config(token=...)`
2. `$BEARER_TOKEN`
3. `$BEARER_TOKEN_FILE` (or `Config(token_file=...)`)
4. `$XDG_RUNTIME_DIR/bt_u$UID`
5. `/tmp/bt_u$UID`

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

Pure Python, like everything else: `kinit` as usual, and the client does the
rest.

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
| `API:` (macOS) | refused - held by Heimdal's credential service over XPC | - |
| `MEMORY:`, `MSLSA:` | refused - they live in another process, or Windows | - |

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

### What was tested against what

| Claim | Tested against |
| --- | --- |
| KCM wire format: framing, opcodes, status codes, marshalling | MIT krb5 1.22 (Homebrew, macOS) and 1.21 (AlmaLinux 9) `kinit`, `klist` and `kvno` running against the in-process fake daemon (`tests/_kcmd.py`) - with and without MIT's extensions |
| KCM reading returns exactly what the FILE reader returns | caches MIT wrote (`tests/_krb5_mit.py`), served by the fake daemon |
| a TGT from KCM logs in to `xrootd`; the stored ticket is one MIT uses | the fake daemon + a real MIT KDC + a real `xrootd`; MIT's `klist` lists the stored ticket and MIT's `kvno` uses it with the KDC stopped |
| the same, against a **real** `sssd-kcm` | sssd 2.9.8 on AlmaLinux 9 (in a container), with MIT 1.21 and `xrootd` 5.9.7: `tests/test_krb5_kcm.py::test_a_real_kcm_daemon_...`, which runs only with `XRDCLIENT_TEST_SYSTEM_KCM=1` because it uses the machine's daemon |
| Heimdal's own `kcm` | only as the fake daemon's `heimdal=True` mode (no MIT extensions); not against Heimdal itself |
| KEYRING layout and resolution, every anchor, the error paths | a fake `keyctl` (`FakeKernel` in `tests/test_krb5_keyring.py`) that is called exactly as the syscall is, ctypes buffers included - runs everywhere |
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
| a macOS `API:` cache, or `MEMORY:` | `KRB5CCNAME=FILE:/tmp/krb5cc_$(id -u) kinit` |
| a `KEYRING:` cache anywhere but Linux | the same |
| tickets that have expired | `kinit` - the error says how long ago |
| a realm with no `kdc =` line (DNS SRV lookup is not done) | add `[realms] REALM = { kdc = host }` |
| a service in another realm than your TGT (cross-realm) | not supported |
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

The same X.509 proxy is presented as the client certificate, so mutual TLS
costs no extra argument. `verify_tls=False` exists for self-signed test
endpoints and is spelled `--no-verify-tls` on the command line so that it
shows up in shell history and in review.

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
