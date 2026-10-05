"""AWS Signature Version 4, in the shape an S3 endpoint asks for.

Signing is the whole of what S3 adds to HTTP: every request carries an
``Authorization`` header holding an HMAC of the request itself, keyed by a
secret derived from the account's own. Nothing here touches the network, and
the secret never leaves the process - what goes on the wire is the signature,
not the key that made it.

    >>> creds = Credentials("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG")
    >>> headers = sign("GET", "/bucket/key", "s3.amazonaws.com", {},
    ...                hash_payload(b""), credentials=creds, region="us-east-1")
    >>> headers["Authorization"].split()[0]
    'AWS4-HMAC-SHA256'
"""

from __future__ import annotations

import configparser
import hashlib
import os
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from botocore.auth import S3SigV4Auth, S3SigV4QueryAuth  # type: ignore[import-untyped]
from botocore.awsrequest import AWSRequest  # type: ignore[import-untyped]
from botocore.credentials import Credentials as _AWSCredentials  # type: ignore[import-untyped]

from .._compat import SLOTS

__all__ = [
    "Credentials",
    "sign",
    "hash_payload",
    "ALGORITHM",
    "EMPTY_SHA256",
    "UNSIGNED_PAYLOAD",
    "DEFAULT_REGION",
]

#: The only algorithm S3 signs with, as it appears in the header.
ALGORITHM = "AWS4-HMAC-SHA256"
#: The hash of an empty body, which every request without one carries.
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
#: What a request whose body is not in hand up front signs instead. S3 accepts
#: it for a streamed upload, where the hash cannot be known before the bytes
#: have been sent.
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
#: The region assumed when nothing names one; AWS's own default.
DEFAULT_REGION = "us-east-1"

#: Where the shared credentials file lives when the environment is quiet.
DEFAULT_CREDENTIALS_FILE = "~/.aws/credentials"


@dataclass(frozen=True, repr=False, **SLOTS)
class Credentials:
    """An access key and the secret that signs with it.

    The secret is the credential; the key id merely names it. So the key id
    prints and the secret does not - a traceback, a log line or a ``repr`` in a
    notebook must not be a way to leak an account.
    """

    access_key: str
    secret_key: str
    #: The session token of a temporary credential, if this is one.
    session_token: str = ""

    def __repr__(self) -> str:
        token = ", session_token=<redacted>" if self.session_token else ""
        return f"Credentials({self.access_key!r}, secret_key=<redacted>{token})"

    @classmethod
    def from_env(cls) -> Credentials | None:
        """``AWS_ACCESS_KEY_ID`` and friends, or ``None`` if unset."""
        access = os.environ.get("AWS_ACCESS_KEY_ID", "")
        secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        if not access or not secret:
            return None
        return cls(access, secret, os.environ.get("AWS_SESSION_TOKEN", ""))

    @classmethod
    def from_file(cls, path: str = "", profile: str = "") -> Credentials | None:
        """One profile of a shared credentials file, or ``None`` if it has none.

        The file is the one every S3 tool reads - ``~/.aws/credentials``, or
        whatever ``AWS_SHARED_CREDENTIALS_FILE`` points at - in the ini format
        they all write. An unreadable or malformed file is *no credentials*
        rather than an error: the caller may have meant to sign with the
        environment, or not to sign at all.
        """
        name = path or os.environ.get("AWS_SHARED_CREDENTIALS_FILE", DEFAULT_CREDENTIALS_FILE)
        section = profile or os.environ.get("AWS_PROFILE", "default")
        parser = configparser.ConfigParser()
        try:
            if not parser.read(os.path.expanduser(name)):
                return None
        except (OSError, configparser.Error):
            return None
        if not parser.has_section(section):
            return None
        values = parser[section]
        access = values.get("aws_access_key_id", "")
        secret = values.get("aws_secret_access_key", "")
        if not access or not secret:
            return None
        return cls(access, secret, values.get("aws_session_token", ""))

    @classmethod
    def discover(cls) -> Credentials | None:
        """The environment first, then the shared file - AWS's own order."""
        return cls.from_env() or cls.from_file()


