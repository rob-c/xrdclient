"""Session-level request encoders: bring-up, keep-alive, teardown, properties, signing.

Part of :mod:`xrdclient.proto.requests`, which re-exports every class here;
import them from there. These are the requests about the *connection* rather
than any file or path: ``kXR_protocol``/``kXR_login``/``kXR_auth`` that
:class:`~xrdclient.proto.machine.SessionMachine` sends during bring-up,
``kXR_ping``, ``kXR_endsess`` and ``kXR_bind`` (an extra data path), the
``kXR_set`` client property, and ``kXR_sigver``, the signature frame the
machine prefixes to a signed request. Namespace, query and file-I/O encoders
live in the sibling ``_requests_*`` modules; shared layouts and helpers in
``_requests_base``.

Layouts follow nginx-xrootd ``wire_core_requests.h``; cross-checked against
go-hep ``xrootd/xrdproto/<op>`` and XRootD.jl ``Wire/requests.jl``. Every
``params`` method writes exactly 16 bytes (header offsets 4..19).

None of these is signed: the signing key does not exist until ``kXR_auth``
has finished, and ``kXR_sigver`` cannot sign itself. The hook contract is
documented on :class:`~xrdclient.proto.frames.Request`.
"""

from __future__ import annotations

import os

from . import constants as c
from ._requests_base import _ZERO_PARAMS
from .buffer import Writer
from .frames import Request


class Protocol(Request):
    """``kXR_protocol`` - capability negotiation.

    The first request on every connection, sent right behind the handshake.
    The reply carries the server's protocol version and flags and - because
    ``kXR_secreqs`` is set - its security requirements (which requests must
    be signed), which the machine needs before it can sign anything.
    """

    __slots__ = ("flags",)
    opcode = c.kXR_protocol

    def __init__(self, flags: int | None = None) -> None:
        """Announce ``flags``; the default asks for security info and offers TLS.

        The machine adds ``kXR_wantTLS`` when the URL demands encryption
        (``roots://``); ``kXR_ableTLS`` alone lets the server decide.
        """
        self.flags = c.kXR_secreqs | c.kXR_ableTLS if flags is None else flags

    def params(self, w: Writer) -> None:
        """``clientpv[4] flags[1] expect[1] reserved[10]``.

        ``expect = kXR_ExpLogin`` tells the server a login follows, so it
        answers with the TLS requirements that apply to login rather than to
        a ``kXR_bind``.
        """
        w.u32(c.kXR_PROTOCOLVERSION).u8(self.flags).u8(c.kXR_ExpLogin).zeros(10)

    def __repr__(self) -> str:
        """Flags in hex, as they are read against ``XProtocol.hh``."""
        return f"Protocol(flags=0x{self.flags:02x})"


class Login(Request):
    """``kXR_login``: identify the client and open a session.

    The reply carries the 16-byte session id (used later by ``kXR_bind`` and
    ``kXR_endsess``) and, when the server requires authentication, the
    security protocol list the ``kXR_auth`` rounds negotiate from.
    """

    __slots__ = ("capver", "pid", "token", "username")
    opcode = c.kXR_login

    def __init__(
        self, username: str, pid: int | None = None, capver: int | None = None, token: str = ""
    ) -> None:
        """Log in as ``username`` (truncated to the 8 bytes the field holds).

        ``capver`` defaults to protocol 5 with ``kXR_asyncap``, which tells
        the server this client handles unsolicited ``kXR_attn`` messages.
        ``token`` is the login CGI (``xrd.cc=..&xrd.tz=..`` in XrdCl), sent
        as the body.
        """
        self.username = username
        self.pid = os.getpid() if pid is None else pid
        self.capver = c.kXR_ver005 | c.kXR_asyncap if capver is None else capver
        self.token = token

    def params(self, w: Writer) -> None:
        """``pid[4] username[8] reserved[1] ability[1] capver[1] role[1]``.

        ``ability`` and the last byte stay 0: no optional server behaviour is
        requested, and 0 in the old ``role`` slot is the ordinary user role.
        """
        w.i32(self.pid).padded(self.username, 8).zeros(2).u8(self.capver).u8(0)

    def payload(self) -> bytes:
        """The login token, unterminated; usually empty."""
        return self.token.encode("utf-8")

    def __repr__(self) -> str:
        """User and pid - the two things a server log shows for the session."""
        return f"Login(username={self.username!r}, pid={self.pid})"


