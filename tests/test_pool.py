"""Connection reuse: who may share a login, and who may not.

The interesting half of pooling is not the caching, it is the refusals - a
connection handed to the wrong caller is an authentication bug, and one handed
back after the server has gone is a hang. Most of what is below is about those
two.
"""

from __future__ import annotations

import subprocess
import sys

import xrdclient
from xrdclient.config import Config
from xrdclient.proto import constants as c
from xrdclient.session import SESSIONS, Session, SessionPool
from xrdclient.session.pool import _identity, _key
from xrdclient.url import parse


def logins(server) -> int:
    return server.seen.count(c.kXR_login)


class FakeSession:
    """Enough of a :class:`~xrdclient.session.Session` to be pooled and closed."""

    def __init__(self, closed: bool = False, broken: bool = False) -> None:
        self.closed = closed
        #: Set on a connection that failed on the wire. A socket whose peer
        #: went away still has a descriptor, so the pool asks this as well as
        #: ``closed`` before keeping or handing out a connection.
        self.broken = broken
        self.endpoint = "example.org:1094"
        self.closes = 0

    def close(self) -> None:
        self.closes += 1
        self.closed = True


def pooled(*sessions: FakeSession) -> Session:
    """One of the stubs above, seen as the Session the pool thinks it holds."""
    return sessions[0]  # type: ignore[return-value]


# ----------------------------------------------------------------------
# Reuse


def test_a_second_filesystem_reuses_the_first_ones_login(server, config):
    with xrdclient.FileSystem(server.url, config) as fs:
        fs.stat("/data/a.root")
    assert len(SESSIONS) == 1

    with xrdclient.FileSystem(server.url, config) as fs:
        fs.stat("/data/a.root")
    assert logins(server) == 1
    assert len(SESSIONS) == 1


def test_two_open_at_once_get_a_connection_each(server, config):
    """Pooling reuses idle connections; it does not multiplex live ones.

    Two ``FileSystem`` objects held open at the same time are two callers, and
    the documented way to have two things in flight at once is to have two
    handles.
    """
    with xrdclient.FileSystem(server.url, config) as first:
        first.stat("/data/a.root")
        with xrdclient.FileSystem(server.url, config) as second:
            second.stat("/data/a.root")
            assert len(SESSIONS) == 0
    assert logins(server) == 2
    assert len(SESSIONS) == 2


def test_a_file_that_owns_its_connection_returns_it(server, config):
    with xrdclient.File(f"{server.url}/data/a.root", config) as handle:
        assert handle.read() == b"hello world"
    assert len(SESSIONS) == 1

    with xrdclient.File(f"{server.url}/data/a.root", config) as handle:
        assert handle.read() == b"hello world"
    assert logins(server) == 1


def test_a_file_opened_through_a_filesystem_borrows(server, config):
    """The file shares the filesystem's connection, so it must not pool it."""
    with xrdclient.FileSystem(server.url, config) as fs:
        with fs.open("/data/a.root") as handle:
            assert handle.read() == b"hello world"
        # Closing the file let go of a connection it did not own: had it been
        # pooled, the next line would be talking to somebody else's session.
        assert len(SESSIONS) == 0
        assert fs.stat("/data/a.root").st_size == 11
    assert logins(server) == 1
    assert len(SESSIONS) == 1


def test_an_open_that_fails_still_returns_its_connection(server, config):
    try:
        xrdclient.File(f"{server.url}/data/missing.root", config).open()
    except FileNotFoundError:
        pass
    assert len(SESSIONS) == 1


def test_a_redirect_leaves_the_manager_pooled(server, config):
    """The server that redirected is fine - it just has not got the file."""
    host, port = server.address
    server.redirects[c.kXR_open] = (host, port, "tok=1")
    with xrdclient.FileSystem(server.url, config) as fs, fs.open("/data/a.root") as handle:
        assert handle.read() == b"hello world"
    # Redirected back to the same address, so the pooled manager connection is
    # the one the second leg picked up: one login for the two hops.
    assert logins(server) == 1


# ----------------------------------------------------------------------
# Refusals


def test_a_different_credential_never_reuses_a_connection(server, config):
    with xrdclient.FileSystem(server.url, config) as fs:
        fs.stat("/data/a.root")
    with xrdclient.FileSystem(server.url, config.evolve(username="somebody-else")) as fs:
        fs.stat("/data/a.root")
    assert logins(server) == 2
    assert len(SESSIONS) == 2


