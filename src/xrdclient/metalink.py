"""Metalink 3 and 4 download descriptions.

XrdCl treats a local or remote ``.meta4``/``.metalink`` file as a virtual
redirector.  The document must describe exactly one file; its URLs are tried
in priority order and its checksums describe the bytes every replica must
contain.  This module is the deliberately small, dependency-free parser for
that metadata.  Moving the bytes remains the copy engine's job.

Parsing is bounded before :mod:`xml.etree` sees the input.  Metalink files are
metadata, not datasets: accepting an unbounded document or XML entity
declarations would turn replica discovery into a memory-exhaustion surface.
"""

from __future__ import annotations

import posixpath
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from ._compat import SLOTS
from .crypto import algorithms
from .crypto import new as new_checksum
from .errors import MetalinkError
from .types import ChecksumInfo

__all__ = [
    "MAX_DESCRIPTOR_SIZE",
    "MAX_REPLICAS",
    "MetalinkError",
    "MetalinkFile",
    "is_metalink",
    "parse_metalink",
]

#: A descriptor is metadata.  Eight MiB leaves room for many thousands of
#: replicas while putting a hard ceiling on hostile input.
MAX_DESCRIPTOR_SIZE = 8 << 20
#: XrdCl limits one URL to 4096 bytes.  Bound the list as well, so a valid but
#: adversarial document cannot make failover run forever.
MAX_URL_SIZE = 4096
MAX_REPLICAS = 10_000

_FORBIDDEN_XML = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_HEX = frozenset("0123456789abcdefABCDEF")
_ALIASES = {"a32": "adler32", "sha": "sha1"}
_SUPPORTED = frozenset(algorithms())


@dataclass(frozen=True, **SLOTS)
class MetalinkFile:
    """The one file described by a Metalink document."""

    name: str
    replicas: tuple[str, ...]
    checksums: tuple[ChecksumInfo, ...] = ()
    size: int | None = None

    @property
    def checksum(self) -> ChecksumInfo | None:
        """XrdCl's default: the first supported checksum alphabetically."""
        return self.checksums[0] if self.checksums else None


def is_metalink(value: object) -> bool:
    """Whether ``value`` names a Metalink file, as XrdCl's ``URL`` decides."""
    if hasattr(value, "read") or hasattr(value, "write"):
        return False
    text = str(value)
    path = urllib.parse.urlsplit(text).path if "://" in text else text.partition("?")[0]
    lowered = path.lower()
    return lowered.endswith(".meta4") or lowered.endswith(".metalink")


def parse_metalink(
    document: bytes | bytearray | memoryview | str, *, base_url: str = ""
) -> MetalinkFile:
    """Parse one bounded Metalink 3 or 4 document.

    ``base_url`` is used only for relative replica references.  RFC 5854 URLs
    are normally absolute, but resolving a relative one is both harmless and
    useful for hermetic catalogues.
    """
    data = document.encode() if isinstance(document, str) else bytes(document)
    root = _parse_document(data)
    element = _one_file(root)
    name = (element.get("name") or "").strip()
    if not name:
        raise MetalinkError("Metalink <file> has no name")
    version = (root.get("version") or "4").strip()
    replicas = _replicas(element, base_url, version)
    if not replicas:
        raise MetalinkError("Metalink file contains no usable replica URL")
    return MetalinkFile(name, replicas, _checksums(element), _size(element))


def _parse_document(data: bytes) -> ET.Element:
    """Parse XML only after enforcing the descriptor's resource bounds."""
    if len(data) > MAX_DESCRIPTOR_SIZE:
        raise MetalinkError(
            f"Metalink descriptor is {len(data)} bytes; limit is {MAX_DESCRIPTOR_SIZE}"
        )
    if _FORBIDDEN_XML.search(data):
        raise MetalinkError("Metalink descriptors may not declare a DOCTYPE or XML entities")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise MetalinkError(f"Malformed or corrupted Metalink file: {exc}") from None
    if _local_name(root.tag) != "metalink":
        raise MetalinkError("Metalink document root must be <metalink>")
    return root


