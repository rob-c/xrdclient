"""Authentication mechanisms and the ladder that picks between them.

The server advertises what it accepts in the ``kXR_login`` security trailer;
:func:`select` intersects that with what this machine can actually produce,
ordered by :attr:`~xrdclient.config.Config.auth_order`. Every mechanism registers
unconditionally: the default installation can attempt ``gsi``, ``ztn``,
``sss``, ``unix`` and ``host`` without an authentication-specific extra.

Portable ``krb5`` reads the credential cache, asks the KDC for a service ticket
when the cache holds only a TGT, and builds the AP-REQ itself (xrdsec's
pure-Python Kerberos client, :mod:`xrdsec.krb5.client`). The optional
python-gssapi binding (:mod:`xrdsec.krb5.gss`) adds the cache types only the
platform library reaches. With no ticket it stays quiet and the ladder
moves on; with one that has expired it raises, so that :func:`select` records
*why* rather than falling through to ``unix`` and leaving the cause unsaid.

A mechanism that raises from ``available()`` is never fatal: :func:`select`
catches it, records the reason, and carries on to the next one.

When nothing at all is usable and there is a person at the terminal,
:func:`select` asks for what is missing rather than failing - see
:mod:`xrdclient.auth.prompt` for the rules, and ``Config(prompt=False)`` to have
none of it. With nobody there, the same explanation goes into the
:class:`~xrdclient.errors.NoMechanismError` instead.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass

from xrdsec.krb5.ccache import ccache_name, resolve_ccache

from .._gridpaths import default_proxy_path
from ..config import Config
from ..errors import CredentialError, NoMechanismError
from ..log import get_logger
from . import prompt
from .base import Credential, Offer, parse_security_trailer
from .gsi import GSICredential
from .krb5 import KerberosCredential
from .prompt import Ask, Prompter, ask_on_terminal, forget
from .simple import HostCredential, UnixCredential
from .sss import SSSCredential, default_keytab_path
from .ztn import TokenCredential, _token_paths, discover_token

__all__ = [
    "AMBIENT_ENV",
    "Ask",
    "Credential",
    "GSICredential",
    "HostCredential",
    "KerberosCredential",
    "Offer",
    "Prompter",
    "SSSCredential",
    "TokenCredential",
    "UnixCredential",
    "ask_on_terminal",
    "credential_files",
    "discover_token",
    "forget",
    "parse_security_trailer",
    "register",
    "registry",
    "require",
    "select",
    "supply",
]

_log = get_logger(__name__)

_REGISTRY: dict[str, type[Credential]] = {}


def register(cls: type[Credential]) -> type[Credential]:
    """Add a mechanism to the registry, keyed on its wire name."""
    _REGISTRY[cls.name] = cls
    return cls


def registry() -> dict[str, type[Credential]]:
    """Every mechanism this installation can attempt."""
    return dict(_REGISTRY)


for _cls in (
    UnixCredential,
    HostCredential,
    SSSCredential,
    TokenCredential,
    GSICredential,
    KerberosCredential,
):
    register(_cls)


#: The environment variables a mechanism consults when it logs in, rather
#: than when its :class:`~xrdclient.config.Config` is made: which token, which
#: proxy, keytab or Kerberos cache is used is decided by these at that moment.
AMBIENT_ENV = (
    "BEARER_TOKEN",
    "BEARER_TOKEN_FILE",
    "XDG_RUNTIME_DIR",
    "X509_USER_PROXY",
    "XrdSecSSSKT",
    "XrdSecsssKT",
    "KRB5CCNAME",
    "KRB5_CONFIG",
)


def credential_files(config: Config) -> tuple[str, ...]:
    """The files the mechanisms in ``auth_order`` would read a credential from.

    What a login would find there is part of who it logs in as: a token file
    rewritten in place, or a renewed proxy, is a different credential under
    the same name. An explicit ``token`` makes the token files irrelevant,
    and a ``proxy`` - or a separate ``usercert`` and ``userkey`` - is read for
    TLS client authentication whatever the order.
    """
    order = set(config.auth_order)
    files: list[str] = []
    if "ztn" in order and not config.token:
        files += _token_paths(config)
    files += _x509_files(config, order)
    if "sss" in order:
        files.append(default_keytab_path(config))
    if "krb5" in order:
        files += _ccache_files()
    return tuple(files)


def _x509_files(config: Config, order: set[str]) -> list[str]:
    """The proxy, and with none named the user certificate and key, TLS and GSI present."""
    files = [default_proxy_path(config)] if "gsi" in order or config.proxy else []
    if config.usercert and config.userkey and not config.proxy:
        files += [config.usercert, config.userkey]
    return files


def _ccache_files() -> list[str]:
    """The default Kerberos cache, and a ``DIR:`` collection's pointer to it."""
    name = ccache_name()
    try:
        path = resolve_ccache(name)
    except CredentialError:
        return []  # not a file: nothing here this client could log in with
    kind, _, rest = name.partition(":")
    if kind == "DIR" and not rest.startswith(":"):
        return [os.path.join(rest, "primary"), path]
    return [path]


