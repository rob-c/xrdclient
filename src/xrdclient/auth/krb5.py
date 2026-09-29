"""``krb5`` — Kerberos 5, in pure Python.

The credential XRootD's ``krb5`` security plugin expects is ``"krb5\\0"``
followed by a raw AP-REQ (RFC 4120 section 3.2) for the service principal
the server names in its offer - ``xrootd/host@REALM`` - exactly what MIT's
``krb5_mk_req_extended`` produces and the server's ``krb5_rd_req`` checks
against its keytab. It is not a GSS-API token: the plugin never calls
GSS-API, and would reject one.

Building it takes a service ticket and its session key. Both come from the
FILE credential cache ``kinit`` wrote; if the cache holds only the
ticket-granting ticket, a TGS exchange with the KDC named in ``krb5.conf``
gets the service ticket first - the step ``kinit`` leaves to the first
program that needs it. The new ticket is kept in memory for this process
and never written back to the cache. When the server runs with ``-exptkn``
(its offer ends ``,fwd``) it then asks for the TGT itself, and gets a
forwarded one in a KRB-CRED.

This is all done here rather than through a system GSS-API library, which
is what makes the package installable with no compiler and no extra - and
it is proven the only way a security exchange can be: by RFC test vectors
for the cryptography, and by a real MIT KDC and a real ``xrootd`` accepting
what it builds (``tests/test_krb5_interop.py``).

What is not supported, each with an error that says so: ``kinit`` itself
(the AS exchange), cross-realm service tickets, KDC discovery through DNS,
credential caches that are not files (``KCM:``, ``KEYRING:``, ``API:``),
and single-DES, triple-DES and RC4 keys.
"""

from __future__ import annotations

import threading

from .._log import get_logger
from ..config import Config
from ..errors import CredentialError
from .base import Credential, Offer

# The cache format's names were this module's before the reader moved into
# :mod:`.kerberos.ccache`; they are re-exported so that code naming them here
# keeps working.
from .kerberos.ccache import CCACHE_VERSION_3 as CCACHE_VERSION_3
from .kerberos.ccache import CCACHE_VERSION_4 as CCACHE_VERSION_4
from .kerberos.ccache import _Reader as _Reader
from .kerberos.ccache import read_ccache, resolve_ccache
from .kerberos.model import Principal, Ticket, parse_principal
from .kerberos.profile import Profile, enctype_list
from .kerberos.tgs import (
    KDC_OPT_FORWARDABLE,
    KDC_OPT_FORWARDED,
    build_ap_req,
    build_krb_cred,
    request_ticket,
)

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

PROTOCOL = b"krb5\x00"

#: The server's challenge when it wants a forwarded TGT (``-exptkn``).
FORWARD_CHALLENGE = b"fwdtgt"

#: MIT's default ``default_tgs_enctypes`` order, restricted to what this client does.
DEFAULT_ENCTYPES = (18, 17, 20, 19)

#: Service tickets fetched from the KDC, by (cache, client, service). A
#: process that opens many connections to one server asks the KDC once.
_FETCHED: dict[tuple[str, str, str], Ticket] = {}
_FETCHED_LOCK = threading.Lock()


def default_ccache_path(config: Config | None = None) -> str:
    """The file the default credential cache lives in, as MIT would find it.

    ``$KRB5CCNAME``, else ``default_ccache_name`` from ``krb5.conf``, else
    ``/tmp/krb5cc_<uid>``. Raises :class:`~xrdclient.errors.CredentialError`
    for a cache that is not a file (``KCM:``, ``KEYRING:``, ...).
    """
    return resolve_ccache()


def tickets(path: str | None = None) -> list[Ticket]:
    """Every unexpired ticket in the credential cache. Empty if there is none."""
    try:
        _default, found = read_ccache(path or default_ccache_path())
    except (OSError, ValueError, CredentialError) as exc:
        _log.debug("no usable credential cache: %s", exc)
        return []
    return [ticket for ticket in found if not ticket.expired]


def service_principal(offer: Offer, host: str) -> str:
    """The principal to ask for a ticket to.

    The server names it - realm and all - as the first parameter of its
    offer; when it does not, the convention is ``xrootd/<host>``, and the
    realm is found from ``krb5.conf`` by :meth:`KerberosCredential.available`.
    """
    named = offer.params.split(",")[0].strip() if offer.params else ""
    if not named or ":" in named:
        named = f"xrootd/{host}" if host else "xrootd"
    return named


def wants_forwarding(offer: Offer) -> bool:
    """Whether the server runs with ``-exptkn`` and will ask for the TGT."""
    return "fwd" in (part.strip() for part in offer.params.split(",")[1:])


