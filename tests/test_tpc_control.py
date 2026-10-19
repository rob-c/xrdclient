"""Calling a third-party copy off, watching it, and delegating it.

The pull - the destination's second ``kXR_sync`` - is the one step of a
rendezvous that can take an hour, and XrdCl lets its caller stop it: the
destination is sent ``ofs.tpc cancel`` and answers the waiting sync. These
tests play that destination: a sync deferred with ``kXR_waitresp`` whose
answer comes later as a ``kXR_asynresp`` - when the transfer is done, or when
the cancel arrives.
"""

from __future__ import annotations

import errno
import socket
import struct
import threading
import time
from collections.abc import Iterator

import pytest

import xrdclient
from xrdclient.config import Config
from xrdclient.copy import tpc
from xrdclient.errors import AbortedError, UnsupportedError
from xrdclient.proto import constants as c
from xrdclient.session import Router
from xrdclient.testing import FakeServer, error, frame
from xrdclient.testing.server import _Connection
from xrdclient.url import XRootDURL, parse


def _later(sid: int, reply: bytes) -> bytes:
    """``reply`` for stream ``sid``, sent as the answer a ``kXR_waitresp`` promised."""
    return frame(0, c.kXR_attn, struct.pack(">i", c.kXR_asynresp) + bytes(4) + reply)


class _Puller:
    """A destination whose pull - the second sync - is answered when it says so."""

    def __init__(self, dst: FakeServer, *, answers_cancel: bool = True) -> None:
        self.dst = dst
        self.syncs = 0
        self.waiting: tuple[_Connection, int] | None = None
        self.parked = threading.Event()
        self.cancels: list[bytes] = []
        self.answers_cancel = answers_cancel
        dst.handlers[c.kXR_sync] = self.sync
        dst.fctl = self.fctl

    def sync(self, conn: _Connection, sid: int, params: bytes, body: bytes) -> Iterator[bytes]:
        self.syncs += 1
        if self.syncs == 1:
            yield frame(sid, c.kXR_ok)
            return
        self.waiting = (conn, sid)
        self.parked.set()
        yield frame(sid, c.kXR_waitresp, struct.pack(">i", 3600))

    def finish(self, reply: bytes | None = None) -> None:
        """Answer the parked pull: done, or ``reply``."""
        assert self.waiting is not None
        conn, sid = self.waiting
        assert isinstance(conn.sock, socket.socket)
        conn.sock.sendall(_later(sid, reply or frame(sid, c.kXR_ok)))

    def fctl(self, path: str, body: bytes) -> bytes:
        self.cancels.append(body)
        if self.answers_cancel:
            assert self.waiting is not None
            _, sid = self.waiting
            self.finish(error(sid, 3017, "destination file prematurely closed"))
        return b""


def test_a_cancelled_pull_sends_the_destination_xrdcls_cancel(server):
    stop = threading.Event()
    with FakeServer(dirs=["/"]) as dst:
        puller = _Puller(dst)

        def cancel_once_parked() -> None:
            puller.parked.wait(10)
            stop.set()

        threading.Thread(target=cancel_once_parked).start()
        started = time.monotonic()
        with pytest.raises(AbortedError) as caught:
            xrdclient.third_party(server.url / "data/a.root", dst.url / "b.root", cancel=stop)
        assert time.monotonic() - started < 10
        assert puller.cancels == [b"ofs.tpc cancel\x00"]
        # The destination's answer to the cancel is what the wait ended with.
        assert "prematurely closed" in str(caught.value.__cause__)
        # Both handles were let go of: the source's after the pull, the
        # destination's once the cancel had been answered.
        assert dst.seen.count(c.kXR_close) >= 1
    assert caught.value.errno == errno.ECANCELED
    assert tpc.failed_end(caught.value) == ""


def test_a_destination_deaf_to_the_cancel_has_its_connection_shut(server, monkeypatch):
    monkeypatch.setattr(tpc, "CANCEL_WAIT", 0.2)
    with FakeServer(dirs=["/"]) as dst:
        puller = _Puller(dst, answers_cancel=False)
        started = time.monotonic()
        with pytest.raises(AbortedError):
            xrdclient.third_party(
                server.url / "data/a.root",
                dst.url / "b.root",
                cancel=lambda: puller.parked.is_set(),
                verify=False,
            )
        assert time.monotonic() - started < 10
        assert puller.cancels == [b"ofs.tpc cancel\x00"]


