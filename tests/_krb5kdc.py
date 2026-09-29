"""A throwaway MIT Kerberos realm, and an ``xrootd`` that authenticates against it.

The Kerberos client in :mod:`xrdclient.auth.kerberos` is only as good as the
proof that the genuine article accepts what it builds. So where MIT krb5 is
installed (Homebrew puts it in ``/usr/local/opt/krb5``; Linux distributions
in ``/usr/sbin`` and ``/usr/bin``), :class:`MitRealm` creates a realm in a
temporary directory - database, stash, a KDC on a free loopback port - with
a user and an ``xrootd/<host>`` service whose passwords are known, so a test
can derive their keys as well as read them from the keytabs MIT wrote.
:class:`KerberizedXrootd` then starts a real ``xrootd`` that accepts ``krb5``
and nothing else, with that keytab.

Nothing here needs privileges, and nothing touches the system's own
Kerberos configuration: every MIT program is pointed at the realm through
``KRB5_CONFIG``, ``KRB5_KDC_PROFILE`` and ``KRB5CCNAME``.
"""

from __future__ import annotations

import os
import shutil
import socket
import struct
import subprocess
import time
from pathlib import Path

from _xrootd import XROOTD, RealServer


def _free_port() -> int:
    """A loopback port free for both TCP and UDP at the moment of asking."""
    while True:
        with socket.create_server(("127.0.0.1", 0)) as tcp:
            port = int(tcp.getsockname()[1])
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                try:
                    udp.bind(("127.0.0.1", port))
                except OSError:
                    continue
                return port


_PREFIXES = ("/usr/local/opt/krb5", "/opt/homebrew/opt/krb5", "/usr")


def _find(name: str) -> str | None:
    """An MIT program, preferring a Homebrew prefix over the system's."""
    for prefix in _PREFIXES:
        for sub in ("sbin", "bin"):
            candidate = Path(prefix, sub, name)
            if candidate.exists():
                return str(candidate)
    return shutil.which(name)


KRB5KDC = _find("krb5kdc")
KDB5_UTIL = _find("kdb5_util")
KADMIN_LOCAL = _find("kadmin.local")
KINIT = _find("kinit")
KLIST = _find("klist")
KVNO = _find("kvno")

USER = "jane"
USER_PASSWORD = "jane-password"
SERVICE_PASSWORD = "service-password"


def available() -> bool:
    """Whether a realm can be made here: every MIT program is present."""
    return all((KRB5KDC, KDB5_UTIL, KADMIN_LOCAL, KINIT, KLIST, KVNO))


