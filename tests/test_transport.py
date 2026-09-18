"""The byte pipes underneath the protocol: sockets, TLS, and the memory pair.

Everything above this layer is written against :class:`~xrdclient.transport.base.
Transport`, so these two implementations are what decides whether a timeout
arrives as :class:`TimeoutError` or as something the caller has to guess at.
The socket tests use a real loopback listener - a mocked socket would test the
mock's idea of ``recv``, not the one the standard library has.
"""

from __future__ import annotations

import contextlib
import socket
import ssl
import threading
from collections import deque

import pytest

from _pki import make_certificate, name, pem, private_key_pem, throwaway_key
from xrdclient.config import Config
from xrdclient.errors import ConnectionError as XrdConnectionError
from xrdclient.errors import TimeoutError as XrdTimeoutError
from xrdclient.transport.memory import MemoryTransport, pipe
from xrdclient.transport.sync import SocketTransport

CONFIG = Config(connect_timeout=5.0, request_timeout=5.0)

# ----------------------------------------------------------------------
# A loopback server, one connection deep
# ----------------------------------------------------------------------


@contextlib.contextmanager
def listener(handle, *, context: ssl.SSLContext | None = None):
    """Serve one connection with ``handle``; yields ``(host, port)``."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)

    def serve():
        try:
            conn, _ = sock.accept()
        except OSError:  # the listener was closed before anyone connected
            return
        try:
            if context is not None:
                conn = context.wrap_socket(conn, server_side=True)
            handle(conn)
        except OSError:  # a handshake this test meant to fail, or a hangup
            pass
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield sock.getsockname()
    finally:
        sock.close()
        thread.join(timeout=5)


def echo(conn):
    """Send back whatever arrives, uppercased, then hang up."""
    data = conn.recv(65536)
    conn.sendall(data.upper())


def hang(conn):
    """Accept and say nothing until the client gives up."""
    conn.recv(65536)


def refused_port() -> int:
    """A port on loopback with nothing behind it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def server_tls(tmp_path_factory):
    """A TLS context holding a self-signed certificate for ``localhost``."""
    key = throwaway_key(3)
    subject = name(("2.5.4.3", "localhost"))
    certificate = make_certificate(subject, subject, key.public, key)
    path = tmp_path_factory.mktemp("tls") / "server.pem"
    path.write_bytes(pem("CERTIFICATE", certificate) + private_key_pem(key))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(path))
    return context


# ----------------------------------------------------------------------
# MemoryTransport
# ----------------------------------------------------------------------


def test_the_two_ends_of_a_pipe_are_wired_to_each_other():
    client, server = pipe()
    client.send(b"ping")
    assert server.receive() == b"ping"
    server.send(b"pong")
    assert client.receive() == b"pong"


def test_reading_less_than_arrived_leaves_the_rest_queued():
    client, server = pipe()
    client.send(b"abcdef")
    assert server.receive(2) == b"ab"
    assert server.receive(2) == b"cd"
    assert server.receive() == b"ef"


def test_a_read_with_nothing_queued_is_empty_rather_than_blocking():
    _client, server = pipe()
    assert server.receive() == b""


def test_an_empty_send_queues_nothing():
    client, server = pipe()
    client.send(b"")
    assert server.receive() == b""


def test_sending_down_a_closed_pipe_is_a_connection_error():
    client, _server = pipe()
    assert not client.closed
    client.close()
    assert client.closed
    with pytest.raises(XrdConnectionError, match="closed"):
        client.send(b"anything")


def test_feed_puts_bytes_in_as_if_the_peer_had_sent_them():
    client, _server = pipe()
    client.feed(b"unsolicited")
    assert client.receive() == b"unsolicited"


def test_sent_drains_what_this_end_has_written():
    client, _server = pipe()
    client.send(b"one")
    client.send(b"two")
    assert client.sent() == b"onetwo"
    assert client.sent() == b""


def test_a_memory_transport_pretends_to_upgrade():
    """Nothing to encrypt, but the flag is what the session machine watches."""
    client, _server = pipe()
    assert not client.tls_started
    client.start_tls("example.org", Config())
    assert client.tls_started


def test_a_memory_transport_shows_its_queues():
    client, server = pipe()
    client.send(b"queued")
    assert repr(server) == "MemoryTransport(rx=1, tx=0, closed=False)"
    client.close()
    assert "closed=True" in repr(client)


def test_a_hand_built_memory_transport_names_a_host():
    """The session machine logs one, so both ends have to carry it."""
    transport = MemoryTransport(deque(), deque())
    assert (transport.host, transport.port) == ("memory", 0)


# ----------------------------------------------------------------------
# SocketTransport: connecting
# ----------------------------------------------------------------------


def test_a_connection_carries_bytes_in_both_directions():
    with listener(echo) as (host, port):
        transport = SocketTransport.connect(host, port, CONFIG)
        try:
            assert (transport.host, transport.port) == (host, port)
            transport.send(b"hello")
            assert transport.receive() == b"HELLO"
        finally:
            transport.close()


def test_the_peer_hanging_up_reads_as_end_of_stream():
    with listener(lambda conn: conn.close()) as (host, port):
        transport = SocketTransport.connect(host, port, CONFIG)
        try:
            assert transport.receive() == b""
        finally:
            transport.close()


