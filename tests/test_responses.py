from __future__ import annotations

import struct

import pytest

from xrdclient.errors import ProtocolError
from xrdclient.flags import StatInfoFlags
from xrdclient.proto import constants as c
from xrdclient.proto import responses as rp
from xrdclient.types import ChecksumInfo


def test_parse_error():
    info = rp.parse_error(struct.pack(">i", 3011) + b"no such file\x00")
    assert (info.code, info.message) == (3011, "no such file")


def test_parse_redirect_splits_the_cgi_token():
    body = struct.pack(">i", 1095) + b"newhost.example.org?xrdclient.k=abc\x00"
    info = rp.parse_redirect(body)
    assert (info.host, info.port, info.token) == ("newhost.example.org", 1095, "xrdclient.k=abc")
    assert info.url == "root://newhost.example.org:1095/"


def test_parse_redirect_without_a_token():
    info = rp.parse_redirect(struct.pack(">i", 1094) + b"h\x00")
    assert info.token == ""


def test_parse_redirect_ends_the_host_at_a_line_ending():
    """A redirector that terminates the field with CRLF names a host."""
    for ending in (b"\r\n", b"\n", b"\r", b"\x00"):
        info = rp.parse_redirect(struct.pack(">i", 1094) + b"newhost" + ending + b"junk")
        assert info.host == "newhost"


def test_parse_redirect_drops_the_tokens_own_separator():
    """EOS hands its open capability over as ``?&cap.sym=...``."""
    body = struct.pack(">i", 1095) + b"eos.example.org?&cap.sym=abc&cap.msg=def\x00"
    info = rp.parse_redirect(body)
    assert info.token == "cap.sym=abc&cap.msg=def"


def test_parse_redirect_of_a_token_that_is_only_separators():
    info = rp.parse_redirect(struct.pack(">i", 1094) + b"h?&\x00")
    assert info.token == ""


def test_a_negative_redirect_port_means_tls():
    info = rp.parse_redirect(struct.pack(">i", -1094) + b"h\x00")
    assert info.url == "roots://h:1094/"


def test_parse_pgwrite_cse_lists_the_corrupt_pages():
    trailer = struct.pack(">IHH", 0, 4096, 4096) + struct.pack(">qq", 0, 8192)
    assert rp.parse_pgwrite_cse(trailer) == (0, 8192)


def test_a_header_only_cse_trailer_names_no_page():
    assert rp.parse_pgwrite_cse(struct.pack(">IHH", 0, 4096, 4096)) == ()


@pytest.mark.parametrize("size", [1, 7, 12, 20])
def test_a_ragged_cse_trailer_is_refused(size):
    with pytest.raises(ProtocolError, match="checksum-error trailer"):
        rp.parse_pgwrite_cse(b"x" * size)


def test_parse_wait_carries_the_delay_and_the_reason():
    info = rp.parse_wait(struct.pack(">i", 5) + b"server busy\x00")
    assert (info.seconds, info.message) == (5, "server busy")


def test_parse_waitresp_is_delay_only():
    assert rp.parse_waitresp(struct.pack(">i", 12)).seconds == 12


def test_parse_attn_keeps_the_raw_parameters():
    info = rp.parse_attn(struct.pack(">i", c.kXR_asynresp) + b"rest")
    assert info.action == c.kXR_asynresp
    assert info.params == b"rest"


def test_attn_message_stops_at_the_nul():
    assert rp.parse_attn(struct.pack(">i", 1) + b"bye\x00junk").message == "bye"


# --------------------------------------------------------------------------
# kXR_status
# --------------------------------------------------------------------------


def status_body(requestid: int, resptype: int, dlen: int, info: bytes = b"") -> bytes:
    return (
        struct.pack(">I", 0)
        + struct.pack(">H", 3)
        + bytes([requestid - c.kXR_1stRequest, resptype])
        + bytes(4)
        + struct.pack(">i", dlen)
        + info
    )


def test_parse_status_decodes_the_16_byte_common_header():
    info = rp.parse_status(status_body(c.kXR_pgread, c.kXR_FinalResult, 4096))
    assert info.streamid == 3
    assert info.requestid == c.kXR_pgread
    assert info.dlen == 4096
    assert info.is_final


def test_partial_results_are_not_final():
    assert not rp.parse_status(status_body(c.kXR_pgread, c.kXR_PartialResult, 0)).is_final


