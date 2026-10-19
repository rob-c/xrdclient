"""A separate user certificate and key: ``Config.usercert`` and ``Config.userkey``.

A proxy is a certificate and its key in one file, and everything that
presents one - GSI, HTTPS client authentication - takes that one file. A user
who has only ``usercert.pem`` and ``userkey.pem`` (a service account, a
robot certificate, a CI job) used to have to splice them into one file
first, and a grid tool doing that for them wrote the *private key* into a
temporary file that stayed behind whenever it crashed. The pair is presented
as it is:

* **HTTPS / ``roots://``.** The certificate goes to OpenSSL by name; the key
  is read through the private-file checks of
  :func:`~xrdsec.credentials.read_private_file` (ours, ``0600`` or tighter, a
  regular file) and handed to OpenSSL through a pipe - the bytes that were
  checked are the bytes used, and none of them touch the disk. An encrypted
  key is decrypted by OpenSSL with the pass phrase from :func:`pass_phrase`.
* **GSI.** The key is read the same way and signs a short-lived proxy made
  in memory (:func:`xrdsec.usercred.memory_proxy`) - what ``XrdSecgsi``'s
  ``createpxy`` does, without the file.

Which credential wins is gfal2's order: a proxy somebody named
(:attr:`Config.proxy <xrdclient.Config.proxy>`, ``$X509_USER_PROXY``), then
this pair when both halves are set, then the default proxy location.

Since 0.4.0 the mechanism - the pipe, the in-memory proxy, the encrypted-key
test - is :mod:`xrdsec.usercred`, shared with xgfalclient and xrdarc, and is
called there (the pre-0.4.0 re-exports ``encrypted``, ``load_memory``,
``load_pair`` and ``password_supplier`` are gone). What stays in xrdclient
is the :class:`~xrdclient.Config` shape: :func:`wanted`, :func:`present` and
:func:`gsi_credential` read their paths from a ``Config``, and
:func:`pass_phrase` knows where *this* client gets a pass phrase -
:attr:`Config.key_password <xrdclient.Config.key_password>`, else a person
asked through :mod:`xrdclient.auth.prompt`. A test that wants another
``/dev/fd`` patches :data:`xrdsec.usercred._FD_DIR`, which the pipe reads.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from xrdsec import usercred
from xrdsec.usercred import KIND

from .errors import CredentialError

if TYPE_CHECKING:
    import ssl

    from xrdsec.x509 import ProxyCredential

    from .config import Config

__all__ = ["gsi_credential", "pass_phrase", "present", "wanted"]


def wanted(config: Config) -> bool:
    """Whether ``config`` presents its user certificate and key rather than a proxy."""
    return usercred.wanted(config.usercert, config.userkey, config.proxy)


def pass_phrase(config: Config, path: str) -> bytes:
    """The pass phrase for the encrypted key at ``path``.

    :attr:`Config.key_password <xrdclient.Config.key_password>` when it is
    set - a callable, so that a GUI, a keyring or a secrets manager can be
    asked at the moment it is needed and nothing sits in the configuration.
    Otherwise a person is asked, through the same prompter as for any
    missing credential (:mod:`xrdclient.auth.prompt`), if there is somebody
    to ask. Raises :class:`~xrdclient.errors.CredentialError` when there is
    no answer, naming the two ways this client can be given one (xrdsec's
    own refusal could only say "supply one").
    """
    answer: str | bytes | None
    if config.key_password is not None:
        answer = config.key_password()
    else:
        from .auth.prompt import Ask, answer_for, interactive

        answer = None
        if interactive(config):
            ask = Ask(
                mechanism="x509",
                what="the pass phrase of your private key",
                reason=f"{path} is encrypted",
                hint="set Config(key_password=...), or decrypt the key with openssl",
                prompt=f"pass phrase for {path}",
                host=path,
                secret=True,
            )
            answer = answer_for(ask, config)
    if not answer:
        raise CredentialError(
            f"the {KIND} {path} is encrypted and no pass phrase was given: set "
            "Config(key_password=...), or run where there is a terminal to ask on"
        )
    return answer.encode("utf-8") if isinstance(answer, str) else answer


def _paths(config: Config) -> tuple[str, str]:
    """``config``'s certificate and key paths, ``~``-expanded."""
    return os.path.expanduser(str(config.usercert)), os.path.expanduser(str(config.userkey))


def present(ctx: ssl.SSLContext, config: Config) -> None:
    """Load ``config``'s user certificate and key into ``ctx``, for TLS client authentication.

    :func:`xrdsec.usercred.present` with the pass phrase from
    :func:`pass_phrase` - asked for only if the key turns out to be
    encrypted.
    """
    certificate, key = _paths(config)
    usercred.present(ctx, certificate, key, password=lambda: pass_phrase(config, key))


def gsi_credential(config: Config) -> ProxyCredential:
    """A proxy for GSI, made in memory from the user certificate and key.

    :func:`xrdsec.usercred.memory_proxy`: raises
    :class:`~xrdclient.errors.InsecureCredentialError` for a key others may
    read, as :func:`~xrdclient.auth.gsi.read_proxy` does for a proxy, and
    :class:`ValueError` for files that are not a certificate and its key.
    """
    certificate, key = _paths(config)
    return usercred.memory_proxy(certificate, key, password=lambda: pass_phrase(config, key))
