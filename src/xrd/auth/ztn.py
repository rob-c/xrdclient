"""``ztn`` - WLCG bearer tokens and SciTokens.

Discovery follows the WLCG Bearer Token Discovery specification, which is
also what the C client uses: an explicit token, then ``$BEARER_TOKEN``, then
``$BEARER_TOKEN_FILE``, then ``$XDG_RUNTIME_DIR/bt_u$UID``, then
``/tmp/bt_u$UID``.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import time

from .._log import get_logger
from ..config import Config
from ..errors import CredentialError, TokenExpiredError
from .base import Credential, Offer
from .prompt import Ask

__all__ = ["TokenCredential", "discover_token", "token_claims", "token_expiry"]

_log = get_logger(__name__)


def _token_paths(config: Config) -> list[str]:
    paths = []
    if config.token_file:
        paths.append(config.token_file)
    uid = os.getuid()
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        paths.append(os.path.join(runtime, f"bt_u{uid}"))
    paths.append(f"/tmp/bt_u{uid}")
    return paths


def discover_token(config: Config | None = None) -> str | None:
    """Locate a bearer token, or ``None`` if there is not one to be had."""
    config = config or Config()
    if config.token:
        return config.token.strip()
    env = os.environ.get("BEARER_TOKEN")
    if env and env.strip():
        return env.strip()
    for path in _token_paths(config):
        try:
            with open(path, encoding="utf-8") as fh:
                content = fh.read().strip()
        except OSError:
            continue
        if content:
            _log.debug("using bearer token from %s", path)
            return content
    return None


def token_claims(token: str) -> dict[str, object]:
    """Decode a JWT's claim set without verifying it.

    Signature verification is the server's job; the client only reads the
    expiry so it can fail fast with a useful message instead of a 3010.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return dict(json.loads(base64.urlsafe_b64decode(payload)))
    except (ValueError, TypeError):
        return {}


def token_expiry(token: str) -> float | None:
    """The ``exp`` claim as a UNIX timestamp, if the token carries one."""
    exp = token_claims(token).get("exp")
    return float(exp) if isinstance(exp, (int, float, str)) and str(exp).isdigit() else None


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

    __slots__ = ("token", "expires_at")
    name = "ztn"

    def __init__(self, token: str) -> None:
        self.token = token
        self.expires_at = token_expiry(token)

    def initial(self) -> bytes:
        if self.expires_at is not None and self.expires_at <= time.time():
            when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.expires_at))
            raise TokenExpiredError(f"bearer token expired at {when}")
        return _token_resp(self.token.encode("ascii"))

    @classmethod
    def available(
        cls, offer: Offer, config: Config, *, username: str, host: str
    ) -> TokenCredential | None:
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
            hint=f"oidc-token <issuer> > /tmp/bt_u{os.getuid()}, or set $BEARER_TOKEN",
            prompt="the token itself, or a path to one",
            host=host,
            secret=True,
        )

    @classmethod
    def using(cls, answer: str, config: Config) -> Config:
        # A token is one long word and a path is not, so which was typed is
        # never in doubt: anything naming a readable file is read from it.
        path = os.path.expanduser(answer)
        if os.path.isfile(path):
            return config.evolve(token=None, token_file=path)
        return config.evolve(token=answer)

    def __repr__(self) -> str:
        return f"TokenCredential(len={len(self.token)}, token=<redacted>)"
