"""``ztn`` - WLCG bearer tokens and SciTokens.

Discovery follows the WLCG Bearer Token Discovery specification, which is
also what the C client uses: an explicit token (or an explicitly named token
file), then ``$BEARER_TOKEN``, then ``$BEARER_TOKEN_FILE``, then
``$XDG_RUNTIME_DIR/bt_u$UID``, then ``/tmp/bt_u$UID``. The search itself is
:mod:`xrdclient._tokens`, shared with ``davs://`` so both protocols find
the same token; it and the unverified claim reading are xrdsec's
(:mod:`xrdsec.credentials`, :mod:`xrdsec.tokens`) since 0.4.0. What is
XRootD's own, and stays here, is the protocol: the server's
``<expiry>:<maxtsz>:`` offer and the byte-frozen ``TokenResp`` framing.

The token is a bearer credential - whoever holds it is its subject - so
:func:`xrdclient.auth.select` withholds this mechanism on a cleartext
connection unless ``ztn_cleartext`` says otherwise, and ``repr()`` here
never shows it. The client never verifies a token: it reads the claims
only to fail early, with a readable message, on one the server would refuse.
"""

from __future__ import annotations

import os
import struct
import time

from xrdsec import tokens

from .._tokens import discover_token, token_files
from ..config import Config
from ..errors import CredentialError, TokenExpiredError
from .base import Credential, Offer
from .prompt import Ask

__all__ = ["TokenCredential", "discover_token", "token_claims", "token_expiry"]


def _token_paths(config: Config) -> list[str]:
    """Every file :func:`discover_token` may read, in the order it reads them."""
    return [path for path, _predictable in token_files(config)]


def token_claims(token: str) -> dict[str, object]:
    """Decode a JWT's claim set without verifying it.

    Signature verification is the server's job; the client only reads the
    expiry so it can fail fast with a useful message instead of a 3010. This
    is deliberately not an authentication or signature check, and it does
    not decode the signature either: expiry inspection must not depend on it.

    :func:`xrdsec.tokens.token_claims` with ``padding=True``: the strict
    decoding is shared (canonical base64url, JSON objects, no RFC 7797
    detached payload), and this client also takes the whole-quantum ``=``
    padding some issuers keep, as it always has. Anything that is not such
    a compact JWS - an opaque token, a malformed segment - has no claims,
    and ``{}`` says so.
    """
    return tokens.token_claims(token, padding=True)


def token_expiry(token: str) -> float | None:
    """The ``exp`` claim as a UNIX timestamp, if the token carries one in whole seconds.

    :func:`xrdsec.tokens.token_expiry`, read the way :func:`token_claims` reads.
    """
    return tokens.token_expiry(token, padding=True)


def _requirements(params: str) -> tuple[int, int]:
    """The server's ``<expiry>:<maxtsz>:`` offer parameters, zero when unstated.

    The reference server sends a decimal minimum remaining lifetime in seconds
    (0 for no requirement) and a decimal maximum token size it will accept,
    each closed by a colon. Anything else here - old servers sent version
    strings - is read as no requirement rather than refused: the parameters
    only exist to fail sooner than the server would.
    """
    lifetime, _, rest = params.partition(":")
    largest, _, _ = rest.partition(":")
    try:
        return int(lifetime), int(largest)
    except ValueError:
        return 0, 0


#: ``TokenHdr`` (8 bytes) plus the ``uint16`` length in front of the token.
#: Not used by the framing below, which spells the layout out; it documents
#: the offset at which the reference server starts reading the token.
_PREFIX_LEN = 10
_VERSION = 0
_IS_TKN = ord("T")  # ``TokenHdr::IsTkn`` - "here is a token"


