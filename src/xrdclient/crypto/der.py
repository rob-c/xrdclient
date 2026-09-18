"""A minimal DER reader — enough for PKCS#1, PKCS#8 and X.509.

DER is the encoding every X.509 structure this client meets is written in:
a tag byte, a length, and that many content bytes, nested. Only the handful
of universal types the certificates and keys actually use are decoded here;
anything else comes back as a raw :class:`Element` for the caller to look at
or ignore.

This exists so that GSI needs no third-party parser. It is a *reader*: it
never writes DER, and it is deliberately strict, because a lenient parser of
attacker-supplied structures is a liability.
"""

from __future__ import annotations

from dataclasses import dataclass

from .._compat import SLOTS

__all__ = [
    "DERError",
    "Element",
    "parse",
    "parse_all",
    "read_integer",
    "oid_string",
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
    """One tag-length-value triple."""

    tag: int
    value: bytes

    @property
    def constructed(self) -> bool:
        """True for SEQUENCE, SET, and the explicit context-specific tags."""
        return bool(self.tag & 0x20)

    def children(self) -> list[Element]:
        """Parse the content as a sequence of elements."""
        if not self.constructed:
            raise DERError(f"tag 0x{self.tag:02x} is primitive and has no children")
        return parse_all(self.value)

    def __getitem__(self, index: int) -> Element:
        return self.children()[index]

    def __repr__(self) -> str:
        return f"Element(tag=0x{self.tag:02x}, len={len(self.value)})"


def _read_length(data: bytes, pos: int) -> tuple[int, int]:
    if pos >= len(data):
        raise DERError("truncated: no length byte")
    first = data[pos]
    pos += 1
    if first < 0x80:
        return first, pos
    count = first & 0x7F
    if count == 0:
        raise DERError("indefinite lengths are not valid DER")
    if count > 4:
        raise DERError(f"length of length {count} is implausible")
    if pos + count > len(data):
        raise DERError("truncated: long-form length runs past the end")
    return int.from_bytes(data[pos : pos + count], "big"), pos + count


def parse(data: bytes, pos: int = 0) -> tuple[Element, int]:
    """Read one element starting at ``pos``; returns it and the next offset."""
    if pos >= len(data):
        raise DERError("truncated: no tag byte")
    tag = data[pos]
    if tag & 0x1F == 0x1F:
        raise DERError("multi-byte tags are not supported")
    length, pos = _read_length(data, pos + 1)
    end = pos + length
    if end > len(data):
        raise DERError(f"truncated: element claims {length} bytes, {len(data) - pos} available")
    return Element(tag, data[pos:end]), end


def parse_all(data: bytes) -> list[Element]:
    """Read every element in ``data``, which must be consumed exactly."""
    out: list[Element] = []
    pos = 0
    while pos < len(data):
        element, pos = parse(data, pos)
        out.append(element)
    return out


def read_integer(element: Element) -> int:
    """A DER INTEGER as a Python ``int`` (two's complement, any width)."""
    if element.tag != TAG_INTEGER:
        raise DERError(f"expected INTEGER, got tag 0x{element.tag:02x}")
    if not element.value:
        raise DERError("INTEGER with no content")
    return int.from_bytes(element.value, "big", signed=True)


def oid_string(element: Element) -> str:
    """An OBJECT IDENTIFIER in dotted form, e.g. ``"1.2.840.113549.1.1.1"``."""
    if element.tag != TAG_OID:
        raise DERError(f"expected OBJECT IDENTIFIER, got tag 0x{element.tag:02x}")
    data = element.value
    if not data:
        raise DERError("OBJECT IDENTIFIER with no content")
    first = data[0]
    parts = [str(min(first // 40, 2)), str(first - 40 * min(first // 40, 2))]
    value = 0
    for index, byte in enumerate(data[1:], start=1):
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(str(value))
            value = 0
        elif index == len(data) - 1:
            raise DERError("OBJECT IDENTIFIER ends mid-arc")
    return ".".join(parts)
