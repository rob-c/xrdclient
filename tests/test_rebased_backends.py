"""Contracts for shared write policies and library-backed storage/PKI parsing."""

from __future__ import annotations

import struct
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from _pki import make_certificate, name, throwaway_key
from xrdclient._xml import UnsafeXML
from xrdclient.config import Config
from xrdclient.crypto import x509 as certificates
from xrdclient.errors import ProtocolError, ServerError
from xrdclient.proto import constants as c
from xrdclient.proto.machine import SessionMachine, State
from xrdclient.s3 import _codec
from xrdclient.session.bulk import BulkReader, BulkUnsupported


class _Wire:
    def __init__(self, statuses=(), *, reverse=False, lost=False):
        self.statuses = iter(statuses)
        self.reverse, self.lost = reverse, lost
        self.packets = deque()
        self.buffer = bytearray()
        self.sent = []

    def send(self, part):
        self.sent.append(bytes(part))
        if isinstance(part, memoryview) or self.lost:
            return
        sid = int.from_bytes(part[:2], "big")
        status = next(self.statuses, c.kXR_ok)
        body = (3009).to_bytes(4, "big") + b"full\0" if status == c.kXR_error else b""
        self.packets.append(struct.pack(">HHI", sid, status, len(body)) + body)
        if status == c.kXR_oksofar:
            self.packets.append(struct.pack(">HHI", sid, c.kXR_ok, 0))

    def receive_into(self, view):
        if not self.buffer:
            packets = list(self.packets)
            self.packets.clear()
            self.buffer.extend(b"".join(reversed(packets) if self.reverse else packets))
        count = min(len(view), len(self.buffer))
        view[:count] = self.buffer[:count]
        del self.buffer[:count]
        return count


def _writer(statuses=(), **wire_options):
    machine = SessionMachine(host="test")
    machine.state = State.READY
    session = SimpleNamespace(
        machine=machine,
        transport=_Wire(statuses, **wire_options),
        config=Config(request_timeout=0.01),
        broken=False,
    )
    session.mark_broken = lambda: setattr(session, "broken", True)
    return BulkReader(session, bytes(4), chunk=4, depth=2), session


def _pieces(*values):
    return [memoryview(value) for value in values]


def test_write_acknowledgements_report_ranges_in_reply_order():
    writer, session = _writer(reverse=True)
    acknowledged = []
    checked = []
    moved = writer.write_chunks(
        _pieces(b"abcd", b"ef", b"", b"gh"),
        10,
        acknowledged=lambda at, count: acknowledged.append((at, count)),
        check=lambda: checked.append(True),
    )
    assert moved == 8
    assert acknowledged == [(14, 2), (10, 4), (16, 2)]
    assert len(checked) == 3
    assert not writer._owed and not writer._leased and not session.broken


def test_wait_only_defers_the_declined_range_and_does_not_replay_it():
    writer, session = _writer([c.kXR_wait, c.kXR_ok])
    waited, acknowledged = [], []
    moved = writer.write_chunks(
        _pieces(b"abcd", b"ef"),
        20,
        waited=lambda at, count: waited.append((at, count)),
        acknowledged=lambda at, count: acknowledged.append((at, count)),
        strict_replies=True,
    )
    assert (moved, waited, acknowledged) == (2, [(20, 4)], [(24, 2)])
    assert len(session.transport.sent) == 4
    assert not session.broken and not writer._owed


def test_native_wait_fallback_remains_available():
    writer, session = _writer([c.kXR_wait, c.kXR_ok])
    with pytest.raises(BulkUnsupported, match="falls back"):
        writer.write_from(memoryview(b"abcdef"), 0)
    assert not session.broken and not writer._owed


def test_refusal_stops_new_writes_but_reports_other_acknowledgements():
    writer, session = _writer([c.kXR_error, c.kXR_ok])
    acknowledged = []
    with pytest.raises(ServerError, match="full"):
        writer.write_chunks(
            _pieces(b"abcd", b"ef", b"gh"),
            acknowledged=lambda at, count: acknowledged.append((at, count)),
        )
    assert acknowledged == [(4, 2)]
    assert len(session.transport.sent) == 4
    assert not writer._owed and not writer._leased and not session.broken


