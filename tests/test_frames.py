from __future__ import annotations

import struct

import pytest

from xrdclient.errors import ProtocolError
from xrdclient.proto import constants as c
from xrdclient.proto import requests as r
from xrdclient.proto.buffer import Writer
from xrdclient.proto.frames import HANDSHAKE, Request, decode_header, encode


def test_handshake_is_the_documented_20_bytes():
    assert HANDSHAKE == bytes(12) + struct.pack(">II", 4, 2012)
    assert len(HANDSHAKE) == 20


def test_encode_lays_out_the_24_byte_header():
    frame = encode(r.Stat("/tmp/x"), 7)
    streamid, opcode, params, dlen = struct.unpack(">HH16sI", frame[:24])
    assert streamid == 7
    assert opcode == c.kXR_stat == 3017
    assert len(params) == 16
    assert dlen == len(b"/tmp/x")
    assert frame[24:] == b"/tmp/x"


def test_encode_masks_the_streamid_to_16_bits():
    assert struct.unpack(">H", encode(r.Ping(), 0x1_0005)[:2])[0] == 5


def test_params_must_be_exactly_16_bytes():
    class Broken(Request):
        __slots__ = ()
        opcode = 3001

        def params(self, w: Writer) -> None:
            w.zeros(15)

    with pytest.raises(ProtocolError, match="wrote 15 bytes"):
        encode(Broken(), 1)


def test_oversized_payloads_are_refused():
    class Huge(Request):
        __slots__ = ()
        opcode = 3001

        def payload(self) -> bytes:
            return b"\x00" * (c.MAX_FRAME_PAYLOAD + 1)

    with pytest.raises(ProtocolError, match="exceeds the protocol maximum"):
        encode(Huge(), 1)


def test_default_request_is_all_zero_params_and_no_body():
    assert encode(r.Ping(), 1) == struct.pack(">HH", 1, c.kXR_ping) + bytes(16) + bytes(4)


def test_decode_header_round_trips():
    header = decode_header(struct.pack(">HHI", 9, c.kXR_oksofar, 42))
    assert (header.streamid, header.status, header.dlen) == (9, c.kXR_oksofar, 42)


def test_decode_header_needs_eight_bytes():
    with pytest.raises(ProtocolError, match="needs 8 bytes"):
        decode_header(b"abc")


def test_response_header_repr_names_the_status():
    assert "kXR_error" in repr(decode_header(struct.pack(">HHI", 1, c.kXR_error, 0)))


# --------------------------------------------------------------------------
# Per-request encodings
# --------------------------------------------------------------------------


def params_of(request: Request) -> bytes:
    return encode(request, 1)[4:20]


def body_of(request: Request) -> bytes:
    return encode(request, 1)[24:]


def test_login_params_carry_pid_and_a_padded_username():
    p = params_of(r.Login("verylongusername", pid=4321))
    assert struct.unpack(">i", p[:4])[0] == 4321
    assert p[4:12] == b"verylon\x00"[:8] or p[4:12] == b"verylong"


def test_login_username_is_nul_padded_to_eight():
    assert params_of(r.Login("bob", pid=1))[4:12] == b"bob\x00\x00\x00\x00\x00"


def test_auth_rejects_a_long_credtype():
    with pytest.raises(ValueError, match="<= 4 bytes"):
        r.Auth("toolong", b"")


def test_auth_puts_the_type_in_the_last_four_param_bytes():
    req = r.Auth("unix", b"unix\x00bob")
    assert params_of(req)[12:16] == b"unix"
    assert body_of(req) == b"unix\x00bob"


def test_stat_carries_the_path_as_the_body():
    assert body_of(r.Stat("/a/b")) == b"/a/b"


def test_statvfs_sets_the_vfs_option():
    assert params_of(r.StatVFS("/"))[0] == c.kXR_vfs


def test_statx_joins_paths_with_newlines():
    assert body_of(r.Statx(["/a", "/b"])) == b"/a\n/b"


def test_dirlist_asks_for_stat_by_default():
    assert params_of(r.Dirlist("/d"))[15] == c.kXR_dstat
    assert params_of(r.Dirlist("/d", options=0))[15] == 0


def test_mv_records_the_source_length_so_the_server_can_split():
    req = r.Mv("/a b", "/c")
    assert struct.unpack(">H", params_of(req)[14:16])[0] == 4
    assert body_of(req) == b"/a b /c"


def test_mkdir_encodes_mode_and_the_mkpath_option():
    p = params_of(r.Mkdir("/a", 0o750, mkpath=True))
    assert p[0] == c.kXR_mkdirpath
    assert struct.unpack(">H", p[14:16])[0] == 0o750


