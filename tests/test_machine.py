"""The sans-io state machine, driven entirely by hand-built frames.

Every test here is one connection's worth of protocol with no socket in
sight - which is the whole point of the sans-io split.
"""

from __future__ import annotations

import struct

import pytest

from conftest import error, frame, handshake_reply, login_body, ok, protocol_body
from xrd.auth.base import Credential
from xrd.auth.simple import HostCredential
from xrd.config import Config
from xrd.errors import ConnectionError as XrdConnectionError
from xrd.errors import NoMechanismError, ProtocolError, ServerError
from xrd.proto import constants as c
from xrd.proto import machine as m
from xrd.proto import requests as r

SID = 4  # the first streamid the machine hands out


def drain(machine: m.SessionMachine) -> list[m.Event]:
    return list(machine.events())


def kinds(events: list[m.Event]) -> list[str]:
    return [type(e).__name__ for e in events]


def only(machine: m.SessionMachine, kind: type) -> m.Event:
    events = drain(machine)
    matching = [e for e in events if isinstance(e, kind)]
    assert matching, f"no {kind.__name__} in {kinds(events)}"
    return matching[0]


def new(config: Config | None = None, **kwargs) -> m.SessionMachine:
    return m.SessionMachine(
        host="srv.example.org",
        config=config or Config(username="tester", auth_order=("host",)),
        **kwargs,
    )


def bring_up(machine: m.SessionMachine, *, sec: str = "", flags: int = 0) -> None:
    """Walk a fresh machine all the way to READY."""
    machine.start()
    machine.data_to_send()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body(flags=flags)))
    machine.receive_data(ok(2, login_body(sec=sec)))
    if sec:
        machine.receive_data(ok(3))
    machine.data_to_send()


def ready(config: Config | None = None, **kwargs) -> m.SessionMachine:
    machine = new(config, **kwargs)
    bring_up(machine)
    drain(machine)
    return machine


# --------------------------------------------------------------------------
# Bring-up
# --------------------------------------------------------------------------


def test_start_pipelines_the_handshake_with_kxr_protocol():
    machine = new()
    machine.start()
    out = machine.data_to_send()
    assert out[:20] == bytes(12) + struct.pack(">II", 4, 2012)
    streamid, opcode = struct.unpack(">HH", out[20:24])
    assert (streamid, opcode) == (1, c.kXR_protocol)
    assert machine.state is m.State.HANDSHAKE


def test_the_protocol_request_advertises_tls_and_asks_for_the_security_block():
    machine = new()
    machine.start()
    flags = machine.data_to_send()[28]  # handshake[20] + streamid/opcode[4] + version[4]
    assert flags & c.kXR_secreqs and flags & c.kXR_ableTLS
    assert not flags & c.kXR_wantTLS


def test_wanting_tls_sets_the_request_flag():
    machine = new(want_tls=True)
    machine.start()
    assert machine.data_to_send()[28] & c.kXR_wantTLS


def test_start_is_not_repeatable():
    machine = new()
    machine.start()
    with pytest.raises(ProtocolError, match="start\\(\\) called in state"):
        machine.start()


def test_data_to_send_drains():
    machine = new()
    machine.start()
    assert machine.has_data_to_send
    machine.data_to_send()
    assert machine.data_to_send() == b""


def test_a_plain_bring_up_reaches_ready():
    machine = new()
    bring_up(machine)
    events = drain(machine)
    assert kinds(events) == ["Negotiated", "Ready"]
    assert machine.state is m.State.READY
    assert machine.session_id == b"\x11" * 16
    assert machine.mechanism == ""


def test_the_login_request_carries_the_username():
    machine = new()
    machine.start()
    machine.data_to_send()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body()))
    out = machine.data_to_send()
    assert struct.unpack(">H", out[2:4])[0] == c.kXR_login
    assert out[8:16].rstrip(b"\x00") == b"tester"


def test_negotiated_reports_what_the_server_advertised():
    machine = new()
    machine.start()
    machine.data_to_send()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body(flags=c.kXR_haveTLS)))
    event = only(machine, m.Negotiated)
    assert event.info.has_tls
    assert machine.protocol_info.version == 0x0500_0000


