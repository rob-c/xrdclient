"""The authentication interface.

A :class:`Credential` is pure state: it turns the server's security trailer
into bytes and consumes challenges. It never does I/O at exchange time - any
file or keyring access happens in :meth:`available`, at construction - which
is what lets the same objects drive the blocking and the asyncio sessions.

The contract every mechanism keeps, and the ladder in :mod:`xrdclient.auth`
relies on:

* :meth:`Credential.available` returns ``None`` for "not configured here" and
  raises only for "configured, but broken" - an expired ticket, an oversized
  token - so the ladder can say *why* a mechanism was passed over.
* :meth:`Credential.missing` and :meth:`Credential.using` come as a pair: the
  first describes what a person could type, the second turns the answer into
  a :class:`~xrdclient.config.Config` that :meth:`available` will accept.
* ``repr()`` never shows secret material; it ends up in debug logs.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..config import Config
from .prompt import Ask

__all__ = ["Credential", "Offer", "parse_security_trailer"]

#: One ``&P=`` clause, up to the next ``&``. Anything in the trailer that is
#: not such a clause is skipped; a mechanism's parameters are comma-separated
#: and never hold ``&``.
_OFFER_RE = re.compile(r"&P=([^&]+)")


@dataclass(frozen=True, slots=True)
class Offer:
    """One ``&P=name,params`` clause of a ``kXR_login`` security trailer."""

    name: str
    params: str = ""

    def options(self) -> dict[str, str]:
        """``params`` split on commas into ``key:value`` pairs.

        Positional parameters without a colon - ``krb5``'s service principal -
        are left out, and a mechanism with a positional format of its own
        (``ztn``'s ``<expiry>:<maxtsz>:``) parses :attr:`params` itself.
        """
        out: dict[str, str] = {}
        for part in self.params.split(","):
            key, sep, value = part.partition(":")
            if sep:
                out[key.strip()] = value.strip()
        return out

    def __str__(self) -> str:
        """The clause as the server wrote it after ``&P=``, for messages."""
        return f"{self.name},{self.params}" if self.params else self.name


def parse_security_trailer(sec: str) -> list[Offer]:
    """Parse ``"&P=ztn,ver:1&P=unix"`` into ordered :class:`Offer` objects.

    Order is preserved because it is the server's preference, which the
    ladder falls back on for mechanisms ``auth_order`` does not rank. A
    trailer with no ``&P=`` clause yields ``[]``.
    """
    offers: list[Offer] = []
    for clause in _OFFER_RE.findall(sec):
        name, _, params = clause.partition(",")
        offers.append(Offer(name.strip(), params.strip()))
    return offers


class Credential(ABC):
    """One authentication mechanism, ready to run.

    A subclass is both a factory (the class methods, which may read files and
    the environment) and one login's state (the instance, which must not).
    Instances are made per login attempt and must not be shared between
    sessions: multi-round mechanisms such as ``gsi`` keep the exchange's secrets on
    ``self`` between :meth:`initial` and :meth:`step`.
    """

    #: Wire name, at most four bytes (``kXR_auth``'s ``credtype``).
    name: str = ""

    #: Key for ``kXR_sigver`` request signing, once the exchange establishes
    #: one. ``None`` means this mechanism does not sign.
    session_key: bytes | None = None

    @abstractmethod
    def initial(self) -> bytes:
        """The first ``kXR_auth`` credential blob."""

    def step(self, challenge: bytes) -> bytes | None:
        """Answer a ``kXR_authmore`` challenge.

        Returns the next blob, or ``None`` when the mechanism considers the
        exchange finished and the server should not have asked again.
        """
        return None

    @classmethod
    @abstractmethod
    def available(
        cls, offer: Offer, config: Config, *, username: str, host: str
    ) -> Credential | None:
        """Build a usable credential, or ``None`` if the material is absent.

        Must not raise for the ordinary "not configured" case - returning
        ``None`` lets the ladder fall through to the next mechanism.
        """

    @classmethod
    def missing(cls, offer: Offer, config: Config, *, username: str, host: str) -> Ask | None:
        """What a person could type to make this mechanism work, and why.

        Answers both questions the ladder needs: it goes into the error when
        nothing worked, and into the prompt when there is somebody to ask.
        ``None`` - the default - means there is nothing worth asking for:
        ``unix`` needs no material, and nobody can type a Kerberos ticket.
        """
        return None

    @classmethod
    def using(cls, answer: str, config: Config) -> Config:
        """A config that takes this mechanism's material from ``answer``.

        Only ever called with what :meth:`missing` asked for, so mechanisms
        that do not implement one need not implement the other.
        """
        raise NotImplementedError(f"{cls.name or cls.__name__} cannot be supplied by hand")

    def __repr__(self) -> str:
        """The class and wire name only - nothing a subclass adds, which may be secret."""
        return f"{type(self).__name__}(name={self.name!r})"
