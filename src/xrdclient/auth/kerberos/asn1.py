"""The RFC 4120 messages this client sends and reads, in DER.

Kerberos is specified in ASN.1 with explicit context tags on every field, so
each message is a SEQUENCE of ``[n]`` wrappers around universal types, and
the top-level messages are additionally wrapped in an ``[APPLICATION n]``
tag. The writer below is a handful of functions that compose; the reader
leans on :mod:`xrdclient.crypto.der` and a :class:`Fields` view that turns a
SEQUENCE into "field number to value" and refuses anything out of order.

Only the messages an AP-REQ login needs are here: Ticket (carried opaquely
except for its outer fields), Authenticator, AP-REQ, the TGS-REQ with its
PA-TGS-REQ, the KDC reply and its encrypted part, KRB-ERROR, and KRB-CRED
for ticket forwarding. The comments give each structure's ASN.1 so the code
can be checked against RFC 4120 section 5 line by line.
"""

from __future__ import annotations

import calendar
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from ..._compat import SLOTS
from ...crypto.der import (
    TAG_GENERALIZED_TIME,
    TAG_OCTET_STRING,
    TAG_SEQUENCE,
    DERError,
    Element,
    parse,
    read_integer,
)
from .model import Principal, Ticket

__all__ = [
    "EncryptedData",
    "Key",
    "Fields",
    "KdcReply",
    "EncKdcReplyPart",
    "KrbError",
    "TicketInfo",
    "decode_enc_kdc_rep_part",
    "decode_krb_error",
    "decode_kdc_rep",
    "decode_ticket",
    "encode_ap_req",
    "encode_authenticator",
    "encode_cred_info",
    "encode_enc_krb_cred_part",
    "encode_krb_cred",
    "encode_req_body",
    "encode_tgs_req",
    "principal_name",
    "read_principal_name",
]

TAG_BIT_STRING = 0x03
TAG_INTEGER = 0x02
TAG_GENERAL_STRING = 0x1B

#: The application tags (RFC 4120 section 5.10), as their constructed tag byte.
APP_TICKET = 0x61
APP_AUTHENTICATOR = 0x62
APP_AS_REP = 0x6B
APP_TGS_REQ = 0x6C
APP_TGS_REP = 0x6D
APP_AP_REQ = 0x6E
APP_KRB_CRED = 0x76
APP_ENC_AS_REP_PART = 0x79
APP_ENC_TGS_REP_PART = 0x7A
APP_ENC_KRB_CRED_PART = 0x7D
APP_KRB_ERROR = 0x7E

#: Message types (``msg-type``), which repeat the application tag number.
KRB_TGS_REQ = 12
KRB_TGS_REP = 13
KRB_AP_REQ = 14
KRB_CRED = 22

PVNO = 5


# -- writing -----------------------------------------------------------------


def _length(size: int) -> bytes:
    if size < 0x80:
        return bytes([size])
    raw = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def tlv(tag: int, content: bytes) -> bytes:
    """One DER element: tag, definite length, content."""
    return bytes([tag]) + _length(len(content)) + content


def integer(value: int) -> bytes:
    """A DER INTEGER, in the fewest two's-complement bytes."""
    size = (value + (value < 0)).bit_length() // 8 + 1
    return tlv(TAG_INTEGER, value.to_bytes(size, "big", signed=True))


def general_string(text: str) -> bytes:
    """A KerberosString: GeneralString restricted, in practice, to UTF-8."""
    return tlv(TAG_GENERAL_STRING, text.encode("utf-8"))


def octets(data: bytes) -> bytes:
    return tlv(TAG_OCTET_STRING, data)


def kerberos_time(epoch: float) -> bytes:
    """KerberosTime: GeneralizedTime, whole seconds, always UTC (``Z``)."""
    text = time.strftime("%Y%m%d%H%M%SZ", time.gmtime(int(epoch)))
    return tlv(TAG_GENERALIZED_TIME, text.encode("ascii"))


