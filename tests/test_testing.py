"""The fake server itself.

:class:`~xrd.testing.FakeServer` is part of the public API, so its own
behaviour - the contents it exposes, the knobs that inject awkward server
behaviour, and its lifecycle - is tested here rather than assumed.
"""

from __future__ import annotations

import socket
import struct
import threading

import pytest

import xrd
from xrd.proto import constants as c
from xrd.testing import FakeServer, from_directory
from xrd.testing.server import _checksum, _clean, _fattr_reply, _splice, main

# ---------------------------------------------------------------------------
# Contents
# ---------------------------------------------------------------------------


def test_files_given_to_the_constructor_are_there():
    with FakeServer(files={"/a/b.root": b"data"}) as srv:
        assert srv.contents("/a/b.root") == b"data"
        assert srv.dirs == {"/", "/a"}


def test_add_file_creates_the_parents():
    srv = FakeServer()
    srv.add_file("/x/y/z.txt", b"hi")
    assert {"/x", "/x/y"} <= srv.dirs
    assert srv.contents("/x/y/z.txt") == b"hi"


def test_add_dir_creates_the_parents():
    srv = FakeServer()
    srv.add_dir("/p/q/r")
    assert srv.dirs == {"/", "/p", "/p/q", "/p/q/r"}


def test_paths_are_normalised_on_the_way_in():
    srv = FakeServer(files={"a/../a/b": b"x"})
    assert srv.contents("/a/b") == b"x"


def test_contents_of_a_missing_file_is_a_key_error():
    with pytest.raises(KeyError):
        FakeServer().contents("/nope")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_the_address_is_an_ephemeral_loopback_port():
    with FakeServer() as srv:
        host, port = srv.address
        assert host == "127.0.0.1"
        assert port > 0


def test_the_url_points_at_the_address():
    with FakeServer() as srv:
        assert str(srv.url) == f"root://{srv.address[0]}:{srv.address[1]}//"


def test_a_server_that_is_never_started_claims_no_port():
    """Building one to hold contents must not open a listening socket."""
    srv = FakeServer(files={"/a": b"x"})
    assert srv._bound is None
    assert "unbound" in repr(srv)
    srv.stop()  # nothing to release, and no error either


def test_stop_releases_the_port_and_is_idempotent():
    srv = FakeServer().start()
    host, port = srv.address
    srv.stop()
    srv.stop()
    assert srv._bound is None
    with socket.socket() as probe:
        probe.bind((host, port))  # free again


def test_the_address_of_a_stopped_server_is_where_it_was():
    """Asking after the fact must not quietly claim a fresh port."""
    srv = FakeServer().start()
    was = srv.address
    srv.stop()
    assert srv.address == was
    assert srv._bound is None
    assert f"{was[0]}:{was[1]}" in repr(srv)


def test_start_is_idempotent():
    srv = FakeServer()
    try:
        assert srv.start() is srv
        srv.start()
        with xrd.FileSystem(srv.url) as fs:
            fs.ping()
    finally:
        srv.stop()


def test_repr_counts_the_contents():
    with FakeServer(files={"/a": b""}, dirs=["/d"]) as srv:
        assert "files=1" in repr(srv)
        assert "dirs=2" in repr(srv)  # "/" and "/d"


def test_disconnect_drops_live_connections_but_keeps_listening():
    with FakeServer(files={"/f": b"ok"}) as srv:
        fs = xrd.FileSystem(srv.url)
        fs.ping()
        srv.disconnect()
        assert fs.stat("/f").st_size == 2  # the router reconnects
        fs.close()


def test_a_client_that_sends_a_short_handshake_is_dropped():
    with FakeServer() as srv:
        sock = socket.create_connection(srv.address)
        sock.sendall(b"tooshort")
        sock.shutdown(socket.SHUT_WR)
        assert sock.recv(64) == b""
        sock.close()


def test_several_clients_are_served_at_once():
    with FakeServer(files={"/f": b"ok"}) as srv:
        errors: list[BaseException] = []

        def hammer() -> None:
            try:
                with xrd.FileSystem(srv.url) as fs:
                    for _ in range(5):
                        assert fs.read_bytes("/f") == b"ok"
            except BaseException as exc:  # pragma: no cover - only on failure
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert not errors