def _target(offer: Offer, host: str, profile: Profile, client: Principal) -> Principal:
    """The service principal, with a realm: named, else mapped from the host, else the client's."""
    target = parse_principal(service_principal(offer, host))
    if target.realm:
        return target
    instance = target.components[1] if len(target.components) > 1 else host
    realm = profile.realm_for_host(instance) or client.realm
    return Principal(target.components, realm, target.name_type)


def _etypes(profile: Profile) -> list[int]:
    for tag in ("default_tgs_enctypes", "permitted_enctypes"):
        configured = profile.libdefault(tag)
        if configured:
            found = enctype_list(configured)
            if found:
                return found
    return list(DEFAULT_ENCTYPES)


def _expired(path: str, found: list[Ticket]) -> CredentialError:
    latest = max(found, key=lambda ticket: ticket.end_time)
    minutes = int(-latest.remaining() // 60)
    return CredentialError(
        f"the Kerberos tickets for {latest.client} in {path} expired {minutes} minutes ago; "
        "run kinit"
    )


def _tgt(path: str, client: Principal, live: list[Ticket]) -> Ticket:
    home = Principal(("krbtgt", client.realm), client.realm)
    for ticket in live:
        if ticket.server.same_name(home) and ticket.key:
            return ticket
    raise CredentialError(
        f"no live ticket-granting ticket for {client.realm} in {path} to get a service "
        "ticket with; run kinit"
    )


def _service_ticket(
    path: str, client: Principal, live: list[Ticket], target: Principal, profile: Profile
) -> Ticket:
    """A ticket for ``target``: from the cache, from memory, or from the KDC."""
    for ticket in live:
        if ticket.server.same_name(target) and ticket.key:
            return ticket
    slot = (path, str(client), str(target))
    with _FETCHED_LOCK:
        held = _FETCHED.get(slot)
    if held is not None and held.remaining() > 60:
        return held
    ticket = request_ticket(_tgt(path, client, live), target, profile, etypes=_etypes(profile))
    with _FETCHED_LOCK:
        _FETCHED[slot] = ticket
    return ticket


def _forwarded_tgt(path: str, client: Principal, live: list[Ticket], profile: Profile) -> Ticket:
    tgt = _tgt(path, client, live)
    if not tgt.forwardable:
        raise CredentialError(
            "the server runs with -exptkn and wants a forwarded ticket-granting ticket, but "
            f"the one in {path} is not forwardable; run kinit -f"
        )
    return request_ticket(
        tgt,
        tgt.server,
        profile,
        etypes=_etypes(profile),
        options=KDC_OPT_FORWARDED | KDC_OPT_FORWARDABLE,
    )


class KerberosCredential(Credential):
    """``krb5`` — a raw AP-REQ for the server's principal, and a forwarded TGT if asked."""

    __slots__ = ("principal", "_ticket", "_forward", "_forwarded")
    name = "krb5"

    def __init__(
        self, principal: str, ticket: Ticket | None = None, *, forward: Ticket | None = None
    ) -> None:
        self.principal = principal
        self._ticket = ticket
        self._forward = forward
        self._forwarded = False

    def initial(self) -> bytes:
        if self._ticket is None:
            raise CredentialError(f"no service ticket for {self.principal}")
        return PROTOCOL + build_ap_req(self._ticket)

    def step(self, challenge: bytes) -> bytes | None:
        if self._forwarded:
            return None
        if not challenge.startswith(FORWARD_CHALLENGE):
            raise CredentialError(f"unexpected krb5 challenge from the server: {challenge[:16]!r}")
        if self._forward is None or self._ticket is None:
            raise CredentialError(
                "the server asked for a forwarded ticket-granting ticket without saying so "
                "in its offer (',fwd'), so none was fetched"
            )
        self._forwarded = True
        return PROTOCOL + build_krb_cred(self._forward, self._ticket)

    @classmethod
    def available(
        cls, offer: Offer, config: Config, *, username: str, host: str
    ) -> KerberosCredential | None:
        profile = Profile.load()
        path = resolve_ccache(profile=profile)
        try:
            client, found = read_ccache(path)
        except FileNotFoundError:
            _log.debug("no credential cache at %s", path)
            return None
        except (OSError, ValueError) as exc:
            raise CredentialError(
                f"the Kerberos credential cache {path} is unreadable: {exc}"
            ) from exc
        live = [ticket for ticket in found if not ticket.expired]
        if not live:
            if found:
                raise _expired(path, found)
            return None
        target = _target(offer, host, profile, client)
        ticket = _service_ticket(path, client, live, target, profile)
        forward = _forwarded_tgt(path, client, live, profile) if wants_forwarding(offer) else None
        return cls(str(target), ticket, forward=forward)

    def __repr__(self) -> str:
        return f"KerberosCredential(principal={self.principal!r})"