def kerberos_flags(value: int) -> bytes:
    """KerberosFlags: a 32-bit BIT STRING, never trimmed (RFC 4120 section 5.2.8)."""
    return tlv(TAG_BIT_STRING, b"\x00" + value.to_bytes(4, "big"))


def sequence(*parts: bytes) -> bytes:
    return tlv(TAG_SEQUENCE, b"".join(parts))


def sequence_of(items: Iterable[bytes]) -> bytes:
    return tlv(TAG_SEQUENCE, b"".join(items))


def fields(*pairs: tuple[int, bytes | None]) -> bytes:
    """A SEQUENCE of explicitly tagged fields; ``None`` leaves an OPTIONAL out."""
    return sequence(*(tlv(0xA0 | number, value) for number, value in pairs if value is not None))


def principal_name(principal: Principal) -> bytes:
    """PrincipalName ::= SEQUENCE { name-type [0] Int32,
    name-string [1] SEQUENCE OF KerberosString }"""
    names = sequence_of(general_string(part) for part in principal.components)
    return fields((0, integer(principal.name_type)), (1, names))


@dataclass(frozen=True, **SLOTS)
class Key:
    """EncryptionKey ::= SEQUENCE { keytype [0] Int32, keyvalue [1] OCTET STRING }

    The value is key material: it is left out of ``repr`` so that a debug
    print of anything holding one cannot leak it.
    """

    enctype: int
    value: bytes = field(repr=False)

    def encode(self) -> bytes:
        return fields((0, integer(self.enctype)), (1, octets(self.value)))


@dataclass(frozen=True, **SLOTS)
class EncryptedData:
    """EncryptedData ::= SEQUENCE { etype [0] Int32, kvno [1] UInt32 OPTIONAL,
    cipher [2] OCTET STRING }"""

    etype: int
    cipher: bytes
    kvno: int | None = None

    def encode(self) -> bytes:
        kvno = None if self.kvno is None else integer(self.kvno)
        return fields((0, integer(self.etype)), (1, kvno), (2, octets(self.cipher)))


def encode_authenticator(
    client: Principal,
    ctime: int,
    cusec: int,
    *,
    checksum: tuple[int, bytes] | None = None,
    subkey: Key | None = None,
    seq_number: int | None = None,
) -> bytes:
    """Authenticator ::= [APPLICATION 2] SEQUENCE { ... }

    ``authenticator-vno [0]``, ``crealm [1]``, ``cname [2]``, ``cksum [3]``
    OPTIONAL, ``cusec [4]``, ``ctime [5]``, ``subkey [6]`` OPTIONAL,
    ``seq-number [7]`` OPTIONAL; authorization-data is never sent.
    """
    cksum = None
    if checksum is not None:
        cksum = fields((0, integer(checksum[0])), (1, octets(checksum[1])))
    body = fields(
        (0, integer(PVNO)),
        (1, general_string(client.realm)),
        (2, principal_name(client)),
        (3, cksum),
        (4, integer(cusec)),
        (5, kerberos_time(ctime)),
        (6, None if subkey is None else subkey.encode()),
        (7, None if seq_number is None else integer(seq_number)),
    )
    return tlv(APP_AUTHENTICATOR, body)


def encode_ap_req(ticket: bytes, authenticator: EncryptedData, options: int = 0) -> bytes:
    """AP-REQ ::= [APPLICATION 14] SEQUENCE { pvno [0], msg-type [1], ap-options [2],
    ticket [3], authenticator [4] }

    ``ticket`` is the Ticket's DER exactly as the KDC issued it.
    """
    body = fields(
        (0, integer(PVNO)),
        (1, integer(KRB_AP_REQ)),
        (2, kerberos_flags(options)),
        (3, ticket),
        (4, authenticator.encode()),
    )
    return tlv(APP_AP_REQ, body)


