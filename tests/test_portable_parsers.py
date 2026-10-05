"""Portable XML loading and bounded local credential-cache readers."""

from __future__ import annotations

import struct
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from xrdclient._xml import UnsafeXML, fromstring
from xrdclient.auth.kerberos import ccache
from xrdclient.auth.kerberos.model import Principal, Ticket
from xrdclient.errors import MetalinkError, ProtocolError
from xrdclient.http.dav import _parse
from xrdclient.metalink import parse_metalink
from xrdclient.s3.fs import _answer


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le", "utf-16-be"])
@pytest.mark.parametrize("padding", [0, 20_000])
@pytest.mark.parametrize(
    "declaration", ["<!DOCTYPE doc>", '<!DOCTYPE doc SYSTEM "urn:test:unused">']
)
def test_parser_rejects_dtds_in_every_encoding(encoding, padding, declaration):
    with pytest.raises(UnsafeXML):
        fromstring((" " * padding + declaration + "<doc/>").encode(encoding))


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le", "utf-16-be"])
def test_normal_xml_keeps_stdlib_elements_and_namespaces(encoding):
    root = fromstring('<doc xmlns="urn:test"><item>hello</item></doc>'.encode(encoding))
    assert isinstance(root, ET.Element)
    assert root.findtext("{urn:test}item") == "hello"


def test_comment_text_is_not_mistaken_for_a_declaration():
    assert fromstring(b"<!-- <!DOCTYPE doc> --><doc/>").tag == "doc"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le", "utf-16-be"])
def test_internal_declarations_are_rejected_before_tree_construction(monkeypatch, encoding):
    from xrdclient import _xml as local_xml

    def unexpected_tree(*args):
        raise AssertionError("a rejected document must not reach tree construction")

    monkeypatch.setattr(local_xml.ET, "fromstring", unexpected_tree)
    document = '<!DOCTYPE doc [<!ENTITY content "hello">]><doc/>'
    with pytest.raises(UnsafeXML):
        fromstring(document.encode(encoding))


@pytest.mark.parametrize(
    "handler", ["StartDoctypeDeclHandler", "EntityDeclHandler", "ExternalEntityRefHandler"]
)
def test_each_expat_declaration_callback_rejects_the_input(monkeypatch, handler):
    from xrdclient import _xml as local_xml

    class DeclarationProbe:
        def Parse(self, data, final):
            getattr(self, handler)()

    monkeypatch.setattr(local_xml.expat, "ParserCreate", DeclarationProbe)
    with pytest.raises(UnsafeXML, match="not allowed"):
        fromstring(b"<doc/>")


def test_predefined_entities_and_literal_declaration_text_are_harmless():
    assert fromstring(b"<doc>A&amp;B</doc>").text == "A&B"
    assert fromstring(b"<doc><![CDATA[<!DOCTYPE doc>]]></doc>").text == "<!DOCTYPE doc>"


def test_malformed_xml_preserves_stdlib_exception_details():
    with pytest.raises(ET.ParseError) as error:
        fromstring(b"<doc>")
    assert error.value.code == 3
    assert error.value.position == (1, 5)


def test_namespace_errors_remain_stdlib_parse_errors():
    with pytest.raises(ET.ParseError, match="unbound prefix"):
        fromstring(b"<unknown:doc/>")


def test_protocols_translate_unsafe_xml_rejections():
    with pytest.raises(ProtocolError, match="document type"):
        _parse(b"<!DOCTYPE doc><doc/>")
    with pytest.raises(MetalinkError, match="DOCTYPE"):
        parse_metalink(b"<!DOCTYPE metalink><metalink/>")
    with pytest.raises(ProtocolError, match="forbidden document type"):
        _answer(
            SimpleNamespace(body=b"<!DOCTYPE doc><doc/>", headers={}), "listing", "ListObjectsV2"
        )


def _ticket():
    return Ticket(
        Principal(("alice",), "EXAMPLE", 1),
        Principal(("xrootd", "host"), "EXAMPLE", 2),
        18,
        100,
        101,
        200,
        300,
        0x40000000,
        b"opaque-ticket",
        bytes(range(32)),
    )


@pytest.mark.parametrize("version", [ccache.CCACHE_VERSION_3, ccache.CCACHE_VERSION_4])
def test_local_cache_reader_round_trips_both_versions(version):
    ticket = _ticket()
    blob = ccache.marshal_ticket(ticket)
    if version == ccache.CCACHE_VERSION_3:
        offset = len(ccache.marshal_principal(ticket.client))
        offset += len(ccache.marshal_principal(ticket.server)) + 2
        blob = blob[:offset] + struct.pack(">H", ticket.enctype) + blob[offset:]
    reader = ccache._Reader(blob)
    restored = ccache._read_entry(reader, version, 12.5)
    assert restored == ticket
    assert restored.kdc_offset == 12.5
    assert restored.key == ticket.key
    assert reader.exhausted


@pytest.mark.parametrize("fraction", range(32))
def test_truncated_local_cache_keeps_value_error(fraction):
    blob = ccache.marshal_ticket(_ticket())
    with pytest.raises(ValueError, match="truncated"):
        ccache.read_credential(blob[: len(blob) * fraction // 32])


def test_partial_cache_tail_keeps_complete_credentials(tmp_path):
    ticket = _ticket()
    path = tmp_path / "cache"
    path.write_bytes(
        struct.pack(">HH", ccache.CCACHE_VERSION_4, 0)
        + ccache.marshal_principal(ticket.client)
        + ccache.marshal_ticket(ticket)
        + b"\0"
    )
    client, entries = ccache.read_ccache(str(path))
    assert client == ticket.client
    assert entries == [ticket]
