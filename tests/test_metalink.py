"""Metalink virtual redirectors, from descriptor through replica copy."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

import xrdclient
from xrdclient.copy import engine
from xrdclient.copy.replicas import NoMoreReplicasError
from xrdclient.metalink import MAX_DESCRIPTOR_SIZE, MAX_REPLICAS, MetalinkError


def meta4(name: str, urls: list[tuple[int, str]], body: bytes) -> bytes:
    replicas = "".join(f'<url priority="{priority}">{url}</url>' for priority, url in urls)
    digest = hashlib.sha256(body).hexdigest()
    return (
        '<metalink xmlns="urn:ietf:params:xml:ns:metalink">'
        f'<file name="{name}"><size>{len(body)}</size>'
        f'<hash type="sha-256">{digest}</hash>{replicas}</file></metalink>'
    ).encode()


def test_local_meta4_fails_over_and_verifies_the_declared_checksum(server, tmp_path: Path) -> None:
    body = b"the bytes named by every replica"
    server.add_file("/replicas/good", body)
    descriptor = tmp_path / "sample.meta4"
    missing = str(server.url / "replicas/missing")
    good = str(server.url / "replicas/good")
    descriptor.write_bytes(meta4("sample.bin", [(1, missing), (2, good)], body))

    target = tmp_path / "sample.bin"
    result = xrdclient.copy(descriptor, target)

    assert target.read_bytes() == body
    assert result.source == f"file://{descriptor}"
    assert result.replica == good
    assert result.checksum == xrdclient.ChecksumInfo("sha256", hashlib.sha256(body).hexdigest())


def test_a_corrupt_replica_is_replaced_by_the_next_one(server, tmp_path: Path) -> None:
    body = b"right"
    server.add_file("/bad", b"wrong")
    server.add_file("/good", body)
    descriptor = tmp_path / "integrity.meta4"
    good = str(server.url / "good")
    descriptor.write_bytes(meta4("integrity.bin", [(1, str(server.url / "bad")), (2, good)], body))

    result = xrdclient.copy(
        descriptor,
        tmp_path / "out",
        config=xrdclient.Config(bulk=False, wait_budget=0, max_metalink_wait=0),
    )

    assert (tmp_path / "out").read_bytes() == body
    assert result.replica == good


def test_remote_metalink_3_uses_highest_preference_first(server, tmp_path: Path) -> None:
    body = b"remote descriptor"
    adler32 = xrdclient.crypto.checksum_bytes("adler32", body)
    server.add_file("/r", body)
    descriptor = f"""<?xml version="1.0"?>
      <metalink version="3.0" xmlns="http://www.metalinker.org/">
        <files><file name="remote.bin"><size>{len(body)}</size>
          <verification><hash type="a32">{adler32}</hash></verification>
          <resources>
            <url preference="1">{server.url / "missing"}</url>
            <url preference="100">{server.url / "r"}</url>
          </resources>
        </file></files>
      </metalink>""".encode()
    server.add_file("/catalog/remote.metalink", descriptor)
    source = server.url / "catalog/remote.metalink"

    result = xrdclient.copy(source, tmp_path / "remote.bin")

    assert (tmp_path / "remote.bin").read_bytes() == body
    assert result.replica == str(server.url / "r")
    assert result.source == str(source)


def test_dry_run_uses_metadata_without_touching_a_replica(tmp_path: Path) -> None:
    descriptor = tmp_path / "dry.meta4"
    descriptor.write_bytes(meta4("dry.bin", [(1, "root://no.example//f")], b"1234"))

    result = xrdclient.copy(descriptor, tmp_path / "out", dry_run=True)

    assert result.size == 4 and result.seconds == 0
    assert not (tmp_path / "out").exists()


def test_remove_source_removes_the_descriptor_not_the_replica(tmp_path: Path) -> None:
    replica = tmp_path / "payload"
    replica.write_bytes(b"payload")
    descriptor = tmp_path / "move.meta4"
    descriptor.write_bytes(meta4("payload", [(1, replica.as_uri())], b"payload"))

    xrdclient.copy(descriptor, tmp_path / "out", remove_source=True)

    assert not descriptor.exists()
    assert replica.read_bytes() == b"payload"


def test_zip_member_can_be_selected_through_a_metalink(tmp_path: Path) -> None:
    import io
    import zipfile

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("nested/member.bin", b"selected bytes")
    archive_path = tmp_path / "replica.zip"
    archive_path.write_bytes(stream.getvalue())
    descriptor = tmp_path / "archive.meta4"
    descriptor.write_bytes(
        meta4("replica.zip", [(1, archive_path.as_uri())], archive_path.read_bytes())
    )
    source = xrdclient.parse(descriptor).with_query(**{"xrdcl.unzip": "nested/member.bin"})

    result = xrdclient.copy(source, tmp_path / "member.bin", algorithm="sha-256")

    assert (tmp_path / "member.bin").read_bytes() == b"selected bytes"
    assert result.source == str(source)
    assert result.replica == archive_path.as_uri()

    checked_descriptor = tmp_path / "checked.meta4"
    checked_descriptor.write_bytes(
        meta4("member.bin", [(1, archive_path.as_uri())], b"selected bytes")
    )
    checked_source = xrdclient.parse(checked_descriptor).with_query(
        **{"xrdcl.unzip": "nested/member.bin"}
    )
    checked = tmp_path / "checked.bin"
    xrdclient.copy(
        checked_source,
        checked,
        algorithm="sha-256",
        config=xrdclient.Config(zip_metalink_checksum=True),
    )
    assert checked.read_bytes() == b"selected bytes"


def test_cli_metalink_zip_flags_select_and_verify_a_member(tmp_path: Path) -> None:
    import io
    import zipfile

    from xrdclient.cli import cp

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("member", b"cli member")
    replica = tmp_path / "replica.zip"
    replica.write_bytes(stream.getvalue())
    descriptor = tmp_path / "cli.meta4"
    descriptor.write_bytes(meta4("member", [(1, replica.as_uri())], b"cli member"))
    target = tmp_path / "out"

    assert (
        cp.main(
            [
                "--zip",
                "member",
                "--zip-mtln-cksum",
                "--tlsmetalink",
                os.fspath(descriptor),
                os.fspath(target),
            ]
        )
        == 0
    )
    assert target.read_bytes() == b"cli member"


def test_metalink_failover_does_not_swallow_process_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = tmp_path / "interrupt.meta4"
    descriptor.write_bytes(meta4("f", [(1, "file:///replica")], b"x"))

    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(engine, "_copy_one", interrupted)
    with pytest.raises(KeyboardInterrupt):
        xrdclient.copy(descriptor, tmp_path / "out")


def test_metalink_processing_can_be_disabled(tmp_path: Path) -> None:
    descriptor = tmp_path / "literal.meta4"
    descriptor.write_bytes(b"not XML when copied literally")

    xrdclient.copy(
        descriptor,
        tmp_path / "literal-copy",
        config=xrdclient.Config(metalink_processing=False),
    )

    assert (tmp_path / "literal-copy").read_bytes() == descriptor.read_bytes()


def test_all_failed_replicas_report_the_last_failure(tmp_path: Path) -> None:
    descriptor = tmp_path / "none.meta4"
    descriptor.write_bytes(
        meta4(
            "none",
            [(1, (tmp_path / "missing-1").as_uri()), (2, (tmp_path / "missing-2").as_uri())],
            b"x",
        )
    )

    with pytest.raises(NoMoreReplicasError, match="no more replicas") as caught:
        xrdclient.copy(descriptor, tmp_path / "out")

    assert isinstance(caught.value.__cause__, FileNotFoundError)


def test_parser_accepts_relative_urls_deduplicates_and_ignores_unknown_hashes() -> None:
    document = """<metalink xmlns="urn:ietf:params:xml:ns:metalink">
      <file name="f"><size>not-a-number</size>
        <hash type="unknown">xx</hash><hash type="md5">bad</hash>
        <url priority="bad">../f</url><url>../f</url><url></url>
      </file></metalink>"""

    parsed = xrdclient.parse_metalink(document, base_url="root://host:1094//catalog/x.meta4")

    assert parsed.replicas == ("root://host:1094//f",)
    assert parsed.size is None and parsed.checksum is None

    network = xrdclient.parse_metalink(
        "<metalink><file name='f'><url>//mirror.example/f</url></file></metalink>",
        base_url="root://origin.example//catalog/f.meta4",
    )
    assert network.replicas == ("root://mirror.example/f",)
    triple_slash = xrdclient.parse_metalink(
        "<metalink><file name='f'><url>../f</url></file></metalink>",
        base_url="root://origin.example///catalog/f.meta4",
    )
    assert triple_slash.replicas == ("root://origin.example//f",)


@pytest.mark.parametrize(
    "document, message",
    [
        ("<not-metalink/>", "root"),
        ("<metalink/>", "exactly one"),
        ("<metalink><file/></metalink>", "no name"),
        ("<metalink><file name='f'/></metalink>", "no usable"),
        ("<metalink><file name='f'><url>relative</url></file></metalink>", "no usable"),
        ("<metalink><file name='a'/><file name='b'/></metalink>", "exactly one"),
        ("<metalink>", "Malformed"),
        ("<!DOCTYPE x><metalink/>", "DOCTYPE"),
    ],
)
def test_malformed_or_unusable_documents_are_rejected(document: str, message: str) -> None:
    with pytest.raises(MetalinkError, match=message):
        xrdclient.parse_metalink(document)


def test_descriptor_and_replica_limits_are_enforced() -> None:
    with pytest.raises(MetalinkError, match="limit"):
        xrdclient.parse_metalink(b" " * (MAX_DESCRIPTOR_SIZE + 1))
    urls = "".join(f"<url>file:///tmp/{index}</url>" for index in range(MAX_REPLICAS + 1))
    with pytest.raises(MetalinkError, match="more than"):
        xrdclient.parse_metalink(f"<metalink><file name='f'>{urls}</file></metalink>")


def test_too_long_and_scheme_less_replicas_are_skipped() -> None:
    too_long = "file:///" + "x" * 4097
    document = (
        "<metalink><file name='f'><url>relative</url>"
        f"<url>{too_long}</url><url>file:///tmp/f</url></file></metalink>"
    )
    assert xrdclient.parse_metalink(document).replicas == ("file:///tmp/f",)


def test_tls_metalink_upgrades_only_xrootd_protocols() -> None:
    assert engine._secure_metalink_url("root://host//f") == "roots://host:1094//f"
    assert engine._secure_metalink_url("xroot://host//f") == "xroots://host:1094//f"
    assert engine._secure_metalink_url("https://host/f") == "https://host/f"


def test_streams_are_not_mistaken_for_descriptors() -> None:
    class NamedStream:
        def read(self) -> bytes:
            return b""

        def __str__(self) -> str:
            return "looks.meta4"

    from xrdclient.metalink import is_metalink

    assert not is_metalink(NamedStream())
    assert is_metalink("file:///tmp/UPPER.META4?token=x")
