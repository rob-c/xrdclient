"""S3: the signature, the bucket as a filesystem, and multipart uploads."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

import xrdclient
import xrdclient.s3.fs as s3fs
from xrdclient.config import Config
from xrdclient.errors import (
    BusyError,
    ExistsError,
    NotFoundError,
    ProtocolError,
    ServerError,
    UnsupportedError,
)
from xrdclient.http import HTTPClient
from xrdclient.s3 import Credentials, S3FileSystem, S3RawIO, hash_payload, open_s3, sign
from xrdclient.s3.sigv4 import EMPTY_SHA256, UNSIGNED_PAYLOAD
from xrdclient.testing import FakeS3Server

#: The account every AWS worked example is signed with.
ACCESS = "AKIAIOSFODNN7EXAMPLE"
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
CREDENTIALS = Credentials(ACCESS, SECRET)

OBJECTS = {
    "runs/2024/a.root": b"hello",
    "runs/2024/b.root": b"world!",
    "runs/2023/c.root": b"x",
    "top.txt": b"top",
}


@pytest.fixture
def bucket():
    """A running S3 endpoint holding a handful of objects."""
    with FakeS3Server(objects=dict(OBJECTS)) as server:
        yield server


@pytest.fixture
def fs(bucket):
    """A filesystem over it, signing with the credentials it demands."""
    with S3FileSystem(
        bucket.url, credentials=CREDENTIALS, endpoint=bucket.endpoint
    ) as filesystem:
        yield filesystem


@pytest.fixture(autouse=True)
def no_aws_environment(monkeypatch):
    """Nothing here may pick up the machine's own AWS configuration."""
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_PROFILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_ENDPOINT_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Credentials, "from_file", classmethod(lambda cls, *a, **k: None))


# ---------------------------------------------------------------------------
# Signature Version 4
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "signature"),
    [
        # Both of these are worked examples from AWS's own documentation, so
        # what they check is that this signer agrees with the specification
        # rather than merely with itself.
        ("/?lifecycle", "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"),
        (
            "/?max-keys=2&prefix=J",
            "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7",
        ),
    ],
)
def test_a_signature_matches_the_worked_example_aws_publishes(target, signature):
    headers = sign(
        "GET",
        target,
        "examplebucket.s3.amazonaws.com",
        {},
        EMPTY_SHA256,
        credentials=CREDENTIALS,
        region="us-east-1",
        when=datetime(2013, 5, 24, tzinfo=timezone.utc),
    )
    assert headers["Authorization"] == (
        f"AWS4-HMAC-SHA256 Credential={ACCESS}/20130524/us-east-1/s3/aws4_request, "
        f"SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature={signature}"
    )
    assert headers["x-amz-date"] == "20130524T000000Z"


def test_only_the_host_and_the_amazon_headers_are_signed():
    headers = sign(
        "GET",
        "/k",
        "h",
        {"Range": "bytes=0-9", "x-amz-acl": "private"},
        EMPTY_SHA256,
        credentials=CREDENTIALS,
        region="us-east-1",
    )
    names = headers["Authorization"].partition("SignedHeaders=")[2].partition(",")[0]
    assert names == "host;x-amz-acl;x-amz-content-sha256;x-amz-date"


def test_a_temporary_credential_signs_its_session_token_too():
    headers = sign(
        "GET",
        "/k",
        "h",
        {},
        EMPTY_SHA256,
        credentials=Credentials(ACCESS, SECRET, "session-token-value"),
        region="us-east-1",
    )
    assert headers["x-amz-security-token"] == "session-token-value"
    assert "x-amz-security-token" in headers["Authorization"]


def test_a_path_signs_the_same_however_its_spaces_were_encoded():
    """The server canonicalises what it receives, so this must match it."""

    def signature(target):
        return sign(
            "GET",
            target,
            "h",
            {},
            EMPTY_SHA256,
            credentials=CREDENTIALS,
            region="us-east-1",
            when=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )["Authorization"]

    assert signature("/a%20b") == signature("/a b")
    assert signature("/a%20b") != signature("/ab")


