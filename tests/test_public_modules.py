"""The public names of 0.4.0's building blocks, the private names they replaced, and the libraries.

``xrdclient.report``, ``safexml``, ``retry`` and ``log`` were private modules
(``cli._output``, ``_xml``, ``_retry``, ``_log``) that xgfalclient and xrdarc
imported anyway. They are public now, and their implementations live in
xrdcore: every name below is the *same module object* as the library's - not
a copy - so state that lives in the module (the active report, the jitter
source) is one thing whichever name reached it, and whichever library set it.
The behaviour of each module is tested in the library's suite; this file tests
the names, and what xrdclient adds on top (its logger hierarchy, its URL type
in reports).

The public HTTP modules that moved to xrdhttp (``http.expect``, ``s3.sigv4``)
are xrdhttp's module objects the same way, and the three that keep a facade
(``metalink``, ``http.tape``, ``http.tpc``) re-export xrdhttp's objects for
every name they share. The pre-0.4.0 *private* names (``cli._output``,
``_xml``, ``_retry``, ``_log``, ``copy._pipeline``, ``http._engine`` and the
other ``http._*``, ``s3._codec``, ``_cancel``, ``_fanout``) were removed once no downstream
package imported them; :data:`REMOVED` holds that line.
"""

from __future__ import annotations

import ast
import importlib
import io
import json
import logging
import pathlib

import pytest
import xrdcore.log

from xrdclient import log, report, safexml

#: Every xrdclient module name that is an xrdcore module, and which one.
ALIASES = [
    ("xrdclient.report", "xrdcore.report"),
    ("xrdclient.safexml", "xrdcore.safexml"),
    ("xrdclient.retry", "xrdcore.retry"),
    ("xrdclient.crypto.checksum", "xrdcore.checksum"),
    ("xrdclient.crypto.crc64", "xrdcore.crc64"),
    ("xrdclient.http.expect", "xrdhttp.expect"),
    ("xrdclient.s3.sigv4", "xrdhttp.s3.sigv4"),
    ("xrdclient.auth.kerberos.asn1", "xrdsec.krb5.asn1"),
    ("xrdclient.auth.kerberos.ccache", "xrdsec.krb5.ccache"),
    ("xrdclient.auth.kerberos.kcm", "xrdsec.krb5.kcm"),
    ("xrdclient.auth.kerberos.kdc", "xrdsec.krb5.kdc"),
    ("xrdclient.auth.kerberos.keyring", "xrdsec.krb5.keyring"),
    ("xrdclient.auth.kerberos.model", "xrdsec.krb5.model"),
    ("xrdclient.auth.kerberos.profile", "xrdsec.krb5.profile"),
    ("xrdclient.auth.kerberos.tgs", "xrdsec.krb5.tgs"),
    ("xrdclient.crypto.rfc3961", "xrdsec.krb5.rfc3961"),
]

#: Pre-0.4.0 private module names removed in 0.4.0, and where the code is now.
REMOVED = [
    ("xrdclient.cli._output", "xrdclient.report"),
    ("xrdclient._xml", "xrdclient.safexml"),
    ("xrdclient._retry", "xrdclient.retry"),
    ("xrdclient._log", "xrdclient.log"),
    ("xrdclient.copy._pipeline", "xrdcore.pipeline"),
    ("xrdclient.http._engine", "xrdhttp.engine"),
    ("xrdclient.http._connection", "xrdhttp.connection"),
    ("xrdclient.http._wire", "xrdhttp.wire"),
    ("xrdclient.http._ranges", "xrdhttp.ranges"),
    ("xrdclient.http._webdav", "xrdhttp.webdav"),
    ("xrdclient.s3._codec", "xrdhttp.s3.codec"),
    ("xrdclient._cancel", "xrdcore.cancel"),
    ("xrdclient._fanout", "xrdcore.fanout"),
]


@pytest.mark.parametrize(("old", "new"), ALIASES)
def test_the_old_name_is_the_same_module(old, new):
    old_module, new_module = importlib.import_module(old), importlib.import_module(new)
    assert old_module is new_module
    assert set(getattr(new_module, "__all__", ())) <= set(dir(old_module))


