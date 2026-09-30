"""``xrdclient.compat.client``: the official bindings' API, on this library.

Two kinds of test. Most run against :class:`~xrdclient.testing.FakeServer`
and pin down the shapes a ported script relies on - ``(status, response)``
pairs, XrdCl's error codes, the ``readline`` cursor, the flag numbers - with
no daemon and no bindings installed. The parity tests at the end run the same
calls through ``XRootD.client`` and through this package against one real
``xrootd`` and require the answers to be equal, field by field.
"""

from __future__ import annotations

import functools
import gc
import os
import sys
import threading
import time
from typing import Any

import pytest

from xrdclient.compat import client
from xrdclient.compat.client import _channels, _status, env
from xrdclient.compat.client.flags import (
    AccessMode,
    DirListFlags,
    MkDirFlags,
    OpenFlags,
    PrepareFlags,
    QueryCode,
    StatInfoFlags,
)
from xrdclient.compat.client.responses import StatInfo, XRootDStatus
from xrdclient.config import Config
from xrdclient.testing import FakeServer

CONFIG = Config(username="tester", auth_order=("host",), require_tls=False, data_streams=0)

TEXT = b"aa\nbbbb\ncccccc\ndd\n"


@pytest.fixture(autouse=True)
def _test_config(monkeypatch):
    """Every compat object is built from :func:`env.config`; make it the test one."""
    monkeypatch.setattr(env, "config", lambda: CONFIG)
    yield
    # A dropped compat object closes itself when it is collected, and one in
    # a reference cycle is only collected when the cycle collector runs: have
    # that happen while this test's server is still there to be closed on.
    gc.collect()
    # Files share a channel per server; each test's server is gone afterwards.
    _channels.close_all()


@pytest.fixture
def srv():
    files = {"/d/l.txt": TEXT, "/d/sub/g": b"g", "/d/sub/deeper/h": b"h"}
    with FakeServer(files=files, dirs=["/d/empty"]) as server:
        yield server


@pytest.fixture
def root(srv):
    """The server's URL as the bindings are handed one: ``root://host:port/``."""
    return f"root://{srv.url.netloc}/"


@pytest.fixture
def fs(root):
    return client.FileSystem(root)


@pytest.fixture
def fh(root):
    f = client.File()
    status, _ = f.open(root + "/d/l.txt")
    assert status.ok, status.message
    yield f
    f.close()


def ok(pair: tuple[XRootDStatus, Any]) -> Any:
    status, response = pair
    assert status.ok, status.message
    return response


# -- flags ------------------------------------------------------------------


def test_the_flags_carry_the_bindings_numbers():
    """Ported code passes these as integers as often as by name."""
    assert (OpenFlags.READ, OpenFlags.UPDATE, OpenFlags.NEW, OpenFlags.DUP) == (16, 32, 8, 65536)
    assert (DirListFlags.STAT, DirListFlags.LOCATE, DirListFlags.RECURSIVE) == (1, 2, 4)
    assert (AccessMode.UR, AccessMode.OX) == (256, 1)
    assert (StatInfoFlags.X_BIT_SET, PrepareFlags.WRITEMODE, QueryCode.OPAQUEFILE) == (1, 16, 32)
    assert MkDirFlags.MAKEPATH == 1


def test_each_flag_namespace_has_the_bindings_reverse_mapping():
    assert OpenFlags.reverse_mapping[16] == "READ"
    assert QueryCode.reverse_mapping[6] == "CHECKSUMCANCEL"
    assert "reverse_mapping" not in OpenFlags.reverse_mapping.values()


# -- statuses ---------------------------------------------------------------


def test_success_is_the_bindings_success():
    status = _status.OK
    assert (status.ok, status.error, status.fatal) == (True, False, False)
    assert (status.status, status.code, status.errno, status.shellcode) == (0, 0, 0, 0)
    assert status.message == str(status) == "[SUCCESS] "


def test_a_server_refusal_is_code_400_with_the_servers_number(fs):
    status, info = fs.stat("/nope")
    assert info is None
    assert (status.status, status.code, status.errno, status.shellcode) == (1, 400, 3011, 54)
    assert status.error and not status.fatal and not status.ok
    assert status.message.startswith("[ERROR] Server responded with an error: [3011] ")
    assert status.message.endswith("\n")


def test_a_status_answers_to_indexing_as_well_as_attributes(fs):
    status, _ = fs.stat("/nope")
    assert status["errno"] == status.errno == 3011
    assert "ok" in status and "nope" not in status


def test_a_dead_server_is_a_fatal_connection_error():
    status, _ = client.FileSystem("root://127.0.0.1:1/").ping()
    assert (status.code, status.fatal, status.shellcode) == (108, True, 51)
    assert status.message.startswith("[FATAL] Connection error")


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (__import__("xrdclient").errors.ChecksumMismatchError("adler32", "a", "b"), 305),
        (__import__("xrdclient").errors.RedirectLimitError("loop"), 306),
        (__import__("xrdclient").errors.AuthenticationError("no"), 204),
        (__import__("xrdclient").errors.TimeoutError("slow"), 206),
        (__import__("xrdclient").errors.ProtocolError("garbled"), 303),
        (NotImplementedError("zip"), 15),
        (FileNotFoundError(2, "No such file or directory"), 12),
        (RuntimeError("?"), 2),
    ],
)
def test_each_native_failure_has_its_xrdcl_code(exc, code):
    assert _status.from_exception(exc).code == code


def test_a_local_os_error_keeps_its_errno():
    status = _status.from_exception(FileNotFoundError(2, "No such file or directory"))
    assert (status.errno, status.message) == (2, "[ERROR] OS Error: No such file or directory")


def test_a_failure_with_no_detail_is_just_its_description():
    assert _status.failure(_status.errInvalidOp).message == "[ERROR] Invalid operation"


def test_a_code_xrdcl_has_no_words_for_is_unknown():
    assert "Unknown error" in _status.status(999).message


def test_a_mistake_in_the_arguments_raises_as_it_does_in_the_bindings():
    with pytest.raises(TypeError):
        _status.guard(TypeError("bad"))
    _status.guard(OSError("fine"))  # not re-raised: it becomes a status


def test_responses_compare_by_value_and_print_like_the_bindings():
    a = StatInfo({"size": 1, "flags": 0})
    assert a == StatInfo({"size": 1, "flags": 0}) != StatInfo({"size": 2, "flags": 0})
    assert repr(a) == "<size: 1, flags: 0>"
    assert (a == 1) is False


# -- URL --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "fields"),
    [
        (
            "root://u:p@h:1234//p?a=1",
            ("u:p@h:1234", "root", "u", "p", "h", 1234, "/p", "/p?a=1", True),
        ),
        ("root://h", ("h:1094", "root", "", "", "h", 1094, "", "", True)),
        ("root://h/rel", ("h:1094", "root", "", "", "h", 1094, "rel", "rel", True)),
        ("root://[::1]:99//x", ("[::1]:99", "root", "", "", "[::1]", 99, "/x", "/x", True)),
        ("ROOT://H//x", ("H:1094", "ROOT", "", "", "H", 1094, "/x", "/x", True)),
        (
            "/local/f",
            ("localhost", "file", "", "", "localhost", 1094, "/local/f", "/local/f", True),
        ),
        ("garbage", ("garbage:1094", "root", "", "", "garbage", 1094, "", "", True)),
        ("notaurl://", ("", "", "", "", "", 1094, "", "", False)),
        ("root://h:abc//x", ("", "", "", "", "", 1094, "", "", False)),
    ],
)
def test_a_url_splits_the_way_xrdcl_splits_it(text, fields):
    url = client.URL(text)
    names = "hostid protocol username password hostname port path path_with_params"
    assert (*(getattr(url, n) for n in names.split()), url.is_valid()) == fields


def test_a_url_prints_back_as_xrdcl_prints_it():
    assert str(client.URL("root://h//x")) == "root://h:1094//x"
    assert str(client.URL("/tmp/f")) == "file://localhost/tmp/f"
    assert str(client.URL("notaurl://")) == ""
    assert repr(client.URL("root://h//x")) == "<XRootD.client.URL 'root://h:1094//x'>"


def test_a_url_can_be_cleared_and_compared():
    url = client.URL("root://h//x")
    assert url == client.URL("root://h:1094//x") and url != "root://h//x"
    assert hash(url) == hash(client.URL("root://h:1094//x"))
    url.clear()
    assert not url.is_valid() and url.path == ""


# -- FileSystem -------------------------------------------------------------


def test_stat_is_the_bindings_statinfo(fs):
    info = ok(fs.stat("/d/l.txt"))
    assert (info.size, info.flags & StatInfoFlags.IS_DIR) == (len(TEXT), 0)
    assert info.modtime == info.mtime
    assert len(info.modtimestr) == len("2026-01-01 00:00:00")
    assert set(vars(info)) >= {"id", "mode", "modeoctstr", "owner", "extended", "checksum"}


def test_stat_fills_the_protocol_5_fields_from_a_server_that_sends_them():
    from xrdclient.compat.client import _convert
    from xrdclient.types import StatInfo as NativeStat

    native = NativeStat(st_size=1, st_mtime=0, mode_str="0750", owner="me", group="us")
    info = _convert.stat_info(native)
    assert (info.mode, info.modeoctstr, info.owner, info.extended) == (
        "0750",
        "rwxr-x---",
        "me",
        True,
    )
    assert info.modtimestr == "1970-01-01 00:00:00"


def test_dirlist_names_without_stat_and_with_it(fs, srv):
    names = ok(fs.dirlist("/d"))
    assert names.parent == "/d/"
    assert sorted(e.name for e in names) == ["empty", "l.txt", "sub"]
    assert all(e.statinfo is None and e.hostaddr for e in names)
    assert names.size == 3
    detailed = ok(fs.dirlist("/d", DirListFlags.STAT))
    assert all(e.statinfo is not None for e in detailed)


