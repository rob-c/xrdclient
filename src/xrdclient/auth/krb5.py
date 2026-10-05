"""``krb5`` — XRootD Kerberos authentication with maintained crypto/GSSAPI.

The credential XRootD's ``krb5`` security plugin expects is ``"krb5\\0"``
followed by a raw AP-REQ (RFC 4120 section 3.2) for the service principal
the server names in its offer - ``xrootd/host@REALM`` - exactly what MIT's
``krb5_mk_req_extended`` produces and the server's ``krb5_rd_req`` checks
against its keytab. It is not a GSS-API token: the plugin never calls
GSS-API, and would reject one.

Building it takes a service ticket and its session key. Both come from the
credential cache ``kinit`` wrote - a ``FILE:`` or ``DIR:`` cache, a ``KCM:``
cache in ``sssd-kcm`` or Heimdal's ``kcm`` (RHEL 9's default), or a Linux
``KEYRING:`` cache; if the cache holds only the ticket-granting ticket, a
TGS exchange with the KDC named in ``krb5.conf`` gets the service ticket
first - the step ``kinit`` leaves to the first program that needs it. The
new ticket is kept in memory for this process; a KCM cache is also given a
copy, as MIT's library gives it one, while a file or a keyring is never
written. When the server runs with ``-exptkn`` (its offer ends ``,fwd``) it
then asks for the TGT itself, and gets a forwarded one in a KRB-CRED.

The legacy cache/TGS and forwarding paths remain protocol adapters, using
``cryptography`` for their ciphers. macOS ``API:`` and ``MEMORY:`` caches use
python-gssapi to obtain the native Kerberos token; its GSS envelope is removed
so XRootD still receives the raw AP-REQ it requires. Unit tests check that
framing; native-cache real-KDC interoperability still needs verification.
The legacy path has RFC vectors and MIT KDC/XRootD interoperability tests
(``tests/test_krb5_interop.py``).

What is not supported, each with an error that says so: ``kinit`` itself
(the AS exchange), cross-realm service tickets, KDC discovery through DNS,
native-cache TGT forwarding (use a ``FILE:`` cache), and single-DES,
triple-DES and RC4 keys.
"""

from __future__ import annotations

import os
import threading

from .._log import get_logger
from ..config import Config
from ..crypto.der import oid_string, parse
from ..errors import CredentialError
from .base import Credential, Offer
from .kerberos.caches import CredentialCache, FileCache, open_ccache

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
    """Every unexpired ticket in the credential cache. Empty if there is none.

    ``path`` is a FILE cache; by default it is whichever cache MIT would use,
    ``KCM:`` and ``KEYRING:`` included.
    """
    try:
        cache = FileCache(path) if path else open_ccache()
        _default, found = cache.read()
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
    cache: CredentialCache,
    client: Principal,
    live: list[Ticket],
    target: Principal,
    profile: Profile,
) -> Ticket:
    """A ticket for ``target``: from the cache, from memory, or from the KDC.

    One from the KDC is kept in memory, and offered to the cache - which a
    KCM cache takes, as MIT's library stores it there.
    """
    for ticket in live:
        if ticket.server.same_name(target) and ticket.key:
            return ticket
    slot = (cache.name, str(client), str(target))
    with _FETCHED_LOCK:
        held = _FETCHED.get(slot)
    if held is not None and held.remaining() > 60:
        return held
    tgt = _tgt(cache.name, client, live)
    ticket = request_ticket(tgt, target, profile, etypes=_etypes(profile))
    with _FETCHED_LOCK:
        _FETCHED[slot] = ticket
    cache.store(ticket)
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

    __slots__ = ("principal", "_ticket", "_forward", "_forwarded", "_native_request")
    name = "krb5"

    def __init__(
        self, principal: str, ticket: Ticket | None = None, *, forward: Ticket | None = None
    ) -> None:
        self.principal = principal
        self._ticket = ticket
        self._forward = forward
        self._forwarded = False
        self._native_request = b""

    def initial(self) -> bytes:
        if self._native_request:
            return PROTOCOL + self._native_request
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
        cache_name = os.environ.get("KRB5CCNAME", "")
        if cache_name.startswith(("API:", "MEMORY:")):
            return cls._from_native_cache(offer, host, cache_name)
        profile = Profile.load()
        cache = open_ccache(profile=profile)
        try:
            client, found = cache.read()
        except FileNotFoundError as exc:
            _log.debug("no credential cache: %s", exc)
            return None
        except (OSError, ValueError) as exc:
            raise CredentialError(
                f"the Kerberos credential cache {cache.name} is unreadable: {exc}"
            ) from exc
        live = [ticket for ticket in found if not ticket.expired]
        if not live:
            if found:
                raise _expired(cache.name, found)
            return None
        target = _target(offer, host, profile, client)
        ticket = _service_ticket(cache, client, live, target, profile)
        forwarding = wants_forwarding(offer)
        forward = _forwarded_tgt(cache.name, client, live, profile) if forwarding else None
        return cls(str(target), ticket, forward=forward)

    @classmethod
    def _from_native_cache(cls, offer: Offer, host: str, cache_name: str) -> KerberosCredential:
        if wants_forwarding(offer):
            raise CredentialError(
                "XRootD ticket forwarding from API/MEMORY caches is not supported yet. "
                "Run kinit -f with a FILE: cache for this server."
            )
        principal = service_principal(offer, host)
        result = cls(principal)
        result._native_request = _native_ap_req(principal, cache_name)
        return result

    def __repr__(self) -> str:
        return f"KerberosCredential(principal={self.principal!r})"


def _native_ap_req(principal: str, ccache: str) -> bytes:
    """Use the platform cache/KDC via python-gssapi; retain XRootD's raw AP-REQ."""
    try:
        import gssapi  # type: ignore[import-not-found,unused-ignore]
    except (ImportError, OSError) as exc:
        raise CredentialError(
            "Native Kerberos authentication is not installed. "
            "Install it with python -m pip install 'xrdclient[krb5]', "
            "or run kinit with a FILE: cache."
        ) from exc

    try:
        # KRB5CCNAME already names this cache. Using the default acquisition
        # also works on Heimdal builds without the credential-store extension.
        creds = gssapi.Credentials(usage="initiate", mechs=[gssapi.MechType.kerberos])
        context = gssapi.SecurityContext(
            name=gssapi.Name(principal, name_type=gssapi.NameType.kerberos_principal),
            mech=gssapi.MechType.kerberos,
            flags=0,
            usage="initiate",
            creds=creds,
        )
        token = context.step() or b""
    except (gssapi.exceptions.GSSError, NotImplementedError) as exc:  # type: ignore[attr-defined,unused-ignore]
        raise CredentialError(
            f"Kerberos authentication could not use {ccache}: {exc}. Run kinit and try again."
        ) from exc
    return _unwrap_ap_req(token)


def _unwrap_ap_req(token: bytes) -> bytes:
    """RFC 2743 mechanism header and RFC 4121 TOK_ID, not GSS message protection."""
    try:
        wrapper, end = parse(token)
        mechanism, pos = parse(wrapper.value)
        payload = wrapper.value[pos:]
        request, request_end = parse(payload[2:])
        valid = (
            wrapper.tag == 0x60
            and end == len(token)
            and oid_string(mechanism) == "1.2.840.113554.1.2.2"
            and payload[:2] == b"\x01\x00"
            and request.tag == 0x6E
            and request_end == len(payload) - 2
        )
    except ValueError:
        valid = False
    if not valid:
        raise CredentialError("The system Kerberos library returned a malformed AP-REQ token.")
    return payload[2:]
