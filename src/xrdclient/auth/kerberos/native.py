"""Read-only native credential-cache handling through optional pykrb5."""

from __future__ import annotations

import time
from typing import Any

from ...errors import CredentialError
from .ccache import is_config_entry
from .model import Principal, Ticket


def _principal(value: Any) -> Principal:
    return Principal(
        tuple(part.decode("utf-8", "replace") for part in value.components),
        value.realm.decode("utf-8", "replace"),
        int(value.type),
    )


def _ticket(value: Any, offset: float) -> Ticket:
    times, key = value.times, value.keyblock
    # pykrb5 normalises MIT/Heimdal flags to RFC bit positions numbered from zero.
    flags = int(f"{int(value.ticket_flags):032b}"[::-1], 2)
    return Ticket(
        client=_principal(value.client),
        server=_principal(value.server),
        enctype=int(key.enctype),
        key=bytes(key.data),
        der=bytes(value.ticket),
        auth_time=times.authtime,
        start_time=times.starttime,
        end_time=times.endtime,
        renew_till=times.renew_till,
        flags=flags,
        kdc_offset=offset,
    )


class NativeCacheError(CredentialError):
    """An actionable cache error retaining the native Kerberos error code."""

    def __init__(self, message: str, native_code: int = 0) -> None:
        self.native_code = native_code
        super().__init__(message)


class NativeCache:
    """FILE, DIR, KCM, KEYRING or macOS API caches, as the installed library supports."""

    def __init__(self, name: str | None = None) -> None:
        self._explicit = name
        self.name = name or "default"

    def read(self) -> tuple[Principal, list[Ticket]]:
        try:
            import krb5  # type: ignore[import-not-found,unused-ignore]
        except (ImportError, OSError) as exc:
            raise CredentialError(
                "Native Kerberos cache support is not installed. "
                "Install it with python -m pip install 'xrdclient[krb5]', "
                "or unset XRD_KRB5_BACKEND to use the portable cache reader."
            ) from exc
        try:
            context = krb5.init_context()
            cache_name = (
                self._explicit.encode("utf-8")
                if self._explicit is not None
                else krb5.cc_default_name(context)
            )
            self.name = cache_name.decode("utf-8", "replace")
            cache = krb5.cc_resolve(context, cache_name)
            client = _principal(krb5.cc_get_principal(context, cache))
            seconds, micros = krb5.us_timeofday(context)
            offset = seconds + micros / 1_000_000 - time.time()
            tickets = [_ticket(value, offset) for value in cache]
        except krb5.Krb5Error as exc:
            raise NativeCacheError(
                f"Cannot read Kerberos cache {self.name!r}: {exc}. "
                "Check KRB5CCNAME and your file permissions, then run kinit.",
                int(exc.err_code),
            ) from exc
        return client, [ticket for ticket in tickets if not is_config_entry(ticket)]

    def store(self, ticket: Ticket) -> bool:
        """Never rewrite a cache owned by kinit or another process."""
        return False
