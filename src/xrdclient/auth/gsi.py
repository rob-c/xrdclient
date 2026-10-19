"""``gsi`` - X.509 proxy authentication and delegation: xrdclient's adapter.

XRootD's ``gsi`` protocol (XrdSecgsi: bucket messages, Diffie-Hellman over
the server's group, an AES session, proxy delegation by signing the server's
request) is implemented once, in :mod:`xrdsec.gsi.secgsi`, since 0.4.0 - it
moved there from this module. What stays here is what makes it an xrdclient
mechanism (:class:`~xrdclient.auth.base.Credential`):

* **where the credential comes from**, in gfal2's order: a proxy somebody
  named (:attr:`Config.proxy <xrdclient.config.Config.proxy>` or
  ``$X509_USER_PROXY``), else the separate user certificate and key (an
  in-memory proxy, :mod:`xrdclient._usercred`), else the proxy at the
  default location - read through :func:`~xrdsec.gsi.secgsi.read_proxy`, so
  a file others can read, or a link at the predictable default name, is
  refused;
* **configuration**: delegation (``Config(gsi_delegate=True)``, the stock
  ``$XrdSecGSIDELEGPROXY``), the login-time server check
  (``Config(gsi_verify_server=False)`` or ``$XRD_GSI_VERIFY_SERVER=0``
  turns it off for a broken test setup; delegation still checks), the CA
  directory, and the offer's ``c:``/``ca:``/``v:`` parameters;
* **the ladder's questions**: :meth:`GSICredential.available` (usable here,
  or ``None`` with a log line saying why) and :meth:`GSICredential.missing`
  (the same refusals, in words a person can act on, for the prompt).

The protocol's public names are re-exported from :mod:`xrdsec.gsi.secgsi`
(the same objects), so code written against ``xrdclient.auth.gsi`` keeps
working; new code imports the library module.
"""

from __future__ import annotations

import os

from xrdsec.gsi.secgsi import BUCKET_CIPHER as BUCKET_CIPHER
from xrdsec.gsi.secgsi import BUCKET_CIPHER_ALG as BUCKET_CIPHER_ALG
from xrdsec.gsi.secgsi import BUCKET_CLNT_OPTS as BUCKET_CLNT_OPTS
from xrdsec.gsi.secgsi import BUCKET_CRYPTOMOD as BUCKET_CRYPTOMOD
from xrdsec.gsi.secgsi import BUCKET_ISSUER_HASH as BUCKET_ISSUER_HASH
from xrdsec.gsi.secgsi import BUCKET_MAIN as BUCKET_MAIN
from xrdsec.gsi.secgsi import BUCKET_MD_ALG as BUCKET_MD_ALG
from xrdsec.gsi.secgsi import BUCKET_MESSAGE as BUCKET_MESSAGE
from xrdsec.gsi.secgsi import BUCKET_PUK as BUCKET_PUK
from xrdsec.gsi.secgsi import BUCKET_RTAG as BUCKET_RTAG
from xrdsec.gsi.secgsi import BUCKET_SIGNED_RTAG as BUCKET_SIGNED_RTAG
from xrdsec.gsi.secgsi import BUCKET_VERSION as BUCKET_VERSION
from xrdsec.gsi.secgsi import BUCKET_X509 as BUCKET_X509
from xrdsec.gsi.secgsi import BUCKET_X509_REQ as BUCKET_X509_REQ
from xrdsec.gsi.secgsi import MIN_DH_BITS as MIN_DH_BITS
from xrdsec.gsi.secgsi import STEP_CLIENT_CERT as STEP_CLIENT_CERT
from xrdsec.gsi.secgsi import STEP_CLIENT_CERTREQ as STEP_CLIENT_CERTREQ
from xrdsec.gsi.secgsi import STEP_CLIENT_SIGPXY as STEP_CLIENT_SIGPXY
from xrdsec.gsi.secgsi import STEP_SERVER_CERT as STEP_SERVER_CERT
from xrdsec.gsi.secgsi import STEP_SERVER_PXYREQ as STEP_SERVER_PXYREQ
from xrdsec.gsi.secgsi import WEAK_DH_BITS as WEAK_DH_BITS
from xrdsec.gsi.secgsi import Bucket as Bucket
from xrdsec.gsi.secgsi import Delegation as Delegation
from xrdsec.gsi.secgsi import PeerPublic as PeerPublic
from xrdsec.gsi.secgsi import SecgsiClient as SecgsiClient
from xrdsec.gsi.secgsi import ServerCheck as ServerCheck
from xrdsec.gsi.secgsi import Session as Session
from xrdsec.gsi.secgsi import answer_certificate as answer_certificate
from xrdsec.gsi.secgsi import answer_proxy_request as answer_proxy_request
from xrdsec.gsi.secgsi import build_cert_response as build_cert_response
from xrdsec.gsi.secgsi import build_certreq as build_certreq
from xrdsec.gsi.secgsi import check_group as check_group
from xrdsec.gsi.secgsi import check_server as check_server
from xrdsec.gsi.secgsi import choose_digest as choose_digest
from xrdsec.gsi.secgsi import client_ca_hashes as client_ca_hashes
from xrdsec.gsi.secgsi import decode_message as decode_message
from xrdsec.gsi.secgsi import encode_message as encode_message
from xrdsec.gsi.secgsi import encode_public_blob as encode_public_blob
from xrdsec.gsi.secgsi import find_bucket as find_bucket
from xrdsec.gsi.secgsi import parse_dh_parameters as parse_dh_parameters
from xrdsec.gsi.secgsi import parse_peer_blob as parse_peer_blob
from xrdsec.gsi.secgsi import read_proxy as read_proxy
from xrdsec.gsi.secgsi import seal as seal
from xrdsec.gsi.secgsi import server_names as server_names
from xrdsec.gsi.secgsi import session_key as session_key
from xrdsec.gsi.secgsi import unseal as unseal
from xrdsec.rsa import RSAPrivateKey
from xrdsec.x509 import ProxyCredential