def test_a_recursive_dirlist_goes_a_level_at_a_time_with_stat(fs):
    listing = ok(fs.dirlist("/d/", DirListFlags.RECURSIVE))
    names = [e.name for e in listing]
    assert set(names) == {"empty", "l.txt", "sub", "sub/g", "sub/deeper", "sub/deeper/h"}
    assert names.index("sub/deeper") < names.index("sub/deeper/h")
    assert all(e.statinfo is not None for e in listing)


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_a_zip_dirlist_lists_the_archives_members(fs, srv):
    srv.add_file("/d/a.zip", _zip_bytes({"x.txt": b"hello", "sub/": b"", "sub/y": b"yy"}))
    archive = ok(fs.stat("/d/a.zip"))
    listing = ok(fs.dirlist("/d/a.zip", DirListFlags.ZIP))
    assert (listing.parent, listing.size) == ("/d/a.zip/", 3)
    assert [(e.name, e.statinfo.size) for e in listing] == [("x.txt", 5), ("sub/", 0), ("sub/y", 2)]
    assert all(e.hostaddr == f"{srv.url.host}:{srv.url.port}" for e in listing)
    first = listing.dirlist[0].statinfo
    assert (first.id, first.modtime) == (archive.id, archive.modtime)
    assert first.flags == archive.flags & ~StatInfoFlags.IS_WRITABLE
    with_stat = ok(fs.dirlist("/d/a.zip?cgi=1", DirListFlags.ZIP | DirListFlags.STAT))
    assert with_stat.parent == "/d/a.zip/"
    assert [e.name for e in with_stat] == ["x.txt", "sub/", "sub/y"]


def test_a_path_ending_in_zip_is_listed_inside_unless_there_is_a_callback(fs, srv):
    """XrdCl's synchronous DirList adds ``Zip`` for ``*.zip``; the asynchronous one does not."""
    srv.add_file("/d/a.zip", _zip_bytes({"x.txt": b"hello"}))
    assert [e.name for e in ok(fs.dirlist("/d/a.zip"))] == ["x.txt"]
    assert [e.name for e in ok(fs.dirlist("/d/a.zip", timeout=5))] == ["x.txt"]
    done = threading.Event()
    got = []
    fs.dirlist("/d/a.zip", callback=lambda st, resp, hosts: (got.append(st), done.set()))
    assert done.wait(10)
    assert got[0].code == 400


def test_a_zip_dirlist_of_a_directory_is_a_dirlist(fs):
    plain = ok(fs.dirlist("/d", DirListFlags.STAT))
    zipped = ok(fs.dirlist("/d", DirListFlags.STAT | DirListFlags.ZIP))
    assert [e.name for e in zipped] == [e.name for e in plain]
    deep = ok(fs.dirlist("/d/", DirListFlags.RECURSIVE | DirListFlags.ZIP))
    assert "sub/deeper/h" in [e.name for e in deep]


def test_a_zip_dirlist_reports_what_xrdcl_reports(fs, srv):
    srv.add_file("/d/bad.zip", b"not an archive")
    srv.add_file("/d/empty.zip", b"")
    status, listing = fs.dirlist("/d/bad.zip", DirListFlags.ZIP)
    assert (status.status, status.code, status.errno, listing) == (1, 14, 0, None)
    assert "End-of-central-directory signature not found." in status.message
    assert ok(fs.dirlist("/d/empty.zip")).size == 0
    assert fs.dirlist("/d/gone.zip")[0].errno == 3011


def test_mkdir_rmdir_mv_rm_truncate_chmod(fs, srv):
    ok(fs.mkdir("/new/a/b", MkDirFlags.MAKEPATH))
    assert "/new/a/b" in srv.dirs
    assert fs.mkdir("/other/x")[0].errno == 3011  # no MAKEPATH, no parent
    ok(fs.rmdir("/new/a/b"))
    ok(fs.mv("/d/l.txt", "/d/m.txt"))
    ok(fs.truncate("/d/m.txt", 2))
    assert bytes(srv.files["/d/m.txt"]) == b"aa"
    ok(fs.chmod("/d/m.txt", AccessMode.UR | AccessMode.UW))
    assert srv.modes["/d/m.txt"] == 0o600
    ok(fs.rm("/d/m.txt"))
    assert fs.rm("/d/m.txt")[0].errno == 3011


def test_mkdir_without_a_mode_uses_the_bindings_default(fs, srv):
    ok(fs.mkdir("/made"))
    assert srv.modes.get("/made", 0o750) == 0o750


def test_ping_protocol_statvfs_query(fs):
    assert ok(fs.ping()) is None
    info = ok(fs.protocol())
    assert set(vars(info)) == {"version", "hostinfo"}
    assert set(vars(ok(fs.statvfs("/")))) >= {"nodes_rw", "free_rw", "utilization_staging"}
    assert ok(fs.query(QueryCode.CONFIG, "version"))
    assert ok(fs.query(QueryCode.CHECKSUM, "/d/l.txt")).startswith(b"adler32 ")


def test_the_protocol_numbers_are_reported_as_the_bindings_report_them():
    from xrdclient.compat.client import _convert
    from xrdclient.types import ProtocolInfo

    info = _convert.protocol_info(ProtocolInfo(version=0x520, flags=0x200001))
    assert (info.version, info.hostinfo) == (0x20050000, 0x01002000)


def test_locate_and_deeplocate_use_xrdcls_numbers(fs):
    where = ok(fs.locate("/d/l.txt", OpenFlags.REFRESH))
    (only,) = list(where)
    assert only.type in (0, 1, 2, 3) and only.accesstype in (0, 1)
    assert only.is_server or only.is_manager
    assert list(ok(fs.deeplocate("/d/l.txt", OpenFlags.NONE)))


def test_prepare_answers_its_handle_as_bytes(fs):
    handle = ok(fs.prepare(["/d/l.txt"], PrepareFlags.STAGE))
    assert isinstance(handle, bytes) and handle


def test_sendinfo_reaches_the_server_and_refuses_a_long_message(fs):
    assert isinstance(ok(fs.sendinfo("hello")), bytes)
    status, _ = fs.sendinfo("x" * 1025)
    assert status.code == 9


def test_sendinfo_needs_a_root_server(monkeypatch, fs):
    monkeypatch.delattr(fs.native, "_router")
    assert fs.sendinfo("x")[0].code == 15


def test_xattrs_come_back_as_the_bindings_tuples(fs):
    set_back = ok(fs.set_xattr("/d/l.txt", [("user.a", "1"), ("user.b", "two")]))
    assert [name for name, _ in set_back] == ["user.a", "user.b"]
    assert all(status["ok"] for _, status in set_back)
    got = ok(fs.get_xattr("/d/l.txt", ["user.a", "user.zz"]))
    assert got[0][:2] == ("user.a", "1") and got[0][2]["ok"]
    assert got[1][:2] == ("user.zz", "")
    refused = got[1][2]
    assert refused["errno"] == 3027
    assert refused["message"] == "[ERROR] Server responded with an error: [3027] \n"
    assert sorted(name for name, _, _ in ok(fs.list_xattr("/d/l.txt"))) == ["user.a", "user.b"]
    deleted = ok(fs.del_xattr("/d/l.txt", ["user.a"]))
    assert deleted[0][1]["ok"]


def test_cat_writes_the_file_to_standard_output(fs, capfd):
    status = fs.cat("/d/l.txt")
    assert status.ok and status["ok"]
    assert capfd.readouterr().out.encode() == TEXT


def test_cat_of_a_missing_file_writes_nothing(fs, capfd):
    assert fs.cat("/nope").errno == 3011
    assert capfd.readouterr().out == ""


def test_cat_to_a_text_only_stdout(fs, monkeypatch):
    import io

    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert fs.cat("/d/l.txt").ok
    assert out.getvalue() == TEXT.decode()


def test_filesystem_copy(fs, root, tmp_path, srv):
    target = tmp_path / "copy.txt"
    status, response = fs.copy(root + "/d/l.txt", str(target))
    assert status.ok and response is None
    assert target.read_bytes() == TEXT
    exists = fs.copy(root + "/d/l.txt", str(target))[0]  # no force
    assert (exists.code, exists.errno) == (402, 3018)  # errLocalError, kXR_ItExists
    assert fs.copy(root + "/d/l.txt", str(target), force=True)[0].ok


def test_filesystem_properties(fs):
    assert fs.get_property("FollowRedirects") == "true"
    assert fs.set_property("FollowRedirects", "false") is True
    assert fs.get_property("FollowRedirects") == "false"
    assert fs.set_property("Nope", "1") is False and fs.get_property("Nope") is None


def test_a_filesystem_knows_its_url(fs, root):
    assert isinstance(fs.url, client.URL) and fs.url.hostid == root[7:-1]
    assert repr(fs).startswith("<XRootD.client.FileSystem ")


# -- timeouts and callbacks -------------------------------------------------


def test_a_callback_gets_status_response_and_hosts(fs):
    handler = client.utils.AsyncResponseHandler()
    assert fs.stat("/d/l.txt", callback=handler).ok
    status, info, hosts = handler.wait()
    assert status.ok and info.size == len(TEXT)
    (host,) = list(hosts)
    assert isinstance(host.url, client.URL) and host.load_balancer is False


def test_a_callback_hears_about_a_failure_too(fs):
    handler = client.utils.AsyncResponseHandler()
    fs.stat("/nope", callback=handler)
    assert handler.wait()[0].errno == 3011


def test_a_callback_hears_about_a_bad_argument_as_a_status(fs, monkeypatch):
    def refuse(path, size):
        raise TypeError("size must be an integer")

    monkeypatch.setattr(fs.native, "truncate", refuse)
    handler = client.utils.AsyncResponseHandler()
    fs.truncate("/d/l.txt", 1, callback=handler)
    assert handler.wait()[0].code == 9


def test_a_callback_must_be_callable(fs):
    with pytest.raises(TypeError):
        fs.stat("/d/l.txt", callback="not a function")


def test_a_call_that_outlives_its_timeout_is_expired(fs, srv):
    from xrdclient.proto import constants as c
    from xrdclient.testing.server import frame

    def slow(conn, sid, params, body):
        time.sleep(2.5)
        yield frame(sid, c.kXR_ok)

    ok(fs.ping())
    srv.handlers[c.kXR_ping] = slow
    started = time.monotonic()
    status, _ = fs.ping(timeout=1)  # whole seconds, as the bindings take them
    assert time.monotonic() - started < 2.4  # expired, not waited out
    assert (status.code, status.error, status.message) == (206, True, "[ERROR] Operation expired")
    del srv.handlers[c.kXR_ping]
    # The connection is untouched: the late reply is dropped when it comes.
    assert ok(fs.stat("/d/l.txt")).size == len(TEXT)


def test_a_call_within_its_timeout_answers(fs):
    assert ok(fs.stat("/d/l.txt", timeout=10)).size == len(TEXT)


