"""XRootD URL parsing and formatting.

Handles the XRootD double-slash convention (``root://host//abs/path``), the
``user@host:port`` authority, IPv6 literals, and CGI round-tripping (the
``?authz=`` and ``tpc.*`` parameters ride here).
"""

from __future__ import annotations

import os
import posixpath
import re
import urllib.parse
from collections.abc import Collection, Mapping
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

#: What a rendered CGI value leaves alone. XrdCl sends opaque data exactly as
#: written, and servers compare it literally - a ``tpc.src`` of ``h:1094`` is
#: not the same string as ``h%3A1094`` to anything on the far side - so only
#: what would change the meaning of the query is escaped: ``&`` and ``#``
#: (structure), ``%`` and ``+`` (which this parser decodes), and whitespace.
_CGI_SAFE = ":/@,;=!$'()*~"


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
    # The query exactly as it goes on the wire. ``query`` is the decoded view
    # of it, for reading; this is what is sent, because both a server
    # (XrdSciTokens strips a literal ``Bearer%20``) and a signature (a CDN's
    # signed redirect covers the escaped bytes) see the spelling, not the
    # values. Kept in step with ``query`` by ``__post_init__``: the fields
    # that were not edited keep their bytes, and only edited ones are
    # rendered afresh.
    _raw_query: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "scheme", self.scheme.lower())
        object.__setattr__(self, "host", self.host.lower())
        if _decode(self._raw_query) != self.query:
            object.__setattr__(self, "_raw_query", _reconcile(self._raw_query, self.query))

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
        """A copy with ``changes`` applied.

        A changed ``query`` keeps the written spelling of every field it did
        not change; see :attr:`cgi`.
        """
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
        return replace(self, query=merged)

    def without_query(self) -> XRootDURL:
        return replace(self, query={}, _raw_query="")

    def glob_pattern(self) -> str:
        """This path read as a glob pattern, ``?`` wildcards and all.

        A ``?`` in a pattern like ``.../f??.dat`` is also the URL's
        query-string delimiter, so parsing moved the tail into the query and
        left ``path`` ending at the first ``?``. Globbing the path alone would
        match the literal name ``f``; joining the raw query back on restores
        the wildcards. A pattern with no ``?`` has an empty query and is
        returned unchanged.
        """
        return f"{self.path}?{self._raw_query}" if self._raw_query else self.path

    # -- formatting ----------------------------------------------------

    @property
    def cgi(self) -> str:
        """The opaque data as it goes on the wire, without the ``?``.

        Byte for byte what was parsed, for every field nobody has changed
        since; a field that was added or changed is rendered the way XrdCl
        would leave it: a space as ``%20``, and ``:``, ``/``, ``@`` and ``,``
        as they are.
        """
        return self._raw_query

    def cgi_except(self, names: Collection[str]) -> str:
        """:attr:`cgi` without the fields called ``names``, the rest untouched."""
        return "&".join(f for f in _fields(self._raw_query) if _field_name(f) not in names)

    @property
    def path_with_cgi(self) -> str:
        """What goes on the wire as an operation's path argument."""
        if not self._raw_query:
            return self.path
        return f"{self.path}?{self._raw_query}"

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
        path = quote_path(self.path) if self.is_http else self.path
        base = f"{self.scheme}://{auth}{where}{sep}{path.lstrip('/')}"
        if self._raw_query:
            base += "?" + self._raw_query
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
        """This URL as another HTTP server must be handed it.

        ``dav``/``davs`` become ``http``/``https``, which is all a server
        resolves, and the path is percent-encoded: this string travels in a
        header (``Source``, ``Destination``), where a raw space ends the URL
        and a name outside Latin-1 cannot be sent at all.
        """
        scheme = {"dav": "http", "davs": "https", "webdav": "https"}.get(self.scheme, self.scheme)
        base = f"{scheme}://{self.netloc}{quote_path(self.path)}"
        if self._raw_query:
            base += "?" + self._raw_query
        return base


#: A slash that is part of one path segment rather than a separator - a git
#: ref such as ``refs%2Fconvert`` in a Hugging Face URL. It stays escaped both
#: ways: decoding it would move the segment boundary.
_ENCODED_SLASH = re.compile("%2F", re.IGNORECASE)


def quote_path(path: str) -> str:
    """A plain path percent-encoded exactly once, as an HTTP URL carries it.

    Everything but ``/`` and RFC 3986's unreserved characters is escaped, a
    ``%`` included, except an escaped slash (``%2F``), which is kept: the
    path is a name, and :func:`parse` decodes an HTTP URL's path exactly once
    and leaves ``%2F`` alone, so the two are inverses and a name survives
    being written out as a URL and read back.
    """
    return "%2F".join(urllib.parse.quote(part, safe="/") for part in _ENCODED_SLASH.split(path))


def unquote_path(path: str) -> str:
    """An HTTP URL's path decoded once into the name it spells; see :func:`quote_path`."""
    return "%2F".join(urllib.parse.unquote(part) for part in _ENCODED_SLASH.split(path))


def _render_field(name: str, value: str) -> str:
    """One CGI field rendered the way XrdCl leaves it.

    Unlike :func:`urllib.parse.urlencode`, a space is ``%20`` rather than
    ``+``, and ``:``, ``/``, ``@`` and ``,`` stay as they are - the spelling
    a server that does not decode its CGI (and most do not) expects.
    """
    quote = urllib.parse.quote
    return f"{quote(name, safe=_CGI_SAFE)}={quote(value, safe=_CGI_SAFE)}"


def _fields(raw: str) -> list[str]:
    """The ``&``-separated fields of a raw query, empty ones dropped."""
    return [f for f in raw.split("&") if f] if raw else []


def _field_name(raw_field: str) -> str:
    return urllib.parse.unquote_plus(raw_field.partition("=")[0])


def _decode(raw: str) -> dict[str, str]:
    """The decoded view of a raw query: what :attr:`XRootDURL.query` holds."""
    return dict(urllib.parse.parse_qsl(raw, keep_blank_values=True)) if raw else {}


def _reconcile(raw: str, query: Mapping[str, str]) -> str:
    """The raw query that decodes to ``query``, reusing ``raw`` where it can.

    A field whose value did not change keeps its written bytes and its
    place; a changed one is rendered afresh in the same place; a removed one
    is dropped; a new one is appended. Only the last of a repeated name is
    kept, since the last is the one ``query`` holds.
    """
    kept: dict[str, str] = {}
    for raw_field in _fields(raw):
        name = _field_name(raw_field)
        if name in query:
            unchanged = _decode(raw_field).get(name) == query[name]
            kept.pop(name, None)
            kept[name] = raw_field if unchanged else _render_field(name, query[name])
    for name, value in query.items():
        if name not in kept:
            kept[name] = _render_field(name, value)
    return "&".join(kept.values())


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
    if scheme in HTTP_SCHEMES:
        # An HTTP URL's path is percent-encoded - that is what a browser, a
        # server's ``Location`` and a copied link all hand over - so it is
        # decoded once into the name it spells. ``root://`` and ``s3://``
        # paths are names as written, as XrdCl and the AWS tools take them.
        path_s = unquote_path(path_s)

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
    return path, cgi, _decode(cgi)
