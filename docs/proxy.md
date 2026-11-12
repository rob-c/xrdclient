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

$ xrd-proxy doctor -voms lhcb                 # can this machine make a proxy at all?
$ xrd-proxy info -exists -valid 1:00 && echo "good for another hour"
$ xrd-proxy info -acexists atlas
$ xrd-proxy destroy
```

`xrd-proxy info` prints everything it can read: the `voms-proxy-info` block,
the proxy's limits, the exact validity, the chain link by link, and one
section per VOMS AC — no `-all` needed.

```console
$ xrd-proxy info
subject   : /DC=ch/DC=cern/OU=Organic Units/OU=Users/CN=jane/CN=123456/CN=987654
issuer    : /DC=ch/DC=cern/OU=Organic Units/OU=Users/CN=jane/CN=123456
identity  : /DC=ch/DC=cern/OU=Organic Units/OU=Users/CN=jane/CN=123456
type      : RFC3820 compliant impersonation proxy
strength  : 2048
path      : /tmp/x509up_u1000
timeleft  : 11:59:59
key usage : Digital Signature, Key Encipherment
proxy     : impersonation
path limit: unlimited
not before: Sat Nov  8 21:49:12 GMT 2026
not after : Mon Nov  9 09:49:12 GMT 2026
chain     : 2 certificates
  [0] /DC=ch/DC=cern/OU=Organic Units/OU=Users/CN=jane/CN=123456/CN=987654
      issuer : /DC=ch/DC=cern/OU=Organic Units/OU=Users/CN=jane/CN=123456
      serial : 987654
      valid  : Sat Nov  8 21:49:12 GMT 2026 to Mon Nov  9 09:49:12 GMT 2026
      kind   : RFC3820 compliant impersonation proxy
  [1] /DC=ch/DC=cern/OU=Organic Units/OU=Users/CN=jane/CN=123456
      issuer : /DC=ch/DC=cern/CN=CERN Grid Certification Authority
      serial : 123456
      valid  : Fri Oct  3 09:12:00 GMT 2025 to Sat Oct  3 09:12:00 GMT 2026
      kind   : end entity credential
=== VO atlas extension information ===
VO        : atlas
subject   : /DC=ch/DC=cern/OU=Organic Units/OU=Users/CN=jane/CN=123456
issuer    : /DC=ch/DC=cern/OU=computers/CN=voms-atlas-auth.cern.ch
attribute : /atlas/Role=pilot
attribute : /atlas/Role=NULL/Capability=NULL
uri       : voms-atlas-auth.cern.ch:443
not before: Sat Nov  8 21:50:03 GMT 2026
not after : Mon Nov  9 09:50:03 GMT 2026
timeleft  : 11:58:00
carrier   : certificate 0
status    : decoded (describing does not check trust; xrdsec.voms.validate_voms does)
```

`-short` is the old compact block (for a script that greps it, or a terminal
that does not want the chain), and the field selectors below print one value
per line as before.

The options are spelled as the VOMS clients spell them (one dash; the
two-dash form works too):

| `init` | |
| --- | --- |
| `-voms VO[:FQAN]` | ask `VO`'s VOMS server for an AC; repeat for more FQANs or VOs |
| `-list-vos` | list the VOs with a configured VOMS server (and their LSC), then exit. On a CVMFS `vomses` directory this reads one file per VO (126 in the WLCG bundle, ~20 s on a Mac's FUSE mount) - `-voms VO` reads only that VO's |
| `-vomses PATH` | read the server list here (repeatable) instead of the defaults |
| `-valid HH:MM`, `-hours H` | proxy lifetime (default 12:00) |
| `-vomslife HH:MM` | AC lifetime to ask for (default: the proxy's) |
| `-bits N` | key size (default 2048) |
| `-limited` | a limited proxy |
| `-path_length N` | allow at most `N` further delegations |
| `-cert`, `-key` | user credential, PEM (default `$X509_USER_CERT`/`$X509_USER_KEY`, else `~/.globus/usercert.pem`/`userkey.pem`, else any single `*.pem` pair in `~/.globus`) |
| `-pub`, `-priv` | the same two options under the names a PEM pair is usually described by |
| `-p12 PATH` | a PKCS#12 bundle (`.p12`/`.pfx`) holding the certificate and its key, instead of `-cert`/`-key`; default `$X509_USER_P12` |
| `-pwstdin` | read the key's or the bundle's pass phrase from standard input |
| `-out PATH` | where to write (default `$X509_USER_PROXY`, else `/tmp/x509up_u<uid>`) |
| `-certdir DIR` | CA directory that authenticates the VOMS server |
| `-rfc`, `-old`, `--profile P` | proxy profile (below); `-old` is a GT2 legacy proxy |

| `info` | |
| --- | --- |
| `-file PATH` | the proxy to describe (default as above) |
| `-verify` | check the proxy's signatures and each AC's signature, holder, issuer, LSC and signing policy (the exit code says whether it all holds) |
| `-certdir DIR` | with `-verify`: the CA directory to check the chain against |
| `-short` | the compact block only: no chain, no AC sections |
| `-all` | accepted as `voms-proxy-info` spells it; everything is shown by default |
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

### `xrd-proxy doctor`: what is wrong, and who can fix it

`doctor` (also spelled `check`) asks every question a proxy needs, in the order
the answers bite, and puts the thing to do under each one:

```console
$ xrd-proxy doctor -voms lhcb
ok  certs         138 CAs, 134 CRLs in /cvmfs/grid.cern.ch/etc/grid-security/certificates
ok  credential    /Users/me/.globus/usercert.pem with userkey.pem: identity /DC=ch/DC=cern/...,
                  until 03 Oct 2027
