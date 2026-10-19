"""Finding the bearer token to present - one search, for every protocol.

``root://`` (the ``ztn`` mechanism), ``davs://`` and HTTP third-party copy all
ask the same question, and before 0.4.0 they answered it differently: the
HTTP side read the token file before ``$BEARER_TOKEN`` and never looked in
``$XDG_RUNTIME_DIR`` or ``/tmp``, so one shell could log in as one identity
over ``root://`` and another over ``davs://``. This module is now the only
answer. It follows the WLCG Bearer Token Discovery specification - what
``XrdSecztn``, ``htgettoken``, ``oidc-agent`` and the SciTokens libraries all
follow - after the two things written at the call site:

1. a token in the URL itself (``?authz=`` or ``?access_token=``, HTTP only:
   on ``root://`` such a CGI is the server's to interpret, not a login);
2. :attr:`Config.token <xrdclient.Config.token>`;
3. :attr:`Config.token_file <xrdclient.Config.token_file>`, when it was set
   explicitly - that is, to something other than ``$BEARER_TOKEN_FILE``,
   which is merely its default and is read at step 5 where the
   specification puts it;
4. ``$BEARER_TOKEN``;
5. the file ``$BEARER_TOKEN_FILE`` names;
6. ``$XDG_RUNTIME_DIR/bt_u<euid>``;
7. ``/tmp/bt_u<euid>``.

Steps 2 to 7 are :func:`xrdsec.credentials.find_bearer_token`, the public
form of the search, which takes the environment as an argument and is what
other tools call (:mod:`xrdsec.tokens` adds step 1 for them) -
``Config.token`` is its ``explicit`` and an explicitly set
``Config.token_file`` its ``token_file``. This module is the adapter
the protocols use: it reads those two off a :class:`~xrdclient.Config` and
adds step 1. Insecure files raise
:class:`~xrdclient.errors.InsecureCredentialError` (xrdcore's class) rather than being skipped,
and a symbolic link is refused at the two predictable locations (6 and 7) and
followed at a location the user named (3 and 5) - the reasons are in
:mod:`xrdsec.credentials`.

What stays here rather than in xrdsec is the :class:`~xrdclient.Config` and
:class:`~xrdclient.XRootDURL` shape, and :data:`TMP_DIR`: the one place both
this suite and xgfalclient's point step 7 at an empty directory.

``<euid>`` is the effective uid, ``id -u``, as the specification says.
"""

from __future__ import annotations

import os

from xrdsec import credentials, tokens
from xrdsec.tokens import TOKEN_FIELDS

from .config import Config
from .url import XRootDURL

__all__ = ["TOKEN_FIELDS", "discover_token", "explicit_token_file", "token_files", "url_token"]

#: Where the last-resort token file lives, for this Config-shaped search; a
#: module attribute so a test can point it at a directory of its own - both
#: this package's suite and xgfalclient's patch it to stay hermetic. The
#: public search reads :data:`xrdsec.credentials.TMP_DIR` instead.
TMP_DIR = "/tmp"


def url_token(url: XRootDURL) -> str | None:
    """The bearer token ``url`` carries in its own query (:data:`TOKEN_FIELDS`), if any."""
    return tokens.url_token(url.query)


def explicit_token_file(config: Config) -> str | None:
    """:attr:`Config.token_file`, if somebody set it rather than inheriting it.

    Its default is ``$BEARER_TOKEN_FILE``; left at that, it is the
    specification's step 5, which comes *after* ``$BEARER_TOKEN``. Set to
    anything else - in code, in a settings file, with ``--token-file`` - it
    was written for this call and outranks the ambient environment
    (:func:`xrdsec.tokens.explicit_token_file`).
    """
    return tokens.explicit_token_file(config.token_file)


def token_files(config: Config) -> list[tuple[str, bool]]:
    """Every file discovery may read, in order, each with whether it is predictable.

    :func:`xrdsec.credentials.bearer_token_files` for ``config``'s
    explicitly named file, with this module's :data:`TMP_DIR`.
    """
    return credentials._token_files(os.environ, os.geteuid(), explicit_token_file(config), TMP_DIR)


def discover_token(config: Config | None = None, url: XRootDURL | None = None) -> str | None:
    """The bearer token to present, or ``None`` if there is not one to be had.

    ``url`` is for HTTP, where a token may ride in the query (step 1);
    ``config`` supplies steps 2 and 3, and the environment the rest, through
    the one implementation in :mod:`xrdsec.credentials`. Raises
    :class:`~xrdclient.errors.InsecureCredentialError` for a token file
    that is there but not private to this user.
    """
    found = url_token(url) if url is not None else None
    if found:
        return found
    config = config or Config()
    return credentials._discover(
        os.environ, os.geteuid(), config.token, explicit_token_file(config), TMP_DIR
    )