def test_a_security_trailer_starts_the_auth_exchange():
    machine = new()
    machine.start()
    machine.data_to_send()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body()))
    machine.data_to_send()  # the login frame
    machine.receive_data(ok(2, login_body(sec="&P=host")))
    assert machine.state is m.State.AUTH
    out = machine.data_to_send()
    assert struct.unpack(">H", out[2:4])[0] == c.kXR_auth
    assert out[16:20] == b"host"
    assert out[24:] == b"host\x00"

    machine.receive_data(ok(3))
    assert only(machine, m.Ready).mechanism == "host"
    assert machine.state is m.State.READY


def test_a_multi_round_mechanism_answers_every_challenge():
    class TwoStep(Credential):
        name = "two"

        def __init__(self):
            self.seen = []

        def initial(self):
            return b"first"

        def step(self, challenge):
            self.seen.append(challenge)
            return b"second" if len(self.seen) == 1 else None

        @classmethod
        def available(cls, offer, config, *, username, host):
            return cls()

    cred = TwoStep()
    machine = new(credentials=iter([cred]))
    machine.start()
    machine.data_to_send()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body()))
    machine.data_to_send()
    machine.receive_data(ok(2, login_body(sec="&P=two")))
    assert machine.data_to_send()[24:] == b"first"

    machine.receive_data(frame(3, c.kXR_authmore, b"challenge"))
    assert cred.seen == [b"challenge"]
    assert machine.data_to_send()[24:] == b"second"

    machine.receive_data(ok(3))
    assert machine.state is m.State.READY


def test_a_mechanism_that_runs_out_falls_through_to_the_next():
    class Dead(Credential):
        name = "dead"

        def initial(self):
            return b"x"

        @classmethod
        def available(cls, offer, config, *, username, host):
            return cls()

    machine = new(credentials=iter([Dead(), HostCredential()]))
    machine.start()
    machine.data_to_send()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body()))
    machine.receive_data(ok(2, login_body(sec="&P=dead&P=host")))
    machine.data_to_send()

    machine.receive_data(frame(3, c.kXR_authmore, b"more"))
    assert machine.data_to_send()[24:] == b"host\x00"
    assert machine.mechanism == "host"


def test_a_credential_that_cannot_mint_a_blob_is_skipped():
    class Broken(Credential):
        name = "broken"

        def initial(self):
            raise RuntimeError("no proxy")

        @classmethod
        def available(cls, offer, config, *, username, host):
            return cls()

    machine = new(credentials=iter([Broken(), HostCredential()]))
    bring_up(machine, sec="&P=broken&P=host")
    assert machine.mechanism == "host"
    assert "RuntimeError: no proxy" in machine._auth_rejected["broken"]


def test_running_out_of_mechanisms_fails_the_session():
    machine = new(credentials=iter([]))
    machine.start()
    machine.data_to_send()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body()))
    machine.receive_data(ok(2, login_body(sec="&P=gsi")))
    event = only(machine, m.Failed)
    assert isinstance(event.error, NoMechanismError)
    assert event.error.offered == ["gsi"]
    assert machine.state is m.State.FAILED


def test_authmore_with_no_exchange_open_is_an_error():
    machine = new(credentials=iter([]))
    machine.start()
    machine.data_to_send()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body()))
    machine.state = m.State.AUTH
    machine.receive_data(frame(3, c.kXR_authmore, b"?"))
    assert "no exchange open" in str(only(machine, m.Failed).error)


def test_a_session_key_installs_a_signer():
    class Signing(HostCredential):
        name = "host"
        session_key = b"k" * 32

    machine = new(credentials=iter([Signing()]))
    machine.start()
    machine.data_to_send()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body(security=True, level=c.kXR_secStandard)))
    machine.receive_data(ok(2, login_body(sec="&P=host")))
    machine.receive_data(ok(3))
    assert machine.signer is not None

    machine.data_to_send()
    machine.submit(r.Write(b"HDL0", 0, b"data"))
    out = machine.data_to_send()
    assert struct.unpack(">H", out[2:4])[0] == c.kXR_sigver
    assert len(out) == (24 + 48) + (24 + 4)  # signature frame, then the request
    assert struct.unpack(">H", out[74:76])[0] == c.kXR_write


