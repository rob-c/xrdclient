"""How the protocols use safe XML.

The DTD-free parser itself is :mod:`xrdcore.safexml` since 0.4.0 and is tested
there (``xrdcore/tests/test_safexml.py``: every encoding, both ElementTree
parsers, the hostile documents); this file checks that each protocol turns
its refusal into the protocol's own error.
"""

from __future__ import annotations

import pytest
from xrdhttp.connection import HTTPMessage

from xrdclient.errors import MetalinkError, ProtocolError
from xrdclient.http.client import Response
from xrdclient.http.dav import _parse
from xrdclient.metalink import parse_metalink
from xrdclient.s3.fs import _answer


def test_protocols_translate_unsafe_xml_rejections():
    with pytest.raises(ProtocolError, match="document type"):
        _parse(b"<!DOCTYPE doc><doc/>")
    with pytest.raises(MetalinkError, match="DOCTYPE"):
        parse_metalink(b"<!DOCTYPE metalink><metalink/>")
    with pytest.raises(ProtocolError, match="forbidden document type"):
        refused = Response(200, "OK", HTTPMessage(), b"<!DOCTYPE doc><doc/>")
        _answer(refused, "listing", "ListObjectsV2")
