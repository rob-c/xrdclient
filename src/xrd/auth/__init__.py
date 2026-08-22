"""Authentication mechanisms and the ladder that picks between them.

The server advertises what it accepts in the ``kXR_login`` security trailer;
:func:`select` intersects that with what this machine can actually produce,
ordered by :attr:`~xrd.config.Config.auth_order`. Every mechanism registers
unconditionally, because a zero-dependency install can genuinely attempt
``gsi``, ``ztn``, ``sss``, ``unix`` and ``host`` — all five are pure Python.

``krb5`` registers too, and reads your credential cache without help, but the
exchange needs :mod:`gssapi`. With no ticket and no module it stays quiet and
the ladder moves on; with a live ticket and no module it raises, so that
:func:`select` records *why* rather than falling through to ``unix`` with a
perfectly good ticket sitting there.

A mechanism that raises from ``available()`` is never fatal: :func:`select`
catches it, records the reason, and carries on to the next one.

When nothing at all is usable and there is a person at the terminal,
:func:`select` asks for what is missing rather than failing - see
:mod:`xrd.auth.prompt` for the rules, and ``Config(prompt=False)`` to have
none of it. With nobody there, the same explanation goes into the
:class:`~xrd.errors.NoMechanismError` instead.
"""

from __future__ import annotations

from collections.abc import Iterator

from .._log import get_logger
from ..config import Config
from ..errors import NoMechanismError
from . import prompt
from .base import Credential, Offer, parse_security_trailer
from .gsi import GSICredential
from .krb5 import KerberosCredential
from .prompt import Ask, Prompter, ask_on_terminal, forget
from .simple import HostCredential, UnixCredential
from .sss import SSSCredential
from .ztn import TokenCredential, discover_token

__all__ = [
    "Ask",
    "Credential",
    "Offer",
    "Prompter",
    "ask_on_terminal",
    "forget",
    "supply",
    "parse_security_trailer",
    "register",
    "registry",
    "require",
    "select",
    "UnixCredential",
    "HostCredential",
    "SSSCredential",
    "TokenCredential",
    "GSICredential",
    "KerberosCredential",
    "discover_token",
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
    :attr:`~xrd.config.Config.ztn_cleartext` opts a lab rig back in. ``None``
    means the caller cannot say, and nothing is withheld.

    If the whole ladder comes up empty and ``config`` allows prompting, one
    last pass asks a person for the missing material - a proxy path, a token
    - and yields whatever that produces.
    """
    config = config or Config()
    offers = _offers(sec)
    by_name = {o.name: o for o in offers}
    order = _offer_order(offers, by_name, config)
    why = {} if rejected is None else rejected
    _withhold_cleartext_token(by_name, order, why, tls, config)

    found = False
    for credential in _available_credentials(order, by_name, config, username, host, why):
        found = True
        yield credential

    # Only now, with nothing to offer the server, is it worth interrupting
    # somebody: a working ``unix`` fallback must never provoke a question.
    if not _should_prompt(found, config):
        return
    prompted = _first_prompted(order, by_name, config, username, host, why)
    if prompted is not None:
        yield prompted


def _offers(sec: str | list[Offer]) -> list[Offer]:
    return parse_security_trailer(sec) if isinstance(sec, str) else list(sec)


def _offer_order(offers: list[Offer], by_name: dict[str, Offer], config: Config) -> list[str]:
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
    if tls is not False or config.ztn_cleartext or "ztn" not in offers:
        return
    del offers["ztn"]
    order.remove("ztn")
    why["ztn"] = (
        "not offered on a cleartext connection - a bearer token is "
        "replayable by anyone on the path; use roots:// (or xroots://), "
        "or set ztn_cleartext=True on a rig you trust end to end"
    )


def _named_credential(
    name: str,
    offers: dict[str, Offer],
    config: Config,
    username: str,
    host: str,
    why: dict[str, str],
) -> Credential | None:
    cls = _REGISTRY.get(name)
    if cls is None:
        why[name] = "not supported by this client"
        return None
    return _build(cls, offers[name], config, username=username, host=host, why=why)


def _available_credentials(
    order: list[str],
    offers: dict[str, Offer],
    config: Config,
    username: str,
    host: str,
    why: dict[str, str],
) -> Iterator[Credential]:
    for name in order:
        credential = _named_credential(name, offers, config, username, host, why)
        if credential is not None:
            yield credential


def _should_prompt(found: bool, config: Config) -> bool:
    return not found and prompt.interactive(config)


def _prompted_credential(
    name: str,
    offers: dict[str, Offer],
    config: Config,
    username: str,
    host: str,
    why: dict[str, str],
) -> Credential | None:
    cls = _REGISTRY.get(name)
    if cls is None:
        return None
    return _ask(cls, offers[name], config, username=username, host=host, why=why)


def _first_prompted(
    order: list[str],
    offers: dict[str, Offer],
    config: Config,
    username: str,
    host: str,
    why: dict[str, str],
) -> Credential | None:
    for name in order:
        credential = _prompted_credential(name, offers, config, username, host, why)
        if credential is not None:
            return credential
    return None


def _build(
    cls: type[Credential],
    offer: Offer,
    config: Config,
    *,
    username: str,
    host: str,
    why: dict[str, str],
) -> Credential | None:
    """One rung of the ladder, with its rejection recorded in ``why``."""
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


def _ask(
    cls: type[Credential],
    offer: Offer,
    config: Config,
    *,
    username: str,
    host: str,
    why: dict[str, str],
) -> Credential | None:
    """Ask a person for this mechanism's material and build a credential from it.

    An answer that does not work is diagnosed by :meth:`~Credential.missing`
    all over again, so the second question says *why* the first answer was no
    good rather than repeating itself.
    """
    try:
        ask = cls.missing(offer, config, username=username, host=host)
    except Exception as exc:
        _log.debug("cannot say what %s wants: %s", cls.name, exc)
        return None
    if ask is None:
        return None
    for attempt in range(_ATTEMPTS):
        answer = prompt.answer_for(ask, config)
        if not answer:
            return None
        trial = cls.using(answer, config)
        cred = _build(cls, offer, trial, username=username, host=host, why=why)
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