@pytest.mark.parametrize(("old", "new"), REMOVED)
def test_a_removed_private_name_is_gone_and_its_replacement_imports(old, new):
    """No alias is left to drift: the old name fails, the replacement works."""
    importlib.import_module(new)
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(old)


#: Every xrdclient module that re-exports a library module's names one by one
#: (``from lib import X as X``) for type checkers, which cannot see the module
#: swap itself in ``sys.modules``: the file, relative to the package, and the
#: library module whose ``__all__`` it must spell out exactly.
RE_EXPORTS = [
    ("credentials.py", "xrdsec.credentials"),
    ("voms_client.py", "xrdsec.voms_client"),
    ("tls.py", "xrdsec.tls"),
    ("retry.py", "xrdcore.retry"),
    ("safexml.py", "xrdcore.safexml"),
    ("report.py", "xrdcore.report"),
    ("crypto/aes.py", "xrdsec.aes"),
    ("crypto/checksum.py", "xrdcore.checksum"),
    ("crypto/crc64.py", "xrdcore.crc64"),
    ("crypto/delegation.py", "xrdsec.delegation"),
    ("crypto/der.py", "xrdsec.der"),
    ("crypto/ed25519.py", "xrdsec.ed25519"),
    ("crypto/p256.py", "xrdsec.p256"),
    ("crypto/proxies.py", "xrdsec.proxies"),
    ("crypto/rsa.py", "xrdsec.rsa"),
    ("crypto/signatures.py", "xrdsec.signatures"),
    ("crypto/trust.py", "xrdsec.trust"),
    ("crypto/userproxy.py", "xrdsec.userproxy"),
    ("crypto/voms.py", "xrdsec.voms"),
    ("crypto/x509.py", "xrdsec.x509"),
    ("s3/sigv4.py", "xrdhttp.s3.sigv4"),
    ("http/expect.py", "xrdhttp.expect"),
    ("testing/pki.py", "xrdsec.testing.pki"),
    ("testing/voms.py", "xrdsec.testing.voms"),
    ("auth/gsi.py", "xrdsec.gsi.secgsi"),
    ("auth/kerberos/asn1.py", "xrdsec.krb5.asn1"),
    ("auth/kerberos/ccache.py", "xrdsec.krb5.ccache"),
    ("auth/kerberos/kcm.py", "xrdsec.krb5.kcm"),
    ("auth/kerberos/kdc.py", "xrdsec.krb5.kdc"),
    ("auth/kerberos/keyring.py", "xrdsec.krb5.keyring"),
    ("auth/kerberos/model.py", "xrdsec.krb5.model"),
    ("auth/kerberos/profile.py", "xrdsec.krb5.profile"),
    ("auth/kerberos/tgs.py", "xrdsec.krb5.tgs"),
    ("crypto/rfc3961.py", "xrdsec.krb5.rfc3961"),
]


def _re_exported(source: str) -> dict[str, set[str]]:
    """The ``from module import X as X`` names in ``source``, by module.

    A relative import is keyed by its dots and name (``..._gridpaths``), so it
    cannot be mistaken for the library module's.
    """
    found: dict[str, set[str]] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            for alias in node.names:
                if alias.asname == alias.name and alias.name != "__all__":
                    found.setdefault(module, set()).add(alias.name)
    return found


@pytest.mark.parametrize(("path", "library"), RE_EXPORTS)
def test_the_explicit_re_exports_name_exactly_the_librarys_public_names(path, library):
    """``import *`` is not used, so this is what keeps the spelled-out list honest.

    A name the library adds to ``__all__`` must be added here too, or a type
    checker would reject what works at runtime; a name it drops must go.
    ``crypto.x509`` keeps its own ``default_proxy_path`` (the pre-0.4.0
    signature), so that one name comes from ``.._gridpaths`` instead, and it
    also re-exports two names xrdsec keeps out of its ``__all__``.
    """
    import xrdclient

    source = (pathlib.Path(xrdclient.__file__).parent / path).read_text()
    exported = _re_exported(source)
    expected = set(importlib.import_module(library).__all__)
    named = exported.get(library, set())
    if path == "crypto/x509.py":
        assert exported.get(".._gridpaths") == {"default_proxy_path"}
        named = named | {"default_proxy_path"}
        expected = expected | {"CertificateData", "certificate_data"}
    assert named == expected