def test_an_unsigned_session_sends_one_frame_per_request():
    machine = ready()
    machine.submit(r.Write(b"HDL0", 0, b"data"))
    assert len(machine.data_to_send()) == 24 + 4


def test_a_server_error_during_bring_up_fails_the_session():
    machine = new()
    machine.start()
    machine.data_to_send()
    machine.receive_data(handshake_reply())
    machine.receive_data(error(1, 3010, "not authorized"))
    event = only(machine, m.Failed)
    assert isinstance(event.error, ServerError)
    assert event.error.code == 3010
    assert machine.state is m.State.FAILED


def test_an_unexpected_status_during_bring_up_fails_the_session():
    machine = new()
    machine.start()
    machine.data_to_send()
    machine.receive_data(frame(0, c.kXR_wait, struct.pack(">i", 1) + b"\x00"))
    assert "unexpected kXR_wait during handshake" in str(only(machine, m.Failed).error)


# --------------------------------------------------------------------------
# TLS
# --------------------------------------------------------------------------


def test_a_server_that_demands_tls_stops_for_the_upgrade():
    machine = new()
    machine.start()
    machine.data_to_send()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body(flags=c.kXR_haveTLS | c.kXR_gotoTLS)))
    event = only(machine, m.NeedTLS)
    assert event.reason == "server requested TLS"
    assert machine.state is m.State.TLS
    assert machine.data_to_send() == b""  # login is withheld until the socket is up

    machine.tls_established()
    assert machine.tls_active and machine.state is m.State.LOGIN
    assert struct.unpack(">H", machine.data_to_send()[2:4])[0] == c.kXR_login


@pytest.mark.parametrize("flag", [c.kXR_gotoTLS, c.kXR_tlsLogin, c.kXR_tlsSess, c.kXR_tlsData])
def test_every_tls_demand_flag_is_honoured(flag):
    machine = new()
    machine.start()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body(flags=c.kXR_haveTLS | flag)))
    assert machine.state is m.State.TLS


def test_a_server_that_wants_data_encrypted_gets_the_whole_socket_encrypted():
    """``kXR_tlsData`` names file data, not the login - but this client reads
    and writes on the connection it logged in on, so there is nowhere else for
    the bytes to go."""
    machine = new()
    machine.start()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body(flags=c.kXR_haveTLS | c.kXR_tlsData)))
    assert only(machine, m.NeedTLS).reason == "server requested TLS"


def test_a_bit_that_names_one_request_does_not_upgrade_the_session():
    """``kXR_tlsGPF`` and ``kXR_tlsTPC`` qualify requests this client either
    does not send or does not send over this socket."""
    machine = new()
    machine.start()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body(flags=c.kXR_haveTLS | c.kXR_tlsGPF | c.kXR_tlsTPC)))
    assert machine.state is not m.State.TLS


def test_client_policy_upgrades_even_when_the_server_did_not_ask():
    machine = new(want_tls=True)
    machine.start()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body(flags=c.kXR_haveTLS)))
    assert only(machine, m.NeedTLS).reason == "client policy"


def test_a_server_without_tls_cannot_satisfy_a_tls_requirement():
    machine = new(want_tls=True)
    machine.start()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body(flags=0)))
    event = only(machine, m.Failed)
    assert "does not offer it" in str(event.error)
    assert machine.state is m.State.FAILED


def test_tls_established_is_refused_outside_the_tls_state():
    machine = ready()
    with pytest.raises(ProtocolError, match="called in state READY"):
        machine.tls_established()


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


def test_submit_is_refused_before_the_session_is_ready():
    machine = new()
    with pytest.raises(ProtocolError, match="cannot submit in state NEW"):
        machine.submit(r.Ping())


def test_regular_streams_start_above_the_bring_up_ids():
    machine = ready()
    assert machine.submit(r.Ping()) == SID
    assert machine.submit(r.Ping()) == SID + 1
    assert machine.in_flight == 2