def encode_req_body(
    *,
    options: int,
    realm: str,
    server: Principal,
    till: int,
    nonce: int,
    etypes: Sequence[int],
) -> bytes:
    """KDC-REQ-BODY ::= SEQUENCE { kdc-options [0], cname [1] OPT, realm [2], sname [3] OPT, ... }

    For a TGS-REQ the client's name is in the ticket, so ``cname`` is left
    out; ``from``, ``rtime``, addresses, authorization data and additional
    tickets are never needed here either.
    """
    return fields(
        (0, kerberos_flags(options)),
        (2, general_string(realm)),
        (3, principal_name(server)),
        (5, kerberos_time(till)),
        (7, integer(nonce)),
        (8, sequence_of(integer(etype) for etype in etypes)),
    )


def encode_tgs_req(padata: Sequence[tuple[int, bytes]], body: bytes) -> bytes:
    """TGS-REQ ::= [APPLICATION 12] KDC-REQ

    ``KDC-REQ ::= SEQUENCE { pvno [1], msg-type [2], padata [3] SEQUENCE OF PA-DATA OPT,
    req-body [4] }``
    and ``PA-DATA ::= SEQUENCE { padata-type [1] Int32, padata-value [2] OCTET STRING }``.
    """
    pas = sequence_of(fields((1, integer(kind)), (2, octets(value))) for kind, value in padata)
    return tlv(
        APP_TGS_REQ, fields((1, integer(PVNO)), (2, integer(KRB_TGS_REQ)), (3, pas), (4, body))
    )


def encode_cred_info(ticket: Ticket) -> bytes:
    """KrbCredInfo ::= SEQUENCE { key [0], prealm [1], pname [2], flags [3], authtime [4], ... }

    Every optional field MIT fills in is filled in: the session key, who the
    ticket is for and until when, so the receiver can write a usable cache.
    """
    return fields(
        (0, Key(ticket.enctype, ticket.key).encode()),
        (1, general_string(ticket.client.realm)),
        (2, principal_name(ticket.client)),
        (3, kerberos_flags(ticket.flags)),
        (4, kerberos_time(ticket.auth_time)),
        (5, kerberos_time(ticket.start_time)),
        (6, kerberos_time(ticket.end_time)),
        (7, kerberos_time(ticket.renew_till) if ticket.renew_till else None),
        (8, general_string(ticket.server.realm)),
        (9, principal_name(ticket.server)),
    )


def encode_enc_krb_cred_part(infos: Sequence[bytes], timestamp: int, usec: int) -> bytes:
    """EncKrbCredPart ::= [APPLICATION 29] SEQUENCE { ticket-info [0], nonce [1] OPT,
    timestamp [2] OPT, usec [3] OPT, ... }

    No addresses: tickets are forwarded addressless, as MIT does by default.
    """
    body = fields((0, sequence_of(infos)), (2, kerberos_time(timestamp)), (3, integer(usec)))
    return tlv(APP_ENC_KRB_CRED_PART, body)


def encode_krb_cred(tickets: Sequence[bytes], enc_part: EncryptedData) -> bytes:
    """KRB-CRED ::= [APPLICATION 22] SEQUENCE { pvno [0], msg-type [1],
    tickets [2] SEQUENCE OF Ticket, enc-part [3] }"""
    body = fields(
        (0, integer(PVNO)),
        (1, integer(KRB_CRED)),
        (2, sequence_of(tickets)),
        (3, enc_part.encode()),
    )
    return tlv(APP_KRB_CRED, body)


# -- reading -----------------------------------------------------------------


def _one(data: bytes, tag: int, what: str) -> Element:
    """Exactly one element with ``tag`` and nothing after it."""
    element, end = parse(data)
    if element.tag != tag:
        raise DERError(f"expected {what} (tag 0x{tag:02x}), got tag 0x{element.tag:02x}")
    if end != len(data):
        raise DERError(f"{len(data) - end} bytes of trailing data after {what}")
    return element