def test_a_body_that_is_still_being_written_signs_as_unsigned():
    assert hash_payload(None) == UNSIGNED_PAYLOAD
    assert hash_payload(b"") == EMPTY_SHA256
    assert hash_payload(b"abc").startswith("ba7816bf")


def test_credentials_never_print_the_secret_they_hold():
    plain = repr(Credentials("AKIA-VISIBLE", "shhh"))
    assert "shhh" not in plain
    assert "AKIA-VISIBLE" in plain and "<redacted>" in plain
    temporary = repr(Credentials("AKIA-VISIBLE", "shhh", "also-secret"))
    assert "also-secret" not in temporary and temporary.count("<redacted>") == 2


def test_credentials_come_from_the_environment_when_it_has_them(monkeypatch):
    assert Credentials.from_env() is None
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA-ENV")
    assert Credentials.from_env() is None  # half a credential is none of one
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "env-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "env-token")
    found = Credentials.from_env()
    assert found == Credentials("AKIA-ENV", "env-secret", "env-token")
    assert Credentials.discover() == found


def test_credentials_come_from_the_shared_file_when_the_environment_is_quiet(
    tmp_path, monkeypatch
):
    monkeypatch.undo()  # the autouse fixture stubs ``from_file`` out
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "credentials"
    path.write_text(
        "[default]\naws_access_key_id = AKIA-FILE\naws_secret_access_key = file-secret\n"
        "[other]\naws_access_key_id = AKIA-OTHER\naws_secret_access_key = other-secret\n"
        "aws_session_token = other-token\n[empty]\n"
    )
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(path))
    assert Credentials.from_file() == Credentials("AKIA-FILE", "file-secret")
    assert Credentials.discover() == Credentials("AKIA-FILE", "file-secret")
    monkeypatch.setenv("AWS_PROFILE", "other")
    assert Credentials.from_file() == Credentials("AKIA-OTHER", "other-secret", "other-token")
    assert Credentials.from_file(profile="empty") is None
    assert Credentials.from_file(profile="absent") is None


@pytest.mark.parametrize("content", ["", "this is not an ini file at all\n[[["])
def test_a_credentials_file_that_cannot_be_read_is_simply_no_credentials(
    tmp_path, monkeypatch, content
):
    monkeypatch.undo()
    path = tmp_path / "credentials"
    path.write_text(content)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(path))
    assert Credentials.from_file() is None
    assert Credentials.from_file(str(tmp_path / "nothing-here")) is None


# ---------------------------------------------------------------------------
# Addressing
# ---------------------------------------------------------------------------


def test_a_bucket_is_addressed_at_amazon_when_nothing_names_an_endpoint():
    with S3FileSystem("s3://my-bucket", credentials=CREDENTIALS, region="eu-west-2") as fs:
        assert fs.endpoint == "my-bucket.s3.eu-west-2.amazonaws.com:443"
        assert str(fs._url("/runs/a.root")) == (
            "https://my-bucket.s3.eu-west-2.amazonaws.com:443/runs/a.root"
        )
        assert repr(fs) == "S3FileSystem('s3://my-bucket/')"


def test_an_endpoint_switches_the_bucket_into_the_path(bucket):
    with S3FileSystem(bucket.url, credentials=CREDENTIALS, endpoint=bucket.endpoint) as fs:
        assert fs._url("/runs/a.root").path == f"/{bucket.bucket}/runs/a.root"


def test_the_region_and_the_endpoint_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    with S3FileSystem("s3://b", credentials=CREDENTIALS) as fs:
        assert fs.region == "ap-south-1"
    monkeypatch.setenv("AWS_REGION", "us-west-1")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://minio.example.org:9000")
    with S3FileSystem("s3://b", credentials=CREDENTIALS) as fs:
        assert fs.region == "us-west-1"
        assert fs.endpoint == "minio.example.org:9000"
        assert fs._url("k").path == "/b/k"


