#!/usr/bin/env python3
"""How fast does ``xrdclient`` pull a file off a GSI-authenticated server?

Stands up a real ``xrootd`` in a container, authenticates to it with an X.509
proxy this script mints, and times a download - against ``xrdcp`` too, where
the stock client is installed, because a number on its own says nothing.

    $ python examples/gsi_copy_benchmark.py                 # 512 MiB, 3 runs
    $ python examples/gsi_copy_benchmark.py --size 2048 --repeat 7
    $ python examples/gsi_copy_benchmark.py --keep          # leave the server up
    $ python examples/gsi_copy_benchmark.py --local         # xrootd here, no Docker
    $ python examples/gsi_copy_benchmark.py --brix ~/brix/bin/brix-xrdcp

Nothing here is installed or left behind: the certificates, the export and the
container all live in one temporary directory that is removed on the way out.

Whatever other clients are on this machine are timed against it on the same
file: the official ``xrdcp`` and its Python bindings, and a BriX client if one
is named. The table in the README came from this program.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import xrdclient

#: What the server is told to be. ``xrd.port`` and the rest are the daemon's
#: own directives; the GSI block is the whole point of the exercise.
CONFIG = """\
xrd.port {port}
all.export /data
all.adminpath /var/run/xrootd
all.pidpath /var/run/xrootd
xrootd.seclib libXrdSec.so
xrootd.chksum max 2 adler32 crc32
ofs.persist off
# ``-gmapopt:0`` is what keeps this to one moving part: the DN in the proxy
# is the identity, with no grid-mapfile to write and keep in step. ``-crl:0``
# says the same about revocation lists for a CA that has existed for a minute.
sec.protocol {libdir} gsi -certdir:/certs/ca -cert:/certs/host.pem \
-key:/certs/host.key -crl:0 -gmapopt:0 -vomsat:0 -moninfo:0 -dlgpxy:0
sec.protbind * only gsi
"""

DOCKERFILE = """\
FROM almalinux:9
RUN dnf -y install epel-release \
 && dnf -y install xrootd-server xrootd-client openssl \
 && dnf clean all \
 && mkdir -p /var/run/xrootd /data
CMD ["xrootd", "-c", "/etc/xrootd/demo.cfg"]
"""

#: OpenSSL needs telling that a proxy certificate is allowed to exist.
PROXY_EXT = """\
[ proxy ]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
1.3.6.1.5.5.7.1.14 = critical, ASN1:SEQUENCE:proxy_info

[ proxy_info ]
policy = SEQUENCE:proxy_policy

