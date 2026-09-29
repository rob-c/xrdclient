"""A real GSI ``xrootd``, and the grid PKI it needs, made with ``openssl``.

Nothing here is written by this package's own encoder: the CA, the host
certificate, the user certificate and the RFC 3820 proxy all come out of the
``openssl`` command line, the way a site's would, so a test that logs in with
them is checked against material this client did not make.

The daemon is started with ``-dlgpxy`` and ``-exppxy``, so a proxy delegated to
it is written to disk where a test can pick it up and hand it to ``openssl
verify``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from _xrootd import XROOTD, RealServer

OPENSSL = shutil.which("openssl")

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

HOST_EXT = """\
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth, clientAuth
subjectAltName = DNS:localhost, IP:127.0.0.1
"""

USER_EXT = "basicConstraints = critical, CA:FALSE\nkeyUsage = critical, digitalSignature\n"

USER_DN = "/O=example/OU=people/CN=Jane Doe"
PROXY_DN = USER_DN + "/CN=proxy"


def plugin_dir() -> str | None:
    """Where this machine keeps ``libXrdSecgsi``, if anywhere."""
    for candidate in ("/usr/local/lib", "/opt/homebrew/lib", "/usr/lib64", "/usr/lib"):
        if list(Path(candidate).glob("libXrdSecgsi*")):
            return candidate
    return None


def available() -> bool:
    """A GSI server can be stood up here: the daemon, its plugin, and ``openssl``."""
    return bool(XROOTD and OPENSSL and plugin_dir())


def _openssl(*argv: str) -> str:
    done = subprocess.run([str(OPENSSL), *argv], capture_output=True, text=True, check=False)
    if done.returncode:
        raise RuntimeError(f"openssl {' '.join(argv)} failed:\n{done.stderr}")
    return done.stdout


class PKI:
    """A CA, a host certificate for ``localhost``, and a proxy for Jane Doe."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.ca_dir = root / "ca"
        self.ca_dir.mkdir(parents=True)
        self.empty_ca_dir = root / "no-ca"
        self.empty_ca_dir.mkdir()
        (root / "proxy.ext").write_text(PROXY_EXT)
        (root / "host.ext").write_text(HOST_EXT)
        (root / "user.ext").write_text(USER_EXT)
        for who in ("ca", "host", "user", "proxy"):
            _openssl("genrsa", "-out", str(root / f"{who}.key"), "2048")
        _openssl(
            "req", "-new", "-x509", "-key", str(root / "ca.key"), "-out", str(root / "ca.pem"),
            "-days", "2", "-subj", "/O=example/CN=Demo CA",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        )  # fmt: skip
        self._issue("host", "/O=example/CN=localhost", "ca", "02", "host.ext")
        self._issue("user", USER_DN, "ca", "03", "user.ext")
        self._issue("proxy", PROXY_DN, "user", "04", "proxy.ext", extensions="proxy")
        digest = _openssl("x509", "-hash", "-noout", "-in", str(root / "ca.pem")).strip()
        shutil.copy(root / "ca.pem", self.ca_dir / f"{digest}.0")
        (self.ca_dir / f"{digest}.signing_policy").write_text(
            'access_id_CA X509 "/O=example/CN=Demo CA"\n'
            "pos_rights globus CA:sign\n"
            'cond_subjects globus "/O=example/*"\n'
        )
        self.proxy = root / "x509up"
        self.proxy.write_bytes(
            (root / "proxy.pem").read_bytes()
            + (root / "proxy.key").read_bytes()
            + (root / "user.pem").read_bytes()
        )
        self.proxy.chmod(0o600)
        #: What ``openssl verify -untrusted`` needs between a delegated proxy and the CA.
        self.untrusted = root / "untrusted.pem"
        self.untrusted.write_bytes(
            (root / "proxy.pem").read_bytes() + (root / "user.pem").read_bytes()
        )

    def _issue(
        self, who: str, subject: str, issuer: str, serial: str, ext: str, *, extensions: str = ""
    ) -> None:
        root = self.root
        _openssl(
            "req", "-new", "-key", str(root / f"{who}.key"), "-out", str(root / f"{who}.csr"),
            "-subj", subject,
        )  # fmt: skip
        argv = [
            "x509", "-req", "-in", str(root / f"{who}.csr"),
            "-CA", str(root / f"{issuer}.pem"), "-CAkey", str(root / f"{issuer}.key"),
            "-set_serial", serial, "-days", "1", "-out", str(root / f"{who}.pem"),
            "-extfile", str(root / ext),
        ]  # fmt: skip
        if extensions:
            argv += ["-extensions", extensions]
        _openssl(*argv)

    def verify(self, leaf_pem: Path) -> subprocess.CompletedProcess[str]:
        """``openssl verify`` of one delegated proxy, through Jane's proxy, to the CA."""
        return subprocess.run(
            [
                str(OPENSSL), "verify", "-allow_proxy_certs",
                "-CAfile", str(self.root / "ca.pem"),
                "-untrusted", str(self.untrusted),
                str(leaf_pem),
            ],
            capture_output=True,
            text=True,
            check=False,
        )  # fmt: skip


class GSIXrootd(RealServer):
    """A real ``xrootd`` that accepts ``gsi`` and nothing else, and keeps what is delegated."""

    def __init__(self, root: Path, pki: PKI, *, dlgpxy: int = 1) -> None:
        super().__init__(root)
        self.pki = pki
        self.dlgpxy = dlgpxy
        self.delegated_dir = pki.root / f"delegated-{self.port}"
        self.delegated_dir.mkdir()

    @property
    def url(self) -> str:
        # ``localhost`` is the name in the host certificate the stock client
        # checks, so both clients see the same thing.
        return f"root://localhost:{self.port}/"

    def start(self) -> GSIXrootd:
        pki = self.pki.root
        self._config.write_text(
            f"xrd.port {self.port}\nall.export {self.root}\n"
            f"all.adminpath {self._admin}\nall.pidpath {self._admin}\n"
            "xrootd.seclib libXrdSec.so\n"
            f"sec.protocol {plugin_dir()} gsi -certdir:{self.pki.ca_dir} "
            f"-cert:{pki / 'host.pem'} -key:{pki / 'host.key'} -crl:0 -gmapopt:0 "
            f"-vomsat:0 -moninfo:0 -dlgpxy:{self.dlgpxy} "
            f"-exppxy:{self.delegated_dir}/dlg_<user> -d:2\n"
            "sec.protbind * only gsi\n"
        )
        with self._log.open("wb") as handle:
            self._proc = subprocess.Popen(
                [str(XROOTD), "-c", str(self._config), "-n", "gsi"],
                cwd=str(self._admin),
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
        self._wait()
        return self

    def delegated(self) -> list[Path]:
        """Proxy files the server wrote for delegations it received."""
        return sorted(self.delegated_dir.glob("dlg_*"))

    def forget(self) -> None:
        """Remove what earlier logins left, so the next test sees only its own."""
        for path in self.delegated():
            path.unlink()
