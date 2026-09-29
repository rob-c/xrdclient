"""The FILE credential cache that ``kinit`` writes, read with its keys.

Versions 3 and 4 of the format are what MIT and Heimdal write: a default
principal, then one entry per credential - client, server, the session key,
times, flags, addresses, authorization data and the ticket itself. The key
is kept, in memory only, because without it a ticket is useless: it is what
the authenticator in an AP-REQ is encrypted under.

Which cache is "the" cache follows MIT: ``$KRB5CCNAME``, else
``default_ccache_name`` from ``krb5.conf``, else ``/tmp/krb5cc_<uid>``.
``FILE:`` and ``DIR:`` caches are files this module can read. ``KCM:``,
``KEYRING:``, ``API:`` and ``MEMORY:`` caches live in a daemon, the kernel
or another process, where a pure-Python client cannot reach; naming one is
an error that says how to get a FILE cache instead, rather than a silent
"no ticket".
"""

from __future__ import annotations

import os
import struct
import tempfile

from ..._log import get_logger
from ...errors import CredentialError
from .model import Principal, Ticket
from .profile import Profile

__all__ = [
    "CCACHE_VERSION_3",
    "CCACHE_VERSION_4",
    "ccache_name",
    "read_ccache",
    "resolve_ccache",
]

_log = get_logger(__name__)

CCACHE_VERSION_4 = 0x0504
CCACHE_VERSION_3 = 0x0503

#: MIT keeps cache metadata as pseudo-credentials in this realm; they are not tickets.
_CONFIG_REALM = "X-CACHECONF:"

#: The version 4 header tag that carries the KDC's clock offset.
_TAG_DELTATIME = 1

_UNREACHABLE = ("KCM", "KEYRING", "API", "MEMORY", "MSLSA")


class _Reader:
    """A big-endian cursor that refuses to read past the end."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, count: int) -> bytes:
        if count < 0 or self.pos + count > len(self.data):
            raise ValueError(f"credential cache truncated at offset {self.pos}")
        out = self.data[self.pos : self.pos + count]
        self.pos += count
        return out

    def u16(self) -> int:
        return int(struct.unpack(">H", self.take(2))[0])

    def u32(self) -> int:
        return int(struct.unpack(">I", self.take(4))[0])

    def blob(self) -> bytes:
        return self.take(self.u32())

    @property
    def exhausted(self) -> bool:
        return self.pos >= len(self.data)


def _read_principal(reader: _Reader) -> Principal:
    """One principal, in the layout versions 3 and 4 share."""
    name_type = reader.u32()
    count = reader.u32()
    realm = reader.blob().decode("utf-8", "replace")
    components = tuple(reader.blob().decode("utf-8", "replace") for _ in range(count))
    return Principal(components, realm, name_type)


def _skip_list(reader: _Reader) -> None:
    """Addresses and authorization data: a count of (u16 type, blob) pairs, unused here."""
    for _ in range(reader.u32()):
        reader.u16()
        reader.blob()


def _kdc_offset(header: bytes) -> float:
    """A version 4 header's DeltaTime tag: the KDC's clock minus ours, else 0.

    The header is a run of ``(u16 tag, u16 length, value)``; tag 1 holds the
    offset as signed seconds and microseconds, which ``kinit`` records when
    ``kdc_timesync`` is on - MIT's default.
    """
    tags = _Reader(header)
    while not tags.exhausted:
        tag, value = tags.u16(), tags.take(tags.u16())
        if tag == _TAG_DELTATIME and len(value) == 8:
            seconds, micros = struct.unpack(">ii", value)
            return float(seconds + micros / 1_000_000)
    return 0.0


def _read_entry(reader: _Reader, version: int, kdc_offset: float) -> Ticket:
    client = _read_principal(reader)
    server = _read_principal(reader)
    enctype = reader.u16()
    if version == CCACHE_VERSION_3:
        reader.u16()  # version 3 wrote the enctype twice
    key = reader.blob()
    auth_time, start_time, end_time, renew_till = (reader.u32() for _ in range(4))
    reader.take(1)  # is_skey: user-to-user, never used here
    flags = reader.u32()
    _skip_list(reader)  # addresses
    _skip_list(reader)  # authorization data
    der = reader.blob()
    reader.blob()  # second ticket, used only for user-to-user
    return Ticket(
        client=client,
        server=server,
        enctype=enctype,
        auth_time=auth_time,
        start_time=start_time,
        end_time=end_time,
        renew_till=renew_till,
        flags=flags,
        der=der,
        key=key,
        kdc_offset=kdc_offset,
    )


def read_ccache(path: str) -> tuple[Principal, list[Ticket]]:
    """Parse a FILE credential cache into its default principal and tickets.

    Entries that will not parse end the scan rather than raising, because a
    cache being rewritten under us should cost the tail, not the whole
    answer. MIT's configuration pseudo-entries are left out.
    """
    with open(path, "rb") as handle:
        reader = _Reader(handle.read())
    version = reader.u16()
    if version not in (CCACHE_VERSION_3, CCACHE_VERSION_4):
        raise ValueError(f"unsupported credential cache version 0x{version:04x} in {path}")
    kdc_offset = _kdc_offset(reader.take(reader.u16())) if version == CCACHE_VERSION_4 else 0.0
    default = _read_principal(reader)

    out: list[Ticket] = []
    while not reader.exhausted:
        try:
            ticket = _read_entry(reader, version, kdc_offset)
        except (ValueError, struct.error) as exc:
            _log.debug("credential cache %s ends early: %s", path, exc)
            break
        if ticket.server.realm != _CONFIG_REALM:
            out.append(ticket)
    return default, out


def _expand(name: str) -> str:
    """MIT's ``%{...}`` tokens in ``default_ccache_name``, the ones seen in the wild."""
    tokens = {
        "%{uid}": str(os.getuid()),
        "%{euid}": str(os.geteuid()),
        "%{USERID}": str(os.geteuid()),
        "%{TEMP}": tempfile.gettempdir(),
        "%{username}": os.environ.get("USER", ""),
        "%{null}": "",
    }
    for token, value in tokens.items():
        name = name.replace(token, value)
    return name


