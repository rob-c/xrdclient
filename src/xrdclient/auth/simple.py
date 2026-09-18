"""``unix`` and ``host`` - the two mechanisms that carry no secret.

``unix`` asserts a username the server chooses to trust (typically only over
a private network or behind TLS); ``host`` asserts nothing at all and lets
the server authorise on the peer address.
"""

from __future__ import annotations

from ..config import Config
from .base import Credential, Offer

__all__ = ["UnixCredential", "HostCredential"]


class UnixCredential(Credential):
    """``unix`` - ``"unix\\0<username>"``."""

    __slots__ = ("username",)
    name = "unix"

    def __init__(self, username: str) -> None:
        self.username = username

    def initial(self) -> bytes:
        return b"unix\x00" + self.username.encode("utf-8")

    @classmethod
    def available(
        cls, offer: Offer, config: Config, *, username: str, host: str
    ) -> UnixCredential | None:
        return cls(username or config.username)

    def __repr__(self) -> str:
        return f"UnixCredential(username={self.username!r})"


class HostCredential(Credential):
    """``host`` - authorisation by peer address; the blob is just the tag."""

    __slots__ = ()
    name = "host"

    def initial(self) -> bytes:
        return b"host\x00"

    @classmethod
    def available(
        cls, offer: Offer, config: Config, *, username: str, host: str
    ) -> HostCredential:
        return cls()