def test_a_cancel_before_the_pull_stops_the_set_up(server):
    with FakeServer(dirs=["/"]) as dst:
        with pytest.raises(AbortedError):
            xrdclient.third_party(
                server.url / "data/a.root", dst.url / "b.root", cancel=lambda: True, verify=False
            )
        assert c.kXR_sync not in dst.seen


def test_a_cancel_hook_that_raises_is_a_cancel(server):
    def broken() -> bool:
        raise RuntimeError("no idea")

    with FakeServer(dirs=["/"]) as dst, pytest.raises(AbortedError):
        xrdclient.third_party(
            server.url / "data/a.root", dst.url / "b.root", cancel=broken, verify=False
        )


def test_progress_is_what_the_destination_holds(server, monkeypatch):
    monkeypatch.setattr(tpc, "PROGRESS_EVERY", 0.05)
    seen: list[tuple[int, int | None]] = []
    with FakeServer(dirs=["/"]) as dst:
        puller = _Puller(dst)

        def report(done: int, total: int | None) -> None:
            seen.append((done, total))
            if len(seen) == 3:
                puller.finish()

        result = xrdclient.third_party(
            server.url / "data/a.root", dst.url / "b.root", progress=report, verify=False
        )
    assert result.size == 11
    assert seen[0] == (0, 11)  # the destination's file, empty while it pulls
    assert seen[-1] == (11, 11)  # and the whole of it once the pull is done


def test_progress_survives_a_destination_that_cannot_say(server, monkeypatch):
    monkeypatch.setattr(tpc, "PROGRESS_EVERY", 0.01)
    seen: list[tuple[int, int | None]] = []
    with FakeServer(dirs=["/"]) as dst:
        puller = _Puller(dst)
        dst.handlers[c.kXR_stat] = lambda conn, sid, params, body: iter([error(sid, 3012, "busy")])
        timer = threading.Timer(0.3, lambda: puller.finish())
        timer.start()
        xrdclient.third_party(
            server.url / "data/a.root",
            dst.url / "b.root",
            progress=lambda done, total: seen.append((done, total)),
            verify=False,
        )
    assert seen == [(11, 11)]  # never told anything wrong while it pulled
    timer.join()


def test_a_failed_pull_watched_is_still_the_destinations_answer(server):
    with FakeServer(dirs=["/"]) as dst:
        puller = _Puller(dst)

        def fail_once_parked() -> None:
            puller.parked.wait(10)
            assert puller.waiting is not None
            puller.finish(error(puller.waiting[1], 3007, "pull failed"))

        threading.Thread(target=fail_once_parked).start()
        with pytest.raises(OSError, match="pull failed") as caught:
            xrdclient.third_party(
                server.url / "data/a.root", dst.url / "b.root", cancel=lambda: False, verify=False
            )
    assert tpc.failed_end(caught.value) == ""


def test_the_end_that_refused_is_named(server):
    with (
        FakeServer(files={"/taken.root": b"x"}) as dst,
        pytest.raises(FileExistsError) as caught,
    ):
        xrdclient.third_party(
            server.url / "data/a.root", dst.url / "taken.root", overwrite=False, verify=False
        )
    assert tpc.failed_end(caught.value) == "destination"
    assert tpc.failed_end(ValueError("unrelated")) == ""


def _tpc_config(answer: bytes):
    """A ``kXR_query`` handler answering ``tpc tpcdlg`` with ``answer``, the rest as usual."""
    from xrdclient.testing import server as fake

    def handler(conn, sid, params, body):
        infotype = struct.unpack(">H", params[:2])[0]
        if infotype == c.kXR_Qconfig and body.startswith(b"tpc tpcdlg"):
            yield frame(sid, c.kXR_ok, answer)
            return
        yield from fake._h_query_fctl(conn, sid, params, body)

    return handler


