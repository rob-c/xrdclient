"""Property tests for the inbound framing: where a read ends must not matter.

TCP hands the client whatever it has, cut wherever it likes. The framer
(:meth:`~xrdclient.proto.machine.SessionMachine.receive_data` and its
per-link cursor), the :class:`~xrdclient.proto.buffer.Reader` and the
response parsers must decode a stream cut at any boundaries exactly as they
decode it whole - and garbage must never get further than the
:class:`~xrdclient.errors.ProtocolError` they document.

Kept quick: modest example counts, no example database (nothing is written
into the working tree), and the heaviest marked ``property`` so a run can
select or skip them. ``XRD_PROPERTY_EXAMPLES`` scales every count up for a
deliberate soak.
"""

from __future__ import annotations

import contextlib
import os
import struct
from collections.abc import Callable

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.conftest import error, frame, ok
from tests.test_machine import ready, status_frame
from xrdclient.errors import ProtocolError
from xrdclient.proto import constants as c
from xrdclient.proto import machine as m
from xrdclient.proto import requests as r
from xrdclient.proto import responses as rp
from xrdclient.proto.buffer import Reader, Writer

_SCALE = int(os.environ.get("XRD_PROPERTY_EXAMPLES", "1"))


def _quick(examples: int) -> settings:
    return settings(
        max_examples=examples * _SCALE,
        deadline=None,
        database=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )


# ---------------------------------------------------------------------------
# Strategies: a valid reply stream for some requests in flight
# ---------------------------------------------------------------------------

_BODY = st.binary(max_size=300)


@st.composite
def _final(draw: st.DrawFn, sid: int) -> bytes:
    """One request's last word."""
    kind = draw(st.sampled_from(["ok", "error", "status", "redirect", "wait", "async"]))
    if kind == "ok":
        return ok(sid, draw(_BODY))
    if kind == "error":
        message = draw(st.text(alphabet=st.characters(codec="ascii", exclude_characters="\0")))
        return error(sid, draw(st.sampled_from([3010, 3011, 3005, 0])), message)
    if kind == "status":
        return status_frame(sid, c.kXR_pgread, c.kXR_FinalResult, draw(_BODY), bytes(8))
    if kind == "redirect":
        return frame(sid, c.kXR_redirect, struct.pack(">i", 1094) + b"elsewhere.example\0")
    if kind == "wait":
        return frame(sid, c.kXR_wait, struct.pack(">i", draw(st.integers(0, 50))) + b"busy\0")
    # A deferred answer: the promise, then the answer itself, wrapped.
    inner = ok(sid, draw(_BODY))
    wrapped = struct.pack(">ii", c.kXR_asynresp, 0) + inner
    return frame(sid, c.kXR_waitresp, struct.pack(">i", 5)) + frame(0, c.kXR_attn, wrapped)


@st.composite
def _script(draw: st.DrawFn, sid: int) -> list[bytes]:
    """Every frame one request is answered with, in order."""
    frames = [frame(sid, c.kXR_oksofar, draw(_BODY)) for _ in range(draw(st.integers(0, 3)))]
    if draw(st.booleans()):
        frames.append(status_frame(sid, c.kXR_pgread, c.kXR_PartialResult, draw(_BODY), bytes(8)))
    frames.append(draw(_final(sid)))
    return frames


@st.composite
def _stream(draw: st.DrawFn, first: int = 4) -> tuple[int, bytes]:
    """How many requests, and their replies interleaved, with noise between.

    The requests are on streams ``first``, ``first + 1``, ...
    """
    count = draw(st.integers(1, 5))
    sids = list(range(first, first + count))
    pending = {sid: draw(_script(sid)) for sid in sids}
    out: list[bytes] = []
    while pending:
        noise = draw(st.sampled_from(["none", "none", "none", "notice", "stranger"]))
        if noise == "notice":
            message = struct.pack(">ii", c.kXR_asyncms, 0) + draw(_BODY)
            out.append(frame(0, c.kXR_attn, message))
        elif noise == "stranger":
            out.append(ok(999, draw(_BODY)))  # a stream nothing was sent on
        sid = draw(st.sampled_from(sorted(pending)))
        out.append(pending[sid].pop(0))
        if not pending[sid]:
            del pending[sid]
    return count, b"".join(out)


def _cuts(data: bytes) -> st.SearchStrategy[list[int]]:
    return st.lists(st.integers(0, len(data)), max_size=12).map(sorted)


