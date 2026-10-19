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

Two implementations produce that AP-REQ, and ``$XRD_KRB5_BACKEND`` picks
(:func:`.kerberos.caches.backend`):

* **the system library, through python-gssapi** (``xrdclient[krb5]``,
  :func:`xrdsec.krb5.gss.initiator_ap_req`). It
  reads every cache type the platform's Kerberos does - macOS ``API:`` and
  ``MEMORY:`` included - and does the TGS exchange with the platform's own
  rules: DNS KDC discovery, cross-realm referrals, whatever enctypes it
  allows. Its GSS envelope (RFC 2743 header, RFC 4121 token id) is removed
  so XRootD still receives the raw AP-REQ it requires; the result is
  checked against MIT ``krb5kdc`` and a real ``xrootd`` in
  ``tests/test_krb5_interop.py``. Like every other Kerberos program, the
  library may keep the service ticket it fetched in the user's cache.
* **the portable reader** (:mod:`xrdsec.krb5.client`): pure Python with
  ``cryptography`` for the ciphers, RFC vectors and the same interoperability
  tests. It needs nothing installed, and it is what forwards a TGT to a
  server running ``-exptkn`` - python-gssapi has no way to hand over the
  raw KRB-CRED XRootD wants.

``auto`` - the default - uses the system library when python-gssapi is
installed and the server does not ask for a forwarded TGT, and the portable
reader otherwise, or when the library finds no credential. ``gssapi`` and
``portable`` force one.

What the portable reader does not support, each with an error that says so:
``kinit`` itself (the AS exchange), cross-realm service tickets, KDC
discovery through DNS, and single-DES, triple-DES and RC4 keys - all of
which the system library handles. Forwarding from an ``API:`` or ``MEMORY:``
cache is not supported at all (use a ``FILE:`` cache).