def select(
    sec: str | list[Offer],
    config: Config | None = None,
    *,
    username: str = "",
    host: str = "",
    rejected: dict[str, str] | None = None,
    tls: bool | None = None,
) -> Iterator[Credential]:
    """Yield usable credentials, most preferred first.

    Iteration is lazy: building a GSI proxy costs a file read and a parse, so
    it only happens if the mechanisms ahead of it were rejected. Pass a dict
    as ``rejected`` to collect why each skipped mechanism was unusable.

    ``tls`` is whether the connection these credentials will travel on is
    encrypted. On a cleartext one - ``tls=False`` - the ``ztn`` rung is
    skipped, because a bearer token is a password to anyone who can read the
    wire; that is what the stock client does, and
    :attr:`~xrdclient.config.Config.ztn_cleartext` opts a lab rig back in. ``None``
    means the caller cannot say, and nothing is withheld.

    If the whole ladder comes up empty and ``config`` allows prompting, one
    last pass asks a person for the missing material - a proxy path, a token
    - and yields whatever that produces.
    """
    config = config or Config()
    offers = _offers(sec)
    by_name = {o.name: o for o in offers}
    order = _offer_order(offers, by_name, config)
    # The caller's dict, when given, is filled in place: ``require`` and the
    # login state machine read it after the generator is exhausted.
    why = {} if rejected is None else rejected
    _withhold_cleartext_token(by_name, order, why, tls, config)
    ladder = _Ladder(by_name, config, username, host, why)

    found = False
    for credential in _available_credentials(ladder, order):
        found = True
        yield credential

    # Only now, with nothing to offer the server, is it worth interrupting
    # somebody: a working ``unix`` fallback must never provoke a question.
    if not _should_prompt(found, config):
        return
    prompted = _first_prompted(ladder, order)
    if prompted is not None:
        yield prompted


@dataclass(frozen=True, slots=True)
class _Ladder:
    """What one :func:`select` call climbs with, shared by every rung.

    The fields never change during a selection - ``why`` is the one mutable
    member, and it is mutated in place so that the caller's ``rejected``
    dict sees every rung's verdict. Bundling them keeps the per-rung helpers
    to the two things that actually vary: which mechanism, and which config
    (a prompted answer is tried against a *derived* config, not this one).
    """

    #: The server's offers that survived filtering, by mechanism name.
    offers: dict[str, Offer]
    #: The configuration the selection started from.
    config: Config
    #: The user name to log in as; mechanisms that do not name one ignore it.
    username: str
    #: The server's host name, for host-bound material (Kerberos principal, sss).
    host: str
    #: Mechanism name -> why it was unusable; filled as the ladder is climbed.
    why: dict[str, str]


def _offers(sec: str | list[Offer]) -> list[Offer]:
    """The offers in a raw ``&P=`` trailer, or a copy of an already parsed list."""
    return parse_security_trailer(sec) if isinstance(sec, str) else list(sec)


def _offer_order(offers: list[Offer], by_name: dict[str, Offer], config: Config) -> list[str]:
    """The order to try the offered mechanisms in.

    The client's ``auth_order`` wins, as with ``XrdSecPROTOCOL``; offers it
    does not name follow in the server's own order, so a mechanism the user
    never ranked is still tried rather than silently dropped.
    """
    order = [name for name in config.auth_order if name in by_name]
    order.extend(offer.name for offer in offers if offer.name not in order)
    return order


def _withhold_cleartext_token(
    offers: dict[str, Offer],
    order: list[str],
    why: dict[str, str],
    tls: bool | None,
    config: Config,
) -> None:
    """Drop the ``ztn`` rung from a cleartext connection, saying why.

    Both ``offers`` and ``order`` are edited in place so the rest of the
    selection never sees the token rung; the explanation goes into ``why`` so
    a :class:`~xrdclient.errors.NoMechanismError` names the real cause rather
    than "no token found" when there was one.
    """
    if tls is not False or config.ztn_cleartext or "ztn" not in offers:
        return
    del offers["ztn"]
    order.remove("ztn")
    why["ztn"] = (
        "not offered on a cleartext connection - a bearer token is "
        "replayable by anyone on the path; use roots:// (or xroots://), "
        "or set ztn_cleartext=True on a rig you trust end to end"
    )


def _named_credential(ladder: _Ladder, name: str) -> Credential | None:
    """The credential for one offered mechanism, or ``None`` with the reason recorded.

    A mechanism the server offers but this client has no class for (``pwd``,
    say) is recorded rather than skipped silently, so the error lists it.
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        ladder.why[name] = "not supported by this client"
        return None
    return _build(ladder, cls, ladder.offers[name], ladder.config)


def _available_credentials(ladder: _Ladder, order: list[str]) -> Iterator[Credential]:
    """Every usable credential, in ``order``, built only as the caller asks for it."""
    for name in order:
        credential = _named_credential(ladder, name)
        if credential is not None:
            yield credential


def _should_prompt(found: bool, config: Config) -> bool:
    """Whether to fall back to asking a person: only when nothing else worked."""
    return not found and prompt.interactive(config)


def _prompted_credential(ladder: _Ladder, name: str) -> Credential | None:
    """A credential built from a person's answer for one mechanism, if any.

    Unsupported mechanisms were already explained by :func:`_named_credential`
    on the first pass, so here they are passed over without a second note.
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        return None
    return _ask(ladder, cls, ladder.offers[name])


