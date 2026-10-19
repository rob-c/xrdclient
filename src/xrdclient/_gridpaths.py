"""The proxy path a :class:`~xrdclient.Config` names, else the one the environment names.

The lookups themselves - the CA directory, the vomsdir, the proxy path - are
:mod:`xrdsec.credentials` (which ``xrdclient.credentials`` is, since 0.4.0),
public and taking the environment as an argument; the package's own modules
call them there. What stays here is the one lookup whose answer depends on a
:class:`~xrdclient.Config`: the GSI login, ``tls_context(config)`` and
:func:`xrdclient.crypto.x509.default_proxy_path` all need "the proxy this
configuration names, else the one the environment names", and a ``Config`` is
xrdclient's type, which xrdsec cannot know.

The pre-0.4.0 re-exports this module carried (``grid_ca_path``,
``grid_voms_dir``, ``default_ca_path``, ``DEFAULT_CA_PATH`` and the
two-argument ``grid_directory``) were removed in 0.4.0; they are
:mod:`xrdsec.credentials`' functions of those names (``grid_directory`` is
``grid_ca_path``/``grid_voms_dir`` with the variable chosen for you).
"""

from __future__ import annotations

from xrdsec import credentials

__all__ = ["default_proxy_path"]


def default_proxy_path(config: object = None) -> str:
    """``config.proxy``, else :func:`xrdsec.credentials.default_proxy_path`.

    ``config`` is anything with a ``proxy`` attribute (a
    :class:`~xrdclient.Config`), so a proxy set in code or a settings file
    outranks the environment, as it does everywhere else in the library.
    """
    configured = getattr(config, "proxy", None)
    if configured:
        return str(configured)
    return credentials.default_proxy_path()