class Auth(Request):
    """``kXR_auth`` - one round of a credential exchange.

    Multi-round protocols (GSI) keep state on the server between rounds, so
    a replayed round would desynchronise the handshake: not idempotent. The
    security plug-ins in :mod:`xrdclient.auth` build ``cred``; this class only
    frames it.
    """

    __slots__ = ("cred", "credtype")
    opcode = c.kXR_auth
    idempotent = False

    def __init__(self, credtype: str, cred: bytes) -> None:
        """One round of ``credtype`` (``gsi``, ``krb5``, ``ztn``, ...) carrying ``cred``.

        Raises :class:`ValueError` for a protocol name past the 4-byte field,
        rather than truncating it into a different protocol's name.
        """
        if len(credtype.encode()) > 4:
            raise ValueError(f"credtype must be <= 4 bytes, got {credtype!r}")
        self.credtype = credtype
        self.cred = cred

    def params(self, w: Writer) -> None:
        """``reserved[12] credtype[4]``, the protocol name NUL-padded."""
        w.zeros(12).padded(self.credtype, 4)

    def payload(self) -> bytes:
        """The credential blob of this round, opaque to the protocol layer."""
        return self.cred

    def __repr__(self) -> str:
        """The protocol and the credential's *length* - never its bytes."""
        return f"Auth(credtype={self.credtype!r}, cred=<{len(self.cred)} bytes>)"


class Ping(Request):
    """``kXR_ping``: a liveness probe with an empty ``kXR_ok`` answer."""

    __slots__ = ()
    opcode = c.kXR_ping

    def header_params(self) -> bytes:
        """All 16 bytes reserved; one shared constant, no allocation."""
        return _ZERO_PARAMS


class EndSession(Request):
    """``kXR_endsess`` - graceful session teardown.

    Ends the session ``sessid`` names, letting the server release its open
    files at once instead of waiting for the socket to time out. Sent by
    :meth:`SessionMachine.close` with the session's own id.
    """

    __slots__ = ("sessid",)
    opcode = c.kXR_endsess
    idempotent = False

    def __init__(self, sessid: bytes = b"") -> None:
        """End ``sessid`` (the 16 bytes ``kXR_login`` returned)."""
        self.sessid = sessid

    def params(self, w: Writer) -> None:
        """``sessid[16]``."""
        w.padded(self.sessid, 16)


class Bind(Request):
    """``kXR_bind`` - attach an extra data connection to a session.

    Sent instead of ``kXR_login`` on a new socket; the reply is the path id
    (``substreamid``) that later reads and writes name to use this socket.
    Data paths multiply bandwidth on links where one TCP stream is the
    bottleneck, as XrdCl's ``XRD_SUBSTREAMSPERCHANNEL`` does.
    """

    __slots__ = ("sessid",)
    opcode = c.kXR_bind
    idempotent = False

    def __init__(self, sessid: bytes) -> None:
        """Bind to the session whose id ``kXR_login`` returned on the control link."""
        self.sessid = sessid

    def params(self, w: Writer) -> None:
        """``sessid[16]``."""
        w.padded(self.sessid, 16)


class Set(Request):
    """``kXR_set`` - set a server-side client property.

    The parameter area is all reserved (the spec's ``modifier`` byte is 0);
    the directive is plain text - ``appid <name>`` or ``monitor ...``.
    """

    __slots__ = ("data",)
    opcode = c.kXR_set
    signed = True
    idempotent = False

    def __init__(self, data: str) -> None:
        """Send the directive ``data``, ``"<what> <value>"``."""
        self.data = data

    def payload(self) -> bytes:
        """The directive text."""
        return self.data.encode("utf-8")


class Sigver(Request):
    """``kXR_sigver`` - the signature frame that prefixes a signed request.

    When the server's security level requires it, every :attr:`signed`
    request is sent right behind one of these: a SHA-256 over the sequence
    number, the request header and (unless ``nodata``) its body. ``seqno``
    rises by one per signed request on a session so a captured frame cannot
    be replayed; the machine owns that counter.
    """

    __slots__ = ("crypto", "expectrid", "nodata", "seqno", "signature")
    opcode = c.kXR_sigver

    def __init__(
        self,
        expectrid: int,
        seqno: int,
        signature: bytes,
        crypto: int | None = None,
        nodata: bool = False,
    ) -> None:
        """Sign the request with opcode ``expectrid``.

        ``nodata`` sets ``kXR_nodata``: the signature leaves out the body,
        which is how a write is signed without hashing its data when the
        server did not ask for ``kXR_secOData``.
        """
        self.expectrid = expectrid
        self.seqno = seqno
        self.signature = signature
        self.crypto = c.kXR_SHA256_sig if crypto is None else crypto
        self.nodata = nodata

    def params(self, w: Writer) -> None:
        """``expectrid[2] version[1] flags[1] seqno[8] crypto[1] reserved[3]``."""
        w.u16(self.expectrid).u8(0).u8(c.kXR_nodata_sig if self.nodata else 0)
        w.u64(self.seqno).u8(self.crypto).zeros(3)

    def payload(self) -> bytes:
        """The signature bytes the security protocol produced."""
        return self.signature

    def __repr__(self) -> str:
        """The signed opcode by name and the sequence number, not the signature."""
        return f"Sigver(expectrid={c.request_name(self.expectrid)}, seqno={self.seqno})"