def hash_payload(body: bytes | bytearray | memoryview | None) -> str:
    """The ``x-amz-content-sha256`` value for ``body``.

    ``None`` is a body that has not been assembled - a streamed upload - and
    signs as :data:`UNSIGNED_PAYLOAD`, which is what S3 offers for exactly
    that case.
    """
    if body is None:
        return UNSIGNED_PAYLOAD
    return hashlib.sha256(body).hexdigest()


def sign(
    method: str,
    target: str,
    host: str,
    headers: Mapping[str, str],
    payload_hash: str,
    *,
    credentials: Credentials,
    region: str,
    service: str = "s3",
    when: datetime | None = None,
) -> dict[str, str]:
    """Sign one request; the result is the headers to send with it.

    ``target`` is the origin-form request target - the percent-encoded path
    and query exactly as they go on the wire - and ``host`` the ``Host``
    header the connection will carry. Both are signed, so both must be what
    is actually sent, which is why this takes them rather than a URL.

    Of the headers, ``host`` and everything beginning ``x-amz-`` are signed,
    as S3 requires; the rest (``Range``, ``Content-Length``, ``User-Agent``)
    travel unsigned, so a proxy that adds one does not break the request.
    """
    path, separator, query = target.partition("?")
    # Encode literal spaces without decoding escaped slashes/object names.
    path = urllib.parse.quote(path or "/", safe="/%")
    return _signed_request(
        method,
        f"https://{host}{path}{separator}{query}",
        host,
        headers,
        payload_hash,
        credentials=_AWSCredentials(
            credentials.access_key, credentials.secret_key, credentials.session_token or None
        ),
        region=region,
        service=service,
        when=when,
    )


def _signed_request(
    method: str,
    url: str,
    host: str,
    headers: Mapping[str, str],
    payload_hash: str,
    *,
    credentials: _AWSCredentials,
    region: str,
    service: str = "s3",
    when: datetime | None = None,
) -> dict[str, str]:
    """Sign an already encoded wire URL without changing its spelling."""
    signed = _selected_headers(headers)
    signed["host"] = host
    signed["x-amz-content-sha256"] = payload_hash
    request = AWSRequest(method=method.upper(), url=url, headers=signed)
    request.params = urllib.parse.parse_qsl(url.partition("?")[2], keep_blank_values=True)
    _authorize(_PayloadSigner(credentials, service, region, payload_hash), request, when)
    return {
        k.lower() if k.lower().startswith("x-amz-") else k: v for k, v in request.headers.items()
    }


def _authorize(
    auth: S3SigV4Auth | S3SigV4QueryAuth, request: AWSRequest, when: datetime | None
) -> None:
    request.context["timestamp"] = (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    auth._modify_request_before_signing(request)
    canonical = auth.canonical_request(request)
    auth._inject_signature_to_request(
        request, auth.signature(auth.string_to_sign(request, canonical), request)
    )


class _PayloadSigner(S3SigV4Auth):  # type: ignore[misc]
    """Use the caller's precomputed hash without reading a streamed body."""

    def __init__(
        self, credentials: _AWSCredentials, service: str, region: str, digest: str
    ) -> None:
        super().__init__(credentials, service, region)
        self._digest = digest

    def payload(self, request: AWSRequest) -> str:
        return self._digest


def _is_signed(name: str) -> bool:
    lowered = name.lower()
    return lowered == "host" or lowered.startswith("x-amz-")


def _selected_headers(headers: Mapping[str, str]) -> dict[str, str]:
    reserved = {"host", "x-amz-date", "x-amz-content-sha256", "x-amz-security-token"}
    return {k: v for k, v in headers.items() if _is_signed(k) and k.lower() not in reserved}