def test_a_timed_call_that_fails_is_a_status(fs):
    status, response = fs.stat("/d/missing", timeout=10)
    assert (status.code, response) == (400, None)


def test_a_timed_call_with_a_bad_argument_raises(fs, monkeypatch):
    def refuse() -> None:
        raise ValueError("bad")

    monkeypatch.setattr(fs.native, "ping", refuse)
    with pytest.raises(ValueError, match="bad"):
        fs.ping(timeout=5)


def test_a_bare_callback_hears_of_no_hosts():
    from xrdclient.compat.client import _dispatch

    heard = []
    done = threading.Event()

    def hear(status: XRootDStatus, response: Any, hosts: Any) -> None:
        heard.append((status.ok, response, list(hosts)))
        done.set()

    assert _dispatch.call(lambda: 1, callback=hear).ok
    assert done.wait(10)
    assert heard == [(True, None, [])]


def test_an_int_subclass_in_range_passes_the_argument_checks():
    from xrdclient.compat.client import _args

    class Number(int):
        pass

    assert _args.u16(Number(7)) == 7
    with pytest.raises(OverflowError):
        _args.u16(1 << 16)


def test_the_conversion_caches_start_again_when_full(monkeypatch):
    from xrdclient.compat.client import _convert
    from xrdclient.types import StatInfo as NativeStat

    monkeypatch.setattr(_convert, "_CACHE_LIMIT", 2)
    monkeypatch.setattr(_convert, "_UTC", {})
    monkeypatch.setattr(_convert, "_PERMISSIONS", {})
    modes = ["0600", "0644", "0755", ""]
    infos = [_convert.stat_info(NativeStat(st_mtime=n, mode_str=m)) for n, m in enumerate(modes)]
    assert [i.modtimestr for i in infos][-1] == "1970-01-01 00:00:03"
    assert [i.modeoctstr for i in infos] == ["rw-------", "rw-r--r--", "rwxr-xr-x", ""]
    assert len(_convert._UTC) <= 2 and len(_convert._PERMISSIONS) <= 2


@pytest.mark.parametrize("seconds", [0, 59, 951782400, 1709208000, 1790000000, 4102444799])
def test_modtimestr_is_the_utc_time_to_the_second(seconds):
    import datetime

    from xrdclient.compat.client import _convert

    when = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    assert _convert._utc(seconds) == when.strftime("%Y-%m-%d %H:%M:%S")


# -- File -------------------------------------------------------------------


def test_a_file_that_is_not_open_raises_like_the_bindings():
    f = client.File()
    assert f.is_open() is False
    for call in (f.read, f.stat, f.sync, f.visa, f.readline, f.readlines, lambda: list(f)):
        with pytest.raises(ValueError, match="closed file"):
            call()
    status, _ = f.close()
    assert (status.ok, status.code) == (True, 4)


def test_read_is_offset_then_size_and_zero_means_the_rest(fh):
    assert ok(fh.read()) == TEXT
    assert ok(fh.read(3, 4)) == b"bbbb"
    assert ok(fh.read(3)) == TEXT[3:]
    assert ok(fh.read(100, 5)) == b""


def test_readline_keeps_a_cursor_that_an_offset_moves_and_leaves(fh):
    assert fh.readline() == "aa\n"
    assert fh.readline() == "bbbb\n"
    assert fh.readline(3) == "bbbb\n"  # moves the cursor to 3, leaves it there
    assert fh.readline() == "bbbb\n"
    assert fh.readline() == "cccccc\n"


def test_readline_size_caps_each_line(fh):
    assert [fh.readline(0, 2) for _ in range(5)] == ["aa", "\n", "bb", "bb", "\n"]


def test_readline_reads_in_small_chunks_when_told(fh):
    assert [fh.readline(0, 0, 2) for _ in range(3)] == ["aa\n", "bbbb\n", "cccccc\n"]


def test_readlines_iteration_and_next_share_the_cursor(fh):
    assert fh.readline() == "aa\n"
    assert fh.readlines() == ["bbbb\n", "cccccc\n", "dd\n"]
    assert list(fh) == []
    assert fh.readlines(3) == ["bbbb\n", "cccccc\n", "dd\n"]  # the bindings hang here


def test_iterating_a_fresh_file_gives_its_lines(root):
    with client.File() as f:
        ok(f.open(root + "/d/l.txt"))
        assert list(f) == ["aa\n", "bbbb\n", "cccccc\n", "dd\n"]
    assert not f.is_open()


def test_next_is_the_same_as_the_iterator(fh):
    assert (next(fh), fh.next()) == ("aa\n", "bbbb\n")


def test_readchunks_does_not_move_the_cursor(fh):
    assert list(fh.readchunks(1, 5)) == [b"a\nbbb", b"b\nccc", b"ccc\nd", b"d\n"]
    assert fh.readline() == "aa\n"


def test_a_line_that_is_not_utf8_raises_as_in_the_bindings(root, srv):
    srv.files["/bin"] = bytearray(b"\xff\xfe\n")
    f = client.File()
    ok(f.open(root + "/bin"))
    with pytest.raises(UnicodeDecodeError):
        f.readline()
    f.close()


def test_vector_read_is_a_vectorreadinfo(fh):
    info = ok(fh.vector_read([(0, 2), (5, 3)]))
    assert info.size == 5
    assert [(c.offset, c.length, c.buffer) for c in info] == [(0, 2, b"aa"), (5, 3, b"bb\n")]


def test_stat_on_a_file(fh):
    assert ok(fh.stat()).size == len(TEXT)
    assert ok(fh.stat(True)).size == len(TEXT)
    assert ok(fh.stat(True, timeout=10)).size == len(TEXT)


def test_writing_a_new_file(root, srv):
    f = client.File()
    ok(f.open(root + "/n.txt", OpenFlags.NEW | OpenFlags.UPDATE, AccessMode.UR | AccessMode.UW))
    ok(f.write("hello\n"))
    ok(f.write(b"ABCDEF", 6, 3, timeout=10))
    ok(f.sync())
    ok(f.truncate(8))
    assert ok(f.read()) == b"hello\nAB"
    ok(f.close())
    assert bytes(srv.files["/n.txt"]) == b"hello\nAB"


def test_opening_an_open_file_is_an_invalid_operation(fh, root):
    status, _ = fh.open(root + "/d/l.txt")
    assert (status.code, status.message) == (3, "[ERROR] Invalid operation")


def test_a_file_whose_open_failed_keeps_answering_with_that_failure(root):
    f = client.File()
    first, _ = f.open(root + "/nope")
    assert first.errno == 3011
    assert f.open(root + "/d/l.txt")[0] == first
    assert f.close()[0] == first
    assert f.is_open() is False


def test_open_with_a_callback(root):
    f = client.File()
    handler = client.utils.AsyncResponseHandler()
    assert f.open(root + "/d/l.txt", callback=handler).ok
    assert handler.wait()[0].ok and f.is_open()
    assert list(handler.hostlist)
    f.close()


def test_a_file_whose_open_failed_through_a_callback_is_finished_too(root):
    """As with the bindings: the callback's failure is what later opens and closes answer."""
    f = client.File()
    handler = client.utils.AsyncResponseHandler()
    assert f.open(root + "/nope", callback=handler).ok
    failed = handler.wait()[0]
    assert failed.errno == 3011 and not f.is_open()
    assert f.open(root + "/d/l.txt")[0] == failed
    assert f.close()[0] == failed


def _slow_opens(monkeypatch):
    """Native opens that wait for ``gate``; returns it, and the files they closed."""
    from xrdclient.compat.client import file as compat_file

    gate, done, closed = threading.Event(), threading.Event(), []
    real_open, real_close = compat_file.NativeFile.open, compat_file.NativeFile.close

    def slow_open(self, *args, **kwargs):
        gate.wait(30)
        try:
            return real_open(self, *args, **kwargs)
        finally:
            done.set()

    def close(self):
        closed.append(self)
        return real_close(self)

    monkeypatch.setattr(compat_file.NativeFile, "open", slow_open)
    monkeypatch.setattr(compat_file.NativeFile, "close", close)
    return gate, done, closed


def test_an_open_that_times_out_stays_failed_when_it_later_succeeds(root, monkeypatch):
    """The late handle is closed, not installed: no reads through it, and no leak."""
    gate, done, closed = _slow_opens(monkeypatch)
    f = client.File()
    expired, _ = f.open(root + "/d/l.txt", timeout=1)
    assert expired.code == 206
    gate.set()
    assert done.wait(30)
    for _ in range(300):  # the worker closes the late handle just after opening it
        if closed:
            break
        time.sleep(0.01)
    assert len(closed) == 1 and not f.is_open()
    assert f.open(root + "/d/l.txt")[0] == expired == f.close()[0]
    with pytest.raises(ValueError, match="closed"):
        f.read()


def test_an_open_that_expires_leaves_the_file_failed(root, srv):
    """The open expires where it stands: no handle, and the failure sticks."""
    from xrdclient.proto import constants as c
    from xrdclient.testing.server import _HANDLERS

    def slow_open(conn, sid, params, body):
        time.sleep(2.5)
        yield from _HANDLERS[c.kXR_open](conn, sid, params, body)

    srv.handlers[c.kXR_open] = slow_open
    f = client.File()
    assert f.open(root + "/d/l.txt", timeout=1)[0].code == 206
    assert not f.is_open() and f.native is None
    assert f.open(root + "/d/l.txt")[0].code == 206


def test_a_callback_on_an_unopened_file_has_no_hosts():
    from xrdclient.compat.client.file import File

    assert list(File()._File__hosts([])) == []  # type: ignore[attr-defined]


def test_fcntl_is_the_servers_to_answer(fh, srv):
    status, response = fh.fcntl(b"x")
    assert (status.code, status.errno, response) == (400, 3013, None)
    assert "fctl operation not supported" in status.message
    srv.fctl = lambda path, data: path.encode() + b":" + data
    assert ok(fh.fcntl(b"q")) == b"/d/l.txt:q"
    assert ok(fh.fcntl("text")) == b"/d/l.txt:text"
    assert ok(fh.fcntl(b"q", timeout=5)) == b"/d/l.txt:q"
    with pytest.raises(ValueError):
        client.File().fcntl(b"x")