def _one_file(root: ET.Element) -> ET.Element:
    """Return the sole file XrdCl permits one descriptor to contain."""
    files = [element for element in root.iter() if _local_name(element.tag) == "file"]
    if len(files) != 1:
        raise MetalinkError(f"Expected exactly one file per Metalink, found {len(files)}")
    return files[0]


def _local_name(tag: str) -> str:
    return tag.rpartition("}")[2].lower()


def _descendants(element: ET.Element, name: str) -> list[ET.Element]:
    return [
        child for child in element.iter() if child is not element and _local_name(child.tag) == name
    ]


def _replicas(element: ET.Element, base_url: str, version: str) -> tuple[str, ...]:
    ranked: list[tuple[int, int, str]] = []
    is_v3 = version.startswith("3")
    for order, node in enumerate(_descendants(element, "url")):
        candidate = _replica(node, base_url, is_v3, order)
        if candidate is None:
            continue
        ranked.append(candidate)
        if len(ranked) > MAX_REPLICAS:
            raise MetalinkError(f"Metalink contains more than {MAX_REPLICAS} replica URLs")
    ranked.sort()
    # Do not spend the retry budget twice on a duplicated URL.
    return tuple(dict.fromkeys(url for _, _, url in ranked))


def _replica(
    node: ET.Element, base_url: str, is_v3: bool, order: int
) -> tuple[int, int, str] | None:
    """One usable URL and its version-specific sort key."""
    text = (node.text or "").strip()
    if not text or len(text.encode()) > MAX_URL_SIZE:
        return None
    url = _absolute(base_url, text)
    if not _has_scheme(url):
        return None
    attribute, default = ("preference", 0) if is_v3 else ("priority", 999_999)
    rank = _integer(node.get(attribute), default)
    # Metalink 3 preference is high-first; Metalink 4 priority is low-first.
    return (-rank if is_v3 else rank, order, url)


def _checksums(element: ET.Element) -> tuple[ChecksumInfo, ...]:
    found: dict[str, ChecksumInfo] = {}
    for node in _descendants(element, "hash"):
        algorithm = _algorithm(node.get("type") or "")
        value = "".join((node.text or "").split())
        if algorithm is None or not _valid_digest(algorithm, value):
            continue
        found.setdefault(algorithm, ChecksumInfo(algorithm, value.lower()))
    return tuple(found[name] for name in sorted(found))


def _algorithm(name: str) -> str | None:
    normal = name.strip().lower().replace("-", "").replace("_", "")
    normal = _ALIASES.get(normal, normal)
    return normal if normal in _SUPPORTED else None


def _valid_digest(algorithm: str, value: str) -> bool:
    width = len(new_checksum(algorithm).hexdigest())
    return len(value) == width and all(char in _HEX for char in value)


def _size(element: ET.Element) -> int | None:
    nodes = _descendants(element, "size")
    text = (nodes[0].text or "").strip() if nodes else ""
    return int(text) if text.isdigit() else None


def _integer(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _has_scheme(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return bool(parsed.scheme and (parsed.netloc or parsed.scheme == "file"))


def _absolute(base: str, reference: str) -> str:
    """Resolve ``reference`` for schemes :func:`urllib.parse.urljoin` does not know."""
    if urllib.parse.urlsplit(reference).scheme or not base:
        return reference
    parsed = urllib.parse.urlsplit(base)
    if reference.startswith("//"):
        return f"{parsed.scheme}:{reference}"
    path = (
        reference
        if reference.startswith("/")
        else posixpath.join(posixpath.dirname(parsed.path), reference)
    )
    path = posixpath.normpath(path)
    if parsed.path.startswith("//") and not path.startswith("//"):
        path = "/" + path
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
