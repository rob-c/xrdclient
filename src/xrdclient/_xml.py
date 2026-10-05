"""DTD-free XML loading using only Python's built-in parsers.

Expat checks declarations before ElementTree builds a tree. Its callbacks are
encoding-aware and reject declarations wherever they occur, including when
the ElementTree C accelerator is unavailable. No external resources are read.
"""

from __future__ import annotations

from typing import NoReturn
from xml.etree import ElementTree as ET
from xml.parsers import expat

__all__ = ["UnsafeXML", "fromstring"]


class UnsafeXML(ValueError):
    """XML that uses document types, declared entities or external references."""


def _reject_declaration(*arguments: object) -> NoReturn:
    raise UnsafeXML("XML document type and entity declarations are not allowed")


def fromstring(data: bytes | str) -> ET.Element:
    """Reject declarations before building a standard-library element tree."""
    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = _reject_declaration
    parser.EntityDeclHandler = _reject_declaration
    parser.ExternalEntityRefHandler = _reject_declaration
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        error = ET.ParseError(str(exc))
        error.code = exc.code
        error.position = (exc.lineno, exc.offset)
        raise error from None
    return ET.fromstring(data)