def test_samefs_and_dup_open_next_to_the_template(fh, root, srv):
    writable = OpenFlags.NEW | OpenFlags.UPDATE
    new = client.File()
    ok(new.openusingtemplate(fh, root + "/d/next", writable | OpenFlags.SAMEFS))
    ok(new.write(b"xy"))
    ok(new.close())
    assert srv.colocated["/d/next"] == "/d/l.txt"
    dup = client.File()
    ok(dup.openusingtemplate(fh, root + "/d/dup", writable | OpenFlags.DUP, 0o644))
    assert ok(dup.read()) == TEXT
    assert dup.get_property("DataServer") == fh.get_property("DataServer")
    ok(dup.close())


def test_a_template_open_without_dup_or_samefs_is_an_open(root):
    """XrdCl does not look at the template unless the flags need it."""
    plain = client.File()
    ok(plain.openusingtemplate(client.File(), root + "/d/l.txt", OpenFlags.READ))
    assert ok(plain.read()) == TEXT
    ok(plain.close())


def test_a_template_must_be_open_and_the_file_not(fh, root):
    colocated = OpenFlags.NEW | OpenFlags.SAMEFS
    status, response = client.File().openusingtemplate(client.File(), root + "/d/x", colocated)
    assert (status.code, status.errno, response) == (3, 0, None)
    assert "Template file not open" in status.message
    status, _ = fh.openusingtemplate(fh, root + "/d/x", colocated)
    assert (status.code, status.message) == (3, "[ERROR] Invalid operation")
    with pytest.raises(AttributeError):
        client.File().openusingtemplate("not a file", root + "/d/x", OpenFlags.NEW)
    with pytest.raises(OverflowError):
        client.File().openusingtemplate(fh, root + "/d/x", 1 << 32)


def test_a_refused_template_open_is_a_failed_open(fh, root):
    new = client.File()
    status, _ = new.openusingtemplate(fh, root + "/d/r", OpenFlags.NEW | OpenFlags.DUP)
    assert (status.code, status.errno) == (400, 3000)
    assert new.openusingtemplate(fh, root + "/d/r", OpenFlags.NEW | OpenFlags.SAMEFS)[0] == status
    assert not new.is_open()


def test_a_template_open_can_answer_a_callback(fh, root, srv):
    done = threading.Event()
    got = []
    new = client.File()
    status = new.openusingtemplate(
        fh, root + "/d/cb", OpenFlags.NEW | OpenFlags.SAMEFS,
        callback=lambda st, resp, hosts: (got.append((st, resp)), done.set()),
    )
    assert status.ok and done.wait(10)
    assert got[0][0].ok and got[0][1] is None
    assert "/d/cb" in srv.colocated
    ok(new.close())


def test_file_xattrs(fh):
    assert ok(fh.set_xattr([("user.k", "v")]))[0][1]["ok"]
    got = ok(fh.get_xattr(["user.k", "user.none"]))
    assert got[0][:2] == ("user.k", "v")
    assert got[1][2]["errno"] == 3027
    assert [n for n, _, _ in ok(fh.list_xattr())] == ["user.k"]
    assert ok(fh.del_xattr(["user.k"]))[0][1]["ok"]


def test_visa_and_properties(fh, srv):
    assert isinstance(ok(fh.visa()), bytes)
    assert fh.get_property("DataServer") == srv.url.netloc
    assert fh.get_property("LastURL").endswith("/d/l.txt")
    assert fh.get_property("ReadRecovery") == "true"
    assert fh.set_property("ReadRecovery", "false") and fh.get_property("ReadRecovery") == "false"
    assert fh.set_property("Nope", "1") is False
    assert repr(fh).startswith("<XRootD.client.File root://")


def test_properties_of_a_file_that_is_not_open():
    f = client.File()
    assert f.get_property("DataServer") is None and f.get_property("LastURL") is None
    assert repr(f) == "<XRootD.client.File not open>"


def test_clone_copies_ranges_inside_the_server(root, srv):
    src = client.File()
    ok(src.open(root + "/d/l.txt"))
    dst = client.File()
    ok(dst.open(root + "/c.txt", OpenFlags.NEW | OpenFlags.UPDATE))
    locs = [
        {"src_file": src, "src_offset": 0, "src_length": 3, "dest_offset": 0},
        {"src_file": src, "src_offset": 3, "src_length": 5, "dest_offset": 3},
    ]
    ok(dst.clone(locs))
    assert ok(dst.read()) == TEXT[:8]
    with pytest.raises(ValueError, match="not open"):
        dst.clone([{"src_file": client.File(), "src_offset": 0, "src_length": 1, "dest_offset": 0}])
    dst.close()
    src.close()


# -- CopyProcess ------------------------------------------------------------


class Recorder(client.utils.CopyProgressHandler):
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    def begin(self, jobId, total, source, target):
        self.events.append(("begin", jobId, total, str(source), str(target)))

    def update(self, jobId, processed, total):
        self.events.append(("update", jobId, processed, total))

    def end(self, jobId, results):
        self.events.append(("end", jobId, results["status"].ok))


def test_a_copy_process_runs_its_jobs_and_reports_each(root, tmp_path):
    process = client.CopyProcess()
    process.add_job(root + "/d/l.txt", str(tmp_path / "a.txt"))
    process.add_job(root + "/nope", str(tmp_path / "b.txt"))
    assert process.prepare().ok
    handler = Recorder()
    status, results = process.run(handler)
    assert status.errno == 3011
    assert results[0] == {"size": len(TEXT), "status": _status.OK}
    assert set(results[1]) == {"status"} and results[1]["status"].errno == 3011
    assert (tmp_path / "a.txt").read_bytes() == TEXT
    kinds = [event[0] for event in handler.events]
    assert kinds[0] == "begin" and "update" in kinds and kinds.count("end") == 2
    assert handler.events[0][1:3] == (1, 2)


def test_a_copy_process_in_parallel_with_checksums(root, tmp_path):
    process = client.CopyProcess()
    process.parallel(2)
    for n in range(3):
        process.add_job(root + "/d/l.txt", str(tmp_path / f"{n}.txt"), checksummode="end2end")
    status, results = process.run()
    assert status.ok
    assert all(r["sourceCheckSum"].startswith("adler32:") for r in results)


def test_a_target_directory_is_not_filled_with_the_sources_name(root, tmp_path, srv):
    """``CopyProcess`` copies to the path it is given - naming the file is ``xrdcp``'s job."""
    process = client.CopyProcess()
    process.add_job(root + "/d/l.txt", str(tmp_path))
    process.add_job(str(tmp_path / "up"), root + "/d/sub/")
    (tmp_path / "up").write_bytes(b"u")
    _, results = process.run()
    assert (results[0]["status"].code, results[0]["status"].errno) == (402, 3018)
    assert not (tmp_path / "l.txt").exists()
    assert results[1]["status"].code == 400 and "/d/sub/up" not in srv.files


def test_a_copy_process_makes_the_parent_when_asked(root, tmp_path, srv):
    process = client.CopyProcess()
    process.add_job(root + "/d/l.txt", str(tmp_path / "x" / "y.txt"), mkdir=True)
    process.add_job(str(tmp_path / "x" / "y.txt"), root + "/up/load.txt", mkdir=True)
    assert process.run()[0].ok
    assert bytes(srv.files["/up/load.txt"]) == TEXT


def test_a_checksum_preset_that_disagrees_fails_the_job(root, tmp_path):
    process = client.CopyProcess()
    process.add_job(
        root + "/d/l.txt", str(tmp_path / "a"), checksummode="end2end", checksumpreset="00000000"
    )
    status, results = process.run()
    assert status.code == 305 and results[0]["size"] == len(TEXT)


def test_a_bad_checksum_can_remove_the_target(root, tmp_path, monkeypatch):
    from xrdclient import errors
    from xrdclient.compat.client import copyprocess

    def mismatched(*args, **kwargs):
        (tmp_path / "bad").write_bytes(b"x")
        raise errors.ChecksumMismatchError("adler32", "a", "b")

    monkeypatch.setattr(copyprocess, "_copy_file", mismatched)
    process = client.CopyProcess()
    process.add_job(root + "/d/l.txt", str(tmp_path / "bad"), rmBadCksum=True)
    status, _ = process.run()
    assert status.code == 305 and not (tmp_path / "bad").exists()


def test_removing_a_bad_remote_target(root, srv, monkeypatch):
    from xrdclient import errors
    from xrdclient.compat.client import copyprocess

    def mismatched(*args, **kwargs):
        raise errors.ChecksumMismatchError("adler32", "a", "b")

    monkeypatch.setattr(copyprocess, "_copy_file", mismatched)
    process = client.CopyProcess()
    process.add_job(root + "/d/sub/g", root + "/d/l.txt", rmBadCksum=True)
    process.add_job(root + "/d/sub/g", root + "/d/missing", rmBadCksum=True)
    process.run()
    assert "/d/l.txt" not in srv.files


def test_a_transient_failure_is_retried(root, tmp_path, monkeypatch):
    from xrdclient import errors
    from xrdclient.compat.client import copyprocess

    real, calls = copyprocess._copy_file, []

    def flaky(*args, **kwargs):
        calls.append(kwargs["resume"])
        if len(calls) < 3:
            raise errors.TransientError("dropped")
        return real(*args, **kwargs)

    monkeypatch.setattr(copyprocess, "_copy_file", flaky)
    process = client.CopyProcess()
    process.add_job(root + "/d/l.txt", str(tmp_path / "r"), retry=2, rtrplc="continue")
    assert process.run()[0].ok
    assert calls == [False, True, True]
    process = client.CopyProcess()
    calls.clear()
    process.add_job(root + "/d/l.txt", str(tmp_path / "s"), retry=1)
    assert process.run()[0].code == 108


def test_a_force_retry_overwrites_what_the_failure_left(root, tmp_path, monkeypatch):
    """XrdCl's default CpRetryPolicy: retry with force, so a partial target is no obstacle."""
    from xrdclient import errors
    from xrdclient.compat.client import copyprocess

    real, calls = copyprocess._copy_file, []

    def flaky(source, target, **kwargs):
        calls.append((kwargs["overwrite"], kwargs["resume"]))
        if len(calls) == 1:
            with open(target, "wb") as partial:
                partial.write(b"part")  # what an interrupted download leaves
            raise errors.TransientError("connection reset mid-transfer")
        return real(source, target, **kwargs)

    monkeypatch.setattr(copyprocess, "_copy_file", flaky)
    process = client.CopyProcess()
    process.add_job(root + "/d/l.txt", str(tmp_path / "x"), retry=2)
    status, _ = process.run()
    assert status.ok, status.message
    assert calls == [(False, False), (True, False)]
    assert (tmp_path / "x").read_bytes() == TEXT