[ proxy_policy ]
language = OID:1.3.6.1.5.5.7.21.1
"""

CA_EXT = "basicConstraints = critical, CA:TRUE\nkeyUsage = critical, keyCertSign, cRLSign\n"
HOST_EXT_TEMPLATE = """\
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth, clientAuth
subjectAltName = DNS:{host}, DNS:localhost, IP:127.0.0.1
"""


def run(*argv: str, quiet: bool = True, **kwargs: object) -> subprocess.CompletedProcess:
    """One command, with its output kept unless it fails."""
    done = subprocess.run(  # the check is two lines below
        argv, capture_output=quiet, text=True, **kwargs  # type: ignore[call-overload]
    )
    if done.returncode != 0:
        if quiet:
            sys.stderr.write(done.stdout or "")
            sys.stderr.write(done.stderr or "")
        raise SystemExit(f"{argv[0]} failed: {' '.join(argv)}")
    return done


def openssl(*argv: str) -> None:
    run("openssl", *argv)


def build_pki(root: Path, host: str) -> Path:
    """A CA, a host certificate and an RFC 3820 proxy, the way a grid has them.

    Returns the directory holding them. The CA lands in its own directory
    under the hash name OpenSSL looks it up by, which is what both this client
    and the server mean by a "certificate directory".
    """
    certs = root / "certs"
    ca_dir = certs / "ca"
    ca_dir.mkdir(parents=True)
    (certs / "ca.ext").write_text(CA_EXT)
    (certs / "host.ext").write_text(HOST_EXT_TEMPLATE.format(host=host))
    (certs / "proxy.ext").write_text(PROXY_EXT)

    def key(name: str) -> None:
        openssl("genrsa", "-out", str(certs / f"{name}.key"), "2048")

    def request(name: str, subject: str) -> None:
        openssl("req", "-new", "-key", str(certs / f"{name}.key"),
                "-out", str(certs / f"{name}.csr"), "-subj", subject)

    def sign(name: str, issuer: str, serial: str, ext: str | None) -> None:
        argv = ["x509", "-req", "-in", str(certs / f"{name}.csr"),
                "-CA", str(certs / f"{issuer}.pem"), "-CAkey", str(certs / f"{issuer}.key"),
                "-set_serial", serial, "-days", "1", "-out", str(certs / f"{name}.pem")]
        if ext:
            argv += ["-extfile", str(certs / ext)]
            if ext == "proxy.ext":
                argv += ["-extensions", "proxy"]
        openssl(*argv)

    # the CA, self-signed, and trusted by both ends
    key("ca")
    openssl("req", "-new", "-x509", "-key", str(certs / "ca.key"), "-out", str(certs / "ca.pem"),
            "-days", "1", "-subj", "/O=example/CN=Demo CA", "-extensions", "v3_ca",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign")

    # what the server presents, and what we check it against
    key("host")
    request("host", f"/O=example/CN={host}")
    sign("host", "ca", "02", "host.ext")

    # who we are, and the short-lived proxy that speaks for us
    key("user")
    request("user", "/O=example/OU=people/CN=Jane Doe")
    sign("user", "ca", "03", None)
    key("proxy")
    request("proxy", "/O=example/OU=people/CN=Jane Doe/CN=proxy")
    sign("proxy", "user", "04", "proxy.ext")

    # $X509_USER_PROXY is one file: the proxy, its key, and the certificate
    # that signed it. The CA is deliberately *not* in it - that is the
    # convention grid-proxy-init and voms-proxy-init write, and the server
    # looks its own trust anchor up in its certificate directory.
    proxy = certs / "proxy.pem.full"
    proxy.write_bytes(
        (certs / "proxy.pem").read_bytes()
        + (certs / "proxy.key").read_bytes()
        + (certs / "user.pem").read_bytes()
    )
    proxy.chmod(0o600)

    # the CA under the name OpenSSL hashes it to, plus the policy file the
    # stock GSI implementation insists on finding beside it
    digest = run("openssl", "x509", "-hash", "-noout", "-in", str(certs / "ca.pem")).stdout.strip()
    shutil.copy(certs / "ca.pem", ca_dir / f"{digest}.0")
    (ca_dir / f"{digest}.signing_policy").write_text(
        'access_id_CA X509 "/O=example/CN=Demo CA"\n'
        "pos_rights globus CA:sign\n"
        'cond_subjects globus "/O=example/*"\n'
    )
    return certs


def _plugin_dir() -> str:
    """Where this machine keeps ``libXrdSecgsi``, which is not where RPMs do.

    The daemon is told a directory and appends the plugin's own name, so what
    matters is finding the directory that has one - Homebrew's ``/usr/local/lib``
    holds ``libXrdSecgsi-6.so``, a distribution's ``/usr/lib64`` holds it
    unversioned.
    """
    for candidate in ("/usr/local/lib", "/opt/homebrew/lib", "/usr/lib64", "/usr/lib"):
        if list(Path(candidate).glob("libXrdSecgsi*")):
            return candidate
    raise SystemExit("no libXrdSecgsi on this machine; use Docker (drop --local)")


def payload(export: Path, size_mib: int) -> str:
    """A file of random bytes, big enough that the transfer is the measurement."""
    export.mkdir(parents=True, exist_ok=True)
    target = export / f"payload-{size_mib}.bin"
    if target.exists() and target.stat().st_size == size_mib << 20:
        return target.name
    block = os.urandom(1 << 20)
    with target.open("wb") as handle:
        for _ in range(size_mib):
            handle.write(block)
    return target.name


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def server(root: Path, certs: Path, export: Path, port: int, *, local: bool, keep: bool,
           image: str | None = None):
    """The daemon, in a container or on this machine, and its URL."""
    config = root / "demo.cfg"
    if local:
        libdir = _plugin_dir()
        config.write_text(
            CONFIG.format(port=port, libdir=libdir)
            .replace("/certs/", f"{certs}/")
            .replace("all.export /data", f"all.export {export}")
            .replace("/var/run/xrootd", str(root / "admin"))
        )
        (root / "admin").mkdir(exist_ok=True)
        log = (root / "server.log").open("wb")
        process = subprocess.Popen(
            [shutil.which("xrootd") or "xrootd", "-c", str(config)], stdout=log, stderr=log
        )
        try:
            yield f"root://127.0.0.1:{port}/", root / "server.log"
        finally:
            if not keep:
                process.terminate()
                process.wait(timeout=10)
            log.close()
        return

    config.write_text(CONFIG.format(port=port, libdir="/usr/lib64"))
    if image is None:
        # a build context of its own: the export lives under ``root`` too, and
        # a few hundred megabytes of payload has no business being sent to the
        # daemon along with a five-line Dockerfile
        context = root / "image"
        context.mkdir(exist_ok=True)
        (context / "Dockerfile").write_text(DOCKERFILE)
        print("no local image has xrootd; building one (several minutes) ...", flush=True)
        run("docker", "build", "-q", "-t", "xrdclient-gsi-demo", str(context))
        image = "xrdclient-gsi-demo"
    print(f"serving {export} from {image} on port {port}", flush=True)
    name = f"xrdclient-gsi-{port}"
    run("docker", "rm", "-f", name, quiet=True) if _exists(name) else None
    run(
        "docker", "run", "-d", "--name", name,
        "-p", f"{port}:{port}",
        "-v", f"{config}:/etc/xrootd/demo.cfg:ro",
        "-v", f"{certs}:/certs:ro",
        "-v", f"{export}:/data",
        "--entrypoint", "xrootd",
        image,
        # no ``-l``: the daemon locks a file beside whatever that names, and
        # ``/dev/.lock`` is not writable. Unlogged, it writes to stdout, which
        # is where ``docker logs`` reads from anyway.
        "-c", "/etc/xrootd/demo.cfg",
    )
    try:
        yield f"root://127.0.0.1:{port}/", name
    finally:
        if keep:
            print(f"\nthe server is still up: docker logs -f {name}; docker rm -f {name}")
        else:
            run("docker", "rm", "-f", name)


def pick_image(preferred: str | None) -> str | None:
    """A local image that already carries ``xrootd`` and the GSI plugin.

    Building one from a distribution image costs several minutes of `dnf`, and
    a machine that has been benchmarking XRootD usually has one already. The
    named image wins if it was given; otherwise the first candidate that has
    both the daemon and ``libXrdSecgsi`` in it does. ``None`` means "build".
    """
    listed = subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                            capture_output=True, text=True, check=False).stdout.split()
    candidates = [preferred] if preferred else [
        name for name in listed
        if any(word in name for word in ("xrootd", "xrdclient-gsi-demo", "eos"))
    ]
    for name in candidates:
        if name not in listed and preferred is None:
            continue
        probe = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "sh", name, "-c",
             "command -v xrootd >/dev/null && ls /usr/lib64/libXrdSecgsi* >/dev/null 2>&1"],
            capture_output=True, text=True, check=False,
        )
        if probe.returncode == 0:
            return name
    return None


def _exists(name: str) -> bool:
    done = subprocess.run(
        ["docker", "ps", "-aq", "-f", f"name=^{name}$"],
        capture_output=True, text=True, check=False,
    )
    return bool(done.stdout.strip())


def wait_ready(url: str, config: xrdclient.Config, where: object, seconds: float = 90.0) -> None:
    """Poll until the daemon answers, or say what it said instead."""
    deadline = time.monotonic() + seconds
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            xrdclient.stat(url + "/data", config=config)
            return
        except FileNotFoundError:
            return  # it answered, which is all this is asking
        except Exception as exc:  # whatever it is, the server may still be starting
            last = exc
            time.sleep(1.0)
    if isinstance(where, Path) and where.exists():
        sys.stderr.write(where.read_text()[-4000:])
    elif isinstance(where, str):
        logs = subprocess.run(
            ["docker", "logs", "--tail", "40", where],
            capture_output=True, text=True, check=False,
        )
        sys.stderr.write(logs.stdout)
    raise SystemExit(f"the server never came up: {last}")


def others(brix: str) -> list[tuple[str, tuple[str, ...]]]:
    """The other command-line clients on this machine, in layers of language.

    Process start-up is inside these numbers and outside the in-process ones
    above them; for a C program against a file this size that is a few
    milliseconds against a few seconds, and saying so is cheaper than pretending
    the harness is perfect.
    """
    found = []
    official = shutil.which("xrdcp")
    if official:
        found.append((f"xrdcp, official C++ ({_version(official)})", (official,)))
    if brix and Path(brix).exists():
        found.append((f"brix-xrdcp, BriX pure C ({_version(brix)})", (brix,)))
    return found


def _version(binary: str) -> str:
    """Whatever the thing says it is, in one short line."""
    for flag in ("--version", "-v"):
        done = subprocess.run([binary, flag], capture_output=True, text=True, check=False)
        text = (done.stdout + done.stderr).strip().splitlines()
        if text:
            return text[0].split()[-1][:16]
    return "?"


def bindings(remote: str, target: Path, env: dict, repeat: int):
    """The official Python bindings, if this machine has them.

    They are not importable from the virtual environment this package is
    developed in, so they are timed where they live - inside their own
    interpreter, around the copy alone, so the comparison is the transfer and
    not the start-up.
    """
    script = f"""