def test_a_url_with_no_bucket_in_it_is_not_an_s3_url():
    with pytest.raises(ValueError, match="names no bucket"):
        S3FileSystem("s3:///key")


def test_an_s3_url_carries_no_port_because_a_bucket_is_not_an_endpoint():
    url = xrdclient.parse("s3://bucket/runs/a.root")
    assert str(url) == "s3://bucket/runs/a.root"
    assert url.is_s3 and url.use_tls and url.port == 443


def test_the_filesystem_for_an_s3_url_is_the_s3_one(bucket, monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", bucket.endpoint)
    bucket.access_key = ""
    with xrdclient.FileSystem(bucket.url) as fs:
        assert isinstance(fs, S3FileSystem)
        assert fs.read_bytes("/top.txt") == b"top"


# ---------------------------------------------------------------------------
# Reading the namespace
# ---------------------------------------------------------------------------


def test_a_listing_reads_prefixes_as_directories_and_keys_as_files(fs):
    entries = {entry.name: entry for entry in fs.scandir("/runs/2024")}
    assert sorted(entries) == ["a.root", "b.root"]
    assert entries["a.root"].stat.st_size == 5
    assert entries["a.root"].stat.is_file()
    assert entries["a.root"].parent == "/runs/2024"
    assert entries["a.root"].stat.st_mtime == 1704164645  # what the listing said
    assert sorted(fs.listdir("/runs")) == ["2023", "2024"]
    assert fs.stat("/runs/2024").is_dir()


def test_a_listing_of_a_bucket_is_the_listing_of_its_root(fs):
    assert sorted(fs.listdir()) == ["runs", "top.txt"]
    assert sorted(entry.name for entry in fs.iterdir("/")) == ["runs", "top.txt"]


def test_a_listing_follows_every_continuation_token_it_is_given(bucket, fs):
    bucket.page_size = 1
    assert sorted(fs.listdir("/")) == ["runs", "top.txt"]
    assert sorted(fs.listdir("/runs/2024")) == ["a.root", "b.root"]
    assert sum(1 for method, _path, query in bucket.seen if "continuation" in query) >= 2


def test_a_listing_can_carry_the_digest_s3_already_has(fs):
    entries = {entry.name: entry.checksum for entry in fs.scandir("/runs/2024", algorithm="md5")}
    assert entries["a.root"].algorithm == "md5"
    assert entries["a.root"].value == "5d41402abc4b2a76b9719d911017c592"
    assert all(entry.checksum is None for entry in fs.scandir("/runs/2024"))


def test_the_friendly_listing_keywords_are_accepted_and_change_nothing(fs):
    """``ListObjectsV2`` volunteers the sizes and times, so there is nothing
    to turn off - but the call has to work here as it does anywhere."""
    entries = fs.scandir("/runs/2024", stat=False, online=True)
    assert sorted(entry.name for entry in entries) == ["a.root", "b.root"]
    assert entries[0].stat is not None


def test_no_other_digest_can_be_asked_for_per_entry(fs):
    with pytest.raises(UnsupportedError, match="sha256 checksum per listing entry"):
        fs.scandir("/runs", algorithm="sha256")


def test_a_stat_asks_the_object_first_and_the_prefix_only_if_it_has_to(bucket, fs):
    info = fs.stat("/runs/2024/a.root")
    assert info.st_size == 5 and info.is_file()
    assert info.id == "5d41402abc4b2a76b9719d911017c592"
    assert info.st_mtime == 1704164645  # the header, and the listing agrees
    assert [method for method, _p, _q in bucket.seen] == ["HEAD"]
    assert fs.stat("/runs").is_dir()
    assert fs.stat("/").is_dir()
    assert fs.exists("/runs/2024/a.root")


def test_a_key_that_is_neither_an_object_nor_a_prefix_is_not_there(fs):
    with pytest.raises(NotFoundError):
        fs.stat("/runs/2024/missing.root")
    assert not fs.exists("/nowhere")
    assert not fs.isdir("/nowhere") and not fs.isfile("/nowhere")


def test_nothing_in_a_bucket_is_a_symbolic_link(fs):
    with pytest.raises(UnsupportedError, match="has no S3 equivalent"):
        fs.stat("/top.txt", follow_symlinks=False)
    assert not fs.is_symlink("/top.txt")


def test_a_bucket_answers_whether_it_is_there(bucket, fs):
    fs.ping()
    bucket.forbidden = True
    with pytest.raises(ServerError):
        fs.ping()


def test_a_bucket_has_no_vendor_extensions_and_needs_no_round_trip_to_say_so(bucket, fs):
    assert fs.extensions() == frozenset()
    assert bucket.seen == []


@pytest.mark.parametrize(
    "ask",
    [
        lambda fs: fs.prepare(["/top.txt"]),
        lambda fs: fs.query_prepare("some-handle", ["/top.txt"]),
        lambda fs: fs.cancel_prepare("some-handle"),
        lambda fs: fs.archive_info(["/top.txt"]),
    ],
)
def test_the_tape_api_is_not_something_a_bucket_has(bucket, fs, ask):
    """Inherited from the WebDAV filesystem, where ``/api/v1`` means something;
    on a bucket that path is a key, so asking is refused rather than sent."""
    with pytest.raises(UnsupportedError, match="no S3 equivalent"):
        ask(fs)
    assert bucket.seen == []


# ---------------------------------------------------------------------------
# Reading and writing objects
# ---------------------------------------------------------------------------


def test_an_object_reads_like_a_file(fs):
    assert fs.read_bytes("/runs/2024/b.root") == b"world!"
    with fs.open("/runs/2024/b.root", "rb") as handle:
        handle.seek(2)
        assert handle.read() == b"rld!"
    with fs.open("/top.txt", "r", encoding="utf-8") as handle:
        assert handle.read() == "top"


def test_a_small_object_is_written_in_one_request(bucket, fs):
    fs.write_bytes("/runs/2024/new.root", b"fresh")
    assert bucket.contents("runs/2024/new.root") == b"fresh"
    assert [method for method, _p, _q in bucket.seen] == ["PUT"]


def test_an_object_can_be_created_only_if_it_is_not_there(fs):
    fs.touch("/made.txt")
    assert fs.read_bytes("/made.txt") == b""
    fs.touch("/made.txt")
    with pytest.raises(ExistsError):
        fs.touch("/made.txt", exist_ok=False)


def test_a_directory_is_a_prefix_so_making_one_is_nothing_at_all(bucket, fs):
    fs.mkdir("/whatever")
    fs.makedirs("/whatever/nested", exist_ok=True)
    assert bucket.seen == []
    assert not fs.exists("/whatever")


def test_a_prefix_can_be_removed_only_once_nothing_is_under_it(fs):
    with pytest.raises(BusyError, match="not empty"):
        fs.rmdir("/runs")
    fs.rmdir("/nothing-is-here")


# ---------------------------------------------------------------------------
# Folder markers: the opt-in that makes an empty directory a thing
# ---------------------------------------------------------------------------


@pytest.fixture
def marker_fs(bucket):
    """The same bucket, with folder markers opted in."""
    with S3FileSystem(
        bucket.url,
        Config(s3_folder_markers=True),
        credentials=CREDENTIALS,
        endpoint=bucket.endpoint,
    ) as filesystem:
        yield filesystem


def test_a_marker_makes_an_empty_directory_exist(bucket, marker_fs):
    marker_fs.mkdir("/incoming")
    assert bucket.contents("incoming/") == b""
    assert marker_fs.isdir("/incoming")
    assert marker_fs.stat("/incoming").is_dir()


def test_listings_hide_the_markers(marker_fs):
    marker_fs.mkdir("/incoming")
    assert marker_fs.listdir("/incoming") == []
    assert "incoming" in marker_fs.listdir("/")


def test_mkdir_refuses_a_name_an_object_holds(marker_fs):
    with pytest.raises(ExistsError, match="object"):
        marker_fs.mkdir("/top.txt")
    with pytest.raises(ExistsError, match="object"):
        marker_fs.makedirs("/top.txt", exist_ok=True)


def test_mkdir_without_exist_ok_refuses_a_directory_that_exists(marker_fs):
    marker_fs.mkdir("/incoming")
    with pytest.raises(ExistsError, match="exists"):
        marker_fs.mkdir("/incoming")
    marker_fs.mkdir("/incoming", exist_ok=True)


def test_makedirs_needs_only_the_leaf_marker(bucket, marker_fs):
    marker_fs.makedirs("/a/b/c")
    assert list(bucket.objects) == [*OBJECTS, "a/b/c/"]
    assert marker_fs.isdir("/a") and marker_fs.isdir("/a/b")


def test_the_bucket_root_needs_no_marker(bucket, marker_fs):
    marker_fs.mkdir("/")
    assert not any(m == "PUT" for m, _p, _q in bucket.seen)


def test_rmdir_takes_the_marker_with_it(bucket, marker_fs):
    marker_fs.mkdir("/incoming")
    marker_fs.rmdir("/incoming")
    assert "incoming/" not in bucket.objects
    assert not marker_fs.exists("/incoming")


def test_rmdir_still_refuses_a_prefix_with_keys_under_it(marker_fs):
    with pytest.raises(BusyError, match="not empty"):
        marker_fs.rmdir("/runs")


def test_rmdir_of_the_root_deletes_nothing(bucket, marker_fs):
    # An empty bucket's root: nothing listed, no marker to delete.
    for key in list(bucket.objects):
        del bucket.objects[key]
    marker_fs.rmdir("/")
    assert not any(m == "DELETE" for m, _p, _q in bucket.seen)


def test_removing_a_key_twice_is_not_an_error_because_s3_cannot_tell(bucket, fs):
    fs.remove("/top.txt")
    assert "top.txt" not in bucket.objects
    fs.remove("/top.txt")
    fs.unlink("/never-existed")


def test_a_rename_is_a_server_side_copy_and_then_a_delete(bucket, fs):
    fs.rename("/top.txt", "/moved.txt")
    assert bucket.contents("moved.txt") == b"top"
    assert "top.txt" not in bucket.objects
    assert [method for method, _p, _q in bucket.seen] == ["PUT", "DELETE"]
    fs.move("/moved.txt", "/moved-again.txt")
    assert bucket.contents("moved-again.txt") == b"top"


def test_renaming_something_that_is_not_there_says_so(fs):
    with pytest.raises(NotFoundError):
        fs.rename("/not-here.txt", "/anywhere.txt")


def test_the_etag_is_the_checksum_when_it_is_one(fs):
    digest = fs.checksum("/runs/2024/a.root")
    assert (digest.algorithm, digest.value) == ("md5", "5d41402abc4b2a76b9719d911017c592")
    assert fs.checksum("/runs/2024/a.root", "MD5") == digest
    with pytest.raises(UnsupportedError, match="adler32 checksum"):
        fs.checksum("/runs/2024/a.root", "adler32")


# ---------------------------------------------------------------------------
# Multipart uploads
# ---------------------------------------------------------------------------


@pytest.fixture
def small_parts(monkeypatch, fs):
    """Shrink the part size, so a test need not write five megabytes."""
    monkeypatch.setattr(s3fs, "MIN_PART_SIZE", 8)
    fs.config = Config(chunk_size=4)
    return 8


def test_an_object_too_big_to_hold_goes_up_in_parts(bucket, fs, small_parts):
    with fs.open("/big.bin", "wb", buffering=0) as handle:
        for step in range(6):
            assert handle.write(bytes([step]) * 5) == 5
    assert bucket.contents("big.bin") == b"".join(bytes([n]) * 5 for n in range(6))
    verbs = [(method, query.partition("&")[0]) for method, _p, query in bucket.seen]
    assert verbs[0] == ("POST", "uploads=")
    assert verbs.count(("PUT", "partNumber=1")) == 1
    assert verbs[-1] == ("POST", "uploadId=upload-0001")
    assert bucket.uploads == {} and bucket.aborted == []


def test_a_part_is_never_smaller_than_the_protocol_allows(bucket, fs, small_parts):
    with fs.open("/big.bin", "wb", buffering=0) as handle:
        for _ in range(20):
            handle.write(b"ab")
    sent = [len(part) for parts in bucket.uploads.values() for part in parts.values()]
    assert sent == []  # the upload completed, so nothing is left in flight
    assert len(bucket.contents("big.bin")) == 40


def test_the_checksum_of_a_multipart_object_is_not_a_checksum_of_anything(fs, small_parts):
    fs.write_bytes("/small.bin", b"tiny")
    assert fs.checksum("/small.bin").value == "d60cadf1a41c651e1f0ade50136bad43"
    with fs.open("/big.bin", "wb", buffering=0) as handle:
        handle.write(b"y" * 40)
    with pytest.raises(UnsupportedError, match="multipart upload"):
        fs.checksum("/big.bin")


def test_an_upload_that_cannot_be_completed_is_thrown_away(bucket, fs, small_parts):
    bucket.complete_fails = True
    with pytest.raises(ProtocolError, match="failed to complete"):
        with fs.open("/big.bin", "wb", buffering=0) as handle:
            handle.write(b"z" * 40)
    assert bucket.aborted == ["upload-0001"]
    assert "big.bin" not in bucket.objects


def test_an_upload_that_is_never_named_cannot_be_continued(bucket, fs, small_parts):
    bucket.nameless_uploads = True
    with pytest.raises(ProtocolError, match="without naming it"):
        with fs.open("/big.bin", "wb", buffering=0) as handle:
            handle.write(b"z" * 40)


def test_a_failure_while_abandoning_an_upload_does_not_mask_the_first_one(
    bucket, fs, small_parts, monkeypatch
):
    bucket.complete_fails = True
    monkeypatch.setattr(HTTPClient, "request", _refusing_after("DELETE", HTTPClient.request))
    with pytest.raises(ProtocolError, match="failed to complete"):
        with fs.open("/big.bin", "wb", buffering=0) as handle:
            handle.write(b"z" * 40)


def _refusing_after(verb, real):
    """``real``, but every ``verb`` raises - a server that stops answering."""

    def request(self, method, url, **kwargs):
        if method == verb:
            raise OSError("the endpoint has gone away")
        return real(self, method, url, **kwargs)

    return request


def test_the_raw_layer_says_what_it_is(fs):
    with fs.open("/top.txt", "rb", buffering=0) as handle:
        assert isinstance(handle, S3RawIO)
        assert repr(handle).startswith("S3RawIO('http")
        assert "mode='rb'" in repr(handle)


# ---------------------------------------------------------------------------
# The rest of the package
# ---------------------------------------------------------------------------


def test_an_object_opens_through_the_ordinary_front_door(bucket, monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", bucket.endpoint)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", ACCESS)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRET)
    with xrdclient.open(f"s3://{bucket.bucket}/runs/2024/a.root") as handle:
        assert handle.read() == b"hello"
    with open_s3(f"s3://{bucket.bucket}/written.txt", "wt", encoding="utf-8") as handle:
        handle.write("through the front door")
    assert bucket.contents("written.txt") == b"through the front door"