def test_a_delegated_copy_to_a_lite_destination_never_opens_the_source_with_a_key():
    with (
        FakeServer(files={"/data/a.root": b"hello world"}) as src,
        FakeServer(dirs=["/"]) as dst,
    ):
        dst.handlers[c.kXR_query] = _tpc_config(b"1\ngsi\x00")
        source = xrdclient.parse(str(src.url / "data/a.root") + "?authz=tok&xrdcl.secuid=1")
        result = xrdclient.third_party(
            source, dst.url / "b.root", delegate=True, streams=4, verify=False
        )
        assert result.size == 11
        assert not [p for p in src.opened if "tpc.key" in p]
        (pull,) = [p for p in dst.opened if "tpc.key" in p]
        assert "tpc.dlgon=1" in pull
        assert "tpc.str=3" in pull
        assert pull.endswith("&tpc.scgi=authz=tok")
        assert dst.seen.count(c.kXR_sync) == 2


def test_a_delegated_copy_to_a_classic_destination_still_keys_the_source(server):
    with FakeServer(dirs=["/"]) as dst:
        dst.handlers[c.kXR_query] = _tpc_config(b"1\ntpcdlg\x00")
        xrdclient.third_party(
            server.url / "data/a.root", dst.url / "b.root", delegate=True, verify=False
        )
        assert [p for p in server.opened if "tpc.key" in p]


def test_a_destination_without_third_party_copy_is_refused():
    with (
        FakeServer(files={"/data/a.root": b"hello world"}) as src,
        FakeServer(dirs=["/"]) as dst,
    ):
        dst.handlers[c.kXR_query] = _tpc_config(b"0\ntpcdlg\x00")
        with pytest.raises(UnsupportedError, match="does not support third-party-copy"):
            xrdclient.third_party(src.url / "data/a.root", dst.url / "b.root", delegate=True)
        assert c.kXR_sync not in dst.seen


def test_a_delegated_copy_of_a_source_it_cannot_place_needs_a_lite_destination():
    with FakeServer() as src, FakeServer(dirs=["/"]) as dst:
        dst.handlers[c.kXR_query] = _tpc_config(b"1\ntpcdlg\x00")
        with pytest.raises(UnsupportedError, match="does not support delegation"):
            xrdclient.third_party(src.url / "no/such.root", dst.url / "b.root", delegate=True)
        # A lite one can: the destination opens the source itself.
        dst.handlers[c.kXR_query] = _tpc_config(b"1\ngsi\x00")
        result = xrdclient.third_party(
            src.url / "no/such.root", dst.url / "c.root", delegate=True, verify=False
        )
        assert result.size == 0
        (pull,) = [p for p in dst.opened if "tpc.key" in p and p.startswith("/c.root")]
        assert "oss.asize" not in pull


def test_a_destination_that_cannot_answer_its_configuration_is_refused():
    with FakeServer(files={"/data/a.root": b"x"}) as src, FakeServer(dirs=["/"]) as dst:
        dst.handlers[c.kXR_query] = lambda conn, sid, params, body: iter([error(sid, 3013, "no")])
        with pytest.raises(UnsupportedError, match="does not support third-party-copy"):
            xrdclient.third_party(src.url / "data/a.root", dst.url / "b.root", delegate=True)


def test_the_host_ids_are_xrdcls():
    assert tpc._host_id(xrdclient.parse("root://u@[::1]:1094//f")) == "u@[::1]:1094"
    assert tpc._host_id(xrdclient.parse("root://h.example:2000//f")) == "h.example:2000"


def test_an_http_pair_is_cancelled_at_a_performance_marker(monkeypatch):
    seen: dict[str, object] = {}

    def fake_http(source, target, **kwargs):
        seen.update(kwargs)
        kwargs["progress"](5, 10)
        raise AssertionError("not reached")

    monkeypatch.setattr("xrdclient.http.third_party", fake_http)
    with pytest.raises(AbortedError):
        xrdclient.third_party(
            "davs://a.example/f", "davs://b.example/f", cancel=lambda: True, verify=False
        )
    assert seen["delegate"] is False and seen["streams"] is None


