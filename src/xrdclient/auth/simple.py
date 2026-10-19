"""``unix`` and ``host`` - the two mechanisms that carry no secret.

``unix`` asserts a username the server chooses to trust (typically only over
a private network or behind TLS); ``host`` asserts nothing at all and lets
the server authorise on the peer address.

Neither can fail, which is why they close the default ``auth_order``: they
are the rungs that always hold, so while a server offers either, the prompt
in :func:`xrdclient.auth.select` is never reached.
"""

from __future__ import annotations

from ..config import Config
from .base import Credential, Offer

__all__ = ["HostCredential", "UnixCredential"]


class UnixCredential(Credential):
    """``unix`` - ``"unix\\0<username>"``."""

    __slots__ = ("username",)
    name = "unix"

    def __init__(self, username: str) -> None:
        """Assert ``username``; nothing is checked here, the server decides."""
        self.username = username

    def initial(self) -> bytes:
        """The tag and the name, UTF-8; one round, so :meth:`step` is never needed."""
        return b"unix\x00" + self.username.encode("utf-8")

    @classmethod
    def available(
        cls, offer: Offer, config: Config, *, username: str, host: str
    ) -> UnixCredential | None:
        """Always usable: the name from the URL, else the configured one.

        ``config.username`` defaults to ``$XRD_USER``, ``$USER`` or the login
        name, so there is always something to assert.
        """
        return cls(username or config.username)

    def __repr__(self) -> str:
        """Shows the name: it is sent in the clear anyway."""
        return f"UnixCredential(username={self.username!r})"


class HostCredential(Credential):
    """``host`` - authorisation by peer address; the blob is just the tag."""

    __slots__ = ()
    name = "host"

    def initial(self) -> bytes:
        """Only the tag: the server has already seen the address it authorises."""
        return b"host\x00"

    @classmethod
    def available(cls, offer: Offer, config: Config, *, username: str, host: str) -> HostCredential:
        """Always usable; there is no material to look for."""
        return cls()