def test_pgread_status_carries_the_data_offset():
    info = rp.parse_status(status_body(c.kXR_pgread, 0, 8192, struct.pack(">q", 65536)))
    assert info.offset == 65536


def test_offset_is_an_error_when_the_server_sent_none():
    with pytest.raises(ProtocolError, match="no offset"):
        _ = rp.parse_status(status_body(c.kXR_pgread, 0, 0)).offset


# --------------------------------------------------------------------------
# Bring-up
# --------------------------------------------------------------------------


def test_parse_protocol_without_a_security_block():
    info = rp.parse_protocol(struct.pack(">iI", 0x05000000, c.kXR_haveTLS))
    assert info.version == 0x05000000
    assert info.has_tls
    assert info.security_level == c.kXR_secNone
    assert info.security_overrides == {}


def test_parse_protocol_with_a_security_block_and_overrides():
    body = struct.pack(">iI", 0x05000000, 0) + bytes(
        [ord("S"), 0, 2, 0, c.kXR_secCompatible, 1, c.kXR_write - c.kXR_1stRequest, 3]
    )
    info = rp.parse_protocol(body)
    assert info.security_level == c.kXR_secCompatible
    assert info.security_version == 2
    assert info.security_overrides == {c.kXR_write: 3}


def test_a_mislabelled_security_block_is_tolerated():
    # A trailer that is neither the spec 'S' record nor a vendor security-methods
    # advert (nginx-xrootd/brix) is ignored rather than failing the handshake -
    # this is what a real XrdCl client does, and it keeps bring-up working against
    # servers that append data we do not recognise.
    body = struct.pack(">iI", 1, 0) + bytes([ord("X"), 0, 0, 0, 0, 0])
    info = rp.parse_protocol(body)
    assert info.version == 1
    assert info.security_level == c.kXR_secNone
    assert info.security_overrides == {}


def test_a_vendor_prefixed_security_block_is_parsed():
    # nginx-xrootd/brix answers kXR_secreqs with a 4-byte security-methods header
    # [rsvd, required, method_count, rsvd] then method_count 8-byte entries, then
    # the ServerResponseReqs_Protocol 'S' record.  With no methods (auth none) the
    # 'S' record sits at trailer offset 4 and still carries the security level.
    trailer = bytes([0, 0, 0, 0]) + bytes([ord("S"), 0, 0, 0, c.kXR_secStandard, 0])
    body = struct.pack(">iI", 0x520, 1) + trailer
    info = rp.parse_protocol(body)
    assert info.version == 0x520
    assert info.security_level == c.kXR_secStandard


def test_parse_login_splits_the_session_id_from_the_security_trailer():
    info = rp.parse_login(b"\x11" * 16 + b"&P=gsi,v:10400&P=unix\x00")
    assert info.sessid == b"\x11" * 16
    assert info.mechanisms == ("gsi", "unix")


def test_parse_login_without_a_security_trailer():
    info = rp.parse_login(b"\x22" * 16)
    assert info.sec == ""
    assert info.mechanisms == ()


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------


def test_parse_stat():
    info = rp.parse_stat(b"id0 4096 19 1700000000\x00", "/a/b")
    assert info.st_size == 4096 == info.size
    assert info.flags == StatInfoFlags(19)
    assert info.st_mtime == 1700000000 == info.modtime
    assert info.path == "/a/b"


def test_parse_stat_rejects_a_short_line():
    with pytest.raises(ProtocolError, match="expected >= 4"):
        rp.parse_stat(b"id0 4096\x00")


def test_stat_flags_drive_the_predicates():
    d = rp.parse_stat(f"id 0 {int(StatInfoFlags.IS_DIR)} 0".encode())
    assert d.is_dir() and not d.is_file()
    f = rp.parse_stat(b"id 0 0 0")
    assert f.is_file() and not f.is_dir()


def test_st_mode_is_a_posix_mode():
    d = rp.parse_stat(f"id 0 {int(StatInfoFlags.IS_DIR | StatInfoFlags.IS_READABLE)} 0".encode())
    assert d.st_mode & 0o040000


def test_parse_statvfs():
    info = rp.parse_statvfs(b"2 1000 50 1 500 20\x00")
    assert (info.nodes_rw, info.free_rw, info.utilization_rw) == (2, 1000, 50)
    assert (info.nodes_staging, info.free_staging, info.utilization_staging) == (1, 500, 20)


def test_parse_statvfs_rejects_a_short_line():
    with pytest.raises(ProtocolError, match="expected 6"):
        rp.parse_statvfs(b"1 2 3\x00")