def test_a_server_refusal_names_the_end_it_came_from(root, tmp_path):
    """``... No such file or directory (source)``, as XrdCl words it.

    Checked against XrdCl 5.9.7 on EOS: a missing source ends ``(source)``
    and a refused destination ``(destination)``, before the newline.
    """
    process = client.CopyProcess()
    process.add_job(root + "/d/absent.txt", str(tmp_path / "out"))
    _, results = process.run()
    message = results[0]["status"].message
    assert message.startswith("[ERROR] Server responded with an error: [3011] ")
    assert message.endswith(" (source)\n")


def test_a_target_that_exists_is_a_local_error_as_in_xrdcl(root, tmp_path):
    (tmp_path / "there").write_bytes(b"x")
    (tmp_path / "dir").mkdir()
    process = client.CopyProcess()
    process.add_job(root + "/d/l.txt", str(tmp_path / "there"))
    process.add_job(root + "/d/l.txt", str(tmp_path / "dir"), force=True)
    process.add_job(str(tmp_path / "missing"), str(tmp_path / "t"))
    _, results = process.run()
    codes = [(r["status"].code, r["status"].errno) for r in results]
    assert codes == [(402, 3018), (402, 3016), (402, 3011)]
    assert results[0]["status"].message == "[ERROR] Local error: file exists:  (destination)"
    assert results[2]["status"].message.endswith(":  (source)")


def test_a_local_error_without_an_errno_the_protocol_names(root, tmp_path, monkeypatch):
    import errno as errnos

    from xrdclient.compat.client import copyprocess

    def refuse(*args, **kwargs):
        raise OSError(errnos.EBUSY, "Resource busy")

    monkeypatch.setattr(copyprocess, "_copy_file", refuse)
    process = client.CopyProcess()
    process.add_job(root + "/d/l.txt", str(tmp_path / "busy"))
    status = process.run()[0]
    assert (status.code, status.errno) == (402, errnos.EBUSY)


def test_a_missing_source_leaves_no_target(root, tmp_path):
    process = client.CopyProcess()
    process.add_job(root + "/nope", str(tmp_path / "b.bin"))
    process.add_job(root + "/nope", str(tmp_path / "kept"), force=True)
    (tmp_path / "kept").write_bytes(b"precious")
    _, results = process.run()
    assert [r["status"].errno for r in results] == [3011, 3011]
    assert not (tmp_path / "b.bin").exists()
    assert (tmp_path / "kept").read_bytes() == b"precious"


def _no_checksums(srv):
    """The server a stock xrootd is without a checksum plugin: kXR_Qcksum is refused."""
    from xrdclient.proto import constants as c
    from xrdclient.testing import error
    from xrdclient.testing import server as fake

    def query(conn, sid, params, body):
        if int.from_bytes(params[:2], "big") == c.kXR_Qcksum:
            yield error(sid, 3013, "query chksum is not supported")
            return
        yield from fake._h_query(conn, sid, params, body)

    srv.handlers[c.kXR_query] = query


@pytest.mark.parametrize(
    ("mode", "preset", "keys", "code"),
    [
        ("end", "", set(), 0),  # not a mode XrdCl knows: nothing is checked
        ("end", "0000abcd", {"sourceCheckSum"}, 0),  # a preset alone compares with nothing
        ("target", "", {"targetCheckSum"}, 0),  # the local target, digested here
        ("target", "0000abcd", {"sourceCheckSum", "targetCheckSum"}, 305),
        ("source", "", set(), 400),  # the server cannot say
        ("end2end", "", set(), 400),
    ],
)
def test_checksum_modes_ask_what_xrdcl_asks(root, srv, tmp_path, mode, preset, keys, code):
    _no_checksums(srv)
    process = client.CopyProcess()
    process.add_job(
        root + "/d/l.txt",
        str(tmp_path / "c"),
        checksummode=mode,
        checksumtype="adler32",
        checksumpreset=preset,
    )
    _, (result,) = process.run()
    assert result["status"].code == code
    assert set(result) == {"status", "size"} | keys and result["size"] == len(TEXT)
    if preset:
        assert result["sourceCheckSum"] == "adler32:abcd"  # leading zeros dropped, as XrdCl
    if code == 400:
        assert result["status"].errno == 3013


def test_a_checksum_other_than_adler32_or_crc32_keeps_its_leading_zeros(root, tmp_path):
    process = client.CopyProcess()
    process.add_job(
        root + "/d/l.txt",
        str(tmp_path / "md5"),
        checksummode="end",
        checksumtype="md5",
        checksumpreset="00ff",
    )
    assert process.run()[1][0]["sourceCheckSum"] == "md5:00ff"


def test_a_bad_checksum_found_after_the_copy_can_remove_the_target(root, tmp_path):
    process = client.CopyProcess()
    process.add_job(
        root + "/d/l.txt",
        str(tmp_path / "bad"),
        checksummode="end2end",
        checksumpreset="1",
        rmBadCksum=True,
    )
    assert process.run()[0].message == "[ERROR] CheckSum error"
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("mkdir", [True, False])
def test_a_local_target_ending_in_a_slash_is_the_file_itself(root, tmp_path, mkdir):
    """XrdCl writes ``sub/dir/`` as the file ``dir``, and makes a local target's parents."""
    process = client.CopyProcess()
    process.add_job(root + "/d/l.txt", str(tmp_path / "sub" / "dir") + "/", mkdir=mkdir)
    assert process.run()[0].ok
    assert (tmp_path / "sub" / "dir").read_bytes() == TEXT


def test_a_handler_can_cancel_a_job(root, tmp_path):
    class Stop(client.utils.CopyProgressHandler):
        def should_cancel(self, jobId):
            return True

    process = client.CopyProcess()
    process.add_job(root + "/d/l.txt", str(tmp_path / "c"))
    assert process.run(Stop())[0].code == 207


def test_third_party_only_and_first(root, tmp_path, monkeypatch):
    from xrdclient.compat.client import copyprocess

    process = client.CopyProcess()
    process.add_job(root + "/d/l.txt", str(tmp_path / "t"), thirdparty="only")
    assert process.run()[0].code == 9  # a local target cannot pull

    def refuse(*args, **kwargs):
        raise ValueError("not these two")

    monkeypatch.setattr(copyprocess, "_third_party", refuse)
    process = client.CopyProcess()
    process.add_job(root + "/d/l.txt", str(tmp_path / "f"), thirdparty="first", parallelchunks=2)
    assert process.run()[0].ok and (tmp_path / "f").read_bytes() == TEXT


def test_third_party_between_servers(root, srv, monkeypatch):
    from types import SimpleNamespace

    from xrdclient.compat.client import copyprocess

    monkeypatch.setattr(
        copyprocess, "_third_party", lambda *a, **k: SimpleNamespace(size=7, checksum=None)
    )
    process = client.CopyProcess()
    process.add_job(root + "/d/l.txt", root + "/d/copy", thirdparty="only")
    assert process.run()[1] == [{"size": 7, "status": _status.OK}]


@pytest.mark.parametrize(
    ("source", "target", "extra"),
    [("notaurl://", "/tmp/x", {}), ("/tmp/a", "/tmp/b", {"thirdparty": "sometimes"})],
)
def test_prepare_refuses_a_job_it_cannot_run(source, target, extra):
    process = client.CopyProcess()
    process.add_job(source, target, **extra)
    assert process.prepare().code == 9


def test_a_job_that_cannot_run_is_a_status(root, tmp_path, monkeypatch):
    from xrdclient.compat.client import copyprocess

    monkeypatch.setattr(copyprocess, "_copy_file", lambda *a, **k: 1 / 0)
    process = client.CopyProcess()
    process.add_job(root + "/d/l.txt", str(tmp_path / "z"))
    assert process.run()[0].code == 2
    monkeypatch.setattr(copyprocess, "_copy_file", lambda *a, **k: int("x"))
    assert process.run()[0].code == 9


def test_a_handler_needs_none_of_the_methods(root, tmp_path):
    process = client.CopyProcess()
    process.add_job(root + "/d/l.txt", str(tmp_path / "h"))
    assert process.run(object())[0].ok


def test_files_on_one_server_share_a_channel_and_others_do_not(root, tmp_path):
    first = _channels.router_for(root + "/d/l.txt", CONFIG)
    second = _channels.router_for(root + "/d/sub/g", CONFIG)
    assert first is not None and first._lender is second._lender
    assert _channels.router_for(str(tmp_path / "local"), CONFIG) is None


def test_the_base_progress_handler_does_nothing():
    handler = client.utils.CopyProgressHandler()
    url = client.URL("root://h//x")
    assert handler.begin(1, 1, url, url) is None
    assert handler.update(1, 0, 0) is None and handler.end(1, {}) is None
    assert handler.should_cancel(1) is False


# -- env, logging, glob, xattr ---------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.setattr(env, "_ints", {})
    monkeypatch.setattr(env, "_strings", {})
    monkeypatch.setattr(env, "_request_timeout", 0)
    for key in ("XRD_REQUESTTIMEOUT", "XRD_POLLERPREFERENCE", "XRD_NETWORKSTACK"):
        monkeypatch.delenv(key, raising=False)


def test_an_int_setting_can_be_put_read_and_deleted(clean_env):
    assert client.EnvGetInt("RequestTimeout") == 1800
    assert client.EnvGetDefault("requesttimeout") == "1800"  # a string, as XrdCl gives it
    assert client.EnvPutInt("RequestTimeout", 60) is True
    assert client.EnvGetInt("RequestTimeout") == 60
    assert client.EnvDelInt("RequestTimeout") is True
    assert client.EnvGetInt("RequestTimeout") == 1800


def test_a_string_setting_can_be_put_read_and_deleted(clean_env):
    assert client.EnvGetString("NetworkStack") == "IPAuto"
    assert client.EnvPutString("NetworkStack", "IPv4") is True
    assert client.EnvGetString("NetworkStack") == "IPv4"
    assert client.EnvDelString("NetworkStack") is True
    assert client.EnvGetString("NetworkStack") == "IPAuto"