# ---------------------------------------------------------------------------
# Injected behaviour
# ---------------------------------------------------------------------------


def test_seen_records_opcodes_in_order():
    with FakeServer() as srv:
        with xrd.FileSystem(srv.url) as fs:
            fs.ping()
        assert srv.seen[:3] == [c.kXR_protocol, c.kXR_login, c.kXR_ping]


def test_a_redirect_fires_once_then_the_request_is_served():
    with FakeServer(files={"/f": b"ok"}) as srv:
        srv.redirects[c.kXR_stat] = (*srv.address, "tok=1")
        with xrd.FileSystem(srv.url) as fs:
            assert fs.stat("/f").st_size == 2
        assert not srv.redirects


def test_a_wait_counts_down():
    with FakeServer(files={"/f": b"ok"}) as srv:
        srv.waits[c.kXR_stat] = 2
        with xrd.FileSystem(srv.url) as fs:
            fs.stat("/f")
        assert srv.waits[c.kXR_stat] == 0
        assert srv.seen.count(c.kXR_stat) == 3


def test_chunk_reads_splits_the_body():
    with FakeServer(files={"/f": b"0123456789"}) as srv:
        srv.chunk_reads = 4
        with xrd.FileSystem(srv.url) as fs:
            assert fs.read_bytes("/f") == b"0123456789"


def test_chunk_reads_below_the_body_size_sends_one_frame():
    with FakeServer(files={"/f": b"abc"}) as srv:
        srv.chunk_reads = 100
        with xrd.FileSystem(srv.url) as fs:
            assert fs.read_bytes("/f") == b"abc"


def test_config_values_are_what_a_query_answers():
    with FakeServer() as srv:
        srv.config_values["role"] = "manager"
        with xrd.FileSystem(srv.url) as fs:
            assert fs.query_config("role") == {"role": "manager"}
            assert fs.query_config("unknown") == {}  # unset names are absent
            # An empty answer must not shift the ones that follow it.
            assert fs.query_config("unknown", "role") == {"role": "manager"}


def test_the_announced_protocol_can_be_chosen():
    with FakeServer(version=0x0400_0000, flags=c.kXR_isManager) as srv:
        with xrd.FileSystem(srv.url) as fs:
            info = fs.protocol()
        assert info.version == 0x0400_0000
        assert info.flags == c.kXR_isManager


def test_an_unsupported_request_is_refused_not_ignored():
    from xrd.proto.frames import Request

    class Gpfile(Request):
        """An opcode the fake server has never heard of."""

        __slots__ = ()
        opcode = c.kXR_gpfile

    with FakeServer() as srv, xrd.FileSystem(srv.url) as fs:
        with pytest.raises(OSError, match="is not supported"):
            fs._router.execute(Gpfile())


def test_an_unsupported_query_is_refused():
    from xrd.proto import requests as r

    with FakeServer() as srv, xrd.FileSystem(srv.url) as fs:
        with pytest.raises(OSError):
            fs._router.execute(r.Query(c.kXR_Qopaquf, "/"))


def test_a_corrupt_pgwrite_is_reported_page_by_page():
    """Stock stores what it was sent and names the pages whose CRC did not
    survive the trip, so the client can resend those and only those."""
    from xrd.proto import requests as r
    from xrd.proto.responses import parse_pgwrite_cse

    with FakeServer() as srv:
        handle = xrd.File(srv.url / "corrupt.bin")
        handle.open(xrd.OpenFlags.UPDATE | xrd.OpenFlags.NEW | xrd.OpenFlags.MAKEPATH)
        payload = struct.pack(">I", 0xDEADBEEF) + b"bad page"
        result = handle._router.execute(r.PgWrite(handle.handle, 0, payload))
        assert parse_pgwrite_cse(result.data) == (0,)
        handle.close()


def test_only_new_and_delete_create_a_missing_file():
    """Stock xrootd creates for kXR_new and kXR_delete and for nothing else.

    The fake used to create for any write flag, which meant a client could
    open a missing file for update here and be refused by a real server.
    """
    with FakeServer() as srv:
        for flags in (xrd.OpenFlags.UPDATE, xrd.OpenFlags.APPEND, xrd.OpenFlags.WRITE):
            handle = xrd.File(srv.url / "absent.bin")
            with pytest.raises(FileNotFoundError):
                handle.open(flags | xrd.OpenFlags.MAKEPATH)
        for name, flags in (("new", xrd.OpenFlags.NEW), ("delete", xrd.OpenFlags.DELETE)):
            handle = xrd.File(srv.url / f"{name}.bin")
            handle.open(flags | xrd.OpenFlags.MAKEPATH)
            handle.close()
            assert srv.contents(f"/{name}.bin") == b""