def test_the_filesystem_an_open_object_needed_is_closed_with_it(bucket, monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", bucket.endpoint)
    bucket.access_key = ""
    handle = open_s3(f"s3://{bucket.bucket}/top.txt", "rb", buffering=0)
    assert handle.read(1) == b"t"
    assert handle.client._pool
    handle.close()
    assert not handle.client._pool
    handle.close()  # idempotent, as every close is


def test_an_object_that_cannot_be_opened_takes_its_connection_with_it(bucket, monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", bucket.endpoint)
    bucket.access_key = ""
    with pytest.raises(ExistsError):
        open_s3(f"s3://{bucket.bucket}/top.txt", "xb")


def test_a_bucket_is_a_copy_source_and_a_copy_destination(bucket, tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", bucket.endpoint)
    bucket.access_key = ""
    local = tmp_path / "a.root"
    xrdclient.copy(f"s3://{bucket.bucket}/runs/2024/a.root", str(local))
    assert local.read_bytes() == b"hello"
    xrdclient.copy(str(local), f"s3://{bucket.bucket}/copied.root")
    assert bucket.contents("copied.root") == b"hello"


def test_an_unsigned_request_is_what_a_public_bucket_wants(bucket):
    bucket.access_key = ""
    with S3FileSystem(bucket.url, credentials=None, endpoint=bucket.endpoint) as fs:
        assert fs.credentials is None
        assert fs.read_bytes("/top.txt") == b"top"


def test_a_signature_the_endpoint_disagrees_with_is_refused(bucket):
    wrong = Credentials(ACCESS, "not-the-secret-it-knows")
    with S3FileSystem(bucket.url, credentials=wrong, endpoint=bucket.endpoint) as fs:
        with pytest.raises(ServerError, match="403"):
            fs.read_bytes("/top.txt")


def test_a_bucket_that_is_not_this_one_is_not_here(bucket):
    with S3FileSystem(
        "s3://some-other-bucket", credentials=CREDENTIALS, endpoint=bucket.endpoint
    ) as fs:
        with pytest.raises(NotFoundError):
            fs.read_bytes("/top.txt")


def test_the_fake_endpoint_describes_itself(bucket):
    assert repr(bucket).startswith("FakeS3Server(127.0.0.1:")
    assert "bucket='test-bucket'" in repr(bucket)
    assert repr(FakeS3Server()) == "FakeS3Server(unbound, bucket='test-bucket', objects=0)"


def test_the_fake_endpoint_can_be_stopped_and_started_as_often_as_a_test_likes():
    FakeS3Server().stop()  # never bound, so nothing to release

    server = FakeS3Server()
    address = server.address  # bound by the asking, but not yet serving
    server.stop()
    assert server.address == address  # remembered, so a URL outlives the port

    with server:
        server.start()  # already serving: the second call is not a second thread
        assert server.address != ()


# ---------------------------------------------------------------------------
# Requests no client of this library would send
# ---------------------------------------------------------------------------


def _raw(server, method, target, headers=None, body=b""):
    """Talk to the endpoint below the library, to reach what it never asks for."""
    import http.client

    connection = http.client.HTTPConnection(*server.address)
    try:
        connection.request(method, target, body=body, headers=headers or {})
        answer = connection.getresponse()
        return answer.status, answer.read().decode()
    finally:
        connection.close()


CREDENTIAL = f"{ACCESS}/20240102/us-east-1/s3/aws4_request"
SIGNED = f"AWS4-HMAC-SHA256 Credential={CREDENTIAL}, SignedHeaders=host, Signature=nope"


@pytest.mark.parametrize(
    ("method", "target"),
    [("PUT", "/test-bucket/k"), ("POST", "/test-bucket/k?uploads"), ("DELETE", "/test-bucket/k")],
)
def test_a_request_that_carries_no_signature_at_all_is_refused(bucket, method, target):
    status, body = _raw(bucket, method, target)
    assert status == 403
    assert "SignatureDoesNotMatch" in body
    assert bucket.objects.get("k") is None


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"Authorization": "Basic aGVsbG8="}, id="not-sigv4"),
        pytest.param(
            {"Authorization": SIGNED.replace(ACCESS, "AKIAWRONGACCOUNTKEY")}, id="wrong-account"
        ),
        pytest.param(
            {"Authorization": SIGNED, "x-amz-date": "20240103T030405Z"}, id="stale-scope"
        ),
        pytest.param(
            {
                "Authorization": SIGNED,
                "x-amz-date": "20240102T030405Z",
                "x-amz-content-sha256": "0" * 64,
            },
            id="wrong-payload-hash",
        ),
    ],
)
def test_a_signature_that_is_the_wrong_shape_is_refused_before_it_is_checked(bucket, headers):
    status, body = _raw(bucket, "HEAD", "/test-bucket/top.txt", headers=headers)
    assert status == 403
    assert body == ""  # a HEAD carries no explanation, only the refusal