def test_a_user_in_the_url_counts_as_a_different_credential(server, config):
    with xrdclient.FileSystem(server.url, config) as fs:
        fs.stat("/data/a.root")
    url = parse(str(server.url)).evolve(username="someone")
    with xrdclient.FileSystem(url, config) as fs:
        fs.stat("/data/a.root")
    assert logins(server) == 2


def test_pooling_can_be_turned_off(server, config):
    off = config.evolve(pool_size=0)
    with xrdclient.FileSystem(server.url, off) as fs:
        fs.stat("/data/a.root")
    assert len(SESSIONS) == 0
    with xrdclient.FileSystem(server.url, off) as fs:
        fs.stat("/data/a.root")
    assert logins(server) == 2


def test_a_connection_idle_too_long_is_not_reused(server, config):
    brief = config.evolve(pool_idle_ttl=0.0)
    with xrdclient.FileSystem(server.url, brief) as fs:
        fs.stat("/data/a.root")
    with xrdclient.FileSystem(server.url, brief) as fs:
        fs.stat("/data/a.root")
    assert logins(server) == 2


def test_a_connection_the_server_dropped_is_not_pooled(server, config):
    with xrdclient.FileSystem(server.url, config) as fs:
        fs.stat("/data/a.root")
        server.disconnect()
        fs.ping()  # reconnects, and the dead session is closed on the way
    assert len(SESSIONS) == 1
    assert logins(server) == 2


def test_a_connection_that_failed_under_a_handle_is_discarded(server, config):
    """A file recovers by dialling again, not by passing the wreck on."""
    with xrdclient.File(f"{server.url}/data/a.root", config) as handle:
        assert handle.read(4) == b"hell"
        server.disconnect()
        assert handle.read(4, offset=0) == b"hell"
        assert handle.recoveries == 1
        assert len(SESSIONS) == 0


# ----------------------------------------------------------------------
# The pool itself


def test_a_full_pool_refuses_what_it_cannot_hold():
    pool = SessionPool()
    url, config = parse("root://example.org/"), Config(pool_size=1)
    first, second = FakeSession(), FakeSession()
    assert pool.release(pooled(first), url, config) is True
    assert pool.release(pooled(second), url, config) is False
    assert len(pool) == 1
    # Refused, not closed: the caller is told so it can close it itself, which
    # is the only arrangement in which nothing leaks.
    assert second.closes == 0
    pool.clear()
    assert (first.closes, len(pool)) == (1, 0)


def test_a_child_lets_go_of_its_parents_connections_without_closing_them():
    """``forget`` is for the far side of a fork: drop them, do not end them.

    Closing would write a ``kXR_endsess`` down a socket the parent is still
    using, which is worse than the sharing it is meant to prevent.
    """
    pool = SessionPool()
    url, config = parse("root://example.org/"), Config()
    session = FakeSession()
    pool.release(pooled(session), url, config)
    pool.forget()
    assert len(pool) == 0
    assert session.closes == 0
    assert pool.acquire(url, config) is None


#: A whole program that logs in, forks, and has both sides read: the child
#: must find an empty pool and dial a connection of its own, and the parent's
#: must still be there and still work afterwards. Two logins is the proof;
#: without the fork hook there would be one, shared, and reading it from both
#: sides is exactly the corruption the hook exists to prevent.
FORK_PROBE = """
import os, sys
import xrdclient
from xrdclient.session import SESSIONS

url = sys.argv[1]
config = xrdclient.Config(username="tester", auth_order=("host",), require_tls=False)


def stat():
    with xrdclient.FileSystem(url, config) as fs:
        return fs.stat("/data/a.root").size


assert stat() == 11 and len(SESSIONS) == 1, "the parent kept its connection"

child = os.fork()
if child == 0:
    ok = len(SESSIONS) == 0 and stat() == 11
    os._exit(0 if ok else 1)

assert os.waitpid(child, 0)[1] == 0, "the child could not read on its own"
assert len(SESSIONS) == 1, "the parent's connection outlived the child"
assert stat() == 11, "and still answers, so nobody read over anybody"
"""


