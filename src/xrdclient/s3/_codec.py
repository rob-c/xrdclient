"""S3 operation models over the clients' existing HTTP transports."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from xml.etree import ElementTree as ET

from botocore.parsers import RestXMLParser  # type: ignore[import-untyped]
from botocore.serialize import create_serializer  # type: ignore[import-untyped]
from botocore.session import Session  # type: ignore[import-untyped]
from botocore.utils import parse_timestamp  # type: ignore[import-untyped]

from .._xml import fromstring


@lru_cache(maxsize=1)
def _model() -> Any:
    # Loading a service model neither discovers credentials nor contacts AWS.
    return Session().get_service_model("s3")


def _timestamp(value: str) -> datetime:
    try:
        return parse_timestamp(value)  # type: ignore[no-any-return]
    except ValueError:
        return datetime.fromtimestamp(0, timezone.utc)


class _Parser(RestXMLParser):  # type: ignore[misc]
    def __init__(self, root: ET.Element | None, lenient: bool) -> None:
        super().__init__(timestamp_parser=_timestamp)
        self.root = root
        self.lenient = lenient

    def _parse_xml_string_to_dom(self, payload: bytes) -> ET.Element | None:
        return self.root

    def _handle_integer(self, shape: Any, node: ET.Element) -> int:
        text = (node.text or "").strip()
        return 0 if self.lenient and not text.isdigit() else int(text or 0)

    _handle_long = _handle_integer


def decode(
    operation: str,
    payload: bytes,
    *,
    status: int = 200,
    headers: Mapping[str, str] | None = None,
    lenient: bool = False,
) -> dict[str, Any]:
    """Decode a modeled answer, including errors embedded in HTTP 200.

    Declaration checks run before botocore sees the tree; namespaces and
    modeled timestamps, numbers, lists and headers are handled by botocore.
    """
    root = (
        fromstring(payload)
        if payload.strip() or operation in ("ListObjects", "ListObjectsV2", "CreateMultipartUpload")
        else None
    )
    embedded_error = root is not None and root.tag.rpartition("}")[2] in ("Error", "ErrorResponse")
    if embedded_error:
        # Botocore's S3 error discriminator compares the root tag literally.
        root.tag = root.tag.rpartition("}")[2]  # type: ignore[union-attr]
    response = {
        "status_code": 400 if embedded_error else status,
        "headers": dict(headers or {}),
        "body": payload if root is not None else b"",
    }
    result: dict[str, Any] = _Parser(root, lenient).parse(
        response, _model().operation_model(operation).output_shape
    )
    return result


def manifest(etags: Sequence[str]) -> bytes:
    """Serialize completion parts with the S3 model and XML escaping."""
    request = create_serializer("rest-xml").serialize_to_request(
        {
            "Bucket": "unused",
            "Key": "unused",
            "UploadId": "unused",
            "MultipartUpload": {
                "Parts": [{"PartNumber": n, "ETag": tag} for n, tag in enumerate(etags, 1)]
            },
        },
        _model().operation_model("CompleteMultipartUpload"),
    )
    return bytes(request["body"])


def listing_items(page: dict[str, Any], prefix: str) -> Iterator[tuple[str, dict[str, Any] | None]]:
    """Common prefixes first, then immediate objects; hide folder markers."""
    for directory in page.get("CommonPrefixes", []):
        name = directory.get("Prefix", "")[len(prefix) :].rstrip("/")
        if name:
            yield name, None
    for item in page.get("Contents", []):
        name = item.get("Key", "")[len(prefix) :]
        if name and "/" not in name:
            yield name, item


def modified(item: dict[str, Any]) -> int:
    stamp = item.get("LastModified")
    return int(stamp.timestamp()) if stamp is not None else 0