def test_a_key_xrdcl_does_not_register_has_only_a_default(clean_env, monkeypatch):
    """``PollerPreference`` is in XrdCl's table of defaults but never registered."""
    monkeypatch.setenv("XRD_POLLERPREFERENCE", "libevent")  # not imported: not registered
    assert client.EnvGetString("PollerPreference") is None
    assert client.EnvGetDefault("PollerPreference") == "built-in"
    assert client.EnvPutString("PollerPreference", "x") is True
    assert client.EnvGetString("PollerPreference") == "x"
    assert client.EnvGetInt("CpRetry") == 0 and client.EnvGetDefault("CpRetry") is None
    assert client.EnvGetInt("TCPKeepProbes") == 9 and client.EnvGetDefault("TCPKeepProbes") is None
    assert client.EnvGetDefault("TCPKeepAliveProbes") == "9"


def test_a_setting_xrdcl_does_not_have_reads_as_none(clean_env):
    assert client.EnvGetInt("NoSuchKey") is None and client.EnvGetString("NoSuchKey") is None
    assert client.EnvGetString("RequestTimeout") is None  # an int, not a string


def test_the_shell_wins_over_a_put(clean_env, monkeypatch):
    monkeypatch.setenv("XRD_REQUESTTIMEOUT", "7")
    monkeypatch.setenv("XRD_NETWORKSTACK", "IPv4")
    assert client.EnvPutInt("RequestTimeout", 60) is False
    assert client.EnvPutString("NetworkStack", "IPv6") is False
    assert client.EnvDelInt("RequestTimeout") is False
    assert client.EnvDelString("NetworkStack") is False
    assert client.EnvGetInt("RequestTimeout") == 7
    assert client.EnvGetString("NetworkStack") == "IPv4"
    monkeypatch.setenv("XRD_REQUESTTIMEOUT", "soon")  # not a number: XrdCl keeps the default
    assert client.EnvGetInt("RequestTimeout") == 1800


def test_puts_reach_the_native_configuration(clean_env, monkeypatch):
    monkeypatch.undo()  # the real ``env.config``, not the test fixture's
    monkeypatch.setattr(env, "_ints", {})
    monkeypatch.setattr(env, "_strings", {})
    monkeypatch.delenv("XRD_REQUESTTIMEOUT", raising=False)
    monkeypatch.setattr(env, "_request_timeout", 0)
    client.EnvPutInt("RequestTimeout", 42)
    client.EnvPutInt("StreamTimeout", 9)
    client.EnvPutInt("SubStreamsPerChannel", 3)
    client.EnvPutInt("DataServerTTL", 30)
    client.EnvPutString("ReadRecovery", "false")
    client.EnvPutString("PollerPreference", "built-in")  # no native field: ignored
    client.EnvPutInt("TimeoutResolution", 1)  # no native equivalent: ignored
    cfg = env.config()
    assert (cfg.stall_deadline, cfg.request_timeout, cfg.data_streams) == (42.0, 9.0, 2)
    assert (cfg.pool_idle_ttl, cfg.recover_handles) == (30.0, False)
    assert env.request_timeout() == 42


def test_set_log_level(monkeypatch):
    import logging

    logger = logging.getLogger("xrdclient")
    monkeypatch.setattr(logger, "level", logger.level)
    client.SetLogLevel("Debug")
    assert logger.level == logging.DEBUG
    with pytest.raises(ValueError, match="unknown log level"):
        client.SetLogLevel("Chatty")
    assert client.SetLogMask("Debug", "All") is None


def test_glob_expands_on_the_server(root):
    base = root + "/d/"
    assert sorted(client.glob(base + "*")) == [base + "empty", base + "l.txt", base + "sub"]
    assert client.glob(base + "s*/*/") == [base + "sub/deeper/"]
    assert list(client.iglob(base + "l.*?authz=x")) == [base + "l.txt?authz=x"]


def test_glob_prefers_a_local_match(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("x")
    assert sorted(client.glob(str(tmp_path / "*.txt"))) == [
        str(tmp_path / "a.txt"),
        str(tmp_path / "b.txt"),
    ]


def test_glob_of_an_unlistable_directory(root):
    with pytest.raises(RuntimeError, match="for path"):
        client.glob(root + "/nope/*")
    assert client.glob(root + "/nope/*", raise_error=False) == []


def test_set_xattr_adler32_writes_xrdcks_record(tmp_path, monkeypatch):
    import struct

    from xrdclient.compat.client import xattr

    written = {}
    monkeypatch.setattr(xattr, "_setxattr", lambda path, name, value: written.update({name: value}))
    target = tmp_path / "f"
    target.write_bytes(b"hello")
    os.utime(target, (1_000_000, 1_000_000))
    monkeypatch.setattr(time, "time", lambda: 1_000_005.0)
    client.setXAttrAdler32(str(target), "062c0215")
    record = written["XrdCks.adler32"]
    assert len(record) == 96
    name, mtime, delta, _, _, length, value = struct.unpack(">16sqihbb64s", record)
    assert (name.rstrip(b"\0"), mtime, delta, length) == (b"adler32", 1_000_000, 5, 4)
    assert value[:4] == bytes.fromhex("062c0215")
    with pytest.raises(ValueError, match="eight hex digits"):
        client.setXAttrAdler32(str(target), "0102")


def test_set_xattr_adler32_on_this_platform(tmp_path):
    target = tmp_path / "f"
    target.write_bytes(b"hello")
    try:
        client.setXAttrAdler32(str(target), "062c0215")
    except OSError as exc:  # a filesystem without xattrs, e.g. some tmpfs
        pytest.skip(f"no extended attributes here: {exc}")


def test_set_xattr_adler32_reports_a_failure(tmp_path):
    with pytest.raises(OSError):
        client.setXAttrAdler32(str(tmp_path / "missing"), "062c0215")


def test_setting_an_xattr_the_system_refuses_raises_its_errno(tmp_path):
    """Whichever of the two system calls this platform has, its failure is an OSError."""
    import errno

    from xrdclient.compat.client import xattr

    with pytest.raises(OSError) as caught:
        xattr._setxattr(str(tmp_path / "missing"), "XrdCks.adler32", b"v")
    assert caught.value.errno == errno.ENOENT


def test_linux_keeps_the_attribute_in_the_user_namespace(monkeypatch):
    from xrdclient.compat.client import xattr

    calls = []
    monkeypatch.setattr(os, "setxattr", lambda *args: calls.append(args), raising=False)
    xattr._setxattr("/f", "XrdCks.adler32", b"v")
    assert calls == [("/f", "user.XrdCks.adler32", b"v")]


def test_macos_hands_setxattr_the_value_and_reads_back_its_errno(monkeypatch):
    """The ``ctypes`` call, and what it makes of a failure, on any platform."""
    import ctypes
    import errno

    from xrdclient.compat.client import xattr

    calls = []

    class LibC:
        def setxattr(self, *args):
            calls.append(args)
            return -1

    monkeypatch.delattr(os, "setxattr", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: LibC())
    monkeypatch.setattr(ctypes, "get_errno", lambda: errno.EPERM)
    with pytest.raises(PermissionError):
        xattr._setxattr("/f", "XrdCks.adler32", b"v")
    assert calls == [(b"/f", b"XrdCks.adler32", b"v", 1, 0, 0)]


# -- lifecycle --------------------------------------------------------------


def test_a_file_that_never_opened_leaves_its_with_block_quietly():
    with client.File() as f:
        assert not f.is_open()


def test_a_file_dropped_while_open_is_closed(root):
    f = client.File()
    ok(f.open(root + "/d/l.txt"))
    native = f.native
    del f
    gc.collect()
    assert native is not None and not native.is_open


def test_a_filesystem_whose_url_is_refused_has_nothing_to_let_go(monkeypatch):
    from xrdclient.compat.client import filesystem

    def refuse(*args, **kwargs):
        raise ValueError("no such endpoint")

    monkeypatch.setattr(filesystem, "NativeFileSystem", refuse)
    with pytest.raises(ValueError, match="no such endpoint"):
        client.FileSystem("root://h/")
    gc.collect()  # the half-built object's ``__del__`` has no connection to close


def test_a_url_with_a_bad_port_is_refused_on_use_not_on_construction():
    status, _ = client.FileSystem("root://h:notaport/").ping()
    assert status.code == 13


def test_a_bad_checksum_without_rmbadcksum_keeps_the_target(root, tmp_path, monkeypatch):
    from xrdclient import errors
    from xrdclient.compat.client import copyprocess

    def mismatched(*args, **kwargs):
        (tmp_path / "bad").write_bytes(b"x")
        raise errors.ChecksumMismatchError("adler32", "a", "b")

    monkeypatch.setattr(copyprocess, "_copy_file", mismatched)
    process = client.CopyProcess()
    process.add_job(root + "/d/l.txt", str(tmp_path / "bad"))
    status, _ = process.run()
    assert status.code == 305 and (tmp_path / "bad").exists()


# -- install ----------------------------------------------------------------


def _forget_xrootd(monkeypatch) -> None:
    """Take every ``XRootD`` module out of :data:`sys.modules`, for this test."""
    for name in [n for n in sys.modules if n.split(".")[0] == "XRootD"]:
        monkeypatch.delitem(sys.modules, name)


def test_install_answers_for_the_xrootd_name(monkeypatch):
    from xrdclient import compat

    _forget_xrootd(monkeypatch)
    compat.install()
    from XRootD import client as installed
    from XRootD.client.flags import OpenFlags as Installed

    assert installed is client and Installed is OpenFlags
    compat.install()  # a second time is harmless
    _forget_xrootd(monkeypatch)


def test_install_refuses_when_the_real_bindings_are_loaded(monkeypatch):
    import types

    from xrdclient import compat

    monkeypatch.setitem(sys.modules, "XRootD", types.ModuleType("XRootD"))
    with pytest.raises(RuntimeError, match="already imported"):
        compat.install()


# -- parity with the official bindings --------------------------------------


def _norm(value: Any) -> Any:
    """A comparable form: responses to dicts, volatile fields dropped."""
    volatile = {"id", "message", "mtime", "modtime", "modtimestr", "ctime", "atime"}
    if isinstance(value, (tuple, list)):
        return type(value)(_norm(v) for v in value)
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items() if k not in volatile}
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return (type(value).__name__, _norm(vars(value)))
    if isinstance(value, str):
        # Each client works in its own sandbox; the names differ by design.
        return value.replace("/theirs", "/SIDE").replace("/ours", "/SIDE")
    return value


