"""S3: the bucket as a filesystem, and multipart uploads.

The signature itself (AWS's worked examples, botocore agreement, credential
discovery) and the decoding of S3's XML are xrdhttp's since 0.4.0 and are
tested there (``xrdhttp/tests/test_sigv4.py``, ``test_codec.py``); this file
tests the filesystem built on them against :class:`FakeS3Server`, which
checks every signature it is sent.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import pytest
from xrdhttp.connection import HTTPMessage

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
from xrdclient.http.client import Response
from xrdclient.s3 import Credentials, S3FileSystem, S3RawIO, open_s3
from xrdclient.testing import FakeS3Server
from xrdclient.url import XRootDURL

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
    with S3FileSystem(bucket.url, credentials=CREDENTIALS, endpoint=bucket.endpoint) as filesystem:
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


def test_an_upload_that_is_never_named_cannot_be_continued(bucket, fs, small_parts):
    bucket.nameless_uploads = True
    with (
        pytest.raises(ProtocolError, match="without naming it"),
        fs.open("/big.bin", "wb", buffering=0) as handle,
    ):
        handle.write(b"z" * 40)


def test_a_failure_while_abandoning_an_upload_does_not_mask_the_first_one(
    bucket, fs, small_parts, monkeypatch
):
    bucket.complete_fails = True
    monkeypatch.setattr(HTTPClient, "request", _refusing_after("DELETE", HTTPClient.request))
    with (
        pytest.raises(ProtocolError, match="failed to complete"),
        fs.open("/big.bin", "wb", buffering=0) as handle,
    ):
        handle.write(b"z" * 40)


def _refusing_after(verb: str, real: Callable[..., Response]) -> Callable[..., Response]:
    """``real``, but every ``verb`` raises - a server that stops answering."""

    def request(self: HTTPClient, method: str, url: XRootDURL, **kwargs: Any) -> Response:
        if method == verb:
            raise OSError("the endpoint has gone away")
        return real(self, method, url, **kwargs)

    return request


def test_a_part_that_fails_abandons_the_upload_rather_than_leave_a_hole(bucket, fs, small_parts):
    bucket.failing_parts = {2}
    handle = fs.open("/big.bin", "wb", buffering=0)
    handle.write(b"a" * 8)
    handle.write(b"b" * 8)  # the first part: sixteen bytes
    with pytest.raises(ServerError):
        handle.write(b"c" * 8)  # the second, which the endpoint loses
    assert bucket.aborted == ["upload-0001"] and bucket.uploads == {}
    # Neither more data nor a close may complete what is now missing a part.
    with pytest.raises(ServerError):
        handle.write(b"d" * 8)
    with pytest.raises(ServerError):
        handle.close()
    assert handle.closed
    assert "big.bin" not in bucket.objects
    assert [m for m, _p, q in bucket.seen if q.startswith("uploadId=")] == ["DELETE"]


def test_a_part_that_fails_inside_a_with_block_stores_nothing(bucket, fs, small_parts):
    bucket.failing_parts = {2}
    with pytest.raises(ServerError), fs.open("/big.bin", "wb", buffering=0) as handle:
        for letter in b"abcdef":
            handle.write(bytes([letter]) * 8)
    assert "big.bin" not in bucket.objects
    assert bucket.aborted == ["upload-0001"]


def test_the_last_part_failing_abandons_the_upload_too(bucket, fs, small_parts):
    bucket.failing_parts = {2}
    handle = fs.open("/big.bin", "wb", buffering=0)
    handle.write(b"a" * 20)
    handle.write(b"b" * 4)  # held for the tail, which close sends
    with pytest.raises(ServerError):
        handle.close()
    assert "big.bin" not in bucket.objects
    assert bucket.aborted == ["upload-0001"]


def test_parts_grow_so_the_part_count_limit_is_never_reached():
    assert s3fs._part_size(0) == s3fs.MIN_PART_SIZE
    assert s3fs._part_size(899) == s3fs.MIN_PART_SIZE
    assert s3fs._part_size(900) == 2 * s3fs.MIN_PART_SIZE
    # Ten thousand parts - all S3 allows - hold more than the five
    # terabytes S3 allows an object to be, and no part is over five gigabytes.
    total = sum(s3fs._part_size(n) for n in range(s3fs.MAX_PARTS))
    assert total >= 5 << 40
    assert s3fs._part_size(s3fs.MAX_PARTS - 1) == s3fs.MAX_PART_SIZE == 5 << 30


def test_a_long_upload_sends_larger_parts_as_it_goes(bucket, fs, small_parts, monkeypatch):
    monkeypatch.setattr(s3fs, "_PARTS_PER_DOUBLING", 1)
    payload = bytes(range(120))
    with fs.open("/big.bin", "wb", buffering=0) as handle:
        for start in range(0, len(payload), 2):
            handle.write(payload[start : start + 2])
    assert bucket.contents("big.bin") == payload
    parts = [q for m, _p, q in bucket.seen if m == "PUT" and q.startswith("partNumber=")]
    assert len(parts) == 4  # 10, 16, 32 and the 62-byte tail


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
    assert handle.client.idle_count() == 0  # the GET is still being read
    handle.close()
    assert handle.client.idle_count() == 0
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


def test_a_bucket_that_is_not_this_one_is_not_here(bucket):
    with (
        S3FileSystem(
            "s3://some-other-bucket", credentials=CREDENTIALS, endpoint=bucket.endpoint
        ) as fs,
        pytest.raises(NotFoundError),
    ):
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
        assert server.address[1] > 0


# ---------------------------------------------------------------------------
# Answers that are not the ones expected
# ---------------------------------------------------------------------------


def test_an_answer_that_is_not_xml_is_a_protocol_error(bucket, fs):
    with pytest.raises(ProtocolError, match="unparsable"):
        s3fs._answer(_response(b"<not xml at all"), "the bucket", "ListObjectsV2")
    assert "Contents" not in s3fs._answer(_response(b"<a><b>x</b></a>"), "x", "ListObjectsV2")


def _response(body: bytes) -> Response:
    return Response(200, "OK", HTTPMessage(), body)


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
    with (
        S3FileSystem(
            bucket.url,
            Config(chunk_size=1 << 24),
            credentials=CREDENTIALS,
            endpoint=bucket.endpoint,
        ) as fs,
        fs.open("/top.txt", "rb", buffering=0) as handle,
    ):
        assert isinstance(handle, S3RawIO)
        assert handle._stream_after() == 1 << 24  # already above the minimum


def test_the_package_exports_what_it_documents():
    import xrdclient.s3

    assert set(xrdclient.s3.__all__) <= set(dir(xrdclient.s3))
    assert os.path.basename(xrdclient.s3.fs.__file__) == "fs.py"