def _machine(count: int) -> m.SessionMachine:
    """A ready machine with ``count`` requests on streams 4, 5, ..."""
    machine = ready()
    for _ in range(count):
        machine.submit(r.Query(c.kXR_Qconfig, "x"))
    machine.data_to_send()
    return machine


def _decoded(events: list[m.Event]) -> list[tuple[object, ...]]:
    """Events as plain values: errors and requests do not compare by content."""
    out: list[tuple[object, ...]] = []
    for event in events:
        fields: list[object] = [type(event).__name__]
        for name in getattr(type(event), "__slots__", ()):
            value = getattr(event, name)
            if name == "request":
                continue
            if isinstance(value, BaseException):
                value = (type(value).__name__, str(value))
            fields.append(value)
        out.append(tuple(fields))
    return out


def _feed(machine: m.SessionMachine, data: bytes, cuts: list[int]) -> list[tuple[object, ...]]:
    start = 0
    for cut in [*cuts, len(data)]:
        if cut > start:
            machine.receive_data(data[start:cut])
            start = cut
    return _decoded(machine.drain())


# ---------------------------------------------------------------------------
# The machine's framer
# ---------------------------------------------------------------------------


@pytest.mark.property
@_quick(60)
@given(st.data())
def test_a_reply_stream_decodes_the_same_however_it_is_cut(data: st.DataObject) -> None:
    count, stream = data.draw(_stream())
    cuts = data.draw(_cuts(stream))
    whole = _feed(_machine(count), stream, [])
    assert _feed(_machine(count), stream, cuts) == whole
    assert whole  # something was decoded


@_quick(30)
@given(st.data())
def test_a_stream_fed_one_byte_at_a_time_decodes_the_same(data: st.DataObject) -> None:
    count, stream = data.draw(_stream())
    whole = _feed(_machine(count), stream, [])
    assert _feed(_machine(count), stream, list(range(len(stream)))) == whole


def _by_stream(events: list[tuple[object, ...]]) -> dict[object, list[tuple[object, ...]]]:
    """Each stream's events in order; events of no stream under ``None``, sorted."""
    out: dict[object, list[tuple[object, ...]]] = {}
    for event in events:
        key = event[1] if len(event) > 1 and isinstance(event[1], int) else None
        out.setdefault(key, []).append(event)
    out[None] = sorted(out.get(None, []), key=repr)
    return out


@_quick(40)
@given(st.data())
def test_a_data_path_and_the_control_link_frame_independently(data: st.DataObject) -> None:
    """Reads from two links interleaved at random never splice their half-frames.

    Each link has a framing cursor of its own; however the reads alternate,
    every stream sees exactly what it would have with each link read whole.
    """
    count, control = data.draw(_stream())
    others, path = data.draw(_stream(4 + count))
    whole = _machine(count + others)
    fed = _feed(whole, control, [])
    whole.receive_data(path, pathid=1)
    expected = fed + _decoded(whole.drain())
    machine = _machine(count + others)
    offsets = {0: 0, 1: 0}
    links = {0: control, 1: path}
    for link, step in data.draw(
        st.lists(st.tuples(st.sampled_from([0, 1]), st.integers(1, 64)), max_size=80)
    ):
        piece = links[link][offsets[link] : offsets[link] + step]
        if piece:  # an empty read is the link's end of stream
            machine.receive_data(piece, pathid=link)
            offsets[link] += len(piece)
    for link in (0, 1):
        if offsets[link] < len(links[link]):
            machine.receive_data(links[link][offsets[link] :], pathid=link)
    assert _by_stream(_decoded(machine.drain())) == _by_stream(expected)


@_quick(60)
@given(st.binary(max_size=600), st.lists(st.integers(0, 600), max_size=8))
def test_garbage_raises_nothing_but_protocol_errors(garbage: bytes, cuts: list[int]) -> None:
    machine = _machine(3)
    with contextlib.suppress(ProtocolError):
        _feed(machine, garbage, sorted(c for c in cuts if c <= len(garbage)))


@_quick(60)
@given(st.binary(max_size=200))
def test_garbage_during_bring_up_raises_nothing_but_protocol_errors(garbage: bytes) -> None:
    machine = m.SessionMachine(host="h")
    machine.start()
    machine.data_to_send()
    with contextlib.suppress(ProtocolError):
        machine.receive_data(ok(0, struct.pack(">ii", 0, c.ROOTD_PQ)) + garbage)


# ---------------------------------------------------------------------------
# The Reader cursor
# ---------------------------------------------------------------------------