def test_parse_statx_is_one_byte_per_path():
    assert rp.parse_statx(bytes([0, 2, 4])) == (0, 2, 4)


def test_parse_statx_hands_back_flags_not_bare_integers():
    """The bytes are a bitmask, and callers ask them questions."""
    flags = rp.parse_statx(bytes([StatInfoFlags.IS_DIR, StatInfoFlags.IS_READABLE]))
    assert all(isinstance(f, StatInfoFlags) for f in flags)
    assert StatInfoFlags.IS_DIR in flags[0]
    assert StatInfoFlags.IS_DIR not in flags[1]


def test_parse_dirlist_with_stat_drops_the_dot_entry():
    body = b".\nid 0 19 0\nfile.root\nid 42 0 1700000000\nsub\nid 0 2 0\x00"
    entries = rp.parse_dirlist(body, "/d")
    assert [e.name for e in entries] == ["file.root", "sub"]
    assert entries[0].stat.st_size == 42
    assert entries[0].path == "/d/file.root"


def test_parse_dirlist_without_stat():
    entries = rp.parse_dirlist(b"a\nb\n\x00", "/d", with_stat=False)
    assert [e.path for e in entries] == ["/d/a", "/d/b"]
    assert entries[0].stat is None


def test_parse_dirlist_reads_the_digest_appended_to_each_stat_line():
    body = (
        b".\nid 0 19 0\n"
        b"file.root\nid 42 0 1700000000 0 0 33188 0 0 [ adler32:1a0b045d ]\n"
        b"sub\nid 0 2 0 0 0 16877 0 0 [ adler32:none ]\x00"
    )
    entries = rp.parse_dirlist(body, "/d")
    assert entries[0].checksum == ChecksumInfo("adler32", "1a0b045d")
    assert entries[0].stat.st_size == 42  # the token does not disturb the stat
    assert entries[1].checksum is None  # "none" is no digest, not a digest of none


@pytest.mark.parametrize(
    "line",
    [
        "id 42 0 1700000000",  # no token at all
        "id 42 0 1700000000 [ adler32 ]",  # a bracket, but nothing to split
        "id 42 0 1700000000 [ :1a0b045d ]",  # a value with no algorithm
        "id 42 0 1700000000 [ adler32:1a0b045d",  # never closed
    ],
)
def test_a_stat_line_without_a_usable_token_keeps_its_fields(line):
    entries = rp.parse_dirlist(f".\nid 0 19 0\nf\n{line}\n".encode(), "/d")
    assert entries[0].checksum is None
    assert entries[0].stat.st_size == 42


def test_a_server_that_ignored_the_stat_flag_still_lists():
    """No dot entry means plain names, whatever we asked for."""
    entries = rp.parse_dirlist(b"a.root\nsub\n\x00", "/d")
    assert [(e.name, e.stat) for e in entries] == [("a.root", None), ("sub", None)]


def test_an_empty_listing_is_empty_either_way():
    assert rp.parse_dirlist(b"\x00", "/d") == []


@pytest.mark.parametrize("name", ["..", "../evil", "a/b", "/etc/passwd"])
def test_a_listing_entry_that_is_not_a_name_is_refused(name):
    """A hostile server does not get to steer a recursive copy off the tree."""
    with pytest.raises(ProtocolError):
        rp.parse_dirlist(f"{name}\n".encode(), "/d", with_stat=False)
    with pytest.raises(ProtocolError):
        rp.parse_dirlist(f"{name}\nid 1 0 0\n".encode(), "/d")


def test_a_dotted_name_that_is_still_a_name_is_kept():
    entries = rp.parse_dirlist(b"..hidden\n...\n\x00", "/d", with_stat=False)
    assert [e.name for e in entries] == ["..hidden", "..."]


def test_dir_entries_are_os_pathlike():
    import os

    entry = rp.parse_dirlist(b"a\n", "/d", with_stat=False)[0]
    assert os.fspath(entry) == "/d/a"


def test_parse_locate():
    locs = rp.parse_locate(b"Sw192.168.1.1:1094 Mr[::1]:1095 sr10.0.0.1:1094\x00")
    assert [loc.address for loc in locs] == ["192.168.1.1:1094", "[::1]:1095", "10.0.0.1:1094"]
    assert locs[0].is_server and locs[0].is_writable and not locs[0].is_pending
    assert locs[1].is_manager and not locs[1].is_writable
    assert locs[2].is_pending