from .. import _usercred
from .._gridpaths import default_proxy_path
from ..config import Config
from ..errors import CredentialError
from ..log import get_logger
from .base import Credential, Offer
from .prompt import Ask, humanise

__all__ = [
    "BUCKET_CIPHER",
    "BUCKET_CIPHER_ALG",
    "BUCKET_CLNT_OPTS",
    "BUCKET_CRYPTOMOD",
    "BUCKET_ISSUER_HASH",
    "BUCKET_MAIN",
    "BUCKET_MD_ALG",
    "BUCKET_MESSAGE",
    "BUCKET_PUK",
    "BUCKET_RTAG",
    "BUCKET_SIGNED_RTAG",
    "BUCKET_VERSION",
    "BUCKET_X509",
    "BUCKET_X509_REQ",
    "MIN_DH_BITS",
    "STEP_CLIENT_CERT",
    "STEP_CLIENT_CERTREQ",
    "STEP_CLIENT_SIGPXY",
    "STEP_SERVER_CERT",
    "STEP_SERVER_PXYREQ",
    "WEAK_DH_BITS",
    "Bucket",
    "Delegation",
    "GSICredential",
    "PeerPublic",
    "SecgsiClient",
    "ServerCheck",
    "Session",
    "answer_certificate",
    "answer_proxy_request",
    "build_cert_response",
    "build_certreq",
    "check_group",
    "check_server",
    "choose_digest",
    "client_ca_hashes",
    "decode_message",
    "encode_message",
    "encode_public_blob",
    "find_bucket",
    "parse_dh_parameters",
    "parse_peer_blob",
    "read_proxy",
    "seal",
    "server_names",
    "session_key",
    "unseal",
]

_log = get_logger(__name__)

#: How a user of this client turns the login-time server check off; named in the refusal.
_BYPASS = "XRD_GSI_VERIFY_SERVER=0"


def _named_proxy(config: Config) -> bool:
    """Whether the proxy path was named by the user rather than defaulted.

    ``/tmp/x509up_u<euid>`` is a name anyone can predict, in a directory
    anyone can write, so a symbolic link there is refused rather than
    followed; a path from :attr:`Config.proxy` or ``$X509_USER_PROXY`` was
    chosen, and may be a link.
    """
    return bool(config.proxy or os.environ.get("X509_USER_PROXY"))


def _source(config: Config) -> str:
    """The file a GSI login's credential comes from, for messages."""
    if _usercred.wanted(config):
        return os.path.expanduser(str(config.userkey))
    return default_proxy_path(config)


def _credential(config: Config) -> ProxyCredential:
    """The credential a GSI login presents, in gfal2's order.

    A proxy somebody named; else the separate user certificate and key,
    from which a proxy is made in memory (see :mod:`xrdclient._usercred`);
    else the proxy at the default location.
    """
    if _usercred.wanted(config):
        return _usercred.gsi_credential(config)
    return read_proxy(default_proxy_path(config), named=_named_proxy(config))