#: Names a facade module shares with its xrdhttp counterpart, by identity.
SHARED = [
    (
        "xrdclient.metalink",
        "xrdhttp.metalink",
        ["MAX_DESCRIPTOR_SIZE", "MAX_REPLICAS", "MAX_URL_SIZE", "XRDCL", "MetalinkError"],
    ),
    ("xrdclient.metalink", "xrdhttp.metalink", ["MetalinkFile", "MetalinkPolicy", "resolve"]),
    ("xrdclient.metalink", "xrdhttp.metalink", ["is_metalink", "parse_metalink"]),
    (
        "xrdclient.http.tape",
        "xrdhttp.tape",
        ["TapeEndpoint", "api_version", "collapse", "entry_for", "json_text"],
    ),
    ("xrdclient.http.tape", "xrdhttp.tape", ["listed_endpoints", "newest_endpoint", "API"]),
    ("xrdclient.http.tpc", "xrdhttp.tpc", ["Marker", "Outcome", "follow_markers", "MAX_LINE"]),
    ("xrdclient.http.client", "xrdhttp.redirects", ["wire_path"]),
    (
        "xrdclient.auth.kerberos.caches",
        "xrdsec.krb5.caches",
        ["CredentialCache", "FileCache", "open_ccache"],
    ),
    (
        "xrdclient.auth.krb5",
        "xrdsec.krb5.client",
        ["DEFAULT_ENCTYPES", "tickets"],
    ),
    ("xrdclient.auth.krb5", "xrdsec.krb5.model", ["Principal", "Ticket"]),
    (
        "xrdclient.auth.krb5",
        "xrdsec.krb5.ccache",
        ["CCACHE_VERSION_3", "CCACHE_VERSION_4", "read_ccache"],
    ),
    ("xrdclient.testing.http", "xrdhttp.testing", ["Handler", "Reply", "BOUNDARY", "DAVEntry"]),
    (
        "xrdclient.testing.http",
        "xrdhttp.testing",
        ["dav_entries", "multistatus", "propfind_reply", "ranged_reply"],
    ),
    (
        "xrdclient.testing.http",
        "xrdsec.testing.https",
        ["FakeHTTPSServer", "Fault", "Request", "Response", "TLSServer", "read_body"],
    ),
]

#: Private names of the pre-0.4.0 WebDAV and S3 fakes, whose implementation is
#: now xrdhttp's shared fake storage element (``xrdhttp.testing``), and of the
#: HTTP client, whose machinery is now ``xrdhttp.session``.
REMOVED_NAMES = [
    (
        "xrdclient.testing.http",
        ["_Handler", "_Server", "_TPCFailure", "_clean", "_decoded", "_endpoint", "_fetch"],
    ),
    ("xrdclient.testing.http", ["_digest_header", "_repr_digest_header", "_DIGESTS"]),
    ("xrdclient.testing.s3", ["_Handler", "_Server", "_Page", "_listing_page", "_listing"]),
    ("xrdclient.testing.s3", ["_sorted_query", "_md5", "_find", "_MODIFIED"]),
    # The pool, the redirect and retry loops and the response bookkeeping of
    # ``HTTPClient`` are xrdhttp's ``session.Session`` since 0.4.0.
    (
        "xrdclient.http.client",
        ["_redirected", "_key", "_checked", "_drain", "_redirect", "_wrap", "DRAIN_LIMIT"],
    ),
    ("xrdclient.http.client", ["_TOKEN_FIELDS"]),
    # The facade's lazily made, fork-aware pool is ``xrdcore.fanout.Pool``.
    ("xrdclient.aio", ["_executor", "_pool_lock"]),
]


@pytest.mark.parametrize(("module", "names"), REMOVED_NAMES)
def test_a_removed_private_name_of_a_fake_is_gone(module, names):
    """The fakes are presets over ``xrdhttp.testing``, the HTTP client a policy over
    ``xrdhttp.session``; no private copy is left."""
    found = importlib.import_module(module)
    assert [name for name in names if hasattr(found, name)] == []


def test_the_fakes_are_presets_of_the_shared_storage_element():
    from xrdhttp.testing import StorageServer

    from xrdclient.testing import FakeDAVServer, FakeS3Server

    assert issubclass(FakeDAVServer, StorageServer) and issubclass(FakeS3Server, StorageServer)