def test_an_http_pair_reports_progress_through_the_markers(monkeypatch):
    got: list[tuple[int, int | None]] = []

    def fake_http(source, target, **kwargs):
        kwargs["progress"](5, 10)
        assert kwargs["streams"] == 3 and kwargs["delegate"] is True
        return xrdclient.CopyResult(str(source), str(target), 10, 0.1)

    monkeypatch.setattr("xrdclient.http.third_party", fake_http)
    xrdclient.third_party(
        "davs://a.example/f",
        "davs://b.example/f",
        progress=lambda d, t: got.append((d, t)),
        delegate=True,
        streams=3,
        verify=False,
    )
    assert got == [(5, 10)]


def test_an_unwatched_http_pair_is_given_no_progress(monkeypatch):
    def fake_http(source, target, **kwargs):
        assert kwargs["progress"] is None
        return xrdclient.CopyResult(str(source), str(target), 1, 0.1)

    monkeypatch.setattr("xrdclient.http.third_party", fake_http)
    xrdclient.third_party("davs://a.example/f", "davs://b.example/f", verify=False)


def test_an_unrelated_note_names_no_end():
    exc = OSError("x")
    exc.add_note("something else entirely")
    assert tpc.failed_end(exc) == ""


def test_an_http_pair_watched_only_for_a_cancel_carries_on(monkeypatch):
    def fake_http(source, target, **kwargs):
        kwargs["progress"](5, 10)
        return xrdclient.CopyResult(str(source), str(target), 10, 0.1)

    monkeypatch.setattr("xrdclient.http.third_party", fake_http)
    result = xrdclient.third_party(
        "davs://a.example/f", "davs://b.example/f", cancel=lambda: False, verify=False
    )
    assert result.size == 10


def test_the_capability_a_placement_redirect_carried_goes_on_the_keyed_open():
    """EOS signs the data server's opens; the key's open must carry the signature too."""
    with (
        FakeServer(files={"/data/a.root": b"hello world"}) as disk,
        FakeServer(files={"/data/a.root": b"hello world"}) as head,
        FakeServer(dirs=["/"]) as dst,
    ):
        head.redirects[c.kXR_open] = (*disk.address, "cap.sym=AQE&cap.msg=zz")
        xrdclient.third_party(head.url / "data/a.root", dst.url / "b.root", verify=False)
        (keyed,) = [p for p in disk.opened if "tpc.key" in p]
        assert keyed.endswith("&cap.sym=AQE&cap.msg=zz")


def test_each_end_can_have_settings_of_its_own(server, monkeypatch):
    seen: dict[str, xrdclient.Config | None] = {}

    class Recording(Router):
        def __init__(self, url: str | XRootDURL, config: Config | None = None, **kw: bool) -> None:
            parsed = url if isinstance(url, XRootDURL) else parse(url)
            seen[parsed.host + str(parsed.port)] = config
            super().__init__(url, config, **kw)

    monkeypatch.setattr(tpc, "Router", Recording)
    with FakeServer(dirs=["/"]) as dst:
        xrdclient.third_party(
            server.url / "data/a.root",
            dst.url / "b.root",
            config=xrdclient.Config(connect_timeout=11, request_timeout=11),
            target_config=xrdclient.Config(connect_timeout=22, request_timeout=22, tpc_timeout=0),
            verify=False,
        )
        theirs = seen[dst.url.host + str(dst.url.port)]
    mine = seen[server.url.host + str(server.url.port)]
    assert mine is not None and theirs is not None
    assert (mine.connect_timeout, mine.request_timeout) == (11, 11)
    # tpc_timeout=0: the destination's own limits stand, the pull's included.
    assert (theirs.connect_timeout, theirs.request_timeout) == (22, 22)