@pytest.fixture
def public(bucket):
    """The same endpoint with the signature check turned off."""
    bucket.access_key = ""
    return bucket


def test_a_part_of_an_upload_nobody_began_has_nowhere_to_go(public):
    status, body = _raw(public, "PUT", "/test-bucket/k?uploadId=never&partNumber=1", body=b"x")
    assert (status, "NoSuchUpload" in body) == (404, True)


def test_a_post_that_asks_for_neither_a_new_upload_nor_an_old_one_is_malformed(public):
    status, body = _raw(public, "POST", "/test-bucket/k")
    assert (status, "MalformedPOSTRequest" in body) == (400, True)


def test_an_upload_nobody_began_cannot_be_completed(public):
    status, body = _raw(public, "POST", "/test-bucket/k?uploadId=never", body=b"<Part/>")
    assert (status, "NoSuchUpload" in body) == (404, True)


def test_a_manifest_that_is_not_xml_names_no_parts_at_all(public):
    status, body = _raw(public, "POST", "/test-bucket/k?uploads")
    assert status == 200
    upload = body.partition("<UploadId>")[2].partition("<")[0]
    status, _ = _raw(public, "POST", f"/test-bucket/k?uploadId={upload}", body=b"<truncated")
    assert (status, public.objects["k"]) == (200, b"")