import time
from XRootD import client
process = client.CopyProcess()
process.add_job({remote!r}, {str(target)!r}, force=True)
process.prepare()
start = time.perf_counter()
status = process.run()[0]
print("ELAPSED", time.perf_counter() - start, bool(status.ok))
"""
    for _ in range(repeat):
        # the bindings are a system package, not one of this project's
        done = subprocess.run(["python3", "-c", script],
                              capture_output=True, text=True, check=False, env=env)
        line = [l for l in done.stdout.splitlines() if l.startswith("ELAPSED")]
        if not line:
            return  # no bindings here, or they refused; the others still stand
        _, seconds, ok = line[0].split()
        if ok != "True":
            return
        target.unlink(missing_ok=True)
        yield float(seconds)


def timed(what: str, size: int, fn) -> float:
    """Run it, and say how fast it moved the bytes."""
    start = time.perf_counter()
    fn()
    elapsed = time.perf_counter() - start
    print(f"  {what:<34} {elapsed:6.2f} s   {size / elapsed / (1 << 20):7.1f} MiB/s", flush=True)
    return elapsed


def arguments(argv: list[str] | None) -> argparse.Namespace:
    """What to measure, how often, and against what."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--size", type=int, default=512, help="file size in MiB (default 512)")
    parser.add_argument("--repeat", type=int, default=3, help="timed runs of each case")
    parser.add_argument("--port", type=int, default=0, help="port to serve on (default: free one)")
    parser.add_argument("--keep", action="store_true", help="leave the server running")
    parser.add_argument("--local", action="store_true", help="run xrootd here, not in Docker")
    parser.add_argument("--image", help="image to serve from (default: any local one with xrootd)")
    parser.add_argument("--brix", default=shutil.which("brix-xrdcp") or "",
                        help="path to a BriX client to compare against, if it is not on PATH")
    return parser.parse_args(argv)


