"""The authentication interface.

A :class:`Credential` is pure state: it turns the server's security trailer
into bytes and consumes challenges. It never does I/O at exchange time - any
file or keyring access happens in :meth:`available`, at construction - which
is what lets the same objects drive the blocking and the asyncio sessions.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .._compat import SLOTS
from ..config import Config
from .prompt import Ask

__all__ = ["Credential", "Offer", "parse_security_trailer"]

_OFFER_RE = re.compile(r"&P=([^&]+)")


@dataclass(frozen=True, **SLOTS)
class Offer:
    """One ``&P=name,params`` clause of a ``kXR_login`` security trailer."""

    name: str
    params: str = ""

    def options(self) -> dict[str, str]:
        """``params`` split on commas into ``key:value`` pairs."""
        out: dict[str, str] = {}
        for part in self.params.split(","):
            key, sep, value = part.partition(":")
            if sep:
                out[key.strip()] = value.strip()
        return out

    def __str__(self) -> str:
        return f"{self.name},{self.params}" if self.params else self.name


def parse_security_trailer(sec: str) -> list[Offer]:
    """Parse ``"&P=ztn,ver:1&P=unix"`` into ordered :class:`Offer` objects."""
    offers: list[Offer] = []
    for clause in _OFFER_RE.findall(sec):
        name, _, params = clause.partition(",")
        offers.append(Offer(name.strip(), params.strip()))
    return offers


class Credential(ABC):
    """One authentication mechanism, ready to run."""

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
        return f"{type(self).__name__}(name={self.name!r})"