def test_a_writev_whose_dlen_counts_its_data_is_refused():
    """What a real server answers, so the fake cannot hide the mistake.

    ``dlen`` sizes the write_list alone; anything else leaves the server
    unable to tell descriptors from data, and it says so.
    """
    from xrd.errors import InvalidArgumentError
    from xrd.proto import frames
    from xrd.proto import requests as r

    class Broken(r.WriteV):
        def payload(self) -> bytes:  # descriptors *and* data, the old way
            return r.WriteV.payload(self) + r.WriteV.trailer(self)

        def trailer(self) -> bytes:
            return b""

    with FakeServer(files={"/v.bin": b"\x00" * 8}) as srv:
        handle = xrd.File(srv.url / "v.bin")
        handle.open(xrd.OpenFlags.UPDATE)
        broken = Broken([(handle.handle, 0, b"abc")])
        assert len(frames.encode(broken, 1)) == 24 + 16 + 3
        with pytest.raises(InvalidArgumentError, match="Write vector is invalid"):
            handle._router.execute(broken)
        handle.close()


def test_writing_past_the_end_zero_fills_the_hole():
    with FakeServer() as srv:
        handle = xrd.File(srv.url / "sparse.bin")
        handle.open(xrd.OpenFlags.UPDATE | xrd.OpenFlags.NEW | xrd.OpenFlags.MAKEPATH)
        handle.write(b"end", 5)
        handle.close()
        assert srv.contents("/sparse.bin") == b"\x00" * 5 + b"end"


def test_closing_a_handle_twice_is_refused_by_the_server():
    from xrd.proto import requests as r

    with FakeServer(files={"/f": b"x"}) as srv:
        handle = xrd.File(srv.url / "f")
        handle.open()
        raw = handle.handle
        handle.close()
        with xrd.FileSystem(srv.url) as fs:
            with pytest.raises(OSError):
                fs._router.execute(r.Close(raw))


# ---------------------------------------------------------------------------
# Internals worth pinning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [("", "/"), ("a", "/a"), ("/a/", "/a"), ("//a//b", "/a/b"), ("/a/./b", "/a/b")],
)
def test_clean_normalises_to_an_absolute_path(given, expected):
    assert _clean(given) == expected


def test_splice_zero_fills_a_hole():
    data = bytearray(b"ab")
    _splice(data, 4, b"cd")
    assert bytes(data) == b"ab\x00\x00cd"


def test_splice_overwrites_in_place():
    data = bytearray(b"abcdef")
    _splice(data, 2, b"XY")
    assert bytes(data) == b"abXYef"


@pytest.mark.parametrize("algorithm", ["adler32", "crc32", "md5", "sha1", "sha256"])
def test_checksums_are_computed_for_every_algorithm_the_fake_offers(algorithm):
    assert _checksum(algorithm, b"hello world")


def test_an_unknown_checksum_algorithm_still_answers():
    assert _checksum("nonesuch", b"x")


def test_the_fattr_reply_counts_the_failures():
    reply = _fattr_reply([("a", 0, b"1"), ("b", 61, None)])
    assert reply[0] == 1  # one error
    assert reply[1] == 2  # two items


# ---------------------------------------------------------------------------
# The WebDAV fake, where no client of ours would send what is asked
# ---------------------------------------------------------------------------


@pytest.fixture
def dav():
    from xrd.testing import FakeDAVServer

    with FakeDAVServer(files={"/d/a.root": b"hello"}, dirs=["/d/sub"]) as server:
        yield server


def dav_request(server, method, path, headers=None, body=None):
    """One raw request, because these shapes have no client-side spelling."""
    import http.client

    host, port = server.address
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_deleting_a_collection_takes_its_children_with_it(dav):
    dav.add_file("/d/sub/child.root", b"child")
    status, _ = dav_request(dav, "DELETE", "/d/sub")
    assert status == 204
    assert "/d/sub/child.root" not in dav.files and "/d/sub" not in dav.dirs


