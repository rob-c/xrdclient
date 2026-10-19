"""Opening a credential cache by name, and xrdclient's choice of Kerberos implementation.

The cache types - ``FILE:``/``DIR:`` files, ``KCM:`` daemons, ``KEYRING:``
kernel keyrings - are :mod:`xrdsec.krb5.caches`'; the names below are that
module's objects (``open_ccache is xrdsec.krb5.caches.open_ccache``), kept
under the name xrdclient published them by.

What is xrdclient's own is :func:`backend`: ``$XRD_KRB5_BACKEND`` says
which implementation a ``krb5`` login uses. ``auto`` (the default) lets
:class:`~xrdclient.auth.krb5.KerberosCredential` ask the system library
through python-gssapi (:mod:`xrdsec.krb5.gss`) first and fall back to the
portable reader (:mod:`xrdsec.krb5.client`); ``portable`` and ``gssapi``
each use one alone. (``native``, which read the cache through pykrb5, was
removed in 0.4.0: python-gssapi reaches every cache type pykrb5 does, and
does the TGS exchange as well.)
"""

from __future__ import annotations

import os

from xrdsec.krb5.caches import CredentialCache as CredentialCache
from xrdsec.krb5.caches import FileCache as FileCache
from xrdsec.krb5.caches import open_ccache as open_ccache

from ...errors import CredentialError

__all__ = ["BACKENDS", "CredentialCache", "FileCache", "backend", "open_ccache"]

#: What ``$XRD_KRB5_BACKEND`` may say.
BACKENDS = ("auto", "portable", "gssapi")


def backend() -> str:
    """``$XRD_KRB5_BACKEND``, checked: ``auto`` when it is unset or blank.

    Anything not in :data:`BACKENDS` is a
    :class:`~xrdclient.errors.CredentialError`, since a typo here would
    otherwise silently choose an implementation nobody asked for; the
    removed ``native`` is told what replaced it.
    """
    choice = os.environ.get("XRD_KRB5_BACKEND", "").strip().lower() or "auto"
    if choice == "native":
        raise CredentialError(
            "XRD_KRB5_BACKEND=native (pykrb5) was removed in 0.4.0: install "
            "python-gssapi ('xrdclient[krb5]') and leave XRD_KRB5_BACKEND unset, "
            "or set it to 'gssapi'."
        )
    if choice not in BACKENDS:
        raise CredentialError(
            f"Unknown Kerberos backend {choice!r}. "
            "Set XRD_KRB5_BACKEND to 'auto', 'gssapi' or 'portable'."
        )
    return choice
