"""Server query and staging request encoders: ``kXR_query`` and ``kXR_prepare``.

Part of :mod:`xrdclient.proto.requests`, which re-exports both classes; import
them from there. They are separate from the namespace encoders because they
ask the *server* or its storage system something rather than naming one entry:
``kXR_query`` multiplexes checksums, space, configuration, opaque plug-in
calls and more behind one ``infotype`` code, and ``kXR_prepare`` stages,
evicts or cancels a whole list of paths at once (the tape interface). Both
carry free-form payloads whose meaning the caller - ``client.filesystem``,
``copy.tpc``, the compat layer - supplies.

Layouts follow nginx-xrootd ``wire_core_requests.h``; cross-checked against
go-hep ``xrootd/xrdproto/<op>`` and XRootD.jl ``Wire/requests.jl``. Every
``params`` method writes exactly 16 bytes (header offsets 4..19).

The hook contract is documented on :class:`~xrdclient.proto.frames.Request`.
"""

from __future__ import annotations

from collections.abc import Sequence

from . import constants as c
from .buffer import Writer
from .frames import Request


class Query(Request):
    """``kXR_query``: one ``kXR_Q*`` question, its argument in the body.

    Replayable: the infotypes read state, and a ``kXR_Qopaque*`` plug-in
    query is treated the same, so a plug-in with side effects must tolerate
    a repeat after a reconnect.
    The reply has no size known in advance, so there is no :meth:`reply_cap`.
    """

    __slots__ = ("args", "fhandle", "infotype")
    opcode = c.kXR_query

    def __init__(
        self, infotype: int, args: str | bytes = b"", fhandle: bytes = c.NULL_FHANDLE
    ) -> None:
        """Ask ``infotype`` about ``args`` (a path, a config key list, ...).

        ``fhandle`` addresses an open file for the infotypes that take one
        (``kXR_Qvisa``, ``kXR_Qopaqug``); otherwise it stays null.
        """
        self.infotype = infotype
        self.args = args
        self.fhandle = fhandle

    def params(self, w: Writer) -> None:
        """``infotype[2] reserved[2] fhandle[4] reserved[8]``."""
        w.u16(self.infotype).zeros(2).padded(self.fhandle, 4).zeros(8)

    def payload(self) -> bytes:
        """The argument, UTF-8 encoded when given as text; opaque bytes pass through."""
        return self.args.encode("utf-8") if isinstance(self.args, str) else self.args

    def __repr__(self) -> str:
        """Infotype and argument."""
        return f"Query(infotype={self.infotype}, args={self.args!r})"


class Prepare(Request):
    """``kXR_prepare`` - stage or evict files.

    The interface to a tape-backed (MSS) server: stage paths to disk, cancel
    a staging request, or evict a staged copy. Not idempotent: each call
    may queue a new request on the storage system and return a new request
    id that a later cancel or query names.
    """

    __slots__ = ("extended", "options", "paths", "port", "priority")
    opcode = c.kXR_prepare
    signed = True
    idempotent = False

    def __init__(
        self,
        paths: Sequence[str],
        options: int = 0,
        priority: int = 0,
        port: int = 0,
        extended: int = 0,
    ) -> None:
        """Prepare every path in ``paths`` with ``kXR_stage``-style ``options``.

        ``priority`` is 0-3 (``kXR_*`` "prty"); ``port`` is the UDP port of a
        notification listener and is effectively unused today. For a cancel,
        ``paths`` starts with the request id the stage returned.
        """
        self.paths = list(paths)
        self.options = options
        self.priority = priority
        self.port = port
        #: ``optionX``, the half-word the later options live in - ``kXR_evict``.
        self.extended = extended

    def params(self, w: Writer) -> None:
        """``options[1] prty[1] port[2] optionX[2] reserved[10]``."""
        w.u8(self.options).u8(self.priority).u16(self.port).u16(self.extended).zeros(10)

    def payload(self) -> bytes:
        """The paths, newline-separated."""
        return "\n".join(self.paths).encode("utf-8")

    def __repr__(self) -> str:
        """Path count and options; a bulk stage may name thousands of paths."""
        return f"Prepare(paths={len(self.paths)}, options=0x{self.options:02x})"