def test_open_encodes_mode_then_options():
    p = params_of(r.Open("/a", options=0x1234, mode=0o640))
    assert struct.unpack(">HH", p[:4]) == (0o640, 0x1234)


def test_read_encodes_handle_offset_length():
    p = params_of(r.Read(b"HDL0", 1 << 40, 8192))
    assert p[:4] == b"HDL0"
    assert struct.unpack(">qi", p[4:16]) == (1 << 40, 8192)


def test_write_puts_the_data_in_the_body():
    req = r.Write(b"HDL0", 16, b"payload")
    assert struct.unpack(">q", params_of(req)[4:12])[0] == 16
    assert body_of(req) == b"payload"


def test_truncate_by_handle_and_by_path():
    assert params_of(r.Truncate(size=99))[4:12] == struct.pack(">q", 99)
    assert body_of(r.Truncate("/a", 0)) == b"/a"


def test_readv_encodes_one_readahead_list_entry_per_chunk():
    body = body_of(r.ReadV([(b"H1\x00\x00", 100, 10), (b"H1\x00\x00", 200, 20)]))
    assert len(body) == 32
    assert struct.unpack(">4siq", body[:16]) == (b"H1\x00\x00", 10, 100)
    assert struct.unpack(">4siq", body[16:]) == (b"H1\x00\x00", 20, 200)


def test_writev_counts_only_its_descriptors_and_trails_the_data():
    """``dlen`` sizes the ``write_list``; the data follows the frame uncounted.

    Counting the data instead leaves the server with a ``dlen`` that does not
    divide by 16, and it answers ``kXR_ArgInvalid: Write vector is invalid``.
    """
    request = r.WriteV([(b"H1\x00\x00", 0, b"aa"), (b"H1\x00\x00", 8, b"bbb")])
    frame = encode(request, 1)
    dlen = struct.unpack(">i", frame[20:24])[0]
    assert dlen == 32 and dlen % 16 == 0
    assert frame[24 : 24 + dlen] == request.payload()
    assert struct.unpack(">4siq", frame[24:40]) == (b"H1\x00\x00", 2, 0)
    assert struct.unpack(">4siq", frame[40:56]) == (b"H1\x00\x00", 3, 8)
    assert frame[24 + dlen :] == b"aabbb" == request.trailer()


def test_clone_names_the_destination_in_the_header_and_the_sources_in_the_body():
    """The destination is one handle for the whole request; each item carries
    the handle it reads from, so ranges of several files can land in one."""
    request = r.Clone(b"DST0", [(b"SRC0", 100, 10, 0), (b"SRC1", 0, 4, 10)])
    frame = encode(request, 1)
    assert frame[4:20] == b"DST0" + bytes(12)
    assert struct.unpack(">i", frame[20:24])[0] == 64 == 2 * c.CLONE_ITEM_LEN
    assert struct.unpack(">4s4sqqq", frame[24:56]) == (b"SRC0", bytes(4), 100, 10, 0)
    assert struct.unpack(">4s4sqqq", frame[56:88]) == (b"SRC1", bytes(4), 0, 4, 10)
    assert repr(request) == "Clone(ranges=2)"


def test_every_other_request_has_no_trailer():
    assert r.Read(b"H1\x00\x00", 0, 10).trailer() == b""
    assert r.Write(b"H1\x00\x00", 0, b"data").trailer() == b""


def test_writev_can_ask_for_a_sync():
    assert params_of(r.WriteV([], sync=True))[0] == c.kXR_wv_doSync


def test_pgread_omits_the_optional_body_when_it_carries_no_flags():
    assert body_of(r.PgRead(b"HDL0", 0, 4096)) == b""
    assert body_of(r.PgRead(b"HDL0", 0, 4096, retry=True)) == bytes([0, c.kXR_pgRetry, 0, 0])


def test_pgwrite_keeps_the_interleaved_payload_intact():
    assert body_of(r.PgWrite(b"HDL0", 0, b"\x01\x02\x03\x04data")) == b"\x01\x02\x03\x04data"


def test_endsess_pads_the_session_id_to_sixteen():
    assert params_of(r.EndSession(b"\xaa" * 4)) == b"\xaa" * 4 + bytes(12)


def test_query_encodes_the_infotype_and_the_handle():
    p = params_of(r.Query(c.kXR_Qcksum, "/a/b"))
    assert struct.unpack(">H", p[:2])[0] == c.kXR_Qcksum
    assert body_of(r.Query(c.kXR_Qcksum, "/a/b")) == b"/a/b"