def _server_version(options: dict[str, str]) -> int:
    """The ``v:`` of the offer; ``0`` - the oldest behaviour - when absent or garbled."""
    try:
        return int(options.get("v", "0"))
    except ValueError:
        return 0


class GSICredential(SecgsiClient, Credential):
    """``gsi`` - an X.509 proxy from ``$X509_USER_PROXY``, as an xrdclient mechanism.

    The exchange itself - :meth:`initial`, :meth:`step`, the session state
    between them - is :class:`xrdsec.gsi.secgsi.SecgsiClient`'s; this class
    adds the factory side (:meth:`available`, :meth:`missing`,
    :meth:`using`) and the wire name.
    """

    __slots__ = ()
    name = "gsi"

    @classmethod
    def available(
        cls, offer: Offer, config: Config, *, username: str, host: str
    ) -> GSICredential | None:
        """A credential from the configured proxy or user certificate, if usable.

        ``None`` for anything that makes GSI impossible here - no file, a
        file that is not this user's alone, an encrypted key nobody unlocked,
        an unreadable or expired proxy, a non-RSA key; :meth:`missing`
        explains the same cases in words a person can act on.
        """
        path = _source(config)
        try:
            proxy = _credential(config)
        except FileNotFoundError:
            return None
        except PermissionError as exc:
            # Loud, as for an exposed SSS keytab: quietly falling through to
            # a weaker mechanism is the downgrade nobody notices.
            _log.warning("ignoring the X.509 proxy - %s", exc)
            return None
        except CredentialError as exc:  # an encrypted user key nobody could unlock
            _log.warning("not using the X.509 certificate - %s", exc)
            return None
        except (OSError, ValueError) as exc:
            _log.debug("unusable X.509 proxy %s: %s", path, exc)
            return None
        if not isinstance(proxy.key, RSAPrivateKey):
            return None
        if proxy.expired:
            _log.debug("X.509 proxy %s expired", path)
            return None
        options = offer.options()
        check = ServerCheck(host, config.ca_path, server_names(), _BYPASS)
        return cls(
            proxy,
            cryptomod=options.get("c", "ssl"),
            # Our CA, not the server's: the server's own ``ca:`` names the
            # authority behind *its* certificate, and echoing it back makes a
            # server whose CA differs from ours build our chain on the wrong
            # anchor ("chain is inconsistent"). Its list is the fallback only
            # for a chain too odd to hash, which is what was sent before.
            issuer_hash=client_ca_hashes(proxy) or options.get("ca", ""),
            server_version=_server_version(options),
            delegation=Delegation(config.gsi_delegate, host, config.ca_path),
            server_check=check if config.gsi_verify_server else None,
        )

    @classmethod
    def missing(cls, offer: Offer, config: Config, *, username: str, host: str) -> Ask | None:
        """Why there is no usable proxy, mirroring :meth:`available`'s refusals.

        Reads the credential again rather than sharing :meth:`available`'s
        result, so a proxy renewed in the meantime is seen as good.
        """
        path = _source(config)
        try:
            proxy = _credential(config)
        except FileNotFoundError:
            reason = f"there is no file at {path}"
        except (PermissionError, CredentialError) as exc:
            reason = str(exc)
        except (OSError, ValueError) as exc:
            reason = f"{path} could not be read as a proxy: {exc}"
        else:
            if proxy.expired:
                reason = f"the proxy in {path} expired {humanise(proxy.remaining())} ago"
            elif not isinstance(proxy.key, RSAPrivateKey):
                reason = f"the key in {path} is not RSA, which is all GSI does"
            else:
                return None  # perfectly good: available() said no for another reason
        return Ask(
            mechanism=cls.name,
            what="an X.509 proxy",
            reason=reason,
            hint="voms-proxy-init -voms <your VO>, or point $X509_USER_PROXY at one",
            prompt="path to a proxy file",
            host=host,
        )

    @classmethod
    def using(cls, answer: str, config: Config) -> Config:
        """A config naming the typed proxy path - a named proxy, so links are followed."""
        return config.evolve(proxy=os.path.expanduser(answer))

    def __repr__(self) -> str:
        """The identity the proxy asserts - public, and what a log reader wants to know."""
        return f"GSICredential(identity={self.identity!r})"
