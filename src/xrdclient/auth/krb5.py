"""``krb5`` — Kerberos 5, over a GSSAPI the platform already trusts.

The credential blob is ``"krb5\\0"`` followed by a marshalled AP-REQ for the
service principal the server names in its offer (``xrootd/host@REALM``).
Producing that AP-REQ means holding a service ticket and encrypting an
authenticator under its session key.

**This module does not do that in Python.** Everything else in this package
is pure Python because the alternative was a compiled extension for a wire
format that is fully specified. Kerberos is different: an AP-REQ this client
built could only be validated against a live KDC, and a security exchange
whose only test is its own decoder is worse than no implementation at all.
So the token comes from :mod:`gssapi` — the platform's MIT or Heimdal
library, the one the KDC administrator already tests against — and this
module is the framing and the discovery around it.

What *is* pure Python is everything that does not need the KDC: the FILE
credential-cache reader below. It is what makes the difference between
"authentication failed" and "your Kerberos ticket expired 40 minutes ago";
it works with no extra installed, and :func:`tickets` is public so a script
can ask the same question.

Install the mechanism with ``pip install xrdclient[krb5]``.
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass
from typing import Any

from .._compat import SLOTS
from .._log import get_logger
from ..config import Config
from ..errors import CredentialError
from .base import Credential, Offer

__all__ = [
    "KerberosCredential",
    "Principal",
    "Ticket",
    "default_ccache_path",
    "read_ccache",
    "tickets",
    "service_principal",
]

_log = get_logger(__name__)

#: The FILE credential cache format this reader understands (``0x0504``).
CCACHE_VERSION_4 = 0x0504
CCACHE_VERSION_3 = 0x0503


@dataclass(frozen=True, **SLOTS)
class Principal:
    """A Kerberos principal: components and a realm."""

    components: tuple[str, ...]
    realm: str
    name_type: int = 0

    def __str__(self) -> str:
        return "/".join(self.components) + (f"@{self.realm}" if self.realm else "")

    def __bool__(self) -> bool:
        return bool(self.components)


@dataclass(frozen=True, **SLOTS)
class Ticket:
    """One credential-cache entry."""

    client: Principal
    server: Principal
    enctype: int
    auth_time: int
    start_time: int
    end_time: int
    renew_till: int
    flags: int
    der: bytes = b""

    @property
    def expired(self) -> bool:
        return self.end_time != 0 and self.end_time <= time.time()

    def remaining(self) -> float:
        """Seconds of validity left; negative once expired."""
        return self.end_time - time.time()

    @property
    def is_tgt(self) -> bool:
        """True for the ticket-granting ticket, ``krbtgt/REALM@REALM``."""
        return bool(self.server.components) and self.server.components[0] == "krbtgt"

    def __repr__(self) -> str:
        return f"Ticket(server={str(self.server)!r}, expires_in={self.remaining():.0f}s)"


class _Reader:
    """A big-endian cursor that refuses to read past the end."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, count: int) -> bytes:
        if count < 0 or self.pos + count > len(self.data):
            raise ValueError(f"credential cache truncated at offset {self.pos}")
        out = self.data[self.pos : self.pos + count]
        self.pos += count
        return out

    def u16(self) -> int:
        return int(struct.unpack(">H", self.take(2))[0])

    def u32(self) -> int:
        return int(struct.unpack(">I", self.take(4))[0])

    def blob(self) -> bytes:
        return self.take(self.u32())

    @property
    def exhausted(self) -> bool:
        return self.pos >= len(self.data)


def _read_principal(reader: _Reader) -> Principal:
    """One principal, in the layout versions 3 and 4 share."""
    name_type = reader.u32()
    count = reader.u32()
    realm = reader.blob().decode("utf-8", "replace")
    components = tuple(reader.blob().decode("utf-8", "replace") for _ in range(count))
    return Principal(components, realm, name_type)


def read_ccache(path: str) -> tuple[Principal, list[Ticket]]:
    """Parse a FILE credential cache into its default principal and tickets.

    Versions 3 and 4 are read; both are what MIT and Heimdal write. Entries
    that will not parse end the scan rather than raising, because a cache
    being rewritten under us should cost the tail, not the whole answer.
    """
    with open(path, "rb") as handle:
        reader = _Reader(handle.read())
    version = reader.u16()
    if version not in (CCACHE_VERSION_3, CCACHE_VERSION_4):
        raise ValueError(f"unsupported credential cache version 0x{version:04x} in {path}")
    if version == CCACHE_VERSION_4:
        reader.take(reader.u16())  # header tags: none of them matter here
    default = _read_principal(reader)

    out: list[Ticket] = []
    while not reader.exhausted:
        try:
            client = _read_principal(reader)
            server = _read_principal(reader)
            enctype = reader.u16()
            if version == CCACHE_VERSION_3:
                reader.u16()  # version 3 wrote the enctype twice
            key = reader.blob()
            auth_time, start_time, end_time, renew_till = (reader.u32() for _ in range(4))
            reader.take(1)  # is_skey
            flags = reader.u32()
            for _ in range(reader.u32()):  # addresses
                reader.u16()
                reader.blob()
            for _ in range(reader.u32()):  # authorization data
                reader.u16()
                reader.blob()
            der = reader.blob()
            reader.blob()  # second ticket, used only for user-to-user
        except (ValueError, struct.error) as exc:
            _log.debug("credential cache %s ends early: %s", path, exc)
            break
        del key  # the session key stays in the cache; nothing here needs it
        out.append(
            Ticket(
                client=client,
                server=server,
                enctype=enctype,
                auth_time=auth_time,
                start_time=start_time,
                end_time=end_time,
                renew_till=renew_till,
                flags=flags,
                der=der,
            )
        )
    return default, out


