# Proxies and VOMS

xrdclient makes X.509 proxies itself — in memory, through one issuer with
named profiles — and fetches VOMS attribute certificates from a VOMS server's
REST interface. Together they replace `voms-proxy-init`, `voms-proxy-info` and
`voms-proxy-destroy` for the common cases, from Python or from the `xrd-proxy`
command, with no private key ever written to a temporary file.

## The `xrd-proxy` command

```console
$ xrd-proxy init -voms atlas:/atlas/Role=production -valid 24:00
Contacting voms-atlas-auth.cern.ch:443 [/DC=ch/DC=cern/OU=computers/CN=atlas-auth.cern.ch] "atlas"...
Remote VOMS server contacted successfully.

Created proxy in /tmp/x509up_u1000.

Your proxy is valid until Sun Oct 19 12:00:00 CEST 2026

$ xrd-proxy info -all
$ xrd-proxy info -exists -valid 1:00 && echo "good for another hour"
$ xrd-proxy info -acexists atlas
$ xrd-proxy destroy
```

The options are spelled as the VOMS clients spell them (one dash; the
two-dash form works too):

| `init` | |
| --- | --- |
| `-voms VO[:FQAN]` | ask `VO`'s VOMS server for an AC; repeat for more FQANs or VOs |
| `-vomses PATH` | read the server list here (repeatable) instead of the defaults |
| `-valid HH:MM`, `-hours H` | proxy lifetime (default 12:00) |
| `-vomslife HH:MM` | AC lifetime to ask for (default: the proxy's) |
| `-bits N` | key size (default 2048) |
| `-limited` | a limited proxy |
| `-path_length N` | allow at most `N` further delegations |
| `-cert`, `-key` | user credential (default `$X509_USER_CERT`/`$X509_USER_KEY`, else `~/.globus/usercert.pem`/`userkey.pem`) |
| `-pwstdin` | read the key's pass phrase from standard input |
| `-out PATH` | where to write (default `$X509_USER_PROXY`, else `/tmp/x509up_u<uid>`) |
| `-certdir DIR` | CA directory that authenticates the VOMS server |
| `-rfc`, `-old`, `--profile P` | proxy profile (below); `-old` is a GT2 legacy proxy |

| `info` | |
| --- | --- |
| `-file PATH` | the proxy to describe (default as above) |
| `-all` | also print each VOMS AC |
| `-subject -issuer -identity -type -strength -timeleft -path -vo -fqan -actimeleft` | print just those values, one per line |
| `-exists [-valid HH:MM]` | exit `0` only if a proxy with that much time left is there |
| `-acexists VO` | exit `0` only if it carries a live AC for `VO` |

`destroy -file PATH` removes the proxy (`-dry` says what it would remove). A
symbolic link is removed, never the file it points to, and another user's file
is left alone.

`--json`, `--xml` and `--output-format text|json|xml` print the
[`storage-client-report`](output.md) document `xrd-fs` writes; `info --json`
carries every field below, VOMS ACs included. As for `xrd-fs`, they go before
or after the subcommand (`xrd-proxy --json info` or `xrd-proxy info --json`),
and `xrd-proxy --output-format json --help` reports the help too.

An encrypted key is decrypted with the pass phrase asked for on the terminal,
through the same prompter as every other credential
([`Config(prompter=...)`](auth.md), `XRD_PROMPT`), or read from standard input
with `-pwstdin`.

## From Python

```python
from xrdclient.crypto.proxies import create_proxy, describe_proxy, load_credential, write_proxy
from xrdclient.voms_client import fetch_ac, find_vomses, servers_for

user = load_credential("~/.globus/usercert.pem", "~/.globus/userkey.pem", password=ask)
ac = fetch_ac(servers_for("atlas", find_vomses()), user, fqans=["/atlas/Role=production"])
proxy = create_proxy(user, voms_acs=[ac])          # in memory: certificate, fresh key, chain
write_proxy("/tmp/x509up_u1000", proxy)            # atomic, 0600, links refused
print(describe_proxy(proxy).time_left)
```

The whole `xrd-proxy init`/`info` workflow is `xrdsec.proxytool`, which
`xrdarc proxy` (`arcproxy`) runs too: `find_user_pair(VOMS_PROXY_INIT)` (or
`ARCPROXY`) chooses the user certificate and key, `voms_requests` turns
`VO[:FQAN]` options into requests, `make_proxy(signer, ProxyRequest(...),
progress)` refuses an expired signer, fetches the ACs (reporting through the
`Progress` hooks: contacting, contacted, warned) and only then issues the
proxy, `proxy_target` says where its file goes and whether a link there may
be followed, and `read_proxy_info` reads it back and describes it.

`create_proxy` returns a `ProxyCredential`; nothing touches the disk until
`write_proxy` is called, and a program that only needs the proxy for its own
connections need never call it. `proxy.to_pem()` is the file's contents
(certificate, key, chain — the order every grid tool expects);
`to_pem(with_key=False)` the public chain.

`issue_proxy(signer, public_key_spki, ...)` is the issuer underneath: it
certifies somebody else's key — a delegation — and returns the certificate.
Give it a parsed PKCS#10 request (`xrdclient.crypto.delegation.parse_proxy_request`)
instead of a bare `SubjectPublicKeyInfo` and the request's signature is checked
before its key is certified.

### Profiles

| profile | subject | serial | validity from | extensions |
| --- | --- | --- | --- | --- |
| `rfc3820` (default) | issuer + `CN=<serial>` | random, 31 bits | now − 5 min | `keyUsage`, `ProxyCertInfo`, VOMS ACs carried forward |
| `arc` | issuer + `CN=<serial>` | random, 63 bits | now − 5 min | as `rfc3820` |
| `xrootd` | the request's (issuer + `CN=<serial>` without one) | the request's `CN` | now | the signer's own, copied; `ProxyCertInfo` |
| `globus-legacy` | issuer + `CN=proxy` / `CN=limited proxy` | SHA-1 of the key | now − 5 min | `keyUsage`, VOMS ACs |

Every profile obeys the same rules, because a verifier enforces them: a
limited signer only issues limited proxies; a signer's path-length constraint
is carried down one smaller, and a signer at `0` cannot issue; validity never
starts before the signer's nor ends after the earliest expiry in its chain;
`subjectAltName`, `issuerAltName` and `cA=TRUE` cannot be added (RFC 3820
sections 3.5–3.7); and a legacy proxy is not issued below an RFC 3820 one, whose
path length it could not carry.

The older entry points are thin profiles of `issue_proxy` and produce what
they always did: `xrdclient.crypto.delegation.sign_proxy_request` (GSI
delegation, `xrootd`), `issue_certificate` (the builder, also xgfalclient's
`build_certificate`), and `xrdclient.crypto.userproxy.user_proxy` (the
in-memory proxy GSI makes from `usercert.pem`/`userkey.pem`, `rfc3820`).

### What `describe_proxy` reports

`describe_proxy(chain_or_credential)` returns a `ProxyInfo`: `subject`,
`issuer`, `identity` (the end-entity subject; with no end-entity certificate
in the chain, the leaf with its proxy CNs stripped), `kind` (voms-proxy-info's
`type` wording, e.g. `RFC3820 compliant limited proxy`), `limited`,
`path_length`, `strength`, `key_usage`, `not_before`, `not_after` (the
chain's earliest), `time_left`, and `voms` — every AC, decoded but not
validated (use `xrdclient.crypto.voms.validate_voms` for a trust decision).

## VOMS servers

`find_vomses()` reads `$X509_VOMSES` (or `$VOMS_USERCONF`, `$X509_VOMS_FILE`;
each may list several paths), which must exist when set; otherwise it merges
every one of `~/.voms/vomses`, `~/.glite/vomses`, `~/.arc/vomses`,
`/etc/vomses` and `grid-security/vomses` that exists. Each line is
`"alias" "host" "port" "server DN" "vo"`, checked by the same parser as
`check_vomses`. `servers_for(name, servers)` matches the alias, then the VO
name.

`fetch_ac` asks `GET https://host:port/generate-ac?fqans=...&lifetime=...` —
the REST interface of VOMS 2.x+ and INDIGO IAM — authenticating with the
credential it is given (its key is handed to OpenSSL through a pipe, never a
file). Servers are tried in order:

- a server that cannot be reached, whose certificate does not verify against
  the CA directory (`ca_path`, default `$X509_CERT_DIR`), that answers
  `InternalError` or HTTP 5xx, or whose answer is not a decodable AC is
  skipped; if all fail, `VOMSUnavailableError` lists each reason;
- an explicit refusal — `NoSuchUser`, `SuspendedUser`, `BadRequest`, or any
  other code — is final (`VOMSRefusedError`, with `code` and the server's
  message), since every replica of a VO shares its membership database;
- the returned AC is validated as the proxy carrying it will be (`vomsdir`,
  CA directory); a failure, and any `<warning>` the server sent, is a
  `VOMSWarning` rather than an error, as `voms-proxy-init` warns.

The legacy (pre-REST) GSI socket protocol is not implemented. The server DN
in `vomses` is informational: the TLS connection is authenticated by CA and
host name, as the Java VOMS clients do.

## Files and permissions

`write_proxy` creates the file fresh beside its destination (`mkstemp`: mode
`0600`, `O_EXCL|O_NOFOLLOW`, an unpredictable name), flushes it and renames it
into place, so the proxy is never readable by anyone else and a reader never
sees half of one. A symbolic link at the destination is refused unless
`follow_symlinks=True` (which `xrd-proxy` passes for a path the user named);
another user's file at `/tmp/x509up_u<uid>` cannot be replaced and is reported
by name. `load_credential` applies the same private-file checks as the rest of
the library to every key it reads.

## Interoperability

The test suite checks, where the tools are installed: `openssl verify
-allow_proxy_certs` on two-deep chains of every RFC 3820 profile;
`arcproxy -I` on a proxy carrying an AC from the test VOMS server; that a proxy
`arcproxy` made is read and described; and that `arcproxy -S ... -H` obtains an
AC from the test VOMS server, so the fake speaks the real protocol.
`voms-proxy-info -all` is checked the same way when it is present.