def _wait_for_port(port: int, proc: subprocess.Popen[bytes], timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False


class MitRealm:
    """A running KDC for ``realm``, with ``jane`` and ``xrootd/<host>`` in it.

    ``enctypes`` are MIT names, first preferred; they become the KDC's
    ``supported_enctypes`` and the clients' ``permitted_enctypes``, so a
    realm made with only ``aes256-cts-hmac-sha384-192`` issues nothing else.
    """

    def __init__(
        self,
        base: Path,
        *,
        realm: str = "XRD.TEST",
        host: str = "localhost",
        enctypes: tuple[str, ...] = ("aes256-cts-hmac-sha1-96",),
    ) -> None:
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)
        self.realm = realm
        self.host = host
        self.enctypes = enctypes
        self.port = _free_port()
        self.ccache = self.base / "ccache"
        self.krb5_conf = self.base / "krb5.conf"
        self.kdc_conf = self.base / "kdc.conf"
        self.service_keytab = self.base / "service.keytab"
        self.user_keytab = self.base / "user.keytab"
        self._proc: subprocess.Popen[bytes] | None = None

    @property
    def service(self) -> str:
        return f"xrootd/{self.host}@{self.realm}"

    @property
    def user(self) -> str:
        return f"{USER}@{self.realm}"

    @property
    def env(self) -> dict[str, str]:
        """What a process needs to live in this realm and nowhere else."""
        return {
            "KRB5_CONFIG": str(self.krb5_conf),
            "KRB5_KDC_PROFILE": str(self.kdc_conf),
            "KRB5CCNAME": f"FILE:{self.ccache}",
            "KRB5RCACHEDIR": str(self.base),
        }

    def _run(self, *argv: str, stdin: str | None = None) -> str:
        done = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            env={**os.environ, **self.env},
            timeout=60,
            check=False,
        )
        if done.returncode:
            raise RuntimeError(f"{' '.join(argv)} failed: {done.stdout}{done.stderr}")
        return done.stdout

    def _write_config(self) -> None:
        names = " ".join(self.enctypes)
        self.kdc_conf.write_text(
            # Loopback only, so nothing else on this machine can collide or connect.
            f"[kdcdefaults]\n kdc_listen = 127.0.0.1:{self.port}\n"
            f" kdc_tcp_listen = 127.0.0.1:{self.port}\n"
            f"[realms]\n {self.realm} = {{\n"
            f"  database_name = {self.base}/principal\n"
            f"  key_stash_file = {self.base}/stash\n"
            f"  acl_file = {self.base}/kadm5.acl\n"
            f"  supported_enctypes = {' '.join(e + ':normal' for e in self.enctypes)}\n"
            f"  master_key_type = aes256-cts-hmac-sha1-96\n"
            f"  max_life = 10h\n  max_renewable_life = 1d\n }}\n"
            f"[logging]\n kdc = FILE:{self.base}/kdc.log\n"
        )
        self.krb5_conf.write_text(
            f"[libdefaults]\n default_realm = {self.realm}\n"
            f" dns_lookup_kdc = false\n dns_lookup_realm = false\n"
            f" dns_canonicalize_hostname = false\n rdns = false\n"
            f" permitted_enctypes = {names}\n"
            f"[realms]\n {self.realm} = {{\n  kdc = 127.0.0.1:{self.port}\n }}\n"
            f"[domain_realm]\n {self.host} = {self.realm}\n"
        )

    def start(self) -> MitRealm:
        self._write_config()
        assert KDB5_UTIL and KADMIN_LOCAL and KRB5KDC
        self._run(KDB5_UTIL, "create", "-s", "-r", self.realm, "-P", "master-password")
        admin = [KADMIN_LOCAL, "-r", self.realm, "-q"]
        self._run(*admin, f"addprinc -pw {USER_PASSWORD} +allow_forwardable {USER}")
        self._run(*admin, f"addprinc -pw {SERVICE_PASSWORD} xrootd/{self.host}")
        # -norandkey keeps the password-derived keys, so tests can derive them too.
        self._run(*admin, f"ktadd -norandkey -k {self.service_keytab} xrootd/{self.host}")
        self._run(*admin, f"ktadd -norandkey -k {self.user_keytab} {USER}")
        # A free port can be taken between choosing it and binding it - other
        # suites run daemons too - so a KDC that cannot bind gets another port.
        for _attempt in range(5):
            if self._launch():
                return self
            self.port = _free_port()
            self._write_config()
        raise RuntimeError(f"krb5kdc did not come up: {self.kdc_log()}")

    def _launch(self) -> bool:
        assert KRB5KDC
        with (self.base / "krb5kdc.out").open("wb") as log:
            self._proc = subprocess.Popen(
                [KRB5KDC, "-n", "-r", self.realm],
                env={**os.environ, **self.env},
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        if _wait_for_port(self.port, self._proc):
            return True
        self.stop()
        return False

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)

    def kdc_log(self) -> str:
        path = self.base / "kdc.log"
        return path.read_text(errors="replace") if path.exists() else ""

    def add_service(self, name: str) -> Path:
        """Another service principal, with its own keytab; returns the keytab."""
        assert KADMIN_LOCAL
        keytab = self.base / f"{name.replace('/', '_')}.keytab"
        self._run(KADMIN_LOCAL, "-r", self.realm, "-q", f"addprinc -pw {SERVICE_PASSWORD} {name}")
        self._run(KADMIN_LOCAL, "-r", self.realm, "-q", f"ktadd -norandkey -k {keytab} {name}")
        return keytab

    def kinit(self, *options: str) -> None:
        """MIT ``kinit`` from the user's keytab into :attr:`ccache`."""
        assert KINIT
        self._run(KINIT, *options, "-k", "-t", str(self.user_keytab), USER)

    def kvno(self, principal: str) -> str:
        """MIT ``kvno``: fetch (or check) a service ticket, the way MIT does."""
        assert KVNO
        return self._run(KVNO, principal)

    def klist(self, cache: Path | None = None) -> str:
        assert KLIST
        return self._run(KLIST, "-e", "-f", "-c", f"FILE:{cache or self.ccache}")

    def __enter__(self) -> MitRealm:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def read_keytab(path: Path) -> dict[tuple[str, int], bytes]:
    """A keytab (format 0x0502) as ``{(principal, enctype): key}``: MIT's own keys."""
    data = path.read_bytes()
    assert data[:2] == b"\x05\x02", "not a version 2 keytab"
    out: dict[tuple[str, int], bytes] = {}
    pos = 2
    while pos + 4 <= len(data):
        (size,) = struct.unpack_from(">i", data, pos)
        pos += 4
        entry, pos = data[pos : pos + abs(size)], pos + abs(size)
        if size <= 0:
            continue  # a hole left by a deleted entry
        count, at = struct.unpack_from(">H", entry, 0)[0], 2
        parts = []
        for _ in range(count + 1):  # the realm first, then each component
            (length,) = struct.unpack_from(">H", entry, at)
            parts.append(entry[at + 2 : at + 2 + length].decode())
            at += 2 + length
        at += 4 + 4 + 1  # name type, timestamp, 8-bit kvno
        enctype, length = struct.unpack_from(">HH", entry, at)
        key = entry[at + 4 : at + 4 + length]
        out[("/".join(parts[1:]) + "@" + parts[0], enctype)] = key
    return out


