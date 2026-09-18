"""XRootD URL parsing and formatting.

Handles the XRootD double-slash convention (``root://host//abs/path``), the
``user@host:port`` authority, IPv6 literals, and CGI round-tripping (the
``?authz=`` and ``tpc.*`` parameters ride here).
"""

from __future__ import annotations

import os
import posixpath
import urllib.parse
from dataclasses import dataclass, field, replace

from ._compat import SLOTS

__all__ = ["XRootDURL", "parse", "DEFAULT_PORT", "ROOT_SCHEMES", "HTTP_SCHEMES", "S3_SCHEMES"]

DEFAULT_PORT = 1094
ROOT_SCHEMES = frozenset({"root", "roots", "xroot", "xroots", "xrootd"})
HTTP_SCHEMES = frozenset({"http", "https", "dav", "davs", "webdav"})
#: Object storage. ``s3://bucket/key`` is HTTP underneath, but the host is a
#: bucket rather than a server, so it is its own thing to this parser.
S3_SCHEMES = frozenset({"s3"})
_TLS_SCHEMES = frozenset({"roots", "xroots", "https", "davs", "s3"})


@dataclass(frozen=True, **SLOTS)
class XRootDURL:
    """A parsed storage URL.

    ``path`` is always the server-side absolute path; the double slash of the
    wire form is a syntax detail handled here and nowhere else.
    """

    scheme: str = "root"
    host: str = ""
    port: int = DEFAULT_PORT
    path: str = "/"
    username: str = ""
    password: str = ""
    query: dict[str, str] = field(default_factory=dict)
    # HTTP signatures cover the escaped query bytes, not merely their decoded
    # values.  Keep those bytes for a URL parsed from text; callers that edit
    # ``query`` deliberately fall back to the ordinary canonical rendering.
    _raw_query: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "scheme", self.scheme.lower())
        object.__setattr__(self, "host", self.host.lower())

    # -- classification ------------------------------------------------

    @property
    def is_root(self) -> bool:
        return self.scheme in ROOT_SCHEMES

    @property
    def is_http(self) -> bool:
        return self.scheme in HTTP_SCHEMES

    @property
    def is_s3(self) -> bool:
        return self.scheme in S3_SCHEMES

    @property
    def is_local(self) -> bool:
        return self.scheme == "file"

    @property
    def use_tls(self) -> bool:
        return self.scheme in _TLS_SCHEMES

    @property
    def netloc(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    @property
    def endpoint(self) -> tuple[str, str, int]:
        """The connection identity: ``(scheme, host, port)``."""
        return (self.scheme, self.host, self.port)

    @property
    def name(self) -> str:
        return posixpath.basename(self.path.rstrip("/"))

    @property
    def parent(self) -> XRootDURL:
        return self.with_path(posixpath.dirname(self.path.rstrip("/")) or "/")

    # -- derivation ----------------------------------------------------

    def evolve(self, **changes: object) -> XRootDURL:
        """A copy with ``changes`` applied."""
        if "query" in changes and "_raw_query" not in changes:
            changes["_raw_query"] = ""
        return replace(self, **changes)  # type: ignore[arg-type]

    def with_path(self, path: str) -> XRootDURL:
        return replace(self, path=_normalize(path))

    def join(self, *parts: str) -> XRootDURL:
        return self.with_path(posixpath.join(self.path, *parts))

    def __truediv__(self, part: str | os.PathLike[str]) -> XRootDURL:
        """``url / "sub" / "file"``, spelled the way :mod:`pathlib` spells it."""
        return self.join(os.fspath(part))

    def with_query(self, **params: str) -> XRootDURL:
        merged = dict(self.query)
        merged.update({k: v for k, v in params.items() if v is not None})
        return replace(self, query=merged, _raw_query="")

    def without_query(self) -> XRootDURL:
        return replace(self, query={}, _raw_query="")

    # -- formatting ----------------------------------------------------

    @property
    def path_with_cgi(self) -> str:
        """What goes on the wire as an operation's path argument."""
        if not self.query:
            return self.path
        return f"{self.path}?{urllib.parse.urlencode(self.query)}"

    def __str__(self) -> str:
        if self.is_local:
            return f"file://{self.path}"
        auth = ""
        if self.username:
            auth = urllib.parse.quote(self.username, safe="")
            if self.password:
                auth += ":" + urllib.parse.quote(self.password, safe="")
            auth += "@"
        sep = "//" if self.is_root else "/"
        # A bucket is a name rather than an endpoint, so an ``s3`` URL carries
        # no port: ``s3://bucket:443/key`` is not what anyone wrote or expects.
        where = self.host if self.is_s3 else self.netloc
        base = f"{self.scheme}://{auth}{where}{sep}{self.path.lstrip('/')}"
        if self.query:
            base += "?" + urllib.parse.urlencode(self.query)
        return base

    def __hash__(self) -> int:
        # The generated hash would choke on ``query`` being a dict.
        return hash((self.endpoint, self.path, self.username, tuple(sorted(self.query.items()))))

    def __repr__(self) -> str:
        redacted = self
        if self.password or "authz" in self.query:
            q = {k: ("<redacted>" if k == "authz" else v) for k, v in self.query.items()}
            redacted = replace(self, password="<redacted>" if self.password else "", query=q)
        return f"XRootDURL({str(redacted)!r})"

    def __fspath__(self) -> str:
        if not self.is_local:
            raise TypeError(f"{self!r} is not a local path")
        return self.path

    @property
    def http_url(self) -> str:
        """This URL rendered for an HTTP client (single slash, no CGI dupes)."""
        scheme = {"dav": "http", "davs": "https", "webdav": "https"}.get(self.scheme, self.scheme)
        base = f"{scheme}://{self.netloc}{self.path}"
        if self.query:
            base += "?" + urllib.parse.urlencode(self.query)
        return base


def _normalize(path: str) -> str:
    """Collapse a wire path to a single leading slash, preserving trailing."""
    if not path:
        return "/"
    trailing = path.endswith("/") and len(path) > 1
    norm = posixpath.normpath("/" + path.lstrip("/"))
    return norm + "/" if trailing and not norm.endswith("/") else norm


def parse(url: str | os.PathLike[str] | XRootDURL) -> XRootDURL:
    """Parse a storage URL. A bare path yields a ``file`` URL."""
    if isinstance(url, XRootDURL):
        return url
    text = os.fspath(url)
    if "://" not in text:
        return _file_url(os.path.abspath(text))

    scheme, rest = text.split("://", 1)
    scheme = scheme.lower()
    if scheme == "file":
        return _file_url(rest or "/")

    authority, sep, tail = rest.partition("/")
    username, password, hostport = _authority(authority)
    host, port_s = _host_port(hostport)
    port = _port(port_s, scheme, text)
    path_s, cgi, query = _path_query(sep, tail)

    return XRootDURL(
        scheme=scheme,
        host=host,
        port=port,
        path=_normalize(path_s),
        username=username,
        password=password,
        query=query,
        _raw_query=cgi,
    )


def _file_url(path: str) -> XRootDURL:
    return XRootDURL(scheme="file", host="", port=0, path=path)


def _authority(authority: str) -> tuple[str, str, str]:
    userinfo, marker, hostport = authority.rpartition("@")
    if not marker:
        return "", "", authority
    user, _, password = userinfo.partition(":")
    return urllib.parse.unquote(user), urllib.parse.unquote(password), hostport


def _host_port(hostport: str) -> tuple[str, str]:
    if hostport.startswith("["):
        host, _, port = hostport[1:].partition("]")
        return host, port.lstrip(":")
    host, _, port = hostport.partition(":")
    return host, port


def _default_port(scheme: str) -> int:
    if scheme in ("https", "davs", "webdav", "s3"):
        return 443
    if scheme in ("http", "dav"):
        return 80
    return DEFAULT_PORT


def _port(value: str, scheme: str, text: str) -> int:
    try:
        return int(value) if value else _default_port(scheme)
    except ValueError as exc:
        raise ValueError(f"invalid port in URL {text!r}") from exc


def _path_query(separator: str, tail: str) -> tuple[str, str, dict[str, str]]:
    path_and_cgi = (separator + tail) if separator else "/"
    path, _, cgi = path_and_cgi.partition("?")
    query = dict(urllib.parse.parse_qsl(cgi, keep_blank_values=True)) if cgi else {}
    return path, cgi, query