# ---------------------------------------------------------------------------
# Answers that are not the ones expected
# ---------------------------------------------------------------------------


def test_an_answer_that_is_not_xml_is_a_protocol_error(bucket, fs):
    with pytest.raises(ProtocolError, match="unparsable"):
        s3fs._xml(_response(b"<not xml at all"), "the bucket")
    assert s3fs._text(s3fs._xml(_response(b"<a><b>x</b></a>"), "x"), "missing") == ""


def _response(body):
    import email.message

    from xrdclient.http.client import Response

    return Response(200, "OK", email.message.Message(), body)


@pytest.mark.parametrize(
    ("stamp", "seconds"),
    [("2024-01-02T03:04:05.000Z", 1704164645), ("last tuesday", 0), ("", 0)],
)
def test_a_timestamp_that_is_not_one_is_simply_unknown(stamp, seconds):
    assert s3fs._iso8601(stamp) == seconds


def test_a_key_with_a_space_in_it_survives_the_round_trip(bucket, fs):
    fs.write_bytes("/a directory/a file.root", b"spaced")
    assert bucket.contents("a directory/a file.root") == b"spaced"
    assert fs.read_bytes("/a directory/a file.root") == b"spaced"
    assert fs.listdir("/a directory") == ["a file.root"]


def test_an_object_whose_key_is_the_prefix_itself_is_not_a_member_of_it(bucket, fs):
    bucket.objects["runs/2024/"] = b""  # the empty marker some consoles write
    assert sorted(fs.listdir("/runs/2024")) == ["a.root", "b.root"]


def test_a_prefix_that_names_nothing_is_no_directory_at_all(bucket, fs):
    bucket.objects["runs/2024//deep.root"] = b"below a doubled separator"
    assert sorted(fs.listdir("/runs/2024")) == ["a.root", "b.root"]


def test_a_chunk_size_above_the_minimum_part_size_is_left_alone(bucket):
    with S3FileSystem(
        bucket.url,
        Config(chunk_size=1 << 24),
        credentials=CREDENTIALS,
        endpoint=bucket.endpoint,
    ) as fs:
        with fs.open("/top.txt", "rb", buffering=0) as handle:
            assert handle._stream_after() == 1 << 24  # already above the minimum


def test_the_package_exports_what_it_documents():
    import xrdclient.s3

    assert set(xrdclient.s3.__all__) <= set(dir(xrdclient.s3))
    assert os.path.basename(xrdclient.s3.fs.__file__) == "fs.py"