class KerberizedXrootd(RealServer):
    """A real ``xrootd`` that accepts ``krb5`` and nothing else."""

    def __init__(
        self,
        root: Path,
        realm: MitRealm,
        *,
        principal: str | None = None,
        keytab: Path | None = None,
        export_tickets: str | None = None,
    ) -> None:
        super().__init__(root)
        self.realm = realm
        self.principal = principal or realm.service
        self.keytab = keytab or realm.service_keytab
        self.export_tickets = export_tickets

    def start(self) -> KerberizedXrootd:
        exptkn = f" -exptkn:{self.export_tickets}" if self.export_tickets else ""
        self._config.write_text(
            f"xrd.port {self.port}\nall.export {self.root}\n"
            f"all.adminpath {self._admin}\nall.pidpath {self._admin}\n"
            f"xrootd.seclib libXrdSec.so\n"
            f"sec.protocol krb5 {self.keytab}{exptkn} {self.principal}\n"
            f"sec.protbind * only krb5\n"
        )
        env = {**os.environ, **self.realm.env}
        env.pop("KRB5CCNAME")  # the server has no business with the user's cache
        with self._log.open("wb") as handle:
            self._proc = subprocess.Popen(
                [str(XROOTD), "-c", str(self._config), "-n", "krb5"],
                cwd=str(self._admin),
                stdout=handle,
                stderr=subprocess.STDOUT,
                env=env,
            )
        self._wait()
        return self

    def logins(self) -> list[str]:
        """Who the server says logged in: ``["jane", ...]`` from its ``login as`` lines."""
        return [
            line.rsplit(" login as ", 1)[1].strip()
            for line in self.log().splitlines()
            if " login as " in line
        ]


# -- fixtures captured from MIT, for the unit tests ---------------------------


def _udp_relay(target_port: int, seen: list[tuple[bytes, bytes]]) -> tuple[int, object]:
    """A one-shot UDP relay to the KDC that records the request and the reply."""
    import threading

    relay = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    relay.bind(("127.0.0.1", 0))

    def pump() -> None:
        data, client = relay.recvfrom(65535)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as up:
            up.settimeout(10)
            up.sendto(data, ("127.0.0.1", target_port))
            reply = up.recv(65535)
        seen.append((data, reply))
        relay.sendto(reply, client)
        relay.close()

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    return relay.getsockname()[1], thread


def _tcp_relay(target_port: int, upstream: bytearray) -> tuple[int, object]:
    """A one-connection TCP relay that records what the client sent."""
    import threading

    listener = socket.create_server(("127.0.0.1", 0))

    def pipe(source: socket.socket, sink: socket.socket, record: bytearray | None) -> None:
        while True:
            try:
                data = source.recv(65536)
            except OSError:
                break
            if not data:
                break
            if record is not None:
                record.extend(data)
            sink.sendall(data)
        try:
            sink.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def relay() -> None:
        client, _ = listener.accept()
        listener.close()
        server = socket.create_connection(("127.0.0.1", target_port))
        back = threading.Thread(target=pipe, args=(server, client, None))
        back.start()
        pipe(client, server, upstream)
        back.join()
        client.close()
        server.close()

    thread = threading.Thread(target=relay, daemon=True)
    thread.start()
    return listener.getsockname()[1], thread


def _credential_blobs(stream: bytes) -> list[bytes]:
    """The ``krb5`` credential of every ``kXR_auth`` in a client's byte stream."""
    out, pos = [], 20  # after the 20-byte initial handshake
    while pos + 24 <= len(stream):
        request_id = int.from_bytes(stream[pos + 2 : pos + 4], "big")
        length = int.from_bytes(stream[pos + 20 : pos + 24], "big")
        if request_id == 3000 and stream[pos + 16 : pos + 20] == b"krb5":  # kXR_auth
            out.append(stream[pos + 24 : pos + 24 + length][5:])  # after "krb5\0"
        pos += 24 + length
    return out