def test_a_successful_response_completes_the_stream():
    machine = ready()
    sid = machine.submit(r.Stat("/a/b"), path="/a/b")
    machine.receive_data(ok(sid, b"id0 1 0 0\x00"))
    event = only(machine, m.Completed)
    assert event.streamid == sid
    assert event.data == b"id0 1 0 0\x00"
    assert isinstance(event.request, r.Stat)
    assert machine.in_flight == 0


def test_streamids_are_recycled_once_a_stream_finishes():
    machine = ready()
    sid = machine.submit(r.Ping())
    machine.receive_data(ok(sid))
    drain(machine)
    assert machine.submit(r.Ping()) == sid


def test_oksofar_chunks_accumulate_into_the_final_body():
    machine = ready()
    sid = machine.submit(r.Read(b"HDL0", 0, 9))
    machine.receive_data(frame(sid, c.kXR_oksofar, b"abc"))
    machine.receive_data(frame(sid, c.kXR_oksofar, b"def"))
    machine.receive_data(ok(sid, b"ghi"))
    events = drain(machine)
    assert kinds(events) == ["Chunk", "Chunk", "Completed"]
    assert [e.data for e in events[:2]] == [b"abc", b"def"]
    assert events[2].data == b"abcdefghi"


def test_a_request_error_names_the_path_it_was_about():
    machine = ready()
    sid = machine.submit(r.Stat("/no/such"), path="/no/such")
    machine.receive_data(error(sid, 3011, "no such file or directory"))
    event = only(machine, m.Failed)
    assert isinstance(event.error, FileNotFoundError)
    assert event.error.filename == "/no/such"
    assert machine.in_flight == 0


def test_a_redirect_leaves_the_stream_for_the_caller_to_release():
    machine = ready()
    sid = machine.submit(r.Open("/a", 0))
    machine.receive_data(frame(sid, c.kXR_redirect, struct.pack(">i", 1095) + b"other\x00"))
    event = only(machine, m.Redirected)
    assert event.target.host == "other" and event.target.port == 1095
    assert machine.in_flight == 1

    machine.release(sid)
    assert machine.in_flight == 0
    assert machine.submit(r.Ping()) == sid


def test_releasing_an_unknown_stream_is_harmless():
    machine = ready()
    machine.release(999)


def test_wait_asks_the_driver_to_sleep_and_resend():
    machine = ready()
    sid = machine.submit(r.Stat("/a"))
    sent = machine.data_to_send()
    machine.receive_data(frame(sid, c.kXR_wait, struct.pack(">i", 3) + b"staging\x00"))
    event = only(machine, m.Waiting)
    assert (event.seconds, event.message, event.resend) == (3, "staging", True)

    machine.resume(sid)
    assert machine.data_to_send() == sent


def test_a_wait_is_capped_by_the_configuration():
    machine = ready(Config(username="t", auth_order=("host",), wait_cap=5.0))
    sid = machine.submit(r.Stat("/a"))
    machine.receive_data(frame(sid, c.kXR_wait, struct.pack(">i", 3600) + b"\x00"))
    assert only(machine, m.Waiting).seconds == 5.0


def test_waitresp_promises_an_unsolicited_answer_later():
    machine = ready()
    sid = machine.submit(r.Prepare(["/a"]))
    machine.receive_data(frame(sid, c.kXR_waitresp, struct.pack(">i", 30)))
    event = only(machine, m.Waiting)
    assert event.resend is False and event.seconds == 30
    assert machine.in_flight == 1

    machine.receive_data(ok(sid, b"done"))
    assert only(machine, m.Completed).data == b"done"


def test_resuming_a_stream_that_is_not_waiting_is_an_error():
    machine = ready()
    with pytest.raises(ProtocolError, match="stream 99 is not waiting"):
        machine.resume(99)


def test_an_unexpected_status_fails_only_that_stream():
    machine = ready()
    sid = machine.submit(r.Ping())
    machine.receive_data(frame(sid, 4999, b""))
    assert "unexpected response status" in str(only(machine, m.Failed).error)
    assert machine.state is m.State.READY


