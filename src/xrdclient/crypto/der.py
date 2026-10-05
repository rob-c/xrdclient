"""DER compatibility helpers backed by asn1crypto.

The public Element API is retained for protocol-specific structures. The
library owns TLV parsing/encoding and ASN.1 primitive decoding; this adapter
keeps the clients\' definite-length, single-byte-tag input policy.
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass

from asn1crypto import core, parser  # type: ignore[import-untyped]

from .._compat import SLOTS

__all__ = [
    "raw_children",
    "encode",
    "encode_integer",
    "encode_oid",
    "encode_time",
    "decode_time",
    "DERError",
    "Element",
    "parse",
    "parse_one",
    "parse_all",
    "read_integer",
    "oid_string",
    "tlv",
    "integer",
    "oid",
    "sequence",
    "set_of",
    "octet_string",
    "bit_string",
    "boolean",
    "null",
    "utf8_string",
    "printable_string",
    "utc_time",
    "generalized_time",
    "validity_time",
    "explicit",
    "TAG_BOOLEAN",
    "TAG_INTEGER",
    "TAG_BIT_STRING",
    "TAG_OCTET_STRING",
    "TAG_NULL",
    "TAG_OID",
    "TAG_UTF8_STRING",
    "TAG_SEQUENCE",
    "TAG_SET",
    "TAG_PRINTABLE_STRING",
    "TAG_IA5_STRING",
    "TAG_UTC_TIME",
    "TAG_GENERALIZED_TIME",
]

TAG_BOOLEAN = 0x01
TAG_INTEGER = 0x02
TAG_BIT_STRING = 0x03
TAG_OCTET_STRING = 0x04
TAG_NULL = 0x05
TAG_OID = 0x06
TAG_UTF8_STRING = 0x0C
TAG_SEQUENCE = 0x30
TAG_SET = 0x31
TAG_PRINTABLE_STRING = 0x13
TAG_IA5_STRING = 0x16
TAG_UTC_TIME = 0x17
TAG_GENERALIZED_TIME = 0x18


class DERError(ValueError):
    """The bytes given are not the DER structure they claim to be."""


@dataclass(frozen=True, **SLOTS)
class Element:
    """One tag-length-value triple; ``encoded`` re-emits it byte for byte."""

    tag: int
    value: bytes

    @property
    def constructed(self) -> bool:
        return bool(self.tag & 0x20)

    @property
    def encoded(self) -> bytes:
        return tlv(self.tag, self.value)

    def children(self) -> list[Element]:
        if not self.constructed:
            raise DERError(f"tag 0x{self.tag:02x} is primitive and has no children")
        return parse_all(self.value)

    def __getitem__(self, index: int) -> Element:
        return self.children()[index]

    def __repr__(self) -> str:
        return f"Element(tag=0x{self.tag:02x}, len={len(self.value)})"


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _check_header(data: bytes, pos: int) -> None:
    if pos < 0 or pos >= len(data):
        raise DERError("truncated: no tag byte")
    if data[pos] & 0x1F == 0x1F:
        raise DERError("multi-byte tags are not supported")
    if pos + 1 >= len(data):
        raise DERError("truncated: no length byte")
    count = data[pos + 1]
    if count == 0x80:
        raise DERError("indefinite lengths are not valid DER")
    if count > 0x80:
        _check_long_length(data, pos, count & 0x7F)


def _check_long_length(data: bytes, pos: int, count: int) -> None:
    if count > 4:
        raise DERError(f"length of length {count} is implausible")
    if pos + 2 + count > len(data):
        raise DERError("truncated: long-form length runs past the end")


def parse(data: bytes, pos: int = 0) -> tuple[Element, int]:
    """Read one definite-length element, preserving the existing API."""
    _check_header(data, pos)
    try:
        class_, method, tag, header, content, _ = parser.parse(bytes(data[pos:]))
    except ValueError as exc:
        raise DERError(f"truncated DER element: {exc}") from exc
    return Element((class_ << 6) | (method << 5) | tag, content), pos + len(header) + len(content)


def parse_one(data: bytes) -> Element:
    element, end = parse(data)
    if end != len(data):
        raise DERError(f"{len(data) - end} trailing bytes after the element")
    return element


def parse_all(data: bytes) -> list[Element]:
    out: list[Element] = []
    pos = 0
    while pos < len(data):
        element, pos = parse(data, pos)
        out.append(element)
    return out


def read_integer(element: Element) -> int:
    if element.tag != TAG_INTEGER:
        raise DERError(f"expected INTEGER, got tag 0x{element.tag:02x}")
    if not element.value:
        raise DERError("INTEGER with no content")
    return int.from_bytes(element.value, "big", signed=True)


def oid_string(element: Element) -> str:
    if element.tag != TAG_OID:
        raise DERError(f"expected OBJECT IDENTIFIER, got tag 0x{element.tag:02x}")
    if not element.value:
        raise DERError("OBJECT IDENTIFIER with no content")
    if element.value[-1] & 0x80:
        raise DERError("OBJECT IDENTIFIER ends mid-arc")
    return str(core.ObjectIdentifier.load(parser.emit(0, 0, TAG_OID, element.value)).dotted)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def tlv(tag: int, value: bytes) -> bytes:
    return bytes(parser.emit(tag >> 6, (tag >> 5) & 1, tag & 0x1F, value))


def integer(value: int) -> bytes:
    return bytes(core.Integer(value).dump())


def oid(dotted: str) -> bytes:
    return bytes(core.ObjectIdentifier(dotted).dump())


def sequence(*parts: bytes) -> bytes:
    return tlv(TAG_SEQUENCE, b"".join(parts))


def set_of(*parts: bytes) -> bytes:
    """A SET OF, members sorted as DER requires."""
    return tlv(TAG_SET, b"".join(sorted(parts)))


def octet_string(data: bytes) -> bytes:
    return tlv(TAG_OCTET_STRING, data)


def bit_string(data: bytes) -> bytes:
    return tlv(TAG_BIT_STRING, b"\x00" + data)


def boolean(value: bool) -> bytes:
    return tlv(TAG_BOOLEAN, b"\xff" if value else b"\x00")


def null() -> bytes:
    return tlv(TAG_NULL, b"")


def utf8_string(text: str) -> bytes:
    return tlv(TAG_UTF8_STRING, text.encode("utf-8"))


def printable_string(text: str) -> bytes:
    return tlv(TAG_PRINTABLE_STRING, text.encode("ascii"))


def utc_time(when: float) -> bytes:
    return tlv(TAG_UTC_TIME, time.strftime("%y%m%d%H%M%SZ", time.gmtime(when)).encode())


def generalized_time(when: float) -> bytes:
    return tlv(TAG_GENERALIZED_TIME, time.strftime("%Y%m%d%H%M%SZ", time.gmtime(when)).encode())


def validity_time(when: float) -> bytes:
    """RFC 5280: UTCTime through 2049, GeneralizedTime from 2050."""
    year = time.gmtime(when).tm_year
    return utc_time(when) if year < 2050 else generalized_time(when)


def explicit(number: int, inner: bytes) -> bytes:
    """A context-specific constructed tag ``[number] EXPLICIT``."""
    return tlv(0xA0 | number, inner)


def decode_time(element: Element) -> float:
    """``UTCTime``/``GeneralizedTime`` as a UNIX timestamp."""
    text = element.value.decode("ascii", "replace").strip()
    if text.endswith("Z"):
        text = text[:-1]
    if element.tag == TAG_UTC_TIME:
        if len(text) < 10:
            raise DERError(f"malformed UTCTime {text!r}")
        year = int(text[:2])
        text = f"{2000 + year if year < 50 else 1900 + year}{text[2:]}"
    elif element.tag != TAG_GENERALIZED_TIME:
        raise DERError(f"tag 0x{element.tag:02x} is not a certificate time")
    text = (text + "000000")[:14]
    try:
        parsed = time.strptime(text, "%Y%m%d%H%M%S")
    except ValueError as exc:
        raise DERError(f"malformed time {text!r}") from exc
    return float(calendar.timegm(parsed))


def raw_children(data: bytes) -> list[bytes]:
    """The children of the one constructed element in ``data``, byte for byte.

    :meth:`Element.children` decodes; this keeps each child exactly as it was
    written, which is what a signature covers and what a copied extension or
    name must reproduce.
    """
    outer, _ = parse(data)
    if not outer.constructed:
        raise DERError(f"tag 0x{outer.tag:02x} is primitive and has no children")
    out: list[bytes] = []
    pos = 0
    while pos < len(outer.value):
        _element, end = parse(outer.value, pos)
        out.append(outer.value[pos:end])
        pos = end
    return out


encode = tlv
encode_integer = integer


def encode_oid(dotted: str) -> bytes:
    if len(dotted.split(".")) < 2:
        raise DERError("OBJECT IDENTIFIER needs at least two arcs")
    try:
        return bytes(core.ObjectIdentifier(dotted).dump())
    except ValueError as exc:
        raise DERError(f"invalid OBJECT IDENTIFIER {dotted!r}: {exc}") from exc


def encode_time(when: float) -> bytes:
    """A certificate time: UTCTime through 2049, GeneralizedTime after (RFC 5280)."""
    import time

    moment = time.gmtime(int(when))
    if moment.tm_year < 2050:
        return encode(TAG_UTC_TIME, time.strftime("%y%m%d%H%M%SZ", moment).encode("ascii"))
    return encode(TAG_GENERALIZED_TIME, time.strftime("%Y%m%d%H%M%SZ", moment).encode("ascii"))