class Fields:
    """A SEQUENCE of ``[n]``-tagged fields, looked up by ``n``.

    Tags must be context-specific, constructed and strictly increasing, as
    DER requires; each field's single inner element is what lookups return.
    """

    __slots__ = ("_fields", "_what")

    def __init__(self, element: Element, what: str) -> None:
        if element.tag != TAG_SEQUENCE:
            raise DERError(f"{what} is not a SEQUENCE (tag 0x{element.tag:02x})")
        self._what = what
        self._fields: dict[int, Element] = {}
        last = -1
        for child in element.children():
            number = child.tag & 0x1F
            if child.tag & 0xE0 != 0xA0 or number <= last:
                raise DERError(f"{what} has a field out of order (tag 0x{child.tag:02x})")
            inner, end = parse(child.value)
            if end != len(child.value):
                raise DERError(f"{what} field [{number}] holds more than one value")
            self._fields[number] = inner
            last = number

    def get(self, number: int) -> Element:
        found = self._fields.get(number)
        if found is None:
            raise DERError(f"{self._what} lacks its required field [{number}]")
        return found

    def optional(self, number: int) -> Element | None:
        return self._fields.get(number)

    def integer(self, number: int) -> int:
        return read_integer(self.get(number))

    def string(self, number: int) -> str:
        return read_string(self.get(number))

    def octets(self, number: int) -> bytes:
        return read_octets(self.get(number))

    def time(self, number: int, default: int = 0) -> int:
        found = self.optional(number)
        return default if found is None else read_time(found)


def read_string(element: Element) -> str:
    if element.tag != TAG_GENERAL_STRING:
        raise DERError(f"expected GeneralString, got tag 0x{element.tag:02x}")
    return element.value.decode("utf-8", "replace")


def read_octets(element: Element) -> bytes:
    if element.tag != TAG_OCTET_STRING:
        raise DERError(f"expected OCTET STRING, got tag 0x{element.tag:02x}")
    return element.value


def read_time(element: Element) -> int:
    """KerberosTime as seconds since the epoch."""
    text = element.value.decode("ascii", "replace")
    if element.tag != TAG_GENERALIZED_TIME or len(text) != 15 or not text.endswith("Z"):
        raise DERError(f"malformed KerberosTime {text!r}")
    try:
        return calendar.timegm(time.strptime(text, "%Y%m%d%H%M%SZ"))
    except ValueError as exc:
        raise DERError(f"malformed KerberosTime {text!r}") from exc


def read_flags(element: Element) -> int:
    """KerberosFlags as the 32-bit integer MIT stores (bit 0 is the MSB)."""
    if element.tag != TAG_BIT_STRING or len(element.value) < 1:
        raise DERError("malformed KerberosFlags")
    bits = element.value[1:5].ljust(4, b"\x00")
    return int.from_bytes(bits, "big")


def read_principal_name(element: Element, realm: str) -> Principal:
    view = Fields(element, "PrincipalName")
    names = view.get(1)
    if names.tag != TAG_SEQUENCE:
        raise DERError("PrincipalName name-string is not a SEQUENCE")
    return Principal(tuple(read_string(part) for part in names.children()), realm, view.integer(0))


def read_encrypted_data(element: Element) -> EncryptedData:
    view = Fields(element, "EncryptedData")
    kvno = view.optional(1)
    return EncryptedData(
        view.integer(0), view.octets(2), None if kvno is None else read_integer(kvno)
    )


def read_key(element: Element) -> Key:
    view = Fields(element, "EncryptionKey")
    return Key(view.integer(0), view.octets(1))


@dataclass(frozen=True, **SLOTS)
class TicketInfo:
    """The cleartext outer fields of a Ticket; the rest is for the service."""

    server: Principal
    enc_part: EncryptedData


