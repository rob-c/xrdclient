"""Opening a credential cache by name, whatever holds it.

``FILE:`` and ``DIR:`` caches are files (:mod:`.ccache`); ``KCM:`` caches
live in a daemon reached over a Unix socket (:mod:`.kcm`); ``KEYRING:``
caches live in the Linux kernel (:mod:`.keyring`). Each opens as a
:class:`CredentialCache`: a name to put in messages, :meth:`~CredentialCache.read`
for the default principal and the tickets, and
:meth:`~CredentialCache.store` for a ticket fetched from the KDC - which only
a KCM cache takes, as MIT's library does; for the others it stays in this
process's memory.

macOS's own ``API:`` caches are held by Heimdal's credential service behind
XPC, and ``MEMORY:`` and ``MSLSA:`` caches by another process or Windows.
The portable reader explains how to get a FILE cache instead. Selecting
``XRD_KRB5_BACKEND=native`` delegates supported cache types to optional pykrb5.
"""

from __future__ import annotations

import os
from typing import Protocol

from ...errors import CredentialError
from .ccache import ccache_name, read_ccache, resolve_ccache
from .kcm import DEFAULT_SOCKET, KcmCache
from .keyring import Keyctl, KeyringCache, system_keyctl
from .model import Principal, Ticket
from .profile import Profile

__all__ = ["CredentialCache", "FileCache", "open_ccache"]

_TO_A_FILE = "KRB5CCNAME=FILE:/tmp/krb5cc_$(id -u) kinit"


class CredentialCache(Protocol):
    """A cache a login reads its tickets from."""

    @property
    def name(self) -> str: ...

    def read(self) -> tuple[Principal, list[Ticket]]:
        """The default principal and every ticket; :class:`FileNotFoundError` for no cache."""
        ...

    def store(self, ticket: Ticket) -> bool:
        """Keep a ticket fetched from the KDC in the cache; ``False`` if it was not."""
        ...


class FileCache:
    """A FILE cache; :attr:`name` is its path, as messages have always named it."""

    __slots__ = ("path",)

    def __init__(self, path: str) -> None:
        self.path = path

    @property
    def name(self) -> str:
        return self.path

    def read(self) -> tuple[Principal, list[Ticket]]:
        return read_ccache(self.path)

    def store(self, ticket: Ticket) -> bool:
        """Not done: rewriting a file ``kinit`` owns would race with it."""
        return False


def _refuse_api(name: str) -> CredentialError:
    return CredentialError(
        f"Kerberos credential cache {name!r} is a macOS API: cache, held by the system's "
        "Heimdal credential service, which the portable cache reader cannot reach. "
        "Install 'xrdclient[krb5]' and set XRD_KRB5_BACKEND=native, "
        f"or get a ticket into a file with: {_TO_A_FILE}"
    )


def open_ccache(
    name: str | None = None, profile: Profile | None = None, *, keyctl: Keyctl | None = None
) -> CredentialCache:
    """The cache ``name`` names - by default the one MIT would use.

    ``keyctl`` replaces the kernel for a ``KEYRING:`` cache, for tests.
    Raises :class:`~xrdclient.errors.CredentialError` for a cache type that
    cannot be read here, naming the fix.
    """
    choice = os.environ.get("XRD_KRB5_BACKEND", "portable")
    if choice == "native":
        from .native import NativeCache

        return NativeCache(name or os.environ.get("KRB5CCNAME") or None)
    if choice != "portable":
        raise CredentialError(
            f"Unknown Kerberos cache backend {choice!r}. "
            "Set XRD_KRB5_BACKEND to 'native' or 'portable'."
        )
    return _portable_ccache(name, profile, keyctl)


def _portable_ccache(
    name: str | None, profile: Profile | None, keyctl: Keyctl | None
) -> CredentialCache:
    name = name if name is not None else ccache_name(profile)
    kind, sep, rest = name.partition(":")
    if sep and kind == "KCM":
        return _open_kcm(rest, profile)
    if sep and kind == "KEYRING":
        return KeyringCache(rest, keyctl or system_keyctl())
    if sep and kind == "API":
        raise _refuse_api(name)
    return FileCache(resolve_ccache(name, profile))


def _open_kcm(residual: str, profile: Profile | None) -> KcmCache:
    """``kcm_socket`` from ``[libdefaults]``, else Heimdal's default path."""
    configured = (profile or Profile.load()).libdefault("kcm_socket")
    return KcmCache(residual, configured or DEFAULT_SOCKET)