def test_a_response_on_an_unknown_stream_is_dropped():
    machine = ready()
    machine.receive_data(ok(999, b"stale"))
    assert drain(machine) == []


# --------------------------------------------------------------------------
# kXR_status
# --------------------------------------------------------------------------


def status_frame(sid: int, requestid: int, resptype: int, data: bytes, info: bytes = b"") -> bytes:
    body = (
        struct.pack(">I", 0)
        + struct.pack(">H", sid)
        + bytes([requestid - c.kXR_1stRequest, resptype])
        + bytes(4)
        + struct.pack(">i", len(data))
        + info
    )
    return frame(sid, c.kXR_status, body) + data


def test_a_final_status_response_carries_its_data_as_a_raw_trailer():
    machine = ready()
    sid = machine.submit(r.PgRead(b"HDL0", 0, 4))
    machine.receive_data(
        status_frame(sid, c.kXR_pgread, c.kXR_FinalResult, b"data", struct.pack(">q", 0))
    )
    event = only(machine, m.Completed)
    assert event.data == b"data"
    assert event.status is not None and event.status.offset == 0
    assert machine.in_flight == 0


def test_partial_status_responses_accumulate():
    machine = ready()
    sid = machine.submit(r.PgRead(b"HDL0", 0, 8))
    machine.receive_data(status_frame(sid, c.kXR_pgread, c.kXR_PartialResult, b"aaaa"))
    assert only(machine, m.Chunk).data == b"aaaa"
    machine.receive_data(status_frame(sid, c.kXR_pgread, c.kXR_FinalResult, b"bbbb"))
    assert only(machine, m.Completed).data == b"aaaabbbb"


def test_a_status_response_with_no_data_still_completes():
    machine = ready()
    sid = machine.submit(r.PgWrite(b"HDL0", 0, b""))
    machine.receive_data(status_frame(sid, c.kXR_pgwrite, c.kXR_FinalResult, b""))
    assert only(machine, m.Completed).data == b""


def test_the_status_trailer_is_reassembled_across_receives():
    machine = ready()
    sid = machine.submit(r.PgRead(b"HDL0", 0, 6))
    raw = status_frame(sid, c.kXR_pgread, c.kXR_FinalResult, b"abcdef")
    for i in range(0, len(raw), 5):
        machine.receive_data(raw[i : i + 5])
    assert only(machine, m.Completed).data == b"abcdef"


# --------------------------------------------------------------------------
# How much of an answer is too much
#
# A request that says how many bytes it wants has said how big its answer may
# be. Anything past that is a server that has lost track of what it is doing,
# or one that would like the client to run out of memory; either way the only
# safe thing is to stop accumulating and fail the stream.
# --------------------------------------------------------------------------


def test_an_answer_of_the_size_that_was_asked_for_is_kept():
    machine = ready()
    sid = machine.submit(r.Read(b"HDL0", 0, 4))
    machine.receive_data(ok(sid, b"abcd"))
    assert only(machine, m.Completed).data == b"abcd"


def test_an_answer_longer_than_the_read_is_refused():
    machine = ready()
    sid = machine.submit(r.Read(b"HDL0", 0, 4))
    machine.receive_data(ok(sid, b"abcde"))
    assert "more than the 4 bytes" in str(only(machine, m.Failed).error)
    assert machine.in_flight == 0


def test_chunks_that_add_up_to_more_than_was_asked_for_are_refused():
    """The case that costs memory: every frame is small, the total is not."""
    machine = ready()
    sid = machine.submit(r.Read(b"HDL0", 0, 4))
    machine.receive_data(frame(sid, c.kXR_oksofar, b"abc"))
    assert kinds(drain(machine)) == ["Chunk"]
    machine.receive_data(frame(sid, c.kXR_oksofar, b"def"))
    assert "more than the 4 bytes" in str(only(machine, m.Failed).error)
    assert machine.in_flight == 0