ok  voms          68 VOs configured, from /cvmfs/grid.cern.ch/etc/grid-security/vomses
ok  vo            lhcb: 2 server(s), voms-lhcb-auth.cern.ch:443, voms2-lhcb-auth.cern.ch:443
ok  vo:lsc        lhcb: present in /cvmfs/grid.cern.ch/etc/grid-security/vomsdir/lhcb
ok  vo:server     voms-lhcb-auth.cern.ch:443 answered and verified
??  vo:alt        voms2-lhcb-auth.cern.ch:443: [Errno 8] nodename nor servname provided
                  -> ask your experiment (lhcb): that server did not answer: ask if it has moved
??  proxy         no proxy at /tmp/x509up_u501
                  -> do: xrd-proxy init -voms lhcb
ok  location      a proxy can be written to /tmp/x509up_u501 (0600)
--  tokens        no bearer token: a proxy is how this machine authenticates
$ echo $?
0
```

Marks are `xrd-fs doctor`'s: `ok`, `!!` for something that stops a proxy,
`??` for something worth knowing, `--` for a question that did not apply, and
the exit code is `0` unless there is a `!!`.

**Every hint says who can act on it**, which is the point of the command:

| prefix | who | examples |
| --- | --- | --- |
| `do:` | you, in your own session | make a proxy, `chmod 600` a file, add your `.p12` |
| `ask your admin:` | whoever runs the machine or the network | a CA directory that is empty or missing, a firewall in front of the VOMS servers, a directory you cannot write |
| `ask your experiment (vo):` | your VO's computing support | membership, a missing LSC, a certificate renewal, a VOMS server that has moved |

What it looks at, in order: the CA directory (count, CRLs, and whether your
certificate's issuer is in it), the credential `init` would sign with (does it
load, is it in date, is it RSA, is the key its own), the `vomses` configuration,
the VO's servers and LSC, the proxy file already there, where a proxy would be
written (a planted symlink is refused), and any WLCG token in the environment.

It never prompts and never makes anything: an encrypted key is reported as
`??` with a note that `xrd-proxy init` will ask for its pass phrase once, so a
report you can paste into a ticket never stops for a password. `-offline`
skips the VOMS servers (a batch node, a locked-down network); `--json` gives
the same lines as data.

#### For site admins

| flag | what it adds |
| --- | --- |
| `--all-vos` | ask **every** configured VO's servers, in parallel, and name the ones nothing answered for (a laptop's resolver makes the first run slow; it is a network sweep) |
| `--strict` | a warning is a failure too: the exit code a monitoring check wants |
| `--summary` | a closing `12 ok, 1 warning(s), 0 problem(s)` line, and `"summary"` in `--json` |

and the lines an admin reads first are the ones a user skips: `host` (python,
platform, uid, `HOME`), the `certs:*`/`vomses:*` stage map above, `vo:cert`
(when the VO's server certificate itself expires — a server nobody renews
takes the whole VO down on that date), and `vos` from the sweep. With `--json`
the whole report is one document, so `xrd-proxy doctor --json --strict` is a
cron job:

```console
$ xrd-proxy doctor --json --strict --all-vos | jq -r '.records[] | select(.kind=="data") | .value.summary'
```


### Where everything is looked for: four stages

Every path the tools need — the CA directory, the VOMS LSC directory, the
`vomses` configuration, a user certificate and key, the proxy file, a bearer
token — is resolved in the same order, and `doctor` prints the whole list so
you can see it:

| stage | what it is | examples |
| --- | --- | --- |
| 1 `env` | a variable somebody set, **used as named** (a typo is an error, not something to paper over) | `$X509_CERT_DIR`, `$X509_VOMS_DIR`, `$X509_VOMSES`, `$X509_USER_CERT`/`$X509_USER_KEY`, `$X509_USER_PROXY`, `$BEARER_TOKEN_FILE` |
| 2 `local` | the machine's own places | `/etc/grid-security/<kind>`, Homebrew's prefix on a Mac, `~/.globus/usercert.pem`+`userkey.pem`, `$TMPDIR`/`/tmp` `x509up_u<uid>` |
| 3 `cvmfs` | the WLCG bundle a grid machine already mounts | `/cvmfs/grid.cern.ch/etc/grid-security/<kind>`, then `/cvmfs/cvmfs-config.cern.ch/etc/grid-security/<kind>` |
| 4 `cwd` | the working directory, for a rig, a tarball or a checkout that carries its own | `./grid-security/<kind>`, `./usercert.pem`+`./userkey.pem`, `./x509up_u<uid>` |

This is `xrdsec.locations`, and it is a promise: stages 1 and 2 are exactly the
list every tool used before, in the same order, so nothing a machine found
stops being found — CVMFS and the working directory only ever add somewhere to
fall back to. A `vomses` *directory* works as well as a file (`$X509_VOMSES`
may name either), which is how the CVMFS bundle's 126 per-VO files are read.

```console
$ xrd-proxy doctor -offline | grep certs
--  certs:local   /etc/grid-security/certificates (not there)
--  certs:local2  /opt/homebrew/etc/grid-security/certificates (not there)
--  certs:local3  /usr/local/etc/grid-security/certificates (not there)
ok  certs:cvmfs   /cvmfs/grid.cern.ch/etc/grid-security/certificates (138 entries)
--  certs:cvmfs2  /cvmfs/cvmfs-config.cern.ch/etc/grid-security/certificates (not there)
--  certs:cwd     /Users/me/work/grid-security/certificates (not there)
ok  certs         138 CAs, 134 CRLs in /cvmfs/grid.cern.ch/etc/grid-security/certificates
```

### Where the credential comes from

`xrd-proxy init` takes the first of these it finds, and no other:

1. `-p12 bundle.p12` - one PKCS#12 file ([below](#a-pkcs12-bundle-instead-of-globus));
2. `-cert`/`-key` (or `-pub`/`-priv`) - a PEM certificate and key you name. Naming
   files always wins over the environment, so a run that says which credential
   to use uses it:
   ```console
   $ xrd-proxy init -pub ~/.globus/cert.pem -priv ~/.globus/key.pem -valid 12:00
   ```
3. `$X509_USER_P12`, when the bundle it names is there. A stale setting is not a
   refusal: it says so (`note: X509_USER_P12 names … which is not there`) and
   carries on looking;
4. `$X509_USER_CERT` with `$X509_USER_KEY`;
5. `~/.globus/usercert.pem` with `userkey.pem` - the traditional pair;
6. any single `*.pem` pair in `~/.globus`: a certificate and key downloaded
   under names of their own (`cert.pem` with `key.pem`, `mycert.pem` with
   `mykey.pem`), or one file holding both. A file is read as a certificate or a
   key by its PEM header, not by its name. **Two** candidate pairs are never
   guessed between - the error says how many it found and asks you to name the
   pair with `-cert` and `-key`.

### Verifying a proxy: is it signed by the VO?

`info` on its own *decodes* an AC and says so (`status : decoded (describing
does not check trust …)`). `-verify` is the other half, and the exit code is
the answer:

```console
$ xrd-proxy info -verify
...
status    : ok
signature : ok  signed by /DC=ch/DC=cern/OU=Organic Units/OU=Users/CN=jane/CN=123456
issuer    : ok  /DC=ch/DC=cern/CN=CERN Grid Certification Authority is in /cvmfs/…/certificates
vo        : verified  lhcb (LSC and signing policy in /cvmfs/…/vomsdir)
$ echo $?
0
```

What it checks, and what it refuses:

* **every link of the chain is signed by the one above it** (`xrdsec.x509.signed_by`,
  the same raw PKCS#1/PSS/ECDSA verification the GSI code uses) — so the proxy
  is signed by *your* certificate, not merely filed beside it;
* **the outermost link's issuer is a file in the CA directory**;
* **each AC**: its signature by the VO's signer, that the signer chains to a
  CA in the directory, that the holder matches the proxy, and that the VO's
  **LSC** and **signing policy** (`CA:sign`, `cond_subjects`) permit it —
  `xrdsec.voms.validate_voms`. A failure names the file it looked for:

```console
$ X509_VOMS_DIR=/wrong xrd-proxy info -verify -q; echo $?
1
$ X509_VOMS_DIR=/wrong xrd-proxy info -verify | grep -E "vo  |diagnostic"
diagnostic: No .lsc trust file was found for VO 'lhcb' in '/wrong/lhcb'. Install the VO's official .lsc file or correct X509_VOMS_DIR.
vo        : !! lhcb: lsc
```

Nothing is written and no server is contacted: it is a check of what is in
the file against the trust files on disk, so it is the line to put in a script
that must not run a job on an unproven proxy.

### Choosing a VO

Which VOs you can ask depends on the `vomses` configuration this machine has —
one file per VO under `~/.voms/vomses`, `/etc/vomses`, or a directory such as
`/cvmfs/grid.cern.ch/etc/grid-security/vomses` named by `$X509_VOMSES`.
`xrd-proxy init -list-vos` prints what is configured here, so picking one is
not guesswork:

```console
$ xrd-proxy init -list-vos
atlas                      atlas         voms-atlas-auth.cern.ch:443         LSC: yes
cms                        cms           voms-cms-auth.cern.ch:443           LSC: yes
lhcb                       lhcb          voms-lhcb-auth.cern.ch:443          LSC: yes
vo.northgrid.ac.uk         northgrid     voms.gridpp.ac.uk:15018             LSC: no
```

The columns are the VO (what `-voms` takes), its aliases, its servers, and
whether the VO's LSC is installed — an AC for a VO with no LSC can be fetched
and carried, but no site can validate it. `-list-vos --json` gives the same as
the [report](output.md) document.

`-voms` takes the VO or any of its aliases, and can be repeated to ask for
several VOs, or several FQANs of one. Only the VO's own `vomses` files are
read for it, however many VOs the directory holds:

```console
$ xrd-proxy init -voms atlas:/atlas/Role=pilot -voms cms -valid 8:00
```

A name that is not configured is refused with the ones that are, and with the
nearest match when the complaint is a typo:

```console
$ xrd-proxy init -voms atls
xrd-proxy: no VOMS server is configured for 'atls' among the configured servers
- did you mean 'atlas'?; configured: alice, atlas, cms, dune, lhcb, ...
```

### A PKCS#12 bundle instead of `~/.globus`

A certificate portal usually hands out one PKCS#12 file (`.p12`, `.pfx`)
holding the certificate *and* its private key. `xrd-proxy init` takes one
directly, so there is no `usercert.pem`/`userkey.pem` pair to lay out and no
second file to keep track of:

```console
$ xrd-proxy init -p12 ~/certs/myCert.p12 -valid 12:00
Enter PEM pass phrase:
Created proxy in /tmp/x509up_u1000.