def test_location_splits_host_and_port_including_ipv6():
    loc = rp.parse_locate(b"Sr[2001:db8::1]:1095")[0]
    assert (loc.host, loc.port) == ("2001:db8::1", 1095)


def test_parse_open_handle_only():
    fhandle, stat, compression = rp.parse_open(b"HDL0")
    assert fhandle == b"HDL0"
    assert stat is None
    assert compression == (0, "")


def test_parse_open_with_retstat():
    body = b"HDL0" + bytes(8) + b"id0 99 0 1700000000\x00"
    fhandle, stat, compression = rp.parse_open(body, "/a")
    assert fhandle == b"HDL0"
    assert stat is not None and stat.st_size == 99 and stat.path == "/a"
    assert compression == (0, "")


def test_parse_open_reads_the_compression_fields():
    """A compressed file reports its page size and the algorithm that made it;
    the four bytes of the name are padded with NULs, not sized."""
    body = b"HDL0" + (8192).to_bytes(4, "big") + b"lzw\x00" + b"id0 99 0 1700000000\x00"
    _, stat, compression = rp.parse_open(body, "/a")
    assert compression == (8192, "lzw")
    assert stat is not None and stat.st_size == 99


def test_parse_checksum():
    info = rp.parse_checksum(b"adler32 0A1B2C3D\x00")
    assert (info.algorithm, info.value) == ("adler32", "0a1b2c3d")
    assert str(info) == "adler32:0a1b2c3d"


def test_parse_checksum_rejects_a_bare_value():
    with pytest.raises(ProtocolError, match="malformed checksum"):
        rp.parse_checksum(b"deadbeef\x00")


# --------------------------------------------------------------------------
# Vector I/O and xattrs
# --------------------------------------------------------------------------


def test_parse_readv_splits_the_segments():
    body = (
        struct.pack(">4siq", b"HDL0", 3, 100)
        + b"abc"
        + struct.pack(">4siq", b"HDL0", 2, 500)
        + b"de"
    )
    segments = rp.parse_readv(body)
    assert [(s.offset, s.data) for s in segments] == [(100, b"abc"), (500, b"de")]


def test_parse_readv_rejects_a_negative_length():
    with pytest.raises(ProtocolError, match="negative length"):
        rp.parse_readv(struct.pack(">4siq", b"HDL0", -1, 0))


def test_parse_fattr_get():
    body = bytes([0, 1]) + struct.pack(">H", 0) + b"user.x\x00" + struct.pack(">i", 3) + b"val"
    result = rp.parse_fattr(body)
    assert result.errors == 0
    assert result.as_dict() == {"user.x": b"val"}


def test_parse_fattr_list_has_no_values():
    body = bytes([0, 2]) + struct.pack(">H", 0) + b"a\x00" + struct.pack(">H", 0) + b"b\x00"
    result = rp.parse_fattr(body, values=False)
    assert [i.name for i in result.items] == ["a", "b"]
    assert result.as_dict() == {}


def test_parse_fattr_reports_errors():
    body = bytes([1, 1]) + struct.pack(">H", 2) + b"a\x00" + struct.pack(">i", 0)
    result = rp.parse_fattr(body)
    assert result.errors == 1
    assert result.items[0].code == 2


def test_parse_fattr_tolerates_an_empty_body():
    assert rp.parse_fattr(b"").items == []


def test_parse_fattr_stops_when_the_body_runs_out_before_the_count_does():
    """``nattr`` is the server's promise, not a guarantee: a truncated reply
    yields the entries that did arrive rather than raising over the rest."""
    body = bytes([0, 3]) + struct.pack(">H", 0) + b"a\x00" + struct.pack(">i", 2) + b"hi" + b"\x00"
    result = rp.parse_fattr(body)
    assert [i.name for i in result.items] == ["a"]
    assert result.as_dict() == {"a": b"hi"}


def test_parse_fattr_tree_groups_the_names_by_the_file_they_are_on():
    body = b"a.root:user.owner\x00sub/b.root:user.run\x00sub/b.root:user.owner\x00"
    assert rp.parse_fattr_tree(body) == {
        "a.root": ["user.owner"],
        "sub/b.root": ["user.run", "user.owner"],
    }


def test_parse_fattr_tree_splits_on_the_last_colon():
    """A colon is legal in a path and unheard of in an attribute name, so the
    last one is the separator - anything else would lose the file."""
    assert rp.parse_fattr_tree(b"odd:name.root:user.x\x00") == {"odd:name.root": ["user.x"]}