def test_a_vector_read_is_capped_by_what_all_its_ranges_come_to():
    machine = ready()
    sid = machine.submit(r.ReadV([(b"HDL0", 0, 8), (b"HDL0", 64, 8)]))
    machine.receive_data(ok(sid, b"x" * (2 * c.READ_LIST_ENTRY_LEN + 17)))
    assert "more than the 48 bytes" in str(only(machine, m.Failed).error)


def test_a_paged_read_is_capped_with_its_checksums_counted_in():
    machine = ready()
    sid = machine.submit(r.PgRead(b"HDL0", 0, 8))
    machine.receive_data(status_frame(sid, c.kXR_pgread, c.kXR_FinalResult, b"x" * 13))
    assert "more than the 12 bytes" in str(only(machine, m.Failed).error)


def test_a_paged_write_is_capped_by_the_pages_it_could_be_asked_to_resend():
    machine = ready()
    sid = machine.submit(r.PgWrite(b"HDL0", 0, b"x" * c.kXR_pgUnitSZ))
    machine.receive_data(status_frame(sid, c.kXR_pgwrite, c.kXR_FinalResult, b"x" * 25))
    assert "more than the 24 bytes" in str(only(machine, m.Failed).error)


def test_a_reply_whose_size_nobody_declared_is_not_capped():
    """A dirlist is as long as the directory is. There is nothing to check it
    against, and a limit invented here would break the large ones."""
    machine = ready()
    sid = machine.submit(r.Dirlist("/data"))
    machine.receive_data(frame(sid, c.kXR_oksofar, b"a\n" * 5000))
    machine.receive_data(ok(sid, b"z\n"))
    assert len(only(machine, m.Completed).data) == 10002


def test_an_error_body_is_never_truncated():
    """The cap is about memory, not about conformance: a long refusal still
    arrives whole, because the reason is all it carries."""
    machine = ready()
    sid = machine.submit(r.Read(b"HDL0", 0, 4), path="/data/a.root")
    machine.receive_data(error(sid, 3011, "x" * 400))
    assert "x" * 400 in str(only(machine, m.Failed).error)


# --------------------------------------------------------------------------
# kXR_attn
# --------------------------------------------------------------------------


def test_an_embedded_async_response_is_delivered_to_its_stream():
    machine = ready()
    sid = machine.submit(r.Prepare(["/a"]))
    inner = ok(sid, b"staged")
    machine.receive_data(
        frame(0, c.kXR_attn, struct.pack(">ii", c.kXR_asynresp, 0) + inner)
    )
    assert only(machine, m.Completed).data == b"staged"


def test_a_plain_attn_becomes_a_notice():
    machine = ready()
    machine.receive_data(
        frame(0, c.kXR_attn, struct.pack(">i", c.kXR_asyncms) + b"going down\x00")
    )
    event = only(machine, m.Attention)
    assert event.info.message == "going down"


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


def test_a_response_split_across_receives_is_reassembled():
    machine = ready()
    sid = machine.submit(r.Stat("/a"))
    raw = ok(sid, b"id0 1 0 0\x00")
    machine.receive_data(raw[:3])
    assert drain(machine) == []
    machine.receive_data(raw[3:9])
    assert drain(machine) == []
    machine.receive_data(raw[9:])
    assert only(machine, m.Completed).data == b"id0 1 0 0\x00"


def test_two_responses_in_one_read_are_both_dispatched():
    machine = ready()
    first, second = machine.submit(r.Ping()), machine.submit(r.Ping())
    machine.receive_data(ok(first) + ok(second))
    assert [e.streamid for e in drain(machine)] == [first, second]


def test_responses_may_arrive_out_of_order():
    machine = ready()
    first, second = machine.submit(r.Ping()), machine.submit(r.Ping())
    machine.receive_data(ok(second) + ok(first))
    assert [e.streamid for e in drain(machine)] == [second, first]


# --------------------------------------------------------------------------
# Teardown
# --------------------------------------------------------------------------


def test_eof_fails_every_stream_in_flight():
    machine = ready()
    sid = machine.submit(r.Stat("/a"))
    machine.receive_data(b"")
    events = drain(machine)
    assert kinds(events) == ["Failed", "Disconnected"]
    assert events[0].streamid == sid
    assert isinstance(events[0].error, XrdConnectionError)
    assert machine.state is m.State.CLOSED
    assert machine.in_flight == 0