def default_ccache_path(config: Config | None = None) -> str:
    """``$KRB5CCNAME`` with its ``FILE:`` prefix stripped, else ``/tmp/krb5cc_<uid>``."""
    name = os.environ.get("KRB5CCNAME", "")
    if name.startswith("FILE:"):
        return name[5:]
    if name and ":" not in name:
        return name
    return f"/tmp/krb5cc_{os.geteuid()}"


def tickets(path: str | None = None) -> list[Ticket]:
    """Every unexpired ticket in the credential cache. Empty if there is none."""
    try:
        _default, found = read_ccache(path or default_ccache_path())
    except (OSError, ValueError) as exc:
        _log.debug("no usable credential cache: %s", exc)
        return []
    return [ticket for ticket in found if not ticket.expired]


def service_principal(offer: Offer, host: str) -> str:
    """The principal to ask for a ticket to.

    The server names it in the offer's parameters; when it does not, the
    convention is ``xrootd/<host>``. The realm is dropped, because GSSAPI
    derives it from the instance and a stale realm in an offer is a common
    way to fail confusingly.
    """
    named = offer.params.split(",")[0].strip() if offer.params else ""
    if not named or ":" in named:
        named = f"xrootd/{host}" if host else "xrootd"
    return named.partition("@")[0]


class KerberosCredential(Credential):
    """``krb5`` — a GSSAPI context against the server's service principal."""

    __slots__ = ("principal", "_context", "_established")
    name = "krb5"

    def __init__(self, principal: str) -> None:
        self.principal = principal
        # ``gssapi.SecurityContext``; the module is optional, so it is only
        # named where an import of it is guarded.
        self._context: Any = None
        self._established = False

    def initial(self) -> bytes:
        return b"krb5\x00" + self._advance(None)

    def step(self, challenge: bytes) -> bytes | None:
        if self._established:
            return None
        token = self._advance(challenge)
        return b"krb5\x00" + token if token else None

    def _advance(self, token: bytes | None) -> bytes:
        """Drive the GSSAPI context one round, importing the module lazily."""
        try:
            import gssapi
        except ImportError as exc:  # pragma: no cover - the extra is not installed
            raise CredentialError(
                "krb5 authentication needs the gssapi module: "
                "pip install xrdclient[krb5]"
            ) from exc
        if self._context is None:
            target = gssapi.Name(self.principal, gssapi.NameType.kerberos_principal)
            self._context = gssapi.SecurityContext(name=target, usage="initiate")
        context = self._context
        try:
            out = context.step(token)
        except Exception as exc:  # gssapi raises its own hierarchy
            raise CredentialError(f"Kerberos exchange failed: {exc}") from exc
        self._established = bool(getattr(context, "complete", False))
        if not out and not self._established:
            raise CredentialError("the Kerberos exchange produced no token and did not complete")
        return bytes(out or b"")

    @classmethod
    def available(
        cls, offer: Offer, config: Config, *, username: str, host: str
    ) -> KerberosCredential | None:
        path = default_ccache_path(config)
        live = tickets(path)
        if os.path.exists(path) and not live:
            # A cache that exists but holds nothing live is the common case,
            # and falling through quietly beats a GSSAPI error five layers down.
            _log.debug("credential cache %s has no unexpired ticket", path)
            return None
        try:
            import gssapi  # noqa: F401
        except ImportError as exc:
            if live:
                # There is a ticket sitting right there. Saying nothing and
                # falling through to `unix` would be the unhelpful answer.
                raise CredentialError(
                    f"a Kerberos ticket for {live[0].client} is available but the gssapi "
                    "module is not installed: pip install xrdclient[krb5]"
                ) from exc
            _log.debug("krb5 offered, but there is no ticket and no gssapi")
            return None
        return cls(service_principal(offer, host))

    def __repr__(self) -> str:
        return f"KerberosCredential(principal={self.principal!r})"