@pytest.mark.parametrize("strict", [False, True])
def test_error_status_is_not_mistaken_for_an_unexpected_reply(strict):
    writer, session = _writer([c.kXR_error])
    with pytest.raises(ServerError, match="full"):
        writer.write_chunks(_pieces(b"x"), strict_replies=strict)
    assert not session.broken


def test_strict_unexpected_reply_breaks_the_session():
    writer, session = _writer([c.kXR_redirect])
    with pytest.raises(ProtocolError, match="unexpected reply to a write"):
        writer.write_chunks(_pieces(b"x"), strict_replies=True)
    assert session.broken


def test_partial_acknowledgement_is_not_double_counted():
    writer, _ = _writer([c.kXR_oksofar])
    assert writer.write_chunks(_pieces(b"abcd")) == 4


def test_empty_and_oversized_write_chunks_release_their_leases():
    writer, session = _writer()
    assert writer.write_chunks(_pieces(b"")) == 0
    with pytest.raises(ValueError, match="chunk size"):
        writer.write_chunks(_pieces(b"12345"))
    assert not writer._leased and not session.broken


@pytest.mark.parametrize("hook", ["acknowledged", "waited", "check"])
def test_callback_failures_keep_their_cause_and_settle_the_session(hook):
    writer, session = _writer([c.kXR_wait] if hook == "waited" else [])
    failure = RuntimeError(hook)

    def fail(*arguments):
        raise failure

    with pytest.raises(RuntimeError) as caught:
        writer.write_chunks(_pieces(b"x", b"y"), **{hook: fail})
    assert caught.value is failure
    assert not writer._leased and not writer._owed and not session.broken


def test_failed_cleanup_does_not_mask_cancellation():
    writer, session = _writer(lost=True)
    failure = RuntimeError("cancelled")

    def cancel():
        raise failure

    with pytest.raises(RuntimeError) as caught:
        writer.write_chunks(_pieces(b"x"), check=cancel)
    assert caught.value is failure and session.broken


def test_an_iterator_failure_happens_before_stream_ids_are_leased():
    writer, session = _writer()

    class Broken:
        def __iter__(self):
            raise RuntimeError("source unavailable")

    with pytest.raises(RuntimeError, match="source unavailable"):
        writer.write_chunks(Broken())
    assert not writer._leased and not session.broken


def test_s3_models_do_not_discover_ambient_credentials(monkeypatch):
    def forbidden(*arguments):
        raise AssertionError("credentials must remain the transport adapter's policy")

    monkeypatch.setattr(_codec.Session, "get_credentials", forbidden)
    _codec._model.cache_clear()
    answer = _codec.decode(
        "CreateMultipartUpload",
        b"<InitiateMultipartUploadResult><UploadId>x</UploadId></InitiateMultipartUploadResult>",
    )
    assert answer["UploadId"] == "x"


@pytest.mark.parametrize("operation", ["CopyObject", "CompleteMultipartUpload"])
@pytest.mark.parametrize("root", ["Error", "ErrorResponse"])
def test_namespaced_errors_inside_http_200_are_modeled(operation, root):
    error = "<Error><Code>SlowDown</Code><Message>try later</Message></Error>"
    body = error if root == "Error" else f"<ErrorResponse>{error}</ErrorResponse>"
    body = body.replace(f"<{root}>", f'<{root} xmlns="urn:s3:test">', 1)
    answer = _codec.decode(operation, body.encode())
    assert answer["Error"]["Code"] == "SlowDown"
    assert answer["Error"]["Message"] == "try later"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le", "utf-16-be"])
def test_s3_declarations_never_reach_the_library_parser(encoding):
    with pytest.raises(UnsafeXML):
        _codec.decode("ListObjectsV2", '<!DOCTYPE x [<!ENTITY a "value">]><x/>'.encode(encoding))