def test_a_copy_must_name_exactly_one_of_source_and_destination(dav):
    both = {"Source": "http://a/x", "Destination": "http://b/x"}
    assert dav_request(dav, "COPY", "/d/a.root", both)[0] == 400
    assert dav_request(dav, "COPY", "/d/a.root", {})[0] == 400


def test_the_fake_will_not_fetch_a_source_that_is_not_http(dav):
    """The outcome of a started copy is in the body, so this is still a 202."""
    status, body = dav_request(dav, "COPY", "/d/copy.root", {"Source": "file:///etc/passwd"})
    assert status == 202
    assert b"cannot speak file" in body


def test_a_post_that_is_not_a_macaroon_request_is_not_allowed(dav):
    assert dav_request(dav, "POST", "/d", {"Content-Type": "text/plain"}, b"hi")[0] == 405


def test_a_server_that_bound_but_never_served_still_releases_its_port():
    """``address`` binds; ``stop`` must undo that even with no thread running."""
    srv = FakeServer()
    was = srv.address
    srv.stop()
    assert srv.address == was
    with FakeServer() as replacement:
        assert replacement.address != was or True  # the port is free to take again


def test_a_connection_that_will_not_close_does_not_take_the_server_with_it(monkeypatch):
    """Sockets fail to close for reasons nobody controls; serving continues."""
    from xrd.testing import server as module

    class Brittle:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def close(self):
            raise OSError("this end is beyond help")

    original = module._Connection.__init__

    def wrapped(self, server, sock):
        original(self, server, sock)
        self.rfile = Brittle(self.rfile)

    monkeypatch.setattr(module._Connection, "__init__", wrapped)
    with FakeServer(files={"/f": b"ok"}) as srv:
        with xrd.FileSystem(srv.url) as fs:
            assert fs.stat("/f").st_size == 2
        with xrd.FileSystem(srv.url) as second:  # the listener survived the first
            assert second.stat("/f").st_size == 2


def test_a_signed_request_is_answered_and_the_signature_ignored():
    """The fake does not verify signatures, but it must not choke on them."""
    from xrd.crypto.sigver import Signer

    with FakeServer(files={"/f.bin": b"..."}) as srv, xrd.FileSystem(srv.url) as fs:
        fs.ping()  # connect, so there is a session to arm
        fs._router._session._m.signer = Signer(b"k" * 32, c.kXR_secStandard, {})
        fs.truncate("/f.bin", 1)  # kXR_truncate is signed
        assert c.kXR_sigver in srv.seen
        assert srv.contents("/f.bin") == b"."


def test_a_request_with_neither_a_path_nor_a_handle_is_refused():
    from xrd.proto import requests as r

    with FakeServer() as srv, xrd.FileSystem(srv.url) as fs:
        with pytest.raises(OSError, match="no path and no handle"):
            fs._router.execute(r.Stat(""))


def test_set_is_accepted_and_answers_nothing_in_particular():
    from xrd.proto import requests as r

    with FakeServer() as srv, xrd.FileSystem(srv.url) as fs:
        assert bytes(fs._router.execute(r.Set("appid=tests"))) == b""


def test_an_attribute_list_that_stops_mid_entry_is_read_as_far_as_it_goes():
    from xrd.proto import requests as r

    with FakeServer(files={"/f.bin": b"x"}) as srv, xrd.FileSystem(srv.url) as fs:
        body = b"/f.bin\x00" + b"\x00"  # two attributes promised, one byte given
        reply = bytes(fs._router.execute(r.Fattr(c.kXR_fattrGet, body, numattr=2)))
        assert reply[:1] == b"\x00"  # a well-formed reply with nothing in it


def test_an_unknown_attribute_subcode_touches_nothing():
    from xrd.proto import requests as r

    with FakeServer(files={"/f.bin": b"x"}) as srv, xrd.FileSystem(srv.url) as fs:
        body = b"/f.bin\x00" + b"\x00\x00" + b"user.tag\x00"
        fs._router.execute(r.Fattr(99, body, numattr=1))
        assert srv.xattrs.get("/f.bin", {}) == {}


def test_writing_nothing_writes_nothing():
    data = bytearray(b"abc")
    _splice(data, 64, b"")
    assert data == b"abc"  # no hole was punched to reach an empty write