def test_the_destinations_connection_is_given_room_for_the_pull(server, monkeypatch):
    seen: list[xrdclient.Config | None] = []

    class Recording(Router):
        def __init__(self, url: str | XRootDURL, config: Config | None = None, **kw: bool) -> None:
            seen.append(config)
            super().__init__(url, config, **kw)

    monkeypatch.setattr(tpc, "Router", Recording)
    with FakeServer(dirs=["/"]) as dst:
        xrdclient.third_party(
            server.url / "data/a.root",
            dst.url / "b.root",
            config=xrdclient.Config(
                request_timeout=11, stall_deadline=0, wait_budget=5, tpc_timeout=99
            ),
            verify=False,
        )
    mine, theirs = seen[0], seen[1]
    assert mine is not None and theirs is not None
    assert (mine.request_timeout, mine.wait_budget) == (11, 5)  # the source's: as given
    assert theirs.request_timeout == theirs.wait_budget == 99  # raised to the pull's limit
    assert theirs.stall_deadline == 0  # no limit stays no limit


# ---------------------------------------------------------------------------
# What each open carries: the URL's own CGI, with the rendezvous added to it
# ---------------------------------------------------------------------------


def test_the_urls_cgi_reaches_every_open_of_the_rendezvous(server):
    """XrdCl merges its ``tpc.*`` fields over the URL's CGI; it does not replace it.

    An ``authz=`` token, a ``svcClass`` space token or an ``XRD.*`` option on
    either URL is for that server, and goes on the placement open, the keyed
    source open and the destination open alike. ``xrdcl.*`` fields are this
    client's directives and stay behind, as ``GetPathWithFilteredParams``
    leaves them; a ``tpc.*`` field the rendezvous sets is the rendezvous's.
    """
    source = xrdclient.parse(
        str(server.url / "data/a.root") + "?authz=srctok&svcClass=t1&xrdcl.secuid=1"
    )
    with FakeServer(dirs=["/"]) as dst:
        target = xrdclient.parse(
            str(dst.url / "b.root") + "?authz=dsttok&XRD.wantprot=gsi&tpc.key=x"
        )
        xrdclient.third_party(source, target, verify=False)
        opened = server.opened + dst.opened
    placement, keyed, pull = _opens_of(opened, "/data/a.root?", "/b.root?")
    assert placement == "/data/a.root?authz=srctok&svcClass=t1&tpc.stage=placement"
    assert keyed.startswith("/data/a.root?authz=srctok&svcClass=t1&tpc.key=")
    assert keyed.endswith("&tpc.stage=copy")
    assert pull.startswith("/b.root?authz=dsttok&XRD.wantprot=gsi&tpc.key=")
    assert pull.count("tpc.key=") == 1 and "tpc.key=x&" not in pull
    assert "xrdcl." not in "".join(opened)


def _opens_of(opened: list[str], source: str, target: str) -> tuple[str, str, str]:
    """The placement, the keyed open and the pull's open, among every path opened."""
    placement, keyed = [p for p in opened if p.startswith(source)]
    (pull,) = [p for p in opened if p.startswith(target)]
    return placement, keyed, pull


def test_a_redirects_capability_follows_the_users_cgi_on_the_keyed_open():
    with (
        FakeServer(files={"/data/a.root": b"hello world"}) as disk,
        FakeServer(files={"/data/a.root": b"hello world"}) as head,
        FakeServer(dirs=["/"]) as dst,
    ):
        head.redirects[c.kXR_open] = (*disk.address, "cap.sym=AQE")
        source = xrdclient.parse(str(head.url / "data/a.root") + "?authz=tok")
        xrdclient.third_party(source, dst.url / "b.root", verify=False)
        (keyed,) = [p for p in disk.opened if "tpc.key" in p]
    assert keyed.startswith("/data/a.root?authz=tok&tpc.key=")
    assert keyed.endswith("&tpc.stage=copy&cap.sym=AQE")  # once: not the user's CGI again


# ---------------------------------------------------------------------------
# After a refusal: only what this copy made is cleaned up
# ---------------------------------------------------------------------------


def test_an_empty_file_that_was_already_there_survives_a_refused_copy(server):
    """kXR_new refused with 3018 (EEXIST): the file is not this copy's to remove."""
    with FakeServer(files={"/taken.root": b""}) as dst:
        with pytest.raises(FileExistsError):
            xrdclient.third_party(
                server.url / "data/a.root", dst.url / "taken.root", overwrite=False, verify=False
            )
        assert "/taken.root" in dst.files
        assert c.kXR_rm not in dst.seen