def test_every_query_code_the_protocol_defines_has_a_name():
    """``fs.query`` takes the enum, so a missing member is a subrequest no
    caller can ask for without reaching into ``xrdclient.proto``."""
    from xrdclient.flags import QueryCode

    defined = {value for name, value in vars(c).items() if name.startswith("kXR_Q")}
    assert {int(code) for code in QueryCode} == defined


def test_setattr_puts_a_44_byte_attribute_block_before_the_path():
    """The offsets are a contract with the two implementations that speak it,
    XRootD.jl's encoder and nginx-xrootd's decoder: flags, atime and mtime as
    second/nanosecond pairs, then uid and gid, then the path."""
    body = body_of(r.Setattr("/a", c.kXR_sa_times | c.kXR_sa_owner, (1, 2), (3, 4), 500, 501))
    assert struct.unpack(">iqqqqii", body[:44]) == (3, 1, 2, 3, 4, 500, 501)
    assert body[44:] == b"/a\x00"


def test_setattr_leaves_an_unnamed_id_at_minus_one():
    """``chown(2)``'s rule, and the one the server applies: -1 means "not this
    one" - a zero there would hand the file to root."""
    body = body_of(r.Setattr("/a", c.kXR_sa_owner, gid=42))
    assert struct.unpack(">ii", body[36:44]) == (-1, 42)


def test_prepare_joins_paths_with_newlines():
    assert body_of(r.Prepare(["/a", "/b"], options=c.kXR_stage)) == b"/a\n/b"


def test_prepare_puts_the_later_options_in_the_extended_half_word():
    """``kXR_evict`` is 0x0001 of ``optionX``, four bytes into the parameter
    area; 128 of the options byte is ``kXR_usetcp``, which means the opposite
    of nothing at all."""
    p = params_of(r.Prepare(["/a"], options=c.kXR_stage, priority=3, extended=c.kXR_evict))
    assert struct.unpack(">BBHH", p[:6]) == (c.kXR_stage, 3, 0, c.kXR_evict)
    assert p[6:] == bytes(10)


def test_fattr_builders_produce_matching_subcodes():
    assert r.Fattr.get("/p", "a").subcode == c.kXR_fattrGet
    assert r.Fattr.set("/p", "a", b"1").subcode == c.kXR_fattrSet
    assert r.Fattr.delete("/p", "a").subcode == c.kXR_fattrDel
    assert r.Fattr.list("/p").subcode == c.kXR_fattrList


def test_fattr_set_encodes_name_then_value():
    body = body_of(r.Fattr.set("/p", "user.x", b"val"))
    assert body.startswith(b"/p\x00")
    assert b"user.x\x00" in body
    assert body.endswith(b"val")


def test_every_locate_option_the_protocol_defines_has_a_name():
    """``fs.locate`` takes the enum and nothing else, so a bit missing from it
    is a question no caller can ask. ``kXR_4dirlist`` was one: XProtocol packs
    the locate options into the open-option numbers, and it is the bit that
    tells a redirector the locate is the prelude to a listing."""
    from xrdclient.flags import LocateFlags

    assert {int(flag) for flag in LocateFlags if flag} == {
        c.kXR_addPeers,
        c.kXR_refreshLoc,
        c.kXR_prefname,
        c.kXR_4dirlist,
        c.kXR_nowaitLoc,
    }
    assert params_of(r.Locate("/a", c.kXR_4dirlist))[:2] == struct.pack(">H", c.kXR_4dirlist)


def test_fattr_list_takes_no_names():
    assert body_of(r.Fattr.list("/p")) == b"/p\x00"


def test_fattr_list_asks_for_values_and_a_subtree_in_the_options_byte():
    """The two flags are independent bits of the same byte, and a subtree
    listing asks for names only - the reply has nowhere to put a value."""
    assert r.Fattr.list("/p").options == 0
    assert r.Fattr.list("/p", values=True).options == c.kXR_fattrAData
    assert r.Fattr.list("/p", recurse=True).options == c.kXR_fattrRecurse
    assert body_of(r.Fattr.list("/p", recurse=True)) == b"/p\x00"


def test_only_reading_fattr_subcodes_are_replayable():
    assert r.Fattr.get("/p", "a").idempotent is True
    assert r.Fattr.list("/p").idempotent is True
    assert r.Fattr.set("/p", "a", b"1").idempotent is False
    assert r.Fattr.delete("/p", "a").idempotent is False