Your proxy is valid until Sat Nov  8 23:14:05 GMT 2026
```

`$X509_USER_P12` names the bundle for a session, so `xrd-proxy init` on its
own finds it (`-cert`/`-key` and `-p12` all win over the variable). The bundle
is held to the same rules as any other private key — a regular file, ours, no
group or other permission bits (`chmod 600 myCert.p12`) — and its pass phrase
is asked for only when it is encrypted, once, through the same prompter an
encrypted PEM key uses.

From Python it is `xrdsec.proxies.load_p12`, which returns the same
`ProxyCredential` `load_credential` does, so it can be handed to `create_proxy`
or used as a delegation signer unchanged:

```python
from xrdclient.crypto.proxies import load_p12

user = load_p12("~/certs/myCert.p12", password=ask)  # or credential_from_p12(bytes)
proxy = create_proxy(user, lifetime=12 * 3600)
```

What the bundle carries beyond the user certificate (issuing CAs, say) is
kept after it, in the bundle's own order, which is the chain GSI puts on the
wire.

## From Python

```python
from xrdclient.crypto.proxies import create_proxy, describe_proxy, load_credential, write_proxy
from xrdclient.voms_client import fetch_ac, find_vomses, servers_for

user = load_credential("~/.globus/usercert.pem", "~/.globus/userkey.pem", password=ask)
ac = fetch_ac(servers_for("atlas", find_vomses()), user, fqans=["/atlas/Role=production"])
proxy = create_proxy(user, voms_acs=[ac])  # in memory: certificate, fresh key, chain
write_proxy("/tmp/x509up_u1000", proxy)  # atomic, 0600, links refused
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