def _outcome(step, target) -> Any:
    try:
        return _norm(step(target))
    except Exception as exc:
        return ("raises", type(exc).__name__)


@pytest.fixture
def official():
    return pytest.importorskip("XRootD.client", reason="the official bindings are not installed")


@pytest.fixture
def two_sandboxes(real_server, sandbox, monkeypatch):
    """One directory for each client, so their changes cannot meet."""
    from conftest import _REAL_CONFIG

    monkeypatch.setattr(env, "config", lambda: _REAL_CONFIG)
    fs = client.FileSystem(real_server.url)
    for side in ("theirs", "ours"):
        ok(fs.mkdir(f"{sandbox}/{side}/sub/deeper", MkDirFlags.MAKEPATH))
        f = client.File()
        ok(f.open(f"{real_server.url}/{sandbox}/{side}/l.txt", OpenFlags.NEW))
        ok(f.write(TEXT))
        ok(f.close())
    return real_server.url, sandbox


FS_STEPS = [
    ("stat", lambda fs, d: fs.stat(f"{d}/l.txt")),
    ("stat missing", lambda fs, d: fs.stat(f"{d}/nope")),
    ("stat dir", lambda fs, d: fs.stat(d)),
    ("statvfs", lambda fs, d: fs.statvfs(d)),
    ("ping", lambda fs, d: fs.ping()),
    ("protocol", lambda fs, d: fs.protocol()),
    ("dirlist", lambda fs, d: fs.dirlist(d)),
    ("dirlist stat", lambda fs, d: fs.dirlist(d, DirListFlags.STAT)),
    ("dirlist recursive", lambda fs, d: fs.dirlist(d, DirListFlags.RECURSIVE)),
    ("dirlist missing", lambda fs, d: fs.dirlist(f"{d}/nope")),
    ("locate", lambda fs, d: fs.locate(f"{d}/l.txt", OpenFlags.NONE)),
    ("query checksum", lambda fs, d: fs.query(QueryCode.CHECKSUM, f"{d}/l.txt")),
    ("mkdir", lambda fs, d: fs.mkdir(f"{d}/new")),
    ("mkdir no parent", lambda fs, d: fs.mkdir(f"{d}/q/r")),
    ("mkdir makepath", lambda fs, d: fs.mkdir(f"{d}/x/y", MkDirFlags.MAKEPATH)),
    ("rmdir not empty", lambda fs, d: fs.rmdir(d)),
    ("rmdir", lambda fs, d: fs.rmdir(f"{d}/new")),
    ("rm missing", lambda fs, d: fs.rm(f"{d}/missing")),
    ("truncate", lambda fs, d: fs.truncate(f"{d}/l.txt", 3)),
    ("chmod", lambda fs, d: fs.chmod(f"{d}/l.txt", AccessMode.UR | AccessMode.UW)),
    ("stat after", lambda fs, d: fs.stat(f"{d}/l.txt")),
    ("set_xattr", lambda fs, d: fs.set_xattr(f"{d}/l.txt", [("user.a", "1")])),
    ("get_xattr", lambda fs, d: fs.get_xattr(f"{d}/l.txt", ["user.a", "user.zz"])),
    ("list_xattr", lambda fs, d: fs.list_xattr(f"{d}/l.txt")),
    ("del_xattr", lambda fs, d: fs.del_xattr(f"{d}/l.txt", ["user.a", "user.zz"])),
    ("sendinfo", lambda fs, d: fs.sendinfo("hello")),
    ("mv", lambda fs, d: fs.mv(f"{d}/l.txt", f"{d}/m.txt")),
    ("mv missing", lambda fs, d: fs.mv(f"{d}/l.txt", f"{d}/n.txt")),
    ("properties", lambda fs, d: (fs.get_property("FollowRedirects"), fs.set_property("X", "1"))),
]


@pytest.mark.interop
@pytest.mark.parity
@pytest.mark.parametrize(("label", "step"), FS_STEPS, ids=[s[0] for s in FS_STEPS])
def test_filesystem_answers_as_the_bindings_do(official, two_sandboxes, label, step):
    url, sandbox = two_sandboxes
    theirs_fs, ours_fs = official.FileSystem(url), client.FileSystem(url)
    assert _outcome(lambda d: step(ours_fs, d), f"{sandbox}/ours") == _outcome(
        lambda d: step(theirs_fs, d), f"{sandbox}/theirs"
    )


FILE_STEPS = [
    ("close unopened", lambda f, u: f.close()),
    ("read unopened", lambda f, u: f.read()),
    ("open", lambda f, u: f.open(f"{u}/l.txt")),
    ("open twice", lambda f, u: f.open(f"{u}/l.txt")),
    ("read all", lambda f, u: f.read()),
    ("read range", lambda f, u: f.read(2, 3)),
    ("read past", lambda f, u: f.read(100, 3)),
    ("readline", lambda f, u: f.readline()),
    ("readline offset", lambda f, u: f.readline(3)),
    ("readline again", lambda f, u: f.readline()),
    ("readline size", lambda f, u: f.readline(0, 2)),
    ("readlines", lambda f, u: f.readlines()),
    ("iterate", lambda f, u: list(f)),
    ("readchunks", lambda f, u: list(f.readchunks(1, 5))),
    ("vector_read", lambda f, u: f.vector_read([(0, 2), (5, 3)])),
    ("stat", lambda f, u: f.stat(True)),
    ("close", lambda f, u: f.close()),
    ("close again", lambda f, u: f.close()),
    ("open new", lambda f, u: f.open(f"{u}/n.txt", OpenFlags.NEW | OpenFlags.UPDATE)),
    ("write", lambda f, u: f.write("hello\n")),
    ("write sized", lambda f, u: f.write(b"ABCDEF", 6, 3)),
    ("sync", lambda f, u: f.sync()),
    ("truncate", lambda f, u: f.truncate(8)),
    ("read back", lambda f, u: f.read()),
    ("set_xattr", lambda f, u: f.set_xattr([("user.k", "v")])),
    ("get_xattr", lambda f, u: f.get_xattr(["user.k", "user.none"])),
    ("close new", lambda f, u: f.close()),
    ("open existing as new", lambda f, u: f.open(f"{u}/n.txt", OpenFlags.NEW)),
]


@pytest.mark.interop
@pytest.mark.parity
def test_a_file_session_goes_as_it_does_with_the_bindings(official, two_sandboxes):
    url, sandbox = two_sandboxes
    theirs_f, ours_f = official.File(), client.File()
    for label, step in FILE_STEPS:
        mine = _outcome(functools.partial(step, ours_f), f"{url}/{sandbox}/ours")
        want = _outcome(functools.partial(step, theirs_f), f"{url}/{sandbox}/theirs")
        assert mine == want, label


@pytest.mark.interop
@pytest.mark.parity
@pytest.mark.parametrize(
    "text",
    ["root://u:p@h:1234//p?a=1", "root://h/rel", "/local/f", "garbage", "notaurl://", "file:///x"],
)
def test_a_url_parses_as_the_bindings_parse_it(official, text):
    names = "hostid protocol username password hostname port path path_with_params".split()
    mine, want = client.URL(text), official.URL(text)
    assert [getattr(mine, n) for n in names] == [getattr(want, n) for n in names]
    assert (str(mine), mine.is_valid()) == (str(want), want.is_valid())


@pytest.mark.interop
@pytest.mark.parity
def test_a_copy_process_reports_as_the_bindings_do(official, two_sandboxes, tmp_path):
    url, sandbox = two_sandboxes
    outcomes = []
    for side, module in (("theirs", official), ("ours", client)):
        process = module.CopyProcess()
        process.add_job(f"{url}/{sandbox}/{side}/l.txt", str(tmp_path / f"{side}.txt"))
        process.add_job(f"{url}/{sandbox}/{side}/nope", str(tmp_path / f"{side}-2.txt"))
        assert process.prepare().ok
        outcomes.append(_norm(process.run()))
    assert outcomes[0] == outcomes[1]


@pytest.mark.interop
@pytest.mark.parity
def test_copy_process_edge_cases_end_as_with_the_bindings(official, two_sandboxes, tmp_path):
    """Existing targets, missing sources, checksum modes and a target ending in ``/``."""
    url, sandbox = two_sandboxes
    outcomes = []
    for side, module in (("theirs", official), ("ours", client)):
        local = tmp_path / side
        local.mkdir()
        (local / "kept").write_bytes(b"precious")
        source = f"{url}/{sandbox}/{side}/l.txt"
        process = module.CopyProcess()
        process.add_job(source, str(local / "a"))
        process.add_job(source, str(local / "a"))  # exists, no force
        process.add_job(f"{url}/{sandbox}/{side}/nope", str(local / "b"))
        process.add_job(f"{url}/{sandbox}/{side}/nope", str(local / "kept"), force=True)
        process.add_job(source, str(local / "sub" / "dir") + "/", mkdir=True)
        process.add_job(source, str(local / "no" / "parent"))
        for n, mode in enumerate(("end", "source", "target", "end2end", "none")):
            process.add_job(source, str(local / f"m{n}"), checksummode=mode, checksumtype="adler32")
        process.add_job(
            source, str(local / "p"), checksummode="target", checksumtype="adler32",
            checksumpreset="0badcafe",
        )
        assert process.prepare().ok
        status, results = process.run()
        files = sorted(
            (str(path.relative_to(local)), path.read_bytes())
            for path in local.rglob("*") if path.is_file()
        )
        outcomes.append((_norm((status, results)), files))
    assert outcomes[0] == outcomes[1]


# -- argument checks, as the bindings' C parser makes them -------------------


@pytest.mark.parametrize(
    ("call", "error"),
    [
        (lambda f: f.read(0, [1, 2]), TypeError),
        (lambda f: f.read(True, 1), TypeError),
        (lambda f: f.read(0, -10), OverflowError),
        (lambda f: f.read(-1, 1), OverflowError),
        (lambda f: f.read(0, 10**11), OverflowError),
        (lambda f: f.read(0, 10, 2**65), OverflowError),
        (lambda f: f.readline(0, 1, -1), OverflowError),
        (lambda f: f.readlines(-1), OverflowError),
        (lambda f: f.readchunks(0, 10**11), OverflowError),
        (lambda f: f.write(123), TypeError),
        (lambda f: f.write("x", 0, 10**11), OverflowError),
        (lambda f: f.vector_read(100), TypeError),
        (lambda f: f.vector_read("ab"), TypeError),
        (lambda f: f.vector_read([1, 2]), TypeError),
        (lambda f: f.vector_read([("a", "b")]), TypeError),
        (lambda f: f.vector_read([(-1, 1)]), OverflowError),
        (lambda f: f.truncate(-1), OverflowError),
    ],
)
def test_a_bad_argument_raises_as_the_bindings_c_parser_does(fh, call, error):
    with pytest.raises(error):
        call(fh)