def median_of(label: str, size: int, repeat: int, once) -> None:
    """Run one client ``repeat`` times and say what the middle run managed."""
    rates = []
    for _ in range(repeat):
        elapsed = timed(label, size, once)
        rates.append(size / elapsed / (1 << 20))
    print(f"  {'-> median':<34} {'':6}     {statistics.median(rates):7.1f} MiB/s\n")


def measure(remote: str, size: int, settings, root: Path, env: dict, args) -> None:
    """Every client this machine has, over the same file, in the same conditions."""
    for label, config in (
        ("xrdclient, bulk data plane", settings),
        ("xrdclient, one connection", settings.evolve(bulk=False)),
    ):
        target = root / "downloaded.bin"

        def once(c=config, d=target) -> None:
            xrdclient.copy(remote, str(d), config=c, verify=False, overwrite=True)
            d.unlink(missing_ok=True)

        median_of(label, size, args.repeat, once)

    for label, command in others(args.brix):
        target = root / "other.bin"

        def once(a=command, d=target) -> None:
            run(*a, "-f", remote, str(d), env=env)
            d.unlink(missing_ok=True)

        median_of(label, size, args.repeat, once)

    rates = []
    for elapsed in bindings(remote, root / "bindings.bin", env, args.repeat):
        print(f"  {'XRootD python bindings':<34} {elapsed:6.2f} s   "
              f"{size / elapsed / (1 << 20):7.1f} MiB/s", flush=True)
        rates.append(size / elapsed / (1 << 20))
    if rates:
        print(f"  {'-> median':<34} {'':6}     {statistics.median(rates):7.1f} MiB/s")


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    if not args.local and not shutil.which("docker"):
        raise SystemExit("docker is not on PATH; --local runs the daemon here instead")

    port = args.port or free_port()
    root = Path(tempfile.mkdtemp(prefix="xrdclient-gsi-"))
    export = root / "export"
    try:
        certs = build_pki(root, socket.gethostname())
        name = payload(export, args.size)
        size = (export / name).stat().st_size
        settings = xrdclient.Config(
            proxy=str(certs / "proxy.pem.full"),
            ca_path=str(certs / "ca"),
            auth_order=("gsi",),
            require_tls=False,
            verify_tls=False,  # the host certificate is this script's own
        )
        image = None if args.local else pick_image(args.image)
        with server(root, certs, export, port, local=args.local, keep=args.keep,
                    image=image) as (url, where):
            wait_ready(url, settings, where)
            remote = f"{url}{export}/{name}" if args.local else f"{url}/data/{name}"
            print(f"\n{args.size} MiB over gsi+root://, {args.repeat} runs each\n")
            env = {**os.environ, "X509_USER_PROXY": str(certs / "proxy.pem.full"),
                   "X509_CERT_DIR": str(certs / "ca")}
            measure(remote, size, settings, root, env, args)
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