## Two names: `xrd-auth` and `xrd-proxy`

The command is installed as both. `xrd-auth` is the name that matches what it has
become - credentials of every kind, and the doctor that explains them:

```sh
xrd-auth --version          # the same program
xrd-auth doctor --summary   # every check, one line each
```

Either name takes the same subcommands (`init`, `info`, `destroy`, `doctor`,
`token`), and the usage line shows whichever you typed.

## Getting a WLCG bearer token (a SCITOKEN)

An issuer will not hand a token to an unnamed client. Some IAMs let a public
client register itself (RFC 7591) and some do not, so `xrd-auth` can ask:

```sh
xrd-auth token providers                     # the issuers it knows, and their scopes
xrd-auth token register --provider wlcg      # register this tool, keep the client id
xrd-auth token init --provider wlcg --flow device
```

`token register` writes the client id to `~/.config/xrdsec/token-clients.json`
(owner-only; a public client holds no secret) and later runs pick it up, so the id
never has to be typed or exported. The WLCG reference IAM allows this with no
approval; **LHCb's IAM does not**, and says so in its own words:

```
$ xrd-auth token register --provider lhcb
xrd-auth: https://lhcb-auth.cern.ch/iam/api/client-registration refused the request (403):
          You do not have enough privileges to access this client registration API
```