def test_connecting_where_nothing_listens_names_the_endpoint():
    port = refused_port()
    with pytest.raises(XrdConnectionError, match=f"cannot connect to 127.0.0.1:{port}"):
        SocketTransport.connect("127.0.0.1", port, CONFIG)


def test_a_connect_that_times_out_is_a_timeout_error(monkeypatch):
    def never(address, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(socket, "create_connection", never)
    with pytest.raises(XrdTimeoutError, match="connecting to storage:1094 timed out"):
        SocketTransport.connect("storage", 1094, CONFIG)


def test_connect_defaults_its_configuration():
    """``connect`` is called without one in a few places; it must not need one."""
    with listener(echo) as (host, port):
        transport = SocketTransport.connect(host, port)
        try:
            assert not transport.closed
        finally:
            transport.close()


def test_a_connection_is_set_up_for_small_latency_sensitive_frames():
    with listener(hang) as (host, port):
        transport = SocketTransport.connect(host, port, CONFIG)
        try:
            sock = transport._sock
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
            assert sock.gettimeout() == CONFIG.request_timeout
        finally:
            transport.close()


# ----------------------------------------------------------------------
# SocketTransport: failing
# ----------------------------------------------------------------------


def test_a_read_that_outlasts_the_timeout_says_so():
    with listener(hang) as (host, port):
        transport = SocketTransport.connect(host, port, Config(request_timeout=0.05))
        try:
            with pytest.raises(XrdTimeoutError, match=f"read from {host}:{port} timed out"):
                transport.receive()
        finally:
            transport.close()


def test_settimeout_moves_the_deadline():
    with listener(hang) as (host, port):
        transport = SocketTransport.connect(host, port, Config(request_timeout=30.0))
        try:
            transport.settimeout(0.05)
            with pytest.raises(XrdTimeoutError):
                transport.receive()
        finally:
            transport.close()


def test_a_send_that_outlasts_the_timeout_says_so():
    """A full send buffer is not reproducible; the mapping of the error is."""

    class Blocked:
        def sendall(self, data):
            raise TimeoutError("timed out")

    with pytest.raises(XrdTimeoutError, match="send to storage:1094 timed out"):
        SocketTransport(Blocked(), "storage", 1094).send(b"stuck")


def test_writing_to_a_closed_connection_is_a_connection_error():
    with listener(echo) as (host, port):
        transport = SocketTransport.connect(host, port, CONFIG)
        transport.close()
        assert transport.closed
        with pytest.raises(XrdConnectionError, match="send to"):
            transport.send(b"too late")


def test_reading_from_a_closed_connection_is_a_connection_error():
    with listener(echo) as (host, port):
        transport = SocketTransport.connect(host, port, CONFIG)
        transport.close()
        with pytest.raises(XrdConnectionError, match="read from"):
            transport.receive()


def test_an_empty_send_never_touches_the_socket():
    """Which is why it is allowed after the connection has gone."""
    with listener(echo) as (host, port):
        transport = SocketTransport.connect(host, port, CONFIG)
        transport.close()
        transport.send(b"")  # no exception


def test_closing_twice_is_allowed():
    with listener(echo) as (host, port):
        transport = SocketTransport.connect(host, port, CONFIG)
        transport.close()
        transport.close()
        assert transport.closed


# ----------------------------------------------------------------------
# SocketTransport: TLS
# ----------------------------------------------------------------------


def test_a_connection_upgrades_in_place(server_tls):
    with listener(echo, context=server_tls) as (host, port):
        transport = SocketTransport.connect(host, port, CONFIG)
        try:
            transport.start_tls("localhost", Config(verify_tls=False))
            assert repr(transport) == f"SocketTransport({host}:{port}, tls)"
            transport.send(b"secret")
            assert transport.receive() == b"SECRET"
        finally:
            transport.close()


def test_an_unverifiable_certificate_stops_the_upgrade(server_tls):
    """The self-signed certificate is exactly what verification is there for."""
    with listener(echo, context=server_tls) as (host, port):
        transport = SocketTransport.connect(host, port, CONFIG)
        try:
            with pytest.raises(XrdConnectionError, match="TLS handshake with localhost failed"):
                transport.start_tls("localhost", Config())
        finally:
            transport.close()


def test_upgrading_against_a_server_that_speaks_no_tls_fails_cleanly():
    with listener(lambda conn: conn.sendall(b"not a hello\r\n")) as (host, port):
        transport = SocketTransport.connect(host, port, CONFIG)
        try:
            with pytest.raises(XrdConnectionError, match="TLS handshake with localhost failed"):
                transport.start_tls("localhost", Config(verify_tls=False))
        finally:
            transport.close()


def test_a_plain_connection_says_so():
    with listener(hang) as (host, port):
        transport = SocketTransport.connect(host, port, CONFIG)
        try:
            assert repr(transport) == f"SocketTransport({host}:{port}, tcp)"
        finally:
            transport.close()


def test_a_socket_that_will_not_close_is_let_go_anyway():
    """Closing is best-effort: the caller is finished with it either way."""

    class Stubborn:
        def close(self):
            raise OSError("already gone")

    with listener(echo) as (host, port):
        transport = SocketTransport.connect(host, port, CONFIG)
        real, transport._sock = transport._sock, Stubborn()
        transport.close()
        real.close()