@pytest.mark.parametrize(
    "request_",
    [
        r.Open("/a", 0),
        r.Write(b"H", 0, b"x"),
        r.Mkdir("/a"),
        r.Rm("/a"),
        r.Rmdir("/a"),
        r.Mv("/a", "/b"),
        r.Symlink("/a", "/b"),
        r.Link("/a", "/b"),
        r.Chmod("/a", 0o644),
        r.Truncate("/a", 0),
        r.Prepare(["/a"]),
        r.WriteV([]),
        r.Clone(b"H", []),
        r.PgWrite(b"H", 0, b""),
    ],
)
def test_mutating_requests_are_signed_and_not_replayable(request_):
    assert request_.signed is True
    assert request_.idempotent is False


@pytest.mark.parametrize(
    "request_", [r.Stat("/a"), r.Read(b"H", 0, 1), r.Dirlist("/a"), r.Locate("/a"), r.Ping()]
)
def test_read_only_requests_are_replayable(request_):
    assert request_.idempotent is True
    assert request_.signed is False


#: One instance of every request the protocol module exports. The gate below
#: keeps it complete, so a new request type cannot be added without deciding
#: - here, in public - whether it may be replayed and whether it is signed.
SAMPLES = [
    r.Protocol(),
    r.Login("u"),
    r.Auth("unix", b""),
    r.Ping(),
    r.EndSession(b""),
    r.Bind(b"\x00" * 16),
    r.Stat("/a"),
    r.StatVFS("/"),
    r.Statx(["/a"]),
    r.Dirlist("/a"),
    r.Locate("/a"),
    r.Query(1, "x"),
    r.Prepare(["/a"]),
    r.Mkdir("/a"),
    r.Rm("/a"),
    r.Rmdir("/a"),
    r.Mv("/a", "/b"),
    r.Symlink("/a", "/b"),
    r.Link("/a", "/b"),
    r.Readlink("/a"),
    r.Setattr("/a", c.kXR_sa_times),
    r.Chmod("/a", 0o644),
    r.Truncate("/a", 1),
    r.Set("x=1"),
    r.Open("/a", 0),
    r.Close(b"H"),
    r.Read(b"H", 0, 1),
    r.Write(b"H", 0, b"x"),
    r.Sync(b"H"),
    r.ReadV([]),
    r.WriteV([]),
    r.Clone(b"H", []),
    r.PgRead(b"H", 0, 1),
    r.PgWrite(b"H", 0, b""),
    r.ChkPoint(b"H", c.kXR_ckpBegin),
    r.Fattr.list("/a"),
    r.Sigver(c.kXR_write, 1, b"\x00" * 32),
]

#: The requests that change something on the server, and so must never be
#: sent a second time after a lost reply. :mod:`test_conformance_semantics`
#: tests that the client honours it; this is where it is declared.
UNREPEATABLE = {
    "Auth",
    "Bind",
    "ChkPoint",
    "Clone",
    "Chmod",
    "Close",
    "EndSession",
    "Mkdir",
    "Mv",
    "Symlink",
    "Link",
    "Open",
    "PgWrite",
    "Prepare",
    "Rm",
    "Rmdir",
    "Set",
    "Setattr",
    "Sync",
    "Truncate",
    "Write",
    "WriteV",
}

IDS = [type(sample).__name__ for sample in SAMPLES]


def test_every_request_class_encodes_without_error():
    """A params() that miscounts is the classic wire bug; catch it wholesale."""
    assert {type(s).__name__ for s in SAMPLES} >= set(r.__all__) - {"Request"}
    for sample in SAMPLES:
        frame = encode(sample, 1)
        assert len(frame) == 24 + len(sample.payload())


@pytest.mark.parametrize("request_", SAMPLES, ids=IDS)
def test_every_request_says_what_it_is_when_printed(request_):
    """These end up in log lines and tracebacks; ``<object at 0x...>`` there
    is the difference between a diagnosable failure and a shrug."""
    shown = repr(request_)
    assert shown.startswith(f"{type(request_).__name__}(")
    assert " object at " not in shown


@pytest.mark.parametrize("request_", SAMPLES, ids=IDS)
def test_every_request_declares_whether_it_may_be_replayed(request_):
    assert request_.idempotent is (type(request_).__name__ not in UNREPEATABLE)


def test_the_unrepeatable_list_names_only_real_requests():
    """A typo in it would silently let a mutation be replayed."""
    assert UNREPEATABLE <= set(IDS)


def test_repr_never_shows_credential_bytes():
    assert "cred=<0 bytes>" in repr(r.Auth("ztn", b""))
    assert "eyJ" not in repr(r.Auth("ztn", b"ztn\x00eyJhbGciOiJIUzI1NiJ9"))