@pytest.mark.parametrize(("ours", "theirs", "names"), SHARED)
def test_a_facade_re_exports_the_librarys_objects(ours, theirs, names):
    """xgfalclient imports these by xrdclient's names; they must be xrdhttp's."""
    mine, library = importlib.import_module(ours), importlib.import_module(theirs)
    for name in names:
        assert getattr(mine, name) is getattr(library, name), name


def test_metalink_answers_this_packages_checksum_type():
    """A descriptor's checksums are ``xrdclient.ChecksumInfo``: one type, xrdcore's."""
    import xrdcore.checksum
    import xrdhttp.metalink

    from xrdclient import ChecksumInfo, parse_metalink

    assert ChecksumInfo is xrdcore.checksum.ChecksumInfo is xrdhttp.metalink.ChecksumInfo
    document = (
        '<metalink xmlns="urn:ietf:params:xml:ns:metalink"><file name="f">'
        '<hash type="adler32">0000abcd</hash><url>root://a//f</url></file></metalink>'
    )
    parsed = parse_metalink(document)
    assert parsed.checksum == ChecksumInfo("adler32", "0000abcd")
    assert type(parsed.checksum) is ChecksumInfo
    assert parsed.replicas == ("root://a//f",)


def test_the_crc32c_module_keeps_the_page_framing_and_re_exports_the_crc():
    """Not an alias: XRootD's 4 KiB page framing is xrdclient's own."""
    import xrdcore.crc32c

    from xrdclient.crypto import crc32c as module
    from xrdclient.crypto.crc32c import IS_ACCELERATED, crc32c, crc32c_py

    assert crc32c is xrdcore.crc32c.crc32c
    assert crc32c_py is xrdcore.crc32c.crc32c_py
    assert not hasattr(module, "_crc32c_py")  # the pre-0.4.0 private name is gone
    assert IS_ACCELERATED is xrdcore.crc32c.IS_ACCELERATED


def test_the_log_names_are_xrdcores_objects():
    """One filter and one mute table on the whole stack, whichever name set them."""
    assert log.redact is xrdcore.log.redact
    assert log.RedactingFilter is xrdcore.log.RedactingFilter
    assert log.mute is xrdcore.log.mute
    assert log._filter is xrdcore.log.FILTER
    assert log._filter in logging.getLogger("xrdclient").filters


# ---------------------------------------------------------------------------
# xrdclient.log
# ---------------------------------------------------------------------------


def test_a_tools_own_logger_can_redact_the_same_way():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(log.RedactingFilter())
    logger = logging.getLogger("a-tool-built-on-xrdclient")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("GET %s", "https://ce.example/jobs?access_token=s3cr3t")
    finally:
        logger.removeHandler(handler)
    assert "s3cr3t" not in stream.getvalue()
    assert "access_token=<redacted>" in stream.getvalue()


def test_get_logger_lives_under_the_package_hierarchy():
    assert log.get_logger("tool").name == "xrdclient.tool"
    assert log.redact("Authorization: Bearer abc.def") == "Authorization: Bearer <redacted>"


# ---------------------------------------------------------------------------
# xrdclient.report and xrdclient.safexml
# ---------------------------------------------------------------------------


def test_a_tool_writes_a_versioned_report(capsys):
    """The shape of a third tool's command: Parser, flags, identify, record, run_cli."""

    def main(argv: list[str]) -> int:
        parser = report.Parser(prog="tool")
        report.flags(parser)
        parser.add_argument("name")
        args = parser.parse_args(argv)
        report.identify("greet", name=args.name)
        report.record(greeting=f"hello {args.name}")
        return 0

    argv = ["--json", "world"]
    assert report.run_cli("tool", argv, lambda: main(argv)) == 0
    document = json.loads(capsys.readouterr().out)
    assert (document["schema"], document["version"]) == (report.SCHEMA, report.VERSION)
    assert document["tool"] == "tool"
    assert document["records"][0]["greeting"] == "hello world"
    assert document["summary"]["ok"] is True


def test_safexml_parses_and_refuses_declarations():
    assert safexml.fromstring(b"<a><b/></a>")[0].tag == "b"
    with pytest.raises(safexml.UnsafeXML):
        safexml.fromstring('<!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>')