If the issuer you need refuses, register a client there by hand (`--client-id`), or
use an issuer that allows it - `--provider wlcg` advertises `storage.read:/`,
`storage.modify:/`, `storage.stage:/` and `wlcg.groups`, and CERN's HTCondor-CEs
accept tokens from WLCG issuers.

The device flow then prints a URL and a code to approve in a browser:

The device flow prints a URL and a code to approve in a browser; it is the flow
with no certificate. `--client-id` must be a public client registered with that
issuer (or set `WLCG_CLIENT_ID`). `xrd-auth token init --from-resource
https://eosuser.cern.ch` finds the issuer from a storage element instead of naming
it.

The other flows, when a browser is not the right tool:

| flow | command | what it needs |
| --- | --- | --- |
| `x509` | `xrd-auth token init --provider lhcb --flow x509 --p12 cert.p12 --pwstdin` | your certificate as the OAuth client; a client id registered at the issuer |
| `exchange` | `xrd-auth token init --flow exchange --from-token "$TOKEN" --scope storage.read:/` | a token you already hold, narrowed |
| `dirac` | `xrd-auth token init --provider lhcb --flow dirac --proxy ~/.x509up` | DIRAC installed: it asks DIRAC for the token and embeds it |

Where it goes: `--out PATH`, else `$BEARER_TOKEN_FILE`, else `bt_u<uid>` in the
current directory - written `0600`, and never through a symlink.

Reading and removing it:

```sh
xrd-auth token info                          # who it is for, its scope, expiry
xrd-auth token info ~/.x509up                # a token embedded in a DIRAC proxy
xrd-auth token destroy                       # remove it
```

For an HTCondor client use `SCITOKEN` instead of GSI, pointing it at the file:

```sh
mkdir -p ~/.condor/tokens.d && cp bt_u$(id -u) ~/.condor/tokens.d/lhcb
_CONDOR_SEC_CLIENT_AUTHENTICATION_METHODS=SCITOKEN condor_status -pool condorce01.cern.ch
```

Environment beats profiles, and flags beat both: `WLCG_TOKEN_ISSUER`,
`WLCG_TOKEN_AUDIENCE`, `WLCG_TOKEN_SCOPE`, `WLCG_CLIENT_ID` (or
`WLCG_TOKEN_CLIENT_ID`).