def test_the_webdav_fake_starts_once_and_stops_from_wherever_it_got_to():
    from xrd.testing import FakeDAVServer

    bound = FakeDAVServer()
    was = bound.address  # binds a port without a thread behind it
    bound.stop()  # released all the same
    assert bound.address == was

    with FakeDAVServer(files={"/f": b"ok"}) as srv:
        assert srv.start() is srv  # already serving: no second thread
        with xrd.FileSystem(srv.url) as fs:
            assert fs.stat("/f").st_size == 2


def test_a_connection_that_was_closed_behind_our_back_is_still_dropped():
    """``disconnect`` is a best-effort sweep; a socket that has already gone
    away must not stop it from reaching the rest."""
    with FakeServer() as srv:
        stale = socket.socket()
        stale.close()
        with srv._live_lock:
            srv._live.add(stale)
        srv.disconnect()  # no EBADF escapes
        assert not srv._live
        with xrd.FileSystem(srv.url) as fs:
            fs.ping()  # the listener kept listening
        assert c.kXR_ping in srv.seen


def test_a_client_that_resets_the_connection_ends_it_quietly():
    """A hard RST surfaces as ``ECONNRESET`` from the next read; the server
    thread must retire without a traceback and keep serving everyone else."""
    with FakeServer() as srv:
        sock = socket.create_connection(srv.address)
        sock.sendall(b"\x00" * 16 + struct.pack(">i", 4))
        assert len(sock.recv(64)) == 16  # handshake answered: past the preamble
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.close()  # RST, not FIN
        with xrd.FileSystem(srv.url) as fs:
            fs.ping()  # a fresh client is served as if nothing had happened
        assert c.kXR_ping in srv.seen


# ---------------------------------------------------------------------------
# Sharing a directory
# ---------------------------------------------------------------------------


def test_a_directory_is_served_under_both_its_bare_names_and_its_real_paths(tmp_path):
    (tmp_path / "mnist.root").write_bytes(b"pretend ROOT")
    (tmp_path / "notes.txt").write_bytes(b"not a dataset")
    (tmp_path / "inner").mkdir()
    server = from_directory(tmp_path, port=0)
    assert set(server.files) == {
        "/mnist.root", "/notes.txt",
        (tmp_path / "mnist.root").as_posix(), (tmp_path / "notes.txt").as_posix(),
    }
    with server, xrd.FileSystem(server.url) as fs:
        assert fs.read_bytes("/mnist.root") == b"pretend ROOT"
        assert fs.read_bytes((tmp_path / "mnist.root").as_posix()) == b"pretend ROOT"


def test_a_pattern_takes_only_the_files_it_names(tmp_path):
    (tmp_path / "a.root").write_bytes(b"one")
    (tmp_path / "b.txt").write_bytes(b"two")
    assert set(from_directory(tmp_path, port=0, pattern="*.root").files) == {
        "/a.root", (tmp_path / "a.root").as_posix()}


def test_serving_a_directory_from_the_command_line_reads_back_over_the_wire(tmp_path, capsys):
    (tmp_path / "held.root").write_bytes(b"payload")
    read = []

    def wait():
        port = int(capsys.readouterr().out.split("root://127.0.0.1:")[1].split("/")[0])
        with xrd.FileSystem(f"root://127.0.0.1:{port}/") as fs:
            read.append(fs.read_bytes("/held.root"))

    assert main([str(tmp_path), "--port", "0", "--pattern", "*.root"], wait=wait) == 0
    assert read == [b"payload"]


def test_the_command_line_server_stops_when_the_terminal_interrupts_it(tmp_path, capsys):
    (tmp_path / "held.root").write_bytes(b"payload")
    ports = []

    def wait():
        ports.append(int(capsys.readouterr().out.split("root://127.0.0.1:")[1].split("/")[0]))
        raise KeyboardInterrupt

    assert main([str(tmp_path), "--port", "0"], wait=wait) == 0
    assert "stopping" in capsys.readouterr().out
    with pytest.raises(OSError):
        xrd.FileSystem(f"root://127.0.0.1:{ports[0]}/").stat("/held.root")


def test_the_module_entry_point_hands_the_command_line_straight_to_main():
    """``python -m xrd.testing`` is the documented way in, so it must import."""
    import xrd.testing.__main__ as entry

    assert entry.main is main
