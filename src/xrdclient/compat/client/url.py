"""``XRootD.client.URL``: a URL split the way XrdCl splits it.

This is not :class:`xrdclient.XRootDURL`, and deliberately so. XrdCl keeps a
single-slash path relative (``root://h/rel`` has the path ``rel``), keeps the
brackets on an IPv6 host, calls a bare path ``file://localhost``, and answers
an unparseable string with ``is_valid() == False`` rather than an exception.
Ported code prints these and compares them, so they are reproduced here
rather than normalised the way the native parser (rightly) does.
"""

from __future__ import annotations

__all__ = ["URL"]

#: XrdCl's port when a URL names none, whatever its scheme.
_DEFAULT_PORT = 1094


class URL:
    """A URL as XrdCl parses it: ``hostid``, ``path``, ``path_with_params``."""

    def __init__(self, url: str) -> None:
        self.__url = str(url)
        self.__parse(self.__url)

    def __parse(self, text: str) -> None:
        self.__clear_parsed()
        if "://" not in text:
            text = f"file://localhost{text}" if text.startswith("/") else f"root://{text}"
        protocol, _, rest = text.partition("://")
        authority, slash, tail = rest.partition("/")
        if protocol == "file":
            # A local path is absolute by definition, so its slash is part of
            # the path rather than the authority's separator.
            authority, tail = authority or "localhost", slash + tail
        if not protocol or not _authority(self, authority):
            self.__clear_parsed()
            return
        self.protocol = protocol  # XrdCl keeps the case it was given
        path, _, params = tail.partition("?")
        # ``root://host//abs`` leaves ``/abs`` after the authority's own slash;
        # ``root://host/rel`` leaves ``rel``, and XrdCl keeps that relative.
        self.path = path if slash else ""
        self.__params = params

    def __clear_parsed(self) -> None:
        self.protocol = self.username = self.password = self.hostname = ""
        self.port = _DEFAULT_PORT
        self.path = self.__params = ""

    @property
    def hostid(self) -> str:
        """``user:password@host:port``, with whichever parts the URL had."""
        if not self.hostname:
            return ""
        credentials = self.username + (f":{self.password}" if self.password else "")
        prefix = f"{credentials}@" if credentials else ""
        if self.protocol == "file":
            return f"{prefix}{self.hostname}"
        return f"{prefix}{self.hostname}:{self.port}"

    @property
    def path_with_params(self) -> str:
        """The path with its ``?key=value`` parameters still on it."""
        return f"{self.path}?{self.__params}" if self.__params else self.path

    def is_valid(self) -> bool:
        """Whether the string parsed into a protocol and a host."""
        return bool(self.protocol and self.hostname)

    def clear(self) -> None:
        """Forget everything, leaving an invalid URL."""
        self.__url = ""
        self.__clear_parsed()

    def __str__(self) -> str:
        if not self.is_valid():
            return ""
        if self.protocol == "file":
            return f"file://{self.hostid}{self.path_with_params}"
        return f"{self.protocol}://{self.hostid}/{self.path_with_params}"

    def __repr__(self) -> str:
        return f"<XRootD.client.URL {str(self)!r}>"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, URL) and str(self) == str(other)

    def __hash__(self) -> int:
        return hash(str(self))


def _authority(url: URL, authority: str) -> bool:
    """Fill ``url``'s credentials, host and port from ``user:pw@host:port``."""
    credentials, at, hostport = authority.rpartition("@")
    if at:
        url.username, _, url.password = credentials.partition(":")
    if hostport.startswith("["):
        # An IPv6 literal: its colons are not the port separator, and XrdCl
        # reports the host with its brackets on.
        host, bracket, port = hostport.partition("]")
        url.hostname = host + bracket
        port = port[1:] if port.startswith(":") else port
    else:
        url.hostname, _, port = hostport.partition(":")
    if port:
        if not port.isdigit():
            return False
        url.port = int(port)
    return bool(url.hostname)