def _first_prompted(ladder: _Ladder, order: list[str]) -> Credential | None:
    """The first mechanism, in ``order``, that a prompted answer makes usable.

    Only one is wanted: a person asked for a proxy path and then a token for
    the same login would rightly think the client was confused.
    """
    for name in order:
        credential = _prompted_credential(ladder, name)
        if credential is not None:
            return credential
    return None


def _build(
    ladder: _Ladder, cls: type[Credential], offer: Offer, config: Config
) -> Credential | None:
    """One rung of the ladder, with its rejection recorded in ``ladder.why``.

    ``config`` is passed separately from ``ladder.config`` because a prompted
    answer is tried against a derived config (:meth:`Credential.using`). A
    success clears any earlier verdict for the mechanism, so a prompt that
    fixed the problem does not leave a stale reason in ``rejected``.
    """
    why, username, host = ladder.why, ladder.username, ladder.host
    try:
        cred = cls.available(offer, config, username=username, host=host)
    except Exception as exc:  # a broken credential must not mask the rest
        why[cls.name] = f"{type(exc).__name__}: {exc}"
        _log.debug("%s unusable: %s", cls.name, exc)
        return None
    if cred is None:
        why[cls.name] = _why_not(cls, offer, config, username=username, host=host)
        return None
    why.pop(cls.name, None)
    return cred


def _why_not(
    cls: type[Credential], offer: Offer, config: Config, *, username: str, host: str
) -> str:
    """The mechanism's own account of what is absent, for the error message.

    This is what turns "no usable authentication mechanism" into something
    actionable in a log nobody is watching - the same sentence a prompt would
    have opened with, and the fix that goes with it.
    """
    try:
        ask = cls.missing(offer, config, username=username, host=host)
    except Exception as exc:  # diagnosing the problem must not become one
        return f"no credential material found ({type(exc).__name__}: {exc})"
    return f"{ask.reason}; try: {ask.hint}" if ask is not None else "no credential material found"


#: One answer, and one more for the typo in it.
_ATTEMPTS = 2


def _ask(ladder: _Ladder, cls: type[Credential], offer: Offer) -> Credential | None:
    """Ask a person for this mechanism's material and build a credential from it.

    An answer that does not work is diagnosed by :meth:`~Credential.missing`
    all over again, so the second question says *why* the first answer was no
    good rather than repeating itself.
    """
    username, host = ladder.username, ladder.host
    try:
        ask = cls.missing(offer, ladder.config, username=username, host=host)
    except Exception as exc:
        _log.debug("cannot say what %s wants: %s", cls.name, exc)
        return None
    if ask is None:
        return None
    for attempt in range(_ATTEMPTS):
        answer = prompt.answer_for(ask, ladder.config)
        if not answer:
            return None
        trial = cls.using(answer, ladder.config)
        cred = _build(ladder, cls, offer, trial)
        if cred is not None:
            return cred
        prompt.forget(ask)  # a wrong answer is not worth remembering
        if attempt + 1 < _ATTEMPTS:
            again = cls.missing(offer, trial, username=username, host=host)
            if again is None:
                break
            ask = again
    prompt.remember(ask, None)  # asked twice and still nothing: stop asking
    return None


def supply(name: str, config: Config, *, host: str = "") -> Config | None:
    """Ask for one mechanism's material and give back a config that uses it.

        >>> config = supply("ztn", config, host="dav.example.org") or config

    This is the door in for surfaces with no security trailer to select from
    - HTTP has a ``401`` and a ``WWW-Authenticate`` header instead. ``None``
    means there was nothing to ask, nobody to ask, or no answer.
    """
    cls = _REGISTRY.get(name)
    if cls is None or not prompt.interactive(config):
        return None
    try:
        ask = cls.missing(Offer(name), config, username=config.username, host=host)
    except Exception as exc:
        _log.debug("cannot say what %s wants: %s", name, exc)
        return None
    if ask is None:
        return None
    answer = prompt.answer_for(ask, config)
    return cls.using(answer, config) if answer else None


def require(
    sec: str | list[Offer],
    config: Config | None = None,
    *,
    username: str = "",
    host: str = "",
) -> list[Credential]:
    """Like :func:`select`, but raises when nothing is usable."""
    rejected: dict[str, str] = {}
    creds = list(select(sec, config, username=username, host=host, rejected=rejected))
    if not creds:
        offers = parse_security_trailer(sec) if isinstance(sec, str) else list(sec)
        raise NoMechanismError(offered=[o.name for o in offers], tried=rejected)
    return creds
