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

!!! note "Not implemented"
    GSI's signed Diffie-Hellman variant and X.509 delegation are refused by
    name rather than mis-answered. Encryption-required endpoints that insist
    on them will say so.

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

The one mechanism that needs an extra:

```console
$ pip install xrdclient[krb5]
```

The credential cache is read with no help at all - so an expired ticket is
reported as such before anything is sent - but the exchange itself goes
through `gssapi`, because a Kerberos token can only honestly be tested
against a live KDC.

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