def _token_resp(token: bytes) -> bytes:
    """Frame a token as the ``XrdSecProtocolztn::TokenResp`` stock reads.

    The layout is byte-frozen in the reference implementation::

        off 0..3  id[4]   = "ztn\\0"        (the protocol id, NUL-terminated)
        off 4     ver     = 0
        off 5     opr     = 'T'            (TokenHdr::IsTkn)
        off 6..7  rsvd[2] = 0, 0
        off 8..9  len     = tsz + 1        (big-endian; counts the NUL)
        off 10..  token + one NUL byte

    Stock ``Authenticate`` routes on ``opr`` at offset 5 and reads the token at
    offset 10, requiring the byte at ``10 + len - 1`` to be NUL. A bare
    ``"ztn\\0" + token`` - which our own parser would take - puts a token
    character where ``opr`` belongs and is refused with "Invalid ztn response
    code", so the framing has to be exact.
    """
    return b"".join(
        (
            b"ztn\x00",
            bytes((_VERSION, _IS_TKN, 0, 0)),
            struct.pack(">H", len(token) + 1),
            token,
            b"\x00",
        )
    )


class TokenCredential(Credential):
    """``ztn`` - a bearer token in an ``XrdSecProtocolztn`` ``TokenResp``."""

    __slots__ = ("expires_at", "token")
    name = "ztn"

    def __init__(self, token: str) -> None:
        """Hold ``token``; its ``exp`` claim is read once, here, for :meth:`initial`."""
        self.token = token
        self.expires_at = token_expiry(token)

    def initial(self) -> bytes:
        """The framed token, or :class:`~xrdclient.errors.TokenExpiredError`.

        :meth:`available` checks only the lifetime the server asks for, so
        this is where a token that has already lapsed is caught - with its
        expiry time in the message, rather than as the server's bare refusal.
        """
        if self.expires_at is not None and self.expires_at <= time.time():
            when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.expires_at))
            raise TokenExpiredError(f"bearer token expired at {when}")
        return _token_resp(self.token.encode("ascii"))

    @classmethod
    def available(
        cls, offer: Offer, config: Config, *, username: str, host: str
    ) -> TokenCredential | None:
        """The discovered token, held to the server's stated lifetime and size limits.

        ``None`` when there is no token; raises when there is one the server
        has said it would refuse, so the reason reaches the user.
        """
        token = discover_token(config)
        if not token:
            return None
        # The server said what it will take; a token that falls short would
        # cross the wire only to be refused, so refuse it here, by name.
        lifetime, largest = _requirements(offer.params)
        if largest and len(token) > largest:
            raise CredentialError(
                f"the bearer token is {len(token)} bytes and {host or 'the server'} "
                f"accepts at most {largest}"
            )
        cred = cls(token)
        if lifetime and cred.expires_at is not None:
            left = cred.expires_at - time.time()
            if left < lifetime:
                raise CredentialError(
                    f"the bearer token expires in {max(int(left), 0)}s and "
                    f"{host or 'the server'} wants {lifetime}s of life left in it"
                )
        return cred

    @classmethod
    def missing(cls, offer: Offer, config: Config, *, username: str, host: str) -> Ask | None:
        """Every place discovery looked, when it found nothing; ``None`` when it did.

        A token that exists but is refused (too short-lived, too big) gets no
        question: typing another token is not what fixes an issuer's policy.
        """
        if discover_token(config) is not None:
            return None
        looked = ", ".join(["$BEARER_TOKEN", *_token_paths(config)])
        return cls.ask(reason=f"nothing in {looked}", host=host)

    @classmethod
    def ask(cls, *, reason: str, host: str = "") -> Ask:
        """The question to put to a person when there is no token to be found."""
        return Ask(
            mechanism=cls.name,
            what="a bearer token",
            reason=reason,
            # The umask makes the file 0600 as it is created, which the
            # discovery insists on; a plain redirect would leave it 0644.
            hint=(
                f"(umask 077; oidc-token <issuer> > /tmp/bt_u{os.geteuid()}), or set $BEARER_TOKEN"
            ),
            prompt="the token itself, or a path to one",
            host=host,
            secret=True,
        )

    @classmethod
    def using(cls, answer: str, config: Config) -> Config:
        """A config carrying the typed token, or naming the file it is in."""
        # A token is one long word and a path is not, so which was typed is
        # never in doubt: anything naming a readable file is read from it.
        path = os.path.expanduser(answer)
        if os.path.isfile(path):
            return config.evolve(token=None, token_file=path)
        return config.evolve(token=answer)

    def __repr__(self) -> str:
        """The length only: a token in a log is a token anyone can use."""
        return f"TokenCredential(len={len(self.token)}, token=<redacted>)"
