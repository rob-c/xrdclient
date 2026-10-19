"""Calling a handle off from another thread: :meth:`File.abort`.

A server that stops answering leaves the thread asking it blocked until its
timeout - minutes, by default. ``abort`` is how another thread (a GUI's
cancel button, gfal2's ``cancel()``, a signal handler's helper) gets it back
at once, and how every later call on the handle is refused rather than sent.
"""

from __future__ import annotations

import errno
import pickle
import threading
import time
from dataclasses import replace

import pytest

from xrdclient import errors
from xrdclient.client import _handle
from xrdclient.client.file import File
from xrdclient.config import Config
from xrdclient.errors import AbortedError, TransientError
from xrdclient.flags import OpenFlags
from xrdclient.proto import constants as c
from xrdclient.session.router import Router
from xrdclient.testing import FakeServer

PAYLOAD = bytes(range(256)) * 64


def _silent(conn, sid, params, body):
    """A handler that never answers."""
    return iter(())


def _abort_soon(target, delay: float = 0.2) -> threading.Timer:
    timer = threading.Timer(delay, target.abort)
    timer.start()
    return timer


@pytest.fixture
def plain(config: Config) -> Config:
    """The event path: no bulk plane, no readahead."""
    return replace(config, bulk=False, readahead=0)


@pytest.mark.parametrize("bulk", [True, False], ids=["plane", "event"])
def test_an_abort_wakes_a_read_blocked_on_a_silent_server(server, config, bulk):
    cfg = replace(config, bulk=bulk, readahead=0, request_timeout=60)
    handle = File(server.url / "data/a.root", cfg)
    handle.open("r")
    server.handlers[c.kXR_read] = _silent
    timer = _abort_soon(handle)
    started = time.monotonic()
    with pytest.raises(AbortedError) as caught:
        handle.read(5, 0)
    assert time.monotonic() - started < 10
    assert caught.value.errno == errno.ECANCELED
    timer.join()
    # Every call after it is refused without being sent.
    reads = server.seen.count(c.kXR_read)
    with pytest.raises(AbortedError):
        handle.read(5, 0)
    with pytest.raises(AbortedError):
        handle.stat(refresh=True)
    assert server.seen.count(c.kXR_read) == reads
    handle.abort()  # idempotent
    handle.close()  # and the close is a quiet release
    handle.close()
    assert not handle.is_open


def test_an_aborted_reader_is_never_reopened(server, config):
    cfg = replace(config, bulk=False, readahead=0, recover_handles=True)
    handle = File(server.url / "data/a.root", cfg)
    handle.open("r")
    handle.abort()
    assert handle.recover_handles is False
    assert not handle.recoverable
    with pytest.raises(AbortedError):
        handle._reopen()
    assert server.seen.count(c.kXR_open) == 1


def test_an_abort_during_a_reopen_reaches_the_new_connection(server, config, monkeypatch):
    """The router a re-open makes after the abort came is aborted as it is made."""
    cfg = replace(config, bulk=False, readahead=0)
    handle = File(server.url / "data/a.root", cfg)
    handle.open("r")

    class Late(Router):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            handle._aborted = True  # the abort lands while the router is being made

    monkeypatch.setattr(_handle, "Router", Late)
    with pytest.raises(AbortedError):
        handle._reopen()
    assert handle._router.aborted
    handle.close()


def test_an_abort_before_the_open_refuses_it(server, plain):
    handle = File(server.url / "data/a.root", plain)
    handle.abort()
    with pytest.raises(AbortedError):
        handle.open("r")
    assert c.kXR_open not in server.seen


def test_an_aborted_writer_closes_without_raising_and_commits_nothing(server, config):
    cfg = replace(config, chunk_size=4096, bulk_depth=2)
    handle = File(server.url / "data/w.bin", cfg)
    handle.open(OpenFlags.NEW | OpenFlags.UPDATE)
    server.handlers[c.kXR_write] = _silent
    timer = _abort_soon(handle)
    with pytest.raises(AbortedError):
        handle.write(PAYLOAD, 0)
    timer.join()
    with pytest.raises(AbortedError):
        handle.write(b"x", 0)
    handle.close()  # a writer's close would raise for a lost connection; not after an abort
    assert c.kXR_close not in server.seen


def test_an_abort_wakes_a_request_out_on_a_redirect(config):
    """A filesystem's redirected request rides a pooled connection; the abort finds it."""
    with FakeServer(files={"/f": b"x"}) as data, FakeServer() as head:
        head.redirects[c.kXR_stat] = (*data.address, "")
        data.handlers[c.kXR_stat] = _silent
        router = Router(head.url, replace(config, request_timeout=60), sticky=False)
        from xrdclient.proto import requests as r

        timer = _abort_soon(router)
        with pytest.raises(AbortedError):
            router.execute(r.Stat("/f"), path="/f")
        timer.join()
        assert router.aborted
        with pytest.raises(AbortedError):
            _ = router.session
        router.close()


def test_an_abort_that_comes_while_dialling_drops_the_new_connection(server, config, monkeypatch):
    router = Router(server.url, config)
    dial = Router._dial

    def dialled(self, url):
        session = dial(self, url)
        self._aborted.set()  # the abort lands while the dial is under way
        return session

    monkeypatch.setattr(Router, "_dial", dialled)
    with pytest.raises(AbortedError):
        _ = router.session
    assert router._session is None


def test_the_aborted_error_is_an_oserror_of_its_own():
    exc = AbortedError()
    assert isinstance(exc, OSError) and isinstance(exc, errors.XRootDError)
    assert not isinstance(exc, TransientError)  # never retried
    assert exc.errno == errno.ECANCELED
    assert str(exc) == "the operation was aborted"
    again = pickle.loads(pickle.dumps(AbortedError("stopped")))
    assert type(again) is AbortedError and str(again) == "stopped"


# ---------------------------------------------------------------------------
# The kXR / errno tables
# ---------------------------------------------------------------------------


def test_every_protocol_code_has_xrdcls_errno():
    assert errors.errno_for(errors.kXR_NotFound) == errno.ENOENT
    assert errors.errno_for(errors.kXR_Cancelled) == errno.ECANCELED
    assert errors.errno_for(errors.kXR_ReqTimedOut) == errno.ETIMEDOUT
    assert errors.errno_for(9999) == errno.ENOMSG
    assert set(errors.KXR_ERRNO) == {
        v for k, v in vars(errors).items() if k.startswith("kXR_") and isinstance(v, int)
    }


@pytest.mark.parametrize(
    ("number", "code"),
    [
        (errno.ENOENT, errors.kXR_NotFound),
        (-errno.EACCES, errors.kXR_NotAuthorized),  # a POSIX -errno return
        (errno.EPERM, errors.kXR_NotAuthorized),
        (errno.EINVAL, errors.kXR_ArgInvalid),  # the first of three
        (errno.ENOTEMPTY, errors.kXR_ItExists),
        (errno.ENOBUFS, errors.kXR_NoMemory),
        (errno.ECANCELED, errors.kXR_Cancelled),
        (errno.ENOTDIR, errors.kXR_FSError),  # nothing of its own: mapError's default
    ],
)
def test_errno_to_protocol_code_is_mapError(number, code):
    assert errors.kxr_for(number) == code


def test_the_two_directions_agree_where_the_mapping_is_one_to_one():
    assert errors.kxr_for(errno.ENOTDIR, default=0) == 0
    for code in (errors.kXR_NotFound, errors.kXR_NoSpace, errors.kXR_fsReadOnly):
        assert errors.kxr_for(errors.errno_for(code)) == code