def test_a_forked_child_starts_with_an_empty_pool(server):
    """The fork hook, run for real, in a process of its own.

    The fork is done in a subprocess rather than here because the official
    XRootD bindings, which :mod:`tests.test_parity` imports where they are
    installed, deadlock in their own ``atexit`` handler if the process they
    are loaded into has forked. Nothing to do with this library, and no reason
    to leave the behaviour untested.
    """
    done = subprocess.run(
        [sys.executable, "-c", FORK_PROBE, str(server.url)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert logins(server) == 2  # the parent's, and the one the child dialled


def test_an_already_closed_connection_is_never_taken_back():
    pool = SessionPool()
    url, config = parse("root://example.org/"), Config()
    assert pool.release(pooled(FakeSession(closed=True)), url, config) is False
    assert len(pool) == 0


def test_a_pooled_connection_that_died_while_idle_is_skipped():
    pool = SessionPool()
    url, config = parse("root://example.org/"), Config()
    dead, alive = FakeSession(), FakeSession()
    pool.release(pooled(dead), url, config)
    pool.release(pooled(alive), url, config)
    dead.closed = True
    # Newest first, so the live one comes back and the corpse is left for the
    # next sweep rather than being handed out.
    assert pool.acquire(url, config) is pooled(alive)
    assert pool.acquire(url, config) is None
    assert len(pool) == 0


def test_stale_connections_are_closed_on_the_way_past():
    pool = SessionPool()
    url = parse("root://example.org/")
    old = FakeSession()
    pool.release(pooled(old), url, Config())
    assert pool.acquire(url, Config(pool_idle_ttl=0.0)) is None
    assert old.closes == 1
    assert len(pool) == 0


def test_a_stale_entry_is_swept_when_the_next_one_arrives():
    pool = SessionPool()
    url, brief = parse("root://example.org/"), Config(pool_idle_ttl=0.0)
    old, new = FakeSession(), FakeSession()
    pool.release(pooled(old), url, Config())
    assert pool.release(pooled(new), url, brief) is True
    assert (old.closes, len(pool)) == (1, 1)


def test_pooling_off_refuses_and_never_answers():
    pool = SessionPool()
    url, off = parse("root://example.org/"), Config(pool_size=0)
    session = FakeSession()
    assert pool.release(pooled(session), url, off) is False
    assert pool.acquire(url, off) is None
    assert session.closes == 0


def test_the_pool_says_how_much_it_is_holding():
    pool = SessionPool()
    assert repr(pool) == "SessionPool(0 idle)"
    pool.release(pooled(FakeSession()), parse("root://example.org/"), Config())
    assert repr(pool) == "SessionPool(1 idle)"


# ----------------------------------------------------------------------
# Keys


def test_tls_and_plain_connections_are_kept_apart():
    plain, secure = parse("root://example.org/"), parse("roots://example.org/")
    assert _key(plain, Config())[0] == "root"
    assert _key(secure, Config()) != _key(plain, Config())
    assert _key(plain, Config(require_tls=True)) == _key(secure, Config(require_tls=True))


def test_the_key_carries_no_credential_anybody_could_read():
    config = Config(username="alice", token="s3cr3t-bearer", proxy="/tmp/x509up_u1000")
    key = _key(parse("root://example.org/"), config)
    printed = repr(key) + repr(SessionPool())
    for secret in ("s3cr3t-bearer", "x509up_u1000", "alice"):
        assert secret not in printed


def test_every_credential_field_changes_the_identity():
    url, base = parse("root://example.org/"), Config()
    for field, value in (
        ("username", "other"),
        ("token", "t"),
        ("token_file", "/tmp/t"),
        ("keytab", "/tmp/kt"),
        ("proxy", "/tmp/p"),
        ("ca_path", "/etc/ca"),
        ("ca_file", "/etc/ca.pem"),
        ("auth_order", ("unix",)),
        ("verify_tls", False),
        ("require_tls", True),
    ):
        assert _identity(url, base.evolve(**{field: value})) != _identity(url, base), field


def test_where_the_question_is_asked_is_not_who_is_answering():
    """A prompter is a callback, not a credential.

    The answer it gives is remembered per endpoint and mechanism for the life
    of the process, so two configs differing only in where the terminal is are
    the same login - and keying on the callback's identity would just mean
    never reusing anything.
    """
    url, base = parse("root://example.org/"), Config()
    assert _identity(url, base.evolve(prompter=lambda ask: None)) == _identity(url, base)


def test_a_connection_that_failed_on_the_wire_is_not_kept():
    """A dead socket still has a descriptor; pooling one fails the next caller."""
    pool = SessionPool()
    config = Config(pool_size=4)
    url = parse("root://example.org//store/f")
    assert not pool.release(pooled(FakeSession(broken=True)), url, config)
    assert len(pool) == 0


def test_a_connection_that_broke_while_idle_is_skipped():
    """It got into the pool healthy and died there; the next caller must not get it."""
    pool = SessionPool()
    config = Config(pool_size=4)
    url = parse("root://example.org//store/f")
    session = FakeSession()
    assert pool.release(pooled(session), url, config)
    session.broken = True
    assert pool.acquire(url, config) is None