def test_open_checks_its_flags_and_mode(root):
    with pytest.raises(OverflowError):
        client.File().open(root + "/d/l.txt", 1 << 16)
    with pytest.raises(TypeError):
        client.File().open(root + "/d/l.txt", 0, "rw")


@pytest.mark.parametrize(
    "call",
    [
        lambda fs: fs.truncate("/d/l.txt", -1),
        lambda fs: fs.chmod("/d/l.txt", 1 << 16),
        lambda fs: fs.mkdir("/x", 0, -1),
        lambda fs: fs.dirlist("/d", -1),
        lambda fs: fs.locate("/d", 1 << 16),
        lambda fs: fs.prepare(["/d"], 1 << 16),
        lambda fs: fs.stat("/d", timeout=-1),
    ],
)
def test_a_filesystem_checks_its_numbers_too(fs, call):
    with pytest.raises(OverflowError):
        call(fs)


def test_creating_a_file_makes_its_parents_as_xrdcl_does(root, srv):
    """XrdCl's ``kXR_async`` makes xrootd create the path; ``MAKEPATH`` asks outright."""
    for flags in (OpenFlags.NEW, OpenFlags.DELETE):
        f = client.File()
        ok(f.open(f"{root}/deep/{flags}/f.txt", flags))
        ok(f.close())
        assert f"/deep/{flags}" in srv.dirs


def test_a_filesystem_on_an_invalid_url_refuses_everything():
    fs = client.FileSystem("://")
    status, response = fs.stat("/tmp/x")
    assert (status.code, status.message, response) == (13, "[ERROR] Operation not supported", None)


def test_a_bare_host_is_a_root_url_as_in_xrdcl(root):
    fs = client.FileSystem(root[len("root://") :].rstrip("/"))
    assert ok(fs.stat("/d/l.txt")).size == len(TEXT)


def test_the_glob_helpers_are_public_as_in_the_bindings():
    from xrdclient.compat.client import glob_funcs

    assert glob_funcs.extract_url_params("root://s//p/f?.txt?k=v") == ("root://s//p/f?.txt", "?k=v")
    assert glob_funcs.extract_url_params("/p/file?.txt") == ("/p/file?.txt", "")
    assert glob_funcs.split_url("root://h:1//a/b") == ("root://h:1/", "//a/b")


def test_a_local_glob_that_finds_nothing_says_so_with_an_error(tmp_path):
    with pytest.raises(RuntimeError, match=r"\[ERROR\]") as excinfo:
        client.glob(str(tmp_path / "not-there"), raise_error=True)
    assert str(tmp_path) in str(excinfo.value)


@pytest.mark.parametrize(
    ("text", "port"),
    [
        ("https://h/x", 443),
        ("davs://h/x", 443),
        ("http://h/x", 80),
        ("dav://h/x", 80),
        ("HTTPS://h/x", 1094),
        ("root://h//x", 1094),
        ("https://h:8443/x", 8443),
    ],
)
def test_an_http_url_gets_https_port_as_in_xrdcl(text, port):
    """XrdCl's default port follows the scheme - looked up as spelled."""
    assert client.URL(text).port == port


def test_cp_parallel_chunks_is_chunks_in_flight_as_in_xrdcl(clean_env, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(env, "_ints", {})
    monkeypatch.setattr(env, "_strings", {})
    client.EnvPutInt("CPParallelChunks", 7)
    assert env.config().in_flight == 7


def test_a_callback_after_the_pool_has_shut_down_still_arrives(fs, monkeypatch):
    """At interpreter exit the pool refuses work; the answer comes anyway."""
    from xrdclient.compat.client import _dispatch

    def refuse(*args, **kwargs):
        raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(_dispatch._POOL, "submit", refuse)
    handler = client.utils.AsyncResponseHandler()
    assert fs.ping(callback=handler).ok
    assert handler.wait()[0].ok


# -- parity: fcntl, template opens, ZIP listings ------------------------------


@pytest.mark.interop
@pytest.mark.parity
def test_a_zip_listing_is_the_bindings_listing(official, two_sandboxes):
    """The same archives through both clients: every field, times and ids included."""
    import pathlib

    url, sandbox = two_sandboxes
    here = pathlib.Path(sandbox)
    (here / "a.zip").write_bytes(
        _zip_bytes({"hello.txt": b"hello world", "dir/": b"", "dir/inner.bin": os.urandom(999)})
    )
    (here / "empty.zip").write_bytes(b"")
    (here / "not.zip").write_bytes(b"abc")
    steps = [
        lambda fs: fs.dirlist(f"{sandbox}/a.zip", DirListFlags.ZIP),
        lambda fs: fs.dirlist(f"{sandbox}/a.zip"),
        lambda fs: fs.dirlist(f"{sandbox}/a.zip", DirListFlags.ZIP | DirListFlags.STAT),
        lambda fs: fs.dirlist(f"{sandbox}/a.zip?x=1", DirListFlags.ZIP | DirListFlags.RECURSIVE),
        lambda fs: fs.dirlist(f"{sandbox}/empty.zip", DirListFlags.ZIP),
        lambda fs: fs.dirlist(f"{sandbox}/not.zip", DirListFlags.ZIP),
        lambda fs: fs.dirlist(f"{sandbox}/nope.zip", DirListFlags.ZIP),
        lambda fs: fs.dirlist(f"{sandbox}/ours", DirListFlags.ZIP | DirListFlags.STAT),
        lambda fs: fs.dirlist(f"{sandbox}/ours", DirListFlags.ZIP | DirListFlags.RECURSIVE),
    ]
    theirs_fs, ours_fs = official.FileSystem(url), client.FileSystem(url)
    for n, step in enumerate(steps):
        mine, want = step(ours_fs), step(theirs_fs)
        assert _exactly(mine) == _exactly(want), n


def _exactly(outcome: Any) -> Any:
    """Everything, the status's message and each entry's times included."""
    status, response = outcome
    fields = (status.status, status.code, status.errno, status.message.rstrip("\n"))
    if response is None:
        return fields, None
    entries = [
        (e.hostaddr, e.name, e.statinfo and dict(vars(e.statinfo))) for e in response.dirlist
    ]
    return fields, (response.parent, response.size, entries)


@pytest.mark.interop
@pytest.mark.parity
def test_fcntl_is_answered_as_the_bindings_are(official, two_sandboxes):
    url, sandbox = two_sandboxes
    answers = []
    for module in (official, client):
        f = module.File()
        ok(f.open(f"{url}/{sandbox}/ours/l.txt"))
        statuses = [vars(f.fcntl(arg)[0]) for arg in (b"x", b"", b"\x00\xff")]
        answers.append([*statuses, f.fcntl(b"x")[1]])
        ok(f.close())
    assert answers[0] == answers[1]


TEMPLATE_STEPS = [
    ("samefs", OpenFlags.NEW | OpenFlags.UPDATE | OpenFlags.SAMEFS),
    ("samefs read", OpenFlags.NEW | OpenFlags.SAMEFS),
    ("dup", OpenFlags.NEW | OpenFlags.UPDATE | OpenFlags.DUP),
    ("dup read-only", OpenFlags.NEW | OpenFlags.DUP),
    ("both", OpenFlags.NEW | OpenFlags.UPDATE | OpenFlags.DUP | OpenFlags.SAMEFS),
    ("not new", OpenFlags.UPDATE | OpenFlags.SAMEFS),
    ("neither", OpenFlags.NEW | OpenFlags.UPDATE),
]


@pytest.mark.interop
@pytest.mark.parity
def test_template_opens_go_as_with_the_bindings(official, two_sandboxes):
    url, sandbox = two_sandboxes
    outcomes = {}
    for side, module in (("theirs", official), ("ours", client)):
        where = f"{url}/{sandbox}/{side}"
        template = module.File()
        ok(template.open(f"{where}/l.txt"))
        seen = [_template_step(template, module.File(), where, *step) for step in TEMPLATE_STEPS]
        samefs = TEMPLATE_STEPS[0][1]
        seen.append(_norm(module.File().openusingtemplate(module.File(), f"{where}/u", samefs)))
        seen.append(_norm(template.openusingtemplate(template, f"{where}/v", samefs)))
        ok(template.close())
        outcomes[side] = seen
    assert outcomes["ours"] == outcomes["theirs"]


def _template_step(template: Any, f: Any, where: str, label: str, flags: int) -> Any:
    """One template open, then what writing to and closing the new file do."""
    opened = _outcome(lambda u: f.openusingtemplate(template, f"{u}/{label}", flags), where)
    is_open = f.is_open()
    wrote = _outcome(lambda u: f.write(b"xy"), where) if is_open else None
    same = f.get_property("DataServer") == template.get_property("DataServer")
    return label, opened, wrote, same if is_open else None, _norm(f.close())


def test_prepare_sends_its_entries_as_given(fs, srv):
    """A cancel's request id, or a full URL, goes to the server untouched."""
    ok(fs.prepare(["/d/l.txt"], PrepareFlags.STAGE))
    status, _ = fs.prepare(["req-123", "/d/l.txt"], PrepareFlags.CANCEL)
    assert status.ok
    assert srv.cancelled_prepares[-1].split("\n") == ["req-123", "/d/l.txt"]


def test_only_a_servers_answer_ends_in_a_newline():
    assert not _status.failure(_status.errOSError, "No such file").message.endswith("\n")
    answer = _status.status(_status.errErrorResponse, errno=3011, message="gone")
    assert answer.message.endswith("\n")


def test_prepare_over_http_goes_through_the_native_call(fs, monkeypatch):
    """With no ``root://`` router, staging is the native (Tape REST) call."""
    asked = []

    class Http:
        def prepare(self, paths, *, flags, priority):
            asked.append((paths, flags, priority))
            return "req-7"

    monkeypatch.setattr(fs, "native", Http())
    assert ok(fs.prepare(["/d/l.txt"], PrepareFlags.STAGE, 2)) == b"req-7"
    assert asked == [(["/d/l.txt"], PrepareFlags.STAGE, 2)]