def test_parse_fattr_tree_ignores_an_entry_that_is_not_one():
    """A server that does not know the flag answers the ordinary list reply,
    whose bytes carry no colon: an empty tree beats a confident wrong one."""
    assert rp.parse_fattr_tree(b"") == {}
    assert rp.parse_fattr_tree(bytes([0, 1]) + struct.pack(">H", 0) + b"user.x\x00") == {}


# ---------------------------------------------------------------------------
# The value objects the parsers hand back
# ---------------------------------------------------------------------------


def test_stat_flags_are_readable_as_questions():
    from xrdclient.types import StatInfo

    info = StatInfo(
        flags=StatInfoFlags.IS_READABLE | StatInfoFlags.IS_WRITABLE | StatInfoFlags.OFFLINE
    )
    assert info.is_readable() and info.is_writable() and info.is_offline()
    assert info.is_file() and not info.is_dir()

    directory = StatInfo(flags=StatInfoFlags.IS_DIR | StatInfoFlags.IS_READABLE)
    assert directory.is_dir() and not directory.is_file()
    assert not directory.is_writable() and not directory.is_offline()


def test_a_location_says_whether_it_may_be_written_to():
    from xrdclient.types import LocationInfo

    assert LocationInfo(address="h:1094", type="S", access="w").is_writable
    assert not LocationInfo(address="h:1094", type="S", access="r").is_writable
    assert str(LocationInfo(address="h:1094")) == "h:1094"


def test_protocol_flags_are_readable_as_questions():
    from xrdclient.types import ProtocolInfo

    manager = ProtocolInfo(version=0x310, flags=c.kXR_isManager | c.kXR_haveTLS)
    assert manager.is_manager and manager.has_tls and not manager.is_server
    assert manager.version_str == "3.1.0"
    server = ProtocolInfo(flags=c.kXR_isServer)
    assert server.is_server and not server.is_manager and not server.has_tls


@pytest.mark.parametrize(
    "flag, question",
    [
        (c.kXR_attrMeta, "is_meta"),
        (c.kXR_attrProxy, "is_proxy"),
        (c.kXR_attrSuper, "is_supervisor"),
        (c.kXR_attrCache, "is_cache"),
        (c.kXR_supposc, "supports_posc"),
        (c.kXR_suppgrw, "supports_pgio"),
        (c.kXR_supgpf, "supports_gpfile"),
        (c.kXR_anongpf, "allows_anonymous_gpfile"),
        (c.kXR_tlsData, "requires_tls_for_data"),
    ],
)
def test_every_capability_the_server_announces_is_one_property(flag, question):
    from xrdclient.types import ProtocolInfo

    assert getattr(ProtocolInfo(flags=flag), question)
    assert not getattr(ProtocolInfo(flags=~flag & 0xFFFFFFFF), question)


def test_the_tls_qualifier_bits_are_the_ones_xprotocol_defines():
    """``kXR_tlsData`` and ``kXR_tlsGPF`` are adjacent and easy to transpose,
    and a client that swapped them would encrypt for the wrong reason."""
    assert c.kXR_tlsData == 0x01000000
    assert c.kXR_tlsGPF == 0x02000000
    for flag in (c.kXR_tlsData, c.kXR_tlsGPF, c.kXR_tlsLogin, c.kXR_tlsSess, c.kXR_tlsTPC):
        assert flag & c.kXR_tlsAny == flag


def test_a_page_result_measures_the_data_it_carries():
    from xrdclient.types import PageResult

    clean = PageResult(data=b"x" * 4096)
    assert len(clean) == 4096 and clean.ok
    assert not PageResult(data=b"", corrupt_pages=(0,)).ok


def test_a_truncated_security_block_stops_at_the_last_whole_override():
    """A short block costs the overrides it did not carry, not the reply."""
    body = struct.pack(">iI", 0x05000000, 0) + bytes([ord("S"), 0, 0, 0, c.kXR_secStandard, 2])
    body += bytes([c.kXR_write - c.kXR_1stRequest, c.kXR_secNone, 0])  # one whole, one truncated
    info = rp.parse_protocol(body)
    assert info.security_overrides == {c.kXR_write: c.kXR_secNone}
    assert info.security_level == c.kXR_secStandard


def test_a_location_token_too_short_to_hold_an_address_is_skipped():
    assert rp.parse_locate(b"Sw1.2.3.4:1094 Xy\x00")[0].address == "1.2.3.4:1094"
    assert len(rp.parse_locate(b"Sw1.2.3.4:1094 Xy\x00")) == 1