@pytest.mark.parametrize("size", ["", "-1", "unknown", "5"])
def test_s3_size_tolerance_is_a_facade_policy(size):
    body = f"<ListBucketResult><Contents><Size>{size}</Size></Contents></ListBucketResult>".encode()
    answer = _codec.decode("ListObjectsV2", body, lenient=True)
    assert answer["Contents"][0]["Size"] == (int(size) if size.isdigit() else 0)
    if size == "unknown":
        with pytest.raises(ValueError):
            _codec.decode("ListObjectsV2", body)
    else:
        assert _codec.decode("ListObjectsV2", body)["Contents"][0]["Size"] == int(size or 0)


def test_s3_manifest_escaping_is_owned_by_botocore():
    tags = ['"a&b"', '"x<y"']
    root = ET.fromstring(_codec.manifest(tags))
    assert [node.text for node in root.iter() if node.tag.endswith("}ETag")] == tags
    assert [node.text for node in root.iter() if node.tag.endswith("}PartNumber")] == ["1", "2"]


@pytest.mark.parametrize("operation", ["ListObjects", "ListObjectsV2", "CreateMultipartUpload"])
def test_an_empty_required_s3_response_is_not_a_success(operation):
    with pytest.raises(ET.ParseError):
        _codec.decode(operation, b"")


@pytest.mark.parametrize("payload", [b"", b" \n\t"])
def test_empty_copy_completion_remains_accepted(payload):
    assert "Error" not in _codec.decode("CopyObject", payload)
    assert "Error" not in _codec.decode("CompleteMultipartUpload", payload)


def test_listing_items_hide_markers_and_descendants():
    page = {
        "CommonPrefixes": [{"Prefix": "p/"}, {"Prefix": "p/sub/"}],
        "Contents": [{"Key": "p/"}, {"Key": "p/a"}, {"Key": "p/deep/b"}],
    }
    assert list(_codec.listing_items(page, "p/")) == [("sub", None), ("a", {"Key": "p/a"})]
    assert list(_codec.listing_items({}, "")) == []
    assert _codec.modified({}) == 0


def test_normal_certificates_are_parsed_by_cryptography(monkeypatch):
    key = throwaway_key(0)
    subject = name(("2.5.4.3", "library certificate"))
    der = make_certificate(subject, subject, key.public, key)

    def forbidden(*arguments):
        raise AssertionError("a normal certificate must not use legacy parsing")

    monkeypatch.setattr(certificates, "_legacy_certificate_data", forbidden)
    monkeypatch.setattr(certificates, "certificate_fields", forbidden)
    monkeypatch.setattr(certificates, "parse_one", forbidden)
    result = certificates.certificate_data(der)
    assert result.subject.cn == "library certificate"
    assert result.der == der and result.public_key == key.public
    assert result.signature and result.tbs and result.spki


def _ec_certificate():
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "EC root")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime(2020, 1, 1, tzinfo=timezone.utc))
        .not_valid_after(datetime(2040, 1, 1, tzinfo=timezone.utc))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.DER)


def test_real_ec_certificates_keep_the_existing_non_rsa_view():
    result = certificates.certificate_data(_ec_certificate())
    assert result.subject.cn == "EC root" and result.public_key is None


@pytest.mark.parametrize(
    "error", [ValueError("unreadable key"), UnsupportedAlgorithm("unknown key")]
)
def test_an_unusable_public_key_does_not_lose_certificate_metadata(monkeypatch, error):
    cert = x509.load_der_x509_certificate(_ec_certificate())

    def unusable():
        raise error

    view = SimpleNamespace(
        subject=cert.subject,
        issuer=cert.issuer,
        extensions=cert.extensions,
        serial_number=cert.serial_number,
        not_valid_before_utc=cert.not_valid_before_utc,
        not_valid_after_utc=cert.not_valid_after_utc,
        public_key=unusable,
        signature=cert.signature,
        tbs_certificate_bytes=cert.tbs_certificate_bytes,
        signature_algorithm_oid=cert.signature_algorithm_oid,
    )
    monkeypatch.setattr(certificates._x509, "load_der_x509_certificate", lambda data: view)
    result = certificates.certificate_data(_ec_certificate())
    assert result.subject.cn == "EC root" and result.public_key is None