def capture(base: Path, enctype: str) -> dict[str, str]:
    """Everything the unit tests replay, from a fresh realm using ``enctype``.

    MIT ``kinit``'s cache; MIT ``kvno``'s TGS-REQ; this client's TGS-REQ and
    the real KDC's reply to it (nonce and clock fixed so it replays); and the
    AP-REQ and KRB-CRED the official ``xrdcp`` sent a real ``-exptkn`` xrootd.
    Every value is hex. Run ``python tests/_krb5kdc.py`` to regenerate
    ``tests/_krb5_mit.py``.
    """
    from xrdclient.auth.kerberos import tgs
    from xrdclient.auth.kerberos.ccache import read_ccache
    from xrdclient.auth.kerberos.kdc import exchange
    from xrdclient.auth.kerberos.model import parse_principal
    from xrdclient.auth.kerberos.profile import Profile

    out: dict[str, str] = {}
    realm = MitRealm(base, enctypes=(enctype,))
    with realm:
        realm.kinit("-f")
        out["tgt_ccache"] = realm.ccache.read_bytes().hex()
        profile = Profile.load(str(realm.krb5_conf))
        _client, found = read_ccache(str(realm.ccache))
        ours: list[bytes] = []

        def send(profile: Profile, name: str, request: bytes) -> bytes:
            ours.append(request)
            reply = exchange(profile, name, request)
            ours.append(reply)
            return reply

        clock = int(time.time()) + 0.25  # must be now: the KDC checks the skew
        tgs.request_ticket(
            found[0],
            parse_principal(realm.service),
            profile,
            etypes=[found[0].enctype],
            clock=lambda: clock,
            send=send,
            nonce=12345678,
        )
        out["our_tgs_req"], out["kdc_tgs_rep"] = ours[0].hex(), ours[1].hex()
        out["our_clock"] = repr(clock)

        seen: list[tuple[bytes, bytes]] = []
        port, thread = _udp_relay(realm.port, seen)
        original = realm.krb5_conf.read_text()
        realm.krb5_conf.write_text(original.replace(f":{realm.port}", f":{port}"))
        realm.kvno(realm.service)
        thread.join(10)  # type: ignore[attr-defined]
        realm.krb5_conf.write_text(original)
        out["mit_tgs_req"] = seen[0][0].hex()

        upstream = bytearray()
        server = KerberizedXrootd(base / "export", realm, export_tickets=str(base / "fwd_<user>"))
        with server:
            port, thread = _tcp_relay(server.port, upstream)
            source = base / "hello.txt"
            source.write_text("hello")
            subprocess.run(
                ["xrdcp", "-f", str(source), f"root://127.0.0.1:{port}/{server.path('x')}"],
                env={**os.environ, **realm.env},
                capture_output=True,
                check=True,
                timeout=60,
            )
            thread.join(10)  # type: ignore[attr-defined]
        out["xrdcp_ap_req"], out["xrdcp_krb_cred"] = (
            b.hex() for b in _credential_blobs(bytes(upstream))
        )
        out["service_ccache"] = realm.ccache.read_bytes().hex()
        keys = read_keytab(realm.service_keytab)
        out["service_key"] = next(iter(keys.values())).hex()
    return out


if __name__ == "__main__":  # pragma: no cover - a maintenance script
    import json
    import sys
    import tempfile

    sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parent.parent / "src")]
    captured = {}
    for number, name in ((18, "aes256-cts-hmac-sha1-96"), (20, "aes256-cts-hmac-sha384-192")):
        with tempfile.TemporaryDirectory(prefix="krb5cap", dir="/tmp") as scratch:
            captured[number] = capture(Path(scratch), name)
    target = Path(__file__).with_name("_krb5_mit.py")
    lines = [
        '"""Kerberos messages captured from MIT krb5 and XRootD, for replay in unit tests.',
        "",
        "Generated by ``python tests/_krb5kdc.py`` against a throwaway realm (see",
        ":func:`_krb5kdc.capture` for what each value is). The realm, its passwords",
        "and its keys existed for a few seconds on loopback; nothing here is secret.",
        '"""',
        "",
        f"USER_PASSWORD = {USER_PASSWORD!r}",
        f"SERVICE_PASSWORD = {SERVICE_PASSWORD!r}",
        "",
        "# ruff: noqa: E501 - hex dumps are one long line each, by design",
        "CAPTURED = " + json.dumps(captured, indent=4, sort_keys=True),
        "",
    ]
    target.write_text("\n".join(lines))
    print(f"wrote {target}")