def test_a_file_nobody_may_read_has_no_read_bits():
    from xrdclient.types import StatInfo

    assert StatInfo(flags=StatInfoFlags.IS_WRITABLE).st_mode & 0o444 == 0
    assert StatInfo(flags=StatInfoFlags.IS_WRITABLE).st_mode & 0o222 == 0o222


def test_a_space_reply_is_read_key_by_key():
    from xrdclient.types import SpaceInfo

    assert rp.parse_space(b"\x00") == SpaceInfo()
    info = rp.parse_space(
        b"oss.cgroup=atlas&oss.space=2000\noss.free=1500&oss.maxf=1400"
        b"&oss.used=500&oss.quota=1800\x00"
    )
    assert (info.name, info.total, info.free) == ("atlas", 2000, 1500)
    assert (info.largest_free, info.used, info.quota) == (1400, 500, 1800)
    assert info.unlimited is False
    assert str(info) == "atlas: 1500 of 2000 bytes free"


def test_a_space_reply_ignores_what_is_not_a_pair():
    """Servers pad these with bare words; a token with no ``=`` is not a key.

    Skipping it rather than failing is the difference between reading a real
    reply and refusing one that is merely wordier than the specification.
    """
    from xrdclient.types import SpaceInfo

    info = rp.parse_space(b"oss.cgroup=public&statistics&oss.free=7&oss.quota=\x00")
    assert (info.name, info.free) == ("public", 7)
    assert (info.quota, info.unlimited) == (-1, True)
    assert str(SpaceInfo()) == "default: 0 of 0 bytes free"


def test_a_prepare_status_is_one_entry_per_file_asked_about():
    reply = (
        b'{"request_id":"prep-1","responses":['
        b'{"path":"/a.root","path_exists":true,"on_tape":true,"online":false,'
        b'"requested":true,"has_reqid":true,"req_time":"1700000000","error_text":""},'
        b'{"path":"/b.root","path_exists":false,"error_text":"no such file"}]}\x00'
    )
    first, second = rp.parse_prepare_status(reply)
    assert (first.path, first.exists, first.on_tape, first.online) == ("/a.root", True, True, False)
    assert (first.requested, first.has_request_id, first.requested_at) == (True, True, "1700000000")
    assert str(first) == "/a.root: on tape"
    assert (second.exists, second.error) == (False, "no such file")


@pytest.mark.parametrize("spelling", [b"true", b"1", b'"1"', b'"True"', b'"yes"'])
def test_a_staged_file_is_online_however_the_server_spelt_it(spelling):
    """``true``, ``1`` and ``"1"`` have all been written by some version of it."""
    reply = b'{"responses":[{"path":"/a","online":' + spelling + b"}]}\x00"
    assert rp.parse_prepare_status(reply)[0].online is True


def test_a_prepare_status_may_be_the_bare_list_and_may_be_empty():
    assert rp.parse_prepare_status(b'[{"path":"/a"}]\x00')[0].path == "/a"
    assert rp.parse_prepare_status(b"\x00") == []
    assert rp.parse_prepare_status(b'{"request_id":"prep-1","responses":null}\x00') == []


@pytest.mark.parametrize("body", [b"not json at all\x00", b'{"responses":[3]}\x00'])
def test_a_prepare_status_that_is_not_the_document_it_claims_is_refused(body):
    with pytest.raises(ProtocolError, match="not a JSON document"):
        rp.parse_prepare_status(body)


def test_a_checkpoint_query_is_capacity_then_use():
    info = rp.parse_checkpoint(struct.pack(">II", 1 << 20, 4096))
    assert (info.capacity, info.used, info.free) == (1 << 20, 4096, (1 << 20) - 4096)
    assert str(info) == "4096/1048576 bytes used"


def test_a_checkpoint_that_reports_more_used_than_it_has_has_no_room_left():
    """Arithmetic, not a negative number: a full checkpoint has zero free."""
    assert rp.parse_checkpoint(struct.pack(">II", 10, 99)).free == 0


def test_a_readlink_answer_is_the_target_up_to_the_first_nul():
    assert rp.parse_readlink(b"/store/real.root\x00\x00") == "/store/real.root"


def test_a_readlink_answer_naming_nothing_is_a_protocol_error():
    from xrdclient.errors import ProtocolError

    with pytest.raises(ProtocolError, match="named no target"):
        rp.parse_readlink(b"   \x00")