Both implementations are xrdsec's (:mod:`xrdsec.krb5`), shared with
xgfalclient since 0.4.0. What is this module's is XRootD's side of the
exchange: the ``krb5\0`` framing, the service principal the server's offer
names, the ``,fwd`` / ``fwdtgt`` forwarding handshake, the
``$XRD_KRB5_BACKEND`` choice, and turning the library's errors into the
:class:`~xrdclient.errors.CredentialError` the authentication ladder records.
"""

from __future__ import annotations

import os

from xrdsec.krb5 import client as _client
from xrdsec.krb5 import gss
from xrdsec.krb5.ccache import CCACHE_VERSION_3 as CCACHE_VERSION_3
from xrdsec.krb5.ccache import CCACHE_VERSION_4 as CCACHE_VERSION_4
from xrdsec.krb5.ccache import read_ccache as read_ccache
from xrdsec.krb5.ccache import resolve_ccache
from xrdsec.krb5.client import DEFAULT_ENCTYPES as DEFAULT_ENCTYPES
from xrdsec.krb5.client import tickets as tickets
from xrdsec.krb5.model import Principal as Principal
from xrdsec.krb5.model import Ticket as Ticket
from xrdsec.krb5.model import parse_principal
from xrdsec.krb5.profile import Profile
from xrdsec.krb5.tgs import build_ap_req, build_krb_cred

from ..config import Config
from ..errors import CredentialError
from ..log import get_logger
from .base import Credential, Offer
from .kerberos.caches import backend

__all__ = [
    "KerberosCredential",
    "Principal",
    "Ticket",
    "default_ccache_path",
    "read_ccache",
    "service_principal",
    "tickets",
]

_log = get_logger(__name__)

#: Prefix of every credential this mechanism sends; the AP-REQ or KRB-CRED follows.
PROTOCOL = b"krb5\x00"

#: The server's challenge when it wants a forwarded TGT (``-exptkn``).
FORWARD_CHALLENGE = b"fwdtgt"


def default_ccache_path(config: Config | None = None) -> str:
    """The file the default credential cache lives in, as MIT would find it.

    ``$KRB5CCNAME``, else ``default_ccache_name`` from ``krb5.conf``, else
    ``/tmp/krb5cc_<uid>``. Raises :class:`~xrdclient.errors.CredentialError`
    for a cache that is not a file (``KCM:``, ``KEYRING:``, ...).
    """
    return resolve_ccache()


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


class KerberosCredential(Credential):
    """``krb5`` — a raw AP-REQ for the server's principal, and a forwarded TGT if asked."""

    __slots__ = ("_forward", "_forwarded", "_native_request", "_ticket", "principal")
    name = "krb5"

    def __init__(
        self, principal: str, ticket: Ticket | None = None, *, forward: Ticket | None = None
    ) -> None:
        """A credential for ``principal`` from a service ``ticket`` already in hand.

        ``ticket`` is ``None`` on the system-library path, where the AP-REQ
        is built by python-gssapi instead and stored in ``_native_request``.
        ``forward`` is the TGT to hand over when the server asks for one.
        """
        self.principal = principal
        self._ticket = ticket
        self._forward = forward
        self._forwarded = False
        self._native_request = b""

    def initial(self) -> bytes:
        """``krb5\\0`` and the AP-REQ: python-gssapi's, or one built from the ticket now.

        Building it here, not in :meth:`available`, gives the authenticator a
        timestamp from the moment of sending, which the server's
        ``krb5_rd_req`` holds to its clock-skew allowance (five minutes by
        default) and its replay cache.
        """
        if self._native_request:
            return PROTOCOL + self._native_request
        if self._ticket is None:
            raise CredentialError(f"no service ticket for {self.principal}")
        return PROTOCOL + build_ap_req(self._ticket)

    def step(self, challenge: bytes) -> bytes | None:
        """Answer the server's ``fwdtgt`` request with a KRB-CRED, once.

        The only follow-up XRootD's ``krb5`` plugin sends; anything else is a
        protocol error. A second request after the TGT went over gets
        ``None`` - the exchange is over as far as this side is concerned.
        """
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
        """A credential from whichever backend ``$XRD_KRB5_BACKEND`` and the offer allow.

        ``API:`` and ``MEMORY:`` caches exist only inside the platform
        library, so they always go to python-gssapi. Otherwise ``auto`` and
        ``gssapi`` try the library first unless the server wants a forwarded
        TGT (which only the portable code can send); ``auto`` falls back to
        the portable reader when the library raises
        :class:`~xrdclient.errors.CredentialError` (not installed, no usable
        credential), ``gssapi`` does not.
        """
        cache_name = os.environ.get("KRB5CCNAME", "")
        if cache_name.startswith(("API:", "MEMORY:")):
            return cls._from_native_cache(offer, host, cache_name)
        choice = backend()
        if choice in ("auto", "gssapi") and not wants_forwarding(offer):
            try:
                return cls._from_native_cache(offer, host, cache_name or "the default cache")
            except CredentialError as exc:
                if choice == "gssapi":
                    raise
                # No python-gssapi, or no credential the library could use:
                # the portable reader says which, in its own words.
                _log.debug("system Kerberos unavailable (%s); using the portable reader", exc)
        return cls._from_portable_cache(offer, host)

    @classmethod
    def _from_portable_cache(cls, offer: Offer, host: str) -> KerberosCredential | None:
        """The pure-Python path: read the cache, ask the KDC, build the AP-REQ here.

        :func:`xrdsec.krb5.client.service_credentials` does the reading and
        the asking; ``None`` (no cache, or an empty one) lets the ladder move
        on quietly, and its errors are already the ``CredentialError`` the
        ladder records.
        """
        found = _client.service_credentials(
            service_principal(offer, host), host, forward=wants_forwarding(offer)
        )
        if found is None:
            return None
        return cls(str(found.target), found.ticket, forward=found.forward)

    @classmethod
    def _from_native_cache(cls, offer: Offer, host: str, cache_name: str) -> KerberosCredential:
        """A credential whose AP-REQ python-gssapi builds now, from the platform's cache.

        ``cache_name`` is only for messages - the library reads
        ``$KRB5CCNAME`` itself. Raises rather than returning ``None`` so
        ``auto`` can log why before falling back.
        """
        if wants_forwarding(offer):
            raise CredentialError(
                "XRootD ticket forwarding from API/MEMORY caches is not supported yet. "
                "Run kinit -f with a FILE: cache for this server."
            )
        principal = _native_principal(offer, host)
        result = cls(principal)
        result._native_request = _native_ap_req(principal, cache_name)
        return result

    def __repr__(self) -> str:
        """The service principal only; tickets and session keys stay out of logs."""
        return f"KerberosCredential(principal={self.principal!r})"


def _native_principal(offer: Offer, host: str) -> str:
    """The service principal for the system library, with the realm the portable path would use.

    A name with no realm would be completed with ``default_realm`` alone;
    the portable path consults ``[domain_realm]`` first, and so does this, so
    the two ask the KDC for the same service. The name the offer gave is
    kept verbatim (quoting included) when it already has a realm.
    """
    named = service_principal(offer, host)
    if parse_principal(named).realm:
        return named
    realm = _client.target_principal(named, host, Profile.load()).realm
    return f"{named}@{realm}" if realm else named


def _native_ap_req(principal: str, ccache: str) -> bytes:
    """The platform library's raw AP-REQ for ``principal``, via :mod:`xrdsec.krb5.gss`.

    ``ccache`` is only for messages - the library reads ``$KRB5CCNAME``
    itself. Both failures are :class:`~xrdclient.errors.CredentialError`, so
    ``auto`` can fall back to the portable reader: the binding missing (with
    this package's install hint), or the library unable to use the cache.
    """
    try:
        gss.load_backend()
    except gss.KerberosError as exc:
        raise CredentialError(
            "Native Kerberos authentication is not installed. "
            "Install it with python -m pip install 'xrdclient[krb5]', "
            "or run kinit with a FILE: cache."
        ) from exc
    try:
        return gss.initiator_ap_req(principal)
    except gss.KerberosError as exc:
        raise CredentialError(
            f"Kerberos authentication could not use {ccache}: {exc}. Run kinit and try again."
        ) from exc