def decode_ticket(der: bytes) -> TicketInfo:
    """Ticket ::= [APPLICATION 1] SEQUENCE { tkt-vno [0], realm [1], sname [2], enc-part [3] }"""
    outer = _one(der, APP_TICKET, "Ticket")
    view = Fields(_one(outer.value, TAG_SEQUENCE, "Ticket body"), "Ticket")
    if view.integer(0) != PVNO:
        raise DERError(f"Ticket version {view.integer(0)} is not 5")
    server = read_principal_name(view.get(2), view.string(1))
    return TicketInfo(server, read_encrypted_data(view.get(3)))


@dataclass(frozen=True, **SLOTS)
class KdcReply:
    """KDC-REP: the new ticket in the clear, and the part only the client can open."""

    msg_type: int
    client: Principal
    ticket: bytes
    enc_part: EncryptedData


def decode_kdc_rep(der: bytes) -> KdcReply:
    """TGS-REP ::= [APPLICATION 13] KDC-REP

    ``KDC-REP ::= SEQUENCE { pvno [0], msg-type [1], padata [2] OPT, crealm [3], cname [4],
    ticket [5], enc-part [6] }``
    """
    outer = _one(der, APP_TGS_REP, "TGS-REP")
    view = Fields(_one(outer.value, TAG_SEQUENCE, "TGS-REP body"), "TGS-REP")
    ticket = view.get(5)
    return KdcReply(
        msg_type=view.integer(1),
        client=read_principal_name(view.get(4), view.string(3)),
        ticket=tlv(ticket.tag, ticket.value),
        enc_part=read_encrypted_data(view.get(6)),
    )


@dataclass(frozen=True, **SLOTS)
class EncKdcReplyPart:
    """EncKDCRepPart: the session key and the terms the KDC granted."""

    key: Key
    nonce: int
    flags: int
    auth_time: int
    start_time: int
    end_time: int
    renew_till: int
    server: Principal


def decode_enc_kdc_rep_part(der: bytes) -> EncKdcReplyPart:
    """EncTGSRepPart ::= [APPLICATION 26] EncKDCRepPart

    ``key [0], last-req [1], nonce [2], key-expiration [3] OPT, flags [4],
    authtime [5], starttime [6] OPT, endtime [7], renew-till [8] OPT,
    srealm [9], sname [10], caddr [11] OPT, encrypted-pa-data [12] OPT``.
    RFC 4120 section 5.4.2 asks clients to accept the AS tag (25) here too,
    because some KDCs have sent it.
    """
    outer, end = parse(der)
    if outer.tag not in (APP_ENC_TGS_REP_PART, APP_ENC_AS_REP_PART) or end != len(der):
        raise DERError(f"expected EncTGSRepPart, got tag 0x{outer.tag:02x}")
    view = Fields(_one(outer.value, TAG_SEQUENCE, "EncKDCRepPart body"), "EncKDCRepPart")
    auth_time = view.time(5)
    return EncKdcReplyPart(
        key=read_key(view.get(0)),
        nonce=view.integer(2),
        flags=read_flags(view.get(4)),
        auth_time=auth_time,
        start_time=view.time(6, auth_time),
        end_time=view.time(7),
        renew_till=view.time(8),
        server=read_principal_name(view.get(10), view.string(9)),
    )


@dataclass(frozen=True, **SLOTS)
class KrbError:
    """The fields of a KRB-ERROR worth telling a person about."""

    code: int
    realm: str
    server: Principal
    text: str
    server_time: int


def decode_krb_error(der: bytes) -> KrbError:
    """KRB-ERROR ::= [APPLICATION 30] SEQUENCE { ... error-code [6], realm [9],
    sname [10], e-text [11] OPT ... }"""
    outer = _one(der, APP_KRB_ERROR, "KRB-ERROR")
    view = Fields(_one(outer.value, TAG_SEQUENCE, "KRB-ERROR body"), "KRB-ERROR")
    text = view.optional(11)
    realm = view.string(9)
    return KrbError(
        code=view.integer(6),
        realm=realm,
        server=read_principal_name(view.get(10), realm),
        text="" if text is None else read_string(text),
        server_time=view.time(4),
    )