def test_an_empty_destination_this_copy_made_is_removed_with_its_own_settings(server, monkeypatch):
    seen: list[str] = []
    from xrdclient.copy.engine import _remove as original  # what tpc calls _remove

    def remove(url, config):
        seen.append(config.username)
        original(url, config)

    monkeypatch.setattr(tpc, "_remove", remove)
    with FakeServer(dirs=["/"]) as dst:
        dst.handlers[c.kXR_sync] = _failing_pull
        with pytest.raises(OSError, match="pull failed"):
            xrdclient.third_party(
                server.url / "data/a.root",
                dst.url / "b.root",
                config=xrdclient.Config(username="src-user"),
                target_config=xrdclient.Config(username="dst-user"),
                verify=False,
            )
        assert "/b.root" not in dst.files
    assert seen == ["dst-user"]


def _failing_pull(conn, sid, params, body):
    """A destination that arms the pull, then fails it."""
    conn.s.syncs = getattr(conn.s, "syncs", 0) + 1
    yield frame(sid, c.kXR_ok) if conn.s.syncs == 1 else error(sid, 3007, "pull failed")


def test_a_bad_copy_is_removed_with_the_destinations_settings(server, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(tpc, "_remove", lambda url, config: seen.append(config.username))
    with FakeServer(dirs=["/"]) as dst, pytest.raises(xrdclient.ChecksumMismatchError):
        xrdclient.third_party(
            server.url / "data/a.root",
            dst.url / "b.root",
            config=xrdclient.Config(username="src-user"),
            target_config=xrdclient.Config(username="dst-user"),
        )
    assert seen == ["dst-user"]


# ---------------------------------------------------------------------------
# XrdCl's capability checks: CheckTPCLite at the destination, CheckTPC at the source
# ---------------------------------------------------------------------------


def test_an_ordinary_copy_asks_the_destination_too():
    """``CheckTPCLite`` runs for every copy; only TPC lite depends on delegating."""
    with (
        FakeServer(files={"/data/a.root": b"hello world"}) as src,
        FakeServer(dirs=["/"]) as dst,
    ):
        dst.handlers[c.kXR_query] = _tpc_config(b"0\ntpcdlg\x00")
        with pytest.raises(UnsupportedError, match="Destination does not support third-party-copy"):
            xrdclient.third_party(src.url / "data/a.root", dst.url / "b.root", verify=False)
        assert c.kXR_sync not in dst.seen
        assert c.kXR_close in dst.seen  # the pull's handle was let go of


def test_a_source_without_third_party_copy_is_refused_before_the_pull():
    with (
        FakeServer(files={"/data/a.root": b"hello world"}) as src,
        FakeServer(dirs=["/"]) as dst,
    ):
        src.config_values["tpc"] = "tpc"  # a stock server echoes a name it has no value for
        with pytest.raises(UnsupportedError) as caught:
            xrdclient.third_party(src.url / "data/a.root", dst.url / "b.root", verify=False)
        # XrdCl's words, without the full stop its destination messages have.
        assert caught.value.message == "Source does not support third-party-copy"
        assert tpc.failed_end(caught.value) == ""
        assert c.kXR_sync not in dst.seen
        assert not [p for p in src.opened if "tpc.key" in p]


def test_a_source_that_cannot_answer_is_refused():
    with (
        FakeServer(files={"/data/a.root": b"hello world"}) as src,
        FakeServer(dirs=["/"]) as dst,
    ):
        src.handlers[c.kXR_query] = lambda conn, sid, params, body: iter([error(sid, 3013, "no")])
        with pytest.raises(UnsupportedError, match="Source does not support"):
            xrdclient.third_party(src.url / "data/a.root", dst.url / "b.root", verify=False)


def test_the_source_is_asked_on_the_data_server_it_landed_on(server):
    with FakeServer() as rdr, FakeServer(dirs=["/"]) as dst:
        rdr.redirects[c.kXR_open] = ("127.0.0.1", server.address[1], "")
        rdr.config_values["tpc"] = "0"  # the redirector's answer is not the question
        xrdclient.third_party(
            f"root://localhost:{rdr.address[1]}//data/a.root", dst.url / "b.root", verify=False
        )
        assert c.kXR_query in server.seen
        assert c.kXR_query not in rdr.seen


def test_a_lite_copy_never_asks_the_source():
    with (
        FakeServer(files={"/data/a.root": b"hello world"}) as src,
        FakeServer(dirs=["/"]) as dst,
    ):
        dst.handlers[c.kXR_query] = _tpc_config(b"1\ngsi\x00")
        xrdclient.third_party(
            src.url / "data/a.root", dst.url / "b.root", delegate=True, verify=False
        )
        assert c.kXR_query not in src.seen


def test_a_destination_open_refused_for_want_of_tpc_says_so(server):
    """XrdCl's one special case of the destination's open: ``tpc not supported``."""
    with FakeServer(dirs=["/"]) as dst:
        dst.handlers[c.kXR_open] = lambda conn, sid, params, body: iter(
            [error(sid, 3005, "tpc not supported")]
        )
        with pytest.raises(UnsupportedError, match="Destination does not support") as caught:
            xrdclient.third_party(server.url / "data/a.root", dst.url / "b.root", verify=False)
    assert tpc.failed_end(caught.value) == ""


def test_a_failed_placement_names_no_end_and_a_failed_keyed_open_the_source(server):
    """XrdCl adds ``(source)`` to the keyed open's failure, not to the placement's."""
    with FakeServer() as dst, pytest.raises(FileNotFoundError) as missing:
        xrdclient.third_party(server.url / "data/none.root", dst.url / "x", verify=False)
    assert tpc.failed_end(missing.value) == ""
    with FakeServer(dirs=["/"]) as dst:
        server.handlers[c.kXR_open] = _refusing_the_key
        with pytest.raises(PermissionError) as keyed:
            xrdclient.third_party(server.url / "data/a.root", dst.url / "b.root", verify=False)
    assert tpc.failed_end(keyed.value) == "source"


def _refusing_the_key(conn, sid, params, body):
    """A source that places the file but will not take the key."""
    from xrdclient.testing import server as fake

    if b"tpc.key=" in body:
        yield error(sid, 3010, "key refused")
        return
    yield from fake._HANDLERS[c.kXR_open](conn, sid, params, body)


# ---------------------------------------------------------------------------
# failed_end is public
# ---------------------------------------------------------------------------


def test_failed_end_is_reachable_without_importing_a_submodule():
    import xrdclient.copy.tpc  # the submodule still imports by name
    from xrdclient.copy import failed_end
    from xrdclient.copy import tpc as module

    assert xrdclient.failed_end is module.failed_end is failed_end
    assert "failed_end" in xrdclient.__all__
    assert callable(xrdclient.copy)  # still the function, not the subpackage


# ---------------------------------------------------------------------------
# The pull's own time limit: XrdCl's CPTPCTimeout
# ---------------------------------------------------------------------------


def test_a_pull_outlives_the_request_timeout(server):
    """A silent destination is pulling, not hung: ``tpc_timeout`` bounds that wait."""
    with FakeServer(dirs=["/"]) as dst:
        puller = _Puller(dst)

        def finish_late() -> None:
            puller.parked.wait(10)
            time.sleep(1.0)
            puller.finish()

        threading.Thread(target=finish_late).start()
        result = xrdclient.third_party(
            server.url / "data/a.root",
            dst.url / "b.root",
            config=xrdclient.Config(request_timeout=0.3, stall_deadline=0.3),
            verify=False,
        )
    assert result.size == 11


def test_tpc_timeout_bounds_the_pull(server):
    with FakeServer(dirs=["/"]) as dst:
        _Puller(dst)  # never finishes
        started = time.monotonic()
        with pytest.raises(TimeoutError) as caught:
            xrdclient.third_party(
                server.url / "data/a.root",
                dst.url / "b.root",
                config=xrdclient.Config(tpc_timeout=0.5),
                verify=False,
            )
    assert time.monotonic() - started < 10
    assert tpc.failed_end(caught.value) == ""


def test_the_copys_own_timeout_wins_over_tpc_timeout(server):
    with FakeServer(dirs=["/"]) as dst:
        _Puller(dst)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            xrdclient.third_party(
                server.url / "data/a.root",
                dst.url / "b.root",
                config=xrdclient.Config(tpc_timeout=3600),
                timeout=0.5,
                verify=False,
            )
    assert time.monotonic() - started < 10


def test_a_destination_that_cannot_be_reached_is_the_destinations_failure(server):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # closed again: nothing listens there
    with pytest.raises(OSError) as caught:
        xrdclient.third_party(
            server.url / "data/a.root",
            f"root://127.0.0.1:{port}//x",
            config=xrdclient.Config(connect_retries=0),
            verify=False,
        )
    assert tpc.failed_end(caught.value) == "destination"


def test_a_clean_up_that_fails_does_not_hide_why_the_copy_failed(server, monkeypatch):
    def refuse(url, config):
        raise PermissionError("not yours to remove")

    monkeypatch.setattr(tpc, "_remove", refuse)
    with FakeServer(dirs=["/"]) as dst:
        dst.handlers[c.kXR_sync] = _failing_pull
        with pytest.raises(OSError, match="pull failed"):
            xrdclient.third_party(server.url / "data/a.root", dst.url / "b.root", verify=False)
        del dst.handlers[c.kXR_sync]  # a pull that "succeeds", leaving the file empty
        with pytest.raises(xrdclient.ChecksumMismatchError):
            xrdclient.third_party(server.url / "data/a.root", dst.url / "c.root")


def test_a_destination_with_bytes_in_it_is_left_alone(server, monkeypatch):
    """A partial pull is evidence, not a lie: only an empty file passes for a finished copy."""
    monkeypatch.setattr(tpc, "_probe", lambda url, config: (5, 0))
    with FakeServer(dirs=["/"]) as dst:
        dst.handlers[c.kXR_sync] = _failing_pull
        with pytest.raises(OSError, match="pull failed"):
            xrdclient.third_party(server.url / "data/a.root", dst.url / "b.root", verify=False)
        assert c.kXR_rm not in dst.seen


def test_the_destination_is_created_readable_by_all_as_xrdcl_creates_it(server):
    """XrdCl's ``ThirdPartyCopyJob`` opens the destination with ``UR|UW|GR|OR`` - 0644."""
    from xrdclient.testing import server as fake

    modes: list[int] = []

    def recording(conn, sid, params, body):
        if b"tpc.key=" in body:
            modes.append(struct.unpack(">H", params[:2])[0])
        yield from fake._HANDLERS[c.kXR_open](conn, sid, params, body)

    with FakeServer(dirs=["/"]) as dst:
        dst.handlers[c.kXR_open] = recording
        xrdclient.third_party(server.url / "data/a.root", dst.url / "b.root", verify=False)
    assert modes == [0o644]


def test_cleanup_false_leaves_a_failed_copys_destination_for_the_caller(server):
    """gfal2's ``transfer_cleanup`` off: whatever the copy left behind stays."""
    with FakeServer(dirs=["/"]) as dst:
        dst.handlers[c.kXR_sync] = _failing_pull
        with pytest.raises(OSError, match="pull failed"):
            xrdclient.third_party(
                server.url / "data/a.root", dst.url / "b.root", verify=False, cleanup=False
            )
        assert "/b.root" in dst.files
        del dst.handlers[c.kXR_sync]
        with pytest.raises(xrdclient.ChecksumMismatchError):
            xrdclient.third_party(server.url / "data/a.root", dst.url / "c.root", cleanup=False)
        assert "/c.root" in dst.files
        assert c.kXR_rm not in dst.seen


def test_cleanup_false_reaches_an_http_pair(monkeypatch):
    seen: dict[str, object] = {}

    def fake_http(source, target, **kwargs):
        seen.update(kwargs)
        return xrdclient.CopyResult(str(source), str(target), 1, 0.1)

    monkeypatch.setattr("xrdclient.http.third_party", fake_http)
    xrdclient.third_party("davs://a.example/f", "davs://b.example/f", verify=False, cleanup=False)
    assert seen["cleanup"] is False