_FIELDS = st.lists(
    st.one_of(
        st.tuples(st.just("u8"), st.integers(0, 2**8 - 1)),
        st.tuples(st.just("u16"), st.integers(0, 2**16 - 1)),
        st.tuples(st.just("u32"), st.integers(0, 2**32 - 1)),
        st.tuples(st.just("i32"), st.integers(-(2**31), 2**31 - 1)),
        st.tuples(st.just("u64"), st.integers(0, 2**64 - 1)),
        st.tuples(st.just("i64"), st.integers(-(2**63), 2**63 - 1)),
        st.tuples(st.just("raw"), st.binary(max_size=40)),
    ),
    max_size=20,
)


def _write(fields: list[tuple[str, object]]) -> bytes:
    w = Writer()
    for kind, value in fields:
        getattr(w, kind)(value)
    return w.bytes()


def _read(reader: Reader, fields: list[tuple[str, object]]) -> list[object]:
    out: list[object] = []
    for kind, value in fields:
        if kind == "raw":
            assert isinstance(value, bytes)
            out.append(reader.bytes(len(value)))
        else:
            out.append(getattr(reader, kind)())
    return out


@_quick(60)
@given(_FIELDS)
def test_the_reader_reads_back_what_the_writer_wrote(fields: list[tuple[str, object]]) -> None:
    data = _write(fields)
    for source in (data, bytearray(data), memoryview(data)):
        reader = Reader(source)
        assert _read(reader, fields) == [value for _, value in fields]
        assert reader.remaining == 0


@_quick(60)
@given(_FIELDS, st.data())
def test_a_truncated_buffer_is_a_protocol_error_and_nothing_else(
    fields: list[tuple[str, object]], data: st.DataObject
) -> None:
    whole = _write(fields)
    if not whole:
        return
    cut = data.draw(st.integers(0, len(whole) - 1))
    with pytest.raises(ProtocolError, match="truncated"):
        _read(Reader(whole[:cut]), fields)


@_quick(60)
@given(st.binary(max_size=100), st.lists(st.integers(0, 120), max_size=6))
def test_cstrings_consume_up_to_their_terminator_wherever_it_falls(
    data: bytes, widths: list[int]
) -> None:
    reader = Reader(data)
    for width in widths:
        try:
            reader.cstring(width)
        except ProtocolError:
            assert width > reader.remaining
    while reader.remaining:
        before = reader.remaining
        reader.cstring()
        assert reader.remaining < before


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------

_PARSERS = [
    rp.parse_error,
    rp.parse_redirect,
    rp.parse_wait,
    rp.parse_waitresp,
    rp.parse_attn,
    rp.parse_status,
    rp.parse_pgwrite_cse,
    rp.parse_protocol,
    rp.parse_login,
    rp.parse_bind,
    rp.parse_stat,
    rp.parse_statvfs,
    rp.parse_statx,
    rp.parse_dirlist,
    rp.parse_locate,
    rp.parse_open,
    rp.parse_checksum,
    rp.parse_prepare_status,
    rp.parse_space,
    rp.parse_checkpoint,
    rp.parse_readlink,
    rp.parse_readv,
    rp.parse_fattr,
    rp.parse_fattr_list,
    rp.parse_fattr_tree,
]


@pytest.mark.property
@pytest.mark.parametrize("parse", _PARSERS, ids=lambda f: f.__name__)
@_quick(25)
@given(garbage=st.binary(max_size=200))
def test_a_parser_given_garbage_raises_only_a_protocol_error(
    parse: Callable[[bytes], object], garbage: bytes
) -> None:
    with contextlib.suppress(ProtocolError):
        parse(garbage)


@_quick(60)
@given(st.integers(-(2**31), 2**31 - 1), st.text(max_size=60).filter(lambda t: "\0" not in t))
def test_an_error_body_parses_to_its_code_and_message(code: int, message: str) -> None:
    body = struct.pack(">i", code) + message.encode() + b"\0"
    info = rp.parse_error(body)
    assert (info.code, info.message) == (code, message)


@_quick(60)
@given(
    st.integers(0, 2**16 - 1),
    st.sampled_from([c.kXR_FinalResult, c.kXR_PartialResult]),
    st.binary(max_size=50),
    st.binary(max_size=16),
)
def test_a_status_body_parses_to_what_it_says(
    sid: int, resptype: int, data: bytes, info: bytes
) -> None:
    whole = status_frame(sid, c.kXR_pgread, resptype, data, info)
    parsed = rp.parse_status(whole[8 : len(whole) - len(data)])
    assert (parsed.streamid, parsed.resptype, parsed.dlen, parsed.info) == (
        sid,
        resptype,
        len(data),
        info,
    )