def ccache_name(profile: Profile | None = None) -> str:
    """The default cache's full name, ``TYPE:residual``, as MIT would choose it."""
    name = os.environ.get("KRB5CCNAME", "")
    if not name:
        configured = (profile or Profile.load()).libdefault("default_ccache_name")
        name = _expand(configured) if configured else f"FILE:/tmp/krb5cc_{os.geteuid()}"
    return name


def _dir_primary(directory: str) -> str:
    """A ``DIR:`` collection's current cache: the file named in ``primary``."""
    try:
        with open(os.path.join(directory, "primary"), encoding="utf-8") as handle:
            primary = handle.read().strip()
    except OSError:
        primary = "tkt"  # what MIT creates when there is no primary yet
    return os.path.join(directory, primary)


def resolve_ccache(name: str | None = None, profile: Profile | None = None) -> str:
    """The file a cache name refers to.

    Raises :class:`~xrdclient.errors.CredentialError` for a cache type that
    is not a file, naming the fix.
    """
    name = name if name is not None else ccache_name(profile)
    kind, sep, rest = name.partition(":")
    if not sep or "/" in kind:
        return name  # a bare path
    if kind == "FILE":
        return rest
    if kind == "DIR":
        # ``DIR::/path/tktXYZ`` names one cache in a collection directly.
        return rest[1:] if rest.startswith(":") else _dir_primary(rest)
    reason = (
        "lives outside the filesystem"
        if kind in _UNREACHABLE
        else "is not a type this client knows"
    )
    raise CredentialError(
        f"Kerberos credential cache {name!r} {reason}, and this client reads only FILE: "
        f"and DIR: caches. Get a ticket into a file with: "
        f"KRB5CCNAME=FILE:/tmp/krb5cc_$(id -u) kinit"
    )
