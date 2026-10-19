"""Branches that depend on the platform or on a failure nobody can arrange for real.

Each was once excluded from coverage with a pragma. They are reached here
instead, through the seam each one already has (a module attribute, a
callable passed in), so the same lines run on Linux and on macOS.
"""

from __future__ import annotations

import io
import os
import sys
import weakref
from types import SimpleNamespace

import pytest

import xrdclient
from xrdclient import doctor
from xrdclient.compat.client import xattr
from xrdclient.config import Config
from xrdclient.errors import AbortedError
from xrdclient.fsspec_impl import XRootDFileSystem
from xrdclient.http.client import HTTPClient
from xrdclient.session import pool
from xrdclient.testing import server
from xrdclient.transport import sync
from xrdclient.url import parse


def test_a_name_in_auth_order_with_no_mechanism_is_passed_over(monkeypatch):
    monkeypatch.setattr("xrdclient.auth.registry", dict)
    report = doctor.diagnose(config=Config(auth_order=("unix",)))
    lines = str(report).splitlines()
    assert not [line for line in lines if "auth:unix" in line]
    assert [line for line in lines if line.split()[1:2] == ["auth"]]


def test_extended_attributes_go_through_os_setxattr_where_there_is_one(monkeypatch, tmp_path):
    calls: list[tuple[str, str, bytes]] = []

    def setxattr(path: str, name: str, value: bytes) -> None:
        calls.append((path, name, value))

    monkeypatch.setattr(os, "setxattr", setxattr, raising=False)
    xattr._setxattr(str(tmp_path), "XrdCks.adler32", b"v")
    assert calls == [(str(tmp_path), "user.XrdCks.adler32", b"v")]


class _Libc:
    """A C library whose ``setxattr`` answers ``result``."""

    def __init__(self, result: int) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    def setxattr(self, *args: object) -> int:
        self.calls.append(args)
        return self.result


def _without_os_setxattr(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    if hasattr(os, "setxattr"):
        monkeypatch.delattr(os, "setxattr")
    monkeypatch.setattr(sys, "platform", platform)


@pytest.mark.parametrize("result", [0, -1])
def test_extended_attributes_on_macos_go_through_libc(monkeypatch, tmp_path, result):
    _without_os_setxattr(monkeypatch, "darwin")
    libc = _Libc(result)
    fake = SimpleNamespace(
        CDLL=lambda *args, **kwargs: libc,
        get_errno=lambda: 1,
        util=SimpleNamespace(find_library=lambda name: name),
    )
    monkeypatch.setattr(xattr, "ctypes", fake)
    if result == 0:
        xattr._setxattr(str(tmp_path), "XrdCks.adler32", b"v")
    else:
        with pytest.raises(OSError, match=str(tmp_path)):
            xattr._setxattr(str(tmp_path), "XrdCks.adler32", b"v")
    assert libc.calls == [(os.fsencode(str(tmp_path)), b"XrdCks.adler32", b"v", 1, 0, 0)]


def test_extended_attributes_elsewhere_are_refused(monkeypatch, tmp_path):
    _without_os_setxattr(monkeypatch, "sunos5")
    with pytest.raises(NotImplementedError, match="not supported on this platform"):
        xattr._setxattr(str(tmp_path), "XrdCks.adler32", b"v")


def test_the_iov_limit_has_a_floor_where_sysconf_cannot_say(monkeypatch):
    def sysconf(name: str) -> int:
        raise ValueError(name)

    monkeypatch.setattr(sync, "os", SimpleNamespace(sysconf=sysconf))
    assert sync._iov_max() == 16


def test_the_command_line_server_blocks_on_an_event(monkeypatch):
    waited: list[bool] = []

    class Event:
        def wait(self) -> None:
            waited.append(True)

    monkeypatch.setattr(server, "threading", SimpleNamespace(Event=Event))
    server._block()
    assert waited == [True]


def test_the_reaper_survives_a_failed_round_and_ends_with_its_pool(monkeypatch):
    class Pool(pool.SessionPool):
        def reap(self) -> int:
            raise RuntimeError("one bad round")

    held: list[pool.SessionPool] = [Pool()]
    ref = weakref.ref(held[0])
    rounds: list[float] = []

    class Event:
        def wait(self, interval: float) -> None:
            rounds.append(interval)
            held.clear()  # the pool goes; the next round finds nothing

    monkeypatch.setattr(pool, "threading", SimpleNamespace(Event=Event))
    pool._reap_forever(ref)
    assert rounds == [pool.REAP_INTERVAL]


def test_ranges_are_read_one_by_one_from_a_file_without_readv():
    handle = io.BytesIO(b"0123456789")
    items: list[tuple[int, int | None, int | None]] = [(0, 2, 4), (1, None, 3), (2, -2, None)]
    assert XRootDFileSystem._ranges_of(handle, items) == [(0, b"23"), (1, b"012"), (2, b"89")]


@pytest.mark.parametrize("url", ["root://h:1094//d", "https://h/d"])
def test_s3_keywords_are_refused_for_any_other_scheme(url):
    with pytest.raises(TypeError, match="only an s3:// filesystem"):
        xrdclient.FileSystem(url, region="eu-west-1")


def test_an_aborted_client_refuses_every_request_as_the_abort():
    """Whether a request fails as itself or as the abort hangs on the client's flag."""
    client = HTTPClient(Config())
    client.abort()
    with pytest.raises(AbortedError, match="aborted"):
        client.request("GET", parse("http://127.0.0.1:1/f"))
    client.close()