def test_eof_after_closing_says_nothing_more():
    machine = ready()
    machine.close()
    drain(machine)
    machine.receive_data(None)
    assert drain(machine) == []


def test_close_sends_endsess_with_the_session_id():
    machine = ready()
    machine.close()
    out = machine.data_to_send()
    assert struct.unpack(">H", out[2:4])[0] == c.kXR_endsess
    assert out[4:20] == b"\x11" * 16
    assert machine.state is m.State.CLOSED
    assert isinstance(drain(machine)[0], m.Disconnected)


def test_an_ungraceful_close_says_nothing_on_the_wire():
    machine = ready()
    machine.close(graceful=False)
    assert machine.data_to_send() == b""


def test_closing_before_ready_says_nothing_on_the_wire():
    machine = new()
    machine.start()
    machine.data_to_send()
    machine.close()
    assert machine.data_to_send() == b""


def test_next_event_pops_one_at_a_time():
    machine = new()
    bring_up(machine)
    assert isinstance(machine.next_event(), m.Negotiated)
    assert isinstance(machine.next_event(), m.Ready)
    assert machine.next_event() is None


def test_repr_shows_the_endpoint_and_the_state():
    machine = ready()
    assert repr(machine) == (
        "SessionMachine(srv.example.org:1094, state=READY, in_flight=0, tls=False)"
    )


def test_stream_ids_wrap_around_at_the_top_of_the_field():
    """The streamid is two bytes; the counter comes back to the bottom."""
    machine = ready()
    machine._next_sid = 0xFFFF
    assert machine.submit(r.Ping()) == 0xFFFF
    assert machine.submit(r.Ping()) == SID


def test_a_streamid_that_is_still_in_flight_is_not_handed_out_twice():
    machine = ready()
    sid = machine.submit(r.Ping())
    machine._free.clear()
    machine._next_sid = sid
    with pytest.raises(ProtocolError, match="stream id space exhausted"):
        machine.submit(r.Ping())


def test_a_signer_leaves_the_requests_it_does_not_cover_alone():
    from xrd.crypto.sigver import Signer

    machine = ready()
    machine.signer = Signer(b"k" * 32, c.kXR_secStandard, {})
    machine.submit(r.Read(b"HDL0", 0, 4))
    assert machine.data_to_send()[:2] != struct.pack(">H", 0)  # no kXR_sigver prologue
    assert machine.signer.seqno == 0


def test_a_credential_that_cannot_answer_a_challenge_is_skipped():
    """``step`` raising is the same kind of unusable as ``initial`` raising."""

    class Halfway(Credential):
        name = "half"

        def initial(self):
            return b"half\x00"

        def step(self, challenge):
            raise RuntimeError("no key for that")

        @classmethod
        def available(cls, offer, config, *, username, host):
            return cls()

    machine = new(credentials=iter([Halfway(), HostCredential()]))
    machine.start()
    machine.data_to_send()
    machine.receive_data(handshake_reply())
    machine.receive_data(ok(1, protocol_body()))
    machine.receive_data(ok(2, login_body(sec="&P=half&P=host")))
    machine.data_to_send()
    machine.receive_data(frame(3, c.kXR_authmore, b"more"))
    assert machine.data_to_send()[24:] == b"host\x00"
    assert "RuntimeError: no key for that" in machine._auth_rejected["half"]


def test_status_data_for_a_stream_nobody_is_waiting_on_is_dropped():
    machine = ready()
    machine._on_status_data(999, b"orphan")
    assert drain(machine) == []


def test_an_error_body_that_is_not_an_error_still_becomes_one():
    """``raise_for_status`` says nothing about code 0; the caller still needs an exception."""
    from xrd.proto import responses as rp

    exc = m._server_error(rp.ErrorInfo(0, "nothing went wrong"), "/d/f.root")
    assert isinstance(exc, ServerError)
    assert "nothing went wrong" in str(exc)
